#!/usr/bin/env bash
# Kill the worker in the middle of a run and start it again, by hand. This is the manual demo of
# Integration checkpoint 2: the task finishes anyway, with one retried activity.
#
#   make up && make sandbox-image && make migrate
#   scripts/chaos_kill.sh [SECONDS]
#
# SECONDS is how long to wait after the first model turn starts before the kill (default 3).
# The task is the inventory fix of tests/fixtures/scripts/inventory_fix.yaml, played by the
# scripted provider, so no model key is needed.
#
# The script touches only what it starts. It uses a task queue of its own, so no other worker
# takes its task, and it sends SIGKILL to the pid of the worker it started, never to a process
# found by name. At the end it looks for containers of its own task, and nothing else. After the
# kill the activity that was running is retried when its heartbeat timeout passes, which takes
# 15 to 60 seconds with the default timeouts (docs/runbook.md). The automated version, with
# short timeouts, is tests/chaos/test_kill_worker.py.
set -euo pipefail

cd "$(dirname "$0")/.."

LONGHAUL="${LONGHAUL:-.venv/bin/longhaul}"
KILL_AFTER="${1:-3}"
WAIT_S=300
FIXTURE=tests/fixtures/repos/inventory

if [ ! -x "$LONGHAUL" ]; then
  echo "$LONGHAUL not found: run uv sync, or set LONGHAUL to the longhaul command" >&2
  exit 2
fi

WORK="$(mktemp -d)"
export LONGHAUL_TASK_QUEUE="chaos-demo-$$-$(date +%s)"
export LONGHAUL_CKPT_DIR="$WORK/ckpt"
# The task brings its script, so the worker needs no provider setting and no key.
unset ANTHROPIC_API_KEY OPENAI_API_KEY LONGHAUL_SCRIPT
export LONGHAUL_PROVIDER=scripted

WORKER_PID=""
RUN_PID=""
TASK_ID=""
FAILED=1

start_worker() {
  "$LONGHAUL" worker >>"$WORK/worker.log" 2>&1 &
  WORKER_PID=$!
  echo "worker started, pid $WORKER_PID"
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
  done
}

cleanup() {
  [ -n "$RUN_PID" ] && kill "$RUN_PID" 2>/dev/null || true
  [ -n "$WORKER_PID" ] && kill "$WORKER_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  if [ "$FAILED" -eq 0 ]; then
    rm -rf "$WORK"
  else
    echo "logs are in $WORK" >&2
  fi
}
trap cleanup EXIT

start_worker
wait_for "$WORK/worker.log" "worker running"

"$LONGHAUL" run \
  --path "$FIXTURE" \
  --issue-file "$FIXTURE/ISSUE.md" \
  --provider scripted \
  --script tests/fixtures/scripts/inventory_fix.yaml >"$WORK/run.log" 2>&1 &
RUN_PID=$!

# The sandbox takes a while to set up. The kill clock starts with the first model turn.
wait_for "$WORK/worker.log" "activity llm_turn started"
sleep "$KILL_AFTER"
kill -9 "$WORKER_PID"
echo "killed worker $WORKER_PID. Its last activity line:"
grep -- "activity " "$WORK/worker.log" | tail -n 1

start_worker

if wait "$RUN_PID"; then CODE=0; else CODE=$?; fi
RUN_PID=""
cat "$WORK/run.log"

TASK_ID="$(sed -n 's/^task \(lh-local-[^:]*\):.*/\1/p' "$WORK/run.log" | head -n 1)"
if [ -z "$TASK_ID" ]; then
  echo "the run printed no task id" >&2
  exit 1
fi
echo "activity lines for a second attempt or later: $(grep -c -- 'attempt [2-9]' "$WORK/worker.log" || true)"

LEFT="$(docker ps -aq --filter "label=longhaul.task_id=$TASK_ID")"
if [ -n "$LEFT" ]; then
  echo "leftover containers of $TASK_ID: $LEFT" >&2
  exit 1
fi
echo "no leftovers"
FAILED=$CODE
exit "$CODE"
