# #58 chore(deps): update dependency ruff to v0.16.1

- Kind: pull, state: closed, by renovate, created 2026-07-30T13:03:08-07:00
- Labels: none; milestone: none
- refs/pull/58/head -> main, merged: True

This PR contains the following updates:

| Package | Change | [Age](https://docs.renovatebot.com/merge-confidence/) | [Confidence](https://docs.renovatebot.com/merge-confidence/) |
|---|---|---|---|
| [ruff](https://docs.astral.sh/ruff) ([source](https://github.com/astral-sh/ruff), [changelog](https://github.com/astral-sh/ruff/blob/main/CHANGELOG.md)) | `0.16.0` → `0.16.1` | ![age](https://developer.mend.io/api/mc/badges/age/pypi/ruff/0.16.1?slim=true) | ![confidence](https://developer.mend.io/api/mc/badges/confidence/pypi/ruff/0.16.0/0.16.1?slim=true) |

---

### Release Notes

<details>
<summary>astral-sh/ruff (ruff)</summary>

### [`v0.16.1`](https://github.com/astral-sh/ruff/blob/HEAD/CHANGELOG.md#0161)

[Compare Source](https://github.com/astral-sh/ruff/compare/0.16.0...0.16.1)

Released on 2026-07-30.

##### Preview features

- Add an option to opt out of human-readable names ([#&#8203;27160](https://github.com/astral-sh/ruff/pull/27160))
- \[`flake8-pytest-style`] Make fixes safe by default and unsafe only when comments are present (`PT018`) ([#&#8203;27201](https://github.com/astral-sh/ruff/pull/27201))
- \[`pyupgrade`] Skip fix when a defaulted `TypeVar` precedes a non-defaulted one (`UP040`, `UP046`, `UP047`) ([#&#8203;27133](https://github.com/astral-sh/ruff/pull/27133))
- \[`ruff`] Fix false positive with unpacked arguments (`RUF065`) ([#&#8203;26959](https://github.com/astral-sh/ruff/pull/26959))

##### Bug fixes

- Bump `gen-lsp-types` to gracefully handle unknown enumeration values in LSP messages ([#&#8203;27230](https://github.com/astral-sh/ruff/pull/27230))
- \[`flake8-bugbear`] Mark `range` as immutable (`B008`) ([#&#8203;27247](https://github.com/astral-sh/ruff/pull/27247))
- \[`flake8-comprehensions`] NFKC-normalize keyword names in `C408` fix ([#&#8203;26813](https://github.com/astral-sh/ruff/pull/26813))
- \[`flake8-return`] Fix false positive when variable is read in `finally` clause (`RET504`) ([#&#8203;25441](https://github.com/astral-sh/ruff/pull/25441))
- \[`pydocstyle`] Skip section detection inside RST directive bodies (`D214`, `D405`, `D413`) ([#&#8203;23635](https://github.com/astral-sh/ruff/pull/23635))
- \[`refurb`] Parenthesize `yield` arguments in the `FURB192` fix ([#&#8203;27192](https://github.com/astral-sh/ruff/pull/27192))

##### Rule changes

- \[`flake8-pytest-style`] Mark `PT022` fixes as unsafe ([#&#8203;26440](https://github.com/astral-sh/ruff/pull/26440))
- \[`refurb`] Mark fixes that remove unknown separators as unsafe (`FURB105`) ([#&#8203;27200](https://github.com/astral-sh/ruff/pull/27200))

##### Server

- Fix indexing of excluded nested Ruff workspaces ([#&#8203;27303](https://github.com/astral-sh/ruff/pull/27303))
- Lint TOML files in the LSP ([#&#8203;26862](https://github.com/astral-sh/ruff/pull/26862))

##### Documentation

- Cover `pycon` Markdown formatting ([#&#8203;27153](https://github.com/astral-sh/ruff/pull/27153))
- \[`flake8-bandit`] Document `TYPE_CHECKING` exception (`S101`) ([#&#8203;27004](https://github.com/astral-sh/ruff/pull/27004))
- \[`flake8-import-conventions`] Document that `extend-aliases` can override default aliases ([#&#8203;27191](https://github.com/astral-sh/ruff/pull/27191))
- \[`pylint`] Add missing fix safety gotchas for `non-augmented-assignment` (`PLR6104`) ([#&#8203;27250](https://github.com/astral-sh/ruff/pull/27250))

##### Other changes

- Reduce syntax error noise by swallowing dedents like indents ([#&#8203;27170](https://github.com/astral-sh/ruff/pull/27170))
- Vendor latest annotate-snippets ([#&#8203;27033](https://github.com/astral-sh/ruff/pull/27033))

##### Contributors

- [@&#8203;bxff](https://github.com/bxff)
- [@&#8203;anishgirianish](https://github.com/anishgirianish)
- [@&#8203;Avasam](https://github.com/Avasam)
- [@&#8203;epage](https://github.com/epage)
- [@&#8203;LHMQ878](https://github.com/LHMQ878)
- [@&#8203;MichaReiser](https://github.com/MichaReiser)
- [@&#8203;ntBre](https://github.com/ntBre)
- [@&#8203;HarshalPatel1972](https://github.com/HarshalPatel1972)
- [@&#8203;mjpieters](https://github.com/mjpieters)
- [@&#8203;joshuavetos](https://github.com/joshuavetos)
- [@&#8203;jesco-absolute](https://github.com/jesco-absolut)
- [@&#8203;vidigoat](https://github.com/vidigoat)
- [@&#8203;baltasarblanco](https://github.com/baltasarblanco)
- [@&#8203;ribru17](https://github.com/ribru17)
- [@&#8203;oh-summy](https://github.com/oh-summy)
- [@&#8203;Jayashanker-Padishala](https://github.com/Jayashanker-Padishala)

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

🔕 **Ignore**: Close this PR and you won't be reminded about this update again.

---

 - [ ] <!-- rebase-check -->If you want to rebase/retry this PR, check this box

---

This PR has been generated by [Mend Renovate](https://github.com/renovatebot/renovate).
<!--renovate-debug:eyJjcmVhdGVkSW5WZXIiOiI0My4yODguMCIsInVwZGF0ZWRJblZlciI6IjQzLjI4OC4wIiwidGFyZ2V0QnJhbmNoIjoibWFpbiIsImxhYmVscyI6WyJwYXRjaC11cGRhdGUiXX0=-->
