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

Each architecture is built on its own hardware through **buildx** (`packaging/deb.Dockerfile`):
amd64 on the Forgejo runner, arm64 on GitHub's native `ubuntu-24.04-arm`. **Nothing is emulated.**
Both forges call the same `packaging/build-linux-arch.sh`, which refuses to run when the host and
target architectures disagree, so an emulated build cannot return by accident. nfpm then packages
the exported per-arch binaries. A **reconcile** job (`packaging/reconcile-releases.sh`)
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
   stem whose stable `vX.Y.Z` is verified present — published (non-draft, non-prerelease) on **all
   three** registries, each serving an **identical, non-empty asset-name set** (same names, each once)
   — deletes that stem's rc releases and git tags, all-three-or-none. There is no fixed asset count,
   so a pre-`.rpm`-era stable (v0.1.0/v0.1.1, five assets) still qualifies as long as all three agree;
   any cross-registry disagreement is read as an unfinished fan-out and keeps the rc. An rc whose
   stable has not shipped yet is kept. It runs only when reconcile
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
| `CLUSTER_FORGEJO_REGISTRY_PUSH_PAT` | `publish` | Package-registry write on the cluster instance. Separate from the repo PAT: the registry is a different blast radius, and it is the one credential that can overwrite what subscribers install. Org-scoped. |
| `GPG_SIGNING_KEY` | `publish` | The namespace signing key's private half (`CCE50015D058E9BF`), shared with the sibling project because the registry groups packages by OWNER — a per-project key would buy no isolation. `publish` FAILS CLOSED without it rather than shipping unsigned packages. Org-scoped. |
| `PYPI_API_TOKEN` | `publish` | Uploads the sdist and wheel. Project-scoped by PyPI, so the sibling's token cannot be reused. **Not yet set:** the `pypi` job skips with a warning until it exists, and the Homebrew formula keeps building from the release asset until the first upload succeeds. |
   | `GH_REPO_WRITE_PAT` | GitHub PAT, Contents: read & write (create the GitHub release). Same PAT as the GitHub push-mirror. |
   | `CLUSTER_FORGEJO_TAP_WRITE_PAT` | Forgejo PAT, `write:repository` scoped to `homebrew-tap` (the `homebrew-tap` job pushes the updated formula — the stable formula for a stable tag, the `dreame-valetudo-rc` formula for a prerelease tag). |

   On GitHub (`…/SisyphusMD/dreame-valetudo` → Settings → Secrets → Actions): the macOS `.pkg`
   signing set (Apple Developer certs/keys, minted from your Apple Developer account):
   `CLUSTER_FORGEJO_REPO_WRITE_PAT`, `MACOS_APP_CERT_P12`, `MACOS_INSTALLER_CERT_P12`,
   `MACOS_CERT_PASSWORD`, `MACOS_APP_IDENTITY`, `MACOS_INSTALLER_IDENTITY`, `MACOS_NOTARY_KEY_P8`,
   `MACOS_NOTARY_KEY_ID`, `MACOS_NOTARY_ISSUER`. (`GITHUB_TOKEN` is automatic; the workflows that
   create or append to a release declare `permissions: contents: write`, without which it can read
   but not publish.)

   **Also on GitHub, `GPG_SIGNING_KEY`** — the same namespace key Forgejo holds. It is needed here
   because the arm64 `.deb`/`.rpm` are built on GitHub's native arm runner, and a package signed on
   one architecture and not the other is worse than neither. Put it in a **`linux-signing`
   environment** (Settings → Environments → New environment) rather than in the repository secrets,
   so only `release-linux-arm64.yml` can read it. Each forge holds the signing material for what it
   builds; the Apple identity lives here on the same principle.

If the macOS secrets are missing, the Linux assets and tap still publish, but the overall publish
workflow fails its qualification gate until both signed `.pkg`s are present. If `GPG_SIGNING_KEY`
is missing on GitHub, `build-linux-arch.sh` fails closed rather than shipping unsigned arm64
packages, and the release carries no arm64 Linux assets at all.

## Build and compatibility contracts

