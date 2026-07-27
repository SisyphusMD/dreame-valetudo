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
TMUX_CMD=(tmux -L dreame-valetudo)

# A unix socket path caps near 104 bytes and macOS TMPDIR is already long, so the private server
# lives somewhere short. Never the default socket: the developer has real sessions on it.
RUNDIR="$(mktemp -d /private/tmp/dvi.XXXXXX 2>/dev/null || mktemp -d /tmp/dvi.XXXXXX)"
export TMUX_TMPDIR="$RUNDIR/t"
mkdir -p "$TMUX_TMPDIR"
HOME_DIR="$RUNDIR/home"
mkdir -p "$HOME_DIR"

cleanup() {
  sessions="$("${TMUX_CMD[@]}" list-sessions -F '#S' 2>/dev/null || true)"
  while IFS= read -r session; do
    [ -n "$session" ] && "${TMUX_CMD[@]}" kill-session -t "$session" 2>/dev/null || true
  done <<< "$sessions"
  rm -rf "$RUNDIR"
}
trap cleanup EXIT

TOOL=()
fail() {
  echo "FAIL: $1"
  # A bare message is useless on a machine you cannot poke at. Dump enough to diagnose from a CI
  # log alone: what the tool resolved to, and what the terminal actually showed.
  echo "--- environment ---"
  echo "tmux:  $(command -v tmux || echo MISSING) $("${TMUX_CMD[@]}" -V 2>/dev/null || true)"
  # Most assertions here count sessions, and a bare "expected 1" cannot distinguish a run that died
  # from one left behind by the case before it.
  echo "sessions now:"
  "${TMUX_CMD[@]}" list-sessions -F '  #S attached=#{session_attached} dead=#{pane_dead}' \
    2>/dev/null || echo "  (none)"
  # A surviving session means a process is still alive in the pane. What it is WAITING on is the
  # whole diagnosis, and it is only visible on the pane itself.
  for s in $("${TMUX_CMD[@]}" list-sessions -F '#S' 2>/dev/null || true); do
    echo "--- pane of $s ---"
    "${TMUX_CMD[@]}" capture-pane -p -t "$s" 2>/dev/null | grep -v '^$' | tail -12
  done
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
input_file = os.environ.get("DREAME_TEST_INPUT_FILE")
# The end-of-run question is answered for cases that only need the run to finish. A case ABOUT that
# question must opt out, or the answer arrives before the case can act and the run ends underneath
# it — which then reads as the run dying on its own.
hold = os.environ.get("DREAME_TEST_HOLD_PROMPT") == "1"
sent = False
while time.time() < deadline:
    if select.select([fd], [], [], 0.5)[0]:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if chunk:
            buf += chunk
            # Matched on the confirm's own [y/N] marker rather than its words: the end-of-run
            # question is phrased from what the run was doing, so pinning any one wording here
            # silently stops answering the moment that wording changes, and the case then fails as
            # a timeout somewhere unrelated.
            if (not input_file and not hold and b"[y/N]" in buf and not sent):
                os.write(fd, b"\n")
                sent = True
    if input_file and not sent and os.path.exists(input_file):
        with open(input_file, "rb") as fh:
            os.write(fd, fh.read())
        sent = True
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
sessions() { "${TMUX_CMD[@]}" ls 2>/dev/null | grep -c dreame-valetudo || true; }

# What the user is LEFT LOOKING AT, for a run whose output must reach the terminal.
#
# A tmux client draws on the alternate screen, so leaving it restores the terminal and erases
# everything the run printed — output that lands before that point is gone. But a command that only
# reports can finish before the client ever attaches, and then no screen was entered and there is
# nothing to survive. Both shapes are correct; only the first constrains WHERE the output lands.
output_reached_terminal() {
  python3 - "$1" "$2" <<'PYEOF'
import sys
raw = open(sys.argv[1], "rb").read()
want = sys.argv[2].encode()

def no(why):
    # Which condition failed is the whole diagnosis, and the caller only dumps a tail of the file.
    print(f"    output check: {why}", file=sys.stderr)
    sys.exit(1)

if want not in raw:
    no(f"{want!r} never reached the terminal at all")
# tmux's own diagnostics must never surface. The client has to inherit stdout to draw, so a session
# that ends while this process is attaching can only be kept quiet by discarding its stderr.
if b"no sessions" in raw:
    no("tmux printed 'no sessions' on the user's terminal")
if b"\x1b[?1049l" not in raw:
    sys.exit(0)                                    # never attached: printed plainly, nothing to erase
after = raw.rsplit(b"\x1b[?1049l", 1)              # everything after the LAST alternate-screen exit
if len(after) != 2 or want not in after[1]:
    no("the alternate screen was left, and the output did not survive it")
# tmux writes "[exited]" to STDOUT when the session it is attached to is destroyed, and it cannot be
# filtered without taking away the stdout the client draws on — so it is ERASED afterwards. The bytes
# therefore still contain it; what matters is that the erase follows, so the terminal never shows it.
# Asserting its absence could never pass.
tail = after[1]
if b"[exited]" in tail:
    if b"\x1b[1A\x1b[2K" not in tail[tail.index(b"[exited]"):]:
        no("tmux's [exited] marker was left on the terminal")
    sys.exit(0)
sys.exit(0)
PYEOF
}

# Teardown is asynchronous: a client exits the moment it detaches, but the run process inside the
# pane still has to finish before tmux destroys the session. Counting straight after waiting on the
# client therefore sees the PREVIOUS case's session and blames the current one.
wait_sessions() {
  local want="$1"
  for _ in $(seq 1 40); do
    [ "$(sessions)" -eq "$want" ] && return 0
    sleep 0.25
  done
  return 1
}
session_for_work() {
  python3 - "$1" <<'PYEOF'
import hashlib, pathlib, sys
base = pathlib.Path(sys.argv[1]).resolve()
print(f"dreame-valetudo-{hashlib.sha256(str(base).encode()).hexdigest()[:8]}")
PYEOF
}

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
output_reached_terminal "$RUNDIR/one.out" "No robots yet" ||
  fail "the run's output did not survive the session ending"
pass "the run's output survives the session ending without a client exit marker"

# --- 3. a run that never chose a robot ends without asking about one ---------------------------
# `status` reports and exits without binding a robot, so there is nothing to continue and nothing to
# follow it. The end-of-run question is only for a run that engaged with a robot; asking here once
# produced "Set up another robot?" for a workspace that had none. What must survive is the OUTPUT,
# and that is the captured pane replayed onto the real terminal — not a keypress holding the screen.
W_HOLD="$RUNDIR/work-hold"
drive hold 120 "DREAME_WORK=$W_HOLD" -- "${TOOL[@]}" status
[ "$(rc_of hold)" = "0" ] || fail "an informational run returned $(rc_of hold)"
text_of hold | grep -q "Set up another robot?" &&
  fail "a run that never chose a robot still asked about setting up another"
wait_sessions 0 || fail "the session outlived a run that had nothing to continue"
output_reached_terminal "$RUNDIR/hold.out" "No robots yet" ||
  fail "the restored terminal did not contain the finished run's output"
pass "a run that chose no robot closes without a question, output intact"

# --- 4. the exit status is the RUN's, not the tmux client's ---------------------------------
drive fail1 120 "DREAME_WORK=$RUNDIR/work-bad" "DREAME_MODEL=no-such-model" -- "${TOOL[@]}" status
[ "$(rc_of fail1)" = "1" ] || fail "a failing wrapped run reported $(rc_of fail1), not the run's 1"
text_of fail1 | grep -q "no-such-model" || fail "the failing run's error was not shown to the user"
pass "a failing wrapped run returns its own status and shows its error"

# --- 5. Ctrl+C returns to the same wrapped run ----------------------------------------------
W_INT="$RUNDIR/work-interrupt"
INT_USB="$RUNDIR/pyusb-stub"
mkdir -p "$INT_USB/usb"
printf '' > "$INT_USB/usb/__init__.py"
printf '' > "$INT_USB/usb/core.py"
REAL_PYTHON="$(command -v python3)"
INT_PYTHON="$RUNDIR/python-with-pyusb"
cat > "$INT_PYTHON" <<EOF
#!/bin/sh
PYTHONPATH='$INT_USB' exec '$REAL_PYTHON' "\$@"
EOF
chmod +x "$INT_PYTHON"
drive interrupted 40 "DREAME_WORK=$W_INT" "DREAME_MODEL=x40-ultra" \
  "DREAME_TEST_HOLD_PROMPT=1" "DREAME_PYTHON=$INT_PYTHON" \
  -- "${TOOL[@]}" &
INT_CLIENT=$!
INT_SESSION="$(session_for_work "$W_INT")"
for _ in $(seq 1 80); do
  "${TMUX_CMD[@]}" capture-pane -p -t "$INT_SESSION" 2>/dev/null | grep -q "Name for this robot" && break
  sleep 0.25
done
# Name it BEFORE interrupting. Only a run that got as far as a robot has anything to resume, so an
# interrupt at the name prompt itself is meant to close without a question — testing the resume path
# from there would assert the opposite of the intended behaviour.
"${TMUX_CMD[@]}" send-keys -t "$INT_SESSION" "Bench Bot" Enter
# Force a usable transport even on the minimal CI runner, then interrupt at a live prompt. A line
# such as "The road ahead" remains in scrollback after the run has moved on and is not a usable
# synchronization point.
ready=0
for _ in $(seq 1 80); do
  if "${TMUX_CMD[@]}" capture-pane -p -t "$INT_SESSION" 2>/dev/null |
      grep -q "Ready to start watching for the robot"; then
    ready=1
    break
  fi
  sleep 0.25
done
[ "$ready" = 1 ] || fail "the interrupt case never reached its live readiness prompt"
"${TMUX_CMD[@]}" send-keys -t "$INT_SESSION" C-c
for _ in $(seq 1 60); do
  "${TMUX_CMD[@]}" capture-pane -p -t "$INT_SESSION" 2>/dev/null | grep -qE "\[y/N\]" && break
  sleep 0.25
done
[ "$(sessions)" -eq 1 ] || fail "Ctrl+C did not leave exactly one resumable run"
"${TMUX_CMD[@]}" detach-client -s "$INT_SESSION"
wait "$INT_CLIENT" 2>/dev/null || true
# The run must outlive the client that was watching it. Asserted separately from the rejoin below,
# because a session that died here makes the next invocation CREATE one — which then looks like a
# successful rejoin on every check that only counts sessions.
wait_sessions 1 || fail "the interrupted run did not survive its client detaching"
INT_INPUT="$RUNDIR/interrupted.input"
printf '1\n' > "$INT_INPUT"
drive interrupted_rejoin 40 "DREAME_WORK=$W_INT" "DREAME_MODEL=x40-ultra" "DREAME_TEST_INPUT_FILE=$INT_INPUT" -- "${TOOL[@]}" &
INT_REJOIN=$!
for _ in $(seq 1 80); do
  attached="$("${TMUX_CMD[@]}" display-message -p -t "$INT_SESSION" '#{session_attached}' 2>/dev/null || true)"
  [ "$attached" = 1 ] && break
  sleep 0.25
done
[ "${attached:-0}" = 1 ] || fail "re-running after Ctrl+C did not return to the existing run"
[ "$(sessions)" -eq 1 ] || fail "re-running after Ctrl+C started a second session"
"${TMUX_CMD[@]}" send-keys -t "$INT_SESSION" Enter
wait "$INT_REJOIN" 2>/dev/null || true
wait_sessions 0 || fail "declining to pick up where you left off did not end the run"
pass "Ctrl+C leaves one run and re-running goes back to it"

# --- 6. a vanished client rejoins the same run ----------------------------------------------
W_DROP="$RUNDIR/work-client-drop"
drive dropped 40 "DREAME_WORK=$W_DROP" "DREAME_MODEL=x40-ultra" -- "${TOOL[@]}" &
DROP_CLIENT=$!
DROP_SESSION="$(session_for_work "$W_DROP")"
for _ in $(seq 1 80); do
  "${TMUX_CMD[@]}" display-message -p -t "$DROP_SESSION" '#{session_attached}' 2>/dev/null | grep -q 1 && break
  sleep 0.25
done
"${TMUX_CMD[@]}" detach-client -s "$DROP_SESSION"
wait "$DROP_CLIENT" 2>/dev/null || true
wait_sessions 1 || fail "detaching the only client killed the run"
DROP_INPUT="$RUNDIR/drop.input"
printf '1\n' > "$DROP_INPUT"
drive dropped_rejoin 40 "DREAME_WORK=$W_DROP" "DREAME_MODEL=x40-ultra" "DREAME_TEST_INPUT_FILE=$DROP_INPUT" -- "${TOOL[@]}" &
DROP_REJOIN=$!
for _ in $(seq 1 80); do
  attached="$("${TMUX_CMD[@]}" display-message -p -t "$DROP_SESSION" '#{session_attached}' 2>/dev/null || true)"
  [ "$attached" = 1 ] && break
  sleep 0.25
done
[ "${attached:-0}" = 1 ] || fail "re-running after the client vanished did not reattach"
[ "$(sessions)" -eq 1 ] || fail "re-running after detach started a second session"
"${TMUX_CMD[@]}" kill-session -t "$DROP_SESSION"
wait "$DROP_REJOIN" 2>/dev/null || true
pass "a vanished client can rejoin the same run"

# --- 7. a dead pane is reported and removed -------------------------------------------------
W_DEAD="$RUNDIR/work-dead"
DEAD_SESSION="$(session_for_work "$W_DEAD")"
printf 'set-option -g remain-on-exit on\n' > "$HOME_DIR/.tmux.conf"
# ~/.tmux.conf is read when the SERVER starts, and the cases above already started one — so the
# setting this case is built around would not be in effect. Ending the server first is what makes
# the next command read it, which is also how the user hits this in the first place.
# HOME matters on both lines: the config is read when the SERVER starts, and only `drive` runs with
# the test's HOME — a bare tmux here would start the server against the real one and silently skip
# the very setting this case is built around.
env HOME="$HOME_DIR" "${TMUX_CMD[@]}" kill-server 2>/dev/null || true
env HOME="$HOME_DIR" "${TMUX_CMD[@]}" new-session -d -s "$DEAD_SESSION" 'exit 7'
for _ in $(seq 1 40); do
  dead="$("${TMUX_CMD[@]}" display-message -p -t "$DEAD_SESSION" '#{pane_dead}' 2>/dev/null || true)"
  [ "$dead" = 1 ] && break
  sleep 0.1
done
DEAD_INPUT="$RUNDIR/dead.input"
printf '1\n' > "$DEAD_INPUT"
drive dead 40 "DREAME_WORK=$W_DEAD" "DREAME_TEST_INPUT_FILE=$DEAD_INPUT" -- "${TOOL[@]}" status
[ "$(rc_of dead)" = 1 ] || fail "a dead pane was not reported as a stopped run"
text_of dead | grep -q "stopped without recording" || fail "the dead run's outcome was not reported"
"${TMUX_CMD[@]}" has-session -t "$DEAD_SESSION" 2>/dev/null && fail "the dead session was left behind"
rm -f "$HOME_DIR/.tmux.conf"
pass "a dead pane is reported instead of attached and its session is removed"

# --- 8. an uninterruptible lock can only be rejoined ----------------------------------------
W_FLASH="$RUNDIR/work-flash"
mkdir -p "$W_FLASH"
FLASH_SESSION="$(session_for_work "$W_FLASH")"
python3 - "$W_FLASH/.lock" <<'PYEOF' &
import fcntl, json, sys, time
with open(sys.argv[1], "w") as lock:
    json.dump({"command": "root", "uninterruptible": True}, lock)
    lock.flush()
    fcntl.flock(lock, fcntl.LOCK_EX)
    time.sleep(20)
PYEOF
LOCK_HOLDER=$!
for _ in $(seq 1 40); do [ -s "$W_FLASH/.lock" ] && break; sleep 0.1; done
"${TMUX_CMD[@]}" new-session -d -s "$FLASH_SESSION" 'sleep 20'
drive flash_guard 5 "DREAME_WORK=$W_FLASH" -- "${TOOL[@]}" status || true
if text_of flash_guard | grep -qi "close it"; then
  fail "the mid-flash guard offered to close an uninterruptible run"
fi
text_of flash_guard | grep -q "part-way through writing" ||
  fail "the mid-flash guard did not send the second invocation back to the run"
"${TMUX_CMD[@]}" kill-session -t "$FLASH_SESSION" 2>/dev/null || true
kill "$LOCK_HOLDER" 2>/dev/null || true
wait "$LOCK_HOLDER" 2>/dev/null || true
pass "an uninterruptible lock never offers to close the live run"

# --- 9. an install path containing a space still starts -------------------------------------
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

# --- 10. a pure command does not disturb a live run ------------------------------------------
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

# --- 11. a second workspace is an independent session, not the same one ----------------------
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

echo "ALL PASS: tmux session wrapper (real tmux $("${TMUX_CMD[@]}" -V | awk '{print $2}'))"
