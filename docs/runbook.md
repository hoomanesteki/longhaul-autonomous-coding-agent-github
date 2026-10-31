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

Exit codes: 0 when the agent called `finish`, 2 when the run failed (turn limit, no tool calls, a refusal, a failing setup, a provider error, no Docker or image) or was interrupted, and 1 for a bug in longhaul, which prints a traceback. The diff is cut like any tool output, to its first 8,000 and last 12,000 characters.

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

## CI

Workflow: `.github/workflows/ci.yml`. It runs on pull requests into `dev` and `main`, on pushes to those branches and to `feat/`, `fix/`, `spike/` and `chore/` branches, and by hand. A new push to a pull request cancels the older run. CI has no secrets and always uses the scripted provider. A unit test scans every file in `.github/workflows/` and fails on any use of the `secrets` context, including `secrets['NAME']`, `toJSON(secrets)` and `secrets: inherit`.

| Job | What it does |
|---|---|
| `lint` | `ruff check` and `ruff format --check` |
| `types` | `mypy src`. Strict checks apply to `longhaul.contracts`, `longhaul.durable`, `longhaul.context` and `longhaul.cost` only |
| `unit` | unit tests with coverage, failing under 80 percent; uploads `coverage.xml` |
| `integration` | Postgres with pgvector and MinIO as service containers, a Temporal dev server in Docker, then the integration tests (see "Test markers and `make gate`" below) |
| `e2e` | builds the sandbox image, then runs the end-to-end tests: `longhaul run-local` on the `calc` fixture with the scripted provider, on a real sandbox (see "Run the agent on a local folder") |
| `security` | gitleaks over the full git history, and `pip-audit` over the locked dependencies |
| `compose` | `docker compose config -q`, so a broken compose file fails |
| `docs` | `quarto render site`, so a broken include, page or diagram fails the pull request (see "Docs site" below) |
| `ci` | waits for all the jobs above and fails unless every one succeeded |

The required check is `ci`. Branch protection points at that one name, so a new job must be added to the `needs` list of `ci`. `tests/unit/test_ci_config.py` fails if any job is missing from that list.

The Temporal server in the `integration` job is the image that `docker-compose.yml` uses (`temporalio/temporal:1.5.1`, see ADR-0002), so CI and `make up` run the same server. CI also pins it by digest, because a tag can be moved to other content. When the version in the compose file changes, change the tag and the digest in `ci.yml` too. To find the digest of a new tag, run `docker pull temporalio/temporal:<tag>` and read it from `docker image ls --digests temporalio/temporal`. `tests/unit/test_ci_config.py` fails if the tags differ or the digest is missing.

Dependabot opens its version-update pull requests against `dev` (`target-branch: dev` in `.github/dependabot.yml`), because `main` moves only through phase-gate pull requests. Dependabot reads that file from the default branch, so it starts working after the first gate merge puts the file on `main`. The setting does not cover Dependabot security updates, which open against `main`. If you turn those on, treat one like a hotfix: merge it, then merge `main` back into `dev` the same day (plan section 5.2).

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

`enforce_admins` is false on purpose. The owner merges the phase-gate pull requests and the maintainer pushes `dev` as the admin, so GitHub has to let admins through. The rules bind collaborators and bots, and they do not bind admins: a direct push to `dev` is rejected for a token without admin rights and accepted for an admin. A fine-grained token that the owner mints acts with the owner's rights, so these rules do not stop it. The branch guard in the agent's own code (invariant I5) is what keeps the agent off the default branch. `tests/unit/test_ci_config.py` pins this value, so changing it means changing this paragraph too.

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
