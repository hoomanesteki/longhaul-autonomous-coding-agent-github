# Runbook

## Start here

Run these from the repo root on a fresh clone. You need `uv`, Docker and `gh`.

```bash
make setup    # uv sync and pre-commit hooks
make up       # local stack via docker compose
make check    # ruff, format check, mypy, unit tests
```

The plan is in `docs/plan/longhaul-build-plan.md`. Story status is in `docs/progress.md`. Contribution rules are in `CONTRIBUTING.md`. Voice rules are in `docs/style.md`.

## Local stack

`make up` starts Postgres with pgvector, Temporal, MinIO, an OpenTelemetry collector, Jaeger, Prometheus and Grafana. To change anything below, copy `.env.example` to `.env` first. Docker Compose and the Python code both read `.env` from the repo root, so they see the same ports and URLs. A variable set in your shell wins over `.env`, and a blank value in `.env` counts as unset.

| Setting | Where | Notes |
|---|---|---|
| Host ports | `LH_*_PORT` in `.env` | If another project holds a default port, change it here. For Postgres, Temporal and MinIO also change the matching `LONGHAUL_*` URL or address, because compose and the code read separate variables. |
| Database password | `LH_POSTGRES_PASSWORD` in `.env` | Default `longhaul`. Keep the password inside `LONGHAUL_DATABASE_URL` the same. |
| Config file | `LONGHAUL_CONFIG` | Default `config/longhaul.example.yaml`. |

### Ports and the database password

Every published port is bound to `127.0.0.1`, so other machines on your network cannot reach the database, Temporal or MinIO. Temporal and MinIO have no real authentication, and the Postgres login is a superuser.

Postgres reads its password once, when the `pgdata` volume is first created. To change it later, run `make down` (this deletes the volumes and all data), edit `.env`, then run `make up`.

Loopback binding does not keep out containers. On Docker Desktop a container on the default bridge network can open `host.docker.internal:<port>` and reach a port that is published on `127.0.0.1`. Sandboxes run repo build code with the network on during `setup_command`, so for any run against a real repo set a random `LH_POSTGRES_PASSWORD` (and the same password in `LONGHAUL_DATABASE_URL`). The sandbox environment is an allowlist and never receives either value.

### Migrations and database tests

`make migrate` runs `alembic upgrade head` against `LONGHAUL_DATABASE_URL`, taken from your shell or from `.env`. There is no default URL, so a missing value stops with an error instead of touching a database you did not mean. To migrate another database from Python, set `sqlalchemy.url` on the Alembic `Config`. Code that already runs inside an event loop must instead set `config.attributes["connection"]` to an open connection and call the command from `AsyncConnection.run_sync`, because Alembic cannot start a second loop there.

`make itest` does not use your database for migration tests. They create a database named `longhaul_migtest_<random>` on the same server, run on it and drop it, so a downgrade never touches your rows. The database role needs the `CREATEDB` privilege, which the compose superuser has. If a test run is killed, a `longhaul_migtest_*` database can stay behind. It is safe to drop.

A pooled database connection belongs to the event loop that opened it. Code that runs more than one loop in a process, such as a CLI that calls `asyncio.run` twice or tests that each get their own loop, must `await dispose_engine()` before each loop ends. Code that builds its own engine can pass `pooled=False` to `make_engine` instead.

### The memory embedder

Memory finds notes by vector search. Tests and CI use `HashEmbedder`, which needs nothing installed. Real use needs the `memory` extra, which adds fastembed and onnxruntime. This installs it and runs the opt-in test against the real model:

```bash
uv sync --extra memory
uv run pytest -m slow tests/slow/test_local_embedder.py -rs
```

The first run downloads the model (66 MB). fastembed keeps it in `FASTEMBED_CACHE_PATH`, or under the system temp directory when that is unset, where a reboot may clear it. Set the variable to a folder that stays for any worker that embeds. Without the extra the test is skipped, and `-rs` shows how to install it. A plain `uv sync` takes the extra's packages out again. ADR-0010 has the reasons.

### Memory items

The agent keeps notes about each repo (ADR-0010). `longhaul memory` shows them and decides on the ones that wait for a person. The commands read `LONGHAUL_DATABASE_URL`, so run them after `make up` and `make migrate`.

```bash
uv run longhaul memory list --repo acme/inventory                  # every item, oldest first
uv run longhaul memory list --repo acme/inventory --status candidate --kind lesson
uv run longhaul memory approve ID --by alice                       # the note may now reach a prompt
uv run longhaul memory reject ID                                   # it stays in the table, out of every prompt
```

`--by` is the name recorded as the approver. Without it, the name of the user of the machine is used. `list` prints the id that `approve` and `reject` take.

When a task starts, `prepare_workspace` runs the test and lint commands that the repo's `pyproject.toml`, `tox.ini`, Makefile and CI files name. It keeps the ones that print a pytest, ruff or tox summary as approved `command` items. This runs only on a worker that was given a memory store, and `memory list --kind command` shows the result.

### The repo map

Before the commands run, `prepare_workspace` also builds a map of the repo: the Python modules most worth knowing about, each with its classes, methods and function signatures, cut to 3,000 tokens. The prompt builder (LH-604) reads it from the `repo_map_ref` of the workspace output. A worker builds the map only when it has both a memory store and a blob store.

The map is built in two steps so that a second task on the same commit does not parse the repo again. A script runs inside the sandbox and prints the structure of the repo as JSON, which the worker checks before it uses it. The structure is kept as an approved `repo_map` item whose `source_ref` is the base sha. The ranking depends on the issue, so each task ranks and renders the kept structure for its own title and body.

```bash
uv run longhaul memory list --repo acme/inventory --kind repo_map    # one item for each base sha that was mapped
```

A file that does not parse, or is over 200 KB, is left out of the map and named in it as "Not parsed", and the worker logs it as a warning. A map that cannot be built at all is logged and the task goes on without one.

A big repo is not cut at a path. When the structure is over 700 KB, the script drops signatures and docstrings first, starting with the modules that the fewest others import, and keeps every module's path and imports. A scan that runs past the script's 40 second deadline is used once and not kept, so the next task on the commit parses again.

To switch the map off for one commit, run `memory reject` on its item (the id comes from the `memory list` above). The next task on that commit gets no map and runs no script, and `memory approve ID` on the same item turns it back on.

## Run the agent on a local folder

`longhaul run-local` copies a folder into a fresh Docker sandbox and lets the agent fix an issue in it. It needs no `make up`: no Temporal, no database. It is the quickest way to watch the agent work.

```bash
make sandbox-image    # once, and again when docker/sandbox/Dockerfile changes
uv run longhaul run-local \
  --path tests/fixtures/repos/calc \
  --issue-file tests/fixtures/repos/calc/ISSUE.md \
  --provider scripted --script tests/fixtures/scripts/calc_fix.yaml
```

That run needs no key, because the scripted provider plays `calc_fix.yaml`. For a real model, put `ANTHROPIC_API_KEY` in your shell or in `.env` and use `--provider anthropic` instead of the last line. A real run costs money. The command has no cost cap, only `--max-turns`.

| Option | Meaning |
|---|---|
| `--path DIR` | The repo to work on. It is copied and never changed. |
| `--issue-file FILE` | The task. The first line is the title (leading `#` removed) and the rest is the body. |
| `--provider` | `scripted`, `anthropic` or `openai`. Default: `LONGHAUL_PROVIDER`. |
| `--script YAML` | Script for the scripted provider. Default: `LONGHAUL_SCRIPT`. |
| `--model ID` | Default: `models.coder` in the config (`LONGHAUL_CONFIG`). With `--provider openai` pass it, because the configured ids are Claude ids. |
| `--test-command CMD` | How `run_tests` runs the tests. Default: `python -m pytest -q`. |
| `--max-turns N` | Stop after N turns. Default: 150. |

