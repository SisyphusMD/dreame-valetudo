"""Release workflow contracts that are otherwise first exercised only after tagging."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_PUBLISH = _ROOT / ".forgejo" / "workflows" / "publish.yml"
_CI = _ROOT / ".forgejo" / "workflows" / "ci.yml"
# Architecture decides the forge, so the build and install-test logic lives in scripts BOTH
# forges call rather than inline in either workflow. Assertions about what a release actually
# does belong against these.
_BUILD_ARCH = _ROOT / "packaging" / "build-linux-arch.sh"
_PINS = _ROOT / "packaging" / "release-pins.env"
_RELEASE = _ROOT / ".forgejo" / "workflows" / "release.yml"
_PRERELEASE = _ROOT / ".forgejo" / "workflows" / "prerelease.yml"
_MACOS = _ROOT / ".github" / "workflows" / "release-macos.yml"
_MACOS_CI = _ROOT / ".github" / "workflows" / "ci-macos.yml"
_MACOS_WAIT = _ROOT / "packaging" / "check-mirror-ci.sh"
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
        # The condition must still OVERRIDE a failed dependency (that is the point of the repair
        # jobs) while additionally requiring the ref guard to have passed — see
        # test_no_publish_job_outruns_the_ref_guard for why the bare form was not enough.
        condition = _job(text, name)
        assert "always()" in condition, name
        assert "needs.guard.result == 'success'" in condition, name


def test_reconcile_requires_both_github_qualified_macos_packages() -> None:
    reconcile = _job(_PUBLISH.read_text(), "reconcile")

    assert "fail=0" in reconcile
    # Per-ARCH, and by suffix because the filenames now carry the version. Still not a blanket
    # `.pkg` match: one arch qualifying must never stand in for the other.
    assert 'any(endswith("-macos-arm64.pkg"))' in reconcile
    assert 'any(endswith("-macos-x86_64.pkg"))' in reconcile
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
    for source in (_CI, _BUILD_ARCH):
        text = source.read_text()
        assert "dpkg-deb -x" in text
        assert text.count("packaging/check-package-parity.py") == 2
        assert "/usr/lib/dreame-valetudo/app" in text
        assert "/usr/lib/dreame-valetudo/fastboot" in text
    # A recursive copy that dereferenced the bundles' symlinks would package something the build
    # never produced, and the parity check is what would report it.
    assert "cp -a out/dreame-valetudo out/dreame-fastboot dist/" in _CI.read_text()
    assert 'cp -a "out-$arch/dreame-valetudo" "out-$arch/dreame-fastboot" dist/' in _BUILD_ARCH.read_text()


def test_pyinstaller_floats_again_on_both_forgejo_workflows() -> None:
    # The hold existed only because a onefile child could not start under the emulated arm64 leg.
    # Onedir has no child process, so the constraint is gone at its root rather than waived.
    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    for rule in config["packageRules"]:
        assert "pyinstaller" not in rule.get("matchDepNames", [])
    pins = {}
    # The Linux pin lives in release-pins.env, which BOTH forges' build jobs source — ci.yml too, so
    # what CI compile-checks is what a release builds with. macOS keeps its own inline copy because
    # its build never touches that file.
    for source in (_PINS, _MACOS):
        text = source.read_text()
        assert "Held at 6.22.0" not in text
        found = re.search(
            r"# renovate: datasource=pypi depName=pyinstaller\s*\n[^\n]*?(\d+\.\d+(?:\.\d+)?)",
            text,
        )
        assert found is not None, source.name
        pins[source.name] = found.group(1)
    # One depName across both files is one grouped Renovate PR. The clamp scoped the Linux pins away
    # from the macOS one, which is exactly how they could drift apart unnoticed.
    assert len(set(pins.values())) == 1, pins
    # Neither Forgejo workflow may re-pin it inline: a second copy is the drift this file prevents.
    # A literal VERSION assignment, not a use — `--build-arg PYINSTALLER="$PYINSTALLER"` is how the
    # sourced value reaches the build and must stay.
    for workflow in (_CI, _PUBLISH):
        assert not re.search(r'PYINSTALLER="\d', workflow.read_text()), workflow.name


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
    assert "packaging/check-mirror-ci.sh" in forgejo
    assert ".github/workflows/ci-macos.yml" in forgejo
    assert 'github.event.pull_request.head.repo.full_name == github.repository' in forgejo
    assert 'os.environ["GITHUB_EVENT_PATH"]' in forgejo
    # Or the repository token sits in git config while a script from a PR-controlled ref
    # runs beside it. The release mirror gate withholds it for the same reason.
    assert "persist-credentials: false" in forgejo, (
        "the gate's checkout leaves the repository token in git config"
    )
    assert '["pull_request"]["head"]["sha"]' in forgejo
    # WRITE is the property, not "no secrets": reading public run conclusions needs a scopeless
    # token, and going without one shares a 60-request hour with everything else leaving this
    # network - including the sibling's copy of this gate - which turns a fine commit into a
    # timeout. What must never appear here is anything that could write.
    # Case-insensitive: secret expressions are not required to be shouted, and a
    # `secrets.write_pat` that this missed would be a silently unguarded credential.
    for secret in re.findall(r"secrets\.([A-Za-z_0-9]+)", forgejo):
        assert secret.upper() == "GH_REPO_READ_PAT", (
            f"the macos gate takes {secret}; it runs on every push and needs only to READ "
            "public run conclusions"
        )
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
    assert 'pip install "pytest==$PYTEST" "pyyaml==$PYYAML" -e .' in floor_job
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
    """EVERY workflow that names a version, not just the one this test used to read.

    The literal appears in five workflow files across two forges. Checking one of them left the
    other four free to drift, and one of them did: a Renovate bump landed the new ruff in the
    Forgejo workflows, `pyproject.toml` and `uv.lock`, while GitHub's `ci-pr.yml` kept linting pull
    requests with the previous release. Nothing failed, because nothing compared them.
    """
    workflows = sorted(
        [*(_ROOT / ".forgejo" / "workflows").glob("*.yml"),
         *(_ROOT / ".github" / "workflows").glob("*.yml")]
    )
    lock = (_ROOT / "uv.lock").read_text()
    project = (_ROOT / "pyproject.toml").read_text()
    for package, var in (("ruff", "RUFF"), ("mypy", "MYPY"), ("pytest", "PYTEST"),
                         ("pytest-cov", "PYTEST_COV"), ("pyyaml", "PYYAML")):
        locked = re.search(rf'name = "{package}"\nversion = "([^"]+)"', lock)
        declared = re.search(rf'"{package}==([^"]+)"', project)
        assert locked is not None, package

        for workflow in workflows:
            for literal in re.findall(rf'{var}="([^"]+)"', workflow.read_text()):
                assert literal == locked.group(1), (
                    f"{package}: {workflow.name} installs {literal}, "
                    f"uv.lock resolves {locked.group(1)}"
                )
        # An `==` pin, not a floor: a floor is permanently satisfied, so pep621 raises nothing and
        # the lock never follows the literal Renovate does move. This is what makes one PR able to
        # carry all three, so it is pinned here rather than left to the config comment.
        assert declared is not None, f"{package} must be pinned exactly in pyproject.toml"
        assert declared.group(1) == locked.group(1), (
            f"{package}: pyproject pins {declared.group(1)}, uv.lock resolves {locked.group(1)}"
        )


def test_every_hold_says_what_CI_cannot_reach() -> None:
    """A hold must carry its reason, and the only admissible reason is that green says nothing.

    The holds this test originally banned outright existed because something bound to the version —
    a digest, a lockfile — had no datasource and could not move with it, so the branch arrived
    half-applied. `refresh-pins.sh` removed that reason, and a hold with no reason is just a chore.

    But a blanket ban was too strong, and the config quietly disagreed with itself for it: two rules
    described their bumps as hand-reviewed while nothing implemented that. Some dependencies are
    genuinely outside what CI can exercise — pyusb's descriptor and bulk-transfer paths are stubbed
    out of the unit tests entirely, so a green run is silent about the code that writes to flash.
    Automerging on a signal that cannot see the risk is worse than holding.

    So: hold if you must, but say what CI cannot reach, in `prBodyNotes`, where the reviewer sees it.
    """
    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    undocumented = [
        rule.get("matchDepNames") or rule.get("matchManagers") or rule.get("description", "?")[:60]
        for rule in config["packageRules"]
        if rule.get("automerge") is False and not rule.get("prBodyNotes")
    ]
    assert undocumented == [], f"held with no stated reason: {undocumented}"


def test_renovate_automerges_patch_minor_and_digest_on_green() -> None:
    """The same set as the sibling, asserted the same way in both repos. Every dependency that
    ships or builds the tool is exercised by a CI job and Renovate only automerges on green, so a
    breaking bump fails the PR before it can merge."""
    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    blanket = [r for r in config["packageRules"] if r.get("automerge") is True]
    assert len(blanket) == 1, "more than one blanket automerge rule"
    assert sorted(blanket[0]["matchUpdateTypes"]) == ["digest", "minor", "patch"]


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
    # The built-in dockerfile manager must not run at all. It reads a plain `debian` where the
    # annotations declare `debian-12-compat`, so with both active one file yields two dependencies
    # under two names, and the allowedVersions rules that hold the compatibility floors apply to
    # only one of them — the floor drifts in a PR of its own that looks entirely reasonable.
    assert "dockerfile" not in config["enabledManagers"], config["enabledManagers"]

    # Asserted by matching, not by spelling: a manager that reads this file through a directory
    # glob covers it just as well as one that names it, and naming it was never the point.
    covered = [
        m
        for m in config["customManagers"]
        if any(
            re.search(pattern.strip("/"), "packaging/install-smoke.Dockerfile")
            for pattern in m.get("managerFilePatterns", [])
        )
    ]
    assert covered, "install-smoke.Dockerfile's annotated pins need a manager that reads them"


def test_native_macos_status_poll_stays_within_the_shared_public_api_budget() -> None:
    bridge = _MACOS_WAIT.read_text()

    # Two Forgejo runners share one public egress IP and GitHub allows 60 unauthenticated requests
    # an hour. A 2400s deadline polled every 300s is 8 requests per gate, so several can be in
    # flight at once and still sit well under the shared limit.
    assert 'MIRROR_CI_TIMEOUT:-2400' in bridge
    assert 'MIRROR_CI_INTERVAL:-300' in bridge
    # A throttled response is a statement about the quota, not about the commit. Failing the gate
    # on one would turn somebody else's traffic into a red release.
    assert '403|429)' in bridge
    # Match the workflow FILE, and let a success outrank a cancelled sibling.
    assert '.split("@", 1)[0] == workflow' in bridge
    assert 'r.get("conclusion") == "success"' in bridge


def test_homebrew_templates_build_from_the_pypi_sdist() -> None:
    """Same source as the sibling project's formula, which is what let `update-tap.sh` become one
    vendored script instead of two that drift.

    The sdist FILENAME is PEP 625-normalised — `dreame_valetudo`, with an underscore — while the
    directory segment keeps the published name. The hyphenated filename 404s, which a project
    whose name has no hyphen would never discover.
    """
    for name in ("dreame-valetudo.rb", "dreame-valetudo-rc.rb"):
        formula = (_ROOT / "packaging" / "homebrew" / name).read_text()
        assert (
            "files.pythonhosted.org/packages/source/d/dreame-valetudo/"
            "dreame_valetudo-REPLACE_VERSION.tar.gz"
        ) in formula, name
        # A release-asset url and a mirror line are what the PyPI sdist replaced; a formula
        # carrying both would be served two different archives for one checksum.
        assert "releases/download/" not in formula, name
        assert "\n  mirror " not in formula, name
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
    # Both gates DISCOVER their inputs rather than enumerating them. A hand-maintained glob left a
    # script added outside packaging/, tests/release/ or docs/research/tools/ unchecked, and the
    # only symptom was silence. The two must still not drift from each other: a script covered by
    # one gate and not the other goes unchecked on whichever path the change happens to take.
    assert "git ls-files '*.sh'" in ci
    assert "git ls-files '*.sh'" in _SHELLCHECK_ALL.read_text()
    assert "packaging/*.sh" not in ci, "back to a hand-maintained list"
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
    publish = yaml.safe_load(_PUBLISH.read_text())
    group = str((publish.get("concurrency") or {}).get("group", ""))
    # The exact ref, not a shared queue and not the short name: `ref_name` drops refs/heads and
    # refs/tags alike, so a branch sharing a tag's name would land in the same group, and
    # publish.yml is dispatchable.
    assert re.search(r"github\.ref\s*}}", group), group
    assert group != "release", "publishing must not share the cutters' queue"


def test_both_release_gates_install_the_real_tmux_integration_dependencies() -> None:
    for workflow in (_RELEASE, _PRERELEASE):
        text = workflow.read_text()
        gate = text[text.index("      - name: Test gate") :]
        assert "apt-get install -y -qq tmux" in gate
        # pytest-cov is in this list because the release path gates coverage too, not only ci.yml:
        # CI runs on a branch and a release is cut from a tag, so a gate that lives only in CI does
        # not constrain what a release may ship.
        assert (
            'pip install "ruff==$RUFF" "mypy==$MYPY" "pytest==$PYTEST" "pytest-cov==$PYTEST_COV"'
            ' "pyyaml==$PYYAML" -e .'
            in gate
        )


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
    # Only that it does NOT use the shared helper. What the inline push must itself guarantee is
    # pinned by test_workflow_security.py, in both projects, so there is one place to change it.
    assert "bash packaging/push-tag.sh" not in prerelease_step


def test_release_write_token_is_confined_to_a_job_that_reproduces_the_gate() -> None:
    for workflow in (_RELEASE, _PRERELEASE):
        text = workflow.read_text()
        gate, tag = _job(text, "gate"), _job(text, "tag")

        # The job that runs third-party lint/test code holds no credential at all, and neither
        # checkout leaves one behind for a later step to pick up.
        assert "secrets." not in gate, workflow
        # EVERY checkout disclaims the credential, rather than a fixed COUNT of them doing so. The
        # count was 2 because there happened to be two checkouts; adding a third credential-free job
        # then failed a test whose actual subject — that none of them persists a token — had only
        # got stronger. Scanned as text: this project has no YAML parser and wants no dev dep for
        # one assertion.
        # Not "- uses:": a checkout may be a bare list item or carry its own `name:` above the
        # `uses:` line, and matching only the first spelling silently found no checkouts at all.
        blocks = text.split("uses: actions/checkout")
        assert len(blocks) > 1, f"{workflow}: no checkout at all"
        for block in blocks[1:]:
            # Up to the next step in the same job, or the next job.
            head = re.split(r"\n\s*- (?:name|uses):|\n  \w[\w-]*:", block)[0]
            assert "persist-credentials: false" in head, (
                f"{workflow}: a checkout does not disclaim the credential:\n{head[:200]}"
            )
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
    for source in (_CI, _BUILD_ARCH):
        contents = source.read_text()
        assert "export VERSION GLIBC_FLOOR" in contents or "export GLIBC_FLOOR" in contents
        assert "packaging/glibc-floor.txt" in contents
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

    # release-pins.env, which is where both forges' build jobs read them from. Counted, because a
    # test whose loop body never runs passes without checking anything: these pins used to be inline
    # in the workflows, and scanning the old location would have gone quietly vacuous the moment
    # they moved.
    checked = 0
    for line in _PINS.read_text().splitlines():
        if "quay.io/pypa/manylinux_2_28_" in line and "sha256:" in line:
            assert ":latest@" not in line, f"manylinux pin regressed to latest: {line.strip()}"
            assert re.search(r":\d{4}\.\d{2}\.\d{2}-\d+@sha256:", line), line.strip()
            checked += 1
    assert checked == 2, f"expected both manylinux builders in {_PINS.name}, found {checked}"


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
    # Assets carry the version now, matching whiskerless, so the copyable instruction has to name
    # a file that will actually exist on disk after the download.
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


def test_every_shipped_python_file_is_linted() -> None:
    """A new shipped .py must not escape ruff by nobody remembering to add it to a list.

    CI globs the tracked set instead of enumerating it; this pins the one deliberate exclusion so
    widening it is a visible edit rather than a silent one.
    """
    workflow = (_ROOT / ".forgejo" / "workflows" / "ci.yml").read_text()
    assert "ruff check $(git ls-files '*.py'" in workflow, "ruff no longer discovers its own inputs"
    excluded = [
        line for line in workflow.splitlines()
        if "ruff check $(git ls-files" in line
    ]
    assert len(excluded) == 1
    assert "grep -v '^docs/research/tools/'" in excluded[0], "the only permitted exclusion changed"


def test_every_release_path_gates_coverage_not_just_ci() -> None:
    """ci.yml runs on a branch; a release is cut from a tag. A non-regression gate that lives only
    in CI does not constrain what a release is allowed to ship, which is how a coverage regression
    could have shipped from this repository while every branch build stayed green."""
    for workflow in (_CI, _RELEASE, _PRERELEASE):
        text = workflow.read_text()
        assert "--cov-fail-under=99" in text, workflow
        # The Runner seam is held at 100 separately: averaged into the repository number, a
        # regression in the one place every external command passes through would be invisible.
        assert "coverage report --include='*/run.py' --fail-under=100" in text, workflow


# --- the apt/dnf repository channel -------------------------------------------------
#
# dnf accepts a package signed by ANY key listed in `gpgkey`, so what these files trust is the
# whole security property of the channel. The key is scoped to the SisyphusMD NAMESPACE, not to
# this project: Forgejo's package registry group is per-owner, every project here publishes into
# the same group, and a shared group would force every project's key into every .repo file anyway
# — at which point any one of them could sign a package named for another.
_REPO_FILES = sorted((_ROOT / "packaging").glob("*.repo"))
_OUR_KEY_URL = (
    "https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo"
    "/raw/branch/main/packaging/sisyphusmd-signing-key.asc"
)
_SIGNING_KEY_ID = "CCE50015D058E9BF"
_FORGEJO_REGISTRY_KEY = "/api/packages/SisyphusMD/rpm/repository.key"


def _gpgkey_urls(text: str) -> list[str]:
    """Every key the file trusts, including INI continuation lines.

    An indented line after `gpgkey=` continues the value, which is exactly how a second key gets
    added without touching the `gpgkey=` line itself — so reading only that one line would miss it.
    """
    urls: list[str] = []
    collecting = False
    for line in text.splitlines():
        if line.startswith("gpgkey="):
            collecting = True
            urls += line[len("gpgkey=") :].split()
        elif collecting and line[:1] in (" ", "\t"):
            urls += line.split()
        elif collecting:
            collecting = False
    return urls


def test_both_repository_channels_are_shipped() -> None:
    assert {p.name for p in _REPO_FILES} == {"sisyphusmd.repo", "sisyphusmd-testing.repo"}


def test_every_dnf_config_trusts_our_key_alone() -> None:
    for path in _REPO_FILES:
        text = path.read_text(encoding="utf-8")
        config = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        assert _gpgkey_urls(config) == [_OUR_KEY_URL], path.name
        # Forgejo keeps its registry keys in its database in plaintext, on the same host that
        # serves the packages — listing it would make one host's compromise sufficient to install
        # arbitrary code on every subscriber.
        assert _FORGEJO_REGISTRY_KEY not in config, path.name
        assert "gpgcheck=1" in config, path.name
        # Exact value, not merely absent: flipping this to 1 makes dnf verify the index against
        # Forgejo's key, which these files deliberately do not list — so the repository breaks.
        assert "repo_gpgcheck=0" in config, path.name


def test_the_pinned_key_is_the_one_this_repository_ships() -> None:
    """The URL could be right while the file behind it is some other key."""
    key = _ROOT / "packaging" / "sisyphusmd-signing-key.asc"
    assert key.exists(), "the signing key the .repo files pin is not in the repository"
    if not shutil.which("gpg"):
        return
    # A throwaway GNUPGHOME. `gpg` opens — and creates — its trust database before it will look
    # at anything, so without this the test depends on the caller having a writable ~/.gnupg and
    # quietly writes to it when they do. It fails in a sandbox for a reason that has nothing to do
    # with the key it is checking.
    with tempfile.TemporaryDirectory() as home:
        proc = subprocess.run(
            ["gpg", "--homedir", home, "--show-keys", "--with-colons", str(key)],
            capture_output=True, text=True, check=False,
        )
    assert proc.returncode == 0, proc.stderr
    fingerprints = [ln.split(":")[9] for ln in proc.stdout.splitlines() if ln.startswith("fpr:")]
    assert any(f.endswith(_SIGNING_KEY_ID) for f in fingerprints), fingerprints


def test_every_dnf_config_names_a_distribution_the_publisher_writes() -> None:
    """A baseurl pointing at a group nothing publishes to is a repository that resolves, returns
    an empty index, and reports no candidate."""
    publisher = (_ROOT / "packaging" / "publish-registry.sh").read_text(encoding="utf-8")
    for path in _REPO_FILES:
        baseurl = next(
            ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("baseurl=")
        )
        distribution = baseurl.rstrip("/").rsplit("/", 1)[1]
        assert re.search(rf'dists="[^"]*\b{distribution}\b', publisher), (
            f"{path.name} points at '{distribution}', which publish-registry.sh never writes to"
        )


def test_publishing_refuses_to_ship_unsigned_packages() -> None:
    """Unsigned packages install fine, so nothing downstream notices — but subscribers running
    `gpgcheck=1` get a channel that fails for them and for nobody else."""
    text = _BUILD_ARCH.read_text()
    assert 'GPG_SIGNING_KEY:?' in text
    assert "NFPM_SIGNING_KEY_FILE" in text
    # The key is written outside the workspace: `docker cp . :/w` sends the whole tree.
    assert 'KEYFILE="$(mktemp)"' in text
    assert "::warning::GPG_SIGNING_KEY is not set" not in text
    # Both forges build packages now, so both must hand the key in — an arm64 release signed by
    # nobody would install fine and fail only for subscribers running gpgcheck=1.
    for workflow in (_PUBLISH, _ROOT / ".github" / "workflows" / "release-linux-arm64.yml"):
        assert "GPG_SIGNING_KEY: ${{ secrets.GPG_SIGNING_KEY }}" in workflow.read_text(), workflow.name


def test_no_publish_job_outruns_the_ref_guard() -> None:
    """publish.yml is dispatchable so a partly-failed release can be finished. The guard job refuses
    a dispatch whose ref is not a release tag, because "main" would otherwise BE the version:
    packages named for it, and releases called `main` created on all three registries.

    But several jobs carry `always()` or `!cancelled()` so that one registry failing does not skip
    the others — and those override a FAILED dependency, not merely an unsuccessful release. A guard
    that refused the ref therefore stopped nothing: every external-write job downstream still ran.
    Naming the guard in `needs` is not enough either; the condition has to test its result.

    Scanned as text — this project has no YAML parser and wants no dev dep for one assertion.
    """
    text = _PUBLISH.read_text()
    assert "\n  guard:\n" in text, "the ref guard is gone"
    unguarded = []
    # Each job is a top-level 2-space key; slice on that and read the header lines.
    for chunk in re.split(r"\n  (?=[a-z][\w-]*:\n)", text.split("\njobs:\n", 1)[1]):
        name = chunk.split(":", 1)[0].strip()
        header = chunk.split("steps:", 1)[0]
        condition = "".join(ln for ln in header.splitlines() if ln.strip().startswith("if:"))
        if "always()" not in condition and "cancelled()" not in condition:
            continue  # the implicit needs-success gate already covers it
        needs = "".join(ln for ln in header.splitlines() if ln.strip().startswith("needs:"))
        if "guard" not in needs or "needs.guard.result" not in condition:
            unguarded.append(name)
    assert unguarded == [], (
        "these run even when the guard refused the ref, and write to registries while they do: "
        f"{unguarded}"
    )


def test_the_registry_publish_runs_but_never_on_a_partial_package_set() -> None:
    """A repository is not a release page: what lands there is what `apt install` hands people
    immediately, with no way to tell the set is short.

    This used to be guarded by `steps.build.outcome == 'success'`, because one job built both
    architectures and a failed arm64 leg left two unsmoked amd64 packages on disk. Architecture now
    decides the forge, so there is no such step to ask about — this job never builds anything. The
    guard is that it must COLLECT all four packages from the release before publishing any."""
    registry = _job(_PUBLISH.read_text(), "registry")
    assert "packaging/publish-registry.sh forgejo.bryantserver.com" in registry
    # A separate token: `write:repository` cannot upload a package to Forgejo's registry.
    assert "CLUSTER_FORGEJO_REGISTRY_PUSH_PAT" in registry
    assert "packaging/fetch-release-assets.sh" in registry
    # Exactly four, and nothing swallowing the failure: matched as the whole command so the
    # explanatory comment beside it cannot satisfy the check.
    fetch = re.search(r"^\s*packaging/fetch-release-assets\.sh[^\n]*$", registry, re.M)
    assert fetch is not None
    assert fetch.group(0).strip() == 'packaging/fetch-release-assets.sh "$GITHUB_REF_NAME" 4 "$select"'


# --- Homebrew bottles -----------------------------------------------------------------
#
# A bottle's keg is rooted at `<formula>/<version>/` and its filename embeds the formula name, so a
# `dreame-valetudo` bottle cannot be renamed into a `dreame-valetudo-rc` one — which is why a stable
# tag builds two sets. The failure mode that matters is a formula advertising checksums for files
# that are not there: unlike a MISSING block, which falls back to building from source, a wrong one
# fails every install outright.
_TEMPLATES = sorted((_ROOT / "packaging" / "homebrew").glob("*.rb"))
_RENDERER = _ROOT / "packaging" / "render-formula.sh"


def _render(template: Path, block: Path | None = None) -> str:
    argv = [str(_RENDERER), str(template), "0.3.0", "deadbeef"]
    if block is not None:
        argv.append(str(block))
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_every_formula_template_renders_with_no_marker_left() -> None:
    """A surviving `REPLACE_BOTTLE_BLOCK` is a bare word, which Ruby parses as a constant — so
    Homebrew fails to load the formula at install time rather than anything failing here."""
    assert _TEMPLATES
    for template in _TEMPLATES:
        assert "REPLACE_" not in _render(template), template.name


def test_every_formula_template_renders_with_a_bottle_block(tmp_path: Path) -> None:
    block = tmp_path / "block"
    block.write_text(
        '  bottle do\n    root_url "https://example.invalid"\n'
        '    sha256 cellar: :any_skip_relocation, arm64_sequoia: "aa"\n  end\n'
    )
    for template in _TEMPLATES:
        rendered = _render(template, block)
        assert "bottle do" in rendered, template.name
        assert "REPLACE_" not in rendered, template.name


def test_the_tap_updater_refuses_a_bottle_pass_that_produced_no_block() -> None:
    """Publishing a formula with no block reports success and leaves every user building from
    source — the exact failure the second pass exists to remove."""
    text = (_ROOT / "packaging" / "update-tap.sh").read_text()
    assert 'if [ -n "$manifests" ] && ! grep -Fq "bottle do" "$out"; then' in text
    # --expect-tags is what makes a platform whose bottle never arrived visible at all.
    assert "--expect-tags 4" in text


def test_every_job_that_writes_the_tap_shares_one_concurrency_group() -> None:
    """Two of them cloning the same tip means the loser pushes a non-fast-forward and the tap keeps
    whichever formula lost the race."""
    publish = _PUBLISH.read_text()
    for job in ("homebrew-tap", "homebrew-bottles"):
        assert "group: tap-write" in _job(publish, job), job
    assert "group: tap-write" in (_ROOT / ".forgejo" / "workflows" / "tap-bottles.yml").read_text()


def test_a_stable_tag_bottles_both_formulae() -> None:
    bottles = _job(_PUBLISH.read_text(), "homebrew-bottles")
    assert "*-rc.*) expected=4 ;;" in bottles
    assert "*)      expected=8 ;;" in bottles


def test_the_formulae_build_sunxi_fel_at_the_pinned_commit() -> None:
    """The bottle carries sunxi-fel, so the formula's pin has to be the same one everything else
    builds from — a formula a commit behind produces a bottle nobody else's build matches.

    Pinned by git revision rather than an archive checksum on purpose: the commit IS the content
    hash, so there is no second digest for refresh-pins.sh to keep in step and nothing to drift.
    """
    ref = re.search(
        r'^SUNXI_TOOLS_REF = "([0-9a-f]{40})"',
        (_ROOT / "src" / "dreame_valetudo" / "constants.py").read_text(),
        re.M,
    )
    assert ref, "SUNXI_TOOLS_REF is not a 40-character commit in constants.py"
    for name in ("dreame-valetudo.rb", "dreame-valetudo-rc.rb"):
        formula = (_ROOT / "packaging" / "homebrew" / name).read_text()
        assert 'resource "sunxi-tools"' in formula, name
        assert f'revision: "{ref.group(1)}"' in formula, name
        # An archive URL would reintroduce the checksum this pin exists to avoid.
        assert "sunxi-tools.git" in formula, name


def test_the_formulae_do_not_pip_install_the_c_resource() -> None:
    """`virtualenv_install_with_resources` installs EVERY resource, and sunxi-tools is a C program.
    Reaching for the convenience wrapper here fails the build at `pip install sunxi-tools`."""
    for name in ("dreame-valetudo.rb", "dreame-valetudo-rc.rb"):
        formula = (_ROOT / "packaging" / "homebrew" / name).read_text()
        called = [
            line for line in formula.splitlines()
            if line.strip().startswith("virtualenv_install_with_resources")
        ]
        assert called == [], f"{name} calls the wrapper: {called}"
        assert 'resources.reject { |r| r.name == "sunxi-tools" }' in formula, name


def test_the_formulae_hand_the_tool_its_own_helper_directory() -> None:
    """find_helper() consults DREAME_LIBEXEC first; without the wrapper the brew install would fall
    through to the system prefixes and find either nothing or another install's sunxi-fel."""
    for name in ("dreame-valetudo.rb", "dreame-valetudo-rc.rb"):
        formula = (_ROOT / "packaging" / "homebrew" / name).read_text()
        # Matched on CODE, with comments stripped: the wrapper's own explanation names the wrong
        # call form as the thing to avoid, so a substring search over the whole file passes on the
        # comment even when the real invocation is gone.
        code = "\n".join(
            line for line in formula.splitlines() if not line.lstrip().startswith("#")
        )
        # `(bin/"name")`, never bare `bin`: write_env_script writes AT the pathname it is called on,
        # so calling it on the directory replaces bin itself with a file and every install is
        # ENOTDIR. A real `brew install` is the only thing that catches it, which the CI build job
        # now runs.
        assert '(bin/"dreame-valetudo").write_env_script' in code, name
        assert "\n    bin.write_env_script" not in code, f"{name}: writes over the bin directory"
        assert "DREAME_LIBEXEC:" in code, name


