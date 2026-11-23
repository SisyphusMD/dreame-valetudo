# #321 Stable v0.2.1 cannot root a robot: ship the Dustfab fix to a stable release

- Kind: issue, state: open, by cody, created 2026-11-22T10:02:18-08:00
- Labels: status:blocked, type:bug; milestone: none

The latest stable release is v0.2.1 (2026-07-24), and it is what the default install (Homebrew \`dreame-valetudo\`, \`releases/latest\`) gives a new user. It still fetches stage 1 from \`builder.dontvacuum.me\` (\`STAGE1_URL\` in \`dreame_valetudo/profiles.py\`), which refuses connections (re-checked 2026-11-22). So every fresh stable install dies at the first download, right after the robot is named.

#277 fixed this on main (#278, 2026-11-16, Dustfab), and it ships in v0.3.0-rc.50 through rc.52, which are all prereleases.

Reported by an outside user on 2026-11-22: "One of the first URLs it reaches out to is down and it's unable to complete the first steps after a name is configured."

Options:
- cut a stable release from current main (the two-step release flow from #297);
- or, until then, point the README and the stable install at the rc channel.

Not proven off-hardware: a real robot FEL-booting the current Dustfab payload (see #277).

## cody, 2026-11-22T10:11:02-08:00

2026-11-22: replied to the reporter (Forgejo account mothent, now activated). I pointed them at the rc channel as a workaround (`brew install sisyphusmd/tap/dreame-valetudo-rc` or `pip install --pre`), told them Dustfab stage 1 is unproven on hardware, and asked for the log if it fails. Next: a stable release from main. If they send a run log, it is the first hardware evidence for #277.

## cody, 2026-11-22T10:12:55-08:00

Decision: Do not cut a stable release yet. Stable waits until the Dustfab stage 1 has run on a real robot (the reporter's run or Cody's own); until then the README sends new users to the release candidate. Why: A stable release now would ship a download path that has never been run on hardware. The rc channel exists for that proof.

Decided by: Cody, 2026-11-22.

## cody, 2026-11-22T10:35:20-08:00

README warning is live (#322/#323). Still blocked on a hardware run of the Dustfab stage 1 (the reporter's or Cody's). Then cut the stable release, which removes the warning (the guard test enforces it).