What it does, in order:

1. Copies the files into the sandbox, commits them as the base commit and tags it `base`, which is what `git_diff` compares against.
2. Runs the setup command with the network on, then cuts the network. With a `pyproject.toml` the setup is `python -m venv .venv` and `pip install -e '.[dev]'` (or `-e . pytest` when there is no `dev` extra), because the sandbox's root filesystem is read-only. The command is printed before it runs. It builds the repo's own code with network access, so run folders you trust, and read "Ports and the database password" above if the local stack is up.
3. Runs the agent and prints one line per turn, such as `turn 3 coder: edit_file src/calc/__init__.py`, then how the run ended, then the final diff. Progress notes, warnings and errors go to stderr.
4. Removes the container and the volume, also after an error, Ctrl-C, SIGTERM or SIGHUP. After SIGKILL or a crash they stay. The first output line holds the task id, so `docker rm -f $(docker ps -aq --filter label=longhaul.task_id=<id>)` and `docker volume rm lh-ws-<id>` clean up by hand.

Exit codes: 0 when the agent called `finish`, 2 when the run failed (turn limit, no tool calls, a refusal, a failing setup, a provider error, no Docker or image) or was interrupted, and 1 for a bug in longhaul, which prints a traceback. The final diff is printed whole. During the run, a tool result over `context.tool_output_max_tokens` reaches the model as its first and last 60 lines and a handle such as `out_toolu_01`, and the model reads the rest with `read_output`. The full text is kept in memory for the length of the run.

### What is not copied

A file called `.longhaulignore` in the folder lists what the sandbox must not get. Its syntax is the one of `.gitignore`:

```
# The reference fix and the issue text stay out of the sandbox.
REFERENCE.patch
ISSUE.md
build/
*.log
docs/private/**
!docs/private/keep.md
```

- A pattern without a slash matches a name at any depth. A slash at the start or inside anchors it to the folder. A slash at the end means directories only.
- `*` and `?` stay inside one name. `**` between slashes crosses directories. `[a-z]` is a range. A `!` line brings back what an earlier line matched, but not inside an ignored directory. A `#` in the first column starts a comment.
- The file is not copied, because it names what it hides. A bad pattern, or an ignore file that cannot be read, stops the run before any sandbox exists.
- Always skipped, whatever the file says: `.git`, `.venv`, `__pycache__`, `*.pyc`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `*.egg-info`, `.DS_Store`, and `.env` and `.env.*` files (but not `.env.example`, `.env.sample` and `.env.template`). A secret in `/workspace` is as readable to the model as one in its environment.
- Links and special files are skipped with a warning. The executable bit is kept.

The three fixture repos list `REFERENCE.patch` and `ISSUE.md`, so a real model never sees the reference fix.

## Prices and the cost ledger

`config/pricing.yaml` holds what each model costs in US dollars per million tokens: `input_per_mtok`, `output_per_mtok`, `cache_write_per_mtok` and `cache_read_per_mtok`. Prices live there and never in code. The comment at the top of the file says when the numbers were last checked against the provider's published model table. Check them again, and change the date, when the provider changes a price or the config names a new model.

A call costs `(input * pi + output * po + cache_write * pcw + cache_read * pcr) / 1,000,000`. The four token counts do not overlap. A model can also have a `long_prompt` block, as Haiku 5.5 has. A call whose prompt is longer than `above_tokens` is billed at the rates in that block, output tokens included. The prompt of a call is its input, cache read and cache write tokens added together, and a prompt of exactly `above_tokens` tokens is not long.

The scripted and replay providers cost nothing, so a scripted run can name any model. For every other provider, `load_pricing(provider, config)` in `longhaul.cost.pricing` reads the pricing file and raises `PricingError` when a model in the config has no price or the file is malformed. The worker calls it before it takes a task (LH-208), so a missing price stops it at startup and not after the first billed call.

Every model call that the provider billed writes one row to the `cost_ledger` table as soon as the call returns. The row holds the task, the repo, the model, the turn, the token counts and the cost in dollars. `attempt` is the Temporal attempt of the activity, so a retry after a failure shows as a second row with attempt 2. A call that failed before the provider billed it has no row. The table is only ever added to, and a turn can have several rows with the same attempt, for example the second call of a reply that was cut off.

```sql
-- what a task cost
SELECT sum(usd) FROM cost_ledger WHERE task_id = 'lh-acme-calc-42';

-- what a repo spent since midnight UTC
SELECT sum(usd) FROM cost_ledger
WHERE repo = 'acme/calc' AND ts >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
```

`tasks.cost_usd` holds the same total for a task, rounded to ten thousandths of a dollar. It is set from the sum of the rows, so it does not drift. A task with no row in `tasks`, as in a local run, still has its calls on the ledger.

## Run tasks on the durable stack

`longhaul run-local` runs the agent in one process and forgets it when it ends. The durable commands run the same agent as a Temporal workflow, so a task survives a stopped worker. They use the stack from `make up`: Temporal for the workflow, Postgres for the records, MinIO for the blobs.

```bash
make up && make sandbox-image && make migrate
# shell 1: the worker stays in the foreground
uv run longhaul worker

# shell 2: start a task and follow it
uv run longhaul run \
  --path tests/fixtures/repos/calc \
  --issue-file tests/fixtures/repos/calc/ISSUE.md \
  --provider scripted --script tests/fixtures/scripts/calc_fix.yaml
```

`make migrate` creates the tables the worker writes to. Both commands read the `LONGHAUL_*` settings from your shell or from `.env` (copy `.env.example`). The worker needs the database, Temporal, the blob store and, for a real model, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. `run` needs Temporal, the blob store and the checkpoint folder, but not the database.

| Command | What it does |
|---|---|
| `longhaul worker` | Polls the task queue (`LONGHAUL_TASK_QUEUE`, default `longhaul`) and runs the activities of every task. Stop it with Ctrl-C or SIGTERM. |
| `longhaul run --path DIR --issue-file FILE` | Starts a task for a folder and follows it. With `--provider scripted --script FILE` the script goes to the worker with the task. |
| `longhaul status TASK_ID` | Prints the state in Temporal, the status of the workflow and, once it ended, the outcome. |
| `longhaul cancel TASK_ID [--reason TEXT]` | Sends the cancel signal. The workflow removes the sandbox and the checkpoint repo and ends as cancelled. |

### What `run` does

1. Builds a task id from the folder name and a random suffix, such as `lh-local-calc-1a2b3c4d`, so a second run of the same folder never meets the rule that a task id is used once.
2. With the scripted provider, uploads the script to the blob store as `tasks/<task id>/script.yaml` and puts its reference in the workflow memo. The worker is started once and plays many tasks, so a script cannot be a setting of the worker. A worker started with `LONGHAUL_PROVIDER=scripted` and no `LONGHAUL_SCRIPT` plays the script each task brings. With any other provider the worker answers with the provider in its own environment, and `--script` is refused.
3. Starts the workflow and prints the task id, then one status line whenever the status changes, such as `status: coding, 3 turns done, cost $0.0000, checkpoint 1a2b3c4`. It asks every 2 seconds. When no worker answers, for example while you restart it, it says `status: waiting for a worker to answer` and goes on.
4. Prints the outcome and the final diff in the shape of `run-local`, and removes the sandbox of the task. The diff is read from the checkpoint repo under `LONGHAUL_CKPT_DIR`, so `run` has to see the same folder as the worker. A workflow does not remove the sandbox of a task that finished yet, so `run` does it.

The task path has to be a folder on the machine of the worker, and the model sees the folder name only. The exit codes are the ones of `run-local`: 0 when the agent called `finish`, 2 for every other end and for a workflow that failed.

Ctrl-C ends `run` and leaves the task running. `longhaul status <id>` shows it and `longhaul cancel <id>` stops it.

### Long tasks: eviction, compaction and the log

