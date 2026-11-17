# #108 fix(bench): USB without the internet, and a hardware session in one command

- Kind: pull, state: closed, by cody, created 2026-08-18T10:42:05-07:00
- Labels: none; milestone: none
- refs/pull/108/head -> main, merged: True

Everything the rc.12 bench session turned up, plus the conductor that keeps it from happening again.

**Product defects**

- A Homebrew install resolved pyusb from PyPI on every fastboot call. That fails on the robot own AP, which has no internet and is exactly where the install runs. The formulae now vendor the pinned pyusb into their virtualenv, the transport prefers an interpreter carrying that exact version, and a failure that is really the network says so instead of blaming libusb.
- Answering "continue?" after a failed command ran the auto chain rather than the command that failed, then reported every phase complete.
- rekey over USB announced a reboot it never confirmed (the request is unacknowledged, and a FEL-booted robot often stays off), then judged the key against whatever answered 192.168.5.1 — the router, on a home network.

**Bench**

- `bench campaign` runs a whole session: it re-reads what the robot can qualify before each scenario, says what to do and what to answer first, waits for the robot rather than for any responder, and explains every skip.
- It schedules in passes so a write cannot strand the scenarios that needed the state it took, and asks before spending a one-time boundary.
- `usb-drop-recon` is gated to a robot with no write history — recon skips the recovery pull otherwise, so it had no transfer to interrupt and failed a healthy robot. Restoring to stock does not reopen it.
- `rekey-over-usb` is judged on the key it authorized, not on a reboot nobody confirmed.
