# #324 File Stefan Gehr's L40 Ultra reports if he doesn't

- Kind: issue, state: open, by cody, created 2026-11-22T17:22:48-08:00
- Labels: status:blocked, type:chore; milestone: none

Trigger: Stefan Gehr (Forgejo BoostCookie, activated 2026-11-22) emailed Cody about restoring his L40 Ultra to stock with dreame-valetudo. On 2026-11-22 at 1:40 PM PT Cody asked him to open two issues here himself. If he has not by the evening of 2026-11-23 (PT), file them and close this one; if he has, link them and close this.

1. On the L40 Ultra (still listed as untested), the FEL to fastboot handoff fails. His workaround: when the tool starts its 90-second wait, power the robot off, power it back into FEL, and run `./valetudo-helper-felroot-amd64 boot fsbl.bin payload.bin`; the tool then recognised everything and finished. Ask for the tool output if he still has it.
2. Feature: an `import` command (e.g. `--dustxzip`, `--config`) for backups made with the old workflow. He restored one by hand: copied his dustx zip to `work/robots/<id>/recon/dreame_recovery_backup.zip`, wrote `config: <config>` to `recon/config.txt`, then ran `restore`, which decrypted and flashed.

Cody agreed this plan in chat on 2026-11-22.
