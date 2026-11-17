# #168 State the ruff target-version and src roots the sibling states

- Kind: pull, state: closed, by cody, created 2026-08-28T05:33:22-07:00
- Labels: none; milestone: none
- refs/pull/168/head -> main, merged: True

Removes the last cosmetic divergence in the ruff config: target-version and src were inferred here and explicit in whiskerless, and isort had no known-first-party. Every ruff policy key now matches the sibling; the remaining differences are structural (each repo naming its own package, files and directories).
