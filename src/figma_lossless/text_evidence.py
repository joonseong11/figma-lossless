"""Keep text-based proposal tools aligned on the same Figma evidence.

Compiled screens may contain non-copy properties, approximate values, and
blank strings. Those records cannot support exact-copy proposals. Keeping the
filter here prevents two proposal commands from silently treating the same
screen differently as either command evolves.
"""

from __future__ import annotations

from typing import Any


def eligible_texts(screen: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep proposal commands on one exact-copy boundary as they evolve."""

    eligible: list[dict[str, Any]] = []
    for item in screen["texts"]:
        if (
            not isinstance(item, dict)
            or item.get("property", "textContent") != "textContent"
            or not item.get("exact", True)
            or not isinstance(item.get("value"), str)
            or not item["value"].strip()
        ):
            continue
        if not isinstance(item.get("nodeId"), str) or not item["nodeId"]:
            raise ValueError(
                f"compiled screen {screen['nodeId']} text is missing nodeId"
            )
        eligible.append(item)
    return eligible
