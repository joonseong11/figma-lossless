from __future__ import annotations

import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw

from .data_contract import validate_data_contract
from .slot_proposal import (
    EMAIL_PATTERN,
    NUMERIC_MASK_PATTERN,
    SINGLE_GRAPHEME_PATTERN,
    TIME_PATTERN,
)
from .util import read_json, resolve_within, safe_slug, sha256_file, write_json


DEFAULT_CONFIG: dict[str, Any] = {
    "requireAllContexts": True,
    "requireAllCompiledSpecs": True,
    "requireAllElements": True,
    "requireAllExactCopy": True,
    "requireLocalAssets": True,
    "requireFlowResults": True,
    "requireFlowEvidenceBinding": True,
    # Every other require* default here is True, and these two were the
    # exceptions -- which made behaviour and component identity the only parts
    # of an implementation a run could skip without saying so. A default that
    # has to be opted into is a default that does not verify: the pilot that
    # motivated this change shipped 61 screens whose flows were never checked
    # and whose report was green. A project with genuinely no flows or no
    # component library sets these to False deliberately, which is a decision
    # someone can review; the silence was not.
    "requireFlowContract": True,
    "requireComponentContract": True,
    # Existing bundles predate reviewed data slots. They keep literal copy
    # semantics by default, while the validator reports that limitation once
    # per run instead of turning every old bundle into a hard failure.
    "requireDataContract": False,
    # Older capture plans were fixture-like but did not say so. Keep their
    # product-route absence advisory until a project deliberately requires the
    # smoke check; this preserves old bundles while making the missing evidence
    # visible in every new report.
    "requireProductRouteCheck": False,
    "requireReferenceScreenshots": True,
    "allowSelfTestSnapshot": False,
    "geometryTolerancePx": 1.0,
    # Tolerances are per unit, because 0.1 means something different for a
    # length in pixels, a unitless line-height ratio and a 0..1 opacity.
    "styleNumericTolerance": 0.1,
    "styleRatioTolerance": 0.01,
    "styleOpacityTolerance": 0.01,
    "gradientAngleToleranceDeg": 0.5,
    "visualChannelTolerance": 8,
    "visualMaxMismatchRatio": 0.005,
    # How a pixel difference is treated. Figma renders a frame at the size its
    # content actually occupies, so a design whose children bleed past the
    # frame comes back a pixel or two wider than the declared viewport and can
    # never line up with a browser capture. That is a property of the file, not
    # of the implementation, and it should not be able to mask real defects by
    # failing every screen -- so a project in that state can drop the pixel
    # channel to advisory while the element gates keep their teeth.
    #   "hard" – a mismatch fails the run (default)
    #   "warn" – a mismatch is reported but does not fail
    #   "off"  – do not compare pixels at all
    "visualPolicy": "hard",
    # How a design token that the implementation resolved by hand is treated.
    # getComputedStyle substitutes var() before the capture sees it, so
    # `#7C3AED` and `var(--color-primary-500)` are the same value to the style
    # gate; only the capture's declared `tokenRefs` tell them apart.
    #   "off"   – do not evaluate token usage at all
    #   "warn"  – report an unused token as a warning (default)
    #   "hard"  – fail the run when a bound token is not referenced
    "tokenUsagePolicy": "warn",
    # Figma variable name -> CSS custom property name, or a list of names when
    # one Figma token is legitimately spelled several ways in CSS, e.g.
    #   {"color/primary-500": "--color-primary-500"}
    # A token absent from this map is reported as config debt, never as an
    # implementation fault, because the harness cannot know the project's
    # naming convention.
    "tokenMap": {},
    # Differences the operator has judged to be design-side errors rather than
    # implementation faults. A Figma file assembled by duplicating frames
    # accumulates copy that was never swapped, values carried over from another
    # locale, and device-chrome measurements a web build must not reproduce.
    # Blocking on each of those stops the work without improving it.
    #
    # An entry does not hide the difference -- it reclassifies the defect as
    # `approved-deviation`, which keeps it out of the failure count but lists it
    # in its own report section with the reason. Matching is by whichever of
    # these fields the entry names; `reason` is mandatory, and an entry that
    # matches nothing is itself reported so stale approvals surface.
    #
    #   {"gate": "style", "property": "fontFamily",
    #    "reason": "시안이 중국어에 Pretendard 지정 — 디자이너가 오류로 확인"}
    #   {"gate": "copy", "screenNodeId": "1:2", "nodeId": "1:3",
    #    "reason": "프레임 복제 시 문구 교체 누락"}
    "approvedDeviations": [],
    # Named subtrees the implementation must NOT reproduce. A mobile mockup
    # carries device chrome -- an iOS status bar, a home indicator -- that a web
    # build has no business rendering. That is a design fact, not an
    # implementation fault, so it is declared here instead of being absorbed by
    # a looser tolerance.
    #
    #   "excludedSubtreeNames": ["Status Bar", "Home Indicator"]
    #
    # An excluded element is not waved through: it is held to the *opposite*
    # assertion. It keeps its place in the contract, is marked non-rendering, and
    # the structure gate still fails the run if the implementation draws it. Its
    # rect is masked out of both images before the visual diff so the pixel gate
    # agrees with the element gates, and every exclusion is listed in the report.
    #
    # There is deliberately no type-based exclusion. Excluding a type (say every
    # `vector`) removes the only visual substance an icon has, and nothing else
    # gates it -- the REST pipeline compiles no asset contract at all -- so an
    # implementation that draws no icons at all would pass every gate.
    "excludedSubtreeNames": [],
    # Named elements whose *interior* is not gated, while the element itself
    # still is. This is for an icon drawn from a shared barrel: the same glyph
    # is one component reused everywhere, but its Figma node ids differ per
    # instance, so the interior paths cannot carry stable markers. The
    # container keeps its node id -- structure and geometry still require the
    # icon to be present, in the right place, at the right size -- and the
    # component gate is what checks the right glyph was used, so name an
    # interior here only alongside a component mapping that covers it.
    #
    #   "excludedInteriorNames": ["Icon/arrow", "Icon/eye-open"]
    "excludedInteriorNames": [],
}

# Which CSS properties can plausibly drive a compiled style key. A token is
# considered used when any declaration under one of these properties references
# the mapped custom property; the shorthand is accepted alongside the longhand
# because `background: var(--x)` really does set the background color.
TOKEN_STYLE_KEY_CSS_PROPERTIES: dict[str, tuple[str, ...]] = {
    "backgroundColor": ("background-color", "background"),
    "color": ("color",),
    "borderColor": (
        "border-color",
        "border",
        "border-top-color",
        "border-right-color",
        "border-bottom-color",
        "border-left-color",
    ),
    "borderTopColor": ("border-top-color", "border-top", "border-color", "border"),
    "borderRightColor": (
        "border-right-color",
        "border-right",
        "border-color",
        "border",
    ),
    "borderBottomColor": (
        "border-bottom-color",
        "border-bottom",
        "border-color",
        "border",
    ),
    "borderLeftColor": ("border-left-color", "border-left", "border-color", "border"),
    "borderRadius": (
        "border-radius",
        "border-top-left-radius",
        "border-top-right-radius",
        "border-bottom-right-radius",
        "border-bottom-left-radius",
    ),
    "borderTopLeftRadius": ("border-top-left-radius", "border-radius"),
    "borderTopRightRadius": ("border-top-right-radius", "border-radius"),
    "borderBottomRightRadius": ("border-bottom-right-radius", "border-radius"),
    "borderBottomLeftRadius": ("border-bottom-left-radius", "border-radius"),
    "paddingTop": ("padding-top", "padding"),
    "paddingRight": ("padding-right", "padding"),
    "paddingBottom": ("padding-bottom", "padding"),
    "paddingLeft": ("padding-left", "padding"),
    "rowGap": ("row-gap", "gap"),
    "columnGap": ("column-gap", "gap"),
    "fontSize": ("font-size", "font"),
    "fontFamily": ("font-family", "font"),
    "fontWeight": ("font-weight", "font"),
    "lineHeight": ("line-height", "font"),
    "letterSpacing": ("letter-spacing",),
    "boxShadow": ("box-shadow",),
    "textShadow": ("text-shadow",),
    "opacity": ("opacity",),
}

# Style keys whose values are "#RRGGBB" / "#RRGGBBAA" and must match exactly,
# alpha byte included: a translucent overlay is a different design decision.
COLOR_STYLE_KEYS = frozenset(
    {
        "color",
        "backgroundColor",
        "borderColor",
        "borderTopColor",
        "borderRightColor",
        "borderBottomColor",
        "borderLeftColor",
    }
)
# Style keys that carry a CSS keyword or a family name: no tolerance applies.
EXACT_STYLE_KEYS = frozenset(
    {"fontFamily", "textAlign", "textTransform", "textDecorationLine"}
)
# Style keys measured as a unitless ratio rather than a length in pixels.
RATIO_STYLE_KEYS = frozenset({"lineHeight"})
OPACITY_STYLE_KEYS = frozenset({"opacity"})
GRADIENT_STOP_POSITION_TOLERANCE = 0.01
BOX_SHADOW_NUMERIC_KEYS = (
    "offsetX",
    "offsetY",
    "blurRadius",
    "spreadRadius",
)
# CSS `filter: drop-shadow()` has neither a spread radius nor an inset form.
DROP_SHADOW_NUMERIC_KEYS = ("offsetX", "offsetY", "blurRadius")

# An approval says "this difference between the design and the implementation is
# acceptable." Only a defect that reports a *measured* difference can carry that
# claim: a check ran, it compared two things, and a human judged the gap
# tolerable. Everything else reports that verification did not happen -- no
# evidence was collected, no contract was authored, no decision was made -- and
# there is nothing there for anyone to judge.
#
# This is an allowlist rather than a denylist on purpose. The first version of
# this guard listed the types that could not be approved, which left every
# unlisted type approvable by default and the pilot's entire blocked set --
# `flow-test-missing`, `flow-assumption-unresolved`, `component-decision-open`,
# 29 hard defects that all meant "we have not verified this yet" -- excusable in
# three config lines. Defaulting to deny costs a new defect type one line in
# `UNAPPROVABLE_DEFECT_TYPES`; defaulting to allow costs a silent pass.
APPROVABLE_DEFECT_TYPES = frozenset(
    {
        "component-not-observed",
        "copy-mismatch",
        "flow-test-failed",
        "geometry-mismatch",
        "gradient-missing",
        "hidden-element-rendered",
        "missing-copy",
        "missing-element",
        "rendered-asset-mismatch",
        "slot-empty",
        "slot-not-bound",
        "slot-shape-mismatch",
        "style-mismatch",
        "token-not-used",
        "visual-mismatch",
    }
)

SLOT_DEVIATION_TYPES = frozenset(
    {"slot-empty", "slot-not-bound", "slot-shape-mismatch"}
)

# Every other defect type, stated rather than implied. Nothing reads this set at
# runtime -- `_apply_approved_deviations` consults the allowlist alone -- but
# `tests/test_approval_guards.py` asserts the two together cover every type the
# validator can raise, so adding a defect without deciding which side it falls
# on fails the suite instead of quietly becoming approvable or not.
UNAPPROVABLE_DEFECT_TYPES = frozenset(
    {
        "accounting-unresolved-token",
        "accounting-unsupported-property",
        "accounting-violation",
        "approved-deviation-unreasoned",
        "approved-deviation-unscoped",
        "asset-file-missing",
        "asset-integrity-mismatch",
        "asset-not-vendored",
        "component-contract-missing",
        "component-decision-open",
        "component-unmapped",
        "data-contract-absent",
        "data-contract-invalid",
        "differential-feature-mismatch",
        "differential-provenance-invalid",
        "differential-self-test-evidence-rejected",
        "duplicate-node-id",
        "empty-style-contract",
        "exclusion-mask-unaligned",
        "feature-id-mismatch",
        "flow-assertion-evidence-incomplete",
        "flow-assumption-unresolved",
        "flow-contract-hash-mismatch",
        "flow-contract-missing",
        "flow-feature-id-mismatch",
        "flow-test-missing",
        "flow-transition-evidence-mismatch",
        "gate-not-evaluated",
        "incomplete-collection",
        "incomplete-compiled-spec",
        "missing-actual-geometry-property",
        "missing-actual-rect",
        "missing-actual-screenshot",
        "missing-actual-style",
        "missing-actual-style-property",
        "missing-figma-context",
        "missing-implementation-screen",
        "accounting-artifact-unusable",
        "copy-fully-excluded",
        "geometry-fully-excluded",
        "missing-reference-screenshot",
        "placeholder-style-unmeasured",
        "product-route-unverified",
        "product-slot-evidence-invalid",
        "product-verified-only",
        "reference-screenshot-absent",
        "screen-absent-from-coverage",
        "screen-absent-from-manifest",
        "screen-fully-excluded",
        "slot-differential-not-run",
        "slot-differential-observation-missing",
        "slot-differential-screen-missing",
        "slot-hidden",
        "slot-marker-duplicated",
        "slot-marker-missing",
        "slot-visibility-unknown",
        "style-fully-excluded",
        "self-test-evidence-rejected",
        "snapshot-provenance-missing",
        "snapshot-invalid",
        "snapshot-screens-invalid",
        "token-binding-unresolved",
        "token-style-key-unsupported",
        "token-unmapped",
        # A viewport that does not match is not a judged difference: the pixel
        # gate stops before comparing when it fires, so approving it approves an
        # unrun comparison.
        "viewport-mismatch",
    }
)

# The fields an entry can use to say which difference it is about. An entry that
# names none of them matches every hard defect in the run.
DEVIATION_SELECTOR_FIELDS = (
    "gate",
    "type",
    "screenNodeId",
    "elementNodeId",
    "nodeId",
    "property",
)


def _opaque_hex(value: str) -> str:
    """Normalize a fully opaque color: "#RRGGBBFF" and "#RRGGBB" are one color."""

    normalized = value.upper()
    if len(normalized) == 9 and normalized.endswith("FF"):
        return normalized[:7]
    return normalized


def _slot_shape_matches(shape: str, value: Any) -> bool:
    """Apply only the format a reviewer named; ``text`` means non-empty copy."""

    if not isinstance(value, str):
        return False
    patterns = {
        "email": EMAIL_PATTERN,
        "time": TIME_PATTERN,
        "numericMask": NUMERIC_MASK_PATTERN,
        "singleGrapheme": SINGLE_GRAPHEME_PATTERN,
    }
    if shape == "text":
        return bool(value.strip())
    pattern = patterns.get(shape)
    return bool(pattern and pattern.fullmatch(value))


# What a `::placeholder` rule can change about the prompt on screen -- the
# `::first-line` property set, narrowed to what this harness gates. When the
# prompt is what is rendered, reading any of these off the base input describes
# text the user cannot see, so they are substituted together or not at all.
# `textAlign` is absent deliberately: it belongs to the block, not the prompt.
PLACEHOLDER_STYLE_KEYS = frozenset(
    {
        "color",
        "fontFamily",
        "fontSize",
        "fontWeight",
        "letterSpacing",
        "lineHeight",
        "opacity",
        "textDecorationLine",
        "textTransform",
    }
)


def comparable_geometry_keys(element: dict[str, Any]) -> list[str]:
    """The rect properties the geometry gate will actually compare.

    Shared with `_evidence_expectations` on purpose. When "what the contract
    offers" and "what the gate counts" are computed by two similar-looking
    expressions, they drift, and the drift shows up as a gate reporting that a
    screen was emptied when the gate simply never had anything comparable
    there -- the compiler emits an all-null rect for a node with no
    `absoluteBoundingBox`, which is ordinary.
    """

    rect = element.get("rect")
    if not isinstance(rect, dict):
        return []
    return [
        key
        for key in ("x", "y", "width", "height")
        if rect.get(key) is not None
    ]


