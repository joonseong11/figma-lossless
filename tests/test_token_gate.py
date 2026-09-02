"""Gate the design-token usage channel.

`getComputedStyle` substitutes `var()` before the capture records anything, so
a hardcoded `#7C3AED` and `var(--color-primary-500)` reach the style gate as
the same value. These tests cover the evidence that separates them: the
adapter's declared `tokenRefs` and the gate that reads them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from figma_lossless.report import render_report
from figma_lossless.util import write_json
from figma_lossless.validators import GateValidator, ValidateOptions

from canonical_bundle import attach_accounting, clean_accounting


FEATURE_ID = "token-gate"
SCREEN_ID = "1:1"
ELEMENT_ID = "1:2"
RECT = {"x": 0, "y": 0, "width": 10, "height": 10}
TOKEN = "color/primary-500"
CUSTOM_PROPERTY = "--color-primary-500"
TOKEN_HEX = "#7C3AED"
ADAPTER = (
    Path(__file__).resolve().parents[1] / "adapters" / "playwright-capture.mjs"
)


class TokenGateHarness(unittest.TestCase):
    """Bundle/snapshot plumbing for a single token-bound element."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.output = self.root / "report"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_bundle(
        self,
        *,
        style: dict[str, Any] | None = None,
        resolved_tokens: dict[str, str] | None = None,
    ) -> None:
        element: dict[str, Any] = {
            "nodeId": ELEMENT_ID,
            "type": "frame",
            "figmaType": "FRAME",
            "name": "Primary button",
            "parentNodeId": None,
            "hidden": False,
            "effectiveHidden": False,
            "rect": dict(RECT),
            "style": dict(style or {"backgroundColor": TOKEN_HEX}),
        }
        if resolved_tokens is not None:
            element["resolvedTokens"] = dict(resolved_tokens)
            element["tokenBindings"] = {
                key: f"VariableID:9:{index}"
                for index, key in enumerate(resolved_tokens)
            }
        write_json(
            self.bundle / "screens" / "1-1.json",
            {
                "schemaVersion": "1.0",
                "nodeId": SCREEN_ID,
                "name": "Screen",
                "evidenceSource": "figma-rest",
                "viewport": {"width": 10, "height": 10},
                "referenceScreenshot": None,
                "referenceCode": None,
                "texts": [],
                "elements": [element],
                "assets": [],
                "variables": {},
                "canonicalEvidence": {
                    "path": "rest/nodes/1-1.json",
                    "sha256": "0" * 64,
                    "bytes": 1,
                },
                "status": {"contextFetched": True, "specCompiled": True},
            },
        )
        accounting = clean_accounting(FEATURE_ID)
        write_json(self.bundle / "property-accounting.json", accounting)
        write_json(
            self.bundle / "manifest.json",
            attach_accounting(
                {
                    "schemaVersion": "1.0",
                    "featureId": FEATURE_ID,
                    "screens": [
                        {
                            "nodeId": SCREEN_ID,
                            "name": "Screen",
                            "compiledPath": "screens/1-1.json",
                        }
                    ],
                    "contracts": {"flowContract": None, "componentMap": None},
                },
                accounting,
            ),
        )
        write_json(
            self.bundle / "coverage.json",
            [
                {
                    "nodeId": SCREEN_ID,
                    "name": "Screen",
                    "status": {"contextFetched": True, "specCompiled": True},
                }
            ],
        )

    def _write_actual(
        self,
        *,
        style: dict[str, Any] | None = None,
        token_refs: dict[str, list[str]] | None = None,
        custom_properties: dict[str, str] | None = None,
    ) -> Path:
        element: dict[str, Any] = {
            "name": "Primary button",
            "rect": dict(RECT),
            "style": dict(style or {"backgroundColor": TOKEN_HEX}),
        }
        if token_refs is not None:
            element["tokenRefs"] = {
                key: list(value) for key, value in token_refs.items()
            }
        screen: dict[str, Any] = {
            "name": "Screen",
            "route": "/",
            "screenshot": None,
            "elements": {ELEMENT_ID: element},
            "assets": {},
        }
        if custom_properties is not None:
            screen["customProperties"] = dict(custom_properties)
        path = self.root / "actual.json"
        write_json(
            path,
            {
                "schemaVersion": "1.0",
                "featureId": FEATURE_ID,
                "provenance": "browser-capture",
                "screens": {SCREEN_ID: screen},
                "components": {},
            },
        )
        return path

    def _validate(
        self, actual_path: Path, **config: Any
    ) -> dict[str, Any]:
        path = self.root / "config.json"
        # These fixtures compile a single screen to exercise the token gate and
        # carry no flow or component contract. Both are required by default, so
        # the opt-out is stated here rather than letting an unrelated hard
        # failure decide what a token-gate assertion means.
        write_json(
            path,
            {
                "requireReferenceScreenshots": False,
                "requireFlowContract": False,
                "requireComponentContract": False,
                **config,
            },
        )
        return GateValidator(
            ValidateOptions(self.bundle, actual_path, self.output, config_path=path)
        ).validate()

    def _run(
        self,
        *,
        token_refs: dict[str, list[str]] | None,
        resolved_tokens: dict[str, str] | None = None,
        custom_properties: dict[str, str] | None = None,
        style_key: str = "backgroundColor",
        **config: Any,
    ) -> dict[str, Any]:
        """Gate one element whose style value is right but may bypass the token."""

        self._write_bundle(
            style={style_key: TOKEN_HEX},
            resolved_tokens=(
                {style_key: TOKEN} if resolved_tokens is None else resolved_tokens
            ),
        )
        actual_path = self._write_actual(
            style={style_key: TOKEN_HEX},
            token_refs=token_refs,
            custom_properties=(
                {CUSTOM_PROPERTY: TOKEN_HEX}
                if custom_properties is None
                else custom_properties
            ),
        )
        config.setdefault("tokenMap", {TOKEN: CUSTOM_PROPERTY})
        return self._validate(actual_path, **config)

    def _token_defects(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in result["defects"] if item["gate"] == "token"]


