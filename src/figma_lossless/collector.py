from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NamedTuple

from .util import read_json, resolve_within, safe_slug, sha256_file, write_json


DEFAULT_BASE_URL = "https://api.figma.com"
DEFAULT_TOKEN_FILE = Path.home() / ".figma-token"
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_FACTOR = 2.0
_ALLOWED_API_HOST = "api.figma.com"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# `KEY=value` names a token file may use to name a Figma credential. Names
# must be unambiguously Figma-scoped (FIGMA_TOKEN, FIGMA_READ_TOKEN,
# FIGMA_API_TOKEN, ...) so a shared .env can never leak an unrelated secret.
ACCEPTED_TOKEN_KEY_PATTERN = re.compile(r"^FIGMA(?:_[A-Z0-9]+)*_?TOKEN$")
_EXPORT_PREFIX = re.compile(r"^export\s+")


class CollectorError(ValueError):
    """Input/config or unrecoverable transport error while collecting evidence."""


class HTTPResponse(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes


Transport = Callable[[str, dict[str, str], float], HTTPResponse]


@dataclass
class CollectOptions:
    file_key: str
    output_dir: Path
    node_ids: list[str] = field(default_factory=list)
    manifest_path: Path | None = None
    token_file: Path | None = None
    base_url: str = DEFAULT_BASE_URL
    transport: Transport | None = None
    timeout_seconds: float = 30.0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR
    sleep: Callable[[float], None] = time.sleep
    include_images: bool = False
    image_transport: Transport | None = None


def resolve_token(token_file: Path | None, env: dict[str, str] | None = None) -> str:
    path = token_file or DEFAULT_TOKEN_FILE
    if path.exists():
        token = _parse_token_file(path.read_text(encoding="utf-8"))
        if token:
            return token
    env_map = os.environ if env is None else env
    value = (env_map.get("FIGMA_TOKEN") or "").strip()
    if value:
        return value
    raise CollectorError(
        f"No Figma token found (checked {path} and the FIGMA_TOKEN environment variable)"
    )


def _parse_token_file(content: str) -> str | None:
    """Read a token file that is either a bare token or a Figma `KEY=value`.

    A token file is frequently a shared `.env`. Taking the first value in one
    would send whatever secret happens to sit at the top — an AWS key, a
    database password — to api.figma.com, so an unrecognised key is refused
    rather than guessed at.
    """

    bare: str | None = None
    foreign_keys: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Token files are often written to be `source`-able by a shell.
        line = _EXPORT_PREFIX.sub("", line, count=1)
        if "=" in line:
            key, _, value = line.partition("=")
            if ACCEPTED_TOKEN_KEY_PATTERN.match(key.strip().upper()):
                value = value.strip().strip("'\"")
                if value:
                    return value
                continue
            foreign_keys.append(key.strip())
            continue
        if bare is None:
            bare = line.strip("'\"")
    if foreign_keys:
        raise CollectorError(
            "Token file holds unrecognised assignments "
            f"({', '.join(sorted(set(foreign_keys)))}) and no "
            "FIGMA_TOKEN / FIGMA_*_TOKEN entry; refusing to "
            "send an unrelated secret to the Figma API"
        )
    return bare or None


def normalize_node_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise CollectorError("Empty Figma node id")
    if ":" in value:
        return value
    if "-" in value:
        return value.replace("-", ":", 1)
    raise CollectorError(f"Invalid Figma node id: {value}")


def _resolve_expected_node_ids(
    node_ids: list[str], manifest_path: Path | None
) -> list[str]:
    ids: set[str] = set()
    for raw in node_ids:
        raw = raw.strip()
        if raw:
            ids.add(normalize_node_id(raw))
    if manifest_path is not None:
        manifest = read_json(manifest_path)
        for raw in manifest.get("expectedNodeIds", []):
            ids.add(normalize_node_id(str(raw)))
    return sorted(ids)


def _validate_figma_api_url(value: str) -> None:
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != _ALLOWED_API_HOST
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise CollectorError("Only https://api.figma.com requests are allowed")


class _FigmaApiOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_figma_api_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_FigmaApiOnlyRedirectHandler())


