# #138 Build the Homebrew formula on macOS before tagging

- Kind: pull, state: closed, by cody, created 2026-08-24T15:13:50-07:00
- Labels: none; milestone: none
- refs/pull/138/head -> main, merged: True

Closes a divergence that was never recorded: whiskerless has `.github/workflows/formula-macos.yml` and dreame had no counterpart.

My first attempt was to record this as *intended*, on the grounds that dreame declares `dependencies = []` and so has no native code to mis-link. That was wrong, and a cold review caught it: the **formula** compiles C regardless of what pyproject says. `dreame-valetudo.rb` stages the `sunxi-tools` resource and builds it with `make sunxi-fel` against libfdt and zlib, and depends on `libusb`, `dtc`, `zlib` and `pkg-config`. A macOS-only compile or link failure was reachable there and nowhere else before a tag existed.

Also wires it into the two places a new workflow has to be registered: the `retry-infra-failures` list, and both release gates, which now require the formula check and the CI matrix rather than the matrix alone.

**Known limit, stated in the workflow:** one `macos-26` lane, so an Intel-only or macOS-floor-only native break still reaches a tag. Whiskerless's job has the same single lane; widening both to the full matrix triples the most expensive runners on every push and is a cost decision, not a fix to slip in here.
