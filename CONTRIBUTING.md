# Contributing to dreame-valetudo

This tool flashes bootloaders on hardware people own. A mistake here does not throw an exception,
it bricks a robot vacuum. Everything below exists because of that.

## Dev setup

The environment is `uv`-managed; dev dependencies are exact-pinned in `[dependency-groups]` and
locked in `uv.lock`, so contributors lint with the same binary the gate does.

```bash
uv run dreame-valetudo                                 # run from source
uv run pytest -q tests/python                          # the Python suite
uv run pytest -q tests/python/test_fel.py::test_name   # one test
uv run ruff check $(git ls-files '*.py' | grep -v '^docs/research/tools/')
uv run mypy                                            # strict
bash tests/release/release-scripts.sh              # release scripts, curl stubbed, no network
```

No hardware is needed for any of that, which is the point — see below.

## Layout: the package lives under `src/`

`src/dreame_valetudo/`, matching the sibling project. **This means the package is not importable
from the checkout root, and that is the point.** With a flat layout, `pytest` imports the package
from the working directory — so a module accidentally left out of the built wheel still passes every
test locally and in CI, and fails for the first person who installs it. Under `src/` there is
nothing to import unless something installed it.

The consequence, and the only thing that bites: **anything that runs the tests has to install the
package first.** `pip install -e .`, or `uv run` (which syncs). Every CI job that runs `pytest` does
this; a job that installs only the toolchain will collect zero importable modules.

Paths that follow the package: `pyproject.toml`'s `packages`/`source`/`files` entries and the ruff
per-file ignores, the workflows' `ruff check` and `compileall` arguments, `.renovaterc.json`'s
manager patterns, `packaging/check-release-boundary.py`'s tables, and the source-tarball copies.
Paths that do NOT: anything naming the *module* (`python -m dreame_valetudo`, imports, the
entry point) and the force-include destinations, which describe the layout *inside* the wheel.

## The Runner seam (please read before writing a phase)

Every side-effecting external command — `sunxi-fel`, the fastboot client, `ssh`, `curl`, `tar`,
`git` — goes through a `Runner` (`src/dreame_valetudo/run.py`): `SubprocessRunner` in production,
`RecordingRunner` in tests. Tests assert **transcripts**: the exact argv sequence a phase issues,
against scripted outputs. That is what proves a phase runs the same commands off-hardware as on.

So:

- Never call `subprocess` directly from phase code. Go through `ctx.runner`, or the `Fastboot` /
  `Fel` / ssh wrappers that do.
- Do text munging in-process. No `grep`/`sed`/`awk`/`jq` through the runner.
- The runner has no cwd. Use absolute paths or `-C`-style flags, never `cd`.
- All user IO goes through the `Console` seam (`console.py`). `die()` for clean fatal errors —
  `cli.main` turns `Die`/`ValueError`/`RunError`/`OSError` into one message, never a traceback.

`tests/python/conftest.py` has the harness: `make_ctx` builds a `Context` over a `RecordingRunner`
and a `ScriptedConsole` with canned answers.

## The safety contract

- **Phase 1 (`recon`) is read-only.** It validates USB and records the robot's config identity.
  Nothing in it may write.
- **Phase 2 (`root`) is the destructive flash**, gated on download verification, a full 32-hex
  config cross-check against the connected robot, and OKAY-checked fastboot responses. Those gates
  are pinned by tests. Preserve them exactly.
- **Every byte handed to hardware is re-hashed** against a staged manifest before the flash
  (`_check_staged_integrity`). If you add a file to `FEL_IMAGE_FILES`, it is covered automatically —
  do not add a hardware-consumed path that bypasses that list.
- **Idempotency is a safety property, not a convenience.** Each phase writes a marker under
  `robots/<id>/state/` and skips itself when present; `--force` overrides. An interrupted run must
  be safe to resume, and an attempt marker is written *before* irreversible work so a half-finished
  flash cannot look like a fresh start.
- UART-method models must never reach the fastboot phases (the `_FASTBOOT_ONLY` guard in `cli.py`).

## Adding a supported model

A fastboot Dreame is a `ModelSpec` row in `models.py` plus a golden update. `models.py` is the single typed
source of truth, taken verbatim from Valetudo's source and the dustbuilder, and pinned byte-for-byte
by TSV goldens in `tests/python/golden/`. `SUPPORTED_MODELS` order is the picker's numbering, so it
is pinned too.

## Zero runtime dependencies

The package imports only the stdlib. Do not add a runtime dependency. `pyusb` is used solely by
`libexec/fastboot-libusb.py`, a standalone fastboot client that runs as a subprocess, because
Google's `fastboot` cannot enumerate the Dreame gadget on Apple Silicon.

## Shared conventions

