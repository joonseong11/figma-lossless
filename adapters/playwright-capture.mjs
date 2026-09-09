#!/usr/bin/env node

// Uses the consuming application's Playwright installation. Audit fixture
// pages expose Figma markers; routeKind records whether a capture came from
// such a fixture or from the shipping product route.
import fs from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import process from "node:process";

const args = Object.fromEntries(
  process.argv.slice(2).reduce((pairs, value, index, all) => {
    if (value.startsWith("--")) pairs.push([value.slice(2), all[index + 1]]);
    return pairs;
  }, []),
);
if (!args.plan || !args.output) {
  console.error("Usage: node playwright-capture.mjs --plan capture-plan.json --output actual.json");
  process.exit(1);
}

// Capturing is a verification step, and verification is locked until someone
// unlocks it on purpose. Same rule as figma_lossless/mode.py and the hooks: a
// valid FIGMA_LOSSLESS_MODE wins, then the nearest .figma-lossless/mode.json
// at or above the working directory, then "extract". Refuse before creating
// any output so a curious run leaves nothing behind.
const MODE_VALUES = new Set(["extract", "verify"]);
const resolveMode = async () => {
  const fromEnv = process.env.FIGMA_LOSSLESS_MODE;
  if (MODE_VALUES.has(fromEnv)) return fromEnv;
  let directory = path.resolve(process.cwd());
  for (;;) {
    const candidate = path.join(directory, ".figma-lossless", "mode.json");
    try {
      const payload = JSON.parse(await fs.readFile(candidate, "utf8"));
      const value = payload && typeof payload === "object" ? payload.mode : undefined;
      return MODE_VALUES.has(value) ? value : "extract";
    } catch (error) {
      if (error?.code !== "ENOENT") return "extract";
    }
    const parent = path.dirname(directory);
    if (parent === directory) return "extract";
    directory = parent;
  }
};
if ((await resolveMode()) !== "verify") {
  console.error(
    "playwright-capture.mjs is a verification step and verification mode is locked " +
      "(the harness ships in extract mode). Unlock it on purpose with\n" +
      "  figma-lossless mode --set verify        # this directory tree\n" +
      "or  FIGMA_LOSSLESS_MODE=verify              # this process only",
  );
  process.exit(1);
}

const planPath = path.resolve(args.plan);
const outputPath = path.resolve(args.output);
const outputDir = path.dirname(outputPath);
const plan = JSON.parse(await fs.readFile(planPath, "utf8"));
await fs.mkdir(path.join(outputDir, "screenshots"), { recursive: true });

const targetRequire = createRequire(path.join(process.cwd(), "package.json"));
let chromium;
try {
  targetRequire.resolve("playwright");
} catch (error) {
  if (error?.code === "MODULE_NOT_FOUND") {
    throw new Error(
      `Playwright must be installed in the target repository: ${process.cwd()}`,
      { cause: error },
    );
  }
  throw error;
}
({ chromium } = targetRequire("playwright"));

const baseUrl = new URL(plan.baseUrl);
const localHosts = new Set(["127.0.0.1", "localhost", "::1"]);
if (!localHosts.has(baseUrl.hostname) || !["http:", "https:"].includes(baseUrl.protocol)) {
  throw new Error("Capture plans are restricted to a localhost HTTP(S) origin.");
}

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport: plan.viewport ?? { width: 375, height: 812 },
  deviceScaleFactor: plan.deviceScaleFactor ?? 1,
  locale: plan.locale ?? "en-US",
  timezoneId: plan.timezoneId ?? "UTC",
  reducedMotion: "reduce",
});
const page = await context.newPage();
// Block every off-origin request, and only those. Matching on the URL means
// same-origin traffic is never intercepted at all: routing it through
// `route.continue()` stalls on a streamed response, which is exactly what a
// framework dev server sends, and the capture would hang instead of failing.
// A plan may name additional localhost origins (a mock backend on another
// port, for example) that the product routes are allowed to call. Anything
// else stays blocked, and a non-localhost entry is a plan error, not a hole.
const allowedOrigins = new Set([baseUrl.origin]);
for (const entry of plan.allowedOrigins ?? []) {
  const origin = new URL(entry);
  if (!localHosts.has(origin.hostname) || !["http:", "https:"].includes(origin.protocol)) {
    throw new Error(`allowedOrigins entries must be localhost HTTP(S) origins: ${entry}`);
  }
  allowedOrigins.add(origin.origin);
}
const isSameOrigin = (url) =>
  ["data:", "blob:"].includes(url.protocol) || allowedOrigins.has(url.origin);