def test_the_caveats_no_longer_promise_a_first_run_source_build() -> None:
    """That warning was the cost this change removes, and it needed a compiler and a network at
    exactly the moment the host is joined to the robot's own AP, which has no internet."""
    for name in ("dreame-valetudo.rb", "dreame-valetudo-rc.rb"):
        formula = (_ROOT / "packaging" / "homebrew" / name).read_text()
        assert "builds sunxi-fel from source" not in formula, name


def test_the_infra_retry_watches_every_github_workflow() -> None:
    """A runner fault is not selective about which workflow it lands on, so a partial watch list is
    just an undetected flake somewhere else. The sibling project's list had drifted to three of
    seven, missing its two longest-running workflows — the same test lives in both repos."""
    # Read with a regex rather than a YAML parser: this project has no runtime dependencies and
    # its dev set is pinned deliberately, so a parser is not worth adding for one list of strings.
    retry = _ROOT / ".github" / "workflows" / "retry-infra-failures.yml"
    block = re.search(r"^    workflows:\n((?:\s*- .+\n)+)", retry.read_text(), re.M)
    assert block, "the retry workflow has no `workflows:` watch list"
    watched = set(re.findall(r'- "([^"]+)"', block.group(1)))
    present = {}
    for path in sorted((_ROOT / ".github" / "workflows").glob("*.yml")):
        found = re.search(r"^name:\s*(.+)$", path.read_text(), re.M)
        if found:
            present[found.group(1).strip()] = path.name
    # Itself excluded: a retry workflow retrying its own runner failure would recurse.
    unwatched = {n: f for n, f in present.items() if n not in watched and f != retry.name}
    assert unwatched == {}, f"GitHub workflows with no infra-retry cover: {unwatched}"
    assert watched <= set(present), f"watches workflows that do not exist: {watched - set(present)}"