Code style, comment policy, test layers, shell and workflow rules live in
[`SisyphusMD/project-standard`](https://github.com/SisyphusMD/project-standard). Files vendored from
it are listed in `STANDARD.lock` and checked by `packaging/check-standard-sync.py` — **improve them
there and re-vendor, never by editing the copy here.**

## Releases

Never hand-edit a version. `.forgejo/workflows/release.yml` owns `pyproject.toml`,
`src/dreame_valetudo/__init__.py`, `uv.lock` and the README's download links. Put user-visible changes
under `## [Unreleased]` in `CHANGELOG.md` and the release workflow promotes them.

## Hardware testing

`dreame-valetudo bench` drives the hardware campaign; the scenarios are in
[`docs/HARDWARE-TESTING.md`](docs/HARDWARE-TESTING.md). If you have a supported robot and are
willing to run a candidate against it, that is the single most useful thing you can contribute —
CI can prove the transcripts, but only a real robot proves the flash.

## Licence, and why this one

**GPL-3.0-or-later.** Contributions are accepted under it.

The reasoning, written down so it is not re-argued:

- **Copyleft, because a closed fork of a rooting tool helps nobody.** Changes stay open, which is
  the norm in this corner of the ecosystem: `dustcloud` and `python-miio` — the two projects closest
  to what this one does — are both GPL-3.0.
- **GPL, not AGPL.** This was AGPL-3.0-or-later until 2026-08-20. AGPL's distinguishing feature is
  section 13: if you modify it and let people use it *over a network*, you owe them the source. That
  is the right tool for a self-hosted server. This is a CLI that talks to a robot over a USB cable —
  nobody will ever run rooting as a service, so the one clause that makes AGPL different from GPL
  cannot fire here. It was paying the cost of the most feared licence in exchange for nothing, and
  no project in this ecosystem uses it.
- **Not permissive, unlike the sibling.** [whiskerless](https://github.com/SisyphusMD/whiskerless)
  is MIT for a specific reason that does not apply here: it is a *library imported into Home
  Assistant*, which is Apache-2.0 and network-served. Copyleft there would create obligations for
  anyone redistributing an HA image and would permanently foreclose becoming an official
  integration. This project is imported by nothing.

### The third-party code this ships alongside

- **`sunxi-fel`** (from [sunxi-tools](https://github.com/linux-sunxi/sunxi-tools), **GPL-2.0**) is
  bundled in the `.deb`, `.rpm` and `.pkg`. GPL-2.0 and GPL-3.0 are not compatible *for combining
  into one work* — but nothing here combines them. `sunxi-fel` is **executed as a separate process**
  through the `Runner` seam and is never linked, imported, or statically bound. Shipping both in one
  package is mere aggregation, exactly as a Linux distribution ships GPL-2 and GPL-3 programs from
  the same repository. Keep it that way: do not link against sunxi-tools or vendor its source.
- **Valetudo** (Apache-2.0) is downloaded at run time, not bundled, and is never modified.

## Where issues and pull requests go

**GitHub.** Open them at [SisyphusMD/dreame-valetudo](https://github.com/SisyphusMD/dreame-valetudo).

Forgejo (`forgejo.bryantserver.com/SisyphusMD/dreame-valetudo`) is the source of truth and runs the full CI
suite on every push to `main`, but it is not where contributions arrive — outside contributors have
no account there, and its runner holds this project's release credentials. Every job in
`.forgejo/workflows/ci.yml` therefore carries a fork-trust gate and deliberately **skips** a pull
request from a fork rather than running untrusted code beside those secrets.

So a fork PR is tested on **GitHub-hosted runners**, which hold none of our secrets, by
`.github/workflows/ci-pr.yml`. You get lint, strict type-checking, the full test suite against both the current interpreter and the 3.11 floor, both coverage gates, shellcheck, documentation links, the vendored-standard check, and the release-script integration suite.

**What that does not cover**, so you are not surprised by a later failure:

- **The `.deb`/`.rpm` build.** `deb.Dockerfile` is only executed by the `build` job on
  Forgejo, so a change under `packaging/` can pass every check here and still break the
  release. Build the affected stage locally once before opening the PR:
  `docker build -f packaging/deb.Dockerfile --target export .`
- **macOS.** The signed `.pkg` is built and notarized on GitHub's macOS runners at release
  time, and the `macos` CI job only asserts that native macOS CI ran for the mirrored
  commit — a claim about the mirror, not about your PR.
- **Hardware.** Nothing in CI touches a robot. Phases are proven by transcript equivalence:
  the exact argv sequence a phase issues, asserted against scripted output. A change to a
  flashing path needs a bench run, which the maintainer does.

The sibling project works the same way, for the same reasons — see its `CONTRIBUTING.md`.

## What this project promises not to break

**The CLI is the contract. The Python modules are not.**

Nothing imports `dreame_valetudo` — it is a program people run, not a library people build on. So
modules, classes and functions may be renamed, split or deleted whenever it makes the code better,
with no aliases and no deprecation window. (The sibling project is the opposite case and promises
its Python API instead; see its `CONTRIBUTING.md` for why the same question has two answers.)

What IS promised:

- **Subcommand names and their flags.** People script against these, and the `auto` chain's phase
  order is part of the observable behaviour.
- **Exit codes.** `0` success — including a deliberate user abort, which is not a failure. `1` an
  error. `2` refused by a safety gate — a verdict, not a crash, so a caller can tell "I declined to
  do this" from "it broke". Human-readable output text is NOT promised; parse the exit code.
- **The workspace layout** under `~/dreame-valetudo/` — `work/` and `backups/`. People have
  irreplaceable factory backups in there. The layout may only move through `migrate.py`'s
  append-only `LAYOUTS` registry, which migrates forward atomically, never clobbers, stamps a
  version marker and refuses a newer on-disk layout rather than mis-reading it. Never move or
  rename anything under `backups/` by any other route.

Adding a subcommand or an optional flag is additive and needs no window. Renaming or removing one
means keeping the old spelling working, noting the deprecation in `CHANGELOG.md`, and removing it no
sooner than the next MINOR release.

