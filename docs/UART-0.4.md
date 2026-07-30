# UART automation and Z10 Pro qualification plan for 0.4

This is the implementation contract for automating the older Dreame UART-shell family. It does not
turn the current manual flow into a partially automated one: every destructive gate below must
exist and pass its off-hardware tests before the first hardware write is enabled.

The upstream procedure was last checked against the Valetudo Dreame UART guide on **2026-07-29**:
<https://valetudo.cloud/pages/installation/dreame/#uart-shell>. The current CLI walkthrough follows
that procedure; the automation described here is future 0.4 work.

## Bench-collector status

The 0.4 development line carries the first U0-U2 implementation plus the reviewed U3 `/tmp` backup
capture for physical evidence collection. It is deliberately not part of 0.3 and does not enable U4:

- `uart-observe` opens the chosen adapter receive-only, proves the selected model from the boot
  banner, and retains the byte-for-byte capture plus a private summary.
- `uart-adopt` derives the login password from hidden local input, runs only the reviewed inventory
  allowlist, stages the four-path archive under robot `/tmp`, verifies the robot/host SHA-256, and
  publishes a private bundle containing the archive and full command evidence.
- U1 boot bytes remain a separate, model-only observation. They are never copied into an
  identity-bound U2/U3 bundle because the pre-login banner does not yet prove which same-model
  physical robot emitted it.
- The full factory config and the exact live hashes of every required identity member bind repeat
  runs to the same robot. A same-model identity mismatch stops before U3 or a new backup is
  published.
- Stock or unknown firmware still gets a complete evidence bundle, but is never marked rooted.
  Persistent root requires both a live UID-0 shell and the exact DustBuilder MOTD. Valetudo
  requires a canonical executable whose size, hash, architecture, and currently running
  `/proc/<pid>/exe` identity agree. No collector path runs `install.sh`, prepares a USB disk, or
  writes persistent robot storage.

The collector exists to maximize what can be learned during the first Z10 Pro bench session. Its
hardware results are inputs to the remaining 0.4 implementation, not a claim that UART installation
is release-qualified.

The current boot evidence has no known pre-authentication unique robot identifier. The collector
therefore asks the operator to keep one robot and adapter connected and re-read that robot's dustbin
serial before U2; the serial-derived credential should fail on a different robot, and no identity
bundle is published until the logged-in identity is verified. That is not a technical continuity
proof. The first bench session must check whether the boot stream exposes a stable unique value or
another non-secret challenge that can bind U1 to U2. Until that evidence exists, 0.4 must not claim
automatic same-model session binding.

For the exact source-package install and bench commands, see `UART-COLLECTOR.md` in this directory.
That operational guide ships only in 0.4+ source archives; 0.3 projections deliberately omit it.

## Scope and non-goals

The target is every profile whose `method == "uart"`, with the Dreame Z10 Pro (`p2028`) as the
first qualification robot. The fastboot/FEL engine must continue to reject these profiles. A D10S
Plus can exercise the shared conductor and host-side safety code, but it cannot validate UART
electrical behavior, login, or secure-boot installation; those need the Z10 Pro.

0.4 will automate the documented procedure and adoption of already-rooted UART robots. It will not
invent stock restore for this family, derive a missing dustbin serial, bypass secure boot outside
the DustBuilder installer, or guess a USB disk. Windows support should remain possible in the
transport design, but it is not a 0.4 release requirement.

## Safety tiers

| Tier | Allowed behavior | Examples |
|---|---|---|
| U0: host-only | No robot connection and no block-device write | package/form parsing, password vectors, fake serial transcripts |
| U1: observe | Receive serial bytes only | device discovery, boot banner, baud/cabling diagnosis, model evidence |
| U2: read-only shell | Log in and run allowlisted inspection commands | identity inventory, rooted/stock classification, backup reads |
| U3: reversible host/robot staging | Write only a selected removable USB disk or robot `/tmp` | root-stick image, backup archive, package transfer and hash checks |
| U4: firmware install | Run the verified model-bound `install.sh` once | secure-boot defeat, firmware install, reboot verification |

The conductor advertises the highest tier the attached robot and available evidence can safely
support. It runs every lower-tier scenario automatically and asks for physical actions only when
needed. A failure or disconnect lowers capability; it never silently promotes a run to a riskier
tier.

## Transport boundary

Add a standalone `libexec/uart-console.py` helper using a Renovate-pinned `pyserial`. As with the
libusb fastboot helper, the main package keeps zero import-time dependencies: source runs resolve an
on-demand helper environment, while release packages carry a frozen helper binary.