def test_the_infra_triage_is_the_shared_one() -> None:
    """The discriminator is subtle enough that two copies would drift into two policies — and the
    dangerous direction of drift is the generous one, which launders flaky tests into green builds
    with nobody noticing because the build is green."""
    retry = (_ROOT / ".github" / "workflows" / "retry-infra-failures.yml").read_text()
    assert "packaging/triage-infra-failure.py" in retry
    assert (_ROOT / "packaging" / "triage-infra-failure.py").exists()
    # The attempt-specific endpoint: plain /jobs returns the LATEST attempt, so after a retry it
    # reports the retry's green jobs and the triage sees nothing to explain.
    assert "attempts/$ATTEMPT/jobs" in retry


# --- the install matrix ---------------------------------------------------------------
_INSTALL_MATRIX = _ROOT / ".forgejo" / "workflows" / "install-matrix.yml"
_INSTALL_MATRIX_GH = _ROOT / ".github" / "workflows" / "install-matrix.yml"
_MATRIX_ARCH = _ROOT / "packaging" / "install-matrix-arch.sh"
_INSTALL_SMOKE = _ROOT / "packaging" / "install-smoke.Dockerfile"


def test_every_channel_the_matrix_builds_has_a_target() -> None:
    """A channel named in the workflow but absent from the Dockerfile fails the build and is
    noticed. The dangerous direction is the other one: a target nobody builds looks like coverage
    in the file and tests nothing at all."""
    script = _MATRIX_ARCH.read_text()
    listed = set(re.search(r"^CHANNELS=\(\n(.*?)^\)$", script, re.M | re.S).group(1).split())
    # The amd64-only addition, appended rather than listed: the linuxbrew image has no arm64 build.
    extra = re.search(r"CHANNELS\+=\(([a-z-]+)\)", script)
    if extra:
        listed.add(extra.group(1))
    targets = set(re.findall(r"AS ([a-z0-9-]+)-result", _INSTALL_SMOKE.read_text()))
    assert listed - targets == set(), f"workflow builds channels with no target: {listed - targets}"
    assert targets - listed == set(), f"Dockerfile targets nothing builds: {targets - listed}"


