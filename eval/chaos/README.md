# Full chaos suite

Twenty runs of recorded custom tasks, each with one to three kills planned, with a count of what they
leave behind. It puts a number behind "survives crashes".

```bash
uv run python eval/chaos/chaos.py --runs 20 --seed 7
```

It needs the sandbox image (`make sandbox-image`) and the MinIO of `make up`. It starts a Temporal
and a Postgres of its own and removes them at the end, so it is safe to run while other builds
use the ones of `make up`. A full run takes about an hour, and longer on a busy machine.

## What a run does

1. The plan is drawn from the seed (`plan.py`). Run `n` takes the next of the four tasks, so each
   task gets five runs, and it gets one to three kills, each of one of four kinds.
2. Each task is played once without a kill, as the reference. Its activities say when the kills
   of the later runs land: a kill waits for the k-th activity of the worker to start and then
   pauses for no longer than that activity took in the reference. Kills follow the progress of
   the worker and not the clock, so a busy machine does not push them past the end of a run.
3. A killed run is played from the same recording. The worker answers with the replay provider,
   so every run sees the same model answers, and a difference in the final diff is a durability
   bug and not noise from a model.
4. Delivery goes to a FakeGitHub that the run owns. The worker clones from it, comments and
   labels the issue, pushes the branch and opens the pull request, as it does on github.com.

| Kill | What it does |
|---|---|
| `worker` | SIGKILL to the worker process, which is then started again |
| `sandbox` | `docker kill` of the sandbox container of the task (found by its task id label) |
| `temporal` | a restart of the Temporal container, which keeps its state in `--db-filename` on a volume |
| `postgres` | a restart of the Postgres container |

With seed 7 every kind is hit at least four times across the twenty runs.
`tests/unit/test_chaos_plan.py::test_kill_type_coverage` asserts it.

## What it records

`eval/results/chaos-<date>/summary.json` adds up the runs, and `run-NN.json` holds one run each.

| Field | Meaning |
|---|---|
| `completed` | the workflow ended with a pull request |
| `diff_equals_reference` | the final diff is the diff of the same task run without a kill |
| `extra_ledger_rows`, `extra_cost_usd` | model calls billed again because a kill made an activity run again, against the run without a kill |
| `resume_latency_s` | for each kill that an activity followed, the seconds from the end of the kill (or of the restart) to the start of that activity, from the timestamps in Temporal's history |
| `side_effects`, `expected_side_effects` | comments, label changes and pull requests found on the FakeGitHub, against the ones that a task writes (1, 3 and 1) |
| `duplicate_side_effects`, `missing_side_effects` | the writes beyond and below the expected ones |
| `problems` | why a run does not count as survived: a fork in the checkpoints, a different diff, a run that did not finish, a run in which none of the planned kills happened |

The summary adds `temporal_heals`, the restarts of Temporal that the watchdog made (see below).

The side effects are counted from the state of the FakeGitHub and never from our own
`side_effects` table, which is where a retry decides whether a write already happened. A table
that is also the witness could hide its own mistake.

The latency is the time to the next activity start, as the story defines it, so it says how fast
the system took up work again and not how long the work took. After a `sandbox` kill the next
activity is the one that makes the sandbox again, which starts at once, so the latency is short
and the rebuild (tens of seconds) is inside that activity. For a restart of Temporal or Postgres
the clock starts when the restart command returns, not when the service stops.

The cost of the extra ledger rows is priced from the tokens of each call at the rates of
`config/pricing.yaml`. A replayed run bills nothing, so the dollars on its ledger are zero.

The heartbeat timeout of the runs is 20 seconds, so a killed worker is noticed after about that
long, and the latency of a `worker` kill is mostly that. A shorter timeout made a live activity
look dead on a loaded machine, and its retry then ran beside it.

## A Temporal that stops answering

The Temporal dev server keeps its state in one SQLite connection. On a busy machine a query can
run out of time and leave that connection interrupted: from then on every call fails with
"interrupted (9)" and the run waits for a server that does not recover by itself. This was met in
the first try of the full suite, 20 seconds into a run that had no kill.

`watchdog.py` asks the private Temporal every 10 seconds for a count of workflows, which reads its
database. When that has failed for 60 seconds in a row, it restarts the container. The state is in
a file on a volume, so the workflows come back. A restart that a kill makes is told to the
watchdog and never counts as a wedge. The restarts of the watchdog are in `temporal_heals` of the
summary, because a run that met one saw a restart that the plan did not draw.

## Recordings

The four tasks are `calc-01`, `calc-02`, `calc-03` and `inventory-01`. Their recordings are in
`recordings/<task id>/`, one JSON file per model call, and the file `SOURCE` says where they came
from.

**These recordings are scripted.** No key was available where they were made, so each one is a run
of `tests/fixtures/scripts/eval/<task id>.yaml` through the worker, recorded by the same provider
code that records a real model. They fix the model answers, which is all the suite needs. A
recording of a real model replaces them: it gives the suite the pace and the shape of real work,
with longer runs and different calls.

To record again, with the scripts:

```bash
uv run python eval/chaos/chaos.py --record
```

To replace them with the recording of a real model (it costs money, and needs a key):

```bash
ANTHROPIC_API_KEY=... uv run python eval/chaos/chaos.py --record --provider anthropic
```

Each recording is one run without a kill, on the same stack and the same delivery as the suite,
so its requests match what the suite sends. The command empties the folder of a task before it
saves the new calls, because the provider keeps the first recording of a request. Commit the
folders afterwards, and `SOURCE` will name the model. `--tasks calc-01,calc-02` records some of
the tasks only.

A request that no recording has gets the first recording of the same turn, with a warning (risk
R6 in the plan). Record again when a prompt or a tool description changed.

## Checks you can run on their own

```bash
uv run pytest tests/unit/test_chaos_plan.py       # the plan, and the coverage of the kinds
uv run pytest tests/chaos/test_chaos_full.py      # two runs of the suite, and the summary fields
```

The second needs Docker and takes a few minutes.
