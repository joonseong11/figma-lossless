from __future__ import annotations

import base64
import html
import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .data_contract import (
    validate_data_contract,
    validate_data_contract_semantics,
)
from .slot_proposal import analyze_slot_candidates
from .util import (
    normalize_number,
    numeric,
    read_json,
    resolve_within,
    safe_slug,
    sha256_file,
    write_json,
)


NODE_LINE_PATTERN = re.compile(
    r'^(?P<indent>\s*)<(?P<type>[a-z-]+)\s+'
    r'id="(?P<id>[^"]+)"\s+name="(?P<name>[^"]*)"'
    r'(?P<attrs>[^>]*)>'
)
ATTR_PATTERN = re.compile(r'([a-zA-Z_-]+)="([^"]*)"')
DIRECT_FRAME_PATTERN = re.compile(
    r'^  <frame id="(?P<id>[^"]+)" name="(?P<name>[^"]*)"'
    r'(?P<attrs>[^>]*)>',
    re.MULTILINE,
)
ASSET_CONSTANT_PATTERN = re.compile(
    r'^const\s+(?P<variable>[A-Za-z0-9_$]+)\s*=\s*'
    r'"(?P<url>https://www\.figma\.com/api/mcp/asset/[^"]+)";',
    re.MULTILINE,
)
LITERAL_TEXT_PATTERN = re.compile(
    r'<(?P<tag>p|span|h[1-6]|label|button)\b'
    r'(?P<attrs>[^>]*)data-node-id="(?P<id>[^"]+)"'
    r'(?P<tail>[^>]*)>\s*(?P<text>[^<>{}]+?)\s*</(?P=tag)>',
    re.DOTALL,
)
OPENING_TAG_PATTERN = re.compile(
    r'<(?P<tag>[a-zA-Z][a-zA-Z0-9]*)\b(?P<attrs>[^>]*)'
    r'data-node-id="(?P<id>[^"]+)"(?P<tail>[^>]*)>',
    re.DOTALL,
)
CLASS_NAME_PATTERN = re.compile(r'className="([^"]*)"')
COPY_ATTRIBUTES = ("placeholder", "alt", "aria-label", "title", "value")
MAX_RAW_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_SCREENSHOT_BASE64_CHARS = 32 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 25_000_000

# Canonical (Figma REST) node properties this compiler reads to build the IR.
COMPILED_KEYS = frozenset(
    {
        "absoluteBoundingBox",
        "boundVariables",
        "characters",
        "children",
        "cornerRadius",
        "effects",
        "fills",
        "id",
        "individualStrokeWeights",
        "itemSpacing",
        "layoutMode",
        "name",
        "opacity",
        "paddingBottom",
        "paddingLeft",
        "paddingRight",
        "paddingTop",
        "rectangleCornerRadii",
        "strokeWeight",
        "strokes",
        "style",
        "type",
        "visible",
    }
)

# Canonical properties that stay in the preserved raw evidence without being
# compiled into the IR. Anything outside both registries is an accounting
# violation, so new Figma properties can never enter a bundle silently.
OPAQUE_KEYS = frozenset(
    {
        # geometry, transform and rasterization detail
        "absoluteRenderBounds",
        "arcData",
        "booleanOperation",
        "constraints",
        "cornerSmoothing",
        "fillGeometry",
        "fillOverrideTable",
        "preserveRatio",
        "relativeTransform",
        "rotation",
        "size",
        "strokeGeometry",
        "targetAspectRatio",
        "uniformScaleFactor",
        "vectorNetwork",
        "vectorPaths",
        # paint and stroke rendering detail
        "background",
        "backgroundColor",
        "blendMode",
        "complexStrokeProperties",
        "isMask",
        "isMaskOutline",
        "maskType",
        "strokeAlign",
        "strokeCap",
        "strokeDashes",
        "strokeJoin",
        "strokeMiterAngle",
        # prototyping and navigation
        "flowStartingPoints",
        "interactions",
        "overflowDirection",
        "prototypeBackgrounds",
        "prototypeDevice",
        "prototypeStartNodeID",
        "reactions",
        "transitionDuration",
        "transitionEasing",
        "transitionNodeID",
        # component and design-system references
        "componentId",
        "componentPropertyDefinitions",
        "componentPropertyReferences",
        "componentProperties",
        "componentSetId",
        "description",
        "documentationLinks",
        "exposedInstances",
        "isExposedInstance",
        "key",
        "overrides",
        "publishStatus",
        "remote",
        "styles",
        "variantProperties",
        # layout hints not expressed as CSS by this compiler
        "counterAxisAlignContent",
        "counterAxisAlignItems",
        "counterAxisSizingMode",
        "counterAxisSpacing",
        "itemReverseZIndex",
        "layoutAlign",
        "layoutGrids",
        "layoutGrow",
        "layoutPositioning",
        "layoutSizingHorizontal",
        "layoutSizingVertical",
        "layoutVersion",
        "layoutWrap",
        "maxHeight",
        "maxWidth",
        "minHeight",
        "minWidth",
        "primaryAxisAlignItems",
        "primaryAxisSizingMode",
        "strokesIncludedInLayout",
        # text detail beyond the compiled typography contract
        "characterStyleOverrides",
        "hyperlink",
        "lineIndentations",
        "lineTypes",
        "listSpacing",
        "maxLines",
        "paragraphIndent",
        "paragraphSpacing",
        "styleOverrideTable",
        "textTruncation",
        # container, workflow and plugin metadata
        "annotations",
        "clipsContent",
        "devStatus",
        "explicitVariableModes",
        "exportSettings",
        "isFixed",
        # star shape geometry (STAR nodes)
        "count",
        "starInnerScale",
        "measurements",
        "pluginData",
        "scrollBehavior",
        "sectionContentsHidden",
        "sharedPluginData",
    }
)

# Nested registries. A canonical property that lives inside `style`, a paint or
# an effect is just as capable of hiding a design decision as a top-level one,
# so every nested context gets the same compiled / preserved-opaque split and
# the same violation on an unrecognised key.
NESTED_KEYS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "style": (
        frozenset(
            {
                "fontFamily",
                "fontSize",
                "fontWeight",
                "letterSpacing",
                "lineHeightPx",
                "textAlignHorizontal",
                "textCase",
                "textDecoration",
            }
        ),
        frozenset(
            {
                "fontPostScriptName",
                "fontStyle",
                "fontVariant",
                "fontVariations",
                "hangingList",
                "hangingPunctuation",
                "hyperlink",
                "italic",
                "leadingTrim",
                "letterSpacingUnit",
                "lineHeightPercent",
                "lineHeightPercentFontSize",
                "lineHeightUnit",
                "listSpacing",
                "maxLines",
                "opentypeFlags",
                "paragraphIndent",
                "paragraphSpacing",
                "textAlignVertical",
                "textAutoResize",
                "textTruncation",
            }
        ),
    ),
    "fills": (
        frozenset(
            {
                "color",
                "gradientHandlePositions",
                "gradientStops",
                "opacity",
                "type",
                "visible",
            }
        ),
        frozenset(
            {
                "blendMode",
                "boundVariables",
                "filters",
                "gifRef",
                "imageRef",
                "imageTransform",
                "rotation",
                "scaleMode",
                "scalingFactor",
                "videoRef",
            }
        ),
    ),
    "effects": (
        frozenset(
            {
                "color",
                "offset",
                "radius",
                "spread",
                "type",
                "visible",
            }
        ),
        frozenset(
            {
                "blendMode",
                "boundVariables",
                "showShadowBehindNode",
            }
        ),
    ),
    "absoluteBoundingBox": (
        frozenset({"x", "y", "width", "height"}),
        frozenset(),
    ),
    "individualStrokeWeights": (
        frozenset({"top", "right", "bottom", "left"}),
        frozenset(),
    ),
}
_RGBA_KEYS = (frozenset({"r", "g", "b", "a"}), frozenset())
_POINT_KEYS = (frozenset({"x", "y"}), frozenset())
NESTED_KEYS["effects.color"] = _RGBA_KEYS
NESTED_KEYS["effects.offset"] = _POINT_KEYS
for _paints in ("fills", "strokes"):
    # Strokes are paints, so they share the paint registry and its subtree.
    NESTED_KEYS[_paints] = NESTED_KEYS["fills"]
    NESTED_KEYS[f"{_paints}.color"] = _RGBA_KEYS
    NESTED_KEYS[f"{_paints}.gradientHandlePositions"] = _POINT_KEYS
    NESTED_KEYS[f"{_paints}.gradientStops"] = (
        frozenset({"color", "position"}),
        frozenset({"boundVariables"}),
    )
    NESTED_KEYS[f"{_paints}.gradientStops.color"] = _RGBA_KEYS
# boundVariables is keyed by the design property being bound rather than by
# schema properties, so only the aliases underneath it are classified; a
# binding the mapper cannot compile is already reported as unsupported.
# Spacing properties whose value would have to be multiplied by an instance's
# uniform scale factor if REST reported them pre-scale.
AUTO_LAYOUT_NUMERIC_KEYS = frozenset(
    {
        "itemSpacing",
        "paddingTop",
        "paddingRight",
        "paddingBottom",
        "paddingLeft",
    }
)

FREE_KEY_CONTAINERS = frozenset({"boundVariables"})
NESTED_KEYS["boundVariables.*"] = (frozenset({"type", "id"}), frozenset())
# Children are nodes in their own right and are accounted for as they are
# walked, so descending into them here would double-count every property.
NODE_RECURSION_EXCLUDED = frozenset({"children"})