def test_the_matrix_covers_the_channels_that_had_nothing_proving_them() -> None:
    """The two that justified writing this: a repository is a URL every subscriber's package
    manager resolves on every update, and a bottle falls back to a SOURCE BUILD when its checksums
    are stale — quietly, and green."""
    script = _MATRIX_ARCH.read_text()
    for channel in ("apt-repo", "dnf-repo", "bottle-pour", "tarball"):
        assert channel in script, channel
    # And BOTH architectures run that one list. Two inline copies on two forges is a divergence with
    # a schedule: the arm64 half would keep passing while testing a shorter set.
    assert "install-matrix-arch.sh amd64" in _INSTALL_MATRIX.read_text()
    assert "install-matrix-arch.sh arm64" in _INSTALL_MATRIX_GH.read_text()
    # It refuses to run for an architecture the host is not, so a leg on the wrong runner fails
    # loudly instead of quietly reporting on an emulator.
    assert "that would be emulation" in script


def test_the_bottle_channel_refuses_a_source_build() -> None:
    """`brew install` succeeding proves nothing on its own — it succeeds by building from source
    when no bottle matches, which is exactly the failure this channel exists to catch."""
    smoke = _INSTALL_SMOKE.read_text()
    assert 'grep -qi "pouring dreame-valetudo"' in smoke
    # sunxi-fel rides inside the bottle now; a poured install that lacks it means the formula
    # change silently stopped working and the first-run source build is back.
    assert "libexec/tools/sunxi-fel" in smoke
    # And NOT by counting installed dependencies. `dtc` and `pkg-config` are ordinary
    # `depends_on`, so Homebrew installs them for a poured bottle too; the count grew on a correct
    # pour and failed the channel one line after the log had already proven it poured.
    assert "build-only deps appeared" not in smoke


