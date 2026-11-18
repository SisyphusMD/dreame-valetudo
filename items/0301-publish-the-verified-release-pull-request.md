# #301 Publish the verified release pull request

- Kind: pull, state: closed, by cody, created 2026-11-17T18:32:17-08:00
- Labels: none; milestone: none
- refs/pull/301/head -> main, merged: True

Closes #297.

Keeps existing qualification and prerelease workflows. Prepare Release opens a normal issue-linked PR and records the exact prepared commit. Publish Release verifies the merged tree and successful preparation for that head, checks the version stamps and latest candidate, and pushes only its annotated tag.

Vendors project-rules v0.3.1. Local validation: 2,478 tests and 99.04% branch coverage, the safety-module coverage gate, workflow and drift checks, plus a bare-origin fixture proving branch-only preparation, retries, and rejection of an unqualified PR head.

Large diff: The qualification workflow, tag-only publisher, verified upstream helpers, and their tests must change together to keep a working release path in this commit.

## forgejo-actions, 2026-11-17T18:41:42-08:00

<!-- ai-review -->
**AI review** of `94534b1fba`: The two-step release (Prepare opens a PR, Publish tags the merged commit) is well-structured with proper qualification verification, idempotent retries, and credential confinement. The three earlier findings are resolved by the author's evidence and the vendored-code testing pattern.

### Withdrawn

