"""Turn a compiled bundle into a design spec someone can hand to an agent.

The bundle is a verification contract: shaped for gates, addressed by node id,
split across a manifest and one JSON file per screen. That is the right shape
for `validate` and the wrong shape for a person who wants to say "build this"
and paste something.

So this flattens the same evidence into one readable document. It invents
nothing and drops nothing measurable -- every position, size, colour and string
here came out of Figma through the collector and compiler, which is the whole
reason a spec written this way is worth more than a screenshot and a
description. What it adds is the structure a reader needs: the element tree in
parent/child order, each node's copy attached to the node that shows it, and
the hidden layers marked as "must not render" rather than silently dropped.

This is a one-way export. Nothing here is gated, and using it does not oblige
anyone to run a verification afterwards -- the node ids are carried along so
that staying with the harness remains possible, not so that it is required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .util import read_json, resolve_within, write_json


FORMATS = ("md", "json")

# Properties whose absence and whose zero mean the same thing to whoever
# builds this. The compiler records them either way because a gate has to
# compare them; a reader drowning in `paddingTop: 0` learns nothing from them.
# Omitting these is stated in the document header, and `--all-properties`
# turns it off. The JSON export never omits anything.
ZERO_IS_ABSENT = frozenset(
    {
        "borderBottomWidth",
        "borderLeftWidth",
        "borderRadius",
        "borderRightWidth",
        "borderTopWidth",
        "borderWidth",
        "columnGap",
        "letterSpacing",
        "paddingBottom",
        "paddingLeft",
        "paddingRight",
        "paddingTop",
        "rowGap",
    }
)


def _is_noise(key: str, value: Any) -> bool:
    if isinstance(value, (list, dict)) and not value:
        return True
    if key in ZERO_IS_ABSENT and isinstance(value, (int, float)):
        return not isinstance(value, bool) and value == 0
    return False


def export_design(
    bundle_dir: Path,
    output_path: Path,
    *,
    format: str = "md",
    screen_ids: list[str] | None = None,
    all_properties: bool = False,
    exclude_names: list[str] | None = None,
) -> dict[str, Any]:
    """Write one self-contained design spec for a whole bundle, or some screens."""

    if format not in FORMATS:
        raise ValueError(f"Unsupported export format: {format!r}")

    bundle_dir = bundle_dir.resolve()
    manifest = read_json(bundle_dir / "manifest.json")
    wanted = set(screen_ids or [])

    screens: list[dict[str, Any]] = []
    for item in manifest.get("screens", []):
        if wanted and item.get("nodeId") not in wanted:
            continue
        screens.append(
            read_json(
                resolve_within(
                    bundle_dir, item["compiledPath"], label="compiled screen"
                )
            )
        )

    missing = sorted(wanted - {screen.get("nodeId") for screen in screens})
    if missing:
        raise ValueError(
            "These screens are not in the bundle: " + ", ".join(missing)
        )

    output_path = output_path.resolve()
    if format == "json":
        write_json(output_path, _json_document(manifest, screens))
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            _markdown_document(
                manifest,
                screens,
                all_properties=all_properties,
                exclude_names=set(exclude_names or []),
            ),
            encoding="utf-8",
        )

    return {
        "screens": len(screens),
        "elements": sum(
            len(screen.get("elements", [])) for screen in screens
        ),
        "texts": sum(len(screen.get("texts", [])) for screen in screens),
        "format": format,
        "output": str(output_path),
    }


def _copy_by_node(screen: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for text in screen.get("texts", []):
        if isinstance(text, dict) and text.get("nodeId"):
            grouped.setdefault(text["nodeId"], []).append(text)
    return grouped


def _children_by_parent(
    screen: dict[str, Any],
) -> tuple[dict[Any, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Group elements under their parent, and return the roots.

    An element whose `parentNodeId` names a node the screen does not contain is
    a root here rather than being dropped: the compiler records the parent it
    saw in Figma, and a subtree collected without its container is still part
    of the design.
    """

    elements = [
        element
        for element in screen.get("elements", [])
        if isinstance(element, dict) and element.get("nodeId")
    ]
    known = {element["nodeId"] for element in elements}
    children: dict[Any, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for element in elements:
        parent = element.get("parentNodeId")
        if parent and parent in known:
            children.setdefault(parent, []).append(element)
        else:
            roots.append(element)
    return children, roots


def _format_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _rect_text(element: dict[str, Any]) -> str:
    rect = element.get("rect")
    if not isinstance(rect, dict):
        return "no position recorded"
    parts = []
    width, height = rect.get("width"), rect.get("height")
    if width is not None or height is not None:
        parts.append(
            f"{_format_value(width) if width is not None else '?'}"
            f"×{_format_value(height) if height is not None else '?'}"
        )
    x, y = rect.get("x"), rect.get("y")
    if x is not None or y is not None:
        parts.append(
            f"at ({_format_value(x) if x is not None else '?'}, "
            f"{_format_value(y) if y is not None else '?'})"
        )
    return " ".join(parts) or "no position recorded"


def _style_lines(
    element: dict[str, Any], *, all_properties: bool
) -> list[str]:
    style = element.get("style")
    if not isinstance(style, dict) or not style:
        return []
    tokens = element.get("resolvedTokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    lines = []
    for key in sorted(style):
        value = style[key]
        if value is None:
            continue
        if not all_properties and _is_noise(key, value):
            continue
        token = tokens.get(key)
        suffix = f"  (design token: {token})" if token else ""
        lines.append(f"{key}: {_format_value(value)}{suffix}")
    return lines


def _element_block(
    element: dict[str, Any],
    children: dict[Any, list[dict[str, Any]]],
    copy: dict[str, list[dict[str, Any]]],
    depth: int,
    *,
    all_properties: bool,
    excluded: set[str],
) -> list[str]:
    pad = "  " * depth
    name = element.get("name") or "(unnamed)"
    kind = element.get("figmaType") or element.get("type") or "node"
    hidden = element.get("effectiveHidden", element.get("hidden"))
    header = f'{pad}- **{name}** `{element["nodeId"]}` · {kind} · {_rect_text(element)}'
    if hidden:
        # Stated, never dropped: a layer hidden in the design is a requirement
        # that the implementation not paint it, and a reader who never sees it
        # cannot honour that.
        header += " · **hidden in the design — must not render**"
    lines = [header]

    for text in copy.get(element["nodeId"], []):
        prop = text.get("property", "textContent")
        label = "text" if prop == "textContent" else prop
        lines.append(f'{pad}  - {label}: "{text.get("value")}"')

    style_lines = _style_lines(element, all_properties=all_properties)
    if style_lines:
        lines.append(f"{pad}  - style")
        lines.extend(f"{pad}    - {line}" for line in style_lines)

    for child in children.get(element["nodeId"], []):
        if child["nodeId"] in excluded:
            continue
        lines.extend(
            _element_block(
                child, children, copy, depth + 1,
                all_properties=all_properties,
                excluded=excluded,
            )
        )
    return lines


def _excluded_nodes(
    screen: dict[str, Any], names: set[str]
) -> set[str]:
    """Node ids inside a named subtree, container included.

    The same vocabulary `excludedSubtreeNames` uses in gate config, for the
    same reason: a spec handed to an implementer should not ask them to build
    an iOS status bar. It differs in what it means -- the gate inverts the
    requirement to "must not render", while here the subtree simply is not
    part of what is being described.
    """

    if not names:
        return set()
    by_id = {
        element["nodeId"]: element
        for element in screen.get("elements", [])
        if isinstance(element, dict) and element.get("nodeId")
    }
    excluded: set[str] = set()
    for node_id, element in by_id.items():
        cursor: dict[str, Any] | None = element
        seen: set[str] = set()
        while cursor is not None:
            current = cursor.get("nodeId")
            if not current or current in seen:
                break
            seen.add(current)
            if cursor.get("name") in names:
                excluded.add(node_id)
                break
            parent = cursor.get("parentNodeId")
            cursor = by_id.get(parent) if parent else None
    return excluded


def _screen_section(
    screen: dict[str, Any],
    *,
    all_properties: bool,
    exclude_names: set[str],
) -> list[str]:
    name = screen.get("name") or "(unnamed screen)"
    lines = [f'## {name}  `{screen.get("nodeId")}`', ""]

    viewport = screen.get("viewport")
    if isinstance(viewport, dict) and (
        viewport.get("width") or viewport.get("height")
    ):
        lines.append(
            f'Viewport: {_format_value(viewport.get("width"))}'
            f' × {_format_value(viewport.get("height"))}'
        )
        lines.append("")

    children, roots = _children_by_parent(screen)
    copy = _copy_by_node(screen)
    excluded = _excluded_nodes(screen, exclude_names)
    roots = [root for root in roots if root["nodeId"] not in excluded]
    if roots:
        for root in roots:
            lines.extend(
                _element_block(
                    root, children, copy, 0,
                    all_properties=all_properties,
                    excluded=excluded,
                )
            )
        lines.append("")
    else:
        lines.append("_This screen compiled no elements._")
        lines.append("")

    assets = [
        asset for asset in screen.get("assets", []) if isinstance(asset, dict)
    ]
    if assets:
        lines.append("### Assets")
        lines.append("")
        for asset in assets:
            local = asset.get("localPath")
            where = f" — `{local}`" if local else ""
            lines.append(f'- `{asset.get("variable")}`{where}')
        lines.append("")
    return lines


def _markdown_document(
    manifest: dict[str, Any],
    screens: list[dict[str, Any]],
    *,
    all_properties: bool = False,
    exclude_names: set[str] | None = None,
) -> str:
    feature = manifest.get("featureId") or "(unnamed feature)"
    lines = [
        f"# Design spec — {feature}",
        "",
        "Every number and string below was read from Figma by the collector "
        "and compiled without interpretation, so it is the design, not a "
        "description of it. Positions are in CSS pixels; `x`/`y` are relative "
        "to the screen frame.",
        "",
        f"Screens: {len(screens)}",
        "",
    ]
    if exclude_names:
        lines.append(
            "Excluded from this spec: "
            + ", ".join(f"`{name}`" for name in sorted(exclude_names))
            + " and everything inside them."
        )
        lines.append("")
    if not all_properties:
        lines.append(
            "Omitted for readability: empty effect lists, and spacing or "
            "border properties whose value is zero. Nothing else is left out; "
            "`--all-properties`, or `--format json`, gives every compiled "
            "property."
        )
        lines.append("")
    sources = {
        screen.get("evidenceSource")
        for screen in screens
        if screen.get("evidenceSource")
    }
    if sources:
        lines.append(f'Evidence: {", ".join(sorted(sources))}')
        lines.append("")
    for screen in screens:
        lines.extend(
            _screen_section(
                screen,
                all_properties=all_properties,
                exclude_names=exclude_names or set(),
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def _json_document(
    manifest: dict[str, Any], screens: list[dict[str, Any]]
) -> dict[str, Any]:
    """The same content, for an agent that would rather parse than read."""

    exported = []
    for screen in screens:
        copy = _copy_by_node(screen)
        elements = []
        for element in screen.get("elements", []):
            if not isinstance(element, dict) or not element.get("nodeId"):
                continue
            entry = {
                "nodeId": element["nodeId"],
                "name": element.get("name"),
                "type": element.get("figmaType") or element.get("type"),
                "parentNodeId": element.get("parentNodeId"),
                "mustNotRender": bool(
                    element.get("effectiveHidden", element.get("hidden"))
                ),
                "rect": element.get("rect"),
                "style": element.get("style") or {},
                "designTokens": element.get("resolvedTokens") or {},
                "copy": [
                    {
                        "property": text.get("property", "textContent"),
                        "value": text.get("value"),
                    }
                    for text in copy.get(element["nodeId"], [])
                ],
            }
            elements.append(entry)
        exported.append(
            {
                "nodeId": screen.get("nodeId"),
                "name": screen.get("name"),
                "viewport": screen.get("viewport"),
                "evidenceSource": screen.get("evidenceSource"),
                "elements": elements,
                "assets": screen.get("assets") or [],
            }
        )
    return {
        "schemaVersion": "1.0",
        "featureId": manifest.get("featureId"),
        "screens": exported,
    }
