"""The REST path hardcodes "assets": [] -- make sure that gap stays visible.

The Figma REST collector renders each node to a PNG for the reference
screenshot, but never resolves a paint's `imageRef` to the source image
bytes (that needs a second call to the files/images endpoint plus a
per-hash download, which the collector does not perform). So every
REST-evidenced screen compiles `"assets": []` unconditionally, and the
asset gate checks zero assets on every run -- even a screen full of icons.

Building real asset verification would mean teaching the collector to fetch
and hash fill images, which is out of scope here. What these tests pin
instead is that the gap cannot pass silently: every visible IMAGE fill the
compiler sees must be named in `propertyAccounting.unverifiedImageFills`,
rolled up into the accounting summary, and rendered as a visible warning in
the report -- so "0 assets checked" reads as "unverified", not "clean".
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from figma_lossless.compiler import (
    CanonicalMapper,
    build_accounting_summary,
)
from figma_lossless.report import render_report
from figma_lossless.util import write_json
from figma_lossless.validators import GateValidator, ValidateOptions

from test_canonical import box, frame_node, write_rest_input


IMAGE_NODE_ID = "1:2"
SCREEN_ID = "1:1"


def screen_with_image_fill() -> dict[str, Any]:
    return frame_node(
        SCREEN_ID,
        children=[
            {
                "id": IMAGE_NODE_ID,
                "name": "Photo",
                "type": "RECTANGLE",
                "absoluteBoundingBox": box(0, 0, 40, 40),
                "fills": [
                    {
                        "type": "IMAGE",
                        "visible": True,
                        "imageRef": "abc123hash",
                        "scaleMode": "FILL",
                    }
                ],
            }
        ],
    )


class ImageFillDetectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _screen(self) -> dict[str, Any]:
        rest_input = write_rest_input(self.root, {SCREEN_ID: screen_with_image_fill()})
        return CanonicalMapper(rest_input).map_screens()[0]

    def test_assets_still_compiles_empty_the_compiler_cannot_fetch_content(
        self,
    ) -> None:
        """Pin the current, honest behavior: no gate-loosening fabrication."""

        screen = self._screen()
        self.assertEqual(screen["assets"], [])

    def test_visible_image_fill_is_named_in_property_accounting(self) -> None:
        screen = self._screen()
        recorded = screen["propertyAccounting"]["unverifiedImageFills"]
        self.assertEqual(
            recorded, [{"nodeId": IMAGE_NODE_ID, "imageRef": "abc123hash"}]
        )

    def test_invisible_image_fill_is_not_recorded(self) -> None:
        document = screen_with_image_fill()
        document["children"][0]["fills"][0]["visible"] = False
        rest_input = write_rest_input(self.root, {SCREEN_ID: document})
        screen = CanonicalMapper(rest_input).map_screens()[0]
        self.assertEqual(screen["propertyAccounting"]["unverifiedImageFills"], [])

    def test_summary_rolls_up_screen_node_id(self) -> None:
        rest_input = write_rest_input(self.root, {SCREEN_ID: screen_with_image_fill()})
        mapper = CanonicalMapper(rest_input)
        screens = mapper.map_screens()
        summary = build_accounting_summary("feature", screens, mapper.collection)

        self.assertEqual(summary["counts"]["unverifiedImageFills"], 1)
        self.assertEqual(
            summary["unverifiedImageFills"],
            [
                {
                    "screenNodeId": SCREEN_ID,
                    "nodeId": IMAGE_NODE_ID,
                    "imageRef": "abc123hash",
                }
            ],
        )

    def test_no_image_fills_reports_zero_not_missing_key(self) -> None:
        rest_input = write_rest_input(self.root, {SCREEN_ID: frame_node(SCREEN_ID)})
        mapper = CanonicalMapper(rest_input)
        screens = mapper.map_screens()
        summary = build_accounting_summary("feature", screens, mapper.collection)

        self.assertEqual(summary["counts"]["unverifiedImageFills"], 0)
        self.assertEqual(summary["unverifiedImageFills"], [])


class ReportSurfacesTheGapTest(unittest.TestCase):
    """The report must not let "0 assets checked" read as a clean sheet."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.output = self.root / "report"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_unverified_image_fill_is_a_visible_warning_in_the_html(self) -> None:
        rest_input = write_rest_input(self.root, {SCREEN_ID: screen_with_image_fill()})
        from figma_lossless.compiler import CompileOptions, DesignCompiler

        DesignCompiler(
            CompileOptions(
                feature_id="asset-gap",
                input_dir=rest_input,
                output_dir=self.bundle,
                rest_input=rest_input,
            )
        ).compile()

        actual_path = self.root / "actual.json"
        write_json(
            actual_path,
            {
                "schemaVersion": "1.0",
                "featureId": "asset-gap",
                "provenance": "browser-capture",
                "screens": {
                    SCREEN_ID: {
                        "name": "Frame",
                        "route": "/",
                        "screenshot": None,
                        "elements": {},
                        "assets": {},
                    }
                },
                "components": {},
            },
        )
        config_path = self.root / "config.json"
        write_json(config_path, {"requireReferenceScreenshots": False})

        result = GateValidator(
            ValidateOptions(
                self.bundle, actual_path, self.output, config_path=config_path
            )
        ).validate()
        self.assertEqual(
            result["accounting"]["counts"]["unverifiedImageFills"], 1
        )
        # The asset gate itself must not be misread as a clean pass either --
        # this run has zero implementation elements, but the point stands
        # regardless of what the asset gate's own checked-count says: the
        # accounting panel must call the image fill out by name.
        html = render_report(result, self.output).read_text(encoding="utf-8")
        self.assertIn("Unverified REST image fills", html)
        self.assertIn(IMAGE_NODE_ID, html)
        self.assertIn("abc123hash", html)
        self.assertIn("harness limitation", html)


if __name__ == "__main__":
    unittest.main()
