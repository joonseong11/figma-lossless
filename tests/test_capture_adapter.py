"""Gate the capture adapter's settle-wait behaviour.

`playwright-capture.mjs` races `document.fonts.ready` and every `<img>`'s
`decode()` against `settleTimeoutMs` before screenshotting. An image behind
`display:none` anywhere in its ancestor chain never resolves `decode()` in
Chromium, so it used to burn the entire timeout on every screen for content
that was never going to be painted. This test covers the `isPaintable` guard
that excludes such images up front, using the same technique as
`test_token_gate.py`: slice the helper out of the file it actually ships in
(inside `page.evaluate`, so it cannot be imported) and run it under Node.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ADAPTER = (
    Path(__file__).resolve().parents[1] / "adapters" / "playwright-capture.mjs"
)


class CaptureAdapterSettleGuardTest(unittest.TestCase):
    def test_is_paintable_excludes_unrendered_images(self) -> None:
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


class CaptureAdapterProductSlotTest(unittest.TestCase):
    def test_product_slot_capture_includes_descendants_and_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "check-product-slot.mjs"
            script.write_text(PRODUCT_SLOT_HELPER_CHECK, encoding="utf-8")
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
const start = source.indexOf("    const isPaintable = (image) => {");
const end = source.indexOf("    const results = await Promise.all([");
assert.ok(start > 0 && end > start, "isPaintable helper block not found");
const build = new Function(source.slice(start, end) + "\\nreturn { isPaintable };");
const { isPaintable } = build();

// checkVisibility is the primary, ancestor-aware signal (Chromium 105+,
// always present in Playwright's bundled browser): a component hidden by a
// `display:none` breakpoint class higher up the tree reports false even
// though the <img> element itself has no inline display:none.
let calledWith = null;
const hiddenAncestor = {
  checkVisibility: (options) => {
    calledWith = options;
    return false;
  },
  getBoundingClientRect: () => ({ width: 69, height: 69 }),
};
assert.equal(isPaintable(hiddenAncestor), false);
assert.deepEqual(calledWith, { checkVisibilityCSS: true });

const painted = {
  checkVisibility: () => true,
  getBoundingClientRect: () => ({ width: 69, height: 69 }),
};
assert.equal(isPaintable(painted), true);

// Fallback path for an engine with no checkVisibility: a zero-size box (the
// only case that fallback can detect) is excluded; a real box is kept.
const zeroSizeNoCheckVisibility = { getBoundingClientRect: () => ({ width: 0, height: 0 }) };
assert.equal(isPaintable(zeroSizeNoCheckVisibility), false);

const renderedNoCheckVisibility = { getBoundingClientRect: () => ({ width: 40, height: 40 }) };
assert.equal(isPaintable(renderedNoCheckVisibility), true);

console.log("ok");
"""


PRODUCT_SLOT_HELPER_CHECK = """
import fs from "node:fs";
import assert from "node:assert/strict";

const source = fs.readFileSync(process.argv[2], "utf8");
const start = source.indexOf("        const captureProductSlot = (node) => {");
const helperStart = source.indexOf("        const captureProductVisibility = (node) => {");
const end = source.indexOf("        for (const node of document.querySelectorAll", helperStart);
assert.ok(start > 0 && end > start, "captureProductSlot helper block not found");
const build = new Function(
  "document",
  "NodeFilter",
  "getComputedStyle",
  source.slice(helperStart, end) + "\\nreturn { captureProductSlot };",
);

const styles = new Map();
const fakeDocument = {
  createTreeWalker: (root) => {
    let index = -1;
    return {
      currentNode: null,
      nextNode() {
        index += 1;
        this.currentNode = root.textNodes?.[index] ?? null;
        return this.currentNode !== null;
      },
    };
  },
};
const { captureProductSlot } = build(
  fakeDocument,
  { SHOW_TEXT: 4 },
  (node) => styles.get(node),
);
const makeNode = ({ visible = true, width = 40, height = 12, style = {}, textNodes = [] } = {}) => {
  const node = {
    textNodes,
    tagName: "SPAN",
    checkVisibility: (options) => {
      assert.deepEqual(options, { checkOpacity: true, checkVisibilityCSS: true });
      return visible;
    },
    getBoundingClientRect: () => ({ x: 1, y: 2, width, height }),
    getAttribute: () => null,
  };
  styles.set(node, {
    display: "block",
    visibility: "visible",
    opacity: "1",
    ...style,
  });
  return node;
};

const visibleChild = makeNode();
const transparentChild = makeNode({ style: { opacity: "0" } });
const nested = captureProductSlot(makeNode({
  textNodes: [
    { nodeValue: "  nested@example.net  ", parentElement: visibleChild },
    { nodeValue: "hidden@example.test", parentElement: transparentChild },
  ],
}));
assert.equal(nested.copy.textContent, "nested@example.net");
assert.ok(!nested.copy.textContent.includes("hidden@example.test"));
assert.equal(nested.text, "nested@example.net");
assert.equal(nested.rendered, true);
assert.deepEqual(nested.rect, { x: 1, y: 2, width: 40, height: 12 });
assert.deepEqual(nested.style, { display: "block", visibility: "visible", opacity: "1" });

assert.equal(captureProductSlot(makeNode({ visible: false })).rendered, false);
assert.equal(captureProductSlot(makeNode({ style: { display: "none" } })).rendered, false);
assert.equal(captureProductSlot(makeNode({ style: { visibility: "hidden" } })).rendered, false);
assert.equal(captureProductSlot(makeNode({ style: { opacity: "0" } })).rendered, false);
assert.equal(captureProductSlot(makeNode({ width: 0 })).rendered, false);

console.log("ok");
"""
