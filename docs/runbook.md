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
| GitHub address | `LONGHAUL_GITHUB_API_URL`, `LONGHAUL_GITHUB_WEB_URL` | Blank means `api.github.com` and `github.com`. A GitHub Enterprise server sets the API address (`https://host/api/v3`) and the web address (`https://host`), and the worker clones from and pushes to `<web address>/<owner>/<name>.git`. The end-to-end test of the issue-to-PR path points them at a FakeGitHub and a folder of bare repos. |

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

A worker picks its embedder from the `memory` section of the config:

```yaml
memory:
  enabled: true     # false: no store, no repo map, no verified commands, no notes in the prompt
  embedder: local   # local needs the memory extra; hash needs nothing and is for tests and CI
  mode: "on"        # "off": the store stays, but no notes reach the prompt (see "Notes in the prompt")
```

With `embedder: local` and no extra, the worker stops when it starts and prints `uv sync --extra memory`, and does not wait for the first task to search. The vectors of the two embedders cannot be compared, so the worker also embeds a few stored notes again before it connects, and refuses a table that the other embedder wrote. To change the embedder of a database, delete the rows in `memory_items` and let the workers build them again. The test and chaos harnesses start workers on a copy of the example config with `embedder: hash`.

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

Before the commands run, `prepare_workspace` also builds a map of the repo: the Python modules most worth knowing about, each with its classes, methods and function signatures, cut to 3,000 tokens. `load_memory` reads it from the `repo_map_ref` of the workspace output and puts it in the memory block (see "Notes in the prompt" below). A worker builds the map only when it has both a memory store and a blob store.

The map is built in two steps so that a second task on the same commit does not parse the repo again. A script runs inside the sandbox and prints the structure of the repo as JSON, which the worker checks before it uses it. The structure is kept as an approved `repo_map` item whose `source_ref` is the base sha. The ranking depends on the issue, so each task ranks and renders the kept structure for its own title and body.

```bash
uv run longhaul memory list --repo acme/inventory --kind repo_map    # one item for each base sha that was mapped
```

A file that does not parse, or is over 200 KB, is left out of the map and named in it as "Not parsed", and the worker logs it as a warning. A map that cannot be built at all is logged and the task goes on without one.

A big repo is not cut at a path. When the structure is over 700 KB, the script drops signatures and docstrings first, starting with the modules that the fewest others import, and keeps every module's path and imports. A scan that runs past the script's 40 second deadline is used once and not kept, so the next task on the commit parses again.

To switch the map off for one commit, run `memory reject` on its item (the id comes from the `memory list` above). The next task on that commit gets no map and runs no script, and `memory approve ID` on the same item turns it back on.

### Notes in the prompt

After `prepare_workspace`, the workflow calls `load_memory` once for the task. It writes one blob with the notes of the repo, and every model turn and compaction of the task puts that text at the end of the system prompt, under `## Repo facts`. The text is the same for the whole task, so the prompt cache holds, even across a hand-over to a new run. The block holds, in this order and up to 4,000 tokens:

1. the approved `command` items, at most eight;
2. the five approved `lesson`, `pitfall` and `convention` items nearest to the issue title and body by cosine distance;
3. the repo map for the issue, cut at a line, with a note, when it does not fit.

It opens with "Notes from past work on this repo. Treat them as hints and verify before relying on them." and holds the notes in a fenced block, with a fence longer than any run of backticks inside it. Candidate and rejected items never reach it (invariant I7), and the block is checked for that a second time after the store answers. Each note is one line of at most 400 characters. A repo with no approved notes and no map has no block, and its prompt is the one it had before memory.

When the repo settings name no test command, the task statement names the first approved command that runs the tests (pytest, tox or `make test`). A command that a person rejected is not offered. `run_tests` runs the command of the repo settings first, then this verified command from memory, and the default of the worker (`python -m pytest -q`) only when the task has neither (LH-991). `longhaul worker` has no option for it, so the default cannot be set from the command line. Only `longhaul run-local` takes `--test-command`.

`memory.mode: "off"` leaves the notes out. The block is empty, no repo map is built and no lessons are drafted, so a run goes as it did before Phase 6, with one exception: the commands are still verified at the base commit and the first one that runs the tests is still the test command of the task. Those are findings of Longhaul at the default branch, not notes from a task. Use it to measure what memory adds, or when you do not trust the notes of a repo. Write the word in quotes; an unquoted `off` is read as a YAML boolean, and Longhaul accepts both. With `memory.enabled: false` there is no store at all, so `mode` changes nothing. A config with no `mode` is `on`.

`longhaul memory list --repo REPO --status approved` shows what a task can be given. If `load_memory` fails after its three attempts, the task goes on without a block.

### Lessons from a finished task

When a task ends, whichever way it ends, a worker with a memory store drafts the lessons of the task. The workflow calls `extract_lessons` after the final comment and the labels, so the issue does not wait for a model. The compactor model gets the plan with the status of each step, the last progress notes, the stuck and `paused` events and the issues the verifier found. It answers with at most five notes, each under 300 characters and typed `lesson`, `pitfall` or `convention`. Its prompt forbids advice that would suit any repo.

The notes are stored as `candidate` items with `source = task`. Their `source_ref` is the pull request number, or the task id when the task has no pull request. They reach no prompt until a person approves them or the pull request merges (ADR-0010).

### Lessons after the review

The poller settles the candidates on every tick. When the pull request of a task is merged, its candidates become `approved` with `approved_by` set to `merged_pr#<number>`, and the next task on the repo can read them. When the pull request is closed without a merge, the candidates become `rejected`: they stay in the table and never reach a prompt. A pull request that is still open decides nothing. Each decision is a `memory_promoted` or `memory_rejected` event on the task, and the poll's result counts them as `promoted` and `rejected`. A promotion names `approved_by` in its payload and a rejection names `decided_by`; events from before LH-991 name `approved_by` for both. When the task recorded no `lessons` event, the event is filed under `<repo>#<number>`, which is not a task id, so it is found by the repo and the pull request in its payload.

A task that opened no pull request, such as a stuck one, keeps its candidates until a person decides. Find them with `memory list --repo OWNER/NAME --status candidate` and decide with `memory approve ID` or `memory reject ID`. The poller never changes an item that a person has decided, even if a pull request is merged later.

```bash
uv run longhaul memory list --repo acme/inventory --status candidate
```

Before they are stored, the worker cuts a note over 300 characters at a word and drops a note that is only generic advice, such as "write tests" or "read the code". A note stays when it names a file, a command or a name of the repo, or when it says at least three more words of substance after the phrase is taken out. A reply that holds no usable note stores nothing and does not fail the task. A task with no plan, no notes and no stuck or verifier events makes no model call.

Each call of the compactor is a row on the cost ledger, and the cost is added to the task. The workflow records a `lessons` event with the status, the `source_ref` and the notes, which the run report reads. The answer is kept in a blob before the first write, so an attempt that is retried after a failed write stores the same notes and does not ask the model again. If the activity fails after its three attempts, the workflow logs it and the task keeps the outcome it had.

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
- A file called `.longhaulimage` is not copied either. It holds the name of one Docker image, and a task that goes through `longhaul worker` (the durable stack, for example from `eval/run.py`) runs in that image instead of `sandbox.image`. The SWE-bench eval writes it, because each instance needs its own environment. `longhaul run-local` does not read it. A file that holds more than one name, or a name that starts with `-`, stops the task before any sandbox exists.

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
uv sync --extra memory   # the example config turns memory on with the local embedder
# shell 1: the worker stays in the foreground
uv run longhaul worker

# shell 2: start a task and follow it
uv run longhaul run \
  --path tests/fixtures/repos/calc \
  --issue-file tests/fixtures/repos/calc/ISSUE.md \
  --provider scripted --script tests/fixtures/scripts/calc_fix.yaml
```

The worker stops at its start with a message that names `uv sync --extra memory` when the extra is missing. Plain `uv sync` and `make setup` leave it out, so run the line above after them. To go without the extra, set `memory.embedder: hash` in your config (see "The memory embedder"), which finds notes by shared words only, or `memory.enabled: false`. The first start with the local embedder downloads the model (66 MB), unless it is cached already.

`make migrate` creates the tables the worker writes to. Both commands read the `LONGHAUL_*` settings from your shell or from `.env` (copy `.env.example`). The worker needs the database, Temporal, the blob store and, for a real model, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. `run` needs Temporal, the blob store and the checkpoint folder, but not the database.

| Command | What it does |
|---|---|
| `longhaul worker` | Polls the task queue (`LONGHAUL_TASK_QUEUE`, default `longhaul`) and runs the activities of every task. Stop it with Ctrl-C or SIGTERM. |
| `longhaul run --path DIR --issue-file FILE` | Starts a task for a folder and follows it. With `--provider scripted --script FILE` the script goes to the worker with the task. `--window N` gives the task a context window of N tokens in place of the config's. `--budgets/--no-budgets` turns the budget checks on (the default) or off; see "Budgets and top-ups". |
| `longhaul status TASK_ID` | Prints the state in Temporal, the status of the workflow, the compactions and the largest request of the task (`context: 5 compactions, max_fraction 0.59`) and, once it ended, the outcome. |
| `longhaul report TASK_ID` | Prints the run report of the task as markdown (see "The run report"). It reads the database only, so it works while the task runs, after it ended, and for a task of `longhaul run`. |
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
| `mode` | full | Which tiers run: `full`, `no_compaction`, `none` or `sliding`. See the next section. |

Each compaction writes a `compaction` event with the tokens before and after, and each call of the compactor model has its own row in `cost_ledger`:

```sql
SELECT ts, payload FROM events WHERE task_id = 'lh-local-calc-1a2b3c4d' AND type = 'compaction';
```

The log is a set of blobs under `tasks/<task id>/log/<turn>/`, a `reply.json` and a `results.json` for each turn. `longhaul.context.log.read_log(blobs, task_id)` joins them into one transcript. It is never elided or compacted, so a report can say what the agent did in turn 3 of 150. The view of a task is the `transcript_ref` of the workflow state, under `tasks/<task id>/turns/`.

A task that has to compact on a short script can ask for a smaller window with `run --window N`. The number goes to the worker in the workflow memo, and every activity that counts tokens uses it in place of `window_tokens`. The cap for one tool result shrinks with it, to 1/32 of the window at most (250 tokens in a window of 8,000), so that a window this small has room for the results of a turn. The prompt and the tool list take about 2,000 tokens, so a window under 3,000 leaves no room.

The long-task fixture `tests/fixtures/repos/longlog` runs the context system under this pressure. It has twelve modules with one planted bug each, and its test command prints 5,000 lines. `tests/fixtures/scripts/longlog.yaml` fixes one module in five turns, 62 turns in all, and `compactor.yaml` answers the compactor with notes that list the modules fixed so far. From the second compaction on its steps need the previous notes in the request, so a run that loses its notes between two compactions gets notes that list nothing and fails the test. The test `tests/e2e/test_long_task.py::test_longlog_many_compactions` runs the command below with `compact_at` 0.6 and `keep_recent_turns` 4, and checks that all twelve modules are fixed, that the task compacted at least three times and that no request took 85 percent of the window. The run compacts five times, at turns 16, 26, 36, 46 and 56, and its fullest request takes 59 percent of the window. The test then asks `longhaul status` for the same two numbers. It takes 2 to 5 minutes, depending on the load of the machine:

```bash
uv run longhaul run --path tests/fixtures/repos/longlog --issue-file tests/fixtures/repos/longlog/ISSUE.md \
  --provider scripted --script tests/fixtures/scripts/longlog.yaml --window 8000
