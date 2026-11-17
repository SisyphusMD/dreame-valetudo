# #27 Reconcile only divergent release assets

- Kind: pull, state: closed, by cody, created 2026-07-27T16:46:44-07:00
- Labels: none; milestone: none
- refs/pull/27/head -> main, merged: True

Closes M37 and M40 from the July audit. Reconcile compares all registry metadata before transfer, skips consistent assets and tags, preserves source priority with fallback, and explicitly reports failed repairs. Integration coverage includes every matching, missing, truncated, unknown-size, fallback, and failure branch.
