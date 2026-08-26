#!/usr/bin/env bash
# Install the exact CI-built packages in real distro userspaces, upgrade them, exercise the frozen
# entry point and bundled helpers, then prove uninstall removes only package-owned files.
set -euo pipefail

fail() {
  echo "package smoke FAIL: $*" >&2
  exit 1
}

installed_smoke() {
  local manager=$1 old_package=$2 new_package=${3:-}
  local old_version new_version installed runtime fastboot_out fastboot_rc upgrade=true

  if [ -z "$new_package" ]; then
    new_package=$old_package
    upgrade=false
  fi

  export HOME=/tmp/dreame-valetudo-package-smoke
  mkdir -p "$HOME/dreame-valetudo/backups"
  printf 'keep\n' > "$HOME/dreame-valetudo/backups/uninstall-must-preserve"

  case "$manager" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq
      old_version=$(dpkg-deb -f "$old_package" Version)
      new_version=$(dpkg-deb -f "$new_package" Version)
      if [ "$upgrade" = true ]; then
        dpkg --compare-versions "$new_version" gt "$old_version" \
          || fail "$new_version is not newer than $old_version"
      fi
      apt-get install -y -qq "$old_package"
      installed=$(dpkg-query -W -f='${Version}' dreame-valetudo)
      [ "$installed" = "$old_version" ] || fail "apt installed $installed, expected $old_version"
      if [ "$upgrade" = true ]; then
        apt-get install -y -qq "$new_package"
        installed=$(dpkg-query -W -f='${Version}' dreame-valetudo)
        [ "$installed" = "$new_version" ] \
          || fail "apt upgraded to $installed, expected $new_version"
      fi
      ;;
    dnf)
      old_version=$(rpm -qp --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}' "$old_package")
      new_version=$(rpm -qp --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}' "$new_package")
      [ "$upgrade" = false ] || [ "$old_version" != "$new_version" ] \
        || fail "old and new RPM metadata are identical"
      dnf install -y --nogpgcheck "$old_package"
      installed=$(rpm -q --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}' dreame-valetudo)
      [ "$installed" = "$old_version" ] || fail "dnf installed $installed, expected $old_version"
      if [ "$upgrade" = true ]; then
        dnf upgrade -y --nogpgcheck "$new_package"
        installed=$(rpm -q --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}' dreame-valetudo)
        [ "$installed" = "$new_version" ] \
          || fail "dnf upgraded to $installed, expected $new_version"
      fi
      ;;
    zypper)
      old_version=$(rpm -qp --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}' "$old_package")
      new_version=$(rpm -qp --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}' "$new_package")
      [ "$upgrade" = false ] || [ "$old_version" != "$new_version" ] \
        || fail "old and new RPM metadata are identical"
      zypper --non-interactive --no-gpg-checks install "$old_package"
      installed=$(rpm -q --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}' dreame-valetudo)
      [ "$installed" = "$old_version" ] \
        || fail "zypper installed $installed, expected $old_version"
      if [ "$upgrade" = true ]; then
        zypper --non-interactive --no-gpg-checks install "$new_package"
        installed=$(rpm -q --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}' dreame-valetudo)
        [ "$installed" = "$new_version" ] \
          || fail "zypper upgraded to $installed, expected $new_version"
      fi
      ;;
    *)
      fail "unknown package manager $manager"
      ;;
  esac

  # Everything below describes the FINAL install: an upgrade case has already replaced whatever
  # layout it started from.
  test -L /usr/bin/dreame-valetudo
  test -x /usr/bin/dreame-valetudo
  test -x /usr/lib/dreame-valetudo/app/dreame-valetudo
  test -L /usr/lib/dreame-valetudo/dreame-fastboot
  test -x /usr/lib/dreame-valetudo/dreame-fastboot
  test -x /usr/lib/dreame-valetudo/fastboot/dreame-fastboot
  test -x /usr/lib/dreame-valetudo/sunxi-fel
  test -f /usr/lib/udev/rules.d/99-dreame-valetudo.rules
  # Data frozen into the bundle that no phase below reads. A tree that lost it still installs,
  # still reports its version and still passes the host smoke, then fails on real hardware.
  test -f /usr/lib/dreame-valetudo/app/_internal/libexec/fastboot-libusb.py
  test -d /usr/lib/dreame-valetudo/app/_internal/libexec/dustbuilder-forms
  test -f /usr/lib/dreame-valetudo/app/_internal/dreame_valetudo/CHANGELOG.md
  command -v tmux >/dev/null

  runtime=$(dreame-valetudo version)
  [[ "$runtime" =~ ^[[:space:]]*dreame-valetudo\ [0-9] ]] \
    || fail "unexpected version output: $runtime"
  DREAME_NO_TMUX=1 DREAME_NO_UPDATE_CHECK=1 DREAME_NO_UDEV_CHECK=1 \
    DREAME_BENCH_BUILD="${runtime##* }" DREAME_BENCH_CHANNEL="$manager-ci" \
    dreame-valetudo bench run host-smoke --campaign package-ci

  set +e
  fastboot_out=$(/usr/lib/dreame-valetudo/dreame-fastboot devices 2>&1)
  fastboot_rc=$?
  set -e
  if (( fastboot_rc > 1 )) \
      || { (( fastboot_rc == 1 )) && [[ -n "$fastboot_out" ]]; } \
      || grep -Eqi 'Traceback|NoBackendError|no libusb backend|library not loaded' \
           <<<"$fastboot_out"; then
    fail "bundled fastboot client did not load cleanly (rc=$fastboot_rc): $fastboot_out"
  fi
  if ldd /usr/lib/dreame-valetudo/sunxi-fel 2>&1 | grep -q 'not found'; then
    fail "sunxi-fel has an unresolved runtime library"
  fi

  case "$manager" in
    apt) apt-get remove -y -qq dreame-valetudo ;;
    dnf) dnf remove -y dreame-valetudo ;;
    zypper) zypper --non-interactive remove dreame-valetudo ;;
  esac

  # -e is FALSE for a dangling symlink, so a package-owned launcher left pointing at a tree that
  # was removed would slip through it unnoticed; -L is what catches that.
  for path in /usr/bin/dreame-valetudo \
              /usr/lib/dreame-valetudo/dreame-fastboot \
              /usr/lib/dreame-valetudo/sunxi-fel \
              /usr/lib/udev/rules.d/99-dreame-valetudo.rules; do
    { [ ! -e "$path" ] && [ ! -L "$path" ]; } || fail "uninstall left $path behind"
  done
  # The trees are the bulk of the install. Removing only the launchers above would satisfy every
  # check so far and leave both _internal directories on disk.
  [ ! -d /usr/lib/dreame-valetudo/app ] || fail "uninstall left the main bundle tree behind"
  [ ! -d /usr/lib/dreame-valetudo/fastboot ] || fail "uninstall left the client bundle tree behind"
  test -f "$HOME/dreame-valetudo/backups/uninstall-must-preserve"
  if [ "$upgrade" = true ]; then
    echo "package smoke PASS: $manager ($old_version -> $new_version -> removed)"
  else
    echo "package smoke PASS: $manager ($old_version -> removed)"
  fi
}

