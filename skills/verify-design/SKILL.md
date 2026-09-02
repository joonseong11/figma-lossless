---
name: verify-design
description: Collect canonical Figma REST evidence (or compile preserved Figma MCP CallToolResult JSON), compile it into a verifiable Design IR with property accounting, and run hard UI implementation gates for coverage, accounting, structure, exact copy, geometry, style, assets, pixels, design-token usage, components, and flows. Use when a Figma design must become implementation contracts, when diagnosing an incomplete Figma-to-code result, or when verifying a local web UI against Figma without relying on visual judgment alone.
---

# Verify a Figma design implementation

Use the packaged harness as the deterministic verdict layer for a Figma-driven UI task. Keep raw Figma responses and generated evidence out of source control unless the user explicitly chooses a safe storage location.

## Resolve the installed runtime

Resolve the plugin root before anything else; do not assume the target repository contains the harness source. Claude Code injects `CLAUDE_PLUGIN_ROOT`, so prefer it and fall back to the directory two levels above this `SKILL.md`:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-<absolute path to the directory containing this skills/ directory>}"
HARNESS="${PLUGIN_ROOT}/skills/verify-design/scripts/run_harness.py"
CAPTURE="${PLUGIN_ROOT}/adapters/playwright-capture.mjs"
```

Run `python3 "$HARNESS" --help` before the first operation. The launcher provisions its own isolated environment on first run and caches it under `~/.cache/figma-lossless` (override with `FIGMA_LOSSLESS_CACHE_DIR`); that one-time setup needs network access. If the environment must not be created, set `FIGMA_LOSSLESS_NO_BOOTSTRAP=1` and install the harness dependencies into an approved environment yourself, then run the same launcher with that interpreter.

## Inputs to confirm with the user

Only these require a human decision — everything else is mechanical:

1. **Target design scope** — the Figma URL(s). Convert URL node ids to API form (`node-id=22143-63319` → `22143:63319`). Use a top-node metadata call only to inventory frames; every implementation frame gets its own collected evidence.
2. **Implementation location and run command** — repository, branch, dev-server command, and one localhost fixture route per screen state (locale, error, loading, ...).
3. **Token map** — Figma variable name → CSS custom property mapping for the token gate (see gate configuration below). Start empty; the first validate run lists every unmapped name.
4. **Flow contract and component map** — PRD state transitions, and which shared component each contracted element must resolve to. `requireFlowContract` and `requireComponentContract` both default to **true**, so a run without them fails hard with `flow-contract-missing` / `component-contract-missing`. These are not optional inputs you may skip silently: either author them, or declare in `gate-config.json` that this feature has no flows / no shared components (`{"requireFlowContract": false, "requireComponentContract": false}`). Declaring it is a decision someone can review; omitting it used to be invisible, which is how a 61-screen pilot shipped with no behaviour verified and a green report.

## Execution policy

**Run to completion.** Once the request and its scope are resolved, drive the selected workflow to its terminal state without pausing between steps to ask permission or report intermediate progress as a question. Mechanical failures (bootstrap, network retries, dev-server startup, a failing gate that implementation work can fix) are yours to diagnose and resolve, not to hand back. End the turn early only when blocked on user-only input or on a request defect (below).

**Resolve the scope first.** When the user's words leave the scope ambiguous, that is itself a request defect — ask once, then run:

- **Copy audit** (locale copy check only): collect the locale frames (omit `--include-images`; no pixels are used) → compile → `export-copy` → diff against the message catalogs the user names → report matched, missing, and mismatched entries per locale. That report is the terminal state. Do not instrument, capture, or validate an implementation in this scope.
- **Full verification**: the complete workflow below. The terminal state is a fresh exit-`0` validate — or a defect report whose remaining entries all require a user decision (genuine design deviations needing an approved-deviation entry, not fixable implementation errors).

Once the scope is resolved, declare it before running `collect`:

```bash
python3 "$PLUGIN_ROOT/hooks/harness_hook.py" scope --set full     # or: --set copy-audit
```

This matters most for the locale copy audit *inside* full verification — dumping the copy contract with `export-copy` before `capture`/`validate` have run. Up to that point the two recipes are byte-identical, so without the declaration the hook cannot tell them apart and may mark the workflow terminal early, leaving the implementation stretch unenforced. It self-heals once `capture` or `validate` runs; declaring the scope closes the window from the start.

**Ask back to complete the request.** Check the request against the evidence twice — when first analyzing the PRD/request, and again after compiling the design evidence. Stop and ask the user targeted questions when the request itself is defective:

- **Unconsidered** — states, locales, or frames present in the design but absent from the request (or vice versa)
- **Insufficient** — the request omits something the workflow needs and no safe default exists (e.g. no fixture route for a state the design shows)
- **Wrong** — the request names a frame/behavior that does not exist or contradicts the design evidence
- **Contradictory** — PRD flows conflict with Figma states, or two requested behaviors conflict with each other

Batch every such question into a single round, propose a default answer for each, and resume immediately once answered. The goal of asking is to make the request complete — never to transfer a decision a sensible default already covers. Everything with a sensible default is not a question; the hard gates exist precisely so defaults can be taken safely.

### Enforcement hooks

The parts of this execution policy that are mechanically checkable are enforced by plugin hooks (`hooks/hooks.json` and `hooks/harness_hook.py`), not left to memory alone. They track workflow state in `<cwd>/.figma-lossless/state.json` (`active`, `scope`, `terminal`, `pausedForUser`, `blockCount`) across the session. These hooks are registered globally across every session and project, so tracking only activates where harness use is actually observed — a real `collect`/`compile`/`validate`/capture/`export-copy` command, or a prompt that starts with `/verify-design` or `$figma-lossless:verify-design` (merely discussing the skill does not activate it) — and a workflow untouched for 24 hours is treated as abandoned and self-heals to inactive rather than nagging the directory indefinitely:

- **Stop** is blocked while a workflow is `active` and not yet `terminal` — copy-audit reaches terminal after a successful `export-copy`; full verification reaches terminal only after the hook reads a fresh `gate-results.json` itself and finds `passed: true`. Chat claims of success are never trusted. The block gives up after 3 consecutive attempts so it pressures completion without trapping the session.
- **UserPromptSubmit** initializes tracking on an explicit invocation, and clears a declared pause when the user answers.
- **SessionStart** re-announces an unfinished, non-stale workflow on resume.
- **PreToolUse** denies `--use-reference-screenshots` outside the harness's own `tests/` directory (it is self-test-only evidence, never implementation evidence — see Integrity rules below), and asks for explicit approval before an edit that plausibly loosens a gate: any single tolerance/policy key in a `gate-config*.json` file, or two or more distinct tolerance/policy keys in any other `.json` file.

To end the turn on a genuine request defect (not a fixable implementation error) without fighting the Stop block, declare it explicitly:

```bash
python3 "$PLUGIN_ROOT/hooks/harness_hook.py" pause --reason "<why the request itself is blocked>"
```

To manually clear tracked state for a directory (abandoned experiment, stuck cleanup) without waiting for the 24-hour expiry, run `python3 "$PLUGIN_ROOT/hooks/harness_hook.py" reset` — no reason required.

## Workflow (REST-first)

1. **Collect canonical evidence.** Requires a Figma personal access token in `~/.figma-token` (bare token, or a `FIGMA_TOKEN=` / `FIGMA_*_TOKEN=` line; `export` prefix allowed) or the `FIGMA_TOKEN` environment variable. Never print or store the token in any artifact.

```bash
python3 "$HARNESS" collect \
  --file-key <file-key-from-url> \
  --node-ids "<id1>,<id2>,..." \
  --output <artifact-dir>/evidence \
  --include-images
