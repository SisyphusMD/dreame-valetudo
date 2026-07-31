# Packaging & release

Six distribution channels, one release flow:

| Channel | Artifact | Built by | Signing |
|---|---|---|---|
| Homebrew stable (macOS **and** Linux) | `dreame-valetudo` formula in `SisyphusMD/homebrew-tap` | `publish.yml` → `update-tap.sh` (stable tags) | none (source build) |
| Homebrew prerelease | `dreame-valetudo-rc` formula (tracks the newest `-rc.N`, then falls through to the stable once it ships) | `publish.yml` → `update-tap.sh` | none (source build) |
| Debian/Ubuntu/Pi | `dreame-valetudo_{amd64,arm64}.deb` (version-less name, bundles sunxi-fel) | `publish.yml` (buildx `deb.Dockerfile` + nfpm) | none (unsigned .deb) |
| Fedora/RHEL/SUSE | `dreame-valetudo.{x86_64,aarch64}.rpm` (version-less name, bundles sunxi-fel) | `publish.yml` (same buildx binaries, `nfpm -p rpm`) | none (unsigned .rpm) |
| Plain tarball | `dreame-valetudo-<v>.tar.gz` | `publish.yml` (`build-tarball.sh`) | none |
| macOS installer | `dreame-valetudo-macos-{arm64,x86_64}.pkg` (per-arch matrix) | `release-macos.yml` (GitHub) | Developer ID + notarized |

Both `.deb` arches are built on the Forgejo runner through **buildx** (`packaging/deb.Dockerfile`): the
`docker-container` BuildKit driver carries QEMU, so the arm64 leg emulates inside the builder — the
runner is a Talos node with no usable host binfmt for a plain `docker run --platform arm64`. nfpm
then packages the exported per-arch binaries. A **reconcile** job (`packaging/reconcile-releases.sh`)
runs after every release and fans every asset out to all three registries (Forgejo, NAS, GitHub),
backfilling any historical gap after two registries agree on the asset's SHA-256. It never trusts
a filename or size alone, and ignores anything outside the exact release artifact matrix. Assets
are produced in two places (`.deb`/tarball on Forgejo, `.pkg` on GitHub), so the quorum is available
without making one registry authoritative. `brew install sisyphusmd/tap/dreame-valetudo-rc`
installs the newest candidate for hardware testing without touching the stable formula.

Pre-merge CI installs the Linux packages at every supported floor and current release, and installs
the source tarball into an isolated environment. Native macOS CI runs the full suite on macOS 15
and 26 on both Apple Silicon and Intel. Tag builds additionally execute both `.deb` architectures;
each `.pkg` is built and exercised on macOS 15, then installed and exercised again on macOS 26
before either release asset is published.

## How a release flows

1. **Cut it on Forgejo**: run the **Release** workflow (`.forgejo/workflows/release.yml`) from
   the Forgejo UI and pick `patch` / `minor` / `major`. (First release on a fresh repo: dispatch
   `minor` → `0.1.0`.) It promotes `## [Unreleased]` in the CHANGELOG — appending a `### Dependencies`
   list of the Renovate `(chore|fix)(deps):` bumps since the previous release, **deduped to the
   latest bump per dependency** (so an image bumped several times shows only its newest version) —
   bumps the version in `pyproject.toml` + the package `__init__`, runs the lint/smoke gate, commits,
   tags, and pushes. The push-mirror fans the commit + tag out to GitHub and the NAS Forgejo.
2. **Forgejo `publish.yml`** (tag-triggered): builds **both `.deb`s** (buildx) and the **tarball**,
   **creates the release on all three** forges (Forgejo, NAS, GitHub) with the CHANGELOG section as
   the notes + those assets, and **updates the Homebrew tap** — a stable tag writes the stable formula
   AND re-points the `dreame-valetudo-rc` formula at the same stable tarball (fall-through); a
   prerelease tag writes only the `dreame-valetudo-rc` formula.
3. **GitHub `release-macos.yml`** (mirrored tag, GitHub's macOS runners; the one job that needs a
   Mac): a 2-leg macOS 15 matrix builds and tests the **signed + notarized `.pkg` for arm64 AND
   x86_64**. A second matrix installs those same artifacts on macOS 26, then `publish` appends both
   to the **GitHub** and **public-Forgejo** releases.
4. **Forgejo `publish.yml` `reconcile` job**: waits for the current tag's `.pkg`s on the public
   Forgejo release, then walks **every** tag, hashes each recognized copy, and fills a **missing**
   copy only when the other two have identical content. A copy that is present but *dissents* is
   reported for review, never overwritten — a published asset is immutable. Without a two-registry
   quorum it warns and changes nothing. The current tag does not pass publishing qualification
   unless both native GitHub macOS jobs have installed and exercised their signed package and
   published the resulting `.pkg`; historical repair still runs before that missing-artifact failure
   is reported.
