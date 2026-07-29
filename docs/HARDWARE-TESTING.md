# Hardware bench qualification

This is the release-candidate qualification plan for a real Dreame robot. It complements the
off-hardware Python and integration suites; it does not replace them. The automated suites prove
the commands and safety gates the tool *would* issue. The bench proves that the real USB, boot ROM,
bootloader, Wi-Fi, SSH, storage, power timing, and packaged binaries behave the same way.

The built-in bench runner calls the same production phase implementations as a normal run, preserves
their interactive prompts, records non-secret evidence, and checks the resulting state. Physical
transitions still require an operator: software cannot press the PCB button, move a cable, join the
robot AP, or confirm that the robot actually booted or docked.

## Running a campaign

Use a separate campaign for each exact executable, install channel, and physical robot. A campaign
is bound to the running version, SHA-256 fingerprints of its installed package and resolved hardware
helpers, one model, and one anonymous robot identity. Robot workspaces and campaigns remain portable:
each result records the OS, release, and architecture that produced it, so moving the campaign can
build a truthful multi-host matrix instead of relabeling earlier evidence. `DREAME_BENCH_BUILD` is
an expected version check, not a label:
the runner stops if it differs from the version actually executing. Set the metadata once in the
terminal where qualification will run:

```bash
export DREAME_BENCH_CAMPAIGN=0.3.0-rc.1-x40-pkg
export DREAME_BENCH_BUILD=0.3.0-rc.1
export DREAME_BENCH_CHANNEL=macos-pkg
export DREAME_MODEL=x40-ultra
export DREAME_ROBOT=x40

dreame-valetudo bench list
dreame-valetudo bench plan
dreame-valetudo bench run host-smoke
dreame-valetudo bench run stock-recon
```


`bench plan` is the state-aware conductor view. It reads the selected robot's saved lifecycle and
the signed campaign report, then labels each scenario PASS, READY, WAIT, RECORD, or SPECIAL and
prints the exact command for every scenario that is safe to start now. This lets the same campaign
tool work with a fresh stock robot, a newly rooted robot, or an older already-rooted reference
without pretending that tests requiring an earlier lifecycle state were performed. It does not
touch the robot and WAIT never counts as a pass.

The core sequence is automated by `bench run`: it invokes the real phase, checks durable markers,
recovery provenance, manifested backup counts, leftover partial generations, and the absence of
superseded dangerous state. Where the result exists outside the computer, it asks for the necessary
physical observation before recording a pass. If that final question is interrupted, the campaign
records it as pending; rerunning the same scenario resumes only the observation and never repeats
the completed hardware phase. Resume is accepted only while the signed result, current scenario
definition, selected robot, and post-phase workspace evidence still match. H3 runs add a separate
harness gate and still retain every normal production confirmation:

```bash
dreame-valetudo bench run first-root --allow-destructive
```

For safe-failure scenarios, `bench run` starts the real phase and the operator performs the action
in the table at the indicated time. The runner treats the expected stop as a pass only when
protected state, recovery artifacts, and published backups remain unchanged, and it asks the
operator to confirm that the named physical action actually occurred. The Ctrl+C recon case also
prompts for and validates a successful retry before it passes:

```bash
dreame-valetudo bench run usb-drop-recon
```

Only the scenarios shown as `record` by `bench list` remain manual. They require a research tool or
a different installed version that the running process cannot honestly impersonate. Follow the
table, then record the result explicitly, for example:

```bash
dreame-valetudo bench record upgrade-resume pass --model x40-ultra --robot x40 \
  --note "stable process finished; fresh RC launch migrated and resumed"
```

A skipped scenario is never silently treated as a pass. Its waiver must carry the reason, remaining
risk, and acceptor:

```bash
dreame-valetudo bench waive usb-drop-recon --model x40-ultra --robot x40 \
  --reason "no replaceable USB cable available" \
  --risk "physical read interruption remains unverified" --accepted-by owner
```

`dreame-valetudo bench report` exits nonzero until every scenario passed or has an explicit waiver,
and until the model, physical robot, and install channel are recorded. Every report and private
record carries an HMAC from the campaign key, so editing checklist results or removing a waiver's
private acceptance makes the campaign invalid instead of changing its conclusion. Share only the
displayed `report.json`. Robot names, config identities, credentials, state contents, and backup
paths are never written there; the adjacent `.robot-key` and `.private.json` are private campaign
bookkeeping and must not be shared. Free-form operator notes and waiver details live only in
`.private.json`; the shareable report records only that each required field was supplied.