```

### Pull requests and labels

A worker with `LONGHAUL_GITHUB_TOKEN` registers the GitHub activities as well (LH-306). A task of an issue, which has an `owner/name` repo and an issue number, shows its state on the issue through labels, and ends with a pull request. The pull request is ready for review when a verifier passed the work and at least one command of the pre-check backed it (see "The verifier and the send-back loop"), and a draft when not:

| When | What the issue shows |
|---|---|
| The task starts | A "Picked up" comment, and the label `longhaul:working` |
| The agent calls `finish`, or the work of a coder with a plan passes its review | The phase is `delivering`. The branch `longhaul-pr/<n>` is pushed and the pull request is opened, as a draft after `finish` and ready for review after a review |
| The pull request is open | `longhaul:working` is removed and `longhaul:pr-open` is added |
| A pause for a person (Phases 5 and 7) | `longhaul:needs-human` is added, and removed when the task resumes |
| The task ends without a pull request | `longhaul:working` is removed. A stuck or failed task gets `longhaul:needs-human`, and a cancelled one gets neither (see "How a task ends") |

The pull request branch is built on top of the base commit, whatever number of checkpoints the agent made. A task with no plan gets one commit, titled from the summary the agent gave to `finish`, as a conventional commit such as `fix(calc): add returns the sum`. A planned task gets one commit for each done step, titled from the step's description, and a last commit for the work after the last step, titled from the summary. The poller starts every task with planning on, so a pull request from an issue normally has one commit for each step (see "The coder and the steps of the plan" below). In both cases the tree is the tree of the last checkpoint, so the diff of the branch is the diff of the scratch branch. The commit author and committer come from `LONGHAUL_GIT_AUTHOR` (default `longhaul <longhaul@users.noreply.github.com>`). The branch is built on the host, from the bare repo of the task, and pushed there with the token. Nothing is ever pushed to the default branch.

The body comes from `templates/pr_body.md.j2`: the summary, the changes per file, how it was tested (the counts of the last `run_tests` call, and a line `Verified: <counts>` when a verifier passed the work) and `Closes #<n>` (`Refs #<n>` for the draft of partial work, see "How a task ends"). The worker finds the folder through `LONGHAUL_TEMPLATES_DIR`, which defaults to `templates/` in the checkout and is set in the worker image. Text from the model is shown with its mentions and issue references broken up, so it cannot notify a person or close another issue.

A worker talks to github.com unless it is told otherwise. `LONGHAUL_GITHUB_API_URL` names another REST API, and `LONGHAUL_GITHUB_CLONE_BASE` names the folder or server that repos are cloned from and pushed to, as `<base>/<owner>/<name>.git`. It wins over `LONGHAUL_GITHUB_WEB_URL`, which does the same for the web address of a GitHub Enterprise server. The full chaos suite sets the API address and the clone base to point a worker at a FakeGitHub on this machine (see "The full chaos suite"). The token is only sent with git requests to github.com, so a clone base never receives it.

Every write has an idempotency key, so a retry or a replay changes nothing on GitHub. The pull request is found again by its branch even if the `side_effects` table is lost. A task whose work changes no file ends with the error `nothing to deliver: no file changed`, because GitHub refuses a pull request with no commit.

### The run report

A pull request carries a report of how the agent got there, inside a collapsed `<details>` block called "Run report", above the `Closes #<n>` line (`Refs #<n>` on the draft of a stuck task, whose report gives the outcome as stuck). `longhaul report TASK_ID` prints the same text. It has six sections:

| Section | What it shows |
|---|---|
| Overview | The issue, the outcome, the wall time, the turns by role, the cost, the tokens and the cache ratio. |
| Timeline | Planning, the wait for approval, coding, pauses, compactions, recoveries (a sandbox that was rebuilt, a hand-over to a new run) and the verifier rounds, in the order they happened. At most 60 entries. A longer timeline keeps its first and last entries, so that the end of the run is always there, and a row says how many entries were left out of the middle. |
| Tool usage | Calls, errors and time for each tool, busiest first, and a total. |
| Plan | The version in force, who decided on it, and the status each step ended in. |
| Interventions | Who did what and when: plan decisions, hints, budget top-ups, answers to a pause and cancels. |
| Candidate lessons | The items that lesson extraction drafted, from the newest `lessons` event. A task whose lessons are not drafted yet shows none. |

The report is built from the database (`turns`, `tool_calls`, `events`, `plans`, `cost_ledger`, and `tasks` when the task has a row), never from the workflow. The cost, the tokens and the cache ratio are sums over the cost ledger, so they include the compactor and the calls that were retried. The turns are counted from `turns` by role and turn index. A task with no `tasks` row is reported from the other tables and shows as "local task". A task with no rows at all exits `longhaul report` with `no task <id> in the database`.

`deliver` stores the report as a blob (`tasks/<task>/report/run-report.md`) the first time it builds it, and a retried attempt reads that copy, so the body of the pull request is the same bytes every time. The body stays under 60,000 characters: the report is cut at the end of a line, with a note, to the room that the rest of the body leaves. The activity `write_run_report` builds a fresh copy of the report and replaces the blob. The wind-up of a task of an issue calls it once more after the outcome event and the lessons are recorded (LH-991), so the stored copy names the final outcome and the lessons. The body of the pull request is not changed by it. Text that people or the model wrote has its mentions and issue references broken up, as in the rest of the body, and any key that matches the log redaction patterns shows as `[redacted]`.

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
| `sliding` | off | off | the oldest whole turns are dropped until the request fits |

The guard in front of the model call stays on in every mode, so a request above `hard_limit` is never sent. In `no_compaction` and `none` nothing can relieve it, and the run ends there. It is a normal end: the workflow completes with that outcome, the transcript and the log are kept, and nothing is retried.

`sliding` is the window of the naive baseline (see "The baselines" below). Before `llm_turn` measures a request it cuts the oldest turns from the view until the request is under `hard_limit`. A turn is an assistant message with the user message that answers its tool calls, and a cut never separates a call from its result. The first message stays, and a line `[longhaul:turn_offset=N]` in it keeps the turn count, as it does after a compaction. Nothing is summarized, so what leaves the window is gone from the view; the log keeps it. The run ends with `context limit` only when no cut helps, for example when one tool result is larger than the window. Each turn that slides changes the start of the request, so the prompt cache is read only as far as the first message.

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

`longhaul status TASK_ID` prints two of them in a `context:` line, such as `context: 5 compactions, max_fraction 0.59`. It reads the database of `LONGHAUL_DATABASE_URL`, and leaves the line out when no database is set or the task is not in it. For a task with a row the line is the row. For a task of `longhaul run`, which has none, it counts the distinct `compaction` events, and it divides the largest prompt that the provider reported for a model turn of the coder (input, cache reads and cache writes) by the window of the run: the `--window` that the task asked for, or else `context.window_tokens` of the config of the command. That figure is a little different from `max_fraction` of a row, which is the estimate from before the request was sent.

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
| `submit_for_review(summary)` | Ends the coding and starts the review (see "The verifier and the send-back loop"). A task of an issue goes on to open the pull request once the work passes. |
| `ask_human(question)` | Asks a person. The task pauses as it does for a stuck signal (see "The pause and the hint" below) and goes back to coding when someone answers. |

A task without a plan keeps `finish` and is not changed. A call to `finish` in a task with a plan is reviewed like `submit_for_review` (LH-991). Only a history recorded before that change still ends on `finish` with no review and a draft pull request. A reply with no tool call gets one nudge, "Use tools or call submit_for_review.", because the coder is not offered `finish`. A second reply in a row ends the run as `no tool calls`.

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

### The stuck detector

The workflow watches the coder for four ways of going in circles (LH-505). It shows each coder turn that does not end the run to the detector in `roles/stuck.py`, and records one `stuck` event when a rule fires, and then pauses the task for a hint. The planner is never watched.

| Reason | Fires when | Default |
|---|---|---|
| `repeated_failure` | The same command failed the same way this many times. Turns with no failing command do not break the streak. | 3 |
| `tool_errors` | The last this many tool calls all failed. A turn with one good call ends the run. | 5 |
| `edit_churn` | One region of a file was edited this many times in the last 10 turns and the number of passing tests did not move. Two edits are the same region when they share half or more of the lines of the longer one, so a new file followed by fixes in other places of it is not churn. | 4 |
| `no_progress` | In this many turns no test run passed more tests than any run before it, and no plan step was finished. The first test count counts as a gain, and so do the steps done when the window starts. | 15 |

When a turn matches more than one rule, the first row that fits is the one recorded. A signal that fires empties the detector's window, so a pattern that goes on is reported again only after it has built up again from nothing. Because the window does not say how many steps were done before it started, `no_progress` can come one turn late after a signal, and never early. The window holds the last 30 turns in `CarryState.stuck_window`, which crosses a hand-over to a new run, and it serializes to under 8 KB, whatever the characters of the file names, since a path is cut to 48 bytes of JSON.

The limits are the `stuck` section of the config. The workflow cannot read the config file and keeps a copy (`roles/stuck.py`, `DEFAULT_LIMITS`), so a worker whose `stuck.no_progress_turns`, `repeated_failure_count`, `churn_edits` or `consecutive_tool_errors` differs from it stops at the start and names the setting. So does a worker whose `stuck.hint_wait_hours` is not 24 (`acts.HINT_WAIT_HOURS`), since the pause below and the plan gate wait for that long.

`tool_exec` works out the facts that the rules need, because only the activity sees the whole text of a result (`durable/activities/signals.py`). The hash of a failed command covers the command, its exit code and the last 20 lines of stderr. When stderr names no error, the last 20 lines of stdout are added, because pytest prints its report there. Run times and memory addresses are taken out first, so the same failure on two runs hashes alike.

```sql
SELECT ts, payload->>'reason', payload->'evidence' FROM events WHERE task_id = 'lh-acme-calc-42' AND type = 'stuck' ORDER BY id;
```

The count by reason is `longhaul_stuck_total{reason}` (see "Metrics and the dashboard"). Evidence reads like `pytest tests/test_inv.py failed 3 times with the same KeyError at inv.py:42`. The detector runs in the durable workflow only. The in-process loop of `longhaul run` without a worker does not use it.

### The pause and the hint

A stuck signal, or a call to `ask_human`, pauses the task (LH-506). The workflow sets the phase to `paused` and asks the compactor model for a short question. Then it posts the question on the issue and adds the label `longhaul:needs-human`. A task of a folder (`local:`) has no issue, so the text goes into the `paused` event instead (`payload.comment`).

The comment has four parts and fits in 250 words. It ends with the commands that answer it:

```text
## Longhaul needs a hint: repeated failure (pause 1)

### What I'm trying to do
### What I tried
### My current guess
### Question
```

Answer it with a comment from someone who has write access:

| Comment | What happens |
|---|---|
| `/longhaul hint <text>` | The text goes to the coder as "Hint from <user>: <text>" on its next turn. A hint is cut to 2,000 bytes, with a note that says so. |
| `/longhaul budget +$2` | Also unblocks the task, because a top-up may be what it needed. |
| `/longhaul cancel` | Ends the task as cancelled. |

A hint or a top-up removes the label, records a `resumed` event, empties the detector's window and puts the task back in `coding`. The task waits 24 hours. With no answer it ends as `stuck` in the phase `failed`, and the summary says how long it waited. A task pauses at most three times: a fourth signal ends it as `stuck` and says that three hints did not unblock it. Those ends post the final comment and set the final labels like every other end (see "How a task ends").

Each pause has its own comment key, `<task id>:stuck:<n>`, so a retry posts nothing twice. The compactor's calls for the question are on the cost ledger like any others, one row for each call, under the turn that was stuck.

