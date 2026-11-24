# #326 Import old backups

- Kind: issue, state: open, by BoostCookie, created 2026-11-23T12:56:02-08:00
- Labels: status:needs-decision, type:feature; milestone: none

## What
Would be nice to have an import command for people who used the old setup before.

## Why
This program assumes that `recon` was done already with this program, which stores the backups and configs into a very specific directory layout.

## Done when
- [ ] new import command which takes the `config` value and the zipfile with the `dustx___.bin` files as arguments

## Depends on
---

## Manual workaround
Here is my manual workaround:
```shell
% mkdir -p ~/dreame-valetudo/work/robots/r2492-aebd2a2f6a86/recon/
% cp path/to/oldbackup/dreame_rxxxx_samples.zip ~/dreame-valetudo/work/robots/r2492-aebd2a2f6a86/recon/dreame_recovery_backup.zip
% echo 'config: aebd2a2f6a86<REDACTED>' > ~/dreame-valetudo/work/robots/r2492-aebd2a2f6a86/recon/config.txt
```
Essentially just the zip with the `dustx___.bin` files and the `config` value at that time need to be put into the correct place. After that, running `restore` automatically unpacks the zip, decrypts the `dustx___.bin`s and places all the other necessary files.
