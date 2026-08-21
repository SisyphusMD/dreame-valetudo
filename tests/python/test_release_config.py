"""Release workflow contracts that are otherwise first exercised only after tagging."""

from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PUBLISH = _ROOT / ".forgejo" / "workflows" / "publish.yml"
_CI = _ROOT / ".forgejo" / "workflows" / "ci.yml"
_RELEASE = _ROOT / ".forgejo" / "workflows" / "release.yml"
_PRERELEASE = _ROOT / ".forgejo" / "workflows" / "prerelease.yml"
_MACOS = _ROOT / ".github" / "workflows" / "release-macos.yml"
_MACOS_CI = _ROOT / ".github" / "workflows" / "ci-macos.yml"
_MACOS_WAIT = _ROOT / "packaging" / "wait-github-macos-ci.sh"
# No datasource tracks these, so nothing bumps them and nothing has to refresh them: the stage1
# tarball has no upstream release feed, and the Dust keystream is a constant of the format.
_STATIC_DIGESTS = ("STAGE1_SHA256", "DUST_KEYSTREAM_SHA256")
_LINUX_PACKAGES = _ROOT / "packaging" / "test-linux-packages.sh"
_SHELLCHECK_ALL = _ROOT / "packaging" / "shellcheck-all.sh"
_README = _ROOT / "README.md"


def _job(text: str, name: str) -> str:
    start = text.index(f"  {name}:\n")
    following = re.search(r"\n  [a-zA-Z0-9_-]+:\n", text[start + 3 :])
    return text[start:] if following is None else text[start : start + 3 + following.start()]


def test_publish_attempts_every_registry_and_always_runs_repair_jobs() -> None:
    text = _PUBLISH.read_text()
    releases = _job(text, "releases")
    step = releases[releases.index("      - name: Create the three releases") :]

    assert "fail=0" in step
    assert step.count("|| fail=1") == 3
    assert 'exit "$fail"' in step
    cluster = step.index("packaging/forgejo-release.sh forgejo.bryantserver.com")
    github = step.index("packaging/github-release.sh", cluster)
    nas = step.index("packaging/forgejo-release.sh forgejo.nas.bryantserver.com")
    assert cluster < github < nas

    for name in ("homebrew-tap", "reconcile"):
        assert "\n    if: ${{ always() }}\n" in _job(text, name)


def test_reconcile_requires_both_github_qualified_macos_packages() -> None:
    reconcile = _job(_PUBLISH.read_text(), "reconcile")

    assert "fail=0" in reconcile
    assert 'index("dreame-valetudo-macos-arm64.pkg")' in reconcile
    assert 'index("dreame-valetudo-macos-x86_64.pkg")' in reconcile
    assert 'endswith(".pkg")' not in reconcile
    assert "current tag's two GitHub-qualified .pkgs were not published" in reconcile
    assert "fail=1" in reconcile
    assert "bash packaging/reconcile-releases.sh || fail=1" in reconcile
    assert 'exit "$fail"' in reconcile
    assert "::warning::current tag's .pkgs" not in reconcile


def test_ci_checks_each_required_deb_binary_independently() -> None:
    text = _CI.read_text()
    for path in (
        "./usr/bin/dreame-valetudo",
        "./usr/lib/dreame-valetudo/dreame-fastboot",
        "./usr/lib/dreame-valetudo/sunxi-fel",
        "./usr/lib/dreame-valetudo/app/dreame-valetudo",
        "./usr/lib/dreame-valetudo/fastboot/dreame-fastboot",
    ):
        assert path in text
    assert "for required in" in text
    assert 'grep -Fq "$required"' in text
    # Both entry points must arrive as links INTO the bundles: a copy would run without the
    # contents directory beside it.
    for link in (
        "./usr/bin/dreame-valetudo -> /usr/lib/dreame-valetudo/app/dreame-valetudo",
        ("./usr/lib/dreame-valetudo/dreame-fastboot"
         " -> /usr/lib/dreame-valetudo/fastboot/dreame-fastboot"),
    ):
        assert link in text
    assert 'grep -Fq "$link"' in text


