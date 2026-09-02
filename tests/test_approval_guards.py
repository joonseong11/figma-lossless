"""What an approved deviation is not allowed to excuse.

`approvedDeviations` reclassifies a judged design-side difference so it stops
failing the run. Two things were true of that mechanism that made it wider than
its own definition:

* `_deviation_matches` only checks the fields an entry names, so an entry that
  named none of them was true of every hard defect. One line with a reason and
  nothing else excused the whole run.
* Nothing distinguished "this difference is acceptable" from "this check never
  ran". A gate that reported it had measured nothing could be approved away,
  which put back the exact silence the empty-gate rules exist to break.

These tests hold both boundaries. They deliberately reuse the exclusion suite's
fixture, because that bundle already produces ordinary hard defects that a
legitimate approval is supposed to be able to cover -- the point is that the
guards stop the two abuses without touching the feature.
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from typing import Any

from figma_lossless.validators import (
    APPROVABLE_DEFECT_TYPES,
    DEVIATION_SELECTOR_FIELDS,
    UNAPPROVABLE_DEFECT_TYPES,
)

from test_gate_exclusions import GateExclusionHarness


def _declared_defect_types() -> set[str]:
    """Every defect type the validator can raise, read from its own source.

    Enumerating by hand would drift the moment someone adds a defect, and the
    drift would be silent in the direction that matters: an unclassified type is
    one nobody decided about. Parsing the module keeps the taxonomy test honest
    without asking anyone to remember to update a list here.
    """

    source = (
        pathlib.Path(__file__).resolve().parent.parent
        / "src"
        / "figma_lossless"
        / "validators.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))

    def literals(node: ast.AST) -> set[str]:
        # `"copy-mismatch" if actual_value is not None else "missing-copy"`
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return literals(node.body) | literals(node.orelse)
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_add_defect"
            and len(node.args) >= 2
        ):
            found |= literals(node.args[1])
        # A gate that raises the same defect on several screens routes it
        # through a helper taking `defect_type=`, so the literal sits at the
        # call site rather than at `_add_defect`.
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "defect_type":
                    found |= literals(keyword.value)
        # The style gate builds its defect as a dict in a helper and passes
        # `defect["type"]` through, so the literal never reaches `_add_defect`.
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "type"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    found.add(value.value)
    return found


class ApprovalTaxonomyTest(unittest.TestCase):
    """Every defect type has to be on one side of the line, explicitly."""

    def test_every_defect_type_is_classified(self) -> None:
        declared = _declared_defect_types()
        classified = APPROVABLE_DEFECT_TYPES | UNAPPROVABLE_DEFECT_TYPES

        self.assertEqual(
            declared - classified,
            set(),
            "a new defect type must be added to APPROVABLE_DEFECT_TYPES or "
            "UNAPPROVABLE_DEFECT_TYPES -- an unclassified type is one nobody "
            "decided about, and it defaults to unapprovable silently",
        )

    def test_the_two_sets_do_not_overlap(self) -> None:
        self.assertEqual(
            APPROVABLE_DEFECT_TYPES & UNAPPROVABLE_DEFECT_TYPES, set()
        )

    def test_no_classified_type_has_been_deleted_from_the_validator(self) -> None:
        """A stale entry hides that the real type is gone."""

        declared = _declared_defect_types()
        classified = APPROVABLE_DEFECT_TYPES | UNAPPROVABLE_DEFECT_TYPES
        self.assertEqual(classified - declared, set())

    def test_verification_absence_types_are_not_approvable(self) -> None:
        """The pilot's blocked set, named: none of it is a judged difference."""

        for defect_type in (
            "flow-test-missing",
            "flow-assumption-unresolved",
            "component-decision-open",
            "gate-not-evaluated",
            "missing-figma-context",
            "missing-reference-screenshot",
            "incomplete-collection",
            "missing-actual-rect",
            "missing-actual-style",
            "product-route-unverified",
            "slot-marker-missing",
        ):
            with self.subTest(defect_type=defect_type):
                self.assertNotIn(defect_type, APPROVABLE_DEFECT_TYPES)