def test_a_stable_tag_is_installed_from_the_stable_distribution() -> None:
    """publish-registry.sh puts a candidate in `testing` and a release in BOTH. Testing a stable
    through `testing` would leave the distribution real users are on unexercised by a matrix that
    claims to cover every channel."""
    script = _MATRIX_ARCH.read_text()
    assert "*-rc.*) DIST=testing; REPOFILE=sisyphusmd-testing.repo ;;" in script
    assert "*)      DIST=stable;  REPOFILE=sisyphusmd.repo ;;" in script
    # The .repo file has to travel with the distribution, or the rc matrix installs the stable
    # repository and qualifies the previous release while the testing channel goes untested.
    assert '--build-arg REPOFILE="$REPOFILE"' in script


def test_every_channel_proves_itself_with_an_exported_file() -> None:
    """buildx caches aggressively, so an exit status can be a cache hit for a build that did
    nothing. The marker file is the only thing that cannot be."""
    assert '[ -f "out/$channel/passed" ]' in _MATRIX_ARCH.read_text()
    exported = re.findall(r"COPY --from=([a-z0-9-]+) [^\n]*passed", _INSTALL_SMOKE.read_text())
    assert len(exported) >= 9, f"only {len(exported)} channels export a marker"



def test_every_job_that_runs_the_suite_installs_the_yaml_parser() -> None:
    """tests/python imports PyYAML, and pip does not install PEP 735 dependency groups.

    `uv run pytest` resolves the dev group from uv.lock and passes locally; every CI job instead
    pip-installs an explicit pinned list plus `-e .`, so a test-only import that is not on that
    list fails at COLLECTION and takes the whole job with it. Local green and CI red, for a
    dependency that looks declared.

    Per JOB, not per step: steps share one environment, so the install and the run are routinely
    two different steps.
    """
    workflows = sorted((_ROOT / ".forgejo" / "workflows").glob("*.yml"))
    workflows += sorted((_ROOT / ".github" / "workflows").glob("*.yml"))
    checked = 0
    for workflow in workflows:
        jobs = yaml.safe_load(workflow.read_text())["jobs"]
        for name, job in jobs.items():
            runs = "\n".join(
                step.get("run", "") for step in job.get("steps", []) if isinstance(step, dict)
            )
            if "pytest -q tests/python" not in runs:
                continue
            checked += 1
            assert "pyyaml==" in runs, f"{workflow.name}:{name} runs the suite without pyyaml"
    assert checked >= 6, f"only found {checked} jobs running the suite"