```sql
SELECT type, payload->>'reason' AS reason, payload->>'pause' AS pause, ts FROM events WHERE task_id = 'lh-acme-calc-42' AND type IN ('stuck', 'paused', 'resumed') ORDER BY id;
```

### The plan gate

A repo can ask for a person to read the plan before any code is written (LH-502). Set `plan_approval_required: true` for the repo in the config. A task that plans on such a repo holds its plan after the planner submits it:

| Step | What happens |
|---|---|
| The plan is stored and posted | As above. The phase becomes `awaiting_plan_approval`. |
| The issue asks for a person | The label `longhaul:needs-human` goes on, and a comment says how to answer. A task on a local folder has no issue and asks nobody. |
| `/longhaul approve` | The `plans` row for that version becomes `approved` with `decided_by` and `decided_at`, the label comes off, and the coder starts. |
| `/longhaul reject <feedback>` | The row becomes `rejected`, the label comes off, and the planner revises its plan in the transcript it already has. The feedback reaches it as a message from you. The new version is stored, posted and put to the same wait. |
| No answer for 24 hours | The task ends as `stuck` with the summary `plan not approved: no decision in 24 hours`. The row stays `proposed` and the label stays on, because a person is still needed. |

The planner writes at most three versions. A third rejection ends the task as `stuck` with the summary `plan not approved`. Each version has its own 20 planner turns and its own repair turn, and every turn counts against `task_turns`.

The wait is `stuck.hint_wait_hours` of the config, 24 hours. The workflow keeps its own copy (`acts.HINT_WAIT_HOURS`), so a worker whose config holds another number stops at the start and names the setting.

Things to know:

- **An answer can come early.** An approval or a rejection sent while the planner is still working is kept and used when the plan is ready, without waiting. It applies to one version: a decision is used once.
- **A late answer does nothing.** After the coder has started, `/longhaul approve` and `/longhaul reject` find no plan to decide on.
- **A cancel works at any time**, also during the wait.
- **The gate is not repeated after a hand-over.** A task that has a plan does not plan again, so it does not ask again either.

```sql
SELECT version, status, decided_by, decided_at FROM plans WHERE task_id = 'lh-acme-calc-42' ORDER BY version;
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

### Budgets and top-ups

A task has four limits (LH-702). They are the `budgets` of the repo in the config:

```yaml
budgets: { task_usd: 5, task_turns: 150, task_wall_minutes: 240, daily_repo_usd: 25 }
```

A task that the poller starts checks them before every model turn. The `check_budget` activity compares each limit with the worst case of the call that is about to be made: the input of the next request (the size the last call projected, with room for the results of its tool calls) at the full input price, plus the output cap of that call at the output price, both for the model of the role (planner or coder). The cap is `loop.max_tokens_per_turn`, and `loop.max_tokens_retry` for the retry of a reply that stopped at max_tokens. The retry, the continuation of a paused turn and the ask after a forced compaction are model calls of their own, so each is checked too. It is better to pause a little early than to find out after the bill. What the task spent comes from the `cost_ledger`, so a call that was billed and then retried counts twice. The checks run in this order, and the first one that fails names the pause:

| Reason | The call is held back when | What the task waits for |
|---|---|---|
| `task_turns` | The turns done are equal to `task_turns` plus the turns that were added. | `/longhaul budget`, or `/longhaul cancel` |
| `task_usd` | Spent plus the worst case of the next call is more than `task_usd` plus the dollars that were added. | `/longhaul budget`, or `/longhaul cancel` |
| `wall_clock` | The task has worked more than `task_wall_minutes`. The workflow judges this one from its own clock. | `/longhaul budget`, or `/longhaul cancel` |
| `daily_repo_usd` | What the repo spent since 00:00 UTC, plus the worst case of the next call, is more than `daily_repo_usd`. | Midnight UTC |

The task sets the phase to `paused`, posts one comment under the key `<task id>:budget:<n>` and adds `longhaul:needs-human`. The comment gives the numbers and the exact commands:

```text
## Longhaul paused: task budget (pause 1)

### Numbers
### What happens next

Reply with `/longhaul budget +2` to add $2, or `/longhaul cancel` to stop the task.
```

| Comment | What happens |
|---|---|
| `/longhaul budget +2` | Adds 2 dollars to the budget of this task, and the task goes on. |
| `/longhaul budget +1 turns +25` | Adds 1 dollar and 25 turns. A top-up always has dollars, and a turns top-up carries one as well. |
| `/longhaul cancel` | Ends the task as cancelled. |

The worst case of the next call counts the whole request: after the first call of the planner, the coder or the verifier it is the size of the request before, and for the first call it is an estimate from the system prompt, the tools, the task, the plan, the memory notes and the review brief. The `paused` and `resumed` events of a budget pause carry `kind` `budget`, because the budget pauses and the pauses for a hint both number from 1 and the run report pairs a pause with its answer by kind and number.

A top-up removes the label, records a `resumed` event (`via` is `budget`) and a `budget_added` event with the dollars and turns that came in, and the next check uses the new limits. A top-up that is too small pauses the task again, with its own comment (`budget:2`). A `/longhaul hint` does not answer a budget pause: the task waits for money. With no answer in 24 hours (`stuck.hint_wait_hours`) the task ends with outcome `budget_exhausted` in the phase `failed`, and the summary says which limit and how long it waited.

Three limits behave in their own way:

- **The wall clock** counts the time the task worked. The time it spent waiting for a hint, a top-up or midnight is left out, so a maintainer who answers after three hours has not used three hours of the task. `/longhaul budget` has no minutes, so a top-up of any amount after a wall-clock pause starts a new window of `task_wall_minutes`.
- **The daily limit of the repo** is not lifted by a top-up. The task sleeps on a durable timer until 00:00 UTC and goes on by itself, with no signal and no label, and the comment says when. A worker restart does not lose the timer. `/longhaul cancel` still ends it. A call that is bigger than a whole day of the repo's budget would get the same answer tomorrow, so the task ends with `budget_exhausted` at once.
- **`longhaul run` and the eval runner** check budgets like the poller does. `longhaul run --no-budgets` starts a task that does not: it ends at its turn limit with the error "turn limit" and nothing else stops it, and the header says "budgets off". A paused task of a folder has no issue to answer on. Top it up with `POST /api/tasks/{id}/budget` (the console does the same), or end it with `longhaul cancel TASK_ID`. With neither, it waits up to `stuck.hint_wait_hours` and then ends as `budget_exhausted`. The eval runner sets the caps of plan section 8.4 (2 dollars on the custom set, 4 on SWE-bench) and leaves the daily limit of the repo out of reach, so only the task's own caps stop a task. Its watcher cancels a task that stays paused for `--pause-timeout-s`.

A task of a folder (`local:`) has no issue, so the numbers are in the `paused` event instead (`payload.numbers`, and `payload.resume_at` for the daily limit).

```sql
SELECT type, payload->>'reason' AS reason, payload->>'pause' AS pause, payload->'numbers' AS numbers, ts
FROM events WHERE task_id = 'lh-acme-calc-42' AND type IN ('paused', 'resumed', 'budget_added') ORDER BY id;
```

The worker prices the next call with the model of the role, so every model in the config needs an entry in `config/pricing.yaml` (the worker already stops at the start when one is missing). The scripted and replay providers cost nothing, so a scripted task only stops at its turns and its clock.

### How a task ends

Every task ends in exactly one outcome (LH-507): `pr_opened`, `stuck`, `budget_exhausted`, `cancelled` or `error`. Whatever the way, the workflow winds the task up in the same order:

| Step | What happens |
|---|---|
| 1 | The outcome is set. `status()` shows it, and the `outcome` event is recorded last, after the other steps, so it counts a task that has finished them. |
| 2 | The sandbox is destroyed: the containers and the workspace volume. This comes first because GitHub can be down for twenty minutes and a container must not wait for it. |
| 3 | A stuck task whose repo sets `open_draft_on_stuck: true` opens a draft pull request with its partial work, if the work changes a file. |
| 4 | A task of an issue posts one final comment, under the key `<task id>:final:1`. |
| 5 | The final labels are set. |

The final comment is the pull request link for a task that delivered. For any other end it is a report: the reason, what was done and what is left (from the plan), the diff stats, and what to do next. A stuck report looks like this:

```markdown
## Longhaul is stuck and has stopped

stuck, repeated failure; no hint in 24 hours

### What was done

- [x] **s1**: Fix the lookup

### What is left

- [ ] **s2**: Test it

### Changes

1 file changed, 3 additions, 1 deletion

- `inv.py`: +3 -1

Comment `/longhaul retry` to start a new attempt, or take over from the checkpoints.

Ran 12 turns, spent $0.43.
```

| Outcome | Labels at the end |
|---|---|
| `pr_opened` | `longhaul:pr-open` (set when the pull request opened) |
| `stuck`, `budget_exhausted` | `longhaul:needs-human`, and `longhaul:pr-open` if a draft of the partial work was opened |
| `error` | `longhaul:needs-human` |
| `cancelled` | none: `longhaul:needs-human` is removed too if the task was paused |

`open_draft_on_stuck` is a setting of the repo entry in the config and is off by default. The poller hands it to the task when it starts it. A stuck task has no verified work, so the pull request is a draft. Its body says that it holds partial work and ends with `Refs #<n>` and not `Closes #<n>`, so merging it to keep some of the changes does not close an issue that the task did not finish.

Each step is best effort. A step that fails after its retries is logged and the next one still runs, so one failure leaves the outcome as it is. The steps run again safely: the comment, the labels and the pull request have idempotency keys, and a cleanup of a sandbox that is gone changes nothing.

An exception that nobody expected becomes the outcome `error`. The summary names the error type, for example `unexpected error: KeyError` or `workflow failed: CheckpointError`, and nothing else, because the message of a provider error can echo the request. The trace, cut to 3,000 characters and run through the log redaction, is the payload of an `error` event. The workflow still fails after the wind-up, so Temporal shows it as failed:

```sql
SELECT ts, payload->>'type', payload->>'trace' FROM events WHERE task_id = 'lh-acme-calc-42' AND type = 'error';
```

The bare repo of a task that ended on its own is kept for seven days, so that you can read its checkpoints. A cancel removes it as it always did: with the sandbox for a local task, and right after the final comment for a task of an issue, because that comment reads the diff stats from the repo. The janitor deletes the rest: `PollerWorkflow` runs the activity `clean_repos` after each poll, so the poller schedule is its clock, and it removes the repos under `LONGHAUL_CKPT_DIR` of tasks whose `outcome` event is more than seven days old. A repo whose task has no `outcome` event is never touched, because the task may be waiting for a hint. A task that Temporal ended from outside (a terminate) records no outcome, so its repo stays until you remove it. A worker sweeps the repos on its own disk, and a failed sweep is logged and tried again on the next tick.

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

The service builds `docker/worker/Dockerfile` and mounts two things from the host. The image installs the memory extra and loads the embedding model into `/opt/fastembed` when it is built, so the example config (`memory.embedder: local`) starts in a container with no network. The build downloads the model, which adds about 200 MB to the image. The checkpoint folder is mounted at the same path inside, because the worker gives that path to the Docker daemon as the source of a mount in each sandbox. The Docker socket is mounted so that the worker can start the sandboxes.

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

