from __future__ import annotations

import os

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any

from figma_lossless.cli import main

# These tests drive verification commands, which the CLI locks in the
# default extract mode (see figma_lossless.mode). Unlock for this process.
os.environ.setdefault("FIGMA_LOSSLESS_MODE", "verify")
from figma_lossless.slot_proposal import propose_slots
from figma_lossless.util import read_json, write_json


LOCALES = ("ko", "en", "ja", "zh")
STATES = ("default", "loading", "complete")
PROFILE_DATA = {"9:41", "F", "Fei", "design.sample@example.com"}


def _text(node_id: str, value: str, **overrides: Any) -> dict[str, Any]:
    text: dict[str, Any] = {
        "nodeId": node_id,
        "value": value,
        "source": "figma",
    }
    text.update(overrides)
    return text


def _screen(
    node_id: str,
    name: str,
    texts: list[dict[str, Any]],
    element_names: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "nodeId": node_id,
        "name": name,
        "evidenceSource": "figma-rest",
        "viewport": {"width": 390, "height": 844},
        "referenceScreenshot": None,
        "referenceCode": None,
        "texts": texts,
        "elements": [
            {"nodeId": f"{node_id}:element:{index}", "name": element_name}
            for index, element_name in enumerate(element_names or [f"{name}:root"])
        ],
        "assets": [],
        "variables": {},
    }


class SlotProposalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_bundle(
        self, screens: list[tuple[Any, ...]]
    ) -> None:
        manifest_screens = []
        for index, screen_fixture in enumerate(screens):
            node_id, name, texts, *structure = screen_fixture
            compiled_path = f"screens/{index}.json"
            write_json(
                self.bundle / compiled_path,
                _screen(node_id, name, texts, structure[0] if structure else None),
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
                "featureId": "slot-proposal",
                "screens": manifest_screens,
                "contracts": {"flowContract": None, "componentMap": None},
            },
        )

    def _write_pilot_shape(self) -> None:
        labels = {
            "ko": ("프로필 수정", "이메일", "알림"),
            "en": ("Edit profile", "Email", "Notifications"),
            "ja": ("プロフィールを編集", "メール", "通知"),
            "zh": ("编辑个人资料", "电子邮件", "通知设置"),
        }
        screens = []
        index = 0
        for locale in LOCALES:
            for state in STATES:
                index += 1
                profile_texts = [
                    _text(f"p:{index}:time", "9:41"),
                    _text(f"p:{index}:initial", "F"),
                    _text(f"p:{index}:name", "Fei"),
                    _text(
                        f"p:{index}:email",
                        "design.sample@example.com",
                    ),
                    *[
                        _text(f"p:{index}:label:{label_index}", f"{label} {state}")
                        for label_index, label in enumerate(labels[locale])
                    ],
                    _text(
                        f"p:{index}:ignored",
                        "Fei",
                        property="aria-label",
                    ),
                ]
                screens.append((f"profile:{index}", "profile", profile_texts))

                setting_texts = [
                    _text(f"s:{index}:time", "9:41"),
                    *[
                        _text(f"s:{index}:label:{label_index}", f"{label} {state}")
                        for label_index, label in enumerate(labels[locale])
                    ],
                ]
                screens.append((f"setting:{index}", "setting", setting_texts))

        self._write_bundle(screens)

    def test_invariant_values_match_the_pilot_shape_without_label_leaks(
        self,
    ) -> None:
        self._write_pilot_shape()
        output = self.root / "slots.json"
        propose_slots(self.bundle, output)
        artifact = read_json(output)

        profile = {
            proposal["value"]
            for proposal in artifact["proposals"]
            if proposal["group"] == "profile"
        }
        setting = {
            proposal["value"]
            for proposal in artifact["proposals"]
            if proposal["group"] == "setting"
        }
        self.assertEqual(profile, PROFILE_DATA)
        self.assertEqual(setting, {"9:41"})
        self.assertEqual(
            {proposal["value"] for proposal in artifact["proposals"]},
            PROFILE_DATA,
        )
        self.assertEqual(
            {group["memberCount"] for group in artifact["groups"]},
            {12},
        )
        profile_group = next(
            group for group in artifact["groups"] if group["name"] == "profile"
        )
        self.assertEqual(
            profile_group["members"],
            [f"profile:{index}" for index in range(1, 13)],
        )
        fei = next(
            proposal
            for proposal in artifact["proposals"]
            if proposal["group"] == "profile" and proposal["value"] == "Fei"
        )
        self.assertEqual(len(fei["occurrences"]), 12)
        self.assertEqual(
            fei["occurrences"][0],
            {"screenNodeId": "profile:1", "nodeId": "p:1:name"},
        )

    def test_structural_multiset_similarity_groups_by_single_linkage_not_name(
        self,
    ) -> None:
        screens = [
            ("a", "duplicate", [_text("a:text", "A")], ["only-a"]),
            (
                "b",
                "duplicate",
                [_text("b:text", "B")],
                ["x", "x", "y", "z"],
            ),
            (
                "c",
                "structural",
                [_text("c:text", "C")],
                ["x", "x", "y", "z", "w"],
            ),
            (
                "d",
                "structural",
                [_text("d:text", "D")],
                ["x", "y", "z", "w"],
            ),
        ]
        self._write_bundle(screens)
        output = self.root / "slots.json"
        propose_slots(self.bundle, output)
        artifact = read_json(output)

        self.assertEqual(
            [(group["name"], group["members"]) for group in artifact["groups"]],
            [("duplicate", ["a"]), ("structural", ["b", "c", "d"])],
        )

        strict_output = self.root / "strict.json"
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = main(
                [
                    "propose-slots",
                    "--bundle",
                    str(self.bundle),
                    "--output",
                    str(strict_output),
                    "--group-similarity",
                    "0.81",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(read_json(strict_output)["groups"]), 4)

    def test_time_is_chrome_and_every_other_pilot_value_is_a_slot(self) -> None:
        self._write_pilot_shape()
        output = self.root / "slots.json"
        propose_slots(self.bundle, output)
        artifact = read_json(output)
        kinds = {
            (proposal["group"], proposal["value"]): proposal["kind"]
            for proposal in artifact["proposals"]
        }

        self.assertEqual(kinds[("profile", "9:41")], "chrome")
        self.assertEqual(kinds[("setting", "9:41")], "chrome")
        for value in PROFILE_DATA - {"9:41"}:
            self.assertEqual(kinds[("profile", value)], "slot")

    def test_single_variant_warns_without_proposing_any_value(self) -> None:
        self._write_bundle(
            [("language:1", "language", [_text("language:2", "한국어")])]
        )
        output = self.root / "slots.json"
        propose_slots(self.bundle, output)
        artifact = read_json(output)

        self.assertEqual(artifact["proposals"], [])
        self.assertEqual(
            artifact["warnings"],
            [
                {
                    "kind": "insufficient-variants",
                    "group": "language",
                    "memberCount": 1,
                }
            ],
        )

    def test_similar_variants_warn_without_proposing_invariant_values(self) -> None:
        self._write_bundle(
            [
                (
                    "checked:1",
                    "checked",
                    [
                        _text("checked:1:space", "Do not use spaces."),
                        _text(
                            "checked:1:case",
                            "Mix upper and lowercase letters.",
                        ),
                        _text(
                            "checked:1:number",
                            "Use at least one number.",
                        ),
                        _text("checked:1:state", "Checked"),
                    ],
                ),
                (
                    "checked:2",
                    "checked",
                    [
                        _text("checked:2:space", "Do not use spaces."),
                        _text(
                            "checked:2:case",
                            "Mix upper and lowercase letters.",
                        ),
                        _text(
                            "checked:2:number",
                            "Use at least one number.",
                        ),
                        _text("checked:2:state", "Unchecked"),
                    ],
                ),
                (
                    "checked:3",
                    "checked",
                    [
                        _text("checked:3:space", "Do not use spaces."),
                        _text(
                            "checked:3:case",
                            "Mix upper and lowercase letters.",
                        ),
                        _text(
                            "checked:3:number",
                            "Use at least one number.",
                        ),
                        _text("checked:3:state", "Unchecked"),
                    ],
                ),
            ]
        )
        output = self.root / "slots.json"
        propose_slots(self.bundle, output)
        artifact = read_json(output)

        self.assertEqual(artifact["proposals"], [])
        self.assertEqual(
            artifact["warnings"],
            [
                {
                    "kind": "variants-too-similar",
                    "group": "checked",
                    "memberCount": 3,
                    "invariantCount": 3,
                    "unionCount": 5,
                    "invarianceRatio": 0.6,
                    "threshold": 0.5,
                }
            ],
        )

    def test_invariance_ratio_equal_to_threshold_still_proposes(self) -> None:
        self._write_bundle(
            [
                (
                    "profile:1",
                    "profile",
                    [_text("profile:1:data", "Fei"), _text("profile:1:a", "A")],
                ),
                (
                    "profile:2",
                    "profile",
                    [_text("profile:2:data", "Fei"), _text("profile:2:b", "B")],
                ),
            ]
        )
        output = self.root / "slots.json"
        propose_slots(self.bundle, output, max_invariance_ratio=1 / 3)
        artifact = read_json(output)

        self.assertEqual(artifact["warnings"], [])
        self.assertEqual(
            [proposal["value"] for proposal in artifact["proposals"]], ["Fei"]
        )
        self.assertEqual(
            artifact["proposals"][0]["signals"]["invarianceRatio"], 1 / 3
        )

    def test_cli_can_lower_maximum_invariance_ratio(self) -> None:
        self._write_pilot_shape()
        output = self.root / "slots.json"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            exit_code = main(
                [
                    "propose-slots",
                    "--bundle",
                    str(self.bundle),
                    "--output",
                    str(output),
                    "--max-invariance-ratio",
                    "0.01",
                ]
            )

        self.assertEqual(exit_code, 0)
        artifact = read_json(output)
        self.assertEqual(artifact["proposals"], [])
        self.assertEqual(
            {warning["group"] for warning in artifact["warnings"]},
            {"profile", "setting"},
        )
        self.assertEqual(
            {warning["threshold"] for warning in artifact["warnings"]}, {0.01}
        )
        self.assertIn('"proposals": 0', stdout.getvalue())

    def test_every_proposal_requires_confirmation(self) -> None:
        self._write_pilot_shape()
        output = self.root / "slots.json"
        propose_slots(self.bundle, output)
        proposals = read_json(output)["proposals"]

        self.assertTrue(proposals)
        self.assertTrue(
            all(proposal["requiresConfirmation"] is True for proposal in proposals)
        )

    def test_copy_defaults_match_the_gate_and_explicit_false_is_excluded(
        self,
    ) -> None:
        self._write_bundle(
            [
                (
                    "profile:1",
                    "profile",
                    [
                        _text("profile:1:default", "Default copy"),
                        _text("profile:1:variant", "Variant one"),
                        _text("profile:1:false", "Not exact", exact=False),
                        _text(
                            "profile:1:aria",
                            "Aria copy",
                            property="aria-label",
                            exact=True,
                        ),
                    ],
                ),
                (
                    "profile:2",
                    "profile",
                    [
                        _text("profile:2:default", "Default copy"),
                        _text("profile:2:variant", "Variant two"),
                        _text("profile:2:false", "Not exact", exact=False),
                        _text(
                            "profile:2:aria",
                            "Aria copy",
                            property="aria-label",
                            exact=True,
                        ),
                    ],
                ),
            ]
        )
        output = self.root / "slots.json"
        propose_slots(self.bundle, output)

        self.assertEqual(
            [proposal["value"] for proposal in read_json(output)["proposals"]],
            ["Default copy"],
        )

    def test_artifact_contract_preserves_schema_shapes_confidence_and_signal(
        self,
    ) -> None:
        self._write_pilot_shape()
        output = self.root / "slots.json"
        propose_slots(self.bundle, output)
        artifact = read_json(output)
        by_value = {
            (proposal["group"], proposal["value"]): proposal
            for proposal in artifact["proposals"]
        }

        self.assertEqual(artifact["schemaVersion"], 1)
        self.assertEqual(by_value[("profile", "Fei")]["confidence"], "medium")
        email = by_value[("profile", "design.sample@example.com")]
        self.assertEqual(email["confidence"], "high")
        self.assertEqual(email["signals"]["shape"], "email")
        self.assertTrue(
            all(
                proposal["signals"]["invariantAcrossVariants"] is True
                for proposal in artifact["proposals"]
            )
        )

        self._write_bundle(
            [
                (
                    "low:1",
                    "low",
                    [
                        _text("low:1:text", "Unshaped value"),
                        _text("low:1:variant", "Variant one"),
                    ],
                ),
                (
                    "low:2",
                    "low",
                    [
                        _text("low:2:text", "Unshaped value"),
                        _text("low:2:variant", "Variant two"),
                    ],
                ),
            ]
        )
        low_output = self.root / "low.json"
        propose_slots(self.bundle, low_output)
        self.assertEqual(
            read_json(low_output)["proposals"][0]["confidence"], "low"
        )

    def test_empty_manifest_and_empty_text_groups_warn_with_measured_zero(
        self,
    ) -> None:
        self._write_bundle([])
        output = self.root / "empty.json"
        propose_slots(self.bundle, output)
        self.assertEqual(read_json(output)["warnings"], [{"kind": "no-screens"}])

        markdown_output = self.root / "empty.md"
        propose_slots(self.bundle, markdown_output, format="md")
        self.assertIn(
            "| no-screens |  |  |",
            markdown_output.read_text(encoding="utf-8"),
        )

        self._write_bundle(
            [("blank:1", "blank", []), ("blank:2", "blank", [])]
        )
        no_text_output = self.root / "no-text.json"
        propose_slots(self.bundle, no_text_output)
        self.assertEqual(
            read_json(no_text_output)["warnings"],
            [
                {
                    "kind": "no-text-evidence",
                    "group": "blank",
                    "memberCount": 2,
                    "invariantCount": 0,
                    "unionCount": 0,
                    "invarianceRatio": 0.0,
                }
            ],
        )

    def test_invalid_ratios_are_controlled_cli_errors(self) -> None:
        self._write_bundle(
            [("profile:1", "profile", [_text("profile:1:text", "Fei")])]
        )
        for option in ("--max-invariance-ratio", "--group-similarity"):
            for value in ("1.1", "nan", "inf", "-0.1"):
                with self.subTest(option=option, value=value):
                    errors = io.StringIO()
                    with contextlib.redirect_stderr(errors):
                        exit_code = main(
                            [
                                "propose-slots",
                                "--bundle",
                                str(self.bundle),
                                "--output",
                                str(self.root / f"invalid-{option[2:]}-{value}.json"),
                                option,
                                value,
                            ]
                        )
                    self.assertEqual(exit_code, 1)
                    self.assertIn("finite number between 0 and 1", errors.getvalue())

    def test_output_inside_bundle_is_a_controlled_error(self) -> None:
        self._write_bundle(
            [("profile:1", "profile", [_text("profile:1:text", "Fei")])]
        )
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            exit_code = main(
                [
                    "propose-slots",
                    "--bundle",
                    str(self.bundle),
                    "--output",
                    str(self.bundle / "manifest.json"),
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("outside bundle directory", errors.getvalue())
        self.assertEqual(
            read_json(self.bundle / "manifest.json")["schemaVersion"], "1.0"
        )

    def test_malformed_bundle_fields_are_controlled_errors(self) -> None:
        cases = (
            "screens-null",
            "texts-null",
            "compiled-path-missing",
            "node-id-missing",
        )
        for case in cases:
            with self.subTest(case=case):
                self.bundle = self.root / case / "bundle"
                self._write_bundle(
                    [("profile:1", "profile", [_text("profile:1:text", "Fei")])]
                )
                manifest_path = self.bundle / "manifest.json"
                manifest = read_json(manifest_path)
                screen_path = self.bundle / "screens/0.json"
                screen = read_json(screen_path)
                if case == "screens-null":
                    manifest["screens"] = None
                    write_json(manifest_path, manifest)
                elif case == "texts-null":
                    screen["texts"] = None
                    write_json(screen_path, screen)
                elif case == "compiled-path-missing":
                    del manifest["screens"][0]["compiledPath"]
                    write_json(manifest_path, manifest)
                else:
                    del screen["nodeId"]
                    write_json(screen_path, screen)

                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    exit_code = main(
                        [
                            "propose-slots",
                            "--bundle",
                            str(self.bundle),
                            "--output",
                            str(self.root / case / "slots.json"),
                        ]
                    )
                self.assertEqual(exit_code, 1)
                self.assertIn("error:", errors.getvalue())
                self.assertNotIn("Traceback", errors.getvalue())

    def test_markdown_format_writes_human_readable_tables(self) -> None:
        self._write_pilot_shape()
        output = self.root / "slots.md"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            exit_code = main(
                [
                    "propose-slots",
                    "--bundle",
                    str(self.bundle),
                    "--output",
                    str(output),
                    "--format",
                    "md",
                    "--min-variants",
                    "2",
                ]
            )

        self.assertEqual(exit_code, 0)
        markdown = output.read_text(encoding="utf-8")
        self.assertIn("| Group | Value | Kind |", markdown)
        self.assertIn("| profile | Fei | slot |", markdown)
        self.assertIn('"proposals": 5', stdout.getvalue())

    def test_markdown_escapes_group_and_value_table_syntax(self) -> None:
        value = "A\\B|C\nD"
        self._write_bundle(
            [
                (
                    "pipe:1",
                    "pipe|group",
                    [
                        _text("pipe:1:text", value),
                        _text("pipe:1:variant", "Variant one"),
                    ],
                ),
                (
                    "pipe:2",
                    "pipe|group",
                    [
                        _text("pipe:2:text", value),
                        _text("pipe:2:variant", "Variant two"),
                    ],
                ),
            ]
        )
        output = self.root / "slots.md"
        propose_slots(self.bundle, output, format="md")
        markdown = output.read_text(encoding="utf-8")

        self.assertIn(r"pipe\|group", markdown)
        self.assertIn(r"A\\B\|C D", markdown)


if __name__ == "__main__":
    unittest.main()
