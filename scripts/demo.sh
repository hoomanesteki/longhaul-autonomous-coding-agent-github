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
set -euo pipefail

cd "$(dirname "$0")/.."

LONGHAUL="${LONGHAUL:-.venv/bin/longhaul}"
FIXTURE=tests/fixtures/repos/calc
SCRIPT=tests/fixtures/scripts/calc_fix.yaml
WAIT_S=600

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
TASK_ID=""
FAILED=1

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

cleanup() {
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
trap cleanup EXIT

START="$(date +%s)"
"$LONGHAUL" worker >"$WORK/worker.log" 2>&1 &
WORKER_PID=$!
wait_for "$WORK/worker.log" "worker running"

if "$LONGHAUL" run \
  --path "$FIXTURE" \
  --issue-file "$FIXTURE/ISSUE.md" \
  --provider scripted \
  --script "$SCRIPT" >"$WORK/run.log" 2>&1; then CODE=0; else CODE=$?; fi
cat "$WORK/run.log"

TASK_ID="$(sed -n 's/^task \(lh-local-[^:]*\):.*/\1/p' "$WORK/run.log" | head -n 1)"
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
