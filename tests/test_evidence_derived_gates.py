"""Whether a gate had work to do is read from the evidence, not from a flag.

`must_evaluate` started as something a caller asserted: the token gate knew its
policy was `hard`, the flow and component gates knew a contract existed. Three
call sites out of sixteen could say that, and the other thirteen could reach
`checked == 0` and report `NOT_EVALUATED` -- a status that does not fail the
run. So a gate could be emptied and the report would stay green.

Emptying one is not hypothetical. `excludedSubtreeNames` exists so a web build
is not asked to paint an iOS status bar, and it deletes that subtree's text
contracts on the way through. Name the only text-bearing container on a screen
and the copy gate compares nothing, while every other gate keeps reporting, so
nothing looks wrong. Deleting `property-accounting.json` does the same to the
gate that proves the compiler dropped no canonical evidence.

These tests pin the derivation that closes that, and pin its timing: the
expectation is computed *before* exclusions are applied, because reading the
contract afterwards would agree there was never anything to check. Each case
also has a companion showing the same bundle staying quiet without the config
that empties it -- a rule that fires either way would prove nothing.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from PIL import Image

from figma_lossless.report import render_report
from figma_lossless.util import write_json
from figma_lossless.validators import GateValidator, ValidateOptions

from canonical_bundle import attach_accounting, clean_accounting


FEATURE_ID = "evidence-derived"
SCREEN_ID = "1:1"
CHROME_ID = "1:2"
CHROME_TEXT_ID = "1:3"
CONTENT_ID = "1:4"
HIDDEN_ID = "1:5"
RECT = {"x": 0, "y": 0, "width": 10, "height": 10}


def element(
    node_id: str,
    name: str,
    *,
    parent: str | None = None,
    hidden: bool = False,
    rect: dict[str, Any] | None = RECT,
    style: dict[str, Any] | None = None,
) -> dict[str, Any]:
    compiled: dict[str, Any] = {
        "nodeId": node_id,
        "type": "frame",
        "figmaType": "FRAME",
        "name": name,
        "parentNodeId": parent,
        "hidden": hidden,
        "effectiveHidden": hidden,
        "rect": dict(rect) if rect else None,
        "style": dict(style) if style is not None else {"color": "#111111"},
    }
    if hidden:
        # `compiler.py:_map_element` moves a hidden layer's style aside so the
        # style gate stops demanding evidence for something never painted.
        compiled["hiddenStyle"] = compiled["style"]
        compiled["style"] = {}
    return compiled


class EvidenceExpectationHarness(unittest.TestCase):
    """Bundles whose gates can be emptied by configuration alone."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.output = self.root / "report"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _screen(
        self,
        node_id: str,
        *,
        elements: list[dict[str, Any]],
        texts: list[dict[str, Any]] | None = None,
        reference: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schemaVersion": "1.0",
            "nodeId": node_id,
            "name": f"Screen {node_id}",
            "evidenceSource": "figma-rest",
            "viewport": {"width": 10, "height": 10},
            "referenceScreenshot": reference,
            "referenceCode": None,
            "texts": list(texts or []),
            "elements": elements,
            "assets": [],
            "variables": {},
            "canonicalEvidence": {
                "path": f"rest/nodes/{node_id}.json",
                "sha256": "0" * 64,
                "bytes": 1,
            },
            "status": {"contextFetched": True, "specCompiled": True},
        }

    def _write_bundle(
        self,
        screens: list[dict[str, Any]],
        *,
        cover: bool = True,
    ) -> None:
        entries = []
        for screen in screens:
            name = f"{screen['nodeId'].replace(':', '-')}.json"
            write_json(self.bundle / "screens" / name, screen)
            entries.append(
                {
                    "nodeId": screen["nodeId"],
                    "name": screen["name"],
                    "compiledPath": f"screens/{name}",
                }
            )
        # Mirror what `build_accounting_summary` writes: a feature with no
        # screens accounts for no nodes, and a control that quietly claims one
        # is not a control at all.
        accounting = clean_accounting(
            FEATURE_ID, screens=len(screens), nodes=1 if screens else 0
        )
        write_json(self.bundle / "property-accounting.json", accounting)
        write_json(
            self.bundle / "manifest.json",
            attach_accounting(
                {
                    "schemaVersion": "1.0",
                    "featureId": FEATURE_ID,
                    "screens": entries,
                    "contracts": {"flowContract": None, "componentMap": None},
                },
                accounting,
            ),
        )
        write_json(
            self.bundle / "coverage.json",
            [
                {
                    "nodeId": entry["nodeId"],
                    "name": entry["name"],
                    "status": {"contextFetched": True, "specCompiled": True},
                }
                for entry in (entries if cover else [])
            ],
        )

    def _write_actual(self, screens: dict[str, Any]) -> Path:
        path = self.root / "actual.json"
        write_json(
            path,
            {
                "schemaVersion": "1.0",
                "featureId": FEATURE_ID,
                "provenance": "browser-capture",
                "screens": screens,
                "components": {},
            },
        )
        return path

    def _validate(self, actual_path: Path, **config: Any) -> dict[str, Any]:
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
                self.bundle, actual_path, self.output, config_path=config_path
            )
        ).validate()

    def _types(self, result: dict[str, Any], gate: str) -> list[str]:
        return [
            item["type"] for item in result["defects"] if item["gate"] == gate
        ]


