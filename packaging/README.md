# Packaging & release

Four distribution channels, one release flow:

| Channel | Artifact | Built by | Signing |
|---|---|---|---|
| Homebrew (macOS **and** Linux) | formula in `SisyphusMD/homebrew-tap` | `publish.yml` → `update-tap.sh` | none (source build) |
| Debian/Ubuntu/Pi | `dreame-valetudo_{amd64,arm64}.deb` (version-less name, bundles sunxi-fel) | `publish.yml` (nfpm) | none (unsigned .deb) |
| Plain tarball | `dreame-valetudo-<v>.tar.gz` | `publish.yml` (`build-tarball.sh`) | none |
| macOS installer | `dreame-valetudo-macos-{arm64,x86_64}.pkg` (per-arch matrix) | `release-macos.yml` (GitHub) | Developer ID + notarized |

`packaging/` files: `homebrew/dreame-valetudo.rb` (formula template), `nfpm.yaml` (.deb),
`build-tarball.sh`, `entitlements.plist` (hardened-runtime exceptions for the `.pkg`'s native
bits), `update-tap.sh`, and the `changelog-section.sh` / `forgejo-release.sh` / `github-release.sh`
release helpers.

## How a release flows

1. **Cut it on Forgejo**: run the **Release** workflow (`.forgejo/workflows/release.yml`) from
   the Forgejo UI and pick `patch` / `minor` / `major`. (First release on a fresh repo: dispatch
   `minor` → `0.1.0`.) It promotes `## [Unreleased]` in the CHANGELOG, bumps the `VERSION` line in
   the script, runs the lint/smoke gate, commits, tags, and pushes. The push-mirror fans the
   commit + tag out to GitHub and the NAS Forgejo.
2. **Forgejo `publish.yml`** (tag-triggered): builds the **.deb** (nfpm) and the **tarball**,
   **creates the release on all three** forges (Forgejo, NAS, GitHub) with the CHANGELOG section as
   the notes + those assets, and **updates the Homebrew tap** (`homebrew-tap` job).
3. **GitHub `release-macos.yml`** (mirrored tag, GitHub's macOS runners; the one job that needs a
   Mac): a 2-leg matrix builds the **signed + notarized `.pkg` for arm64 AND x86_64**, then a
   `publish` job appends **both** to the **GitHub** and **public-Forgejo** releases.
4. **Forgejo `publish.yml` `nas-pkg` job**: waits for the `.pkg` on the public Forgejo release, then
   copies it to the internal NAS release.

The release helpers are idempotent (create-or-reuse + replace assets), so the forges can write the
same release in any order.

## Dev / prerelease builds

To validate the real artifacts on hardware without cutting a stable version, dispatch the
**Prerelease** workflow (`.forgejo/workflows/prerelease.yml`) from any branch and pick the target
bump. It stamps a `-rc.N` version onto a **tag only** (the branch, CHANGELOG, and README are left
untouched), then the same `publish.yml` + `release-macos.yml` build the `.deb` / `.pkg` / tarball
and publish them as a GitHub + Forgejo **prerelease** — never marked "latest", and the Homebrew tap
stays on the last stable. When an rc checks out on hardware, cut the matching stable release the
normal way; no dev branch is required (though you can dispatch from one).

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
   | `CLUSTER_FORGEJO_TAP_WRITE_PAT` | Forgejo PAT, `write:repository` scoped to `homebrew-tap` (the `homebrew-tap` job pushes the updated formula). Stable releases only. |

   On GitHub (`…/SisyphusMD/dreame-valetudo` → Settings → Secrets → Actions): the macOS `.pkg`
   signing set (Apple Developer certs/keys, minted from your Apple Developer account):
   `CLUSTER_FORGEJO_REPO_WRITE_PAT`, `MACOS_APP_CERT_P12`, `MACOS_INSTALLER_CERT_P12`,
   `MACOS_CERT_PASSWORD`, `MACOS_APP_IDENTITY`, `MACOS_INSTALLER_IDENTITY`, `MACOS_NOTARY_KEY_P8`,
   `MACOS_NOTARY_KEY_ID`, `MACOS_NOTARY_ISSUER`. (`GITHUB_TOKEN` is automatic.)

If the macOS secrets are missing, only the macOS job fails; the tap, `.deb`, tarball, and the
Forgejo/NAS releases still complete.

## Caveats to validate on the first release

- **The bundle is built per arch (PyInstaller can't cross-compile).** Each channel
  freezes Python + the package into a self-contained `dreame-valetudo` binary, plus a separate
  `dreame-fastboot` client (pyusb frozen in) and a prebuilt `sunxi-fel`. The main binary finds its
  sibling helpers via the tool's own libexec search (`find_helper`) — the `.deb` at
  `/usr/lib/dreame-valetudo` needs no wrapper; the `.pkg`/brew set `DREAME_LIBEXEC`. Build scripts:
  `packaging/build-bundle.sh` (main) + `packaging/build-fastboot-client.sh` (client).
- **The `.pkg` libusb bundling is the only piece not dry-runnable off-CI.** `release-macos.yml`
  rewrites `sunxi-fel`'s libusb reference to `@loader_path`; the frozen `dreame-fastboot` links
  libusb the same way. If the first `.pkg` can't load libusb at runtime, adjust the
  `install_name_tool` / signing steps.
- **Per-arch bundle builds run in a `python:3.14` container of each arch** (amd64 natively, arm64
  via QEMU emulation in `publish.yml`). It's therefore arch-specific (amd64/arm64; 32-bit armhf
  Pis aren't built, so use the source tarball + `uv`/`pipx` there). The arm64 emulated build is
  CI-only; if it's too slow or fails, add an `ubuntu-*-arm` native runner instead.
- **No system `fastboot`, no python3 dep.** Every OS/install path uses the same libusb fastboot
  client (frozen into `dreame-fastboot` for the `.pkg`/`.deb`, run via `uv` for brew/source). The
  `.deb` ships a udev rule (installed via the postinstall) for sudo-less USB.
  `DREAME_FASTBOOT=system` is a documented manual override, never automatic.
- **`macos-26` (arm64) + `macos-26-intel` (x86_64)** are the pinned runner images; bump them
  together as GitHub's images move. Intel/x86_64 runners are on GitHub's sunset path (~2027); if
  that leg disappears, drop it and fall back to brew/source for Intel.
