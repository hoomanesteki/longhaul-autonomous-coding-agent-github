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

Each compaction writes a `compaction` event with the tokens before and after, and each call of the compactor model has its own row in `cost_ledger`:

```sql
SELECT ts, payload FROM events WHERE task_id = 'lh-local-calc-1a2b3c4d' AND type = 'compaction';
```

The log is a set of blobs under `tasks/<task id>/log/<turn>/`, a `reply.json` and a `results.json` for each turn. `longhaul.context.log.read_log(blobs, task_id)` joins them into one transcript. It is never elided or compacted, so a report can say what the agent did in turn 3 of 150. The view of a task is the `transcript_ref` of the workflow state, under `tasks/<task id>/turns/`.

### Pull requests and labels

A worker with `LONGHAUL_GITHUB_TOKEN` registers the GitHub activities as well (LH-306). A task of an issue, which has an `owner/name` repo and an issue number, shows its state on the issue through labels, and ends with a draft pull request:

| When | What the issue shows |
|---|---|
| The task starts | A "Picked up" comment, and the label `longhaul:working` |
| The agent calls `finish` | The phase is `delivering`. The branch `longhaul-pr/<n>` is pushed and a draft pull request is opened |
| The pull request is open | `longhaul:working` is removed and `longhaul:pr-open` is added |
| A pause for a person (Phases 5 and 7) | `longhaul:needs-human` is added, and removed when the task resumes |

The pull request branch has one commit on top of the base commit, whatever number of checkpoints the agent made. Its tree is the tree of the last checkpoint, so its diff is the diff of the scratch branch. The commit is titled from the summary the agent gave to `finish`, as a conventional commit such as `fix(calc): add returns the sum`, and its author and committer come from `LONGHAUL_GIT_AUTHOR` (default `longhaul <longhaul@users.noreply.github.com>`). The branch is built on the host, from the bare repo of the task, and pushed there with the token. Nothing is ever pushed to the default branch.

The body comes from `templates/pr_body.md.j2`: the summary, the changes per file, how it was tested (the counts of the last `run_tests` call) and `Closes #<n>`. The worker finds the folder through `LONGHAUL_TEMPLATES_DIR`, which defaults to `templates/` in the checkout and is set in the worker image. Text from the model is shown with its mentions and issue references broken up, so it cannot notify a person or close another issue.

Every write has an idempotency key, so a retry or a replay changes nothing on GitHub. The pull request is found again by its branch even if the `side_effects` table is lost. A task that ends in an error or a cancel leaves its labels as they are for now (LH-507). A task whose work changes no file ends with the error `nothing to deliver: no file changed`, because GitHub refuses a pull request with no commit.

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

**Security note.** Access to the Docker socket is the same as root on the host. Anything that runs in the worker, or takes it over, can start a container that mounts the whole disk. The sandboxes keep repo code away from the worker (no secrets in their environment, no network after the setup, no capabilities), but the worker itself is trusted. Run it only on a machine you would give that trust, never on a shared host, and do not publish a port from it (the service has none). The container runs as root, because the group of the socket differs between hosts and a non-root user could not open it. A worker started with `uv run longhaul worker` has the same reach, because your user can use Docker.

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
- A task for a GitHub repo clones it in `prepare_workspace` (LH-305) and works on it. Nothing pushes the branch or opens a pull request until LH-306 adds the delivery, so a finished task leaves no change on GitHub yet.
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

The end-to-end test of this path is `tests/e2e/test_issue_to_pr.py::test_label_to_pr_fake_github`. It serves a FakeGitHub over HTTP on port 8099 (a free port when 8099 is taken, and the test says so), starts `longhaul worker` against it with the scripted provider and the `calc` fixture, runs the poller once, and checks that the issue ends with one "Picked up" comment, one draft pull request and the label `longhaul:pr-open`. It needs `make up` and `make sandbox-image`, and takes about two minutes.

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
| `test_a_kill_at_a_random_moment_resumes` (two seeds) | at a seeded random delay between 0.5 seconds and 85 percent of the duration of a run that was not killed, counted from the first model turn |

Each of the first three asserts the kill point from the last activity line in the worker's log, so a kill that misses its aim fails the test and does not pass for the wrong reason. The full suite draws 1 to 3 kills for each of 20 seeds. Its later kills wait until the new worker has finished an activity, so they fall in new work and not in the retry of the activity that was killed (an activity gives up after three attempts). It also hands the run over to a new one every four turns (Continue-As-New), so the kills also fall in the hand-over.

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

## CI

Workflow: `.github/workflows/ci.yml`. It runs on pull requests into `dev` and `main`, on pushes to those branches and to `feat/`, `fix/`, `spike/` and `chore/` branches, and by hand. A new push to a pull request cancels the older run. CI has no secrets and always uses the scripted provider. A unit test scans every file in `.github/workflows/` and fails on any use of the `secrets` context, including `secrets['NAME']`, `toJSON(secrets)` and `secrets: inherit`.

| Job | What it does |
|---|---|
| `lint` | `ruff check` and `ruff format --check` |
| `types` | `mypy src`. Strict checks apply to `longhaul.contracts`, `longhaul.durable`, `longhaul.context` and `longhaul.cost` only |
| `unit` | unit tests with coverage, failing under 80 percent; uploads `coverage.xml` |
| `integration` | Postgres with pgvector and MinIO as service containers, a Temporal dev server in Docker, then the integration tests (see "Test markers and `make gate`" below) |
| `e2e` | starts Postgres, MinIO and the Temporal dev server like `integration`, builds the sandbox image, then runs the end-to-end tests with the scripted provider on a real sandbox: `longhaul run-local` on the `calc` fixture, and `longhaul worker` with `longhaul run` on the same fixture, stopped and restarted mid-run (see "Run tasks on the durable stack"), and a labeled issue on a FakeGitHub that ends as a draft pull request (see "Steer a task from its issue") |
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