class CopyEmptiedByExclusionTest(EvidenceExpectationHarness):
    """The case the review called most reachable: the pilot's own config shape.

    `Status Bar` is a real entry in the pilot's `excludedSubtreeNames`, and the
    mock clock inside it is a real text contract. On a screen whose only other
    element carries no copy, excluding it takes the copy gate to zero.
    """

    def _bundle(self) -> None:
        self._write_bundle(
            [
                self._screen(
                    SCREEN_ID,
                    elements=[
                        element(CHROME_ID, "Status Bar"),
                        element(CHROME_TEXT_ID, "Value", parent=CHROME_ID),
                        element(CONTENT_ID, "Panel"),
                    ],
                    texts=[
                        {
                            "nodeId": CHROME_TEXT_ID,
                            "value": "9:41",
                            "property": "textContent",
                            "exact": True,
                        }
                    ],
                )
            ]
        )

    def _actual(self) -> Path:
        return self._write_actual(
            {
                SCREEN_ID: {
                    "name": "Screen",
                    "route": "/",
                    "screenshot": None,
                    "elements": {
                        CONTENT_ID: {
                            "name": "Panel",
                            "rect": dict(RECT),
                            "style": {"color": "#111111"},
                        }
                    },
                    "assets": {},
                }
            }
        )

    def test_excluding_the_only_text_bearing_subtree_is_named(self) -> None:
        """A warning, not a failure -- and never silence.

        The screen's only copy was the mock clock, so after the exclusion it
        genuinely has no text of its own; failing it would punish the
        exclusion for doing exactly what it is documented to do. What must not
        happen is the gate going quiet, because then "this screen has no copy"
        and "this screen's copy stopped being checked" look identical.
        """

        self._bundle()
        result = self._validate(
            self._actual(), excludedSubtreeNames=["Status Bar"]
        )
        self.assertEqual(result["gates"]["copy"]["checked"], 0)
        defects = [
            item for item in result["defects"] if item["gate"] == "copy"
        ]
        # The slot-contract feature adds one run-level warning to every legacy
        # bundle. The exclusion still contributes exactly its original warning;
        # neither warning is a hard failure and neither hides the other.
        self.assertEqual(
            [item["type"] for item in defects],
            ["data-contract-absent", "copy-fully-excluded"],
        )
        self.assertTrue(all(item["severity"] == "warning" for item in defects))
        self.assertIsNone(defects[0]["screenNodeId"])
        self.assertEqual(defects[1]["screenNodeId"], SCREEN_ID)
        self.assertTrue(result["passed"], result["defects"])

    def test_the_screen_is_not_reported_as_fully_excluded(self) -> None:
        """The existing guard does not cover this, which is why it is needed.

        `screen-fully-excluded` fires only when every contracted element goes.
        Here one remains, so structure keeps reporting normally and the copy
        gate's silence would otherwise be the only trace.
        """

        self._bundle()
        result = self._validate(
            self._actual(), excludedSubtreeNames=["Status Bar"]
        )
        self.assertNotIn("screen-fully-excluded", self._types(result, "structure"))

    def test_the_same_bundle_is_quiet_without_the_exclusion(self) -> None:
        """The rule has to fire because of the config, not the fixture."""

        self._bundle()
        result = self._validate(
            self._write_actual(
                {
                    SCREEN_ID: {
                        "name": "Screen",
                        "route": "/",
                        "screenshot": None,
                        "elements": {
                            node_id: {
                                "name": node_id,
                                "rect": dict(RECT),
                                "style": {"color": "#111111"},
                                "text": "9:41" if node_id == CHROME_TEXT_ID else "",
                            }
                            for node_id in (CHROME_ID, CHROME_TEXT_ID, CONTENT_ID)
                        },
                        "assets": {},
                    }
                }
            )
        )
        self.assertEqual(result["gates"]["copy"]["checked"], 1)
        self.assertEqual(result["gates"]["copy"]["status"], "PASS")
        self.assertTrue(result["passed"], result["defects"])


