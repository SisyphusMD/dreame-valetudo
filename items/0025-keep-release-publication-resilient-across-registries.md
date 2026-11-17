# #25 Keep release publication resilient across registries

- Kind: pull, state: closed, by cody, created 2026-07-27T15:58:42-07:00
- Labels: none; milestone: none
- refs/pull/25/head -> main, merged: True

Closes H19, H20, M34, M38, and M39 from the July audit. Publication attempts all registries, repair jobs always run, GitHub tag lookup is exact, Homebrew uses replicated release assets with fail-closed generation, and CI independently verifies every packaged binary.