def test_linux_freezes_onedir_while_the_shared_build_scripts_default_to_onefile() -> None:
    # Both scripts are also called by release-macos.yml, which signs, bundles and notarizes a
    # single file. The mode therefore has to be a parameter the Linux image passes, never a new
    # default — and ordinary macOS CI does not assemble the .pkg, so nothing else would notice.
    dockerfile = (_ROOT / "packaging" / "deb.Dockerfile").read_text()
    macos = _MACOS.read_text()

    for script in ("build-bundle.sh", "build-fastboot-client.sh"):
        text = (_ROOT / "packaging" / script).read_text()
        assert 'MODE="${BUNDLE_MODE:-onefile}"' in text
        # The tool identifies an installed bundle by this directory name, so the build pins it
        # rather than inheriting whatever PyInstaller currently defaults to.
        assert "--contents-directory _internal" in text
        assert f"BUNDLE_MODE=onedir bash packaging/{script}" in dockerfile

    assert "BUNDLE_MODE" not in macos
    bench = (_ROOT / "src" / "dreame_valetudo" / "bench.py").read_text()
    assert '_BUNDLE_CONTENTS_DIR = "_internal"' in bench


def test_packages_install_bundle_trees_reachable_through_symlinks() -> None:
    nfpm = (_ROOT / "packaging" / "nfpm.yaml").read_text()
    contents = nfpm.split("contents:\n", 1)[1].split("\nscripts:", 1)[0]

    for entry in (
        "  - src: ./dist/dreame-valetudo",
        "    dst: /usr/lib/dreame-valetudo/app\n    type: tree",
        ("  - src: /usr/lib/dreame-valetudo/app/dreame-valetudo\n"
         "    dst: /usr/bin/dreame-valetudo\n    type: symlink"),
        "    dst: /usr/lib/dreame-valetudo/fastboot\n    type: tree",
        ("  - src: /usr/lib/dreame-valetudo/fastboot/dreame-fastboot\n"
         "    dst: /usr/lib/dreame-valetudo/dreame-fastboot\n    type: symlink"),
    ):
        assert entry in contents
    # An explicit mode on a tree is applied to every member of it, which would hand the whole
    # bundle whatever bit the launcher needs.
    assert "type: tree\n    file_info" not in contents


def test_the_package_matrix_upgrades_from_the_pre_onedir_layout() -> None:
    # Every other case upgrades one current-layout package to another, so nothing else exercises
    # the transition real users take: two regular files become symlinks, two directories appear.
    smoke = _LINUX_PACKAGES.read_text()
    workflow = _CI.read_text()
    legacy = (_ROOT / "packaging" / "nfpm-legacy-layout.yaml").read_text()

    assert "Debian 13 (upgrade from the pre-onedir layout)" in smoke
    assert "Fedora 44 (upgrade from the pre-onedir layout)" in smoke
    assert "<legacy.deb> <legacy.rpm>" in smoke
    assert "nfpm-legacy-layout.yaml -t /w/ci-legacy.deb" in workflow
    assert "nfpm-legacy-layout.yaml -t /w/ci-legacy.rpm" in workflow
    assert "ci-legacy.deb ci-legacy.rpm" in workflow
    for entry in ("type: tree", "type: symlink"):
        assert entry not in legacy


def test_every_release_deb_is_compared_against_the_tree_that_was_built() -> None:
    # nfpm runs outside the build image. Without this, a package that dropped bundled data would
    # still install, still report its version and still pass the host smoke.
    for workflow in (_CI, _PUBLISH):
        text = workflow.read_text()
        assert "dpkg-deb -x" in text
        assert text.count("packaging/check-package-parity.py") == 2
        assert "/usr/lib/dreame-valetudo/app" in text
        assert "/usr/lib/dreame-valetudo/fastboot" in text
    # A recursive copy that dereferenced the bundles' symlinks would package something the build
    # never produced, and the parity check is what would report it.
    assert "cp -a out/dreame-valetudo out/dreame-fastboot dist/" in _CI.read_text()
    assert 'cp -a "out-$arch/dreame-valetudo" "out-$arch/dreame-fastboot" dist/' in _PUBLISH.read_text()


def test_pyinstaller_floats_again_on_both_forgejo_workflows() -> None:
    # The hold existed only because a onefile child could not start under the emulated arm64 leg.
    # Onedir has no child process, so the constraint is gone at its root rather than waived.
    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    for rule in config["packageRules"]:
        assert "pyinstaller" not in rule.get("matchDepNames", [])
    pins = {}
    for workflow in (_CI, _PUBLISH, _MACOS):
        text = workflow.read_text()
        assert "Held at 6.22.0" not in text
        found = re.search(
            r"# renovate: datasource=pypi depName=pyinstaller\s*\n[^\n]*?(\d+\.\d+(?:\.\d+)?)",
            text,
        )
        assert found is not None, workflow.name
        pins[workflow.name] = found.group(1)
    # One depName across three files is one grouped Renovate PR. The clamp scoped the Forgejo pair
    # away from the macOS one, which is exactly how they could drift apart unnoticed.
    assert len(set(pins.values())) == 1, pins


