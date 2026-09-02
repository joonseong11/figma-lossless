"""Flip device chrome from "must render" to "must not render".

A mobile mockup contracts an iOS status bar and a home indicator that a web
build has no business painting. Declaring those subtrees does not wave them
through -- it inverts what is required of them, so the structure gate now fails
if the implementation draws one. These tests pin that inversion, and pin the
absence of the escape hatch that made it unsafe: there is deliberately no
type-based exclusion, because nothing else in the REST pipeline gates an icon.
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


FEATURE_ID = "gate-exclusions"
SCREEN_ID = "1:1"
CHROME_ROOT_ID = "1:2"
CHROME_CHILD_ID = "1:3"
ICON_PATH_ID = "1:4"
CONTENT_ID = "1:5"
RECT = {"x": 0, "y": 0, "width": 10, "height": 10}


class GateExclusionHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.output = self.root / "report"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _element(
        self,
        node_id: str,
        name: str,
        *,
        kind: str = "frame",
        parent: str | None = None,
    ) -> dict[str, Any]:
        return {
            "nodeId": node_id,
            "type": kind,
            "figmaType": kind.upper(),
            "name": name,
            "parentNodeId": parent,
            "hidden": False,
            "effectiveHidden": False,
            "rect": dict(RECT),
            "style": {"backgroundColor": "#FFFFFF"},
        }

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
                "texts": [
                    {
                        "nodeId": CHROME_CHILD_ID,
                        "value": "9:41",
                        "property": "textContent",
                        "exact": True,
                        "source": "canonical",
                    },
                    {
                        "nodeId": CONTENT_ID,
                        "value": "Language",
                        "property": "textContent",
                        "exact": True,
                        "source": "canonical",
                    },
                ],
                "elements": [
                    self._element(CHROME_ROOT_ID, "Status Bar"),
                    self._element(
                        CHROME_CHILD_ID, "Value", kind="text", parent=CHROME_ROOT_ID
                    ),
                    self._element(ICON_PATH_ID, "Path", kind="vector"),
                    self._element(CONTENT_ID, "Language", kind="text"),
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
        accounting = clean_accounting(FEATURE_ID, nodes=4)
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

    def _write_actual(self, *, rendered: tuple[str, ...] = (CONTENT_ID,)) -> Path:
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
                        "elements": {
                            node_id: {
                                "name": node_id,
                                "rect": dict(RECT),
                                "style": {"backgroundColor": "#FFFFFF"},
                                "text": "Language",
                            }
                            for node_id in rendered
                        },
                        "assets": {},
                    }
                },
                "components": {},
            },
        )
        return path

    def _validate(
        self, *, rendered: tuple[str, ...] = (CONTENT_ID,), **config: Any
    ) -> dict[str, Any]:
        self._write_bundle()
        actual_path = self._write_actual(rendered=rendered)
        config_path = self.root / "config.json"
        write_json(
            config_path,
            {
                "requireReferenceScreenshots": False,
                "requireFlowContract": False,
                "requireComponentContract": False,
                "requireAssetFreeze": False,
                **config,
            },
        )
        return GateValidator(
            ValidateOptions(
                self.bundle, actual_path, self.output, config_path=config_path
            )
        ).validate()

    def _types(self, result: dict[str, Any], defect_type: str) -> set[str]:
        return {
            item.get("elementNodeId")
            for item in result["defects"]
            if item.get("type") == defect_type
        }


class GateExclusionTest(GateExclusionHarness):
    def test_unexcluded_chrome_is_demanded(self) -> None:
        """Without config, an absent contracted element is a hard failure."""

        result = self._validate()

        self.assertIn(CHROME_ROOT_ID, self._types(result, "missing-element"))
        self.assertIn(CHROME_CHILD_ID, self._types(result, "missing-element"))
        self.assertEqual(result["summary"]["excludedElements"], 0)

    def test_excluded_subtree_is_no_longer_demanded(self) -> None:
        result = self._validate(excludedSubtreeNames=["Status Bar"])

        missing = self._types(result, "missing-element")
        self.assertNotIn(CHROME_ROOT_ID, missing)
        self.assertNotIn(CHROME_CHILD_ID, missing)

    def test_excluded_subtree_must_not_be_rendered(self) -> None:
        """The inversion: drawing an iOS status bar in a web build is a defect."""

        result = self._validate(
            rendered=(CONTENT_ID, CHROME_ROOT_ID),
            excludedSubtreeNames=["Status Bar"],
        )

        self.assertIn(
            CHROME_ROOT_ID, self._types(result, "hidden-element-rendered")
        )
        self.assertFalse(result["passed"])

    def test_interior_exclusion_keeps_the_container_gated(self) -> None:
        """An icon's glyph leaves the contract; the icon itself does not."""

        result = self._validate(
            rendered=(CONTENT_ID,), excludedInteriorNames=["Status Bar"]
        )

        missing = self._types(result, "missing-element")
        # The named container is still required...
        self.assertIn(CHROME_ROOT_ID, missing)
        # ...while its interior is not.
        self.assertNotIn(CHROME_CHILD_ID, missing)

    def test_interior_exclusion_does_not_forbid_rendering(self) -> None:
        """Glyph paths really are drawn -- asserting them absent would be wrong."""

        result = self._validate(
            rendered=(CONTENT_ID, CHROME_ROOT_ID, CHROME_CHILD_ID),
            excludedInteriorNames=["Status Bar"],
        )

        self.assertNotIn(
            CHROME_CHILD_ID, self._types(result, "hidden-element-rendered")
        )

    def test_icon_glyph_is_always_gated(self) -> None:
        """No config may drop an icon: nothing else in the REST path gates one.

        The asset contract is empty for canonical REST screens, so excluding
        vector glyphs would let an implementation that draws no icons at all
        pass every gate.
        """

        result = self._validate(
            excludedSubtreeNames=["Status Bar"],
            excludedElementTypes=["vector"],  # not a supported key
        )

        self.assertIn(ICON_PATH_ID, self._types(result, "missing-element"))
        self.assertFalse(result["passed"])

    def test_excluded_text_leaves_the_copy_gate(self) -> None:
        """Nobody should implement the mock status-bar clock."""

        result = self._validate(excludedSubtreeNames=["Status Bar"])

        self.assertEqual(
            [
                item
                for item in result["defects"]
                if item["gate"] == "copy"
                and item.get("elementNodeId") == CHROME_CHILD_ID
            ],
            [],
        )

    def test_fully_excluded_screen_is_a_defect(self) -> None:
        """A screen where nothing is gated must not read as a pass."""

        result = self._validate(
            excludedSubtreeNames=["Status Bar", "Value", "Path", "Language"]
        )

        self.assertIn(
            "screen-fully-excluded",
            {item.get("type") for item in result["defects"]},
        )
        self.assertFalse(result["passed"])

    def test_every_exclusion_is_reported(self) -> None:
        result = self._validate(excludedSubtreeNames=["Status Bar"])

        excluded = result["exclusions"]["elements"]
        self.assertEqual(result["summary"]["excludedElements"], len(excluded))
        self.assertEqual(
            {item["nodeId"] for item in excluded},
            {CHROME_ROOT_ID, CHROME_CHILD_ID},
        )
        self.assertEqual(result["exclusions"]["subtreeNames"], ["Status Bar"])

    def test_rendered_report_identifies_each_withheld_node(self) -> None:
        """A count alone cannot tell a reader which screen lost what."""

        result = self._validate(excludedSubtreeNames=["Status Bar"])
        html = render_report(result, self.output).read_text(encoding="utf-8")

        self.assertIn("Withheld from gating", html)
        self.assertIn("does <b>not</b> render them", html)
        self.assertIn(CHROME_ROOT_ID, html)
        self.assertIn(CHROME_CHILD_ID, html)
        self.assertIn(SCREEN_ID, html)

    def test_clean_run_reports_nothing_withheld(self) -> None:
        result = self._validate()
        html = render_report(result, self.output).read_text(encoding="utf-8")

        self.assertIn("Nothing was withheld", html)

    def test_content_is_never_excluded_by_accident(self) -> None:
        result = self._validate(excludedSubtreeNames=["Status Bar"])

        excluded_ids = {
            item["nodeId"] for item in result["exclusions"]["elements"]
        }
        self.assertNotIn(CONTENT_ID, excluded_ids)
        self.assertNotIn(ICON_PATH_ID, excluded_ids)