if [ "${1:-}" = "--inside" ]; then
  [ "$#" -eq 4 ] || fail "inside usage: --inside <apt|dnf|zypper> <old> <new>"
  installed_smoke "$2" "$3" "$4"
  exit 0
fi

if [ "${1:-}" = "--inside-single" ]; then
  [ "$#" -eq 3 ] || fail "inside usage: --inside-single <apt|dnf|zypper> <package>"
  installed_smoke "$2" "$3"
  exit 0
fi

[ "$#" -eq 6 ] \
  || fail "usage: $0 <old.deb> <new.deb> <old.rpm> <new.rpm> <legacy.deb> <legacy.rpm>"
: "${DEBIAN_FLOOR_IMAGE:?set DEBIAN_FLOOR_IMAGE to a pinned image}"
: "${DEBIAN_CURRENT_IMAGE:?set DEBIAN_CURRENT_IMAGE to a pinned image}"
: "${UBUNTU_FLOOR_IMAGE:?set UBUNTU_FLOOR_IMAGE to a pinned image}"
: "${UBUNTU_CURRENT_IMAGE:?set UBUNTU_CURRENT_IMAGE to a pinned image}"
: "${FEDORA_FLOOR_IMAGE:?set FEDORA_FLOOR_IMAGE to a pinned image}"
: "${FEDORA_CURRENT_IMAGE:?set FEDORA_CURRENT_IMAGE to a pinned image}"
: "${RHEL_FLOOR_IMAGE:?set RHEL_FLOOR_IMAGE to a pinned image}"
: "${RHEL_CURRENT_IMAGE:?set RHEL_CURRENT_IMAGE to a pinned image}"
: "${OPENSUSE_IMAGE:?set OPENSUSE_IMAGE to a pinned image}"