# blendMode only round-trips to CSS when it is the neutral Figma default.
OPAQUE_BLEND_MODES = frozenset({"NORMAL", "PASS_THROUGH"})
TEXT_ALIGN_TO_CSS = {
    "LEFT": "left",
    "CENTER": "center",
    "RIGHT": "right",
    "JUSTIFIED": "justify",
}
TEXT_CASE_TO_CSS = {
    "ORIGINAL": "none",
    "UPPER": "uppercase",
    "LOWER": "lowercase",
    "TITLE": "capitalize",
}
TEXT_DECORATION_TO_CSS = {
    "NONE": "none",
    "UNDERLINE": "underline",
    "STRIKETHROUGH": "line-through",
}
SHADOW_EFFECT_INSET = {"DROP_SHADOW": False, "INNER_SHADOW": True}
# Node types whose fill renders as a CSS background rather than SVG paint.
CONTAINER_FILL_TYPES = frozenset(
    {
        "COMPONENT",
        "COMPONENT_SET",
        "ELLIPSE",
        "FRAME",
        "GROUP",
        "INSTANCE",
        "RECTANGLE",
        "SECTION",
    }
)
CORNER_RADIUS_KEYS = (
    "borderTopLeftRadius",
    "borderTopRightRadius",
    "borderBottomRightRadius",
    "borderBottomLeftRadius",
)
STROKE_WIDTH_KEYS = {
    "top": "borderTopWidth",
    "right": "borderRightWidth",
    "bottom": "borderBottomWidth",
    "left": "borderLeftWidth",
}
# Figma has one stroke paint per node, so all four CSS sides carry that color.
# Per-side stroke paints do not exist in the canonical schema.
STROKE_COLOR_KEYS = (
    "borderColor",
    "borderTopColor",
    "borderRightColor",
    "borderBottomColor",
    "borderLeftColor",
)
# A container that declares fills but paints none is transparent by design, and
# that is a contract: the browser reports rgba(0,0,0,0) for exactly this case.
TRANSPARENT = "#00000000"
PADDING_KEYS = {
    "paddingTop": "paddingTop",
    "paddingRight": "paddingRight",
    "paddingBottom": "paddingBottom",
    "paddingLeft": "paddingLeft",
}
# boundVariables keys whose style target does not depend on the node type.
STATIC_TOKEN_STYLE_KEYS = {
    "strokes": "borderColor",
    "cornerRadius": "borderRadius",
    "paddingTop": "paddingTop",
    "paddingRight": "paddingRight",
    "paddingBottom": "paddingBottom",
    "paddingLeft": "paddingLeft",
}


@dataclass(frozen=True)
class CompileOptions:
    feature_id: str
    input_dir: Path
    output_dir: Path
    flow_contract: Path | None = None
    component_map: Path | None = None
    data_contract: Path | None = None
    rest_input: Path | None = None
    variable_defs: Path | None = None


