"""Regressions for the findings of the 2026-08-07 adversarial review.

Every test here fails against the pre-review behaviour and is named after the
finding it pins, so a future rewrite cannot quietly reintroduce the gap.
"""

from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from figma_lossless.compiler import (
    CanonicalMapper,
    CompileOptions,
    DesignCompiler,
    linear_gradient,
    paint_hex,
    rgba_hex,
)
from figma_lossless.report import render_report
from figma_lossless.util import read_json, safe_slug, sha256_file, write_json
from figma_lossless.validators import DEFAULT_CONFIG, GateValidator, ValidateOptions

from test_canonical import box, frame_node, solid, text_node, write_rest_input
from test_gates_canonical import (
    ELEMENT_ID,
    RECT,
    GateHarness,
    expected_element,
    shadow,
)


# The live payload the reviewer captured on 2026-08-07 carries keys that did
# not exist when the registries were first written. Compiling it is the proof
# that nested accounting covers today's Figma REST schema, not a 2024 one.
LIVE_TEXT_STYLE = {
    "fontFamily": "Pretendard",
    "fontPostScriptName": "Pretendard-SemiBold",
    "fontStyle": "SemiBold",
    "fontWeight": 600,
    "fontSize": 14.0,
    "letterSpacing": -0.35,
    "lineHeightPx": 22.4,
    "lineHeightPercent": 114.0,
    "lineHeightPercentFontSize": 160.0,
    "lineHeightUnit": "FONT_SIZE_%",
    "textAlignHorizontal": "CENTER",
    "textAlignVertical": "CENTER",
    "textAutoResize": "WIDTH_AND_HEIGHT",
}
LIVE_IMAGE_PAINT = {
    "blendMode": "NORMAL",
    "type": "IMAGE",
    "scaleMode": "FILL",
    "imageRef": "6f2d1c4e",
}


def write_rest_input_with_manifest(
    root: Path,
    documents: dict[str, dict[str, Any]],
    **manifest_overrides: Any,
) -> Path:
    """Like write_rest_input, but lets a test declare a broken collection."""

    directory = write_rest_input(root, documents)
    manifest_path = directory / "rest" / "collection-manifest.json"
    manifest = read_json(manifest_path)
    manifest.update(manifest_overrides)
    write_json(manifest_path, manifest)
    return directory


class CompilerRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _screen(self, document: dict[str, Any]) -> dict[str, Any]:
        directory = write_rest_input(self.root, {document["id"]: document})
        return CanonicalMapper(directory).map_screens()[0]

    def _style(self, document: dict[str, Any], node_id: str) -> dict[str, Any]:
        screen = self._screen(document)
        return next(
            item for item in screen["elements"] if item["nodeId"] == node_id
        )["style"]

    # ---- M1 alpha byte parity -------------------------------------------

    def _translucent_112233(self, opacity: float) -> str | None:
        """The exact paint the reviewer probed in Chromium, at one alpha."""

        return paint_hex(
            solid(17 / 255, 34 / 255, 51 / 255, opacity=opacity)
        )

    def test_m1_alpha_byte_uses_half_up_rounding_like_the_browser(self) -> None:
        # Chromium reports rgba(..., 0.3) as byte 0x4D and 0.7 as 0xB3. Python's
        # half-to-even round() lands on 0x4C and 0xB2 for these two exact
        # midpoints, which is a mismatch on every such translucent color.
        self.assertEqual(self._translucent_112233(0.3), "#1122334D")
        self.assertEqual(self._translucent_112233(0.7), "#112233B3")

    def test_m1_alpha_that_quantises_to_opaque_drops_the_alpha_byte(self) -> None:
        # 0.999 rounds to 255, and Chromium serializes that as rgb(), so the
        # contract has to be 6-digit or it can never be satisfied.
        self.assertEqual(self._translucent_112233(0.999), "#112233")

    def test_m1_alpha_parity_sweep_matches_js_math_round(self) -> None:
        for step in range(1, 100):
            alpha = step / 100
            expected = math.floor(alpha * 255 + 0.5)
            value = rgba_hex({"r": 0.0, "g": 0.0, "b": 0.0, "a": alpha})
            byte = 255 if len(value) == 7 else int(value[7:9], 16)
            self.assertEqual(byte, expected, f"alpha={alpha}")

    # ---- M2 gradient angle in pixel space --------------------------------

    def test_m2_gradient_angle_is_measured_in_pixel_space(self) -> None:
        paint = {
            "type": "GRADIENT_LINEAR",
            "gradientHandlePositions": [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}],
            "gradientStops": [
                {"color": {"r": 1.0, "g": 0.0, "b": 0.0, "a": 1.0}, "position": 0.0},
                {"color": {"r": 0.0, "g": 0.0, "b": 1.0, "a": 1.0}, "position": 1.0},
            ],
        }
        wide = frame_node(
            "1:1", absoluteBoundingBox=box(0, 0, 400, 100), fills=[paint]
        )
        # On a 400x100 node the corner-to-corner handle renders far flatter than
        # the 135 degrees the normalized handles alone suggest.
        self.assertAlmostEqual(
            self._style(wide, "1:1")["backgroundGradient"]["angleDeg"],
            104.0362,
            delta=0.05,
        )
        square = frame_node(
            "2:1", absoluteBoundingBox=box(0, 0, 100, 100), fills=[paint]
        )
        self.assertEqual(
            self._style(square, "2:1")["backgroundGradient"]["angleDeg"], 135
        )

    def test_m2_axis_aligned_gradients_are_unchanged_by_the_aspect_ratio(
        self,
    ) -> None:
        vertical = {
            "type": "GRADIENT_LINEAR",
            "gradientHandlePositions": [{"x": 0.5, "y": 0.0}, {"x": 0.5, "y": 1.0}],
            "gradientStops": [],
        }
        horizontal = dict(
            vertical,
            gradientHandlePositions=[{"x": 0.0, "y": 0.5}, {"x": 1.0, "y": 0.5}],
        )
        self.assertEqual(linear_gradient(vertical, 400, 100)["angleDeg"], 180)
        self.assertEqual(linear_gradient(horizontal, 400, 100)["angleDeg"], 90)

    def test_m2_gradient_stop_positions_stay_proportional(self) -> None:
        paint = {
            "type": "GRADIENT_LINEAR",
            "gradientHandlePositions": [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}],
            "gradientStops": [
                {"color": {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0}, "position": 0.25}
            ],
        }
        self.assertEqual(
            linear_gradient(paint, 400, 100)["stops"][0]["position"], 0.25
        )

    # ---- r1 gradient stop extent ------------------------------------------

    def _gradient_node(
        self,
        node_id: str,
        handles: tuple[tuple[float, float], tuple[float, float]],
        positions: tuple[float, ...] = (0.0, 1.0),
        **overrides: Any,
    ) -> dict[str, Any]:
        colors = [
            {"r": 1.0, "g": 0.0, "b": 0.0, "a": 1.0},
            {"r": 0.0, "g": 0.0, "b": 1.0, "a": 1.0},
        ]
        paint = {
            "type": "GRADIENT_LINEAR",
            "gradientHandlePositions": [
                {"x": handles[0][0], "y": handles[0][1]},
                {"x": handles[1][0], "y": handles[1][1]},
            ],
            "gradientStops": [
                {"color": colors[index % 2], "position": position}
                for index, position in enumerate(positions)
            ],
        }
        return frame_node(node_id, fills=[paint], **overrides)

    def test_r1_stops_are_reprojected_onto_the_css_gradient_line(self) -> None:
        # Handles spanning only the middle half of a 400x100 node: CSS measures
        # from the box edge, so 0..1 in Figma is 0.25..0.75 in CSS.
        node = self._gradient_node(
            "1:1",
            ((0.25, 0.5), (0.75, 0.5)),
            absoluteBoundingBox=box(0, 0, 400, 100),
        )
        gradient = self._style(node, "1:1")["backgroundGradient"]
        self.assertEqual(gradient["angleDeg"], 90)
        self.assertEqual(
            [stop["position"] for stop in gradient["stops"]], [0.25, 0.75]
        )

    def test_r1_full_span_axis_aligned_stops_stay_at_zero_and_one(self) -> None:
        for handles, angle in (
            (((0.5, 0.0), (0.5, 1.0)), 180),
            (((0.0, 0.5), (1.0, 0.5)), 90),
        ):
            node = self._gradient_node(
                "1:1", handles, absoluteBoundingBox=box(0, 0, 400, 100)
            )
            gradient = self._style(node, "1:1")["backgroundGradient"]
            self.assertEqual(gradient["angleDeg"], angle)
            self.assertEqual(
                [stop["position"] for stop in gradient["stops"]], [0, 1]
            )

    def test_r1_handles_reaching_past_the_box_keep_positions_outside_it(
        self,
    ) -> None:
        node = self._gradient_node(
            "1:1",
            ((-0.25, 0.5), (1.25, 0.5)),
            absoluteBoundingBox=box(0, 0, 400, 100),
        )
        gradient = self._style(node, "1:1")["backgroundGradient"]
        self.assertEqual(
            [stop["position"] for stop in gradient["stops"]], [-0.25, 1.25]
        )

    # ---- r2 degenerate geometry -------------------------------------------

    def _degenerate_screen(self, child: dict[str, Any]) -> dict[str, Any]:
        return self._screen(frame_node("1:1", children=[child]))

    def test_r2_gradient_without_a_bounding_box_is_not_compiled(self) -> None:
        child = self._gradient_node("1:2", ((0.0, 0.0), (1.0, 1.0)))
        del child["absoluteBoundingBox"]
        screen = self._degenerate_screen(child)
        element = next(
            item for item in screen["elements"] if item["nodeId"] == "1:2"
        )
        self.assertNotIn("backgroundGradient", element["style"])
        self.assertIn(
            {
                "nodeId": "1:2",
                "key": "fills",
                "reason": "gradient conversion requires positive node dimensions",
            },
            screen["propertyAccounting"]["unsupported"],
        )

    def test_r2_zero_sized_node_gradient_is_not_compiled(self) -> None:
        for width, height in ((0, 100), (400, 0)):
            child = self._gradient_node(
                "1:2",
                ((0.0, 0.0), (1.0, 1.0)),
                absoluteBoundingBox=box(0, 0, width, height),
            )
            screen = self._degenerate_screen(child)
            element = next(
                item for item in screen["elements"] if item["nodeId"] == "1:2"
            )
            self.assertNotIn(
                "backgroundGradient", element["style"], f"{width}x{height}"
            )
            self.assertIn(
                "positive node dimensions",
                screen["propertyAccounting"]["unsupported"][0]["reason"],
            )

    # ---- r3 accounting below depth one ------------------------------------

    def test_r3_unknown_keys_below_depth_one_are_violations(self) -> None:
        document = frame_node(
            "1:1",
            absoluteBoundingBox={
                "x": 0,
                "y": 0,
                "width": 200,
                "height": 100,
                "quantumBox": 1,
            },
            individualStrokeWeights={
                "top": 1,
                "right": 1,
                "bottom": 1,
                "left": 1,
                "quantumSide": 1,
            },
            strokes=[solid(0.0, 0.0, 0.0)],
            strokeWeight=1.0,
            fills=[
                {
                    "type": "GRADIENT_LINEAR",
                    "gradientHandlePositions": [
                        {"x": 0.0, "y": 0.0},
                        {"x": 0.0, "y": 1.0},
                    ],
                    "gradientStops": [
                        {
                            "color": {
                                "r": 1.0,
                                "g": 0.0,
                                "b": 0.0,
                                "a": 1.0,
                                "quantumChannel": 1,
                            },
                            "position": 0.0,
                            "quantumStop": 1,
                        }
                    ],
                }
            ],
            effects=[
                {
                    "type": "DROP_SHADOW",
                    "color": {"r": 0.0, "g": 0.0, "b": 0.0, "a": 0.5, "quantumTint": 1},
                    "offset": {"x": 0.0, "y": 1.0, "quantumAxis": 1},
                    "radius": 2.0,
                }
            ],
        )
        screen = self._screen(document)
        self.assertEqual(
            sorted(
                item["key"] for item in screen["propertyAccounting"]["violations"]
            ),
            [
                "absoluteBoundingBox.quantumBox",
                "effects[].color.quantumTint",
                "effects[].offset.quantumAxis",
                "fills[].gradientStops[].color.quantumChannel",
                "fills[].gradientStops[].quantumStop",
                "individualStrokeWeights.quantumSide",
            ],
        )
        self.assertFalse(screen["status"]["specCompiled"])

    def test_r3_an_opaque_key_covers_its_entire_subtree(self) -> None:
        document = frame_node(
            "1:1",
            fills=[
                solid(
                    0.0,
                    0.0,
                    0.0,
                    boundVariables={"color": {"anythingAtAll": {"deep": 1}}},
                )
            ],
        )
        accounting = self._screen(document)["propertyAccounting"]
        self.assertEqual(accounting["violations"], [])
        self.assertIn("fills[].boundVariables", accounting["preservedOpaque"])

    def test_r3_a_compiled_container_without_a_registry_is_a_violation(
        self,
    ) -> None:
        # cornerRadius is compiled as a number; a dict there means the schema
        # grew a shape these registries do not describe yet.
        screen = self._screen(frame_node("1:1", cornerRadius={"value": 4}))
        self.assertEqual(
            screen["propertyAccounting"]["violations"],
            [
                {
                    "nodeId": "1:1",
                    "key": "cornerRadius",
                    "reason": "compiled container has no property registry",
                }
            ],
        )

    def test_r3_node_children_are_not_double_counted(self) -> None:
        document = frame_node("1:1", children=[text_node("1:2", "Copy")])
        screen = self._screen(document)
        self.assertEqual(screen["propertyAccounting"]["nodes"], 2)
        self.assertEqual(screen["propertyAccounting"]["violations"], [])
        self.assertIn("children", screen["propertyAccounting"]["compiled"])
        self.assertNotIn(
            "children.id", screen["propertyAccounting"]["compiled"]
        )

    def test_r3_nested_colors_and_boxes_are_accounted_at_depth_two(self) -> None:
        document = frame_node("1:1", fills=[solid(0.0, 0.0, 0.0)])
        compiled = self._screen(document)["propertyAccounting"]["compiled"]
        for key in (
            "absoluteBoundingBox.width",
            "fills[].color.r",
            "fills[].color.a",
        ):
            self.assertIn(key, compiled)

    # ---- M3 recursive property accounting --------------------------------

    def test_m3_unknown_nested_style_key_is_a_violation(self) -> None:
        node = text_node("1:1", "Copy")
        node["style"]["quantumKerning"] = "SUPERPOSED"
        screen = self._screen(node)
        self.assertEqual(
            screen["propertyAccounting"]["violations"],
            [
                {
                    "nodeId": "1:1",
                    "key": "style.quantumKerning",
                    "reason": "unknown canonical property",
                }
            ],
        )
        self.assertFalse(screen["status"]["specCompiled"])

    def test_m3_unknown_nested_paint_and_effect_keys_are_violations(self) -> None:
        document = frame_node(
            "1:1",
            fills=[solid(0.0, 0.0, 0.0, quantumPaint=1)],
            effects=[
                {
                    "type": "DROP_SHADOW",
                    "color": {"r": 0.0, "g": 0.0, "b": 0.0, "a": 0.5},
                    "offset": {"x": 0.0, "y": 1.0},
                    "radius": 2.0,
                    "quantumGlow": True,
                }
            ],
        )
        screen = self._screen(document)
        self.assertEqual(
            sorted(
                item["key"] for item in screen["propertyAccounting"]["violations"]
            ),
            ["effects[].quantumGlow", "fills[].quantumPaint"],
        )

    def test_m3_text_align_horizontal_becomes_the_text_align_contract(self) -> None:
        node = text_node("1:1", "Copy")
        node["style"]["textAlignHorizontal"] = "CENTER"
        self.assertEqual(self._style(node, "1:1")["textAlign"], "center")

    def test_m3_text_nodes_always_emit_a_text_align_contract(self) -> None:
        # No textAlignHorizontal in the payload still means left-aligned text.
        self.assertEqual(self._style(text_node("1:1", "Copy"), "1:1")["textAlign"], "left")
        for figma_value, css in (
            ("RIGHT", "right"),
            ("JUSTIFIED", "justify"),
        ):
            node = text_node("1:1", "Copy")
            node["style"]["textAlignHorizontal"] = figma_value
            self.assertEqual(self._style(node, "1:1")["textAlign"], css)

    def test_m3_live_2026_payload_shape_compiles_without_violations(self) -> None:
        document = frame_node(
            "1:1",
            targetAspectRatio={"x": 16.0, "y": 9.0},
            fills=[LIVE_IMAGE_PAINT],
            strokes=[solid(0.0, 0.0, 0.0, blendMode="NORMAL", opacity=1.0)],
            strokeWeight=1.0,
            children=[
                text_node("1:2", "Copy", style=dict(LIVE_TEXT_STYLE)),
            ],
        )
        screen = self._screen(document)
        self.assertEqual(screen["propertyAccounting"]["violations"], [])
        accounting = screen["propertyAccounting"]
        self.assertIn("targetAspectRatio", accounting["preservedOpaque"])
        self.assertIn("style.lineHeightUnit", accounting["preservedOpaque"])
        self.assertIn("fills[].imageRef", accounting["preservedOpaque"])
        self.assertIn("style.textAlignHorizontal", accounting["compiled"])

    # ---- m1 transparent container background -----------------------------

    def test_m1_minor_container_with_no_visible_paint_is_transparent(self) -> None:
        self.assertEqual(
            self._style(frame_node("1:1", fills=[]), "1:1")["backgroundColor"],
            "#00000000",
        )
        hidden_paint = frame_node("2:1", fills=[solid(1.0, 0.0, 1.0, visible=False)])
        self.assertEqual(
            self._style(hidden_paint, "2:1")["backgroundColor"], "#00000000"
        )

    def test_m1_minor_absent_fills_key_declares_no_background(self) -> None:
        self.assertNotIn("backgroundColor", self._style(frame_node("1:1"), "1:1"))

    # ---- m6 padding only under auto layout --------------------------------

    def test_m6_layout_mode_none_compiles_no_padding_or_gap(self) -> None:
        document = frame_node(
            "1:1",
            layoutMode="NONE",
            paddingTop=12.0,
            paddingLeft=16.0,
            itemSpacing=8.0,
        )
        style = self._style(document, "1:1")
        for key in ("paddingTop", "paddingRight", "paddingBottom", "paddingLeft"):
            self.assertNotIn(key, style)
        self.assertNotIn("rowGap", style)
        self.assertNotIn("columnGap", style)

    # ---- m5 text shadows ---------------------------------------------------

    def test_m5_text_node_shadows_compile_to_the_text_shadow_contract(self) -> None:
        node = text_node(
            "1:1",
            "Copy",
            effects=[
                {
                    "type": "DROP_SHADOW",
                    "color": {"r": 0.0, "g": 0.0, "b": 0.0, "a": 0.25},
                    "offset": {"x": 0.0, "y": 2.0},
                    "radius": 4.0,
                }
            ],
        )
        style = self._style(node, "1:1")
        self.assertEqual(
            style["textShadow"],
            [{"offsetX": 0, "offsetY": 2, "blurRadius": 4, "color": "#00000040"}],
        )
        self.assertNotIn("boxShadow", style)

    def test_m5_container_shadows_still_compile_to_box_shadow(self) -> None:
        document = frame_node(
            "1:1",
            effects=[
                {
                    "type": "DROP_SHADOW",
                    "color": {"r": 0.0, "g": 0.0, "b": 0.0, "a": 0.25},
                    "offset": {"x": 0.0, "y": 2.0},
                    "radius": 4.0,
                    "spread": 1.0,
                }
            ],
        )
        style = self._style(document, "1:1")
        self.assertEqual(style["boxShadow"][0]["spreadRadius"], 1)
        self.assertNotIn("textShadow", style)

    # ---- m3 minor per-side border colors -----------------------------------

    def test_m3_minor_stroke_paint_fills_all_four_side_colors(self) -> None:
        document = frame_node(
            "1:1", strokes=[solid(1.0, 0.0, 0.0)], strokeWeight=1.0
        )
        style = self._style(document, "1:1")
        self.assertEqual(
            {
                key: style[key]
                for key in (
                    "borderColor",
                    "borderTopColor",
                    "borderRightColor",
                    "borderBottomColor",
                    "borderLeftColor",
                )
            },
            {
                "borderColor": "#FF0000",
                "borderTopColor": "#FF0000",
                "borderRightColor": "#FF0000",
                "borderBottomColor": "#FF0000",
                "borderLeftColor": "#FF0000",
            },
        )

    # ---- multiple fills ----------------------------------------------------

    def test_multiple_visible_solids_take_the_topmost_paint(self) -> None:
        document = frame_node(
            "1:1", fills=[solid(1.0, 0.0, 0.0), solid(0.0, 0.0, 1.0)]
        )
        screen = self._screen(document)
        style = next(
            item for item in screen["elements"] if item["nodeId"] == "1:1"
        )["style"]
        # Figma paints bottom-to-top, so the last visible solid is what shows.
        self.assertEqual(style["backgroundColor"], "#0000FF")
        self.assertEqual(
            [item["reason"] for item in screen["propertyAccounting"]["unsupported"]],
            ["solid paint below the topmost fill is not compiled"],
        )