class GeometryAndStyleEmptiedTest(EvidenceExpectationHarness):
    """A designer-hidden sibling is what keeps the existing guard quiet.

    `screen-fully-excluded` compares the excluded count against every element
    in the contract, and a layer hidden in Figma counts toward that total while
    contributing nothing to the geometry or style gates. One hidden layer next
    to one excluded subtree is therefore a screen where those two gates measure
    nothing and no existing defect says so.
    """

    def _bundle(self) -> None:
        self._write_bundle(
            [
                self._screen(
                    SCREEN_ID,
                    elements=[
                        element(CHROME_ID, "Status Bar"),
                        element(HIDDEN_ID, "Legacy banner", hidden=True),
                    ],
                )
            ]
        )

    def test_both_gates_report_that_they_measured_nothing(self) -> None:
        self._bundle()
        result = self._validate(
            self._write_actual(
                {
                    SCREEN_ID: {
                        "name": "Screen",
                        "route": "/",
                        "screenshot": None,
                        "elements": {},
                        "assets": {},
                    }
                }
            ),
            excludedSubtreeNames=["Status Bar"],
        )
        self.assertNotIn(
            "screen-fully-excluded", self._types(result, "structure")
        )
        for gate in ("geometry", "style"):
            with self.subTest(gate=gate):
                self.assertEqual(result["gates"][gate]["checked"], 0)
                self.assertEqual(
                    self._types(result, gate), [f"{gate}-fully-excluded"]
                )
                defect = [
                    item
                    for item in result["defects"]
                    if item["gate"] == gate
                ][0]
                self.assertEqual(defect["screenNodeId"], SCREEN_ID)
                self.assertEqual(defect["severity"], "warning")
        self.assertTrue(result["passed"], result["defects"])

    def test_a_contract_of_only_hidden_layers_stays_quiet(self) -> None:
        """Nothing was emptied here -- the design itself hides everything."""

        self._write_bundle(
            [
                self._screen(
                    SCREEN_ID,
                    elements=[element(HIDDEN_ID, "Legacy banner", hidden=True)],
                )
            ]
        )
        result = self._validate(
            self._write_actual(
                {
                    SCREEN_ID: {
                        "name": "Screen",
                        "route": "/",
                        "screenshot": None,
                        "elements": {},
                        "assets": {},
                    }
                }
            )
        )
        for gate in ("geometry", "style"):
            with self.subTest(gate=gate):
                self.assertEqual(result["gates"][gate]["status"], "NOT_EVALUATED")
        self.assertTrue(result["passed"], result["defects"])


class CoverageEmptiedTest(EvidenceExpectationHarness):
    def test_declared_screens_with_an_empty_coverage_list_fail(self) -> None:
        self._write_bundle(
            [self._screen(SCREEN_ID, elements=[element(CONTENT_ID, "Panel")])],
            cover=False,
        )
        result = self._validate(
            self._write_actual(
                {
                    SCREEN_ID: {
                        "name": "Screen",
                        "route": "/",
                        "screenshot": None,
                        "elements": {
                            CONTENT_ID: {
                                "name": "Panel",
                                "rect": dict(RECT),
                                "style": {"color": "#111111"},
                            }
                        },
                        "assets": {},
                    }
                }
            )
        )
        self.assertEqual(result["gates"]["coverage"]["checked"], 0)
        self.assertEqual(
            sorted(self._types(result, "coverage")),
            ["gate-not-evaluated", "screen-absent-from-coverage"],
        )
        self.assertFalse(result["passed"])

    def test_a_bundle_that_declares_no_screens_stays_quiet(self) -> None:
        """An empty feature is a legitimate state, not a deleted gate."""

        self._write_bundle([])
        result = self._validate(self._write_actual({}))
        self.assertEqual(result["gates"]["coverage"]["checked"], 0)
        self.assertEqual(result["gates"]["coverage"]["status"], "NOT_EVALUATED")
        self.assertEqual(self._types(result, "coverage"), [])
        self.assertTrue(result["passed"], result["defects"])


