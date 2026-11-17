# #191 fix(ci): drop the brew image's stale GitHub CLI apt source before installing python3

- Kind: pull, state: closed, by cody, created 2026-09-16T08:26:48-07:00
- Labels: none; milestone: none
- refs/pull/191/head -> main, merged: True

The build leg has failed on every PR since 2026-09-12 at `apt-get update` inside the Homebrew smoke stage:

```
W: GPG error: https://cli.github.com/packages stable InRelease: NO_PUBKEY 5612B36462313325
E: The repository 'https://cli.github.com/packages stable InRelease' is not signed.
```

The pinned `homebrew/brew` digest is still Docker Hub's latest, so there is nothing to bump to. The image bakes in GitHub's CLI apt source and its keyring at image build time; GitHub rotated the key. The smoke never uses `gh`, so the source is removed before the update rather than re-keyed. Whiskerless's smoke never runs apt in that image, so there is nothing to mirror.
