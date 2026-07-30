# UART bench collector from a 0.4+ source package

This guide is only for source archives reporting version 0.4 or newer. The 0.3 release line has the
established manual UART walkthrough, not these collector commands. Never copy collector files into
a 0.3 checkout or package.

The collector exists to gather bench evidence. It does not automate UART rooting and it does not
run the DustBuilder installer. `uart-observe` only receives boot bytes. `uart-adopt` logs
into an existing shell, runs its reviewed inventory, and stages a verified backup under robot
`/tmp` before copying it into the private host backup area.

## Install the package-matched transport

Check the downloaded source tarball against the release's published SHA-256, then extract it and
install its `uart` extra. Do not install an unrelated global pyserial or `dreame-uart` helper.

```bash
shasum -a 256 -c SHA256SUMS --ignore-missing
tar -xzf dreame-valetudo-<version>.tar.gz
cd dreame-valetudo-<version>
uv sync --extra uart --frozen
uv run dreame-valetudo version
```

For an isolated command install instead, use `uv tool install '.[uart]'` or
`pipx install '.[uart]'` from the extracted directory. Confirm the command reports 0.4 or newer
before attaching an adapter.

## Collect receive-only boot evidence

Connect only one robot and one 3.3 V UART adapter, with a common ground. Do not connect the
adapter's power lead. Select the exact robot model and robot workspace explicitly:

```bash
DREAME_MODEL=z10-pro DREAME_ROBOT=<robot-id> uv run dreame-valetudo uart-observe
```

Follow the power-cycle prompt. The command records raw receive bytes and a sanitized summary. A
model mismatch, missing boot evidence, ambiguous adapter, or changed collector/helper fingerprint
stops the campaign.

## Adopt an already rooted shell

Keep the same robot and adapter connected. Have the full dustbin-sticker serial available locally;
it is entered as a secret and must not be pasted into notes or logs.

```bash
DREAME_MODEL=z10-pro DREAME_ROBOT=<robot-id> uv run dreame-valetudo uart-adopt
```

Review every displayed identity and rooted-status result. The collector refuses persistent robot
writes, never runs `install.sh`, and never marks an unknown or stock image as rooted. A successful
run publishes a private, manifest-bound backup generation. Keep that backup before changing the
robot or disconnecting the bench.

If the command reports uncertain identity, changed hardware, missing backup members, a hash
mismatch, or an unsupported firmware state, stop. Preserve the output bundle and use the manual
procedure documented at <https://valetudo.cloud/pages/installation/dreame/#uart-shell>; do not
improvise a write command.
