# Reproduction tooling

Source for the research tooling, sanitized (owner-specific paths/IPs/serials replaced with
placeholders like `<repo>`, `<work>`, `<robot-ip>`). These are reference implementations of the
methods documented in the chapters, not a packaged CLI — paths at the top of each script are
placeholders to fill in.

They depend on **sister tooling already in this repository**:
- `dreame_valetudo/dust_decrypt.py` — recovers the transport keystream and de-obfuscates `upload`
  dumps (chapter 04).
- `libexec/fastboot-libusb.py` — the libusb fastboot client that binds the Dreame gadget (chapter 02).

## Index

| script | chapter | purpose |
|---|---|---|
| `run_chain.py` | 04, 13 | full FEL→fsbl→payload→`oem dust`→write toc0 (verified)→`flash:toc1` chain; also the recovery invocation |
| `recover_stock.py` | 13 | revert to the genuine chain without the 399 MiB read-back |
| `reboot_and_check.py` | 10 | reboot and race Valetudo-HTTP vs an FEL drop |
| `confirm_autofel.py` | 10 | time an FEL drop after a flash |
| `compute_dust_token.py` | 03 | derive the `oem dust` token from `config` (`--selftest` against oracles) |
| `read_efuse.sh`, `sid_read.sh` | 05 | non-destructive eFuse ROTPK read over FEL |
| `make_boot0_writer.py` | 04 | build the 36-byte boot0-write stub |
| `resign_toc0.py` | 06 | re-sign TOC0/item0 (raw-RSA scheme) with an owner key |
| `resign_toc1_generic.py` | 08 | re-sign all seven TOC1 certs (PKCS#1 v1.5) with a shared root key |
| `build_selfsigned_toc0_correct.py` | 06 | drive `resign_toc0.py` to build the byte-perfect self-signed toc0 |
| `verify_toc0_generic.py`, `verify_toc1_generic.py` | 08, 13 | offline structure checks — **known to false-pass; hardware is ground truth** |
| `disasm_verify.py` | 07 | capstone byte-check of the SPL eFuse anchor + the debug-gate |
| `pull_robot.sh` | — | full-eMMC mirror over SSH (needs a root shell; used to build the offline substrate) |

> The offline verifiers (`verify_toc0/1_generic.py`, and a Unicorn emulation) each passed a
> hardware-rejected file. Never treat an offline "verified" as acceptance — see chapter 13.
