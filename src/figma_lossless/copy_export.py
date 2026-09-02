from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .util import read_json, resolve_within, write_json

CSV_FIELDNAMES = ["screenNodeId", "screenName", "nodeId", "property", "value"]


def export_copy(
    bundle_dir: Path,
    output_path: Path,
    *,
    format: str = "json",
) -> dict[str, Any]:
    """Flatten every screen's exact-copy contract into one locale-audit artifact.

    Reads `<bundle>/manifest.json` for the screen list (the same entry point
    `create_snapshot_template` and `GateValidator` use), then every compiled
    screen JSON's `texts` list, and writes one flat, sorted artifact for
    diffing against i18n message catalogs.
    """

    if format not in ("json", "csv"):
        raise ValueError(f"Unsupported export format: {format!r}")

    bundle_dir = bundle_dir.resolve()
    manifest = read_json(bundle_dir / "manifest.json")

    entries: list[dict[str, str]] = []
    for screen_item in manifest.get("screens", []):
        screen_path = resolve_within(
            bundle_dir, screen_item["compiledPath"], label="compiled screen"
        )
        screen = read_json(screen_path)
        screen_node_id = screen["nodeId"]
        screen_name = screen.get("name") or ""
        for item in screen.get("texts", []):
            entries.append(
                {
                    "screenNodeId": screen_node_id,
                    "screenName": screen_name,
                    "nodeId": item["nodeId"],
                    "property": item.get("property", "textContent"),
                    "value": item["value"],
                }
            )

    entries.sort(
        key=lambda entry: (
            entry["screenName"],
            entry["screenNodeId"],
            entry["nodeId"],
            entry["property"],
        )
    )

    output_path = output_path.resolve()
    if format == "csv":
        _write_csv(output_path, entries)
    else:
        write_json(output_path, entries)

    return {
        "screens": len(manifest.get("screens", [])),
        "entries": len(entries),
        "output": str(output_path),
    }


def _write_csv(output_path: Path, entries: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(entries)