def comparable_style_keys(element: dict[str, Any]) -> list[str]:
    """The style properties the style gate will actually compare.

    A contract of `{"backgroundGradient": None}` is not empty, but it offers
    nothing to compare; the gate has always skipped it.
    """

    style = element.get("style")
    if not isinstance(style, dict):
        return []
    return [
        key
        for key, value in style.items()
        if not (key == "backgroundGradient" and value is None)
    ]


def is_canonical_screen(screen: dict[str, Any]) -> bool:
    """Report whether a compiled screen carries Figma REST canonical evidence."""

    if screen.get("canonicalEvidence"):
        return True
    return "figma-rest" in str(screen.get("evidenceSource", ""))


@dataclass(frozen=True)
class ValidateOptions:
    bundle_dir: Path
    actual_snapshot: Path
    output_dir: Path
    config_path: Path | None = None
    flow_results_path: Path | None = None
    actual_snapshot_b: Path | None = None


@dataclass(frozen=True)
class SlotObservation:
    """One normalized view of a captured slot for every runtime gate."""

    value: Any
    readable: bool
    non_empty: bool
    visibility_state: str
    visible: bool
    hidden: bool

    @property
    def valid(self) -> bool:
        return (
            self.readable
            and self.non_empty
            and self.visibility_state != "incomplete"
            and self.visible
        )


