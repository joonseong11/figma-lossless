"""Surface copy that may be data without weakening the copy contract.

The copy gate is deliberately literal: a string preserved from Figma must be
the string the implementation renders. That makes it a reliable verifier, but
it cannot also decide that one exact string was only an example of user data.
Teaching the gate to guess would turn an auditable mismatch into an invisible
exception.

Compiled variants already carry a narrower signal that can be reviewed without
changing any gate. Labels change between locale or state variants; example data
often does not. This export records that invariance, the screens that produced
it, and every underlying node id. It remains a proposal because invariance is
evidence, not proof: every item requires confirmation and nothing here is
applied to the bundle or validator.

Known limitations: locale-specific formatting can hide data that appears as
different literals (for example, English ``1``/``0`` versus Korean
``1개``/``0개`` in ``bundle-v10/screens/21823-41542.json`` versus
``bundle-v10/screens/21823-47812.json``). The invariance ratio also measures
unique strings rather than semantic similarity, so text-sparse screens can
propose static labels or block real data as a group.
"""

from __future__ import annotations

import re
from collections import Counter
from math import isfinite
from pathlib import Path
from typing import Any

from .text_evidence import eligible_texts
from .util import read_json, resolve_within, write_json


FORMATS = ("json", "md")
DEFAULT_MAX_INVARIANCE_RATIO = 0.5
DEFAULT_GROUP_SIMILARITY = 0.8

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
TIME_PATTERN = re.compile(r"^\d{1,2}:\d{2}$")
NUMERIC_MASK_PATTERN = re.compile(r"^(?=.*\d)[\d\s,./()+-]+$")
SINGLE_GRAPHEME_PATTERN = re.compile(r"^.$", re.DOTALL)


def propose_slots(
    bundle_dir: Path,
    output_path: Path,
    *,
    format: str = "json",
    min_variants: int = 2,
    max_invariance_ratio: float = DEFAULT_MAX_INVARIANCE_RATIO,
    group_similarity: float = DEFAULT_GROUP_SIMILARITY,
) -> dict[str, Any]:
    """Write reviewable slot candidates while leaving every hard gate intact.

    Variant count alone cannot distinguish meaningfully different variants from
    several copies of the same locale and state. The invariant-to-union ratio
    measures that diversity without depending on language: pilot groups with
    useful variation were at most 9.5% invariant, while false-positive groups
    were 60% invariant. Groups above the configured threshold are therefore
    reported for review instead of producing proposals.
    """

    if format not in FORMATS:
        raise ValueError(f"Unsupported proposal format: {format!r}")
    _validate_ratio("max invariance ratio", max_invariance_ratio)
    _validate_ratio("group similarity", group_similarity)

    bundle_dir = bundle_dir.resolve()
    output_path = output_path.resolve()
    try:
        output_path.relative_to(bundle_dir)
    except ValueError:
        pass
    else:
        raise ValueError(f"output path must be outside bundle directory: {bundle_dir}")

    manifest = read_json(bundle_dir / "manifest.json")
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    manifest_screens = manifest.get("screens")
    if not isinstance(manifest_screens, list):
        raise ValueError("manifest screens must be a list")

    screens: list[dict[str, Any]] = []
    for index, screen_item in enumerate(manifest_screens):
        if not isinstance(screen_item, dict):
            raise ValueError(f"manifest screen {index} must be a JSON object")
        compiled_path = screen_item.get("compiledPath")
        if not isinstance(compiled_path, str) or not compiled_path:
            raise ValueError(f"manifest screen {index} is missing compiledPath")
        screen = read_json(
            resolve_within(
                bundle_dir, compiled_path, label="compiled screen"
            )
        )
        if not isinstance(screen, dict):
            raise ValueError(f"compiled screen {compiled_path} must be a JSON object")
        if not isinstance(screen.get("nodeId"), str) or not screen["nodeId"]:
            raise ValueError(f"compiled screen {compiled_path} is missing nodeId")
        texts = screen.get("texts")
        if not isinstance(texts, list):
            raise ValueError(f"compiled screen {compiled_path} texts must be a list")
        elements = screen.get("elements")
        if not isinstance(elements, list):
            raise ValueError(f"compiled screen {compiled_path} elements must be a list")
        screens.append(screen)

    analysis = analyze_slot_candidates(
        screens,
        min_variants=min_variants,
        max_invariance_ratio=max_invariance_ratio,
        group_similarity=group_similarity,
    )
    artifact = {
        "schemaVersion": 1,
        "featureId": manifest.get("featureId"),
        **analysis,
    }

    if format == "json":
        write_json(output_path, artifact)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(_markdown(artifact), encoding="utf-8")

    return {
        "groups": len(artifact["groups"]),
        "proposals": len(artifact["proposals"]),
        "warnings": len(artifact["warnings"]),
        "format": format,
        "output": str(output_path),
    }


