# #304 Prove the standard checks before protecting Dreame main

- Kind: issue, state: closed, by cody, created 2026-11-17T19:42:31-08:00
- Labels: none; milestone: none

Part of SisyphusMD/project-rules#72. Dreame #301 already proved successful shared and native checks. Exercise the six standard checks using a disposable failing draft and verify the native required checks using their existing exact run evidence. Do not merge the draft or publish a tag. Protection waits for independent release workflow proof.

## cody, 2026-11-17T19:50:42-08:00

Draft #305 is proving the expected negative checks on 2040943ff1031e55d16e96c4ba1f2ce8683c1d41. Do not merge or tag it.

## cody, 2026-11-17T19:52:25-08:00

Draft #305 at 2040943ff1031e55d16e96c4ba1f2ce8683c1d41 verified every shared check rejects the intended fixture. Seven exact diagnostics were captured, including the workflow check and the separate AI review status. PR #301 verified the positive contexts. Draft #305 is closed unmerged. Keeping the branch pending cleanup approval.

## cody, 2026-11-17T20:41:05-08:00

Proof complete. PR #303 passed every shared gate at exact head f7d56bccb348ef2952c4ef586f5c365ac50a01a0. Draft #305 recorded each expected rejection at 2040943ff1031e55d16e96c4ba1f2ce8683c1d41 and was closed without merging. The parent adoption issue still tracks the live release dependency and branch protection.
