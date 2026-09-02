"""Handing the design to someone who is going to build it.

The bundle is shaped for gates: a manifest, one JSON file per screen, elements
addressed by Figma node id and flattened into a list. Nobody can read that and
build from it, which left the harness with no answer for the most ordinary
thing anyone wants from a Figma file -- a spec to give an implementer.

`export-design` writes that spec, and these tests hold it to being a spec
rather than a dump: the tree is nested the way the design is, each string sits
on the node that shows it, a layer hidden in Figma is stated as "must not
render" instead of quietly disappearing, and everything the document leaves out
for readability is named in the document itself.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from figma_lossless.design_export import export_design
from figma_lossless.util import write_json


FEATURE_ID = "design-export"
SCREEN_ID = "1:1"
ROOT_ID = "1:2"
CHROME_ID = "1:3"
CHROME_TEXT_ID = "1:4"
LABEL_ID = "1:5"
HIDDEN_ID = "1:6"


def element(
    node_id: str,
    name: str,
    *,
    parent: str | None = None,
    kind: str = "FRAME",
    hidden: bool = False,
    rect: dict[str, Any] | None = None,
    style: dict[str, Any] | None = None,
    tokens: dict[str, str] | None = None,
) -> dict[str, Any]:
    compiled: dict[str, Any] = {
        "nodeId": node_id,
        "type": kind.lower(),
        "figmaType": kind,
        "name": name,
        "parentNodeId": parent,
        "hidden": hidden,
        "effectiveHidden": hidden,
        "rect": rect or {"x": 0, "y": 0, "width": 10, "height": 10},
        "style": dict(style or {}),
    }
    if tokens:
        compiled["resolvedTokens"] = dict(tokens)
    return compiled


class DesignExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self._write_bundle()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_bundle(self) -> None:
        screen = {
            "schemaVersion": "1.0",
            "nodeId": SCREEN_ID,
            "name": "Profile",
            "evidenceSource": "figma-rest",
            "viewport": {"width": 375, "height": 812},
            "texts": [
                {
                    "nodeId": LABEL_ID,
                    "value": "이름",
                    "property": "textContent",
                    "exact": True,
                },
                {
                    "nodeId": CHROME_TEXT_ID,
                    "value": "9:41",
                    "property": "textContent",
                    "exact": True,
                },
            ],
            "elements": [
                element(ROOT_ID, "Container", rect={
                    "x": 0, "y": 0, "width": 375, "height": 812,
                }, style={"backgroundColor": "#FFFFFF"}),
                element(CHROME_ID, "Status Bar", parent=ROOT_ID),
                element(CHROME_TEXT_ID, "Value", parent=CHROME_ID, kind="TEXT"),
                element(
                    LABEL_ID,
                    "Label",
                    parent=ROOT_ID,
                    kind="TEXT",
                    rect={"x": 16, "y": 60, "width": 100, "height": 20},
                    style={
                        "color": "#111111",
                        "fontSize": 14,
                        "paddingTop": 0,
                        "boxShadow": [],
                    },
                    tokens={"color": "color/text-primary"},
                ),
                element(HIDDEN_ID, "Legacy banner", parent=ROOT_ID, hidden=True),
            ],
            "assets": [],
            "variables": {},
            "status": {"contextFetched": True, "specCompiled": True},
        }
        write_json(self.bundle / "screens" / "1-1.json", screen)
        write_json(
            self.bundle / "manifest.json",
            {
                "schemaVersion": "1.0",
                "featureId": FEATURE_ID,
                "screens": [
                    {
                        "nodeId": SCREEN_ID,
                        "name": "Profile",
                        "compiledPath": "screens/1-1.json",
                    }
                ],
                "contracts": {"flowContract": None, "componentMap": None},
            },
        )

    def _markdown(self, **kwargs: Any) -> str:
        path = self.root / "spec.md"
        export_design(self.bundle, path, **kwargs)
        return path.read_text(encoding="utf-8")

    def _json(self, **kwargs: Any) -> dict[str, Any]:
        path = self.root / "spec.json"
        export_design(self.bundle, path, format="json", **kwargs)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_copy_sits_on_the_node_that_shows_it(self) -> None:
        lines = self._markdown().splitlines()
        label = next(index for index, line in enumerate(lines) if LABEL_ID in line)
        self.assertIn('text: "이름"', lines[label + 1])

    def test_the_tree_is_nested_the_way_the_design_is(self) -> None:
        lines = [
            line
            for line in self._markdown().splitlines()
            if line.lstrip().startswith("- **")
        ]
        indent = {
            node: len(line) - len(line.lstrip())
            for node, line in (
                (node, next(item for item in lines if node in item))
                for node in (ROOT_ID, CHROME_ID, CHROME_TEXT_ID)
            )
        }
        self.assertLess(indent[ROOT_ID], indent[CHROME_ID])
        self.assertLess(indent[CHROME_ID], indent[CHROME_TEXT_ID])

    def test_a_hidden_layer_is_stated_rather_than_dropped(self) -> None:
        """Dropping it would turn a requirement into an absence."""

        line = next(
            item
            for item in self._markdown().splitlines()
            if HIDDEN_ID in item
        )
        self.assertIn("must not render", line)

    def test_geometry_and_tokens_are_carried(self) -> None:
        document = self._markdown()
        self.assertIn("100×20 at (16, 60)", document)
        self.assertIn("(design token: color/text-primary)", document)

    def test_noise_is_omitted_and_the_omission_is_stated(self) -> None:
        document = self._markdown()
        self.assertNotIn("paddingTop", document)
        self.assertNotIn("boxShadow", document)
        self.assertIn("Omitted for readability", document)

    def test_all_properties_keeps_everything(self) -> None:
        document = self._markdown(all_properties=True)
        self.assertIn("paddingTop: 0", document)
        self.assertIn("boxShadow: []", document)
        self.assertNotIn("Omitted for readability", document)

    def test_excluding_a_subtree_removes_it_and_says_so(self) -> None:
        document = self._markdown(exclude_names=["Status Bar"])
        self.assertNotIn(CHROME_ID, document)
        self.assertNotIn(CHROME_TEXT_ID, document)
        self.assertNotIn('"9:41"', document)
        self.assertIn("Excluded from this spec: `Status Bar`", document)
        # The rest of the screen is untouched.
        self.assertIn(LABEL_ID, document)

    def test_the_json_export_omits_nothing(self) -> None:
        """The readable document trades completeness for legibility; this does not."""

        document = self._json()
        label = next(
            item
            for item in document["screens"][0]["elements"]
            if item["nodeId"] == LABEL_ID
        )
        self.assertEqual(label["style"]["paddingTop"], 0)
        self.assertEqual(label["style"]["boxShadow"], [])
        self.assertEqual(
            label["copy"], [{"property": "textContent", "value": "이름"}]
        )
        self.assertEqual(label["designTokens"], {"color": "color/text-primary"})
        hidden = next(
            item
            for item in document["screens"][0]["elements"]
            if item["nodeId"] == HIDDEN_ID
        )
        self.assertTrue(hidden["mustNotRender"])

    def test_selecting_screens(self) -> None:
        result = export_design(
            self.bundle, self.root / "one.md", screen_ids=[SCREEN_ID]
        )
        self.assertEqual(result["screens"], 1)

    def test_an_unknown_screen_id_is_an_error_not_an_empty_file(self) -> None:
        """A silent empty spec is how someone implements nothing and ships it."""

        with self.assertRaises(ValueError) as raised:
            export_design(
                self.bundle, self.root / "none.md", screen_ids=["9:9"]
            )
        self.assertIn("9:9", str(raised.exception))

    def test_an_unsupported_format_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            export_design(self.bundle, self.root / "spec.txt", format="txt")


if __name__ == "__main__":
    unittest.main()
