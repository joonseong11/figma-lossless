"""Operating modes: extraction is open by default, verification is locked.

Readers who arrive at this repository want one thing first: a lossless copy
of a Figma design they can implement from. That needs a token and three
commands (`collect`, `compile`, `export-design`). Everything past that —
capture, gates, enforcement hooks — assumes an implementation exists and a
person has agreed to be held to a contract. Turning that on by accident is
how the enforcement hook ends up blocking a session that only wanted a spec.

So the harness ships in `extract` mode. Verification commands refuse to run
until someone unlocks `verify` mode on purpose, either per directory tree
(`figma-lossless mode --set verify`, which writes
`<cwd>/.figma-lossless/mode.json`; the nearest such file at or above the
current directory applies, so a subdirectory inherits its project's mode)
or per process (`FIGMA_LOSSLESS_MODE=verify`). A valid environment value
wins over the file so CI can pin a mode without touching the working tree;
an invalid value in either place is ignored and resolution falls through to
the next source, so a typo can never unlock on its own.

Only the file reaches the Claude Code hooks: they run in the host's
environment, not in the shell that ran one command, so
`FIGMA_LOSSLESS_MODE=verify figma-lossless validate …` unlocks that one
process and nothing else.

`hooks/harness_hook.py` re-implements `resolve_mode` because it runs under
the system interpreter with no access to this package; the two must agree,
and `tests/test_mode.py` checks that they do.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

EXTRACT_MODE = "extract"
VERIFY_MODE = "verify"
MODES = (EXTRACT_MODE, VERIFY_MODE)

MODE_ENV = "FIGMA_LOSSLESS_MODE"
STATE_DIR_NAME = ".figma-lossless"
MODE_FILE_NAME = "mode.json"

# Commands that only read Figma and write documents. They never look at an
# implementation, so they are safe to run without any agreement to be gated.
EXTRACT_COMMANDS = frozenset(
    {"collect", "compile", "export-design", "export-copy", "mode"}
)


def mode_file(cwd: Path | str | None = None) -> Path:
    """Where `mode --set` writes for this directory (no upward search)."""

    base = Path(cwd) if cwd is not None else Path.cwd()
    return base / STATE_DIR_NAME / MODE_FILE_NAME


def find_mode_file(cwd: Path | str | None = None) -> Path | None:
    """The nearest mode file at or above ``cwd``, or None.

    Walking up mirrors how git finds its repository: a mode declared at a
    project root should hold for commands run from a package inside it, and
    the hooks, which only know the session's directory, must land on the
    same file as a command run from a subdirectory.
    """

    # absolute(), not resolve(): keep the caller's spelling of a symlinked
    # path (macOS /var -> /private/var) so the file found here compares equal
    # to the one mode_file() writes.
    base = (Path(cwd) if cwd is not None else Path.cwd()).absolute()
    for directory in (base, *base.parents):
        candidate = directory / STATE_DIR_NAME / MODE_FILE_NAME
        if candidate.is_file():
            return candidate
    return None


def _read_mode_file(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = payload.get("mode") if isinstance(payload, dict) else None
    return value if value in MODES else None


def resolve_mode(
    cwd: Path | str | None = None, environ: Mapping[str, str] | None = None
) -> tuple[str, str]:
    """Return ``(mode, source)`` where source is ``env``, ``file`` or ``default``.

    An unrecognised value in either place is ignored rather than raised: a
    typo must not silently unlock verification, and it must not break
    extraction either.
    """

    env = os.environ if environ is None else environ
    value = env.get(MODE_ENV)
    if value in MODES:
        return value, "env"
    found = find_mode_file(cwd)
    from_file = _read_mode_file(found) if found is not None else None
    if from_file is not None:
        return from_file, "file"
    return EXTRACT_MODE, "default"


def set_mode(mode: str, cwd: Path | str | None = None) -> Path:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    path = mode_file(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"mode": mode, "setAt": datetime.now(timezone.utc).isoformat()},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def clear_mode(cwd: Path | str | None = None) -> bool:
    path = mode_file(cwd)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def is_locked(command: str, mode: str) -> bool:
    return command not in EXTRACT_COMMANDS and mode != VERIFY_MODE


def locked_message(command: str) -> str:
    return (
        f"'{command}' is a verification command and verification mode is "
        "locked (the harness ships in extract mode: collect, compile, "
        "export-design, export-copy). Unlock it on purpose with\n"
        "  figma-lossless mode --set verify        # this directory tree, "
        "writes .figma-lossless/mode.json and also arms the Claude Code hooks\n"
        f"or  {MODE_ENV}=verify                      # this process only; "
        "the hooks do not see it"
    )
