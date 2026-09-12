from __future__ import annotations

import os

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any

import figma_lossless.reuse_proposal as reuse_proposal_module
import figma_lossless.slot_proposal as slot_proposal_module
from figma_lossless.cli import main

# These tests drive verification commands, which the CLI locks in the
# default extract mode (see figma_lossless.mode). Unlock for this process.
os.environ.setdefault("FIGMA_LOSSLESS_MODE", "verify")
from figma_lossless.reuse_proposal import propose_reuse
from figma_lossless.util import read_json, write_json


def _text(node_id: str, value: str, **overrides: Any) -> dict[str, Any]:
    item = {"nodeId": node_id, "value": value, "source": "figma"}
    item.update(overrides)
    return item


class ReuseProposalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.index = self.root / "repo-index.json"
        self.output = self.root / "reuse.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_bundle(
        self, screens: list[tuple[str, str, list[dict[str, Any]]]]
    ) -> None:
        manifest_screens = []
        for position, (node_id, name, texts) in enumerate(screens):
            compiled_path = f"screens/{position}.json"
            write_json(
                self.bundle / compiled_path,
                {
                    "schemaVersion": "1.0",
                    "nodeId": node_id,
                    "name": name,
                    "texts": texts,
                    "elements": [],
                },
            )
            manifest_screens.append(
                {
                    "nodeId": node_id,
                    "name": name,
                    "compiledPath": compiled_path,
                }
            )
        write_json(
            self.bundle / "manifest.json",
            {
                "schemaVersion": "1.0",
                "featureId": "reuse-proposal",
                "screens": manifest_screens,
            },
        )

    def _write_index(
        self,
        strings: list[dict[str, Any]],
        components: list[dict[str, Any]] | None = None,
    ) -> None:
        value: dict[str, Any] = {"schemaVersion": 1, "strings": strings}
        if components is not None:
            value["components"] = components
        write_json(self.index, value)

    def test_only_sources_meeting_minimum_overlap_are_proposed(self) -> None:
        self._write_bundle(
            [
                (
                    "screen:1",
                    "Fare",
                    [
                        _text("text:1", "동행 수"),
                        _text("text:2", "환급액"),
                        _text("text:3", "최종 결제금액"),
                        _text("text:4", "unindexed"),
                    ],
                )
            ]
        )
        self._write_index(
            [
                {"value": "동행 수", "source": "src/fare.ts:10", "symbol": "a"},
                {"value": "환급액", "source": "src/fare.ts:11", "symbol": "b"},
                {"value": "최종 결제금액", "source": "src/other.ts:1"},
            ],
            [{"name": "FareTable", "source": "src/FareTable.tsx"}],
        )

        propose_reuse(self.bundle, self.index, self.output, min_overlap=2)
        artifact = read_json(self.output)

        self.assertEqual(len(artifact["proposals"]), 1)
        proposal = artifact["proposals"][0]
        self.assertEqual(proposal["source"], "src/fare.ts")
        self.assertEqual(proposal["matchedCount"], 2)
        self.assertEqual(proposal["screenTextCount"], 4)
        self.assertEqual(proposal["coverage"], 0.5)
        self.assertEqual(proposal["component"], "FareTable")
        self.assertTrue(proposal["requiresConfirmation"])

    def test_same_value_in_multiple_sources_proposes_every_source(self) -> None:
        self._write_bundle(
            [("screen:1", "Fare", [_text("text:1", "동행 수")])]
        )
        self._write_index(
            [
                {"value": "동행 수", "source": "src/a.ts:1"},
                {"value": "동행 수", "source": "src/b.ts:2"},
            ]
        )

        propose_reuse(self.bundle, self.index, self.output, min_overlap=1)

        self.assertEqual(
            [item["source"] for item in read_json(self.output)["proposals"]],
            ["src/a.ts", "src/b.ts"],
        )

    def test_candidates_are_sorted_by_count_then_source(self) -> None:
        self._write_bundle(
            [
                (
                    "screen:1",
                    "Fare",
                    [_text("one", "one"), _text("two", "two")],
                )
            ]
        )
        self._write_index(
            [
                {"value": "one", "source": "src/z.ts:1"},
                {"value": "two", "source": "src/z.ts:2"},
                {"value": "one", "source": "src/b.ts:1"},
                {"value": "one", "source": "src/a.ts:1"},
            ]
        )

        propose_reuse(self.bundle, self.index, self.output, min_overlap=1)

        self.assertEqual(
            [
                (item["matchedCount"], item["source"])
                for item in read_json(self.output)["proposals"]
            ],
            [(2, "src/z.ts"), (1, "src/a.ts"), (1, "src/b.ts")],
        )

    def test_screen_text_count_keeps_repeated_eligible_occurrences(self) -> None:
        self._write_bundle(
            [
                (
                    "screen:1",
                    "Fare",
                    [
                        _text("one:first", "one"),
                        _text("one:second", "one"),
                        _text("two", "two"),
                    ],
                )
            ]
        )
        self._write_index(
            [
                {"value": "one", "source": "src/fare.ts:1"},
                {"value": "two", "source": "src/fare.ts:2"},
            ]
        )

        propose_reuse(self.bundle, self.index, self.output, min_overlap=2)

        proposal = read_json(self.output)["proposals"][0]
        self.assertEqual(proposal["screenTextCount"], 3)
        self.assertEqual(proposal["coverage"], 2 / 3)
        self.assertEqual(
            [match["value"] for match in proposal["matches"]],
            ["one", "two"],
        )

    def test_warning_for_empty_repo_strings(self) -> None:
        self._write_bundle([("screen:1", "Fare", [_text("one", "one")])])
        self._write_index([])

        propose_reuse(self.bundle, self.index, self.output)

        self.assertIn(
            {"kind": "no-repo-strings"}, read_json(self.output)["warnings"]
        )

    def test_warning_for_screen_without_match(self) -> None:
        self._write_bundle([("screen:1", "Fare", [_text("one", "one")])])
        self._write_index([{"value": "other", "source": "src/a.ts:1"}])

        propose_reuse(self.bundle, self.index, self.output)

        self.assertIn(
            {
                "kind": "screen-without-match",
                "screenNodeId": "screen:1",
                "screenName": "Fare",
            },
            read_json(self.output)["warnings"],
        )

    def test_warning_for_screen_without_text_evidence(self) -> None:
        self._write_bundle([("screen:1", "Empty", [])])
        self._write_index([{"value": "other", "source": "src/a.ts:1"}])

        propose_reuse(self.bundle, self.index, self.output)

        self.assertIn(
            {"kind": "no-text-evidence", "screenNodeId": "screen:1"},
            read_json(self.output)["warnings"],
        )

    def test_every_proposal_requires_confirmation(self) -> None:
        self._write_bundle([("screen:1", "Fare", [_text("one", "one")])])
        self._write_index(
            [
                {"value": "one", "source": "src/a.ts:1"},
                {"value": "one", "source": "src/b.ts:1"},
            ]
        )
        propose_reuse(self.bundle, self.index, self.output, min_overlap=1)

        proposals = read_json(self.output)["proposals"]
        self.assertGreater(len(proposals), 0)
        self.assertTrue(
            all(
                proposal["requiresConfirmation"] is True
                for proposal in proposals
            )
        )

    def test_values_above_unique_source_frequency_are_excluded_and_recorded(
        self,
    ) -> None:
        self._write_bundle(
            [
                (
                    "screen:1",
                    "Actions",
                    [_text("common", "확인"), _text("rare", "상세 보기")],
                )
            ]
        )
        self._write_index(
            [
                {"value": "확인", "source": "src/a.ts:1"},
                {"value": "확인", "source": "src/b.ts:2"},
                {"value": "확인", "source": "src/c.ts:3"},
                {"value": "상세 보기", "source": "src/a.ts:4"},
            ]
        )

        propose_reuse(
            self.bundle,
            self.index,
            self.output,
            min_overlap=1,
            max_source_frequency=2,
        )
        artifact = read_json(self.output)

        self.assertEqual(len(artifact["proposals"]), 1)
        self.assertEqual(
            [match["value"] for match in artifact["proposals"][0]["matches"]],
            ["상세 보기"],
        )
        self.assertIn(
            {
                "kind": "value-too-common",
                "value": "확인",
                "sourceCount": 3,
                "threshold": 2,
                "sources": ["src/a.ts", "src/b.ts", "src/c.ts"],
            },
            artifact["warnings"],
        )

    def test_common_value_exclusion_is_readable_in_markdown(self) -> None:
        self._write_bundle([("screen:1", "Actions", [_text("common", "확인")])])
        self._write_index(
            [
                {"value": "확인", "source": "src/a.ts:1"},
                {"value": "확인", "source": "src/b.ts:2"},
            ]
        )
        output = self.root / "reuse.md"

        propose_reuse(
            self.bundle,
            self.index,
            output,
            format="md",
            min_overlap=1,
            max_source_frequency=1,
        )

        markdown = output.read_text(encoding="utf-8")
        self.assertIn("value-too-common", markdown)
        self.assertIn("확인", markdown)
        self.assertIn("2", markdown)

    def test_max_source_frequency_is_validated_and_wired_through_cli(self) -> None:
        self._write_bundle([("screen:1", "Actions", [_text("common", "확인")])])
        self._write_index(
            [
                {"value": "확인", "source": "src/a.ts:1"},
                {"value": "확인", "source": "src/b.ts:2"},
            ]
        )
        for value in (0, -1, 1.5, True):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "max source frequency"
            ):
                propose_reuse(
                    self.bundle,
                    self.index,
                    self.output,
                    max_source_frequency=value,  # type: ignore[arg-type]
                )

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(
            io.StringIO()
        ):
            exit_code = main(
                [
                    "propose-reuse",
                    "--bundle",
                    str(self.bundle),
                    "--repo-index",
                    str(self.index),
                    "--output",
                    str(self.output),
                    "--min-overlap",
                    "1",
                    "--max-source-frequency",
                    "1",
                ]
            )

        self.assertEqual(exit_code, 0, stderr.getvalue())
        self.assertEqual(read_json(self.output)["proposals"], [])

    def test_invalid_min_overlap_and_output_inside_bundle_are_controlled(self) -> None:
        self._write_bundle([])
        self._write_index([])
        for value in (0, -1, 1.5, True):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "min overlap"
            ):
                propose_reuse(
                    self.bundle,
                    self.index,
                    self.output,
                    min_overlap=value,  # type: ignore[arg-type]
                )
        with self.assertRaisesRegex(ValueError, "outside bundle directory"):
            propose_reuse(self.bundle, self.index, self.bundle / "reuse.json")

    def test_null_strings_and_missing_fields_are_controlled(self) -> None:
        self._write_bundle([])
        for invalid_index, message in (
            ({"schemaVersion": 1, "strings": None}, "strings must be a list"),
            ({"schemaVersion": 1, "strings": [{}]}, "missing value"),
            (
                {"schemaVersion": 1, "strings": [{"value": "one"}]},
                "missing source",
            ),
        ):
            with self.subTest(message=message):
                write_json(self.index, invalid_index)
                with self.assertRaisesRegex(ValueError, message):
                    propose_reuse(self.bundle, self.index, self.output)

    def test_missing_index_is_reported_without_traceback_by_cli(self) -> None:
        self._write_bundle([])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "propose-reuse",
                    "--bundle",
                    str(self.bundle),
                    "--repo-index",
                    str(self.index),
                    "--output",
                    str(self.output),
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("error:", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_markdown_cli_writes_a_readable_table(self) -> None:
        self._write_bundle([("screen:1", "Fare", [_text("one", "one")])])
        self._write_index([{"value": "one", "source": "src/a.ts:1"}])
        output = self.root / "reuse.md"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "propose-reuse",
                    "--bundle",
                    str(self.bundle),
                    "--repo-index",
                    str(self.index),
                    "--output",
                    str(output),
                    "--format",
                    "md",
                    "--min-overlap",
                    "1",
                ]
            )

        self.assertEqual(exit_code, 0)
        markdown = output.read_text(encoding="utf-8")
        self.assertIn("| Screen | Source | Matches |", markdown)
        self.assertIn("| Fare | src/a.ts | 1 (one) |", markdown)

    def test_reuse_and_slot_proposals_use_the_same_text_filter(self) -> None:
        self.assertIs(
            reuse_proposal_module.eligible_texts,
            slot_proposal_module.eligible_texts,
        )
        screen = {
            "nodeId": "screen:1",
            "texts": [
                _text("default", "default property"),
                _text("explicit", "explicit property", property="textContent"),
                _text("aria", "aria", property="aria-label"),
                _text("approximate", "approximate", exact=False),
                _text("blank", "  "),
            ],
        }
        self.assertEqual(
            [
                item["value"]
                for item in reuse_proposal_module.eligible_texts(screen)
            ],
            ["default property", "explicit property"],
        )


if __name__ == "__main__":
    unittest.main()