def test_the_pinned_standard_version_matches_what_is_actually_vendored() -> None:
    """Renovate bumps the pin. It does not re-vendor the files.

    The pin in `packaging/release-pins.env` records which standard this repo SHOULD carry;
    `STANDARD.lock` records which one it actually does. Nothing else compares them, and a bumped pin
    whose files were never re-synced leaves this repo green while running a different standard than
    it claims — the drift the lock exists to prevent, arriving through the one door the lock does not
    watch, since every vendored file still matches the older lock perfectly.

    The pin omits the leading `v` the tag carries, because the shared Renovate matchString requires a
    digit first and a `v`-prefixed value matches nothing at all, silently.
    """
    pins = (_ROOT / "packaging" / "release-pins.env").read_text(encoding="utf-8")
    found = re.search(r'^PROJECT_STANDARD="([^"]+)"', pins, re.M)
    assert found, "packaging/release-pins.env does not pin PROJECT_STANDARD"
    pinned = found.group(1)
    assert not pinned.startswith("v"), f"the pin carries a leading v, which Renovate will not match: {pinned}"

    lock = json.loads((_ROOT / "STANDARD.lock").read_text(encoding="utf-8"))
    assert lock["source_tag"] == f"v{pinned}", (
        f"pinned v{pinned}, but the vendored files come from {lock['source_tag']} — "
        "re-vendor from the pinned tag and land both together"
    )


def test_every_hold_survives_rule_ordering() -> None:
    """Renovate applies every matching packageRule in order, and the LAST one to set a field wins.

    So a hold placed ABOVE the broad patch/minor/digest automerge rule is silently undone by it: the
    config still reads as a hold, review still looks required, and the dependency automerges anyway.
    Position is not the property, so this resolves the rules the way Renovate does and asserts the
    value that actually results — for every held dependency, not just the newest one.
    """
    rules = json.loads((_ROOT / ".renovaterc.json").read_text(encoding="utf-8"))["packageRules"]

    def resolved(dep: str, update_type: str) -> object:
        value: object = None
        for rule in rules:
            names = rule.get("matchDepNames") or rule.get("matchPackageNames")
            if names is not None and dep not in names:
                continue
            types = rule.get("matchUpdateTypes")
            if types is not None and update_type not in types:
                continue
            if "automerge" in rule:
                value = rule["automerge"]
        return value

    held = sorted({
        dep
        for rule in rules
        if rule.get("automerge") is False
        for dep in (rule.get("matchDepNames") or rule.get("matchPackageNames") or [])
    })
    assert held, "no held dependencies found; this invariant would assert nothing"
    for dep in held:
        for update_type in ("patch", "minor", "digest"):
            assert resolved(dep, update_type) is False, (
                f"{dep} is written as a hold but resolves to automerge on {update_type}: "
                "its rule sits above the broad automerge rule, which overrides it"
            )


def test_renovate_never_edits_a_vendored_file() -> None:
    """A vendored file is owned by the standard, and editing it in place breaks STANDARD.lock.

    Renovate does not know that. Pointed at a locked path that carries a `# renovate:` annotation it
    opens a perfectly reasonable pin bump — and that PR then fails this repo's own drift check,
    because the file no longer matches the lock it was vendored under. The bump has to originate in
    the standard and arrive here as a re-vendor, so no manager may scan a locked path at all.
    """
    config = json.loads((_ROOT / ".renovaterc.json").read_text(encoding="utf-8"))
    vendored = set(json.loads((_ROOT / "STANDARD.lock").read_text(encoding="utf-8"))["files"])
    assert vendored, "no vendored files recorded; this invariant would assert nothing"

    scanned = [
        (index, path)
        for index, manager in enumerate(config.get("customManagers", []))
        for pattern in manager.get("managerFilePatterns", [])
        for path in sorted(vendored)
        if re.search(pattern.strip("/"), path)
    ]
    assert not scanned, (
        f"customManagers scan vendored files, whose bumps would fail the drift check: {scanned}"
    )


def test_every_inline_shellcheck_pin_matches_the_vendored_script() -> None:
    """Three copies of one pin, and they have to move together.

    `packaging/shellcheck-all.sh` is vendored, and two workflows deliberately do NOT call it: those
    jobs run on untrusted refs, where `actions/checkout` puts a FORK's copy of the script in the
    workspace, so the command has to be text this repo defines. The cost of that safety is a second
    and third copy of the image pin. Re-vendoring moves only the script, so nothing but this compares
    them — and a fork PR would then qualify against a different shellcheck than every other gate.
    """
    script = (_ROOT / "packaging" / "shellcheck-all.sh").read_text(encoding="utf-8")
    found = re.search(r'SHELLCHECK="([^"]+)"', script)
    assert found, "packaging/shellcheck-all.sh no longer pins SHELLCHECK"
    pin = found.group(1)

    for relative in (".forgejo/workflows/ci.yml", ".github/workflows/ci-pr.yml"):
        text = (_ROOT / relative).read_text(encoding="utf-8")
        assert f'SHELLCHECK="{pin}"' in text, (
            f"{relative} pins a different shellcheck image than the vendored script; the pin is "
            "owned by the standard, so re-vendor and update every inline copy together"
        )


