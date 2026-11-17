# #18 Protect existing SSH key material

- Kind: pull, state: closed, by cody, created 2026-07-27T11:37:39-07:00
- Labels: none; milestone: none
- refs/pull/18/head -> main, merged: True

Refuses incomplete SSH key pairs before key generation, closes ssh-keygen stdin, reuses existing dedicated keys, and handles an empty safe-choice set explicitly. Coverage reproduces both audit triggers and mutation-checks the final race guard.
