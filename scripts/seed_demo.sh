#!/usr/bin/env bash
# Serve the console with three tasks in the states a person acts on (LH-808): one with a pull
# request, one paused for a hint, one waiting for its plan to be approved, and two lesson
# candidates in memory. The work is in scripts/demo_stack.py; this file checks the services and
# hands over to it.
#
#   make up && make sandbox-image
#   npm --prefix console ci && npm --prefix console run build
#   ./scripts/seed_demo.sh                 # then open http://127.0.0.1:8765
#
# The seed takes a minute or two, because each task sets up a sandbox. The script prints "ready"
# when the console answers, and removes everything it made on ctrl-c. LH_DEMO_PORT changes the
# port and LONGHAUL_ADMIN_TOKEN the token (the default is printed). The browser test of the
# console starts this script itself (console/playwright.config.ts).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if ! docker info > /dev/null 2>&1; then
  echo "seed_demo: the Docker daemon does not answer; the tasks run in sandbox containers" >&2
  exit 1
fi

# exec, so that the SIGTERM of whoever stops the demo reaches the Python process, which removes
# what it made, and not a shell around it. --no-sync keeps uv from touching the environment.
export PYTHONPATH="$root/src:$root${PYTHONPATH:+:$PYTHONPATH}"
exec uv run --no-sync python scripts/demo_stack.py "$@"
