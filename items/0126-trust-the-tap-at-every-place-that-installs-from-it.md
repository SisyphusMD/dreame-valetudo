# #126 Trust the tap at every place that installs from it

- Kind: pull, state: closed, by cody, created 2026-08-23T10:33:42-07:00
- Labels: none; milestone: none
- refs/pull/126/head -> main, merged: True

build-bottles.sh was one of four call sites. The install matrix and the linuxbrew smoke image run their own `brew install` from the tap and hit the same refusal — rc.19 built its bottles fine and then failed every install-matrix leg on it.

An invariant now fails if anything installs from the tap without trusting it first, mutation-verified against all three call sites in both repos. It matches `$TAP` as well as the literal name, because the shared script spells it through a variable, and is scoped to files where brew actually runs — the upgrade hint in update_check.py is a string the user runs, not an install.
