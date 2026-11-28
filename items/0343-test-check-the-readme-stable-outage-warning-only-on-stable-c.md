# #343 test: check the README stable-outage warning only on stable cuts

- Kind: pull, state: closed, by cody, created 2026-11-27T07:43:35-08:00
- Labels: none; milestone: none
- refs/pull/343/head -> main, merged: True

Closes #342

## What and why

Every Prerelease dispatch failed at "Qualify prerelease intent". That job runs the suite with the rc version stamped in, and `test_readme_stable_outage_warning_names_the_current_stable` refused the README warning that stable v0.2.1 cannot root a robot. On an rc that warning is still true, because v0.2.1 is still the stable release. The check now runs only when `__version__` is a stable version, which is the release cut it exists to guard (#322).

## Sibling repo

- [x] Not applicable, because: whiskerless has no stable-outage warning or test for one.

## Checks

- [x] Regression test that fails on the parent commit (`test_stale_outage_warning_fails_a_stable_cut_but_not_an_rc`)
- [x] `STANDARD.lock` still in sync (no vendored file edited in place)

The full suite passes on a tree stamped with `packaging/stamp-version.py 0.3.0-rc.53`: 2505 passed.


## forgejo-actions, 2026-11-27T07:43:52-08:00

<!-- ai-review -->
**AI review** of `8e473d6e69`: Small, focused fix that skips the README stable-outage assertion on prerelease versions and adds a regression test covering all branches of the extracted helper. Logic is correct, tests cover the rc case, and the change directly serves issue #342.

No findings.

Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W10= -->
