# #131 Converge the Renovate surface onto one manager

- Kind: pull, state: closed, by cody, created 2026-08-23T17:58:21-07:00
- Labels: none; milestone: none
- refs/pull/131/head -> main, merged: True

The same rockylinux:9 image carried two depNames — `rocky-9-compat` in ci.yml, `rocky-9-current` in install-smoke.Dockerfile — and only the first was clamped, so Renovate retried a bump to Rocky 10 forever. whiskerless names it `rocky-9-compat` in the same file.

Converges the Renovate surface onto whiskerless shape: one custom manager owns every packaging Dockerfile (globbed, not named file by file), the built-in dockerfile manager is off rather than disabled per file, and `homebrew/brew` is annotated like its sibling.

Also fixes a live drift the old invariant could not see: it compared the toolchain pin in one workflow, so a ruff bump reached the Forgejo workflows, pyproject.toml and uv.lock while GitHub PR lint stayed on 0.16.3. It now compares all five workflows and all five tools.