def test_forgejo_buildkit_uses_the_nas_pull_through_cache() -> None:
    mirror = 'mirrors = ["dockerhub-mirror.nas.bryantserver.com"]'
    for workflow in (_CI, _PUBLISH):
        text = workflow.read_text()
        assert "buildkitd-config-inline: |" in text
        assert mirror in text

    # Shared runner garbage collection owns residue from interrupted jobs. A repository job may
    # remove artifacts it created, but must not evict unrelated repositories' tagged image cache.
    assert "docker image prune --all" not in _CI.read_text()


def test_forgejo_requires_native_macos_suites_for_the_exact_mirrored_commit() -> None:
    forgejo = _job(_CI.read_text(), "macos")
    macos = _MACOS_CI.read_text()

    assert "needs: [shellcheck, python, python-floor, build, integration]" in forgejo
    assert "packaging/wait-github-macos-ci.sh" in forgejo
    assert 'github.event.pull_request.head.repo.full_name == github.repository' in forgejo
    assert 'os.environ["GITHUB_EVENT_PATH"]' in forgejo
    assert '["pull_request"]["head"]["sha"]' in forgejo
    assert "secrets." not in forgejo
    assert "macos-15\n" in macos
    assert "macos-15-intel" in macos
    assert "macos-26\n" in macos
    assert "macos-26-intel" in macos
    assert "ruff check src/dreame_valetudo libexec tests/python" in macos
    assert "mypy" in macos
    assert "pytest -q tests/python" in macos
    assert "tests/release/*.sh" in macos
    assert "permissions:" not in macos
    assert "persist-credentials: false" in macos

    release = _MACOS.read_text()
    assert 'MACOSX_DEPLOYMENT_TARGET: "15.0"' in release
    assert "runs-on: ${{ matrix.os }}" in release
    assert "needs: [build, current]" in release
    assert release.count("bash packaging/test-macos-package.sh") == 2
    assert release.index("- os: macos-15") < release.index("  current:")
    assert release.index("- os: macos-26", release.index("  current:")) > release.index("  current:")


def test_native_macos_ci_uses_the_pinned_linux_test_toolchain() -> None:
    linux = _CI.read_text()
    macos = _MACOS_CI.read_text()
    for name in ("RUFF", "MYPY", "PYTEST"):
        pin = re.search(rf'{name}="([^"]+)"', linux)
        assert pin is not None
        assert f'{name}="{pin.group(1)}"' in macos


def test_claimed_python_floor_is_installed_and_fully_tested() -> None:
    project = (_ROOT / "pyproject.toml").read_text()
    floor_job = _job(_CI.read_text(), "python-floor")

    assert 'requires-python = ">=3.11"' in project
    assert 'python-version: "3.11.0"' in floor_job
    assert 'pip install "pytest==$PYTEST" -e .' in floor_job
    assert "pytest -q tests/python" in floor_job

    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    floor_rules = [
        rule
        for rule in config["packageRules"]
        if rule.get("matchManagers") == ["github-actions"]
        and rule.get("matchDepNames") == ["python"]
        and rule.get("matchFileNames") == [".forgejo/workflows/ci.yml"]
    ]
    assert len(floor_rules) == 1
    assert floor_rules[0]["matchCurrentValue"] == r"/^3\.11\.0$/"
    assert floor_rules[0]["allowedVersions"] == r"/^3\.11\.0$/"


def test_python_version_bumps_wait_for_the_setup_python_manifest() -> None:
    workflows = sorted((_ROOT / ".forgejo" / "workflows").glob("*.yml"))
    workflows += sorted((_ROOT / ".github" / "workflows").glob("*.yml"))
    pinned = [w for w in workflows if re.search(r'python-version: "(?!3\.11\.0")', w.read_text())]
    assert len(pinned) >= 3

    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    delayed = [
        rule
        for rule in config["packageRules"]
        if rule.get("matchManagers") == ["github-actions"]
        and rule.get("matchDepNames") == ["python"]
        and "minimumReleaseAge" in rule
    ]
    assert len(delayed) == 1
    # Unscoped by file, so it covers every workflow above rather than whichever one broke last.
    assert "matchFileNames" not in delayed[0]
    # Days, not hours: actions/python-versions merges the manifest setup-python reads well over an
    # hour after cutting the release Renovate watches, and nothing bounds that lag to one hour.
    assert re.fullmatch(r"[1-9]\d* days?", delayed[0]["minimumReleaseAge"])