A task that runs for many turns keeps two records of its transcript (ADR-0013). The view is what the model reads, and the log is everything that was said. The `context` section of the config sets when the view shrinks:

| Setting | Default | What it does |
|---|---|---|
| `window_tokens` | 200000 | The budget Longhaul plans for, not the limit of the model. |
| `elide_tool_outputs_after_turns` | 4 | A tool result older than this many turns becomes `[elided: <tool> output, <n> lines, handle <h>]`. The model reads it again with `read_output`. |
| `compact_at` | 0.60 | At this share of the window the older turns are folded into progress notes by the compactor model. |
| `hard_limit` | 0.85 | A request projected above this share is not sent. The view is compacted down to the last two turns and the turn is asked once more. |
| `keep_recent_turns` | 6 | The turns a compaction leaves as they are. |
| `mode` | full | Which tiers run: `full`, `no_compaction` or `none`. See the next section. |

Each compaction writes a `compaction` event with the tokens before and after, and each call of the compactor model has its own row in `cost_ledger`:

```sql
SELECT ts, payload FROM events WHERE task_id = 'lh-local-calc-1a2b3c4d' AND type = 'compaction';
```

The log is a set of blobs under `tasks/<task id>/log/<turn>/`, a `reply.json` and a `results.json` for each turn. `longhaul.context.log.read_log(blobs, task_id)` joins them into one transcript. It is never elided or compacted, so a report can say what the agent did in turn 3 of 150. The view of a task is the `transcript_ref` of the workflow state, under `tasks/<task id>/turns/`.

### Pull requests and labels

A worker with `LONGHAUL_GITHUB_TOKEN` registers the GitHub activities as well (LH-306). A task of an issue, which has an `owner/name` repo and an issue number, shows its state on the issue through labels, and ends with a pull request. The pull request is ready for review when a verifier passed the work and at least one command of the pre-check backed it (see "The verifier and the send-back loop"), and a draft when not:

| When | What the issue shows |
|---|---|
| The task starts | A "Picked up" comment, and the label `longhaul:working` |
| The agent calls `finish`, or the work of a coder with a plan passes its review | The phase is `delivering`. The branch `longhaul-pr/<n>` is pushed and the pull request is opened, as a draft after `finish` and ready for review after a review |
| The pull request is open | `longhaul:working` is removed and `longhaul:pr-open` is added |
| A pause for a person (Phases 5 and 7) | `longhaul:needs-human` is added, and removed when the task resumes |

The pull request branch has one commit on top of the base commit, whatever number of checkpoints the agent made. Its tree is the tree of the last checkpoint, so its diff is the diff of the scratch branch. The commit is titled from the summary the agent gave to `finish`, as a conventional commit such as `fix(calc): add returns the sum`, and its author and committer come from `LONGHAUL_GIT_AUTHOR` (default `longhaul <longhaul@users.noreply.github.com>`). The branch is built on the host, from the bare repo of the task, and pushed there with the token. Nothing is ever pushed to the default branch.

The body comes from `templates/pr_body.md.j2`: the summary, the changes per file, how it was tested (the counts of the last `run_tests` call, and a line `Verified: <counts>` when a verifier passed the work) and `Closes #<n>`. The worker finds the folder through `LONGHAUL_TEMPLATES_DIR`, which defaults to `templates/` in the checkout and is set in the worker image. Text from the model is shown with its mentions and issue references broken up, so it cannot notify a person or close another issue.

Every write has an idempotency key, so a retry or a replay changes nothing on GitHub. The pull request is found again by its branch even if the `side_effects` table is lost. A task that ends in an error or a cancel leaves its labels as they are for now (LH-507). A task whose work changes no file ends with the error `nothing to deliver: no file changed`, because GitHub refuses a pull request with no commit.

### The prompt cache

Every request of a task starts with the same bytes: the tool list, then the system prompt (the role and rules, the tool guide, the task statement and the memory block). The plan and the progress notes are in the first user message, behind that prefix, so a plan update or a compaction does not touch it (ADR-0013). The Anthropic provider marks up to four places in the request for the cache: the last tool, the end of the system prompt, the last assistant turn that has a message after it, and the newest message that holds an elided result.

The `turns` table has the result for every model call. `cache_ratio` is `cache_read_tokens / (input_tokens + cache_read_tokens)`, so 0.9 means that nine tenths of the input tokens, not counting the ones just written to the cache, were read from it:

```sql
SELECT turn_index, input_tokens, cache_read_tokens, cache_write_tokens, round(cache_ratio::numeric, 2)
FROM turns WHERE task_id = 'lh-local-calc-1a2b3c4d' ORDER BY turn_index, attempt;
```

The first turn writes the prefix and reads nothing. On the second turn only the tools and the system prompt are read (about 1,900 tokens for the coder), and the first tool result is new input, so the ratio is low. It climbs as the conversation grows, because each turn reads the turns before it from the cache. A ratio of 0 on every turn means the prefix changes between requests, or that it is shorter than the model's minimum for caching: 512 tokens on the 5.5 models, up to 4,096 on older ones. A model that is under its minimum is not told, and `cache_write_tokens` stays 0. A drop on one turn is normal after a compaction or a plan update, which write the conversation again. To check the cache against the real API, run `uv run pytest -m live tests/live/test_cache_live.py` with `ANTHROPIC_API_KEY` set. It makes four small requests.

### Switching the tiers off, and what a task records about its context

The evaluation compares runs with the tiers on and off (plan section 8), so `context.mode` turns them off one at a time:

| Mode | Tier 1: elide old tool outputs | Tier 2: compact into notes | A full window |
|---|---|---|---|
| `full` | on | on | one forced compaction, then the turn is asked again |
| `no_compaction` | on | off | the run ends with `error` and the summary `context limit` |
| `none` | off | off | the run ends with `error` and the summary `context limit` |

The guard in front of the model call stays on in every mode, so a request above `hard_limit` is never sent. In the two modes without compaction nothing can relieve it, and the run ends there. It is a normal end: the workflow completes with that outcome, the transcript and the log are kept, and nothing is retried.

`context.mode` in the config is the default of a worker. A single run can ask for its own, and that wins:

```bash
uv run longhaul run --path tests/fixtures/repos/calc --issue-file tests/fixtures/repos/calc/ISSUE.md \
  --provider scripted --script tests/fixtures/scripts/calc_fix.yaml --context-mode none
```

The run puts the mode in the workflow memo, next to the script. The workflow hands it to every model turn and every compaction, and the header line of `run` says `context mode none`. A run that names no mode uses the config of the worker that picks up each turn, so all workers of a queue should share one `context.mode`.

Each task row keeps five totals for the report (`tasks`, migration `0010_task_context_totals`). Activities write them, and none of them is added to a counter, so a retried activity cannot count a call twice:

| Column | What it holds | Where it comes from |
|---|---|---|
| `input_tokens_total` | Tokens the model read that were not in the prompt cache: input plus cache writes | the sum of the `cost_ledger` rows of the task |
| `output_tokens_total` | Tokens the model wrote | the same rows |
| `cache_read_total` | Tokens served from the prompt cache | the same rows |
| `compactions` | Compactions that changed the view | the number of different `compaction` events |
| `max_fraction` | The largest share of `window_tokens` that one request took, as `llm_turn` projected it before sending | the maximum over the requests |

The three token totals do not overlap, and the compactor's own calls are in them, so they add up to what the task cost to run. Tokens per task is their sum, and the cache ratio is `cache_read_total / (input_tokens_total + cache_read_total)`:

```sql
SELECT id, input_tokens_total, output_tokens_total, cache_read_total, compactions, max_fraction
FROM tasks WHERE id = 'lh-acme-calc-42';
```

A task has these columns only when it has a row in `tasks`. The poller makes the row when it starts a task. `longhaul run` does not, so a task started with it has its calls in `cost_ledger` and its compactions in `events`, and the totals stay at 0. The cost ledger works the same way.