class PartialEmptinessTest(EvidenceExpectationHarness):
    """One emptied screen must not be paid for by another screen's work.

    The first version of this rule counted per feature. In a multi-screen
    bundle that is worth nothing: exclude every contract on screen A, leave one
    comparison on screen B, and the feature total stays above zero, so the gate
    reports PASS and screen A appears nowhere. Every gate here is deliberately
    left in its passing state by the other screen, so a regression to a
    feature-wide count makes these tests green again -- which is the point.
    """

    OTHER_ID = "1:9"

    def _two_screens(self, *, texts_on_other: bool) -> None:
        self._write_bundle(
            [
                self._screen(
                    SCREEN_ID,
                    elements=[
                        element(CHROME_ID, "Status Bar"),
                        element(CHROME_TEXT_ID, "Value", parent=CHROME_ID),
                    ],
                    texts=[
                        {
                            "nodeId": CHROME_TEXT_ID,
                            "value": "9:41",
                            "property": "textContent",
                            "exact": True,
                        }
                    ],
                ),
                self._screen(
                    self.OTHER_ID,
                    elements=[element(CONTENT_ID, "Panel")],
                    texts=(
                        [
                            {
                                "nodeId": CONTENT_ID,
                                "value": "Language",
                                "property": "textContent",
                                "exact": True,
                            }
                        ]
                        if texts_on_other
                        else []
                    ),
                ),
            ]
        )

    def _actual(self) -> Path:
        return self._write_actual(
            {
                SCREEN_ID: {
                    "name": "Screen",
                    "route": "/",
                    "screenshot": None,
                    "elements": {},
                    "assets": {},
                },
                self.OTHER_ID: {
                    "name": "Screen",
                    "route": "/other",
                    "screenshot": None,
                    "elements": {
                        CONTENT_ID: {
                            "name": "Panel",
                            "rect": dict(RECT),
                            "style": {"color": "#111111"},
                            "text": "Language",
                        }
                    },
                    "assets": {},
                },
            }
        )

    def test_an_emptied_screen_is_named_while_the_gate_still_passes(
        self,
    ) -> None:
        self._two_screens(texts_on_other=True)
        result = self._validate(
            self._actual(), excludedSubtreeNames=["Status Bar"]
        )
        # The other screen keeps the gate's own count positive -- exactly the
        # state that used to make this invisible.
        self.assertEqual(result["gates"]["copy"]["checked"], 1)
        self.assertEqual(result["gates"]["copy"]["status"], "PASS")
        named = [
            item["screenNodeId"]
            for item in result["defects"]
            if item["type"] == "copy-fully-excluded"
        ]
        self.assertEqual(named, [SCREEN_ID])

    def test_an_emptied_screen_is_named_for_geometry_and_style(self) -> None:
        self._two_screens(texts_on_other=False)
        result = self._validate(
            self._actual(), excludedSubtreeNames=["Status Bar"]
        )
        for gate in ("geometry", "style"):
            with self.subTest(gate=gate):
                self.assertGreater(result["gates"][gate]["checked"], 0)
                named = [
                    item["screenNodeId"]
                    for item in result["defects"]
                    if item["type"] == f"{gate}-fully-excluded"
                ]
                self.assertEqual(named, [SCREEN_ID])

    def test_emptying_the_manifest_screen_list_does_not_pass(self) -> None:
        """The cheapest tamper: delete the contracts, keep everything else.

        With no screen loaded, every element gate has nothing to do and reports
        `NOT_EVALUATED`, while coverage still counts the entries coverage.json
        kept -- so checking the manifest against coverage in one direction only
        left the whole feature verifiable by deleting one list.
        """

        self._two_screens(texts_on_other=True)
        manifest_path = self.bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["screens"] = []
        write_json(manifest_path, manifest)

        result = self._validate(self._actual())
        absent = [
            item
            for item in result["defects"]
            if item["type"] == "screen-absent-from-manifest"
        ]
        self.assertEqual(
            sorted(item["screenNodeId"] for item in absent),
            sorted([SCREEN_ID, self.OTHER_ID]),
        )
        self.assertFalse(result["passed"])

    def test_an_unevaluated_gate_does_not_vouch_for_a_screen(self) -> None:
        """`passed` is true for a gate that measured nothing; PASS is not."""

        self._two_screens(texts_on_other=True)
        result = self._validate(
            self._actual(), excludedSubtreeNames=["Status Bar"]
        )
        by_id = {item["nodeId"]: item for item in result["coverage"]}
        # This bundle contracts no assets at all, so the asset gate looked at
        # nothing -- and used to record `assetPassed: true` for every screen.
        self.assertEqual(result["gates"]["asset"]["status"], "NOT_EVALUATED")
        self.assertIsNone(by_id[self.OTHER_ID]["status"]["assetPassed"])
        # A gate that did evaluate still vouches for the screen.
        self.assertEqual(result["gates"]["copy"]["status"], "PASS")
        self.assertTrue(by_id[self.OTHER_ID]["status"]["copyPassed"])

    def test_a_screen_missing_from_coverage_is_a_hard_defect(self) -> None:
        """`coverage.json` is what every other coverage check reads."""

        self._two_screens(texts_on_other=True)
        write_json(
            self.bundle / "coverage.json",
            [
                {
                    "nodeId": self.OTHER_ID,
                    "name": "Screen",
                    "status": {"contextFetched": True, "specCompiled": True},
                }
            ],
        )
        result = self._validate(self._actual())
        absent = [
            item
            for item in result["defects"]
            if item["type"] == "screen-absent-from-coverage"
        ]
        self.assertEqual([item["screenNodeId"] for item in absent], [SCREEN_ID])
        self.assertEqual(absent[0]["severity"], "hard")
        # The gate counted the screen that *is* listed, so the count alone
        # would have called this a clean run.
        self.assertEqual(result["gates"]["coverage"]["checked"], 1)
        self.assertFalse(result["passed"])