5. **Forgejo `publish.yml` `prune-rcs` job** (stable tags only): after the tap re-point and the
   reconcile fan-out, it sweeps the release candidates the shipped stable supersedes. It enumerates
   every `vX.Y.Z-rc.N` release across all three registries, groups them by stable stem, and for each
   stem whose stable `vX.Y.Z` is verified present — published (non-draft, non-prerelease) with all
   canonical assets — on **all three** registries, deletes that stem's rc releases and git tags,
   all-three-or-none. An rc whose stable has not shipped yet is kept. It runs only when reconcile
   succeeded (so an incompletely fanned-out stable never authorizes a prune), is warn-only and
   idempotent, and needs no tag argument, so the same job also backfills any historical rc left over
   from before the policy.

The release helpers are idempotent (create-or-reuse + replace assets), so the forges can write the
same release in any order, and the reconcile job can safely re-run them. **Stable** releases and
their assets are kept indefinitely on all three registries and re-reconciled every release.
**Release candidates** are the one thing pruned: once a stable `vX.Y.Z` is fully published on all
three registries, the `prune-rcs` job removes every `vX.Y.Z-rc.N` release and tag — and because the
`dreame-valetudo-rc` formula has already fallen through to that stable, the rc brew channel keeps
resolving with no surviving-rc to keep. The prune is gated on the stable being present everywhere and
is warn-only, so it can never make a valid stable disappear; reconcile still walks every *surviving*
tag.

Before merge, GitHub's native arm64 and Intel runners execute the Python, Ruff, mypy, ShellCheck,
research self-test, and integration suites for every mirrored branch commit. Forgejo's `macos` job
polls that public workflow by the exact commit SHA, so its required checks remain the single merge
gate without granting pull-request code a cross-forge write token. Package installation and signing
remain separate tag-time qualification because they require a release artifact and signing secrets.

## Dev / prerelease builds

To validate the real artifacts on hardware without cutting a stable version, dispatch the
**Prerelease** workflow (`.forgejo/workflows/prerelease.yml`) from any branch and pick the target
bump. It stamps a `-rc.N` version onto a **tag only** (the branch, CHANGELOG, and README are left
untouched), then the same `publish.yml` + `release-macos.yml` build the `.deb` / `.pkg` / tarball
and publish them as a GitHub + Forgejo **prerelease** — never marked "latest", and the Homebrew tap
stays on the last stable. When an rc checks out on hardware, cut the matching stable release the
normal way — that publishes the stable, re-points the `dreame-valetudo-rc` formula at it, and prunes
the version's now-superseded rc releases and tags; no dev branch is required (though you can dispatch
from one).

## One-time setup

1. **Create the repos.** Primary: `forgejo.bryantserver.com/SisyphusMD/dreame-valetudo` (add the
   GitHub + NAS push-mirrors). GitHub mirror:
   `github.com/SisyphusMD/dreame-valetudo`. **Homebrew tap:** `SisyphusMD/homebrew-tap`, also
   Forgejo-primary with GitHub + NAS push-mirrors — the `homebrew-tap` job writes the formula to the
   Forgejo tap and the mirror carries it to GitHub, where `brew` fetches it.
2. **Secrets.**

   On Forgejo (`…/SisyphusMD/dreame-valetudo` → Settings → Actions → Secrets):

   | Secret | What it is |
   |---|---|
   | `CLUSTER_FORGEJO_REPO_WRITE_PAT` | Forgejo PAT, `write:repository` scoped to `dreame-valetudo` (release commit/tag + create/append the Forgejo release). |
   | `NAS_FORGEJO_REPO_WRITE_PAT` | PAT on the NAS Forgejo, repo write (NAS release + the bridged `.pkg`). |
   | `GH_REPO_WRITE_PAT` | GitHub PAT, Contents: read & write (create the GitHub release). Same PAT as the GitHub push-mirror. |
   | `CLUSTER_FORGEJO_TAP_WRITE_PAT` | Forgejo PAT, `write:repository` scoped to `homebrew-tap` (the `homebrew-tap` job pushes the updated formula — the stable formula for a stable tag, the `dreame-valetudo-rc` formula for a prerelease tag). |

   On GitHub (`…/SisyphusMD/dreame-valetudo` → Settings → Secrets → Actions): the macOS `.pkg`
   signing set (Apple Developer certs/keys, minted from your Apple Developer account):
   `CLUSTER_FORGEJO_REPO_WRITE_PAT`, `MACOS_APP_CERT_P12`, `MACOS_INSTALLER_CERT_P12`,
   `MACOS_CERT_PASSWORD`, `MACOS_APP_IDENTITY`, `MACOS_INSTALLER_IDENTITY`, `MACOS_NOTARY_KEY_P8`,
   `MACOS_NOTARY_KEY_ID`, `MACOS_NOTARY_ISSUER`. (`GITHUB_TOKEN` is automatic.)