```

Exit `0` proves `expected == collected` (nodes and images) at a pinned file version. Exit `2` means the collection is incomplete — stop and resolve before compiling; downstream gates will hard-fail the missing nodes regardless.

2. **Fetch variable definitions** for the same selection with the Figma MCP `get_variable_defs` tool when available, and save the JSON payload. This resolves `boundVariables` ids to token names (the REST Variables API is Enterprise-only; MCP is the non-Enterprise path).

3. **Compile before implementing:**

```bash
python3 "$HARNESS" compile \
  --rest-input <artifact-dir>/evidence \
  --output <artifact-dir>/bundle \
  --feature-id <stable-feature-id> \
  [--variable-defs <variable-defs.json>] \
  [--flow-contract <flow-contract.json>] \
  [--component-map <component-map.json>] \
  [--data-contract <data-contract.json>]
```

The brackets are CLI syntax, not permission to skip: `requireFlowContract` and
`requireComponentContract` default to true, so omitting a contract here fails
the run at `validate` unless `gate-config.json` declares the feature has none.
`data-contract.json` is optional for compatibility and its absence is reported;
when reviewed slots exist, compile it so runtime data is not judged as fixed copy.

Treat a nonzero exit as a hard stop. Do not implement while `missingContexts` or `accountingViolations` are nonzero: an accounting violation means Figma returned a property the compiler cannot classify, and silence is never an acceptable resolution.

4. **Freeze short-lived Figma assets** promptly when network access and redistribution authority are in scope:

```bash
python3 "$HARNESS" vendor-assets --bundle <artifact-dir>/bundle
```

5. **Implement a three-layer boundary against the compiled screen JSON.** Do
   not make the shipping page own Figma sample values or permanent audit
   markers. Treat the fixture like a crash-test rig: it exercises the same
   cabin as the real car, but its sensors and dummy passenger do not ship with
   customers.

   - **Presentational shell component.** It receives every displayed value,
     callback, and optional `ids?` map through props. It does not call product
     hooks or APIs itself. Apply `data-node-id={ids?.field}` to contracted
     descendants (not only the screen root), because child rectangles are how
     padding and gaps are measured. Put the stable contract binding on each
     runtime value as `data-slot="user.email"`. Unlike a Figma node ID, this is
     meaningful product metadata, like `data-testid`: it says what data belongs
     in that position without exposing design-tool internals. Accept the other audit metadata through
     fixture-only props or wrappers in the same way: `data-component`,
     `data-figma-asset`, and `data-asset-sha256` must not force audit concerns
     into the product data layer.
   - **Audit fixture page.** It injects the exact Figma exemplars and the `ids`
     map into that shell. Close this route in deployed builds: omit it from the
     production route set or return 404 unless an explicit audit environment is
     enabled. A fixture is a deterministic page made for the harness, not a
     user-facing fallback.
   - **Product page.** It loads real data, supplies real callbacks, and renders
     the same shell without `ids`. Its slot element retains `data-slot` with a
     value exactly equal to the reviewed contract's `binding`. Do not copy the
     Figma exemplar into product source as a default value.

   Capturing only the fixture is insufficient. It proves the shell can draw the
   design, but it cannot reveal a product page that still hard-codes the sample
   person or bypasses the real data hook. Every screen that declares a data
   slot must also be captured once from its product route with
   `routeKind: "product"`; otherwise validation records
   `product-route-unverified` (warning by default, hard when
   `requireProductRouteCheck` is true). This absence-of-evidence defect cannot
   be approved as a design deviation.

6. **Capture the implementation.** Create or update a trusted capture plan with localhost routes. Each screen entry may set `routeKind` to `"fixture"` or `"product"`; omission keeps the existing fixture behavior. A fixture requires the Figma `data-node-id` root and collects the full design evidence. A product capture does not look for that root: it collects only `[data-slot]` markers and their rendered values, keyed by the binding name. Copy, geometry, style, structure, asset, token, and pixel gates do not evaluate a product screen. A passing product row therefore means only that real data occupied the declared slots, never that the whole screen matched Figma.

   Capture the fixture and product routes in separate adapter runs. Pass the fixture snapshot as `--actual` and the product snapshot as `--actual-b` when one report should include both. If `--actual-b` is also needed for the two-fixture differential slot check, run that check separately and retain both reports as evidence. From the target web repository, run its development server and then run:

```bash
node "$CAPTURE" \
  --plan <capture-plan.json> \
  --output <artifact-dir>/actual.json
