# #296 Adopt the SisyphusMD project rules

- Kind: pull, state: closed, by cody, created 2026-11-17T14:09:33-08:00
- Labels: none; milestone: none
- ci/adopt-project-rules -> main, merged: True

Closes #295

Add the shared Forgejo checks, templates, issue archive and private security reporting. Move the existing CI workflow to tests.yml with its display name and required contexts intact. Record only the verified historical scanner fingerprints.

Large diff: the shared workflows, scripts, templates and rules are copied verbatim from project-rules.

Validated locally with 2,476 Python tests on both tested interpreters, both coverage gates, the release shell suite, mypy, Ruff, documentation links and the vendored standard.

GitHub mirror lockdown follows after checking the mirror token's permission to write workflow files. Branch protection is tracked on the project-rules adoption issue.