class GateValidator:
    """Validate an implementation snapshot against compiled Figma evidence."""

    def __init__(self, options: ValidateOptions):
        self.options = options
        self.bundle_dir = options.bundle_dir.resolve()
        self.output_dir = options.output_dir.resolve()
        self.config = dict(DEFAULT_CONFIG)
        if options.config_path:
            self.config.update(read_json(options.config_path))
        self.defects: list[dict[str, Any]] = []
        self.gates: dict[str, dict[str, Any]] = {}
        self.accounting: dict[str, Any] | None = None
        # gate name -> did the compiled evidence give this gate work to do.
        # Filled by `_evidence_expectations` before exclusions are applied.
        self.evidence_expectations: dict[str, bool] = {}
        # nodeId -> masked rect, per screen; filled by _apply_exclusions.
        self.excluded_rects: dict[str, list[dict[str, Any]]] = {}
        self.exclusion_ledger: list[dict[str, Any]] = []
        self.copy_exclusion_ledger: list[dict[str, Any]] = []
        self.data_contract: dict[str, Any] | None = None
        self.data_contract_path: str | None = None
        self.data_contract_error: str | None = None
        self.slot_differential_ran = False
        self.slot_differential_checked = 0
        self.screen_measurements: dict[str, dict[str, dict[str, Any]]] = {}

    def validate(self) -> dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        manifest = read_json(self.bundle_dir / "manifest.json")
        coverage = read_json(self.bundle_dir / "coverage.json")
        actual_raw = read_json(self.options.actual_snapshot)
        actual_b_raw = (
            read_json(self.options.actual_snapshot_b)
            if self.options.actual_snapshot_b
            else None
        )
        actual = self._normalize_snapshot(actual_raw, label="actual")
        actual_b = self._normalize_differential_snapshot(
            actual_b_raw, manifest
        )
        screens = self._load_screens(manifest)
        self._load_data_contract(manifest)
        self.evidence_expectations = self._evidence_expectations(
            manifest, screens
        )
        self._apply_exclusions(screens)
        actual_screens = actual["screens"]
        for item in coverage:
            node_id = item["nodeId"]
            captured = self._screen(actual_screens, node_id)
            if node_id not in actual_screens:
                implementation_state = "missing"
            elif self._is_product_screen(captured):
                implementation_state = "product-verified-only"
            else:
                implementation_state = "design-verified"
            status = item.setdefault("status", {})
            status["implementationState"] = implementation_state
            status["implemented"] = implementation_state == "design-verified"
            status["designCoverageComplete"] = (
                implementation_state == "design-verified"
            )

        self._coverage_gate(coverage, actual_screens, manifest)
        self._accounting_gate(manifest, screens)
        self._structure_gate(manifest, screens, actual, actual_screens)
        self._copy_gate(
            screens,
            actual_screens,
            actual_b.get("screens", {})
            if isinstance(actual_b, dict)
            else None,
        )
        self._finish_differential_gate()
        self._product_route_gate(actual, actual_b)
        self._geometry_gate(screens, actual_screens)
        self._style_gate(screens, actual_screens)
        self._token_gate(screens, actual, actual_screens)
        self._asset_gate(screens, actual_screens)
        comparisons = self._visual_gate(screens, actual_screens)
        self._component_gate(manifest, actual)
        self._flow_gate(manifest)
        unused_approvals = self._apply_approved_deviations()
        self._finalize_screen_measurements()
        self._update_coverage_statuses(coverage)

        hard_failures = sum(
            1 for defect in self.defects if defect["severity"] == "hard"
        )
        warnings = sum(
            1 for defect in self.defects if defect["severity"] == "warning"
        )
        design_coverage_complete = bool(
            self.gates.get("coverage", {}).get("designCoverageComplete", True)
        )
        result = {
            "schemaVersion": "1.0",
            "featureId": manifest.get("featureId"),
            "snapshotProvenance": actual.get("provenance"),
            "designCoverageComplete": design_coverage_complete,
            "passed": hard_failures == 0
            and self.gates.get("coverage", {}).get("passed", True),
            "summary": {
                "gateCount": len(self.gates),
                "passedGates": sum(
                    1
                    for gate in self.gates.values()
                    if gate["status"] == "PASS"
                ),
                "hardFailures": hard_failures,
                "warnings": warnings,
                "defects": len(self.defects),
                "excludedElements": (
                    len(self.exclusion_ledger)
                    + len(self.copy_exclusion_ledger)
                ),
                "approvedDeviations": sum(
                    1
                    for defect in self.defects
                    if defect["severity"] == "approved-deviation"
                ),
            },
            "approvedDeviations": {
                "defects": [
                    defect
                    for defect in self.defects
                    if defect["severity"] == "approved-deviation"
                ],
                "unusedEntries": unused_approvals,
            },
            "exclusions": {
                "subtreeNames": list(self.config.get("excludedSubtreeNames") or []),
                "interiorNames": list(
                    self.config.get("excludedInteriorNames") or []
                ),
                "elements": self.exclusion_ledger,
                "copy": self.copy_exclusion_ledger,
            },
            "dataContract": (
                {
                    "path": self.data_contract_path,
                    **self.data_contract,
                    "differential": {
                        "ran": self.slot_differential_ran,
                        "checked": self.slot_differential_checked,
                    },
                }
                if self.data_contract is not None
                else None
            ),
            "captureRoutes": self._capture_routes(actual, actual_b),
            "config": self.config,
            "gates": self.gates,
            "accounting": self.accounting,
            "defects": self.defects,
            "comparisons": comparisons,
            "coverage": coverage,
            "inputs": {
                "bundle": self.bundle_dir.name,
                "actualSnapshot": self.options.actual_snapshot.name,
                "actualSnapshotB": (
                    self.options.actual_snapshot_b.name
                    if self.options.actual_snapshot_b
                    else None
                ),
                "flowResults": (
                    self.options.flow_results_path.name
                    if self.options.flow_results_path
                    else None
                ),
            },
        }
        write_json(self.output_dir / "gate-results.json", result)
        write_json(self.output_dir / "defects.json", self.defects)
        result["_runtimePaths"] = {
            "bundle": str(self.bundle_dir),
            "actualRoot": str(self.options.actual_snapshot.resolve().parent),
            "output": str(self.output_dir),
        }
        return result

    def _update_coverage_statuses(
        self, coverage: list[dict[str, Any]]
    ) -> None:
        status_to_gate = {
            "structurePassed": "structure",
            "copyPassed": "copy",
            "assetPassed": "asset",
            "visualPassed": "visual",
            "flowPassed": "flow",
        }
        for item in coverage:
            status = item.setdefault("status", {})
            screen_id = item.get("nodeId")
            for status_key, gate_name in status_to_gate.items():
                measurement = self.screen_measurements.get(gate_name, {}).get(
                    screen_id, {"status": "NOT_MEASURED", "checked": 0}
                )
                status[status_key] = (
                    True
                    if measurement["status"] == "PASS"
                    else (
                        False if measurement["status"] == "FAIL" else None
                    )
                )

    def _record_screen_measurement(
        self, gate: str, screen_id: str, checked: int
    ) -> None:
        """Record only what this gate actually attempted on this screen."""

        self.screen_measurements.setdefault(gate, {})[screen_id] = {
            "checked": checked,
            "status": "NOT_MEASURED",
        }

    def _finalize_screen_measurements(self) -> None:
        """Resolve screen PASS/FAIL after approved deviations are applied."""

        for gate, screens in self.screen_measurements.items():
            for screen_id, measurement in screens.items():
                hard = any(
                    defect.get("gate") == gate
                    and defect.get("screenNodeId") == screen_id
                    and defect.get("severity") == "hard"
                    for defect in self.defects
                )
                measurement["status"] = (
                    "FAIL"
                    if hard
                    else (
                        "PASS"
                        if measurement.get("checked", 0) > 0
                        else "NOT_MEASURED"
                    )
                )
            if gate in self.gates:
                self.gates[gate]["screens"] = screens

    def _normalize_snapshot(self, value: Any, *, label: str) -> dict[str, Any]:
        """Turn malformed snapshot shapes into loud, non-crashing evidence."""

        if not isinstance(value, dict):
            self._add_defect(
                "structure",
                "snapshot-invalid",
                f"The {label} snapshot must be a JSON object.",
                expected="JSON object",
                actual=type(value).__name__,
            )
            return {"featureId": None, "provenance": None, "screens": {}}
        normalized = dict(value)
        if not isinstance(value.get("screens"), dict):
            self._add_defect(
                "structure",
                "snapshot-screens-invalid",
                f"The {label} snapshot screens must be a JSON object.",
                expected="object keyed by screen node ID",
                actual=type(value.get("screens")).__name__,
            )
            normalized["screens"] = {}
        return normalized

    def _normalize_differential_snapshot(
        self, value: Any, manifest: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Accept actual-b only when it is independent implementation evidence."""

        if value is None:
            return None
        if not isinstance(value, dict):
            self._add_defect(
                "differential",
                "snapshot-invalid",
                "The actual-b snapshot must be a JSON object.",
                expected="JSON object",
                actual=type(value).__name__,
            )
            return None
        if value.get("featureId") != manifest.get("featureId"):
            self._add_defect(
                "differential",
                "differential-feature-mismatch",
                "The actual-b snapshot belongs to a different feature.",
                expected=manifest.get("featureId"),
                actual=value.get("featureId"),
            )
            return None
        provenance = value.get("provenance")
        if provenance == "self-test":
            self._add_defect(
                "differential",
                "differential-self-test-evidence-rejected",
                "Harness self-test output is not differential implementation evidence.",
                expected="browser-capture",
                actual=provenance,
            )
            return None
        if provenance != "browser-capture":
            self._add_defect(
                "differential",
                "differential-provenance-invalid",
                "The actual-b snapshot provenance is missing or invalid.",
                expected="browser-capture",
                actual=provenance,
            )
            return None
        screens = value.get("screens")
        if not isinstance(screens, dict):
            self._add_defect(
                "differential",
                "snapshot-screens-invalid",
                "The actual-b snapshot screens must be a JSON object.",
                expected="object keyed by screen node ID",
                actual=type(screens).__name__,
            )
            return None
        return value

    def _finish_differential_gate(self) -> None:
        """Summarize actual-b evidence without mixing it into design copy."""

        defects = [
            item for item in self.defects if item.get("gate") == "differential"
        ]
        if not defects and self.slot_differential_checked == 0:
            return
        hard = sum(1 for item in defects if item["severity"] == "hard")
        warnings = sum(1 for item in defects if item["severity"] == "warning")
        self.gates["differential"] = {
            "passed": hard == 0,
            "status": (
                "FAIL"
                if hard
                else (
                    "PASS"
                    if self.slot_differential_checked > 0
                    else "NOT_EVALUATED"
                )
            ),
            "checked": self.slot_differential_checked,
            "hardFailures": hard,
            "warnings": warnings,
        }

    def _load_screens(self, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        screens = []
        for item in manifest.get("screens", []):
            path = self._resolve_bundle_path(item["compiledPath"])
            screens.append(read_json(path))
        return screens

    def _load_data_contract(self, manifest: dict[str, Any]) -> None:
        """Load the reviewed copy exceptions without inferring any new ones.

        The proposal step only identifies candidates. Validation may weaken
        exact copy after a human-authored contract is compiled, so this reads
        only the manifest path and never falls back to proposals or guesses.
        """

        contracts = manifest.get("contracts") or {}
        value = contracts.get("dataContract") if isinstance(contracts, dict) else None
        if not value:
            return
        self.data_contract_path = value
        contract = read_json(self._resolve_bundle_path(value))
        try:
            self.data_contract = validate_data_contract(contract)
        except ValueError as error:
            self.data_contract_error = str(error)
            self.data_contract = None
            return
        for item in self.data_contract.get("chrome", []):
            if not isinstance(item, dict):
                continue
            self.copy_exclusion_ledger.append(
                {
                    "screenNodeId": item.get("screenNodeId"),
                    "nodeId": item.get("nodeId"),
                    "reason": (
                        "declared as chrome in the data contract; exact copy "
                        "comparison is excluded"
                    ),
                }
            )

    def _evidence_expectations(
        self, manifest: dict[str, Any], screens: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Say what the compiled evidence gave each gate to measure.

        A gate that checked nothing is only harmless when there was nothing to
        check, and the harness could not tell those two apart: it asked a
        `require*` flag whether the operator wanted a check, never the bundle
        whether one was possible. So naming a container in
        `excludedSubtreeNames` could take every text contract on a screen with
        it, and the copy gate would report `NOT_EVALUATED` and let the run
        pass -- one config line, one gate switched off, no defect anywhere.

        These answers come from the evidence instead, and they are computed
        *before* `_apply_exclusions` runs, because exclusions are the mechanism
        that empties a gate: reading the contract afterwards would agree there
        was never anything to check, which is precisely the lie.

        The element gates answer **per screen**, not per feature. A feature-wide
        count hides the case that actually happens in a multi-screen bundle --
        one screen's contracts entirely excluded while another screen still
        reports a comparison, so the total stays above zero and the emptied
        screen never appears anywhere.
        """

        def entries(screen: dict[str, Any], key: str) -> list[dict[str, Any]]:
            value = screen.get(key)
            if not isinstance(value, list):
                return []
            return [item for item in value if isinstance(item, dict)]

        per_screen: dict[str, dict[str, bool]] = {
            "copy": {},
            "geometry": {},
            "style": {},
        }
        for screen in screens:
            screen_id = screen.get("nodeId")
            visible = [
                element
                for element in entries(screen, "elements")
                if not element.get("effectiveHidden", element.get("hidden"))
            ]
            per_screen["copy"][screen_id] = any(
                text.get("exact", True) for text in entries(screen, "texts")
            )
            per_screen["geometry"][screen_id] = any(
                comparable_geometry_keys(element) for element in visible
            )
            per_screen["style"][screen_id] = any(
                comparable_style_keys(element) for element in visible
            )
        return {
            "coverage": bool(manifest.get("screens")),
            # A bundle that names an accounting artifact has to have it, even
            # with no screens compiled -- otherwise deleting the file and the
            # screens together is quieter than deleting the file alone.
            "accounting": bool(manifest.get("propertyAccounting"))
            or any(is_canonical_screen(screen) for screen in screens),
            **per_screen,
        }

    def _screen_measured_nothing(
        self,
        gate: str,
        screen_id: Any,
        checked: int,
        *,
        screen_start: int,
        defect_type: str,
        severity: str,
        message: str,
    ) -> None:
        """Report a screen whose contracts for one gate all stopped existing.

        These are warnings, not failures, and that is a judgement rather than
        timidity. Exclusions are supposed to remove things -- nobody should
        implement a mock status-bar clock -- so a screen whose only text, or
        only styled element, was device chrome ends up with nothing this gate
        can read *and is perfectly fine*. Both compilers produce the same shape
        without any exclusion at all: the MCP path writes `style: {}` for an
        element with no classes, and the canonical path writes an all-null rect
        for a node with no `absoluteBoundingBox`. Failing on that would punish
        the exclusion for doing its documented job.

        What must not happen is silence, because then "this screen had nothing
        to check" and "this screen stopped being checked" look identical in a
        green report. So the emptiness is always named, and always against the
        screen it happened to.
        """

        if checked or not self.evidence_expectations.get(gate, {}).get(
            screen_id
        ):
            return
        if any(
            defect["severity"] == "hard"
            for defect in self.defects[screen_start:]
        ):
            # This gate is already failing on this screen -- an element whose
            # capture carries no rect or style raises `missing-actual-*` and
            # moves on without counting. The screen is loud, not silent, and
            # blaming exclusions for it would be wrong as well as redundant.
            # Only a hard defect earns that: a warning leaves the screen able
            # to pass, which is exactly the state this report exists for.
            return
        self._add_defect(
            gate,
            defect_type,
            message,
            severity=severity,
            screen_node_id=screen_id,
            expected="at least one checked item on this screen",
            actual=0,
        )

    def _apply_approved_deviations(self) -> list[dict[str, Any]]:
        """Reclassify judged design-side differences, and say which they were.

        This is the one place a difference stops counting as a failure, so it
        is deliberately narrow: an entry needs a reason, it only matches the
        fields it names, and it never removes a defect -- the entry is rewritten
        to `approved-deviation` severity and carries the reason with it. An
        approval that matched nothing comes back to the caller so a stale
        entry cannot quietly widen what passes.
        """

        entries = self.config.get("approvedDeviations") or []
        if not isinstance(entries, list) or not entries:
            return []
        matched = [0] * len(entries)
        unscoped: set[int] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or not str(entry.get("reason") or "").strip():
                self._add_defect(
                    "structure",
                    "approved-deviation-unreasoned",
                    "An approved deviation must state why the difference is "
                    "acceptable.",
                    expected="reason",
                    actual=entry if isinstance(entry, dict) else str(entry),
                )
                continue
            has_slot_node_scope = any(
                entry.get(field) is not None
                for field in ("nodeId", "elementNodeId")
            )
            targets_slot_defect = entry.get("type") in SLOT_DEVIATION_TYPES or any(
                defect.get("severity") == "hard"
                and defect.get("type") in SLOT_DEVIATION_TYPES
                and self._deviation_selectors_match(entry, defect)
                for defect in self.defects
            )
            if targets_slot_defect and not has_slot_node_scope:
                unscoped.add(index)
                self._add_defect(
                    "structure",
                    "approved-deviation-unscoped",
                    "An approved data-slot deviation must name nodeId or "
                    "elementNodeId so it cannot suppress every slot defect.",
                    expected="nodeId or elementNodeId",
                    actual=entry,
                )
                continue
            if any(
                entry.get(field) is not None
                for field in DEVIATION_SELECTOR_FIELDS
            ):
                continue
            # `_deviation_matches` only checks the fields an entry names, so an
            # entry that names none of them is true of everything: one line
            # excuses every hard defect in the run at once. A judgement that
            # broad is not a judgement about any particular difference.
            unscoped.add(index)
            self._add_defect(
                "structure",
                "approved-deviation-unscoped",
                "An approved deviation must say which difference it covers: "
                "name at least one of "
                f"{', '.join(DEVIATION_SELECTOR_FIELDS)}.",
                expected="at least one selector field",
                actual=entry,
            )
        for defect in self.defects:
            if defect["severity"] != "hard":
                continue
            if defect["type"] not in APPROVABLE_DEFECT_TYPES:
                continue
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict) or index in unscoped:
                    continue
                reason = str(entry.get("reason") or "").strip()
                if not reason or not self._deviation_matches(entry, defect):
                    continue
                defect["severity"] = "approved-deviation"
                defect["approvedReason"] = reason
                matched[index] += 1
                break
        return [
            {**entry, "matched": 0}
            for index, entry in enumerate(entries)
            if isinstance(entry, dict) and matched[index] == 0
        ]

    def _deviation_matches(
        self, entry: dict[str, Any], defect: dict[str, Any]
    ) -> bool:
        """Every field the entry names has to match; unnamed fields are free."""

        if defect.get("type") in SLOT_DEVIATION_TYPES and not any(
            entry.get(field) is not None
            for field in ("nodeId", "elementNodeId")
        ):
            return False
        return self._deviation_selectors_match(entry, defect)

    def _deviation_selectors_match(
        self, entry: dict[str, Any], defect: dict[str, Any]
    ) -> bool:
        """Match selectors without deciding whether their scope is sufficient."""

        for field in ("gate", "type", "screenNodeId", "elementNodeId"):
            wanted = entry.get(field)
            if wanted is not None and defect.get(field) != wanted:
                return False
        node = entry.get("nodeId")
        if node is not None and defect.get("elementNodeId") != node:
            return False
        prop = entry.get("property")
        if prop is not None:
            expected = defect.get("expected")
            actual = defect.get("actual")
            in_payload = (isinstance(expected, dict) and prop in expected) or (
                isinstance(actual, dict) and prop in actual
            )
            if not in_payload and f"'{prop}'" not in str(defect.get("message")):
                return False
        return True

    def _apply_exclusions(self, screens: list[dict[str, Any]]) -> None:
        """Flip a declared subtree from "must render" to "must not render".

        This is not an exemption. An excluded element keeps its entry in the
        contract and is marked non-rendering, so the structure gate's
        `hidden-element-rendered` check still fails the run if the
        implementation draws it. What changes is only the direction of the
        assertion -- which is the whole point for device chrome, where the
        requirement really is "a web build must not paint an iOS status bar".

        Its exact copy leaves the copy gate (nobody should implement the mock
        clock), and its rect is masked out of the visual diff, because the
        pixel gate would otherwise contradict the element gates. Every
        exclusion lands in `self.exclusion_ledger` and in the report.
        """

        excluded_names = {
            str(name) for name in self.config.get("excludedSubtreeNames") or []
        }
        interior_names = {
            str(name) for name in self.config.get("excludedInteriorNames") or []
        }
        if not excluded_names and not interior_names:
            return

        for screen in screens:
            screen_id = screen.get("nodeId")
            elements = screen.get("elements")
            if not isinstance(elements, list):
                continue
            by_id = {
                element["nodeId"]: element
                for element in elements
                if isinstance(element, dict) and element.get("nodeId")
            }
            excluded: dict[str, tuple[str, str]] = {}
            for element in elements:
                if not isinstance(element, dict) or not element.get("nodeId"):
                    continue
                # Walk to the root so a named container takes its subtree with
                # it. `seen` stops a malformed parent cycle from hanging the
                # run. An interior name only claims its descendants, so it is
                # checked from the parent up -- the container itself stays
                # gated.
                cursor: dict[str, Any] | None = element
                seen: set[str] = set()
                depth = 0
                while cursor is not None:
                    node_id = cursor.get("nodeId")
                    if not node_id or node_id in seen:
                        break
                    seen.add(node_id)
                    name = cursor.get("name")
                    if name in excluded_names:
                        excluded[element["nodeId"]] = (
                            "subtree",
                            f"inside excluded subtree {name!r}",
                        )
                        break
                    if depth > 0 and name in interior_names:
                        excluded[element["nodeId"]] = (
                            "interior",
                            f"interior of {name!r}, gated as a component",
                        )
                        break
                    parent_id = cursor.get("parentNodeId")
                    cursor = by_id.get(parent_id) if parent_id else None
                    depth += 1

            if not excluded:
                continue
            if len(excluded) == len(by_id):
                self._add_defect(
                    "structure",
                    "screen-fully-excluded",
                    "Every contracted element on this screen is excluded, so "
                    "nothing about it is verified.",
                    screen_node_id=screen_id,
                    expected="at least one gated element",
                    actual={"excluded": len(excluded)},
                )
            rects = []
            for node_id, (kind, reason) in excluded.items():
                element = by_id[node_id]
                if kind == "subtree":
                    # Device chrome: invert the requirement. The element stays
                    # under contract so the structure gate keeps failing the run
                    # if the implementation draws it, and its rect is masked out
                    # of the pixel diff. `hiddenStyle` mirrors what the compiler
                    # does for a designer-hidden layer so the style gate stops
                    # demanding it.
                    rect = element.get("rect")
                    if isinstance(rect, dict):
                        rects.append(rect)
                    if element.get("style"):
                        element["hiddenStyle"] = element["style"]
                    element["style"] = {}
                    element["hidden"] = True
                    element["effectiveHidden"] = True
                # An icon interior is the opposite case: those paths *are* drawn,
                # they just cannot carry per-instance markers because the glyph
                # is one shared barrel component. They leave the contract rather
                # than being asserted absent, and the pixel diff still sees them.
                element["exclusionReason"] = reason
                self.exclusion_ledger.append(
                    {
                        "screenNodeId": screen_id,
                        "nodeId": node_id,
                        "name": element.get("name"),
                        "type": element.get("type"),
                        "reason": reason,
                    }
                )
            self.excluded_rects[screen_id] = rects
            screen["elements"] = [
                element
                for element in elements
                if excluded.get(element.get("nodeId"), ("", ""))[0] != "interior"
            ]
            texts = screen.get("texts")
            if isinstance(texts, list):
                screen["texts"] = [
                    text
                    for text in texts
                    if not (
                        isinstance(text, dict)
                        and text.get("nodeId") in excluded
                    )
                ]

    def _can_mask(
        self, image: "Image.Image", screen_id: str, viewport: Any
    ) -> bool:
        """Refuse to mask when rect coordinates do not address image pixels.

        Element rects are measured in the screen's own coordinate space. A
        Figma render that bleeds past the frame comes back wider than the
        declared viewport, and then every rect is offset against the image --
        a mask placed on those coordinates would blank the wrong region and
        hide a real difference. Failing loudly beats masking blind.
        """

        if not self.excluded_rects.get(screen_id):
            return False
        width = (viewport or {}).get("width") if isinstance(viewport, dict) else None
        height = (viewport or {}).get("height") if isinstance(viewport, dict) else None
        if image.width == width and image.height == height:
            return True
        self._add_defect(
            "visual",
            "exclusion-mask-unaligned",
            "Image size differs from the contracted viewport, so excluded "
            "regions cannot be masked at element coordinates.",
            screen_node_id=screen_id,
            expected={"width": width, "height": height},
            actual={"width": image.width, "height": image.height},
        )
        return False

    def _mask_excluded(self, image: "Image.Image", screen_id: str) -> "Image.Image":
        """Paint excluded rects flat so the pixel diff ignores those regions.

        Both sides get the same treatment, so masking can only remove a
        difference that the element gates were already told not to judge.
        """

        rects = self.excluded_rects.get(screen_id)
        if not rects:
            return image
        masked = image.copy()
        painter = ImageDraw.Draw(masked)
        for rect in rects:
            try:
                left = int(rect["x"])
                top = int(rect["y"])
                right = left + int(rect["width"])
                bottom = top + int(rect["height"])
            except (KeyError, TypeError, ValueError):
                continue
            if right <= left or bottom <= top:
                continue
            painter.rectangle([left, top, right - 1, bottom - 1], fill=(0, 0, 0, 255))
        return masked

    def _resolve_bundle_path(self, value: str) -> Path:
        return resolve_within(self.bundle_dir, value, label="bundle artifact")

    def _screen(self, actual_screens: dict[str, Any], node_id: str) -> dict[str, Any]:
        value = actual_screens.get(node_id, {})
        return value if isinstance(value, dict) else {}

    def _is_product_screen(self, screen: Any) -> bool:
        """Product captures expose bindings, not Figma implementation evidence."""

        return isinstance(screen, dict) and screen.get("routeKind") == "product"

    def _elements(self, screen: dict[str, Any]) -> dict[str, dict[str, Any]]:
        elements = screen.get("elements", {})
        if isinstance(elements, list):
            return {
                item["nodeId"]: item
                for item in elements
                if isinstance(item, dict) and item.get("nodeId")
            }
        return elements if isinstance(elements, dict) else {}

    def _add_defect(
        self,
        gate: str,
        defect_type: str,
        message: str,
        *,
        severity: str = "hard",
        screen_node_id: str | None = None,
        element_node_id: str | None = None,
        expected: Any = None,
        actual: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        defect = {
            "id": f"D{len(self.defects) + 1:05d}",
            "gate": gate,
            "type": defect_type,
            "severity": severity,
            "screenNodeId": screen_node_id,
            "elementNodeId": element_node_id,
            "expected": expected,
            "actual": actual,
            "message": message,
        }
        if extra:
            defect.update(extra)
        self.defects.append(defect)

    def _finish_gate(
        self,
        gate: str,
        start: int,
        checked: int,
        *,
        must_evaluate: bool = False,
        not_evaluated_message: str | None = None,
    ) -> None:
        """Record a gate's outcome, and refuse to call an empty gate a pass.

        A gate that checked nothing is not a gate that found nothing wrong, so
        its status stays `NOT_EVALUATED` rather than `PASS`. That distinction is
        only advisory on its own -- `passed` follows the hard-failure count, so
        an unevaluated gate still lets the run through. `must_evaluate` says the
        caller has established that this gate had a reason to measure something
        (a policy explicitly set to `hard`, a contract that exists), and in that
        case measuring nothing is itself the defect: the configuration promised
        a check that never happened, which is exactly the silence this harness
        exists to break.
        """

        if checked == 0 and must_evaluate:
            self._add_defect(
                gate,
                "gate-not-evaluated",
                not_evaluated_message
                or (
                    f"The {gate} gate is configured to run but checked nothing, "
                    "so its result is not evidence of anything."
                ),
                expected="at least one checked item",
                actual=0,
            )
        defects = self.defects[start:]
        hard = sum(1 for item in defects if item["severity"] == "hard")
        warnings = sum(
            1 for item in defects if item["severity"] == "warning"
        )
        self.gates[gate] = {
            "passed": hard == 0,
            "status": (
                "FAIL"
                if hard
                else ("NOT_EVALUATED" if checked == 0 else "PASS")
            ),
            "checked": checked,
            "hardFailures": hard,
            "warnings": warnings,
        }

    def _coverage_gate(
        self,
        coverage: list[dict[str, Any]],
        actual_screens: dict[str, Any],
        manifest: dict[str, Any],
    ) -> None:
        gate = "coverage"
        start = len(self.defects)
        # Every other check here reads `coverage.json`, so a screen the
        # manifest compiled but coverage never lists is invisible to all of
        # them -- and to the gate's own count, which would still be positive
        # from the screens that are listed.
        listed = {item.get("nodeId") for item in coverage}
        declared_ids = {
            item.get("nodeId") for item in manifest.get("screens", [])
        }
        for declared in manifest.get("screens", []):
            if declared.get("nodeId") in listed:
                continue
            self._add_defect(
                gate,
                "screen-absent-from-coverage",
                f"Screen {declared.get('name')!r} was compiled but is missing "
                "from coverage.json, so nothing tracks whether it was "
                "implemented.",
                screen_node_id=declared.get("nodeId"),
                expected="a coverage entry",
                actual="missing",
            )
        # And the other direction. Emptying the manifest's screen list is the
        # cheaper tamper: no screen is loaded, so every element gate has
        # nothing to do and reports NOT_EVALUATED, while coverage still counts
        # the entries coverage.json kept and calls the run clean.
        for item in coverage:
            if item.get("nodeId") in declared_ids:
                continue
            self._add_defect(
                gate,
                "screen-absent-from-manifest",
                f"Screen {item.get('name')!r} is tracked in coverage.json but "
                "the manifest compiles no contract for it, so no gate reads "
                "it.",
                screen_node_id=item.get("nodeId"),
                expected="a compiled screen contract",
                actual="missing",
            )
        for item in coverage:
            node_id = item["nodeId"]
            if (
                self.config["requireAllContexts"]
                and not item.get("status", {}).get("contextFetched")
            ):
                self._add_defect(
                    gate,
                    "missing-figma-context",
                    f"Figma context was not fetched for {item.get('name')!r}.",
                    screen_node_id=node_id,
                    expected="contextFetched=true",
                    actual=False,
                )
            elif (
                self.config["requireAllCompiledSpecs"]
                and not item.get("status", {}).get("specCompiled")
            ):
                self._add_defect(
                    gate,
                    "incomplete-compiled-spec",
                    f"Compiler could not account for all evidence in {item.get('name')!r}.",
                    screen_node_id=node_id,
                    expected="specCompiled=true",
                    actual=False,
                )
            if node_id not in actual_screens:
                self._add_defect(
                    gate,
                    "missing-implementation-screen",
                    f"Implementation snapshot has no screen {item.get('name')!r}.",
                    screen_node_id=node_id,
                    expected="screen present",
                    actual="missing",
                )
            elif self._is_product_screen(actual_screens[node_id]):
                self._add_defect(
                    gate,
                    "product-verified-only",
                    f"Screen {item.get('name')!r} has only product-route data "
                    "slot evidence; no design gate measured it.",
                    severity=(
                        "hard"
                        if self.config["requireProductRouteCheck"]
                        else "warning"
                    ),
                    screen_node_id=node_id,
                    expected="fixture capture for design gates",
                    actual="product-route capture only",
                )
        self._finish_gate(
            gate,
            start,
            len(coverage),
            must_evaluate=self.evidence_expectations.get("coverage", False),
            not_evaluated_message=(
                "The bundle declares screens but coverage.json lists none, so "
                "no screen was checked for context, compilation or "
                "implementation."
            ),
        )
        design_coverage_complete = all(
            item.get("status", {}).get("designCoverageComplete") is True
            for item in coverage
        )
        coverage_result = self.gates[gate]
        coverage_result["designCoverageComplete"] = design_coverage_complete
        if not design_coverage_complete and coverage_result["hardFailures"] == 0:
            coverage_result["status"] = "INCOMPLETE"
            coverage_result["passed"] = False

    def _accounting_gate(
        self, manifest: dict[str, Any], screens: list[dict[str, Any]]
    ) -> None:
        """Fail when the compiler could not account for canonical evidence."""

        gate = "accounting"
        start = len(self.defects)
        accounting = self._load_accounting(manifest)
        if accounting is None:
            # A legacy MCP-only bundle carries no accounting artifact and has
            # nothing here to evaluate. A canonical bundle makes a different
            # claim -- that its evidence came from the REST collector -- and
            # the artifact proving the compiler dropped none of that evidence
            # is simply absent. Deleting one file used to erase this gate in
            # silence; the comment that coverage would report it was wrong,
            # because coverage only reads what the bundle wrote about itself.
            self._finish_gate(
                gate,
                start,
                0,
                must_evaluate=self.evidence_expectations.get(
                    "accounting", False
                ),
                not_evaluated_message=(
                    "This bundle carries canonical Figma evidence but no "
                    "property-accounting.json, so nothing verifies that the "
                    "compiler accounted for that evidence. Recompile the "
                    "bundle from its canonical input."
                ),
            )
            return
        self.accounting = accounting
        self._collection_defects(gate, accounting.get("restCollection"))
        for violation in accounting.get("violations", []):
            self._add_defect(
                gate,
                "accounting-violation",
                f"Canonical property {violation.get('key')!r} was not accounted "
                f"for: {violation.get('reason')}.",
                screen_node_id=violation.get("screenNodeId"),
                element_node_id=violation.get("nodeId"),
                expected="every canonical property compiled or preserved",
                actual=violation,
            )
        for item in accounting.get("unsupported", []):
            self._add_defect(
                gate,
                "accounting-unsupported-property",
                f"Canonical property {item.get('key')!r} is not compiled into a "
                f"contract: {item.get('reason')}.",
                severity="warning",
                screen_node_id=item.get("screenNodeId"),
                element_node_id=item.get("nodeId"),
                expected="compiled style contract",
                actual=item,
            )
        for item in accounting.get("unresolvedTokenBindings", []):
            self._add_defect(
                gate,
                "accounting-unresolved-token",
                f"Design token {item.get('variableId')!r} bound to "
                f"{item.get('styleKey')!r} has no resolved name.",
                severity="warning",
                screen_node_id=item.get("screenNodeId"),
                element_node_id=item.get("nodeId"),
                expected="resolved variable name",
                actual=item,
            )
        counts = accounting.get("counts", {})
        counts = counts if isinstance(counts, dict) else {}
        # No `or 1` fallback. It was there so an artifact without a node count
        # would not read as an unevaluated gate, but it did that by inventing a
        # measurement: an empty or truncated `property-accounting.json` claimed
        # one checked item and passed. An artifact that cannot say how much it
        # accounted for has not accounted for anything.
        raw_nodes = counts.get("nodes")
        checked = int(raw_nodes) if isinstance(raw_nodes, int) else 0
        screen_count = counts.get("screens")
        # `nodes: 0` is honest for a feature with no screens -- the compiler
        # writes exactly that -- so the defect is a count that cannot be true,
        # not a count that is zero. A negative one is the interesting case: it
        # is falsy-safe, so a bare `if not checked` waved it through.
        if not isinstance(raw_nodes, int) or checked < 0 or (
            checked == 0 and screen_count not in (0, None) and screens
        ):
            self._add_defect(
                gate,
                "accounting-artifact-unusable",
                "The accounting artifact does not report a usable node count, "
                "so it proves nothing about what the compiler accounted for.",
                expected="counts.nodes >= 0, and > 0 when screens were compiled",
                actual=raw_nodes,
            )
            checked = 0
        for key in ("violations", "unsupported", "unresolvedTokenBindings"):
            if not isinstance(accounting.get(key), list):
                # Each of these drives a defect loop above. A missing key is
                # read as an empty list, so truncating the artifact silently
                # removes the findings it was supposed to carry.
                self._add_defect(
                    gate,
                    "accounting-artifact-unusable",
                    f"The accounting artifact declares no {key!r} list, so "
                    "nothing states what the compiler could not account for.",
                    expected=f"{key}: []",
                    actual=accounting.get(key),
                )
        if not isinstance(accounting.get("restCollection"), dict):
            # `_collection_defects` returns silently on a non-dict, so a
            # missing key used to disable the completeness proof outright.
            self._add_defect(
                gate,
                "accounting-artifact-unusable",
                "The accounting artifact declares no restCollection, so "
                "nothing states whether the canonical evidence is complete.",
                expected="restCollection",
                actual=accounting.get("restCollection"),
            )
        self._finish_gate(gate, start, checked)

    def _collection_defects(
        self, gate: str, collection: Any
    ) -> None:
        """Refuse to gate a feature whose canonical evidence is incomplete.

        Without this, a node the collector never fetched simply is not in the
        bundle, and every gate happily passes on the screens that are.
        """

        if not isinstance(collection, dict) or collection.get("complete") is True:
            return
        missing = collection.get("missingNodeIds") or []
        failures = collection.get("failures") or []
        for node_id in missing:
            self._add_defect(
                gate,
                "incomplete-collection",
                f"Figma node {node_id} was expected but never collected, so "
                "nothing about that screen is gated.",
                screen_node_id=str(node_id),
                expected="canonical node evidence",
                actual="missing",
            )
        for failure in failures:
            node_id = (
                failure.get("id") if isinstance(failure, dict) else None
            )
            reason = (
                failure.get("reason") if isinstance(failure, dict) else failure
            )
            self._add_defect(
                gate,
                "incomplete-collection",
                f"Collecting Figma node {node_id} failed: {reason}.",
                screen_node_id=None if node_id is None else str(node_id),
                expected="canonical node evidence",
                actual=failure,
            )
        if not missing and not failures:
            self._add_defect(
                gate,
                "incomplete-collection",
                "The collection manifest does not declare the canonical "
                "evidence complete.",
                expected="complete=true",
                actual=collection.get("complete"),
            )

    def _load_accounting(
        self, manifest: dict[str, Any]
    ) -> dict[str, Any] | None:
        declared = manifest.get("propertyAccounting")
        value = declared.get("path") if isinstance(declared, dict) else None
        path = (
            self._resolve_bundle_path(value)
            if value
            else self.bundle_dir / "property-accounting.json"
        )
        if not path.exists():
            return None
        return read_json(path)

    def _structure_gate(
        self,
        manifest: dict[str, Any],
        screens: list[dict[str, Any]],
        actual: dict[str, Any],
        actual_screens: dict[str, Any],
    ) -> None:
        gate = "structure"
        start = len(self.defects)
        checked = 0
        checked += 1
        if actual.get("featureId") != manifest.get("featureId"):
            self._add_defect(
                gate,
                "feature-id-mismatch",
                "Implementation snapshot belongs to a different feature.",
                expected=manifest.get("featureId"),
                actual=actual.get("featureId"),
            )
        provenance = actual.get("provenance")
        if provenance == "self-test" and not self.config["allowSelfTestSnapshot"]:
            self._add_defect(
                gate,
                "self-test-evidence-rejected",
                "Reference-derived self-test data is not implementation evidence.",
                expected="browser-capture",
                actual=provenance,
            )
        elif provenance not in {"browser-capture", "self-test"}:
            self._add_defect(
                gate,
                "snapshot-provenance-missing",
                "Implementation snapshot provenance is missing or invalid.",
                expected="browser-capture",
                actual=provenance,
            )
        for expected_screen in screens:
            screen_id = expected_screen["nodeId"]
            actual_screen = self._screen(actual_screens, screen_id)
            if self._is_product_screen(actual_screen):
                self._record_screen_measurement(gate, screen_id, 0)
                continue
            on_screen = 0
            actual_elements = self._elements(actual_screen)
            for duplicate in actual_screen.get("duplicateNodeIds", []):
                self._add_defect(
                    gate,
                    "duplicate-node-id",
                    "A Figma node ID occurs more than once in one screen snapshot.",
                    screen_node_id=screen_id,
                    element_node_id=duplicate,
                    expected="unique data-node-id",
                    actual="duplicate",
                )
            for expected in expected_screen.get("elements", []):
                node_id = expected["nodeId"]
                if expected.get("effectiveHidden", expected.get("hidden")):
                    checked += 1
                    on_screen += 1
                    if self._visibly_rendered(actual_elements.get(node_id)):
                        self._add_defect(
                            gate,
                            "hidden-element-rendered",
                            f"Element {expected.get('name')!r} is hidden in "
                            "Figma but occupies space in the implementation.",
                            screen_node_id=screen_id,
                            element_node_id=node_id,
                            expected="not rendered",
                            actual=actual_elements[node_id].get("rect"),
                        )
                    continue
                checked += 1
                on_screen += 1
                if node_id not in actual_elements:
                    self._add_defect(
                        gate,
                        "missing-element",
                        f"Required element {expected.get('name')!r} is absent.",
                        screen_node_id=screen_id,
                        element_node_id=node_id,
                        expected={
                            "name": expected.get("name"),
                            "type": expected.get("type"),
                        },
                        actual="missing",
                        severity=(
                            "hard"
                            if self.config["requireAllElements"]
                            else "warning"
                        ),
                    )
            self._record_screen_measurement(gate, screen_id, on_screen)
        self._finish_gate(gate, start, checked)

    def _visibly_rendered(self, actual_element: Any) -> bool:
        """Report whether a captured element is actually visible to a user.

        `display:none`, `visibility:hidden` and `opacity:0` are all legitimate
        ways to implement a node the design hides, so none of them counts as
        rendering it — only occupying space while remaining visible does.
        """

        if not isinstance(actual_element, dict):
            return False
        if actual_element.get("rendered") is False:
            return False
        rect = actual_element.get("rect")
        if not isinstance(rect, dict):
            return False
        try:
            area = float(rect.get("width") or 0) * float(rect.get("height") or 0)
        except (TypeError, ValueError):
            return False
        if area <= 0:
            return False
        style = actual_element.get("style")
        style = style if isinstance(style, dict) else {}
        if style.get("display") == "none" or style.get("visibility") in {
            "hidden",
            "collapse",
        }:
            return False
        opacity = style.get("opacity")
        if isinstance(opacity, (int, float, str)) and not isinstance(
            opacity, bool
        ):
            try:
                return float(opacity) > 0
            except (TypeError, ValueError):
                return False
        return True

    def _slot_observation(self, actual_element: Any) -> SlotObservation:
        """Normalize readability, visible value, and optional visibility proof."""

        if not isinstance(actual_element, dict):
            return SlotObservation(None, False, False, "absent", True, False)
        visibility_keys = {
            key for key in ("rendered", "rect", "style") if key in actual_element
        }
        if not visibility_keys:
            # Visibility-less fixture snapshots predate this evidence channel.
            visibility_state = "absent"
            visible = True
            hidden = False
        else:
            rect = actual_element.get("rect")
            style = actual_element.get("style")
            rendered = actual_element.get("rendered")
            hidden = rendered is False
            width = rect.get("width") if isinstance(rect, dict) else None
            height = rect.get("height") if isinstance(rect, dict) else None
            width_valid = (
                isinstance(width, (int, float))
                and not isinstance(width, bool)
                and math.isfinite(width)
                and width >= 0
            )
            height_valid = (
                isinstance(height, (int, float))
                and not isinstance(height, bool)
                and math.isfinite(height)
                and height >= 0
            )
            if width_valid and height_valid:
                hidden = hidden or width == 0 or height == 0
            display = style.get("display") if isinstance(style, dict) else None
            visibility = (
                style.get("visibility") if isinstance(style, dict) else None
            )
            display_valid = isinstance(display, str) and bool(display.strip())
            visibility_valid = isinstance(visibility, str) and bool(
                visibility.strip()
            )
            opacity = style.get("opacity") if isinstance(style, dict) else None
            opacity_valid = False
            opacity_value = None
            if not isinstance(opacity, bool) and isinstance(
                opacity, (int, float, str)
            ):
                try:
                    opacity_value = float(opacity)
                    opacity_valid = (
                        math.isfinite(opacity_value)
                        and 0 <= opacity_value <= 1
                    )
                except (TypeError, ValueError):
                    pass
            if isinstance(style, dict):
                hidden = hidden or display == "none"
                hidden = hidden or visibility in {
                    "hidden",
                    "collapse",
                }
                hidden = hidden or (opacity_valid and opacity_value == 0)
            complete = (
                isinstance(rendered, bool)
                and width_valid
                and height_valid
                and isinstance(style, dict)
                and display_valid
                and visibility_valid
                and opacity_valid
            )
            visibility_state = "complete" if complete else "incomplete"
            visible = (
                self._visibly_rendered(actual_element)
                if complete
                else not hidden
            )
            if complete:
                hidden = not visible
        copy = actual_element.get("copy")
        has_value_channel = "text" in actual_element or (
            isinstance(copy, dict)
            and any(
                key in copy
                for key in (
                    "textContent",
                    "value",
                    "placeholder",
                    "selectedText",
                )
            )
        )
        value = self._slot_value(actual_element)
        readable = has_value_channel and value is not None
        non_empty = readable and bool(str(value).strip())
        return SlotObservation(
            value,
            readable,
            non_empty,
            visibility_state,
            visible,
            hidden,
        )

    def _copy_gate(
        self,
        screens: list[dict[str, Any]],
        actual_screens: dict[str, Any],
        actual_screens_b: dict[str, Any] | None = None,
    ) -> None:
        gate = "copy"
        start = len(self.defects)
        checked = 0
        if self.data_contract_error is not None:
            self._add_defect(
                gate,
                "data-contract-invalid",
                "The compiled data contract is malformed and cannot weaken "
                "exact-copy checks.",
                expected="a valid reviewed data contract",
                actual=self.data_contract_error,
            )
        if self.data_contract is None and self.data_contract_error is None:
            self._add_defect(
                gate,
                "data-contract-absent",
                "No data contract was compiled, so every text node was judged "
                "as fixed interface copy.",
                severity=(
                    "hard"
                    if self.config["requireDataContract"]
                    else "warning"
                ),
                expected="data-contract.json",
                actual=None,
            )
        slots = {
            (item.get("screenNodeId"), item.get("nodeId")): item
            for item in (self.data_contract or {}).get("slots", [])
            if isinstance(item, dict)
        }
        chrome = {
            (item.get("screenNodeId"), item.get("nodeId"))
            for item in (self.data_contract or {}).get("chrome", [])
            if isinstance(item, dict)
        }
        fixture_screens_b = (
            {
                screen_id: screen
                for screen_id, screen in actual_screens_b.items()
                if not self._is_product_screen(screen)
            }
            if isinstance(actual_screens_b, dict)
            else {}
        )
        if slots and not fixture_screens_b:
            self._add_defect(
                "differential",
                "slot-differential-not-run",
                "No second implementation capture was supplied, so data slots "
                "were not checked for change under different injected data.",
                severity="warning",
                expected="validate --actual-b <snapshot>",
                actual=None,
            )
        for screen_id in sorted({identity[0] for identity in slots}):
            if screen_id in fixture_screens_b:
                continue
            self._add_defect(
                "differential",
                "slot-differential-screen-missing",
                "No fixture-route actual-b screen exists for this "
                "screen's declared data slots.",
                severity="warning",
                screen_node_id=screen_id,
                expected="fixture actual-b screen",
                actual="missing",
            )
        for expected_screen in screens:
            screen_id = expected_screen["nodeId"]
            actual_screen = self._screen(actual_screens, screen_id)
            if self._is_product_screen(actual_screen):
                self._record_screen_measurement(gate, screen_id, 0)
                continue
            on_screen = 0
            screen_start = len(self.defects)
            actual_elements = self._elements(actual_screen)
            actual_elements_b = (
                self._elements(self._screen(fixture_screens_b, screen_id))
                if screen_id in fixture_screens_b
                else {}
            )
            for expected in expected_screen.get("texts", []):
                if not expected.get("exact", True):
                    continue
                node_id = expected["nodeId"]
                identity = (screen_id, node_id)
                if identity in slots or identity in chrome:
                    continue
                checked += 1
                on_screen += 1
                actual_element = actual_elements.get(node_id, {})
                property_name = expected.get("property", "textContent")
                actual_copy = actual_element.get("copy", {})
                actual_value = (
                    actual_copy.get(property_name)
                    if isinstance(actual_copy, dict)
                    else None
                )
                if property_name == "textContent" and actual_value is None:
                    actual_value = actual_element.get("text")
                if property_name == "textContent" and not actual_value:
                    fallback = self._rendered_copy(actual_copy)
                    if fallback is not None:
                        actual_value = fallback
                if actual_value != expected["value"]:
                    self._add_defect(
                        gate,
                        "copy-mismatch" if actual_value is not None else "missing-copy",
                        f"Exact copy differs at Figma node {node_id}.",
                        screen_node_id=screen_id,
                        element_node_id=node_id,
                        expected=expected["value"],
                        actual=actual_value,
                        severity=(
                            "hard"
                            if self.config["requireAllExactCopy"]
                            else "warning"
                        ),
                    )
            for (slot_screen_id, node_id), slot in slots.items():
                if slot_screen_id != screen_id:
                    continue
                checked += 1
                on_screen += 1
                actual_element = actual_elements.get(node_id, {})
                observation_a = self._slot_observation(actual_element)
                actual_value = observation_a.value
                common = {
                    "screen_node_id": screen_id,
                    "element_node_id": node_id,
                    "extra": {
                        "binding": slot.get("binding"),
                        "shape": slot.get("shape"),
                    },
                }
                visibility_unknown = "rendered" not in actual_element or (
                    observation_a.visibility_state == "incomplete"
                    and not observation_a.hidden
                )
                if visibility_unknown:
                    self._add_defect(
                        gate,
                        "slot-visibility-unknown",
                        "This capture did not record usable visibility for "
                        f"the data slot at Figma node {node_id}. Recapture "
                        "with the latest adapter.",
                        severity=(
                            "hard"
                            if self.config["requireProductRouteCheck"]
                            else "warning"
                        ),
                        expected="visibility recorded by the latest adapter",
                        actual={
                            "rendered": actual_element.get("rendered"),
                            "rect": actual_element.get("rect"),
                            "style": actual_element.get("style"),
                        },
                        **common,
                    )
                elif observation_a.hidden:
                    self._add_defect(
                        gate,
                        "slot-hidden",
                        f"Data slot at Figma node {node_id} is not visibly rendered.",
                        expected="visible rendered slot",
                        actual={
                            "rendered": actual_element.get("rendered"),
                            "rect": actual_element.get("rect"),
                            "style": actual_element.get("style"),
                        },
                        **common,
                    )
                if actual_value is None or not str(actual_value).strip():
                    self._add_defect(
                        gate,
                        "slot-empty",
                        f"Data slot at Figma node {node_id} renders no value.",
                        expected={"binding": slot.get("binding"), "nonEmpty": True},
                        actual=actual_value,
                        **common,
                    )
                elif actual_value == slot.get("designExemplar"):
                    self._add_defect(
                        gate,
                        "slot-not-bound",
                        f"Data slot at Figma node {node_id} still renders the design exemplar.",
                        expected={
                            "binding": slot.get("binding"),
                            "differentFromDesignExemplar": slot.get(
                                "designExemplar"
                            ),
                        },
                        actual=actual_value,
                        **common,
                    )
                elif slot.get("shape") and not _slot_shape_matches(
                    slot["shape"], actual_value
                ):
                    self._add_defect(
                        gate,
                        "slot-shape-mismatch",
                        f"Data slot at Figma node {node_id} does not match its declared shape.",
                        expected={
                            "binding": slot.get("binding"),
                            "shape": slot.get("shape"),
                        },
                        actual=actual_value,
                        **common,
                    )
                if screen_id in fixture_screens_b:
                    observation_b = self._slot_observation(
                        actual_elements_b.get(node_id, {})
                    )
                    if observation_b.hidden:
                        self._add_defect(
                            "differential",
                            "slot-hidden",
                            "The actual-b slot is not visibly rendered, so it "
                            "cannot prove that the binding changes.",
                            screen_node_id=screen_id,
                            element_node_id=node_id,
                            expected="visible actual-b slot",
                            actual={
                                "rendered": actual_elements_b.get(node_id, {}).get(
                                    "rendered"
                                ),
                                "rect": actual_elements_b.get(node_id, {}).get(
                                    "rect"
                                ),
                                "style": actual_elements_b.get(node_id, {}).get(
                                    "style"
                                ),
                            },
                            extra={
                                "binding": slot.get("binding"),
                                "snapshot": "actual-b",
                            },
                        )
                        continue
                    if not observation_a.valid or not observation_b.valid:
                        self._add_defect(
                            "differential",
                            "slot-differential-observation-missing",
                            "A declared slot lacks a visible, readable, non-empty "
                            "observation in one of the two fixture captures.",
                            severity="warning",
                            screen_node_id=screen_id,
                            element_node_id=node_id,
                            expected={
                                "actualA": "visible, readable, non-empty",
                                "actualB": "visible, readable, non-empty",
                            },
                            actual={
                                "actualA": {
                                    "visibility": observation_a.visibility_state,
                                    "visible": observation_a.visible,
                                    "readable": observation_a.readable,
                                    "nonEmpty": observation_a.non_empty,
                                },
                                "actualB": {
                                    "visibility": observation_b.visibility_state,
                                    "visible": observation_b.visible,
                                    "readable": observation_b.readable,
                                    "nonEmpty": observation_b.non_empty,
                                },
                            },
                            extra={"binding": slot.get("binding")},
                        )
                        continue
                    self.slot_differential_checked += 1
                    actual_value = observation_a.value
                    actual_value_b = observation_b.value
                    already_unbound = any(
                        defect["type"] == "slot-not-bound"
                        and defect.get("screenNodeId") == screen_id
                        and defect.get("elementNodeId") == node_id
                        for defect in self.defects[screen_start:]
                    )
                    if (
                        actual_value is not None
                        and str(actual_value).strip()
                        and actual_value == actual_value_b
                        and not already_unbound
                    ):
                        self._add_defect(
                            gate,
                            "slot-not-bound",
                            f"Data slot at Figma node {node_id} did not change "
                            "between two implementation captures.",
                            screen_node_id=screen_id,
                            element_node_id=node_id,
                            expected={
                                "binding": slot.get("binding"),
                                "actualA": "different from actualB",
                            },
                            actual={
                                "actualA": actual_value,
                                "actualB": actual_value_b,
                            },
                            extra={
                                "binding": slot.get("binding"),
                                "shape": slot.get("shape"),
                                "differential": True,
                            },
                        )
            self._screen_measured_nothing(
                gate,
                screen_id,
                on_screen,
                screen_start=screen_start,
                defect_type="copy-fully-excluded",
                severity="warning",
                message=(
                    "No exact-copy contract on this screen survived to be "
                    "checked, so no text was compared here."
                ),
            )
            self._record_screen_measurement(gate, screen_id, on_screen)
        self.slot_differential_ran = self.slot_differential_checked > 0
        self._finish_gate(gate, start, checked)

    def _product_route_gate(
        self,
        actual: dict[str, Any],
        actual_b: dict[str, Any] | None,
    ) -> None:
        """Check real product values by semantic ``data-slot`` binding only.

        Product pages deliberately carry no Figma node markers. Their capture
        is therefore a second, narrower kind of evidence: it can prove that a
        named data position contains a plausible real value, but it cannot
        prove copy, geometry, style, or structure. Both supplied snapshots are
        searched because the usual useful pairing is a fixture capture plus a
        product-route capture of the same Figma screen.
        """

        declared_slots = [
            item
            for item in (self.data_contract or {}).get("slots", [])
            if isinstance(item, dict)
            and item.get("screenNodeId")
            and item.get("binding")
        ]
        if not declared_slots:
            return

        product_screens: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for snapshot_name, snapshot in (("actual", actual), ("actual-b", actual_b)):
            if not isinstance(snapshot, dict):
                continue
            screens = snapshot.get("screens")
            if not isinstance(screens, dict):
                continue
            for screen_id, screen in screens.items():
                if self._is_product_screen(screen):
                    product_screens.setdefault(str(screen_id), []).append(
                        (snapshot_name, screen)
                    )

        slots_by_screen: dict[str, list[dict[str, Any]]] = {}
        for slot in declared_slots:
            slots_by_screen.setdefault(str(slot["screenNodeId"]), []).append(slot)

        gate = "product-route"
        start = len(self.defects)
        checked = 0
        for screen_id in sorted(slots_by_screen):
            captures = product_screens.get(screen_id, [])
            if not captures:
                checked += 1
                self._add_defect(
                    gate,
                    "product-route-unverified",
                    "This screen declares data slots, but no shipping product "
                    "route capture was supplied.",
                    severity=(
                        "hard"
                        if self.config["requireProductRouteCheck"]
                        else "warning"
                    ),
                    screen_node_id=screen_id,
                    expected='at least one capture with routeKind: "product"',
                    actual="no product capture",
                )
                continue

            for snapshot_name, screen in captures:
                captured_slots = screen.get("slots")
                if not isinstance(captured_slots, dict):
                    self._add_defect(
                        gate,
                        "product-slot-evidence-invalid",
                        "Product route slot evidence must be an object keyed "
                        "by data-slot binding.",
                        screen_node_id=screen_id,
                        expected="object keyed by binding",
                        actual=type(captured_slots).__name__,
                        extra={"snapshot": snapshot_name},
                    )
                    captured_slots = {}
                duplicate_slots = screen.get("duplicateSlots")
                duplicate_slots = (
                    duplicate_slots if isinstance(duplicate_slots, list) else []
                )
                for slot in slots_by_screen[screen_id]:
                    checked += 1
                    binding = str(slot["binding"])
                    captured = captured_slots.get(binding)
                    duplicate_count = duplicate_slots.count(binding)
                    if isinstance(captured, list):
                        duplicate_count += max(0, len(captured) - 1)
                        captured = captured[0] if captured else None
                    common = {
                        "screen_node_id": screen_id,
                        "element_node_id": slot.get("nodeId"),
                        "extra": {
                            "binding": binding,
                            "shape": slot.get("shape"),
                            "snapshot": snapshot_name,
                            "route": screen.get("route"),
                        },
                    }
                    if duplicate_count:
                        self._add_defect(
                            gate,
                            "slot-marker-duplicated",
                            "Product route contains more than one data-slot "
                            f"marker for {binding!r}.",
                            severity="warning",
                            expected={"binding": binding, "count": 1},
                            actual={
                                "binding": binding,
                                "count": duplicate_count + 1,
                            },
                            **common,
                        )
                    if not isinstance(captured, dict):
                        self._add_defect(
                            gate,
                            "slot-marker-missing",
                            f"Product route has no data-slot marker for {binding!r}.",
                            severity=(
                                "hard"
                                if self.config["requireProductRouteCheck"]
                                else "warning"
                            ),
                            expected={"data-slot": binding},
                            actual="missing",
                            **common,
                        )
                        continue

                    observation = self._slot_observation(captured)
                    visibility_evidence_invalid = not (
                        observation.visibility_state == "complete"
                        and isinstance(captured.get("rendered"), bool)
                    )
                    if "rendered" not in captured:
                        self._add_defect(
                            gate,
                            "slot-visibility-unknown",
                            "This capture did not record usable visibility for "
                            f"the product data slot {binding!r}. Recapture "
                            "with the latest adapter.",
                            severity=(
                                "hard"
                                if self.config["requireProductRouteCheck"]
                                else "warning"
                            ),
                            expected="visibility recorded by the latest adapter",
                            actual={
                                "rendered": captured.get("rendered"),
                                "rect": captured.get("rect"),
                                "style": captured.get("style"),
                            },
                            **common,
                        )
                    elif visibility_evidence_invalid:
                        self._add_defect(
                            gate,
                            "product-slot-evidence-invalid",
                            f"Product data slot {binding!r} lacks complete "
                            "rendered, rect, and computed-style evidence.",
                            expected={
                                "rendered": "boolean",
                                "rect": ["width", "height"],
                                "style": ["display", "visibility", "opacity"],
                            },
                            actual={
                                "rendered": captured.get("rendered"),
                                "rect": captured.get("rect"),
                                "style": captured.get("style"),
                            },
                            **common,
                        )
                    actual_value = observation.value
                    if (
                        not visibility_evidence_invalid
                        and not observation.visible
                    ):
                        self._add_defect(
                            gate,
                            "slot-hidden",
                            f"Product data slot {binding!r} is not visibly rendered.",
                            expected="visible rendered slot",
                            actual={
                                "rendered": captured.get("rendered"),
                                "rect": captured.get("rect"),
                                "style": captured.get("style"),
                            },
                            **common,
                        )
                    if actual_value is None or not str(actual_value).strip():
                        self._add_defect(
                            gate,
                            "slot-empty",
                            f"Product data slot {binding!r} renders no value.",
                            expected={"binding": binding, "nonEmpty": True},
                            actual=actual_value,
                            **common,
                        )
                    elif actual_value == slot.get("designExemplar"):
                        self._add_defect(
                            gate,
                            "slot-not-bound",
                            f"Product data slot {binding!r} still renders the "
                            "design exemplar.",
                            expected={
                                "binding": binding,
                                "differentFromDesignExemplar": slot.get(
                                    "designExemplar"
                                ),
                            },
                            actual=actual_value,
                            **common,
                        )
                    elif slot.get("shape") and not _slot_shape_matches(
                        slot["shape"], actual_value
                    ):
                        self._add_defect(
                            gate,
                            "slot-shape-mismatch",
                            f"Product data slot {binding!r} does not match its "
                            "declared shape.",
                            expected={
                                "binding": binding,
                                "shape": slot.get("shape"),
                            },
                            actual=actual_value,
                            **common,
                        )
        self._finish_gate(gate, start, checked)

    def _capture_routes(
        self,
        actual: dict[str, Any],
        actual_b: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Keep route provenance in the verdict so the HTML can show it."""

        routes: list[dict[str, Any]] = []
        for snapshot_name, snapshot in (("actual", actual), ("actual-b", actual_b)):
            if not isinstance(snapshot, dict):
                continue
            screens = snapshot.get("screens")
            if not isinstance(screens, dict):
                continue
            for screen_id in sorted(screens):
                screen = screens[screen_id]
                if not isinstance(screen, dict):
                    continue
                route_kind = screen.get("routeKind") or "fixture"
                routes.append(
                    {
                        "snapshot": snapshot_name,
                        "screenNodeId": screen_id,
                        "name": screen.get("name"),
                        "route": screen.get("route"),
                        "routeKind": (
                            route_kind
                            if route_kind in {"fixture", "product"}
                            else "unknown"
                        ),
                        "verificationScope": (
                            "data slots only"
                            if route_kind == "product"
                            else "design gates"
                        ),
                    }
                )
        return routes

    def _slot_value(self, actual_element: Any) -> Any:
        """Read the one value a user sees, including real form controls."""

        if not isinstance(actual_element, dict):
            return None
        actual_copy = actual_element.get("copy")
        actual_value = (
            actual_copy.get("textContent")
            if isinstance(actual_copy, dict)
            else None
        )
        if actual_value is None:
            actual_value = actual_element.get("text")
        if not actual_value:
            fallback = self._rendered_copy(actual_copy)
            if fallback is not None:
                actual_value = fallback
        return actual_value

    def _geometry_gate(
        self,
        screens: list[dict[str, Any]],
        actual_screens: dict[str, Any],
    ) -> None:
        gate = "geometry"
        start = len(self.defects)
        checked = 0
        tolerance = float(self.config["geometryTolerancePx"])
        for expected_screen in screens:
            screen_id = expected_screen["nodeId"]
            actual_screen = self._screen(actual_screens, screen_id)
            if self._is_product_screen(actual_screen):
                self._record_screen_measurement(gate, screen_id, 0)
                continue
            on_screen = 0
            screen_start = len(self.defects)
            actual_elements = self._elements(actual_screen)
            for expected in expected_screen.get("elements", []):
                if expected.get("effectiveHidden", expected.get("hidden")):
                    continue
                node_id = expected["nodeId"]
                actual_rect = actual_elements.get(node_id, {}).get("rect")
                comparable = comparable_geometry_keys(expected)
                if not isinstance(actual_rect, dict):
                    if comparable:
                        self._add_defect(
                            gate,
                            "missing-actual-rect",
                            "Geometry evidence is missing for a required element.",
                            screen_node_id=screen_id,
                            element_node_id=node_id,
                            expected=expected.get("rect"),
                            actual=actual_rect,
                        )
                    continue
                expected_rect = expected["rect"]
                for key in comparable:
                    expected_value = expected_rect[key]
                    actual_value = actual_rect.get(key)
                    checked += 1
                    on_screen += 1
                    if actual_value is None:
                        self._add_defect(
                            gate,
                            "missing-actual-geometry-property",
                            f"Geometry property {key!r} is missing.",
                            screen_node_id=screen_id,
                            element_node_id=node_id,
                            expected={key: expected_value},
                            actual={key: None},
                        )
                        continue
                    if not self._numbers_close(
                        expected_value, actual_value, tolerance
                    ):
                        self._add_defect(
                            gate,
                            "geometry-mismatch",
                            f"{key} differs beyond {tolerance}px.",
                            screen_node_id=screen_id,
                            element_node_id=node_id,
                            expected={key: expected_value},
                            actual={key: actual_value},
                        )
            self._screen_measured_nothing(
                gate,
                screen_id,
                on_screen,
                screen_start=screen_start,
                defect_type="geometry-fully-excluded",
                severity="warning",
                message=(
                    "No element on this screen offers geometry this gate can "
                    "compare, so no position or size was verified here."
                ),
            )
            self._record_screen_measurement(gate, screen_id, on_screen)
        self._finish_gate(gate, start, checked)

    # Elements whose on-screen text is not their `textContent`. Everything else
    # reads what it contains, so a stray `value`/`placeholder` attribute on it
    # says nothing about the screen and must not satisfy a copy contract.
    TEXT_BEARING_CONTROLS = frozenset({"input", "textarea"})

    def _rendered_copy(self, actual_copy: Any) -> str | None:
        """Return what a form control puts on screen, or None if it is not one.

        An `<input>` shows `value` once filled and `placeholder` while empty;
        its `textContent` is empty in both cases, so reading only `textContent`
        fails an implementation for being editable. A `<select>` is different
        again -- `.value` is the option's code, and the label is what a reader
        sees -- and an ordinary element carrying a `value` attribute renders
        none of it. Restricting the fallback by tag is what keeps this a fix
        for form controls rather than a way for any element to claim any text.
        """

        if not isinstance(actual_copy, dict):
            return None
        tag = actual_copy.get("tag")
        if tag == "select":
            selected = actual_copy.get("selectedText")
            return selected if selected else None
        if tag not in self.TEXT_BEARING_CONTROLS:
            return None
        for key in ("value", "placeholder"):
            candidate = actual_copy.get(key)
            if candidate:
                return candidate
        return None

    def _visible_style(self, actual_element: Any) -> Any:
        """Return the style of what is actually on screen.

        An empty field shows its `::placeholder`; `color` is the colour a typed
        value *would* take, which nobody can see yet. The design draws that
        prompt as an ordinary grey text node, so comparing `color` reports a
        mismatch against an implementation that is right -- the same way
        reading only `textContent` does in the copy gate.

        Substituted only while the placeholder is what is rendered. Once the
        field holds a value, `color` is the visible colour again and is used
        unchanged, so this never hides a real difference on a filled field.

        Every property the prompt can override is substituted, not just the
        colour. `::placeholder` takes the whole `::first-line` set, so a prompt
        rendered at `font-size: 1px; opacity: 0` used to satisfy the gate on
        the strength of its colour alone: every other property still came off
        the base input, which reports the size and opacity of text nobody can
        see.
        """

        if not isinstance(actual_element, dict):
            return None
        style = actual_element.get("style")
        if not isinstance(style, dict):
            return style
        copy = actual_element.get("copy")
        if not isinstance(copy, dict):
            return style
        if copy.get("tag") not in self.TEXT_BEARING_CONTROLS:
            return style
        if copy.get("value") or not copy.get("placeholder"):
            return style
        prompt = style.get("placeholderStyle")
        if isinstance(prompt, dict) and prompt:
            visible = {
                key: value for key, value in prompt.items() if value is not None
            }
            return {**style, **visible}
        placeholder_color = style.get("placeholderColor")
        if not placeholder_color:
            # The prompt is what is rendered and this capture never measured
            # it. Comparing the base values instead would pass or fail on text
            # nobody can see -- so drop those keys and let the gate report the
            # evidence as missing.
            return {
                key: value
                for key, value in style.items()
                if key not in PLACEHOLDER_STYLE_KEYS
            }
        # A capture taken before the full prompt style was recorded. Its colour
        # is real evidence and is used. The rest stays as captured rather than
        # being dropped: `::placeholder` inherits the input's font properties
        # unless a rule overrides them, so the base values usually *are* the
        # prompt's, and refusing to compare them would turn a usually-correct
        # check into a guaranteed failure on every capture taken before this
        # existed. The uncertainty is real though, so `_style_gate` reports it
        # per element instead of leaving it implicit.
        return {**style, "color": placeholder_color}

    def _placeholder_evidence_is_partial(self, actual_element: Any) -> bool:
        """Whether only the prompt's colour was ever measured for this field."""

        if not isinstance(actual_element, dict):
            return False
        style = actual_element.get("style")
        copy = actual_element.get("copy")
        if not isinstance(style, dict) or not isinstance(copy, dict):
            return False
        if copy.get("tag") not in self.TEXT_BEARING_CONTROLS:
            return False
        if copy.get("value") or not copy.get("placeholder"):
            return False
        if isinstance(style.get("placeholderStyle"), dict) and style[
            "placeholderStyle"
        ]:
            return False
        return bool(style.get("placeholderColor"))

    def _style_gate(
        self,
        screens: list[dict[str, Any]],
        actual_screens: dict[str, Any],
    ) -> None:
        gate = "style"
        start = len(self.defects)
        checked = 0
        for expected_screen in screens:
            screen_id = expected_screen["nodeId"]
            actual_screen = self._screen(actual_screens, screen_id)
            if self._is_product_screen(actual_screen):
                self._record_screen_measurement(gate, screen_id, 0)
                continue
            on_screen = 0
            screen_start = len(self.defects)
            canonical = is_canonical_screen(expected_screen)
            actual_elements = self._elements(actual_screen)
            for expected in expected_screen.get("elements", []):
                node_id = expected["nodeId"]
                expected_style = expected.get("style", {})
                actual_element = actual_elements.get(node_id, {})
                actual_style = self._visible_style(actual_element)
                hidden = bool(
                    expected.get("effectiveHidden", expected.get("hidden"))
                )
                if not expected_style:
                    if canonical and not hidden:
                        # A canonical element with no compiled style is
                        # legitimate for pure containers, but it is also what a
                        # silent extraction failure looks like. Never skip it
                        # without saying so.
                        self._add_defect(
                            gate,
                            "empty-style-contract",
                            f"Canonical element {expected.get('name')!r} "
                            "compiled no style properties, so nothing about "
                            "its appearance is gated.",
                            severity="warning",
                            screen_node_id=screen_id,
                            element_node_id=node_id,
                            expected="at least one compiled style property",
                            actual={},
                        )
                    continue
                if self._placeholder_evidence_is_partial(actual_element):
                    self._add_defect(
                        gate,
                        "placeholder-style-unmeasured",
                        f"Only the prompt colour was measured for "
                        f"{expected.get('name')!r}; its size, weight and "
                        "spacing are being compared against the input's own "
                        "style. Re-capture to gate the prompt itself.",
                        severity="warning",
                        screen_node_id=screen_id,
                        element_node_id=node_id,
                        expected="style.placeholderStyle",
                        actual="style.placeholderColor",
                    )
                if not isinstance(actual_style, dict):
                    self._add_defect(
                        gate,
                        "missing-actual-style",
                        "Style evidence is missing for a styled element.",
                        screen_node_id=screen_id,
                        element_node_id=node_id,
                        expected=expected_style,
                        actual=actual_style,
                    )
                    continue
                for key in comparable_style_keys(expected):
                    expected_value = expected_style[key]
                    checked += 1
                    on_screen += 1
                    token = self._token_payload(expected, key)
                    if key not in actual_style:
                        self._add_defect(
                            gate,
                            "missing-actual-style-property",
                            f"Style property {key!r} is missing.",
                            screen_node_id=screen_id,
                            element_node_id=node_id,
                            expected={key: expected_value},
                            actual={key: None},
                            extra=token,
                        )
                        continue
                    defect = self._style_property_defect(
                        key, expected_value, actual_style
                    )
                    if defect is not None:
                        self._add_defect(
                            gate,
                            defect["type"],
                            defect["message"],
                            screen_node_id=screen_id,
                            element_node_id=node_id,
                            expected={key: expected_value},
                            actual=defect["actual"],
                            extra=token,
                        )
            self._screen_measured_nothing(
                gate,
                screen_id,
                on_screen,
                screen_start=screen_start,
                defect_type="style-fully-excluded",
                severity="warning",
                message=(
                    "No element on this screen offers a style this gate can "
                    "compare, so no appearance was verified here."
                ),
            )
            self._record_screen_measurement(gate, screen_id, on_screen)
        self._finish_gate(gate, start, checked)

    def _token_gate(
        self,
        screens: list[dict[str, Any]],
        actual: dict[str, Any],
        actual_screens: dict[str, Any],
    ) -> None:
        """Check that bound design tokens are referenced, not re-typed by hand.

        The style gate compares resolved values, so an element that hardcodes
        the token's current hex passes it while quietly leaving the design
        system. This gate reads the capture's declared `tokenRefs` instead, and
        it is a presence-of-reference check: no specificity is resolved, so a
        token named by any rule that matches the element counts as used.
        """

        gate = "token"
        start = len(self.defects)
        policy = str(self.config.get("tokenUsagePolicy") or "warn").lower()
        if policy == "off":
            self._finish_gate(gate, start, 0)
            return
        severity = "hard" if policy == "hard" else "warning"
        token_map = self.config.get("tokenMap")
        token_map = token_map if isinstance(token_map, dict) else {}
        snapshot_properties = actual.get("customProperties")
        checked = 0
        for expected_screen in screens:
            screen_id = expected_screen["nodeId"]
            actual_screen = self._screen(actual_screens, screen_id)
            if self._is_product_screen(actual_screen):
                self._record_screen_measurement(gate, screen_id, 0)
                continue
            on_screen = 0
            actual_elements = self._elements(actual_screen)
            declared = actual_screen.get("customProperties")
            if not isinstance(declared, dict):
                declared = snapshot_properties
            for expected in expected_screen.get("elements", []):
                if expected.get("effectiveHidden", expected.get("hidden")):
                    continue
                resolved = expected.get("resolvedTokens")
                resolved = resolved if isinstance(resolved, dict) else {}
                bindings = expected.get("tokenBindings")
                if isinstance(bindings, dict):
                    # The compiler writes `tokenBindings` from the design's raw
                    # variable ids and `resolvedTokens` only for the ids it
                    # could name. A binding present here but absent there means
                    # the bundle knows this property came from a design token
                    # and cannot say which one -- so token usage is
                    # unverifiable for it. Skipping those quietly is how a
                    # whole file's worth of bindings can fail to resolve and
                    # still leave the gate looking clean.
                    for style_key, variable_id in bindings.items():
                        if style_key in resolved:
                            continue
                        checked += 1
                        self._add_defect(
                            gate,
                            "token-binding-unresolved",
                            f"Design token bound to {style_key!r} could not be "
                            "resolved to a token name, so its use is "
                            "unverifiable.",
                            severity=severity,
                            screen_node_id=screen_id,
                            element_node_id=expected["nodeId"],
                            expected="a resolved token name for the bound Figma variable",
                            actual=variable_id,
                        )
                if not resolved:
                    continue
                actual_element = actual_elements.get(expected["nodeId"])
                token_refs = (
                    actual_element.get("tokenRefs")
                    if isinstance(actual_element, dict)
                    else None
                )
                if not isinstance(token_refs, dict):
                    # A capture taken before this channel existed carries no
                    # declared values at all. Claiming the tokens are unused
                    # would be inventing evidence, so those entries stay
                    # unevaluated and out of the checked count.
                    continue
                for style_key, token_name in resolved.items():
                    checked += 1
                    self._token_defect(
                        gate,
                        severity,
                        token_map,
                        declared,
                        screen_id=screen_id,
                        node_id=expected["nodeId"],
                        style_key=style_key,
                        token_name=token_name,
                        token_refs=token_refs,
                    )
        self._finish_gate(
            gate,
            start,
            checked,
            must_evaluate=policy == "hard",
            not_evaluated_message=(
                "tokenUsagePolicy is 'hard' but no design token binding was "
                "checked, so nothing enforced token usage."
            ),
        )

    def _token_defect(
        self,
        gate: str,
        severity: str,
        token_map: dict[str, Any],
        declared: Any,
        *,
        screen_id: str,
        node_id: str,
        style_key: str,
        token_name: Any,
        token_refs: dict[str, Any],
    ) -> None:
        """Report one bound token that the element failed to reference."""

        if token_name not in token_map:
            self._add_defect(
                gate,
                "token-unmapped",
                f"Figma token {token_name!r} has no CSS custom property in "
                "tokenMap, so its use cannot be verified.",
                severity="warning",
                screen_node_id=screen_id,
                element_node_id=node_id,
                expected={"styleKey": style_key, "figmaToken": token_name},
                actual={"tokenMap": None},
                extra={"expectedToken": token_name},
            )
            return
        mapped = token_map[token_name]
        mapped_names = [mapped] if isinstance(mapped, str) else list(mapped or [])
        css_properties = TOKEN_STYLE_KEY_CSS_PROPERTIES.get(style_key)
        if css_properties is None:
            self._add_defect(
                gate,
                "token-style-key-unsupported",
                f"Style key {style_key!r} has no CSS property mapping, so a "
                f"token bound to it cannot be verified.",
                severity="warning",
                screen_node_id=screen_id,
                element_node_id=node_id,
                expected={"styleKey": style_key, "figmaToken": token_name},
                actual={"cssProperties": None},
                extra={"expectedToken": token_name},
            )
            return
        observed = {
            css_property: token_refs[css_property]
            for css_property in css_properties
            if css_property in token_refs
        }
        referenced = {
            name
            for names in observed.values()
            if isinstance(names, list)
            for name in names
        }
        if referenced.intersection(mapped_names):
            return
        undefined = (
            [name for name in mapped_names if name not in declared]
            if isinstance(declared, dict)
            else []
        )
        message = (
            f"Style key {style_key!r} must come from {token_name!r} "
            f"({', '.join(mapped_names) or 'no mapped property'}), but the "
            "element's declared CSS references it nowhere."
        )
        extra: dict[str, Any] = {"expectedToken": token_name}
        if undefined:
            message += (
                f" {', '.join(undefined)} is not defined by any captured "
                ":root rule either, so the token system itself is missing."
            )
            extra["undefinedCustomProperties"] = undefined
        self._add_defect(
            gate,
            "token-not-used",
            message,
            severity=severity,
            screen_node_id=screen_id,
            element_node_id=node_id,
            expected={
                "styleKey": style_key,
                "figmaToken": token_name,
                "customProperties": mapped_names,
                "cssProperties": list(css_properties),
            },
            actual={"tokenRefs": observed},
            extra=extra,
        )

    def _token_payload(
        self, element: dict[str, Any], key: str
    ) -> dict[str, Any]:
        """Name the design token behind a style key so defects are actionable."""

        token = None
        resolved = element.get("resolvedTokens")
        if isinstance(resolved, dict):
            token = resolved.get(key)
        if token is None:
            bindings = element.get("tokenBindings")
            if isinstance(bindings, dict):
                token = bindings.get(key)
        return {"expectedToken": token} if token else {}

    def _style_property_defect(
        self, key: str, expected_value: Any, actual_style: dict[str, Any]
    ) -> dict[str, Any] | None:
        if key == "boxShadow":
            return self._box_shadow_defect(expected_value, actual_style)
        if key == "textShadow":
            return self._text_shadow_defect(expected_value, actual_style)
        if key == "backgroundGradient":
            return self._gradient_defect(expected_value, actual_style)
        actual_value = actual_style.get(key)
        if self._style_values_equal(key, expected_value, actual_value):
            return None
        return {
            "type": "style-mismatch",
            "message": f"Style property {key!r} differs.",
            "actual": {key: actual_value},
        }

    def _box_shadow_defect(
        self, expected_value: Any, actual_style: dict[str, Any]
    ) -> dict[str, Any] | None:
        expected_shadows = self._shadow_list(expected_value)
        actual_shadows = self._shadow_list(actual_style.get("boxShadow"))
        drop_shadows = self._shadow_list(actual_style.get("filterDropShadows"))
        payload = {
            "boxShadow": actual_shadows,
            "filterDropShadows": drop_shadows,
        }
        if expected_shadows and not actual_shadows and drop_shadows:
            return self._drop_shadow_defect(
                expected_shadows, drop_shadows, payload
            )
        if len(expected_shadows) != len(actual_shadows):
            return {
                "type": "style-mismatch",
                "message": (
                    f"boxShadow renders {len(actual_shadows)} shadow(s) where "
                    f"{len(expected_shadows)} are required."
                ),
                "actual": payload,
            }
        if not self._shadows_match(
            expected_shadows,
            actual_shadows,
            BOX_SHADOW_NUMERIC_KEYS,
            compare_inset=True,
        ):
            return {
                "type": "style-mismatch",
                "message": (
                    "No boxShadow in the implementation matches every required "
                    "shadow."
                ),
                "actual": payload,
            }
        return None

    def _text_shadow_defect(
        self, expected_value: Any, actual_style: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Compare CSS `text-shadow`, which has no spread and no inset form."""

        expected_shadows = self._shadow_list(expected_value)
        actual_shadows = self._shadow_list(actual_style.get("textShadow"))
        payload = {"textShadow": actual_shadows}
        if len(expected_shadows) != len(actual_shadows):
            return {
                "type": "style-mismatch",
                "message": (
                    f"textShadow renders {len(actual_shadows)} shadow(s) where "
                    f"{len(expected_shadows)} are required."
                ),
                "actual": payload,
            }
        if not self._shadows_match(
            expected_shadows,
            actual_shadows,
            DROP_SHADOW_NUMERIC_KEYS,
            compare_inset=False,
        ):
            return {
                "type": "style-mismatch",
                "message": (
                    "No text-shadow in the implementation matches every "
                    "required shadow."
                ),
                "actual": payload,
            }
        return None

    def _drop_shadow_defect(
        self,
        expected_shadows: list[dict[str, Any]],
        drop_shadows: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Accept `filter: drop-shadow()` for the shadows it can express."""

        tolerance = self._style_tolerance
        unrepresentable = [
            index
            for index, shadow in enumerate(expected_shadows)
            if shadow.get("inset")
            or not self._numbers_close(
                shadow.get("spreadRadius", 0), 0, tolerance
            )
        ]
        if unrepresentable:
            return {
                "type": "style-mismatch",
                "message": (
                    f"boxShadow{unrepresentable} carries spread or inset, "
                    "which filter: drop-shadow() cannot express."
                ),
                "actual": payload,
            }
        if len(expected_shadows) != len(drop_shadows):
            return {
                "type": "style-mismatch",
                "message": (
                    f"filter: drop-shadow() renders {len(drop_shadows)} "
                    f"shadow(s) where {len(expected_shadows)} are required."
                ),
                "actual": payload,
            }
        if not self._shadows_match(
            expected_shadows,
            drop_shadows,
            DROP_SHADOW_NUMERIC_KEYS,
            compare_inset=False,
        ):
            return {
                "type": "style-mismatch",
                "message": (
                    "No rendered filter: drop-shadow() matches every required "
                    "boxShadow."
                ),
                "actual": payload,
            }
        return None

    def _gradient_defect(
        self, expected_value: Any, actual_style: dict[str, Any]
    ) -> dict[str, Any] | None:
        actual_value = actual_style.get("backgroundGradient")
        if not isinstance(expected_value, dict):
            if expected_value == actual_value:
                return None
            return {
                "type": "style-mismatch",
                "message": "Style property 'backgroundGradient' differs.",
                "actual": {"backgroundGradient": actual_value},
            }
        if not isinstance(actual_value, dict):
            return {
                "type": "gradient-missing",
                "message": (
                    "A linear gradient background is required but none was "
                    "rendered."
                ),
                "actual": {
                    "backgroundGradient": actual_value,
                    "backgroundImageRaw": actual_style.get(
                        "backgroundImageRaw"
                    ),
                },
            }
        payload = {"backgroundGradient": actual_value}
        if expected_value.get("type") != actual_value.get("type"):
            return {
                "type": "style-mismatch",
                "message": "backgroundGradient type differs.",
                "actual": payload,
            }
        angle_tolerance = float(self.config["gradientAngleToleranceDeg"])
        if not self._numbers_close(
            expected_value.get("angleDeg"),
            actual_value.get("angleDeg"),
            angle_tolerance,
        ):
            return {
                "type": "style-mismatch",
                "message": (
                    f"backgroundGradient angle differs beyond "
                    f"{angle_tolerance} degrees."
                ),
                "actual": payload,
            }
        expected_stops = expected_value.get("stops") or []
        actual_stops = actual_value.get("stops") or []
        if len(expected_stops) != len(actual_stops):
            return {
                "type": "style-mismatch",
                "message": (
                    f"backgroundGradient has {len(actual_stops)} stop(s) where "
                    f"{len(expected_stops)} are required."
                ),
                "actual": payload,
            }
        for index, (expected_stop, actual_stop) in enumerate(
            zip(expected_stops, actual_stops)
        ):
            if not self._colors_equal(
                expected_stop.get("color"), actual_stop.get("color")
            ) or not self._numbers_close(
                expected_stop.get("position"),
                actual_stop.get("position"),
                GRADIENT_STOP_POSITION_TOLERANCE,
            ):
                return {
                    "type": "style-mismatch",
                    "message": f"backgroundGradient stop {index} differs.",
                    "actual": payload,
                }
        return None

    def _shadow_list(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def _shadows_match(
        self,
        expected: list[dict[str, Any]],
        actual: list[dict[str, Any]],
        numeric_keys: tuple[str, ...],
        *,
        compare_inset: bool,
    ) -> bool:
        """Pair every required shadow with a distinct rendered one.

        Figma effect order and CSS shadow order are independent, so an index
        zip reports a defect for a list that is merely written differently.
        Only the absence of a complete pairing is a real mismatch.
        """

        candidates = [
            [
                index
                for index, rendered in enumerate(actual)
                if self._shadows_equal(
                    required,
                    rendered,
                    numeric_keys,
                    compare_inset=compare_inset,
                )
            ]
            for required in expected
        ]
        paired_to: dict[int, int] = {}

        def pair(required_index: int, visited: set[int]) -> bool:
            for rendered_index in candidates[required_index]:
                if rendered_index in visited:
                    continue
                visited.add(rendered_index)
                holder = paired_to.get(rendered_index)
                if holder is None or pair(holder, visited):
                    paired_to[rendered_index] = required_index
                    return True
            return False

        return all(pair(index, set()) for index in range(len(expected)))

    def _shadows_equal(
        self,
        expected: dict[str, Any],
        actual: dict[str, Any],
        numeric_keys: tuple[str, ...],
        *,
        compare_inset: bool,
    ) -> bool:
        tolerance = self._style_tolerance
        for key in numeric_keys:
            if not self._numbers_close(
                expected.get(key, 0), actual.get(key, 0), tolerance
            ):
                return False
        if not self._colors_equal(expected.get("color"), actual.get("color")):
            return False
        if compare_inset and bool(expected.get("inset")) != bool(
            actual.get("inset")
        ):
            return False
        return True

    def _asset_gate(
        self,
        screens: list[dict[str, Any]],
        actual_screens: dict[str, Any],
    ) -> None:
        gate = "asset"
        start = len(self.defects)
        checked = 0
        for expected_screen in screens:
            screen_id = expected_screen["nodeId"]
            actual_screen = self._screen(actual_screens, screen_id)
            if self._is_product_screen(actual_screen):
                self._record_screen_measurement(gate, screen_id, 0)
                continue
            on_screen = 0
            actual_assets = actual_screen.get("assets", {})
            if isinstance(actual_assets, list):
                actual_assets = {
                    item.get("variable"): item
                    for item in actual_assets
                    if isinstance(item, dict)
                }
            if not isinstance(actual_assets, dict):
                actual_assets = {}
            for asset in expected_screen.get("assets", []):
                if not asset.get("exactRequired", True):
                    continue
                checked += 1
                on_screen += 1
                variable = asset["variable"]
                local_path = asset.get("localPath")
                expected_sha = asset.get("sha256")
                if self.config["requireLocalAssets"] and not local_path:
                    self._add_defect(
                        gate,
                        "asset-not-vendored",
                        f"Figma asset {variable!r} has no immutable local file.",
                        screen_node_id=screen_id,
                        expected="localPath + sha256",
                        actual={"source": "figma-mcp", "urlRedacted": True},
                    )
                    continue
                if local_path:
                    path = self._resolve_bundle_path(local_path)
                    if not path.exists():
                        self._add_defect(
                            gate,
                            "asset-file-missing",
                            f"Vendored asset {variable!r} does not exist.",
                            screen_node_id=screen_id,
                            expected=str(path),
                            actual="missing",
                        )
                        continue
                    calculated = sha256_file(path)
                    if expected_sha and calculated != expected_sha:
                        self._add_defect(
                            gate,
                            "asset-integrity-mismatch",
                            f"Vendored asset {variable!r} changed.",
                            screen_node_id=screen_id,
                            expected=expected_sha,
                            actual=calculated,
                        )
                actual_sha = actual_assets.get(variable, {}).get("sha256")
                if expected_sha and actual_sha != expected_sha:
                    self._add_defect(
                        gate,
                        "rendered-asset-mismatch",
                        f"Rendered asset {variable!r} does not match the contract.",
                        screen_node_id=screen_id,
                        expected=expected_sha,
                        actual=actual_sha,
                    )
            self._record_screen_measurement(gate, screen_id, on_screen)
        # No `must_evaluate` here either, and not because this gate is safe.
        # Nothing removes assets from a compiled screen -- exclusions rewrite
        # elements and texts only -- and the loop above skips on exactly the
        # `exactRequired` test an expectation would use, so a derived rule here
        # could never fire: dead code shaped like a defence. What actually
        # keeps this gate at zero is that the REST pipeline compiles no asset
        # contract at all, and that gap is reported where it is real, as
        # `unverifiedImageFills` in the accounting artifact and its report
        # panel. Closing it means teaching the collector to fetch fill images.
        self._finish_gate(gate, start, checked)

    def _visual_gate(
        self,
        screens: list[dict[str, Any]],
        actual_screens: dict[str, Any],
    ) -> list[dict[str, Any]]:
        gate = "visual"
        start = len(self.defects)
        checked = 0
        comparisons = []
        policy = str(self.config.get("visualPolicy") or "hard").lower()
        if policy == "off":
            for screen in screens:
                self._record_screen_measurement(
                    gate, screen.get("nodeId"), 0
                )
            self._finish_gate(gate, start, 0)
            return []
        severity = "hard" if policy == "hard" else "warning"
        diff_dir = self.output_dir / "diffs"
        diff_dir.mkdir(parents=True, exist_ok=True)
        for expected_screen in screens:
            screen_id = expected_screen["nodeId"]
            actual_screen = self._screen(actual_screens, screen_id)
            if self._is_product_screen(actual_screen):
                self._record_screen_measurement(gate, screen_id, 0)
                continue
            reference_value = expected_screen.get("referenceScreenshot")
            actual_value = actual_screen.get("screenshot")
            comparison = {
                "screenNodeId": screen_id,
                "name": expected_screen.get("name"),
                "reference": reference_value,
                "actual": actual_value,
                "diff": None,
                "mismatchRatio": None,
                "passed": False,
            }
            comparisons.append(comparison)
            if not reference_value:
                if self.config["requireReferenceScreenshots"]:
                    self._add_defect(
                        gate,
                        "missing-reference-screenshot",
                        "Compiled screen has no reference screenshot.",
                        screen_node_id=screen_id,
                        expected="reference screenshot",
                        actual=None,
                    )
                else:
                    # The opt-out is legitimate, but it is per run while the
                    # gap is per screen: with it set, a bundle where only some
                    # screens carry references still reports a PASS, and
                    # nothing says which screens the pixel gate never saw.
                    # That is the silence -- not the empty gate, which stays
                    # visible as NOT_EVALUATED.
                    self._add_defect(
                        gate,
                        "reference-screenshot-absent",
                        "No reference screenshot was compiled for this "
                        "screen, so no pixel of it is compared.",
                        severity="warning",
                        screen_node_id=screen_id,
                        expected="reference screenshot",
                        actual=None,
                    )
                self._record_screen_measurement(gate, screen_id, 0)
                continue
            checked += 1
            self._record_screen_measurement(gate, screen_id, 1)
            reference_path = self._resolve_bundle_path(reference_value)
            actual_path = self._resolve_actual_path(actual_value)
            if not actual_path or not actual_path.exists():
                self._add_defect(
                    gate,
                    "missing-actual-screenshot",
                    "No implementation screenshot was provided.",
                    severity=severity,
                    screen_node_id=screen_id,
                    expected=str(reference_path),
                    actual=actual_value,
                )
                continue
            with Image.open(reference_path) as reference_image, Image.open(
                actual_path
            ) as actual_image:
                reference_rgba = reference_image.convert("RGBA")
                actual_rgba = actual_image.convert("RGBA")
                if reference_rgba.size != actual_rgba.size:
                    self._add_defect(
                        gate,
                        "viewport-mismatch",
                        "Reference and implementation viewport sizes differ.",
                        severity=severity,
                        screen_node_id=screen_id,
                        expected={
                            "width": reference_rgba.width,
                            "height": reference_rgba.height,
                        },
                        actual={
                            "width": actual_rgba.width,
                            "height": actual_rgba.height,
                        },
                    )
                    continue
                viewport = expected_screen.get("viewport")
                if self._can_mask(reference_rgba, screen_id, viewport):
                    reference_rgba = self._mask_excluded(reference_rgba, screen_id)
                    actual_rgba = self._mask_excluded(actual_rgba, screen_id)
                ratio, diff = self._pixel_diff(reference_rgba, actual_rgba)
                diff_path = diff_dir / f"{safe_slug(screen_id)}.png"
                diff.save(diff_path)
                comparison["diff"] = str(diff_path.relative_to(self.output_dir))
                comparison["mismatchRatio"] = ratio
                comparison["passed"] = (
                    ratio <= float(self.config["visualMaxMismatchRatio"])
                )
                if not comparison["passed"]:
                    self._add_defect(
                        gate,
                        "visual-mismatch",
                        "Pixel mismatch ratio exceeds the configured limit.",
                        severity=severity,
                        screen_node_id=screen_id,
                        expected={
                            "maxMismatchRatio": self.config[
                                "visualMaxMismatchRatio"
                            ]
                        },
                        actual={"mismatchRatio": ratio},
                    )
        # No `must_evaluate` here, deliberately. `visualPolicy` grades how a
        # pixel difference is treated; the setting that demands a comparison
        # happen at all is `requireReferenceScreenshots`, and that one already
        # fails per screen. Deriving "this gate had work to do" from the
        # severity dial would reject the documented image-less workflow --
        # `collect` without `--include-images` for a copy audit -- over a
        # contradiction that is not one. What that pairing really hides is
        # partial coverage, which is reported per screen above.
        self._finish_gate(gate, start, checked)
        return comparisons

    def _component_gate(
        self, manifest: dict[str, Any], actual: dict[str, Any]
    ) -> None:
        gate = "component"
        start = len(self.defects)
        contract_value = manifest.get("contracts", {}).get("componentMap")
        if not contract_value:
            if self.config["requireComponentContract"]:
                self._add_defect(
                    gate,
                    "component-contract-missing",
                    "No component mapping contract was compiled.",
                    expected="component-map.json",
                    actual=None,
                )
            self._finish_gate(gate, start, 0)
            return
        contract = read_json(self._resolve_bundle_path(contract_value))
        actual_components = actual.get("components", {})
        checked = 0
        mappings = contract.get("mappings", [])
        mapped_figma = {
            item.get("figmaComponent")
            for item in mappings
            if isinstance(item, dict)
        }
        for required in contract.get("requiredComponents", []):
            checked += 1
            if required not in mapped_figma:
                self._add_defect(
                    gate,
                    "component-unmapped",
                    f"Required Figma component {required!r} has no mapping decision.",
                    expected="mapping",
                    actual="missing",
                )
        for mapping in mappings:
            checked += 1
            decision = mapping.get("decision")
            if decision not in {"exact-match", "approved-deviation"}:
                self._add_defect(
                    gate,
                    "component-decision-open",
                    f"Component mapping for {mapping.get('figmaComponent')!r} is unresolved.",
                    expected="exact-match or approved-deviation",
                    actual=decision,
                )
            implementation = mapping.get("implementation")
            if implementation and implementation not in actual_components:
                self._add_defect(
                    gate,
                    "component-not-observed",
                    f"Mapped component {implementation!r} was not observed in the implementation.",
                    expected=implementation,
                    actual=list(actual_components),
                )
        self._finish_gate(
            gate,
            start,
            checked,
            must_evaluate=True,
            not_evaluated_message=(
                "A component contract was compiled but declares no required "
                "component and no mapping, so it decides nothing."
            ),
        )

    def _flow_gate(self, manifest: dict[str, Any]) -> None:
        gate = "flow"
        start = len(self.defects)
        contract_value = manifest.get("contracts", {}).get("flowContract")
        if not contract_value:
            if self.config["requireFlowContract"]:
                self._add_defect(
                    gate,
                    "flow-contract-missing",
                    "No flow contract was compiled.",
                    expected="flow-contract.json",
                    actual=None,
                )
            self._finish_gate(gate, start, 0)
            return
        contract_path = self._resolve_bundle_path(contract_value)
        contract = read_json(contract_path)
        results: dict[str, Any] = {}
        result_payload: dict[str, Any] = {}
        if self.options.flow_results_path:
            result_payload = read_json(self.options.flow_results_path)
            results = result_payload.get("results", {})
            if self.config["requireFlowEvidenceBinding"]:
                expected_sha = sha256_file(contract_path)
                if result_payload.get("contractSha256") != expected_sha:
                    self._add_defect(
                        gate,
                        "flow-contract-hash-mismatch",
                        "Flow results are not bound to the compiled contract bytes.",
                        expected=expected_sha,
                        actual=result_payload.get("contractSha256"),
                    )
                if result_payload.get("featureId") != manifest.get("featureId"):
                    self._add_defect(
                        gate,
                        "flow-feature-id-mismatch",
                        "Flow results belong to a different feature.",
                        expected=manifest.get("featureId"),
                        actual=result_payload.get("featureId"),
                    )
        checked = 0
        per_screen: dict[str, int] = {}
        for transition in contract.get("transitions", []):
            checked += 1
            from_screen = transition.get("from")
            if from_screen:
                per_screen[from_screen] = per_screen.get(from_screen, 0) + 1
            transition_id = transition.get("id")
            if transition.get("requiresConfirmation"):
                self._add_defect(
                    gate,
                    "flow-assumption-unresolved",
                    f"Flow {transition_id!r} still requires product confirmation.",
                    screen_node_id=transition.get("from"),
                    expected="requiresConfirmation=false",
                    actual=True,
                )
            result = results.get(transition_id)
            if self.config["requireFlowResults"] and not result:
                self._add_defect(
                    gate,
                    "flow-test-missing",
                    f"No executable test result exists for flow {transition_id!r}.",
                    screen_node_id=transition.get("from"),
                    expected={"passed": True},
                    actual=None,
                )
            elif result and not result.get("passed", False):
                self._add_defect(
                    gate,
                    "flow-test-failed",
                    f"Flow {transition_id!r} failed.",
                    screen_node_id=transition.get("from"),
                    expected={"passed": True},
                    actual=result,
                )
            elif result:
                if result.get("from") != transition.get("from") or result.get(
                    "to"
                ) != transition.get("to"):
                    self._add_defect(
                        gate,
                        "flow-transition-evidence-mismatch",
                        f"Flow {transition_id!r} evidence has different endpoints.",
                        screen_node_id=transition.get("from"),
                        expected={
                            "from": transition.get("from"),
                            "to": transition.get("to"),
                        },
                        actual={
                            "from": result.get("from"),
                            "to": result.get("to"),
                        },
                    )
                assertions = result.get("assertions", [])
                if transition.get("assertions") and (
                    len(assertions) != len(transition["assertions"])
                    or not all(item.get("passed") for item in assertions)
                ):
                    self._add_defect(
                        gate,
                        "flow-assertion-evidence-incomplete",
                        f"Flow {transition_id!r} lacks passing assertion evidence.",
                        screen_node_id=transition.get("from"),
                        expected=transition.get("assertions"),
                        actual=assertions,
                    )
        for screen_id, screen_checked in per_screen.items():
            self._record_screen_measurement(gate, screen_id, screen_checked)
        self._finish_gate(
            gate,
            start,
            checked,
            must_evaluate=True,
            not_evaluated_message=(
                "A flow contract was compiled but declares no transition, so "
                "no behaviour was verified."
            ),
        )

    def _resolve_actual_path(self, value: str | None) -> Path | None:
        if not value:
            return None
        return resolve_within(
            self.options.actual_snapshot.resolve().parent,
            value,
            label="actual screenshot",
        )

    def _numbers_close(
        self, expected: Any, actual: Any, tolerance: float
    ) -> bool:
        try:
            return math.isclose(
                float(expected), float(actual), abs_tol=tolerance
            )
        except (TypeError, ValueError):
            return False

    @property
    def _style_tolerance(self) -> float:
        return float(self.config["styleNumericTolerance"])

    def _tolerance_for(self, key: str) -> float:
        if key in RATIO_STYLE_KEYS:
            return float(self.config["styleRatioTolerance"])
        if key in OPACITY_STYLE_KEYS:
            return float(self.config["styleOpacityTolerance"])
        return self._style_tolerance

    def _style_values_equal(self, key: str, expected: Any, actual: Any) -> bool:
        if key in COLOR_STYLE_KEYS or (
            isinstance(expected, str) and expected.startswith("#")
        ):
            return self._colors_equal(expected, actual)
        if key in EXACT_STYLE_KEYS:
            return isinstance(actual, str) and expected == actual
        if isinstance(expected, bool):
            return expected is actual
        if isinstance(expected, (int, float)):
            return self._numbers_close(expected, actual, self._tolerance_for(key))
        return expected == actual

    def _colors_equal(self, expected: Any, actual: Any) -> bool:
        """Compare colors alpha-aware: #000000 is not #00000099."""

        if isinstance(expected, str) and isinstance(actual, str):
            return _opaque_hex(expected) == _opaque_hex(actual)
        return expected == actual

    def _pixel_diff(
        self, reference: Image.Image, actual: Image.Image
    ) -> tuple[float, Image.Image]:
        raw_diff = ImageChops.difference(reference, actual)
        tolerance = int(self.config["visualChannelTolerance"])
        mismatched = sum(
            1 for pixel in raw_diff.getdata() if max(pixel[:3]) > tolerance
        )
        ratio = mismatched / max(1, raw_diff.width * raw_diff.height)
        amplified = raw_diff.convert("RGB").point(
            lambda value: min(255, value * 4)
        )
        return ratio, amplified


def create_snapshot_template(
    bundle_dir: Path,
    output_path: Path,
    *,
    use_reference_screenshots: bool = False,
) -> dict[str, Any]:
    """Create an executable snapshot contract for adapters or harness demos."""

    bundle_dir = bundle_dir.resolve()
    manifest = read_json(bundle_dir / "manifest.json")
    snapshot: dict[str, Any] = {
        "schemaVersion": "1.0",
        "featureId": manifest.get("featureId"),
        "provenance": "self-test" if use_reference_screenshots else "template",
        "screens": {},
        "components": {},
    }
    for screen_item in manifest.get("screens", []):
        screen_path = Path(screen_item["compiledPath"])
        if not screen_path.is_absolute():
            screen_path = bundle_dir / screen_path
        screen = read_json(screen_path)
        copy_by_node: dict[str, dict[str, str]] = {}
        for item in screen.get("texts", []):
            copy_by_node.setdefault(item["nodeId"], {})[
                item.get("property", "textContent")
            ] = item["value"]
        elements = {}
        for element in screen.get("elements", []):
            elements[element["nodeId"]] = {
                "name": element.get("name"),
                "text": copy_by_node.get(element["nodeId"], {}).get(
                    "textContent"
                ),
                "copy": copy_by_node.get(element["nodeId"], {}),
                "rect": element.get("rect"),
                "style": element.get("style", {}),
            }
        for node_id, copy in copy_by_node.items():
            elements.setdefault(
                node_id,
                {
                    "name": "text",
                    "text": copy.get("textContent"),
                    "copy": copy,
                    "rect": None,
                    "style": {},
                },
            )["copy"] = copy
            if "textContent" in copy:
                elements[node_id]["text"] = copy["textContent"]
        reference = screen.get("referenceScreenshot")
        if reference and use_reference_screenshots:
            reference_path = resolve_within(
                bundle_dir, reference, label="reference screenshot"
            )
            reference_dir = output_path.resolve().parent / "reference-screenshots"
            reference_dir.mkdir(parents=True, exist_ok=True)
            reference_target = reference_dir / f"{safe_slug(screen['nodeId'])}.png"
            shutil.copy2(reference_path, reference_target)
            reference = str(
                reference_target.relative_to(output_path.resolve().parent)
            )
        snapshot["screens"][screen["nodeId"]] = {
            "name": screen.get("name"),
            "route": "TODO",
            "screenshot": reference if use_reference_screenshots else "TODO",
            "elements": elements,
            "assets": {},
        }
    write_json(output_path, snapshot)
    return snapshot
