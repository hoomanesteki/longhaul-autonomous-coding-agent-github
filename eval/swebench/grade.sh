#!/usr/bin/env bash
# Grade a finished SWE-bench run with the official harness and write the verdicts into its
# result files (LH-902).
#
#   eval/swebench/grade.sh RUN_DIR [MAX_WORKERS]
#
# RUN_DIR is eval/results/<run id>, which eval/run.py --set swebench made. The script
#   1. writes RUN_DIR/predictions.jsonl from the result files, unless it exists,
#   2. runs python -m swebench.harness.run_evaluation on it, with the run id as --run_id,
#   3. reads the harness report into the `resolved` and `grading` fields of each result file and
#      rebuilds summary.json (eval/swebench/report.py).
#
# The harness works in WORK_DIR (default RUN_DIR/harness): its logs/ folder and its report land
# there and not in the repo. It runs the instance images, so it needs Docker, the images (it
# pulls them), the eval extra of this project and about 120 GB of disk for a full run. It caches
# by run id and instance, so running this twice on one run reads the first answers again.
#
# Environment: SWEBENCH_MAX_WORKERS (default 4), SWEBENCH_WORK_DIR, SWEBENCH_TIMEOUT (seconds per
# instance, default 1800), SWEBENCH_DATASET (default SWE-bench/SWE-bench_Verified).
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 RUN_DIR [MAX_WORKERS]" >&2
  exit 2
fi
if [ ! -d "$1" ]; then
  echo "error: $1 is not a folder" >&2
  exit 2
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
run_dir=$(cd "$1" && pwd)
run_id=$(basename "$run_dir")
workers=${2:-${SWEBENCH_MAX_WORKERS:-4}}
timeout=${SWEBENCH_TIMEOUT:-1800}
dataset=${SWEBENCH_DATASET:-SWE-bench/SWE-bench_Verified}
work_dir=${SWEBENCH_WORK_DIR:-$run_dir/harness}
python=(uv run --project "$root" --extra eval python)

mkdir -p "$work_dir"
work_dir=$(cd "$work_dir" && pwd)

if [ ! -f "$run_dir/predictions.jsonl" ]; then
  (cd "$root" && "${python[@]}" -m eval.swebench.adapter predictions "$run_dir")
fi

# Run from the work folder: the harness writes logs/ into the working directory, and the
# eval/swebench folder of this repo must not be on sys.path, where it would hide the package.
(
  cd "$work_dir"
  "${python[@]}" -m swebench.harness.run_evaluation \
    --dataset_name "$dataset" \
    --predictions_path "$run_dir/predictions.jsonl" \
    --max_workers "$workers" \
    --run_id "$run_id" \
    --timeout "$timeout" \
    --report_dir "$work_dir"
)

(cd "$root" && "${python[@]}" -m eval.swebench.report "$run_dir" --work-dir "$work_dir")
