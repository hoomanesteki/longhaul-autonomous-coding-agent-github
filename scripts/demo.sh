#!/usr/bin/env bash
# Run one finished task end to end on the calc fixture, with the scripted model. This is what
# `make demo` runs. It needs no GitHub account and no API key.
#
#   make setup && make up && make demo
#
# It starts a worker, runs the task of tests/fixtures/repos/calc with the answers of
# tests/fixtures/scripts/calc_fix.yaml, prints each turn and the final diff, and stops the
# worker. Nothing is left running: the worker is stopped by its own pid, and the sandbox of the
# task is checked for at the end.
#
# The script touches only what it starts. It uses a task queue of its own, so no other worker
# takes its task, and a checkpoint folder in a temporary directory. It starts the worker as the
# console script of the virtualenv and not as `uv run longhaul`, because `uv run` swallows the
# signal and the worker would outlive it. The sandbox setup installs pytest from PyPI, so the
# machine needs outbound network for the first minute of the task.
#
# The output of the run is printed while the task runs, so a slow first setup can be told from a
# hang. If the script is interrupted or the worker dies, it cancels the workflow of the task (and
# terminates it if the cancel does not close it), because a workflow on a task queue that no
# worker polls would stay open for good.
set -euo pipefail

cd "$(dirname "$0")/.."

LONGHAUL="${LONGHAUL:-.venv/bin/longhaul}"
FIXTURE=tests/fixtures/repos/calc
SCRIPT=tests/fixtures/scripts/calc_fix.yaml
# The limits can be lowered from the environment, which the tests of this script do.
WAIT_S="${DEMO_WAIT_S:-600}"
RUN_WAIT_S="${DEMO_RUN_WAIT_S:-3600}"
CANCEL_WAIT_S="${DEMO_CANCEL_WAIT_S:-60}"
PYTHON="${DEMO_PYTHON:-.venv/bin/python}"

if [ ! -x "$LONGHAUL" ]; then
  echo "$LONGHAUL not found: run make setup first, or set LONGHAUL to the longhaul command" >&2
  exit 2
fi

WORK="$(mktemp -d)"
export LONGHAUL_TASK_QUEUE="demo-$$-$(date +%s)"
export LONGHAUL_CKPT_DIR="$WORK/ckpt"
# The scripted provider plays the task, so no key is needed and a stray one must not be used.
unset ANTHROPIC_API_KEY OPENAI_API_KEY OPENAI_BASE_URL LONGHAUL_SCRIPT
export LONGHAUL_PROVIDER=scripted
# Any free port for the metrics, so the demo does not fight another worker for 9464.
export LONGHAUL_METRICS_PORT=0
# macOS skips the .pth file of an editable install when it carries the hidden flag, so the
# console script would not find the package. Naming src avoids that.
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"

# The example config names the local embedder, which needs the memory extra and a model
# download. The demo uses the hash embedder, which needs nothing. The pricing path is made
# absolute because the copy sits in another folder.
sed -e 's/^\(  embedder:\) local\(.*\)$/\1 hash/' \
    -e "s#^pricing_file: config/pricing.yaml#pricing_file: $PWD/config/pricing.yaml#" \
    config/longhaul.example.yaml >"$WORK/longhaul.yaml"
export LONGHAUL_CONFIG="$WORK/longhaul.yaml"

WORKER_PID=""
RUN_PID=""
TAIL_PID=""
TASK_ID=""
FAILED=1

# The task id is the second word of the first line `longhaul run` prints once it has started the
# workflow: "task lh-local-calc-1234abcd: <title>".
read_task_id() {
  sed -n 's/^task \(lh-local-[^:]*\):.*/\1/p' "$WORK/run.log" 2>/dev/null | head -n 1
}

# Wait until a log file has a line that matches, or give up.
wait_for() {
  local file="$1" pattern="$2" waited=0
  until grep -q -- "$pattern" "$file" 2>/dev/null; do
    sleep 0.2
    waited=$((waited + 1))
    if [ "$waited" -gt $((WAIT_S * 5)) ]; then
      echo "gave up after ${WAIT_S}s waiting for: $pattern" >&2
      return 1
    fi
    if [ -n "$WORKER_PID" ] && ! kill -0 "$WORKER_PID" 2>/dev/null; then
      echo "the worker stopped before it printed: $pattern" >&2
      cat "$WORK/worker.log" >&2
      return 1
    fi
  done
}

# Close the workflow of a run that is still going. The cancel goes first and while the worker is
# up, because the workflow removes its own sandbox when it is cancelled. When the cancel does not
# close it in time, or no worker is left to act on it, the workflow is terminated.
stop_run() {
  if [ -z "$RUN_PID" ] || ! kill -0 "$RUN_PID" 2>/dev/null; then
    return 0
  fi
  if [ -z "$TASK_ID" ]; then
    TASK_ID="$(read_task_id)"
  fi
  if [ -n "$TASK_ID" ]; then
    echo "stopping task $TASK_ID" >&2
    local waited=0
    if [ -n "$WORKER_PID" ] && kill -0 "$WORKER_PID" 2>/dev/null; then
      "$LONGHAUL" cancel "$TASK_ID" --reason "demo interrupted" >&2 || true
      # `longhaul run` follows the workflow, so it exits once the workflow has closed.
      while kill -0 "$RUN_PID" 2>/dev/null && [ "$waited" -lt $((CANCEL_WAIT_S * 5)) ]; do
        sleep 0.2
        waited=$((waited + 1))
      done
    fi
    if kill -0 "$RUN_PID" 2>/dev/null; then
      terminate_workflow
    fi
  fi
  kill "$RUN_PID" 2>/dev/null || true
  wait "$RUN_PID" 2>/dev/null || true
  RUN_PID=""
}

