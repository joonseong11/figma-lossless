#!/usr/bin/env python3
"""Run the packaged harness from either a source checkout or plugin cache.

An installed plugin runs under whatever ``python3`` is on PATH, which almost
never carries the harness dependencies. When they are missing this launcher
provisions a cached virtual environment once and re-executes itself with that
interpreter, so the plugin works without asking the user to install anything.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = PLUGIN_ROOT / "src"

# Keep in sync with `project.dependencies` in pyproject.toml. The launcher can
# be executed from a plugin cache without a TOML parser available on Python
# 3.9/3.10, so the list is duplicated here and asserted by the test suite.
HARNESS_DEPENDENCIES = ("Pillow>=10,<13",)

BOOTSTRAP_MARKER = "FIGMA_LOSSLESS_BOOTSTRAPPED"
DISABLE_BOOTSTRAP = "FIGMA_LOSSLESS_NO_BOOTSTRAP"
CACHE_OVERRIDE = "FIGMA_LOSSLESS_CACHE_DIR"

MANUAL_INSTALL_HINT = (
    "error: Pillow is required. Install this plugin package into an "
    "isolated Python environment before running the harness."
)


def _cache_root() -> Path:
    override = os.environ.get(CACHE_OVERRIDE)
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "figma-lossless"


def _venv_dir() -> Path:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return _cache_root() / f"venv-py{version}"


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _dependency_fingerprint() -> str:
    payload = "\n".join(HARNESS_DEPENDENCIES).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _ensure_environment():
    """Return an interpreter that satisfies the harness dependencies, or None."""
    venv = _venv_dir()
    python = _venv_python(venv)
    stamp = venv / ".figma-lossless-deps"
    fingerprint = _dependency_fingerprint()

    if python.exists() and stamp.is_file():
        try:
            installed = stamp.read_text(encoding="utf-8").strip()
        except OSError:
            installed = ""
        if installed == fingerprint:
            return python

    print(
        "figma-lossless: preparing an isolated Python environment at "
        f"{venv} (first run only)",
        file=sys.stderr,
    )
    try:
        venv.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"figma-lossless: cannot create {venv.parent}: {error}", file=sys.stderr)
        return None

    if not python.exists():
        created = subprocess.run(
            [sys.executable, "-m", "venv", str(venv)], check=False
        )
        if created.returncode != 0 or not python.exists():
            print(
                "figma-lossless: failed to create a virtual environment. Ensure "
                "the 'venv' module is available for this interpreter.",
                file=sys.stderr,
            )
            return None

    installed = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--quiet",
            *HARNESS_DEPENDENCIES,
        ],
        check=False,
    )
    if installed.returncode != 0:
        print(
            "figma-lossless: failed to install harness dependencies. Network "
            "access is required for this one-time setup.",
            file=sys.stderr,
        )
        return None

    try:
        stamp.write_text(fingerprint, encoding="utf-8")
    except OSError as error:
        print(f"figma-lossless: cannot record {stamp}: {error}", file=sys.stderr)
        return None
    return python


def _bootstrap_and_rerun() -> int:
    if os.environ.get(BOOTSTRAP_MARKER) or os.environ.get(DISABLE_BOOTSTRAP):
        print(MANUAL_INSTALL_HINT, file=sys.stderr)
        return 1

    python = _ensure_environment()
    if python is None:
        print(MANUAL_INSTALL_HINT, file=sys.stderr)
        return 1

    environment = dict(os.environ)
    environment[BOOTSTRAP_MARKER] = "1"
    completed = subprocess.run(
        [str(python), str(Path(__file__).resolve()), *sys.argv[1:]],
        env=environment,
        check=False,
    )
    return completed.returncode


def main() -> int:
    sys.path.insert(0, str(SOURCE_ROOT))
    try:
        from figma_lossless.cli import main as harness_main
    except ModuleNotFoundError as error:
        if error.name != "PIL":
            raise
        return _bootstrap_and_rerun()
    return harness_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
