# #124 Give the tap job its own Python, and clamp the two unguarded floors

- Kind: pull, state: closed, by cody, created 2026-08-23T06:33:02-07:00
- Labels: none; milestone: none
- refs/pull/124/head -> main, merged: True

**The tap job.** update-tap.sh needs a build frontend, and pip refuses to install into an externally managed system interpreter (PEP 668). The job had no setup-python, so the formula never reached the tap on rc.17 — after the tag was cut and PyPI already had the upload. The sibling has always set up its own Python here.

**The floors.** debian-12-floor and ubuntu-22.04-floor carried no Renovate rule, which is how PR #121 (raise the Debian floor to 13) came to exist at all. Merging it would have turned the floor leg into a second current test.

Both invariants are mirrored in whiskerless.
