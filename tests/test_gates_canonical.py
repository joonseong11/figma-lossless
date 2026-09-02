from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from figma_lossless.report import render_report
from figma_lossless.util import write_json
from figma_lossless.validators import GateValidator, ValidateOptions

from canonical_bundle import (
    DEFAULT_ACCOUNTING,
    attach_accounting,
    clean_accounting,
)


FEATURE_ID = "canonical-gates"
SCREEN_ID = "1:1"
ELEMENT_ID = "1:2"
RECT = {"x": 0, "y": 0, "width": 10, "height": 10}


def expected_element(**overrides: Any) -> dict[str, Any]:
    """A compiled canonical element with the shape compiler.py emits."""

    element: dict[str, Any] = {
        "nodeId": ELEMENT_ID,
        "type": "frame",
        "figmaType": "FRAME",
        "name": "Primary button",
        "parentNodeId": None,
        "hidden": False,
        "effectiveHidden": False,
        "rect": dict(RECT),
        "style": {},
    }
    element.update(overrides)
    return element


def shadow(**overrides: Any) -> dict[str, Any]:
    value = {
        "offsetX": 0,
        "offsetY": 4,
        "blurRadius": 8,
        "spreadRadius": 0,
        "color": "#0000001A",
        "inset": False,
    }
    value.update(overrides)
    return value


def gradient(angle_deg: float = 180) -> dict[str, Any]:
    return {
        "type": "linear",
        "angleDeg": angle_deg,
        "stops": [
            {"color": "#FFFFFF", "position": 0},
            {"color": "#112233", "position": 1},
        ],
    }