The dependency must be provisioned before a hardware session. A checkout uses
`uv sync --extra uart`; a tool install uses `uv tool install '.[uart]'` or
`pipx install '.[uart]'`. Homebrew installs the exact hash-pinned pyserial resource in the formula
virtualenv. Native `.pkg`, `.deb`, and `.rpm` channels carry `dreame-uart` with pyserial frozen in.
The source archive includes `uv.lock`, and the fallback resolver uses only that package-matched lock
in frozen offline mode; it never borrows an unrelated global `dreame-uart` or resolves dependencies
after the serial session begins.

The helper owns bytes and timing, not policy. It exposes framed operations such as open, read-until,
write-line, drain, and close. The Python phase owns the state machine and sends every helper command
through `Runner`, preserving transcript-equivalence tests. Never put shell commands, model choices,
or retry policy inside the helper.

The CLI authenticates the selected helper again immediately before every process spawn and removes
ambient Python, uv, and dynamic-loader controls from that process. This detects stale packages and
ordinary concurrent replacement. It is not a security boundary against a malicious process already
running as the same host user: that user can modify package files and race a pathname between hash
and `exec`. Do not run the collector alongside untrusted code under the account that owns its
installation and private evidence directories.

Serial handling must tolerate fragmented UTF-8, binary boot noise, carriage-return variants, and
unrelated kernel output interleaved with the prompt. After login, establish a random command nonce
and recognize completion only from a nonce plus exit status. A bare `#` or `$` is not proof of a
shell. Timeouts report whether the likely cause is no RX, swapped RX/TX, wrong baud, lost power, or
multiple shells; they do not send speculative input.

## State machine and gates

1. **Discover and observe.** List serial devices without opening them for output. The operator
   chooses when more than one exists. Open RX-only, capture the boot banner, and compare the reported
   model with the selected profile. A mismatch stops before login.
2. **Classify.** Determine stock, already rooted, or unknown from read-only evidence. An already
   rooted robot enters adoption/maintenance and is never told to factory-reset. A stock robot that
   has used a vendor app gets the upstream factory-reset instruction before continuing.
3. **Authenticate.** Ask for the full dustbin-sticker serial locally, normalize only surrounding
   whitespace, retain Xiaomi prefixes such as `41717/`, calculate the password locally, and never
   log either value. Refuse copied app/base/box identifiers as substitutes.
4. **Inventory.** Record model, implementation, architecture, firmware/root status, available disk,
   and hashes of identity files. Bind subsequent state to this evidence. An old rooted robot can be
   adopted here without reinstalling.
5. **Back up.** Create an archive containing `/mnt/private/`, `/mnt/misc/`,
   `/etc/OTA_Key_pub.pem`, and `/etc/publickey.pem`. Transfer it to a private, generation-staged host
   directory, reject missing/empty members, hash it, write a manifest, and publish atomically. U4 is
   impossible without a verified generation.
6. **Build and bind.** Guide the model-specific DustBuilder manual form with both **Prepackage
   Valetudo** and **Patch DNS** selected. Inspect the returned archive, bind its model/class and hash
   to the live identity, and refuse a mismatch or ambiguous package.
7. **Transfer.** Run a built-in, tokenized HTTP bridge bound only to the robot-facing
   `192.168.5.x` interface. Accept the backup upload with a size limit and atomic rename; serve only
   the exact package path. Verify host and robot hashes. Never bind to every interface or serve a
   browsable directory.
8. **Install.** On secure-boot profiles, prove no filesystem-modifying command outside `/tmp` was
   sent before the installer. Require a fresh explicit dock confirmation, then run the exact
   model-bound `install.sh` once. A disconnect after launch records an uncertain attempt and resumes
   observation, never installation.
9. **Verify.** Require the post-reboot DustBuilder MOTD, the expected Valetudo process/binary class,
   and reachable Web UI. Only then mark rooted. Offer current Valetudo maintenance on every later
   connection, whether the robot was rooted by this tool or adopted.

State markers should be evidence-bearing records, not booleans: `uart-observed`, `uart-identity`,
`uart-backup`, `uart-package`, `uart-install-attempt`, and `uart-rooted`. Each includes the profile,
live identity fingerprint, relevant hashes, and tool version. Resume rechecks the live identity and
starts at the first unproved gate.

## Safe USB-stick preparation