class ApprovalScopeTest(GateExclusionHarness):
    """An approval has to say which difference it is about."""

    def _types(self, result: dict[str, Any]) -> set[str]:
        return {item.get("type") for item in result["defects"]}

    def _approved(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            item
            for item in result["defects"]
            if item["severity"] == "approved-deviation"
        ]

    def test_entry_with_only_a_reason_approves_nothing(self) -> None:
        """The blanket entry: a reason, no selector, every defect excused."""

        result = self._validate(
            approvedDeviations=[{"reason": "전부 확인했음"}]
        )

        self.assertIn("approved-deviation-unscoped", self._types(result))
        self.assertEqual(self._approved(result), [])
        self.assertFalse(result["passed"])

    def test_unscoped_entry_is_reported_as_unused(self) -> None:
        """Rejecting it silently would be its own kind of quiet."""

        result = self._validate(
            approvedDeviations=[{"reason": "전부 확인했음"}]
        )

        unused = result["approvedDeviations"]["unusedEntries"]
        self.assertEqual([item["reason"] for item in unused], ["전부 확인했음"])

    def test_any_single_selector_field_is_enough(self) -> None:
        """The guard is against naming nothing, not against being broad."""

        result = self._validate(
            approvedDeviations=[
                {"gate": "structure", "reason": "복제 실수로 확인됨"}
            ]
        )

        self.assertNotIn("approved-deviation-unscoped", self._types(result))
        self.assertTrue(self._approved(result))

    def test_each_selector_field_scopes_an_entry_on_its_own(self) -> None:
        """All six fields count, not just the one an earlier test happened to use.

        Naming any of them is enough to make an entry a statement about some
        particular difference. Dropping a field from the list would silently
        reject entries that were always legitimate -- the pilot config has three
        that name `property` and nothing else.
        """

        samples: dict[str, Any] = {
            "gate": "structure",
            "type": "missing-element",
            "screenNodeId": "1:1",
            "elementNodeId": "2:2",
            "nodeId": "2:2",
            "property": "backgroundColor",
        }
        self.assertEqual(set(samples), set(DEVIATION_SELECTOR_FIELDS))

        for field, value in samples.items():
            with self.subTest(field=field):
                result = self._validate(
                    approvedDeviations=[{field: value, "reason": "확인됨"}]
                )
                self.assertNotIn(
                    "approved-deviation-unscoped", self._types(result)
                )

    def test_reasonless_entry_is_not_also_reported_as_unscoped(self) -> None:
        """One entry, one complaint: the missing reason is the first thing wrong."""

        result = self._validate(approvedDeviations=[{"reason": "  "}])

        types = self._types(result)
        self.assertIn("approved-deviation-unreasoned", types)
        self.assertNotIn("approved-deviation-unscoped", types)


class UnapprovableDefectTest(GateExclusionHarness):
    """"The check never ran" is not a difference anyone can approve."""

    def _severity_of(self, result: dict[str, Any], defect_type: str) -> list[str]:
        return [
            item["severity"]
            for item in result["defects"]
            if item.get("type") == defect_type
        ]

    def test_gate_not_evaluated_cannot_be_approved(self) -> None:
        result = self._validate(
            tokenUsagePolicy="hard",
            approvedDeviations=[
                {
                    "gate": "token",
                    "type": "gate-not-evaluated",
                    "reason": "토큰은 다음 스프린트에",
                }
            ],
        )

        self.assertEqual(
            self._severity_of(result, "gate-not-evaluated"), ["hard"]
        )
        self.assertFalse(result["passed"])

    def test_rejected_approval_comes_back_as_unused(self) -> None:
        """The attempt is on the record, not quietly dropped."""

        result = self._validate(
            tokenUsagePolicy="hard",
            approvedDeviations=[
                {
                    "gate": "token",
                    "type": "gate-not-evaluated",
                    "reason": "토큰은 다음 스프린트에",
                }
            ],
        )

        unused = result["approvedDeviations"]["unusedEntries"]
        self.assertEqual([item["type"] for item in unused], ["gate-not-evaluated"])

    def test_missing_contracts_cannot_be_approved(self) -> None:
        result = self._validate(
            requireFlowContract=True,
            requireComponentContract=True,
            approvedDeviations=[
                {"gate": "flow", "type": "flow-contract-missing",
                 "reason": "이 기능엔 플로우가 없음"},
                {"gate": "component", "type": "component-contract-missing",
                 "reason": "컴포넌트 라이브러리 아직 없음"},
            ],
        )

        self.assertEqual(
            self._severity_of(result, "flow-contract-missing"), ["hard"]
        )
        self.assertEqual(
            self._severity_of(result, "component-contract-missing"), ["hard"]
        )
        self.assertFalse(result["passed"])

    def test_a_broad_gate_entry_does_not_reach_them_either(self) -> None:
        """Scoping by gate alone is legal, but still cannot excuse an absence."""

        result = self._validate(
            requireFlowContract=True,
            approvedDeviations=[{"gate": "flow", "reason": "플로우 없음"}],
        )

        self.assertEqual(
            self._severity_of(result, "flow-contract-missing"), ["hard"]
        )

    def test_the_pilot_blocked_set_cannot_be_waved_through(self) -> None:
        """The concrete regression: 29 hard defects, three config lines.

        Every one of these means "we have not verified this yet" -- a flow with
        no test, a transition nobody confirmed against the PRD, a component
        decision left open. Under the first version of this guard, which listed
        what could *not* be approved, all three were approvable by default and
        a blocked pilot went green.
        """

        for gate, defect_type in (
            ("flow", "flow-test-missing"),
            ("flow", "flow-assumption-unresolved"),
            ("component", "component-decision-open"),
        ):
            with self.subTest(defect_type=defect_type):
                result = self._validate(
                    requireFlowContract=True,
                    requireComponentContract=True,
                    approvedDeviations=[
                        {"gate": gate, "type": defect_type,
                         "reason": "나중에 하기로 함"}
                    ],
                )
                approved_types = {
                    item["type"]
                    for item in result["defects"]
                    if item["severity"] == "approved-deviation"
                }
                self.assertNotIn(defect_type, approved_types)

    def test_an_ordinary_difference_is_still_approvable(self) -> None:
        """The guards must not turn the feature off."""

        result = self._validate(
            approvedDeviations=[
                {"gate": "structure", "type": "missing-element",
                 "reason": "디바이스 크롬은 앱 기존 패턴을 따름"}
            ]
        )

        approved = [
            item
            for item in result["defects"]
            if item["severity"] == "approved-deviation"
        ]
        self.assertTrue(approved)
        self.assertTrue(
            all(item["type"] == "missing-element" for item in approved)
        )
