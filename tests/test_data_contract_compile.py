"""Compile only data contracts that a reviewer can safely approve.

The validator treats a declared node differently from ordinary exact copy, so
an ambiguous or duplicated declaration would weaken a hard gate. These tests
keep that decision at the compile boundary: the bundle contains either one
unambiguous reviewed contract or no data contract at all.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from figma_lossless import compiler as compiler_module
from figma_lossless.cli import main
from figma_lossless.data_contract import (
    DataContractError,
    validate_data_contract_semantics,
)
from figma_lossless.util import read_json, write_json

from test_canonical import frame_node, text_node, write_rest_input


FEATURE_ID = "data-contract-compile"
SCREEN_ID = "1:1"
SLOT_ID = "1:2"


def contract(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schemaVersion": 1,
        "featureId": FEATURE_ID,
        "slots": [
            {
                "screenNodeId": SCREEN_ID,
                "nodeId": SLOT_ID,
                "binding": "user.email",
                "shape": "email",
                "designExemplar": "design@example.com",
            }
        ],
        "chrome": [],
    }
    value.update(overrides)
    return value


class DataContractCompileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.rest_input = write_rest_input(
            self.root,
            {
                SCREEN_ID: frame_node(
                    SCREEN_ID,
                    children=[text_node(SLOT_ID, "design@example.com")],
                )
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _compile(self, payload: Any) -> tuple[int, str]:
        contract_path = self.root / "data-contract.json"
        write_json(contract_path, payload)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(
            io.StringIO()
        ):
            exit_code = main(
                [
                    "compile",
                    "--rest-input",
                    str(self.rest_input),
                    "--output",
                    str(self.bundle),
                    "--feature-id",
                    FEATURE_ID,
                    "--data-contract",
                    str(contract_path),
                ]
            )
        return exit_code, errors.getvalue()

    def test_valid_contract_is_copied_and_recorded_in_the_manifest(self) -> None:
        payload = contract()

        exit_code, errors = self._compile(payload)

        self.assertEqual(exit_code, 0, errors)
        manifest = read_json(self.bundle / "manifest.json")
        self.assertEqual(
            manifest["contracts"]["dataContract"],
            "contracts/data-contract.json",
        )
        self.assertEqual(
            read_json(self.bundle / manifest["contracts"]["dataContract"]),
            payload,
        )

    def test_missing_required_keys_fail_before_a_bundle_is_accepted(self) -> None:
        cases = {
            "top-level slots": {
                key: value for key, value in contract().items() if key != "slots"
            },
            "slot binding": contract(
                slots=[
                    {
                        "screenNodeId": SCREEN_ID,
                        "nodeId": SLOT_ID,
                        "designExemplar": "design@example.com",
                    }
                ]
            ),
            "slot exemplar": contract(
                slots=[
                    {
                        "screenNodeId": SCREEN_ID,
                        "nodeId": SLOT_ID,
                        "binding": "user.email",
                    }
                ]
            ),
            "chrome node": contract(chrome=[{"screenNodeId": SCREEN_ID}]),
        }

        for label, payload in cases.items():
            with self.subTest(label=label):
                exit_code, errors = self._compile(payload)
                self.assertEqual(exit_code, 1)
                self.assertIn("missing required key", errors)

    def test_a_node_cannot_be_declared_twice_or_as_both_slot_and_chrome(self) -> None:
        duplicate = {
            "screenNodeId": SCREEN_ID,
            "nodeId": SLOT_ID,
        }
        for payload in (
            contract(slots=[*contract()["slots"], contract()["slots"][0]]),
            contract(chrome=[duplicate]),
        ):
            with self.subTest(payload=payload):
                exit_code, errors = self._compile(payload)
                self.assertEqual(exit_code, 1)
                self.assertIn("duplicate node", errors)

    def test_shape_is_limited_to_the_reviewed_vocabulary(self) -> None:
        payload = contract(slots=[{**contract()["slots"][0], "shape": "phone"}])

        exit_code, errors = self._compile(payload)

        self.assertEqual(exit_code, 1)
        self.assertIn("unsupported shape", errors)

    def test_declared_slot_and_chrome_must_name_compiled_text_nodes(self) -> None:
        for label, payload in (
            (
                "slot",
                contract(
                    slots=[
                        {
                            **contract()["slots"][0],
                            "nodeId": "missing:slot",
                        }
                    ]
                ),
            ),
            (
                "chrome",
                contract(
                    slots=[],
                    chrome=[
                        {"screenNodeId": SCREEN_ID, "nodeId": "missing:chrome"}
                    ],
                ),
            ),
        ):
            with self.subTest(label=label):
                exit_code, errors = self._compile(payload)

                self.assertEqual(exit_code, 1)
                self.assertIn("data-contract-node-unknown", errors)

    def test_exemplar_must_equal_the_single_eligible_figma_text_value(self) -> None:
        payload = contract(
            slots=[
                {
                    **contract()["slots"][0],
                    "designExemplar": "wrong@example.com",
                }
            ]
        )

        exit_code, errors = self._compile(payload)

        self.assertEqual(exit_code, 1)
        self.assertIn("data-contract-exemplar-mismatch", errors)
        self.assertIn('"expected": "design@example.com"', errors)
        self.assertIn('"actual": "wrong@example.com"', errors)

    def test_failed_recompile_invalidates_the_previous_manifest(self) -> None:
        first_exit, first_errors = self._compile(contract())
        manifest_path = self.bundle / "manifest.json"
        self.assertEqual(first_exit, 0, first_errors)
        self.assertTrue(manifest_path.exists())

        failed_exit, failed_errors = self._compile(
            contract(
                slots=[
                    {
                        **contract()["slots"][0],
                        "designExemplar": "wrong@example.com",
                    }
                ]
            )
        )

        self.assertEqual(failed_exit, 1)
        self.assertIn("data-contract-exemplar-mismatch", failed_errors)
        self.assertFalse(
            manifest_path.exists(),
            "a semantic failure must not leave the previous manifest consumable",
        )

    def test_supporting_write_failure_never_publishes_a_manifest(self) -> None:
        first_exit, first_errors = self._compile(contract())
        manifest_path = self.bundle / "manifest.json"
        self.assertEqual(first_exit, 0, first_errors)
        self.assertTrue(manifest_path.exists())

        real_write_json = compiler_module.write_json

        def fail_raw_index(path: Path, value: Any) -> None:
            if Path(path).name == "raw-index.json":
                raise OSError("injected raw-index write failure")
            real_write_json(path, value)

        with mock.patch.object(
            compiler_module, "write_json", side_effect=fail_raw_index
        ):
            failed_exit, failed_errors = self._compile(contract())

        self.assertEqual(failed_exit, 1)
        self.assertIn("injected raw-index write failure", failed_errors)
        self.assertTrue((self.bundle / "coverage.json").exists())
        self.assertTrue((self.bundle / "assets-manifest.json").exists())
        self.assertFalse(
            manifest_path.exists(),
            "manifest.json must be the final successful publish step",
        )

    def test_ambiguous_or_non_text_content_values_cannot_validate_an_exemplar(
        self,
    ) -> None:
        self.rest_input = write_rest_input(
            self.root,
            {
                SCREEN_ID: frame_node(
                    SCREEN_ID,
                    children=[
                        text_node(SLOT_ID, "design@example.com"),
                        text_node(SLOT_ID, "second@example.com"),
                    ],
                )
            },
        )

        exit_code, errors = self._compile(contract())

        self.assertEqual(exit_code, 1)
        self.assertIn("data-contract-exemplar-mismatch", errors)
        self.assertIn("second@example.com", errors)

        with self.assertRaises(DataContractError) as raised:
            validate_data_contract_semantics(
                contract(),
                [
                    {
                        "nodeId": SCREEN_ID,
                        "texts": [
                            {
                                "nodeId": SLOT_ID,
                                "value": "design@example.com",
                                "property": "placeholder",
                                "exact": True,
                            }
                        ],
                    }
                ],
                [],
            )
        self.assertEqual(raised.exception.code, "data-contract-node-unknown")

    def test_blank_binding_is_rejected_as_a_controlled_compile_error(self) -> None:
        for binding in ("", "   "):
            with self.subTest(binding=binding):
                payload = contract(
                    slots=[{**contract()["slots"][0], "binding": binding}]
                )

                exit_code, errors = self._compile(payload)

                self.assertEqual(exit_code, 1)
                self.assertIn("data-contract-binding-blank", errors)
                self.assertNotIn("Traceback", errors)

    def test_compiler_warns_when_shared_slot_analysis_finds_undeclared_nodes(
        self,
    ) -> None:
        second_screen = "2:1"
        second_slot = "2:2"
        self.rest_input = write_rest_input(
            self.root,
            {
                SCREEN_ID: frame_node(
                    SCREEN_ID,
                    children=[
                        text_node(SLOT_ID, "design@example.com", name="email"),
                        text_node("1:3", "first", name="variant"),
                    ],
                ),
                second_screen: frame_node(
                    second_screen,
                    children=[
                        text_node(
                            second_slot,
                            "design@example.com",
                            name="email",
                        ),
                        text_node("2:3", "second", name="variant"),
                    ],
                ),
            },
        )

        exit_code, errors = self._compile(contract(slots=[], chrome=[]))

        self.assertEqual(exit_code, 0, errors)
        warnings = read_json(self.bundle / "manifest.json")["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["code"], "data-contract-incomplete")
        self.assertEqual(warnings[0]["severity"], "warning")
        self.assertEqual(warnings[0]["value"], "design@example.com")
        self.assertEqual(
            warnings[0]["occurrences"],
            [
                {"screenNodeId": SCREEN_ID, "nodeId": SLOT_ID},
                {"screenNodeId": second_screen, "nodeId": second_slot},
            ],
        )


if __name__ == "__main__":
    unittest.main()