def default_transport(url: str, headers: dict[str, str], timeout: float) -> HTTPResponse:
    _validate_figma_api_url(url)
    request = urllib.request.Request(url, headers=headers)
    try:
        with _opener.open(request, timeout=timeout) as response:
            _validate_figma_api_url(response.geturl())
            return HTTPResponse(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as error:
        return HTTPResponse(error.code, dict(error.headers or {}), error.read())


def _header(headers: dict[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _is_retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status < 600


def _backoff_delay(
    response: HTTPResponse | None, attempt: int, base: float, factor: float
) -> float:
    if response is not None:
        retry_after = _header(response.headers, "Retry-After")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
    return base * (factor ** (attempt - 1))


def _request_with_retry(
    transport: Transport,
    url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    backoff_base_seconds: float,
    backoff_factor: float,
    sleep: Callable[[float], None],
    timeout: float,
) -> HTTPResponse:
    attempt = 1
    while True:
        try:
            response = transport(url, headers, timeout)
        except OSError as error:
            if attempt >= max_attempts:
                raise CollectorError(f"request to Figma API failed: {error}") from error
            sleep(_backoff_delay(None, attempt, backoff_base_seconds, backoff_factor))
            attempt += 1
            continue
        if not _is_retryable_status(response.status) or attempt >= max_attempts:
            return response
        sleep(_backoff_delay(response, attempt, backoff_base_seconds, backoff_factor))
        attempt += 1


def _file_url(base_url: str, file_key: str) -> str:
    key = urllib.parse.quote(file_key, safe="")
    return f"{base_url}/v1/files/{key}?depth=1"


def _node_url(base_url: str, file_key: str, node_id: str, version: str) -> str:
    key = urllib.parse.quote(file_key, safe="")
    query = urllib.parse.urlencode(
        {"ids": node_id, "version": version, "geometry": "paths"}
    )
    return f"{base_url}/v1/files/{key}/nodes?{query}"


def _images_url(
    base_url: str, file_key: str, node_ids: list[str], version: str
) -> str:
    key = urllib.parse.quote(file_key, safe="")
    query = urllib.parse.urlencode(
        {
            "ids": ",".join(node_ids),
            "format": "png",
            "scale": "1",
            "version": version,
        }
    )
    return f"{base_url}/v1/images/{key}?{query}"


def _validate_node_payload(body: bytes, node_id: str) -> tuple[bool, str | None]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, "response is not valid JSON"
    if not isinstance(data, dict):
        return False, "response is not a JSON object"
    if data.get("err"):
        return False, f"figma api error: {data['err']}"
    nodes = data.get("nodes")
    if not isinstance(nodes, dict) or nodes.get(node_id) is None:
        return False, f"node {node_id} missing from response"
    return True, None


def _validate_images_payload(
    body: bytes,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "response is not valid JSON"
    if not isinstance(data, dict):
        return None, "response is not a JSON object"
    if data.get("err"):
        return None, f"figma api error: {data['err']}"
    images = data.get("images")
    if not isinstance(images, dict):
        return None, "response has no images map"
    return images, None


def _validate_image_payload(body: bytes) -> str | None:
    if len(body) > MAX_IMAGE_BYTES:
        return "rendered image exceeds the configured byte limit"
    if not body.startswith(_PNG_SIGNATURE):
        return "downloaded bytes are not a valid PNG image"
    return None


def _validate_image_url(value: str) -> None:
    """Check scheme/credential hygiene on a temporary Figma render URL.

    Rendered-image URLs are pre-signed storage links on a host Figma picks
    per-request, so — unlike `_validate_figma_api_url` — this cannot pin a
    single hostname. It still refuses non-https and embedded credentials.
    """

    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise CollectorError(
            "Only https rendered-image URLs without embedded credentials are allowed"
        )


class _RenderedImageRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_image_opener = urllib.request.build_opener(_RenderedImageRedirectHandler())


def default_image_transport(
    url: str, headers: dict[str, str], timeout: float
) -> HTTPResponse:
    """Download a temporary rendered-image URL. Never sends the Figma token.

    The URL's host is not api.figma.com, so this never reuses `default_transport`
    or its headers — a bearer token has no business leaving api.figma.com.
    """

    _validate_image_url(url)
    request = urllib.request.Request(url, headers=headers)
    try:
        with _image_opener.open(request, timeout=timeout) as response:
            _validate_image_url(response.geturl())
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    oversized = int(content_length) > MAX_IMAGE_BYTES
                except ValueError:
                    oversized = False
                if oversized:
                    raise CollectorError(
                        "rendered image exceeds the configured byte limit"
                    )
            chunks = []
            received = 0
            while True:
                chunk = response.read(min(64 * 1024, MAX_IMAGE_BYTES - received + 1))
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_IMAGE_BYTES:
                    raise CollectorError(
                        "rendered image exceeds the configured byte limit"
                    )
                chunks.append(chunk)
            return HTTPResponse(
                response.status, dict(response.headers), b"".join(chunks)
            )
    except urllib.error.HTTPError as error:
        return HTTPResponse(error.code, dict(error.headers or {}), error.read())


class FigmaCollector:
    def __init__(self, options: CollectOptions):
        self.options = options
        self.output_dir = options.output_dir.resolve()
        self.transport = options.transport or default_transport
        self.image_transport = options.image_transport or default_image_transport

    def collect(self) -> dict[str, Any]:
        """Pin the file version, then fetch each expected node exactly once."""

        token = resolve_token(self.options.token_file)
        expected_ids = _resolve_expected_node_ids(
            self.options.node_ids, self.options.manifest_path
        )
        if not expected_ids:
            raise CollectorError(
                "No expected node ids provided (use --node-ids and/or --manifest)"
            )

        nodes_dir = resolve_within(self.output_dir, "rest/nodes", label="nodes directory")
        nodes_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = resolve_within(
            self.output_dir, "rest/collection-manifest.json", label="collection manifest"
        )

        headers = {"X-Figma-Token": token, "Accept": "application/json"}
        file_response = self._request(
            _file_url(self.options.base_url, self.options.file_key), headers
        )
        if file_response.status >= 400:
            raise CollectorError(
                f"Failed to fetch Figma file metadata: HTTP {file_response.status}"
            )
        file_data = json.loads(file_response.body.decode("utf-8"))
        file_version = file_data.get("version")
        file_name = file_data.get("name")
        if not file_version:
            raise CollectorError("Figma file response did not include a version")

        previous_manifest = self._load_previous_manifest(manifest_path)
        can_resume = (
            previous_manifest is not None
            and previous_manifest.get("fileVersion") == file_version
        )

        per_node: dict[str, Any] = {}
        collected_ids: list[str] = []
        failures: list[dict[str, str]] = []

        for node_id in expected_ids:
            node_path = resolve_within(
                self.output_dir,
                f"rest/nodes/{safe_slug(node_id)}.json",
                label="node evidence path",
            )
            body, http_status = self._obtain_node_body(
                node_id,
                node_path,
                previous_manifest if can_resume else None,
                headers,
                file_version,
            )
            if body is None:
                failures.append({"id": node_id, "reason": http_status})
                continue
            per_node[node_id] = {
                "path": str(node_path.relative_to(self.output_dir)),
                "sha256": sha256_file(node_path),
                "httpStatus": http_status,
            }
            valid, reason = _validate_node_payload(body, node_id)
            if valid:
                collected_ids.append(node_id)
            else:
                failures.append({"id": node_id, "reason": reason})

        complete = set(collected_ids) == set(expected_ids) and not failures
        manifest = {
            "fileKey": self.options.file_key,
            "fileName": file_name,
            "fileVersion": file_version,
            "expectedNodeIds": sorted(expected_ids),
            "collectedNodeIds": sorted(collected_ids),
            "perNode": per_node,
            "complete": complete,
            "failures": failures,
        }
        if self.options.include_images:
            images_result = self._collect_images(collected_ids, file_version, headers)
            manifest["imagesComplete"] = images_result["complete"]
            manifest["imageFailures"] = images_result["failures"]
            for node_id, image_entry in images_result["perNode"].items():
                per_node[node_id]["image"] = image_entry
        write_json(manifest_path, manifest)
        return manifest

    def _collect_images(
        self,
        collected_ids: list[str],
        file_version: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """Render every collected node to a PNG pinned to the collected version.

        Node evidence and rendered images are independent proofs: a failure
        here never touches `collectedNodeIds`/`complete`. The pinned
        `version` query param is never dropped and retried unpinned — any
        failure fetching the mapping (including a rejected `version`) fails
        the whole image batch loudly instead of silently falling back.
        """

        if not collected_ids:
            return {"complete": True, "perNode": {}, "failures": []}

        images_dir = resolve_within(
            self.output_dir, "rest/images", label="images directory"
        )
        images_dir.mkdir(parents=True, exist_ok=True)

        url = _images_url(
            self.options.base_url, self.options.file_key, collected_ids, file_version
        )
        try:
            response = self._request(url, headers)
        except CollectorError as error:
            reason = str(error)
            return {
                "complete": False,
                "perNode": {},
                "failures": [
                    {"id": node_id, "reason": reason} for node_id in collected_ids
                ],
            }
        if response.status >= 400:
            reason = f"HTTP {response.status}"
            return {
                "complete": False,
                "perNode": {},
                "failures": [
                    {"id": node_id, "reason": reason} for node_id in collected_ids
                ],
            }
        images, reason = _validate_images_payload(response.body)
        if images is None:
            return {
                "complete": False,
                "perNode": {},
                "failures": [
                    {"id": node_id, "reason": reason} for node_id in collected_ids
                ],
            }

        per_node: dict[str, Any] = {}
        failures: list[dict[str, str]] = []
        image_headers = {"User-Agent": "figma-lossless/0.6"}
        for node_id in collected_ids:
            image_url = images.get(node_id)
            if not image_url:
                failures.append(
                    {"id": node_id, "reason": "no rendered image returned for node"}
                )
                continue
            try:
                image_response = self._image_request(image_url, image_headers)
            except CollectorError as error:
                failures.append({"id": node_id, "reason": str(error)})
                continue
            if image_response.status >= 400:
                failures.append(
                    {"id": node_id, "reason": f"HTTP {image_response.status}"}
                )
                continue
            payload_reason = _validate_image_payload(image_response.body)
            if payload_reason is not None:
                failures.append({"id": node_id, "reason": payload_reason})
                continue
            image_path = resolve_within(
                self.output_dir,
                f"rest/images/{safe_slug(node_id)}.png",
                label="rendered image path",
            )
            image_path.write_bytes(image_response.body)
            per_node[node_id] = {
                "path": str(image_path.relative_to(self.output_dir)),
                "sha256": sha256_file(image_path),
            }

        complete = len(per_node) == len(collected_ids)
        return {"complete": complete, "perNode": per_node, "failures": failures}

    def _image_request(self, url: str, headers: dict[str, str]) -> HTTPResponse:
        return _request_with_retry(
            self.image_transport,
            url,
            headers,
            max_attempts=self.options.max_attempts,
            backoff_base_seconds=self.options.backoff_base_seconds,
            backoff_factor=self.options.backoff_factor,
            sleep=self.options.sleep,
            timeout=self.options.timeout_seconds,
        )

    def _obtain_node_body(
        self,
        node_id: str,
        node_path: Path,
        previous_manifest: dict[str, Any] | None,
        headers: dict[str, str],
        file_version: str,
    ) -> tuple[bytes | None, str | int]:
        if previous_manifest is not None and node_path.exists():
            previous_entry = (previous_manifest.get("perNode") or {}).get(node_id)
            if previous_entry and previous_entry.get("sha256") == sha256_file(node_path):
                return node_path.read_bytes(), previous_entry.get("httpStatus", 200)

        url = _node_url(self.options.base_url, self.options.file_key, node_id, file_version)
        try:
            response = self._request(url, headers)
        except CollectorError as error:
            return None, str(error)
        if response.status >= 400:
            return None, f"HTTP {response.status}"
        node_path.write_bytes(response.body)
        return response.body, response.status

    def _load_previous_manifest(self, manifest_path: Path) -> dict[str, Any] | None:
        if not manifest_path.exists():
            return None
        try:
            manifest = read_json(manifest_path)
        except (OSError, json.JSONDecodeError):
            return None
        return manifest if isinstance(manifest, dict) else None

    def _request(self, url: str, headers: dict[str, str]) -> HTTPResponse:
        return _request_with_retry(
            self.transport,
            url,
            headers,
            max_attempts=self.options.max_attempts,
            backoff_base_seconds=self.options.backoff_base_seconds,
            backoff_factor=self.options.backoff_factor,
            sleep=self.options.sleep,
            timeout=self.options.timeout_seconds,
        )
