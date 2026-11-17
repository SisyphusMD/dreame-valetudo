# #28 Fail clearly on SSH key and authentication errors

- Kind: pull, state: closed, by cody, created 2026-07-27T17:06:39-07:00
- Labels: none; milestone: none
- refs/pull/28/head -> main, merged: True

Closes audit findings M8, M9, and M10. Pins non-interactive SSH behavior in tests, fails immediately for missing explicit keys, and gives router-aware guidance for authentication and negotiation failures without misreporting them as an unreachable robot.