class LoudScreenIsNotCalledSilentTest(EvidenceExpectationHarness):
    """A screen already failing must not also be blamed on exclusions.

    Geometry and style skip an element whose capture carries no rect or style,
    raising `missing-actual-*` and moving on without counting it. Judged only
    by the counter, such a screen looks exactly like one whose contracts were
    all excluded -- but it is the loudest screen in the run, and a second
    defect saying "every element is hidden or excluded" would be false.
    """

    def test_an_unimplemented_screen_reports_only_the_real_defect(self) -> None:
        self._write_bundle(
            [self._screen(SCREEN_ID, elements=[element(CONTENT_ID, "Panel")])]
        )
        result = self._validate(
            self._write_actual(
                {
                    SCREEN_ID: {
                        "name": "Screen",
                        "route": "/",
                        "screenshot": None,
                        "elements": {},
                        "assets": {},
                    }
                }
            )
        )
        self.assertIn("missing-actual-rect", self._types(result, "geometry"))
        self.assertNotIn(
            "geometry-fully-excluded", self._types(result, "geometry")
        )
        self.assertIn("missing-actual-style", self._types(result, "style"))
        self.assertNotIn("style-fully-excluded", self._types(result, "style"))


class UnusableAccountingArtifactTest(EvidenceExpectationHarness):
    """Truncating the artifact must not be quieter than deleting it.

    The gate used to count `counts.nodes or 1`, so an artifact that said
    nothing still claimed one checked item and passed, and `_collection_defects`
    returns silently on a non-dict `restCollection` -- together, an empty JSON
    object satisfied the gate that exists to prove the compiler dropped nothing.
    """

    def _bundle_with_accounting(self, accounting: dict[str, Any]) -> Path:
        self._write_bundle(
            [self._screen(SCREEN_ID, elements=[element(CONTENT_ID, "Panel")])]
        )
        write_json(self.bundle / "property-accounting.json", accounting)
        return self._write_actual(
            {
                SCREEN_ID: {
                    "name": "Screen",
                    "route": "/",
                    "screenshot": None,
                    "elements": {
                        CONTENT_ID: {
                            "name": "Panel",
                            "rect": dict(RECT),
                            "style": {"color": "#111111"},
                        }
                    },
                    "assets": {},
                }
            }
        )

    def _unusable(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            item
            for item in result["defects"]
            if item["type"] == "accounting-artifact-unusable"
        ]

    def test_an_empty_artifact_does_not_satisfy_the_gate(self) -> None:
        result = self._validate(self._bundle_with_accounting({}))
        self.assertEqual(result["gates"]["accounting"]["checked"], 0)
        self.assertEqual(
            self._types(result, "accounting"),
            ["accounting-artifact-unusable"] * 5,
        )
        self.assertFalse(result["passed"])

    def test_a_missing_rest_collection_is_reported(self) -> None:
        result = self._validate(
            self._bundle_with_accounting(
                {
                    "schemaVersion": "1.0",
                    "featureId": FEATURE_ID,
                    "counts": {"screens": 1, "nodes": 1},
                    "violations": [],
                    "unsupported": [],
                    "unresolvedTokenBindings": [],
                }
            )
        )
        defects = self._unusable(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertIn("restCollection", defects[0]["message"])
        self.assertFalse(result["passed"])

    def test_a_negative_node_count_cannot_stand_in_for_a_measurement(
        self,
    ) -> None:
        """`-1` is falsy-safe, so a bare zero check waved it through."""

        result = self._validate(
            self._bundle_with_accounting(
                clean_accounting(FEATURE_ID, nodes=-1)
            )
        )
        self.assertEqual(result["gates"]["accounting"]["checked"], 0)
        self.assertEqual(len(self._unusable(result)), 1, result["defects"])
        self.assertFalse(result["passed"])

    def test_a_truncated_violation_list_is_reported(self) -> None:
        """Deleting the list is how findings disappear without a trace."""

        accounting = clean_accounting(FEATURE_ID)
        del accounting["violations"]
        result = self._validate(self._bundle_with_accounting(accounting))
        defects = self._unusable(result)
        self.assertEqual(
            [item["expected"] for item in defects], ["violations: []"]
        )
        self.assertFalse(result["passed"])

    def test_a_feature_with_no_screens_may_report_zero_nodes(self) -> None:
        """`build_accounting_summary` writes exactly this for an empty feature."""

        self._write_bundle([])
        result = self._validate(self._write_actual({}))
        self.assertEqual(self._unusable(result), [])
        self.assertTrue(result["passed"], result["defects"])

    def test_the_report_separates_an_empty_artifact_from_a_missing_one(
        self,
    ) -> None:
        """A verdict of FAIL beside "not evaluated" is a report nobody trusts."""

        result = self._validate(self._bundle_with_accounting({}))
        html = render_report(result, self.output).read_text(encoding="utf-8")
        self.assertIn("present but empty", html)
        self.assertNotIn("carries no canonical property accounting", html)


class PartialReferenceCoverageTest(EvidenceExpectationHarness):
    """`requireReferenceScreenshots` is per run; the gap it hides is per screen.

    Opting out is legitimate -- a copy audit collects no images at all -- and
    an entirely image-less bundle correctly reports `NOT_EVALUATED`, which is
    visible. What was invisible is the mixed bundle: some screens compared,
    some never looked at, and a PASS covering both.
    """

    def _reference(self) -> str:
        path = self.bundle / "images" / "screen.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (10, 10), (255, 255, 255, 255)).save(path)
        return "images/screen.png"

    def test_a_screen_without_a_reference_is_named_as_uncompared(self) -> None:
        second = "1:9"
        reference = self._reference()
        self._write_bundle(
            [
                self._screen(
                    SCREEN_ID,
                    elements=[element(CONTENT_ID, "Panel")],
                    reference=reference,
                ),
                self._screen(second, elements=[element(CONTENT_ID, "Panel")]),
            ]
        )
        actual_image = self.root / "screen.png"
        Image.new("RGBA", (10, 10), (255, 255, 255, 255)).save(actual_image)
        captured = {
            "name": "Screen",
            "route": "/",
            "screenshot": "screen.png",
            "elements": {
                CONTENT_ID: {
                    "name": "Panel",
                    "rect": dict(RECT),
                    "style": {"color": "#111111"},
                }
            },
            "assets": {},
        }
        result = self._validate(
            self._write_actual({SCREEN_ID: captured, second: dict(captured)})
        )

        absent = [
            item
            for item in result["defects"]
            if item["type"] == "reference-screenshot-absent"
        ]
        self.assertEqual([item["screenNodeId"] for item in absent], [second])
        self.assertEqual(absent[0]["severity"], "warning")
        # The opt-out was deliberate, so this does not fail the run -- it just
        # stops the PASS from claiming the screen was compared.
        self.assertTrue(result["passed"], result["defects"])
        self.assertEqual(result["gates"]["visual"]["checked"], 1)


if __name__ == "__main__":
    unittest.main()
