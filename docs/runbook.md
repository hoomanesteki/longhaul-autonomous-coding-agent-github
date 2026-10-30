# Runbook

## Start here

Run these from the repo root on a fresh clone. You need `uv`, Docker and `gh`.

```bash
make setup    # uv sync and pre-commit hooks
make up       # local stack via docker compose
make check    # ruff, format check, mypy, unit tests
```

The plan is in `docs/plan/longhaul-build-plan.md`. Story status is in `docs/progress.md`. Working rules for coding agents are in `CLAUDE.md`. Voice rules are in `docs/style.md`.

## Local stack

`make up` starts Postgres with pgvector, Temporal, MinIO, an OpenTelemetry collector, Jaeger, Prometheus and Grafana. To change anything below, copy `.env.example` to `.env` first. Docker Compose and the Python code both read `.env` from the repo root, so they see the same ports and URLs. A variable set in your shell wins over `.env`, and a blank value in `.env` counts as unset.

| Setting | Where | Notes |
|---|---|---|
| Host ports | `LH_*_PORT` in `.env` | If another project holds a default port, change it here. For Postgres, Temporal and MinIO also change the matching `LONGHAUL_*` URL or address, because compose and the code read separate variables. |
| Database password | `LH_POSTGRES_PASSWORD` in `.env` | Default `longhaul`. Keep the password inside `LONGHAUL_DATABASE_URL` the same. |
| Config file | `LONGHAUL_CONFIG` | Default `config/longhaul.example.yaml`. |

Every published port is bound to `127.0.0.1`, so other machines on your network cannot reach the database, Temporal or MinIO. Temporal and MinIO have no real authentication, and the Postgres login is a superuser.

Postgres reads its password once, when the `pgdata` volume is first created. To change it later, run `make down` (this deletes the volumes and all data), edit `.env`, then run `make up`.

Loopback binding does not keep out containers. On Docker Desktop a container on the default bridge network can open `host.docker.internal:<port>` and reach a port that is published on `127.0.0.1`. Sandboxes run repo build code with the network on during `setup_command`, so for any run against a real repo set a random `LH_POSTGRES_PASSWORD` (and the same password in `LONGHAUL_DATABASE_URL`). The sandbox environment is an allowlist and never receives either value.

## Commit attribution

Claude Code adds co-author trailers to commits and pull requests by default. This repo turns that off in `.claude/settings.json`:

```json
{"attribution": {"commit": "", "pr": ""}, "includeCoAuthoredBy": false}
```

The `attribution` key is the current setting. The older `includeCoAuthoredBy: false` key was its predecessor. It stays in the file so that older Claude Code versions also leave out the trailer.

## CI

Workflow: `.github/workflows/ci.yml`. It runs on pull requests into `dev` and `main`, on pushes to those branches and to `feat/`, `fix/`, `spike/` and `chore/` branches, and by hand. A new push to a pull request cancels the older run. CI has no secrets and always uses the scripted provider.

| Job | What it does |
|---|---|
| `lint` | `ruff check` and `ruff format --check` |
| `types` | `mypy src` |
| `unit` | unit tests with coverage, failing under 80 percent; uploads `coverage.xml` |
| `integration` | Postgres with pgvector and MinIO as service containers, a Temporal dev server started in the job, then `pytest -m integration` |
| `security` | gitleaks over the full git history, and `pip-audit` over the locked dependencies |
| `compose` | `docker compose config -q`, so a broken compose file fails |
| `ci` | waits for all the jobs above and fails unless every one succeeded |

The required check is `ci`. Branch protection points at that one name, so adding a job later only means adding it to the `needs` list of `ci`.

To run the quick jobs on your machine, use `make ci-local`.

To protect `main` and `dev`, run this once as a repo admin with `gh` logged in:

```bash
scripts/protect_branches.sh            # uses the repo of the current checkout
scripts/protect_branches.sh OWNER/REPO # or name it
```

It requires a pull request and an up-to-date `ci` check, and blocks force pushes and deletions. Admins are not forced to follow the rules, so the owner can still recover from a mistake.
