# #189 feat(ci): retry a run the runner's resolver failed inside one of our steps

- Kind: pull, state: closed, by cody, created 2026-09-16T07:51:55-07:00
- Labels: none; milestone: none
- refs/pull/189/head -> main, merged: True

Mirror of the whiskerless change. Whiskerless GitHub run 35055059591 stayed red when `brew test` could not resolve rubygems.org on the runner, because the failure landed in our named step and the step-name rule refused the retry.

Vendors project-standard v0.1.25, whose discriminator now reads each failed job's log and retries only when the failed step's own closing lines carry an exact resolver-failure spelling and name a host CI is known to reach. The workflow downloads one log per failed job (`gh api --allow-escape-sequences`, without which `gh` refuses ANSI-coloured bodies) and passes the directory through. The invariant test pins the fetch and the flag.