def test_every_install_matrix_fetch_retries_and_stays_portable() -> None:
    """A single-attempt download makes the matrix only as reliable as one DNS lookup.

    Not hypothetical: a candidate's macOS leg died on `Could not resolve host: astral.sh`, and the
    infra-retry workflow rightly declined to rescue it — the failure was inside one of our own
    steps, which is exactly what that workflow refuses to launder into green.

    curl's `--retry` does NOT cover a name-resolution failure; only `--retry-all-errors` does, and
    that arrived in curl 7.71 while Rocky 8 ships 7.61 and rejects the option outright. So the
    container fetches retry in the shell, through `fetch.sh`, which every stage carries. The uv
    installer is the one exception: it runs on GitHub runners and Debian-family images where the
    flag exists — and it is written to a file first, because curl cannot rewind a pipe on retry.
    """
    dockerfile = _ROOT / "packaging" / "install-smoke.Dockerfile"
    text = dockerfile.read_text()

    assert (_ROOT / "packaging" / "fetch.sh").is_file(), "the retrying fetch helper is missing"

    # Every stage that can smoke can also fetch.
    smoke = text.count("COPY packaging/installed-smoke.sh /smoke.sh")
    helper = text.count("COPY packaging/fetch.sh /fetch")
    assert smoke == helper, f"{smoke} stages carry the smoke script but {helper} carry fetch.sh"

    raw = []
    for number, line in enumerate(text.splitlines(), 1):
        if not re.search(r"\bcurl\s+-[A-Za-z]+\s", line):
            continue
        if "astral.sh/uv/install.sh" in line:
            assert "--retry-all-errors" in line, f"install-smoke.Dockerfile:{number} cannot retry DNS"
            assert "| sh" not in line, f"install-smoke.Dockerfile:{number} pipes a retryable body to sh"
            continue
        raw.append(f"install-smoke.Dockerfile:{number}")
    assert not raw, f"fetches raw instead of through /fetch, so a DNS blip is fatal: {raw}"

    for name in (".github/workflows/install-matrix.yml", ".forgejo/workflows/install-matrix.yml"):
        path = _ROOT / name
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not re.search(r"\bcurl\s+-[A-Za-z]+\s", line):
                continue
            # These run on hosted runners only, never on the old curl the containers must
            # tolerate, so the flag that actually covers a failed DNS lookup is required here.
            assert "--retry-all-errors" in line, f"{path.name}:{number} cannot retry a DNS failure"
            assert re.search(r"--retry(?:\s|=)\d", line), f"{path.name}:{number} does not retry"
            assert "| sh" not in line, f"{path.name}:{number} pipes a retryable body to sh"


def test_the_macos_signing_step_survives_a_bad_minute_at_apples_timestamp_service() -> None:
    """`codesign` and `productsign` each request a trusted timestamp from Apple, `notarytool`
    uploads, and `stapler` downloads the ticket back. Every one reaches a network service on every
    invocation, and a release that cannot reach it dies with "A timestamp was expected but was not
    found."

    Dropping the timestamp is not an option: it is what keeps the signature verifiable past the
    certificate's expiry, and notarization refuses a build without one. So the retry wraps the call.
    Keychain grants (`security import -T /usr/bin/codesign`) name the tools without invoking them
    and are deliberately not matched here.
    """
    assert (_ROOT / "packaging" / "retry.sh").is_file(), "the retry helper is missing"

    workflow = (_ROOT / ".github" / "workflows" / "release-macos.yml").read_text()
    bare = []
    for number, line in enumerate(workflow.splitlines(), 1):
        if "security import" in line or line.lstrip().startswith("#"):
            continue
        if not re.search(r"\b(codesign|productsign|notarytool submit|stapler staple)\b", line):
            continue
        if "retry.sh" not in line:
            bare.append(f"{number}: {line.strip()[:60]}")
    assert not bare, f"reaches Apple without a retry, so one bad minute ends the release: {bare}"

    # The claim above is only worth making if the schedule backs it. Derived from the script and the
    # workflow rather than restated, so widening one without the other cannot pass quietly.
    helper = (_ROOT / "packaging" / "retry.sh").read_text()
    step = re.search(r"sleep \$\(\(n \* (\d+)\)\)", helper)
    assert step, "retry.sh no longer backs off between attempts"
    seconds = int(step.group(1))

    attempts = [int(n) for n in re.findall(r"retry\.sh (\d+) ", workflow)]
    assert attempts, "nothing in the release goes through the retry helper"
    for count in attempts:
        window = sum(seconds * n for n in range(1, count))
        assert window >= 120, (
            f"{count} attempts {seconds}s apart only covers {window}s; a minute-long outage wins"
        )


def test_every_install_matrix_caller_states_its_cache_ceiling() -> None:
    """The ceiling cannot be measured from inside the job, so the caller has to say it.

    The self-hosted runner reaches its Docker daemon over TCP, so a `df` in the job container
    measures a different filesystem entirely and would do it silently. The default is deliberately
    the small one, because a ceiling ABOVE the disk never prunes at all -- the failure that looks
    like nothing is wrong. Left implicit, a large runner would quietly keep the small default.
    """
    for name in (".forgejo/workflows/install-matrix.yml", ".github/workflows/install-matrix.yml"):
        path = _ROOT / name
        if not path.is_file():
            continue
        document = yaml.safe_load(path.read_text())
        for job, spec in document["jobs"].items():
            for step in spec.get("steps") or []:
                if "install-matrix-arch.sh" not in str(step.get("run", "")):
                    continue
                ceiling = (step.get("env") or {}).get("CACHE_CEILING_GB")
                assert ceiling is not None, (
                    f"{path.name}:{job} runs the matrix without stating a cache ceiling"
                )
                assert str(ceiling).isdigit(), f"{path.name}:{job} ceiling is not a number"


def test_ci_can_be_redispatched_without_an_unrelated_commit() -> None:
    """Forgejo has no rerun API.

    Without a dispatch trigger, a fault outside this repository - a runner losing the network, a
    hosted action failing to fetch its own manifest - leaves main red and the only way back is an
    unrelated commit, which is a lie in the history about what changed and why.
    """
    document = yaml.safe_load((_ROOT / ".forgejo" / "workflows" / "ci.yml").read_text())
    # PyYAML resolves a bare `on:` to the boolean True under YAML 1.1.
    triggers = document.get("on") or document.get(True) or {}
    assert "workflow_dispatch" in triggers, (
        f"ci.yml cannot be redispatched, so an infrastructure fault strands main: {sorted(triggers)}"
    )


def test_ci_supersedes_itself_and_publishing_never_does() -> None:
    """The two workflows want opposite concurrency, and getting either backwards is expensive.

    A second push to a branch makes the first CI answer irrelevant, so that run should be cancelled
    rather than left competing for runners. A publish is the reverse: cancelling one midway leaves a
    release half-written across three registries, and a group any wider than the tag lets a later
    tag displace an earlier tag's still-pending publication and leave it assetless.
    """
    def concurrency(name: str) -> dict:
        document = yaml.safe_load((_ROOT / ".forgejo" / "workflows" / name).read_text())
        value = document.get("concurrency")
        assert isinstance(value, dict), f"{name} declares no workflow-level concurrency: {value!r}"
        return value

    ci = concurrency("ci.yml")
    assert "github.ref" in str(ci["group"]), f"ci.yml does not group per ref: {ci['group']}"
    assert ci.get("cancel-in-progress") is True, "a superseded CI run should not keep a runner"

    publish = concurrency("publish.yml")
    # `github.ref`, not `ref_name`: the short name drops refs/heads and refs/tags alike, so a
    # branch sharing a tag's name shares its group - and publish.yml can be dispatched on one.
    assert re.search(r"github\.ref\s*}}", str(publish["group"])), (
        f"publish.yml groups wider than the exact ref, so one ref can displace another: "
        f"{publish['group']}"
    )
    assert publish.get("cancel-in-progress") is False, (
        "cancelling a publish leaves a release half-written across registries"
    )