class GateHarness(unittest.TestCase):
    """Bundle/snapshot plumbing shared by every gate-level test."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.output = self.root / "report"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_bundle(
        self,
        elements: list[dict[str, Any]],
        *,
        canonical: bool = True,
        accounting: Any = DEFAULT_ACCOUNTING,
    ) -> None:
        screen: dict[str, Any] = {
            "schemaVersion": "1.0",
            "nodeId": SCREEN_ID,
            "name": "Screen",
            "evidenceSource": "figma-rest" if canonical else "figma-mcp",
            "viewport": {"width": 10, "height": 10},
            "referenceScreenshot": None,
            "referenceCode": None,
            "texts": [],
            "elements": elements,
            "assets": [],
            "variables": {},
            "status": {"contextFetched": True, "specCompiled": True},
        }
        if canonical:
            screen["canonicalEvidence"] = {
                "path": "rest/nodes/1-1.json",
                "sha256": "0" * 64,
                "bytes": 1,
            }
        write_json(self.bundle / "screens" / "1-1.json", screen)
        manifest: dict[str, Any] = {
            "schemaVersion": "1.0",
            "featureId": FEATURE_ID,
            "screens": [
                {
                    "nodeId": SCREEN_ID,
                    "name": "Screen",
                    "compiledPath": "screens/1-1.json",
                }
            ],
            "contracts": {"flowContract": None, "componentMap": None},
        }
        if accounting is DEFAULT_ACCOUNTING:
            # A canonical bundle always ships an accounting artifact, so a
            # fixture that says nothing about it gets one that accounts for
            # everything -- and a fixture about its absence passes None.
            accounting = (
                clean_accounting(FEATURE_ID, nodes=len(elements))
                if canonical
                else None
            )
        if accounting is not None:
            write_json(self.bundle / "property-accounting.json", accounting)
            attach_accounting(manifest, accounting)
        write_json(self.bundle / "manifest.json", manifest)
        write_json(
            self.bundle / "coverage.json",
            [
                {
                    "nodeId": SCREEN_ID,
                    "name": "Screen",
                    "status": {"contextFetched": True, "specCompiled": True},
                }
            ],
        )

    def _write_actual(self, elements: dict[str, Any]) -> Path:
        path = self.root / "actual.json"
        write_json(
            path,
            {
                "schemaVersion": "1.0",
                "featureId": FEATURE_ID,
                "provenance": "browser-capture",
                "screens": {
                    SCREEN_ID: {
                        "name": "Screen",
                        "route": "/",
                        "screenshot": None,
                        "elements": elements,
                        "assets": {},
                    }
                },
                "components": {},
            },
        )
        return path

    def _validate(self, actual_path: Path) -> dict[str, Any]:
        config = self.root / "config.json"
        # This fixture bundle has no flow or component contract; both are
        # required by default, and this suite is asserting about other gates.
        write_json(
            config,
            {
                "requireReferenceScreenshots": False,
                "requireFlowContract": False,
                "requireComponentContract": False,
            },
        )
        return GateValidator(
            ValidateOptions(
                self.bundle, actual_path, self.output, config_path=config
            )
        ).validate()

    def _compare_style(
        self,
        expected_style: dict[str, Any],
        actual_style: dict[str, Any],
        *,
        canonical: bool = True,
        element_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        element = expected_element(
            style=expected_style, **(element_overrides or {})
        )
        self._write_bundle([element], canonical=canonical)
        actual_path = self._write_actual(
            {
                ELEMENT_ID: {
                    "name": "Primary button",
                    "rect": dict(RECT),
                    "style": actual_style,
                }
            }
        )
        return self._validate(actual_path)

    def _style_defects(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in result["defects"] if item["gate"] == "style"]

    def _gate_defects(
        self, result: dict[str, Any], gate: str
    ) -> list[dict[str, Any]]:
        return [item for item in result["defects"] if item["gate"] == gate]


class CanonicalGateTest(GateHarness):
    def test_padding_mismatch_is_a_hard_style_defect(self) -> None:
        result = self._compare_style({"paddingTop": 12}, {"paddingTop": 16})
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "style-mismatch")
        self.assertEqual(defects[0]["severity"], "hard")
        self.assertEqual(defects[0]["expected"], {"paddingTop": 12})
        self.assertEqual(defects[0]["actual"], {"paddingTop": 16})
        self.assertFalse(result["passed"])
        self.assertFalse(result["gates"]["style"]["passed"])

    def test_padding_within_numeric_tolerance_passes(self) -> None:
        result = self._compare_style({"paddingTop": 12}, {"paddingTop": 12.05})
        self.assertEqual(self._style_defects(result), [])
        self.assertTrue(result["passed"], result["defects"])

    def test_missing_actual_style_property_is_still_reported(self) -> None:
        result = self._compare_style(
            {"paddingTop": 12}, {"paddingBottom": 12}
        )
        defects = self._style_defects(result)
        self.assertEqual(
            [item["type"] for item in defects],
            ["missing-actual-style-property"],
        )

    def test_translucent_color_is_not_satisfied_by_an_opaque_color(
        self,
    ) -> None:
        result = self._compare_style(
            {"backgroundColor": "#00000099"}, {"backgroundColor": "#000000"}
        )
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "style-mismatch")
        self.assertEqual(defects[0]["severity"], "hard")

    def test_opaque_color_is_not_satisfied_by_a_translucent_color(
        self,
    ) -> None:
        result = self._compare_style(
            {"backgroundColor": "#000000"}, {"backgroundColor": "#00000099"}
        )
        self.assertEqual(len(self._style_defects(result)), 1)

    def test_color_comparison_ignores_hex_case_only(self) -> None:
        result = self._compare_style(
            {"color": "#112233"}, {"color": "#112233"}
        )
        self.assertEqual(self._style_defects(result), [])
        result = self._compare_style(
            {"color": "#112233"}, {"color": "#112234"}
        )
        self.assertEqual(len(self._style_defects(result)), 1)

    def test_line_height_ratio_within_tolerance_passes(self) -> None:
        result = self._compare_style(
            {"fontSize": 16, "lineHeight": 1.4},
            {"fontSize": 16, "lineHeight": 1.4005},
        )
        self.assertEqual(self._style_defects(result), [])
        result = self._compare_style(
            {"fontSize": 16, "lineHeight": 1.4},
            {"fontSize": 16, "lineHeight": 1.6},
        )
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["expected"], {"lineHeight": 1.4})

    def test_text_transform_requires_exact_equality(self) -> None:
        result = self._compare_style(
            {"textTransform": "none"}, {"textTransform": "uppercase"}
        )
        self.assertEqual(len(self._style_defects(result)), 1)

    def test_box_shadow_is_satisfied_by_filter_drop_shadow(self) -> None:
        result = self._compare_style(
            {"boxShadow": [shadow()]},
            {
                "boxShadow": [],
                "filterDropShadows": [
                    {
                        "offsetX": 0,
                        "offsetY": 4,
                        "blurRadius": 8,
                        "color": "#0000001A",
                    }
                ],
            },
        )
        self.assertEqual(self._style_defects(result), [])
        self.assertTrue(result["passed"], result["defects"])

    def test_drop_shadow_with_a_different_color_is_a_defect(self) -> None:
        result = self._compare_style(
            {"boxShadow": [shadow()]},
            {
                "boxShadow": [],
                "filterDropShadows": [
                    {
                        "offsetX": 0,
                        "offsetY": 4,
                        "blurRadius": 8,
                        "color": "#00000033",
                    }
                ],
            },
        )
        self.assertEqual(len(self._style_defects(result)), 1)

    def test_box_shadow_with_spread_is_not_satisfiable_by_drop_shadow(
        self,
    ) -> None:
        result = self._compare_style(
            {"boxShadow": [shadow(spreadRadius=2)]},
            {
                "boxShadow": [],
                "filterDropShadows": [
                    {
                        "offsetX": 0,
                        "offsetY": 4,
                        "blurRadius": 8,
                        "color": "#0000001A",
                    }
                ],
            },
        )
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "style-mismatch")
        self.assertIn("drop-shadow", defects[0]["message"])

    def test_inset_box_shadow_is_not_satisfiable_by_drop_shadow(self) -> None:
        result = self._compare_style(
            {"boxShadow": [shadow(inset=True)]},
            {
                "boxShadow": [],
                "filterDropShadows": [
                    {
                        "offsetX": 0,
                        "offsetY": 4,
                        "blurRadius": 8,
                        "color": "#0000001A",
                    }
                ],
            },
        )
        self.assertEqual(len(self._style_defects(result)), 1)

    def test_box_shadow_count_mismatch_is_a_defect(self) -> None:
        result = self._compare_style(
            {"boxShadow": [shadow(), shadow(offsetY=8)]},
            {"boxShadow": [shadow()], "filterDropShadows": []},
        )
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["actual"]["boxShadow"], [shadow()])

    def test_matching_box_shadows_pass_element_wise(self) -> None:
        result = self._compare_style(
            {"boxShadow": [shadow(), shadow(offsetY=8, inset=True)]},
            {
                "boxShadow": [shadow(), shadow(offsetY=8, inset=True)],
                "filterDropShadows": [],
            },
        )
        self.assertEqual(self._style_defects(result), [])

    def test_gradient_angle_within_tolerance_passes(self) -> None:
        result = self._compare_style(
            {"backgroundGradient": gradient(180)},
            {"backgroundGradient": gradient(180.4)},
        )
        self.assertEqual(self._style_defects(result), [])

    def test_gradient_angle_beyond_tolerance_fails(self) -> None:
        result = self._compare_style(
            {"backgroundGradient": gradient(180)},
            {"backgroundGradient": gradient(181)},
        )
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 1)
        self.assertIn("angle", defects[0]["message"])

    def test_gradient_stop_drift_fails(self) -> None:
        drifted = gradient(180)
        drifted["stops"][1] = {"color": "#112233", "position": 0.8}
        result = self._compare_style(
            {"backgroundGradient": gradient(180)},
            {"backgroundGradient": drifted},
        )
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 1)
        self.assertIn("stop 1", defects[0]["message"])

    def test_missing_gradient_reports_the_raw_background_image(self) -> None:
        result = self._compare_style(
            {"backgroundGradient": gradient(180)},
            {
                "backgroundGradient": None,
                "backgroundImageRaw": "url(\"/assets/hero.png\")",
            },
        )
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "gradient-missing")
        self.assertEqual(defects[0]["severity"], "hard")
        self.assertEqual(
            defects[0]["actual"]["backgroundImageRaw"],
            'url("/assets/hero.png")',
        )

    def test_absent_expected_gradient_is_skipped(self) -> None:
        result = self._compare_style(
            {"backgroundGradient": None, "paddingTop": 12},
            {"paddingTop": 12},
        )
        self.assertEqual(self._style_defects(result), [])
        self.assertEqual(result["gates"]["style"]["checked"], 1)

    def test_empty_style_contract_warns_on_a_canonical_element(self) -> None:
        result = self._compare_style({}, {"paddingTop": 12})
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "empty-style-contract")
        self.assertEqual(defects[0]["severity"], "warning")
        self.assertEqual(defects[0]["elementNodeId"], ELEMENT_ID)
        self.assertTrue(result["gates"]["style"]["passed"])
        self.assertTrue(result["passed"], result["defects"])
        self.assertEqual(result["gates"]["style"]["warnings"], 1)

    def test_empty_style_contract_is_silent_for_legacy_screens(self) -> None:
        result = self._compare_style(
            {}, {"paddingTop": 12}, canonical=False
        )
        self.assertEqual(self._style_defects(result), [])

    def test_empty_style_contract_is_silent_for_hidden_elements(self) -> None:
        result = self._compare_style(
            {},
            {"paddingTop": 12},
            element_overrides={"hidden": True, "effectiveHidden": True},
        )
        self.assertEqual(self._style_defects(result), [])

    def test_style_defect_names_the_resolved_design_token(self) -> None:
        result = self._compare_style(
            {"backgroundColor": "#112233"},
            {"backgroundColor": "#445566"},
            element_overrides={
                "tokenBindings": {"backgroundColor": "VariableID:9:1"},
                "resolvedTokens": {"backgroundColor": "color/primary-500"},
            },
        )
        defects = self._style_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["expectedToken"], "color/primary-500")

    def test_style_defect_falls_back_to_the_token_binding_id(self) -> None:
        result = self._compare_style(
            {"backgroundColor": "#112233"},
            {"backgroundColor": "#445566"},
            element_overrides={
                "tokenBindings": {"backgroundColor": "VariableID:9:1"}
            },
        )
        self.assertEqual(
            self._style_defects(result)[0]["expectedToken"], "VariableID:9:1"
        )

    def test_style_defect_without_a_token_carries_no_token_field(self) -> None:
        result = self._compare_style(
            {"backgroundColor": "#112233"}, {"backgroundColor": "#445566"}
        )
        self.assertNotIn("expectedToken", self._style_defects(result)[0])

    def _accounting_result(
        self, accounting: dict[str, Any] | None, *, canonical: bool = True
    ) -> dict[str, Any]:
        self._write_bundle(
            [expected_element(style={"paddingTop": 12})],
            canonical=canonical,
            accounting=accounting,
        )
        actual_path = self._write_actual(
            {
                ELEMENT_ID: {
                    "name": "Primary button",
                    "rect": dict(RECT),
                    "style": {"paddingTop": 12},
                }
            }
        )
        return self._validate(actual_path)

    def test_accounting_violations_fail_the_gate(self) -> None:
        result = self._accounting_result(
            {
                "schemaVersion": "1.0",
                "featureId": FEATURE_ID,
                "restCollection": {"complete": True},
                "counts": {"screens": 1, "nodes": 1, "violations": 1},
                "unsupported": [],
                "violations": [
                    {
                        "screenNodeId": SCREEN_ID,
                        "nodeId": ELEMENT_ID,
                        "key": "quantumFillMode",
                        "reason": "unknown canonical property",
                    }
                ],
                "unresolvedTokenBindings": [],
            }
        )
        defects = [
            item for item in result["defects"] if item["gate"] == "accounting"
        ]
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "accounting-violation")
        self.assertEqual(defects[0]["severity"], "hard")
        self.assertEqual(defects[0]["elementNodeId"], ELEMENT_ID)
        self.assertEqual(result["gates"]["accounting"]["status"], "FAIL")
        self.assertFalse(result["passed"])

    def test_accounting_unsupported_properties_only_warn(self) -> None:
        result = self._accounting_result(
            {
                "schemaVersion": "1.0",
                "featureId": FEATURE_ID,
                "restCollection": {"complete": True},
                "counts": {"screens": 1, "nodes": 1, "unsupported": 1},
                "unsupported": [
                    {
                        "screenNodeId": SCREEN_ID,
                        "nodeId": ELEMENT_ID,
                        "key": "blendMode",
                        "reason": "blend mode is not compiled: MULTIPLY",
                    }
                ],
                "violations": [],
                "unresolvedTokenBindings": [],
            }
        )
        defects = [
            item for item in result["defects"] if item["gate"] == "accounting"
        ]
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(
            defects[0]["type"], "accounting-unsupported-property"
        )
        self.assertEqual(defects[0]["severity"], "warning")
        self.assertEqual(result["gates"]["accounting"]["status"], "PASS")
        self.assertTrue(result["passed"], result["defects"])

    def test_unresolved_token_bindings_only_warn(self) -> None:
        result = self._accounting_result(
            {
                "schemaVersion": "1.0",
                "featureId": FEATURE_ID,
                "restCollection": {"complete": True},
                "counts": {"screens": 1, "nodes": 1},
                "unsupported": [],
                "violations": [],
                "unresolvedTokenBindings": [
                    {
                        "screenNodeId": SCREEN_ID,
                        "nodeId": ELEMENT_ID,
                        "styleKey": "backgroundColor",
                        "variableId": "VariableID:9:1",
                    }
                ],
            }
        )
        defects = [
            item for item in result["defects"] if item["gate"] == "accounting"
        ]
        self.assertEqual(
            [item["severity"] for item in defects], ["warning"]
        )
        self.assertTrue(result["passed"], result["defects"])

    def test_legacy_bundle_without_accounting_is_not_evaluated(self) -> None:
        """An MCP-only bundle never had accounting to produce."""

        result = self._accounting_result(None, canonical=False)
        self.assertEqual(
            result["gates"]["accounting"],
            {
                "passed": True,
                "status": "NOT_EVALUATED",
                "checked": 0,
                "hardFailures": 0,
                "warnings": 0,
            },
        )
        self.assertIsNone(result["accounting"])
        self.assertTrue(result["passed"], result["defects"])

    def test_canonical_bundle_without_accounting_fails(self) -> None:
        """Deleting one file must not delete a gate.

        A canonical bundle's accounting artifact is the only thing asserting
        the compiler dropped none of the REST evidence. Absent, this gate used
        to record `NOT_EVALUATED` and pass, on the reasoning that coverage
        would report the gap -- which it does not, because coverage reads only
        what the bundle wrote about itself.
        """

        result = self._accounting_result(None)
        gate = result["gates"]["accounting"]
        self.assertEqual(gate["checked"], 0)
        self.assertEqual(gate["status"], "FAIL")
        self.assertEqual(
            [
                item["type"]
                for item in self._gate_defects(result, "accounting")
            ],
            ["gate-not-evaluated"],
        )
        self.assertFalse(result["passed"])

    def test_report_renders_accounting_and_defect_types(self) -> None:
        self._write_bundle(
            [expected_element(style={})],
            accounting={
                "schemaVersion": "1.0",
                "featureId": FEATURE_ID,
                "restCollection": {"complete": True},
                "counts": {"screens": 1, "nodes": 1, "unsupported": 1},
                "unsupported": [
                    {
                        "screenNodeId": SCREEN_ID,
                        "nodeId": ELEMENT_ID,
                        "key": "blendMode",
                        "reason": "blend mode is not compiled: MULTIPLY",
                    }
                ],
                "violations": [],
                "unresolvedTokenBindings": [],
            },
        )
        actual_path = self._write_actual(
            {
                ELEMENT_ID: {
                    "name": "Primary button",
                    "rect": dict(RECT),
                    "style": {"paddingTop": 12},
                }
            }
        )
        result = self._validate(actual_path)
        document = render_report(result, self.output).read_text(
            encoding="utf-8"
        )
        self.assertIn("Property accounting", document)
        self.assertIn("empty-style-contract", document)
        self.assertIn("accounting-unsupported-property", document)


if __name__ == "__main__":
    unittest.main()