class TokenGateTest(TokenGateHarness):
    def test_referenced_token_passes(self) -> None:
        result = self._run(
            token_refs={"background-color": [CUSTOM_PROPERTY]},
        )
        self.assertEqual(self._token_defects(result), [])
        self.assertEqual(result["gates"]["token"]["status"], "PASS")
        self.assertEqual(result["gates"]["token"]["checked"], 1)
        self.assertTrue(result["passed"], result["defects"])

    def test_hardcoded_value_warns_by_default(self) -> None:
        result = self._run(token_refs={})
        defects = self._token_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "token-not-used")
        self.assertEqual(defects[0]["severity"], "warning")
        self.assertEqual(defects[0]["expectedToken"], TOKEN)
        self.assertEqual(
            defects[0]["expected"],
            {
                "styleKey": "backgroundColor",
                "figmaToken": TOKEN,
                "customProperties": [CUSTOM_PROPERTY],
                "cssProperties": ["background-color", "background"],
            },
        )
        self.assertEqual(defects[0]["actual"], {"tokenRefs": {}})
        # The style gate is satisfied: the hex is right, only its provenance
        # is wrong, which is exactly the hole this channel closes.
        self.assertEqual(
            [item for item in result["defects"] if item["gate"] == "style"], []
        )
        self.assertTrue(result["passed"], result["defects"])

    def test_hardcoded_value_fails_under_hard_policy(self) -> None:
        result = self._run(token_refs={}, tokenUsagePolicy="hard")
        defects = self._token_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "token-not-used")
        self.assertEqual(defects[0]["severity"], "hard")
        self.assertFalse(result["gates"]["token"]["passed"])
        self.assertFalse(result["passed"])

    def test_a_different_custom_property_does_not_satisfy_the_token(self) -> None:
        result = self._run(
            token_refs={"background-color": ["--brand-purple"]},
            tokenUsagePolicy="hard",
        )
        defects = self._token_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "token-not-used")
        self.assertEqual(
            defects[0]["actual"],
            {"tokenRefs": {"background-color": ["--brand-purple"]}},
        )

    def test_reference_under_an_unrelated_property_does_not_count(self) -> None:
        result = self._run(
            token_refs={"color": [CUSTOM_PROPERTY]},
            tokenUsagePolicy="hard",
        )
        defects = self._token_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "token-not-used")
        # `color` cannot drive backgroundColor, so it is not even reported as
        # what was observed for the properties that could.
        self.assertEqual(defects[0]["actual"], {"tokenRefs": {}})

    def test_unmapped_token_is_a_warning(self) -> None:
        result = self._run(
            token_refs={"background-color": [CUSTOM_PROPERTY]},
            tokenMap={},
            tokenUsagePolicy="hard",
        )
        defects = self._token_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "token-unmapped")
        # A mapping gap is config debt: it stays a warning even under the
        # hard policy, so an unconfigured map can never fail someone's build.
        self.assertEqual(defects[0]["severity"], "warning")
        self.assertEqual(defects[0]["expectedToken"], TOKEN)
        self.assertEqual(result["gates"]["token"]["checked"], 1)
        self.assertTrue(result["passed"], result["defects"])

    def test_fallback_chain_reference_satisfies_the_token(self) -> None:
        # var(--brand, var(--color-primary-500)) names both properties, and
        # either one being the mapped token means the element is on-system.
        result = self._run(
            token_refs={
                "background-color": ["--brand", CUSTOM_PROPERTY],
            },
            tokenUsagePolicy="hard",
        )
        self.assertEqual(self._token_defects(result), [])
        self.assertTrue(result["passed"], result["defects"])

    def test_shorthand_background_satisfies_background_color(self) -> None:
        result = self._run(
            token_refs={"background": [CUSTOM_PROPERTY]},
            tokenUsagePolicy="hard",
        )
        self.assertEqual(self._token_defects(result), [])
        self.assertTrue(result["passed"], result["defects"])

    def test_any_mapped_alias_satisfies_the_token(self) -> None:
        result = self._run(
            token_refs={"background-color": ["--legacy-primary"]},
            tokenMap={TOKEN: [CUSTOM_PROPERTY, "--legacy-primary"]},
            tokenUsagePolicy="hard",
        )
        self.assertEqual(self._token_defects(result), [])
        self.assertTrue(result["passed"], result["defects"])

    def test_policy_off_skips_the_gate(self) -> None:
        result = self._run(token_refs={}, tokenUsagePolicy="off")
        self.assertEqual(self._token_defects(result), [])
        self.assertEqual(result["gates"]["token"]["status"], "NOT_EVALUATED")
        self.assertEqual(result["gates"]["token"]["checked"], 0)

    def test_legacy_snapshot_without_token_refs_fails_the_hard_policy(
        self,
    ) -> None:
        result = self._run(token_refs=None, tokenUsagePolicy="hard")
        defects = self._token_defects(result)
        # The element itself is not accused of anything -- a capture taken
        # before this channel existed carries no evidence either way. But the
        # run asked for hard token enforcement and got none, and reporting that
        # as a clean gate is how a policy becomes decorative.
        self.assertEqual([item["elementNodeId"] for item in defects], [None])
        self.assertEqual([item["type"] for item in defects], ["gate-not-evaluated"])
        self.assertEqual(result["gates"]["token"]["status"], "FAIL")
        self.assertEqual(result["gates"]["token"]["checked"], 0)
        self.assertFalse(result["passed"])

    def test_element_missing_from_the_snapshot_is_not_evaluated(self) -> None:
        self._write_bundle(resolved_tokens={"backgroundColor": TOKEN})
        path = self.root / "actual.json"
        write_json(
            path,
            {
                "schemaVersion": "1.0",
                "featureId": FEATURE_ID,
                "provenance": "browser-capture",
                "screens": {
                    SCREEN_ID: {
                        "name": "Screen",
                        "route": "/",
                        "screenshot": None,
                        "elements": {},
                        "assets": {},
                    }
                },
                "components": {},
            },
        )
        result = self._validate(
            path, tokenUsagePolicy="hard", tokenMap={TOKEN: CUSTOM_PROPERTY}
        )
        # The structure gate owns the missing element; the token gate must not
        # pile a second, misleading verdict on top of it -- so nothing here is
        # attributed to the element. The gate still reports that a hard policy
        # measured nothing.
        defects = self._token_defects(result)
        self.assertEqual([item["elementNodeId"] for item in defects], [None])
        self.assertEqual([item["type"] for item in defects], ["gate-not-evaluated"])
        self.assertEqual(result["gates"]["token"]["status"], "FAIL")

    def test_undefined_custom_property_is_flagged_in_the_payload(self) -> None:
        result = self._run(
            token_refs={},
            custom_properties={"--space-2": "8px"},
        )
        defects = self._token_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(
            defects[0]["undefinedCustomProperties"], [CUSTOM_PROPERTY]
        )
        self.assertIn("the token system itself is missing", defects[0]["message"])

    def test_a_defined_custom_property_is_not_reported_as_missing(self) -> None:
        result = self._run(token_refs={})
        defects = self._token_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertNotIn("undefinedCustomProperties", defects[0])
        self.assertNotIn("the token system itself", defects[0]["message"])

    def test_snapshot_level_custom_properties_are_used_as_a_fallback(
        self,
    ) -> None:
        self._write_bundle(resolved_tokens={"backgroundColor": TOKEN})
        path = self.root / "actual.json"
        write_json(
            path,
            {
                "schemaVersion": "1.0",
                "featureId": FEATURE_ID,
                "provenance": "browser-capture",
                "customProperties": {CUSTOM_PROPERTY: TOKEN_HEX},
                "screens": {
                    SCREEN_ID: {
                        "name": "Screen",
                        "route": "/",
                        "screenshot": None,
                        "elements": {
                            ELEMENT_ID: {
                                "name": "Primary button",
                                "rect": dict(RECT),
                                "style": {"backgroundColor": TOKEN_HEX},
                                "tokenRefs": {},
                            }
                        },
                        "assets": {},
                    }
                },
                "components": {},
            },
        )
        result = self._validate(path, tokenMap={TOKEN: CUSTOM_PROPERTY})
        defects = self._token_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertNotIn("undefinedCustomProperties", defects[0])

    def test_hidden_elements_are_not_gated(self) -> None:
        self._write_bundle(resolved_tokens={"backgroundColor": TOKEN})
        screen_path = self.bundle / "screens" / "1-1.json"
        screen = json.loads(screen_path.read_text(encoding="utf-8"))
        screen["elements"][0]["effectiveHidden"] = True
        screen["elements"][0]["hiddenStyle"] = screen["elements"][0]["style"]
        screen["elements"][0]["style"] = {}
        write_json(screen_path, screen)
        actual_path = self._write_actual(token_refs={})
        result = self._validate(
            actual_path, tokenUsagePolicy="hard", tokenMap={TOKEN: CUSTOM_PROPERTY}
        )
        # A hidden element is not asked to reference its token. With the only
        # token-bearing element hidden, though, the hard policy enforced
        # nothing on this screen, and the gate says so instead of passing.
        defects = self._token_defects(result)
        self.assertEqual([item["elementNodeId"] for item in defects], [None])
        self.assertEqual([item["type"] for item in defects], ["gate-not-evaluated"])
        self.assertEqual(result["gates"]["token"]["status"], "FAIL")

    def test_style_key_without_a_css_mapping_is_a_warning(self) -> None:
        result = self._run(
            token_refs={"background-color": [CUSTOM_PROPERTY]},
            resolved_tokens={"strokeAlign": TOKEN},
            tokenUsagePolicy="hard",
        )
        defects = self._token_defects(result)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["type"], "token-style-key-unsupported")
        self.assertEqual(defects[0]["severity"], "warning")
        self.assertTrue(result["passed"], result["defects"])

    def test_every_bound_token_on_an_element_is_counted(self) -> None:
        self._write_bundle(
            style={"backgroundColor": TOKEN_HEX, "paddingTop": 8},
            resolved_tokens={
                "backgroundColor": TOKEN,
                "paddingTop": "space/2",
            },
        )
        actual_path = self._write_actual(
            style={"backgroundColor": TOKEN_HEX, "paddingTop": 8},
            token_refs={"padding": ["--space-2"]},
            custom_properties={CUSTOM_PROPERTY: TOKEN_HEX, "--space-2": "8px"},
        )
        result = self._validate(
            actual_path,
            tokenUsagePolicy="hard",
            tokenMap={TOKEN: CUSTOM_PROPERTY, "space/2": "--space-2"},
        )
        defects = self._token_defects(result)
        self.assertEqual(result["gates"]["token"]["checked"], 2)
        self.assertEqual(len(defects), 1, result["defects"])
        self.assertEqual(defects[0]["expected"]["styleKey"], "backgroundColor")


