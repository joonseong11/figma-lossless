from __future__ import annotations

import mimetypes
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .util import read_json, resolve_within, safe_slug, sha256_file, write_json


DEFAULT_MAX_ASSET_BYTES = 10 * 1024 * 1024
FIGMA_ASSET_PATH = re.compile(r"^/api/mcp/asset/[A-Za-z0-9._-]+$")


class _FigmaOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_asset_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def vendor_assets(
    bundle_dir: Path,
    timeout_seconds: int = 30,
    max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES,
) -> dict[str, Any]:
    """Freeze short-lived Figma MCP asset URLs into the compiled bundle."""

    bundle_dir = bundle_dir.resolve()
    manifest = read_json(bundle_dir / "manifest.json")
    asset_dir = resolve_within(bundle_dir, "assets", label="asset directory")
    asset_dir.mkdir(parents=True, exist_ok=True)
    all_assets: list[dict[str, Any]] = []
    downloaded = 0
    failures = []

    for item in manifest.get("screens", []):
        screen_path = resolve_within(
            bundle_dir, item["compiledPath"], label="compiled screen"
        )
        screen = read_json(screen_path)
        for index, asset in enumerate(screen.get("assets", []), start=1):
            source_url = asset.get("sourceUrl")
            if not source_url:
                failures.append(
                    {
                        "screenNodeId": screen["nodeId"],
                        "variable": asset.get("variable"),
                        "reason": "missing URL",
                    }
                )
                all_assets.append(asset)
                continue
            extension = Path(urllib.parse.urlparse(source_url).path).suffix
            if not extension:
                extension = (
                    mimetypes.guess_extension(asset.get("mimeType", ""))
                    or ".bin"
                )
            filename = (
                f"{safe_slug(screen['nodeId'])}-{index:02d}-"
                f"{safe_slug(asset.get('variable', 'asset'))}{extension}"
            )
            target = asset_dir / filename
            try:
                _download_asset(
                    source_url,
                    target,
                    timeout_seconds=timeout_seconds,
                    max_bytes=max_asset_bytes,
                    expected_format=asset.get("format", extension.lstrip(".")),
                )
                asset["localPath"] = str(target.relative_to(bundle_dir))
                asset["sha256"] = sha256_file(target)
                asset["sizeBytes"] = target.stat().st_size
                downloaded += 1
            except Exception as error:  # network failures belong in the manifest
                failures.append(
                    {
                        "screenNodeId": screen["nodeId"],
                        "variable": asset.get("variable"),
                        "reason": type(error).__name__,
                    }
                )
            all_assets.append(asset)
        write_json(screen_path, screen)

    write_json(bundle_dir / "assets-manifest.json", all_assets)
    result = {
        "requested": len(all_assets),
        "downloaded": downloaded,
        "failed": len(failures),
        "failures": failures,
    }
    write_json(bundle_dir / "asset-vendor-result.json", result)
    return result


def _validate_asset_url(value: str) -> None:
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.figma.com"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not FIGMA_ASSET_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("Only canonical HTTPS Figma MCP asset URLs are allowed")


def _download_asset(
    source_url: str,
    target: Path,
    *,
    timeout_seconds: int,
    max_bytes: int,
    expected_format: str,
) -> None:
    _validate_asset_url(source_url)
    request = urllib.request.Request(
        source_url,
        headers={"User-Agent": "figma-lossless/0.6"},
    )
    opener = urllib.request.build_opener(_FigmaOnlyRedirectHandler())
    chunks = []
    received = 0
    with opener.open(request, timeout=timeout_seconds) as response:
        _validate_asset_url(response.geturl())
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError("Figma asset exceeds the configured byte limit")
        while True:
            chunk = response.read(min(64 * 1024, max_bytes - received + 1))
            if not chunk:
                break
            received += len(chunk)
            if received > max_bytes:
                raise ValueError("Figma asset exceeds the configured byte limit")
            chunks.append(chunk)
    payload = b"".join(chunks)
    _validate_asset_payload(payload, expected_format)
    target.write_bytes(payload)


def _validate_asset_payload(payload: bytes, expected_format: str) -> None:
    format_name = expected_format.lower().lstrip(".")
    signatures = {
        "png": (b"\x89PNG\r\n\x1a\n",),
        "jpg": (b"\xff\xd8\xff",),
        "jpeg": (b"\xff\xd8\xff",),
        "gif": (b"GIF87a", b"GIF89a"),
        "webp": (b"RIFF",),
    }
    if format_name in signatures and not payload.startswith(signatures[format_name]):
        raise ValueError(f"Downloaded bytes are not a valid {format_name} asset")
    if format_name == "webp" and payload[8:12] != b"WEBP":
        raise ValueError("Downloaded bytes are not a valid webp asset")
    if format_name != "svg":
        return
    prefix = payload[: 1024 * 1024].decode("utf-8", errors="ignore")
    lowered = prefix.lower()
    if "<svg" not in lowered:
        raise ValueError("Downloaded bytes are not a valid svg asset")
    active_patterns = (
        r"<script\b",
        r"<foreignobject\b",
        r"\son[a-z]+\s*=",
        r"(?:href|src)\s*=\s*[\"'](?:javascript:|https?:|//|data:text/html)",
    )
    if any(re.search(pattern, lowered) for pattern in active_patterns):
        raise ValueError("Active or externally-referencing SVG content is rejected")
