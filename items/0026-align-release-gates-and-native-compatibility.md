# #26 Align release gates and native compatibility

- Kind: pull, state: closed, by cody, created 2026-07-27T16:23:52-07:00
- Labels: none; milestone: none
- refs/pull/26/head -> main, merged: True

Closes M33, M35, and M36 from the July audit. Release gates share CI and Renovate-managed tool pins, shell-check every script through the DinD-safe copy path, native packages declare the measured glibc floor, and macOS reads the shared sunxi-tools pin.
