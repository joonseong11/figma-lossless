"""What a gate that measured nothing is allowed to report.

A gate with `checked == 0` raised no defect because it looked at nothing. The
harness used to record that as `NOT_EVALUATED` and still let the run pass, so a
policy set to `hard` could be entirely inert without saying so. That is what
happened to a 61-screen pilot: `tokenUsagePolicy` was `"hard"`, every Figma
variable binding in the file failed to resolve to a token name, the gate
iterated zero elements, and the report came back green.

These tests fix the boundary. A gate whose policy is explicitly `hard`, or a
contract that exists at all, has to measure something; a gate switched `off` or
left at its advisory default is still allowed to be quiet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from figma_lossless.util import write_json
from figma_lossless.validators import (
    DEFAULT_CONFIG,
    GateValidator,
    ValidateOptions,
)

from test_token_gate import CUSTOM_PROPERTY, TOKEN, TokenGateHarness


class UnresolvedTokenBindingTest(TokenGateHarness):
    """The pilot's actual failure, reproduced at fixture scale."""

    def _drop_resolved_tokens(self) -> None:
        """Leave the variable binding in place but remove its resolved name.

        This is the shape the compiler emits when it has no variable index: the
        element still records that `backgroundColor` came from a Figma
        variable, and nothing can say which token that variable is.
        """

        path = self.bundle / "screens" / "1-1.json"
        screen = json.loads(path.read_text(encoding="utf-8"))
        element = screen["elements"][0]
        del element["resolvedTokens"]
        self.assertTrue(element["tokenBindings"])
        write_json(path, screen)

    def test_unresolved_binding_under_a_hard_policy_fails(self) -> None:
        self._write_bundle(resolved_tokens={"backgroundColor": TOKEN})
        self._drop_resolved_tokens()
        actual_path = self._write_actual(
            token_refs={"background-color": [CUSTOM_PROPERTY]}
        )
        result = self._validate(
            actual_path,
            tokenUsagePolicy="hard",
            tokenMap={TOKEN: CUSTOM_PROPERTY},
        )
        defects = self._token_defects(result)
        self.assertEqual(
            [item["type"] for item in defects], ["token-binding-unresolved"]
        )
        self.assertEqual(defects[0]["severity"], "hard")
        # The binding counts as checked, so the gate reports a failure it can
        # attribute to an element rather than a blanket "nothing ran".
        self.assertEqual(result["gates"]["token"]["checked"], 1)
        self.assertEqual(result["gates"]["token"]["status"], "FAIL")
        self.assertFalse(result["passed"])

    def test_unresolved_binding_only_warns_under_the_default_policy(
        self,
    ) -> None:
        self._write_bundle(resolved_tokens={"backgroundColor": TOKEN})
        self._drop_resolved_tokens()
        actual_path = self._write_actual(
            token_refs={"background-color": [CUSTOM_PROPERTY]}
        )
        result = self._validate(actual_path, tokenMap={TOKEN: CUSTOM_PROPERTY})
        defects = self._token_defects(result)
        self.assertEqual(
            [item["severity"] for item in defects], ["warning"]
        )
        self.assertTrue(result["passed"], result["defects"])


