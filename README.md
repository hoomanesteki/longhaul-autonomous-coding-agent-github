# Longhaul

A background coding agent that takes a GitHub issue, works on it for hours in a sandbox, survives crashes, resumes where it stopped, and asks a person only at checkpoints. It ends with a pull request or an honest report of where it got stuck.

Status: Phase 0 of 9 (foundation). The agent itself starts in Phase 1. See [docs/progress.md](docs/progress.md).

[![ci](https://github.com/hoomanesteki/longhaul-autonomous-coding-agent-github/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/hoomanesteki/longhaul-autonomous-coding-agent-github/actions/workflows/ci.yml)

## Why

Coding agents can fix many small, well-specified issues, but they are usually run by hand in a terminal. Runs die when a laptop sleeps, context fills up on long tasks, nobody notices when the agent loops, retries post the same comment twice, and cost is a surprise. Longhaul is a durable, observable harness around a coding agent that is built to measure what durability, compaction and verification actually buy.

## How it will work

- Every model turn and every tool call is a separate Temporal activity, with its own timeout, retry and heartbeat.
- Every workspace change is a git checkpoint, so a retried tool call always starts from a known commit.
- Large tool outputs become handles, and older turns become structured progress notes, so long tasks fit the context window.
- A planner, coder and verifier work inside one workflow. A stuck detector pauses the run and asks one specific question.
- Hard budgets per task and per repo. The agent never pushes to a default branch, and a person merges every PR.

The full design, contracts and story plan are in [docs/plan/longhaul-build-plan.md](docs/plan/longhaul-build-plan.md).

## Quickstart (what works today)

Requirements: Docker, [uv](https://docs.astral.sh/uv/), Python 3.12.

```bash
make setup      # install dependencies and pre-commit hooks
make check      # lint, format check, mypy, unit tests with coverage
make up         # start postgres, temporal, minio, otel, jaeger, prometheus, grafana
make migrate    # create the database tables
make itest      # integration tests against the local stack
make down       # stop the stack and remove its volumes
```

If another project already uses a port, copy `.env.example` to `.env` and change the `LH_*_PORT` values.

## Repo layout

| Path | What it holds |
|---|---|
| `src/longhaul/` | The package: settings and database now, agent, durable layer and integrations in later phases |
| `tests/` | Unit and integration tests, plus toy fixture repos with planted bugs |
| `config/` | Example `longhaul.yaml` and the pricing table |
| `docker-compose.yml` | Local backing services |
| `docs/adr/` | Architecture decision records |
| `docs/gates/` | Phase gate reports |
| `scripts/` | Backlog import, branch protection, fixture repo builder |

## Development

One story per branch (`feat/lh-<id>-<slug>`), merged into `dev`. `main` only moves when a phase gate passes, and each gate is tagged. Working rules for contributors and coding agents are in [CLAUDE.md](CLAUDE.md). CI details are in [docs/runbook.md](docs/runbook.md).

## License

MIT
