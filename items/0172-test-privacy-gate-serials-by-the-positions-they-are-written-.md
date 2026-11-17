# #172 test(privacy): gate serials by the positions they are written in

- Kind: pull, state: closed, by cody, created 2026-08-29T11:24:24-07:00
- Labels: none; milestone: none
- refs/pull/172/head -> main, merged: True

Converges the sibling's privacy guard. A Dreame serial has no recognisable prefix, so a value-shaped regex would match ordinary prose; this walks the AST instead and judges a string by the position it is written in — named and annotated assignments, dict values, call and keyword arguments, simple and destructured `for` bindings, transcript keys.

The loop-binding case is the one that matters: the real serial that reached PyPI was bound by `for serial in (...)`, which a value-shaped sweep missed.

Unparseable files fail loudly rather than passing silently.