old_deb=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
new_deb=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
old_rpm=$(cd "$(dirname "$3")" && pwd)/$(basename "$3")
new_rpm=$(cd "$(dirname "$4")" && pwd)/$(basename "$4")
legacy_deb=$(cd "$(dirname "$5")" && pwd)/$(basename "$5")
legacy_rpm=$(cd "$(dirname "$6")" && pwd)/$(basename "$6")
for package in "$old_deb" "$new_deb" "$old_rpm" "$new_rpm" "$legacy_deb" "$legacy_rpm"; do
  [ -s "$package" ] || fail "missing or empty package: $package"
done

containers=()
images=()
cleanup() {
  if ((${#containers[@]})); then
    docker rm -f "${containers[@]}" >/dev/null 2>&1 || true
  fi
  if ((${#images[@]})); then
    docker image rm "${images[@]}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

run_case() {
  local label=$1 image=$2 manager=$3 old_package=$4 new_package=$5 cid extension
  echo "== package smoke: $label =="
  extension=rpm
  [ "$manager" = apt ] && extension=deb
  cid=$(docker create --platform linux/amd64 "$image" \
    /bin/bash /tmp/test-linux-packages.sh --inside "$manager" \
    "/tmp/old-package.$extension" "/tmp/new-package.$extension")
  containers+=("$cid")
  images+=("$image")
  docker cp "$0" "$cid":/tmp/test-linux-packages.sh
  docker cp "$old_package" "$cid":"/tmp/old-package.$extension"
  docker cp "$new_package" "$cid":"/tmp/new-package.$extension"
  docker start -a "$cid"
  docker rm "$cid" >/dev/null
  docker image rm "$image" >/dev/null 2>&1 || true
}

# The pre-onedir installed layout is a package-manager transition of its own: two regular files
# become symlinks and two bundle directories appear. Every other case here upgrades one onedir
# package to another, which cannot see that. The legacy fixture is never executed — only its shape
# is under test — so it is packaged from the same binaries rather than from an archived release.
run_case "Debian 13 (upgrade from the pre-onedir layout)" \
  "$DEBIAN_CURRENT_IMAGE" apt "$legacy_deb" "$new_deb"
run_case "Fedora 44 (upgrade from the pre-onedir layout)" \
  "$FEDORA_CURRENT_IMAGE" dnf "$legacy_rpm" "$new_rpm"
run_case "Debian 12 (oldstable floor)" "$DEBIAN_FLOOR_IMAGE" apt "$old_deb" "$new_deb"
run_case "Debian 13 (current stable)" "$DEBIAN_CURRENT_IMAGE" apt "$old_deb" "$new_deb"
run_case "Ubuntu 22.04 (glibc floor)" "$UBUNTU_FLOOR_IMAGE" apt "$old_deb" "$new_deb"
run_case "Ubuntu 26.04 (current LTS)" "$UBUNTU_CURRENT_IMAGE" apt "$old_deb" "$new_deb"
run_case "Fedora 43 (supported floor)" "$FEDORA_FLOOR_IMAGE" dnf "$old_rpm" "$new_rpm"
run_case "Fedora 44 (current)" "$FEDORA_CURRENT_IMAGE" dnf "$old_rpm" "$new_rpm"
run_case "Rocky Linux 8 (RHEL-compatible glibc floor)" "$RHEL_FLOOR_IMAGE" dnf "$old_rpm" "$new_rpm"
run_case "Rocky Linux 10 (RHEL-compatible current)" "$RHEL_CURRENT_IMAGE" dnf "$old_rpm" "$new_rpm"
run_case "openSUSE Leap 16.0" "$OPENSUSE_IMAGE" zypper "$old_rpm" "$new_rpm"
