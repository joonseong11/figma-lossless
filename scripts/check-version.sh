#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$ROOT" python3 -I - <<'PY'
from pathlib import Path
import json, os, re
root = Path(os.environ["ROOT"])

def one(pattern, text, label):
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise SystemExit(f"missing {label}")
    return match.group(1)

def read(name):
    return (root / name).read_text(encoding="utf-8")

claude_plugin = json.loads(read(".claude-plugin/plugin.json"))
codex_plugin = json.loads(read(".codex-plugin/plugin.json"))
marketplace = json.loads(read(".claude-plugin/marketplace.json"))
entries = [p for p in marketplace["plugins"] if p.get("name") == "figma-lossless"]
if len(entries) != 1:
    raise SystemExit("marketplace.json must list figma-lossless exactly once")

values = {
    "pyproject.toml": one(r'^version = "([^"]+)"$', read("pyproject.toml"), "pyproject version"),
    ".claude-plugin/plugin.json": claude_plugin["version"],
    ".claude-plugin/marketplace.json": entries[0]["version"],
    ".codex-plugin/plugin.json": codex_plugin["version"],
    "README.md": one(r'img\.shields\.io/badge/version-([0-9][^-"]*)-', read("README.md"), "README version badge"),
    "CHANGELOG.md": one(r'^## \[([^]]+)\]', read("CHANGELOG.md"), "CHANGELOG release"),
}
if len(set(values.values())) != 1:
    raise SystemExit(f"version mismatch: {values}")
version = next(iter(values.values()))
ref_type = os.environ.get("GITHUB_REF_TYPE", "")
ref_name = os.environ.get("GITHUB_REF_NAME", "")
if ref_type == "tag" and ref_name != f"v{version}":
    raise SystemExit(f"tag/version mismatch: tag={ref_name}, version={version}")
print(f"version consistency: PASS ({version})")
PY
