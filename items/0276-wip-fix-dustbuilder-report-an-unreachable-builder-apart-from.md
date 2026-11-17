# #276 WIP: fix(dustbuilder): report an unreachable builder apart from a changed form

- Kind: pull, state: closed, by cody, created 2026-11-16T14:21:06-08:00
- Labels: none; milestone: none
- refs/pull/276/head -> main, merged: False

`DustBuilder forms / verify` has failed on every run since 2026-10-28: each weekly scheduled run, and every pull request that touches the forms workflow or its watched files (#275 changes the workflow file, so its path filter matches). The cause is upstream. builder.dontvacuum.me still resolves (139.162.132.106) and the host answers ping, but ports 80 and 443 refuse connections from outside networks too. dontvacuum.me itself is up and still links the builder, and Valetudo's docs still point at it, so it has not moved. Before this change, `verify-forms` reported a page it could not fetch as a failed comparison, the same exit 1 as a changed form.

Now each page is a match, a change, or could not be fetched:
- Any fetched page that differs from its golden still exits 1 and fails the job, even when other pages are unreachable.
- When nothing differed but a page could not be fetched, `verify-forms` exits 3. The workflow turns that into a warning ("DustBuilder could not be reached, so its forms were not checked") and does not advance the verified-on date. The date the tool shows as last verified therefore keeps reflecting the last full match.

The new tests reject:
- an all-fetches-failed run reported as anything but "unreachable";
- a changed form hidden behind an unreachable page;
- the exit-code mapping;
- the stamp step running on anything but a full match.

9 of them fail against main's code.

No sibling change: whiskerless has no DustBuilder.

## cody, 2026-11-16T15:28:32-08:00

Closing: this assumed DustBuilder was only down for a while, but Valetudo has moved to Dustfab. The unreachable-versus-changed split, including the narrowing to curl exits 6/7/28/35, is folded into #278 (commit 3e2955e) against Dustfab.