def test_pinned_toolchain_matches_the_lockfile() -> None:
    ci = _CI.read_text()
    lock = (_ROOT / "uv.lock").read_text()
    project = (_ROOT / "pyproject.toml").read_text()
    for package, var in (("ruff", "RUFF"), ("mypy", "MYPY"), ("pytest", "PYTEST")):
        pin = re.search(rf'{var}="([^"]+)"', ci)
        locked = re.search(rf'name = "{package}"\nversion = "([^"]+)"', lock)
        declared = re.search(rf'"{package}==([^"]+)"', project)
        assert pin is not None and locked is not None, package
        # CI installs the literal; `uv run` resolves the lock. Contributors lint with whatever
        # these disagree on, and no other check compares them.
        assert pin.group(1) == locked.group(1), (
            f"{package}: CI installs {pin.group(1)}, uv.lock resolves {locked.group(1)}"
        )
        # An `==` pin, not a floor: a floor is permanently satisfied, so pep621 raises nothing and
        # the lock never follows the literal Renovate does move. This is what makes one PR able to
        # carry all three, so it is pinned here rather than left to the config comment.
        assert declared is not None, f"{package} must be pinned exactly in pyproject.toml"
        assert declared.group(1) == locked.group(1), (
            f"{package}: pyproject pins {declared.group(1)}, uv.lock resolves {locked.group(1)}"
        )


def test_no_dependency_is_held_back_for_hand_updating() -> None:
    """Nothing may require a person to edit a file to make its bump mergeable.

    Every hold here existed because something bound to the version — a digest, a lockfile — had no
    datasource and could not move with it, so the branch arrived half-applied. Refreshing those
    from the version removes the reason, and a hold with no reason is just a chore.
    """
    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    held = [
        rule.get("matchDepNames") or rule.get("matchManagers") or rule.get("description", "?")
        for rule in config["packageRules"]
        if rule.get("automerge") is False
    ]
    assert held == [], f"held for hand review: {held}"

    blanket = [
        rule
        for rule in config["packageRules"]
        if rule.get("automerge") is True and "patch" in rule.get("matchUpdateTypes", [])
    ]
    assert len(blanket) == 1, "nothing automerges a green patch bump any more"


def test_every_version_bound_digest_is_refreshed_automatically() -> None:
    """A digest pinned beside a version must be recomputed from it, or the next bump strands it.

    This is the anti-rot half: adding a new digest pin without teaching the refresher about it
    would quietly reintroduce the hand-editing this removed.
    """
    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    task = config["postUpgradeTasks"]
    assert any("refresh-pins.sh" in command for command in task["commands"])
    # Per branch, not per update: several deps can land in one branch and the refresher reads
    # whatever versions the branch ended up with.
    assert task["executionMode"] == "branch"
    assert "src/dreame_valetudo/constants.py" in task["fileFilters"]
    assert any(f.startswith("packaging/homebrew/") for f in task["fileFilters"])

    refresher = (_ROOT / "packaging" / "refresh-pins.sh").read_text()
    constants = (_ROOT / "src" / "dreame_valetudo" / "constants.py").read_text()
    for name in sorted(set(re.findall(r"^(\w*SHA256)\b", constants, re.M))):
        if name in _STATIC_DIGESTS:
            continue
        assert name in refresher, (
            f"{name} moves with a version but nothing refreshes it, so its next bump lands "
            "half-applied and waits for a human"
        )


def test_the_cpython_pin_is_proposed_without_its_tag_prefix() -> None:
    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    rules = [
        rule
        for rule in config["packageRules"]
        if rule.get("matchDepNames") == ["python/cpython"] and "extractVersion" in rule
    ]
    assert len(rules) == 1
    # Renovate matches with RE2, which spells a named group `(?<name>)`; Python's re wants `(?P<name>)`.
    pattern = re.compile(rules[0]["extractVersion"].replace("(?<", "(?P<"))
    extracted = pattern.match("v3.14.7")
    assert extracted is not None and extracted.group("version") == "3.14.7"

    # Every consumer writes the bare value, so a `v`-prefixed proposal would break all of them.
    for path in (_CI, _MACOS_CI, _ROOT / "src" / "dreame_valetudo" / "constants.py"):
        assert not re.search(r'"v\d+\.\d+\.\d+"', path.read_text())