class EmptyGateStatusTest(TokenGateHarness):
    def test_hard_policy_that_checked_nothing_fails(self) -> None:
        # No element carries a token binding at all, so there is nothing for
        # the gate to measure -- and a policy that enforces nothing is a defect
        # in the configuration, not a pass.
        self._write_bundle()
        actual_path = self._write_actual(token_refs={})
        result = self._validate(actual_path, tokenUsagePolicy="hard")
        defects = self._token_defects(result)
        self.assertEqual(
            [item["type"] for item in defects], ["gate-not-evaluated"]
        )
        self.assertEqual(defects[0]["severity"], "hard")
        self.assertEqual(result["gates"]["token"]["checked"], 0)
        self.assertEqual(result["gates"]["token"]["status"], "FAIL")
        self.assertFalse(result["passed"])

    def test_advisory_policy_that_checked_nothing_stays_quiet(self) -> None:
        # `warn` is the default and means "not opted in". Failing there would
        # break every project that has no design tokens, which is a real and
        # legitimate state.
        self._write_bundle()
        actual_path = self._write_actual(token_refs={})
        result = self._validate(actual_path)
        self.assertEqual(self._token_defects(result), [])
        self.assertEqual(result["gates"]["token"]["status"], "NOT_EVALUATED")
        self.assertTrue(result["passed"], result["defects"])

    def test_not_evaluated_is_not_reported_as_a_pass(self) -> None:
        self._write_bundle()
        actual_path = self._write_actual(token_refs={})
        result = self._validate(actual_path)
        gate = result["gates"]["token"]
        self.assertEqual(gate["checked"], 0)
        self.assertNotEqual(gate["status"], "PASS")


class EmptyContractTest(TokenGateHarness):
    """A contract that decides nothing is not a contract that was satisfied."""

    def _install_contract(
        self, key: str, filename: str, payload: dict[str, Any]
    ) -> None:
        contracts = self.bundle / "contracts"
        contracts.mkdir(parents=True, exist_ok=True)
        write_json(contracts / filename, payload)
        manifest_path = self.bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.setdefault("contracts", {})[key] = f"contracts/{filename}"
        write_json(manifest_path, manifest)

    def _gate_defects(
        self, result: dict[str, Any], gate: str
    ) -> list[dict[str, Any]]:
        return [item for item in result["defects"] if item["gate"] == gate]

    def test_empty_component_contract_does_not_pass(self) -> None:
        self._write_bundle()
        self._install_contract(
            "componentMap",
            "component-map.json",
            {"requiredComponents": [], "mappings": []},
        )
        actual_path = self._write_actual(token_refs={})
        result = self._validate(actual_path)
        gate = result["gates"]["component"]
        self.assertEqual(gate["checked"], 0)
        self.assertEqual(gate["status"], "FAIL")
        self.assertIn(
            "gate-not-evaluated",
            [item["type"] for item in self._gate_defects(result, "component")],
        )
        self.assertFalse(result["passed"])

    def test_empty_flow_contract_does_not_pass(self) -> None:
        self._write_bundle()
        self._install_contract(
            "flowContract", "flow-contract.json", {"transitions": []}
        )
        actual_path = self._write_actual(token_refs={})
        result = self._validate(actual_path)
        gate = result["gates"]["flow"]
        self.assertEqual(gate["checked"], 0)
        self.assertEqual(gate["status"], "FAIL")
        self.assertIn(
            "gate-not-evaluated",
            [item["type"] for item in self._gate_defects(result, "flow")],
        )
        self.assertFalse(result["passed"])


class ContractRequirementDefaultTest(TokenGateHarness):
    """Behaviour and component identity are verified unless opted out."""

    def test_contracts_are_required_by_default(self) -> None:
        self.assertTrue(DEFAULT_CONFIG["requireFlowContract"])
        self.assertTrue(DEFAULT_CONFIG["requireComponentContract"])

    def test_a_run_without_config_demands_both_contracts(self) -> None:
        self._write_bundle()
        actual_path = self._write_actual(token_refs={})
        result = GateValidator(
            ValidateOptions(self.bundle, actual_path, self.output)
        ).validate()
        types = {item["type"] for item in result["defects"]}
        self.assertIn("component-contract-missing", types)
        self.assertIn("flow-contract-missing", types)
        self.assertFalse(result["passed"])

    def test_the_opt_out_is_still_available(self) -> None:
        self._write_bundle()
        actual_path = self._write_actual(token_refs={})
        result = self._validate(actual_path)
        types = {item["type"] for item in result["defects"]}
        self.assertNotIn("component-contract-missing", types)
        self.assertNotIn("flow-contract-missing", types)
