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

fail() { echo "FAIL: $1"; exit 1; }
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

buf, deadline, status = bytearray(), time.time() + timeout, None
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
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    rc = "TIMEOUT"
else:
    rc = str(os.waitstatus_to_exitcode(status))
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
      DREAME_NO_UPDATE_CHECK=1 \
      python3 "$RUNDIR/drive.py" "$RUNDIR/$name.out" "$timeout" -- "$@" >/dev/null 2>&1
}
rc_of()   { head -1 "$RUNDIR/$1.out"; }
text_of() { tail -n +2 "$RUNDIR/$1.out"; }
sessions() { tmux ls 2>/dev/null | grep -c dreame-valetudo || true; }

TOOL=(uv run dreame-valetudo)

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
exec $(command -v uv) run --project "$PWD" dreame-valetudo "\$@"
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