## Safety classes

| Class | Meaning | Examples |
|---|---|---|
| H0 | Host only; no robot contact | package launch, help/version, dependency discovery |
| H1 | Read-only robot work | FEL discovery, RAM boot to fastboot, identity and recovery reads |
| H2 | Rooted-robot maintenance | SSH identity check, factory backup, Valetudo/UI diagnosis |
| H3 | Destructive flash | first root, deliberate reflash, stock restore, terminal-loss survival during flash |

H3 scenarios require `--allow-destructive` and a typed confirmation naming both the scenario and
the anonymous campaign robot slot. That protects against accidentally pasting an RC qualification
command while a daily-use robot is attached without copying its private display name into the run
log. It is an additional harness gate; the production CLI's own model, image, identity, recovery,
and final-risk gates still run normally.

Never deliberately test these on hardware:

- unplugging USB, removing power, killing tmux, or rebooting the host while a partition write is in
  progress;
- flashing an image for another robot or model;
- changing any staged payload/image or any published restore-kit artifact and attempting to execute
  or flash it, including `payload.bin`, `check.txt`, toc1, boot/rootfs, private, and misc;
- forcing a wrong R2338/R2338H or R2394/R2253 selection;
- bypassing a non-`OKAY` fastboot result.

Those cases are mutation- and transcript-tested off-hardware. A physical run would add brick risk
without proving anything the safe rejection path cannot prove before the first write.

## Bench inventory

Record this once per qualification campaign:

- robot marketing name, underside model/revision, and whether it started stock or rooted;
- host model, OS version, CPU architecture, install channel, and CLI version;
- breakout-board revision, USB cable, direct port versus hub, and power state;
- whether the robot was ever enrolled in Dreame Home/Mi Home;
- starting workspace backup, plus an off-host copy of every recovery/factory backup;
- RC tag and SHA-256 of the installed package.

Do not put the robot config, serial, miio key, SSH private key, or factory backup contents in a
shareable report. The runner records only model keys, scenario names, exit status, elapsed time,
and the presence of expected state/backup evidence.

### Research reference baseline

Before using a physical reference robot for any experiment that could change toc0, a hardware boot
partition, or another area outside the supported DustBuilder flow, preserve the fullest readable
baseline first. This is deliberately larger than the normal user's stock-restore kit:

- the complete `/dev/mmcblk0` user area, not only recon's three-slice boot-critical prefix;
- both `/dev/mmcblk0boot0` and `/dev/mmcblk0boot1` hardware boot areas;
- the reserved head and every named partition as separate cross-checkable images;
- GPT, partition, boot-version, config-prefix, toc0/toc1 hash, and readable eFuse observations;
- byte counts and SHA-256 hashes in a manifest, with at least two off-host copies.

Some device state is not an ordinary restorable file. eFuses are one-time hardware state, RPMB may
be authenticated or unreadable, and controller-internal state may not be exposed at all. Record
everything readable without claiming that every observation can be written back.

Capture broadly, restore narrowly. A future experiment must add and validate only the restore steps
for the exact regions it changes. It must also refuse to begin until its required pre-change images
exist and verify. The supported `restore` command remains narrower on purpose: normal DustBuilder
rooting does not change toc0 or the hardware boot areas, so rewriting them would add risk without
improving that rollback.

## Core qualification sequence

Run the applicable scenarios in this order. A factory reset does **not** make rooted firmware stock
again. The `stock-restore` scenario is the supported route back across that boundary; it reconstructs
the boot-critical stock set from recon rather than claiming to replay the entire eMMC.