### The plan

A task can plan before it codes (LH-501, ADR-0007). The planner is a role of the same workflow. It has the tools that read and run (`list_dir`, `read_file`, `search`, `run_tests`, `run_command`, `read_output`) and the tool `submit_plan`, and it has no tool that writes a file. Any file that one of its commands writes is deleted again when the command ends, so the coder starts from the tree the task began with.

Planning is a choice of the task. The poller turns it on for every task it starts from an issue. For `longhaul run` add `--plan`, and give the script steps for the `planner` role as well as for the coder:

```bash
uv run longhaul run --plan --path DIR --issue-file FILE --provider scripted --script FILE
```

A task started without `--plan` goes straight to coding, as it did before the planner.

| Where | What shows the plan |
|---|---|
| `longhaul run`, `longhaul status` | The status line has `plan v1` once the plan is stored and posted, and the phase moves from `planning` to `coding`. |
| Postgres | One row for each version in `plans`, with the plan as JSON and the status `proposed`. |
| The issue | A task of an issue posts the plan as a checklist, once. The comment ends with the hidden marker for the key `<task id>:plan_comment:1`, so a retry finds it and posts nothing new. A task on a local folder posts nothing. |

```sql
SELECT version, status, plan->>'summary' FROM plans WHERE task_id = 'lh-acme-calc-42';
```

The planner has 20 turns to submit. That number is `acts.PLANNER_MAX_TURNS`, and `loop.planner_max_turns` must equal it: a worker whose config holds another number stops at the start and names the setting. A plan has 2 to 8 steps, each with a `done_when`, and at least one acceptance check. A plan that does not fit gets one repair turn: the answer to `submit_plan` lists what is wrong, and the planner may call it again. These ends are an `error` outcome with a fixed summary:

| Summary | Meaning |
|---|---|
| `planner submitted an invalid plan twice: <problems>` | The repair turn did not fix the plan. |
| `planner submitted no plan in 20 turns` | The planner used all its turns reading and running commands. |
| `no tool calls` | Two replies in a row had no tool call. The first one gets a nudge to call `submit_plan`. |

The turns of the planner count against `task_turns` like any other.

### The coder and the steps of the plan

A coder that has a plan works it one step at a time (LH-503). It reads the plan in the first message of its transcript, after the line that starts the work, with the status of each step: `todo`, `doing`, `done` or `skipped`. It is offered three tools in place of `finish`:

| Tool | What it does |
|---|---|
| `update_step(step_id, status, note)` | Sets the status of one step. A step that is `done` gets a checkpoint. A step id or a status the plan does not have is answered with an error and changes nothing. |
| `submit_for_review(summary)` | Ends the coding and starts the review (next section). A task of an issue goes on to open the pull request once the work passes. |
| `ask_human(question)` | Asks a person. The pause flow is not built yet (LH-506), so the task records the question and ends as `stuck`, in the phase `paused`, with the question in the summary. |

A task without a plan keeps `finish` and is not changed. A call to `finish` in a task with a plan still ends it, without a review and with a draft pull request, for a worker or a history from before the verifier.

An update rewrites the plan block in the first message before the next model call, so the cache of the conversation after that message is written again once for each update (ADR-0013). The system prompt and the tool list do not change.

| Where | What shows the steps |
|---|---|
| `status()` of the workflow | The plan with the status of each step. `TaskStatus` has no field for it, so ask for the query by name with the type: `handle.query("status", result_type=acts.PlannedStatus)`. A plain `TaskStatus` read still works. |
| Postgres, `events` | One `step_updated` event for each update, with the step, the status, the note and the head of the work. |
| Postgres, `checkpoints` | One row of kind `step` for each step marked `done`, with its `step_id`. |
| The pull request | One commit for each done step, in the order the steps were finished, titled from the step's description (`fix(calc): change the operator in add()`). The work after the last step is a last commit titled from the summary. A step with no change of its own makes no commit, and together the commits are the change of the scratch branch to the byte. |

```sql
SELECT step_id, sha, turn_index FROM checkpoints WHERE task_id = 'lh-acme-calc-42' AND kind = 'step' ORDER BY id;
```

### The verifier and the send-back loop

A task with a plan does not trust `submit_for_review` (LH-504). The phase becomes `verifying`, and the work is checked in two halves. It passes only when both say so.

| Half | What it does |
|---|---|
| The pre-check | Runs the test command of the repo, then every acceptance check of the plan that is a command, then `git_diff`. All of it is read-only: a file that a command writes is put back. A command that exits with a code other than 0, or does not finish, fails the review, whatever the verifier says. A repo with no test command and a plan with no command in its checks runs none, and the verdict decides alone. |
| The verdict | A verifier with a fresh transcript gets the plan, the pre-check and the diff, reads the code with `list_dir`, `read_file`, `search`, `git_diff`, `run_tests` and `read_output`, and ends with `submit_verdict`: `passed`, `tests_run`, `tests_summary`, `issues` and `send_back_step`. It has 15 turns for a round. |

An acceptance check is a command when it starts with `$ ` or is a command in backticks and nothing else. The planner is told the mark. A check without it is read by the verifier and not run, so a sentence is never run as a command.

| Result | What follows |
|---|---|
| The work passes | The task goes on as it did after `finish`. A task of an issue opens the pull request ready for review, with `Verified: <tests_summary>` in the body. When the pre-check ran no command, the counts are the verifier's word only, so the pull request is a draft and has no `Verified:` line. |
| The work fails | The round is counted, the coder gets the user message `Review failed: <issues>. Fix step <send_back_step>.` at the end of its own transcript, and the phase is `coding` again. |
| The work fails the third time | The task ends as `stuck` in the phase `failed`, with the reason and the last issues in the summary. A task of an issue has no pull request and keeps its labels (LH-507). |

The number of failed reviews is `loop.max_verify_rounds`, and it must equal `acts.MAX_VERIFY_ROUNDS` (3): a worker whose config holds another number stops at the start and names the setting. The count is part of the state that crosses a hand-over to a new run.

| Where | What shows the review |
|---|---|
| `status()`, `longhaul status` | The phase `verifying` while the pre-check and the verifier run. |
| Postgres, `events` | One `verdict` event for each round, with `passed`, `model_passed`, `precheck_passed`, `tests_run`, `tests_summary`, `issues` and `send_back_step`. `passed` is the result, and the other two show which half failed. |
| Postgres, `tool_calls` | The commands of the pre-check, with the ids `<task id>-verify<round>-tests`, `<task id>-verify<round>-check<n>` and `<task id>-verify<round>-diff`. The task id is part of the id because the table keys its rows by it. |
| The pull request | Ready for review, with a `Verified:` line, after a pass. |

```sql
SELECT payload->>'round' AS round, payload->>'passed' AS passed, payload->>'model_passed' AS model, payload->>'precheck_passed' AS precheck, payload->'issues' AS issues
FROM events WHERE task_id = 'lh-acme-calc-42' AND type = 'verdict' ORDER BY id;
```

These ends of the review are fixed, so a caller can tell them apart without reading prose:

| Outcome | Meaning |
|---|---|
| `stuck`: `the work failed review 3 times; last review: <issues>` | Three reviews in a row failed. The coder was sent back after the first two, and the third ended the task. |
| `error`: `verifier submitted an invalid verdict twice: <problems>` | The repair turn did not fix the verdict. This is not a failed round. |
| `error`: `verifier submitted no verdict in 15 turns` | The verifier used all its turns reading. |
| `error`: `turn limit` | The turns of the review count against `task_turns`, and the message that sends the coder back takes one too. A coder that submits with no turn left gets no review, and no command of the pre-check runs. |

### Stopping a worker

