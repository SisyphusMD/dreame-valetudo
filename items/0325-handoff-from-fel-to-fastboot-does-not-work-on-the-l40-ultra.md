# #325 Handoff from FEL to Fastboot does not work on the L40 Ultra

- Kind: issue, state: open, by BoostCookie, created 2026-11-23T12:35:40-08:00
- Labels: status:in-progress; milestone: none

### Version

0.3.0-rc.52

### How did you install it?

source tarball

### Host OS and architecture

Arch Linux amd64

### Robot model

l40-ultra

### Which phase?

recon

### What happened

I only tried recon and restore, but this probably happens for all phases that require a FEL->fastboot handoff.
At one point the program sends the payload to the robot to hopefully put it into fastboot and then it waits for 90 seconds to hopefully discover the fastboot devices. It is never discovered and the timer runs out.

### Output
This is what I saw in the terminal.
```text
Waiting for the FEL device — do the button sequence now. Ctrl+C stops waiting.
FEL up: AWUSBFEX soc=00001855(A133) 00000001 ver=0001 44 08 scratchpad=00042200 00000000 00000000
Watching for the FEL device — done

Booting fastboot payload via FEL...

Waiting up to 90s for the robot to come up in fastboot...
XX fastboot: FAILED no device
XX Robot never appeared in fastboot. Nothing was written to the robot's flash yet (this is a RAM
load) — power off, redo the FEL button sequence, and re-run. Still failing? Try the other USB
port or a data cable.
A scrubbed log of this run was saved to
<PWD>/restore-work/logs/run-20261119-165750-018241.log
You can share it to report the problem:
https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/issues
```

This is what my snapshot of `<PWD>/restore-work/logs/run-20261119-165750-018241.log` contains. I hope this is actually the correct info.
```text
# dreame-valetudo 0.3.0-rc.52   Thu Nov 19 16:57:50 CET 2026
# command: restore
# platform: Linux-7.2.14-arch1-1-x86_64-with-glibc2.44   python 3.15.2
# personal + identifying values are redacted below; safe to share
# each line is stamped [+seconds] elapsed since start; commands show their duration

[+   0.0s]    Robot: l40-factory-return (from DREAME_ROBOT)
[+   0.0s]    Model: Dreame L40 Ultra
[+   0.4s] == Restore the captured stock firmware — DESTRUCTIVE
[+   0.4s] !! This removes the rooted firmware and Valetudo. It restores private/misc, both boot/rootfs slots, and toc1 from this robot's pre-root capture.
[+   0.4s]    toc0 is verified but deliberately NOT rewritten: normal Dustfab rooting never changes it. This command is not for the experimental self-root/toc0 path.
[+   0.4s] !! The recon backup is a boot-critical prefix, not the whole 3.9 GB disk, so UDISK (/data) is not replayed byte-for-byte. Factory-reset the robot after it boots stock to clear the old local data and Valetudo files.
[+   0.4s] ?? Restore Dreame L40 Ultra to its captured stock firmware now?
[+  17.9s] -> yes
[+  18.1s] $ dreame-fastboot devices   (rc=1, 0.14s)
[+  18.1s] => Redo the PCB button sequence (steps above).
[+  18.1s]    (If the robot powered itself back on while you were deciding, hold power ~15s until it is fully off first.)
[+  18.1s] >> Waiting for the FEL device — do the button sequence now. Ctrl+C stops waiting.
[+  18.1s] $ sunxi-fel ver   (rc=1, 0.02s)
    ! ERROR: Allwinner USB FEL device not found!
[+  19.1s] $ sunxi-fel ver   (rc=1, 0.02s)
    ! ERROR: Allwinner USB FEL device not found!
[+  20.1s] $ sunxi-fel ver   (rc=1, 0.02s)
    ! ERROR: Allwinner USB FEL device not found!
[+  21.2s] $ sunxi-fel ver   (rc=1, 0.01s)
    ! ERROR: Allwinner USB FEL device not found!
[+  22.2s] $ sunxi-fel ver   (rc=1, 0.02s)
    ! ERROR: Allwinner USB FEL device not found!
[+  23.2s] $ sunxi-fel ver   (rc=1, 0.02s)
    ! ERROR: Allwinner USB FEL device not found!
[+  24.2s] $ sunxi-fel ver   (rc=1, 0.02s)
    ! ERROR: Allwinner USB FEL device not found!
[+  25.2s] $ sunxi-fel ver   (rc=1, 0.02s)
    ! ERROR: Allwinner USB FEL device not found!
[+  26.2s] $ sunxi-fel ver   (rc=1, 0.01s)
    ! ERROR: Allwinner USB FEL device not found!
[+  27.3s] $ sunxi-fel ver   (rc=1, 0.02s)
    ! ERROR: Allwinner USB FEL device not found!
[+  28.3s] $ sunxi-fel ver   (rc=1, 0.02s)
    ! ERROR: Allwinner USB FEL device not found!
[+  29.3s] $ sunxi-fel ver   (rc=0, 0.03s)
[+  29.3s]    FEL up: AWUSBFEX soc=00001855(A133) 00000001 ver=0001 44 08 scratchpad=00042200 00000000 00000000
[+  29.3s] -> Watching for the FEL device — done
[+  29.3s] >> Booting fastboot payload via FEL...
[+  29.5s] $ sunxi-fel write 0x28000 ~/Documents/valetudo.infos.for-restore/restore-work/cache/dist/fsbl.bin   (rc=0, 0.17s)
[+  29.5s] $ sunxi-fel exe 0x28000   (rc=0, 0.03s)
[+  43.3s] $ sunxi-fel write 0x4a000000 ~/Documents/valetudo.infos.for-restore/restore-work/cache/dist/payload.bin   (rc=0, 8.80s)
[+  43.3s] $ sunxi-fel exe 0x4a000000   (rc=0, 0.03s)
[+  43.3s] >> Waiting up to 90s for the robot to come up in fastboot...
[+ 133.9s] $ dreame-fastboot wait 90   (rc=1, 90.54s)
    ! FAILED no device
[+ 133.9s] XX fastboot: FAILED no device
[+ 133.9s] XX Robot never appeared in fastboot. Nothing was written to the robot's flash yet (this is a RAM load) — power off, redo the FEL button sequence, and re-run. Still failing? Try the other USB port or a data cable.
[+ 133.9s]    A scrubbed log of this run was saved to <PWD>/restore-work/logs/run-20261119-165750-018241.log
[+ 133.9s]    You can share it to report the problem: https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/issues

# exit 1 after 133.9s total
```

