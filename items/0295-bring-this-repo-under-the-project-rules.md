# #295 Bring this repo under the project rules

- Kind: issue, state: closed, by cody, created 2026-11-17T13:34:29-08:00
- Labels: status:in-progress, type:chore; milestone: none

Tracked in SisyphusMD/project-rules#72. Add the canonical gates, issue archive, issue templates, security policy and GitHub mirror lockdown. Move the repository workflow to tests.yml and preserve its existing tests and trust gates. Prove required checks before protecting main. Branch protection depends on SisyphusMD/project-rules#86.

## cody, 2026-11-17T13:35:42-08:00

Ownership: this rollout session is implementing the adoption in ci/adopt-project-rules. The workflow moves to tests.yml with its existing CI display name because main already requires five CI / … (pull_request) contexts. The canonical gates publish distinct job names under pull_request_target.
