# #29 Keep rooted resumes off the FEL toolchain

- Kind: pull, state: closed, by cody, created 2026-07-27T17:32:00-07:00
- Labels: none; milestone: none
- refs/pull/29/head -> main, merged: True

Closes audit finding M16. Splits stage1 and Valetudo fetching, sends rooted resumes directly through a verified Valetudo-only path, preserves forced recon, and diagnoses missing curl or ssh without provisioning USB tools.
