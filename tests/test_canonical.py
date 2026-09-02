from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any

from figma_lossless.cli import main
from figma_lossless.compiler import (
    CanonicalMapper,
    CompileOptions,
    DesignCompiler,
    build_variable_index,
    linear_gradient,
    paint_hex,
)
from figma_lossless.util import read_json, safe_slug, sha256_file, write_json
from figma_lossless.validators import (
    GateValidator,
    ValidateOptions,
    create_snapshot_template,
)


# 1x1 white PNG, the smallest reference screenshot the MCP path accepts.
PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)


def write_rest_input(root: Path, documents: dict[str, dict[str, Any]]) -> Path:
    """Write a collector-shaped canonical evidence directory."""

    directory = root / "rest-input"
    nodes_dir = directory / "rest" / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    per_node = {}
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
        },
    )
    return directory


def box(x: float, y: float, width: float, height: float) -> dict[str, Any]:
    return {"x": x, "y": y, "width": width, "height": height}


def solid(red: float, green: float, blue: float, **overrides: Any) -> dict[str, Any]:
    paint = {"type": "SOLID", "color": {"r": red, "g": green, "b": blue, "a": 1.0}}
    paint.update(overrides)
    return paint


def text_node(node_id: str, characters: str, **overrides: Any) -> dict[str, Any]:
    node = {
        "id": node_id,
        "name": characters or node_id,
        "type": "TEXT",
        "absoluteBoundingBox": box(0, 0, 100, 20),
        "characters": characters,
        "style": {
            "fontFamily": "Pretendard",
            "fontWeight": 500,
            "fontSize": 14.0,
            "letterSpacing": -0.35000000000000003,
            "lineHeightPx": 22.399999618530273,
        },
        "fills": [solid(1.0, 1.0, 1.0)],
    }
    node.update(overrides)
    return node


def frame_node(node_id: str, **overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "name": "Frame",
        "type": "FRAME",
        "absoluteBoundingBox": box(0, 0, 200, 100),
    }
    node.update(overrides)
    return node


class CanonicalMapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _rest_input(self, documents: dict[str, dict[str, Any]]) -> Path:
        return write_rest_input(self.root, documents)

    def _screens(
        self,
        documents: dict[str, dict[str, Any]],
        variable_defs: Any = None,
    ) -> list[dict[str, Any]]:
        return CanonicalMapper(
            self._rest_input(documents), variable_defs=variable_defs
        ).map_screens()

    def _screen(
        self,
        document: dict[str, Any],
        variable_defs: Any = None,
    ) -> dict[str, Any]:
        return self._screens({document["id"]: document}, variable_defs)[0]

    def _element(self, screen: dict[str, Any], node_id: str) -> dict[str, Any]:
        return next(
            item for item in screen["elements"] if item["nodeId"] == node_id
        )

    def _style(self, screen: dict[str, Any], node_id: str) -> dict[str, Any]:
        return self._element(screen, node_id)["style"]

    def test_auto_layout_padding_and_vertical_gap(self) -> None:
        vertical = frame_node(
            "1:1",
            layoutMode="VERTICAL",
            paddingTop=12.0,
            paddingRight=16.0,
            paddingBottom=12.0,
            itemSpacing=8.0,
        )
        horizontal = frame_node(
            "2:1", layoutMode="HORIZONTAL", paddingLeft=4.0, itemSpacing=6.0
        )
        screens = self._screens({"1:1": vertical, "2:1": horizontal})
        vertical_style = self._style(screens[0], "1:1")
        self.assertEqual(
            {
                key: vertical_style[key]
                for key in (
                    "paddingTop",
                    "paddingRight",
                    "paddingBottom",
                    "paddingLeft",
                    "rowGap",
                )
            },
            {
                "paddingTop": 12,
                "paddingRight": 16,
                "paddingBottom": 12,
                "paddingLeft": 0,
                "rowGap": 8,
            },
        )
        self.assertNotIn("columnGap", vertical_style)
        horizontal_style = self._style(screens[1], "2:1")
        self.assertEqual(horizontal_style["columnGap"], 6)
        self.assertEqual(horizontal_style["paddingLeft"], 4)
        self.assertNotIn("rowGap", horizontal_style)

    def test_paint_alpha_composition_matches_adapter_hex(self) -> None:
        document = frame_node(
            "1:1",
            fills=[solid(0.0, 0.0, 0.0, opacity=0.6000000238418579)],
            children=[text_node("1:2", "Copy", fills=[solid(1.0, 1.0, 1.0)])],
        )
        screen = self._screen(document)
        self.assertEqual(
            self._style(screen, "1:1")["backgroundColor"], "#00000099"
        )
        self.assertEqual(self._style(screen, "1:2")["color"], "#FFFFFF")
        self.assertEqual(paint_hex(solid(0.2, 0.4, 0.6, opacity=0.5)), "#33669980")

    def test_linear_gradient_angle_and_stops(self) -> None:
        gradient_paint = {
            "type": "GRADIENT_LINEAR",
            "gradientHandlePositions": [
                {"x": 0.5, "y": 0.0},
                {"x": 0.5, "y": 1.0},
                {"x": 1.0, "y": 0.0},
            ],
            "gradientStops": [
                {"color": {"r": 1.0, "g": 0.0, "b": 0.0, "a": 1.0}, "position": 0.0},
                {"color": {"r": 0.0, "g": 0.0, "b": 1.0, "a": 0.5}, "position": 1.0},
            ],
        }
        screen = self._screen(frame_node("1:1", fills=[gradient_paint]))
        self.assertEqual(
            self._style(screen, "1:1")["backgroundGradient"],
            {
                "type": "linear",
                "angleDeg": 180,
                "stops": [
                    {"color": "#FF0000", "position": 0},
                    {"color": "#0000FF80", "position": 1},
                ],
            },
        )
        sideways = dict(
            gradient_paint,
            gradientHandlePositions=[{"x": 0.0, "y": 0.5}, {"x": 1.0, "y": 0.5}],
        )
        # The conversion is measured against the node box, so it needs one.
        self.assertEqual(linear_gradient(sideways, 200, 100)["angleDeg"], 90)

    def test_drop_and_inner_shadows_keep_inset_flag_and_alpha(self) -> None:
        document = frame_node(
            "1:1",
            effects=[
                {
                    "type": "DROP_SHADOW",
                    "color": {"r": 0.0, "g": 0.0, "b": 0.0, "a": 0.25},
                    "offset": {"x": 0.0, "y": 4.0},
                    "radius": 8.0,
                    "spread": 2.0,
                    "visible": True,
                },
                {
                    "type": "INNER_SHADOW",
                    "color": {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0},
                    "offset": {"x": 1.0, "y": -1.0},
                    "radius": 3.0,
                },
                {
                    "type": "DROP_SHADOW",
                    "visible": False,
                    "color": {"r": 1.0, "g": 0.0, "b": 0.0, "a": 1.0},
                    "offset": {"x": 9.0, "y": 9.0},
                    "radius": 9.0,
                },
            ],
        )
        screen = self._screen(document)
        self.assertEqual(
            self._style(screen, "1:1")["boxShadow"],
            [
                {
                    "offsetX": 0,
                    "offsetY": 4,
                    "blurRadius": 8,
                    "spreadRadius": 2,
                    "color": "#00000040",
                    "inset": False,
                },
                {
                    "offsetX": 1,
                    "offsetY": -1,
                    "blurRadius": 3,
                    "spreadRadius": 0,
                    "color": "#FFFFFF",
                    "inset": True,
                },
            ],
        )
        self.assertEqual(
            self._style(self._screen(frame_node("2:1", effects=[])), "2:1")[
                "boxShadow"
            ],
            [],
        )

    def test_text_style_ratio_and_text_case(self) -> None:
        node = text_node("1:1", "Shout")
        node["style"]["textCase"] = "UPPER"
        node["style"]["textDecoration"] = "UNDERLINE"
        screen = self._screen(node)
        self.assertEqual(
            self._style(screen, "1:1"),
            {
                "fontFamily": "Pretendard",
                "fontSize": 14,
                "fontWeight": 500,
                "lineHeight": 1.6,
                "letterSpacing": -0.35,
                "textTransform": "uppercase",
                "textDecorationLine": "underline",
                "textAlign": "left",
                "color": "#FFFFFF",
            },
        )
        plain = self._screen(text_node("2:1", "Plain"))
        self.assertEqual(self._style(plain, "2:1")["textTransform"], "none")
        self.assertEqual(
            self._style(plain, "2:1")["textDecorationLine"], "none"
        )

    def test_unknown_property_is_a_violation_that_blocks_the_spec(self) -> None:
        document = frame_node("1:1", quantumFillMode="SUPERPOSED")
        screen = self._screen(document)
        self.assertEqual(
            screen["propertyAccounting"]["violations"],
            [
                {
                    "nodeId": "1:1",
                    "key": "quantumFillMode",
                    "reason": "unknown canonical property",
                }
            ],
        )
        self.assertFalse(screen["propertyAccounting"]["accountingComplete"])
        self.assertFalse(screen["status"]["specCompiled"])

    def test_known_but_uncompilable_values_are_recorded_not_violations(self) -> None:
        document = frame_node(
            "1:1",
            blendMode="MULTIPLY",
            fills=[
                {
                    "type": "GRADIENT_RADIAL",
                    "gradientStops": [
                        {"color": {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0}, "position": 0}
                    ],
                }
            ],
            children=[
                {
                    "id": "1:2",
                    "name": "Icon",
                    "type": "VECTOR",
                    "absoluteBoundingBox": box(0, 0, 16, 16),
                    "fills": [solid(0.0, 0.0, 0.0)],
                }
            ],
        )
        screen = self._screen(document)
        accounting = screen["propertyAccounting"]
        self.assertEqual(accounting["violations"], [])
        self.assertTrue(screen["status"]["specCompiled"])
        self.assertEqual(
            [(item["nodeId"], item["key"]) for item in accounting["unsupported"]],
            [("1:1", "blendMode"), ("1:1", "fills"), ("1:2", "fills")],
        )
        self.assertIn("GRADIENT_RADIAL", accounting["unsupported"][1]["reason"])
        self.assertNotIn("backgroundColor", self._style(screen, "1:2"))
        self.assertIn("blendMode", accounting["preservedOpaque"])

    def test_bound_variables_become_token_bindings_and_resolve(self) -> None:
        document = frame_node(
            "1:1",
            layoutMode="VERTICAL",
            itemSpacing=8.0,
            cornerRadius=4.0,
            fills=[solid(0.2, 0.4, 1.0)],
            boundVariables={
                "fills": [{"type": "VARIABLE_ALIAS", "id": "VariableID:1:2"}],
                "itemSpacing": {"type": "VARIABLE_ALIAS", "id": "VariableID:3:4"},
                "cornerRadius": {"type": "VARIABLE_ALIAS", "id": "VariableID:5:6"},
            },
        )
        defs = {
            "VariableID:1:2": {"name": "color/brand/primary", "value": "#3366FF"},
            "VariableID:3:4": {"name": "space/sm", "value": 8},
        }
        screen = self._screen(document, variable_defs=defs)
        element = self._element(screen, "1:1")
        self.assertEqual(
            element["tokenBindings"],
            {
                "backgroundColor": "VariableID:1:2",
                "rowGap": "VariableID:3:4",
                "borderRadius": "VariableID:5:6",
            },
        )
        self.assertEqual(
            element["resolvedTokens"],
            {"backgroundColor": "color/brand/primary", "rowGap": "space/sm"},
        )
        self.assertEqual(
            screen["propertyAccounting"]["unresolvedTokenBindings"],
            [
                {
                    "nodeId": "1:1",
                    "styleKey": "borderRadius",
                    "variableId": "VariableID:5:6",
                }
            ],
        )
        self.assertEqual(
            build_variable_index({"space/sm": 8})["space/sm"], "space/sm"
        )

    def test_rect_is_relative_to_the_collected_root(self) -> None:
        document = frame_node(
            "1:1",
            absoluteBoundingBox=box(-53150.0, 54869.0, 145.0, 76.0),
            children=[
                text_node(
                    "1:2",
                    "Nested",
                    absoluteBoundingBox=box(-53134.0, 54881.0, 28.0, 22.0),
                )
            ],
        )
        screen = self._screen(document)
        self.assertEqual(
            self._element(screen, "1:1")["rect"],
            {"x": 0, "y": 0, "width": 145, "height": 76},
        )
        self.assertEqual(
            self._element(screen, "1:2")["rect"],
            {"x": 16, "y": 12, "width": 28, "height": 22},
        )
        self.assertEqual(screen["viewport"], {"width": 145, "height": 76})

    def test_characters_become_deduplicated_exact_copy_entries(self) -> None:
        document = frame_node(
            "1:1",
            children=[
                text_node("1:2", "  Exact copy  "),
                text_node("1:3", ""),
                text_node("1:4", "Hidden", visible=False),
            ],
        )
        screen = self._screen(document)
        self.assertEqual(
            screen["texts"],
            [
                {
                    "nodeId": "1:2",
                    "value": "Exact copy",
                    "property": "textContent",
                    "exact": True,
                    "source": "figma",
                }
            ],
        )
        self.assertEqual(
            screen["extractionAccounting"],
            {
                "visibleMetadataTextNodes": 2,
                "extractedCopyRecords": 1,
                "unparsedTextNodeIds": [],
                "textEvidenceComplete": True,
            },
        )

    def test_invisible_parent_propagates_effective_hidden(self) -> None:
        document = frame_node(
            "1:1",
            children=[
                frame_node(
                    "1:2",
                    visible=False,
                    children=[text_node("1:3", "Buried")],
                )
            ],
        )
        screen = self._screen(document)
        child = self._element(screen, "1:3")
        self.assertFalse(child["hidden"])
        self.assertTrue(child["effectiveHidden"])
        self.assertTrue(self._element(screen, "1:2")["hidden"])
        self.assertEqual(screen["texts"], [])
        # Hidden properties stay recorded but out of the gated style slot.
        self.assertEqual(child["style"], {})
        self.assertEqual(child["hiddenStyle"]["fontSize"], 14)

    def test_individual_stroke_weights_override_uniform_weight(self) -> None:
        document = frame_node(
            "1:1",
            strokes=[solid(0.0, 0.0, 0.0)],
            strokeWeight=1.0,
            individualStrokeWeights={
                "top": 4.0,
                "right": 1.0,
                "bottom": 0.0,
                "left": 1.0,
            },
        )
        style = self._style(self._screen(document), "1:1")
        self.assertEqual(
            {
                key: style[key]
                for key in (
                    "borderColor",
                    "borderTopWidth",
                    "borderRightWidth",
                    "borderBottomWidth",
                    "borderLeftWidth",
                )
            },
            {
                "borderColor": "#000000",
                "borderTopWidth": 4,
                "borderRightWidth": 1,
                "borderBottomWidth": 0,
                "borderLeftWidth": 1,
            },
        )
        invisible = frame_node(
            "2:1", strokes=[solid(0.0, 0.0, 0.0, visible=False)], strokeWeight=2.0
        )
        self.assertNotIn(
            "borderTopWidth", self._style(self._screen(invisible), "2:1")
        )

    def test_rectangle_corner_radii_map_per_corner(self) -> None:
        document = frame_node("1:1", rectangleCornerRadii=[8.0, 4.0, 2.0, 0.0])
        style = self._style(self._screen(document), "1:1")
        self.assertEqual(
            {
                "borderTopLeftRadius": style["borderTopLeftRadius"],
                "borderTopRightRadius": style["borderTopRightRadius"],
                "borderBottomRightRadius": style["borderBottomRightRadius"],
                "borderBottomLeftRadius": style["borderBottomLeftRadius"],
            },
            {
                "borderTopLeftRadius": 8,
                "borderTopRightRadius": 4,
                "borderBottomRightRadius": 2,
                "borderBottomLeftRadius": 0,
            },
        )
        self.assertNotIn("borderRadius", style)
        uniform = self._style(self._screen(frame_node("2:1", cornerRadius=6.0)), "2:1")
        self.assertEqual(uniform["borderRadius"], 6)
        self.assertEqual(uniform["borderBottomLeftRadius"], 6)

    def test_changed_evidence_bytes_are_rejected(self) -> None:
        directory = self._rest_input({"1:1": frame_node("1:1")})
        node_path = directory / "rest" / "nodes" / "1-1.json"
        payload = read_json(node_path)
        payload["nodes"]["1:1"]["document"]["name"] = "Tampered"
        write_json(node_path, payload)
        with self.assertRaisesRegex(ValueError, "changed since collection"):
            CanonicalMapper(directory).map_screens()


class CanonicalCompileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _rest_input(self, documents: dict[str, dict[str, Any]]) -> Path:
        return write_rest_input(self.root, documents)

    def _document(self) -> dict[str, Any]:
        return frame_node(
            "1:1",
            name="Screen",
            layoutMode="VERTICAL",
            itemSpacing=8.0,
            paddingTop=12.0,
            fills=[solid(0.0, 0.0, 0.0, opacity=0.6000000238418579)],
            cornerRadius=8.0,
            children=[
                text_node(
                    "1:2",
                    "Exact copy",
                    absoluteBoundingBox=box(10.0, 20.0, 60.0, 24.0),
                )
            ],
        )

    def test_rest_only_bundle_passes_the_existing_gates(self) -> None:
        rest_input = self._rest_input({"1:1": self._document()})
        manifest = DesignCompiler(
            CompileOptions(
                feature_id="canonical",
                input_dir=rest_input,
                output_dir=self.bundle,
                rest_input=rest_input,
            )
        ).compile()
        self.assertEqual(manifest["counts"]["canonicalScreens"], 1)
        self.assertEqual(manifest["counts"]["discoveredScreens"], 1)
        self.assertEqual(manifest["counts"]["accountingViolations"], 0)
        coverage = read_json(self.bundle / "coverage.json")
        self.assertEqual(coverage[0]["nodeId"], "1:1")
        self.assertTrue(coverage[0]["status"]["specCompiled"])

        actual = self.root / "actual.json"
        create_snapshot_template(self.bundle, actual, use_reference_screenshots=True)
        config = self.root / "config.json"
        write_json(
            config,
            {
                "allowSelfTestSnapshot": True,
                "requireReferenceScreenshots": False,
                # A REST-only bundle compiles no flow or component contract,
                # and both are required by default.
                "requireFlowContract": False,
                "requireComponentContract": False,
            },
        )
        result = GateValidator(
            ValidateOptions(
                self.bundle, actual, self.root / "report", config_path=config
            )
        ).validate()
        self.assertTrue(result["passed"], result["defects"])
        self.assertGreater(result["gates"]["style"]["checked"], 0)
        self.assertGreater(result["gates"]["geometry"]["checked"], 0)

    def test_hidden_nodes_do_not_demand_implementation_evidence(self) -> None:
        document = self._document()
        document["children"].append(
            frame_node(
                "1:3",
                visible=False,
                fills=[solid(1.0, 0.0, 0.0)],
                cornerRadius=2.0,
                children=[text_node("1:4", "Never rendered")],
            )
        )
        rest_input = self._rest_input({"1:1": document})
        DesignCompiler(
            CompileOptions(
                feature_id="canonical",
                input_dir=rest_input,
                output_dir=self.bundle,
                rest_input=rest_input,
            )
        ).compile()
        actual_path = self.root / "actual.json"
        snapshot = create_snapshot_template(
            self.bundle, actual_path, use_reference_screenshots=True
        )
        # A browser capture only sees rendered nodes.
        for node_id in ("1:3", "1:4"):
            del snapshot["screens"]["1:1"]["elements"][node_id]
        write_json(actual_path, snapshot)
        config = self.root / "config.json"
        write_json(
            config,
            {
                "allowSelfTestSnapshot": True,
                "requireReferenceScreenshots": False,
                # A REST-only bundle compiles no flow or component contract,
                # and both are required by default.
                "requireFlowContract": False,
                "requireComponentContract": False,
            },
        )
        result = GateValidator(
            ValidateOptions(
                self.bundle, actual_path, self.root / "report", config_path=config
            )
        ).validate()
        self.assertTrue(result["passed"], result["defects"])

    def test_bundle_accounting_summary_lists_every_category(self) -> None:
        documents = {
            "1:1": self._document(),
            "2:1": frame_node("2:1", quantumFillMode="SUPERPOSED", blendMode="MULTIPLY"),
        }
        rest_input = self._rest_input(documents)
        manifest = DesignCompiler(
            CompileOptions(
                feature_id="canonical",
                input_dir=rest_input,
                output_dir=self.bundle,
                rest_input=rest_input,
            )
        ).compile()
        accounting = read_json(self.bundle / "property-accounting.json")
        self.assertEqual(accounting["counts"]["screens"], 2)
        self.assertEqual(accounting["counts"]["nodes"], 3)
        self.assertEqual(accounting["counts"]["violations"], 1)
        self.assertEqual(
            accounting["violations"],
            [
                {
                    "screenNodeId": "2:1",
                    "nodeId": "2:1",
                    "key": "quantumFillMode",
                    "reason": "unknown canonical property",
                }
            ],
        )
        self.assertEqual(accounting["counts"]["unsupported"], 1)
        self.assertEqual(accounting["unsupported"][0]["key"], "blendMode")
        self.assertIn("characters", accounting["compiled"])
        self.assertEqual(accounting["restCollection"]["fileVersion"], "9001")
        self.assertEqual(
            manifest["propertyAccounting"]["counts"], accounting["counts"]
        )
        coverage = read_json(self.bundle / "coverage.json")
        blocked = next(item for item in coverage if item["nodeId"] == "2:1")
        self.assertFalse(blocked["status"]["specCompiled"])

    def test_canonical_properties_replace_mcp_derived_properties(self) -> None:
        raw = self.root / "raw"
        raw.mkdir()
        reference = (
            'function Screen() {\n'
            '  return <div data-node-id="1:1" data-name="Screen" className="bg-white">\n'
            '    <p className="font-[\'Pretendard:Bold\'] text-[99px] '
            'text-[#112233]" data-node-id="1:2">Exact copy</p>\n'
            '  </div>;\n}'
        )
        metadata = (
            '<frame id="1:1" name="Screen" x="0" y="0" width="200" height="100">\n'
            '  <text id="1:2" name="Exact copy" x="10" y="20" width="60" height="24" />\n'
            "</frame>"
        )
        write_json(
            raw / "01-screen.get-design-context.raw.json",
            {
                "content": [
                    {"type": "text", "text": reference},
                    {
                        "type": "image",
                        "mimeType": "image/png",
                        "data": PNG_BASE64,
                    },
                ]
            },
        )
        write_json(
            raw / "01-screen.metadata.raw.json",
            {"content": [{"type": "text", "text": metadata}]},
        )
        rest_input = self._rest_input({"1:1": self._document()})
        manifest = DesignCompiler(
            CompileOptions(
                feature_id="merged",
                input_dir=raw,
                output_dir=self.bundle,
                rest_input=rest_input,
            )
        ).compile()
        self.assertEqual(manifest["counts"]["detailedContexts"], 1)
        self.assertEqual(len(manifest["screens"]), 1)
        screen = read_json(self.bundle / manifest["screens"][0]["compiledPath"])
        self.assertEqual(screen["evidenceSource"], "figma-mcp+figma-rest")
        self.assertTrue(screen["referenceScreenshot"])
        element = next(
            item for item in screen["elements"] if item["nodeId"] == "1:2"
        )
        self.assertEqual(element["style"]["fontSize"], 14)
        self.assertEqual(element["style"]["color"], "#FFFFFF")
        self.assertIn("text-[99px]", element["className"])
        self.assertEqual(
            screen["elements"][0]["style"]["backgroundColor"], "#00000099"
        )
        self.assertEqual(
            [item["value"] for item in screen["texts"]], ["Exact copy"]
        )
        self.assertTrue(screen["status"]["specCompiled"])

    def test_cli_compiles_canonical_input_with_variable_defs(self) -> None:
        document = frame_node(
            "1:1",
            fills=[solid(0.2, 0.4, 1.0)],
            boundVariables={
                "fills": [{"type": "VARIABLE_ALIAS", "id": "VariableID:1:2"}]
            },
        )
        rest_input = self._rest_input({"1:1": document})
        defs_path = self.root / "variable-defs.json"
        write_json(
            defs_path,
            {
                "content": [
                    {
                        "type": "text",
                        "text": '{"VariableID:1:2": {"name": "color/brand"}}',
                    }
                ]
            },
        )
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = main(
                [
                    "compile",
                    "--rest-input",
                    str(rest_input),
                    "--output",
                    str(self.bundle),
                    "--feature-id",
                    "cli",
                    "--variable-defs",
                    str(defs_path),
                ]
            )
        self.assertEqual(exit_code, 0)
        screen = read_json(self.bundle / "screens" / "1-1.json")
        self.assertEqual(
            screen["elements"][0]["resolvedTokens"],
            {"backgroundColor": "color/brand"},
        )
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(
                main(["compile", "--output", str(self.bundle), "--feature-id", "x"]), 1
            )
        self.assertIn("--input and/or --rest-input", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
