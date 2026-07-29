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
_LINUX_PACKAGES = _ROOT / "packaging" / "test-linux-packages.sh"
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
    ):
        assert path in text
    assert "for required in" in text
    assert 'grep -Fq "$required"' in text


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
    assert "ruff check dreame_valetudo libexec tests/python" in macos
    assert "mypy" in macos
    assert "pytest -q tests/python" in macos
    assert "tests/integration/*.sh" in macos
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
    assert 'depName=python-3.11-floor packageName=python/cpython' in floor_job
    assert 'python-version: "3.11.0"' in floor_job
    assert 'pip install "pytest==$PYTEST" -e .' in floor_job
    assert "pytest -q tests/python" in floor_job

    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    floor_rules = [
        rule
        for rule in config["packageRules"]
        if "python-3.11-floor" in rule.get("matchDepNames", [])
    ]
    assert len(floor_rules) == 1
    assert floor_rules[0]["allowedVersions"] == r"/^3\.11\.0$/"


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
        assert "bump by hand with each CPython minor" in formula

    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    homebrew_rules = [
        rule for rule in config["packageRules"]
        if "python/cpython" in rule.get("matchDepNames", []) and "prBodyNotes" in rule
    ]
    assert len(homebrew_rules) == 1
    assert any("packaging/homebrew/*.rb" in note for note in homebrew_rules[0]["prBodyNotes"])


def test_ci_and_both_release_gates_use_one_pinned_toolchain() -> None:
    ci = _CI.read_text()
    pins = {
        name: re.search(rf'{name}="([^"]+)"', ci).group(1)  # type: ignore[union-attr]
        for name in ("RUFF", "MYPY", "PYTEST", "SHELLCHECK")
    }
    for workflow in (_CI, _RELEASE, _PRERELEASE):
        text = workflow.read_text()
        for name, value in pins.items():
            assert f'{name}="{value}"' in text, workflow
        assert "packaging/*.sh tests/integration/*.sh docs/research/tools/*.sh" in text, workflow
        assert "apt-get install -y shellcheck" not in text, workflow
        assert '-v "$PWD:' not in text, workflow
        assert 'docker create -w /work "$SHELLCHECK"' in text, workflow
        assert 'docker cp . "$cid":/work' in text, workflow


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


def test_stable_release_uses_renovate_pinned_uv_and_limits_the_lock_diff() -> None:
    text = _RELEASE.read_text()
    sync = text[text.index("      - name: Sync uv.lock") :]
    assert "# renovate: datasource=pypi depName=uv" in sync
    assert re.search(r'UV="\d+\.\d+\.\d+"', sync)
    assert 'python -m pip install "uv==$UV"' in sync
    assert "uv.lock.before" in sync
    assert "after != expected" in sync


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
    step = text[text.index("      - name: Commit, tag, push") :]
    assert "push --atomic origin" in step
    assert '"HEAD:${{ github.ref_name }}" "${TAG}"' in step


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
    assert rules[0]["matchUpdateTypes"] == ["patch", "minor", "major"]
    assert rules[0]["automerge"] is False
    assert "BUNDLE_PYTHON_SHA256" in rules[0]["prBodyNotes"][0]


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
    floors = {
        name: rule["allowedVersions"]
        for rule in config["packageRules"]
        for name in rule.get("matchDepNames", [])
        if name
        in {
            "debian-12-compat",
            "ubuntu-22.04-compat",
            "fedora-43-compat",
            "rocky-8-compat",
            "rocky-9-compat",
        }
    }
    assert floors == {
        "debian-12-compat": "/^12-slim$/",
        "ubuntu-22.04-compat": r"/^22\.04$/",
        "fedora-43-compat": "/^43$/",
        "rocky-8-compat": "/^8$/",
        "rocky-9-compat": "/^9$/",
    }


def test_readme_source_install_names_every_host_runtime_dependency() -> None:
    source = _README.read_text().split("### From source", 1)[1].split("## What you need", 1)[0]
    for dependency in ("libusb", "curl", "tmux", "OpenSSH", "tar", "zip", "unzip"):
        assert dependency in source


def test_readme_covers_rpm_candidate_switching_and_manual_removal() -> None:
    readme = _README.read_text()
    assert "sudo dnf install ./dreame-valetudo.<arch>.rpm" in readme
    assert "sudo dnf downgrade ./dreame-valetudo.<arch>.rpm" in readme
    assert "sudo zypper install --oldpackage ./dreame-valetudo.<arch>.rpm" in readme
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
