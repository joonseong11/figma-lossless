from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPOSITORY_ROOT / "hooks" / "harness_hook.py"
HOOKS_CONFIG = REPOSITORY_ROOT / "hooks" / "hooks.json"
STALE_AFTER_SECONDS = 86400


def verify_env(base: dict | None = None) -> dict:
    """Environment with verify mode unlocked.

    The harness ships in extract mode, where every hook is inert; the
    enforcement behaviour these tests pin only exists once verify mode is
    unlocked. tests/test_mode.py covers the locked default.
    """

    env = dict(os.environ if base is None else base)
    env["FIGMA_LOSSLESS_MODE"] = "verify"
    return env


def fresh_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def stale_timestamp() -> str:
    age = timedelta(seconds=STALE_AFTER_SECONDS + 3600)
    return (datetime.now(timezone.utc) - age).isoformat()


def run_hook(
    event: str,
    payload: dict | None = None,
    cwd: Path | None = None,
    raw_stdin: str | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    stdin = raw_stdin if raw_stdin is not None else json.dumps(payload or {})
    return subprocess.run(
        [sys.executable, str(HOOK), event],
        input=stdin,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env=verify_env(env),
        check=False,
    )


def run_pause(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK), "pause", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=verify_env(),
        check=False,
    )


def state_path(cwd: Path) -> Path:
    return cwd / ".figma-lossless" / "state.json"


def read_state(cwd: Path) -> dict:
    return json.loads(state_path(cwd).read_text(encoding="utf-8"))


def write_gate_results(directory: Path, passed: bool) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    gates = {
        "coverage": {"status": "PASS"},
        "structure": {"status": "PASS" if passed else "FAIL"},
    }
    (directory / "gate-results.json").write_text(
        json.dumps({"passed": passed, "gates": gates}), encoding="utf-8"
    )


class HarnessConfigTest(unittest.TestCase):
    def test_hooks_json_parses_and_scripts_exist(self) -> None:
        config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
        self.assertIn("hooks", config)
        for event, matcher_groups in config["hooks"].items():
            self.assertIsInstance(matcher_groups, list, event)
            for group in matcher_groups:
                for handler in group["hooks"]:
                    self.assertEqual(handler["type"], "command")
                    self.assertEqual(handler["command"], "python3")
                    script = handler["args"][0].replace(
                        "${CLAUDE_PLUGIN_ROOT}", str(REPOSITORY_ROOT)
                    )
                    self.assertTrue(
                        Path(script).is_file(), f"missing script: {script}"
                    )

    def test_hooks_wired_for_the_five_documented_events(self) -> None:
        config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(
            set(config["hooks"]),
            {
                "PostToolUse",
                "Stop",
                "UserPromptSubmit",
                "SessionStart",
                "PreToolUse",
            },
        )


