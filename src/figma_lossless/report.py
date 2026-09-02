from __future__ import annotations

import html
import json
import shutil
from pathlib import Path
from typing import Any

from .util import resolve_within, safe_slug, write_json


def render_report(result: dict[str, Any], output_dir: Path) -> Path:
    """Render a dependency-free, auditable HTML validation report."""

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = output_dir / "report-assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    comparisons = _copy_comparison_images(result, assets_dir)
    gates = _reconcile_gates(result)
    passed_gates = sum(1 for gate in gates.values() if gate["status"] == "PASS")
    report_data = {
        **{key: value for key, value in result.items() if key != "_runtimePaths"},
        "gates": gates,
        "summary": {**result["summary"], "passedGates": passed_gates},
        "comparisons": comparisons,
    }
    write_json(output_dir / "report-data.json", report_data)

    summary = report_data["summary"]
    status = "PASS" if result["passed"] else "FAIL"
    status_class = "pass" if result["passed"] else "fail"
    provenance = result.get("snapshotProvenance")
    provenance_warning = (
        '<div class="self-test">SELF TEST — NOT IMPLEMENTATION EVIDENCE</div>'
        if provenance == "self-test"
        else ""
    )
    gates_html = "".join(_gate_row(name, gate) for name, gate in gates.items())
    defects_html = "".join(_defect_row(item) for item in result["defects"])
    if not defects_html:
        defects_html = (
            '<tr><td colspan="8" class="empty">No defects found.</td></tr>'
        )
    accounting_html = _accounting_panel(result.get("accounting"))
    token_html = _token_panel(result)
    exclusions_html = _exclusions_panel(result.get("exclusions"))
    data_contract_html = _data_contract_panel(result.get("dataContract"))
    deviations_html = _deviations_panel(result.get("approvedDeviations"))
    capture_routes_html = "".join(
        _capture_route_row(item) for item in result.get("captureRoutes", [])
    )
    if not capture_routes_html:
        capture_routes_html = (
            '<tr><td colspan="6" class="empty">No captured routes.</td></tr>'
        )
    coverage_html = "".join(_coverage_row(item) for item in result["coverage"])
    comparison_html = "".join(
        _comparison_card(item) for item in comparisons
    )
    if not comparison_html:
        comparison_html = '<p class="empty">No visual comparisons.</p>'

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lossless Figma gate report · {_e(result.get('featureId'))}</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0b0e14; --panel:#121722; --line:#273044;
      --text:#edf2ff; --muted:#9ba8bf; --red:#ff647c; --green:#43d18b;
      --amber:#ffca65; --blue:#7ea8ff; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5
      ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ max-width:1500px; margin:auto; padding:32px; }}
    h1 {{ margin:0 0 4px; font-size:28px; }} h2 {{ margin:34px 0 12px; font-size:18px; }}
    .muted,.empty {{ color:var(--muted); }}
    .headline {{ display:flex; align-items:center; gap:14px; flex-wrap:wrap; }}
    .badge {{ border:1px solid; border-radius:999px; padding:5px 10px; font-weight:800; }}
    .badge.pass {{ color:var(--green); border-color:var(--green); }}
    .badge.fail {{ color:var(--red); border-color:var(--red); }}
    .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
      gap:10px; margin-top:24px; }}
    .card,.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; }}
    .card {{ padding:15px; }} .card b {{ display:block; font-size:24px; }}
    .panel {{ overflow:auto; }} table {{ width:100%; border-collapse:collapse; }}
    th,td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:left;
      vertical-align:top; }} th {{ color:var(--muted); font-size:12px; position:sticky;
      top:0; background:var(--panel); }}
    tr:last-child td {{ border-bottom:0; }} code {{ color:#c5d5ff; font-size:12px; }}
    .yes {{ color:var(--green); }} .no,.hard {{ color:var(--red); }}
    .warning {{ color:var(--amber); }}
    .self-test {{ margin:18px 0; border:2px solid var(--amber); color:var(--amber);
      border-radius:10px; padding:12px; text-align:center; font-weight:900; letter-spacing:.08em; }}
    input,select {{ background:#0d121c; color:var(--text); border:1px solid var(--line);
      border-radius:8px; padding:9px 11px; }}
    .filters {{ display:flex; gap:8px; margin:0 0 10px; }} .filters input {{ flex:1; }}
    .comparisons {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); gap:14px; }}
    .compare {{ padding:14px; }} .compare h3 {{ margin:0 0 3px; }}
    .images {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:7px; margin-top:12px; }}
    figure {{ margin:0; }} figcaption {{ color:var(--muted); font-size:11px; margin-bottom:5px; }}
    img {{ width:100%; height:300px; object-fit:contain; background:#080a0f; border-radius:6px; }}
    details pre {{ white-space:pre-wrap; word-break:break-word; color:#bac6da; }}
    @media (max-width:700px) {{ main {{ padding:18px; }} .images {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body><main>
  <div class="headline"><h1>Lossless design gate</h1><span class="badge {status_class}">{status}</span></div>
  <div class="muted">Feature: <code>{_e(result.get('featureId'))}</code></div>
  {provenance_warning}
  <section class="cards">
    <div class="card"><span>Passed gates</span><b>{summary['passedGates']}/{summary['gateCount']}</b></div>
    <div class="card"><span>Hard failures</span><b>{summary['hardFailures']}</b></div>
    <div class="card"><span>Warnings</span><b>{summary['warnings']}</b></div>
    <div class="card"><span>Total defects</span><b>{summary['defects']}</b></div>
    <div class="card"><span>Withheld elements</span><b>{summary.get('excludedElements', 0)}</b></div>
    <div class="card"><span>Approved deviations</span><b>{summary.get('approvedDeviations', 0)}</b></div>
  </section>

  <h2>Gate summary</h2><div class="panel"><table>
    <thead><tr><th>Gate</th><th>Status</th><th>Checked</th><th>Hard</th><th>Warnings</th></tr></thead>
    <tbody>{gates_html}</tbody></table></div>

  <h2>Property accounting</h2><div class="panel">{accounting_html}</div>

  <h2>Design token usage</h2><div class="panel">{token_html}</div>

  <h2>Withheld from gating</h2><div class="panel">{exclusions_html}</div>

  <h2>Data bindings</h2><div class="panel">{data_contract_html}</div>

  <h2>Approved deviations</h2><div class="panel">{deviations_html}</div>

  <h2>Defect manifest</h2>
  <div class="filters"><input id="search" placeholder="Filter node, gate, type, message…">
    <select id="severity"><option value="">All severities</option><option>hard</option><option>warning</option></select></div>
  <div class="panel"><table id="defects"><thead><tr><th>ID</th><th>Gate</th><th>Type</th><th>Severity</th>
    <th>Screen</th><th>Element</th><th>Message</th><th>Expected / actual</th></tr></thead>
    <tbody>{defects_html}</tbody></table></div>

  <h2>Screen coverage</h2><div class="panel"><table>
    <thead><tr><th>Node</th><th>Name</th><th>Context</th><th>Spec</th><th>Implementation</th>
    <th>Structure</th><th>Copy</th><th>Assets</th><th>Visual</th><th>Flow</th></tr></thead>
    <tbody>{coverage_html}</tbody></table></div>

  <h2>Capture routes</h2><div class="panel">
    <p style="padding:12px 12px 0"><strong>Scope warning:</strong> a product-route
    capture checks data slots only. It does not verify that screen's copy,
    geometry, style, structure, assets, tokens, or pixels; those design gates
    require a fixture capture.</p><table>
    <thead><tr><th>Snapshot</th><th>Node</th><th>Name</th><th>Route</th><th>Route kind</th><th>Verification scope</th></tr></thead>
    <tbody>{capture_routes_html}</tbody></table></div>

  <h2>Visual evidence</h2><section class="comparisons">{comparison_html}</section>
  <h2>Configuration</h2><details class="panel"><summary style="padding:12px">Exact thresholds used</summary>
    <pre style="padding:0 12px 12px">{_e(json.dumps(result['config'], ensure_ascii=False, indent=2))}</pre></details>
</main>
<script>
  const search = document.querySelector('#search');
  const severity = document.querySelector('#severity');
  const rows = [...document.querySelectorAll('#defects tbody tr[data-search]')];
  function filterRows() {{
    const query = search.value.toLowerCase();
    rows.forEach(row => {{
      const matchesText = row.dataset.search.includes(query);
      const matchesSeverity = !severity.value || row.dataset.severity === severity.value;
      row.hidden = !(matchesText && matchesSeverity);
    }});
  }}
  search.addEventListener('input', filterRows); severity.addEventListener('change', filterRows);
</script></body></html>
"""
    path = output_dir / "report.html"
    path.write_text(document, encoding="utf-8")
    return path


def _reconcile_gates(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Recompute each gate's status against final, post-deviation severities.

    The validator caches a gate's status right after that gate runs, before
    approved deviations reclassify some of its hard defects. Left alone, a
    gate that failed and was then excused still reads FAIL forever, while the
    run as a whole reads passed -- a report that contradicts itself. `result`
    already carries the final severities on every defect (approved
    deviations are rewritten in place, never deleted), so this rebuilds
    `checked`/`hardFailures`/`warnings`/`status`/`passed` from that source of
    truth. The pre-deviation numbers are kept under `original*` because "how
    many hard failures were reclassified" is exactly what a reviewer needs to
    trust the excuse.
    """

    defects_by_gate: dict[str, list[dict[str, Any]]] = {}
    for defect in result.get("defects", []):
        defects_by_gate.setdefault(defect.get("gate"), []).append(defect)

    reconciled: dict[str, dict[str, Any]] = {}
    for name, gate in result.get("gates", {}).items():
        gate_defects = defects_by_gate.get(name, [])
        hard = sum(1 for item in gate_defects if item["severity"] == "hard")
        warnings = sum(
            1 for item in gate_defects if item["severity"] == "warning"
        )
        checked = gate.get("checked", 0)
        incomplete = (
            gate.get("designCoverageComplete") is False and hard == 0
        )
        status = (
            "FAIL"
            if hard
            else (
                "INCOMPLETE"
                if incomplete
                else ("NOT_EVALUATED" if checked == 0 else "PASS")
            )
        )
        reconciled[name] = {
            **gate,
            "status": status,
            "passed": hard == 0 and not incomplete,
            "hardFailures": hard,
            "warnings": warnings,
            "originalStatus": gate.get("status"),
            "originalHardFailures": gate.get("hardFailures"),
            "originalWarnings": gate.get("warnings"),
            "originalPassed": gate.get("passed"),
            "reclassifiedHard": max(0, (gate.get("hardFailures") or 0) - hard),
        }
    return reconciled


def _copy_comparison_images(
    result: dict[str, Any], assets_dir: Path
) -> list[dict[str, Any]]:
    runtime = result.get("_runtimePaths", {})
    bundle_dir = Path(runtime["bundle"])
    actual_base = Path(runtime["actualRoot"])
    output_root = Path(runtime["output"])
    copied = []
    for item in result.get("comparisons", []):
        next_item = dict(item)
        for kind in ("reference", "actual", "diff"):
            value = item.get(kind)
            if not value or value == "TODO":
                next_item[kind] = None
                continue
            source_root = {
                "reference": bundle_dir,
                "actual": actual_base,
                "diff": output_root,
            }[kind]
            source = resolve_within(
                source_root, value, label=f"{kind} report image"
            )
            if not source.exists():
                next_item[kind] = None
                continue
            target = assets_dir / (
                f"{safe_slug(item['screenNodeId'])}-{kind}{source.suffix or '.png'}"
            )
            if source.resolve() != target.resolve():
                shutil.copy2(source, target)
            next_item[kind] = str(target.relative_to(assets_dir.parent))
        copied.append(next_item)
    return copied


def _gate_row(name: str, gate: dict[str, Any]) -> str:
    status = gate.get("status", "PASS" if gate["passed"] else "FAIL")
    klass = (
        "yes"
        if status == "PASS"
        else (
            "warning"
            if status in {"NOT_EVALUATED", "INCOMPLETE"}
            else "no"
        )
    )
    original = gate.get("originalStatus")
    reclassified = gate.get("reclassifiedHard") or 0
    note = (
        f' <span class="muted">(was {_e(original)}; '
        f"{reclassified} hard failure(s) reclassified as approved "
        "deviations)</span>"
        if original and original != status and reclassified
        else ""
    )
    return (
        f"<tr><td><code>{_e(name)}</code></td><td class=\"{klass}\">{status}{note}</td>"
        f"<td>{gate['checked']}</td><td>{gate['hardFailures']}</td>"
        f"<td>{gate['warnings']}</td></tr>"
    )


def _defect_row(item: dict[str, Any]) -> str:
    search = " ".join(
        str(item.get(key, ""))
        for key in (
            "id",
            "gate",
            "type",
            "severity",
            "screenNodeId",
            "elementNodeId",
            "message",
            "expectedToken",
        )
    ).lower()
    payload: dict[str, Any] = {
        "expected": item.get("expected"),
        "actual": item.get("actual"),
    }
    if item.get("expectedToken"):
        payload["expectedToken"] = item["expectedToken"]
    if item.get("undefinedCustomProperties"):
        payload["undefinedCustomProperties"] = item["undefinedCustomProperties"]
    values = _e(json.dumps(payload, ensure_ascii=False))
    return (
        f'<tr data-search="{_e(search)}" data-severity="{_e(item["severity"])}">'
        f'<td><code>{_e(item["id"])}</code></td><td>{_e(item["gate"])}</td>'
        f'<td><code>{_e(item.get("type"))}</code></td>'
        f'<td class="{_e(item["severity"])}">{_e(item["severity"])}</td>'
        f'<td><code>{_e(item.get("screenNodeId"))}</code></td>'
        f'<td><code>{_e(item.get("elementNodeId"))}</code></td>'
        f'<td>{_e(item["message"])}</td><td><code>{values}</code></td></tr>'
    )


def _accounting_panel(accounting: dict[str, Any] | None) -> str:
    if accounting is None:
        return (
            '<p class="empty" style="padding:12px">This bundle carries no '
            "canonical property accounting, so the accounting gate was not "
            "evaluated.</p>"
        )
    if not accounting:
        # A falsy-but-present artifact is a different fact from an absent one,
        # and the validator fails the run for it. Rendering both as "not
        # evaluated" made the report contradict the verdict beside it.
        return (
            '<p class="empty no" style="padding:12px">This bundle\'s property '
            "accounting artifact is present but empty, so it proves nothing "
            "about what the compiler accounted for. The accounting gate "
            "fails.</p>"
        )
    counts = accounting.get("counts", {})
    rows = "".join(
        _accounting_row(label, counts.get(key, 0), alert)
        for label, key, alert in (
            ("Canonical screens", "screens", None),
            ("Canonical nodes", "nodes", None),
            ("Compiled property keys", "compiledKeys", None),
            ("Preserved opaque keys", "preservedOpaqueKeys", None),
            ("Unsupported properties", "unsupported", "warning"),
            ("Unaccounted properties", "violations", "no"),
            ("Unresolved token bindings", "unresolvedTokenBindings", "warning"),
            (
                "Unverified REST image fills",
                "unverifiedImageFills",
                "warning",
            ),
        )
    )
    return (
        "<table><thead><tr><th>Canonical evidence</th><th>Count</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"{_unverified_image_fills_panel(accounting.get('unverifiedImageFills'))}"
        f"{_collection_panel(accounting.get('restCollection'))}"
    )


def _unverified_image_fills_panel(items: Any) -> str:
    """Name every node whose IMAGE fill the asset gate could not check.

    The REST collector never fetches fill image content, so the asset gate
    checks zero assets on every REST-evidenced screen -- even one full of
    icons and photos. That is a known limitation, not a defect the
    implementation introduced, but a silent "assets": [] reads exactly like
    "this screen has no images". Listing the nodes here is what tells a
    reviewer the difference.
    """

    if not items:
        return ""
    rows = "".join(
        f"<tr><td><code>{_e(item.get('screenNodeId'))}</code></td>"
        f"<td><code>{_e(item.get('nodeId'))}</code></td>"
        f"<td><code>{_e(item.get('imageRef'))}</code></td></tr>"
        for item in items
    )
    return (
        f'<p style="padding:12px 12px 0"><b>{len(items)} IMAGE fill(s)</b> were '
        "detected in the Figma REST evidence but never verified: the REST "
        "collector does not fetch fill content, so the asset gate cannot "
        "check these against the implementation. This is a harness "
        "limitation, not a passing result -- treat these nodes' images as "
        "unverified.</p>"
        "<table><thead><tr><th>Screen</th><th>Node</th><th>Image ref</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _deviations_panel(deviations: Any) -> str:
    """List differences the operator judged to be design-side, with reasons.

    These stopped counting as failures, so the reasons have to be readable
    here: an approval nobody can review is indistinguishable from a silenced
    defect.
    """

    if not isinstance(deviations, dict):
        return ('<p class="empty" style="padding:12px">No approved '
                "deviations: every difference was gated.</p>")
    defects = deviations.get("defects") or []
    unused = deviations.get("unusedEntries") or []
    blocks = []
    if defects:
        rows = "".join(
            f"<tr><td>{_e(item.get('gate'))}</td>"
            f"<td><code>{_e(item.get('screenNodeId'))}</code></td>"
            f"<td><code>{_e(item.get('elementNodeId'))}</code></td>"
            f"<td>{_e(item.get('message'))}</td>"
            f"<td>{_e(item.get('approvedReason'))}</td></tr>"
            for item in defects
        )
        blocks.append(
            f'<p style="padding:12px 12px 0">{len(defects)} differences were '
            "judged to be design-side and do not fail the run. Each one is "
            "still a difference from the design.</p>"
            "<table><thead><tr><th>Gate</th><th>Screen</th><th>Node</th>"
            f"<th>Difference</th><th>Reason</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    else:
        blocks.append('<p class="empty" style="padding:12px">No approved '
                      "deviations: every difference was gated.</p>")
    if unused:
        rows = "".join(
            f"<tr><td>{_e(json.dumps(item, ensure_ascii=False))}</td></tr>"
            for item in unused
        )
        blocks.append(
            f'<p style="padding:12px 12px 0"><b>{len(unused)} approval(s) '
            "matched nothing.</b> A stale approval widens what passes without "
            "anyone noticing; remove it or fix its selector.</p>"
            f"<table><tbody>{rows}</tbody></table>"
        )
    return "".join(blocks)


def _exclusions_panel(exclusions: Any) -> str:
    """List every contracted element the config withheld from the gates.

    A withheld element is not a passing element -- it is one the operator took
    responsibility for. Naming each one here is what keeps the escape hatch
    from reading as a clean sheet.
    """

    if not isinstance(exclusions, dict):
        return (
            '<p class="empty" style="padding:12px">Nothing was withheld: every '
            "contracted element was gated.</p>"
        )
    elements = exclusions.get("elements") or []
    copy_elements = exclusions.get("copy") or []
    names = exclusions.get("subtreeNames") or []
    if not elements and not copy_elements:
        return (
            '<p class="empty" style="padding:12px">Nothing was withheld: every '
            "contracted element was gated.</p>"
        )
    blocks = []
    if elements:
        rows = "".join(
            f"<tr><td><code>{_e(item.get('screenNodeId'))}</code></td>"
            f"<td><code>{_e(item.get('nodeId'))}</code></td>"
            f"<td>{_e(item.get('name'))}</td>"
            f"<td>{_e(item.get('reason'))}</td></tr>"
            for item in sorted(
                elements,
                key=lambda entry: (
                    str(entry.get("screenNodeId")),
                    str(entry.get("nodeId")),
                ),
            )
        )
        blocks.append(
            f'<p style="padding:12px 12px 0">{len(elements)} contracted elements '
            "are declared device chrome. They were not verified as rendered "
            "content; instead the structure gate requires that the implementation "
            "does <b>not</b> render them, and their regions were masked out of the "
            "visual diff. Declared subtree names: "
            f"<code>{_e(', '.join(names)) or '(none)'}</code>.</p>"
            "<table><thead><tr><th>Screen</th><th>Node</th><th>Element</th>"
            f"<th>Reason</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    if copy_elements:
        rows = "".join(
            f"<tr><td><code>{_e(item.get('screenNodeId'))}</code></td>"
            f"<td><code>{_e(item.get('nodeId'))}</code></td>"
            f"<td>{_e(item.get('reason'))}</td></tr>"
            for item in copy_elements
        )
        blocks.append(
            f'<p style="padding:12px 12px 0">{len(copy_elements)} node(s) are '
            "reviewed chrome in the data contract. Only exact-copy comparison "
            "was withheld; the other gates are unchanged.</p>"
            "<table><thead><tr><th>Screen</th><th>Node</th><th>Reason</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )
    return "".join(blocks)


def _data_contract_panel(contract: Any) -> str:
    """Show which runtime source each reviewed slot is expected to use."""

    if not isinstance(contract, dict):
        return (
            '<p class="empty" style="padding:12px">No data contract was '
            "compiled; every text node was judged as fixed interface copy.</p>"
        )
    slots = contract.get("slots") or []
    if not slots:
        return (
            '<p class="empty" style="padding:12px">The data contract declares '
            "no runtime data slots.</p>"
        )
    rows = "".join(
        f"<tr><td><code>{_e(item.get('screenNodeId'))}</code></td>"
        f"<td><code>{_e(item.get('nodeId'))}</code></td>"
        f"<td><code>{_e(item.get('binding'))}</code></td>"
        f"<td>{_e(item.get('shape') or 'any non-empty text')}</td>"
        f"<td><code>{_e(item.get('designExemplar'))}</code></td></tr>"
        for item in slots
    )
    return (
        "<table><thead><tr><th>Screen</th><th>Node</th><th>Binding</th>"
        "<th>Shape</th><th>Design exemplar</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _token_panel(result: dict[str, Any]) -> str:
    """Show whether bound design tokens survived into the implementation's CSS."""

    gate = result.get("gates", {}).get("token")
    config = result.get("config", {})
    policy = str(config.get("tokenUsagePolicy") or "warn")
    if not gate:
        return (
            '<p class="empty" style="padding:12px">The token gate did not '
            "run.</p>"
        )
    if gate.get("status") == "NOT_EVALUATED":
        reason = (
            "tokenUsagePolicy is \"off\"."
            if policy == "off"
            else (
                "no captured element carries declared var() references, so "
                "the snapshot predates the token channel or the elements "
                "under contract bind no Figma variables."
            )
        )
        return (
            f'<p class="empty" style="padding:12px">Token usage was not '
            f"evaluated: {_e(reason)}</p>"
        )
    defects = [
        item for item in result.get("defects", []) if item.get("gate") == "token"
    ]
    unmapped = _token_names(defects, "token-unmapped")
    unused = _token_names(defects, "token-not-used")
    undefined: list[str] = []
    for item in defects:
        for name in item.get("undefinedCustomProperties") or []:
            if name not in undefined:
                undefined.append(name)
    token_map = config.get("tokenMap")
    rows = "".join(
        _accounting_row(label, value, alert)
        for label, value, alert in (
            ("Usage policy", policy, None),
            (
                "Mapped tokens in config",
                len(token_map) if isinstance(token_map, dict) else 0,
                None,
            ),
            ("Token bindings checked", gate.get("checked", 0), None),
            (
                "Tokens with no CSS mapping",
                ", ".join(unmapped) or "none",
                "warning" if unmapped else None,
            ),
            (
                "Tokens bypassed by the implementation",
                ", ".join(unused) or "none",
                ("no" if policy == "hard" else "warning") if unused else None,
            ),
            (
                "Custom properties never defined",
                ", ".join(undefined) or "none",
                "no" if undefined else None,
            ),
        )
    )
    return (
        "<table><thead><tr><th>Design token usage</th><th>Value</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _token_names(defects: list[dict[str, Any]], defect_type: str) -> list[str]:
    names: list[str] = []
    for item in defects:
        if item.get("type") != defect_type:
            continue
        name = item.get("expectedToken")
        if name and name not in names:
            names.append(str(name))
    return names


def _collection_panel(collection: Any) -> str:
    """Show whether the REST collection behind this bundle is provably whole."""

    if not isinstance(collection, dict):
        return ""
    complete = collection.get("complete") is True
    missing = collection.get("missingNodeIds") or []
    failures = collection.get("failures") or []
    rows = "".join(
        _accounting_row(label, value, alert)
        for label, value, alert in (
            (
                "Collection complete",
                "yes" if complete else "no",
                None if complete else "no",
            ),
            (
                "Expected node IDs",
                len(collection.get("expectedNodeIds") or []),
                None,
            ),
            (
                "Collected node IDs",
                len(collection.get("collectedNodeIds") or []),
                None,
            ),
            (
                "Uncollected node IDs",
                ", ".join(str(item) for item in missing) or "none",
                "no" if missing else None,
            ),
        )
    )
    failure_rows = "".join(_failure_row(item) for item in failures)
    return (
        '<table style="margin-top:12px"><thead><tr><th>Figma REST collection</th>'
        f"<th>Value</th></tr></thead><tbody>{rows}{failure_rows}</tbody></table>"
    )


def _failure_row(item: Any) -> str:
    node_id = item.get("id") if isinstance(item, dict) else item
    reason = item.get("reason") if isinstance(item, dict) else ""
    return (
        "<tr><td>Collection failure</td>"
        f'<td class="no"><code>{_e(node_id)}</code> {_e(reason)}</td></tr>'
    )


def _accounting_row(label: str, value: Any, alert: str | None) -> str:
    cell = (
        f'<td class="{alert}">{_e(value)}</td>'
        if alert and value
        else f"<td>{_e(value)}</td>"
    )
    return f"<tr><td>{_e(label)}</td>{cell}</tr>"


def _coverage_row(item: dict[str, Any]) -> str:
    status = item.get("status", {})
    implementation_state = status.get("implementationState")
    implementation = (
        '<span class="yes">design-verified</span>'
        if implementation_state == "design-verified"
        else (
            '<span class="warning">product-verified-only</span>'
            if implementation_state == "product-verified-only"
            else '<span class="no">missing</span>'
        )
    )
    return (
        f'<tr><td><code>{_e(item.get("nodeId"))}</code></td><td>{_e(item.get("name"))}</td>'
        f'<td>{_bool(status.get("contextFetched"))}</td>'
        f'<td>{_bool(status.get("specCompiled"))}</td>'
        f'<td>{implementation}</td>'
        f'<td>{_bool(status.get("structurePassed"))}</td>'
        f'<td>{_bool(status.get("copyPassed"))}</td>'
        f'<td>{_bool(status.get("assetPassed"))}</td>'
        f'<td>{_bool(status.get("visualPassed"))}</td>'
        f'<td>{_bool(status.get("flowPassed"))}</td></tr>'
    )


def _capture_route_row(item: dict[str, Any]) -> str:
    route_kind = item.get("routeKind") or "unknown"
    klass = (
        "yes"
        if route_kind == "product"
        else ("warning" if route_kind == "fixture" else "no")
    )
    return (
        f'<tr><td>{_e(item.get("snapshot"))}</td>'
        f'<td><code>{_e(item.get("screenNodeId"))}</code></td>'
        f'<td>{_e(item.get("name"))}</td>'
        f'<td><code>{_e(item.get("route"))}</code></td>'
        f'<td class="{klass}">{_e(route_kind)}</td>'
        f'<td>{_e(item.get("verificationScope"))}</td></tr>'
    )


def _comparison_card(item: dict[str, Any]) -> str:
    status = "PASS" if item.get("passed") else "FAIL"
    klass = "yes" if item.get("passed") else "no"
    ratio = item.get("mismatchRatio")
    ratio_text = "n/a" if ratio is None else f"{float(ratio) * 100:.4f}%"
    images = "".join(
        _figure(label, item.get(kind))
        for label, kind in (("Figma", "reference"), ("Implementation", "actual"), ("Diff ×4", "diff"))
    )
    return (
        f'<article class="compare panel"><h3>{_e(item.get("name"))}</h3>'
        f'<div><code>{_e(item.get("screenNodeId"))}</code> · '
        f'<span class="{klass}">{status}</span> · mismatch {ratio_text}</div>'
        f'<div class="images">{images}</div></article>'
    )


def _figure(label: str, path: str | None) -> str:
    content = (
        f'<img loading="lazy" src="{_e(path)}" alt="{_e(label)}">'
        if path
        else '<div class="empty">Missing</div>'
    )
    return f'<figure><figcaption>{_e(label)}</figcaption>{content}</figure>'


def _bool(value: Any) -> str:
    if value is None:
        return '<span class="warning">not measured</span>'
    return '<span class="yes">yes</span>' if value else '<span class="no">no</span>'


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)
