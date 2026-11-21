# #315 chore(deps): update dependency pyyaml to v6.0.4

- Kind: pull, state: closed, by renovate, created 2026-11-20T16:04:05-08:00
- Labels: none; milestone: none
- refs/pull/315/head -> main, merged: True

This PR contains the following updates:

| Package | Change | [Age](https://docs.renovatebot.com/merge-confidence/) | [Confidence](https://docs.renovatebot.com/merge-confidence/) |
|---|---|---|---|
| [pyyaml](https://pyyaml.org/) ([source](https://github.com/yaml/pyyaml)) | `6.0.3` → `6.0.4` | ![age](https://developer.mend.io/api/mc/badges/age/pypi/pyyaml/6.0.4?slim=true) | ![confidence](https://developer.mend.io/api/mc/badges/confidence/pypi/pyyaml/6.0.3/6.0.4?slim=true) |
| [pyyaml](https://pyyaml.org/) ([source](https://github.com/yaml/pyyaml)) | `==6.0.3` → `==6.0.4` | ![age](https://developer.mend.io/api/mc/badges/age/pypi/pyyaml/6.0.4?slim=true) | ![confidence](https://developer.mend.io/api/mc/badges/confidence/pypi/pyyaml/6.0.3/6.0.4?slim=true) |

---

### Release Notes

<details>
<summary>yaml/pyyaml (pyyaml)</summary>

### [`v6.0.4`](https://github.com/yaml/pyyaml/releases/tag/6.0.4)

[Compare Source](https://github.com/yaml/pyyaml/compare/6.0.3...6.0.4)

#### What's Changed

- Added support for Python 3.15.
- Dropped support for EoL Python 3.8, 3.9.
- Fixed exponential merge key expansion ([#&#8203;937](https://github.com/yaml/pyyaml/issues/937)).
- Exposed libyaml 0.2.6 recursion control API ([#&#8203;1010](https://github.com/yaml/pyyaml/issues/1010)) - no-op when missing.
- Bumped wheel builds to libyaml 0.2.6.

**Full Changelog**: <https://github.com/yaml/pyyaml/compare/6.0.3...6.0.4>

</details>

---

### Configuration

📅 **Schedule**: (UTC)

- Branch creation
  - At any time (no schedule defined)
- Automerge
  - At any time (no schedule defined)

🚦 **Automerge**: Enabled.

♻ **Rebasing**: Whenever PR is behind base branch, or you tick the rebase/retry checkbox.

🔕 **Ignore**: Close this PR and you won't be reminded about these updates again.

---

 - [ ] <!-- rebase-check -->If you want to rebase/retry this PR, check this box

---

Refs #294: standing issue for dependency updates.

No tests: dependency pins and their generated files are checked by the existing build and test jobs.
<!--renovate-debug:eyJjcmVhdGVkSW5WZXIiOiI0NC4yNTcuNSIsInVwZGF0ZWRJblZlciI6IjQ0LjI1Ny41IiwidGFyZ2V0QnJhbmNoIjoibWFpbiIsImxhYmVscyI6WyJwYXRjaC11cGRhdGUiXX0=-->


## forgejo-actions, 2026-11-20T16:04:40-08:00

<!-- ai-review -->
**AI review** of `8b35c81079`: Routine Renovate patch bump of pyyaml 6.0.3→6.0.4 across all workflow pins, pyproject.toml, and uv.lock. No runtime dependency affected; the change is consistent and complete.

No findings.

Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W10= -->
