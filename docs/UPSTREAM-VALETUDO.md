# Official Valetudo contract

The fastboot implementation follows the official
[Dreame rooting guide](https://valetudo.cloud/pages/installation/dreame/#fastboot) and each
model's entry on the
[Supported Robots page](https://valetudo.cloud/pages/general/supported-robots/). The weekly
`DustBuilder forms` workflow checks a fresh checkout of `Hypfer/Valetudo`; the local rooting flow
never depends on that network check.

## Per-model contract

All 13 automated models are `aarch64`, use Secure Boot, and use the Fastboot method. The official
DDR rule is applied independently to every profile: D10s Pro, D10s Plus, and W10 Pro use the DDR3
stage-one loader; every other model uses DDR4.

| Model key | Official recon FSBL | Model-specific official guidance handled locally |
|---|---|---|
| `x40-ultra` | DDR4 | Wi-Fi reset helper; negative-deviceId repair |
| `x40-master` | DDR4 | Wi-Fi reset helper; negative-deviceId repair |
| `x30-ultra` | DDR4 | Wi-Fi reset helper |
| `l40-ultra` | DDR4 | Unsupported rebadged L40 warning; Wi-Fi reset helper; negative-deviceId repair |
| `l20-ultra` | DDR4 | R2394/R2253 brick warning and positive model verification; Wi-Fi reset helper |
| `l10s-ultra` | DDR4 | Supported-model documentation distinguishes the unsupported Gen2 |
| `l10s-pro-ultra-heat` | DDR4 | R2338/R2338H gate; Wi-Fi and negative-deviceId repair; MCU firmware resync guidance |
| `l10s-pro-ultra-heat-h` | DDR4 | Separate revision identity and firmware; the same post-root Heat guidance |
| `d10s-pro` | DDR3 | Supported-model documentation distinguishes the non-Pro D10s |
| `d10s-plus` | DDR3 | Supported-model documentation distinguishes the non-s D10 Plus |
| `w10-pro` | DDR3 | Empty `cloudKey` is recovered from secure storage before Valetudo starts |
| `mova-s20-ultra` | DDR4 | Wi-Fi reset helper |
| `mova-p10-pro-ultra` | DDR4 | Supported-model documentation distinguishes the unsupported P10 Ultra |

The verifier also checks each model code against its current Valetudo implementation class and
checks the architecture, Secure Boot value, rooting-method link, look-alike warning, and actionable
post-root notes in that model's official section. Tests drive recon and the destructive flash
transcript separately for all 13 profiles, not just one representative model.

## Procedure comparison and intentional differences

| Official instruction | Tool behavior |
|---|---|
| Factory-reset a robot previously connected to the vendor cloud. | Printed before recon. The tool cannot prove a physical factory reset, so this remains an explicit user action. |
| Use native Debian, a root shell, distro `sunxi-tools`, and Google's `fastboot`. | Intentionally broader: macOS and Linux are supported without running the whole tool as root. The pinned `sunxi-fel` build is verified, and a libusb fastboot client is used because Google's client cannot enumerate the Dreame gadget reliably on Apple Silicon. |
| Perform the PCB button sequence and verify FEL. | The same button timing and disconnected OTG jumper are printed; `sunxi-fel ver` is polled automatically. |
| Load the model's DDR3/DDR4 FSBL at `0x28000`, then `payload.bin` at `0x4a000000`. | Same files, addresses, order, five-second wait, and fastboot wait. |
| Read `dustversion`, then `config`. | Both are read. `config` is intentionally read first because it creates or binds the per-robot workspace before the remaining diagnostic values are persisted. |
| Pull `dustx100`, select stage 1, pull `dustx101`, select stage 2, and pull `dustx102`; check sizes and zip them. | Same command order, with stricter minimum-size, alignment, ZIP-member, and corruption checks before the backup is accepted. |
| Build and download a DustBuilder FEL image. | Same FEL format and model-specific form choices. The installed guide is static so a website outage cannot stop rooting; separate CI compares every live form with a semantic golden. |
| Re-enter FEL using `fsbl.bin` and `payload.bin` from the built ZIP. | Same files, addresses, order, and wait. Extracted members are additionally bound to the selected model and SHA-256 checked immediately before flashing. |
| Read live `config`; run `oem dust`, `oem prep`, flash `toc1`, then both boot/rootfs slots; require `OKAY`; reboot. | Same wire order. The tool additionally binds the ZIP, recon identity, live device, model revision, and recovery evidence before the first write, masks terminal-loss signals during the flash window, and stops on the first non-`OKAY`. |
| Download the model's Valetudo binary, use the helper HTTP bridge, back up `/mnt/private` and `/mnt/misc`, install the postboot hook, and reboot. | The same `aarch64` binary and robot-side destinations are used. Direct SSH streaming replaces the helper webserver intentionally. The backup is expanded to raw private/misc partitions and `/etc/*.pem`, validated before publication, and bound to the live robot identity before Valetudo is copied. |
| Follow model-specific Wi-Fi, negative-deviceId, W10 Pro key, and Heat MCU recovery notes. | Exposed as `fix-wifi`, automatic plus manual `fix-did`, automatic plus manual `fix-key`, and the Heat post-install warning. |

These differences automate manual work or add fail-closed checks; none changes the official flash
payloads, addresses, partition order, or robot-side Valetudo installation contract.
