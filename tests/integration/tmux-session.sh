#!/usr/bin/env bash
# End-to-end checks of the tmux session wrapper against REAL tmux and a REAL terminal.
#
# The python suite drives a FAKE tmux, which cannot model the things this feature actually gets
# wrong: the alternate screen (a client restores the terminal on exit, erasing everything the run
# printed), the server's environment snapshot, or whether a wrapped run executes at all. 473 unit
# tests once passed while the wrapper destroyed every run it started — these cases are the ones
# that would have caught it.
#
# Each case runs the real binary under a real pty, in an isolated HOME and on a private tmux
# server. No hardware: only commands that stop before touching a robot.
set -euo pipefail

cd "$(dirname "$0")/../.."

if ! command -v tmux >/dev/null 2>&1; then
  echo "SKIP: tmux is not installed, so the session wrapper cannot be exercised"
  exit 0
fi

# A unix socket path caps near 104 bytes and macOS TMPDIR is already long, so the private server
# lives somewhere short. Never the default socket: the developer has real sessions on it.
RUNDIR="$(mktemp -d /private/tmp/dvi.XXXXXX 2>/dev/null || mktemp -d /tmp/dvi.XXXXXX)"
export TMUX_TMPDIR="$RUNDIR/t"
mkdir -p "$TMUX_TMPDIR"
HOME_DIR="$RUNDIR/home"
mkdir -p "$HOME_DIR"

cleanup() {
  tmux kill-server 2>/dev/null || true   # TMUX_TMPDIR-scoped: only ever this suite's own server
  rm -rf "$RUNDIR"
}
trap cleanup EXIT