def analyze_slot_candidates(
    screens: list[dict[str, Any]],
    *,
    min_variants: int = 2,
    max_invariance_ratio: float = DEFAULT_MAX_INVARIANCE_RATIO,
    group_similarity: float = DEFAULT_GROUP_SIMILARITY,
) -> dict[str, list[dict[str, Any]]]:
    """Return slot proposal signals without reading or writing an artifact."""

    _validate_ratio("max invariance ratio", max_invariance_ratio)
    _validate_ratio("group similarity", group_similarity)
    grouped = _structural_groups(screens, group_similarity)

    groups: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not screens:
        warnings.append({"kind": "no-screens"})

    for group_name, group_screens in grouped:
        members = [screen["nodeId"] for screen in group_screens]
        member_count = len(group_screens)
        groups.append(
            {
                "name": group_name,
                "members": members,
                "memberCount": member_count,
            }
        )

        texts_by_screen = [eligible_texts(screen) for screen in group_screens]
        values_by_screen = [
            {item["value"] for item in texts} for texts in texts_by_screen
        ]
        union_values = set.union(*values_by_screen)
        invariant_values = set.intersection(*values_by_screen)
        invariance_ratio = _invariance_ratio(invariant_values, union_values)
        if not union_values:
            warnings.append(
                {
                    "kind": "no-text-evidence",
                    "group": group_name,
                    "memberCount": member_count,
                    "invariantCount": 0,
                    "unionCount": 0,
                    "invarianceRatio": invariance_ratio,
                }
            )
        if member_count < min_variants:
            warnings.append(
                {
                    "kind": "insufficient-variants",
                    "group": group_name,
                    "memberCount": member_count,
                }
            )
            continue
        if not union_values:
            continue

        if invariance_ratio > max_invariance_ratio:
            warnings.append(
                {
                    "kind": "variants-too-similar",
                    "group": group_name,
                    "memberCount": member_count,
                    "invariantCount": len(invariant_values),
                    "unionCount": len(union_values),
                    "invarianceRatio": invariance_ratio,
                    "threshold": max_invariance_ratio,
                }
            )
            continue

        for value in sorted(invariant_values):
            shape = _shape(value)
            proposals.append(
                {
                    "value": value,
                    "kind": "chrome" if shape == "time" else "slot",
                    "confidence": _confidence(member_count, shape),
                    "signals": {
                        "invariantAcrossVariants": True,
                        "invarianceRatio": invariance_ratio,
                        "variantCount": member_count,
                        "shape": shape,
                    },
                    "group": group_name,
                    "occurrences": [
                        {
                            "screenNodeId": screen["nodeId"],
                            "nodeId": item["nodeId"],
                        }
                        for screen, texts in zip(group_screens, texts_by_screen)
                        for item in texts
                        if item["value"] == value
                    ],
                    "requiresConfirmation": True,
                }
            )

    return {
        "groups": groups,
        "proposals": proposals,
        "warnings": warnings,
    }


