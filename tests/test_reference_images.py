from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from figma_lossless.collector import (
    MAX_IMAGE_BYTES,
    CollectOptions,
    FigmaCollector,
    HTTPResponse,
)
from figma_lossless.compiler import CompileOptions, DesignCompiler
from figma_lossless.util import read_json, safe_slug, sha256_file, write_json


# 1x1 white PNG — the smallest valid rendered-image payload.
PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)
PNG_BYTES = base64.b64decode(PNG_BASE64)


class FakeTransport:
    """Records every request and answers it via a test-supplied handler."""

    def __init__(self, handler):
        self.calls: list[str] = []
        self.headers_seen: list[dict[str, str]] = []
        self._handler = handler

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> HTTPResponse:
        self.calls.append(url)
        self.headers_seen.append(dict(headers))
        return self._handler(url)


def file_body(version: str, name: str = "Demo File") -> bytes:
    return json.dumps({"version": version, "name": name}).encode("utf-8")


def node_body(node_id: str) -> bytes:
    return json.dumps(
        {"nodes": {node_id: {"document": {"id": node_id, "type": "FRAME"}}}}
    ).encode("utf-8")


def images_body(images: dict[str, str | None], err: str | None = None) -> bytes:
    return json.dumps({"err": err, "images": images}).encode("utf-8")


# ---------------------------------------------------------------------------
# Collector: `--include-images`
# ---------------------------------------------------------------------------


class CollectorImagesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "evidence"
        self.token_file = self.root / "token"
        self.token_file.write_text("figd_test_token", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _options(
        self, transport, node_ids, image_transport=None, **overrides
    ) -> CollectOptions:
        kwargs = dict(
            file_key="ABC123",
            output_dir=self.output,
            node_ids=node_ids,
            transport=transport,
            image_transport=image_transport,
            token_file=self.token_file,
            include_images=True,
            sleep=lambda seconds: None,
        )
        kwargs.update(overrides)
        return CollectOptions(**kwargs)

    # ---- (a) happy path ---------------------------------------------------

    def test_happy_path_pins_version_batches_ids_and_stores_pngs(self) -> None:
        image_url = "https://s3-figma-renders.example.com/render.png?sig=abc"

        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.startswith("/v1/images/"):
                params = parse_qs(parsed.query)
                self.assertEqual(params["ids"], ["1:1,1:2"])
                self.assertEqual(params["version"], ["42"])
                self.assertEqual(params["format"], ["png"])
                return HTTPResponse(
                    200, {}, images_body({"1:1": image_url, "1:2": image_url})
                )
            if parsed.path.endswith("/nodes"):
                params = parse_qs(parsed.query)
                return HTTPResponse(200, {}, node_body(params["ids"][0]))
            self.assertEqual(parsed.path, "/v1/files/ABC123")
            return HTTPResponse(200, {}, file_body(version="42"))

        transport = FakeTransport(handler)

        def image_handler(url: str) -> HTTPResponse:
            self.assertEqual(url, image_url)
            return HTTPResponse(200, {}, PNG_BYTES)

        image_transport = FakeTransport(image_handler)
        options = self._options(
            transport, ["1:1", "1:2"], image_transport=image_transport
        )
        manifest = FigmaCollector(options).collect()

        self.assertTrue(manifest["complete"])
        self.assertTrue(manifest["imagesComplete"])
        self.assertEqual(manifest["imageFailures"], [])
        for node_id in ("1:1", "1:2"):
            image_entry = manifest["perNode"][node_id]["image"]
            image_path = self.output / image_entry["path"]
            self.assertTrue(image_path.exists())
            self.assertEqual(image_entry["sha256"], sha256_file(image_path))
            self.assertEqual(image_path.read_bytes(), PNG_BYTES)
        self.assertTrue(
            (self.output / "rest" / "images" / "1-2.png").exists()
        )
        # The temporary render host never receives the Figma API token.
        for headers in image_transport.headers_seen:
            self.assertNotIn("X-Figma-Token", headers)

        on_disk = read_json(self.output / "rest" / "collection-manifest.json")
        self.assertEqual(on_disk, manifest)

    # ---- (b) null image URL ------------------------------------------------

    def test_null_image_url_is_recorded_as_a_failure_without_touching_node_completeness(
        self,
    ) -> None:
        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.startswith("/v1/images/"):
                return HTTPResponse(200, {}, images_body({"1:1": None}))
            if parsed.path.endswith("/nodes"):
                params = parse_qs(parsed.query)
                return HTTPResponse(200, {}, node_body(params["ids"][0]))
            return HTTPResponse(200, {}, file_body(version="7"))

        transport = FakeTransport(handler)
        image_transport = FakeTransport(lambda url: HTTPResponse(200, {}, PNG_BYTES))
        options = self._options(transport, ["1:1"], image_transport=image_transport)
        manifest = FigmaCollector(options).collect()

        self.assertTrue(manifest["complete"])
        self.assertFalse(manifest["imagesComplete"])
        self.assertEqual(len(manifest["imageFailures"]), 1)
        self.assertEqual(manifest["imageFailures"][0]["id"], "1:1")
        self.assertNotIn("image", manifest["perNode"]["1:1"])
        # A null URL is never worth attempting to download.
        self.assertEqual(image_transport.calls, [])

    # ---- (c) non-PNG bytes ---------------------------------------------------

    def test_non_png_bytes_are_rejected(self) -> None:
        image_url = "https://cdn.example.com/render.png"

        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.startswith("/v1/images/"):
                return HTTPResponse(200, {}, images_body({"1:1": image_url}))
            if parsed.path.endswith("/nodes"):
                params = parse_qs(parsed.query)
                return HTTPResponse(200, {}, node_body(params["ids"][0]))
            return HTTPResponse(200, {}, file_body(version="3"))

        transport = FakeTransport(handler)
        image_transport = FakeTransport(lambda url: HTTPResponse(200, {}, b"not a png"))
        options = self._options(transport, ["1:1"], image_transport=image_transport)
        manifest = FigmaCollector(options).collect()

        self.assertTrue(manifest["complete"])
        self.assertFalse(manifest["imagesComplete"])
        self.assertIn("not a valid PNG", manifest["imageFailures"][0]["reason"])
        self.assertNotIn("image", manifest["perNode"]["1:1"])
        self.assertFalse((self.output / "rest" / "images" / "1-1.png").exists())

    # ---- (d) oversize ----------------------------------------------------

    def test_oversize_image_is_rejected(self) -> None:
        image_url = "https://cdn.example.com/render.png"
        oversized = b"\x00" * (MAX_IMAGE_BYTES + 1)

        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.startswith("/v1/images/"):
                return HTTPResponse(200, {}, images_body({"1:1": image_url}))
            if parsed.path.endswith("/nodes"):
                params = parse_qs(parsed.query)
                return HTTPResponse(200, {}, node_body(params["ids"][0]))
            return HTTPResponse(200, {}, file_body(version="3"))

        transport = FakeTransport(handler)
        image_transport = FakeTransport(lambda url: HTTPResponse(200, {}, oversized))
        options = self._options(transport, ["1:1"], image_transport=image_transport)
        manifest = FigmaCollector(options).collect()

        self.assertFalse(manifest["imagesComplete"])
        self.assertIn(
            "exceeds the configured byte limit", manifest["imageFailures"][0]["reason"]
        )
        self.assertFalse((self.output / "rest" / "images" / "1-1.png").exists())

    # ---- pinned version is never silently dropped -------------------------

    def test_images_endpoint_rejecting_the_pinned_version_is_a_loud_batch_failure(
        self,
    ) -> None:
        image_calls: list[str] = []

        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.startswith("/v1/images/"):
                image_calls.append(parsed.query)
                return HTTPResponse(
                    200, {}, images_body({}, err="Invalid version parameter")
                )
            if parsed.path.endswith("/nodes"):
                params = parse_qs(parsed.query)
                return HTTPResponse(200, {}, node_body(params["ids"][0]))
            return HTTPResponse(200, {}, file_body(version="55"))

        transport = FakeTransport(handler)
        image_transport = FakeTransport(lambda url: HTTPResponse(200, {}, PNG_BYTES))
        options = self._options(transport, ["1:1"], image_transport=image_transport)
        manifest = FigmaCollector(options).collect()

        self.assertTrue(manifest["complete"])
        self.assertFalse(manifest["imagesComplete"])
        self.assertIn(
            "Invalid version parameter", manifest["imageFailures"][0]["reason"]
        )
        # Exactly one attempt, still carrying the pinned version — never retried unpinned.
        self.assertEqual(len(image_calls), 1)
        self.assertIn("version=55", image_calls[0])
        self.assertEqual(image_transport.calls, [])

    # ---- (g) token hygiene -------------------------------------------------

    def test_token_never_appears_in_any_written_artifact(self) -> None:
        secret_token_file = self.root / "secret-token"
        secret_token_file.write_text(
            "FIGMA_TOKEN=figd_super_secret_value\n", encoding="utf-8"
        )
        image_url = "https://cdn.example.com/render.png"

        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.startswith("/v1/images/"):
                return HTTPResponse(200, {}, images_body({"1:1": image_url}))
            if parsed.path.endswith("/nodes"):
                params = parse_qs(parsed.query)
                return HTTPResponse(200, {}, node_body(params["ids"][0]))
            return HTTPResponse(200, {}, file_body(version="1"))

        transport = FakeTransport(handler)
        image_transport = FakeTransport(lambda url: HTTPResponse(200, {}, PNG_BYTES))
        options = self._options(
            transport, ["1:1"], image_transport=image_transport, token_file=secret_token_file
        )
        manifest = FigmaCollector(options).collect()

        self.assertTrue(manifest["imagesComplete"])
        self.assertNotIn("figd_super_secret_value", json.dumps(manifest))
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"figd_super_secret_value", path.read_bytes())
        for headers in image_transport.headers_seen:
            self.assertNotIn("figd_super_secret_value", json.dumps(headers))