Each poll is a workflow named `longhaul-poller-run-<time>` in the Temporal UI (http://localhost:8233). It returns five counts: the repos it looked at, the labeled issues it saw, the tasks it started, the issues that already had a task and the comment commands it carried out. A poll fails when a repo cannot be read, for example a typo in the config or a token without access to it. The other repos are still polled first, and the failure names the repo.

What the poller does and does not do:

- A task id is `lh-<owner>-<repo>-<issue number>`, in lowercase. The task is started once. A row in `tasks` keeps a finished task from starting again while the label stays on the issue.
- Only the allowlist counts. A labeled issue on a repo that is not in the config, or is there with `allowlisted: false`, is never read.
- The poller reads from GitHub and writes one thing: the reply to a command from someone who may not steer a task (see the next section). It sets no label and posts no "Picked up" comment. The task does that itself (LH-306).
- A task for a GitHub repo clones it in `prepare_workspace` (LH-305) and works on it. The delivery of LH-306 pushes the branch and opens the pull request when the work is done (see "Pull requests and labels").
- To change the label or the interval, edit the config, restart the worker, and run `longhaul poller install` again for the interval.

## Steer a task from its issue

A person with write access to the repo steers a task with a comment on its issue (LH-308). The comment has to begin with the command, and each poll of the poller carries out the commands that were written since the last one.

| Comment | What happens |
|---|---|
| `/longhaul approve` | Sends `approve_plan` to the task. |
| `/longhaul reject <feedback>` | Sends `reject_plan` with the feedback. |
| `/longhaul hint <text>` | Sends `hint`, and the model sees the text on its next turn. |
| `/longhaul budget +<usd> [turns +<n>]` | Sends `add_budget`, for example `/longhaul budget +5 turns +20`. |
| `/longhaul cancel [reason]` | Sends `cancel`. The task cleans up and ends as cancelled. |
| `/longhaul status` | Sends `report_status`. The task answers with a comment that gives its phase, turn, spend and last checkpoint. |
| `/longhaul retry` | Starts the next attempt of a task that has ended, as `<task id>-a2`, `-a3` and so on, from the issue as it is now. |

Rules that keep a command from doing something nobody meant:

- **Only the first line counts.** A command is not recognised after other text, inside a code block or a quote, or with extra words. `/longhaul approve now` does nothing, and so does a comment that only mentions a command. Case does not matter. Nothing is posted for a comment that is not a command, so a typo shows no sign.
- **Only people with write access may steer.** The poller asks GitHub for the role of the author (admin, maintain and write count) every time it meets a command from them. A person without it, and a person whose role GitHub will not tell, gets one reply on the issue, "Only maintainers can steer this task", for each task, and the command is dropped. Comments of bots and the comments of Longhaul itself are never commands.
- **A command is carried out once.** Each task row keeps the time and id of the newest comment handled (`tasks.last_comment_at` and `last_comment_id`). The cursor moves after the signal was sent, so a poll that fails halfway and is retried sends only the commands it had not sent. A worker that dies between the signal and the write of the cursor sends that one command again.
- **Comments from before the label are not commands.** A new task starts its cursor at the newest comment its spec was built from.
- **A task that has ended takes no command but `retry`.** Temporal refuses a signal to a closed workflow, and the command is dropped, not kept for the next attempt. `retry` on a task that still runs does nothing. Commands go to the latest attempt of the issue.
- **Issues that are closed or have lost the label are not read.** To stop a task whose label was removed, cancel it with `longhaul cancel` or in the Temporal UI.

Each poll now reads the new comments of every open labeled issue that has a task, which is one more GitHub call for each such issue, and one more for each author of a command that the poll has not asked about yet.

The end-to-end test of this path is `tests/e2e/test_issue_to_pr.py::test_label_to_pr_fake_github`. It serves a FakeGitHub over HTTP on port 8099 (a free port when 8099 is taken, and the test says so), starts `longhaul worker` against it with the scripted provider and the `calc` fixture, runs the poller once, and checks that the issue ends with one "Picked up" comment, one comment with the plan, one pull request that is ready for review and the label `longhaul:pr-open`. The task plans first, as every task of the poller does, so its script (`calc_fix_planned.yaml`) has steps for the planner, the coder and the verifier, and the review passes the work. It needs `make up` and `make sandbox-image`, and takes about two minutes.

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
| `chaos` without `chaos_lite` | 20 | 1 to 3 | replay | on request, in Phase 9. The worker is the only thing killed. |
| `eval/chaos/chaos.py` | 20 | 1 to 3 | replay | on request, in Phase 9. Four kinds of kill, a private Temporal and Postgres, and a count of the writes to GitHub. See "The full chaos suite". |

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

## The full chaos suite

`eval/chaos/chaos.py` puts a number behind "survives crashes": 20 runs of four recorded custom tasks, each killed one to three times, with a count of what each run left behind. It is the Phase 9 suite (LH-904). `eval/chaos/README.md` has the details, and this section has what an operator needs.

```bash
make up && make sandbox-image
uv run python eval/chaos/chaos.py --runs 20 --seed 7     # about an hour
uv run python eval/chaos/chaos.py --runs 2 --seed 7      # the short version
```

**It leaves the services of `make up` alone.** The suite restarts Temporal and Postgres on purpose, and other work on the machine uses the ones of `make up`. So it starts a second compose project from the same `docker-compose.yml`, named `longhaul-chaos-<8 hex>`, with other host ports that it picks free, and with the volumes that compose gives that project. The Temporal dev server runs with `--db-filename` on a volume, as it does in the main file, so a restart keeps the workflows. At the end the suite removes the containers, volumes and network of its project, found by the compose label. It does not run `docker compose down`, and `eval/chaos/services.py` refuses any project name without the prefix. MinIO is shared, and each run removes its own blobs. If a suite is killed, remove `longhaul-chaos-*` containers and volumes by hand.

**The kills.** Each kill is one of four kinds, drawn from the seed:

| Kind | What it does | What has to hold |
|---|---|---|
| `worker` | SIGKILL to the worker, then a new worker | the activity runs again from its `pre_sha` |
| `sandbox` | `docker kill` of the container of the task | the sandbox is made again at the head commit |
| `temporal` | restart of the Temporal container | the workflow goes on from the saved history |
| `postgres` | restart of the Postgres container | the activities that failed on the database run again |

When a kill lands follows the progress of a run that was not killed (the harness's `plan_kills`), and it may land in the delivery of the pull request, which is where a repeated write would show. With seed 7 each kind is hit at least four times, and `tests/unit/test_chaos_plan.py::test_kill_type_coverage` asserts it.

**What it records.** `eval/results/chaos-<date>/summary.json` and one `run-NN.json` for each run:

| Field | Meaning |
|---|---|
| `completed`, `diff_equals_reference` | the task ended with a pull request, and its diff is the diff of the same task without a kill |
| `extra_ledger_rows`, `extra_cost_usd` | model calls billed again after a kill, and what they cost, against the run without a kill |
| `resume_latency_s` | for each kill, the time from the end of the kill to the start of the next activity, from the timestamps in Temporal's history. The clock of the Docker VM is compared with this machine's first (`clock_offset_s`) |
| `side_effects`, `expected_side_effects`, `duplicate_side_effects`, `missing_side_effects` | comments, label changes and pull requests found on the FakeGitHub of the run, against the 1, 3 and 1 that a task writes |

If the private Temporal stops answering (its SQLite connection can be left interrupted on a busy machine), a watchdog restarts it after 60 seconds, and `temporal_heals` in the summary counts those restarts. The writes are counted from the state of FakeGitHub and not from the `side_effects` table, because that table is where a retry decides whether a write already happened. The heartbeat timeout of these runs is 20 seconds, so the latency of a killed worker is mostly that. The exit code says that the suite ran. Whether the runs survived is in the summary, and a run that did not has a `run-NN.log` with the worker logs.

**The recordings** in `eval/chaos/recordings/<task id>/` are scripted runs, because no key was available when they were made. `uv run python eval/chaos/chaos.py --record --provider anthropic` records a real model into the same folders and replaces them. See "Record and replay model calls" for how a recording is matched.

## Record and replay model calls

The replayed chaos runs must not depend on what a model says, so they play a recording. A recording is a folder with one JSON file per model call: the request, the reply, and the turn. Two settings control it:

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

Record without kills, and commit the folder. The first recording of a request is kept, so a folder that holds an older run must be emptied first. The replay tests of `tests/chaos/test_kill_worker.py` play this folder when it holds recordings. Until then they record a scripted run of `inventory_fix.yaml` through the same code, in a temporary folder, and play that. The four tasks of `eval/chaos/chaos.py` have folders of their own, recorded with `chaos.py --record` (see "The full chaos suite").

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

## The console API

`longhaul api` serves the API that the console reads (LH-801, ADR-0011). It needs the stack from `make up` and the `LONGHAUL_DATABASE_URL`, `LONGHAUL_BLOB_*` and `LONGHAUL_TEMPORAL_*` settings of `.env.example`. It does not need a worker to start. The live status of a task and the actions need one.

```bash
export LONGHAUL_ADMIN_TOKEN=$(openssl rand -hex 32)    # or put it in .env
uv run longhaul api                      # http://127.0.0.1:8000
uv run longhaul api --host 0.0.0.0 --port 8100
curl -s -H "Authorization: Bearer $LONGHAUL_ADMIN_TOKEN" http://127.0.0.1:8000/api/tasks | head -c 400
```

The address comes from `LONGHAUL_API_HOST` (default `127.0.0.1`) and `LONGHAUL_API_PORT` (default `8000`), and `--host` and `--port` win over both. The interactive docs (`/docs`, `/redoc` and `/openapi.json`) are off: they need no token, and `/docs` loads Swagger UI from a CDN on the origin where the console keeps its token. `LONGHAUL_API_DOCS=true` turns them on for local development. `make api-types` does not need them.

**Token (LH-807).** Every `/api` route needs `Authorization: Bearer <LONGHAUL_ADMIN_TOKEN>`. A missing, malformed or wrong token gets 401, and so does every request when the server has no token, but a server without one does not start: it stops with an error that names the setting. The token is compared with `hmac.compare_digest`. Rotating it means changing the setting and restarting the API, and the console asks for the new one at the next 401.

| Route | Needs the token |
|---|---|
| `/api/*` | Always |
| `/healthz` | Never. A probe has no token. |
| `/metrics` | Only when the server is not bound to this machine. With `127.0.0.1`, `::1` or `localhost` it is open, so a Prometheus on the same host scrapes it with no secret. With `0.0.0.0` or any other address it needs the token. |
| The console's own files at `/` | Never. The login page has to load before there is a token. |

A token may send 30 actions (POST routes) a minute. The 31st gets 429 with a `Retry-After` header. The count is a sliding window kept in the memory of the API process, so it is exact for one API process and is not shared between several. Reads are not limited. A request with a wrong token is refused before it is counted.

Keep the token out of shell history and logs, and put the API behind TLS before it leaves a trusted network: a bearer token over plain HTTP can be read by anyone on the path.

| Endpoint | Returns |
|---|---|
| `GET /api/tasks?status=&repo=&limit=50&cursor=` | The `tasks` rows, newest first, with `next_cursor` when there are more |
| `GET /api/tasks/{id}` | The row, the answer of the workflow's `status` query, and the newest plan |
| `GET /api/tasks/{id}/timeline?cursor=&limit=` | Events, model calls, tool calls and checkpoints merged by time, oldest first. A tool call carries its `post_sha`; a checkpoint row carries its `sha`, its `checkpoint_kind` (`tool`, `step`, `compaction` or `bundle`) and the turn that made it |
| `GET /api/tasks/{id}/turns/{n}?attempt=` | The request and the reply of a model turn, redacted |
| `GET /api/tasks/{id}/tool-calls/{call_id}/output?start=&end=` | Lines `start` to `end` of the full output of a tool call, redacted |
| `POST /api/tasks/{id}/approve-plan`, `reject-plan`, `hint`, `budget`, `cancel` | Send the signal, with `by` set to `console` |
| `GET /api/memory?repo=&status=&kind=` | Memory items without their vectors |
| `POST /api/memory/{id}/approve`, `reject` | Decide on an item, as `longhaul memory approve` does |
| `GET /api/repos` | The rows of `repo_settings` with a count of tasks |
| `GET /healthz`, `GET /metrics` | 200 when the database answers; Prometheus text |

Things to know:

- **The row and the live status.** The `tasks` row is written by the poller at pickup and then kept current by the workflow (`record_task_state`): at every change of phase, every three turns while the task codes, and at the end with the outcome, the pull request URL and the end time. The list reads only the row, so it follows the task within a few turns, and a task started with `longhaul run` has a row from its first write. The workflow's write is best effort, so the row can still lag when the database was down. `GET /api/tasks/{id}` therefore also has a `live` field with the answer of the workflow, and a `workflow` field that says why `live` is empty: `not_found` (Temporal has forgotten the workflow), `unreachable` (Temporal does not answer) or, for a workflow with no worker, `running` with `live` empty. The plan in the detail shows each step as the coder last set it, and a plan that nobody decided on is `approved` once the task works from it.
- **Errors.** 404 is a task, turn, call or item that does not exist. A signal to a task that has ended answers 409, and so does `approve-plan` or `reject-plan` for a task whose live phase is not `awaiting_plan_approval`: the workflow would ignore it, and the issue must not show it as taken. A signal when Temporal is down answers 503. A turn or an output that the blob store cannot return answers 502. A cursor that is not one the API made answers 400, and a line range past the end of an output answers 416.
- **Redaction.** Turns, outputs, event payloads and memory notes are redacted when they are read, with the patterns of the logs (`telemetry.logging.redact`). The stored blobs are not changed. Line numbers of an output page count the redacted text.
- **Metrics.** `/metrics` has the metrics of the API process (`longhaul_api_requests_total{method,route,status}` and `longhaul_api_request_duration_seconds{route}`) and the worker metrics of the same process, which stay at zero unless the API runs a worker, which it does not. Point Prometheus at the worker for the worker metrics and do not scrape the API's `/metrics` for them: the copy there has the same names and is always zero, so a second series would pull down any `avg()` or per-instance panel.
- **Errors from Temporal.** A signal answers 503 when Temporal does not answer (unavailable or timed out). When it answers and refuses, the API maps the status: 400 for `INVALID_ARGUMENT`, 409 for `FAILED_PRECONDITION`, and 502 for any other status, such as a permission or a rate limit. The detail names the status and never the server's message.
- **Types for the console.** After a change to `src/longhaul/api`, run `make api-types` and commit `console/openapi.json` and `console/src/api/schema.ts`. A unit test fails when `openapi.json` is out of date, and the console job of CI fails when `schema.ts` is.

## The console

The console (LH-802, ADR-0011) is a Vite, React and TypeScript app in `console/`. It reads the API and nothing else. Node 24 or later and npm are the only tools it needs.

```bash
cd console && npm ci
npm run dev          # http://localhost:5173, with /api and /healthz proxied to the API
npm run lint && npm run typecheck && npm test && npm run build   # the checks of the console job
```

`npm run dev` expects `longhaul api` on `http://127.0.0.1:8000`. Set `LONGHAUL_API_URL` before `npm run dev` for another address. `make console-check` runs the four checks after a clean install.

- **Types.** `console/src/api/schema.ts` is written by `make api-types` from `console/openapi.json`. Never edit it. Every call in `console/src/api/client.ts` names a method and a path of the generated schema, and takes its path parameters, query, body and reply types from it. A renamed or moved path, a renamed parameter, a body or query that became required, or a changed reply stops `npm run typecheck`.
- **Token.** The client sends `Authorization: Bearer <token>` when a token is stored. The token lives in `localStorage` under `longhaul.token`, and in memory when `localStorage` is blocked. The login screen keeps it only after the API accepts it (a request to `/api/repos`), and a 401 on any later call clears it and returns to the login. The sign-out button clears it and the cached pages.
- **One process.** When `LONGHAUL_CONSOLE_DIR` names the build folder (`console/dist`), `longhaul api` serves the pages at `/` as well, and any path that is not a file gets `index.html`. Unknown paths under `/api` stay 404. The compose `full` profile does this in a container: `docker compose --profile full up -d --build api` serves the console at `http://127.0.0.1:8000` (`LH_API_PORT` changes the port). Set `LONGHAUL_ADMIN_TOKEN` in `.env` or the shell first, or the container stops at start. The container listens on `0.0.0.0`, so `/metrics` needs the token there. The image is built from `docker/api/Dockerfile`.
- **Tasks page.** `/tasks` lists the tasks newest first and polls every 10 seconds. The status and repo filters go to the API, and "Load more" follows `next_cursor`. The "Needs you" toggle works on the rows already loaded: a task needs you in the `paused` and `awaiting_plan_approval` phases. The filters are kept in the address (`?status=&repo=&needs_you=1`). The phase comes from the `tasks` row, which the workflow keeps current (see the console API notes above).
- **Task page.** `/tasks/:id` shows the summary of a task (phase, cost, turns, tokens, cache ratio, pull request, plan with the status of each step) and its timeline. The summary uses the workflow's own answer when there is one and says why when there is not. The timeline loads every page of `/timeline` (500 rows each) and groups the rows by turn, with the tool calls inside. Only the rows in view are in the page (`@tanstack/react-virtual`), so a task of hundreds of turns stays light. Each turn has a Details button that fetches that turn's request and reply, and each tool call with a stored output has a Show output button that fetches 200 lines at a time. The arguments of a tool call come with its turn's reply, so they show once Details is open. Events are coloured by type: compaction, pause (paused, stuck, resumed), hint, verdict, recovery (sandbox recreated, continue as new) and error. Each known event reads as one line (for example "Step s1 done" with the short sha, or the verifier's verdict and round), with the full payload behind a toggle, and each checkpoint shows as a marker on the turn that made it. A task that has not ended is polled every 10 seconds.
- **Actions.** The task page has an Actions section under the summary. Approve plan and Reject plan (with a feedback box) show only while the task is in `awaiting_plan_approval`. Send hint, Add budget (dollars, turns or both) and Cancel task show for every phase until the task ends. Cancel stays disabled until the task id is typed in full. Nothing shows for a task that has ended or whose workflow is closed or missing, and the page says so. Each action posts to its route, shows a toast, then fetches the task and its timeline again. A refused action keeps what was typed and shows the API's reason.
- **Console actions on the issue.** For a task of an issue, the workflow repeats each signal that comes from the console as a short comment, so the issue shows every steer: "Hint from console:" with the text in a code fence, "Plan approved from console.", "Plan rejected from console:" with the feedback, and "Budget added from console: $2.50 and 10 turns." The text is cut to 1,000 characters and fenced so an @name in it notifies nobody. A cancel is not repeated, because the final comment already says "cancelled by console". Hints and commands typed on the issue are not repeated either, and a task without an issue posts nothing. The comments have the keys `<task>:console:<n>`, and the count is carried across a hand-over in the workflow memo (`console_posts`), as it is for `/longhaul status`. A comment that cannot be posted is logged and does not fail the task.
- **Memory page.** `/memory` shows the notes of one repo in three tabs: Approved, Candidates and Rejected. Pick the repo in the selector; the repo and the tab stay in the address (`/memory?repo=acme/inventory&tab=candidates`). Each item shows its kind, content, source, uses and last used time. A source is a link when it has a known shape: a pull request number goes to the pull request on GitHub, a task id goes to `/tasks/<id>` in the console, and a commit sha goes to the commit. Any other reference is shown as text. Candidates have Approve and Reject, which call the same routes as `longhaul memory approve|reject` and record `console` as the approver. Each tab asks the API for its own status, so a busy repo cannot push new candidates out of view; a tab shows at most 1000 items and says so when it is cut. A repo map folds into a panel and renders as indented JSON in monospace text.
- **CI.** The `console` job installs with `npm ci`, regenerates `schema.ts` and fails when it differs from the committed file, then runs lint, typecheck, test and build.
- **Demo stack.** `./scripts/seed_demo.sh` serves the console with data to act on. It needs `make up`, `make sandbox-image` and `npm --prefix console run build`, and a machine with outbound network, because each task sets up a sandbox from PyPI. It creates a scratch database and a FakeGitHub with three repos, starts `longhaul worker` with the scripted model of `tests/fixtures/scripts/console_demo.yaml`, polls once, and waits until the tasks are in their states: `demo/calc-shipped` has opened a pull request, `demo/calc-stuck` is paused for a hint and `demo/calc-plan` waits for a decision on its plan. It adds two lesson candidates to the memory of `demo/calc-shipped`. Then it starts `longhaul api` with the built console and prints the address, `http://127.0.0.1:8765` unless `LH_DEMO_PORT` says otherwise. Log in with `LONGHAUL_ADMIN_TOKEN`, or with `demo-admin-token` when it is unset. The seed takes one to two minutes. Press ctrl-c to stop: the script removes its database, sandboxes, volumes, blobs, open workflows and temporary folder, and touches nothing else. It does not use the compose `api` and `worker` services, because those have no GitHub to poll.
- **Browser test.** `make console-e2e` (or `npm run e2e` in `console/` after a build and `npx playwright install chromium`) runs `console/e2e/main-path.spec.ts` with Playwright. The test starts `scripts/seed_demo.sh` as its web server and waits for the health check of the API, which comes up only after the seed is done. It then logs in and sees three tasks, sends a hint to the stuck task and sees its phase change to `coding` within 15 seconds, approves the plan of the third task, and approves a lesson. The scripted model waits 20 seconds on the first model call after the hint, so the phase stays at `coding` for longer than the page takes to poll. The steps change the state of the stack, so the test runs once and does not retry. A failure keeps a trace and a screenshot in `console/test-results`, and CI uploads them with the report.
- **Browser test in CI.** The `console-e2e` job runs on every push and pull request, with the services of the `e2e` job, a built sandbox image and Chromium installed by Playwright. It runs on every event and not only on `dev`, because the `ci` job waits for every job and counts a job that did not run as a failure.

## Route tool calls through Keystone

[Keystone](https://github.com/hoomanesteki/keystone-mcp-gateway-ai-agent-security) is a gateway for MCP tool calls: policy decides each call, a person can be asked first, and every call leaves an audit row (LH-909). This is optional. The default is `tools.backend: direct`, which runs each tool in the sandbox as before and needs none of this.

With `tools.backend: keystone` the path of one tool call is:

```
tool_exec -> Keystone /mcp -> Keystone policy, approval, audit -> the worker's MCP server -> the sandbox
```

The worker serves the sandbox tools as an MCP server called `longhaul-sandbox` (`src/longhaul/tools/mcp_server.py`). Keystone knows it as an upstream, and `tool_exec` calls each tool through Keystone's own endpoint as an MCP client. The tools that touch the sandbox go this way, which is every tool of the registry except `finish` and `read_output`. Those two never leave the worker, so Keystone does not audit them. A `run_tests` call carries the test command of its task in the `_longhaul` argument, so the worker's server runs the same command as in direct mode and not the command the worker was started with. This is a setting of the durable worker. `longhaul run-local`, which uses no worker, runs its tools in the sandbox directly, whatever the config says. All the calls of one task share one Keystone session, so what Keystone has learned about a session (what it read, whether it was tainted) follows the task and never crosses to another.

What the agent sees:

| Keystone says | The agent gets |
|---|---|
| allow | The tool's own result, as in direct mode |
| deny, or any other refusal (rate limit, budget, a rejected approval) | A failed result (`ok=False`) with Keystone's text: `[keystone:KS_DENIED] call <id> denied: <reason>` |
| require approval, and nobody decides within Keystone's hold time (`approvals.wait_seconds`) | A failed result that says the call did not run, and the task pauses. The reason is the question, as in the pause for a hint |
| the worker's tool server failed or did not answer (`KS_UPSTREAM_ERROR`, `KS_UPSTREAM_TIMEOUT`) | Nothing: `tool_exec` raises and Temporal runs the activity again from the checkpoint, as it does when direct mode meets a Docker error. A timeout means the upstream's `timeout_s` is too short for the command, see step 3 |

A paused task waits for a hint, a budget top-up or a cancel (see "The pause and the hint"). Approve or reject the request in Keystone's console, then send the hint: `/longhaul hint approved in Keystone, go ahead` on the issue, or `POST /api/tasks/{id}/hint` of the console API for a local task. The model calls the same tool again, and the worker settles the request that Keystone is holding with `keystone__await_approval` and does not ask a second time.

That works only while Keystone still has the request and the session that made it. Keystone lets a request be used once, by the session that asked, within `approvals.ttl_seconds` (900 seconds unless you change it), and it drops a session that has been idle for 30 minutes. A pause can wait up to a day for a hint, and the worker keeps the request id in memory. So approve in Keystone and send the hint within that time, and raise `ttl_seconds` in Keystone's config if your reviewers are slower. If the time ran out, the worker restarted, or another worker takes the call, Keystone refuses the old request. The agent is told the approval could not be used and that the call did not run. Its next call makes a new request, which a person has to approve again and which pauses the task a second time.

If Keystone is down, `tool_exec` raises and Temporal runs the activity again. The worker never falls back to running a tool unchecked.

### Point a worker at Keystone

1. In the config that `LONGHAUL_CONFIG` names, turn the mode on and say where Keystone is:

   ```yaml
   tools:
     backend: keystone
     gateway:
       url: http://localhost:8080/mcp      # Keystone's MCP endpoint
       upstream: longhaul_sandbox          # the id the worker's server has in Keystone
       listen_host: 0.0.0.0                # where the worker serves its tools; see the note below
       listen_port: 8765
   ```

   Keystone allows an upstream id of lowercase letters, digits and single underscores, so the id is `longhaul_sandbox` and not the server's name. Keystone then lists the tools as `longhaul_sandbox__run_command` and so on.

2. Set two secrets in the environment of the worker (see `.env.example`):

   ```bash
   export LONGHAUL_KEYSTONE_TOKEN=$(scripts/get_token.sh olivia ops-agent)   # run in the Keystone checkout
   export LONGHAUL_SANDBOX_MCP_KEY=$(openssl rand -hex 24)                    # 48 characters
   ```

   The token signs the worker in to Keystone as a user and an agent. A token from the local Keycloak lasts five minutes, so for more than a short test put it in a file that something keeps fresh and set `LONGHAUL_KEYSTONE_TOKEN_FILE` to it: the worker reads the file again for every request. The key is what Keystone sends to the worker's server. The server refuses a request without it, because the tools run commands. A worker in keystone mode will not start without a token and a key of at least 16 characters, and the message names the variable.

3. Register the upstream in Keystone. Add this to the `upstreams` of its config (`keystone.yaml`, or `deploy/keystone.compose.yaml` for the compose stack) and restart Keystone, which reads the section at every start:

   ```yaml
   - id: longhaul_sandbox
     tenant: acme
     transport: http
     url: "http://host.docker.internal:8765/mcp"   # an address Keystone can reach the worker at
     credential_mode: static
     credential_env: {X-Api-Key: LONGHAUL_SANDBOX_MCP_KEY}
     tools:
       run_command: {risk: medium, timeout_s: 660, extract: {path: "$.command | lower"}}
       run_tests: {risk: medium, timeout_s: 660}
       read_file: {risk: low}
       list_dir: {risk: low}
       search: {risk: low}
       git_diff: {risk: low}
       edit_file: {risk: medium}
       write_file: {risk: medium}
   ```

   Set `timeout_s` on `run_command` and `run_tests`. Keystone stops waiting for an upstream after 30 seconds unless the tool says more, while a command in the sandbox may run for 600 seconds (`TOOL_TIMEOUT_S`, with `sandbox.command_timeout_s` as the default of the call). With the default, a test run of 45 seconds comes back as `KS_UPSTREAM_TIMEOUT`, `tool_exec` retries it, and the worker's server may still be running the first attempt. Keep `timeout_s` above the longest command the sandbox allows: 660 covers the 600 seconds and a margin. Keystone's own client also gives up on an upstream that sends nothing for 300 seconds, so a quiet command that runs longer than that can still be cut short. The worker does not check this setting, because it cannot see Keystone's config.

   Keystone reads the key from `LONGHAUL_SANDBOX_MCP_KEY` in its own environment at start and keeps it encrypted in its vault. It needs the same value as the worker. Keystone refuses an upstream on a private address unless `KEYSTONE_ALLOW_PRIVATE_UPSTREAMS` is `true`, which its compose file sets. Keystone in Docker reaches this machine as `host.docker.internal`, so the worker has to listen on an address that is not only the loopback (`listen_host: 0.0.0.0`). Do that only on a machine whose network you trust: the key is the only thing that protects the server. A Keystone that runs on the host can use `http://127.0.0.1:8765/mcp` and the default `listen_host`.

   The agent in the token (`azp`) has to be listed under `agents` in Keystone's config, and its user has to be allowed the tools by the access policy. Risk `medium` and below is open to every user of the tenant. `extract` makes the text of a command available to policy as `context.args.path`; `lower` makes a rule independent of upper case.

4. Add a policy that denies `run_command` when the command contains `curl`. Put it in the tenant's own directory of Keystone's policies, for example `policies/acme/access/longhaul.cedar`, and run `uv run keystone policy test policies tests/policy_cases --coverage` and `uv run keystone policy reload` there:

   ```cedar
   @id("longhaul-no-curl")
   forbid (principal, action == Keystone::Action::"call_tool", resource == Keystone::Tool::"longhaul_sandbox__run_command")
   when { context.args has path && context.args.path like "*curl*" };
   ```

   A rule that asks a person instead goes in the tenant's `approval/` directory and is a `permit` on the same condition, as `policies/acme/approval/mail.cedar` is in Keystone's checkout.

### Check it

Start the worker as usual (`uv run longhaul worker`). It logs `sandbox MCP server on http://<host>:<port>/mcp` before it takes a task. Then run the live test, which starts its own copy of the server on the address Keystone has registered, runs one allowed and one denied command through Keystone, and checks that the denied command never ran:

```bash
export KEYSTONE_URL=http://localhost:8080/mcp
export KEYSTONE_TOKEN=$(scripts/get_token.sh olivia ops-agent)   # in the Keystone checkout
export LONGHAUL_SANDBOX_MCP_KEY=<the same key Keystone has>
export KEYSTONE_UPSTREAM_HOST=0.0.0.0                            # when Keystone runs in Docker
export KEYSTONE_ADMIN_TOKEN=<a token of an admin of the tenant>  # optional: also checks the audit log
uv run pytest tests/integration/test_keystone_backend.py -v
```

Stop the worker first, because the test serves the upstream on the same port (`KEYSTONE_UPSTREAM_PORT`, default 8765). Without `KEYSTONE_URL` the test is skipped and says why. A pass shows the deny reaching the agent. The audit log is the second half of the check. With `KEYSTONE_ADMIN_TOKEN` set, the test reads `GET /admin/api/v1/audit?tool=longhaul_sandbox__run_command` itself and checks that the allowed call and the denied one are both there, with the task id of the run in their arguments and one session id. Without the token it skips that half and says so. You can read the same rows with `keystone audit export`.

## Operate a running install

This section is the short form for the person who looks after a Longhaul install. The details sit in the sections it links to.

### Start and stop

Start, in this order. Each step needs the one before it.

```bash
make up                          # Postgres, Temporal, MinIO and the monitoring services
make sandbox-image               # once, and again when docker/sandbox changes
make migrate                     # the tables, at the head revision
uv run longhaul worker           # the process that does the work; keep it in the foreground or a service
uv run longhaul poller install   # once; GitHub tasks only
uv run longhaul api              # optional; the console API (see "The console API")
```

Stop in the reverse order, and leave the data alone:

1. `uv run longhaul poller pause`, so no new task starts. A poll that is running finishes.
2. Stop the API with Ctrl-C or SIGTERM.
3. Drain the worker (next section).
4. `docker compose stop` stops the services and keeps their volumes. `make down` also deletes the volumes, which means the database, the history of every task and the blobs, so use it only to throw a stack away.

Start again with `make up`, the worker, `uv run longhaul poller resume` and the API. Tasks that were running carry on from their last step, because their state is in Temporal and not in the worker.

### Drain a worker

A task does not belong to a worker, so there is nothing to wait for. SIGTERM or Ctrl-C starts a graceful stop (see "Stopping a worker"): the worker takes no new activity, waits up to 30 seconds for the ones that run, and cancels the rest. Temporal notices the missing answer when the heartbeat timeout passes (15 to 60 seconds) and runs the activity again on a worker that is up, from the checkpoint it started at. No task is lost and no tool call runs twice on half-applied edits.

To replace a worker with no gap, start the new one first on the same task queue, check that its log says `worker running`, and then stop the old one. A task keeps its bare checkpoint repo on the worker host, in `LONGHAUL_CKPT_DIR`. Run the new worker on the same host, or with the same folder, so a task that is still running finds its repo.

`longhaul_active_tasks` on the metrics port says how many tasks the worker holds. It is 0.0 when a worker is idle.

### Cancel stuck tasks

First find out what the task is doing:

```bash
uv run longhaul status TASK_ID     # phase, turns, spend, outcome once it ended
uv run longhaul report TASK_ID     # the run report as markdown
```

A task that is `paused` is waiting for a person, and its comment or its console page says for what. Answer it with a hint or a budget top-up, or cancel it. A task that is `coding` and shows no new turn for a long time, with a worker up, is the case the stuck detector exists for, and it pauses the task by itself. If it does not, cancel it:

| Where | How |
|---|---|
| CLI | `uv run longhaul cancel TASK_ID --reason "text"` |
| Issue | a comment that starts with `/longhaul cancel` |
| Console | Cancel task on the task page; the task id must be typed in full |
| API | `POST /api/tasks/{id}/cancel` |

The cancel is a signal. The workflow removes the sandbox and ends as `cancelled`, and a task of an issue posts a final comment. With no worker running, nothing handles the signal until one starts.

If the workflow cannot take the signal, terminate it in the Temporal UI (http://localhost:8233). A terminated task records no outcome and cleans up nothing, so remove what it left:

```bash
docker ps -a --filter "label=longhaul.task_id=TASK_ID"     # then docker rm -f the container ids
docker volume rm "lh-ws-TASK_ID"
```

Its checkpoint repo stays in `LONGHAUL_CKPT_DIR` until you delete `TASK_ID.git`.

### Rotate tokens

`docs/security.md` has the steps for each secret, under "Rotating tokens": the GitHub token, the GitHub App key, the console admin token, the model keys, and the database, blob store and Temporal credentials. The order is always the same. Make the new secret, put it where the process reads it, restart that process, check it works, and revoke the old one last. The GitHub token and the model keys are read by the worker at start, so the worker is the process to restart. The admin token belongs to the API.

### The replay test

A workflow that is running is replayed from its history every time a worker picks it up, and a change to workflow code that makes a different command from the recorded one fails the task with a non-determinism error. The replay tests are the check against that. They run the current `TaskWorkflow` over a recorded history and fail when it would make another command.

- `test_recorded_histories_replay` in `tests/integration/test_task_workflow.py` makes fresh runs (signals, a cancel, a lost sandbox, a hand-over to a new run, a task of an issue), fetches their histories and replays them against the same code. It checks that the workflow is replay-safe in itself.
- `test_histories_from_before_the_console_line_replay` and its siblings in `test_pause_flow.py`, `test_plan_gate.py` and `test_coder_steps.py` replay JSON histories that were recorded before a change, from `tests/fixtures/histories/`. These are the ones that prove an old task survives an upgrade.

```bash
uv run pytest tests/integration -k replay -q
```

They run on a local Temporal test server.

To add one for a change: before you touch the workflow, run a task that reaches the code you will change, fetch its history with `handle.fetch_history()`, write it with `history.to_json()` into `tests/fixtures/histories/<name>.json`, and add the name to a replay test like the ones above. Then make the change behind a patch marker and see that the old history still replays and a new run records the marker.

### Upgrade with workflow versioning

Any change to what `TaskWorkflow` or `PollerWorkflow` does, such as a new activity call, a different order, a new wait or a new branch, goes behind `workflow.patched("<marker>")`. A history that began before the change has no marker, so `patched` answers false on its replay and the old path runs. A run that begins after the change records the marker and takes the new path. Name a marker `lh-<story>-<what>`, keep the constant next to the code it guards, and say in a comment which history it protects.

The markers in the code today:

| Marker | Story | What it guards |
|---|---|---|
| `lh-502-plan-gate` | LH-502 | The wait for approval of the plan. A history from before it goes straight to coding. |
| `lh-506-pause-for-hint` | LH-506 | The pause that asks a person a question. |
| `lh-507-final-outcome` | LH-507 | The wind-up that ends every task: final comment, labels, outcome event and clean-up. |
| `lh-507-sweep-repos` | LH-507 | The `clean_repos` call that `PollerWorkflow` makes after each poll. |
| `lh-604-load-memory` | LH-604 | The `load_memory` call after `prepare_workspace`. |
| `lh-605-lessons` | LH-605 | The `extract_lessons` call at the end of a task that has a memory ref. |
| `lh-805-console-mirror` | LH-805 | The comments that repeat a console action on the issue. |
| `lh-891-task-row` | LH-891 | The writes of the task row that the console reads. |
| `lh-991-finish-reviewed` | LH-991 | The verifier round after a planned coder calls `finish`. A history from before it opens the pull request without a review. |
| `lh-991-keep-phase` | LH-991 | The phase a task keeps across a hand-over to a new run. A history from before it writes `preparing` again. |
| `lh-991-final-report` | LH-991 | The second write of the stored run report, after the outcome and the lessons. |

To upgrade a running install:

1. Run the replay tests (`uv run pytest tests/integration -k replay -q`). If one fails with a non-determinism error, the change needs a marker or the marker is in the wrong place.
2. Start the new worker on the same task queue, then drain the old one. Running tasks are replayed on the new code, and the markers keep the old ones on their old path.
3. Check the Temporal UI for a task that was running during the switch. It should show no failed workflow task.

Other rules for an upgrade:

- Do not delete a marker while a workflow older than the change can still be open. A task waits up to `stuck.hint_wait_hours` (24) for a hint, and a task that was handed to a new run is a new history, so the open histories of the last few days are the ones to check. Do not remove a marker in v1.
- A new field on a payload gets a default, so a history written before it still reads back. `tests/unit/test_acts.py` has examples of old shapes that still read.
- Do not roll a worker back across a marker without replaying a history that the newer worker wrote against the older code. The replay test is how you know.
- A schema change goes through Alembic. Run `make migrate` before the new worker starts, and write the migration so the old worker still runs on the new schema for the minutes the two overlap.

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
| `--set custom\|swebench` | `custom` is the 15 custom tasks. `swebench` is the 40 instances of `eval/swebench/subset.txt`, and `--limit` takes the first N of them (see "The SWE-bench set"). |
| `--config full\|naive\|single_shot` | A file in `eval/configs`. `full` is the product, and `naive` and `single_shot` are the baselines (see "The baselines"). The sha256 of the file is stored in every result. Every config runs on both sets. |
| `--run-id NAME` | Names the folder `eval/results/NAME`. A run id is never reused: the runner stops when the folder exists, even an empty one, or when Temporal knows a workflow `lh-eval-NAME-...`. |
| `--limit N` | Only the first N tasks, in the order of `tasks.yaml`. |
| `--concurrency K` | How many tasks run at once. The default is 1. |
| `--model ID` | Checked against the coder model in the worker's config, and recorded. |
| `--context-mode full\|no_compaction\|none\|sliding` | Only with `--config full`, for an ablation. It takes the place of the context mode in the config file, travels with each task and is recorded. |
| `--memory on\|off` | Only with `--config full`. Recorded in each result file. The worker reads its memory settings from its own config, so start it with the settings you mean to measure. |
| `--run-set NAME`, `--allow-mixed-models` | Runs that share a run set are compared with each other, so they must use one model. The runner stops a run whose model differs from the earlier runs of its set, unless `--allow-mixed-models` is passed. A run with no run set is compared with nothing at start time, so every result file records `allow_mixed_models`, and a report that pairs two runs calls `results.check_comparable`, which refuses two models unless a run allowed it. |
| `--seed N` | Recorded in each result file. The default is 42, the seed of the SWE-bench subset. Nothing in the custom runner draws random numbers yet. |
| `--provider scripted --script PATH` | Plays a script. A file serves every task. A folder holds one `<task id>.yaml` per task. `--script` alone means the scripted provider. For the durable configs every other `--provider` is only recorded, because the worker answers with its own provider. For `single_shot` the runner makes the call itself, so `--provider` chooses who answers. |
| `--results-dir`, `--pause-timeout-s` | Where results go (default `eval/results`), and how long a pause lasts before the runner ends the task (default 60). |

### What a run does

1. Checks everything it can before it starts a task: the model, each repo folder (it must match the `base_sha` in `tasks.yaml`, and must hold no hidden test), the scripts, and that the run id is new. The results folder is made last, so a run that stops here leaves nothing behind.
2. Starts each task as a workflow with id `lh-eval-<run id>-<task id>`. The task is a folder on this machine (`local:/abs/path`), so nothing is written to GitHub. The plan gate is off, no hints are sent, and the caps are the ones of plan section 8.4: 2 dollars, 150 turns, 120 minutes.
3. Watches the task. A pause marks it `intervention_needed`. A pause that lasts a minute ends the task with a cancel, because nobody is there to answer. A task that runs past its time limit is ended the same way, and one that ignores the cancel for 30 seconds is terminated.
4. Reads the patch from the checkpoint repo and grades it. A task counts as resolved when the agent finished (outcome `pr_opened`) and the grader passed the patch. A task that ended any other way is not graded and is not resolved.
5. Writes `<task id>.json` and `<task id>.patch`, removes the sandbox and the checkpoint repo of the task, and at the end writes `summary.json`.

A `single_shot` task takes a different path through steps 2 to 4. There is no workflow: the runner builds the repo map in a sandbox, makes the one call, books it on the cost ledger under the id `lh-eval-<run id>-<task id>` and applies the diff in a throwaway copy of the repo. The patch it hands to the grader is `git diff` of that copy. Outcome `pr_opened` means that a patch was applied. A reply with no diff, a diff that neither tool applies and a reply that was cut off at max tokens end as `error` and are not graded.

When the runner itself fails on a task, for example a lost connection or a database error, that task gets a result with outcome `error` and `resolved` null, and the other tasks go on. The summary counts it as attempted and not graded. Ctrl-C ends the whole run: the tasks that are running are terminated, no summary is written, and the run id cannot be used again.

A result file holds the outcome, `resolved`, the grader's one-line reason, the cost and tokens from the ledger, the cache ratio, the wall time, the turns, the model, the commit of this checkout, the hash of the config file, the seed, the run set and the flags of the run. `scripted` is true when a script played the answers, and `provider` is what `--provider` asked for (null means the worker's own). A scripted run costs nothing and fixes what its script fixes, so keep its files out of `eval/results`, which the report reads, and leave them out of any number you quote. `compactions`, `verify_rounds` and `stuck_reasons` start at 0 and empty until the phases that count them have landed. `resolved` is `null` when the grader itself failed, and the summary counts those tasks as `ungraded`. The summary has the resolve rate over every started task, the cost per resolved task, the medians of wall time, tokens and cache ratio, and the number of tasks that needed a person. Intervals and paired tests come from the report (LH-905).

### The baselines

The headline numbers compare `full` with two simpler setups on the same model and tasks (plan section 8.3). Each is defined by a file in `eval/configs`, and the runner stores the sha256 of that file in every result. A comment in the file changes the hash, so change a config in a commit of its own.

| Config | What it is |
|---|---|
| `full` | The product: planner, verifier, memory and stuck detector on, and both context tiers. |
| `naive` | The same durable stack, tools, model, turn limit and cost limit. Planner, verifier, memory and stuck detector are off. The context mode is `sliding`: when a request would pass the hard limit, the oldest whole turns are dropped. |
| `single_shot` | No loop and no tools. The five files closest to the issue by BM25, plus the repo map, in one prompt; one call asks for a unified diff. |

For the naive config the runner can apply the context mode only: it travels with each task in the workflow memo. The planner, the verifier, memory and the stuck detector are not per-run switches yet. The file records them as off, the worker's own config decides what runs, and a part that the stack does not have yet is not there. When a story adds one of those switches, it makes the runner pass the config's value on.

The single-shot baseline works in three steps (`eval/baselines/single_shot.py`):

1. Retrieve. BM25 (`rank_bm25`) between the issue and the text of each file the agent would see, with camelCase and snake_case names split into words. Files with equal scores come in path order. The top five go into the prompt whole (a file over 30,000 characters is cut and says so), after the repo map.
2. Ask. One call through the provider protocol with no tools and the larger output cap of the loop (`max_tokens_retry`), because the reply is a whole diff. A provider error that asking again can fix is retried up to three times, and each billed call is a ledger row.
3. Apply. The reply is searched for the diff, which may sit in a code fence between prose. Every path in it is checked first: no absolute path, no `..`, nothing in `.git`. The check reads the `---` and `+++` lines, the `diff --git` and rename lines, and also the `Index:` lines and context diff headers, because `patch` takes file names from those too and the `patch` of macOS follows a `..` in them. Then `git apply --recount` runs on a throwaway copy of the repo, one file at a time, and when git refuses, `patch -p1 -u --fuzz=2` runs on a fresh copy. It is given only the `---` and `+++` headers and the hunks of the diff, so a line it would read a name from, and any ed command, never reaches it. A reply cut off at max tokens is not applied. The number of files in step 1 is `top_files` in `eval/configs/single_shot.yaml`.

Compare the three on one model by giving the runs a run set. The model comes from the config of the worker, so the runs of a set share it unless the worker was started with another config:

```bash
uv run python eval/run.py --set custom --config full --run-set headline --run-id headline-full
uv run python eval/run.py --set custom --config naive --run-set headline --run-id headline-naive
uv run python eval/run.py --set custom --config single_shot --run-set headline --run-id headline-single-shot
```

A dry run of the single-shot config plays `tests/fixtures/scripts/eval_single_shot` (one script per task, each with the one answer) in place of `tests/fixtures/scripts/eval`.

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

### The SWE-bench set

`--set swebench` runs the frozen subset of SWE-bench Verified (40 instances: 15 easy, 20 medium, 5 hard, seed 42) and has the official harness grade the patches. The runner works as for the custom set, with three differences:

* Each task runs in an image made from the instance's own image, with its repo and conda environment, and the dollar cap is 4 (plan section 8.4). The runner builds the image and copies the repo to a temporary folder. The folder names the image in a file `.longhaulimage`, which the worker reads when it prepares the sandbox (`agent/workspace.py`, `read_image_file`) and when it makes a new one after a crash.
* The result files say `not graded yet`. The runner also writes `predictions.jsonl`, with the test files of each instance cut out of the agent's diff.
* `eval/swebench/grade.sh eval/results/<run id>` runs `python -m swebench.harness.run_evaluation` and puts `resolved` into the result file of each task that finished and into the summary. A task that ended any other way keeps the verdict the runner gave it.

```bash
uv sync --extra eval                    # swebench and datasets, which CI does not install
uv run python eval/run.py --set swebench --config full --limit 3 --run-id swe-smoke-1
eval/swebench/grade.sh eval/results/swe-smoke-1
```

The harness pulls the instance images (several GB each, built for x86_64) and wants about 120 GB of free disk for a full run, so run it on the eval VM. `eval/swebench/README.md` has the notes of the spike on the `swebench` package, the exact smoke command with the agent, how to check the grader with the reference patches, and the known limits.

`make itest` does not run `test_every_task_fails_before_and_passes_after_its_reference_patch`, which sets up 30 sandboxes. Run it with `uv run pytest -m slow tests/integration/test_eval_runner.py` after you change a repo, a hidden test or a reference patch.

### Build the results page

`eval/report/build_report.py` turns the result files of finished runs into `docs/results.md` and its charts in `docs/img/`. It reads only `eval/results/<run id>/`, so the numbers on the page are the numbers in the files, and the footer lists the run ids and the git sha it was built at.

```bash
uv sync --extra eval                    # matplotlib draws the charts; without it the page has no images
uv run python eval/report/build_report.py swe-single-shot swe-naive swe-full \
  custom-single-shot custom-naive custom-full custom-full-2 \
  abl-full abl-no-compaction abl-no-memory chaos-2026-11-20
```

| What the builder does | Why |
|---|---|
| Takes the headline model from the first run that is not an ablation | The headline tables hold one model. List the headline runs first. |
| Puts a run in a table by what its files say: config, set, context mode, memory switch | The run id is a name the operator chose, so nothing depends on it. |
| Pairs each ablation (`--context-mode no_compaction`, `--memory off`) with the plain `full` run on the same model that shares the most tasks | The comparison is paired on the tasks that both runs attempted. |
| Computes Wilson 95% intervals and McNemar's exact test (`eval/report/stats.py`, no scipy) | The resolve rates come from 15 or 40 tasks, so the intervals matter more than the points. |
| Counts a task that was not graded as attempted and not resolved | It follows the resolve rate of plan section 8.5. The page names the runs that have such tasks. |
| Adds no date | The same files give the same page. |
| Shows a task that ended before grading with the end summary of the result file as its reason | The grader line of such a task only repeats the outcome. A task that was graded and failed keeps the grader line. |

Two steps of the real run are easy to miss, and `docs/results.md` (the placeholder) lists both:

* **The chaos recordings.** `eval/chaos/chaos.py --record --provider anthropic` records the four chaos tasks with a real model (it needs `ANTHROPIC_API_KEY`). Without it the suite replays the committed scripted recordings, and the builder refuses its summary without `--allow-scripted`.
* **The memory ablation.** `--memory off` is only recorded. Start the worker on a copy of the config with `memory.mode: "off"` (`LONGHAUL_CONFIG=...`) before the "without" run, and run a warm-up pass with memory on before the "with" run, as plan section 8.4 asks.

With no run ids it writes the placeholder, which says that no real run exists and gives the commands to make one. `docs/results.md` stays that placeholder until a real run is reported. `tests/unit/test_report_tables.py` accepts the placeholder or a page the builder wrote: it reads the run ids and the git sha from the footer of the committed page, rebuilds the page from `eval/results`, and fails when the two differ, so commit the result folders with the page. Runs that a script played (`provider: scripted`) are refused, because their numbers say nothing about a model. `--allow-scripted` builds a dry-run page from them, with a notice above the first number, to try the builder. The fixtures behind the tests are in `tests/fixtures/eval_results/` and `scripts/make_eval_result_fixtures.py` rewrites them.

## CI

Workflow: `.github/workflows/ci.yml`. It runs on pull requests into `dev` and `main`, on pushes to those branches and to `feat/`, `fix/`, `spike/` and `chore/` branches, and by hand. A new push to a pull request cancels the older run. CI has no secrets and always uses the scripted provider. A unit test scans every file in `.github/workflows/` and fails on any use of the `secrets` context, including `secrets['NAME']`, `toJSON(secrets)` and `secrets: inherit`.

| Job | What it does |
|---|---|
| `lint` | `ruff check` and `ruff format --check` |
| `types` | `mypy src eval`. Strict checks apply to `longhaul.contracts`, `longhaul.durable`, `longhaul.context` and `longhaul.cost` only |
| `unit` | unit tests with coverage, failing under 80 percent; uploads `coverage.xml` |
| `integration` | Postgres with pgvector and MinIO as service containers, a Temporal dev server in Docker, then the integration tests (see "Test markers and `make gate`" below). The Grafana test starts Prometheus and Grafana from `docker-compose.yml` itself |
| `e2e` | starts Postgres, MinIO and the Temporal dev server like `integration`, builds the sandbox image, then runs the end-to-end tests with the scripted provider on a real sandbox: `longhaul run-local` on the `calc` fixture, and `longhaul worker` with `longhaul run` on the same fixture, stopped and restarted mid-run (see "Run tasks on the durable stack"), and a labeled issue on a FakeGitHub that ends as a pull request a verifier passed (see "Steer a task from its issue") |
| `chaos-lite` | the `chaos_lite` tests (see "Kill the worker on purpose") on the same services as `integration`. It runs on the nightly schedule (03:17 UTC, on the default branch) and by hand, not on pushes or pull requests |
| `security` | gitleaks over the full git history of the clone (`gitleaks git --log-opts="--all"`, every branch and tag), and `pip-audit` over the locked dependencies |
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

For `PHASE=8` and `PHASE=9` the gate then runs the console as well: `make console-check` (install, lint, typecheck, unit tests and build) and `make console-e2e` (the Playwright main path). Integration checkpoint 8 asks for both, and phase 9 includes phase 8. The browser test starts the demo stack, so it needs `make up`. Its target builds the sandbox image first, which is quick when the image is already there. `PHASE=7` and below leave the console out. `CONSOLE_PHASES` in the `Makefile` lists the phases.

### Branch protection

To protect `main` and `dev`, run this once as a repo admin with `gh` logged in:

```bash
scripts/protect_branches.sh            # uses the repo of the current checkout
scripts/protect_branches.sh OWNER/REPO # or name it
```

It requires a pull request and an up-to-date `ci` check, and blocks force pushes and deletions.

`enforce_admins` is false on purpose. Admins merge the phase-gate pull requests and push `dev`, so GitHub has to let admins through. The rules bind collaborators and bots, and they do not bind admins: a direct push to `dev` is rejected for a token without admin rights and accepted for an admin. A fine-grained token that the owner mints acts with the owner's rights, so these rules do not stop it. The branch guard in the agent's own code (invariant I5) is what keeps the agent off the default branch. `tests/unit/test_ci_config.py` pins this value, so changing it means changing this paragraph too.

## Docs site

The site in `site/` is a Quarto project. Its pages include the Markdown in `docs/` with the Quarto include shortcode and copy no text, so the site cannot drift from the docs. The prose that a page adds itself, such as the overview and the console page, is short and leaves the numbers that change to tables and charts generated from committed files.

The navbar groups the pages by what a reader is after: Overview; How it works (architecture, decisions, console); Proof (results, gate reports, case study); Operate (runbook, security, deploy to AWS); Project (plan, progress, demo script). `tests/unit/test_docs_site.py` holds the same list, and fails when a page is missing from the navbar or from the "Find your way" table of the overview.

| Command | What it does |
|---|---|
| `make site` | renders the site to `site/_site/` |
| `make site-preview` | serves the site with live reload |

You need [Quarto](https://quarto.org/docs/download/) 1.10.18, which is the version CI pins, and `python3` on the PATH. View the pages through the preview server or any local web server. Opening an `.html` file straight from disk can break the table of contents.

Before every render, Quarto runs `scripts/site_pages.py`. It reads `docs/` and writes two folders that git ignores: `site/adr/` holds one page per ADR, and `site/_generated/` holds the ADR index, the list of gate reports, the phase status table, the diagrams and tables that live in sections 3.1 to 3.3 and 8.1 of the plan, the chaos table (from `eval/results/chaos-2026-11-07/summary.json`) and the chaos chart. It also copies every image under `docs/` to the same path under `site/_generated/`, and writes `doc-pages.txt`, the map from each doc to its page. A new ADR in `docs/adr/` or a new report in `docs/gates/` therefore shows up on the site with no edit under `site/`. The script stops the render with a message when a doc loses a shape it relies on, such as an ADR without a `- Status:` line or a renamed plan section.

Things that are easy to trip over:

- `site/_quarto.yml` lists the pages to render. Without that list Quarto renders nothing when a checkout sits under a dot folder such as `.worktrees`.
- A page cannot include a file from `site/_generated/` with the include shortcode. Quarto expands that shortcode while it scans the project, before `scripts/site_pages.py` has run, so on a clean checkout the file is missing and the render stops. `site/filters/fragments.lua` embeds those files later instead, and `tests/unit/test_site_pages.py` fails a page that includes one.
- `site/filters/docs.lua` drops the first heading of an included file, because the page already has a title, turns status words in tables into badges, and sizes table columns by their content.
- An included doc keeps its links and images relative to its own folder, as GitHub reads them. The div around the include names that folder (`::: {.drop-title base="docs"}`), and `docs.lua` then sends a link to a doc with a page to that page, a link to any other file to the file on GitHub, and an image to its copy in `site/_generated/`. A new page for a doc needs its line in `DOC_PAGES` of `scripts/site_pages.py`.
- The chaos chart is `docs/media/chaos-results.svg`, which `uv run python scripts/chaos_chart.py` draws from the summary of the chaos run. `tests/unit/test_chaos_chart.py` fails when the committed file differs from a fresh drawing. The site embeds it inline, so `site/styles.css` can give it the colors of the light or dark theme.
- `tests/unit/test_docs_style.py` applies the voice rules to the `.qmd` pages as it does to the docs.

### Publishing

The `docs` job in `ci.yml` renders the site on every pull request. `.github/workflows/pages.yml` renders it again and deploys it to GitHub Pages on every push to `main`, and on a manual run.

The owner turns the site on once, in two steps: set the Pages source of the repository to GitHub Actions (Settings, Pages, Build and deployment, Source, GitHub Actions), then add the repository variable `PAGES_ENABLED` with the value `true` (Settings, Secrets and variables, Actions, Variables). Until both are done the workflow renders the site and skips the deploy, so `main` stays green and the site stays offline. The site goes live with the first push to `main` after both steps are done. The address is `https://hoomanesteki.github.io/longhaul-autonomous-coding-agent-github/`, and the README links to it. The repo is public, but these two steps are not done yet, so the address answers 404 and the README says the site is not live.
