"""A gate's status must reflect approved deviations, not a pre-approval snapshot.

`GateValidator._finish_gate` caches each gate's status right after that gate
runs, before `_apply_approved_deviations` reclassifies some of its hard
defects. Left uncorrected, `result["gates"]["structure"]["status"]` reads FAIL
forever even after every one of its hard defects was excused -- while
`result["passed"]` and `result["summary"]["hardFailures"]` already read the
post-deviation truth. That contradiction is exactly what a reviewer hits when
skimming the gate table: the run passed, but three gates say FAIL and nothing
explains why. `render_report` reconciles this at report time; these tests
pin the reconciled fields and their original-value counterparts.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from figma_lossless.report import render_report
from figma_lossless.util import write_json
from figma_lossless.validators import GateValidator, ValidateOptions

from canonical_bundle import attach_accounting, clean_accounting


FEATURE_ID = "gate-reconciliation"
SCREEN_ID = "1:1"
MISSING_ID = "1:2"
RECT = {"x": 0, "y": 0, "width": 10, "height": 10}


class GateReconciliationHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.output = self.root / "report"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_bundle(self) -> None:
        write_json(
            self.bundle / "screens" / "1-1.json",
            {
                "schemaVersion": "1.0",
                "nodeId": SCREEN_ID,
                "name": "Screen",
                "evidenceSource": "figma-rest",
                "viewport": {"width": 10, "height": 10},
                "referenceScreenshot": None,
                "referenceCode": None,
                "texts": [],
                "elements": [
                    {
                        "nodeId": MISSING_ID,
                        "type": "frame",
                        "figmaType": "FRAME",
                        "name": "Panel",
                        "parentNodeId": None,
                        "hidden": False,
                        "effectiveHidden": False,
                        "rect": dict(RECT),
                        "style": {"backgroundColor": "#FFFFFF"},
                    }
                ],
                "assets": [],
                "variables": {},
                "canonicalEvidence": {
                    "path": "rest/nodes/1-1.json",
                    "sha256": "0" * 64,
                    "bytes": 1,
                },
                "status": {"contextFetched": True, "specCompiled": True},
            },
        )
        accounting = clean_accounting(FEATURE_ID)
        write_json(self.bundle / "property-accounting.json", accounting)
        write_json(
            self.bundle / "manifest.json",
            attach_accounting(
                {
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
                },
                accounting,
            ),
        )
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

    def _write_actual(self) -> Path:
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
                        # MISSING_ID is contracted but never rendered, so the
                        # structure gate raises a hard "missing-element" defect.
                        "elements": {},
                        "assets": {},
                    }
                },
                "components": {},
            },
        )
        return path

    def _validate(self, **config: Any) -> dict[str, Any]:
        self._write_bundle()
        actual_path = self._write_actual()
        config_path = self.root / "config.json"
        write_json(
            config_path,
            {
                "requireReferenceScreenshots": False,
                "requireAssetFreeze": False,
                **config,
            },
        )
        return GateValidator(
            ValidateOptions(
                self.bundle, actual_path, self.output, config_path=config_path
            )
        ).validate()


class GateStatusReconciliationTest(GateReconciliationHarness):
    def test_unapproved_structure_defect_fails_the_gate(self) -> None:
        result = self._validate()

        self.assertFalse(result["passed"])
        self.assertEqual(result["gates"]["structure"]["status"], "FAIL")

    def test_approved_deviation_flips_the_gate_to_pass(self) -> None:
        result = self._validate(
            approvedDeviations=[
                {"gate": "structure", "reason": "디자인 쪽 실수로 확인됨"}
            ]
        )

        # The approval named only the structure gate's defect; other gates
        # (geometry, style, ...) still have their own unapproved hard
        # defects for the same missing element, so the run overall still
        # fails. That is exactly why the structure gate's OWN status must
        # be judged on its own reconciled defects, not on `result["passed"]`.
        structure_defects = [
            item for item in result["defects"] if item["gate"] == "structure"
        ]
        self.assertTrue(structure_defects)
        self.assertTrue(
            all(item["severity"] == "approved-deviation" for item in structure_defects)
        )

        html = render_report(result, self.output).read_text(encoding="utf-8")
        report_data = self._read_report_data()
        gate = report_data["gates"]["structure"]

        # The gate-level fields must now agree with the run-level truth.
        self.assertEqual(gate["status"], "PASS")
        self.assertTrue(gate["passed"])
        self.assertEqual(gate["hardFailures"], 0)

        # ...while the pre-approval numbers stay on the record.
        self.assertEqual(gate["originalStatus"], "FAIL")
        self.assertFalse(gate["originalPassed"])
        self.assertGreaterEqual(gate["originalHardFailures"], 1)
        self.assertEqual(
            gate["reclassifiedHard"], gate["originalHardFailures"]
        )

        # "Passed gates" in the summary must count reconciled statuses, not
        # the stale pre-approval ones.
        self.assertEqual(
            report_data["summary"]["passedGates"],
            sum(
                1
                for g in report_data["gates"].values()
                if g["status"] == "PASS"
            ),
        )
        self.assertIn("structure", [
            name
            for name, g in report_data["gates"].items()
            if g["status"] == "PASS"
        ])

        # The HTML must not silently say PASS with no trace of the excuse.
        self.assertIn("PASS", html)
        self.assertIn("reclassified as approved", html)
        self.assertIn("was FAIL", html)

    def _read_report_data(self) -> dict[str, Any]:
        from figma_lossless.util import read_json

        return read_json(self.output / "report-data.json")


if __name__ == "__main__":
    unittest.main()