- **The bundle is built per arch (PyInstaller can't cross-compile).** Each channel
  freezes Python + the package into a self-contained `dreame-valetudo` artifact, plus a separate
  `dreame-fastboot` client (pyusb frozen in) and a prebuilt `sunxi-fel`. The main tool finds its
  sibling helpers via the tool's own libexec search (`find_helper`) — the `.deb`/`.rpm` at
  `/usr/lib/dreame-valetudo` need no wrapper; the `.pkg`/brew set `DREAME_LIBEXEC`. Build scripts:
  `packaging/build-bundle.sh` (main) + `packaging/build-fastboot-client.sh` (client).
- **`BUNDLE_MODE` selects onefile or onedir, and Linux is the only caller that asks for onedir.**
  The scripts default to onefile because `release-macos.yml` calls the same two and signs, bundles
  and notarizes a single file; `deb.Dockerfile` is the one caller that passes `BUNDLE_MODE=onedir`.

  Onedir began as an escape: a onefile app re-executes itself as a child, PyInstaller's bootloader
  requires that child's parent to be running the same executable (GHSA-9fxf-4qw3-ghmr), and under
  emulation the kernel names the injected emulator as the parent, so no onefile binary the arm64 leg
  built could start. **That constraint is gone** — arm64 builds natively now. Onedir stays because
  the standalone channel is a tarball of this tree with a launcher beside it, which is a shipped
  artifact shape with its own smoke test; changing it to match the sibling would be uniformity, not
  convergence. The sibling keeps onefile for the same kind of reason — one downloadable file suits a
  tool run once from a laptop. See project-standard/VARIANCE.md. Each Linux bundle installs as its own tree (`/usr/lib/dreame-valetudo/app`
  and `.../fastboot`) reached through a symlink to its launcher, because `find_helper` wants a
  runnable FILE at `/usr/lib/dreame-valetudo/dreame-fastboot` and the bootloader resolves symlinks
  before looking for its contents directory. `packaging/check-package-parity.py` compares each
  installed tree against the tree that was built, entry types included — nfpm assembles the package
  outside the build image, so nothing else in the release path would notice a bundle that lost a
  file.
- **The `.pkg` native-library bundling is the only piece not dry-runnable off-CI.**
  `release-macos.yml` rewrites `sunxi-fel`'s libusb reference to its co-located `@loader_path`
  dylib. PyInstaller's pyusb hook separately embeds libusb inside the frozen `dreame-fastboot`
  onefile, so hardened-runtime removal of `DYLD_*` variables does not affect that client. Each
  native macOS leg now installs its signed and notarized package, runs the built-in host smoke and
  helper checks, then uninstalls it and proves the test backup survived before uploading the asset.
- **Per-arch `.deb` builds go through buildx (`packaging/deb.Dockerfile`), each on its own
  hardware.** amd64 on the Forgejo runner, arm64 on GitHub's `ubuntu-24.04-arm` — the same "GitHub
  builds what the cluster can't" pattern the `.pkg` job uses, because no node here is arm64.
  `--platform` names the architecture the host already is; `build-linux-arch.sh` checks that and
  refuses the mismatch. buildx rather than `docker run` because the Forgejo job is itself
  containerised and a bind mount of the workspace does not reach the daemon. It's arch-specific
  (amd64/arm64; 32-bit armhf Pis aren't built — use the source tarball + `uv`/`pipx` there).

  This replaced emulation, which was the durable answer to the class of problem rc.13 hit:
  emulation is a proxy for the artifact that ships, and it was the only pre-release check that
  architecture got.
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
  Linux 8 and 10 as the RHEL-compatible floor and current hosts, and openSUSE Leap 16; it exercises the
  frozen entry point and helpers, upgrades, removes, and proves backups survive. The equivalent
  `.deb` test runs on
  current Debian and Ubuntu LTS plus Debian 12 and Ubuntu 22.04 compatibility floors. Physical USB
  access and the udev rule still require a real host.

## Secrets

Derived from the workflows, not maintained by hand — if this list and
`grep -rhoE 'secrets\.[A-Z0-9_]+' .forgejo/workflows .github/workflows` disagree, the workflows are
right. Structure mirrors whiskerless's equivalent section so the two are readable side by side.

### On Forgejo (`forgejo.bryantserver.com/SisyphusMD/dreame-valetudo` → Settings → Actions → Secrets)

| Secret | Used by | What it is |
|---|---|---|
| `CLUSTER_FORGEJO_REPO_WRITE_PAT` | `release`, `prerelease`, `publish`, `prune-rcs`, `dustbuilder-forms` | Repo-write PAT on the cluster instance: pushes the release commit and tag, creates releases, prunes superseded candidates. |
| `CLUSTER_FORGEJO_TAP_WRITE_PAT` | `publish` | Separate PAT scoped to `SisyphusMD/homebrew-tap`. Deliberately not the repo PAT: the tap is a different blast radius. |
| `NAS_FORGEJO_REPO_WRITE_PAT` | `publish`, `prune-rcs` | The same two operations against the NAS instance. |
| `PYPI_API_TOKEN` | `publish` | PyPI API token (`pypi-…`), named `dreame-valetudo-forgejo-ci` and **scoped to this project**, not the account — the sibling's token cannot be reused and a broader one has no business on a self-hosted runner. PyPI accepts OIDC only from GitHub Actions, GitLab.com, Google Cloud and ActiveState, so a token is the only option that keeps publishing on Forgejo; see the rejection rationale in project-standard's VARIANCE.md. |
| `CLUSTER_FORGEJO_TAP_WRITE_PAT` | `publish`, `tap-bottles` | Forgejo PAT with write access to `SisyphusMD/homebrew-tap`, so the tap jobs can push the rendered formulas. Held at the **org** level, not on this repo — the same credential the sibling uses for the same tap. A repo-level copy shadows the org one, which is a second place a tap-write credential lives for no benefit. |
| `GH_REPO_WRITE_PAT` | `publish`, `prune-rcs` | Creates and prunes releases on the GitHub mirror, which the Forgejo runner cannot do with its own token. |

### On GitHub (`github.com/SisyphusMD/dreame-valetudo` → Settings → Secrets and variables → Actions)

Only `release-macos.yml` runs there, because the signed and notarized `.pkg` needs Apple's toolchain
on a real macOS runner.

| Secret | What it is |
|---|---|
| `MACOS_APP_CERT_P12` / `MACOS_APP_IDENTITY` | Developer ID Application certificate (base64 `.p12`) and the identity string to sign the binaries with. |
| `MACOS_INSTALLER_CERT_P12` / `MACOS_INSTALLER_IDENTITY` | Developer ID Installer certificate and identity, for the `.pkg` itself. |
| `MACOS_CERT_PASSWORD` | Password for both `.p12` imports. |
| `MACOS_NOTARY_KEY_P8` / `MACOS_NOTARY_KEY_ID` / `MACOS_NOTARY_ISSUER` | App Store Connect API key, key id and issuer id, for `notarytool`. |
| `GITHUB_TOKEN` | Automatic. Attaches the built `.pkg` to the release. |

`*.p12` and `*.p8` are in `.gitignore` for a reason: those two files are exactly what a release
engineer ends up holding locally, and neither belongs in a repository.

## apt / dnf repositories — enabled

Both projects now publish signed apt and dnf repositories, so users get `apt install
dreame-valetudo` or `dnf install dreame-valetudo` and automatic updates. Direct `.deb`/`.rpm`
downloads remain, for machines that would rather not add a repository.

| Piece | State |
|---|---|
| `publish-registry.sh` | Vendored from project-standard. Called by `publish.yml` after the packages are built and smoke-tested. |
| `sisyphusmd.repo`, `sisyphusmd-testing.repo` | Shipped, and pointing at a key that exists (`CCE50015D058E9BF`). |
| `sisyphusmd-signing-key.asc` | The public half, vendored so both projects trust the same namespace key. |
| Qualification | The install matrix installs from both repositories every release — `apt-repo` and `dnf-repo` channels, on both architectures. |

**A candidate never reaches a subscriber who asked for releases.** deb and rpm version ordering
cannot express that on its own (`0.3.0~rc.1` sorts below `0.3.0`, which only helps once `0.3.0`
exists), so the two audiences are separated by DISTRIBUTION instead: a candidate goes to `testing`
only, a release to both. The install matrix picks its `.repo` file to match, or an rc run would
qualify the previous stable and leave the testing channel untested.

**The trust root is the Forgejo host that also serves the packages**, which is why
`repo_gpgcheck=0` and why the package signature — ours, not the registry's — is what actually
authenticates a download. That weakness is documented rather than hidden, and it is identical in
both projects by design; read the `.repo` comments before changing either.

### Generating the signing key

The workflow **fails closed** if `GPG_SIGNING_KEY` is absent — it refuses to publish unsigned
packages rather than quietly shipping them. So packaging signing is wired but inert until the secret
exists. To create it:

```bash
# RSA 4096, no expiry, no passphrase — CI cannot answer a prompt, and an expiring key silently
# breaks every subscriber's update on a date nobody has written down.
gpg --batch --quick-generate-key "Dreame Valetudo <SisyphusMD@users.noreply.github.com>" rsa4096 sign never
gpg --armor --export-secret-keys <KEYID>   # -> the GPG_SIGNING_KEY secret, Forgejo org scope
gpg --armor --export <KEYID> > packaging/sisyphusmd-signing-key.asc   # the PUBLIC half, committed
```

Commit only the **public** half. The private key lives in the Forgejo secret and nowhere else — the
workflow writes it to a `mktemp` path outside the build context, copies it into the packaging
container separately from the source tree, and force-removes both on EXIT so a failed build cannot
leave it in a stopped container on a reused Docker host.

Then flip on the repository channel: wire `publish-registry.sh` into `publish.yml`, point the
`.repo` files' `gpgkey` at the committed public key, and remove their NOT-LIVE banners.

**Before you do, decide where that key's trust is rooted.** Whiskerless serves its public key from
the same Forgejo host that serves the packages, which is why it runs `repo_gpgcheck=0` — a host
compromise would hand out both the packages and the key that vouches for them. It is documented
there rather than hidden, and it is worth fixing in **both** projects rather than reproducing here.