SIGTERM and Ctrl-C start a graceful stop. The worker takes no new work, waits up to 30 seconds for the activities that are running, and cancels the rest. Temporal notices the missing answer when the heartbeat timeout passes (15 to 60 seconds) and runs the activity again on the next worker, which restores the checkpoint it started from first. Start a worker again and the task goes on. SIGKILL does the same without the waiting.

### What the worker logs

The worker logs one line when an activity starts and one when it ends or fails:

```
INFO longhaul.durable.activity_log: activity tool_exec started: task lh-local-inventory-1a2b3c4d, attempt 1, turn 8, run_command
INFO longhaul.durable.activity_log: activity tool_exec ended: task lh-local-inventory-1a2b3c4d, attempt 1, turn 8, run_command
```

A line names the activity, the task, the Temporal attempt and, for `llm_turn` and `tool_exec`, the turn and the tool. It never holds arguments or output, which can hold code and secrets. After a SIGKILL these lines are all the worker leaves behind. A `started` line with no `ended` after it is the activity that was cut off. An `ended` line as the last one means the worker was between two activities. A line with `attempt 2` or higher is a retry. The chaos tests read these lines to say where each kill landed.

### The worker in Docker

The compose service `worker` is optional. It is in the profile `full`, so `make up` does not start it:

```bash
echo "LH_CKPT_DIR=$HOME/.longhaul/ckpt" >> .env      # an absolute path that exists on the host
mkdir -p "$HOME/.longhaul/ckpt"
docker compose --profile full up -d --build worker
```

The service builds `docker/worker/Dockerfile` and mounts two things from the host. The checkpoint folder is mounted at the same path inside, because the worker gives that path to the Docker daemon as the source of a mount in each sandbox. The Docker socket is mounted so that the worker can start the sandboxes.

**Security note.** Access to the Docker socket is the same as root on the host. Anything that runs in the worker, or takes it over, can start a container that mounts the whole disk. The sandboxes keep repo code away from the worker (no secrets in their environment, no network after the setup, no capabilities), but the worker itself is trusted. Run it only on a machine you would give that trust, never on a shared host, and publish no port from it beyond the metrics port below, which compose binds to `127.0.0.1`. The container runs as root, because the group of the socket differs between hosts and a non-root user could not open it. A worker started with `uv run longhaul worker` has the same reach, because your user can use Docker.

The folder that `--path` names has to exist inside the worker container to be read, and the example folders do not. For local folders use the worker from `uv run`, and use the container for a worker that fixes cloned repos (Phase 3).

The same image runs on AWS. `docs/deploy-aws.md` describes the Terraform that puts workers on ECS with EC2 capacity, because a Fargate task cannot run the Docker daemon that the sandboxes need.

## Start tasks from GitHub issues

The poller finds open issues that carry the label in `poller.label` (`longhaul` in the example config) on the repos with `allowlisted: true`, and starts one task for each. It is a Temporal Schedule that runs once every `poller.interval_seconds`. The design is in ADR-0008.

```bash
export LONGHAUL_GITHUB_TOKEN=github_pat_...   # the fine-grained token of ADR-0012
uv run longhaul worker                       # shell 1: the worker runs the polls
uv run longhaul poller install               # shell 2: create the Schedule, once
```

The worker needs `LONGHAUL_GITHUB_TOKEN` to poll. Without it the worker still starts, and each poll fails with a message that names the setting. The compose worker takes the token from your shell or from `.env`. `install` needs the config and Temporal, but not the worker, and it can be run before the worker starts: the first poll waits for a worker.

| Command | What it does |
|---|---|
| `longhaul poller install` | Creates the Schedule `longhaul-poller`, or updates it to `poller.interval_seconds` and the task queue of `LONGHAUL_TASK_QUEUE`. It prints `created`, `updated` or `unchanged`. A second run with the same config changes nothing, and a paused Schedule stays paused. |
| `longhaul poller pause [--note TEXT]` | Stops the polls. A poll that is running finishes. |
| `longhaul poller resume` | Starts them again. |
| `longhaul poller trigger` | Polls now. It works on a paused Schedule, and it does nothing while a poll is running. |

