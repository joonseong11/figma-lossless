from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "figma-lossless"
MARKETPLACE_NAME = "figma-lossless-dev"
LAUNCHER = (
    REPOSITORY_ROOT / "skills" / "verify-design" / "scripts" / "run_harness.py"
)


def _load_launcher():
    spec = importlib.util.spec_from_file_location("run_harness", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pillow_available() -> bool:
    return importlib.util.find_spec("PIL") is not None


class PluginManifestTest(unittest.TestCase):
    def _manifest(self, directory: str) -> dict:
        return json.loads(
            (REPOSITORY_ROOT / directory / "plugin.json").read_text(encoding="utf-8")
        )

    def test_codex_manifest_exposes_the_packaged_skill(self) -> None:
        manifest = self._manifest(".codex-plugin")
        self.assertEqual(manifest["name"], PLUGIN_NAME)
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertTrue(
            (
                REPOSITORY_ROOT / "skills" / "verify-design" / "SKILL.md"
            ).is_file()
        )
        skill_metadata = (
            REPOSITORY_ROOT
            / "skills"
            / "verify-design"
            / "agents"
            / "openai.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn(f"${manifest['name']}:verify-design", skill_metadata)

    def test_claude_manifest_matches_the_codex_manifest(self) -> None:
        claude = self._manifest(".claude-plugin")
        codex = self._manifest(".codex-plugin")
        self.assertEqual(claude["name"], PLUGIN_NAME)
        self.assertEqual(claude["name"], codex["name"])
        # A release bumps four declarations; pin them to each other rather than
        # to a literal so a partial bump fails instead of aging into a lie.
        self.assertEqual(claude["version"], codex["version"])
        marketplace = json.loads(
            (REPOSITORY_ROOT / ".claude-plugin" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(marketplace["plugins"][0]["version"], claude["version"])
        pyproject = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'version = "{claude["version"]}"', pyproject)
        # Claude Code discovers `skills/` at the plugin root by default, so the
        # manifest must not point elsewhere.
        self.assertNotIn("skills", claude)

    def test_claude_marketplace_publishes_the_repository_root(self) -> None:
        marketplace = json.loads(
            (REPOSITORY_ROOT / ".claude-plugin" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("owner", marketplace)
        entries = marketplace["plugins"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], PLUGIN_NAME)
        self.assertEqual(entries[0]["source"], "./")

    def test_codex_marketplace_publishes_the_repository_root(self) -> None:
        marketplace = json.loads(
            (REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(marketplace["name"], MARKETPLACE_NAME)
        entries = marketplace["plugins"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], PLUGIN_NAME)
        self.assertEqual(entries[0]["source"], {"source": "local", "path": "./"})
        # Codex rejects any other value; keep the enum honest.
        self.assertIn(
            entries[0]["policy"]["authentication"], {"ON_INSTALL", "ON_USE"}
        )

    def test_both_marketplaces_agree_on_the_install_id(self) -> None:
        claude = json.loads(
            (REPOSITORY_ROOT / ".claude-plugin" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        codex = json.loads(
            (REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(claude["name"], codex["name"])
        install_id = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"claude plugin install {install_id}", readme)
        self.assertIn(f"codex plugin add {install_id}", readme)

    def test_skill_resolves_paths_from_the_plugin_root(self) -> None:
        skill = (
            REPOSITORY_ROOT / "skills" / "verify-design" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("CLAUDE_PLUGIN_ROOT", skill)
        self.assertIn(
            "${PLUGIN_ROOT}/skills/verify-design/scripts/run_harness.py", skill
        )
        self.assertIn("${PLUGIN_ROOT}/adapters/playwright-capture.mjs", skill)


class LauncherBootstrapTest(unittest.TestCase):
    def test_declared_dependencies_match_pyproject(self) -> None:
        try:
            import tomllib
        except ModuleNotFoundError:
            self.skipTest("tomllib requires Python 3.11+")
        pyproject = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        launcher = _load_launcher()
        self.assertEqual(
            list(launcher.HARNESS_DEPENDENCIES),
            pyproject["project"]["dependencies"],
        )

    def test_cache_location_honours_the_override(self) -> None:
        launcher = _load_launcher()
        with tempfile.TemporaryDirectory() as directory:
            os.environ[launcher.CACHE_OVERRIDE] = directory
            try:
                venv = launcher._venv_dir()
            finally:
                del os.environ[launcher.CACHE_OVERRIDE]
        self.assertEqual(venv.parent, Path(directory))
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        self.assertEqual(venv.name, f"venv-py{version}")

    @unittest.skipUnless(_pillow_available(), "Pillow is required")
    def test_packaged_launcher_resolves_the_repository_runtime(self) -> None:
        environment = dict(os.environ)
        # The current interpreter already satisfies the dependencies, so assert
        # the fast path without allowing a network install to mask a regression.
        environment["FIGMA_LOSSLESS_NO_BOOTSTRAP"] = "1"
        result = subprocess.run(
            [sys.executable, str(LAUNCHER), "--help"],
            cwd=Path(tempfile.gettempdir()),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Collect Figma REST evidence losslessly", result.stdout)

    def test_launcher_reports_a_clear_error_when_bootstrap_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_without_pillow(
                directory, {"FIGMA_LOSSLESS_NO_BOOTSTRAP": "1"}
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Pillow is required", result.stderr)
        self.assertNotIn("preparing an isolated Python environment", result.stderr)

    def test_launcher_attempts_bootstrap_and_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # A regular file where the cache root must be forces mkdir to fail,
            # exercising the bootstrap branch without touching the network.
            blocker = Path(directory) / "blocker"
            blocker.write_text("not a directory\n", encoding="utf-8")
            result = self._run_without_pillow(
                directory, {"FIGMA_LOSSLESS_CACHE_DIR": str(blocker / "cache")}
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("preparing an isolated Python environment", result.stderr)
        self.assertIn("Pillow is required", result.stderr)

    def _run_without_pillow(self, directory: str, overrides: dict):
        """Run the launcher with `PIL` made unimportable for the child process."""
        blocked = Path(directory) / "sitedir"
        blocked.mkdir()
        (blocked / "sitecustomize.py").write_text(
            """
import sys


class _BlockPillow:
    def find_spec(self, name, path=None, target=None):
        if name == "PIL" or name.startswith("PIL."):
            raise ModuleNotFoundError("No module named 'PIL'", name="PIL")
        return None


sys.meta_path.insert(0, _BlockPillow())
""".lstrip(),
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment.pop("FIGMA_LOSSLESS_NO_BOOTSTRAP", None)
        environment.pop("FIGMA_LOSSLESS_CACHE_DIR", None)
        environment.pop("FIGMA_LOSSLESS_BOOTSTRAPPED", None)
        environment["PYTHONPATH"] = str(blocked)
        environment.update(overrides)
        return subprocess.run(
            [sys.executable, str(LAUNCHER), "--help"],
            cwd=Path(tempfile.gettempdir()),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )


class CaptureAdapterTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_capture_adapter_resolves_playwright_from_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            playwright = target / "node_modules" / "playwright"
            playwright.mkdir(parents=True)
            (target / "package.json").write_text(
                '{"name":"adapter-target","private":true}\n', encoding="utf-8"
            )
            (playwright / "index.js").write_text(
                """
const fs = require("node:fs/promises");

exports.chromium = {
  async launch() {
    return {
      async newContext() {
        return {
          async newPage() {
            return {
              async route() {},
              async goto() {},
              async setViewportSize() {},
              locator() { return { async waitFor() {} }; },
              async addStyleTag() {},
              async screenshot({ path }) { await fs.writeFile(path, "fake-png"); },
              async evaluate(callback, screenNodeId) {
                if (callback.toString().includes('querySelectorAll("[data-slot]")')) {
                  return {
                    slots: { "user.email": { text: "real@example.com" } },
                    duplicates: [],
                  };
                }
                if (screenNodeId === undefined) return undefined;
                return {
                  elements: {}, components: {}, assets: {}, duplicates: [], customProperties: {},
                };
              },
            };
          },
        };
      },
      async close() {},
    };
  },
};
""".lstrip(),
                encoding="utf-8",
            )
            plan = target / "capture-plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "baseUrl": "http://127.0.0.1:3000",
                        "featureId": "adapter-smoke",
                        "screens": [
                            {
                                "nodeId": "1:1",
                                "name": "Screen",
                                "route": "/screen",
                                "routeKind": "product",
                            },
                            {
                                "nodeId": "1:2",
                                "name": "Fixture screen",
                                "route": "/fixture",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = target / "actual.json"
            adapter = REPOSITORY_ROOT / "adapters" / "playwright-capture.mjs"
            result = subprocess.run(
                [
                    "node",
                    str(adapter),
                    "--plan",
                    str(plan),
                    "--output",
                    str(output),
                ],
                cwd=target,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            snapshot = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["provenance"], "browser-capture")
            self.assertEqual(
                snapshot["screens"]["1:1"]["routeKind"], "product"
            )
            self.assertEqual(
                snapshot["screens"]["1:1"]["slots"]["user.email"]["text"],
                "real@example.com",
            )
            self.assertNotIn("elements", snapshot["screens"]["1:1"])
            self.assertEqual(
                snapshot["screens"]["1:2"]["routeKind"], "fixture"
            )


if __name__ == "__main__":
    unittest.main()