If the macOS secrets are missing, the Linux assets and tap still publish, but the overall publish
workflow fails its qualification gate until both signed `.pkg`s are present.

## Build and compatibility contracts

- **The bundle is built per arch (PyInstaller can't cross-compile).** Each channel
  freezes Python + the package into a self-contained `dreame-valetudo` binary, plus a separate
  `dreame-fastboot` client (pyusb frozen in) and a prebuilt `sunxi-fel`. The main binary finds its
  sibling helpers via the tool's own libexec search (`find_helper`) — the `.deb` at
  `/usr/lib/dreame-valetudo` needs no wrapper; the `.pkg`/brew set `DREAME_LIBEXEC`. Build scripts:
  `packaging/build-bundle.sh` (main) + `packaging/build-fastboot-client.sh` (client).
- **The `.pkg` native-library bundling is the only piece not dry-runnable off-CI.**
  `release-macos.yml` rewrites `sunxi-fel`'s libusb reference to its co-located `@loader_path`
  dylib. PyInstaller's pyusb hook separately embeds libusb inside the frozen `dreame-fastboot`
  onefile, so hardened-runtime removal of `DYLD_*` variables does not affect that client. Each
  native macOS leg now installs its signed and notarized package, runs the built-in host smoke and
  helper checks, then uninstalls it and proves the test backup survived before uploading the asset.
- **Per-arch `.deb` builds go through buildx (`packaging/deb.Dockerfile`).** amd64 builds natively on
  the runner; arm64 emulates inside BuildKit's builder (the `docker-container` driver carries QEMU),
  because the Talos runner node has no usable host binfmt for a plain `docker run --platform arm64`
  (that gets `exec format error` — the sister repos build their arm64 images the same buildx way).
  It's arch-specific (amd64/arm64; 32-bit armhf Pis aren't built — use the source tarball + `uv`/
  `pipx` there). The arm64 PyInstaller freeze under QEMU is the slowest step; if it's ever too slow
  or flaky, move the arm64 `.deb` to a GitHub native `ubuntu-24.04-arm` runner (mirroring the `.pkg`
  job's "GitHub builds what the cluster can't" pattern).
- **Python 3.11.0 is the source-install floor; current CPython is bundled for package users.** The
  full suite runs on the literal floor, while the ordinary and bundle jobs use the exact current
  release pinned in `constants.py`. Updating bundled Python therefore does not raise the source
  requirement.
- **glibc 2.28 is the supported Linux package floor.** It includes maintained RHEL-compatible 8
  hosts instead of inheriting whichever ABI the current general-purpose Python image happened to
  use. The release freezes checksum-verified official CPython source inside pinned
  `manylinux_2_28` builders, then opens both PyInstaller archives and rejects any embedded ELF that
  needs a newer symbol. Some architectures may remain compatible with older glibc without that
  becoming an untested support promise.
- **No system `fastboot`, no python3 dep.** Every OS/install path uses the same libusb fastboot
  client (frozen into `dreame-fastboot` for the `.pkg`/`.deb`, run via `uv` for brew/source). The
  `.deb` ships a udev rule (installed via the postinstall) for sudo-less USB.
  `DREAME_FASTBOOT=system` is a documented manual override, never automatic.
- **macOS 15 is the package floor; macOS 26 is the current qualification host.** Both run on native
  arm64 and Intel workers. Build on the floor, then test that exact signed artifact on current so a
  newer SDK cannot silently become the minimum. Intel/x86_64 runners are on GitHub's sunset path
  (~2027); if that leg disappears, fall back to brew/source for Intel.
- **The `.rpm` shares the `.deb`'s bundle + udev postinstall**, built from the same buildx binaries
  with a second `nfpm -p rpm` (config in `packaging/nfpm.yaml`; `overrides.rpm.depends`). Its runtime
  deps are SONAME/file requires (`libusb-1.0.so.0()(64bit)`, `/usr/bin/ssh`, …) rather than distro
  package names. CI installs ordered RC builds on the supported and current Fedora releases, Rocky
  Linux 8/9/10 as the maintained RHEL-compatible hosts, and openSUSE Leap 16; it exercises the
  frozen entry point and helpers, upgrades, removes, and proves backups survive. The equivalent
  `.deb` test runs on
  current Debian and Ubuntu LTS plus Debian 12 and Ubuntu 22.04 compatibility floors. Physical USB
  access and the udev rule still require a real host.
