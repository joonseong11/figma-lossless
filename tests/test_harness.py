from __future__ import annotations

import base64
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from figma_lossless.assets import vendor_assets
from figma_lossless.compiler import CompileOptions, DesignCompiler
from figma_lossless.report import render_report
from figma_lossless.util import read_json, resolve_within, write_json
from figma_lossless.validators import (
    GateValidator,
    ValidateOptions,
    create_snapshot_template,
)


def response(*content: dict) -> dict:
    return {"content": list(content), "_meta": {"mcpRequestId": "test"}}


class HarnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.raw = self.root / "raw"
        self.bundle = self.root / "bundle"
        self.raw.mkdir()
        self._write_fixture()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_fixture(self) -> None:
        root_markup = (
            '<section id="1:0" name="Feature" x="0" y="0" width="100" height="100">\n'
            '  <frame id="1:1" name="Screen" x="0" y="0" width="100" height="100">\n'
            "  </frame>\n</section>"
        )
        write_json(
            self.raw / "00-root.get-design-context.raw.json",
            response({"type": "text", "text": root_markup}),
        )
        reference = """function Screen() {
  return <div data-node-id="1:1" data-name="Screen" className="bg-white">
    <p className="font-['Pretendard:Bold'] text-[16px] leading-[1.5] text-[#112233]" data-node-id="1:2">Exact copy</p>
    <input data-node-id="1:3" placeholder="Email address" />
  </div>;
}"""
        image = Image.new("RGB", (100, 100), "white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        write_json(
            self.raw / "01-screen.get-design-context.raw.json",
            response(
                {"type": "text", "text": reference},
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "data": base64.b64encode(buffer.getvalue()).decode("ascii"),
                },
            ),
        )
        metadata = """<frame id="1:1" name="Screen" x="0" y="0" width="100" height="100">
  <text id="1:2" name="Exact copy" x="10" y="20" width="60" height="24" />
  <frame id="1:3" name="Email input" x="10" y="50" width="80" height="30" />
</frame>"""
        write_json(
            self.raw / "01-screen.metadata.raw.json",
            response({"type": "text", "text": metadata}),
        )

    def _compile(self) -> dict:
        return DesignCompiler(
            CompileOptions(
                feature_id="fixture",
                input_dir=self.raw,
                output_dir=self.bundle,
            )
        ).compile()

    def test_compiler_preserves_coverage_copy_geometry_and_reference(self) -> None:
        manifest = self._compile()
        self.assertEqual(manifest["counts"]["discoveredScreens"], 1)
        self.assertEqual(manifest["counts"]["detailedContexts"], 1)
        screen = read_json(self.bundle / manifest["screens"][0]["compiledPath"])
        self.assertEqual(screen["texts"][0]["value"], "Exact copy")
        self.assertIn(
            {"nodeId": "1:3", "value": "Email address", "property": "placeholder", "exact": True, "source": "figma"},
            screen["texts"],
        )
        element = next(item for item in screen["elements"] if item["nodeId"] == "1:2")
        self.assertEqual(element["rect"], {"x": 10, "y": 20, "width": 60, "height": 24})
        self.assertEqual(element["style"]["fontWeight"], 700)
        self.assertTrue((self.bundle / screen["referenceScreenshot"]).exists())

    def test_reference_snapshot_passes_all_gates(self) -> None:
        self._compile()
        actual = self.root / "actual.json"
        create_snapshot_template(
            self.bundle, actual, use_reference_screenshots=True
        )
        output = self.root / "report"
        config = self.root / "self-test-config.json"
        # The self-test bundle has no flow or component contract, and both are
        # required by default, so the opt-out is explicit here: this test is
        # about the gates a contract-free bundle can actually satisfy.
        write_json(
            config,
            {
                "allowSelfTestSnapshot": True,
                "requireFlowContract": False,
                "requireComponentContract": False,
            },
        )
        result = GateValidator(
            ValidateOptions(self.bundle, actual, output, config_path=config)
        ).validate()
        self.assertTrue(result["passed"], result["defects"])
        report = render_report(result, output)
        self.assertTrue(report.exists())
        self.assertIn("Lossless design gate", report.read_text(encoding="utf-8"))

    def test_copy_geometry_and_visual_drift_are_hard_failures(self) -> None:
        self._compile()
        actual_path = self.root / "actual.json"
        snapshot = create_snapshot_template(
            self.bundle, actual_path, use_reference_screenshots=True
        )
        element = snapshot["screens"]["1:1"]["elements"]["1:2"]
        element["text"] = "Almost exact"
        element["copy"]["textContent"] = "Almost exact"
        element["rect"]["x"] = 15
        drift = self.root / "drift.png"
        Image.new("RGB", (100, 100), "black").save(drift)
        snapshot["screens"]["1:1"]["screenshot"] = drift.name
        write_json(actual_path, snapshot)
        result = GateValidator(
            ValidateOptions(self.bundle, actual_path, self.root / "report")
        ).validate()
        types = {item["type"] for item in result["defects"]}
        self.assertIn("copy-mismatch", types)
        self.assertIn("geometry-mismatch", types)
        self.assertIn("visual-mismatch", types)
        self.assertFalse(result["passed"])

    def test_visual_policy_warn_keeps_element_gates_hard(self) -> None:
        """Dropping the pixel channel must not soften anything else.

        A Figma frame whose children bleed past its bounds renders wider than
        the declared viewport, so the pixel diff can never line up. Demoting
        that channel is a project-level fact; the copy and geometry drift in
        the same snapshot still has to fail.
        """

        self._compile()
        actual_path = self.root / "actual.json"
        snapshot = create_snapshot_template(
            self.bundle, actual_path, use_reference_screenshots=True
        )
        element = snapshot["screens"]["1:1"]["elements"]["1:2"]
        element["text"] = "Almost exact"
        element["copy"]["textContent"] = "Almost exact"
        element["rect"]["x"] = 15
        drift = self.root / "drift.png"
        Image.new("RGB", (100, 100), "black").save(drift)
        snapshot["screens"]["1:1"]["screenshot"] = drift.name
        write_json(actual_path, snapshot)
        config = self.root / "visual-warn.json"
        write_json(config, {"visualPolicy": "warn"})
        result = GateValidator(
            ValidateOptions(
                self.bundle,
                actual_path,
                self.root / "report",
                config_path=config,
            )
        ).validate()

        by_type = {item["type"]: item["severity"] for item in result["defects"]}
        self.assertEqual(by_type.get("visual-mismatch"), "warning")
        self.assertEqual(by_type.get("copy-mismatch"), "hard")
        self.assertEqual(by_type.get("geometry-mismatch"), "hard")
        self.assertFalse(result["passed"])

    def test_visual_policy_off_skips_the_comparison(self) -> None:
        self._compile()
        actual_path = self.root / "actual.json"
        snapshot = create_snapshot_template(
            self.bundle, actual_path, use_reference_screenshots=True
        )
        drift = self.root / "drift-off.png"
        Image.new("RGB", (100, 100), "black").save(drift)
        snapshot["screens"]["1:1"]["screenshot"] = drift.name
        write_json(actual_path, snapshot)
        config = self.root / "visual-off.json"
        write_json(
            config,
            {
                "visualPolicy": "off",
                "allowSelfTestSnapshot": True,
                "requireFlowContract": False,
                "requireComponentContract": False,
            },
        )
        result = GateValidator(
            ValidateOptions(
                self.bundle,
                actual_path,
                self.root / "report",
                config_path=config,
            )
        ).validate()

        self.assertNotIn(
            "visual-mismatch", {item["type"] for item in result["defects"]}
        )
        self.assertTrue(result["passed"], result["defects"])

    def test_vendor_assets_freezes_bytes_and_hash(self) -> None:
        manifest = self._compile()
        screen_path = self.bundle / manifest["screens"][0]["compiledPath"]
        screen = read_json(screen_path)
        screen["assets"] = [
            {
                "screenNodeId": "1:1",
                "variable": "icon",
                "sourceUrl": "https://www.figma.com/api/mcp/asset/test.svg",
                "format": "svg",
                "localPath": None,
                "sha256": None,
                "exactRequired": True,
            }
        ]
        write_json(screen_path, screen)
        def fake_download(source_url, target, **kwargs):
            target.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"/>',
                encoding="utf-8",
            )

        with patch(
            "figma_lossless.assets._download_asset",
            side_effect=fake_download,
        ):
            result = vendor_assets(self.bundle)
        self.assertEqual(result["downloaded"], 1)
        frozen = read_json(screen_path)["assets"][0]
        self.assertTrue((self.bundle / frozen["localPath"]).exists())
        self.assertEqual(len(frozen["sha256"]), 64)

    def test_vendor_assets_rejects_local_file_url(self) -> None:
        manifest = self._compile()
        screen_path = self.bundle / manifest["screens"][0]["compiledPath"]
        screen = read_json(screen_path)
        source = self.root / "private.txt"
        source.write_text("do not copy", encoding="utf-8")
        screen["assets"] = [
            {
                "screenNodeId": "1:1",
                "variable": "icon",
                "sourceUrl": source.as_uri(),
                "format": "svg",
                "localPath": None,
                "sha256": None,
                "exactRequired": True,
            }
        ]
        write_json(screen_path, screen)
        result = vendor_assets(self.bundle)
        self.assertEqual(result["failed"], 1)
        self.assertFalse((self.bundle / "assets" / "1-1-01-icon.txt").exists())

    def test_compiler_rejects_mcp_error_response(self) -> None:
        write_json(
            self.raw / "01-screen.get-design-context.raw.json",
            {
                "isError": True,
                "content": [{"type": "text", "text": "Permission denied"}],
            },
        )
        with self.assertRaisesRegex(ValueError, "isError=true"):
            self._compile()

    def test_missing_geometry_and_style_evidence_fail(self) -> None:
        self._compile()
        actual_path = self.root / "actual.json"
        snapshot = create_snapshot_template(
            self.bundle, actual_path, use_reference_screenshots=True
        )
        element = snapshot["screens"]["1:1"]["elements"]["1:2"]
        element["rect"] = None
        element["style"] = None
        write_json(actual_path, snapshot)
        config = self.root / "self-test-config.json"
        write_json(config, {"allowSelfTestSnapshot": True})
        result = GateValidator(
            ValidateOptions(
                self.bundle,
                actual_path,
                self.root / "report",
                config_path=config,
            )
        ).validate()
        types = {item["type"] for item in result["defects"]}
        self.assertIn("missing-actual-rect", types)
        self.assertIn("missing-actual-style", types)

    def test_bundle_path_escape_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "escapes"):
            resolve_within(self.bundle, "../outside.json", label="test path")

    def test_optional_missing_flow_result_does_not_crash(self) -> None:
        flow = self.root / "flow.json"
        write_json(
            flow,
            {
                "transitions": [
                    {
                        "id": "transition",
                        "from": "1:1",
                        "to": "1:1",
                        "requiresConfirmation": False,
                    }
                ]
            },
        )
        DesignCompiler(
            CompileOptions(
                feature_id="fixture",
                input_dir=self.raw,
                output_dir=self.bundle,
                flow_contract=flow,
            )
        ).compile()
        actual = self.root / "actual.json"
        create_snapshot_template(
            self.bundle, actual, use_reference_screenshots=True
        )
        config = self.root / "config.json"
        write_json(
            config,
            {
                "allowSelfTestSnapshot": True,
                "requireFlowResults": False,
            },
        )
        result = GateValidator(
            ValidateOptions(
                self.bundle,
                actual,
                self.root / "report",
                config_path=config,
            )
        ).validate()
        self.assertEqual(result["gates"]["flow"]["status"], "PASS")

    def test_hidden_parent_marks_descendants_effectively_hidden(self) -> None:
        compiler = DesignCompiler(
            CompileOptions("fixture", self.raw, self.bundle)
        )
        nodes = compiler._parse_metadata_nodes(
            """<frame id="1:1" name="root" x="0" y="0" width="10" height="10">
  <frame id="1:2" name="hidden" x="0" y="0" width="10" height="10" hidden="true">
    <text id="1:3" name="child" x="0" y="0" width="10" height="10" />
  </frame>
</frame>"""
        )
        child = next(item for item in nodes if item["nodeId"] == "1:3")
        self.assertFalse(child["hidden"])
        self.assertTrue(child["effectiveHidden"])


if __name__ == "__main__":
    unittest.main()