await page.route(
  (url) => !isSameOrigin(url),
  async (route) => {
    await route.abort("blockedbyclient");
  },
);
const snapshot = {
  schemaVersion: "1.0",
  featureId: plan.featureId,
  provenance: "browser-capture",
  screens: {},
  components: {},
  customProperties: {},
};

for (const screen of plan.screens) {
  const routeKind = screen.routeKind ?? "fixture";
  if (!["fixture", "product"].includes(routeKind)) {
    throw new Error(`Unsupported routeKind: ${routeKind}`);
  }
  const targetUrl = new URL(screen.route, baseUrl);
  if (targetUrl.origin !== baseUrl.origin) throw new Error(`Cross-origin route rejected: ${screen.route}`);
  if (screen.viewport) await page.setViewportSize(screen.viewport);
  // `networkidle` is the safe default for a static build, but a dev server
  // holds an HMR socket open forever and the wait never resolves. A plan that
  // targets one can say so; pair it with `readySelector` so the capture still
  // waits for real content rather than an arbitrary moment.
  const waitUntil = screen.waitUntil ?? plan.waitUntil ?? "networkidle";
  if (!["load", "domcontentloaded", "networkidle", "commit"].includes(waitUntil)) {
    throw new Error(`Unsupported waitUntil: ${waitUntil}`);
  }
  await page.goto(targetUrl.toString(), { waitUntil });
  if (screen.prepareScript) {
    if (args["allow-prepare-script"] !== "true") {
      throw new Error("prepareScript requires --allow-prepare-script true for a trusted plan");
    }
    await page.evaluate(screen.prepareScript);
  }
  if (screen.readySelector) await page.locator(screen.readySelector).waitFor({ state: "visible" });
  await page.addStyleTag({ content: "*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}" });
  // Custom-property overrides declared in the plan. A design drawn for a phone
  // reserves room for the OS status bar through `env(safe-area-inset-top)`,
  // which a desktop browser reports as 0 -- so every element sits ~44px above
  // where the design puts it, through no fault of the implementation. Setting
  // the variable here makes the page lay out the way it does on the device.
  // Only custom properties are accepted, so a plan cannot restyle the page
  // into passing: it can supply an environment value, not a look.
  const rootStyle = { ...(plan.rootStyle ?? {}), ...(screen.rootStyle ?? {}) };
  const rootStyleEntries = Object.entries(rootStyle);
  if (rootStyleEntries.length) {
    for (const [property] of rootStyleEntries) {
      if (!/^--[A-Za-z0-9_-]+$/.test(property)) {
        throw new Error(`rootStyle accepts CSS custom properties only, got: ${property}`);
      }
    }
    const declarations = rootStyleEntries
      .map(([property, value]) => `${property}:${String(value)} !important`)
      .join(";");
    await page.addStyleTag({ content: `:root{${declarations}}` });
  }
  // Settling fonts and images keeps text metrics and raster content stable
  // before measurement. Both waits are bounded: an <img> whose request was
  // blocked as off-origin never settles, and an unbounded decode() would hang
  // the capture with no output rather than reporting what it could not load.
  const settleTimeoutMs = Number(plan.settleTimeoutMs ?? 10000);
  const unsettled = await page.evaluate(async (timeoutMs) => {
    const bound = (promise, label) =>
      Promise.race([
        Promise.resolve(promise).then(
          () => null,
          () => label,
        ),
        new Promise((resolve) => setTimeout(() => resolve(label), timeoutMs)),
      ]);
    // Chromium only decodes an image it is actually going to paint: one
    // sitting behind `display:none` (or otherwise not rendered) anywhere in
    // its ancestor chain never resolves `decode()` at all, no matter how
    // long the wait -- a permanently-mounted, breakpoint-hidden component is
    // enough to trigger this. Racing that promise against the full timeout
    // still buys nothing but a guaranteed timeout, so it is excluded up
    // front instead: it was never going to appear in the screenshot either
    // way, so this costs zero accuracy. `checkVisibility` (Chromium 105+,
    // always present in Playwright's bundled browser) is the ancestor-aware
    // signal; the rect check is a defensive fallback for an image with no
    // rendered box.
    const isPaintable = (image) => {
      if (typeof image.checkVisibility === "function") {
        return image.checkVisibility({ checkVisibilityCSS: true });
      }
      const rect = image.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };
    const results = await Promise.all([
      bound(document.fonts.ready, "fonts"),
      ...[...document.images]
        .filter(isPaintable)
        .map((image) => bound(image.decode(), image.currentSrc || image.src || "image")),
    ]);
    return results.filter(Boolean);
  }, settleTimeoutMs);
  if (unsettled.length) {
    console.error(
      `[capture] ${screen.nodeId}: ${unsettled.length} resource(s) did not settle: ${unsettled.slice(0, 5).join(", ")}`,
    );
  }
  const filename = `${screen.nodeId.replace(/[^a-zA-Z0-9_-]/g, "-")}.png`;
  const screenshot = path.join(outputDir, "screenshots", filename);
  await page.screenshot({ path: screenshot, fullPage: false, animations: "disabled" });
  const captured = routeKind === "product"
    ? await page.evaluate(() => {
        const slots = {};
        const duplicates = [];
        const captureProductVisibility = (node) => {
          const rect = node.getBoundingClientRect();
          const computed = getComputedStyle(node);
          const visibleByBrowser =
            typeof node.checkVisibility === "function"
              ? node.checkVisibility({
                  checkOpacity: true,
                  checkVisibilityCSS: true,
                })
              : true;
          const style = {
            display: computed.display,
            visibility: computed.visibility,
            opacity: computed.opacity,
          };
          return {
            rendered:
              visibleByBrowser &&
              rect.width > 0 &&
              rect.height > 0 &&
              style.display !== "none" &&
              style.visibility !== "hidden" &&
              Number.parseFloat(style.opacity) > 0,
            rect: {
              x: rect.x,
              y: rect.y,
              width: rect.width,
              height: rect.height,
            },
            style,
          };
        };
        const renderedDescendantText = (node) => {
          const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
          const chunks = [];
          while (walker.nextNode()) {
            const textNode = walker.currentNode;
            const parent = textNode.parentElement;
            if (parent && captureProductVisibility(parent).rendered) {
              chunks.push(textNode.nodeValue ?? "");
            }
          }
          return chunks.join("").trim();
        };
        const captureProductSlot = (node) => {
          const textContent = renderedDescendantText(node);
          const visibility = captureProductVisibility(node);
          const copy = {
            // Include descendants: semantic slots often wrap their displayed
            // value in <strong> or another presentational child.
            textContent,
            placeholder: node.getAttribute("placeholder") ?? undefined,
            alt: node.getAttribute("alt") ?? undefined,
            "aria-label": node.getAttribute("aria-label") ?? undefined,
            title: node.getAttribute("title") ?? undefined,
            value: "value" in node ? node.value : node.getAttribute("value") ?? undefined,
            tag: node.tagName.toLowerCase(),
            selectedText:
              node.tagName === "SELECT" && node.selectedOptions?.length
                ? node.selectedOptions[0].textContent.trim()
                : undefined,
          };
          return {
            text: copy.textContent,
            copy,
            ...visibility,
          };
        };
        for (const node of document.querySelectorAll("[data-slot]")) {
          const binding = node.getAttribute("data-slot");
          if (slots[binding]) {
            duplicates.push(binding);
            continue;
          }
          slots[binding] = captureProductSlot(node);
        }
        return { slots, duplicates };
      })
    : await page.evaluate((screenNodeId) => {
    const px = (value) => {
      const parsed = Number.parseFloat(value);
      return Number.isFinite(parsed) ? parsed : value;
    };
    const color = (value) => {
      const match = value.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
      if (!match) return value.toUpperCase();
      const [, r, g, b, a] = match;
      const hex = [r, g, b].map((channel) => Number(channel).toString(16).padStart(2, "0")).join("");
      const alpha = a === undefined ? 1 : Number(a);
      const alphaByte = Math.round(alpha * 255);
      // 255 is the opaque form Chromium itself serializes as rgb(), so it has
      // to read back as plain 6-digit hex or it can never equal the contract.
      if (alphaByte === 255) return `#${hex}`.toUpperCase();
      return `#${hex}${alphaByte.toString(16).padStart(2, "0")}`.toUpperCase();
    };
    // "normal" is Chromium's keyword for "no extra spacing", i.e. 0.
    const zeroForNormal = (value) => (value === "normal" ? 0 : px(value));
    // A percentage radius resolves against the box, so it is not the same
    // contract as a pixel radius. Keep the raw string so the gate says so.
    const radius = (value) => (String(value).includes("%") ? value : px(value));
    const textAlign = (value) => {
      // Chromium reports the writing-direction-relative keywords for the
      // default value; these captures are all ltr.
      if (value === "start") return "left";
      if (value === "end") return "right";
      return value;
    };
    // Splits on top-level commas only, so commas inside rgb()/rgba() (used by
    // box-shadow, filter, and gradient color stops) don't fragment an entry.
    const splitTopLevel = (value) => {
      const parts = [];
      let depth = 0;
      let current = "";
      for (const char of value) {
        if (char === "(") depth++;
        if (char === ")") depth--;
        if (char === "," && depth === 0) {
          parts.push(current.trim());
          current = "";
        } else {
          current += char;
        }
      }
      if (current.trim()) parts.push(current.trim());
      return parts;
    };
    // Extracts the (paren-balanced) argument text of every `name(...)` call in
    // `text`, so a color function nested inside (e.g. drop-shadow(rgba(...))) )
    // doesn't truncate the match at its own closing paren.
    const extractFunctionArgs = (text, name) => {
      const results = [];
      const marker = `${name}(`;
      let index = 0;
      while ((index = text.indexOf(marker, index)) !== -1) {
        let depth = 1;
        const start = index + marker.length;
        let pos = start;
        while (pos < text.length && depth > 0) {
          if (text[pos] === "(") depth++;
          else if (text[pos] === ")") depth--;
          pos++;
        }
        results.push(text.slice(start, pos - 1));
        index = pos;
      }
      return results;
    };
    // Every custom property named by a value, fallback chains included:
    // var(--a, var(--b, #fff)) yields ["--a", "--b"]. extractFunctionArgs
    // consumes a nested var() along with its parent's argument text, so the
    // fallback tail is rescanned instead of being lost.
    const extractCustomProperties = (value) => {
      const names = [];
      for (const inner of extractFunctionArgs(value, "var")) {
        const [first, ...fallback] = splitTopLevel(inner);
        const name = (first ?? "").trim();
        if (name.startsWith("--")) names.push(name);
        const rest = fallback.join(",");
        if (rest.includes("var(")) names.push(...extractCustomProperties(rest));
      }
      return names;
    };
    // Splits a declaration block into [property, value] pairs as authored.
    // getComputedStyle would resolve the var() away and expand shorthands, so
    // the raw cssText is the only place "background: var(--x)" still exists.
    const parseDeclarations = (cssText) => {
      const declarations = [];
      let depth = 0;
      let quote = null;
      let current = "";
      const flush = () => {
        const text = current.trim();
        current = "";
        const colon = text.indexOf(":");
        if (colon <= 0) return;
        const rawProperty = text.slice(0, colon).trim();
        const value = text.slice(colon + 1).trim();
        // CSS property names are case-insensitive, custom property names are not.
        const property = rawProperty.startsWith("--")
          ? rawProperty
          : rawProperty.toLowerCase();
        if (property && value) declarations.push([property, value]);
      };
      for (const char of cssText) {
        if (quote) {
          current += char;
          if (char === quote) quote = null;
          continue;
        }
        if (char === '"' || char === "'") {
          quote = char;
          current += char;
          continue;
        }
        if (char === "(") depth++;
        else if (char === ")") depth = Math.max(0, depth - 1);
        if (char === ";" && depth === 0) {
          flush();
          continue;
        }
        current += char;
      }
      flush();
      return declarations;
    };
    // Flatten every accessible style rule once, so each element only pays for
    // matches(). A cross-origin sheet throws on .cssRules; the capture must
    // report the tokens it can see rather than die on the ones it cannot.
    const collectStyleRules = () => {
      const rules = [];
      const walk = (container) => {
        let list = null;
        try {
          list = container.cssRules;
        } catch {
          return; // Cross-origin or otherwise unreadable sheet.
        }
        if (!list) return;
        for (const rule of list) {
          const selectorText = rule.selectorText;
          if (typeof selectorText === "string" && rule.style) {
            rules.push({
              selectorText,
              declarations: parseDeclarations(rule.style.cssText ?? ""),
            });
          }
          // @media/@supports/@layer wrappers, and CSS nesting inside a rule.
          if (rule.cssRules) walk(rule);
          // An @import'd sheet is not in document.styleSheets, so a token
          // defined behind one would look undeclared without this.
          if (rule.styleSheet) walk(rule.styleSheet);
        }
      };
      for (const sheet of document.styleSheets) walk(sheet);
      // Constructable stylesheets never appear in document.styleSheets; a
      // design system that ships its tokens this way is still on-system.
      for (const sheet of document.adoptedStyleSheets ?? []) walk(sheet);
      return rules;
    };
    // The declared design tokens themselves. A mapped custom property missing
    // here means the token system never defined it, which is a different
    // failure from an element that defined it and then bypassed it. Source
    // order wins, which is the cascade for a set of equal-specificity :root
    // rules.
    const collectRootCustomProperties = (rules) => {
      const declared = {};
      for (const rule of rules) {
        const isRootRule = splitTopLevel(rule.selectorText).some(
          (part) => part.trim() === ":root",
        );
        if (!isRootRule) continue;
        for (const [property, value] of rule.declarations) {
          if (property.startsWith("--")) declared[property] = value;
        }
      }
      return declared;
    };
    // Which custom properties an element's declarations reference, per CSS
    // property as authored. This is presence-of-reference across every
    // matching rule, not cascade-winner determination: no specificity is
    // resolved, so a token named by any matching rule counts as referenced.
    const collectTokenRefs = (node) => {
      const refs = {};
      const record = (declarations) => {
        for (const [property, value] of declarations) {
          if (!value.includes("var(")) continue;
          const names = extractCustomProperties(value);
          if (names.length === 0) continue;
          const bucket = (refs[property] ??= []);
          for (const name of names) {
            if (!bucket.includes(name)) bucket.push(name);
          }
        }
      };
      record(parseDeclarations(node.style?.cssText ?? ""));
      for (const rule of styleRules) {
        let matched = false;
        try {
          matched = node.matches(rule.selectorText);
        } catch {
          matched = false; // Selector this engine cannot match (e.g. "&:hover").
        }
        if (matched) record(rule.declarations);
      }
      return refs;
    };
    // Chromium always serializes box-shadow/drop-shadow with the color first
    // (e.g. "rgba(0, 0, 0, 0.25) 0px 4px 8px 0px"), so the color can be lifted
    // off the front and the rest split on whitespace as plain lengths.
    const leadingColor = (text) => {
      const match = text.match(/^(rgba?\([^)]*\)|#[0-9a-fA-F]+|[a-zA-Z]+)/);
      if (!match) return { value: null, rest: text };
      return { value: color(match[1]), rest: text.slice(match[1].length).trim() };
    };
    const parseBoxShadow = (value) => {
      if (!value || value === "none") return [];
      return splitTopLevel(value).map((entry) => {
        let text = entry.trim();
        let inset = false;
        if (/^inset\b/.test(text)) {
          inset = true;
          text = text.replace(/^inset\s*/, "");
        }
        if (/\binset$/.test(text)) {
          inset = true;
          text = text.replace(/\s*inset$/, "");
        }
        const { value: shadowColor, rest } = leadingColor(text);
        const lengths = rest.split(/\s+/).filter(Boolean).map(px);
        const [offsetX = 0, offsetY = 0, blurRadius = 0, spreadRadius = 0] = lengths;
        return { offsetX, offsetY, blurRadius, spreadRadius, color: shadowColor ?? "#000000", inset };
      });
    };
    // CSS text-shadow has no spread and no inset, and Chromium serializes it
    // with the color first exactly like box-shadow.
    const parseTextShadow = (value) => {
      if (!value || value === "none") return [];
      return splitTopLevel(value).map((entry) => {
        const { value: shadowColor, rest } = leadingColor(entry.trim());
        const lengths = rest.split(/\s+/).filter(Boolean).map(px);
        const [offsetX = 0, offsetY = 0, blurRadius = 0] = lengths;
        return { offsetX, offsetY, blurRadius, color: shadowColor ?? "#000000" };
      });
    };
    const parseFilterDropShadows = (value) => {
      if (!value || value === "none") return [];
      return extractFunctionArgs(value, "drop-shadow").map((inner) => {
        const { value: shadowColor, rest } = leadingColor(inner.trim());
        const lengths = rest.split(/\s+/).filter(Boolean).map(px);
        const [offsetX = 0, offsetY = 0, blurRadius = 0] = lengths;
        return { offsetX, offsetY, blurRadius, color: shadowColor ?? "#000000" };
      });
    };
    // Only the simple `<angle>` / `to <side>` forms are resolved to a single
    // angleDeg; corner keywords like "to top right" have no exact single-angle
    // equivalent (it depends on the box's aspect ratio), so those are left null.
    const gradientAngleKeywords = { "to top": 0, "to right": 90, "to bottom": 180, "to left": 270 };
    const parseLinearGradient = (raw) => {
      const [inner] = extractFunctionArgs(raw, "linear-gradient");
      if (inner === undefined) return null;
      const parts = splitTopLevel(inner);
      if (parts.length === 0) return null;
      let angleDeg = 180; // CSS default gradient direction is "to bottom".
      let stopParts = parts;
      const first = parts[0].trim();
      if (/^-?\d+(\.\d+)?deg$/.test(first)) {
        angleDeg = Number.parseFloat(first);
        stopParts = parts.slice(1);
      } else if (first in gradientAngleKeywords) {
        angleDeg = gradientAngleKeywords[first];
        stopParts = parts.slice(1);
      } else if (first.startsWith("to ") || /^-?\d+(\.\d+)?(rad|grad|turn)$/.test(first)) {
        return null; // corner keywords / non-deg angle units: punt.
      }
      const stops = stopParts.map((stopText) => {
        const { value: stopColor, rest } = leadingColor(stopText.trim());
        const percentMatch = rest.match(/^(-?\d+(\.\d+)?)%/);
        const position = percentMatch ? Number.parseFloat(percentMatch[1]) / 100 : null;
        return { color: stopColor, position };
      });
      return { type: "linear", angleDeg, stops };
    };
    const parseBackgroundGradient = (backgroundImageRaw) => {
      if (!backgroundImageRaw) return null;
      const layers = splitTopLevel(backgroundImageRaw);
      if (layers.length !== 1 || !layers[0].startsWith("linear-gradient(")) return null;
      return parseLinearGradient(layers[0]);
    };
    const styleRules = collectStyleRules();
    const customProperties = collectRootCustomProperties(styleRules);
    const elements = {};
    const duplicates = [];
    const root = document.querySelector(`[data-node-id="${CSS.escape(screenNodeId)}"]`);
    if (!root) throw new Error(`Screen root data-node-id not found: ${screenNodeId}`);
    const rootRect = root.getBoundingClientRect();
    for (const node of document.querySelectorAll("[data-node-id]")) {
      const id = node.getAttribute("data-node-id");
      if (elements[id]) {
        duplicates.push(id);
        continue;
      }
      const rect = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      const copy = {
        textContent: node.children.length === 0 ? node.textContent.trim() : undefined,
        placeholder: node.getAttribute("placeholder") ?? undefined,
        alt: node.getAttribute("alt") ?? undefined,
        "aria-label": node.getAttribute("aria-label") ?? undefined,
        title: node.getAttribute("title") ?? undefined,
        value: "value" in node ? node.value : node.getAttribute("value") ?? undefined,
        // Which element this is, so a gate can tell whether `value` is what
        // the user reads. On an `<input>` it is; on a `<select>` the visible
        // text is the chosen option's label and `.value` is a code behind it;
        // on anything else a stray `value` attribute means nothing on screen.
        tag: node.tagName.toLowerCase(),
        selectedText:
          node.tagName === "SELECT" && node.selectedOptions?.length
            ? node.selectedOptions[0].textContent.trim()
            : undefined,
      };
      elements[id] = {
        text: copy.textContent,
        copy,
        rendered:
          (typeof node.checkVisibility !== "function" ||
            node.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) &&
          rect.width > 0 &&
          rect.height > 0 &&
          style.display !== "none" &&
          style.visibility !== "hidden" &&
          Number.parseFloat(style.opacity) > 0,
        rect: {
          x: rect.x - rootRect.x,
          y: rect.y - rootRect.y,
          width: rect.width,
          height: rect.height,
        },
        style: {
          fontFamily: style.fontFamily.split(",")[0].replace(/[\"']/g, "").trim(),
          fontSize: px(style.fontSize),
          fontWeight: Number.parseInt(style.fontWeight, 10) || style.fontWeight,
          lineHeight: Number((px(style.lineHeight) / px(style.fontSize)).toFixed(4)),
          letterSpacing: zeroForNormal(style.letterSpacing),
          color: color(style.color),
          // What the user actually sees on an empty field. The design draws
          // the prompt as an ordinary grey text node, but in the DOM it lives
          // in `::placeholder` -- `style.color` here is the colour the typed
          // value *would* take, which is not on screen yet. Captured only when
          // there is a placeholder, so nothing changes for other elements.
          placeholderColor: node.getAttribute("placeholder")
            ? color(getComputedStyle(node, "::placeholder").color)
            : undefined,
          // ...and everything else the prompt can override. Collecting only
          // the colour let a prompt drawn at 1px and zero opacity satisfy the
          // gate, because every property except `color` was still read off the
          // base input. These are the properties `::placeholder` accepts (the
          // `::first-line` set) and that this capture gates on; `textAlign` is
          // not among them, since it belongs to the block, not the prompt.
          placeholderStyle: node.getAttribute("placeholder")
            ? (() => {
                const prompt = getComputedStyle(node, "::placeholder");
                const size = px(prompt.fontSize);
                const leading = px(prompt.lineHeight);
                return {
                  color: color(prompt.color),
                  fontFamily: prompt.fontFamily
                    .split(",")[0]
                    .replace(/[\"']/g, "")
                    .trim(),
                  fontSize: size,
                  fontWeight:
                    Number.parseInt(prompt.fontWeight, 10) || prompt.fontWeight,
                  // Chromium reports `normal` here even when the
                  // `::placeholder` rule sets line-height explicitly, so on
                  // that engine this is simply not measurable (checked
                  // against a real browser). Reporting nothing says "not
                  // measured", which leaves the base value -- the leading
                  // that actually lays the field out -- in play. Dividing the
                  // keyword instead would have produced `null` by arithmetic
                  // accident rather than by decision, and other engines may
                  // yet report a real value here.
                  lineHeight:
                    typeof leading === "number" && typeof size === "number" && size
                      ? Number((leading / size).toFixed(4))
                      : undefined,
                  letterSpacing: zeroForNormal(prompt.letterSpacing),
                  opacity: Number.parseFloat(prompt.opacity),
                  textTransform: prompt.textTransform,
                  textDecorationLine: prompt.textDecorationLine,
                };
              })()
            : undefined,
          backgroundColor: color(style.backgroundColor),
          // borderColor stays the shorthand's first color for back-compat; the
          // per-side keys are what a four-color border can actually be gated on.
          borderColor: color(style.borderColor),
          borderTopColor: color(style.borderTopColor),
          borderRightColor: color(style.borderRightColor),
          borderBottomColor: color(style.borderBottomColor),
          borderLeftColor: color(style.borderLeftColor),
          borderRadius: radius(style.borderRadius),
          paddingTop: px(style.paddingTop),
          paddingRight: px(style.paddingRight),
          paddingBottom: px(style.paddingBottom),
          paddingLeft: px(style.paddingLeft),
          rowGap: zeroForNormal(style.rowGap),
          columnGap: zeroForNormal(style.columnGap),
          borderTopWidth: px(style.borderTopWidth),
          borderRightWidth: px(style.borderRightWidth),
          borderBottomWidth: px(style.borderBottomWidth),
          borderLeftWidth: px(style.borderLeftWidth),
          borderTopLeftRadius: radius(style.borderTopLeftRadius),
          borderTopRightRadius: radius(style.borderTopRightRadius),
          borderBottomRightRadius: radius(style.borderBottomRightRadius),
          borderBottomLeftRadius: radius(style.borderBottomLeftRadius),
          opacity: Number.parseFloat(style.opacity),
          // Not gated against Figma; they tell the structure gate whether a
          // node the design hides is actually invisible in the implementation.
          display: style.display,
          visibility: style.visibility,
          textAlign: textAlign(style.textAlign),
          textTransform: style.textTransform,
          textDecorationLine: style.textDecorationLine,
          boxShadow: parseBoxShadow(style.boxShadow),
          textShadow: parseTextShadow(style.textShadow),
          filterDropShadows: parseFilterDropShadows(style.filter),
          backgroundImageRaw: style.backgroundImage === "none" ? null : style.backgroundImage,
          backgroundGradient: parseBackgroundGradient(
            style.backgroundImage === "none" ? null : style.backgroundImage,
          ),
        },
        // Declared var() references. getComputedStyle has already substituted
        // them away in `style`, so this is the only evidence that separates a
        // design token from a hardcoded value that happens to match it. Always
        // present (possibly empty) so a gate can tell "referenced nothing"
        // from "captured by a build that never looked".
        tokenRefs: collectTokenRefs(node),
      };
    }
    const components = {};
    for (const node of document.querySelectorAll("[data-component]")) {
      components[node.getAttribute("data-component")] = { observed: true };
    }
    const assets = {};
    for (const node of document.querySelectorAll("[data-figma-asset]")) {
      assets[node.getAttribute("data-figma-asset")] = {
        sha256: node.getAttribute("data-asset-sha256"),
      };
    }
    return { elements, components, assets, duplicates, customProperties };
  }, screen.nodeId);
  const capturedScreen = {
    name: screen.name,
    route: screen.route,
    routeKind,
    screenshot: path.relative(outputDir, screenshot),
    // Recorded so the snapshot states the environment it was taken in; a
    // capture that needed an override should never look like a plain one.
    rootStyle,
  };
  if (routeKind === "product") {
    capturedScreen.slots = captured.slots;
    capturedScreen.duplicateSlots = captured.duplicates;
  } else {
    capturedScreen.elements = captured.elements;
    capturedScreen.assets = captured.assets;
    capturedScreen.duplicateNodeIds = captured.duplicates;
    capturedScreen.customProperties = captured.customProperties;
    Object.assign(snapshot.components, captured.components);
    // Screens can load different stylesheets, so each keeps its own set; the
    // union is what a gate falls back to when a screen declares none.
    Object.assign(snapshot.customProperties, captured.customProperties);
  }
  snapshot.screens[screen.nodeId] = capturedScreen;
}

await browser.close();
await fs.writeFile(outputPath, `${JSON.stringify(snapshot, null, 2)}\n`, "utf8");
console.log(JSON.stringify({ screens: Object.keys(snapshot.screens).length, output: outputPath }));