- **F1** (blocking, `packaging/verify-release-pr.py:30`) head["label"] is `owner/repo:branch`, not `branch` — publish always fails: 
  - Reviewer: Withdrawn. You demonstrated with live releases (Appliance #17, project-rules #98, and this PR) that your Forgejo primary returns a bare branch name in head.label, not the owner/repo:branch form. The code is correct for the instance it runs on, and the head.ref fallback for deleted branches is the right retry path.
- **F2** (blocking, `packaging/verify-release-pr.py:1`) No tests for the tag-authorization gate: 
  - Reviewer: Withdrawn. The file is vendored from project-rules v0.3.1 (pinned in STANDARD.lock by hash), and its tests live in the upstream repo (tests/test_verify_release_pr.py) covering the cases I listed. This matches the CONTRIBUTING.md pattern: 'Change those files in project-rules and re-vendor them here.' Your local validation (2,478 tests, 99.04% branch coverage, bare-origin fixture) confirms the integration works.
- **F3** (suggestion, `.forgejo/workflows/release.yml:350`) Existing-PR idempotency check compares head.label to bare branch name: 
  - Reviewer: Withdrawn. Same root cause as F1; your Forgejo returns bare head.label, so the comparison is correct on the instance that runs it.


Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W3siaWQiOiAiRjEiLCAiZmlsZSI6ICJwYWNrYWdpbmcvdmVyaWZ5LXJlbGVhc2UtcHIucHkiLCAibGluZSI6IDMwLCAic2V2ZXJpdHkiOiAiYmxvY2tpbmciLCAic3RhdHVzIjogIndpdGhkcmF3biIsICJ0aXRsZSI6ICJoZWFkW1wibGFiZWxcIl0gaXMgYG93bmVyL3JlcG86YnJhbmNoYCwgbm90IGBicmFuY2hgIFx1MjAxNCBwdWJsaXNoIGFsd2F5cyBmYWlscyIsICJkZXRhaWwiOiAiIiwgInJlc3BvbnNlIjogIldpdGhkcmF3bi4gWW91IGRlbW9uc3RyYXRlZCB3aXRoIGxpdmUgcmVsZWFzZXMgKEFwcGxpYW5jZSAjMTcsIHByb2plY3QtcnVsZXMgIzk4LCBhbmQgdGhpcyBQUikgdGhhdCB5b3VyIEZvcmdlam8gcHJpbWFyeSByZXR1cm5zIGEgYmFyZSBicmFuY2ggbmFtZSBpbiBoZWFkLmxhYmVsLCBub3QgdGhlIG93bmVyL3JlcG86YnJhbmNoIGZvcm0uIFRoZSBjb2RlIGlzIGNvcnJlY3QgZm9yIHRoZSBpbnN0YW5jZSBpdCBydW5zIG9uLCBhbmQgdGhlIGhlYWQucmVmIGZhbGxiYWNrIGZvciBkZWxldGVkIGJyYW5jaGVzIGlzIHRoZSByaWdodCByZXRyeSBwYXRoLiJ9LCB7ImlkIjogIkYyIiwgImZpbGUiOiAicGFja2FnaW5nL3ZlcmlmeS1yZWxlYXNlLXByLnB5IiwgImxpbmUiOiAxLCAic2V2ZXJpdHkiOiAiYmxvY2tpbmciLCAic3RhdHVzIjogIndpdGhkcmF3biIsICJ0aXRsZSI6ICJObyB0ZXN0cyBmb3IgdGhlIHRhZy1hdXRob3JpemF0aW9uIGdhdGUiLCAiZGV0YWlsIjogIiIsICJyZXNwb25zZSI6ICJXaXRoZHJhd24uIFRoZSBmaWxlIGlzIHZlbmRvcmVkIGZyb20gcHJvamVjdC1ydWxlcyB2MC4zLjEgKHBpbm5lZCBpbiBTVEFOREFSRC5sb2NrIGJ5IGhhc2gpLCBhbmQgaXRzIHRlc3RzIGxpdmUgaW4gdGhlIHVwc3RyZWFtIHJlcG8gKHRlc3RzL3Rlc3RfdmVyaWZ5X3JlbGVhc2VfcHIucHkpIGNvdmVyaW5nIHRoZSBjYXNlcyBJIGxpc3RlZC4gVGhpcyBtYXRjaGVzIHRoZSBDT05UUklCVVRJTkcubWQgcGF0dGVybjogJ0NoYW5nZSB0aG9zZSBmaWxlcyBpbiBwcm9qZWN0LXJ1bGVzIGFuZCByZS12ZW5kb3IgdGhlbSBoZXJlLicgWW91ciBsb2NhbCB2YWxpZGF0aW9uICgyLDQ3OCB0ZXN0cywgOTkuMDQlIGJyYW5jaCBjb3ZlcmFnZSwgYmFyZS1vcmlnaW4gZml4dHVyZSkgY29uZmlybXMgdGhlIGludGVncmF0aW9uIHdvcmtzLiJ9LCB7ImlkIjogIkYzIiwgImZpbGUiOiAiLmZvcmdlam8vd29ya2Zsb3dzL3JlbGVhc2UueW1sIiwgImxpbmUiOiAzNTAsICJzZXZlcml0eSI6ICJzdWdnZXN0aW9uIiwgInN0YXR1cyI6ICJ3aXRoZHJhd24iLCAidGl0bGUiOiAiRXhpc3RpbmctUFIgaWRlbXBvdGVuY3kgY2hlY2sgY29tcGFyZXMgaGVhZC5sYWJlbCB0byBiYXJlIGJyYW5jaCBuYW1lIiwgImRldGFpbCI6ICIiLCAicmVzcG9uc2UiOiAiV2l0aGRyYXduLiBTYW1lIHJvb3QgY2F1c2UgYXMgRjE7IHlvdXIgRm9yZ2VqbyByZXR1cm5zIGJhcmUgaGVhZC5sYWJlbCwgc28gdGhlIGNvbXBhcmlzb24gaXMgY29ycmVjdCBvbiB0aGUgaW5zdGFuY2UgdGhhdCBydW5zIGl0LiJ9XQ== -->

## cody, 2026-11-17T18:43:29-08:00

F1/F3: I checked this Forgejo primary and the matching live release. Appliance #17 was tagged successfully via this byte-identical verifier. Its head.label still reads release/v0.1.1, with head.ref now refs/pull/17/head after pruning. Project-rules #98 returns the same shape (label release/v0.3.1, ref refs/pull/98/head). This PR also returns a bare label and ref while its branch is open. Switching to head.ref after deletion would break proven release retries.

F2: These files are byte-identical to the official project-rules v0.3.1 lock. The tests for that exact helper live in project-rules tests/test_verify_release_pr.py, including wrong PR metadata, differing tree or changelog, non-primary history, version ordering, annotated-tag replay, and tag-only push against a bare Git origin. Dreame's modified workflow tests enforce the new contract. I also ran this exact Prepare and Publish verification against a local bare origin, including retry, branch-only push, and refusal when the final head lacks successful preparation. The package and its tests passed on this branch (2,478 tests and 99.04% branch coverage).
