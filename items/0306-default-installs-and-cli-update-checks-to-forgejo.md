# #306 Default installs and CLI update checks to Forgejo

- Kind: issue, state: open, by cody, created 2026-11-18T06:33:50-08:00
- Labels: status:blocked, type:chore; milestone: none

Use the primary for the first Homebrew tap, stable and candidate release lookups, package metadata and user instructions. Preserve the full GitHub mirror and bottle distribution.

## cody, 2026-11-18T06:53:21-08:00

The local patch uses Forgejo for the tap, stable and candidate update checks, help and package metadata. All 2,480 Python tests and the doc-link checks pass. The three v0.3.0-rc.52 bottles on the primary match the tap checksums. Stable v0.2.1 predates bottles. The public listener returns HTTP 500 for the tap, release API and bottle URLs. Landing waits for public downloads and tap access.
