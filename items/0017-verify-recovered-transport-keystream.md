# #17 Verify recovered transport keystream

- Kind: pull, state: closed, by cody, created 2026-07-27T11:20:43-07:00
- Labels: none; milestone: none
- refs/pull/17/head -> main, merged: True

Pins the existing public SHA-256 fingerprint of the locally recovered transport keystream, rejects constant-XOR-offset false positives, and makes zero-fill sampling coprime with the keystream period. Coverage includes adversarial unit, public-helper, migration no-output, and stride regression tests.
