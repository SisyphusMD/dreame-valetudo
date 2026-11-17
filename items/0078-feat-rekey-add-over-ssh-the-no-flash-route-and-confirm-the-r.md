# #78 feat(rekey): add --over-ssh, the no-flash route, and confirm the result on both routes

- Kind: pull, state: closed, by cody, created 2026-08-04T14:28:49-07:00
- Labels: none; milestone: none
- refs/pull/78/head -> main, merged: True

`rekey` could authorize a key but never checked whether the robot took it — it printed an `ssh` command for the operator to run. That is the thing this tool exists not to do.

**Both routes now confirm their own result.** The USB route walks the operator onto the AP the way `push` already does and reports one of three outcomes, not two: confirmed, refused, or *unproven*. Declining the check and failing to reach the AP are both unproven — reporting a rejection nobody observed is what sends an operator hunting a fault that may not exist.

**`--over-ssh` is a complete second route that writes nothing to flash.** The rooted image's `/etc/rc.d/adbd.sh` sets root's password from the serial under the dustbin on every boot and starts dropbear without `-s`, so the same edit becomes an ordinary file write: no button sequence, no partition write, nothing opened. It shares `_compose` and the removed-key reporting with the USB route, so the semantics are identical — replace by default, `--keep-existing` appends, every dropped key named with fingerprint and comment first.

This may deserve to be the preferred route: the USB path writes a partition carrying the unit's irreplaceable camera and lidar calibration, and this one does not.

**Secret handling.** The serial is read with a new `Console.ask_secret` (`getpass`, with `GetPassWarning` promoted to an error so there is no path where it echoes). Neither the serial nor either derived password ever reaches argv, the environment, the console, or the run log — the environment names a `0700` helper, deleted in a `finally` and re-swept at the start of every run, and a log scrub rule backstops any future call site. `Runner.run` gained no `env` parameter; that seam stays as it is.

**Known limitation:** OpenSSH older than 8.4 has no `SSH_ASKPASS_REQUIRE` and prefers `/dev/tty`, so the route degrades to a failed login there — never to a leak — and the guidance says so.

Also fixes a bug in the USB route's verification that never shipped: it used the bare AP address, so it logged in as the host username. The robot only has `root`, so that check could never have succeeded on hardware.