def test_dependencies_that_move_together_are_reviewed_together() -> None:
    """The two manylinux builders are one upstream release under two names.

    They move to the same dated tag together, so reviewing them apart shows half the change - and a
    toolchain skew BETWEEN the arches is exactly the risk their hand-review exists to catch. It also
    doubles the rebase churn, since merging either rebases the other open PR and restarts its checks.

    The versioning regex belongs with it: without one, `latest` is offered as an upgrade over a
    dated tag, and the reviewer loses the version they need to judge the bump at all.
    """
    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    arches = {
        "quay.io/pypa/manylinux_2_28_x86_64",
        "quay.io/pypa/manylinux_2_28_aarch64",
    }
    grouped = [
        rule for rule in config.get("packageRules", [])
        if rule.get("groupName") and arches <= set(rule.get("matchDepNames") or [])
    ]
    assert grouped, "the manylinux arches are not grouped, so they arrive as separate reviews"
    assert len(grouped) == 1, f"more than one rule groups them: {[r['groupName'] for r in grouped]}"
    assert "regex:" in str(grouped[0].get("versioning", "")), (
        "without a dated-tag versioning scheme, `latest` is offered as an upgrade over a dated tag"
    )
    # Grouping alone does not stop a lone arch arriving: Renovate opens a branch as soon as ONE
    # update in the group exists, so whichever arch Quay published first would be reviewed by
    # itself - the exact skew the grouping is for.
    assert grouped[0].get("minimumGroupSize", 1) >= len(arches), (
        f"a branch can open with fewer than both arches: {grouped[0].get('minimumGroupSize')}"
    )

    # And the group size still only COUNTS updates; it does not compare their targets. If the pins
    # already lag a release and Quay publishes the next tag for one arch first, both arches have an
    # update and the branch opens with mismatched targets. Renovate cannot express "same tag", so
    # the pins themselves are what proves it: whatever lands, the two must agree.
    tags = re.findall(r"manylinux_2_28_(?:x86_64|aarch64):([0-9.]+-\d+)@sha256:", (_PINS).read_text())
    assert len(tags) == 2, f"expected both manylinux pins, found {len(tags)}"
    assert tags[0] == tags[1], f"the two arches are pinned to different builder releases: {tags}"

def test_homebrew_bottles_come_from_the_mirror_where_it_is_reachable() -> None:
    """Homebrew fetches bottles with its own HTTPS client, so neither dockerd's registry mirror nor
    BuildKit's applies to them - and they are the bulk of what these jobs download.

    It has to be ARTIFACT_DOMAIN. `HOMEBREW_BOTTLE_DOMAIN` makes Homebrew request a legacy flat file
    (`.../oniguruma-6.9.10.x86_64_linux.bottle.tar.gz`) that an OCI registry does not serve, so every
    bottle 404s and falls back upstream: configured, and mirroring nothing. ARTIFACT_DOMAIN rewrites
    only the scheme and host and keeps `/v2/homebrew/core/...`, which is why the registry serves that
    namespace at its root rather than under the usual per-upstream one.

    Self-hosted only. The hosted runners are not on that network, and pointing them at it buys a
    timeout before the fallback rather than a cache hit.
    """
    for name in ("packaging/install-smoke.Dockerfile", "packaging/homebrew-smoke.Dockerfile"):
        text = (_ROOT / name).read_text()
        assert "ARG BREW_MIRROR=" in text, f"{name} cannot receive a mirror"
        # Setting ARTIFACT_DOMAIN makes Homebrew drop the anonymous `Bearer QQ==` it would
        # otherwise send to ghcr.io, so a bottle the mirror cannot serve falls back and gets a 401
        # instead. That is what reverted this the first time, and nothing else in the file says so.
        assert "HOMEBREW_DOCKER_REGISTRY_TOKEN" in text, (
            f"{name} sets a mirror without restoring the anonymous ghcr credential, so any mirror "
            "miss 401s on fallback instead of downloading the bottle"
        )
        assert "HOMEBREW_CURL_RETRIES" in text, (
            f"{name} sets a mirror without raising the retry budget, so a cold entry expires "
            "before the registry finishes syncing it"
        )
        assert 'export HOMEBREW_ARTIFACT_DOMAIN="$BREW_MIRROR"' in text, (
            f"{name} must export ARTIFACT_DOMAIN when a mirror was given"
        )
        assert '[ -z "$BREW_MIRROR" ] ||' in text, (
            f"{name} must export nothing when no mirror was given"
        )
        assert "HOMEBREW_BOTTLE_DOMAIN" not in text, (
            f"{name} uses BOTTLE_DOMAIN, which an OCI registry cannot serve"
        )

    def mirrored(path: Path) -> bool:
        for job in yaml.safe_load(path.read_text())["jobs"].values():
            for step in job.get("steps") or []:
                if "BREW_MIRROR" in (step.get("env") or {}):
                    return True
                if "BREW_MIRROR=" in str(step.get("run", "")):
                    return True
        return False

    forgejo = _ROOT / ".forgejo" / "workflows"
    assert mirrored(forgejo / "install-matrix.yml"), "the self-hosted matrix does not mirror bottles"

    # Every self-hosted build of the formula smoke, found rather than listed: the release path
    # builds it a second time, and a fixed list would have passed while that one bypassed the
    # mirror to pull the same rust and llvm bottles again.
    for path in sorted(forgejo.glob("*.yml")):
        document = yaml.safe_load(path.read_text())
        for job, spec in (document.get("jobs") or {}).items():
            for step in spec.get("steps") or []:
                if "homebrew-smoke.Dockerfile" not in str(step.get("run", "")):
                    continue
                assert "BREW_MIRROR=" in str(step["run"]), (
                    f"{path.name}:{job} builds the formula smoke without the mirror"
                )
    assert not mirrored(_ROOT / ".github" / "workflows" / "install-matrix.yml"), (
        "a hosted runner was pointed at a mirror it cannot reach"
    )


def test_the_formula_is_built_on_every_macos_lane_the_library_is() -> None:
    """A formula proven on current arm64 only leaves the two axes that differ to a tag.

    This formula compiles C on the installing machine - `sunxi-tools` built with `make sunxi-fel`
    against libfdt and zlib - so a break can be specific to Intel or to the macOS floor. The bottle
    build covers those lanes but runs at tag time, and a tag is immutable by the time it reports.
    Whatever set `ci-macos.yml` holds the library to, the formula is held to as well.
    """
    def lanes(name: str) -> set[str]:
        document = yaml.safe_load((_ROOT / ".github" / "workflows" / name).read_text())
        job = next(iter(document["jobs"].values()))
        include = (((job.get("strategy") or {}).get("matrix") or {}).get("include")) or []
        return {entry["os"] for entry in include} or {job["runs-on"]}

    library, formula = lanes("ci-macos.yml"), lanes("formula-macos.yml")
    assert formula == library, (
        f"the formula is built on {sorted(formula)} while the library is tested on "
        f"{sorted(library)}; the difference is where a break reaches a tag unseen"
    )
