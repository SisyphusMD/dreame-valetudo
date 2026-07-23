# docs

- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — post-root gotchas (Valetudo won't start, negative
  `deviceId`, empty miio key, and the rest) and the `fix-*` helper for each.
- **[DESIGN.md](DESIGN.md)** — how it works: what the tool automates, SSH-key and run-log handling,
  the macOS toolchain, and why it speaks fastboot over libusb.
- **[LAYOUT.md](LAYOUT.md)** — the `~/dreame-valetudo/` workspace layout, its versioned migrations,
  and the on-disk backup format.
- **[research/](research/)** — the low-level research compendium: reverse-engineering of the gen3
  (Allwinner A133 / MR813) secure-boot chain, a documented attempt to root with owner-generated keys
  (and exactly where it is blocked), the reproduction tooling, and an artifact-sourcing manifest.
  Start at [research/README.md](research/README.md).