def _structural_groups(
    screens: list[dict[str, Any]], similarity_threshold: float
) -> list[tuple[str, list[dict[str, Any]]]]:
    element_names = [
        Counter(
            item.get("name")
            if isinstance(item, dict) and isinstance(item.get("name"), str)
            else ""
            for item in screen["elements"]
        )
        for screen in screens
    ]
    parents = list(range(len(screens)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left in range(len(screens)):
        for right in range(left + 1, len(screens)):
            intersection = sum((element_names[left] & element_names[right]).values())
            union_size = sum((element_names[left] | element_names[right]).values())
            similarity = intersection / union_size if union_size else 0.0
            if similarity >= similarity_threshold:
                union(left, right)

    components: dict[int, list[dict[str, Any]]] = {}
    for index, screen in enumerate(screens):
        components.setdefault(find(index), []).append(screen)

    grouped: list[tuple[str, list[dict[str, Any]]]] = []
    for component in components.values():
        name_counts = Counter(str(screen.get("name") or "") for screen in component)
        # Prefer the more descriptive label when the mode is tied; the final
        # lexical key keeps the result deterministic for equally long labels.
        label = max(
            name_counts,
            key=lambda name: (name_counts[name], len(name), name),
        )
        grouped.append((label, component))
    return sorted(grouped, key=lambda item: (item[0], item[1][0]["nodeId"]))


def _validate_ratio(label: str, value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError(f"{label} must be a finite number between 0 and 1")


def _invariance_ratio(invariant_values: set[str], union_values: set[str]) -> float:
    return len(invariant_values) / len(union_values) if union_values else 0.0


def _shape(value: str) -> str:
    if EMAIL_PATTERN.fullmatch(value):
        return "email"
    if TIME_PATTERN.fullmatch(value):
        return "time"
    if NUMERIC_MASK_PATTERN.fullmatch(value):
        return "numericMask"
    if SINGLE_GRAPHEME_PATTERN.fullmatch(value):
        return "singleGrapheme"
    return "unknown"


def _confidence(variant_count: int, shape: str) -> str:
    if variant_count >= 4 and shape != "unknown":
        return "high"
    if variant_count >= 4:
        return "medium"
    return "low"


def _markdown(artifact: dict[str, Any]) -> str:
    lines = [
        "# Slot proposals",
        "",
        f"Feature: `{_escape_markdown(str(artifact.get('featureId') or ''))}`",
        "",
        "## Groups",
        "",
        "| Group | Members | Member count |",
        "| --- | --- | ---: |",
    ]
    for group in artifact["groups"]:
        members = ", ".join(group["members"])
        lines.append(
            f"| {_escape_markdown(group['name'])} "
            f"| {_escape_markdown(members)} | {group['memberCount']} |"
        )

    lines.extend(
        [
            "",
            "## Proposals",
            "",
            "| Group | Value | Kind | Confidence | Shape | Variants | Occurrences | Confirmation required |",
            "| --- | --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for proposal in artifact["proposals"]:
        lines.append(
            f"| {_escape_markdown(proposal['group'])} "
            f"| {_escape_markdown(proposal['value'])} "
            f"| {proposal['kind']} | {proposal['confidence']} "
            f"| {proposal['signals']['shape']} "
            f"| {proposal['signals']['variantCount']} "
            f"| {len(proposal['occurrences'])} | yes |"
        )

    lines.extend(
        [
            "",
            "## Warnings",
            "",
            "| Kind | Group | Member count |",
            "| --- | --- | ---: |",
        ]
    )
    for warning in artifact["warnings"]:
        lines.append(
            f"| {warning['kind']} "
            f"| {_escape_markdown(str(warning.get('group') or ''))} "
            f"| {warning.get('memberCount', '')} |"
        )
    lines.append("")
    return "\n".join(lines)


def _escape_markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