# Temporal has no command here, so a helper asks the client library, with the address from the
# same settings the worker reads (.env included). The task id is the id of the workflow. The task
# row of a terminated task is closed later by the janitor.
terminate_workflow() {
  "$PYTHON" scripts/demo_terminate.py "$TASK_ID" >&2 ||
    echo "could not terminate the workflow of $TASK_ID" >&2
  # Give the run a moment to see the end, then let the caller kill it if it still hangs.
  local waited=0
  while kill -0 "$RUN_PID" 2>/dev/null && [ "$waited" -lt 25 ]; do
    sleep 0.2
    waited=$((waited + 1))
  done
}

cleanup() {
  # A second Ctrl-C must not cut the cleanup short and leave the worker behind.
  trap '' INT TERM
  if [ -n "$TAIL_PID" ]; then
    kill "$TAIL_PID" 2>/dev/null || true
    wait "$TAIL_PID" 2>/dev/null || true
  fi
  stop_run
  if [ -n "$WORKER_PID" ]; then
    kill "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
  fi
  # A finished task removes its own sandbox. One that was cut short may have left it behind.
  if [ -n "$TASK_ID" ]; then
    local left
    left="$(docker ps -aq --filter "label=longhaul.task_id=$TASK_ID")"
    # shellcheck disable=SC2086
    [ -n "$left" ] && docker rm -f $left >/dev/null 2>&1 || true
    docker volume rm -f "lh-ws-$TASK_ID" >/dev/null 2>&1 || true
  fi
  if [ "$FAILED" -eq 0 ]; then
    rm -rf "$WORK"
  else
    echo "logs are in $WORK" >&2
  fi
}
# Ctrl-C and a plain kill end the script through the exit trap, which does the cleanup.
trap 'exit 130' INT
trap 'exit 143' TERM
trap cleanup EXIT

# Start a command in a process group of its own. A terminal Ctrl-C goes to the whole foreground
# group, and the real worker stops at once when it gets one, so it would no longer poll when the
# cancel arrives. In a group of its own it sees only the signals this script sends. macOS has no
# setsid, so perl does it. Perl is the background command itself, and not a shell function that
# calls it: a function in the background runs in a forked subshell, so $! would be the pid of
# that subshell and a kill would miss the command. Perl execs the command in its own process, so
# $! is the pid of the worker or the run.
DETACH='setpgrp(0, 0); exec @ARGV or die "exec $ARGV[0]: $!\n"'

START="$(date +%s)"
perl -e "$DETACH" -- "$LONGHAUL" worker >"$WORK/worker.log" 2>&1 &
WORKER_PID=$!
wait_for "$WORK/worker.log" "worker running"

# The run goes to a file and `tail` shows the file as it grows. The run is a background job so
# the script can watch the worker while it follows the task.
: >"$WORK/run.log"
perl -e "$DETACH" -- "$LONGHAUL" run \
  --path "$FIXTURE" \
  --issue-file "$FIXTURE/ISSUE.md" \
  --provider scripted \
  --script "$SCRIPT" >"$WORK/run.log" 2>&1 &
RUN_PID=$!
tail -n +1 -f "$WORK/run.log" &
TAIL_PID=$!

waited=0
while kill -0 "$RUN_PID" 2>/dev/null; do
  # Record the id as soon as it is printed, so an interrupt can cancel the workflow.
  if [ -z "$TASK_ID" ]; then
    TASK_ID="$(read_task_id)"
  fi
  if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "the worker stopped while the task was running; the worker log follows" >&2
    cat "$WORK/worker.log" >&2
    exit 1
  fi
  sleep 0.2
  waited=$((waited + 1))
  if [ "$waited" -gt $((RUN_WAIT_S * 5)) ]; then
    echo "gave up after ${RUN_WAIT_S}s waiting for the task to end" >&2
    exit 1
  fi
done
if wait "$RUN_PID"; then CODE=0; else CODE=$?; fi
RUN_PID=""
# Let `tail` print the last lines before it is stopped.
sleep 0.5
kill "$TAIL_PID" 2>/dev/null || true
wait "$TAIL_PID" 2>/dev/null || true
TAIL_PID=""

TASK_ID="$(read_task_id)"
if [ "$CODE" -ne 0 ]; then
  echo "the run failed with exit code $CODE; the worker log follows" >&2
  cat "$WORK/worker.log" >&2
  exit "$CODE"
fi
if [ -z "$TASK_ID" ]; then
  echo "the run printed no task id" >&2
  exit 1
fi

kill "$WORKER_PID"
wait "$WORKER_PID" 2>/dev/null || true
WORKER_PID=""

if [ -n "$(docker ps -aq --filter "label=longhaul.task_id=$TASK_ID")" ]; then
  echo "the sandbox of $TASK_ID was left behind" >&2
  exit 1
fi
echo "demo finished in $(($(date +%s) - START)) seconds: task $TASK_ID, worker stopped, no sandbox left"
FAILED=0
