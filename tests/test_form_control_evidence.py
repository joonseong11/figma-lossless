"""What the copy and style gates read when a design's text becomes a real field.

A design draws an editable field as a static text node: the surname reads
"Lin", the password prompt reads "Current password", both in ordinary grey. An
implementation that satisfies "the user can change this" renders an `<input>`
instead, and in the DOM none of that text is where the gates were looking --
`textContent` is empty for an input, and `color` is the colour a typed value
*would* take rather than the grey prompt on screen.

Read literally, the gates then report the working implementation as broken:
the pilot that motivated these tests produced 43 copy and 16 style hard
failures the moment its static spans became fields the PRD had asked for. That
is a contract punishing behaviour, which is the failure this harness exists to
prevent rather than cause.

The capture already carried the answer -- `copy.value`, `copy.placeholder`, and
now `style.placeholderStyle`. These tests hold the gates to reading what is
actually on screen, and to still failing when the text or its appearance is
genuinely wrong.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from figma_lossless.util import write_json
from figma_lossless.validators import GateValidator, ValidateOptions

from canonical_bundle import attach_accounting, clean_accounting

FEATURE_ID = "form-controls"
SCREEN_ID = "1:1"
FIELD_ID = "2:2"
GREY = "#B3B3B3"
INK = "#111111"


class FormControlHarness(unittest.TestCase):
    """One screen, one contracted text node, rendered as whatever a test needs."""

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
        text: str,
        color: str = GREY,
        style: dict[str, Any] | None = None,
    ) -> None:
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
                "canonicalEvidence": {
                    "path": "rest/nodes/1-1.json",
                    "sha256": "0" * 64,
                    "bytes": 1,
                },
                "texts": [{"nodeId": FIELD_ID, "value": text, "exact": True}],
                "elements": [
                    {
                        "nodeId": FIELD_ID,
                        "name": "Field",
                        "type": "TEXT",
                        "style": dict(style or {"color": color}),
                    }
                ],
                "assets": [],
                "variables": {},
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

    def _write_actual(self, element: dict[str, Any]) -> Path:
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
                        "elements": {FIELD_ID: element},
                        "assets": {},
                    }
                },
                "components": {},
            },
        )
        return path

    def _input(
        self,
        *,
        value: str | None = None,
        placeholder: str | None = None,
        color: str = INK,
        placeholder_color: str | None = GREY,
        placeholder_style: dict[str, Any] | None = None,
        base_style: dict[str, Any] | None = None,
        tag: str = "input",
    ) -> dict[str, Any]:
        """A captured `<input>`: text lives off `textContent`, which is empty."""

        copy: dict[str, Any] = {"textContent": "", "tag": tag}
        if value is not None:
            copy["value"] = value
        if placeholder is not None:
            copy["placeholder"] = placeholder
        style: dict[str, Any] = {"color": color, **(base_style or {})}
        if placeholder_color is not None:
            style["placeholderColor"] = placeholder_color
        if placeholder_style is not None:
            style["placeholderStyle"] = dict(placeholder_style)
        return {"text": "", "copy": copy, "style": style}

    def _span(self, text: str, *, color: str = GREY) -> dict[str, Any]:
        return {
            "text": text,
            "copy": {"textContent": text, "tag": "span"},
            "style": {"color": color},
        }

    def _validate(self, actual_path: Path, **config: Any) -> dict[str, Any]:
        path = self.root / "config.json"
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

    def _defects(self, result: dict[str, Any], gate: str) -> list[str]:
        return [
            item["type"]
            for item in result["defects"]
            if item["gate"] == gate and item["severity"] == "hard"
        ]


class CopyFromFormControlTest(FormControlHarness):
    def test_a_filled_field_satisfies_the_contracted_text(self) -> None:
        """The surname the design drew as text is the input's value."""

        self._write_bundle(text="Lin")
        result = self._validate(self._write_actual(self._input(value="Lin")))

        self.assertEqual(self._defects(result, "copy"), [])

    def test_an_empty_field_satisfies_it_through_the_placeholder(self) -> None:
        """"Current password" is drawn as text and implemented as a prompt."""

        self._write_bundle(text="Current password")
        result = self._validate(
            self._write_actual(self._input(placeholder="Current password"))
        )

        self.assertEqual(self._defects(result, "copy"), [])

    def test_the_value_wins_over_the_placeholder(self) -> None:
        """Once a field is filled, the placeholder is not what is on screen."""

        self._write_bundle(text="Lin")
        result = self._validate(
            self._write_actual(
                self._input(value="Lin", placeholder="Enter your surname")
            )
        )

        self.assertEqual(self._defects(result, "copy"), [])

    def test_a_wrong_value_still_fails(self) -> None:
        """The fallback must not turn the gate into a rubber stamp."""

        self._write_bundle(text="Lin")
        result = self._validate(self._write_actual(self._input(value="Lynn")))

        self.assertEqual(self._defects(result, "copy"), ["copy-mismatch"])

    def test_a_wrong_placeholder_still_fails(self) -> None:
        self._write_bundle(text="Current password")
        result = self._validate(
            self._write_actual(self._input(placeholder="Password"))
        )

        self.assertEqual(self._defects(result, "copy"), ["copy-mismatch"])

    def test_a_field_with_neither_still_fails(self) -> None:
        """An input that shows nothing has not implemented the copy."""

        self._write_bundle(text="Lin")
        result = self._validate(self._write_actual(self._input()))

        self.assertEqual(self._defects(result, "copy"), ["copy-mismatch"])

    def test_a_plain_text_node_is_unaffected(self) -> None:
        """The ordinary path keeps reading `textContent`."""

        self._write_bundle(text="Lin")
        result = self._validate(self._write_actual(self._span("Lin")))
        self.assertEqual(self._defects(result, "copy"), [])

        result = self._validate(self._write_actual(self._span("Lynn")))
        self.assertEqual(self._defects(result, "copy"), ["copy-mismatch"])

    def test_a_stray_value_on_an_ordinary_element_proves_nothing(self) -> None:
        """Otherwise any element could claim any text by carrying an attribute.

        A `<div data-node-id=… value="Lin">` renders nothing; letting its
        `value` satisfy the contract would turn the fallback into a way to pass
        the copy gate without putting the copy on screen.
        """

        self._write_bundle(text="Lin")
        element = {
            "text": "",
            "copy": {"textContent": "", "tag": "div", "value": "Lin"},
            "style": {"color": GREY},
        }

        result = self._validate(self._write_actual(element))

        self.assertEqual(self._defects(result, "copy"), ["copy-mismatch"])

    def test_a_select_is_judged_by_its_visible_option(self) -> None:
        """`.value` is the option's code; the label is what a reader sees."""

        self._write_bundle(text="Expected")
        wrong = {
            "text": "",
            "copy": {
                "textContent": "",
                "tag": "select",
                "value": "Expected",
                "selectedText": "Wrong visible label",
            },
            "style": {"color": GREY},
        }
        result = self._validate(self._write_actual(wrong))
        self.assertEqual(self._defects(result, "copy"), ["copy-mismatch"])

        right = {**wrong, "copy": {**wrong["copy"], "selectedText": "Expected"}}
        result = self._validate(self._write_actual(right))
        self.assertEqual(self._defects(result, "copy"), [])

    def test_a_legacy_capture_without_a_tag_does_not_fall_back(self) -> None:
        """Before this change nothing recorded `tag`; stay strict, not lenient."""

        self._write_bundle(text="Lin")
        element = {
            "text": "",
            "copy": {"textContent": "", "value": "Lin"},
            "style": {"color": GREY},
        }

        result = self._validate(self._write_actual(element))

        self.assertEqual(self._defects(result, "copy"), ["copy-mismatch"])


