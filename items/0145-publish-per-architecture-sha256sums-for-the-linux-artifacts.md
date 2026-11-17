# #145 Publish per-architecture SHA256SUMS for the Linux artifacts

- Kind: pull, state: closed, by cody, created 2026-08-24T22:37:54-07:00
- Labels: none; milestone: none
- refs/pull/145/head -> main, merged: True

whiskerless has shipped `SHA256SUMS-x86_64` and `SHA256SUMS-aarch64` since backlog #75; this repo shipped none, so anyone downloading a `.deb`, `.rpm` or tarball from a release page had no integrity check at all. `dpkg -i` verifies nothing and `rpm -i` only warns if configured.

This is **not** a replacement for signing. The packages are GPG-signed and apt/dnf verify that automatically from the repository channel — that proves origin. Checksums answer the different question, *did this arrive intact*, for the direct-download path where nothing checks anything.

Three review findings fixed along the way:

- **Historical dispatches would have broken.** Adding the roles made `wait-for-release.sh` require them for every tag, and both install matrices deliberately support dispatching current scripts against an older tag — every pre-checksum release would have waited out 120 polls and failed. There is now an explicit optional-role concept: reconcile still heals them, completeness does not block.
- **The NAS bridge count** was still 5 against six selectors, so it could have called an incomplete mirror complete.
- **The source tarball** is arch-independent but hashed into the x86_64 manifest, since that is the machine that builds it; the comment now says so rather than implying `uname -m` covers it.

Reconcile treats a missing role as a warning, so existing releases (which have no checksums) warn rather than fail — honest, since they genuinely lack them.