class PostToolUseTest(unittest.TestCase):
    def test_collect_activates_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py collect --file-key abc "
                        f"--node-ids 1:1 --output {cwd}/evidence --include-images"
                    )
                },
                "tool_response": {
                    "stdout": "ok",
                    "stderr": "",
                    "interrupted": False,
                    "isImage": False,
                },
            }
            result = run_hook("PostToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertTrue(state["active"])
            self.assertFalse(state["terminal"])

    def test_compile_activates_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py compile --rest-input "
                        f"{cwd}/evidence --output {cwd}/bundle --feature-id x"
                    )
                },
                "tool_response": {"stdout": "{}", "stderr": ""},
            }
            result = run_hook("PostToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertTrue(state["active"])
            self.assertFalse(state["terminal"])

    def test_validate_all_gates_passed_reaches_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            report_dir = cwd / "report"
            write_gate_results(report_dir, passed=True)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py validate --bundle "
                        f"{cwd}/bundle --actual {cwd}/actual.json --output "
                        f"{report_dir}"
                    )
                },
                "tool_response": {"stdout": "{}", "stderr": ""},
            }
            result = run_hook("PostToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertTrue(state["terminal"])
            self.assertEqual(state["scope"], "full")
            self.assertTrue(state["lastValidate"]["passed"])

    def test_validate_with_a_failed_gate_is_not_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            report_dir = cwd / "report"
            write_gate_results(report_dir, passed=False)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py validate --bundle "
                        f"{cwd}/bundle --actual {cwd}/actual.json --output "
                        f"{report_dir}"
                    )
                },
                "tool_response": {"stdout": "{}", "stderr": ""},
            }
            result = run_hook("PostToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertFalse(state["terminal"])
            self.assertFalse(state["lastValidate"]["passed"])

    def test_validate_never_trusts_a_missing_gate_results_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            report_dir = cwd / "report-missing"
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py validate --bundle "
                        f"{cwd}/bundle --actual {cwd}/actual.json --output "
                        f"{report_dir}"
                    )
                },
                "tool_response": {"stdout": "{}", "stderr": ""},
            }
            result = run_hook("PostToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertFalse(state["terminal"])

    def test_validate_with_unexpanded_shell_variable_in_output_is_not_recorded(
        self,
    ) -> None:
        # Reproduces: `--output $ART/report` reaches the hook as a literal,
        # unexpanded token (shlex.split never expands shell variables, and
        # the hook has no access to the invoking shell's environment). The
        # buggy behavior recorded "<cwd>/$ART/report" as outputDir — a path
        # that can never exist — permanently hiding a real pass. With no
        # usable "report" path in stdout to fall back on, the hook must now
        # treat the output dir as unknown rather than record the literal.
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py validate --bundle "
                        f"{cwd}/bundle --actual {cwd}/actual.json --output "
                        "$ART/report"
                    )
                },
                "tool_response": {"stdout": "{}", "stderr": ""},
            }
            result = run_hook("PostToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertFalse(state["terminal"])
            self.assertIsNone(state["lastValidate"]["outputDir"])

    def test_validate_recovers_output_dir_from_cli_stdout_despite_unexpanded_var(
        self,
    ) -> None:
        # The real shell expands $ART before invoking the CLI, so the CLI's
        # own stdout reports the true, already-resolved output dir even
        # though the command *text* the hook sees still contains "$ART".
        # Preferring that stdout-reported path over re-parsing the command
        # closes the gap for real world use without needing to guess at
        # shell expansion.
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            real_output_dir = cwd / "actual-artifacts" / "report"
            write_gate_results(real_output_dir, passed=True)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py validate --bundle "
                        f"{cwd}/bundle --actual {cwd}/actual.json --output "
                        "$ART/report"
                    )
                },
                "tool_response": {
                    "stdout": json.dumps(
                        {
                            "passed": True,
                            "report": str(real_output_dir / "report.html"),
                        }
                    ),
                    "stderr": "",
                },
            }
            result = run_hook("PostToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertTrue(state["terminal"])
            self.assertEqual(
                state["lastValidate"]["outputDir"], str(real_output_dir)
            )

    def test_playwright_capture_sets_scope_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "node /opt/playwright-capture.mjs --plan "
                        f"{cwd}/plan.json --output {cwd}/actual.json"
                    )
                },
                "tool_response": {"stdout": "{}", "stderr": ""},
            }
            result = run_hook("PostToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertEqual(state["scope"], "full")

    def test_export_copy_success_reaches_terminal_when_scope_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py export-copy --bundle "
                        f"{cwd}/bundle --output {cwd}/copy.json"
                    )
                },
                "tool_response": {
                    "stdout": "ok",
                    "stderr": "",
                    "interrupted": False,
                },
            }
            result = run_hook("PostToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertTrue(state["terminal"])

    def test_export_design_reaches_terminal_and_names_its_scope(self) -> None:
        """Producing the spec is the whole job for a hand-off session.

        Without this, `collect`/`compile` arm enforcement and the only exits
        are a passing validate -- of an implementation that does not exist yet
        -- or an export-copy that would mislabel the run as a locale audit.
        """

        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py export-design --bundle "
                        f"{cwd}/bundle --output {cwd}/spec.md"
                    )
                },
                "tool_response": {
                    "stdout": "ok",
                    "stderr": "",
                    "interrupted": False,
                },
            }
            result = run_hook("PostToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertTrue(state["terminal"])
            self.assertEqual(state["scope"], "design-spec")

    def test_export_design_does_not_terminate_a_full_verification(self) -> None:
        """A mid-workflow spec dump still owes the capture and validate steps."""

        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            capture_payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": "node adapters/playwright-capture.mjs --plan p.json"
                },
                "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
            }
            run_hook("PostToolUse", capture_payload, cwd=cwd)
            export_payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py export-design --bundle "
                        f"{cwd}/bundle --output {cwd}/spec.md"
                    )
                },
                "tool_response": {"stdout": "ok", "stderr": "", "interrupted": False},
            }
            result = run_hook("PostToolUse", export_payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertEqual(state["scope"], "full")
            self.assertFalse(state["terminal"])

    def test_export_copy_does_not_terminate_a_full_scope_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            # Establish scope=full first, as a real validate/capture run would.
            capture_payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"node /opt/playwright-capture.mjs --output {cwd}/a.json"
                },
                "tool_response": {"stdout": "{}", "stderr": ""},
            }
            run_hook("PostToolUse", capture_payload, cwd=cwd)

            export_payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py export-copy --bundle "
                        f"{cwd}/bundle --output {cwd}/copy.json"
                    )
                },
                "tool_response": {"stdout": "ok", "stderr": ""},
            }
            result = run_hook("PostToolUse", export_payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertEqual(state["scope"], "full")
            self.assertFalse(state["terminal"])

    def test_export_copy_with_tool_error_does_not_reach_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py export-copy --bundle "
                        f"{cwd}/bundle --output {cwd}/copy.json"
                    )
                },
                "tool_response": {
                    "stdout": "",
                    "stderr": "boom",
                    "error": "failed",
                },
            }
            result = run_hook("PostToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertFalse(state["terminal"])

    def test_export_copy_before_capture_no_longer_disarms_full_verification(
        self,
    ) -> None:
        # Reproduces defect 1: SKILL.md documents dumping the copy contract
        # (collect -> compile -> export-copy) *inside* a full-verification
        # run, to plan an i18n catalog, before capture/validate ever run.
        # At that point scope is genuinely still "unknown", so export-copy
        # used to mark the workflow terminal as a (wrongly) inferred
        # standalone copy audit -- disarming the Stop gate for the entire
        # implement/capture/validate window that followed. Once scope is
        # later proven "full" by a capture/validate command, that stale
        # terminal claim must not survive.
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)

            collect_payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"python3 /opt/run_harness.py collect --output {cwd}/evidence"
                },
                "tool_response": {"stdout": "{}", "stderr": ""},
            }
            run_hook("PostToolUse", collect_payload, cwd=cwd)

            compile_payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"python3 /opt/run_harness.py compile --output {cwd}/bundle"
                },
                "tool_response": {"stdout": "{}", "stderr": ""},
            }
            run_hook("PostToolUse", compile_payload, cwd=cwd)

            export_payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py export-copy --bundle "
                        f"{cwd}/bundle --output {cwd}/copy.json"
                    )
                },
                "tool_response": {"stdout": "ok", "stderr": ""},
            }
            run_hook("PostToolUse", export_payload, cwd=cwd)

            # Confirms the ambiguity: with nothing else observed yet, the
            # hook still can't tell this apart from a real standalone copy
            # audit, so it stays with today's default (this is the residual
            # window that an explicit `scope --set full` closes, tested
            # separately below).
            state = read_state(cwd)
            self.assertEqual(state["scope"], "copy-audit")
            self.assertTrue(state["terminal"])

            capture_payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"node /opt/playwright-capture.mjs --output {cwd}/actual.json"
                },
                "tool_response": {"stdout": "{}", "stderr": ""},
            }
            result = run_hook("PostToolUse", capture_payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)

            state = read_state(cwd)
            self.assertEqual(state["scope"], "full")
            self.assertFalse(
                state["terminal"],
                "stale terminal=True from the earlier ambiguous export-copy "
                "must not survive scope being proven full",
            )

    def test_declaring_full_scope_up_front_prevents_export_copy_from_ever_faking_terminal(
        self,
    ) -> None:
        # The root-cause fix: declaring scope explicitly removes the
        # ambiguity instead of leaving a window for it, unlike the
        # capture/validate-time invalidation covered above.
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            scope_result = subprocess.run(
                [sys.executable, str(HOOK), "scope", "--set", "full"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                env=verify_env(),
                check=False,
            )
            self.assertEqual(scope_result.returncode, 0, scope_result.stderr)

            for command in (
                f"python3 /opt/run_harness.py collect --output {cwd}/evidence",
                f"python3 /opt/run_harness.py compile --output {cwd}/bundle",
                (
                    "python3 /opt/run_harness.py export-copy --bundle "
                    f"{cwd}/bundle --output {cwd}/copy.json"
                ),
            ):
                payload = {
                    "cwd": str(cwd),
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                    "tool_response": {"stdout": "ok", "stderr": ""},
                }
                result = run_hook("PostToolUse", payload, cwd=cwd)
                self.assertEqual(result.returncode, 0, result.stderr)
                state = read_state(cwd)
                self.assertEqual(state["scope"], "full")
                self.assertFalse(state["terminal"])

    def test_non_harness_bash_command_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {"command": "ls -la"},
                "tool_response": {"stdout": "", "stderr": ""},
            }
            result = run_hook("PostToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_path(cwd).exists())

    def test_any_harness_command_resets_block_count_and_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            state_path(cwd).parent.mkdir(parents=True)
            state_path(cwd).write_text(
                json.dumps(
                    {
                        "active": True,
                        "scope": "full",
                        "terminal": False,
                        "pausedForUser": False,
                        "pauseReason": None,
                        "blockCount": 2,
                        "lastActivityAt": None,
                        "lastValidate": None,
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"python3 /opt/run_harness.py vendor-assets --bundle {cwd}/bundle"
                },
                "tool_response": {"stdout": "{}", "stderr": ""},
            }
            result = run_hook("PostToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertEqual(state["blockCount"], 0)
            self.assertIsNotNone(state["lastActivityAt"])


class StopHookTest(unittest.TestCase):
    def _seed_state(self, cwd: Path, **overrides) -> None:
        state = {
            "active": True,
            "scope": "full",
            "terminal": False,
            "pausedForUser": False,
            "pauseReason": None,
            "blockCount": 0,
            "lastActivityAt": fresh_timestamp(),
            "lastValidate": None,
        }
        state.update(overrides)
        state_path(cwd).parent.mkdir(parents=True, exist_ok=True)
        state_path(cwd).write_text(json.dumps(state), encoding="utf-8")

    def test_stop_blocks_when_active_and_not_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._seed_state(cwd)
            result = run_hook("Stop", {"cwd": str(cwd), "stop_hook_active": False}, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["decision"], "block")
            self.assertTrue(output["reason"])
            self.assertEqual(read_state(cwd)["blockCount"], 1)

    def test_stop_allows_when_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._seed_state(cwd, terminal=True)
            result = run_hook("Stop", {"cwd": str(cwd)}, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_stop_allows_when_paused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._seed_state(cwd, pausedForUser=True, pauseReason="waiting on user")
            result = run_hook("Stop", {"cwd": str(cwd)}, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_stop_allows_when_no_state_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_hook("Stop", {"cwd": str(cwd)}, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_stop_allows_when_not_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._seed_state(cwd, active=False)
            result = run_hook("Stop", {"cwd": str(cwd)}, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_stop_loop_guard_allows_after_repeated_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._seed_state(cwd)
            decisions = []
            for _ in range(4):
                result = run_hook("Stop", {"cwd": str(cwd)}, cwd=cwd)
                self.assertEqual(result.returncode, 0, result.stderr)
                decisions.append(result.stdout.strip())
            self.assertEqual(decisions.count(""), 2)
            blocked = [d for d in decisions if d]
            self.assertEqual(len(blocked), 2)
            for entry in blocked:
                self.assertEqual(json.loads(entry)["decision"], "block")
            # 3rd and 4th calls give up enforcing once blockCount reaches 3.
            self.assertEqual(decisions[2], "")
            self.assertEqual(decisions[3], "")

    def test_stop_allows_and_self_heals_a_stale_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._seed_state(cwd, lastActivityAt=stale_timestamp())
            result = run_hook("Stop", {"cwd": str(cwd)}, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")
            state = read_state(cwd)
            self.assertFalse(state["active"])

    def test_stop_allows_a_workflow_with_no_lastActivityAt_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._seed_state(cwd, lastActivityAt=None)
            result = run_hook("Stop", {"cwd": str(cwd)}, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")
            self.assertFalse(read_state(cwd)["active"])

    def test_stop_treats_an_unparseable_lastActivityAt_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._seed_state(cwd, lastActivityAt="not-a-timestamp")
            result = run_hook("Stop", {"cwd": str(cwd)}, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")
            self.assertFalse(read_state(cwd)["active"])

    def test_stop_does_not_refresh_lastActivityAt_when_it_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            just_under_stale = (
                datetime.now(timezone.utc)
                - timedelta(seconds=STALE_AFTER_SECONDS - 60)
            ).isoformat()
            self._seed_state(cwd, lastActivityAt=just_under_stale)
            result = run_hook("Stop", {"cwd": str(cwd)}, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["decision"], "block")
            self.assertEqual(read_state(cwd)["lastActivityAt"], just_under_stale)


class PauseSubcommandTest(unittest.TestCase):
    def test_pause_requires_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_pause([], cwd=cwd)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(state_path(cwd).exists())

    def test_pause_requires_a_non_empty_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_pause(["--reason", "  "], cwd=cwd)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(state_path(cwd).exists())

    def test_pause_with_a_reason_sets_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_pause(["--reason", "no fixture route for error state"], cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertTrue(state["pausedForUser"])
            self.assertEqual(state["pauseReason"], "no fixture route for error state")
            self.assertIsNotNone(state["lastActivityAt"])


def run_reset(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK), "reset"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=verify_env(),
        check=False,
    )


class ResetSubcommandTest(unittest.TestCase):
    def test_reset_clears_an_active_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            state_path(cwd).parent.mkdir(parents=True)
            state_path(cwd).write_text(
                json.dumps(
                    {
                        "active": True,
                        "scope": "full",
                        "terminal": False,
                        "pausedForUser": True,
                        "pauseReason": "stuck",
                        "blockCount": 2,
                        "lastActivityAt": fresh_timestamp(),
                        "lastValidate": {"outputDir": "x", "passed": False},
                    }
                ),
                encoding="utf-8",
            )
            result = run_reset(cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertFalse(state["active"])
            self.assertFalse(state["terminal"])
            self.assertFalse(state["pausedForUser"])
            self.assertIsNone(state["pauseReason"])
            self.assertEqual(state["blockCount"], 0)
            self.assertEqual(state["scope"], "unknown")

    def test_reset_without_a_prior_state_is_a_no_op_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_reset(cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertFalse(state["active"])


def run_scope(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK), "scope", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=verify_env(),
        check=False,
    )


class ScopeSubcommandTest(unittest.TestCase):
    def test_scope_set_full_activates_and_declares_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_scope(["--set", "full"], cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertTrue(state["active"])
            self.assertEqual(state["scope"], "full")
            self.assertFalse(state["terminal"])

    def test_scope_set_copy_audit_still_lets_export_copy_reach_terminal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            scope_result = run_scope(["--set", "copy-audit"], cwd)
            self.assertEqual(scope_result.returncode, 0, scope_result.stderr)

            export_payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py export-copy --bundle "
                        f"{cwd}/bundle --output {cwd}/copy.json"
                    )
                },
                "tool_response": {"stdout": "ok", "stderr": ""},
            }
            result = run_hook("PostToolUse", export_payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertEqual(state["scope"], "copy-audit")
            self.assertTrue(state["terminal"])

    def test_scope_set_resets_a_stale_terminal_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            state_path(cwd).parent.mkdir(parents=True)
            state_path(cwd).write_text(
                json.dumps(
                    {
                        "active": True,
                        "scope": "copy-audit",
                        "terminal": True,
                        "pausedForUser": False,
                        "pauseReason": None,
                        "blockCount": 0,
                        "lastActivityAt": fresh_timestamp(),
                        "lastValidate": None,
                    }
                ),
                encoding="utf-8",
            )
            result = run_scope(["--set", "full"], cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertEqual(state["scope"], "full")
            self.assertFalse(state["terminal"])

    def test_scope_rejects_an_unrecognized_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_scope(["--set", "bogus"], cwd)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(state_path(cwd).exists())

    def test_scope_requires_a_set_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_scope([], cwd)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(state_path(cwd).exists())

    def test_scope_set_equals_form_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_scope(["--set=full"], cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(read_state(cwd)["scope"], "full")


class UserPromptSubmitTest(unittest.TestCase):
    def test_clears_pause_and_emits_additional_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            state_path(cwd).parent.mkdir(parents=True)
            state_path(cwd).write_text(
                json.dumps(
                    {
                        "active": True,
                        "scope": "full",
                        "terminal": False,
                        "pausedForUser": True,
                        "pauseReason": "needs a fixture route",
                        "blockCount": 2,
                        "lastActivityAt": None,
                        "lastValidate": None,
                    }
                ),
                encoding="utf-8",
            )
            result = run_hook(
                "UserPromptSubmit",
                {"cwd": str(cwd), "prompt": "use /login for the error state"},
                cwd=cwd,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertIn(
                "resume the paused harness workflow",
                output["hookSpecificOutput"]["additionalContext"],
            )
            state = read_state(cwd)
            self.assertFalse(state["pausedForUser"])
            self.assertEqual(state["blockCount"], 0)
            # Answering a question is not work on the workflow, so it must not
            # stamp activity. Stamping it meant a workflow paused and never
            # resumed could not go stale, and every later session in this
            # directory stayed hostage to it.
            self.assertIsNone(state["lastActivityAt"])

    def test_a_question_to_the_user_does_not_hold_the_workflow_open(
        self,
    ) -> None:
        """Answering is not resuming, so it must not stamp activity."""

        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            state_path(cwd).parent.mkdir(parents=True)
            state_path(cwd).write_text(
                json.dumps(
                    {
                        "active": True,
                        "scope": "full",
                        "terminal": False,
                        "pausedForUser": True,
                        "pauseReason": "stopped on a question to the user",
                        "blockCount": 1,
                        "lastActivityAt": stale_timestamp(),
                        "lastValidate": None,
                    }
                ),
                encoding="utf-8",
            )
            run_hook(
                "UserPromptSubmit",
                {"cwd": str(cwd), "prompt": "something else entirely"},
                cwd=cwd,
            )
            # Still stale afterwards, so the next Stop can self-heal instead of
            # enforcing a workflow nobody came back to.
            result = run_hook("Stop", {"cwd": str(cwd)}, cwd=cwd)
            self.assertEqual(result.stdout.strip(), "")
            self.assertFalse(read_state(cwd)["active"])


    def test_initializes_state_on_slash_verify_design_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_hook(
                "UserPromptSubmit",
                {"cwd": str(cwd), "prompt": "/verify-design check my page"},
                cwd=cwd,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertTrue(state["active"])
            self.assertEqual(state["scope"], "unknown")
            self.assertFalse(state["terminal"])

    def test_initializes_state_on_plugin_scoped_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_hook(
                "UserPromptSubmit",
                {
                    "cwd": str(cwd),
                    "prompt": "$figma-lossless:verify-design check this",
                },
                cwd=cwd,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertTrue(state["active"])
            self.assertEqual(state["scope"], "unknown")
            self.assertFalse(state["terminal"])

    def test_does_not_initialize_on_a_question_mentioning_verify_design(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_hook(
                "UserPromptSubmit",
                {"cwd": str(cwd), "prompt": "what does verify-design do?"},
                cwd=cwd,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_path(cwd).exists())

    def test_does_not_initialize_on_a_korean_question_mentioning_verify_design(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_hook(
                "UserPromptSubmit",
                {"cwd": str(cwd), "prompt": "verify-design이 뭐야"},
                cwd=cwd,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_path(cwd).exists())

    def test_does_not_initialize_when_the_prefix_is_mid_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_hook(
                "UserPromptSubmit",
                {
                    "cwd": str(cwd),
                    "prompt": "can you run /verify-design on this page",
                },
                cwd=cwd,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_path(cwd).exists())

    def test_unrelated_prompt_does_not_create_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_hook(
                "UserPromptSubmit", {"cwd": str(cwd), "prompt": "fix a typo"}, cwd=cwd
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_path(cwd).exists())

    def test_does_not_reinitialize_an_already_active_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            state_path(cwd).parent.mkdir(parents=True)
            state_path(cwd).write_text(
                json.dumps(
                    {
                        "active": True,
                        "scope": "full",
                        "terminal": False,
                        "pausedForUser": False,
                        "pauseReason": None,
                        "blockCount": 1,
                        "lastActivityAt": fresh_timestamp(),
                        "lastValidate": {"outputDir": "x", "passed": False},
                    }
                ),
                encoding="utf-8",
            )
            result = run_hook(
                "UserPromptSubmit",
                {"cwd": str(cwd), "prompt": "/verify-design run it again"},
                cwd=cwd,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state = read_state(cwd)
            self.assertEqual(state["scope"], "full")
            self.assertEqual(state["blockCount"], 1)


class SessionStartTest(unittest.TestCase):
    def test_emits_context_for_unfinished_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            state_path(cwd).parent.mkdir(parents=True)
            state_path(cwd).write_text(
                json.dumps(
                    {
                        "active": True,
                        "scope": "copy-audit",
                        "terminal": False,
                        "pausedForUser": False,
                        "pauseReason": None,
                        "blockCount": 0,
                        "lastActivityAt": fresh_timestamp(),
                        "lastValidate": None,
                    }
                ),
                encoding="utf-8",
            )
            result = run_hook(
                "SessionStart", {"cwd": str(cwd), "source": "startup"}, cwd=cwd
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("copy-audit", context)
            self.assertIn(str(state_path(cwd)), context)

    def test_silent_on_a_stale_workflow_and_self_heals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            state_path(cwd).parent.mkdir(parents=True)
            state_path(cwd).write_text(
                json.dumps(
                    {
                        "active": True,
                        "scope": "full",
                        "terminal": False,
                        "pausedForUser": False,
                        "pauseReason": None,
                        "blockCount": 0,
                        "lastActivityAt": stale_timestamp(),
                        "lastValidate": None,
                    }
                ),
                encoding="utf-8",
            )
            result = run_hook(
                "SessionStart", {"cwd": str(cwd), "source": "startup"}, cwd=cwd
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")
            self.assertFalse(read_state(cwd)["active"])

    def test_silent_when_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_hook(
                "SessionStart", {"cwd": str(cwd), "source": "startup"}, cwd=cwd
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_silent_when_already_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            state_path(cwd).parent.mkdir(parents=True)
            state_path(cwd).write_text(
                json.dumps(
                    {
                        "active": True,
                        "scope": "full",
                        "terminal": True,
                        "pausedForUser": False,
                        "pauseReason": None,
                        "blockCount": 0,
                        "lastActivityAt": fresh_timestamp(),
                        "lastValidate": {"outputDir": "x", "passed": True},
                    }
                ),
                encoding="utf-8",
            )
            result = run_hook(
                "SessionStart", {"cwd": str(cwd), "source": "startup"}, cwd=cwd
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")


class PreToolUseTest(unittest.TestCase):
    def test_denies_use_reference_screenshots_outside_the_harness_tests_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 /opt/run_harness.py snapshot-template "
                        "--use-reference-screenshots"
                    )
                },
            }
            result = run_hook("PreToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            decision = output["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")
            self.assertIn("self-test", decision["permissionDecisionReason"])

    def test_allows_use_reference_screenshots_inside_the_harness_tests_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 -m pytest "
                        f"{REPOSITORY_ROOT}/tests/test_reference_images.py "
                        "--use-reference-screenshots"
                    )
                },
            }
            full_env = dict(os.environ)
            full_env["CLAUDE_PLUGIN_ROOT"] = str(REPOSITORY_ROOT)
            result = run_hook("PreToolUse", payload, cwd=cwd, env=full_env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_allows_a_bash_command_without_the_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Bash",
                "tool_input": {"command": "ls -la"},
            }
            result = run_hook("PreToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_asks_on_gate_config_json_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": f"{cwd}/examples/sign-in/gate-config.json",
                    "old_string": '"geometryTolerancePx": 1.0',
                    "new_string": '"geometryTolerancePx": 50.0',
                },
            }
            result = run_hook("PreToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            decision = output["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "ask")

    def test_asks_on_a_gate_config_variant_basename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": f"{cwd}/examples/sign-in/Gate-Config.dev.JSON",
                    "old_string": '"tokenUsagePolicy": "warn"',
                    "new_string": '"tokenUsagePolicy": "off"',
                },
            }
            result = run_hook("PreToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_allows_a_tolerance_key_in_a_typescript_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": f"{cwd}/src/foo.ts",
                    "old_string": "const config = {}",
                    "new_string": 'const config = { "tokenMap": {} }',
                },
            }
            result = run_hook("PreToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_allows_a_single_tolerance_key_in_an_unrelated_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Write",
                "tool_input": {
                    "file_path": f"{cwd}/config/random-config.json",
                    "content": '{"tokenUsagePolicy": "off"}',
                },
            }
            result = run_hook("PreToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_asks_when_two_distinct_tolerance_keys_touched_in_an_unrelated_json_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Write",
                "tool_input": {
                    "file_path": f"{cwd}/config/random-config.json",
                    "content": (
                        '{"tokenUsagePolicy": "off", "geometryTolerancePx": 50.0}'
                    ),
                },
            }
            result = run_hook("PreToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_allows_editing_validators_py_default_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": f"{cwd}/src/figma_lossless/validators.py",
                    "old_string": '"geometryTolerancePx": 1.0,\n    "styleNumericTolerance": 0.1,',
                    "new_string": '"geometryTolerancePx": 50.0,\n    "styleNumericTolerance": 5.0,',
                },
            }
            result = run_hook("PreToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_allows_an_unrelated_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            payload = {
                "cwd": str(cwd),
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": f"{cwd}/src/app.tsx",
                    "old_string": "old",
                    "new_string": "new",
                },
            }
            result = run_hook("PreToolUse", payload, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")


class MalformedInputTest(unittest.TestCase):
    def test_malformed_json_stdin_does_not_crash_any_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            for event in (
                "PostToolUse",
                "Stop",
                "UserPromptSubmit",
                "SessionStart",
                "PreToolUse",
            ):
                result = run_hook(event, raw_stdin="not json {{{", cwd=cwd)
                self.assertEqual(result.returncode, 0, (event, result.stderr))
        self.assertFalse(state_path(cwd).exists())

    def test_empty_stdin_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_hook("PostToolUse", raw_stdin="", cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_event_is_a_silent_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            result = run_hook("SomeFutureEvent", {"cwd": str(cwd)}, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_path(cwd).exists())

    def test_no_argv_is_a_silent_no_op(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input="",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)




class HandingControlBackTest(unittest.TestCase):
    """Stopping to ask the user something is not abandoning the workflow.

    Enforcement exists because a 61-screen pilot once shipped green with its
    verification unfinished. It does not exist to make an agent guess at a
    decision that was the user's to make -- but that is what blocking a stop
    does when the agent stopped in order to ask. The escape hatch was always
    there (`pause --reason`); what was missing is that the honest case has to
    take it by hand, every turn, which is how six pauses got declared in one
    session.
    """

    def _active_state(self, cwd: Path) -> None:
        state_path(cwd).parent.mkdir(parents=True, exist_ok=True)
        state_path(cwd).write_text(
            json.dumps(
                {
                    "active": True,
                    "scope": "full",
                    "terminal": False,
                    "pausedForUser": False,
                    "pauseReason": None,
                    "blockCount": 0,
                    "lastActivityAt": fresh_timestamp(),
                    "lastValidate": None,
                }
            ),
            encoding="utf-8",
        )

    def _transcript(self, cwd: Path, text: str) -> Path:
        path = cwd / "transcript.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_answering_a_question_does_not_disarm_the_next_stop(self) -> None:
        """`AskUserQuestion` looks like a hand-off signal and is not.

        PostToolUse fires when a tool returns, and this one returns the answer
        the user already gave -- so treating it as "waiting on a person" marks
        the workflow paused at the moment the person stopped waiting. An agent
        could then ask anything, receive an answer, and abandon an unfinished
        verification with the block disarmed. The hook is not registered for
        it at all; whether the agent is handing over is decided from the
        message it stops on.
        """

        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._active_state(cwd)
            run_hook(
                "PostToolUse",
                {
                    "cwd": str(cwd),
                    "tool_name": "AskUserQuestion",
                    "tool_input": {"questions": []},
                    "tool_response": {},
                },
                cwd=cwd,
            )
            self.assertFalse(read_state(cwd)["pausedForUser"])

            stop = run_hook(
                "Stop",
                {
                    "cwd": str(cwd),
                    "last_assistant_message": "검증은 다음에 하겠습니다.",
                },
                cwd=cwd,
            )
            self.assertEqual(json.loads(stop.stdout)["decision"], "block")

    def test_only_bash_is_registered_for_post_tool_use(self) -> None:
        config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(
            [group.get("matcher") for group in config["hooks"]["PostToolUse"]],
            ["Bash"],
        )

    def test_a_stop_on_a_question_pauses_instead_of_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._active_state(cwd)
            transcript = self._transcript(
                cwd, "구현은 끝났습니다.\n\nPR 을 만들까요?"
            )
            result = run_hook(
                "Stop",
                {"cwd": str(cwd), "transcript_path": str(transcript)},
                cwd=cwd,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")
            state = read_state(cwd)
            self.assertTrue(state["pausedForUser"])
            self.assertEqual(
                state["pauseReason"], "stopped on a question to the user"
            )

    def test_a_question_followed_by_a_sign_off_still_counts(self) -> None:
        """The real shape of these messages, and what reading one line missed."""

        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._active_state(cwd)
            transcript = self._transcript(
                cwd,
                "고쳤습니다.\n\n한 줄만 더 손볼까요?\n\n"
                "컨텍스트가 81%입니다. 큰 작업은 다음 세션이 낫습니다.",
            )
            run_hook(
                "Stop",
                {"cwd": str(cwd), "transcript_path": str(transcript)},
                cwd=cwd,
            )
            self.assertTrue(read_state(cwd)["pausedForUser"])

    def test_quoting_the_users_question_is_not_asking_one(self) -> None:
        """The coaching block opens by quoting the user back to them.

        Across this project's transcripts that quote is the single largest
        source of false positives, and a message can end in a plain conclusion
        while carrying one at the top.
        """

        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._active_state(cwd)
            transcript = self._transcript(
                cwd,
                "> 🇬🇧 **Does it really take that long?**\n\n---\n\n"
                "확인했습니다. 46초입니다.",
            )
            result = run_hook(
                "Stop",
                {"cwd": str(cwd), "transcript_path": str(transcript)},
                cwd=cwd,
            )
            self.assertEqual(json.loads(result.stdout)["decision"], "block")
            self.assertFalse(read_state(cwd)["pausedForUser"])

    def test_the_hosts_own_message_field_is_preferred(self) -> None:
        """The transcript can lag; what the host hands us cannot.

        Claude Code passes `last_assistant_message` on Stop. Reading the file
        instead looked equivalent and was not: at Stop time the final message
        is not necessarily flushed yet, so the parse returned the previous
        turn's text and a real question was blocked. Here the transcript holds
        a stale conclusion and the field holds the question actually asked.
        """

        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._active_state(cwd)
            stale = self._transcript(cwd, "이전 턴 메시지입니다. 계속합니다.")
            run_hook(
                "Stop",
                {
                    "cwd": str(cwd),
                    "transcript_path": str(stale),
                    "last_assistant_message": "고쳤습니다.\n\n이어서 할까요?",
                },
                cwd=cwd,
            )
            state = read_state(cwd)
            self.assertTrue(state["pausedForUser"])
            self.assertEqual(
                state["pauseReason"], "stopped on a question to the user"
            )

    def test_an_empty_message_field_falls_back_to_the_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._active_state(cwd)
            transcript = self._transcript(cwd, "끝냈습니다.\n\nPR 만들까요?")
            run_hook(
                "Stop",
                {
                    "cwd": str(cwd),
                    "transcript_path": str(transcript),
                    "last_assistant_message": "   ",
                },
                cwd=cwd,
            )
            self.assertTrue(read_state(cwd)["pausedForUser"])

    def test_a_stop_on_a_conclusion_is_still_blocked(self) -> None:
        """The case enforcement is actually for: leaving without saying why."""

        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._active_state(cwd)
            transcript = self._transcript(
                cwd, "구현을 마쳤습니다. 검증은 다음에 하겠습니다."
            )
            result = run_hook(
                "Stop",
                {"cwd": str(cwd), "transcript_path": str(transcript)},
                cwd=cwd,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "block")
            self.assertFalse(read_state(cwd)["pausedForUser"])

    def test_a_missing_transcript_keeps_the_old_behaviour(self) -> None:
        """No signal is not permission; it degrades to blocking, as before."""

        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._active_state(cwd)
            for payload in (
                {"cwd": str(cwd)},
                {"cwd": str(cwd), "transcript_path": str(cwd / "nope.jsonl")},
            ):
                with self.subTest(payload=sorted(payload)):
                    self._active_state(cwd)
                    result = run_hook("Stop", payload, cwd=cwd)
                    self.assertEqual(
                        json.loads(result.stdout)["decision"], "block"
                    )

    def test_a_terminal_workflow_is_never_paused_by_a_question(self) -> None:
        """Nothing here may resurrect a workflow that already finished."""

        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self._active_state(cwd)
            state = read_state(cwd)
            state["terminal"] = True
            state_path(cwd).write_text(json.dumps(state), encoding="utf-8")
            run_hook(
                "Stop",
                {"cwd": str(cwd), "last_assistant_message": "이어서 할까요?"},
                cwd=cwd,
            )
            self.assertFalse(read_state(cwd)["pausedForUser"])

    def test_an_inactive_directory_is_not_activated_by_a_question(self) -> None:
        """These hooks are global; stopping on a question in an unrelated
        project must not create a harness workflow there."""

        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            run_hook(
                "Stop",
                {
                    "cwd": str(cwd),
                    "last_assistant_message": "이어서 할까요?",
                },
                cwd=cwd,
            )
            self.assertFalse(state_path(cwd).exists())


if __name__ == "__main__":
    unittest.main()
