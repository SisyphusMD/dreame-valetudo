# #65 fix(renovate): make the digest automation actually write, and restore the staleness canary

- Kind: pull, state: closed, by cody, created 2026-08-03T15:17:27-07:00
- Labels: none; milestone: none
- refs/pull/65/head -> main, merged: True

Fixes the Renovate digest automation from #63, which **did not work**. The hourly run after that merge failed:

```
DEBUG: currentValue not found in string to replace
       stringToReplace: "\"b5ae76123ee33bfa...\""
       currentValue:    "2026.07.0"
DEBUG: Value is not updated
       expectedValue: "2026.08.0",  foundValue: "2026.07.0"
WARN:  Error updating branch: update failure
```

Renovate can only rewrite a value it finds **inside that dependency's own replaced text**. #63 gave the three digest deps a shared `currentValue` captured from `VALETUDO_VERSION_DEFAULT` while their replace regions were just the bare digest strings, so no digest region contained the version. One version and three digests cannot satisfy that with disjoint regions — and the regions must be disjoint, since overlapping ones break auto-replace a different way.

Net effect: Renovate stopped updating `renovate/hypfer-valetudo-2026.x` entirely, every hour. Nothing was corrupted and no wrong digest was written; it failed loudly.

## Fix

Annotate each digest with the release it came from:

```python
VALETUDO_SHA256 = {
    "aarch64": "e5e4487...",       # 2026.07.0
    "armv7": "e0d984d...",         # 2026.07.0
    "armv7-lowmem": "b5ae761...",  # 2026.07.0
}
```

Each dep's replace region is now one line carrying **both** its digest and a version, and the three are disjoint. Verified with the same image the cluster runs (43.288.0): three deps extract, each `replaceString` contains both values, and all three digests resolve correctly for 2026.08.0.

## The more important half

Decoupling the tests in #63 removed an accidental safety net. Before, a version bump with stale digests produced 9 red tests. After, those tests pass — so a half-applied bump would have shown **green CI on a PR that ships digests failing every install**. That canary was destroyed and not replaced.

`tests/python/test_constants.py` restores it deliberately, and offline: every `VALETUDO_SHA256` entry must be annotated, and every annotation must equal `VALETUDO_VERSION_DEFAULT`. Simulating the exact bug (bump the version, leave the digests) fails with:

```
digest annotations disagree with VALETUDO_VERSION_DEFAULT=2026.08.0:
{'aarch64': '2026.07.0', 'armv7': '2026.07.0', 'armv7-lowmem': '2026.07.0'}
```

A second test guards the guard — if the annotation format ever changes, the regex would match nothing and the check would pass vacuously for any input, so the match count is pinned.

The annotation is genuinely useful independent of Renovate: a reader can see which release each digest belongs to.

## Verification

1432 passed, ruff and mypy clean, `renovate-config-validator` clean, Codex review clean. Codex additionally invoked Renovate's own `confirmIfDepUpdated` — the internal check that returned false and failed the branch — against the new replace regions, and it returns `true`.

CI's `read_pin` regex over `constants.py` is unaffected: it reads `SUNXI_TOOLS_REF`, `PYUSB_VERSION` and `BUNDLE_PYTHON_*`, none of which changed shape.

**Still only provable by a real run.** Extraction, resolution and the previously-failing internal check all pass, but the actual write happens on Renovate's next hourly run after this merges. If it fails again the consistency check makes it loud rather than silent, and the fallback is a CI job that computes the digests directly.