class PlaceholderColorTest(FormControlHarness):
    def test_an_empty_field_is_judged_by_its_placeholder_colour(self) -> None:
        """Grey prompt on screen; `color` is the ink a typed value would use."""

        self._write_bundle(text="Current password", color=GREY)
        result = self._validate(
            self._write_actual(
                self._input(
                    placeholder="Current password",
                    color=INK,
                    placeholder_color=GREY,
                )
            )
        )

        self.assertEqual(self._defects(result, "style"), [])

    def test_a_wrong_placeholder_colour_still_fails(self) -> None:
        self._write_bundle(text="Current password", color=GREY)
        result = self._validate(
            self._write_actual(
                self._input(
                    placeholder="Current password",
                    color=GREY,
                    placeholder_color="#FF0000",
                )
            )
        )

        self.assertEqual(self._defects(result, "style"), ["style-mismatch"])

    def test_a_filled_field_is_judged_by_its_own_colour(self) -> None:
        """With a value on screen, the substitution must not apply.

        The field carries a placeholder too — an input usually does — so this
        exercises the value guard rather than stopping at "has no placeholder".
        """

        self._write_bundle(text="Lin", color=INK)
        result = self._validate(
            self._write_actual(
                self._input(
                    value="Lin",
                    placeholder="Surname",
                    color=INK,
                    placeholder_color=GREY,
                )
            )
        )
        self.assertEqual(self._defects(result, "style"), [])

        result = self._validate(
            self._write_actual(
                self._input(
                    value="Lin",
                    placeholder="Surname",
                    color="#FF0000",
                    placeholder_color=INK,
                )
            )
        )
        self.assertEqual(self._defects(result, "style"), ["style-mismatch"])

    def test_a_capture_without_placeholder_colour_reports_missing_evidence(
        self,
    ) -> None:
        """An older capture never measured the prompt's colour.

        Falling back to the base `color` would decide the gate on a value
        nobody can see — and it would pass whenever that base colour happened
        to match the design, which is the shape of a silent false pass.
        """

        self._write_bundle(text="Current password", color=GREY)
        for base_color in (INK, GREY):
            with self.subTest(base_color=base_color):
                result = self._validate(
                    self._write_actual(
                        self._input(
                            placeholder="Current password",
                            color=base_color,
                            placeholder_color=None,
                        )
                    )
                )
                self.assertEqual(
                    self._defects(result, "style"),
                    ["missing-actual-style-property"],
                )

    def test_a_plain_text_node_is_unaffected(self) -> None:
        self._write_bundle(text="Lin", color=GREY)
        result = self._validate(self._write_actual(self._span("Lin", color=GREY)))
        self.assertEqual(self._defects(result, "style"), [])

        result = self._validate(self._write_actual(self._span("Lin", color=INK)))
        self.assertEqual(self._defects(result, "style"), ["style-mismatch"])