`dreame-valetudo prep-stick` is a separate host operation because selecting a block device is the
most dangerous non-robot action in this flow.

- Download and hash the upstream known-good image; retain its source URL and verification date.
- Enumerate with `diskutil list -plist` on macOS or `lsblk --json` on Linux. Never infer a target
  from “the newest disk.”
- Display path, transport, removable/internal flag, size, vendor, model, mounts, and partitions.
- Refuse the system disk, an internal disk, an unresolved parent, a mounted target that cannot be
  cleanly unmounted, or a target too small. Unknown removability fails closed.
- Require the operator to type the exact whole-disk path and a displayed random confirmation word.
- Unmount, write, flush, read the image-sized prefix back, compare SHA-256, then eject. A write or
  verification failure leaves no “prepared” marker.

No wildcard, environment variable, saved prior target, or single-disk shortcut may select the
destination.

## Automated test matrix

### Pure and fake-device tests (CI)

- Password vectors: ordinary uppercase serial, Xiaomi slash prefix, whitespace rejection, damaged
  or missing serial, and secret redaction.
- Every UART profile: baud, architecture, secure-boot branch, DustBuilder page, Valetudo class, and
  model-specific tip.
- PTY serial peer: byte-at-a-time and split banners, CR/LF variants, boot noise, invalid UTF-8,
  interleaved kernel logs, wrong prompt, duplicate shell, delayed password prompt, rejected login,
  disconnect/reconnect at every state, and timeout diagnosis.
- Command framing: fake prompts inside output cannot complete a command; nonce mismatch and missing
  exit status fail closed; no modifying command precedes the backup gate on secure-boot profiles.
- Backup: every required path, empty/missing member, truncated upload, oversize upload, hash
  mismatch, private modes, interrupted generation, and never-clobber publication.
- HTTP bridge: robot-interface-only binding, unguessable token, exact-path serving, traversal and
  duplicate upload rejection, size limits, cancellation, and atomic completion.
- Package: wrong model/class/architecture, missing installer or prepackaged Valetudo, archive
  traversal/link tricks, duplicate members, corrupted/truncated tar, and robot/host hash mismatch.
- Install resume: disconnect before launch is retryable; disconnect after launch is uncertain and
  never launches again; missing MOTD or wrong implementation cannot mark rooted.
- Stick writer: synthetic `diskutil`/`lsblk` inventories covering the system disk, internal disk,
  USB disk, ambiguous parents, mounted volumes, hot-unplug, short write, flush failure, readback
  mismatch, and exact successful transcript. CI never opens a real block device.
- Adoption and maintenance: stock, legacy-rooted, current-rooted, unknown, stale Valetudo, current
  Valetudo, and deliberate re-root choice remain distinct.

### Physical Z10 Pro campaign

The bench conductor begins with U1/U2 against the already-rooted Z10 Pro: boot observation,
pre-authentication continuity discovery, model match, login, read-only inventory, adoption,
complete backup, and reconnect/resume at each prompt. It must preserve the existing installation.
Valetudo maintenance remains a later 0.4 lifecycle step after the identity and transport evidence
from that session has been reviewed.

U3 qualifies USB preparation on a sacrificial stick, including intentional wrong-disk selection,
hot-unplug, and readback verification, then verifies the robot spawns exactly one UART shell.

U4 requires a separately approved stock-capable campaign with identity backup and a known recovery
plan. Run first-root, interrupt before installer, disconnect immediately after installer launch,
post-reboot MOTD verification, already-rooted rerun, adoption, and update. The existing rooted Z10
Pro alone cannot honestly prove the factory-new path unless it is first returned to a known stock
state by a separately validated procedure.

Every physical result records tool/build version, profile, capability tier, sanitized evidence,
operator actions, start/end time, and pass/fail/skip reason. “Skipped because this robot cannot
exercise the scenario” is evidence, not a pass.

## Delivery order

1. Land the PTY helper protocol and fake-device matrix with all production writes disabled.
2. Land read-only discovery, classification, and adoption; qualify U1/U2 on the Z10 Pro.
3. Land the private HTTP bridge, backup manifest, and package validator; qualify backup/transfer.
4. Land `prep-stick` behind its full target-selection suite; qualify on sacrificial media.
5. Enable install only after the secure-boot command-order and uncertain-resume gates pass a cold
   review, then run the approved U4 campaign.
6. Mark a UART model automated only after its model-specific physical report is complete. Other UART
   profiles remain guided-manual until their own hardware differences are qualified.