## BoostCookie, 2026-11-23T12:45:09-08:00

In the end I managed to find a workaround:
As soon as the 90-s-timer starts, turn the robot off by pressing the power button for 15 seconds.
Then turn it back on and put it into FEL mode (breakout button on, power button on, after 5 seconds power button off, after 3 more seconds breakout button off).

Then I used the old tool from Dustfab
https://fab.valetudo.cloud/build/dreame/r2492
where it says
> For initial rooting, click here to download the FEL recon package for this robot.

[link here](https://fab.valetudo.cloud/robot/dreame/r2492/fel/recon).

I ran
`./valetudo-helper-felroot-amd64 boot fsbl.bin payload.bin`
which was the old way of putting the robot into fastboot mode. After this `dreame-valetudo` should recognise the robot in fastboot mode and do its magic. The timer was saying 60 seconds, so 30 seconds would've still been left.

## cody, 2026-11-26T20:41:22-08:00

Claude (for Cody) has this one.

Lead so far, not yet proven on a robot: the Dustfab helper and this tool send the same files to the same addresses in the same order (FSBL at 0x28000, a short wait, payload at 0x4a000000). The difference is that every `sunxi-fel` call first runs its secure-boot workaround for the A133 (`SECURE_BOOT_MONITOR_H6`): it patches the boot ROM's secure-monitor vector and switches FEL into secure state. The Dustfab helper never does that, and the payload Dustfab rebuilt on 2026-10-27 is the one that now fails after it. Next: confirm that this is the cause, then load the FSBL and payload without the workaround.

## cody, 2026-11-26T21:30:11-08:00

Status: fix up as draft PR #338, not yet proven on a robot.

The FEL probe and the FSBL and payload load now go through the libusb client and send only the plain FEL requests the Dustfab helper sends, with no sunxi-fel secure-boot workaround. sunxi-fel remains only for `DREAME_FASTBOOT=system`.

Next: one recon run on an X40 Ultra, which gets the same Dustfab FSBL and payload as the L40 Ultra. If it reaches fastboot, that proves both the cause and the fix. If not, the same session runs `sunxi-fel ver` followed by the Dustfab helper's `boot`, to test the cause directly.
