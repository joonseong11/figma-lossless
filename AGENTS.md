# AGENTS.md

Rules for any agent or person changing this repository. Read this before the first write.

What the tool does and how to use it is in [README.md](README.md); gate design is in [docs/](docs/). `.agents/plugins/marketplace.json` is only the Codex marketplace manifest and holds no guidance. This file covers one thing: how versions, branches and tags are kept in step with the code.

## Where the version lives

A version is declared in six places, and they must always agree:

| Place | Form |
|---|---|
| `pyproject.toml` | `version = "X.Y.Z"` |
| `.claude-plugin/plugin.json` | `"version": "X.Y.Z"` |
| `.claude-plugin/marketplace.json` | `"version"` of the `figma-lossless` entry |
| `.codex-plugin/plugin.json` | `"version": "X.Y.Z"` |
| `README.md` | version badge, `img.shields.io/badge/version-X.Y.Z-blue` |
| `CHANGELOG.md` | topmost `## [X.Y.Z] — YYYY-MM-DD` heading |
| git | annotated tag `vX.Y.Z` |

`scripts/check-version.sh` checks the first six on every push, and checks the tag name when CI runs for a tag. A version that has no tag is not a release.

`src/figma_lossless/__init__.py` also has a `__version__`, but it is stale (`0.1.0`) and nothing reads it. It is not part of this scheme; fixing it is a source change that needs its own review.

## Choosing the number

- **Patch** (`0.7.0` → `0.7.1`): fixes that do not change what a user or an agent has to do: bug fixes, classification fixes, hook command fixes, documentation corrections that ship with a fix.
- **Minor** (`0.7.0` → `0.8.0`): any change to the CLI surface or exit codes, bundle or contract schema, the gate set or gate verdicts, mode defaults (extract vs verify), hook behaviour, capture-plan options, or install and bootstrap behaviour. Anything that breaks a caller is a minor until `1.0.0`, and the CHANGELOG lists it under "변경 (호환성 깨짐)".
- **Major**: reserved for `1.0.0` and later breaking changes.
- Changes to documentation, tests or CI only do not change the version and are not tagged.

One version number belongs to exactly one commit. Never reuse a number for different code, and never skip a number.

## Release procedure

Do these in order. Do not start the next version until the last step is done for the current one.

1. Create a new branch from an up-to-date `main`: `<type>/<short-topic>-<YYYYMMDD>`.
2. Make the change. In the final commit of the branch, set the new version in all six places above and add the `CHANGELOG.md` section dated today.
3. Run `scripts/check-version.sh` and `PYTHONPATH=src python3 -m unittest discover -s tests`. Both must pass.
4. Push the branch and open a pull request against `main`. CI must pass.
5. Merge with a **merge commit**. Do not squash or rebase: the tag must point at a commit that keeps its hash.
6. Tag the commit that set the version, and push the tag:

   ```bash
   git fetch origin
   git tag -a vX.Y.Z <release-commit> -m "vX.Y.Z — <one-line summary>"
   git push origin vX.Y.Z
   ```

   The tagged commit must be reachable from `origin/main`. Check with `git merge-base --is-ancestor vX.Y.Z origin/main`.
7. In the working checkout, return to `main` and fast-forward it: `git switch main && git pull --ff-only`.

A branch is finished once its pull request is merged. Do not push further commits to it; new work starts at step 1 on a new branch.

## Released is not the same as active

A tag says the code is in `main` and passed CI. It does not say any installed copy has picked it up. The Claude Code plugin, the Codex plugin and any other checkout (for example a server that runs this repository as an agent plugin) are updated separately, and a stale or dirty checkout can sit on an old commit indefinitely. Do not describe a version as deployed anywhere until that installation has been updated and checked.

## CHANGELOG rules

- Keep the existing format: Korean text, headings `## [X.Y.Z] — YYYY-MM-DD` (em dash), newest first, subsections such as `### 수정`, `### 추가`, `### 변경`, `### 문서`.
- The date in a heading is the date the version was released (the day its pull request was merged and tagged).
- Add an `Unreleased` section only on the topmost position while its branch is still open, and replace it with the dated heading before merging.
- Never edit the section of a version that is already tagged, except to correct a factual error.
- Docs, tests and CI-only changes do not get a section.

## Known gaps in history

Checked against `git log` on 2026-10-07. The only tag is `v0.7.1`.

- **`0.7.0` has no tag.** It was set by `7213682` (manifests) and completed by `67ddbe7` (README badge and CHANGELOG); both are in `main` through merge `4dedf0c` (PR #5). `0.7.0` was never tagged.
- **`0.6.0` has no tag.** It is the version of the first commit, `866fdc8`, and is unchanged through `4ff5644`.
- **`0.5.0`, `0.4.0` and `0.3.0` have no tag and no commit.** They appear in `CHANGELOG.md` but no commit in this repository ever declared them; history starts at `0.6.0`. They cannot be tagged.
- **`v0.7.1` is correct.** It is an annotated tag on `e85b3ba`, the merge of PR #6, where every place above declares `0.7.1`, and it is an ancestor of `origin/main`. The commit that first set `0.7.1` is `c9aa0f0`; tagging the merge commit instead is acceptable because the tree is identical for version purposes.
- **`0.7.1` is a patch that includes a small addition** (`allowedOrigins` in capture plans). Under the rules above this would be a minor. The number is left as it is, since it is already released.
- **`src/figma_lossless/__init__.py` declares `__version__ = "0.1.0"`** and has not been kept in step (see "Where the version lives").
