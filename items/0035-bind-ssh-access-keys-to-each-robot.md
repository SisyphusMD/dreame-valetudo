# #35 Bind SSH access keys to each robot

- Kind: pull, state: closed, by cody, created 2026-07-27T20:54:54-07:00
- Labels: none; milestone: none
- refs/pull/35/head -> main, merged: True

Records the key embedded in each robot image, resolves it for push and every post-root SSH command, and prevents OpenSSH configuration or agent identities from being offered alongside an explicit robot key. Includes composed multi-robot and command-dispatch regressions.