# ---------------------------------------------------------------------------
# Compiler: wiring a validated REST render into referenceScreenshot
# ---------------------------------------------------------------------------


def box(x: float, y: float, width: float, height: float) -> dict[str, Any]:
    return {"x": x, "y": y, "width": width, "height": height}


def frame_node(node_id: str, **overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "name": "Frame",
        "type": "FRAME",
        "absoluteBoundingBox": box(0, 0, 200, 100),
    }
    node.update(overrides)
    return node


def write_rest_input(
    root: Path,
    documents: dict[str, dict[str, Any]],
    images: dict[str, bytes] | None = None,
) -> Path:
    """Write a collector-shaped canonical evidence directory.

    Mirrors the collector's own on-disk layout (`rest/nodes`, optionally
    `rest/images`, `rest/collection-manifest.json`) so the compiler is
    exercised against exactly the shape `figma-lossless collect` produces.
    """

    directory = root / "rest-input"
    nodes_dir = directory / "rest" / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    per_node: dict[str, Any] = {}
    for node_id, document in documents.items():
        path = nodes_dir / f"{safe_slug(node_id)}.json"
        write_json(
            path,
            {
                "name": "Fixture file",
                "version": "9001",
                "nodes": {
                    node_id: {
                        "document": document,
                        "components": {},
                        "componentSets": {},
                        "schemaVersion": 0,
                        "styles": {},
                    }
                },
            },
        )
        per_node[node_id] = {
            "path": str(path.relative_to(directory)),
            "sha256": sha256_file(path),
            "httpStatus": 200,
        }

    images = images or {}
    if images:
        images_dir = directory / "rest" / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        for node_id, payload in images.items():
            image_path = images_dir / f"{safe_slug(node_id)}.png"
            image_path.write_bytes(payload)
            per_node.setdefault(node_id, {})["image"] = {
                "path": str(image_path.relative_to(directory)),
                "sha256": sha256_file(image_path),
            }

    write_json(
        directory / "rest" / "collection-manifest.json",
        {
            "fileKey": "FILEKEY",
            "fileName": "Fixture file",
            "fileVersion": "9001",
            "expectedNodeIds": sorted(documents),
            "collectedNodeIds": sorted(documents),
            "perNode": per_node,
            "complete": True,
            "failures": [],
            "imagesComplete": bool(images) and set(images) >= set(documents),
            "imageFailures": [],
        },
    )
    return directory


class CompilerReferenceImageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _compile(self, rest_input: Path) -> dict[str, Any]:
        return DesignCompiler(
            CompileOptions(
                feature_id="images",
                input_dir=rest_input,
                output_dir=self.bundle,
                rest_input=rest_input,
            )
        ).compile()

    # ---- (e) wires when valid, leaves unset otherwise ----------------------

    def test_wires_reference_screenshot_only_for_screens_with_a_valid_image(
        self,
    ) -> None:
        rest_input = write_rest_input(
            self.root,
            {
                "1:1": frame_node("1:1", name="Screen One"),
                "2:1": frame_node("2:1", name="Screen Two"),
            },
            images={"1:1": PNG_BYTES},
        )
        manifest = self._compile(rest_input)
        screen_by_id = {item["nodeId"]: item for item in manifest["screens"]}
        screen_one = read_json(self.bundle / screen_by_id["1:1"]["compiledPath"])
        screen_two = read_json(self.bundle / screen_by_id["2:1"]["compiledPath"])

        self.assertIsNotNone(screen_one["referenceScreenshot"])
        self.assertEqual(
            screen_one["referenceScreenshotProvenance"], "figma-rest-render"
        )
        reference_path = self.bundle / screen_one["referenceScreenshot"]
        self.assertTrue(reference_path.exists())
        self.assertEqual(reference_path.read_bytes(), PNG_BYTES)

        self.assertIsNone(screen_two["referenceScreenshot"])
        self.assertIsNone(screen_two["referenceScreenshotProvenance"])

        accounting = read_json(self.bundle / "property-accounting.json")
        self.assertFalse(accounting["restCollection"]["imagesComplete"])
        self.assertEqual(
            accounting["restCollection"]["missingImageNodeIds"], ["2:1"]
        )

    def test_leaves_reference_screenshot_unset_when_no_images_were_collected(
        self,
    ) -> None:
        rest_input = write_rest_input(
            self.root, {"1:1": frame_node("1:1", name="Screen One")}
        )
        manifest = self._compile(rest_input)
        screen = read_json(self.bundle / manifest["screens"][0]["compiledPath"])
        self.assertIsNone(screen["referenceScreenshot"])
        self.assertIsNone(screen["referenceScreenshotProvenance"])

        accounting = read_json(self.bundle / "property-accounting.json")
        self.assertFalse(accounting["restCollection"]["imagesComplete"])
        self.assertEqual(
            accounting["restCollection"]["missingImageNodeIds"], ["1:1"]
        )

    # ---- (f) sha mismatch --------------------------------------------------

    def test_sha_mismatched_image_on_disk_is_not_wired_and_is_recorded(
        self,
    ) -> None:
        rest_input = write_rest_input(
            self.root,
            {"1:1": frame_node("1:1", name="Screen One")},
            images={"1:1": PNG_BYTES},
        )
        # Corrupt the file on disk after the manifest recorded its original hash.
        image_path = rest_input / "rest" / "images" / "1-1.png"
        image_path.write_bytes(PNG_BYTES + b"\x00")

        manifest = self._compile(rest_input)
        screen = read_json(self.bundle / manifest["screens"][0]["compiledPath"])

        self.assertIsNone(screen["referenceScreenshot"])
        self.assertIsNone(screen["referenceScreenshotProvenance"])
        self.assertFalse(screen["canonicalImageEvidence"]["valid"])
        self.assertEqual(screen["canonicalImageEvidence"]["reason"], "sha256 mismatch")

        accounting = read_json(self.bundle / "property-accounting.json")
        self.assertEqual(
            accounting["restCollection"]["missingImageNodeIds"], ["1:1"]
        )

    def test_missing_image_file_on_disk_is_not_wired_and_is_recorded(self) -> None:
        rest_input = write_rest_input(
            self.root,
            {"1:1": frame_node("1:1", name="Screen One")},
            images={"1:1": PNG_BYTES},
        )
        (rest_input / "rest" / "images" / "1-1.png").unlink()

        manifest = self._compile(rest_input)
        screen = read_json(self.bundle / manifest["screens"][0]["compiledPath"])

        self.assertIsNone(screen["referenceScreenshot"])
        self.assertFalse(screen["canonicalImageEvidence"]["valid"])
        self.assertEqual(screen["canonicalImageEvidence"]["reason"], "file missing")

    def test_compile_output_is_deterministic_across_reruns(self) -> None:
        rest_input = write_rest_input(
            self.root,
            {"1:1": frame_node("1:1", name="Screen One")},
            images={"1:1": PNG_BYTES},
        )
        first = self._compile(rest_input)
        second_bundle = self.root / "bundle-2"
        second = DesignCompiler(
            CompileOptions(
                feature_id="images",
                input_dir=rest_input,
                output_dir=second_bundle,
                rest_input=rest_input,
            )
        ).compile()
        screen_a = read_json(self.bundle / first["screens"][0]["compiledPath"])
        screen_b = read_json(second_bundle / second["screens"][0]["compiledPath"])
        self.assertEqual(
            screen_a["referenceScreenshotProvenance"],
            screen_b["referenceScreenshotProvenance"],
        )
        self.assertEqual(
            (self.bundle / screen_a["referenceScreenshot"]).read_bytes(),
            (second_bundle / screen_b["referenceScreenshot"]).read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
