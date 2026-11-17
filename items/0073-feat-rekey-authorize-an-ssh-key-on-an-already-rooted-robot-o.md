# #73 feat(rekey): authorize an SSH key on an already-rooted robot over USB

- Kind: pull, state: closed, by cody, created 2026-08-04T12:24:43-07:00
- Labels: none; milestone: none
- refs/pull/73/head -> main, merged: True

The built image copies its key to `/mnt/misc/authorized_keys` only when that file is absent, and `misc` is not one of the five partitions a root flash writes. So the key uploaded to the builder is installed only on a robot that has never been rooted, a robot whose key was lost could not be reached again, and re-rooting it silently could not help. Found on hardware at the bench, then confirmed off-hardware from the staged image and the pre-root capture.

`rekey` reads the live `misc` partition over USB, rewrites `authorized_keys` inside it, and writes the partition back.

- Replaces by default; `--keep-existing` appends. This is the only way to REMOVE a key from the robot, so appending by default would leave a lost key authorized with no way to revoke it. Every key about to stop working is named, with fingerprint and comment, before the write.
- Two FEL sequences per run, one to read and one to write. The rail clock starts at the button sequence and cannot be extended, so the 400 MiB read and the operator's decision must not run down the window the write has to finish inside.
- `oem dust` authorizes the write; `oem prep` is deliberately never issued, so Secure Boot stays on.
- `misc` also carries this unit's camera and lidar calibration, so it is read-modify-written live and never replayed from a capture. A pre-write copy is kept, fsynced, and never overwritten by a later run.
- An interrupted flash leaves a durable marker; the next run offers to put the pristine copy back rather than reading a half-written partition and laundering damaged calibration into the new baseline.

Also corrects the guidance that caused the wasted flash: `recon`'s adopt-vs-re-root prompt and `image`'s checklist now say outright that re-rooting cannot install a new key, and `docs/DESIGN.md` no longer claims the uploaded key simply lands in `authorized_keys`.

New `dreame_valetudo/ext4.py` is a minimal, stdlib-only in-place editor: it writes only bytes the file already owns and refuses every layout it cannot represent that way (metadata_csum, inline data, non-extent, multi-extent, non-regular inodes). Tested against a real ext4 fixture built with the robot's geometry, and validated against the actual partition read off the bench robot.