class DesignCompiler:
    def __init__(self, options: CompileOptions):
        self.options = options
        self.input_dir = options.input_dir.resolve()
        self.output_dir = options.output_dir.resolve()

    def compile(self) -> dict[str, Any]:
        # Semantic validation can fail before a replacement manifest is
        # written. Remove the previous entry point up front so yesterday's
        # bundle cannot remain consumable after a failed recompile.
        (self.output_dir / "manifest.json").unlink(missing_ok=True)
        data_contract = None
        if self.options.data_contract:
            data_contract = validate_data_contract(
                read_json(self.options.data_contract)
            )
        raw_files = sorted(
            self.input_dir.glob("*.get-design-context.raw.json")
        )
        if not raw_files and self.options.rest_input is None:
            raise ValueError(
                f"No *.get-design-context.raw.json files in {self.input_dir}"
            )

        for raw_file in raw_files:
            self._validate_raw_capture(raw_file)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        screens_dir = self.output_dir / "screens"
        references_dir = self.output_dir / "references"
        screens_dir.mkdir(parents=True, exist_ok=True)
        references_dir.mkdir(parents=True, exist_ok=True)

        raw_index: list[dict[str, Any]] = []
        root_inventory: list[dict[str, Any]] = []
        compiled_screens: list[dict[str, Any]] = []
        global_assets: list[dict[str, Any]] = []

        for raw_file in raw_files:
            response = read_json(raw_file)
            content = response.get("content", [])
            text_blocks = [
                item.get("text", "")
                for item in content
                if item.get("type") == "text"
            ]
            image_blocks = [
                item for item in content if item.get("type") == "image"
            ]
            first_text = text_blocks[0] if text_blocks else ""

            raw_index.append(
                {
                    "path": raw_file.name,
                    "sha256": sha256_file(raw_file),
                    "bytes": raw_file.stat().st_size,
                    "contentTypes": [
                        item.get("type", "unknown") for item in content
                    ],
                }
            )

            if not image_blocks and "<section " in first_text:
                root_inventory.extend(self._parse_direct_frames(first_text))
                continue

            screen = self._compile_screen(
                raw_file=raw_file,
                response=response,
                text_blocks=text_blocks,
                image_blocks=image_blocks,
                references_dir=references_dir,
            )
            compiled_screens.append(screen)

        canonical_screens: list[dict[str, Any]] = []
        canonical_only_ids: list[str] = []
        accounting: dict[str, Any] | None = None
        if self.options.rest_input is not None:
            mapper = CanonicalMapper(
                self.options.rest_input,
                variable_defs=self._load_variable_defs(),
            )
            canonical_screens = mapper.map_screens()
            canonical_only_ids = self._merge_canonical_screens(
                compiled_screens, canonical_screens
            )
            self._wire_canonical_reference_screenshots(
                compiled_screens, references_dir
            )
            accounting = build_accounting_summary(
                self.options.feature_id, canonical_screens, mapper.collection
            )

        contract_warnings: list[dict[str, Any]] = []
        if data_contract is not None:
            slot_analysis = analyze_slot_candidates(compiled_screens)
            contract_warnings = validate_data_contract_semantics(
                data_contract,
                compiled_screens,
                slot_analysis["proposals"],
            )

        for screen in compiled_screens:
            screen_path = screens_dir / f"{safe_slug(screen['nodeId'])}.json"
            write_json(screen_path, screen)
            screen["compiledPath"] = str(screen_path.relative_to(self.output_dir))
            global_assets.extend(screen["assets"])

        if not root_inventory:
            root_inventory = [
                {
                    "nodeId": screen["nodeId"],
                    "name": screen["name"],
                    "x": None,
                    "y": None,
                    "width": screen["viewport"].get("width"),
                    "height": screen["viewport"].get("height"),
                }
                for screen in compiled_screens
            ]

        screen_by_id = {screen["nodeId"]: screen for screen in compiled_screens}
        inventory_ids = {item["nodeId"] for item in root_inventory}
        if accounting is not None:
            # A screen the collector never brought back must still be listed, or
            # a failed collection would look like a smaller feature.
            for node_id in accounting["restCollection"]["missingNodeIds"]:
                if node_id in inventory_ids or node_id in screen_by_id:
                    continue
                inventory_ids.add(node_id)
                root_inventory.append(
                    {
                        "nodeId": node_id,
                        "name": None,
                        "x": None,
                        "y": None,
                        "width": None,
                        "height": None,
                    }
                )
        for node_id in canonical_only_ids:
            # A canonically collected root that no MCP section listed still has
            # to appear in the coverage ledger, or gates would never see it.
            if node_id in inventory_ids:
                continue
            screen = screen_by_id[node_id]
            root_inventory.append(
                {
                    "nodeId": node_id,
                    "name": screen["name"],
                    "x": None,
                    "y": None,
                    "width": screen["viewport"].get("width"),
                    "height": screen["viewport"].get("height"),
                }
            )
        coverage = []
        for item in root_inventory:
            node_id = item["nodeId"]
            context_fetched = node_id in screen_by_id
            spec_compiled = bool(
                context_fetched
                and screen_by_id[node_id]["status"]["specCompiled"]
            )
            coverage.append(
                {
                    **item,
                    "status": {
                        "discovered": True,
                        "contextFetched": context_fetched,
                        "specCompiled": spec_compiled,
                        "implemented": False,
                        "structurePassed": False,
                        "copyPassed": False,
                        "assetPassed": False,
                        "visualPassed": False,
                        "flowPassed": False,
                    },
                }
            )

        copied_contracts = self._copy_optional_contracts()
        manifest = {
            "schemaVersion": "1.0",
            "featureId": self.options.feature_id,
            "sourceDirectory": self.input_dir.name,
            "counts": {
                "discoveredScreens": len(coverage),
                "detailedContexts": len(compiled_screens),
                "missingContexts": sum(
                    1
                    for item in coverage
                    if not item["status"]["contextFetched"]
                ),
                "assets": len(global_assets),
                "exactTexts": sum(
                    len(screen["texts"]) for screen in compiled_screens
                ),
                "unparsedTextNodes": sum(
                    len(
                        screen.get("extractionAccounting", {}).get(
                            "unparsedTextNodeIds", []
                        )
                    )
                    for screen in compiled_screens
                ),
            },
            "screens": [
                {
                    "nodeId": screen["nodeId"],
                    "name": screen["name"],
                    "compiledPath": screen["compiledPath"],
                }
                for screen in compiled_screens
            ],
            "contracts": copied_contracts,
        }
        if data_contract is not None:
            manifest["warnings"] = contract_warnings
        if accounting is not None:
            manifest["counts"]["canonicalScreens"] = len(canonical_screens)
            manifest["counts"]["accountingViolations"] = accounting["counts"][
                "violations"
            ]
            write_json(
                self.output_dir / "property-accounting.json", accounting
            )
            manifest["propertyAccounting"] = {
                "path": "property-accounting.json",
                "counts": accounting["counts"],
                "violations": accounting["violations"],
            }

        write_json(self.output_dir / "coverage.json", coverage)
        write_json(self.output_dir / "assets-manifest.json", global_assets)
        write_json(self.output_dir / "raw-index.json", raw_index)
        # Publish the bundle entry point only after every artifact it names is
        # durable. Any supporting-write failure therefore leaves no consumable
        # manifest, and the next run starts from the same explicit state.
        write_json(self.output_dir / "manifest.json", manifest)

        return manifest

    def _compile_screen(
        self,
        raw_file: Path,
        response: dict[str, Any],
        text_blocks: list[str],
        image_blocks: list[dict[str, Any]],
        references_dir: Path,
    ) -> dict[str, Any]:
        prefix = raw_file.name.replace(
            ".get-design-context.raw.json", ""
        )
        reference_path = self.input_dir / f"{prefix}.reference.tsx"
        reference = (
            reference_path.read_text(encoding="utf-8")
            if reference_path.exists()
            else (text_blocks[0] if text_blocks else "")
        )

        metadata_path = self.input_dir / f"{prefix}.metadata.raw.json"
        variable_path = self.input_dir / f"{prefix}.variable-defs.raw.json"
        assets_path = self.input_dir / f"{prefix}.download-assets.raw.json"
        metadata_text = self._all_text(metadata_path)
        metadata_nodes = self._parse_metadata_nodes(metadata_text)
        node_id, name = self._find_identity(metadata_nodes, reference, prefix)

        reference_output = references_dir / f"{safe_slug(node_id)}.tsx"
        reference_output.write_text(reference, encoding="utf-8")

        screenshot_output = references_dir / f"{safe_slug(node_id)}.png"
        if image_blocks:
            screenshot_output.write_bytes(
                base64.b64decode(image_blocks[0].get("data", ""), validate=True)
            )
        else:
            existing = self.input_dir / f"{prefix}.screenshot.png"
            if existing.exists():
                shutil.copy2(existing, screenshot_output)

        viewport = {"width": None, "height": None}
        if screenshot_output.exists() and screenshot_output.stat().st_size:
            with Image.open(screenshot_output) as image:
                viewport = {"width": image.width, "height": image.height}

        class_by_node = self._parse_node_classes(reference)
        elements = self._compile_elements(
            metadata_nodes=metadata_nodes,
            class_by_node=class_by_node,
            root_node_id=node_id,
        )
        texts = self._parse_literal_texts(reference)
        extracted_text_ids = {item["nodeId"] for item in texts}
        metadata_text_ids = {
            item["nodeId"]
            for item in elements
            if item["type"] == "text" and not item.get("effectiveHidden")
        }
        unparsed_text_ids = sorted(metadata_text_ids - extracted_text_ids)
        extraction_accounting = {
            "visibleMetadataTextNodes": len(metadata_text_ids),
            "extractedCopyRecords": len(texts),
            "unparsedTextNodeIds": unparsed_text_ids,
            "textEvidenceComplete": not unparsed_text_ids,
        }
        assets = self._parse_assets(
            reference=reference,
            screen_node_id=node_id,
            download_assets_path=assets_path,
        )
        variables = self._parse_json_text_payload(variable_path)
        evidence_paths = [
            raw_file,
            reference_path,
            metadata_path,
            variable_path,
            assets_path,
        ]
        evidence = [
            {
                "path": path.name,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            for path in evidence_paths
            if path.exists()
        ]

        return {
            "schemaVersion": "1.0",
            "nodeId": node_id,
            "name": name,
            "viewport": viewport,
            "referenceScreenshot": (
                str(screenshot_output.relative_to(self.output_dir))
                if screenshot_output.exists()
                else None
            ),
            "referenceCode": str(reference_output.relative_to(self.output_dir)),
            "texts": texts,
            "elements": elements,
            "assets": assets,
            "variables": variables,
            "extractionAccounting": extraction_accounting,
            "contextBlocks": [
                {
                    "index": index,
                    "type": item.get("type"),
                    "chars": len(item.get("text", item.get("data", ""))),
                    "mimeType": item.get("mimeType"),
                }
                for index, item in enumerate(response.get("content", []))
            ],
            "rawEvidence": evidence,
            "status": {
                "contextFetched": True,
                "specCompiled": not unparsed_text_ids,
                "implemented": False,
                "structurePassed": False,
                "copyPassed": False,
                "assetPassed": False,
                "visualPassed": False,
                "flowPassed": False,
            },
        }

    def _validate_raw_capture(self, raw_file: Path) -> None:
        if raw_file.stat().st_size > MAX_RAW_RESPONSE_BYTES:
            raise ValueError(f"MCP response exceeds size limit: {raw_file.name}")
        response = read_json(raw_file)
        if response.get("isError"):
            raise ValueError(f"MCP returned isError=true: {raw_file.name}")
        content = response.get("content")
        if not isinstance(content, list) or not content:
            raise ValueError(f"MCP response has no content: {raw_file.name}")
        text_blocks = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        image_blocks = [
            item
            for item in content
            if isinstance(item, dict) and item.get("type") == "image"
        ]
        first_text = text_blocks[0] if text_blocks else ""
        if not image_blocks and "<section " in first_text:
            if not self._parse_direct_frames(first_text):
                raise ValueError(
                    f"Section response contains no direct frames: {raw_file.name}"
                )
            return
        if not first_text or "data-node-id=" not in first_text:
            raise ValueError(
                f"Detailed response has no reference node IDs: {raw_file.name}"
            )
        if not image_blocks or not image_blocks[0].get("data"):
            raise ValueError(
                f"Detailed response has no reference screenshot: {raw_file.name}"
            )
        if len(image_blocks[0]["data"]) > MAX_SCREENSHOT_BASE64_CHARS:
            raise ValueError(
                f"Detailed screenshot exceeds size limit: {raw_file.name}"
            )
        prefix = raw_file.name.replace(".get-design-context.raw.json", "")
        metadata_path = self.input_dir / f"{prefix}.metadata.raw.json"
        if not metadata_path.exists():
            raise ValueError(f"Metadata capture is missing: {metadata_path.name}")
        metadata_nodes = self._parse_metadata_nodes(self._all_text(metadata_path))
        if not metadata_nodes:
            raise ValueError(f"Metadata capture has no nodes: {metadata_path.name}")
        metadata_root_id = metadata_nodes[0]["nodeId"]
        if f'data-node-id="{metadata_root_id}"' not in first_text:
            raise ValueError(
                "Reference does not contain the metadata root node ID: "
                f"{raw_file.name}"
            )
        try:
            base64.b64decode(image_blocks[0]["data"], validate=True)
        except (KeyError, ValueError) as error:
            raise ValueError(
                f"Detailed screenshot is not valid base64: {raw_file.name}"
            ) from error

    def _parse_direct_frames(self, text: str) -> list[dict[str, Any]]:
        nodes = self._parse_metadata_nodes(text)
        if nodes:
            root_id = nodes[0]["nodeId"]
            direct = [
                node
                for node in nodes
                if node["type"] == "frame" and node["parentNodeId"] == root_id
            ]
            if direct:
                return [
                    {
                        "nodeId": node["nodeId"],
                        "name": node["name"],
                        "x": node["x"],
                        "y": node["y"],
                        "width": node["width"],
                        "height": node["height"],
                    }
                    for node in direct
                ]
        result = []
        for match in DIRECT_FRAME_PATTERN.finditer(text):
            attrs = dict(ATTR_PATTERN.findall(match.group("attrs")))
            result.append(
                {
                    "nodeId": match.group("id"),
                    "name": html.unescape(match.group("name")).strip(),
                    "x": normalize_number(numeric(attrs.get("x"))),
                    "y": normalize_number(numeric(attrs.get("y"))),
                    "width": normalize_number(numeric(attrs.get("width"))),
                    "height": normalize_number(numeric(attrs.get("height"))),
                }
            )
        return result

    def _all_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        response = read_json(path)
        return "\n".join(
            item.get("text", "")
            for item in response.get("content", [])
            if item.get("type") == "text"
        )

    def _parse_metadata_nodes(self, text: str) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        stack: list[dict[str, Any]] = []
        for line in text.splitlines():
            match = NODE_LINE_PATTERN.match(line)
            if not match:
                continue
            attrs = dict(ATTR_PATTERN.findall(match.group("attrs")))
            depth = len(match.group("indent")) // 2
            stack = stack[:depth]
            parent = stack[-1] if stack else None
            x = numeric(attrs.get("x")) or 0.0
            y = numeric(attrs.get("y")) or 0.0
            absolute_x = (parent.get("absoluteX", 0.0) if parent else 0.0) + x
            absolute_y = (parent.get("absoluteY", 0.0) if parent else 0.0) + y
            own_hidden = attrs.get("hidden") == "true"
            node = {
                "nodeId": match.group("id"),
                "type": match.group("type"),
                "name": html.unescape(match.group("name")).strip(),
                "parentNodeId": parent.get("nodeId") if parent else None,
                "x": normalize_number(x),
                "y": normalize_number(y),
                "width": normalize_number(numeric(attrs.get("width"))),
                "height": normalize_number(numeric(attrs.get("height"))),
                "absoluteX": absolute_x,
                "absoluteY": absolute_y,
                "hidden": own_hidden,
                "effectiveHidden": own_hidden
                or bool(parent and parent.get("effectiveHidden")),
            }
            nodes.append(node)
            if not line.rstrip().endswith("/>"):
                if len(stack) == depth:
                    stack.append(node)
                else:
                    stack[depth] = node
        return nodes

    def _find_identity(
        self,
        metadata_nodes: list[dict[str, Any]],
        reference: str,
        prefix: str,
    ) -> tuple[str, str]:
        if metadata_nodes:
            return metadata_nodes[0]["nodeId"], metadata_nodes[0]["name"]
        match = re.search(
            r'data-node-id="(?P<id>\d+:\d+)"\s+'
            r'data-name="(?P<name>[^"]+)"',
            reference,
        )
        if match:
            return match.group("id"), match.group("name")
        raise ValueError(f"Could not identify Figma root node for {prefix}")

    def _compile_elements(
        self,
        metadata_nodes: list[dict[str, Any]],
        class_by_node: dict[str, str],
        root_node_id: str,
    ) -> list[dict[str, Any]]:
        root = next(
            (node for node in metadata_nodes if node["nodeId"] == root_node_id),
            {"absoluteX": 0.0, "absoluteY": 0.0},
        )
        root_x = float(root.get("absoluteX", 0.0))
        root_y = float(root.get("absoluteY", 0.0))
        elements = []
        for node in metadata_nodes:
            class_name = class_by_node.get(node["nodeId"], "")
            elements.append(
                {
                    "nodeId": node["nodeId"],
                    "type": node["type"],
                    "name": node["name"],
                    "parentNodeId": node["parentNodeId"],
                    "hidden": node["hidden"],
                    "effectiveHidden": node.get(
                        "effectiveHidden", node["hidden"]
                    ),
                    "rect": {
                        "x": normalize_number(
                            float(node["absoluteX"]) - root_x
                        ),
                        "y": normalize_number(
                            float(node["absoluteY"]) - root_y
                        ),
                        "width": node["width"],
                        "height": node["height"],
                    },
                    "className": class_name,
                    "style": self._parse_tailwind_style(class_name),
                }
            )
        return elements

    def _parse_node_classes(self, reference: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for match in OPENING_TAG_PATTERN.finditer(reference):
            attrs = match.group("attrs") + match.group("tail")
            class_match = CLASS_NAME_PATTERN.search(attrs)
            if class_match:
                result[match.group("id")] = class_match.group(1)
        return result

    def _parse_literal_texts(self, reference: str) -> list[dict[str, Any]]:
        texts = []
        seen = set()
        for match in LITERAL_TEXT_PATTERN.finditer(reference):
            value = html.unescape(match.group("text")).strip()
            if not value:
                continue
            key = (match.group("id"), value)
            if key in seen:
                continue
            seen.add(key)
            texts.append(
                {
                    "nodeId": match.group("id"),
                    "value": value,
                    "property": "textContent",
                    "exact": True,
                    "source": "figma",
                }
            )
        for match in OPENING_TAG_PATTERN.finditer(reference):
            attrs = dict(
                ATTR_PATTERN.findall(match.group("attrs") + match.group("tail"))
            )
            for property_name in COPY_ATTRIBUTES:
                value = attrs.get(property_name)
                if not value or "{" in value or "}" in value:
                    continue
                value = html.unescape(value)
                key = (match.group("id"), property_name, value)
                if key in seen:
                    continue
                seen.add(key)
                texts.append(
                    {
                        "nodeId": match.group("id"),
                        "value": value,
                        "property": property_name,
                        "exact": True,
                        "source": "figma",
                    }
                )
        return texts

    def _parse_tailwind_style(self, class_name: str) -> dict[str, Any]:
        style: dict[str, Any] = {}
        patterns = {
            "fontSize": r'(?:^|\s)text-\[(-?[0-9.]+)px\]',
            "letterSpacing": r'(?:^|\s)tracking-\[(-?[0-9.]+)px\]',
            "lineHeight": r'(?:^|\s)leading-\[(-?[0-9.]+)\]',
            "borderRadius": r'(?:^|\s)rounded-\[([0-9.]+)px\]',
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, class_name)
            if match:
                style[key] = float(match.group(1))

        font_match = re.search(r"font-\['([^']+)'\]", class_name)
        if font_match:
            family_style = font_match.group(1).split(":", 1)
            style["fontFamily"] = family_style[0]
            if len(family_style) > 1:
                inferred_weights = {
                    "Thin": 100,
                    "ExtraLight": 200,
                    "Light": 300,
                    "Regular": 400,
                    "Medium": 500,
                    "SemiBold": 600,
                    "Bold": 700,
                    "ExtraBold": 800,
                    "Black": 900,
                }
                inferred = inferred_weights.get(family_style[1])
                if inferred:
                    style["fontWeight"] = inferred

        weight_match = re.search(r'(?:^|\s)font-([1-9]00)(?:\s|$)', class_name)
        if weight_match:
            style["fontWeight"] = int(weight_match.group(1))

        color_patterns = [
            ("color", r'(?:^|\s)text-\[(#[0-9a-fA-F]{3,8})\]'),
            ("backgroundColor", r'(?:^|\s)bg-\[(#[0-9a-fA-F]{3,8})\]'),
            ("borderColor", r'(?:^|\s)border-\[(#[0-9a-fA-F]{3,8})\]'),
        ]
        for key, pattern in color_patterns:
            match = re.search(pattern, class_name)
            if match:
                style[key] = match.group(1).upper()

        if re.search(r'(?:^|\s)text-white(?:\s|$)', class_name):
            style["color"] = "#FFFFFF"
        if re.search(r'(?:^|\s)bg-white(?:\s|$)', class_name):
            style["backgroundColor"] = "#FFFFFF"
        return style

    def _parse_assets(
        self,
        reference: str,
        screen_node_id: str,
        download_assets_path: Path,
    ) -> list[dict[str, Any]]:
        downloaded_payload = self._parse_json_text_payload(download_assets_path)
        exported = (
            downloaded_payload.get("svgAssets", [])
            if isinstance(downloaded_payload, dict)
            else []
        )
        exported_by_url = {
            item.get("url"): item for item in exported if item.get("url")
        }
        assets = []
        for match in ASSET_CONSTANT_PATTERN.finditer(reference):
            url = match.group("url")
            payload = exported_by_url.get(url, {})
            assets.append(
                {
                    "screenNodeId": screen_node_id,
                    "variable": match.group("variable"),
                    "sourceUrl": url,
                    "format": payload.get("format", "svg"),
                    "sizeBytes": payload.get("sizeBytes"),
                    "localPath": None,
                    "sha256": None,
                    "source": "figma",
                    "exactRequired": True,
                }
            )
        return assets

    def _parse_json_text_payload(self, path: Path) -> Any:
        if not path.exists():
            return {}
        response = read_json(path)
        for item in response.get("content", []):
            if item.get("type") != "text":
                continue
            text = item.get("text", "").strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
        return {}

    def _load_variable_defs(self) -> Any:
        if self.options.variable_defs is None:
            return {}
        payload = read_json(self.options.variable_defs)
        # Accept a plain get_variable_defs payload or the MCP CallToolResult
        # envelope a capture step may have stored it in.
        if isinstance(payload, dict) and isinstance(payload.get("content"), list):
            for item in payload["content"]:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                try:
                    return json.loads(item.get("text", "").strip())
                except json.JSONDecodeError:
                    continue
            return {}
        return payload

    def _merge_canonical_screens(
        self,
        compiled_screens: list[dict[str, Any]],
        canonical_screens: list[dict[str, Any]],
    ) -> list[str]:
        screen_by_id = {
            screen["nodeId"]: screen for screen in compiled_screens
        }
        canonical_only: list[str] = []
        for canonical in canonical_screens:
            existing = screen_by_id.get(canonical["nodeId"])
            if existing is None:
                compiled_screens.append(canonical)
                screen_by_id[canonical["nodeId"]] = canonical
                canonical_only.append(canonical["nodeId"])
                continue
            merge_canonical_into_screen(existing, canonical)
        return canonical_only

    def _wire_canonical_reference_screenshots(
        self, compiled_screens: list[dict[str, Any]], references_dir: Path
    ) -> None:
        """Use a validated Figma REST render as the reference screenshot.

        Only screens with no screenshot yet are touched, so an MCP capture
        (always present on a screen that came through `_compile_screen`) is
        never replaced by a server render.
        """

        for screen in compiled_screens:
            if screen.get("referenceScreenshot"):
                continue
            image_evidence = screen.get("canonicalImageEvidence")
            if not image_evidence or not image_evidence.get("valid"):
                continue
            source_path = resolve_within(
                self.options.rest_input,
                image_evidence["path"],
                label="canonical image evidence",
            )
            target = references_dir / f"{safe_slug(screen['nodeId'])}.png"
            shutil.copy2(source_path, target)
            screen["referenceScreenshot"] = str(target.relative_to(self.output_dir))
            screen["referenceScreenshotProvenance"] = "figma-rest-render"

    def _copy_optional_contracts(self) -> dict[str, str | None]:
        contracts_dir = self.output_dir / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        result: dict[str, str | None] = {
            "flowContract": None,
            "componentMap": None,
            "dataContract": None,
        }
        if self.options.flow_contract:
            target = contracts_dir / "flow-contract.json"
            shutil.copy2(self.options.flow_contract, target)
            result["flowContract"] = str(target.relative_to(self.output_dir))
        if self.options.component_map:
            target = contracts_dir / "component-map.json"
            shutil.copy2(self.options.component_map, target)
            result["componentMap"] = str(target.relative_to(self.output_dir))
        if self.options.data_contract:
            target = contracts_dir / "data-contract.json"
            shutil.copy2(self.options.data_contract, target)
            result["dataContract"] = str(target.relative_to(self.output_dir))
        return result


def _holds_dicts(value: Any) -> bool:
    """Report whether a value has further canonical properties underneath it."""

    if isinstance(value, dict):
        return True
    return isinstance(value, list) and any(
        isinstance(item, dict) for item in value
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def channel_byte(value: float) -> int:
    """Quantise a 0..1 channel the way `Math.round` does in the adapter.

    Python's `round` is half-to-even, so 0.3 and 0.7 would land one byte below
    the browser's value and every translucent color would mismatch.
    """

    return max(0, min(255, math.floor(value * 255 + 0.5)))


def _channel_hex(value: float) -> str:
    return f"{channel_byte(value):02X}"


def rgba_hex(color: Any, opacity: float = 1.0) -> str | None:
    """Format a Figma color in the adapter's #RRGGBB / #RRGGBBAA vocabulary."""

    if not isinstance(color, dict):
        return None
    red = _number(color.get("r"))
    green = _number(color.get("g"))
    blue = _number(color.get("b"))
    if red is None or green is None or blue is None:
        return None
    own_alpha = _number(color.get("a"))
    alpha = (1.0 if own_alpha is None else own_alpha) * opacity
    value = f"#{_channel_hex(red)}{_channel_hex(green)}{_channel_hex(blue)}"
    # An alpha that quantises to 255 is opaque to the browser: Chromium
    # serializes it as rgb(), which the adapter renders as plain 6-digit hex.
    if channel_byte(alpha) >= 255:
        return value
    return f"{value}{_channel_hex(alpha)}"


def paint_hex(paint: dict[str, Any]) -> str | None:
    opacity = _number(paint.get("opacity"))
    return rgba_hex(paint.get("color"), 1.0 if opacity is None else opacity)


def has_positive_dimensions(width: Any, height: Any) -> bool:
    """Report whether a node box can carry a gradient conversion at all."""

    return (
        isinstance(width, (int, float))
        and isinstance(height, (int, float))
        and width > 0
        and height > 0
    )


def linear_gradient(
    paint: dict[str, Any], width: float, height: float
) -> dict[str, Any] | None:
    """Convert a Figma linear gradient into the CSS angle convention."""

    if not has_positive_dimensions(width, height):
        return None
    handles = paint.get("gradientHandlePositions")
    if not isinstance(handles, list) or len(handles) < 2:
        return None
    start, end = handles[0], handles[1]
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    # Handles are normalized to the node box, so the rendered direction only
    # matches the handle direction on a square node. Working in pixel space is
    # what makes a diagonal on a wide node flatten toward horizontal.
    start_x = (_number(start.get("x")) or 0.0) * width
    start_y = (_number(start.get("y")) or 0.0) * height
    end_x = (_number(end.get("x")) or 0.0) * width
    end_y = (_number(end.get("y")) or 0.0) * height
    delta_x = end_x - start_x
    delta_y = end_y - start_y
    if delta_x == 0.0 and delta_y == 0.0:
        return None
    # CSS measures gradient angles clockwise from "to top"; Figma handle space
    # has y growing downwards, so "to bottom" must come out as 180.
    angle = math.degrees(math.atan2(delta_x, -delta_y)) % 360.0
    radians = math.radians(angle)
    direction_x = math.sin(radians)
    direction_y = -math.cos(radians)
    # A CSS gradient line is centred on the box and long enough to cover it,
    # while Figma's handles set their own start and end. Positions therefore
    # have to be re-projected onto the CSS line, not copied across.
    line_length = width * abs(direction_x) + height * abs(direction_y)
    origin_x = width / 2 - direction_x * line_length / 2
    origin_y = height / 2 - direction_y * line_length / 2
    paint_opacity = _number(paint.get("opacity"))
    paint_opacity = 1.0 if paint_opacity is None else paint_opacity
    stops = []
    for stop in paint.get("gradientStops") or []:
        if not isinstance(stop, dict):
            continue
        offset = _number(stop.get("position")) or 0.0
        point_x = start_x + offset * delta_x
        point_y = start_y + offset * delta_y
        # Handles may extend past the box, so a converted position outside
        # [0, 1] is legitimate and CSS renders it.
        position = (
            (point_x - origin_x) * direction_x
            + (point_y - origin_y) * direction_y
        ) / line_length
        stops.append(
            {
                "color": rgba_hex(stop.get("color"), paint_opacity),
                "position": normalize_number(round(position, 4)),
            }
        )
    return {
        "type": "linear",
        "angleDeg": normalize_number(round(angle, 4)),
        "stops": stops,
    }


def alias_id(value: Any) -> str | None:
    """Return the first VARIABLE_ALIAS id inside a boundVariables entry."""

    if isinstance(value, dict):
        if value.get("type") == "VARIABLE_ALIAS" and value.get("id"):
            return str(value["id"])
        return None
    if isinstance(value, list):
        for item in value:
            found = alias_id(item)
            if found:
                return found
    return None


def build_variable_index(defs: Any) -> dict[str, str]:
    """Index get_variable_defs output by every key that could name a token."""

    entries: list[tuple[str, Any]] = []
    if isinstance(defs, dict):
        entries = [(str(key), value) for key, value in defs.items()]
    elif isinstance(defs, list):
        for item in defs:
            if isinstance(item, dict) and item.get("id"):
                entries.append((str(item["id"]), item))
    index: dict[str, str] = {}
    for key, value in entries:
        name = key
        if isinstance(value, dict):
            candidate = value.get("name")
            if isinstance(candidate, str) and candidate:
                name = candidate
        index.setdefault(key, name)
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            index.setdefault(value["id"], name)
    return index


def resolve_variable_name(index: dict[str, str], variable_id: str) -> str | None:
    if variable_id in index:
        return index[variable_id]
    if variable_id.startswith("VariableID:"):
        bare = variable_id.split(":", 1)[1]
        if bare in index:
            return index[bare]
    elif f"VariableID:{variable_id}" in index:
        return index[f"VariableID:{variable_id}"]
    return None


def non_rendering_reason(node: dict[str, Any], parent_is_boolean: bool) -> str | None:
    """Say why Figma paints nothing for this node, or None if it does paint.

    Both cases here are nodes Figma reports with `absoluteRenderBounds: null`:
    they carry a fill and a box, but that paint never reaches the canvas on its
    own. Contracting them as rendered elements would demand phantom rectangles
    the design never shows, so they are recorded as hidden -- which is a hard
    assertion that the implementation must *not* render them, not an exemption.
    """

    if node.get("visible") is False:
        return "designer-hidden"
    # A leaf mask only clips its siblings. A mask that has children is a
    # different shape entirely (gradient-filled text, a clipped logo group):
    # its subtree is visible through the mask, so hiding it would delete real
    # copy from the contract without a trace.
    if node.get("isMask") is True and not node.get("children"):
        return "mask"
    # Only the boolean result renders; the operands describe how it is built.
    if parent_is_boolean:
        return "boolean-operand"
    return None


def walk_canonical_nodes(root: dict[str, Any]):
    """Yield (node, parentNodeId, hidden, effectiveHidden, hiddenReason)."""

    stack: list[tuple[dict[str, Any], str | None, bool, bool]] = [
        (root, None, False, False)
    ]
    while stack:
        node, parent_id, parent_hidden, parent_is_boolean = stack.pop()
        reason = non_rendering_reason(node, parent_is_boolean)
        hidden = reason is not None
        effective_hidden = hidden or parent_hidden
        yield node, parent_id, hidden, effective_hidden, reason
        children = node.get("children")
        if not isinstance(children, list):
            continue
        is_boolean = node.get("type") == "BOOLEAN_OPERATION"
        for child in reversed(children):
            if isinstance(child, dict):
                stack.append(
                    (child, node.get("id"), effective_hidden, is_boolean)
                )


class CanonicalMapper:
    """Compile canonical Figma REST node JSON into Design IR screens.

    Every top-level node property is classified as compiled, preserved-opaque
    or unknown, so a Figma schema addition surfaces as an accounting violation
    instead of disappearing from the contract.
    """

    def __init__(self, rest_input: Path, variable_defs: Any = None):
        self.rest_input = rest_input.resolve()
        self.variable_defs = (
            variable_defs if isinstance(variable_defs, (dict, list)) else {}
        )
        self.variable_index = build_variable_index(self.variable_defs)
        self.collection = read_json(self._manifest_path())

    def _manifest_path(self) -> Path:
        path = resolve_within(
            self.rest_input,
            "rest/collection-manifest.json",
            label="collection manifest",
        )
        if not path.exists():
            raise ValueError(
                f"No Figma REST collection manifest in {self.rest_input}"
            )
        return path

    def map_screens(self) -> list[dict[str, Any]]:
        per_node = self.collection.get("perNode") or {}
        screens = []
        for node_id in self.collection.get("collectedNodeIds", []):
            entry = per_node.get(node_id)
            if not isinstance(entry, dict) or not entry.get("path"):
                raise ValueError(
                    f"Collection manifest has no evidence path for {node_id}"
                )
            path = resolve_within(
                self.rest_input, entry["path"], label="canonical node evidence"
            )
            if not path.exists():
                raise ValueError(
                    f"Canonical node evidence is missing: {entry['path']}"
                )
            if path.stat().st_size > MAX_RAW_RESPONSE_BYTES:
                raise ValueError(
                    f"Canonical node evidence exceeds size limit: {entry['path']}"
                )
            digest = sha256_file(path)
            if entry.get("sha256") and entry["sha256"] != digest:
                raise ValueError(
                    "Canonical node evidence changed since collection: "
                    f"{entry['path']}"
                )
            payload = read_json(path)
            nodes = payload.get("nodes")
            document = None
            if isinstance(nodes, dict) and isinstance(nodes.get(node_id), dict):
                document = nodes[node_id].get("document")
            if not isinstance(document, dict):
                raise ValueError(
                    f"Canonical node evidence has no document for {node_id}"
                )
            screens.append(
                self._map_screen(
                    node_id,
                    document,
                    {
                        "path": entry["path"],
                        "sha256": digest,
                        "bytes": path.stat().st_size,
                        "fileKey": self.collection.get("fileKey"),
                        "fileName": self.collection.get("fileName"),
                        "fileVersion": self.collection.get("fileVersion"),
                    },
                    self._resolve_image_evidence(entry.get("image")),
                )
            )
        return screens

    def _resolve_image_evidence(self, image_entry: Any) -> dict[str, Any] | None:
        """Validate a collector-recorded rendered image without hard-failing.

        Node evidence integrity is mandatory (a hash mismatch raises). A
        rendered image is optional polish for the visual gate, so a missing
        or mismatched one is recorded as invalid and left unwired rather
        than treated as a broken bundle.
        """

        if not isinstance(image_entry, dict) or not image_entry.get("path"):
            return None
        try:
            path = resolve_within(
                self.rest_input,
                image_entry["path"],
                label="canonical image evidence",
            )
        except ValueError:
            return {
                "path": image_entry.get("path"),
                "valid": False,
                "reason": "unsafe path",
            }
        if not path.exists():
            return {"path": image_entry["path"], "valid": False, "reason": "file missing"}
        digest = sha256_file(path)
        if image_entry.get("sha256") and image_entry["sha256"] != digest:
            return {
                "path": image_entry["path"],
                "valid": False,
                "reason": "sha256 mismatch",
            }
        return {"path": image_entry["path"], "sha256": digest, "valid": True}

    def _map_screen(
        self,
        node_id: str,
        document: dict[str, Any],
        evidence: dict[str, Any],
        image_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        accounting: dict[str, Any] = {
            "nodes": 0,
            "compiled": set(),
            "preservedOpaque": set(),
            "unsupported": [],
            "violations": [],
            "unresolvedTokenBindings": [],
            "nonRenderingNodes": [],
            # Visible IMAGE fills the REST path detects but cannot fetch --
            # see `_detect_image_fills` for why "assets": [] below does not
            # mean "no images".
            "unverifiedImageFills": [],
        }
        root_box = document.get("absoluteBoundingBox")
        elements: list[dict[str, Any]] = []
        texts: list[dict[str, Any]] = []
        seen_copy: set[tuple[str, str]] = set()
        visible_text_ids: list[str] = []
        unparsed_text_ids: list[str] = []
        for (
            node,
            parent_id,
            hidden,
            effective_hidden,
            hidden_reason,
        ) in walk_canonical_nodes(document):
            accounting["nodes"] += 1
            if hidden_reason and hidden_reason != "designer-hidden":
                accounting["nonRenderingNodes"].append(
                    {
                        "nodeId": str(node.get("id")),
                        "name": node.get("name"),
                        "type": node.get("type"),
                        "reason": hidden_reason,
                    }
                )
            elements.append(
                self._map_element(
                    node,
                    parent_id,
                    hidden,
                    effective_hidden,
                    root_box,
                    accounting,
                    hidden_reason,
                )
            )
            if node.get("type") != "TEXT" or effective_hidden:
                continue
            element_id = str(node.get("id"))
            visible_text_ids.append(element_id)
            characters = node.get("characters")
            if not isinstance(characters, str):
                unparsed_text_ids.append(element_id)
                continue
            # The browser adapter reads trimmed textContent, so the contract
            # has to be trimmed too or every copy check fails on whitespace.
            value = characters.strip()
            if not value or (element_id, value) in seen_copy:
                continue
            seen_copy.add((element_id, value))
            texts.append(
                {
                    "nodeId": element_id,
                    "value": value,
                    "property": "textContent",
                    "exact": True,
                    "source": "figma",
                }
            )
        width = height = None
        if isinstance(root_box, dict):
            width = normalize_number(_number(root_box.get("width")))
            height = normalize_number(_number(root_box.get("height")))
        property_accounting = {
            "nodes": accounting["nodes"],
            "compiled": sorted(accounting["compiled"]),
            "preservedOpaque": sorted(accounting["preservedOpaque"]),
            "unsupported": accounting["unsupported"],
            "violations": accounting["violations"],
            "unresolvedTokenBindings": accounting["unresolvedTokenBindings"],
            # Nodes the compiler judged to paint nothing. Recording them keeps
            # that inference reviewable instead of leaving it implicit in a
            # hidden flag.
            "nonRenderingNodes": accounting["nonRenderingNodes"],
            "accountingComplete": not accounting["violations"],
            "unverifiedImageFills": accounting["unverifiedImageFills"],
        }
        return {
            "schemaVersion": "1.0",
            "nodeId": node_id,
            "name": document.get("name", ""),
            "evidenceSource": "figma-rest",
            "viewport": {"width": width, "height": height},
            "referenceScreenshot": None,
            "referenceScreenshotProvenance": None,
            "referenceCode": None,
            "texts": texts,
            "elements": elements,
            # The REST collector never fetches fill image content (see
            # `_detect_image_fills`), so this is always empty -- do not read
            # it as "no raster assets"; check propertyAccounting's
            # `unverifiedImageFills` instead.
            "assets": [],
            "variables": self.variable_defs,
            "extractionAccounting": {
                "visibleMetadataTextNodes": len(visible_text_ids),
                "extractedCopyRecords": len(texts),
                "unparsedTextNodeIds": unparsed_text_ids,
                "textEvidenceComplete": not unparsed_text_ids,
            },
            "propertyAccounting": property_accounting,
            "canonicalEvidence": evidence,
            "canonicalImageEvidence": image_evidence,
            "rawEvidence": [
                {
                    "path": evidence["path"],
                    "sha256": evidence["sha256"],
                    "bytes": evidence["bytes"],
                }
            ],
            "status": {
                "contextFetched": True,
                "specCompiled": (
                    not unparsed_text_ids and not accounting["violations"]
                ),
                "implemented": False,
                "structurePassed": False,
                "copyPassed": False,
                "assetPassed": False,
                "visualPassed": False,
                "flowPassed": False,
            },
        }

    def _map_element(
        self,
        node: dict[str, Any],
        parent_id: str | None,
        hidden: bool,
        effective_hidden: bool,
        root_box: Any,
        accounting: dict[str, Any],
        hidden_reason: str | None = None,
    ) -> dict[str, Any]:
        node_id = str(node.get("id"))

        def unsupported(key: str, reason: str) -> None:
            accounting["unsupported"].append(
                {"nodeId": node_id, "key": key, "reason": reason}
            )

        self._classify_keys(node, node_id, accounting, unsupported)
        self._check_scaled_auto_layout(node, unsupported)
        self._detect_image_fills(node, node_id, accounting)
        style = self._map_style(node, unsupported)
        bindings, resolved = self._map_token_bindings(
            node, style, node_id, accounting, unsupported
        )
        element: dict[str, Any] = {
            "nodeId": node_id,
            "type": str(node.get("type", "")).lower(),
            "figmaType": node.get("type"),
            "name": node.get("name", ""),
            "parentNodeId": parent_id,
            "hidden": hidden,
            "effectiveHidden": effective_hidden,
            "rect": self._map_rect(node, root_box),
            "style": {} if effective_hidden else style,
        }
        if hidden_reason:
            # Without this the bundle cannot tell a layer the designer switched
            # off from one the compiler inferred paints nothing, so a wrong
            # inference would be indistinguishable from an intended hide.
            element["hiddenReason"] = hidden_reason
        if effective_hidden and style:
            # The style gate compares every element that carries a style, while
            # the structure and geometry gates skip hidden ones. Keeping hidden
            # properties out of the gated slot avoids demanding an
            # implementation for nodes no other gate expects to exist.
            element["hiddenStyle"] = style
        if bindings:
            element["tokenBindings"] = bindings
        if resolved:
            element["resolvedTokens"] = resolved
        return element

    def _map_rect(self, node: dict[str, Any], root_box: Any) -> dict[str, Any]:
        box = node.get("absoluteBoundingBox")
        if not isinstance(box, dict) or not isinstance(root_box, dict):
            return {"x": None, "y": None, "width": None, "height": None}
        origin_x = _number(root_box.get("x")) or 0.0
        origin_y = _number(root_box.get("y")) or 0.0
        box_x = _number(box.get("x"))
        box_y = _number(box.get("y"))
        return {
            "x": None if box_x is None else normalize_number(box_x - origin_x),
            "y": None if box_y is None else normalize_number(box_y - origin_y),
            "width": normalize_number(_number(box.get("width"))),
            "height": normalize_number(_number(box.get("height"))),
        }

    def _classify_keys(
        self,
        node: dict[str, Any],
        node_id: str,
        accounting: dict[str, Any],
        unsupported: Callable[[str, str], None],
    ) -> None:
        for key, value in node.items():
            if key in COMPILED_KEYS:
                accounting["compiled"].add(key)
                if key not in NODE_RECURSION_EXCLUDED and _holds_dicts(value):
                    self._classify_container(
                        value, key, key, node_id, accounting
                    )
            elif key in OPAQUE_KEYS:
                # An opaque key covers its whole subtree: the evidence is
                # preserved verbatim, so nothing below it can go missing.
                accounting["preservedOpaque"].add(key)
            else:
                accounting["violations"].append(
                    {
                        "nodeId": node_id,
                        "key": key,
                        "reason": "unknown canonical property",
                    }
                )
        blend_mode = node.get("blendMode")
        if isinstance(blend_mode, str) and blend_mode not in OPAQUE_BLEND_MODES:
            unsupported("blendMode", f"blend mode is not compiled: {blend_mode}")

    def _classify_container(
        self,
        value: Any,
        path: str,
        label: str,
        node_id: str,
        accounting: dict[str, Any],
    ) -> None:
        """Classify every dict reachable through a compiled property.

        `path` selects the registry, `label` is how the property reads in a
        defect. A compiled container with no registry is itself a violation, so
        a gap in these tables announces itself instead of letting a subtree
        through unaccounted.
        """

        def violation(key: str, reason: str) -> None:
            accounting["violations"].append(
                {"nodeId": node_id, "key": key, "reason": reason}
            )

        entries = value if isinstance(value, list) else [value]
        if isinstance(value, list):
            label = f"{label}[]"
        free_keys = path in FREE_KEY_CONTAINERS
        registry = NESTED_KEYS.get(path)
        if registry is None and not free_keys:
            violation(label, "compiled container has no property registry")
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for key, child in entry.items():
                qualified = f"{label}.{key}"
                if free_keys:
                    if _holds_dicts(child):
                        self._classify_container(
                            child, f"{path}.*", qualified, node_id, accounting
                        )
                    continue
                compiled, opaque = registry
                if key in opaque:
                    accounting["preservedOpaque"].add(qualified)
                    continue
                if key not in compiled:
                    violation(qualified, "unknown canonical property")
                    continue
                accounting["compiled"].add(qualified)
                if _holds_dicts(child):
                    self._classify_container(
                        child, f"{path}.{key}", qualified, node_id, accounting
                    )

    def _check_scaled_auto_layout(
        self, node: dict[str, Any], unsupported: Callable[[str, str], None]
    ) -> None:
        """Flag auto-layout numbers living under a scaled instance.

        Everything measured on a scaled instance -- box, fontSize, strokeWeight,
        cornerRadius -- comes back from REST already multiplied, which is why
        `uniformScaleFactor` is preserved opaquely rather than compiled. Spacing
        is the one family this run could not confirm, because no scaled instance
        here declares any. Rather than assume the same holds elsewhere, record
        it so a future file surfaces the question instead of quietly compiling a
        contract that may be off by the scale factor.
        """

        factor = node.get("uniformScaleFactor")
        if not isinstance(factor, (int, float)) or abs(factor - 1) < 1e-6:
            return
        stack = [node]
        while stack:
            current = stack.pop()
            if not isinstance(current, dict):
                continue
            for key in AUTO_LAYOUT_NUMERIC_KEYS:
                if isinstance(current.get(key), (int, float)):
                    unsupported(
                        key,
                        "auto-layout spacing under a scaled instance "
                        f"(uniformScaleFactor {factor}) is not verified to be "
                        "post-scale",
                    )
            children = current.get("children")
            if isinstance(children, list):
                stack.extend(children)

    def _detect_image_fills(
        self,
        node: dict[str, Any],
        node_id: str,
        accounting: dict[str, Any],
    ) -> None:
        """Record every visible IMAGE-type fill the REST path cannot verify.

        The REST client renders each node to a PNG for the reference
        screenshot, but never resolves a paint's `imageRef` to the source
        image bytes -- that needs a second call to the files/images endpoint
        plus a per-hash download, which this collector does not perform.
        Every canonical REST screen therefore compiles `"assets": []`
        unconditionally. Without this record that reads as "this screen has
        no raster assets" even when it has several, and the asset gate would
        silently check zero of them forever. Naming the node here is what
        keeps that gap visible instead of indistinguishable from a clean
        screen.
        """

        fills = node.get("fills")
        if not isinstance(fills, list):
            return
        for paint in fills:
            if not isinstance(paint, dict) or paint.get("visible") is False:
                continue
            if paint.get("type") != "IMAGE":
                continue
            accounting["unverifiedImageFills"].append(
                {"nodeId": node_id, "imageRef": paint.get("imageRef")}
            )

    def _map_style(
        self, node: dict[str, Any], unsupported: Callable[[str, str], None]
    ) -> dict[str, Any]:
        style: dict[str, Any] = {}
        self._map_typography(node, style, unsupported)
        self._map_fills(node, style, unsupported)
        self._map_strokes(node, style, unsupported)
        self._map_corner_radius(node, style, unsupported)
        self._map_auto_layout(node, style, unsupported)
        if "opacity" in node:
            value = _number(node.get("opacity"))
            if value is None:
                unsupported("opacity", "opacity is not a number")
            else:
                style["opacity"] = normalize_number(value)
        self._map_effects(node, style, unsupported)
        return style

    def _map_typography(
        self,
        node: dict[str, Any],
        style: dict[str, Any],
        unsupported: Callable[[str, str], None],
    ) -> None:
        if node.get("type") != "TEXT":
            return
        text_style = node.get("style")
        if not isinstance(text_style, dict):
            unsupported("style", "text node has no type style block")
            return
        family = text_style.get("fontFamily")
        if isinstance(family, str) and family:
            style["fontFamily"] = family
        font_size = _number(text_style.get("fontSize"))
        if font_size is not None:
            style["fontSize"] = normalize_number(font_size)
        weight = _number(text_style.get("fontWeight"))
        if weight is not None:
            style["fontWeight"] = int(weight)
        line_height_px = _number(text_style.get("lineHeightPx"))
        if line_height_px is not None and font_size:
            style["lineHeight"] = round(line_height_px / font_size, 4)
        elif font_size:
            unsupported(
                "style", "text style has no lineHeightPx to derive a ratio"
            )
        letter_spacing = _number(text_style.get("letterSpacing"))
        if letter_spacing is not None:
            style["letterSpacing"] = normalize_number(letter_spacing)
        text_case = text_style.get("textCase", "ORIGINAL")
        mapped_case = TEXT_CASE_TO_CSS.get(text_case)
        if mapped_case is None:
            unsupported("style", f"textCase has no CSS mapping: {text_case}")
        else:
            style["textTransform"] = mapped_case
        decoration = text_style.get("textDecoration", "NONE")
        mapped_decoration = TEXT_DECORATION_TO_CSS.get(decoration)
        if mapped_decoration is None:
            unsupported(
                "style", f"textDecoration has no CSS mapping: {decoration}"
            )
        else:
            style["textDecorationLine"] = mapped_decoration
        alignment = text_style.get("textAlignHorizontal", "LEFT")
        mapped_alignment = TEXT_ALIGN_TO_CSS.get(alignment)
        if mapped_alignment is None:
            unsupported(
                "style", f"textAlignHorizontal has no CSS mapping: {alignment}"
            )
        else:
            style["textAlign"] = mapped_alignment

    def _map_fills(
        self,
        node: dict[str, Any],
        style: dict[str, Any],
        unsupported: Callable[[str, str], None],
    ) -> None:
        fills = node.get("fills")
        if not isinstance(fills, list):
            if fills is not None:
                unsupported("fills", "fills is not a paint list")
            return
        node_type = str(node.get("type", ""))
        is_text = node_type == "TEXT"
        # Vector-like nodes paint their geometry, not a CSS box background.
        renders_as_css_box = is_text or node_type in CONTAINER_FILL_TYPES
        color_key = "color" if is_text else "backgroundColor"
        box = node.get("absoluteBoundingBox")
        box = box if isinstance(box, dict) else {}
        width = _number(box.get("width"))
        height = _number(box.get("height"))
        for paint in fills:
            if not isinstance(paint, dict) or paint.get("visible") is False:
                continue
            paint_type = paint.get("type")
            if not renders_as_css_box:
                unsupported(
                    "fills",
                    f"{node_type} paint is vector fill, not a CSS property",
                )
                continue
            if paint_type == "SOLID":
                value = paint_hex(paint)
                if value is None:
                    unsupported("fills", "solid paint has no usable color")
                    continue
                if color_key in style:
                    # Figma paints the fills array bottom-to-top, so a later
                    # visible solid covers the earlier one and is what the
                    # browser reports. The covered paint is still announced.
                    unsupported(
                        "fills",
                        "solid paint below the topmost fill is not compiled",
                    )
                style[color_key] = value
                continue
            if paint_type == "GRADIENT_LINEAR" and not is_text:
                if "backgroundGradient" in style:
                    unsupported(
                        "fills",
                        "linear gradient beyond the first is not compiled",
                    )
                    continue
                if not has_positive_dimensions(width, height):
                    # Both the angle and the stop extent are measured against
                    # the node box. Without one there is no honest answer, and
                    # a fabricated angle would gate an implementation wrongly.
                    unsupported(
                        "fills",
                        "gradient conversion requires positive node dimensions",
                    )
                    continue
                gradient = linear_gradient(paint, width, height)
                if gradient is None:
                    unsupported(
                        "fills", "linear gradient has no usable handle positions"
                    )
                    continue
                style["backgroundGradient"] = gradient
                continue
            unsupported("fills", f"paint type is not compiled: {paint_type}")
        if (
            renders_as_css_box
            and not is_text
            and color_key not in style
            and "backgroundGradient" not in style
        ):
            # Declaring fills and painting nothing visible is a decision, not an
            # absence: the implementation must stay transparent here.
            style[color_key] = TRANSPARENT

    def _map_strokes(
        self,
        node: dict[str, Any],
        style: dict[str, Any],
        unsupported: Callable[[str, str], None],
    ) -> None:
        strokes = node.get("strokes")
        if not isinstance(strokes, list):
            if strokes is not None:
                unsupported("strokes", "strokes is not a paint list")
            return
        visible = [
            paint
            for paint in strokes
            if isinstance(paint, dict) and paint.get("visible") is not False
        ]
        if not visible:
            return
        for paint in visible:
            if paint.get("type") != "SOLID":
                unsupported(
                    "strokes",
                    f"stroke paint type is not compiled: {paint.get('type')}",
                )
                continue
            if "borderColor" in style:
                unsupported(
                    "strokes",
                    "solid stroke beyond the first paint is not compiled",
                )
                continue
            value = paint_hex(paint)
            if value is None:
                unsupported("strokes", "solid stroke paint has no usable color")
                continue
            for key in STROKE_COLOR_KEYS:
                style[key] = value
        widths: dict[str, float] = {}
        uniform = _number(node.get("strokeWeight"))
        if uniform is not None:
            widths = {side: uniform for side in STROKE_WIDTH_KEYS}
        individual = node.get("individualStrokeWeights")
        if isinstance(individual, dict):
            for side in STROKE_WIDTH_KEYS:
                value = _number(individual.get(side))
                if value is not None:
                    widths[side] = value
        elif individual is not None:
            unsupported(
                "individualStrokeWeights",
                "expected top/right/bottom/left stroke widths",
            )
        if not widths:
            unsupported("strokeWeight", "visible stroke has no compiled width")
            return
        for side, key in STROKE_WIDTH_KEYS.items():
            if side in widths:
                style[key] = normalize_number(widths[side])

    def _map_corner_radius(
        self,
        node: dict[str, Any],
        style: dict[str, Any],
        unsupported: Callable[[str, str], None],
    ) -> None:
        uniform = _number(node.get("cornerRadius"))
        if uniform is not None:
            style["borderRadius"] = normalize_number(uniform)
            for key in CORNER_RADIUS_KEYS:
                style[key] = normalize_number(uniform)
        radii = node.get("rectangleCornerRadii")
        if isinstance(radii, list) and len(radii) == len(CORNER_RADIUS_KEYS):
            for key, value in zip(CORNER_RADIUS_KEYS, radii):
                number = _number(value)
                if number is None:
                    unsupported(
                        "rectangleCornerRadii", "corner radius is not a number"
                    )
                    continue
                style[key] = normalize_number(number)
        elif radii is not None:
            unsupported(
                "rectangleCornerRadii",
                "expected four corner radii in [TL, TR, BR, BL] order",
            )

    def _map_auto_layout(
        self,
        node: dict[str, Any],
        style: dict[str, Any],
        unsupported: Callable[[str, str], None],
    ) -> None:
        if "layoutMode" not in node:
            return
        layout_mode = node.get("layoutMode")
        if layout_mode not in {"VERTICAL", "HORIZONTAL"}:
            # Figma keeps padding and itemSpacing on nodes with auto layout
            # switched off, where they change nothing. Compiling them would
            # demand CSS the design does not actually specify.
            if "itemSpacing" in node:
                unsupported(
                    "itemSpacing",
                    f"itemSpacing has no CSS gap axis for layoutMode {layout_mode}",
                )
            return
        for key, source in PADDING_KEYS.items():
            style[key] = normalize_number(_number(node.get(source)) or 0.0)
        spacing = normalize_number(_number(node.get("itemSpacing")) or 0.0)
        if layout_mode == "VERTICAL":
            style["rowGap"] = spacing
        else:
            style["columnGap"] = spacing

    def _map_effects(
        self,
        node: dict[str, Any],
        style: dict[str, Any],
        unsupported: Callable[[str, str], None],
    ) -> None:
        if "effects" not in node:
            return
        effects = node.get("effects")
        if not isinstance(effects, list):
            unsupported("effects", "effects is not a list")
            return
        # A shadow on a TEXT node is CSS `text-shadow`, which has neither a
        # spread radius nor an inset form, so it is a different contract key.
        is_text = node.get("type") == "TEXT"
        shadows = []
        for effect in effects:
            if not isinstance(effect, dict) or effect.get("visible") is False:
                continue
            inset = SHADOW_EFFECT_INSET.get(effect.get("type"))
            if inset is None:
                unsupported(
                    "effects",
                    f"effect type is not compiled: {effect.get('type')}",
                )
                continue
            offset = effect.get("offset")
            offset = offset if isinstance(offset, dict) else {}
            color = rgba_hex(effect.get("color"))
            if color is None:
                unsupported("effects", "shadow effect has no usable color")
                continue
            spread = normalize_number(_number(effect.get("spread")) or 0.0)
            geometry = {
                "offsetX": normalize_number(_number(offset.get("x")) or 0.0),
                "offsetY": normalize_number(_number(offset.get("y")) or 0.0),
                "blurRadius": normalize_number(
                    _number(effect.get("radius")) or 0.0
                ),
            }
            if is_text:
                if inset:
                    unsupported(
                        "effects", "text-shadow cannot express an inner shadow"
                    )
                    continue
                if spread:
                    unsupported(
                        "effects", "text-shadow cannot express a spread radius"
                    )
                shadows.append({**geometry, "color": color})
                continue
            shadows.append(
                {
                    **geometry,
                    "spreadRadius": spread,
                    "color": color,
                    "inset": inset,
                }
            )
        style["textShadow" if is_text else "boxShadow"] = shadows

    def _map_token_bindings(
        self,
        node: dict[str, Any],
        style: dict[str, Any],
        node_id: str,
        accounting: dict[str, Any],
        unsupported: Callable[[str, str], None],
    ) -> tuple[dict[str, str], dict[str, str]]:
        source = node.get("boundVariables")
        if not isinstance(source, dict):
            if source is not None:
                unsupported("boundVariables", "boundVariables is not an object")
            return {}, {}
        bindings: dict[str, str] = {}
        resolved: dict[str, str] = {}
        for binding_key, value in source.items():
            variable_id = alias_id(value)
            if variable_id is None:
                unsupported(
                    "boundVariables",
                    f"binding for {binding_key} has no variable alias id",
                )
                continue
            style_key = self._token_style_key(binding_key, node)
            if style_key is None or style_key not in style:
                unsupported(
                    "boundVariables",
                    f"binding for {binding_key} has no compiled style key",
                )
                continue
            bindings[style_key] = variable_id
            name = resolve_variable_name(self.variable_index, variable_id)
            if name is None:
                accounting["unresolvedTokenBindings"].append(
                    {
                        "nodeId": node_id,
                        "styleKey": style_key,
                        "variableId": variable_id,
                    }
                )
            else:
                resolved[style_key] = name
        return bindings, resolved

    def _token_style_key(
        self, binding_key: str, node: dict[str, Any]
    ) -> str | None:
        if binding_key == "fills":
            return "color" if node.get("type") == "TEXT" else "backgroundColor"
        if binding_key == "itemSpacing":
            layout_mode = node.get("layoutMode")
            if layout_mode == "VERTICAL":
                return "rowGap"
            if layout_mode == "HORIZONTAL":
                return "columnGap"
            return None
        return STATIC_TOKEN_STYLE_KEYS.get(binding_key)


def merge_canonical_into_screen(
    screen: dict[str, Any], canonical: dict[str, Any]
) -> None:
    """Let canonical REST properties replace regex-derived MCP properties."""

    elements_by_id = {
        item["nodeId"]: item for item in screen.get("elements", [])
    }
    for element in canonical["elements"]:
        target = elements_by_id.get(element["nodeId"])
        if target is None:
            screen["elements"].append(element)
            continue
        for key, value in element.items():
            # className is MCP-only provenance and has no canonical counterpart.
            if key != "className":
                target[key] = value
    seen = {
        (
            item.get("nodeId"),
            item.get("property", "textContent"),
            item.get("value"),
        )
        for item in screen.get("texts", [])
    }
    for text in canonical["texts"]:
        key = (text["nodeId"], text["property"], text["value"])
        if key in seen:
            continue
        seen.add(key)
        screen["texts"].append(text)
    screen["propertyAccounting"] = canonical["propertyAccounting"]
    screen["canonicalEvidence"] = canonical["canonicalEvidence"]
    # Recorded for traceability even when an MCP screenshot already exists
    # and wins over it below — the compiler never overwrites a real capture.
    screen["canonicalImageEvidence"] = canonical.get("canonicalImageEvidence")
    screen["evidenceSource"] = "figma-mcp+figma-rest"
    if canonical.get("variables") and not screen.get("variables"):
        screen["variables"] = canonical["variables"]
    extraction = screen.setdefault("extractionAccounting", {})
    covered = {
        item["nodeId"]
        for item in screen["texts"]
        if item.get("property", "textContent") == "textContent"
    }
    unparsed = [
        node_id
        for node_id in extraction.get("unparsedTextNodeIds", [])
        if node_id not in covered
    ]
    extraction["extractedCopyRecords"] = len(screen["texts"])
    extraction["unparsedTextNodeIds"] = unparsed
    extraction["textEvidenceComplete"] = not unparsed
    screen["status"]["specCompiled"] = bool(
        not unparsed and not canonical["propertyAccounting"]["violations"]
    )


def build_accounting_summary(
    feature_id: str,
    canonical_screens: list[dict[str, Any]],
    collection: dict[str, Any],
) -> dict[str, Any]:
    compiled: set[str] = set()
    preserved: set[str] = set()
    unsupported: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    non_rendering: list[dict[str, Any]] = []
    unverified_image_fills: list[dict[str, Any]] = []
    nodes = 0
    for screen in canonical_screens:
        accounting = screen["propertyAccounting"]
        nodes += accounting["nodes"]
        compiled.update(accounting["compiled"])
        preserved.update(accounting["preservedOpaque"])
        for key, sink in (
            ("unsupported", unsupported),
            ("violations", violations),
            ("unresolvedTokenBindings", unresolved),
            ("nonRenderingNodes", non_rendering),
            ("unverifiedImageFills", unverified_image_fills),
        ):
            for item in accounting.get(key, []):
                sink.append({"screenNodeId": screen["nodeId"], **item})
    missing_image_node_ids = sorted(
        screen["nodeId"]
        for screen in canonical_screens
        if not (screen.get("canonicalImageEvidence") or {}).get("valid")
    )
    return {
        "schemaVersion": "1.0",
        "featureId": feature_id,
        "counts": {
            "screens": len(canonical_screens),
            "nodes": nodes,
            "compiledKeys": len(compiled),
            "preservedOpaqueKeys": len(preserved),
            "unsupported": len(unsupported),
            "violations": len(violations),
            "unresolvedTokenBindings": len(unresolved),
            "nonRenderingNodes": len(non_rendering),
            "unverifiedImageFills": len(unverified_image_fills),
        },
        "compiled": sorted(compiled),
        "preservedOpaque": sorted(preserved),
        "unsupported": unsupported,
        "violations": violations,
        "unresolvedTokenBindings": unresolved,
        "nonRenderingNodes": non_rendering,
        "unverifiedImageFills": unverified_image_fills,
        "restCollection": {
            "fileKey": collection.get("fileKey"),
            "fileName": collection.get("fileName"),
            "fileVersion": collection.get("fileVersion"),
            "complete": collection.get("complete"),
            "expectedNodeIds": collection.get("expectedNodeIds", []),
            "collectedNodeIds": collection.get("collectedNodeIds", []),
            "missingNodeIds": missing_node_ids(collection),
            "failures": collection.get("failures", []),
            "imagesComplete": not missing_image_node_ids,
            "missingImageNodeIds": missing_image_node_ids,
        },
    }


def missing_node_ids(collection: dict[str, Any]) -> list[str]:
    """Node ids the collector was asked for but never brought back."""

    expected = collection.get("expectedNodeIds") or []
    collected = collection.get("collectedNodeIds") or []
    return sorted({str(item) for item in expected} - {str(item) for item in collected})