Each poll is a workflow named `longhaul-poller-run-<time>` in the Temporal UI (http://localhost:8233). It returns four counts: the repos it looked at, the labeled issues it saw, the tasks it started and the issues that already had a task. A poll fails when a repo cannot be read, for example a typo in the config or a token without access to it. The other repos are still polled first, and the failure names the repo.

What the poller does and does not do:

- A task id is `lh-<owner>-<repo>-<issue number>`, in lowercase. The task is started once. A row in `tasks` keeps a finished task from starting again while the label stays on the issue.
- Only the allowlist counts. A labeled issue on a repo that is not in the config, or is there with `allowlisted: false`, is never read.
- The poller only reads from GitHub. It posts no comment and sets no label. The task does that itself (LH-306).
- A task for a GitHub repo clones it in `prepare_workspace` (LH-305) and works on it. Nothing pushes the branch or opens a pull request until LH-306 adds the delivery, so a finished task leaves no change on GitHub yet.
- To change the label or the interval, edit the config, restart the worker, and run `longhaul poller install` again for the interval.

## Kill the worker on purpose

A task has to survive a worker that dies at any moment. Two things check it.

**By hand.** `scripts/chaos_kill.sh [SECONDS]` runs the inventory fix with a worker, waits for the first model turn plus SECONDS (default 3), sends SIGKILL to that worker, starts another and waits for the task to finish. It then looks for containers of its own task and prints `no leftovers`. It needs `make up`, `make sandbox-image` and `make migrate`. The scripted provider plays the task, so no key is needed. The script uses a task queue of its own, kills only the pid it started, and removes nothing else, so it is safe on a shared machine. With the default timeouts Temporal notices the dead worker after the heartbeat timeout of the activity that was running (15 to 60 seconds), so the run pauses for that long. The Temporal UI shows the retried activity.

**By test.** `tests/chaos/test_kill_worker.py` starts `longhaul worker` as a process, starts a task, sends SIGKILL to the worker, starts it again, and checks what the run left behind:

- the run ended with `finish`;
- its final diff equals the diff of a run that was never killed, played from the same script;
- its `tool_calls` rows form one line of checkpoints, from the first `pre_sha` to the last `post_sha`, and the commits in the checkpoint repo are exactly those (no killed attempt left a fork). A call that was retried has one row, the one of its last attempt.

The task is `tests/fixtures/scripts/inventory_fix.yaml`: eleven turns, with reads, edits, a command and a test run. Two of its steps exist for the test. Turn 3 waits 3 seconds before it answers (`delay_s`), as a slow model does, so a kill can land inside a model call. Turn 8 is a command that is not safe to run twice: it appends a line to `CHANGES.txt` and then sleeps.

| Marker | Runs | Kills per run | Model | Where it runs |
|---|---|---|---|---|
| `chaos_lite` | 5 | 1 | scripted | `make gate`, and the `chaos-lite` CI job at night and by hand |
| `chaos` without `chaos_lite` | 20 | 1 to 3 | replay | on request, in Phase 9 |

```bash
uv run pytest -m chaos_lite                    # 6 to 9 minutes
uv run pytest -m "chaos and not chaos_lite"    # the full suite, about 25 minutes
```

Both need `make up` and `make sandbox-image`, and the sandbox setup installs pytest from PyPI. They run on a private task queue with a scratch database, a checkpoint folder and task ids of their own, and remove what they made. If a run is killed, a `longhaul_chaos_*` database, containers labelled `longhaul.task_id=lh-local-inventory-*` and their `lh-ws-*` volumes can stay behind. They are safe to remove.

Each run starts its task with a heartbeat timeout of 5 seconds (`acts.Tuning`), so a dead activity is retried in seconds and not after the 15 to 60 seconds of the plan. Activities that have no heartbeat timeout, such as `append_results`, are retried only when their start-to-close timeout of 2 minutes passes.

**Where the kills land.** The five `chaos_lite` runs are:

| Test | The kill |
|---|---|
| `test_kill_during_tool_exec_resumes` | inside the command of turn 8, after it wrote its line and before the call ended |
| `test_kill_during_llm_turn_resumes` | one second into the wait of turn 3 |
| `test_kill_between_activities` | right after the first `edit_file` call ended |
| `test_a_kill_at_a_random_moment_resumes` (two seeds) | when the k-th activity of the worker starts, plus a seeded pause that is no longer than that activity took in a run that was not killed. The seed draws k from the first model turn to the first 85 percent of the activities of that run |

Each of the first three asserts the kill point from the last activity line in the worker's log, so a kill that misses its aim fails the test and does not pass for the wrong reason. A seeded kill counts activities and not seconds, so a run that is faster or slower than the reference, because the machine is quieter or busier, still reaches its kill. The test prints the activity each seed chose and the place where the kill landed (`pytest -s`). The full suite draws 1 to 3 kills for each of 20 seeds. Its later kills wait until the new worker has finished an activity, so they fall in new work and not in the retry of the activity that was killed (an activity gives up after three attempts). It also hands the run over to a new one every four turns (Continue-As-New), so the kills also fall in the hand-over.

**The mutation check.** The test must fail when the recovery code is gone. To see it, comment out `await self._checkpoints.restore(sandbox, pre_sha)` in `ToolActivities._start_from` (`src/longhaul/durable/activities/tools.py`) and run `uv run pytest tests/chaos/test_kill_worker.py::test_kill_during_tool_exec_resumes`. The retried command appends its line to the half done tree of the first attempt, `CHANGES.txt` ends up with the line twice, and the test fails on the diff. Put the line back afterwards. The result of this check belongs in the gate report.

## Record and replay model calls

The full chaos suite must not depend on what a model says, so it plays a recording. A recording is a folder with one JSON file per model call: the request, the reply, and the turn. Two settings control it:

| Setting | Meaning |
|---|---|
| `LONGHAUL_PROVIDER=replay` | The provider plays the folder in `LONGHAUL_RECORDINGS_DIR`. |
| `LONGHAUL_RECORD=1` | The provider named by `LONGHAUL_PROVIDER` is wrapped, and every call it makes is saved into `LONGHAUL_RECORDINGS_DIR`. Not with `replay`. |

Both work for `longhaul worker` and for `longhaul run-local`, which reads the provider from `LONGHAUL_PROVIDER` when you give no `--provider`. A worker that records needs a provider of its own, so a scripted worker needs `LONGHAUL_SCRIPT` (tasks that bring a script are not recorded).

**Matching.** A call is found by its turn and the SHA-256 of its request: the model, the system prompt, the messages, the tools and `max_tokens`, as JSON with sorted keys. When no recording has that hash, the provider plays the first recording of the same turn and logs a warning with the hash (risk R6 in the plan). That happens when a prompt or a tool description changed after the recording, and also for any turn that follows a `run_tests` call, because its output holds the seconds the tests took. A turn with no recording at all is an error that is not retried. Record again when the warnings are many.

**Recording a real run (manual, Phase 9).** The recording of the inventory fix with a real model is made once, by hand, because it needs a key and costs money:

```bash
rm -f eval/chaos/recordings/inventory/*.json
LONGHAUL_RECORD=1 LONGHAUL_RECORDINGS_DIR=eval/chaos/recordings/inventory \
  uv run longhaul run-local --provider anthropic \
  --path tests/fixtures/repos/inventory \
  --issue-file tests/fixtures/repos/inventory/ISSUE.md
```

Record without kills, and commit the folder. The first recording of a request is kept, so a folder that holds an older run must be emptied first. The full chaos suite plays this folder when it holds recordings. Until then it records a scripted run of `inventory_fix.yaml` through the same code, in a temporary folder, and plays that.

## Metrics and the dashboard

Each worker serves Prometheus metrics at `/metrics`, and the compose stack shows them in a Grafana dashboard. The metrics are in `src/longhaul/telemetry/metrics.py`.

| What | Where |
|---|---|
| The metrics endpoint | `http://127.0.0.1:9464/metrics`. Set the port with `LONGHAUL_METRICS_PORT` (0 lets the system pick one) and the address with `LONGHAUL_METRICS_HOST`. The worker logs the address when it starts. |
| Prometheus | `http://127.0.0.1:9090`, or `LH_PROMETHEUS_PORT`. It scrapes `host.docker.internal:9464` every 15 seconds (`docker/prometheus/prometheus.yml`). |
| Grafana | `http://127.0.0.1:3000`, or `LH_GRAFANA_PORT`. Anonymous visitors are viewers. The dashboard is called Longhaul. |
| The dashboard | `docker/grafana/dashboards/longhaul.json`, loaded by `docker/grafana/provisioning/dashboards/longhaul.yaml`. Grafana reads the file again every 30 seconds. The file is the only copy, so Grafana does not let the dashboard be saved or deleted from its screen. Edit the file instead. |

Check a running worker with `curl -s http://127.0.0.1:9464/metrics | grep '^longhaul_'`.

| Metric | Counts |
|---|---|
| `longhaul_tasks_total{outcome,repo}` | Tasks that ended, by outcome (`pr_opened`, `stuck`, `budget_exhausted`, `cancelled`, `error`) |
| `longhaul_active_tasks` | Tasks the worker is running now |
| `longhaul_turns_total{role}` | Model calls that returned a reply |
| `longhaul_llm_cost_usd_total{model,repo}` | Dollars the cost ledger priced for model calls |
| `longhaul_llm_errors_total{kind}` | Model calls that failed, by provider error kind |
| `longhaul_tool_duration_seconds{tool}` | How long tool runs took (histogram) |
| `longhaul_compactions_total` | Transcript compactions (counted from the `compaction` event, LH-404) |
| `longhaul_stuck_total{reason}` | Stuck detections (counted from the `stuck` event, LH-505) |
| `longhaul_sandbox_recreated_total` | Sandboxes rebuilt after the container was lost |
| `longhaul_activity_retries_total{activity}` | Activity attempts after the first |
| `longhaul_cache_ratio` | The share of a prompt that came from the cache (histogram) |

The activities update the metrics and the workflow never does, because a workflow runs again on every replay. A run reaches the end of its task through one `record_event` call with the type `outcome`, which is also a row in the `events` table. The count is made before the row is written, so a task that ends while Postgres is down is still counted.

Things to know when you read the numbers:

- The counters belong to one worker process and start again at zero when it restarts. Prometheus functions such as `rate()` and `increase()` handle that.
- `longhaul_active_tasks` counts the tasks a process has seen start, or has seen again after a hand-over. A task that Temporal ends from outside, by a terminate or a timeout, stays in the count until the worker restarts.
- A task that was started from a local folder has the repo label `local`, whatever the folder. A tool name that the registry does not know is counted as `unknown`.
- Two workers on one machine need two ports. The second one stops at the start with an error that names `LONGHAUL_METRICS_PORT`. Add its port as a second target under `worker` in `docker/prometheus/prometheus.yml`.
- On Docker Desktop, Prometheus reaches a worker that listens on `127.0.0.1`, and it reaches the compose service `worker` through the port that service publishes on `127.0.0.1:9464`. On Linux the container comes through the docker bridge, which a port published only on `127.0.0.1` does not answer. A worker on the host needs `LONGHAUL_METRICS_HOST=0.0.0.0` there, with the port closed to the network by a firewall. To scrape the compose service `worker` on Linux, put `worker:9464` in `docker/prometheus/prometheus.yml` in place of `host.docker.internal:9464`, since both services share the compose network. Do not list both on Docker Desktop: it would scrape the compose worker twice, and every sum on the dashboard would double.
- A checkout that was running before this change has a Grafana without the dashboards folder. Run `docker compose up -d grafana` once. It recreates Grafana and nothing else.
- Every checkout of the repo, a git worktree included, belongs to the one compose project `longhaul`, and its bind mounts name the folder of the checkout that started the container. `tests/integration/test_grafana.py` runs `docker compose up -d --wait prometheus grafana`, which recreates both containers with the files and the port variables of the checkout it runs in. Run it from a worktree only with `LH_GRAFANA_PORT` and `LH_PROMETHEUS_PORT` exported as in the main checkout, or the new containers ask for ports that another project may hold. When a worktree is deleted, run `docker compose up -d prometheus grafana` once from the main checkout, so that the two containers stop pointing at the deleted folder.

`tests/integration/test_metrics.py` runs a scripted task and reads every metric back from `/metrics`. `tests/integration/test_grafana.py` starts Grafana and Prometheus from compose, finds the dashboard through the Grafana API and sends each query of each panel to Prometheus.

## Run the evaluation

`eval/run.py` runs a task set on the durable stack and writes the numbers that the README quotes. Every config goes through this one command, so the numbers can be compared.

```bash
make up && make sandbox-image && make migrate
# shell 1: the worker, with the config you mean to measure
uv run longhaul worker

# shell 2: a dry run on two tasks with scripted answers (its results go to a scratch folder,
# because they are not model results), then a real run
uv run python eval/run.py --set custom --config full --limit 2 --run-id dry-1 \
  --script tests/fixtures/scripts/eval --results-dir "${TMPDIR:-/tmp}/longhaul-dry"
uv run python eval/run.py --set custom --config full --run-id headline-1
```

The runner needs the settings that `longhaul run` needs, plus `LONGHAUL_DATABASE_URL`, because the cost of a task is the sum of its rows in the cost ledger. It must see the same `LONGHAUL_CKPT_DIR` as the worker, because it reads the agent's patch from the checkpoint repo.

| Flag | Meaning |
|---|---|
| `--set custom` | The 15 custom tasks. `swebench` is accepted and refused until LH-902. |
| `--config full` | The product. `naive` and `single_shot` are accepted and refused until LH-903. |
| `--run-id NAME` | Names the folder `eval/results/NAME`. A run id is never reused: the runner stops when the folder exists, even an empty one, or when Temporal knows a workflow `lh-eval-NAME-...`. |
| `--limit N` | Only the first N tasks, in the order of `tasks.yaml`. |
| `--concurrency K` | How many tasks run at once. The default is 1. |
| `--model ID` | Checked against the coder model in the worker's config, and recorded. |
| `--memory on\|off`, `--context-mode full\|no_compaction\|none` | Recorded in each result file. The worker reads memory and context settings from its own config, so start it with the settings you mean to measure. |
| `--provider scripted --script PATH` | Plays a script. A file serves every task. A folder holds one `<task id>.yaml` per task. `--script` alone means the scripted provider. Every other `--provider` is only recorded: the worker answers with its own provider. |
| `--results-dir`, `--pause-timeout-s` | Where results go (default `eval/results`), and how long a pause lasts before the runner ends the task (default 60). |

### What a run does

1. Checks everything it can before it starts a task: the model, each repo folder (it must match the `base_sha` in `tasks.yaml`, and must hold no hidden test), the scripts, and that the run id is new. The results folder is made last, so a run that stops here leaves nothing behind.
2. Starts each task as a workflow with id `lh-eval-<run id>-<task id>`. The task is a folder on this machine (`local:/abs/path`), so nothing is written to GitHub. The plan gate is off, no hints are sent, and the caps are the ones of plan section 8.4: 2 dollars, 150 turns, 120 minutes.
3. Watches the task. A pause marks it `intervention_needed`. A pause that lasts a minute ends the task with a cancel, because nobody is there to answer. A task that runs past its time limit is ended the same way, and one that ignores the cancel for 30 seconds is terminated.
4. Reads the patch from the checkpoint repo and grades it. A task counts as resolved when the agent finished (outcome `pr_opened`) and the grader passed the patch. A task that ended any other way is not graded and is not resolved.
5. Writes `<task id>.json` and `<task id>.patch`, removes the sandbox and the checkpoint repo of the task, and at the end writes `summary.json`.

When the runner itself fails on a task, for example a lost connection or a database error, that task gets a result with outcome `error` and `resolved` null, and the other tasks go on. The summary counts it as attempted and not graded. Ctrl-C ends the whole run: the tasks that are running are terminated, no summary is written, and the run id cannot be used again.

A result file holds the outcome, `resolved`, the grader's one-line reason, the cost and tokens from the ledger, the cache ratio, the wall time, the turns, the model, the commit of this checkout and the flags of the run. `scripted` is true when a script played the answers, and `provider` is what `--provider` asked for (null means the worker's own). A scripted run costs nothing and fixes what its script fixes, so keep its files out of `eval/results`, which the report reads, and leave them out of any number you quote. `compactions`, `verify_rounds` and `stuck_reasons` start at 0 and empty until the phases that count them have landed. `resolved` is `null` when the grader itself failed, and the summary counts those tasks as `ungraded`. The summary has the resolve rate over every started task, the cost per resolved task, the medians of wall time, tokens and cache ratio, and the number of tasks that needed a person. Intervals and paired tests come from the report (LH-905).

