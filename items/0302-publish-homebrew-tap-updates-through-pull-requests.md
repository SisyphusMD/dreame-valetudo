# #302 Publish Homebrew Tap updates through pull requests

- Kind: issue, state: closed, by cody, created 2026-11-17T18:50:18-08:00
- Labels: status:in-progress, type:chore; milestone: none

Part of SisyphusMD/homebrew-tap#3. Isolate formula rendering from the tap write credential, submit the formula and bottles as normal pull requests, and wait until the intended files land before downstream install verification or pruning. Keep the existing manual bottle repair path.

Depends on SisyphusMD/homebrew-tap#7.

## cody, 2026-11-17T20:11:16-08:00

PR #303 settled at 47b1bc891097e70cc6e7d55ca9d3e87b2a20ea85. All exact-head native and shared checks passed and both mirrors match. The first real Tap publisher proof is tracked under homebrew-tap #3 and requires the separately approved v0.3.0-rc.52.