class ApprovedDeviationTest(GateExclusionHarness):
    """Judged design-side differences stop failing, but stay on the record."""

    def _deviations(self, result):
        return [
            item for item in result["defects"]
            if item["severity"] == "approved-deviation"
        ]

    def test_unapproved_difference_still_fails(self) -> None:
        result = self._validate()

        self.assertFalse(result["passed"])
        self.assertEqual(self._deviations(result), [])

    def test_approval_reclassifies_without_deleting(self) -> None:
        result = self._validate(
            approvedDeviations=[
                {"gate": "structure", "reason": "복제 실수로 확인됨"}
            ]
        )

        approved = self._deviations(result)
        self.assertTrue(approved)
        self.assertEqual(approved[0]["approvedReason"], "복제 실수로 확인됨")
        # The defect is still in the manifest, just not counted as a failure.
        self.assertIn(approved[0], result["defects"])
        self.assertEqual(
            result["summary"]["approvedDeviations"], len(approved)
        )

    def test_approval_only_matches_named_fields(self) -> None:
        """An approval scoped to another gate must not absolve this one."""

        result = self._validate(
            approvedDeviations=[
                {"gate": "structure", "type": "duplicate-node-id",
                 "reason": "이 실행에는 없는 결함"}
            ]
        )

        self.assertEqual(self._deviations(result), [])
        self.assertFalse(result["passed"])

    def test_reasonless_approval_is_rejected(self) -> None:
        result = self._validate(
            approvedDeviations=[{"gate": "structure", "reason": "  "}]
        )

        self.assertIn(
            "approved-deviation-unreasoned",
            {item.get("type") for item in result["defects"]},
        )
        self.assertEqual(self._deviations(result), [])

    def test_stale_approval_is_reported(self) -> None:
        """An approval that matches nothing widens the gate invisibly."""

        result = self._validate(
            approvedDeviations=[
                {"gate": "structure", "reason": "쓰임"},
                {"gate": "flow", "reason": "안 쓰임"},
            ]
        )

        unused = result["approvedDeviations"]["unusedEntries"]
        self.assertEqual([item["gate"] for item in unused], ["flow"])

    def test_report_shows_each_reason(self) -> None:
        result = self._validate(
            approvedDeviations=[
                {"gate": "structure", "reason": "복제 실수로 확인됨"}
            ]
        )
        html = render_report(result, self.output).read_text(encoding="utf-8")

        self.assertIn("Approved deviations", html)
        self.assertIn("복제 실수로 확인됨", html)


if __name__ == "__main__":
    unittest.main()