def test_the_cpython_tag_pin_governs_only_what_ships() -> None:
    workflows = sorted((_ROOT / ".forgejo" / "workflows").glob("*.yml"))
    workflows += sorted((_ROOT / ".github" / "workflows").glob("*.yml"))
    # A test-runner `python-version:` belongs to the built-in github-actions manager as dep
    # `python`, which tracks the actions/python-versions manifest setup-python installs from.
    # Annotating one with the CPython tag datasource adds a second owner for the same line and
    # proposes a version the manifest cannot serve for another day or more.
    for workflow in workflows:
        if workflow == _MACOS:
            continue
        assert "depName=python/cpython" not in workflow.read_text(), workflow.name

    # release-macos.yml is the exception: PyInstaller freezes this interpreter into the shipped
    # .pkg, so it must move with the bundle pin and its reviewed checksum, not on its own.
    macos = _MACOS.read_text()
    assert "depName=python/cpython" in macos
    constants = (_ROOT / "src" / "dreame_valetudo" / "constants.py").read_text()
    assert "depName=python/cpython" in constants
    bundled = re.search(r'BUNDLE_PYTHON_VERSION = "([^"]+)"', constants)
    assert bundled is not None
    assert f'python-version: "{bundled.group(1)}"' in macos

    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    disabled = [
        rule
        for rule in config["packageRules"]
        if rule.get("matchManagers") == ["github-actions"]
        and rule.get("matchDepNames") == ["python"]
        and rule.get("matchFileNames") == [".github/workflows/release-macos.yml"]
    ]
    assert len(disabled) == 1
    assert disabled[0]["enabled"] is False


def test_the_package_smoke_base_is_one_pin_with_the_qualification_image() -> None:
    smoke = (_ROOT / "packaging" / "package-smoke.Dockerfile").read_text()
    assert "depName=ubuntu-26.04-current packageName=ubuntu" in smoke
    digest = re.search(r"ubuntu:26\.04@(sha256:[0-9a-f]{64})", smoke)
    assert digest is not None
    assert digest.group(1) in _CI.read_text()

    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    disabled = [
        rule
        for rule in config["packageRules"]
        if rule.get("matchManagers") == ["dockerfile"]
        and rule.get("matchFileNames") == ["packaging/package-smoke.Dockerfile"]
    ]
    assert len(disabled) == 1
    assert disabled[0]["enabled"] is False


def test_native_macos_status_poll_stays_within_the_shared_public_api_budget() -> None:
    bridge = _MACOS_WAIT.read_text()

    assert 'DREAME_GITHUB_CI_ATTEMPTS:-12' in bridge
    assert 'DREAME_GITHUB_CI_DELAY:-300' in bridge
    assert 'DREAME_GITHUB_CI_INITIAL_DELAY:-180' in bridge
    assert '.split("@", 1)[0] == workflow' in bridge
    assert 'r.get("conclusion") == "success"' in bridge


def test_homebrew_templates_use_the_replicated_release_tarball() -> None:
    for name in ("dreame-valetudo.rb", "dreame-valetudo-rc.rb"):
        formula = (_ROOT / "packaging" / "homebrew" / name).read_text()
        assert "forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/" in formula
        assert "github.com/SisyphusMD/dreame-valetudo/releases/download/" in formula
        assert "/archive/" not in formula
        assert "refresh-pins.sh" in formula

    # No Renovate manager reads a .rb, so the formula's interpreter can only stay correct by being
    # rewritten from the pin. Assert they actually agree rather than that a note asks someone to.
    series = ".".join(
        re.search(  # type: ignore[union-attr]
            r'^BUNDLE_PYTHON_VERSION = "([^"]+)"',
            (_ROOT / "src" / "dreame_valetudo" / "constants.py").read_text(), re.M,
        ).group(1).split(".")[:2]
    )
    for name in ("dreame-valetudo.rb", "dreame-valetudo-rc.rb"):
        formula = (_ROOT / "packaging" / "homebrew" / name).read_text()
        assert f'depends_on "python@{series}"' in formula, name