### The custom task set

`eval/custom/tasks.yaml` lists 15 tasks on three small repos that we own, five on each: `calc`, `textkit` and `inventory`. They are 5 bug fixes, 3 features, 3 refactors, 2 dependency changes and 2 flaky-test fixes, as plan section 8.2 asks.

| Folder | What it holds |
|---|---|
| `eval/custom/repos/<repo>` | The repo as the agent gets it. Its own tests pass. |
| `eval/custom/hidden_tests/<repo>` | The test file of each task. It fails on the repo as it is and passes on a correct fix. The agent never sees it. |
| `eval/custom/reference/<id>.patch` | A fix that passes the hidden tests. It is kept to prove the tests can pass, and the agent never sees it either. |

`base_sha` is the commit that `scripts/make_fixture_git.py` makes of the repo folder, so it changes whenever a file in the folder changes. Run `uv run python eval/custom_tasks.py` for the current values and copy them into `tasks.yaml`. A unit test and the runner both refuse to go on while they differ.

Hidden tests stay out of the sandbox in three ways. The repo folders hold none of them. The runner reads each folder the way the worker copies it, and stops when it finds a hidden test or a reference patch there, by name or by content. And the integration test `test_hidden_tests_not_visible` searches a real sandbox for them.

### The grader

`eval/grade_custom.py` starts a fresh sandbox, builds the base commit of the task in it from a git bundle, runs the setup with the network on, and cuts the network. It then applies the patch, copies in the hidden tests and runs the repo tests and the hidden tests. Both runs must pass. Nothing of the repo runs on the host. Grade a patch by hand with:

```bash
uv run python eval/grade_custom.py calc-01 --patch eval/custom/reference/calc-01.patch
```

A failed setup raises an error and does not mark the patch as wrong, because the setup does not depend on the patch.

`make itest` does not run `test_every_task_fails_before_and_passes_after_its_reference_patch`, which sets up 30 sandboxes. Run it with `uv run pytest -m slow tests/integration/test_eval_runner.py` after you change a repo, a hidden test or a reference patch.

## CI

Workflow: `.github/workflows/ci.yml`. It runs on pull requests into `dev` and `main`, on pushes to those branches and to `feat/`, `fix/`, `spike/` and `chore/` branches, and by hand. A new push to a pull request cancels the older run. CI has no secrets and always uses the scripted provider. A unit test scans every file in `.github/workflows/` and fails on any use of the `secrets` context, including `secrets['NAME']`, `toJSON(secrets)` and `secrets: inherit`.