```

The adapter resolves `playwright` from the target repository. Install Playwright in the target only when the project already uses it or the user authorizes that dependency change. `prepareScript` is disabled by default; pass `--allow-prepare-script true` only for a reviewed, trusted plan.

7. **Run the complete gate set:**

```bash
python3 "$HARNESS" validate \
  --bundle <artifact-dir>/bundle \
  --actual <artifact-dir>/actual.json \
  [--actual-b <artifact-dir>/actual-b.json] \
  --output <artifact-dir>/report \
  [--config <gate-config.json>] \
  [--flow-results <flow-results.json>]
```

Exit `0` is the only passing verdict. Exit `2` means one or more hard gates failed; read `defects.json` and fix the implementation or an explicitly incorrect contract, then recapture and revalidate. Exit `1` means the harness input or operation itself failed.

### Gate configuration for token usage

The token gate compares Figma variable bindings against the custom properties the implementation actually references (`var(--...)` declarations observed in CSSOM). Configure it in `gate-config.json`:

```json
{
  "tokenUsagePolicy": "warn",
  "tokenMap": {
    "color/primary-500": "--color-primary-500",
    "color/grey-50": ["--color-grey-50", "--grey-50"]
  }
}
```

Start with `"warn"` and an empty map; the first validate run emits `token-unmapped` warnings naming every Figma token the design uses — fill the map from that list plus the project's token definitions, then raise the policy to `"hard"` once the team agrees. In projects whose styling does not flow through CSS custom properties, leave the policy at `"warn"` and treat the gate as advisory.

### Locale copy audit

When designers ship a per-locale frame for each screen (e.g. a Korean and an English sibling of the same notification), collect and compile each as its own screen — the capture plan then maps every locale route to its matching frame nodeId, so the copy gate enforces exact per-locale text at validation time just like any other screen. To audit that copy against i18n message catalogs (`ko.json`, `en.json`, ...) without a running implementation, dump the full copy contract from the compiled bundle:

```bash
python3 "$HARNESS" export-copy --bundle <artifact-dir>/bundle --output <artifact-dir>/copy.json [--format csv]
```

This flattens every screen's `texts` entries (`screenNodeId`, `screenName`, `nodeId`, `property`, `value`) into one sorted artifact — `json` (default) or `csv` — suitable for diffing against a catalog file. It reads the compiled contract only; it never touches the implementation, so a clean diff here does not replace running `validate`.

## Fallback: saved MCP evidence

When REST access is unavailable, the original path still works: build a raw-evidence directory containing one root inventory `*.get-design-context.raw.json` plus a detailed context and matching `*.metadata.raw.json` per frame (complete `CallToolResult` objects including `isError`, `content`, `_meta`, image data, node ids), and compile with `--input <raw-evidence-dir>` instead of `--rest-input`. Both inputs may be combined; canonical REST data is the property source of record when present. Note that the MCP path extracts a narrower style contract and has no collection-completeness proof.

## Integrity rules

- Never summarize Figma evidence into prose and discard the raw response. Pass raw files, compiled screen JSON, hashes, node IDs, and defect IDs between agents.
- Never use `snapshot-template --use-reference-screenshots` as implementation evidence. It exists only for harness self-tests and normal validation rejects its provenance.
- Never silently omit a screen, state transition, component decision, asset, or unsupported property to obtain a pass. Missing evidence must remain visible as a hard failure or explicit confirmation requirement.
- Never widen a gate tolerance or drop a contract key to make a defect disappear. A deliberate design deviation needs an explicit approved-deviation entry, not a looser gate.
- An `approvedDeviations` entry needs both a `reason` and at least one selector (`gate`, `type`, `screenNodeId`, `elementNodeId`, `nodeId`, `property`). An entry that names only a reason matches every hard defect in the run and is rejected as `approved-deviation-unscoped`.
- An approval covers a *measured difference between the design and the implementation*, and only these defect types qualify: `copy-mismatch`, `missing-copy`, `slot-empty`, `slot-not-bound`, `slot-shape-mismatch`, `geometry-mismatch`, `style-mismatch`, `gradient-missing`, `visual-mismatch`, `missing-element`, `hidden-element-rendered`, `rendered-asset-mismatch`, `token-not-used`, `component-not-observed`, `flow-test-failed`. Every other type is unapprovable and an entry aiming at one comes back in `unusedEntries`.
- Nothing that reports an *absence of verification* can be approved — `flow-test-missing` (no E2E exists), `flow-assumption-unresolved` (nobody confirmed the transition), `component-decision-open` (no mapping decided), `gate-not-evaluated` (the gate measured nothing), `product-route-unverified` (no product capture), `slot-marker-missing` (the product DOM has no marker for the reviewed binding), and `missing-actual-*` (the capture has no data). These are not differences anyone is in a position to judge. Do the verification, or declare in `gate-config.json` that there is nothing to verify — do not approve the silence.
- Keep capture routes localhost and same-origin. Do not enable `prepareScript` for an untrusted plan.
- Do not commit raw REST/MCP responses, compiled bundles, screenshots, reports, local paths, request metadata, or short-lived asset URLs by default.
- The token file must never be read into logs, artifacts, or chat output; the collector already refuses non-Figma key names — do not work around that refusal.

## Completion report

Report the collection proof (expected/collected/version), compiled screen counts, accounting summary, every gate status, hard-failure and warning counts, report path, commands actually run, and any validation gap. Declare completion only after fresh exit-`0` validation with no missing required evidence.