def test_ci_and_both_release_gates_use_one_pinned_toolchain() -> None:
    ci = _CI.read_text()
    pins = {
        name: re.search(rf'{name}="([^"]+)"', ci).group(1)  # type: ignore[union-attr]
        for name in ("RUFF", "MYPY", "PYTEST", "SHELLCHECK")
    }
    for workflow in (_CI, _RELEASE, _PRERELEASE):
        text = workflow.read_text()
        for name in ("RUFF", "MYPY", "PYTEST"):
            assert f'{name}="{pins[name]}"' in text, workflow

    # ci.yml's shellcheck job runs on every pull_request, including forks, and is deliberately the
    # one command in that job not gated to trusted refs — so its wrapper stays inline text this
    # repo's own workflow defines, rather than a script read back off a fork's checkout and executed
    # directly. Only the workflow_dispatch-only release/prerelease gates, never fork-triggered,
    # share packaging/shellcheck-all.sh.
    assert f'SHELLCHECK="{pins["SHELLCHECK"]}"' in ci
    glob = "packaging/*.sh tests/integration/*.sh docs/research/tools/*.sh"
    assert glob in ci
    # The two must not drift: a script covered by one gate and not the other goes unchecked
    # on whichever path the change happens to take.
    assert glob in _SHELLCHECK_ALL.read_text()
    assert "apt-get install -y shellcheck" not in ci
    assert '-v "$PWD:' not in ci
    assert 'docker create -w /work "$SHELLCHECK"' in ci
    assert 'docker cp . "$cid":/work' in ci

    for workflow in (_RELEASE, _PRERELEASE):
        assert "run: bash packaging/shellcheck-all.sh" in workflow.read_text(), workflow

    # Both copies of the pin move together: Renovate's regex manager is configured to scan this
    # script alongside the workflow YAMLs (.renovaterc.json), so a bump touches both in one PR.
    script = (_ROOT / "packaging" / "shellcheck-all.sh").read_text()
    assert f'SHELLCHECK="{pins["SHELLCHECK"]}"' in script


def test_release_cutters_serialize_without_sharing_the_tag_publish_queue() -> None:
    for workflow in (_RELEASE, _PRERELEASE):
        text = workflow.read_text()
        assert re.search(
            r"\nconcurrency:\n(?:  #.*\n)+  group: release-cut\n"
            r"  cancel-in-progress: false\n",
            text,
        )
    publish = _PUBLISH.read_text()
    assert "group: publish-${{ github.ref_name }}" in publish
    assert "group: release\n" not in publish


def test_both_release_gates_install_the_real_tmux_integration_dependencies() -> None:
    for workflow in (_RELEASE, _PRERELEASE):
        text = workflow.read_text()
        gate = text[text.index("      - name: Test gate") :]
        assert "apt-get install -y -qq tmux" in gate
        assert 'pip install "ruff==$RUFF" "mypy==$MYPY" "pytest==$PYTEST" -e .' in gate


def test_both_release_gates_stamp_every_version_record_including_the_lock() -> None:
    # The behavior stamp-version.py implements (all three files, the uv.lock rc normalization, the
    # exactly-one-match guard) is pinned directly against the script in test_stamp_version.py; this
    # only pins that both workflows actually call it, then verify it, rather than re-locking. The
    # variable holding the version differs by step ($VERSION from a job-level output mapping,
    # $version from next-version.sh's own output), so the call is matched by shape, not by name.
    for workflow in (_RELEASE, _PRERELEASE):
        text = workflow.read_text()
        assert re.search(r'stamp-version\.py "\$\w+"\n', text), workflow
        assert re.search(r'stamp-version\.py "\$\w+" --check', text), workflow
        assert "uv lock" not in text, workflow


def test_macos_build_reads_the_sunxi_pin_from_constants() -> None:
    text = _MACOS.read_text()
    build = text[text.index("      - name: Build sunxi-fel") : text.index("      - name: Bundle libusb")]
    assert 'SREF="$(read_pin SUNXI_TOOLS_REF)"' in build
    assert 'checkout "$SREF"' in build
    assert not re.search(r"checkout [0-9a-f]{40}", build)


def test_macos_build_verifies_the_pinned_tmux_release_tarball() -> None:
    text = _MACOS.read_text()
    assert 'TMUX="$(read_pin TMUX_VERSION)"' in text
    assert 'TMUX_SHA="$(read_pin TMUX_SHA256)"' in text
    assert "shasum -a 256 -c -" in text
    assert text.index("shasum -a 256 -c -") < text.index("tar -xzf /tmp/tmux.tgz")


def test_stable_release_pushes_commit_and_tag_atomically() -> None:
    text = _RELEASE.read_text()
    step = text[text.index("      - name: Push the commit and tag atomically") :]
    assert "packaging/push-tag.sh" in step
    assert '"$TAG" "$TOKEN" "${{ github.ref_name }}"' in step

    script = (_ROOT / "packaging" / "push-tag.sh").read_text()
    assert "push --atomic origin" in script
    assert '"HEAD:${branch_ref}" "$tag"' in script
    assert '[ "$(git cat-file -t "refs/tags/$tag")" = tag ]' in script
    assert '[ "$(git rev-parse "$tag^{commit}")" = "$(git rev-parse HEAD)" ]' in script
    assert 'test -z "$(git status --porcelain)"' in script

    # prerelease.yml is dispatchable from any branch (packaging/README.md), unlike release.yml's
    # tag job which refuses non-main dispatches — so it does NOT hand the repo-write PAT to a
    # script whose content would come from whatever ref was dispatched; its push stays inline,
    # keeping the same three safety checks the shared script performs for release.yml.
    prerelease_step = _PRERELEASE.read_text()
    prerelease_step = prerelease_step[prerelease_step.index("      - name: Push the prerelease tag") :]
    assert "bash packaging/push-tag.sh" not in prerelease_step
    assert 'push origin "$TAG"' in prerelease_step
    assert '[ "$(git cat-file -t "refs/tags/$TAG")" = tag ]' in prerelease_step
    assert '[ "$(git rev-parse "$TAG^{commit}")" = "$(git rev-parse HEAD)" ]' in prerelease_step
    assert 'test -z "$(git status --porcelain)"' in prerelease_step