class TokenReportTest(TokenGateHarness):
    def test_report_panel_names_the_bypassed_token(self) -> None:
        result = self._run(
            token_refs={}, custom_properties={"--space-2": "8px"}
        )
        document = render_report(result, self.output).read_text(encoding="utf-8")
        self.assertIn("Design token usage", document)
        self.assertIn("Tokens bypassed by the implementation", document)
        self.assertIn(TOKEN, document)
        self.assertIn(CUSTOM_PROPERTY, document)
        self.assertIn("token-not-used", document)

    def test_report_panel_explains_a_skipped_gate(self) -> None:
        result = self._run(token_refs={}, tokenUsagePolicy="off")
        document = render_report(result, self.output).read_text(encoding="utf-8")
        self.assertIn("Token usage was not evaluated", document)
        self.assertIn("tokenUsagePolicy", document)


class CaptureTokenRefsTest(unittest.TestCase):
    """Exercise the adapter's own extraction, not a Python restatement of it."""

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_adapter_collects_declared_var_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "check.mjs"
            script.write_text(ADAPTER_HELPER_CHECK, encoding="utf-8")
            result = subprocess.run(
                ["node", str(script), str(ADAPTER)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


# Loads the helper text the adapter actually ships (it lives inside
# page.evaluate and cannot be imported) and asserts on its behaviour.
ADAPTER_HELPER_CHECK = """
import fs from "node:fs";
import assert from "node:assert/strict";

const source = fs.readFileSync(process.argv[2], "utf8");
const start = source.indexOf("    const splitTopLevel");
const end = source.indexOf("    // Chromium always serializes box-shadow");
assert.ok(start > 0 && end > start, "capture helper block not found");
const build = new Function(
  "styleRules",
  source.slice(start, end) +
    "\\nreturn { extractCustomProperties, parseDeclarations," +
    " collectRootCustomProperties, collectTokenRefs, collectStyleRules };",
);

const rules = [
  { selectorText: ".btn", declarations: [["background", "var(--color-primary-500)"]] },
  {
    selectorText: ":root, [data-theme='dark']",
    declarations: [["--color-primary-500", "#7C3AED"], ["--space-2", "8px"]],
  },
  { selectorText: ".btn:hover", declarations: [["color", "var(--fg, var(--fg-fallback, #fff))"]] },
  { selectorText: "&:focus", declarations: [["outline-color", "var(--ring)"]] },
  { selectorText: ".other", declarations: [["gap", "var(--unrelated)"]] },
];
const api = build(rules);

// Fallback chains name every custom property, at any depth.
assert.deepEqual(api.extractCustomProperties("var(--a, var(--b, #fff))"), ["--a", "--b"]);
assert.deepEqual(
  api.extractCustomProperties("var(--a, var(--b, var(--c, red)))"),
  ["--a", "--b", "--c"],
);
assert.deepEqual(api.extractCustomProperties("calc(var(--a) + var(--b))"), ["--a", "--b"]);
assert.deepEqual(api.extractCustomProperties("#7C3AED"), []);

// Declarations stay as authored: the shorthand is not expanded and the
// custom property keeps its case.
assert.deepEqual(api.parseDeclarations("background: var(--x); PADDING-TOP: 4px"), [
  ["background", "var(--x)"],
  ["padding-top", "4px"],
]);
assert.deepEqual(api.parseDeclarations("--My-Token: red"), [["--My-Token", "red"]]);
assert.deepEqual(
  api.parseDeclarations('background-image: url("http://x/a;b.png"); color: red'),
  [["background-image", 'url("http://x/a;b.png")'], ["color", "red"]],
);

// :root declarations are the defined token system.
assert.deepEqual(api.collectRootCustomProperties(rules), {
  "--color-primary-500": "#7C3AED",
  "--space-2": "8px",
});

// Inline style plus every matching rule, unioned; a selector this engine
// cannot match is skipped instead of crashing the capture.
const node = {
  style: { cssText: "gap: var(--space-2)" },
  matches(selector) {
    if (selector.startsWith("&")) throw new SyntaxError("invalid selector");
    return selector === ".btn" || selector === ".btn:hover";
  },
};
assert.deepEqual(api.collectTokenRefs(node), {
  gap: ["--space-2"],
  background: ["--color-primary-500"],
  color: ["--fg", "--fg-fallback"],
});

// An unreadable (cross-origin) sheet yields no rules rather than an error.
globalThis.document = {
  styleSheets: [
    {
      get cssRules() {
        throw new Error("SecurityError");
      },
    },
  ],
};
assert.deepEqual(api.collectStyleRules(), []);

// Rules reachable only through @media, @import and a constructable sheet are
// still rules: a token declared behind one is declared.
const styleRule = (selectorText, cssText) => ({
  selectorText,
  style: { cssText },
});
globalThis.document = {
  styleSheets: [
    {
      cssRules: [
        { cssRules: [styleRule(".in-media", "color: var(--a)")] },
        { styleSheet: { cssRules: [styleRule(".imported", "color: var(--b)")] } },
      ],
    },
  ],
  adoptedStyleSheets: [{ cssRules: [styleRule(".adopted", "color: var(--c)")] }],
};
assert.deepEqual(
  api.collectStyleRules().map((rule) => rule.selectorText),
  [".in-media", ".imported", ".adopted"],
);
"""


if __name__ == "__main__":
    unittest.main()
