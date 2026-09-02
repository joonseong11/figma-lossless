"""Validate the human decision that separates data from fixed interface copy.

A data contract weakens exact-copy comparison only at named nodes. That makes
an invalid contract more dangerous than an ordinary malformed input: duplicate
entries could give one node two meanings, while a missing exemplar would leave
the validator unable to detect that sample design data was hard-coded. The
compiler therefore rejects those ambiguities before it records the contract in
a bundle.
"""

from __future__ import annotations

import json
from typing import Any

from .text_evidence import eligible_texts


SHAPES = frozenset(
    {"email", "time", "numericMask", "singleGrapheme", "text"}
)


class DataContractError(ValueError):
    """A controlled compile failure with machine-readable evidence."""

    def __init__(self, code: str, **evidence: Any):
        self.code = code
        self.evidence = evidence
        super().__init__(
            json.dumps(
                {"code": code, **evidence},
                ensure_ascii=False,
                sort_keys=True,
            )
        )


def validate_data_contract(value: Any) -> dict[str, Any]:
    """Return a schema-checked contract that assigns each node one meaning."""

    if not isinstance(value, dict):
        raise ValueError("data contract must be a JSON object")
    _require_keys(
        value,
        ("schemaVersion", "featureId", "slots", "chrome"),
        "data contract",
    )
    if value["schemaVersion"] != 1:
        raise ValueError("data contract schemaVersion must be 1")
    if not isinstance(value["featureId"], str):
        raise ValueError("data contract featureId must be a string")
    for key in ("slots", "chrome"):
        if not isinstance(value[key], list):
            raise ValueError(f"data contract {key} must be a list")

    seen: set[tuple[str, str]] = set()
    for index, slot in enumerate(value["slots"]):
        label = f"data contract slot {index}"
        _require_object(slot, label)
        _require_keys(
            slot,
            ("screenNodeId", "nodeId", "binding", "designExemplar"),
            label,
        )
        _require_strings(
            slot,
            ("screenNodeId", "nodeId", "binding", "designExemplar"),
            label,
        )
        if not slot["binding"].strip():
            raise DataContractError(
                "data-contract-binding-blank",
                screenNodeId=slot["screenNodeId"],
                nodeId=slot["nodeId"],
                actual=slot["binding"],
            )
        shape = slot.get("shape")
        if shape is not None and shape not in SHAPES:
            raise ValueError(f"{label} has unsupported shape {shape!r}")
        _record_node(slot, label, seen)

    for index, chrome in enumerate(value["chrome"]):
        label = f"data contract chrome {index}"
        _require_object(chrome, label)
        _require_keys(chrome, ("screenNodeId", "nodeId"), label)
        _require_strings(chrome, ("screenNodeId", "nodeId"), label)
        _record_node(chrome, label, seen)

    return value


def validate_data_contract_semantics(
    contract: dict[str, Any],
    screens: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind reviewed declarations to the compiled exact-copy contract."""

    text_values: dict[tuple[str, str], set[str]] = {}
    for screen in screens:
        screen_id = screen["nodeId"]
        for item in eligible_texts(screen):
            text_values.setdefault((screen_id, item["nodeId"]), set()).add(
                item["value"]
            )

    for kind, declaration_kind in (("slots", "slot"), ("chrome", "chrome")):
        for declaration in contract[kind]:
            identity = (
                declaration["screenNodeId"],
                declaration["nodeId"],
            )
            values = sorted(text_values.get(identity, set()))
            if not values:
                raise DataContractError(
                    "data-contract-node-unknown",
                    declarationKind=declaration_kind,
                    screenNodeId=identity[0],
                    nodeId=identity[1],
                )
            if kind == "slots" and (
                len(values) != 1 or values[0] != declaration["designExemplar"]
            ):
                raise DataContractError(
                    "data-contract-exemplar-mismatch",
                    screenNodeId=identity[0],
                    nodeId=identity[1],
                    expected=values[0] if len(values) == 1 else values,
                    actual=declaration["designExemplar"],
                )

    declared = {
        (item["screenNodeId"], item["nodeId"])
        for kind in ("slots", "chrome")
        for item in contract[kind]
    }
    warnings: list[dict[str, Any]] = []
    for proposal in proposals:
        missing = [
            occurrence
            for occurrence in proposal["occurrences"]
            if (occurrence["screenNodeId"], occurrence["nodeId"])
            not in declared
        ]
        if not missing:
            continue
        warnings.append(
            {
                "code": "data-contract-incomplete",
                "severity": "warning",
                "value": proposal["value"],
                "kind": proposal["kind"],
                "group": proposal["group"],
                "occurrences": missing,
            }
        )
    return warnings


def _require_object(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")


def _require_keys(
    value: dict[str, Any], keys: tuple[str, ...], label: str
) -> None:
    for key in keys:
        if key not in value:
            raise ValueError(f"{label} is missing required key {key!r}")


def _require_strings(
    value: dict[str, Any], keys: tuple[str, ...], label: str
) -> None:
    for key in keys:
        if not isinstance(value[key], str):
            raise ValueError(f"{label} {key} must be a string")


def _record_node(
    value: dict[str, Any], label: str, seen: set[tuple[str, str]]
) -> None:
    identity = (value["screenNodeId"], value["nodeId"])
    if identity in seen:
        raise ValueError(
            f"{label} declares duplicate node {identity[0]!r}/{identity[1]!r}"
        )
    seen.add(identity)
