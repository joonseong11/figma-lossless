"""Propose existing repository code from exact copy shared with Figma.

The harness cannot know which components and message catalogs already exist in
the target repository unless the caller supplies that evidence. This module
accepts a language-neutral index, joins its strings to compiled Figma copy, and
writes reviewable reuse candidates. It never parses application source and it
never applies a candidate because matching words are useful evidence, not proof
that two pieces of UI have the same behavior.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from .text_evidence import eligible_texts
from .util import read_json, resolve_within, write_json


FORMATS = ("json", "md")
DEFAULT_MIN_OVERLAP = 2
DEFAULT_MAX_SOURCE_FREQUENCY = 20


def propose_reuse(
    bundle_dir: Path,
    repo_index_path: Path,
    output_path: Path,
    *,
    format: str = "json",
    min_overlap: int = DEFAULT_MIN_OVERLAP,
    max_source_frequency: int = DEFAULT_MAX_SOURCE_FREQUENCY,
) -> dict[str, Any]:
    """Write reviewable candidates so copy overlap never implies automatic reuse."""

    if format not in FORMATS:
        raise ValueError(f"Unsupported proposal format: {format!r}")
    if (
        isinstance(min_overlap, bool)
        or not isinstance(min_overlap, int)
        or min_overlap < 1
    ):
        raise ValueError("min overlap must be an integer of at least 1")
    if (
        isinstance(max_source_frequency, bool)
        or not isinstance(max_source_frequency, int)
        or max_source_frequency < 1
    ):
        raise ValueError(
            "max source frequency must be an integer of at least 1"
        )

    bundle_dir = bundle_dir.resolve()
    repo_index_path = repo_index_path.resolve()
    output_path = output_path.resolve()
    try:
        output_path.relative_to(bundle_dir)
    except ValueError:
        pass
    else:
        raise ValueError(f"output path must be outside bundle directory: {bundle_dir}")

    screens, feature_id = _load_screens(bundle_dir)
    strings, components = _load_repo_index(repo_index_path)

    strings_by_value: dict[str, list[dict[str, str | None]]] = {}
    for item in strings:
        strings_by_value.setdefault(item["value"], []).append(item)

    common_values: dict[str, list[str]] = {}
    for value, items in strings_by_value.items():
        sources = sorted({_source_file(item["source"]) for item in items})
        if len(sources) > max_source_frequency:
            common_values[value] = sources

    proposals: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not strings:
        warnings.append({"kind": "no-repo-strings"})
    warnings.extend(
        {
            "kind": "value-too-common",
            "value": value,
            "sourceCount": len(sources),
            "threshold": max_source_frequency,
            "sources": sources,
        }
        for value, sources in sorted(common_values.items())
    )

    for screen in screens:
        screen_texts = eligible_texts(screen)
        screen_text_by_value: dict[str, dict[str, Any]] = {}
        for item in screen_texts:
            screen_text_by_value.setdefault(item["value"], item)

        if not screen_text_by_value:
            warnings.append(
                {"kind": "no-text-evidence", "screenNodeId": screen["nodeId"]}
            )

        matches_by_source: dict[str, dict[str, dict[str, Any]]] = {}
        for value, text_item in screen_text_by_value.items():
            if value in common_values:
                continue
            for index_item in strings_by_value.get(value, []):
                source = _source_file(index_item["source"])
                match = {
                    "value": value,
                    "nodeId": text_item["nodeId"],
                    "location": index_item["source"],
                    "symbol": index_item["symbol"],
                }
                source_matches = matches_by_source.setdefault(source, {})
                previous = source_matches.get(value)
                if previous is None or _match_key(match) < _match_key(previous):
                    source_matches[value] = match

        if not matches_by_source:
            warnings.append(
                {
                    "kind": "screen-without-match",
                    "screenNodeId": screen["nodeId"],
                    "screenName": str(screen.get("name") or ""),
                }
            )

        for source, matches_by_value in matches_by_source.items():
            matches = list(matches_by_value.values())
            if len(matches) < min_overlap:
                continue
            proposals.append(
                {
                    "screenNodeId": screen["nodeId"],
                    "screenName": str(screen.get("name") or ""),
                    "source": source,
                    "matchedCount": len(matches),
                    "screenTextCount": len(screen_texts),
                    "coverage": len(matches) / len(screen_texts),
                    "matches": matches,
                    "component": _component_for_source(source, components),
                    "requiresConfirmation": True,
                }
            )

    proposals.sort(
        key=lambda proposal: (
            -proposal["matchedCount"],
            proposal["source"],
            proposal["screenNodeId"],
        )
    )
    artifact = {
        "schemaVersion": 1,
        "featureId": feature_id,
        "proposals": proposals,
        "warnings": warnings,
    }

    if format == "json":
        write_json(output_path, artifact)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(_markdown(artifact), encoding="utf-8")

    return {
        "proposals": len(proposals),
        "warnings": len(warnings),
        "format": format,
        "output": str(output_path),
    }


def _load_screens(bundle_dir: Path) -> tuple[list[dict[str, Any]], Any]:
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
            resolve_within(bundle_dir, compiled_path, label="compiled screen")
        )
        if not isinstance(screen, dict):
            raise ValueError(f"compiled screen {compiled_path} must be a JSON object")
        if not isinstance(screen.get("nodeId"), str) or not screen["nodeId"]:
            raise ValueError(f"compiled screen {compiled_path} is missing nodeId")
        texts = screen.get("texts")
        if not isinstance(texts, list):
            raise ValueError(f"compiled screen {compiled_path} texts must be a list")
        screens.append(screen)
    return screens, manifest.get("featureId")


def _load_repo_index(
    repo_index_path: Path,
) -> tuple[list[dict[str, str | None]], list[dict[str, str]]]:
    index = read_json(repo_index_path)
    if not isinstance(index, dict):
        raise ValueError("repo index must be a JSON object")
    if index.get("schemaVersion") != 1:
        raise ValueError("repo index schemaVersion must be 1")
    raw_strings = index.get("strings")
    if not isinstance(raw_strings, list):
        raise ValueError("repo index strings must be a list")

    strings: list[dict[str, str | None]] = []
    for item_index, item in enumerate(raw_strings):
        if not isinstance(item, dict):
            raise ValueError(f"repo index string {item_index} must be a JSON object")
        value = item.get("value")
        source = item.get("source")
        if not isinstance(value, str) or not value:
            raise ValueError(f"repo index string {item_index} is missing value")
        if not isinstance(source, str) or not source:
            raise ValueError(f"repo index string {item_index} is missing source")
        symbol = item.get("symbol")
        if symbol is not None and not isinstance(symbol, str):
            raise ValueError(f"repo index string {item_index} symbol must be a string")
        strings.append({"value": value, "source": source, "symbol": symbol})

    raw_components = index.get("components", [])
    if not isinstance(raw_components, list):
        raise ValueError("repo index components must be a list")
    components: list[dict[str, str]] = []
    for item_index, item in enumerate(raw_components):
        if not isinstance(item, dict):
            raise ValueError(
                f"repo index component {item_index} must be a JSON object"
            )
        name = item.get("name")
        source = item.get("source")
        if not isinstance(name, str) or not name:
            raise ValueError(f"repo index component {item_index} is missing name")
        if not isinstance(source, str) or not source:
            raise ValueError(f"repo index component {item_index} is missing source")
        components.append({"name": name, "source": _source_file(source)})
    return strings, components


def _source_file(source: str) -> str:
    path, separator, line = source.rpartition(":")
    if separator and path and line.isdigit():
        return path
    return source


def _component_for_source(
    source: str, components: list[dict[str, str]]
) -> str | None:
    source_directory = str(PurePosixPath(source).parent)
    ranked = sorted(
        (
            (
                0 if component["source"] == source else 1,
                component["source"],
                component["name"],
            ),
            component["name"],
        )
        for component in components
        if component["source"] == source
        or str(PurePosixPath(component["source"]).parent) == source_directory
    )
    return ranked[0][1] if ranked else None


def _match_key(match: dict[str, Any]) -> tuple[str, str, str]:
    return (
        match["value"],
        match["location"],
        str(match.get("symbol") or ""),
    )


def _markdown(artifact: dict[str, Any]) -> str:
    lines = [
        "# Reuse proposals",
        "",
        f"Feature: `{_escape_markdown(str(artifact.get('featureId') or ''))}`",
        "",
        "| Screen | Source | Matches | Coverage | Component | Confirmation required |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ]
    for proposal in artifact["proposals"]:
        match_values = ", ".join(match["value"] for match in proposal["matches"])
        component = proposal["component"] or ""
        lines.append(
            f"| {_escape_markdown(proposal['screenName'])} "
            f"| {_escape_markdown(proposal['source'])} "
            f"| {proposal['matchedCount']} ({_escape_markdown(match_values)}) "
            f"| {proposal['coverage']:.4f} "
            f"| {_escape_markdown(component)} | yes |"
        )

    lines.extend(
        [
            "",
            "## Warnings",
            "",
            "| Kind | Screen | Value | Source count | Threshold | Sources |",
            "| --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for warning in artifact["warnings"]:
        screen = warning.get("screenName") or warning.get("screenNodeId") or ""
        lines.append(
            f"| {warning['kind']} | {_escape_markdown(str(screen))} "
            f"| {_escape_markdown(str(warning.get('value') or ''))} "
            f"| {warning.get('sourceCount', '')} "
            f"| {warning.get('threshold', '')} "
            f"| {_escape_markdown(', '.join(warning.get('sources', [])))} |"
        )
    lines.append("")
    return "\n".join(lines)


def _escape_markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
