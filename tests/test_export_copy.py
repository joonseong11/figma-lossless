from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from figma_lossless.cli import main
from figma_lossless.copy_export import export_copy
from figma_lossless.util import read_json, write_json


def _screen(
    node_id: str,
    name: str,
    texts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "nodeId": node_id,
        "name": name,
        "evidenceSource": "figma-rest",
        "viewport": {"width": 10, "height": 10},
        "referenceScreenshot": None,
        "referenceCode": None,
        "texts": texts,
        "elements": [],
        "assets": [],
        "variables": {},
    }


def _text(node_id: str, value: str, property: str = "textContent") -> dict[str, Any]:
    return {
        "nodeId": node_id,
        "value": value,
        "property": property,
        "exact": True,
        "source": "figma",
    }


class ExportCopyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_manifest(self, screens: list[dict[str, str]]) -> None:
        write_json(
            self.bundle / "manifest.json",
            {
                "schemaVersion": "1.0",
                "featureId": "copy-export",
                "screens": screens,
                "contracts": {"flowContract": None, "componentMap": None},
            },
        )

    def _write_two_locale_screens(self) -> None:
        write_json(
            self.bundle / "screens" / "ko-1.json",
            _screen(
                "1:1",
                "Notification / KO",
                [
                    _text("1:2", "프로필을 수정했어요."),
                    _text("1:3", "확인", "aria-label"),
                ],
            ),
        )
        write_json(
            self.bundle / "screens" / "en-1.json",
            _screen(
                "2:1",
                "Notification / EN",
                [_text("2:2", "Profile updated.")],
            ),
        )
        self._write_manifest(
            [
                {
                    "nodeId": "1:1",
                    "name": "Notification / KO",
                    "compiledPath": "screens/ko-1.json",
                },
                {
                    "nodeId": "2:1",
                    "name": "Notification / EN",
                    "compiledPath": "screens/en-1.json",
                },
            ]
        )

    def test_json_export_has_all_entries_with_exact_unicode_and_sorted_order(
        self,
    ) -> None:
        self._write_two_locale_screens()
        output = self.root / "copy.json"
        result = export_copy(self.bundle, output, format="json")

        self.assertEqual(result["screens"], 2)
        self.assertEqual(result["entries"], 3)
        self.assertEqual(result["output"], str(output.resolve()))

        entries = read_json(output)
        self.assertEqual(len(entries), 3)
        # Sorted by (screenName, screenNodeId, nodeId, property): "Notification
        # / EN" sorts before "Notification / KO".
        self.assertEqual(
            entries,
            [
                {
                    "screenNodeId": "2:1",
                    "screenName": "Notification / EN",
                    "nodeId": "2:2",
                    "property": "textContent",
                    "value": "Profile updated.",
                },
                {
                    "screenNodeId": "1:1",
                    "screenName": "Notification / KO",
                    "nodeId": "1:2",
                    "property": "textContent",
                    "value": "프로필을 수정했어요.",
                },
                {
                    "screenNodeId": "1:1",
                    "screenName": "Notification / KO",
                    "nodeId": "1:3",
                    "property": "aria-label",
                    "value": "확인",
                },
            ],
        )
        # Round-trip fidelity: the raw bytes must contain the literal Korean
        # text, not an escaped \uXXXX sequence.
        raw = output.read_text(encoding="utf-8")
        self.assertIn("프로필을 수정했어요.", raw)

    def test_csv_export_round_trips_commas_and_quotes(self) -> None:
        write_json(
            self.bundle / "screens" / "1-1.json",
            _screen(
                "1:1",
                "Screen",
                [_text("1:2", 'Terms, "conditions" apply')],
            ),
        )
        self._write_manifest(
            [{"nodeId": "1:1", "name": "Screen", "compiledPath": "screens/1-1.json"}]
        )
        output = self.root / "copy.csv"
        result = export_copy(self.bundle, output, format="csv")
        self.assertEqual(result["entries"], 1)

        with output.open("r", encoding="utf-8", newline="") as file:
            rows = list(csv.reader(file))
        self.assertEqual(
            rows[0], ["screenNodeId", "screenName", "nodeId", "property", "value"]
        )
        self.assertEqual(
            rows[1],
            ["1:1", "Screen", "1:2", "textContent", 'Terms, "conditions" apply'],
        )

    def test_missing_bundle_directory_is_a_cli_error(self) -> None:
        missing = self.root / "does-not-exist"
        output = self.root / "copy.json"
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            exit_code = main(
                [
                    "export-copy",
                    "--bundle",
                    str(missing),
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("error:", errors.getvalue())
        self.assertFalse(output.exists())

    def test_screen_with_empty_texts_contributes_nothing(self) -> None:
        write_json(
            self.bundle / "screens" / "1-1.json", _screen("1:1", "Empty", [])
        )
        self._write_manifest(
            [{"nodeId": "1:1", "name": "Empty", "compiledPath": "screens/1-1.json"}]
        )
        output = self.root / "copy.json"
        result = export_copy(self.bundle, output, format="json")
        self.assertEqual(result["screens"], 1)
        self.assertEqual(result["entries"], 0)
        self.assertEqual(read_json(output), [])

    def test_property_variants_are_all_exported(self) -> None:
        write_json(
            self.bundle / "screens" / "1-1.json",
            _screen(
                "1:1",
                "Form",
                [
                    _text("1:2", "Submit", "textContent"),
                    _text("1:3", "Email address", "placeholder"),
                    _text("1:4", "Close dialog", "aria-label"),
                ],
            ),
        )
        self._write_manifest(
            [{"nodeId": "1:1", "name": "Form", "compiledPath": "screens/1-1.json"}]
        )
        output = self.root / "copy.json"
        result = export_copy(self.bundle, output, format="json")
        self.assertEqual(result["entries"], 3)
        properties = {entry["property"] for entry in read_json(output)}
        self.assertEqual(properties, {"textContent", "placeholder", "aria-label"})

    def test_cli_prints_summary_and_writes_output(self) -> None:
        self._write_two_locale_screens()
        output = self.root / "copy.json"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            exit_code = main(
                [
                    "export-copy",
                    "--bundle",
                    str(self.bundle),
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(exit_code, 0)
        summary = json.loads(out.getvalue())
        self.assertEqual(summary["screens"], 2)
        self.assertEqual(summary["entries"], 3)
        self.assertEqual(summary["output"], str(output.resolve()))
        self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
