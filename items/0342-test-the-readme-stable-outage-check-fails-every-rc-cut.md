# #342 test: the README stable-outage check fails every rc cut

- Kind: issue, state: closed, by cody, created 2026-11-27T07:38:00-08:00
- Labels: status:in-progress, type:bug; milestone: none

The Prerelease workflow cannot cut an rc. Its "Qualify prerelease intent" job runs the test suite with the rc version stamped in, and `test_readme_stable_outage_warning_names_the_current_stable` fails because README still warns that v0.2.1 is broken while `__version__` is `0.3.0-rc.53`. The warning is correct on an rc, because the stable release is still v0.2.1. The test exists to stop the warning riding into the stable release that fixes it, so it should check only stable versions.

Seen on the rc.53 dispatch (run 49580, job 235430) after the FEL fix (#325) merged.

Fix: skip the assertion when `__version__` is a prerelease, and add a test that covers the rc case.

## cody, 2026-11-27T07:58:19-08:00

Status: fixed by PR #343, merged. The rc.53 prerelease is dispatched again from main (1fd4790).
