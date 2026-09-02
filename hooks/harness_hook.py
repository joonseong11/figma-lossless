#!/usr/bin/env python3
"""Plugin-lifecycle enforcement dispatcher for figma-lossless.

Wired from hooks/hooks.json as:

    python3 ${CLAUDE_PLUGIN_ROOT}/hooks/harness_hook.py <event>

for the PostToolUse, Stop, UserPromptSubmit, SessionStart, and PreToolUse
lifecycle events, plus three standalone subcommands the agent runs directly
through the Bash tool: `pause` to declare a pause on a request defect,
`reset` to unconditionally clear tracked state, and `scope --set
full|copy-audit|design-spec` to declare workflow scope explicitly rather than leaving
it to be inferred from which commands have run so far.

State lives at <cwd>/.figma-lossless/state.json, where <cwd> is the "cwd"
field Claude Code sends on the hook's stdin (the session's working
directory), not this process's own working directory. This file must never
raise past main(): enforcement is fail-open, so any unexpected error exits 0
without touching state rather than breaking an unrelated tool call.

These hooks are registered globally (every session, every project), so
enforcement only engages where harness use is actually observed: a real
`collect`/`compile`/`validate`/capture/`export-copy`/`export-design` command,
or an explicit
`/verify-design` (or `$figma-lossless:verify-design`)
invocation prefix. A workflow untouched for STALE_AFTER_SECONDS is treated
as abandoned and self-heals to inactive rather than nagging a directory
forever.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIR_NAME = ".figma-lossless"
STATE_FILE_NAME = "state.json"
MAX_STOP_BLOCKS = 3
STALE_AFTER_SECONDS = 86400

# Invocation prefixes that activate tracking from a raw prompt. Anything
# short of an explicit invocation (e.g. asking what the skill does) must not
# activate state — the primary, false-positive-free activation path is
# observing a real harness command in PostToolUse.
PROMPT_ACTIVATION_PREFIXES = (
    "/verify-design",
    "$figma-lossless:verify-design",
)

DEFAULT_STATE: dict[str, Any] = {
    "active": False,
    "scope": "unknown",
    "terminal": False,
    "pausedForUser": False,
    "pauseReason": None,
    "blockCount": 0,
    "lastActivityAt": None,
    "lastValidate": None,
}

# Gate-tuning keys from figma_lossless.validators.DEFAULT_CONFIG.
# How many of these an Edit/Write must touch before we ask depends on the
# target file — see GATE_CONFIG_BASENAME_RE and handle_pre_tool_use below.
GATE_TOLERANCE_KEYS = (
    "requireAllContexts",
    "requireAllCompiledSpecs",
    "requireAllElements",
    "requireAllExactCopy",
    "requireLocalAssets",
    "requireFlowResults",
    "requireFlowEvidenceBinding",
    "requireFlowContract",
    "requireComponentContract",
    "requireReferenceScreenshots",
    "allowSelfTestSnapshot",
    "geometryTolerancePx",
    "styleNumericTolerance",
    "styleRatioTolerance",
    "styleOpacityTolerance",
    "gradientAngleToleranceDeg",
    "visualChannelTolerance",
    "visualMaxMismatchRatio",
    "visualPolicy",
    "tokenUsagePolicy",
    "tokenMap",
    # Naming a subtree here removes it from the rendered-content gates for
    # every screen at once, so it belongs under the same approval as a
    # tolerance.
    "excludedSubtreeNames",
    "excludedInteriorNames",
    "approvedDeviations",
)

# Files named like this are gate config by definition (see gate-config.json
# in SKILL.md), so a single recognized key is enough to ask. Any other .json
# file needs at least two distinct keys before we ask, and non-JSON files
# (source code such as validators.py, where editing DEFAULT_CONFIG during
# development is legitimate) never trigger this guard at all.
GATE_CONFIG_BASENAME_RE = re.compile(r"^gate-config.*\.json$", re.IGNORECASE)

STOP_BLOCK_REASON = (
    "A figma-lossless workflow is active and has not reached "
    "its terminal state (copy-audit: catalog diff report after export-copy; "
    "design-spec: exported spec after export-design; "
    "full: fresh all-gates-passed validate). Continue the workflow, or if "
    "blocked on a request defect, declare a pause: "
    "python3 $CLAUDE_PLUGIN_ROOT/hooks/harness_hook.py pause --reason '<why>'"
)


# --- State I/O --------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_stale(state: dict[str, Any]) -> bool:
    """A workflow is abandoned once it has sat idle past the threshold.

    Missing or unparseable lastActivityAt on an active workflow counts as
    stale too, since a real activation always stamps this field.
    """
    if not state.get("active"):
        return False
    last = _parse_iso(state.get("lastActivityAt"))
    if last is None:
        return True
    age_seconds = (datetime.now(timezone.utc) - last).total_seconds()
    return age_seconds > STALE_AFTER_SECONDS


def _expire_if_stale(cwd: str, state: dict[str, Any]) -> dict[str, Any]:
    """Self-heal a stale active state to inactive, once, and persist it."""
    if _is_stale(state):
        state["active"] = False
        # ...and drop the pause with it. Left behind, `{active: false,
        # pausedForUser: true}` makes the next prompt announce that a workflow
        # is resuming when there is no longer one to resume.
        state["pausedForUser"] = False
        state["pauseReason"] = None
        state["blockCount"] = 0
        save_state(cwd, state)
    return state


def _state_dir(cwd: str) -> Path:
    return Path(cwd) / STATE_DIR_NAME


def _state_path(cwd: str) -> Path:
    return _state_dir(cwd) / STATE_FILE_NAME


def state_exists(cwd: str) -> bool:
    return _state_path(cwd).is_file()


def load_state(cwd: str) -> dict[str, Any]:
    path = _state_path(cwd)
    if not path.is_file():
        return dict(DEFAULT_STATE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_STATE)
    if not isinstance(data, dict):
        return dict(DEFAULT_STATE)
    state = dict(DEFAULT_STATE)
    state.update(data)
    return state


def save_state(cwd: str, state: dict[str, Any]) -> None:
    directory = _state_dir(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    gitignore = directory / ".gitignore"
    if not gitignore.is_file():
        gitignore.write_text("*\n", encoding="utf-8")
    # Written through a temporary file and renamed. A direct write truncates
    # first, so a reader arriving in that window sees an empty or half-written
    # file, decodes nothing, and falls back to an inactive default -- meaning
    # enforcement silently switches itself off exactly while two processes are
    # touching it.
    path = _state_path(cwd)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


# --- stdin / stdout helpers --------------------------------------------------


def _read_stdin_json() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj))


# --- Bash command inspection --------------------------------------------------


def _command_has_token(command: str, token: str) -> bool:
    pattern = r"(?<![\w.-])" + re.escape(token) + r"(?![\w.-])"
    return re.search(pattern, command) is not None


def _is_harness_command(command: str) -> bool:
    return bool(re.search(r"figma-lossless|run_harness\.py", command))


def _is_capture_command(command: str) -> bool:
    return "playwright-capture.mjs" in command


def _split_command(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


# Matches an unexpanded shell variable reference ($FOO, ${FOO}) or command
# substitution ($(...)). shlex.split() never expands these — it only
# tokenizes quoting — so a token like "$ART/report" survives intact. This
# hook has no access to the invoking shell's environment (it runs as a
# separate process, and Claude Code's Bash tool does not persist exported
# variables into the hook's own env), so such a value cannot be resolved
# and must not be trusted as a real path.
_UNEXPANDED_SHELL_VALUE_RE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$\(")


def _looks_like_unexpanded_shell_value(value: str) -> bool:
    return bool(_UNEXPANDED_SHELL_VALUE_RE.search(value))


def _extract_output_dir(command: str) -> str | None:
    """Best-effort fallback: parse --output from the raw command text.

    Prefer _extract_output_dir_from_tool_response over this — the CLI's own
    stdout reports its already-shell-expanded, resolved path, whereas this
    function re-parses the literal command string and can be fooled by a
    shell variable the invoking shell expanded but we never saw expand.
    """
    tokens = _split_command(command)
    for index, token in enumerate(tokens):
        if token == "--output" and index + 1 < len(tokens):
            value = tokens[index + 1]
        elif token.startswith("--output="):
            value = token.split("=", 1)[1]
        else:
            continue
        if _looks_like_unexpanded_shell_value(value):
            # Recording this literal (e.g. "<cwd>/$ART/report") would point
            # at a path that can never exist, permanently hiding a real
            # pass. Treat it as unknown instead of misreporting it.
            return None
        return value
    return None


def _extract_output_dir_from_tool_response(tool_response: Any) -> Path | None:
    """Recover the validate output dir from the CLI's own stdout.

    `validate` prints {"report": "<resolved-output-dir>/report.html", ...}
    on success. `report_path` is built from `output_dir.resolve()` inside
    the harness process, after the invoking shell has already expanded any
    variables — so this path is always absolute and trustworthy, unlike
    re-parsing --output out of the command string.
    """
    if not isinstance(tool_response, dict):
        return None
    stdout = tool_response.get("stdout")
    if not isinstance(stdout, str) or not stdout.strip():
        return None
    # The CLI emits exactly one JSON object; be tolerant of a trailing
    # newline or incidental surrounding output by trying the last line.
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            return None
        report = data.get("report")
        if not isinstance(report, str) or not report:
            return None
        report_path = Path(report)
        if not report_path.is_absolute():
            return None
        return report_path.parent
    return None


def _resolve_path(value: str, cwd: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    return path


def _validate_passed(output_dir: Path) -> bool:
    """Read gate-results.json ourselves; never trust the chat transcript."""
    gate_results = output_dir / "gate-results.json"
    try:
        data = json.loads(gate_results.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return data.get("passed") is True


def _tool_response_has_error(tool_response: Any) -> bool:
    if isinstance(tool_response, dict):
        if tool_response.get("error"):
            return True
        if tool_response.get("isError"):
            return True
        if tool_response.get("interrupted") is True:
            return True
    return False


def _distinct_tolerance_keys_touched(*texts: str) -> set[str]:
    found: set[str] = set()
    for text in texts:
        if not text:
            continue
        for key in GATE_TOLERANCE_KEYS:
            if key in found:
                continue
            if re.search(r'"' + re.escape(key) + r'"\s*:', text):
                found.add(key)
    return found


# --- Event handlers -----------------------------------------------------------


def _pause_for_user(cwd: str, reason: str) -> None:
    """Record that the workflow is waiting on a person, and why.

    `lastActivityAt` is deliberately untouched. A pause that refreshed it would
    hold the 24-hour self-heal open forever, which is how a workflow nobody
    intends to finish stays enforced against every future session in this
    directory.
    """

    state = load_state(cwd)
    if not state.get("active") or state.get("terminal"):
        return
    state["pausedForUser"] = True
    state["pauseReason"] = reason
    state["blockCount"] = 0
    save_state(cwd, state)


def handle_post_tool_use(payload: dict[str, Any]) -> None:
    # Only Bash. `AskUserQuestion` and `ExitPlanMode` look like hand-off
    # signals and are not: PostToolUse fires when a tool *returns*, and those
    # return the answer the user already gave. Pausing there would mark the
    # workflow "waiting on a person" at the moment the person finished waiting,
    # so an agent could ask anything, get an answer, and then walk away from an
    # unfinished verification with the Stop block disarmed. Whether the agent
    # is handing over is a question about the message it stops on, and that is
    # decided in `handle_stop`.
    if payload.get("tool_name") != "Bash":
        return
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return
    cwd = payload.get("cwd") or os.getcwd()

    is_harness = _is_harness_command(command)
    is_capture = _is_capture_command(command)
    if not (is_harness or is_capture):
        return

    state = load_state(cwd)

    if is_harness and (
        _command_has_token(command, "collect")
        or _command_has_token(command, "compile")
    ):
        state["active"] = True
        state["terminal"] = False

    if is_capture or (is_harness and _command_has_token(command, "validate")):
        if state.get("scope") != "full":
            # Scope is only proven full-verification now. Any terminal claim
            # made earlier while scope was still ambiguous — e.g. an
            # export-copy that looked like a standalone copy audit but was
            # actually a mid-workflow copy-contract dump — was provisional
            # and must not survive discovering the real scope.
            state["terminal"] = False
        state["scope"] = "full"

    if is_harness and _command_has_token(command, "validate"):
        output_dir = _extract_output_dir_from_tool_response(
            payload.get("tool_response")
        )
        if output_dir is None:
            output_arg = _extract_output_dir(command)
            output_dir = _resolve_path(output_arg, cwd) if output_arg else None
        output_dir_str = str(output_dir) if output_dir else None
        passed = _validate_passed(output_dir) if output_dir else False
        state["terminal"] = passed
        state["lastValidate"] = {"outputDir": output_dir_str, "passed": passed}

    if is_harness and _command_has_token(command, "export-copy"):
        if not _tool_response_has_error(payload.get("tool_response")):
            if state.get("scope") != "full":
                state["terminal"] = True
                if state.get("scope") == "unknown":
                    state["scope"] = "copy-audit"

    if is_harness and _command_has_token(command, "export-design"):
        # Producing the spec *is* the deliverable for this scope: there is no
        # implementation to verify yet, and the point of the export is to hand
        # the design to whoever builds it. Same shape as export-copy -- it
        # cannot end a run that has already proven itself to be a full
        # verification, so a mid-workflow spec dump does not release the Stop
        # block that the capture and validate steps are still owed.
        if not _tool_response_has_error(payload.get("tool_response")):
            if state.get("scope") != "full":
                state["terminal"] = True
                if state.get("scope") == "unknown":
                    state["scope"] = "design-spec"

    state["lastActivityAt"] = _now_iso()
    state["blockCount"] = 0
    save_state(cwd, state)


def _last_assistant_text(transcript_path: Any) -> str:
    """The text of the message the agent is stopping on, or "".

    Read defensively: the transcript is somebody else's format, it can be
    absent, partially written, or shaped differently in a future version, and
    none of that is worth failing enforcement over.
    """

    if not isinstance(transcript_path, str) or not transcript_path:
        return ""
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return ""
    for line in reversed(lines[-40:]):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            continue
        text = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text.strip():
            return text
    return ""


# How many closing lines can carry the question, measured rather than guessed.
# Not the last line alone: the real shape is "question, then a sign-off", and
# reading only the final line missed a genuine "shall I fix that line?" that a
# remark about context budget followed. Not the whole message either.
#
# Across 1014 assistant messages in this project's transcripts: a window of one
# finds 20, five finds 39, twenty finds 48, and the whole message finds 56. Every
# one of the nine between five and twenty is a false positive -- seven are a
# quoted user sentence at the top of the message, two are section headings
# phrased as questions inside a report. Nothing genuine appears beyond five.
QUESTION_WINDOW_LINES = 5


def _asks_the_user_something(text: str) -> bool:
    """Whether the agent stopped on a question rather than on a conclusion.

    Deliberately shallow. Something subtler would be guessing at intent, and
    being wrong is cheap in both directions: a missed question costs one
    blocked stop, and a false positive costs a pause the agent could have
    declared itself with `pause --reason` anyway. That escape hatch was always
    agent-controlled and needed no one's consent; this only takes the ceremony
    out of the honest case.
    """

    lines = []
    for line in text.strip().splitlines():
        raw = line.strip()
        # A blockquote is someone else's sentence -- most often the user's own
        # question, quoted back to them. Quoting a question is not asking one.
        if raw.startswith(">"):
            continue
        if stripped := raw.rstrip("*_`)]>\"'” "):
            lines.append(stripped)
    return any(
        line.endswith("?") or line.endswith("？")
        for line in lines[-QUESTION_WINDOW_LINES:]
    )


def handle_stop(payload: dict[str, Any]) -> None:
    cwd = payload.get("cwd") or os.getcwd()
    if not state_exists(cwd):
        return
    state = load_state(cwd)
    state = _expire_if_stale(cwd, state)
    if not state.get("active"):
        return
    if state.get("terminal") or state.get("pausedForUser"):
        return

    # `last_assistant_message` is what Claude Code hands a Stop hook for exactly
    # this: the text the agent is stopping on, taken from memory. Reading the
    # transcript file instead looked equivalent and was not -- at Stop time the
    # final message has not necessarily been flushed to it yet, so the parse
    # returned the *previous* turn's text and a real question went unnoticed.
    # That is measured, not assumed: a stop on a question blocked while this
    # file's own parser, run against the same transcript a moment later, found
    # the message and classified it correctly. The file stays as a fallback for
    # a host that sends no such field.
    spoken = payload.get("last_assistant_message")
    if not isinstance(spoken, str) or not spoken.strip():
        spoken = _last_assistant_text(payload.get("transcript_path"))
    if _asks_the_user_something(spoken):
        # Stopping to ask is the opposite of walking away: the user is looking
        # at the question, and can answer it or tell the agent to finish the
        # verification first. Blocking here produced the failure this whole
        # file is about -- an agent told to keep going on a decision that was
        # never its to make.
        _pause_for_user(cwd, "stopped on a question to the user")
        return

    # Only blockCount changes here; lastActivityAt is deliberately not
    # refreshed by a block itself, or an abandoned workflow would never go
    # stale as long as something kept calling Stop on it.
    state["blockCount"] = int(state.get("blockCount") or 0) + 1
    save_state(cwd, state)

    if state["blockCount"] >= MAX_STOP_BLOCKS:
        return

    _emit({"decision": "block", "reason": STOP_BLOCK_REASON})


def handle_user_prompt_submit(payload: dict[str, Any]) -> None:
    cwd = payload.get("cwd") or os.getcwd()
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        prompt = ""
    state = load_state(cwd)

    if state.get("pausedForUser"):
        state["pausedForUser"] = False
        state["pauseReason"] = None
        state["blockCount"] = 0
        # `lastActivityAt` is not refreshed here. It used to be, so that the
        # next Stop could not immediately expire a workflow the user had just
        # been asked to resume -- but a workflow that has been idle past the
        # staleness window *should* expire, and answering a question about
        # something else is not work on it. Refreshing it meant that pausing
        # repeatedly held self-healing open forever, so any directory where a
        # workflow was abandoned mid-pause stayed enforced indefinitely.
        save_state(cwd, state)
        _emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        "The user has answered; resume the paused harness "
                        "workflow and run it to its terminal state."
                    ),
                }
            }
        )
        return

    stripped = prompt.strip()
    is_invocation = stripped.startswith(PROMPT_ACTIVATION_PREFIXES)
    if is_invocation and not state.get("active"):
        new_state = dict(DEFAULT_STATE)
        new_state["active"] = True
        new_state["scope"] = "unknown"
        new_state["terminal"] = False
        new_state["lastActivityAt"] = _now_iso()
        save_state(cwd, new_state)


def handle_session_start(payload: dict[str, Any]) -> None:
    cwd = payload.get("cwd") or os.getcwd()
    if not state_exists(cwd):
        return
    state = load_state(cwd)
    state = _expire_if_stale(cwd, state)
    if not state.get("active") or state.get("terminal"):
        return

    context = (
        "An unfinished figma-lossless workflow is tracked at "
        f"{_state_path(cwd)} (scope: {state.get('scope', 'unknown')}). "
        "Resume it to its terminal state before ending the session."
    )
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }
    )


def _is_self_test_context(command: str, cwd: str) -> bool:
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        return False
    tests_dir = str(Path(plugin_root) / "tests")
    if tests_dir in command:
        return True
    try:
        Path(cwd).resolve().relative_to(Path(tests_dir).resolve())
        return True
    except (OSError, ValueError):
        return False


def handle_pre_tool_use(payload: dict[str, Any]) -> None:
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()

    if tool_name == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str) and "--use-reference-screenshots" in command:
            if not _is_self_test_context(command, cwd):
                _emit(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                "self-test snapshots are not implementation "
                                "evidence"
                            ),
                        }
                    }
                )
        return

    if tool_name in ("Edit", "Write"):
        file_path = tool_input.get("file_path") or ""
        basename = Path(file_path).name if file_path else ""
        texts = (
            tool_input.get("content") or "",
            tool_input.get("old_string") or "",
            tool_input.get("new_string") or "",
        )
        touched_keys = _distinct_tolerance_keys_touched(*texts)

        if GATE_CONFIG_BASENAME_RE.match(basename):
            # A gate-config*.json file is gate config by definition — one
            # recognized key is enough.
            touches = bool(touched_keys)
        elif basename.lower().endswith(".json"):
            # Any other JSON file needs at least two distinct gate keys
            # before we treat it as a gate-tuning edit rather than
            # coincidental overlap with an unrelated config file.
            touches = len(touched_keys) >= 2
        else:
            # Non-JSON files (source code, docs, fixtures) never trigger
            # this guard — editing validators.py's own DEFAULT_CONFIG during
            # development is legitimate.
            touches = False

        if touches:
            _emit(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": (
                            "Gate tolerance/config change — loosening gates "
                            "to silence defects removes real coverage; "
                            "requires user approval"
                        ),
                    }
                }
            )
        return


def handle_pause(args: list[str]) -> int:
    reason = None
    for index, arg in enumerate(args):
        if arg == "--reason" and index + 1 < len(args):
            reason = args[index + 1]
            break
        if arg.startswith("--reason="):
            reason = arg.split("=", 1)[1]
            break
    reason = (reason or "").strip()
    if not reason:
        print("pause requires a non-empty --reason", file=sys.stderr)
        return 1

    cwd = os.getcwd()
    state = load_state(cwd)
    state["pausedForUser"] = True
    state["pauseReason"] = reason
    state["lastActivityAt"] = _now_iso()
    save_state(cwd, state)
    print(json.dumps({"paused": True, "reason": reason}))
    return 0


SCOPE_VALUES = ("full", "copy-audit", "design-spec")


def handle_scope(args: list[str]) -> int:
    """Declare workflow scope explicitly instead of letting it be inferred.

    Command-order inference cannot tell "export-copy is the whole workflow"
    (copy-audit) apart from "export-copy is a mid-workflow copy-contract
    dump inside a larger full-verification run" — both start with the same
    collect/compile/export-copy sequence, and full verification's own
    Locale-copy-audit step documents running export-copy before capture or
    validate ever appear. Declaring scope up front removes the ambiguity
    instead of guessing at it.
    """
    value = None
    for index, arg in enumerate(args):
        if arg == "--set" and index + 1 < len(args):
            value = args[index + 1]
            break
        if arg.startswith("--set="):
            value = arg.split("=", 1)[1]
            break
    if value not in SCOPE_VALUES:
        print(
            f"scope --set requires one of {SCOPE_VALUES}, got {value!r}",
            file=sys.stderr,
        )
        return 1

    cwd = os.getcwd()
    state = load_state(cwd)
    state["active"] = True
    state["scope"] = value
    # Declaring scope always means the workflow is still in progress: a
    # prior terminal claim made under a different (or ambiguous) scope no
    # longer applies once the real scope is confirmed.
    state["terminal"] = False
    state["lastActivityAt"] = _now_iso()
    save_state(cwd, state)
    print(json.dumps({"scope": value}))
    return 0


def handle_reset(args: list[str]) -> int:
    """Manual cleanup path: unconditionally clear tracked state.

    No reason required, unlike pause — this is for walking away from a
    directory's workflow entirely (stale-directory cleanup, abandoned
    experiment, etc.), not for pausing mid-workflow.
    """
    cwd = os.getcwd()
    save_state(cwd, dict(DEFAULT_STATE))
    print(json.dumps({"reset": True}))
    return 0


EVENT_HANDLERS = {
    "PostToolUse": handle_post_tool_use,
    "Stop": handle_stop,
    "UserPromptSubmit": handle_user_prompt_submit,
    "SessionStart": handle_session_start,
    "PreToolUse": handle_pre_tool_use,
}


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        return 0

    event = argv[0]
    if event == "pause":
        return handle_pause(argv[1:])
    if event == "reset":
        return handle_reset(argv[1:])
    if event == "scope":
        return handle_scope(argv[1:])

    handler = EVENT_HANDLERS.get(event)
    if handler is None:
        return 0

    payload = _read_stdin_json()
    handler(payload)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        # Enforcement must fail open: never break an unrelated tool call.
        sys.exit(0)
