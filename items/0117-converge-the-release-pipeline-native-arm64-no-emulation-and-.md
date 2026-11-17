# #117 Converge the release pipeline: native arm64, no emulation, and the audit fixes

- Kind: pull, state: closed, by cody, created 2026-08-22T07:21:39-07:00
- Labels: none; milestone: none
- refs/pull/117/head -> main, merged: True

Converges the release pipeline with whiskerless and removes every trace of QEMU emulation.

**Architecture decides the forge, and nothing is emulated.** amd64 builds and install-tests on the
Forgejo runner; arm64 and macOS on GitHub's native runners, via the same
`build-linux-arch.sh`/`install-matrix-arch.sh` both projects now share. Adds the GitHub install
matrix this repo never had (macOS `.pkg`, bottle pour, PyPI, plus the arm64 Linux legs).

**Release-path fixes found by a cold cross-repo audit:**

- **a stable could be cut with a red install matrix** — the workflow claimed the matrix gated the
  next stable and nothing read its result; there is now a `candidate-gate`
- `publish.yml` had no manual redrive, so any late transient failure cost another tag; it is now
  dispatchable behind a tag-grammar guard
- jobs carrying `always()`/`!cancelled()` ran even when that guard *refused* the ref
- the rc sweep never touched the apt/dnf repositories, so superseded rc packages were served
  forever; it now prunes them, gated on the stable actually replacing them in the same
  distributions and architectures
- an unreadable registry was reported as "not published", which is a different claim
- the published `.rpm` was never install-tested on openSUSE at all

Every `setup-python` call now carries the retry the rest of the pipeline already used.

**Do not cut a release from `main` until this lands:** the signing key and `.repo` files the
install channels fetch exist only on this branch.
