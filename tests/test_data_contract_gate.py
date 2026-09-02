"""Treat reviewed data slots as bindings without weakening ordinary copy.

The motivating failure is concrete: exact-copy comparison rewards a product
for hard-coding the sample profile from Figma and punishes it for showing a
real user's value. A reviewed data contract reverses that rule only for named
nodes. Everything else in the screen remains literal, and every exclusion or
approval stays visible in the report.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from figma_lossless.cli import main
from figma_lossless.report import render_report
from figma_lossless.util import read_json, write_json
from figma_lossless.validators import (
    DEFAULT_CONFIG,
    GateValidator,
    ValidateOptions,
)


FEATURE_ID = "data-contract-gate"
SCREEN_ID = "1:1"
SLOT_ID = "1:2"
CHROME_ID = "1:3"
LABEL_ID = "1:4"
EXEMPLAR = "design@example.com"


def malformed_visibility_variants() -> dict[str, dict[str, Any]]:
    valid_rect = {"width": 10, "height": 10}
    valid_style = {
        "display": "block",
        "visibility": "visible",
        "opacity": "1",
    }
    return {
        "rendered-null": {
            "rendered": None,
            "rect": valid_rect,
            "style": valid_style,
        },
        "rendered-not-bool": {
            "rendered": 1,
            "rect": valid_rect,
            "style": valid_style,
        },
        "width-null": {
            "rendered": True,
            "rect": {**valid_rect, "width": None},
            "style": valid_style,
        },
        "width-bool": {
            "rendered": True,
            "rect": {**valid_rect, "width": True},
            "style": valid_style,
        },
        "height-negative": {
            "rendered": True,
            "rect": {**valid_rect, "height": -1},
            "style": valid_style,
        },
        "width-infinity": {
            "rendered": True,
            "rect": {**valid_rect, "width": float("inf")},
            "style": valid_style,
        },
        "height-nan": {
            "rendered": True,
            "rect": {**valid_rect, "height": float("nan")},
            "style": valid_style,
        },
        "display-empty": {
            "rendered": True,
            "rect": valid_rect,
            "style": {**valid_style, "display": ""},
        },
        "visibility-empty": {
            "rendered": True,
            "rect": valid_rect,
            "style": {**valid_style, "visibility": "   "},
        },
        "opacity-bool": {
            "rendered": True,
            "rect": valid_rect,
            "style": {**valid_style, "opacity": True},
        },
        "opacity-nonnumeric": {
            "rendered": True,
            "rect": valid_rect,
            "style": {**valid_style, "opacity": "opaque"},
        },
        "opacity-negative": {
            "rendered": True,
            "rect": valid_rect,
            "style": {**valid_style, "opacity": -0.1},
        },
        "opacity-out-of-range": {
            "rendered": True,
            "rect": valid_rect,
            "style": {**valid_style, "opacity": 1.1},
        },
        "opacity-infinity": {
            "rendered": True,
            "rect": valid_rect,
            "style": {**valid_style, "opacity": float("inf")},
        },
        "opacity-nan": {
            "rendered": True,
            "rect": valid_rect,
            "style": {**valid_style, "opacity": float("nan")},
        },
    }


class DataContractGateHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.output = self.root / "report"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_bundle(
        self, *, with_contract: bool = True, with_slots: bool = True
    ) -> None:
        write_json(
            self.bundle / "screens" / "1-1.json",
            {
                "schemaVersion": "1.0",
                "nodeId": SCREEN_ID,
                "name": "Profile",
                "evidenceSource": "figma-mcp",
                "viewport": {"width": 10, "height": 10},
                "referenceScreenshot": None,
                "referenceCode": None,
                "texts": [
                    {"nodeId": SLOT_ID, "value": EXEMPLAR, "exact": True},
                    {"nodeId": CHROME_ID, "value": "9:41", "exact": True},
                    {"nodeId": LABEL_ID, "value": "Email", "exact": True},
                ],
                "elements": [
                    {"nodeId": SLOT_ID, "name": "Email value"},
                    {"nodeId": CHROME_ID, "name": "Status bar time"},
                    {"nodeId": LABEL_ID, "name": "Email label"},
                ],
                "assets": [],
                "variables": {},
                "status": {"contextFetched": True, "specCompiled": True},
            },
        )
        contracts: dict[str, str | None] = {
            "flowContract": None,
            "componentMap": None,
        }
        if with_contract:
            write_json(
                self.bundle / "contracts" / "data-contract.json",
                {
                    "schemaVersion": 1,
                    "featureId": FEATURE_ID,
                    "slots": (
                        [
                            {
                                "screenNodeId": SCREEN_ID,
                                "nodeId": SLOT_ID,
                                "binding": "user.email",
                                "shape": "email",
                                "designExemplar": EXEMPLAR,
                            }
                        ]
                        if with_slots
                        else []
                    ),
                    "chrome": [
                        {"screenNodeId": SCREEN_ID, "nodeId": CHROME_ID}
                    ],
                },
            )
            contracts["dataContract"] = "contracts/data-contract.json"
        write_json(
            self.bundle / "manifest.json",
            {
                "schemaVersion": "1.0",
                "featureId": FEATURE_ID,
                "screens": [
                    {
                        "nodeId": SCREEN_ID,
                        "name": "Profile",
                        "compiledPath": "screens/1-1.json",
                    }
                ],
                "contracts": contracts,
            },
        )
        write_json(
            self.bundle / "coverage.json",
            [
                {
                    "nodeId": SCREEN_ID,
                    "name": "Profile",
                    "status": {"contextFetched": True, "specCompiled": True},
                }
            ],
        )

    def _write_actual(
        self,
        slot: str,
        *,
        chrome: str = "not the design clock",
        label: str = "Email",
        filename: str = "actual.json",
        route_kind: str | None = None,
        slot_marker: bool = True,
        duplicate_slot: bool = False,
    ) -> Path:
        path = self.root / filename
        actual_screen = {
            "name": "Profile",
            "route": "/profile",
            "screenshot": None,
        }
        if route_kind is not None:
            actual_screen["routeKind"] = route_kind
        if route_kind == "product":
            actual_screen["slots"] = (
                {
                    "user.email": {
                        "text": slot,
                        "copy": {"textContent": slot, "tag": "span"},
                        "rendered": True,
                        "rect": {"x": 0, "y": 0, "width": 10, "height": 10},
                        "style": {
                            "display": "block",
                            "visibility": "visible",
                            "opacity": "1",
                        },
                    }
                }
                if slot_marker
                else {}
            )
            actual_screen["duplicateSlots"] = (
                ["user.email"] if duplicate_slot else []
            )
        else:
            actual_screen["elements"] = {
                SLOT_ID: {
                    "text": slot,
                    "copy": {"textContent": slot, "tag": "span"},
                },
                CHROME_ID: {
                    "text": chrome,
                    "copy": {"textContent": chrome, "tag": "span"},
                },
                LABEL_ID: {
                    "text": label,
                    "copy": {"textContent": label, "tag": "span"},
                },
            }
            actual_screen["assets"] = {}
        write_json(
            path,
            {
                "schemaVersion": "1.0",
                "featureId": FEATURE_ID,
                "provenance": "browser-capture",
                "screens": {SCREEN_ID: actual_screen},
                "components": {},
            },
        )
        return path

    def _validate(
        self,
        slot: str,
        *,
        with_contract: bool = True,
        with_slots: bool = True,
        chrome: str = "not the design clock",
        label: str = "Email",
        route_kind: str | None = None,
        slot_marker: bool = True,
        duplicate_slot: bool = False,
        slot_visibility: dict[str, Any] | None = None,
        **config: Any,
    ) -> dict[str, Any]:
        self._write_bundle(
            with_contract=with_contract, with_slots=with_slots
        )
        actual = self._write_actual(
            slot,
            chrome=chrome,
            label=label,
            route_kind=route_kind,
            slot_marker=slot_marker,
            duplicate_slot=duplicate_slot,
        )
        if slot_visibility is not None and route_kind != "product":
            payload = read_json(actual)
            payload["screens"][SCREEN_ID]["elements"][SLOT_ID].update(
                slot_visibility
            )
            write_json(actual, payload)
            if isinstance(slot_visibility.get("rect"), dict):
                compiled = self.bundle / "screens" / "1-1.json"
                screen = read_json(compiled)
                next(
                    item
                    for item in screen["elements"]
                    if item["nodeId"] == SLOT_ID
                )["rect"] = slot_visibility["rect"]
                write_json(compiled, screen)
        config_path = self.root / "config.json"
        write_json(
            config_path,
            {
                "requireReferenceScreenshots": False,
                "requireFlowContract": False,
                "requireComponentContract": False,
                **config,
            },
        )
        return GateValidator(
            ValidateOptions(
                self.bundle, actual, self.output, config_path=config_path
            )
        ).validate()

    def _copy_defects(
        self, result: dict[str, Any], *, severity: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in result["defects"]
            if item["gate"] == "copy"
            and (severity is None or item["severity"] == severity)
        ]


class SlotCopyGateTest(DataContractGateHarness):
    missing_rendered_evidence = {
        "rect": {"width": 10, "height": 10},
        "style": {
            "display": "block",
            "visibility": "visible",
            "opacity": "1",
        },
    }

    def test_missing_rendered_field_reports_visibility_unknown(self) -> None:
        for name, visibility in (
            ("no-visibility-keys", {}),
            ("legacy-rect-and-style", self.missing_rendered_evidence),
        ):
            with self.subTest(name=name):
                result = self._validate(
                    "person@example.net",
                    slot_visibility=visibility,
                )

                unknown = [
                    item
                    for item in self._copy_defects(result)
                    if item["type"] == "slot-visibility-unknown"
                ]
                self.assertEqual(len(unknown), 1, result["defects"])
                self.assertEqual(unknown[0]["severity"], "warning")
                self.assertIn("latest adapter", unknown[0]["message"])
                self.assertNotIn(
                    "slot-hidden",
                    {item["type"] for item in self._copy_defects(result)},
                )

    def test_explicitly_hidden_slot_remains_a_hard_failure(self) -> None:
        result = self._validate(
            "person@example.net",
            slot_visibility={
                **self.missing_rendered_evidence,
                "rendered": False,
            },
        )

        hidden = [
            item
            for item in self._copy_defects(result)
            if item["type"] == "slot-hidden"
        ]
        self.assertEqual(len(hidden), 1, result["defects"])
        self.assertEqual(hidden[0]["severity"], "hard")
        self.assertNotIn(
            "slot-visibility-unknown",
            {item["type"] for item in self._copy_defects(result)},
        )

    def test_visibility_unknown_and_value_defect_are_both_reported(self) -> None:
        result = self._validate(
            EXEMPLAR,
            slot_visibility=self.missing_rendered_evidence,
        )

        self.assertTrue(
            {"slot-visibility-unknown", "slot-not-bound"}.issubset(
                {item["type"] for item in self._copy_defects(result)}
            ),
            result["defects"],
        )

    def test_visibility_unknown_is_hard_when_product_route_is_required(
        self,
    ) -> None:
        result = self._validate(
            "person@example.net",
            slot_visibility=self.missing_rendered_evidence,
            requireProductRouteCheck=True,
        )

        unknown = next(
            item
            for item in self._copy_defects(result)
            if item["type"] == "slot-visibility-unknown"
        )
        self.assertEqual(unknown["severity"], "hard")

    def test_visibility_unknown_cannot_be_approved(self) -> None:
        result = self._validate(
            "person@example.net",
            slot_visibility=self.missing_rendered_evidence,
            requireProductRouteCheck=True,
            approvedDeviations=[
                {
                    "gate": "copy",
                    "type": "slot-visibility-unknown",
                    "screenNodeId": SCREEN_ID,
                    "nodeId": SLOT_ID,
                    "reason": "The missing measurement is acceptable.",
                }
            ],
        )

        unknown = next(
            item
            for item in self._copy_defects(result)
            if item["type"] == "slot-visibility-unknown"
        )
        self.assertEqual(unknown["severity"], "hard")
        self.assertEqual(
            result["approvedDeviations"]["unusedEntries"][0]["type"],
            "slot-visibility-unknown",
        )

    def test_the_design_exemplar_is_rejected_as_an_unbound_slot(self) -> None:
        result = self._validate(EXEMPLAR)

        defect = next(
            item
            for item in self._copy_defects(result, severity="hard")
            if item["type"] == "slot-not-bound"
        )
        self.assertEqual(defect["binding"], "user.email")
        self.assertFalse(result["passed"])

    def test_an_empty_or_whitespace_slot_is_hard_failure(self) -> None:
        for value in ("", " \t\n"):
            with self.subTest(value=value):
                result = self._validate(value)
                self.assertIn(
                    "slot-empty",
                    {
                        item["type"]
                        for item in self._copy_defects(result, severity="hard")
                    },
                )

    def test_a_declared_shape_rejects_the_wrong_format(self) -> None:
        result = self._validate("not an email")

        self.assertIn(
            "slot-shape-mismatch",
            {
                item["type"]
                for item in self._copy_defects(result, severity="hard")
            },
        )

    def test_real_data_passes_without_falling_back_to_exact_copy(self) -> None:
        result = self._validate("person@example.net")

        hard_types = {
            item["type"]
            for item in self._copy_defects(result, severity="hard")
        }
        self.assertNotIn("copy-mismatch", hard_types)
        self.assertNotIn("slot-shape-mismatch", hard_types)
        self.assertEqual(hard_types, set())

    def test_static_copy_outside_the_contract_remains_exact(self) -> None:
        result = self._validate("person@example.net", label="E-mail")

        mismatch = next(
            item
            for item in self._copy_defects(result, severity="hard")
            if item["type"] == "copy-mismatch"
        )
        self.assertEqual(mismatch["elementNodeId"], LABEL_ID)


class ChromeAndContractPresenceTest(DataContractGateHarness):
    def test_chrome_leaves_copy_comparison_but_stays_in_the_ledger(self) -> None:
        result = self._validate("person@example.net")

        self.assertFalse(
            any(
                item.get("elementNodeId") == CHROME_ID
                and item["type"] == "copy-mismatch"
                for item in result["defects"]
            )
        )
        ledger = result["exclusions"]["copy"]
        self.assertEqual(
            [(item["screenNodeId"], item["nodeId"]) for item in ledger],
            [(SCREEN_ID, CHROME_ID)],
        )
        self.assertIn("data contract", ledger[0]["reason"])

    def test_the_binding_is_present_in_json_and_html_reports(self) -> None:
        result = self._validate("person@example.net")

        self.assertEqual(
            result["dataContract"]["slots"][0]["binding"], "user.email"
        )
        html = render_report(result, self.output).read_text(encoding="utf-8")
        self.assertIn("user.email", html)

    def test_absence_keeps_exact_copy_and_warns_once_per_run(self) -> None:
        self.assertFalse(DEFAULT_CONFIG["requireDataContract"])

        result = self._validate(EXEMPLAR, with_contract=False)

        warnings = [
            item
            for item in result["defects"]
            if item["type"] == "data-contract-absent"
        ]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["severity"], "warning")
        self.assertEqual(result["gates"]["copy"]["checked"], 3)

    def test_absence_is_hard_when_the_project_requires_a_contract(self) -> None:
        result = self._validate(
            EXEMPLAR, with_contract=False, requireDataContract=True
        )

        absence = next(
            item
            for item in result["defects"]
            if item["type"] == "data-contract-absent"
        )
        self.assertEqual(absence["severity"], "hard")
        self.assertFalse(result["passed"])


class SlotApprovalTest(DataContractGateHarness):
    def test_all_three_measured_slot_defects_can_be_approved(self) -> None:
        cases = (
            (EXEMPLAR, "slot-not-bound"),
            ("", "slot-empty"),
            ("not an email", "slot-shape-mismatch"),
        )
        for value, defect_type in cases:
            with self.subTest(defect_type=defect_type):
                result = self._validate(
                    value,
                    approvedDeviations=[
                        {
                            "gate": "copy",
                            "type": defect_type,
                            "screenNodeId": SCREEN_ID,
                            "nodeId": SLOT_ID,
                            "reason": "The measured design difference is accepted.",
                        }
                    ],
                )
                approved = [
                    item
                    for item in result["approvedDeviations"]["defects"]
                    if item["type"] == defect_type
                ]
                self.assertEqual(len(approved), 1)
                self.assertEqual(result["summary"]["hardFailures"], 0)
                self.assertTrue(
                    result["coverage"][0]["status"]["copyPassed"]
                )
                self.assertTrue(result["passed"], result["defects"])

    def test_slot_approvals_without_a_node_scope_are_rejected(self) -> None:
        for value, defect_type in (
            (EXEMPLAR, "slot-not-bound"),
            ("", "slot-empty"),
            ("not an email", "slot-shape-mismatch"),
        ):
            with self.subTest(defect_type=defect_type):
                result = self._validate(
                    value,
                    approvedDeviations=[
                        {
                            "type": defect_type,
                            "reason": "This must not approve every slot.",
                        }
                    ],
                )
                self.assertIn(
                    "approved-deviation-unscoped",
                    {item["type"] for item in result["defects"]},
                )
                defect = next(
                    item
                    for item in result["defects"]
                    if item["type"] == defect_type
                )
                self.assertEqual(defect["severity"], "hard")

    def test_generic_copy_approval_cannot_suppress_a_slot_without_node_scope(
        self,
    ) -> None:
        result = self._validate(
            EXEMPLAR,
            approvedDeviations=[
                {
                    "gate": "copy",
                    "screenNodeId": SCREEN_ID,
                    "reason": "This screen-wide approval must not hide slots.",
                }
            ],
        )

        self.assertIn(
            "approved-deviation-unscoped",
            {item["type"] for item in result["defects"]},
        )
        defect = next(
            item
            for item in result["defects"]
            if item["type"] == "slot-not-bound"
        )
        self.assertEqual(defect["severity"], "hard")


class ProductRouteGateTest(DataContractGateHarness):
    def _route_defects(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            item
            for item in result["defects"]
            if item["gate"] == "product-route"
        ]

    def test_product_capture_verifies_a_screen_with_slots(self) -> None:
        real = self._validate(
            "person@example.net", route_kind="product"
        )
        exemplar = self._validate(EXEMPLAR, route_kind="product")
        empty = self._validate("  ", route_kind="product")
        wrong_shape = self._validate("not an email", route_kind="product")

        self.assertEqual(self._route_defects(real), [])
        self.assertEqual(real["gates"]["product-route"]["checked"], 1)
        self.assertFalse(real["passed"])
        coverage = real["coverage"][0]
        self.assertEqual(
            coverage["status"]["implementationState"],
            "product-verified-only",
        )
        self.assertFalse(coverage["status"]["implemented"])
        self.assertFalse(coverage["status"]["designCoverageComplete"])
        self.assertFalse(real["designCoverageComplete"])
        self.assertEqual(real["gates"]["coverage"]["status"], "INCOMPLETE")
        self.assertFalse(real["gates"]["coverage"]["passed"])
        self.assertFalse(
            real["gates"]["coverage"]["designCoverageComplete"]
        )
        self.assertEqual(
            [
                (item["type"], item["severity"])
                for item in real["defects"]
                if item["gate"] == "coverage"
            ],
            [("product-verified-only", "warning")],
        )
        for result, defect_type in (
            (exemplar, "slot-not-bound"),
            (empty, "slot-empty"),
            (wrong_shape, "slot-shape-mismatch"),
        ):
            with self.subTest(defect_type=defect_type):
                defect = next(
                    item
                    for item in self._route_defects(result)
                    if item["type"] == defect_type
                )
                self.assertEqual(defect["severity"], "hard")
                self.assertFalse(result["passed"])

    def test_fixture_only_capture_warns_that_the_product_route_is_unverified(
        self,
    ) -> None:
        result = self._validate(
            "person@example.net", route_kind="fixture", label="E-mail"
        )
        legacy = self._validate("person@example.net")
        no_slots = self._validate(
            "person@example.net",
            with_slots=False,
            route_kind="fixture",
            requireProductRouteCheck=True,
        )

        self.assertEqual(
            [
                (item["type"], item["severity"])
                for item in self._route_defects(result)
            ],
            [("product-route-unverified", "warning")],
        )
        mismatch = next(
            item for item in result["defects"] if item["type"] == "copy-mismatch"
        )
        self.assertEqual(mismatch["elementNodeId"], LABEL_ID)
        self.assertEqual(
            [item["type"] for item in self._route_defects(legacy)],
            ["product-route-unverified"],
        )
        self.assertNotIn("product-route", no_slots["gates"])

    def test_product_route_check_can_make_fixture_only_capture_hard(
        self,
    ) -> None:
        result = self._validate(
            "person@example.net",
            route_kind="fixture",
            requireProductRouteCheck=True,
        )

        self.assertEqual(
            [
                (item["type"], item["severity"])
                for item in self._route_defects(result)
            ],
            [("product-route-unverified", "hard")],
        )
        self.assertFalse(result["passed"])

    def test_actual_b_product_capture_verifies_the_primary_fixture_pair(
        self,
    ) -> None:
        self._write_bundle()
        actual_a = self._write_actual(
            "person@example.net",
            filename="actual-a.json",
            route_kind="fixture",
        )
        actual_b = self._write_actual(
            "other@example.org",
            filename="actual-b.json",
            route_kind="product",
        )
        config = self.root / "config.json"
        write_json(
            config,
            {
                "requireReferenceScreenshots": False,
                "requireFlowContract": False,
                "requireComponentContract": False,
            },
        )

        result = GateValidator(
            ValidateOptions(
                self.bundle,
                actual_a,
                self.output,
                config_path=config,
                actual_snapshot_b=actual_b,
            )
        ).validate()

        self.assertEqual(self._route_defects(result), [])

    def test_route_verification_absence_cannot_be_approved(self) -> None:
        fixture = self._validate(
            "person@example.net",
            route_kind="fixture",
            requireProductRouteCheck=True,
            approvedDeviations=[
                {
                    "type": "product-route-unverified",
                    "reason": "The fixture is close enough.",
                }
            ],
        )
        missing_marker = self._validate(
            "person@example.net",
            route_kind="product",
            slot_marker=False,
            requireProductRouteCheck=True,
            approvedDeviations=[
                {
                    "type": "slot-marker-missing",
                    "reason": "The marker is probably present elsewhere.",
                }
            ],
        )

        fixture_defect = self._route_defects(fixture)[0]
        self.assertEqual(fixture_defect["severity"], "hard")
        self.assertEqual(
            fixture["approvedDeviations"]["unusedEntries"][0]["type"],
            "product-route-unverified",
        )
        self.assertEqual(
            missing_marker["approvedDeviations"]["unusedEntries"][0]["type"],
            "slot-marker-missing",
        )

    def test_missing_product_slot_marker_warns_or_fails_by_policy(self) -> None:
        warning = self._validate(
            "person@example.net", route_kind="product", slot_marker=False
        )
        hard = self._validate(
            "person@example.net",
            route_kind="product",
            slot_marker=False,
            requireProductRouteCheck=True,
        )

        self.assertEqual(
            next(
                item
                for item in self._route_defects(warning)
                if item["type"] == "slot-marker-missing"
            )["severity"],
            "warning",
        )
        self.assertEqual(
            next(
                item
                for item in self._route_defects(hard)
                if item["type"] == "slot-marker-missing"
            )["severity"],
            "hard",
        )

    def test_duplicate_product_slot_marker_is_reported_as_a_warning(self) -> None:
        result = self._validate(
            "person@example.net", route_kind="product", duplicate_slot=True
        )

        defect = next(
            item
            for item in self._route_defects(result)
            if item["type"] == "slot-marker-duplicated"
        )
        self.assertEqual(defect["severity"], "warning")
        self.assertEqual(defect["actual"]["count"], 2)

    def test_product_capture_is_excluded_from_design_screen_gates(self) -> None:
        result = self._validate(
            "person@example.net", route_kind="product", label="wrong"
        )

        design_gates = {"copy", "geometry", "style", "structure"}
        self.assertFalse(
            any(
                item["gate"] in design_gates
                and item.get("screenNodeId") == SCREEN_ID
                for item in result["defects"]
            ),
            result["defects"],
        )
        html = render_report(result, self.output).read_text(encoding="utf-8")
        self.assertIn("Capture routes", html)
        self.assertIn("/profile", html)
        self.assertIn("product", html)
        self.assertIn("data slots only", html)
        self.assertIn("does not verify", html)
        coverage = result["coverage"][0]["status"]
        for key in (
            "structurePassed",
            "copyPassed",
            "assetPassed",
            "visualPassed",
            "flowPassed",
        ):
            self.assertIsNone(coverage[key])
        self.assertIn("product-verified-only", html)
        self.assertIn("not measured", html)
        self.assertIn("INCOMPLETE", html)
        report_data = read_json(self.output / "report-data.json")
        self.assertEqual(
            report_data["gates"]["coverage"]["status"], "INCOMPLETE"
        )
        self.assertFalse(report_data["gates"]["coverage"]["passed"])

    def test_product_only_coverage_is_hard_when_required(self) -> None:
        result = self._validate(
            "person@example.net",
            route_kind="product",
            requireProductRouteCheck=True,
        )

        defect = next(
            item
            for item in result["defects"]
            if item["type"] == "product-verified-only"
        )
        self.assertEqual(defect["severity"], "hard")
        self.assertEqual(result["gates"]["coverage"]["status"], "FAIL")
        self.assertFalse(result["passed"])

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "validate",
                    "--bundle",
                    str(self.bundle),
                    "--actual",
                    str(self.root / "actual.json"),
                    "--output",
                    str(self.output),
                    "--config",
                    str(self.root / "config.json"),
                ]
            )
        cli_result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(cli_result["status"], "FAIL")
        self.assertEqual(cli_result["coverageStatus"], "FAIL")

    def test_product_only_validation_exits_two_by_default(self) -> None:
        self._write_bundle()
        actual = self._write_actual(
            "person@example.net", route_kind="product"
        )
        config = self.root / "config.json"
        write_json(
            config,
            {
                "requireReferenceScreenshots": False,
                "requireFlowContract": False,
                "requireComponentContract": False,
            },
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "validate",
                    "--bundle",
                    str(self.bundle),
                    "--actual",
                    str(actual),
                    "--output",
                    str(self.output),
                    "--config",
                    str(config),
                ]
            )

        self.assertEqual(exit_code, 2)
        cli_result = json.loads(stdout.getvalue())
        self.assertEqual(cli_result["status"], "INCOMPLETE")
        self.assertEqual(cli_result["coverageStatus"], "INCOMPLETE")
        self.assertFalse(cli_result["passed"])
        gate_results = read_json(self.output / "gate-results.json")
        self.assertFalse(gate_results["passed"])
        self.assertEqual(
            gate_results["gates"]["coverage"]["status"], "INCOMPLETE"
        )

    def test_hidden_product_slots_do_not_skip_value_validation(self) -> None:
        variants = {
            "rendered": {"rendered": False, "rect": {"width": 10, "height": 10}},
            "display": {
                "rect": {"width": 10, "height": 10},
                "style": {"display": "none", "visibility": "visible", "opacity": "1"},
            },
            "visibility": {
                "rect": {"width": 10, "height": 10},
                "style": {"display": "block", "visibility": "hidden", "opacity": "1"},
            },
            "opacity": {
                "rect": {"width": 10, "height": 10},
                "style": {"display": "block", "visibility": "visible", "opacity": "0"},
            },
            "zero-size": {
                "rect": {"width": 0, "height": 10},
                "style": {"display": "block", "visibility": "visible", "opacity": "1"},
            },
        }
        for name, visibility in variants.items():
            with self.subTest(name=name):
                self._write_bundle()
                actual = self._write_actual(
                    EXEMPLAR, route_kind="product"
                )
                payload = read_json(actual)
                payload["screens"][SCREEN_ID]["slots"]["user.email"].update(
                    visibility
                )
                write_json(actual, payload)
                config = self.root / "config.json"
                write_json(
                    config,
                    {
                        "requireReferenceScreenshots": False,
                        "requireFlowContract": False,
                        "requireComponentContract": False,
                    },
                )
                result = GateValidator(
                    ValidateOptions(
                        self.bundle, actual, self.output, config_path=config
                    )
                ).validate()
                hidden = [
                    item
                    for item in result["defects"]
                    if item["type"] == "slot-hidden"
                ]
                self.assertEqual(len(hidden), 1, result["defects"])
                self.assertEqual(hidden[0]["severity"], "hard")
                self.assertEqual(
                    len(
                        [
                            item
                            for item in result["defects"]
                            if item["type"] == "slot-not-bound"
                        ]
                    ),
                    1,
                    result["defects"],
                )

    def test_array_product_slots_are_a_controlled_hard_defect(self) -> None:
        self._write_bundle()
        actual = self._write_actual(
            "person@example.net", route_kind="product"
        )
        payload = read_json(actual)
        payload["screens"][SCREEN_ID]["slots"] = []
        write_json(actual, payload)
        config = self.root / "config.json"
        write_json(
            config,
            {
                "requireReferenceScreenshots": False,
                "requireFlowContract": False,
                "requireComponentContract": False,
            },
        )

        result = GateValidator(
            ValidateOptions(self.bundle, actual, self.output, config_path=config)
        ).validate()

        self.assertIn(
            "product-slot-evidence-invalid",
            {item["type"] for item in result["defects"]},
        )
        self.assertFalse(result["passed"])

    def test_product_slot_requires_complete_visibility_evidence(self) -> None:
        for missing_key in ("rendered", "rect", "style"):
            with self.subTest(missing_key=missing_key):
                self._write_bundle()
                actual = self._write_actual(
                    "person@example.net", route_kind="product"
                )
                payload = read_json(actual)
                payload["screens"][SCREEN_ID]["slots"]["user.email"].pop(
                    missing_key
                )
                write_json(actual, payload)
                config = self.root / "config.json"
                write_json(
                    config,
                    {
                        "requireReferenceScreenshots": False,
                        "requireFlowContract": False,
                        "requireComponentContract": False,
                    },
                )

                result = GateValidator(
                    ValidateOptions(
                        self.bundle, actual, self.output, config_path=config
                    )
                ).validate()

                defect_type = (
                    "slot-visibility-unknown"
                    if missing_key == "rendered"
                    else "product-slot-evidence-invalid"
                )
                invalid = [
                    item
                    for item in result["defects"]
                    if item["type"] == defect_type
                ]
                self.assertEqual(len(invalid), 1, result["defects"])
                self.assertEqual(
                    invalid[0]["severity"],
                    "warning" if missing_key == "rendered" else "hard",
                )
                self.assertNotIn(
                    "slot-hidden", {item["type"] for item in result["defects"]}
                )

    def test_product_slot_rejects_malformed_visibility_evidence(self) -> None:
        for name, visibility in malformed_visibility_variants().items():
            with self.subTest(name=name):
                self._write_bundle()
                actual = self._write_actual(
                    "person@example.net", route_kind="product"
                )
                payload = read_json(actual)
                slot = payload["screens"][SCREEN_ID]["slots"]["user.email"]
                slot.update(visibility)
                write_json(actual, payload)

                result = GateValidator(
                    ValidateOptions(self.bundle, actual, self.output)
                ).validate()

                invalid = [
                    item
                    for item in result["defects"]
                    if item["type"] == "product-slot-evidence-invalid"
                ]
                self.assertEqual(len(invalid), 1, result["defects"])
                self.assertEqual(invalid[0]["severity"], "hard")


class SlotDifferentialTest(DataContractGateHarness):
    def _validate_pair(
        self, value_a: str, value_b: str | None
    ) -> dict[str, Any]:
        self._write_bundle()
        actual_a = self._write_actual(value_a, filename="actual-a.json")
        actual_b = (
            self._write_actual(value_b, filename="actual-b.json")
            if value_b is not None
            else None
        )
        config = self.root / "config.json"
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
                self.bundle,
                actual_a,
                self.output,
                config_path=config,
                actual_snapshot_b=actual_b,
            )
        ).validate()

    def _validate_with_actual_b_element(
        self, element: dict[str, Any]
    ) -> dict[str, Any]:
        self._write_bundle()
        actual_a = self._write_actual(
            "person@example.net", filename="actual-a.json"
        )
        actual_b = self._write_actual(
            "other@example.org", filename="actual-b.json"
        )
        payload = read_json(actual_b)
        payload["screens"][SCREEN_ID]["elements"][SLOT_ID] = element
        write_json(actual_b, payload)
        config = self.root / "config.json"
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
                self.bundle,
                actual_a,
                self.output,
                config_path=config,
                actual_snapshot_b=actual_b,
            )
        ).validate()

    def test_equal_slot_values_in_two_captures_are_not_bound(self) -> None:
        result = self._validate_pair(
            "person@example.net", "person@example.net"
        )

        defect = next(
            item
            for item in result["defects"]
            if item["type"] == "slot-not-bound"
        )
        self.assertEqual(defect["severity"], "hard")
        self.assertTrue(defect["differential"])
        self.assertEqual(defect["binding"], "user.email")
        self.assertFalse(result["passed"])

    def test_different_slot_values_pass_the_differential_check(self) -> None:
        result = self._validate_pair(
            "person@example.net", "other@example.org"
        )

        self.assertNotIn(
            "slot-not-bound", {item["type"] for item in result["defects"]}
        )
        self.assertNotIn(
            "slot-differential-not-run",
            {item["type"] for item in result["defects"]},
        )
        self.assertTrue(result["dataContract"]["differential"]["ran"])

    def test_omitting_the_second_capture_warns_once(self) -> None:
        result = self._validate_pair("person@example.net", None)

        warnings = [
            item
            for item in result["defects"]
            if item["type"] == "slot-differential-not-run"
        ]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["severity"], "warning")
        missing_screens = [
            item
            for item in result["defects"]
            if item["type"] == "slot-differential-screen-missing"
        ]
        self.assertEqual(
            [item["screenNodeId"] for item in missing_screens],
            [SCREEN_ID],
        )
        self.assertFalse(result["dataContract"]["differential"]["ran"])

    def test_validate_cli_accepts_the_second_capture(self) -> None:
        self._write_bundle()
        actual_a = self._write_actual(
            "person@example.net", filename="actual-a.json"
        )
        actual_b = self._write_actual(
            "other@example.org", filename="actual-b.json"
        )
        config = self.root / "config.json"
        write_json(
            config,
            {
                "requireReferenceScreenshots": False,
                "requireFlowContract": False,
                "requireComponentContract": False,
            },
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "validate",
                    "--bundle",
                    str(self.bundle),
                    "--actual",
                    str(actual_a),
                    "--actual-b",
                    str(actual_b),
                    "--output",
                    str(self.output),
                    "--config",
                    str(config),
                ]
            )

        self.assertEqual(exit_code, 0)
        cli_result = json.loads(stdout.getvalue())
        self.assertEqual(cli_result["status"], "PASS")
        self.assertEqual(cli_result["coverageStatus"], "PASS")
        self.assertTrue(cli_result["passed"])

    def test_actual_b_must_match_the_manifest_feature(self) -> None:
        self._write_bundle()
        actual_a = self._write_actual("person@example.net", filename="actual-a.json")
        actual_b = self._write_actual("other@example.org", filename="actual-b.json")
        payload = read_json(actual_b)
        payload["featureId"] = "another-feature"
        write_json(actual_b, payload)

        result = GateValidator(
            ValidateOptions(
                self.bundle, actual_a, self.output, actual_snapshot_b=actual_b
            )
        ).validate()

        self.assertIn(
            "differential-feature-mismatch",
            {item["type"] for item in result["defects"]},
        )
        self.assertFalse(result["dataContract"]["differential"]["ran"])
        self.assertFalse(result["passed"])

    def test_self_test_actual_b_is_not_implementation_evidence(self) -> None:
        self._write_bundle()
        actual_a = self._write_actual("person@example.net", filename="actual-a.json")
        actual_b = self._write_actual("other@example.org", filename="actual-b.json")
        payload = read_json(actual_b)
        payload["provenance"] = "self-test"
        write_json(actual_b, payload)

        result = GateValidator(
            ValidateOptions(
                self.bundle, actual_a, self.output, actual_snapshot_b=actual_b
            )
        ).validate()

        self.assertIn(
            "differential-self-test-evidence-rejected",
            {item["type"] for item in result["defects"]},
        )
        self.assertFalse(result["dataContract"]["differential"]["ran"])

    def test_missing_actual_b_screen_is_named_per_slot_screen(self) -> None:
        self._write_bundle()
        actual_a = self._write_actual("person@example.net", filename="actual-a.json")
        actual_b = self._write_actual("other@example.org", filename="actual-b.json")
        payload = read_json(actual_b)
        payload["screens"] = {}
        write_json(actual_b, payload)

        result = GateValidator(
            ValidateOptions(
                self.bundle, actual_a, self.output, actual_snapshot_b=actual_b
            )
        ).validate()

        missing = [
            item
            for item in result["defects"]
            if item["type"] == "slot-differential-screen-missing"
        ]
        self.assertEqual([item["screenNodeId"] for item in missing], [SCREEN_ID])
        self.assertFalse(result["dataContract"]["differential"]["ran"])

    def test_unreadable_actual_b_slot_does_not_increment_checked(self) -> None:
        self._write_bundle()
        actual_a = self._write_actual("person@example.net", filename="actual-a.json")
        actual_b = self._write_actual("other@example.org", filename="actual-b.json")
        payload = read_json(actual_b)
        payload["screens"][SCREEN_ID]["elements"][SLOT_ID] = {}
        write_json(actual_b, payload)

        result = GateValidator(
            ValidateOptions(
                self.bundle, actual_a, self.output, actual_snapshot_b=actual_b
            )
        ).validate()

        self.assertIn(
            "slot-differential-observation-missing",
            {item["type"] for item in result["defects"]},
        )
        self.assertEqual(result["dataContract"]["differential"]["checked"], 0)
        self.assertFalse(result["dataContract"]["differential"]["ran"])

    def test_hidden_actual_b_slot_is_hard_and_not_compared(self) -> None:
        variants = {
            "rendered": {
                "rendered": False,
            },
            "display": {
                "style": {
                    "display": "none",
                    "visibility": "visible",
                    "opacity": "1",
                },
            },
            "visibility": {
                "style": {
                    "display": "block",
                    "visibility": "hidden",
                    "opacity": "1",
                },
            },
            "opacity": {
                "style": {
                    "display": "block",
                    "visibility": "visible",
                    "opacity": "0",
                },
            },
            "zero-size": {
                "rendered": True,
                "rect": {"width": 0, "height": 10},
                "style": {
                    "display": "block",
                    "visibility": "visible",
                    "opacity": "1",
                },
            },
        }
        for name, visibility in variants.items():
            with self.subTest(name=name):
                result = self._validate_with_actual_b_element(
                    {
                        "text": "other@example.org",
                        "copy": {
                            "textContent": "other@example.org",
                            "tag": "span",
                        },
                        **visibility,
                    }
                )
                hidden = [
                    item
                    for item in result["defects"]
                    if item["gate"] == "differential"
                    and item["type"] == "slot-hidden"
                ]
                self.assertEqual(len(hidden), 1, result["defects"])
                self.assertEqual(hidden[0]["severity"], "hard")
                self.assertEqual(
                    result["dataContract"]["differential"]["checked"], 0
                )
                self.assertFalse(
                    result["dataContract"]["differential"]["ran"]
                )

    def test_empty_or_incomplete_actual_b_observation_is_not_compared(self) -> None:
        variants = {
            "null": {
                "text": None,
                "copy": {"textContent": None, "tag": "span"},
            },
            "whitespace": {
                "text": "   ",
                "copy": {"textContent": "   ", "tag": "span"},
            },
            "partial-visibility": {
                "text": "other@example.org",
                "copy": {
                    "textContent": "other@example.org",
                    "tag": "span",
                },
                "rect": {"width": 10, "height": 10},
            },
        }
        for name, element in variants.items():
            with self.subTest(name=name):
                result = self._validate_with_actual_b_element(element)
                warnings = [
                    item
                    for item in result["defects"]
                    if item["type"]
                    == "slot-differential-observation-missing"
                ]
                self.assertEqual(len(warnings), 1, result["defects"])
                self.assertEqual(warnings[0]["severity"], "warning")
                self.assertEqual(
                    result["dataContract"]["differential"]["checked"], 0
                )
                self.assertFalse(
                    result["dataContract"]["differential"]["ran"]
                )

    def test_malformed_actual_b_visibility_is_not_compared(self) -> None:
        for name, visibility in malformed_visibility_variants().items():
            with self.subTest(name=name):
                result = self._validate_with_actual_b_element(
                    {
                        "text": "other@example.org",
                        "copy": {
                            "textContent": "other@example.org",
                            "tag": "span",
                        },
                        **visibility,
                    }
                )
                warnings = [
                    item
                    for item in result["defects"]
                    if item["type"]
                    == "slot-differential-observation-missing"
                ]
                self.assertEqual(len(warnings), 1, result["defects"])
                self.assertEqual(
                    result["dataContract"]["differential"]["checked"], 0
                )
                self.assertFalse(
                    result["dataContract"]["differential"]["ran"]
                )


class MalformedInputTest(DataContractGateHarness):
    def _validate_path(self, actual: Path, **config: Any) -> dict[str, Any]:
        config_path = self.root / "config.json"
        write_json(
            config_path,
            {
                "requireReferenceScreenshots": False,
                "requireFlowContract": False,
                "requireComponentContract": False,
                **config,
            },
        )
        return GateValidator(
            ValidateOptions(
                self.bundle, actual, self.output, config_path=config_path
            )
        ).validate()

    def test_array_actual_top_level_is_a_controlled_defect(self) -> None:
        self._write_bundle()
        actual = self.root / "actual.json"
        write_json(actual, [])

        result = self._validate_path(actual)

        self.assertIn(
            "snapshot-invalid", {item["type"] for item in result["defects"]}
        )
        self.assertFalse(result["passed"])

    def test_array_actual_screens_is_a_controlled_defect(self) -> None:
        self._write_bundle()
        actual = self._write_actual("person@example.net")
        payload = read_json(actual)
        payload["screens"] = []
        write_json(actual, payload)

        result = self._validate_path(actual)

        self.assertIn(
            "snapshot-screens-invalid",
            {item["type"] for item in result["defects"]},
        )
        self.assertFalse(result["passed"])

    def test_missing_contract_slots_fails_when_contract_is_required(self) -> None:
        self._write_bundle()
        contract_path = self.bundle / "contracts" / "data-contract.json"
        contract = read_json(contract_path)
        contract.pop("slots")
        write_json(contract_path, contract)
        actual = self._write_actual("person@example.net")

        result = self._validate_path(actual, requireDataContract=True)

        self.assertIn(
            "data-contract-invalid",
            {item["type"] for item in result["defects"]},
        )
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