TOOL=()
fail() {
  echo "FAIL: $1"
  # A bare message is useless on a machine you cannot poke at. Dump enough to diagnose from a CI
  # log alone: what the tool resolved to, and what the terminal actually showed.
  echo "--- environment ---"
  echo "tmux:  $(command -v tmux || echo MISSING) $(tmux -V 2>/dev/null || true)"
  echo "uv:    $(command -v uv || echo MISSING)"
  echo "tool:  ${TOOL[*]:-<unresolved>}"
  echo "python: $(python3 -V 2>&1)"
  for f in "$RUNDIR"/*.out; do
    [ -e "$f" ] || continue
    echo "--- $(basename "$f") (rc=$(head -1 "$f")) ---"
    tail -n +2 "$f" | tr -d '\000' | tail -40
  done
  exit 1
}
pass() { echo "ok: $1"; }

# Drive the real binary under a pty. Writes the child's exit status as the first line of the
# output file, then the raw terminal bytes — escape sequences included, because what the terminal
# is left showing is exactly what several of these cases are about.
cat > "$RUNDIR/drive.py" <<'PYEOF'
import os, pty, select, sys, time

out_path, timeout = sys.argv[1], float(sys.argv[2])
argv = sys.argv[sys.argv.index("--") + 1:]

pid, fd = pty.fork()
if pid == 0:
    os.execvp(argv[0], argv)

buf, deadline, status, timed_out = bytearray(), time.time() + timeout, None, False
while time.time() < deadline:
    if select.select([fd], [], [], 0.5)[0]:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if chunk:
            buf += chunk
    done, st = os.waitpid(pid, os.WNOHANG)
    if done == pid:
        status = st
        while select.select([fd], [], [], 0.5)[0]:      # drain what is left
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        break
if status is None:
    # The loop also leaves here on EOF, which a short run reaches before waitpid was ever polled —
    # mislabelling that as a 120s timeout sent one diagnosis entirely the wrong way. EOF can also
    # arrive while the child is still tearing down, so keep reaping until the REAL deadline rather
    # than letting one empty poll stand for "still running".
    while time.time() < deadline:
        done, st = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            status = st
            break
        time.sleep(0.05)
if status is None:
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass                      # it exited between the last poll and the kill
    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        status = 0
    timed_out = True
rc = "TIMEOUT after %.1fs" % timeout if timed_out else str(os.waitstatus_to_exitcode(status))
with open(out_path, "wb") as fh:
    fh.write((rc + "\n").encode() + bytes(buf))
PYEOF

# drive <name> <timeout> <env-assignment...> -- <argv...>
drive() {
  local name="$1" timeout="$2"; shift 2
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done
  shift
  env "${envs[@]}" HOME="$HOME_DIR" TMUX_TMPDIR="$TMUX_TMPDIR" TERM=xterm-256color \
      DREAME_NO_UPDATE_CHECK=1 DREAME_NO_UDEV_CHECK=1 \
      python3 "$RUNDIR/drive.py" "$RUNDIR/$name.out" "$timeout" -- "$@" >/dev/null 2>&1
}
rc_of()   { head -1 "$RUNDIR/$1.out"; }
text_of() { tail -n +2 "$RUNDIR/$1.out"; }
sessions() { tmux ls 2>/dev/null | grep -c dreame-valetudo || true; }

# How to invoke the tool — and it must be THIS TREE, not whatever is on PATH. A released copy
# installed by Homebrew or the .pkg shadows the source, and the suite would then quietly certify a
# build that does not contain the code under test. `uv run` is project-scoped by construction, so
# it wins; the bare console script is for CI, where it is an editable install of this tree.
# `python -m dreame_valetudo` is deliberately not an option: its argv[0] is a non-executable
# __main__.py, which tmux could never exec.
tree_of_installed_script() {
  # Ask the script's OWN interpreter where it imports the package from. A frozen release binary
  # (.pkg/brew) has no usable shebang, so this prints nothing and the caller rejects it.
  local script interp
  script="$(command -v dreame-valetudo)" || return 1
  interp="$(head -1 "$script")"
  interp="${interp#\#!}"
  [ -x "${interp%% *}" ] || return 1
  # From / and with -P, so the current directory is NOT on sys.path. Probing from the repo made
  # every interpreter "find" the tree and the check passed for a release install it should reject.
  (cd / && "${interp%% *}" -P -c 'import dreame_valetudo, pathlib
print(pathlib.Path(dreame_valetudo.__file__).resolve().parent.parent)' 2>/dev/null)
}

if command -v uv >/dev/null 2>&1; then
  TOOL=(uv run dreame-valetudo)
elif command -v dreame-valetudo >/dev/null 2>&1; then
  installed_tree="$(tree_of_installed_script || true)"
  [ "$installed_tree" = "$PWD" ] ||
    fail "the dreame-valetudo on PATH is not this checkout (it resolves to '${installed_tree:-a frozen release}'), so this suite would certify code that is not under test — install this tree with 'pip install -e .' or make uv available"
  TOOL=(dreame-valetudo)
else
  # Deliberately a failure, not a skip: skipping here would hide a broken wrapper behind a missing
  # toolchain, which is exactly how this feature stayed broken through 473 green tests.
  fail "neither an installed dreame-valetudo nor uv is available to run the tool"
fi

# --- 1. a wrapped run really executes, in the workspace it was told to use ------------------
W1="$RUNDIR/work1"
drive one 120 "DREAME_WORK=$W1" -- "${TOOL[@]}" status
[ "$(rc_of one)" = "0" ] || fail "a wrapped 'status' did not exit 0 (got $(rc_of one))"
[ -d "$W1/logs" ] || fail "the run never created its workspace at the DREAME_WORK it was given"
ls "$W1"/logs/run-*.log >/dev/null 2>&1 || fail "the run inside the session wrote no log"
[ -f "$W1/.lock" ] || fail "the run inside the session took no workspace lock"
pass "a wrapped run executes and uses the DREAME_WORK it was given"

# --- 2. its output survives the session ending ---------------------------------------------
# A tmux client draws on the alternate screen, so ending the session restores the terminal and
# erases everything the run printed. What the user is left looking at is what matters.
python3 - "$RUNDIR/one.out" <<'PYEOF' || fail "the run's output did not survive the session ending"
import sys
raw = open(sys.argv[1], "rb").read()
after = raw.rsplit(b"\x1b[?1049l", 1)          # everything after the LAST alternate-screen exit
sys.exit(0 if len(after) == 2 and b"No robots yet" in after[1] else 1)
PYEOF
pass "the run's output is still on the screen after the session ends"

# --- 3. the exit status is the RUN's, not the tmux client's ---------------------------------
drive fail1 120 "DREAME_WORK=$RUNDIR/work-bad" "DREAME_MODEL=no-such-model" -- "${TOOL[@]}" status
[ "$(rc_of fail1)" = "1" ] || fail "a failing wrapped run reported $(rc_of fail1), not the run's 1"
text_of fail1 | grep -q "no-such-model" || fail "the failing run's error was not shown to the user"
pass "a failing wrapped run returns its own status and shows its error"

# --- 4. an install path containing a space still starts -------------------------------------
# tmux runs a SINGLE trailing argument through /bin/sh, and a bare invocation is the one form that
# produces exactly one — so from a spaced path the binary silently never started.
SPACED="$RUNDIR/robot stuff"
mkdir -p "$SPACED"
cat > "$SPACED/dreame-valetudo" <<EOF
#!/usr/bin/env bash
exec ${TOOL[*]} "\$@"
EOF
chmod +x "$SPACED/dreame-valetudo"
drive spaced 120 "DREAME_WORK=$RUNDIR/work-sp" -- "$SPACED/dreame-valetudo" status
[ "$(rc_of spaced)" = "0" ] || fail "a bare run from a path with a space did not start"
[ -d "$RUNDIR/work-sp/logs" ] || fail "the spaced-path run never reached its workspace"
pass "a bare invocation from a path containing a space still starts"

# --- 5. a pure command does not disturb a live run ------------------------------------------
# A bare run with no robots stops at the naming prompt and sits there, which is a live session.
W2="$RUNDIR/work2"
drive live 40 "DREAME_WORK=$W2" -- "${TOOL[@]}" &
LIVE=$!
for _ in $(seq 1 80); do [ "$(sessions)" -ge 1 ] && break; sleep 0.5; done
[ "$(sessions)" -ge 1 ] || fail "no session appeared for a run left waiting at a prompt"

drive ver 90 "DREAME_WORK=$W2" -- "${TOOL[@]}" --version
[ "$(rc_of ver)" = "0" ] || fail "--version during a live run exited $(rc_of ver)"
text_of ver | grep -q "dreame-valetudo" || fail "--version printed no version"
if text_of ver | grep -qi "already in progress"; then
  fail "--version was offered the menu whose second option closes a live run"
fi
pass "--version during a live run prints a version and is offered nothing"

# --- 6. a second workspace is an independent session, not the same one ----------------------
# The mid-flash guard cross-checks a PER-WORKSPACE lock, so a shared session name would leave it
# unable to see the run it is guarding.
W3="$RUNDIR/work3"
drive live2 40 "DREAME_WORK=$W3" -- "${TOOL[@]}" &
LIVE2=$!
for _ in $(seq 1 80); do [ "$(sessions)" -ge 2 ] && break; sleep 0.5; done
[ "$(sessions)" -ge 2 ] ||
  fail "two workspaces shared one session ($(sessions) live), so the mid-flash guard is blind"
pass "two workspaces get two independent sessions"

wait "$LIVE" 2>/dev/null || true
wait "$LIVE2" 2>/dev/null || true

echo "ALL PASS: tmux session wrapper (real tmux $(tmux -V | awk '{print $2}'))"
