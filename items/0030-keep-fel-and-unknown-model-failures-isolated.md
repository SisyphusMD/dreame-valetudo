# #30 Keep FEL and unknown-model failures isolated

- Kind: pull, state: closed, by cody, created 2026-07-27T17:44:16-07:00
- Labels: none; milestone: none
- refs/pull/30/head -> main, merged: True

Closes audit findings M11 and M12. FEL polling now trusts the real command exit status and keeps waiting after permission failures; saved models from newer releases remain visible and manageable without breaking healthy robots, while selecting one gives an upgrade error naming that robot.