def test_release_write_token_is_confined_to_a_job_that_reproduces_the_gate() -> None:
    for workflow in (_RELEASE, _PRERELEASE):
        text = workflow.read_text()
        gate, tag = _job(text, "gate"), _job(text, "tag")

        # The job that runs third-party lint/test code holds no credential at all, and neither
        # checkout leaves one behind for a later step to pick up.
        assert "secrets." not in gate, workflow
        assert text.count("persist-credentials: false") == 2, workflow
        assert "token: ${{ secrets" not in text, workflow

        # The job that does hold the token re-derives the edits on a tree the gate never touched
        # and refuses to push anything whose bytes the gate did not qualify.
        assert "ACTUAL_INTENT_SHA256=$(git diff --binary | sha256sum" in tag, workflow
        assert '[ "$ACTUAL_INTENT_SHA256" = "$QUALIFIED_INTENT_SHA256" ]' in tag, workflow
        assert tag.index("QUALIFIED_INTENT_SHA256") < tag.index("CLUSTER_FORGEJO_REPO_WRITE_PAT")

        # An annotated tag object: a lightweight ref could be retargeted to another commit later
        # without leaving a trace of the version it was cut for. release.yml's tag job delegates
        # the re-verification (before it ever pushes) to packaging/push-tag.sh; prerelease.yml
        # keeps it inline (see test_stable_release_pushes_commit_and_tag_atomically for both).
        assert "git tag -a -m" in tag, workflow

        # Forgejo has no `permissions:` field; it warns and ignores it, so it must never appear.
        assert "permissions:" not in text, workflow


def test_native_packages_refuse_hosts_below_their_libc_floor() -> None:
    text = (_ROOT / "packaging" / "nfpm.yaml").read_text()
    deb, rpm = text.split("overrides:\n", 1)
    floor = (_ROOT / "packaging" / "glibc-floor.txt").read_text().strip()
    assert floor == "2.28"
    assert "libc6 (>= ${GLIBC_FLOOR})" in deb
    assert "  - tar" in deb
    assert "glibc >= ${GLIBC_FLOOR}" in rpm
    assert "- /usr/bin/tar" in rpm

    dockerfile = (_ROOT / "packaging" / "deb.Dockerfile").read_text()
    assert "packaging/check-glibc-floor.py" in dockerfile
    assert '"$(cat packaging/glibc-floor.txt)"' in dockerfile
    for workflow in (_CI, _PUBLISH):
        contents = workflow.read_text()
        assert "export GLIBC_FLOOR" in contents
        assert "GLIBC_FLOOR=$(cat packaging/glibc-floor.txt)" in contents
        assert "-e GLIBC_FLOOR" in contents


def test_bundled_python_updates_require_a_matching_source_checksum() -> None:
    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    rules = [
        rule
        for rule in config["packageRules"]
        if rule.get("matchDepNames") == ["python/cpython"]
        and "prBodyNotes" in rule
    ]

    assert len(rules) == 1
    assert "BUNDLE_PYTHON_SHA256" in rules[0]["prBodyNotes"][0]
    # The checksum is what makes the frozen interpreter verifiable, so the refresher has to derive
    # it from the same version Renovate writes — the .tar.xz the Linux bundle actually compiles.
    refresher = (_ROOT / "packaging" / "refresh-pins.sh").read_text()
    assert "BUNDLE_PYTHON_SHA256" in refresher
    assert "Python-${PY_VERSION}.tar.xz" in refresher