class PlaceholderStyleTest(FormControlHarness):
    """The prompt is a whole appearance, not just a colour.

    `::placeholder` accepts the `::first-line` property set, so a rule there
    can change the size, weight and opacity of the prompt as well as its ink.
    While only `color` was substituted, every other property was still read
    off the base input -- which describes the typed value nobody can see yet --
    so a prompt drawn at one pixel and zero opacity satisfied the contract on
    the strength of its colour alone.
    """

    CONTRACT = {"color": GREY, "fontSize": 16, "opacity": 1}

    def test_an_invisible_prompt_does_not_pass_on_its_colour(self) -> None:
        self._write_bundle(text="Current password", style=self.CONTRACT)
        result = self._validate(
            self._write_actual(
                self._input(
                    placeholder="Current password",
                    color=INK,
                    # The base input still measures as the contract expects,
                    # which is exactly why reading it was the bug.
                    base_style={"fontSize": 16, "opacity": 1},
                    placeholder_style={
                        "color": GREY,
                        "fontSize": 1,
                        "opacity": 0,
                    },
                )
            )
        )
        mismatched = sorted(
            key
            for item in result["defects"]
            if item["gate"] == "style" and item["type"] == "style-mismatch"
            for key in item["expected"]
        )
        self.assertEqual(mismatched, ["fontSize", "opacity"])
        self.assertFalse(result["passed"])

    def test_a_correct_prompt_passes_on_every_property(self) -> None:
        self._write_bundle(text="Current password", style=self.CONTRACT)
        result = self._validate(
            self._write_actual(
                self._input(
                    placeholder="Current password",
                    color=INK,
                    base_style={"fontSize": 99, "opacity": 0.5},
                    placeholder_style={
                        "color": GREY,
                        "fontSize": 16,
                        "opacity": 1,
                    },
                )
            )
        )
        self.assertEqual(self._defects(result, "style"), [])

    def test_a_capture_that_measured_only_the_colour_says_so(self) -> None:
        """An older snapshot is partial evidence -- and it is still evidence.

        Refusing to compare its font properties was the first attempt here, and
        it was wrong: `::placeholder` inherits them from the input unless a rule
        overrides them, so the base values usually *are* the prompt's. Dropping
        them turned a usually-correct comparison into a guaranteed failure --
        112 hard defects on a 61-screen pilot that had passed minutes earlier,
        every one of them the harness refusing to read evidence it had.

        So the comparison happens and the uncertainty is named, per field.
        """

        self._write_bundle(text="Current password", style=self.CONTRACT)
        result = self._validate(
            self._write_actual(
                self._input(
                    placeholder="Current password",
                    color=INK,
                    base_style={"fontSize": 16, "opacity": 1},
                    placeholder_color=GREY,
                )
            )
        )
        warned = [
            item
            for item in result["defects"]
            if item["type"] == "placeholder-style-unmeasured"
        ]
        self.assertEqual(len(warned), 1, result["defects"])
        self.assertEqual(warned[0]["severity"], "warning")
        self.assertEqual(warned[0]["elementNodeId"], FIELD_ID)
        self.assertEqual(self._defects(result, "style"), [])
        self.assertTrue(result["passed"], result["defects"])

    def test_the_full_prompt_style_raises_no_such_warning(self) -> None:
        self._write_bundle(text="Current password", style=self.CONTRACT)
        result = self._validate(
            self._write_actual(
                self._input(
                    placeholder="Current password",
                    color=INK,
                    placeholder_style={
                        "color": GREY,
                        "fontSize": 16,
                        "opacity": 1,
                    },
                )
            )
        )
        self.assertNotIn(
            "placeholder-style-unmeasured",
            [item["type"] for item in result["defects"]],
        )
