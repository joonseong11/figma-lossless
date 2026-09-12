"""Extract mode is the default; verification is locked until unlocked on purpose.

The harness is published for people who want a lossless spec first. Nothing
that judges an implementation — capture, gates, the Stop-blocking hooks —
may switch itself on because someone ran `collect`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from figma_lossless import mode as mode_module
from figma_lossless.cli import main

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPOSITORY_ROOT / "hooks" / "harness_hook.py"
ADAPTER = REPOSITORY_ROOT / "adapters" / "playwright-capture.mjs"

LOCKED_COMMANDS = (
    ["validate", "--bundle", "b", "--actual", "a.json", "--output", "r"],
    ["propose-slots", "--bundle", "b", "--output", "s.json"],
    ["propose-reuse", "--bundle", "b", "--repo-index", "i.json", "--output", "o"],
    ["vendor-assets", "--bundle", "b"],
    ["snapshot-template", "--bundle", "b", "--output", "t.json"],
    ["serve-report", "--directory", "r"],
)


def extract_env(base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    env.pop("FIGMA_LOSSLESS_MODE", None)
    return env


def run_hook(event: str, payload: dict, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK), event],
        input=json.dumps(payload),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def run_hook_subcommand(args: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


class ResolveModeTest(unittest.TestCase):
    def test_default_is_extract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                mode_module.resolve_mode(directory, environ={}), ("extract", "default")
            )

    def test_mode_file_unlocks_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = mode_module.set_mode("verify", directory)
            self.assertEqual(path, Path(directory) / ".figma-lossless" / "mode.json")
            self.assertEqual(
                mode_module.resolve_mode(directory, environ={}), ("verify", "file")
            )
            self.assertTrue(mode_module.clear_mode(directory))
            self.assertFalse(mode_module.clear_mode(directory))
            self.assertEqual(
                mode_module.resolve_mode(directory, environ={}), ("extract", "default")
            )

    def test_environment_wins_over_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mode_module.set_mode("verify", directory)
            self.assertEqual(
                mode_module.resolve_mode(
                    directory, environ={"FIGMA_LOSSLESS_MODE": "extract"}
                ),
                ("extract", "env"),
            )

    def test_garbage_never_unlocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = mode_module.mode_file(directory)
            target.parent.mkdir(parents=True)
            target.write_text('{"mode": "yes please"}', encoding="utf-8")
            self.assertEqual(
                mode_module.resolve_mode(
                    directory, environ={"FIGMA_LOSSLESS_MODE": "VERIFY"}
                ),
                ("extract", "default"),
            )
            target.write_text("not json", encoding="utf-8")
            self.assertEqual(
                mode_module.resolve_mode(directory, environ={})[0], "extract"
            )

    def test_nearest_mode_file_above_cwd_applies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "packages" / "web"
            nested.mkdir(parents=True)
            mode_module.set_mode("verify", root)
            self.assertEqual(mode_module.find_mode_file(nested), mode_module.mode_file(root))
            self.assertEqual(mode_module.resolve_mode(nested, environ={}), ("verify", "file"))
            # A closer declaration wins over the one above it.
            mode_module.set_mode("extract", nested)
            self.assertEqual(mode_module.resolve_mode(nested, environ={}), ("extract", "file"))
            self.assertEqual(mode_module.resolve_mode(root, environ={}), ("verify", "file"))

    def test_set_mode_rejects_unknown_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                mode_module.set_mode("audit", directory)

    def test_extract_commands_are_exactly_the_read_only_ones(self) -> None:
        self.assertEqual(
            mode_module.EXTRACT_COMMANDS,
            {"collect", "compile", "export-design", "export-copy", "mode"},
        )
        for argv in LOCKED_COMMANDS:
            self.assertTrue(mode_module.is_locked(argv[0], "extract"), argv[0])
            self.assertFalse(mode_module.is_locked(argv[0], "verify"), argv[0])
        for command in mode_module.EXTRACT_COMMANDS:
            self.assertFalse(mode_module.is_locked(command, "extract"), command)


class CliLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.cwd = Path(self.directory.name)
        self._old_cwd = os.getcwd()
        os.chdir(self.cwd)
        self._env = mock.patch.dict(os.environ, extract_env(), clear=True)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        os.chdir(self._old_cwd)
        self.directory.cleanup()

    def _capture(self, argv: list[str]) -> tuple[int, str, str]:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_verification_commands_refuse_in_extract_mode(self) -> None:
        for argv in LOCKED_COMMANDS:
            code, out, err = self._capture(argv)
            self.assertEqual(code, 1, argv[0])
            self.assertIn("verification mode is locked", err, argv[0])
            self.assertIn("mode --set verify", err, argv[0])
            self.assertEqual(out, "", argv[0])
        # Refusal happens before any output directory is created.
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])

    def test_mode_shows_sets_and_clears(self) -> None:
        code, out, _ = self._capture(["mode"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"mode": "extract", "source": "default"})

        code, out, _ = self._capture(["mode", "--set", "verify"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "verify")
        self.assertTrue((self.cwd / ".figma-lossless" / "mode.json").exists())

        code, out, _ = self._capture(["mode"])
        self.assertEqual(json.loads(out), {"mode": "verify", "source": "file"})

        code, out, _ = self._capture(["mode", "--clear"])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out), {"mode": "extract", "source": "default", "removed": True}
        )

    def test_unlocked_directory_lets_verification_commands_past_the_lock(self) -> None:
        self._capture(["mode", "--set", "verify"])
        # The command now fails on its real input (a bundle that does not
        # exist), not on the lock — proving the lock stepped aside.
        code, _, err = self._capture(
            ["propose-slots", "--bundle", "missing", "--output", "s.json"]
        )
        self.assertNotIn("verification mode is locked", err)
        self.assertEqual(code, 1)

    def test_help_works_in_extract_mode(self) -> None:
        for argv in (["--help"], ["validate", "--help"], ["mode", "--help"]):
            with self.assertRaises(SystemExit) as raised:
                self._capture(argv)
            self.assertEqual(raised.exception.code, 0, argv)

    def test_set_reports_the_effective_mode_when_the_environment_overrides(self) -> None:
        with mock.patch.dict(os.environ, {"FIGMA_LOSSLESS_MODE": "verify"}):
            code, out, err = self._capture(["mode", "--set", "extract"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual((payload["mode"], payload["source"], payload["fileMode"]), ("verify", "env", "extract"))
        self.assertIn("overrides", err)
        with mock.patch.dict(os.environ, {"FIGMA_LOSSLESS_MODE": "extract"}):
            code, out, _ = self._capture(["mode", "--set", "verify"])
        self.assertEqual(json.loads(out)["mode"], "extract")
        # Without the override the file speaks for itself.
        code, out, err = self._capture(["mode", "--set", "verify"])
        self.assertEqual((json.loads(out)["mode"], json.loads(out)["source"]), ("verify", "file"))
        self.assertEqual(err, "")

    def test_a_subdirectory_inherits_the_project_unlock(self) -> None:
        self._capture(["mode", "--set", "verify"])
        nested = self.cwd / "packages" / "web"
        nested.mkdir(parents=True)
        os.chdir(nested)
        code, _, err = self._capture(["propose-slots", "--bundle", "missing", "--output", "s.json"])
        self.assertNotIn("verification mode is locked", err)
        self.assertEqual(code, 1)

    def test_extraction_commands_are_not_locked(self) -> None:
        # export-design on a missing bundle fails on the bundle, not the lock.
        code, _, err = self._capture(
            ["export-design", "--bundle", "missing", "--output", "spec.md"]
        )
        self.assertNotIn("verification mode is locked", err)
        self.assertEqual(code, 1)


class CaptureAdapterLockTest(unittest.TestCase):
    """The Node capture adapter honours the same lock as the Python CLI."""

    def _run(self, cwd: Path, env: dict) -> subprocess.CompletedProcess:
        plan = cwd / "capture-plan.json"
        plan.write_text(json.dumps({"baseUrl": "http://127.0.0.1:3000", "screens": []}))
        return subprocess.run(
            ["node", str(ADAPTER), "--plan", str(plan), "--output", str(cwd / "out" / "actual.json")],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_refuses_in_extract_mode_without_touching_the_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = self._run(cwd, extract_env())
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("verification mode is locked", result.stderr)
            self.assertFalse((cwd / "out").exists())

    def test_unlock_lets_it_past_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            mode_module.set_mode("verify", cwd)
            result = self._run(cwd, extract_env())
            # It now fails for the real reason a bare directory cannot capture:
            # no Playwright in the target repository, not the lock.
            self.assertNotIn("verification mode is locked", result.stderr)
            self.assertIn("Playwright must be installed", result.stderr)
            self.assertFalse((cwd / "out" / "actual.json").exists())


class HooksAreInertInExtractModeTest(unittest.TestCase):
    """Every lifecycle event is a no-op until the directory is unlocked."""

    def _active_state(self, cwd: Path) -> None:
        state_dir = cwd / ".figma-lossless"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "state.json").write_text(
            json.dumps(
                {
                    "active": True,
                    "scope": "full",
                    "terminal": False,
                    "pausedForUser": False,
                    "pauseReason": None,
                    "blockCount": 0,
                    "lastActivityAt": "2999-01-01T00:00:00+00:00",
                    "lastValidate": None,
                }
            ),
            encoding="utf-8",
        )

    def test_collect_does_not_start_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"python3 /opt/run_harness.py collect --output {cwd}/evidence"
                },
                "tool_response": {"stdout": "{}"},
            }
            result = run_hook("PostToolUse", payload, cwd, extract_env())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((cwd / ".figma-lossless" / "state.json").exists())

    def test_stop_never_blocks_and_session_start_stays_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._active_state(cwd)
            payload = {"cwd": str(cwd), "last_assistant_message": "Done."}
            for event in ("Stop", "SessionStart", "UserPromptSubmit"):
                result = run_hook(event, {**payload, "prompt": "/verify-design x"}, cwd, extract_env())
                self.assertEqual(result.returncode, 0, (event, result.stderr))
                self.assertEqual(result.stdout.strip(), "", event)
            # State on disk is untouched, so unlocking later resumes it.
            state = json.loads((cwd / ".figma-lossless" / "state.json").read_text())
            self.assertEqual(state["blockCount"], 0)
            self.assertTrue(state["active"])

    def test_gate_config_edits_are_not_challenged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Write",
                "tool_input": {
                    "file_path": str(cwd / "gate-config.json"),
                    "content": '{"geometryTolerancePx": 40}',
                },
            }
            result = run_hook("PreToolUse", payload, cwd, extract_env())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_scope_declaration_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_hook_subcommand(["scope", "--set", "full"], cwd, extract_env())
            self.assertEqual(result.returncode, 1)
            self.assertIn("mode --set verify", result.stderr)
            self.assertFalse((cwd / ".figma-lossless" / "state.json").exists())

    def test_mode_file_unlocks_the_hooks_too(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            mode_module.set_mode("verify", cwd)
            self._active_state(cwd)
            payload = {"cwd": str(cwd), "last_assistant_message": "Done."}
            result = run_hook("Stop", payload, cwd, extract_env())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_hook_resolves_the_nearest_mode_file_above_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "packages" / "web"
            nested.mkdir(parents=True)
            mode_module.set_mode("verify", root)
            self._active_state(nested)
            payload = {"cwd": str(nested), "last_assistant_message": "Done."}
            result = run_hook("Stop", payload, nested, extract_env())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_hook_and_package_resolve_the_same_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            cases = [
                (extract_env(), None, "extract"),
                (extract_env(), "verify", "verify"),
                ({**extract_env(), "FIGMA_LOSSLESS_MODE": "verify"}, None, "verify"),
                ({**extract_env(), "FIGMA_LOSSLESS_MODE": "extract"}, "verify", "extract"),
                ({**extract_env(), "FIGMA_LOSSLESS_MODE": "bogus"}, "verify", "verify"),
            ]
            for env, file_mode, expected in cases:
                mode_module.clear_mode(cwd)
                if file_mode:
                    mode_module.set_mode(file_mode, cwd)
                self.assertEqual(
                    mode_module.resolve_mode(cwd, environ=env)[0], expected, (env, file_mode)
                )
                probe = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import importlib.util,sys;"
                        f"spec=importlib.util.spec_from_file_location('h', {str(HOOK)!r});"
                        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
                        f"print(m._resolve_mode({str(cwd)!r}))",
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                )
                self.assertEqual(probe.stdout.strip(), expected, (env, file_mode, probe.stderr))


if __name__ == "__main__":
    unittest.main()