| ID | Class | Starting state | What it proves | Expected result |
|---|---:|---|---|---|
| `host-smoke` | H0 | any | The selected package launches; its installed entry point reports this exact runtime version and its help works | clean exit |
| `research-baseline` | H1/H2 | stock reference robot | Full readable user area, both hardware boot areas, named partitions, metadata, and hashes are preserved before boot-chain research | manifest verifies; two off-host copies |
| `stock-recon` | H1 | stock | Real FEL discovery, correct DDR loader, fastboot enumeration, model/config read, and three-part recovery capture | recon marker plus valid recovery evidence |
| `legacy-root-adoption` | H1 | rooted by an older/manual flow, not yet in this workspace | Capture the current recovery evidence, explicitly identify its prior-root history, and choose leave-as-is | adopted rooted/Valetudo state; no flash attempt or firmware write |
| `recon-repeat` | H1 | stock, recon complete | A repeat adopts the same robot and refreshes identity without creating a duplicate | one robot, same identity |
| `first-root` | H3 | stock, image staged | Image/model/config/recovery gates and the exact timed flash sequence | rooted marker; robot reboots |
| `post-root-install` | H2 | rooted, no Valetudo | Robot-AP identity check, complete factory backup, binary install, key/DID repair, and reboot | Valetudo marker plus a new valid manifest |
| `implementation-fix` | H2 | rooted, Valetudo binary installed | Run `fix-impl` on the X40 (and on any other model whose autodetect fails); the live factory config must match the selected workspace before the implementation is written | identity gate and helper succeed; Valetudo UI starts |
| `rooted-resume` | H2 | Valetudo running | A normal rerun skips FEL and flashing and returns to the UI path | no USB request or flash |
| `diagnose` | H2 | Valetudo running | SSH/HTTP diagnosis recognizes a healthy rooted robot | clean diagnosis |
| `valetudo-update` | H2 | older Valetudo running | Live model/config and HTTP version checks, robot-side SHA-256, atomic executable replacement, and reboot | expected version in the UI; prior binary survives any pre-rename transfer failure |
| `stock-restore` | H3 | Valetudo running, off-host backups confirmed | Restore-kit derivation, A/B evidence checks, exact live identity, stock flash order, automatic-FEL watch, and resumable physical boot confirmation | stock boot; factory reset; restored-stock marker only after boot confirmation |
| `reroot-after-restore` | H3 | stock-restored | Bare auto refuses; explicit root `--force` performs a new deliberate rooting cycle | no automatic write; forced cycle succeeds |

The first RC should run the full sequence on the X40 Ultra reference robot. Repeat `stock-recon`
on every other physically available fastboot model because that is non-destructive and catches
loader, enumeration, identity, and model-table errors. Run `first-root` only when that particular
robot was already intended to be rooted.

## Failure and interruption scenarios

These reproduce mistakes a normal user can make without intentionally damaging flash.

| ID | Class | Operator action | Required behavior |
|---|---:|---|---|
| `fel-not-entered` | H1 | Start recon without doing the PCB sequence, then cancel | no robot/recon completion marker; useful retry guidance |
| `fel-wrong-timing` | H1 | Perform the button sequence incorrectly once, then correctly | same run keeps watching and succeeds after retry |
| `usb-drop-recon` | H1 | Unplug during the recovery *read*, never during a write | incomplete capture is rejected behind the refresh marker; any older good generation stays preserved; retry replaces it atomically |
| `ctrl-c-recon` | H1 | Press Ctrl+C while waiting or reading | clean interruption; rerun resumes without false completion |
| `terminal-loss-prompt` | H1 | Close the terminal at an ordinary question | tmux run survives, pending question is shown on rejoin |
| `wrong-model-recon` | H1 | After `stock-recon` binds the campaign correctly, use a temporary fresh robot workspace and deliberately select a different supported model | safety stop names the reported model; no completed recon; campaign remains bound to the real model |
| `wrong-robot-root` | H3 | Stage for robot A, attach robot B, stop before any write | live config mismatch stops before `oem dust` |
| `decline-flash` | H3 | Decline the final flash confirmation | successful cancellation and zero writes |
| `terminal-loss-root` | H3 | Close only the terminal client after the destructive sequence has visibly begun | tmux and signal masking carry the flash to completion; rejoin shows outcome |
| `wrong-robot-restore` | H3 | Select robot A's stock kit, attach robot B, stop before any write | live config mismatch stops before `oem dust` |
| `decline-restore` | H3 | Decline the stock-restore confirmation | durable kit may be prepared locally, but the robot receives zero writes |
| `terminal-loss-restore` | H3 | Close only the terminal client after the restore sequence has visibly begun | tmux and signal masking carry the restore to completion; rejoin shows outcome |
| `terminal-loss-after-restore-reboot` | H2 | Close the terminal after reboot is sent but before answering the stock-boot question | the pending observation resumes without another `oem dust` or flash |
| `restore-returns-to-fel` | H2 | Observe a post-restore automatic FEL fallback | no completion marker and no speculative alternate-generation flash; the durable attempt remains for inspection |
| `wifi-wrong-network` | H2 | Stay on the home LAN where `192.168.5.1` is the router | router is rejected as not-Dreame; no SSH write |
| `wifi-drop-backup` | H2 | Leave the robot AP during the factory-backup transfer | no published manifest/partial generation; retry succeeds |
| `ctrl-c-push` | H2 | Interrupt during a pre-install backup transfer | incomplete backup is removed; Valetudo marker is absent |
| `ssh-wrong-key` | H2 | Select an unrelated explicit key | error names authentication/key problem without password fallback |
| `already-rooted-recon` | H1 | Force recon on a rooted robot | identity refreshes, but the pre-root recovery capture is not overwritten |
| `already-rooted-root` | H3 | Invoke root normally on a rooted workspace | it refuses/skips without provisioning or flashing; only explicit `--force` can reflash |
| `offline-cached-binary` | H2 | Fetch while online, then join the offline robot AP and resume | digest-bound cached Valetudo remains accepted |
| `multi-robot-selection` | H2 | Keep two robot workspaces and connect the non-selected unit; this invokes the write-capable `push` path, so confirm the intended non-selected bench robot is on the AP | config mismatch stops before backup or install |
| `rename-resume` | H2 | Rename a completed robot, then resume | identity, key, state, and backup association remain intact |
| `upgrade-resume` | H2 | Start on the prior stable, stop at a prompt, resume and finish that process, then upgrade and launch a fresh RC process | the RC migration is atomic and the existing data remain intelligible |
| `downgrade-readonly` | H0 | Open an RC-migrated workspace with the older stable | older release refuses the newer layout without modifying it |