def test_manylinux_builders_are_pinned_to_dated_tags() -> None:
    # The digest already freezes the build, so `latest` bought nothing and cost the reader
    # everything: a bump arrives as a bare hex diff with no version to order or compare, for the
    # images that define the shipped glibc ABI. The dated tag makes each bump self-describing, and
    # the regex versioning keeps `latest` out of the candidates entirely. What proves a bump safe
    # is packaging/check-glibc-floor.py running inside deb.Dockerfile, which the build job builds.
    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    rules = [
        rule for rule in config["packageRules"]
        if rule.get("matchDepNames") == [
            "quay.io/pypa/manylinux_2_28_x86_64", "quay.io/pypa/manylinux_2_28_aarch64",
        ]
    ]

    assert len(rules) == 1
    assert rules[0]["versioning"].startswith("regex:")

    for workflow in (_CI, _PUBLISH):
        for line in workflow.read_text().splitlines():
            if "quay.io/pypa/manylinux_2_28_" in line and "sha256:" in line:
                assert ":latest@" not in line, f"manylinux pin regressed to latest: {line.strip()}"
                assert re.search(r":\d{4}\.\d{2}\.\d{2}-\d+@sha256:", line), line.strip()


def test_linux_package_matrix_keeps_floors_alongside_current_releases() -> None:
    workflow = _CI.read_text()
    smoke = _LINUX_PACKAGES.read_text()

    for image in (
        'debian:12-slim@sha256:',
        'debian:13-slim@sha256:',
        'ubuntu:22.04@sha256:',
        'ubuntu:26.04@sha256:',
        'fedora:43@sha256:',
        'fedora:44@sha256:',
        'rockylinux/rockylinux:8@sha256:',
        'rockylinux/rockylinux:9@sha256:',
        'rockylinux/rockylinux:10@sha256:',
        'opensuse/leap:16.0@sha256:',
    ):
        assert image in workflow
    for label in (
        "Debian 12 (oldstable floor)",
        "Debian 13 (current stable)",
        "Ubuntu 22.04 (glibc floor)",
        "Ubuntu 26.04 (current LTS)",
        "Fedora 43 (supported floor)",
        "Fedora 44 (current)",
        "Rocky Linux 8 (RHEL-compatible glibc floor)",
        "Rocky Linux 9 (RHEL-compatible maintained release)",
        "Rocky Linux 10 (RHEL-compatible current)",
        "openSUSE Leap 16.0",
    ):
        assert label in smoke

    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    fixed_hosts = {
        name: rule["allowedVersions"]
        for rule in config["packageRules"]
        for name in rule.get("matchDepNames", [])
        if name
        in {
            "debian-12-compat",
            "debian-13-current",
            "ubuntu-22.04-compat",
            "ubuntu-26.04-current",
            "fedora-43-compat",
            "fedora-44-current",
            "rocky-8-compat",
            "rocky-9-compat",
            "rocky-10-current",
            "opensuse-leap-16-current",
        }
    }
    assert fixed_hosts == {
        "debian-12-compat": "/^12-slim$/",
        "debian-13-current": "/^13-slim$/",
        "ubuntu-22.04-compat": r"/^22\.04$/",
        "ubuntu-26.04-current": r"/^26\.04$/",
        "fedora-43-compat": "/^43$/",
        "fedora-44-current": "/^44$/",
        "rocky-8-compat": "/^8$/",
        "rocky-9-compat": "/^9$/",
        "rocky-10-current": "/^10$/",
        "opensuse-leap-16-current": r"/^16\.0$/",
    }
    for dependency in fixed_hosts:
        assert f"depName={dependency} packageName=" in workflow


def test_readme_source_install_names_every_host_runtime_dependency() -> None:
    source = _README.read_text().split("### From source", 1)[1].split("## What you need", 1)[0]
    for dependency in ("libusb", "curl", "tmux", "OpenSSH", "tar", "zip", "unzip"):
        assert dependency in source


def test_readme_covers_rpm_candidate_switching_and_manual_removal() -> None:
    readme = _README.read_text()
    # Assets carry the version now, matching the sibling project, so the copyable instruction has
    # to name a file that will actually exist on disk after the download.
    assert "sudo dnf install ./dreame-valetudo-<version>.<arch>.rpm" in readme
    assert "sudo dnf downgrade ./dreame-valetudo-<version>.<arch>.rpm" in readme
    assert "sudo zypper install --oldpackage ./dreame-valetudo-<version>.<arch>.rpm" in readme
    assert "sudo dnf remove dreame-valetudo" in readme


def test_gitignore_covers_release_and_device_artifacts_created_in_the_repo() -> None:
    patterns = set((_ROOT / ".gitignore").read_text().splitlines())
    assert {
        "*_fel_ng*.zip",
        ".dreame-valetudo-image.json",
        ".private.json",
        "dreame-*-stock-recovery/",
        "/out/",
        "/out-*/",
        "/homebrew-smoke-result/",
        "/package-smoke-*/",
        "/sunxi-fel",
        "/notes.md",
    } <= patterns
