# #36 Keep image staging and reboot guidance recoverable

- Kind: pull, state: closed, by cody, created 2026-07-27T21:03:26-07:00
- Labels: none; milestone: none
- refs/pull/36/head -> main, merged: True

Makes the dustbuilder checklist explicit about key upload and prepackaging, discloses sensitive recovery-image contents, repairs stale image markers through the normal flow, preserves the config-rejection fallback when auxiliary setup fails, and avoids offering a Wi-Fi AP immediately after reboot dropped it.
