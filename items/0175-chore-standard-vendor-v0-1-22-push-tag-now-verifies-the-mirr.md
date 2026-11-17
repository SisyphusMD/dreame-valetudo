# #175 chore(standard): vendor v0.1.22 (push-tag now verifies the mirror instead of assuming it synced)

- Kind: pull, state: closed, by cody, created 2026-08-29T12:02:39-07:00
- Labels: none; milestone: none
- refs/pull/175/head -> main, merged: True

Same vendor as the sibling. `push_mirrors-sync` returns 403 for a repo-scoped token, so the sync call has always been best-effort and the tag push assumed it worked — which is what failed rc.41 here.

v0.1.22 polls the mirror for the tag after requesting the sync and reports present, absent and indeterminate distinctly, warning rather than blocking when the mirror never catches up.