The wrong-model probe uses two workspace names so the harness can compare the known real robot
before and after recon adopts it. Keep `--actual-robot` on the correctly reconned workspace and use
a disposable `DREAME_ROBOT` name for the deliberate wrong selection:

```bash
DREAME_ROBOT=wrong-model-probe DREAME_MODEL=x30-ultra \
  dreame-valetudo bench run wrong-model-recon --actual-robot x40
```

## Platform and package matrix

Hardware behavior and packaging behavior are separate dimensions. Do not reflash the robot merely
to test another installer. Once the robot is rooted, use H0/H2 scenarios for package coverage.

CI installs, upgrades, exercises, and removes the amd64 `.deb` and `.rpm` in Debian 12, Ubuntu
22.04, Fedora 43, and openSUSE Leap 16.0 containers. It also builds the production Homebrew formula
from the exact source tarball and installs, tests, and removes it in Linuxbrew. Those checks prove
dependency resolution, package ownership, the installed entry point and helpers, and backup
preservation. The source tarball is separately inspected for its exact payload, installed into an
isolated Python environment, exercised, and uninstalled on every CI run. Tag builds execute the
exact `.deb` on both amd64 and arm64, while each native macOS release leg installs and removes both
the Homebrew formula and its signed `.pkg`.

Pre-merge containers cannot prove host USB permissions, a physical udev event, or communication
with a robot. The table below keeps those as explicit RC evidence instead of treating package smoke
tests as hardware passes.

| Platform/channel | Minimum RC evidence |
|---|---|
| macOS Apple Silicon `.pkg` | install on a Mac without Homebrew libraries; H0, H1, H2; uninstall leaves backups |
| macOS Apple Silicon Homebrew RC | H0, H1, H2; stable-to-RC and RC-to-stable switch |
| macOS Intel `.pkg` or Homebrew | H0 and H1 when hardware is available |
| Debian/Ubuntu amd64 `.deb` | install/upgrade/remove, udev access, H1 and H2 |
| Debian/Raspberry Pi arm64 `.deb` | SSH terminal-loss behavior, udev access, H1 and H2 |
| Fedora/openSUSE RPM | install/update/remove guidance, udev access, H1 and H2 |
| source checkout | H0 and H1 with system dependencies documented exactly |

## Release acceptance

An RC is eligible for stable promotion only when:

1. All normal CI jobs are green for the exact RC commit.
2. The published RC assets install and report the expected version.
3. Every applicable core scenario completes on the X40 reference robot. Any RC that adds or changes
   restore must complete `stock-restore` and `reroot-after-restore`; any other flash-changing RC must
   complete the full stock-to-Valetudo sequence. Only an explicitly non-flash-changing RC may use a
   previously rooted X40 and the applicable H0/H2 subset.
4. Every applicable safe failure scenario has a saved pass/fail report and no unexplained result.
   A skipped scenario requires a written waiver naming the reason, residual risk, and who accepted
   it; absence of an attempt is not a pass.
5. Recovery and factory backups produced on hardware validate and have an off-host copy.
6. No shareable report or log contains robot credentials.
7. Any failure is reproduced off-hardware before a fix is accepted, whenever a deterministic seam
   can represent it.

The bench report is evidence about one physical combination, not a universal claim. README model
status changes to Verified only after that exact model completes the end-to-end root and Valetudo
installation flow.
