# Results

No real run has been made yet, so this page has no numbers. It does not guess.

`eval/report/build_report.py` writes this page from the result files of eval runs, and every
number on the finished page comes from those files. The report holds:

* the results table of each config on the SWE-bench Verified subset and on the custom set, with a
  Wilson 95% interval on each resolve rate, dollars per resolved task, median wall time, tokens
  per task and the cache ratio
* a McNemar test of full against naive, paired on the same tasks
* a box plot of tokens per task
* the paired ablations: compaction on and off, and memory on and off
* the chaos table: completion, diff equality, duplicate side effects and resume latency by kill type
* every unresolved task of the full config, with its outcome and a one-line reason
* a footer with the run ids and the git sha

The method, the task sets and the formulas are in section 8 of the plan.

## Produce the real page

Start the stack and the worker as in the runbook ("Run the evaluation"), then run each config on
each set with one model, and grade the SWE-bench runs. The first command of each group names the
run set, so the runner refuses a second model inside it.

```bash
make up && make sandbox-image && make migrate
uv sync --extra eval

RUN="uv run python eval/run.py"
$RUN --set swebench --config single_shot --run-id swe-single-shot --run-set swe-headline
$RUN --set swebench --config naive --run-id swe-naive --run-set swe-headline
$RUN --set swebench --config full --run-id swe-full --run-set swe-headline
eval/swebench/grade.sh eval/results/swe-single-shot
eval/swebench/grade.sh eval/results/swe-naive
eval/swebench/grade.sh eval/results/swe-full

$RUN --set custom --config single_shot --run-id custom-single-shot --run-set custom-headline
$RUN --set custom --config naive --run-id custom-naive --run-set custom-headline
$RUN --set custom --config full --run-id custom-full --run-set custom-headline
$RUN --set custom --config full --run-id custom-full-2 --run-set custom-headline

# Ablations, on a Haiku-class worker. Plan section 8.4 warms memory with a prior pass, so the
# warm-up runs first and its notes are approved before the plain full run, which is the partner
# of both ablations. The warm-up run stays out of the report.
$RUN --set custom --config full --run-id abl-warmup --run-set ablations
# approve the notes it learned: longhaul memory list --status candidate, then memory approve
$RUN --set custom --config full --run-id abl-full --run-set ablations
$RUN --set custom --config full --context-mode no_compaction --run-id abl-no-compaction \
  --run-set ablations
```

The memory ablation needs a restart. `--memory off` is only recorded in the result files, and the
worker keeps the memory settings of its own config. So stop the worker, copy the config with
`memory.mode: "off"`, start the worker on the copy, and then run the "without" side. That mode
leaves the notes out and still verifies the commands, so the two sides differ in the notes only:

```bash
# in a copy of the config (config/longhaul.no-memory.yaml), set memory.mode: "off", then
LONGHAUL_CONFIG=config/longhaul.no-memory.yaml uv run longhaul worker
$RUN --set custom --config full --memory off --run-id abl-no-memory --run-set ablations
```

The chaos suite replays recorded model answers. Record the four chaos tasks with a real model
first, because the report refuses a suite whose recordings were scripted:

```bash
export ANTHROPIC_API_KEY=...        # the recording calls the model
uv run python eval/chaos/chaos.py --record --provider anthropic
uv run python eval/chaos/chaos.py --runs 20 --seed 7
```

Then build the page. List the headline runs first, because the first plain run names the headline
model, and name the chaos folder that the suite printed:

```bash
uv run python eval/report/build_report.py swe-single-shot swe-naive swe-full \
  custom-single-shot custom-naive custom-full custom-full-2 \
  abl-full abl-no-compaction abl-no-memory chaos-DATE
```

The compaction ablation is paired on the tasks that both of its runs attempted. The runner has no
switch for the long-task subset yet, so until it has one the pairing covers every custom task.

`--allow-scripted` builds a page from runs that a script played. It adds a dry-run notice, and its
output says nothing about a model, so it is for trying the builder and not for this file.
