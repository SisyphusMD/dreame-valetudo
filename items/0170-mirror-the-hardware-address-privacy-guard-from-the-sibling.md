# #170 Mirror the hardware-address privacy guard from the sibling

- Kind: pull, state: closed, by cody, created 2026-08-28T22:31:29-07:00
- Labels: none; milestone: none
- refs/pull/170/head -> main, merged: True

whiskerless gained this after a real robot MAC reached two shipped docs and a test fixture, and would have entered a stable sdist. Nothing caught it because the guard of the day only knew serials. Same matcher here: colon, hyphen, Cisco-dotted and bytes.fromhex spellings, maximal runs judged by shape so IPv6 and longer byte sequences are not misread.

The sibling SERIAL guard is deliberately not mirrored and the reason is recorded in project-standard/VARIANCE.md: an LR4 serial has a fixed shape a pattern can recognise, while a Dreame serial is [A-Za-z0-9][A-Za-z0-9._-]{5,63} — almost any token, so the same check would match prose on every run.
