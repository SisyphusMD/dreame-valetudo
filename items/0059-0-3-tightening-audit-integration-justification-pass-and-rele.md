# #59 0.3 tightening: audit integration, justification pass, and release-boundary tripwire

- Kind: pull, state: closed, by cody, created 2026-07-30T20:57:20-07:00
- Labels: none; milestone: none
- refs/pull/59/head -> main, merged: True

Integrates the Codex audit as verified commits, then a full justification pass over the whole tree.

Highlights:
- Accident-safety fixes: staged firmware publish, atomic robot-side config and identity writes, fsync of the recovery capture before publish, an honest restore disk advisory, and a corrected CI floor job.
- Threat-model conformance: symlink refusals on tool-managed read/write paths removed (every delete-path guard kept), the manifest and migration inner locks removed (the workspace lock already serializes invocations), and the bench report HMAC seal removed (self-keyed, unverifiable by recipients).
- The pre-0.4 release-boundary tripwire: release and prerelease gates refuse to tag a pre-0.4 version from a tree containing UART material; self-retires at 0.4.0.
- Deduplication: one audited home for the staged-publish pattern, shared version-bump/tag-push/shellcheck scripts in the workflows, shared FEL wait, shared test scaffolding in conftest.
- New behavioral tests for stamp-version and the changelog scripts; duplicate and subset test pins removed.
- Docs pass so the README and unreleased changelog read plainly.

Verification at head: 1429 Python tests, ruff, strict mypy, uv lock --check, and the release-scripts, reconcile, release-boundary, and github-macos-status integration suites all green locally.