class CollectionCompletenessTest(unittest.TestCase):
    """B1: an incomplete REST collection must block the whole feature."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _compile_incomplete(self) -> dict[str, Any]:
        rest_input = write_rest_input_with_manifest(
            self.root,
            {"1:1": frame_node("1:1", name="Screen")},
            expectedNodeIds=["1:1", "9:9"],
            collectedNodeIds=["1:1"],
            complete=False,
            failures=[{"id": "9:9", "reason": "HTTP 403"}],
        )
        return DesignCompiler(
            CompileOptions(
                feature_id="incomplete",
                input_dir=rest_input,
                output_dir=self.bundle,
                rest_input=rest_input,
            )
        ).compile()

    def test_b1_compiler_records_the_incomplete_collection(self) -> None:
        self._compile_incomplete()
        collection = read_json(self.bundle / "property-accounting.json")[
            "restCollection"
        ]
        self.assertFalse(collection["complete"])
        self.assertEqual(collection["missingNodeIds"], ["9:9"])
        self.assertEqual(
            collection["failures"], [{"id": "9:9", "reason": "HTTP 403"}]
        )

    def test_b1_uncollected_screen_is_listed_in_coverage(self) -> None:
        self._compile_incomplete()
        coverage = read_json(self.bundle / "coverage.json")
        missing = next(item for item in coverage if item["nodeId"] == "9:9")
        self.assertFalse(missing["status"]["contextFetched"])
        self.assertFalse(missing["status"]["specCompiled"])

    def test_b1_validation_fails_and_the_report_names_the_failure(self) -> None:
        self._compile_incomplete()
        actual = self.root / "actual.json"
        write_json(
            actual,
            {
                "schemaVersion": "1.0",
                "featureId": "incomplete",
                "provenance": "browser-capture",
                "screens": {
                    "1:1": {
                        "name": "Screen",
                        "route": "/",
                        "screenshot": None,
                        "elements": {},
                        "assets": {},
                    }
                },
                "components": {},
            },
        )
        config = self.root / "config.json"
        write_json(config, {"requireReferenceScreenshots": False})
        output = self.root / "report"
        result = GateValidator(
            ValidateOptions(self.bundle, actual, output, config_path=config)
        ).validate()
        self.assertFalse(result["passed"])
        incomplete = [
            item
            for item in result["defects"]
            if item["type"] == "incomplete-collection"
        ]
        self.assertTrue(
            any(item["screenNodeId"] == "9:9" for item in incomplete), incomplete
        )
        self.assertTrue(
            any("HTTP 403" in item["message"] for item in incomplete), incomplete
        )
        self.assertEqual(result["gates"]["accounting"]["status"], "FAIL")
        document = render_report(result, output).read_text(encoding="utf-8")
        self.assertIn("HTTP 403", document)
        self.assertIn("Figma REST collection", document)

    def test_b1_complete_collection_raises_no_defect(self) -> None:
        rest_input = write_rest_input(self.root, {"1:1": frame_node("1:1")})
        DesignCompiler(
            CompileOptions(
                feature_id="complete",
                input_dir=rest_input,
                output_dir=self.bundle,
                rest_input=rest_input,
            )
        ).compile()
        actual = self.root / "actual.json"
        write_json(
            actual,
            {
                "schemaVersion": "1.0",
                "featureId": "complete",
                "provenance": "browser-capture",
                "screens": {
                    "1:1": {"name": "Frame", "elements": {}, "assets": {}}
                },
                "components": {},
            },
        )
        config = self.root / "config.json"
        write_json(config, {"requireReferenceScreenshots": False})
        result = GateValidator(
            ValidateOptions(self.bundle, actual, self.root / "report", config_path=config)
        ).validate()
        self.assertEqual(
            [
                item
                for item in result["defects"]
                if item["type"] == "incomplete-collection"
            ],
            [],
        )


class GateRegressionTest(GateHarness):
    """Gate-side halves of the same findings, driven through a real bundle."""

    # ---- M1 alpha equivalence -------------------------------------------

    def test_m1_opaque_alpha_byte_equals_six_digit_hex(self) -> None:
        result = self._compare_style(
            {"backgroundColor": "#112233FF"}, {"backgroundColor": "#112233"}
        )
        self.assertEqual(self._style_defects(result), [])
        result = self._compare_style(
            {"backgroundColor": "#112233"}, {"backgroundColor": "#112233FF"}
        )
        self.assertEqual(self._style_defects(result), [])

    def test_m1_translucent_alpha_is_still_compared_byte_for_byte(self) -> None:
        result = self._compare_style(
            {"backgroundColor": "#1122334D"}, {"backgroundColor": "#1122334C"}
        )
        self.assertEqual(len(self._style_defects(result)), 1)

    # ---- M4 letterSpacing normal ----------------------------------------

    def test_m4_letter_spacing_zero_is_satisfied_by_a_normal_capture(self) -> None:
        # The adapter now maps the "normal" keyword to 0 the way gap() does.
        result = self._compare_style({"letterSpacing": 0}, {"letterSpacing": 0})
        self.assertEqual(self._style_defects(result), [])

    def test_m4_letter_spacing_keyword_reaching_the_gate_is_a_defect(self) -> None:
        # Pre-fix the adapter passed the raw "normal" string through px().
        result = self._compare_style(
            {"letterSpacing": 0}, {"letterSpacing": "normal"}
        )
        self.assertEqual(len(self._style_defects(result)), 1)

    # ---- M5 unit-aware tolerances ---------------------------------------

    def test_m5_opacity_uses_its_own_tolerance(self) -> None:
        result = self._compare_style({"opacity": 1.0}, {"opacity": 0.9})
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["expected"], {"opacity": 1.0})

    def test_m5_line_height_ratio_uses_its_own_tolerance(self) -> None:
        result = self._compare_style({"lineHeight": 1.6}, {"lineHeight": 1.601})
        self.assertEqual(self._style_defects(result), [])
        result = self._compare_style({"lineHeight": 1.6}, {"lineHeight": 1.65})
        self.assertEqual(len(self._style_defects(result)), 1)

    def test_m5_default_config_documents_every_tolerance(self) -> None:
        self.assertEqual(DEFAULT_CONFIG["styleNumericTolerance"], 0.1)
        self.assertEqual(DEFAULT_CONFIG["styleRatioTolerance"], 0.01)
        self.assertEqual(DEFAULT_CONFIG["styleOpacityTolerance"], 0.01)

    # ---- m1 transparent background is a contract -------------------------

    def test_m1_minor_painting_over_a_transparent_container_is_a_defect(self) -> None:
        result = self._compare_style(
            {"backgroundColor": "#00000000"}, {"backgroundColor": "#FF00FF"}
        )
        self.assertEqual(len(self._style_defects(result)), 1)
        result = self._compare_style(
            {"backgroundColor": "#00000000"}, {"backgroundColor": "#00000000"}
        )
        self.assertEqual(self._style_defects(result), [])

    # ---- m2 hidden elements must not render ------------------------------

    def _hidden_element_result(
        self, actual_rect: Any, style: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._write_bundle(
            [
                expected_element(
                    style={},
                    hidden=True,
                    effectiveHidden=True,
                    hiddenStyle={"paddingTop": 12},
                )
            ]
        )
        elements = (
            {}
            if actual_rect is None
            else {
                ELEMENT_ID: {
                    "name": "Primary button",
                    "rect": actual_rect,
                    "style": style or {},
                }
            }
        )
        return self._validate(self._write_actual(elements))

    def test_m2_hidden_element_absent_from_the_capture_is_fine(self) -> None:
        result = self._hidden_element_result(None)
        self.assertEqual(
            self._gate_defects(result, "structure"), [], result["defects"]
        )

    def test_m2_hidden_element_that_renders_is_a_hard_defect(self) -> None:
        result = self._hidden_element_result(
            {"x": 0, "y": 0, "width": 10, "height": 10}
        )
        defects = self._gate_defects(result, "structure")
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "hidden-element-rendered")
        self.assertEqual(defects[0]["severity"], "hard")
        self.assertFalse(result["passed"])

    def test_m2_hidden_element_collapsed_to_zero_area_is_fine(self) -> None:
        result = self._hidden_element_result(
            {"x": 0, "y": 0, "width": 0, "height": 0}
        )
        self.assertEqual(self._gate_defects(result, "structure"), [])

    # ---- r4 legitimate ways to implement "hidden" -------------------------

    def test_r4_visibility_hidden_implements_a_hidden_element(self) -> None:
        result = self._hidden_element_result(
            dict(RECT), {"display": "block", "visibility": "hidden"}
        )
        self.assertEqual(
            self._gate_defects(result, "structure"), [], result["defects"]
        )

    def test_r4_opacity_zero_implements_a_hidden_element(self) -> None:
        result = self._hidden_element_result(
            dict(RECT),
            {"display": "block", "visibility": "visible", "opacity": 0},
        )
        self.assertEqual(
            self._gate_defects(result, "structure"), [], result["defects"]
        )

    def test_r4_display_none_implements_a_hidden_element(self) -> None:
        result = self._hidden_element_result(
            dict(RECT), {"display": "none", "visibility": "visible"}
        )
        self.assertEqual(
            self._gate_defects(result, "structure"), [], result["defects"]
        )

    def test_r4_a_visible_hidden_element_is_still_a_hard_defect(self) -> None:
        result = self._hidden_element_result(
            dict(RECT),
            {"display": "block", "visibility": "visible", "opacity": 1},
        )
        defects = self._gate_defects(result, "structure")
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "hidden-element-rendered")
        self.assertEqual(defects[0]["severity"], "hard")


    # ---- m3 per-side border colors ---------------------------------------

    def test_m3_minor_wrong_side_border_colors_are_defects(self) -> None:
        result = self._compare_style(
            {
                "borderTopColor": "#FF0000",
                "borderRightColor": "#FF0000",
                "borderBottomColor": "#FF0000",
                "borderLeftColor": "#FF0000",
            },
            {
                "borderTopColor": "#FF0000",
                "borderRightColor": "#00FF00",
                "borderBottomColor": "#0000FF",
                "borderLeftColor": "#FFFF00",
            },
        )
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 3, result["defects"])
        self.assertEqual(
            sorted(next(iter(item["expected"])) for item in defects),
            ["borderBottomColor", "borderLeftColor", "borderRightColor"],
        )

    # ---- m4 percentage radius --------------------------------------------

    def test_m4_minor_percentage_radius_never_reads_as_a_pixel_radius(self) -> None:
        result = self._compare_style({"borderRadius": 50}, {"borderRadius": "50%"})
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["actual"], {"borderRadius": "50%"})

    # ---- m5 text shadow --------------------------------------------------

    def test_m5_css_text_shadow_satisfies_the_text_shadow_contract(self) -> None:
        required = {"offsetX": 0, "offsetY": 2, "blurRadius": 4, "color": "#00000040"}
        result = self._compare_style(
            {"textShadow": [required]}, {"textShadow": [dict(required)]}
        )
        self.assertEqual(self._style_defects(result), [])

    def test_m5_missing_text_shadow_is_a_defect(self) -> None:
        required = {"offsetX": 0, "offsetY": 2, "blurRadius": 4, "color": "#00000040"}
        result = self._compare_style({"textShadow": [required]}, {"textShadow": []})
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertIn("textShadow", defects[0]["message"])

    def test_m5_text_shadow_does_not_fall_back_to_filter_drop_shadow(self) -> None:
        required = {"offsetX": 0, "offsetY": 2, "blurRadius": 4, "color": "#00000040"}
        result = self._compare_style(
            {"textShadow": [required]},
            {"textShadow": [], "filterDropShadows": [dict(required)]},
        )
        self.assertEqual(len(self._style_defects(result)), 1)

    # ---- shadow order ----------------------------------------------------

    def test_box_shadows_in_a_different_order_still_match(self) -> None:
        first = shadow()
        second = shadow(offsetY=8, color="#00000033")
        result = self._compare_style(
            {"boxShadow": [first, second]},
            {"boxShadow": [second, first], "filterDropShadows": []},
        )
        self.assertEqual(self._style_defects(result), [])

    def test_a_genuinely_different_shadow_is_still_a_defect(self) -> None:
        result = self._compare_style(
            {"boxShadow": [shadow(), shadow(offsetY=8)]},
            {
                "boxShadow": [shadow(offsetY=8), shadow(offsetY=99)],
                "filterDropShadows": [],
            },
        )
        self.assertEqual(len(self._style_defects(result)), 1)

    def test_duplicate_expected_shadows_need_distinct_rendered_shadows(self) -> None:
        # A bijection, not just "each required shadow appears somewhere".
        result = self._compare_style(
            {"boxShadow": [shadow(), shadow()]},
            {"boxShadow": [shadow(), shadow(offsetY=8)], "filterDropShadows": []},
        )
        self.assertEqual(len(self._style_defects(result)), 1)


class ContractEndToEndTest(unittest.TestCase):
    """Compile a canonical node, then gate a capture against it.

    The compiler, the adapter vocabulary and the comparators only hold together
    end to end; a gate-only test passes even when the compiler emits nothing.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _gate(
        self, document: dict[str, Any], captured: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        rest_input = write_rest_input(self.root, {document["id"]: document})
        DesignCompiler(
            CompileOptions(
                feature_id="e2e",
                input_dir=rest_input,
                output_dir=self.bundle,
                rest_input=rest_input,
            )
        ).compile()
        screen = read_json(self.bundle / "screens" / f"{safe_slug(document['id'])}.json")
        elements = {
            element["nodeId"]: {
                "name": element.get("name"),
                "rect": element.get("rect"),
                "style": captured.get(element["nodeId"], {}),
            }
            for element in screen["elements"]
        }
        actual = self.root / "actual.json"
        write_json(
            actual,
            {
                "schemaVersion": "1.0",
                "featureId": "e2e",
                "provenance": "browser-capture",
                "screens": {
                    document["id"]: {
                        "name": screen["name"],
                        "route": "/",
                        "screenshot": None,
                        "elements": elements,
                        "assets": {},
                    }
                },
                "components": {},
            },
        )
        config = self.root / "config.json"
        write_json(config, {"requireReferenceScreenshots": False})
        return GateValidator(
            ValidateOptions(
                self.bundle, actual, self.root / "report", config_path=config
            )
        ).validate()

    def _style_defects(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in result["defects"] if item["gate"] == "style"]

    def test_m1_minor_transparent_container_is_gated_end_to_end(self) -> None:
        document = frame_node("1:1", fills=[])
        painted = self._gate(document, {"1:1": {"backgroundColor": "#FF00FF"}})
        defects = self._style_defects(painted)
        self.assertEqual(len(defects), 1, painted["defects"])
        self.assertEqual(defects[0]["expected"], {"backgroundColor": "#00000000"})
        transparent = self._gate(document, {"1:1": {"backgroundColor": "#00000000"}})
        self.assertEqual(self._style_defects(transparent), [])

    def test_m3_minor_side_border_colors_are_gated_end_to_end(self) -> None:
        document = frame_node(
            "1:1", strokes=[solid(1.0, 0.0, 0.0)], strokeWeight=1.0
        )
        captured = {
            "borderColor": "#FF0000",
            "borderTopColor": "#FF0000",
            "borderRightColor": "#00FF00",
            "borderBottomColor": "#0000FF",
            "borderLeftColor": "#FFFF00",
            "borderTopWidth": 1,
            "borderRightWidth": 1,
            "borderBottomWidth": 1,
            "borderLeftWidth": 1,
            "backgroundColor": "#00000000",
        }
        result = self._gate(document, {"1:1": captured})
        defects = self._style_defects(result)
        self.assertEqual(
            sorted(next(iter(item["expected"])) for item in defects),
            ["borderBottomColor", "borderLeftColor", "borderRightColor"],
            result["defects"],
        )

    def test_r1_gradient_stop_extent_is_gated_end_to_end(self) -> None:
        # A 400x100 node whose gradient handles span only its middle half.
        paint = {
            "type": "GRADIENT_LINEAR",
            "gradientHandlePositions": [
                {"x": 0.25, "y": 0.5},
                {"x": 0.75, "y": 0.5},
            ],
            "gradientStops": [
                {"color": {"r": 1.0, "g": 0.0, "b": 0.0, "a": 1.0}, "position": 0.0},
                {"color": {"r": 0.0, "g": 0.0, "b": 1.0, "a": 1.0}, "position": 1.0},
            ],
        }
        document = frame_node(
            "1:1", fills=[paint], absoluteBoundingBox=box(0, 0, 400, 100)
        )

        def captured(first: float, last: float) -> dict[str, Any]:
            return {
                "backgroundGradient": {
                    "type": "linear",
                    "angleDeg": 90,
                    "stops": [
                        {"color": "#FF0000", "position": first},
                        {"color": "#0000FF", "position": last},
                    ],
                }
            }

        # linear-gradient(90deg, red 25%, blue 75%) is what Figma renders.
        self.assertEqual(
            self._style_defects(
                self._gate(document, {"1:1": captured(0.25, 0.75)})
            ),
            [],
        )
        # linear-gradient(90deg, red 0%, blue 100%) is a different gradient.
        defects = self._style_defects(
            self._gate(document, {"1:1": captured(0, 1)})
        )
        self.assertEqual(len(defects), 1, defects)
        self.assertIn("stop 0", defects[0]["message"])

    def test_m5_text_shadow_is_gated_end_to_end(self) -> None:
        document = text_node(
            "1:1",
            "Copy",
            effects=[
                {
                    "type": "DROP_SHADOW",
                    "color": {"r": 0.0, "g": 0.0, "b": 0.0, "a": 0.25},
                    "offset": {"x": 0.0, "y": 2.0},
                    "radius": 4.0,
                }
            ],
        )
        rendered = {
            "fontFamily": "Pretendard",
            "fontSize": 14,
            "fontWeight": 500,
            "lineHeight": 1.6,
            "letterSpacing": -0.35,
            "textTransform": "none",
            "textDecorationLine": "none",
            "textAlign": "left",
            "color": "#FFFFFF",
            "textShadow": [
                {"offsetX": 0, "offsetY": 2, "blurRadius": 4, "color": "#00000040"}
            ],
            # A text node never renders a CSS box-shadow for a Figma effect.
            "boxShadow": [],
        }
        self.assertEqual(
            self._style_defects(self._gate(document, {"1:1": rendered})), []
        )
        missing = dict(rendered, textShadow=[])
        defects = self._style_defects(self._gate(document, {"1:1": missing}))
        self.assertEqual(len(defects), 1, defects)
        self.assertEqual(next(iter(defects[0]["expected"])), "textShadow")


class RealCollectedNodeTest(unittest.TestCase):
    """Compile a genuine Figma REST capture when one is supplied.

    The rest of the suite runs on synthetic fixtures. Point
    ``FIGMA_LOSSLESS_REAL_BUNDLE`` at a directory produced by ``collect`` to
    additionally compile real Figma output and assert it accounts for every
    property. Skipped when the variable is unset.
    """

    BUNDLE_ENV = "FIGMA_LOSSLESS_REAL_BUNDLE"
    BUNDLE = Path(os.environ.get(BUNDLE_ENV, ""))

    @unittest.skipUnless(
        os.environ.get(BUNDLE_ENV)
        and (BUNDLE / "rest" / "collection-manifest.json").exists(),
        f"set {BUNDLE_ENV} to a bundle produced by `collect` to run this",
    )
    def test_m3_real_collected_node_compiles_without_violations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = DesignCompiler(
                CompileOptions(
                    feature_id="real",
                    input_dir=self.BUNDLE,
                    output_dir=Path(temporary) / "bundle",
                    rest_input=self.BUNDLE,
                )
            ).compile()
        self.assertEqual(manifest["counts"]["accountingViolations"], 0)


if __name__ == "__main__":
    unittest.main()