| Job | What it does |
|---|---|
| `lint` | `ruff check` and `ruff format --check` |
| `types` | `mypy src eval`. Strict checks apply to `longhaul.contracts`, `longhaul.durable`, `longhaul.context` and `longhaul.cost` only |
| `unit` | unit tests with coverage, failing under 80 percent; uploads `coverage.xml` |
| `integration` | Postgres with pgvector and MinIO as service containers, a Temporal dev server in Docker, then the integration tests (see "Test markers and `make gate`" below). The Grafana test starts Prometheus and Grafana from `docker-compose.yml` itself |
| `e2e` | starts Postgres, MinIO and the Temporal dev server like `integration`, builds the sandbox image, then runs the end-to-end tests with the scripted provider on a real sandbox: `longhaul run-local` on the `calc` fixture, and `longhaul worker` with `longhaul run` on the same fixture, stopped and restarted mid-run (see "Run tasks on the durable stack") |
| `chaos-lite` | the `chaos_lite` tests (see "Kill the worker on purpose") on the same services as `integration`. It runs on the nightly schedule (03:17 UTC, on the default branch) and by hand, not on pushes or pull requests |
| `security` | gitleaks over the full git history, and `pip-audit` over the locked dependencies |
| `compose` | `docker compose config -q`, so a broken compose file fails |
| `docs` | `quarto render site`, so a broken include, page or diagram fails the pull request (see "Docs site" below) |
| `terraform` | `terraform fmt -check`, `init -backend=false` and `validate` on `deploy/terraform/aws`, with the Terraform version pinned in `versions.tf`. It never plans or applies, because CI has no AWS credentials (see `docs/deploy-aws.md`) |
| `ci` | waits for all the jobs above and fails unless every one succeeded |

The required check is `ci`. Branch protection points at that one name, so a new job must be added to the `needs` list of `ci`. `tests/unit/test_ci_config.py` fails if any job is missing from that list. The one exception is a job that only a schedule or a manual run starts, such as `chaos-lite`: a job that did not run is "skipped", and `ci` counts that as a failure, so `ci` does not wait for it. The test holds such a job to an `if` that lets through `schedule` and `workflow_dispatch` only. A failed nightly run shows on the workflow run and not on a pull request.

The Temporal server in the `integration` job is the image that `docker-compose.yml` uses (`temporalio/temporal:1.5.1`, see ADR-0002), so CI and `make up` run the same server. CI also pins it by digest, because a tag can be moved to other content. When the version in the compose file changes, change the tag and the digest in `ci.yml` too. To find the digest of a new tag, run `docker pull temporalio/temporal:<tag>` and read it from `docker image ls --digests temporalio/temporal`. `tests/unit/test_ci_config.py` fails if the tags differ or the digest is missing.

Dependabot opens its version-update pull requests against `dev` (`target-branch: dev` in `.github/dependabot.yml`), because `main` moves only through phase-gate pull requests. Updates are grouped, so each ecosystem opens one pull request a week instead of one per package. A major Python bump still opens on its own, because it can need code changes and an ADR-0002 update. Dependabot reads that file from the default branch, so it starts working after the first gate merge puts the file on `main`. The setting does not cover Dependabot security updates, which open against `main`. If you turn those on, treat one like a hotfix: merge it, then merge `main` back into `dev` the same day (plan section 5.2).

To run the quick jobs on your machine, use `make ci-local`.

### Test markers and `make gate`

Every test carries two markers:

- The layer comes from the directory. `tests/conftest.py` adds it, so `tests/unit` gives `unit`, and `integration`, `e2e`, `chaos`, `live` and `slow` work the same way.
- The phase, `phase0` to `phase9`, names the phase that introduced the test. You write it, for example `pytestmark = [pytest.mark.unit, pytest.mark.phase2]`.

A test with no phase marker, or a test file outside a layer directory, fails collection with a message that names it. Without the phase marker, `make gate PHASE=N` would never run the test. Test files in different layers may share a name, because pytest imports them by path (`--import-mode=importlib`).

Some tests run only on request: `live` (paid model calls), `slow`, and the full `chaos` suite. `make gate`, `make itest`, `make e2e` and the `integration` and `e2e` jobs all leave them out. `chaos_lite` stays in, because gates run it. To run the others, name the marker, for example `uv run pytest -m slow`.

`make gate PHASE=N` runs `make check`, then pytest with the markers `phase0` to `phaseN`. It stops with an error if `scripts/markers.py` rejects `N`, so a typo cannot select every test.

### Branch protection

To protect `main` and `dev`, run this once as a repo admin with `gh` logged in:

```bash
scripts/protect_branches.sh            # uses the repo of the current checkout
scripts/protect_branches.sh OWNER/REPO # or name it
```

It requires a pull request and an up-to-date `ci` check, and blocks force pushes and deletions.

`enforce_admins` is false on purpose. Admins merge the phase-gate pull requests and push `dev`, so GitHub has to let admins through. The rules bind collaborators and bots, and they do not bind admins: a direct push to `dev` is rejected for a token without admin rights and accepted for an admin. A fine-grained token that the owner mints acts with the owner's rights, so these rules do not stop it. The branch guard in the agent's own code (invariant I5) is what keeps the agent off the default branch. `tests/unit/test_ci_config.py` pins this value, so changing it means changing this paragraph too.

## Docs site

The site in `site/` is a Quarto project. Its pages include the Markdown in `docs/` with the Quarto include shortcode and copy nothing, so the site cannot drift from the docs. The prose that a page adds itself, such as the overview, is short and leaves the numbers that change to tables generated from `docs/`.

| Command | What it does |
|---|---|
| `make site` | renders the site to `site/_site/` |
| `make site-preview` | serves the site with live reload |

You need [Quarto](https://quarto.org/docs/download/) 1.10.18, which is the version CI pins, and `python3` on the PATH. View the pages through the preview server or any local web server. Opening an `.html` file straight from disk can break the table of contents.

Before every render, Quarto runs `scripts/site_pages.py`. It reads `docs/` and writes two folders that git ignores: `site/adr/` holds one page per ADR, and `site/_generated/` holds the ADR index, the list of gate reports, the phase status table, and the diagrams and tables that live in sections 3.1 to 3.3 and 8.1 of the plan. A new ADR in `docs/adr/` or a new report in `docs/gates/` therefore shows up on the site with no edit under `site/`. The script stops the render with a message when a doc loses a shape it relies on, such as an ADR without a `- Status:` line or a renamed plan section.

Things that are easy to trip over:

- `site/_quarto.yml` lists the pages to render. Without that list Quarto renders nothing when a checkout sits under a dot folder such as `.worktrees`.
- A page cannot include a file from `site/_generated/` with the include shortcode. Quarto expands that shortcode while it scans the project, before `scripts/site_pages.py` has run, so on a clean checkout the file is missing and the render stops. `site/filters/fragments.lua` embeds those files later instead, and `tests/unit/test_site_pages.py` fails a page that includes one.
- `site/filters/docs.lua` drops the first heading of an included file, because the page already has a title, turns status words in tables into badges, and sizes table columns by their content.
- `tests/unit/test_docs_style.py` applies the voice rules to the `.qmd` pages as it does to the docs.

### Publishing

The `docs` job in `ci.yml` renders the site on every pull request. `.github/workflows/pages.yml` renders it again and deploys it to GitHub Pages on every push to `main`, and on a manual run.

The owner turns the site on once, in two steps: set the Pages source of the repository to GitHub Actions (Settings, Pages, Build and deployment, Source, GitHub Actions), then add the repository variable `PAGES_ENABLED` with the value `true` (Settings, Secrets and variables, Actions, Variables). Until both are done the workflow renders the site and skips the deploy, so `main` stays green and the site stays offline. The site goes live after the first push to `main` that runs the workflow, which is the first phase gate merge. The address is `https://hoomanesteki.github.io/longhaul-autonomous-coding-agent-github/`, and the README links to it.
