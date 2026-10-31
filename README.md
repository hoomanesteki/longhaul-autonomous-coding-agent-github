# Longhaul

A background coding agent that takes a GitHub issue, works on it for hours in a sandbox, survives crashes, resumes where it stopped, and asks a person only at checkpoints. It ends with a pull request or an honest report of where it got stuck.

Status: Phase 1 of 9 done (`v0.1.0`). The agent fixes a local repo end to end inside a hardened Docker sandbox, with a scripted, Anthropic or OpenAI-compatible model. Phase 2 (Temporal durability and git checkpoints) is in progress. See [docs/progress.md](docs/progress.md).

Docs site: [hoomanesteki.github.io/longhaul-autonomous-coding-agent-github](https://hoomanesteki.github.io/longhaul-autonomous-coding-agent-github/), built from `docs/` with Quarto.

[![ci](https://github.com/hoomanesteki/longhaul-autonomous-coding-agent-github/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/hoomanesteki/longhaul-autonomous-coding-agent-github/actions/workflows/ci.yml)

## Why

Coding agents can fix many small, well-specified issues, but they are usually run by hand in a terminal. Runs die when a laptop sleeps, context fills up on long tasks, nobody notices when the agent loops, retries post the same comment twice, and cost is a surprise. Longhaul is a durable, observable harness around a coding agent that is built to measure what durability, compaction and verification actually buy.

## How it works

- Every model turn and every tool call is a separate step with its own timeout and retry. From Phase 2 each one is a Temporal activity with a heartbeat.
- The agent works in a Docker sandbox: non-root, no capabilities, read-only root filesystem, no network after setup, and no credentials inside.
- Every workspace change becomes a git checkpoint (Phase 2), so a retried tool call always starts from a known commit.
- Large tool outputs become handles and older turns become structured progress notes (Phase 4), so long tasks fit the context window.
- A planner, coder and verifier work inside one workflow, and a stuck detector pauses the run and asks one specific question (Phase 5).
- Hard budgets per task and per repo (Phase 7). The agent never pushes to a default branch, and a person merges every PR.

The design, contracts and story plan are in [docs/plan/longhaul-build-plan.md](docs/plan/longhaul-build-plan.md). Decisions are in [docs/adr/](docs/adr/).

## What works today

| Piece | State |
|---|---|
| Shared contracts (v2) | Messages, blocks, stop reasons, call options, task and plan models |
| Providers | Scripted (for CI), Anthropic (thinking stripped at ingestion, see ADR-0003), OpenAI-compatible |
| Sandbox | Hardened Docker container per task, streamed and bounded command output, background processes cleaned up after every command |
| Tools | `list_dir`, `read_file`, `search`, `edit_file`, `write_file`, `run_command`, `run_tests`, `git_diff`, `finish` |
| Agent loop | In-process loop with explicit rules for refusals, output limits, context limits and tool pairing |
| CLI | `longhaul run-local` runs the agent on a local folder |

## Quickstart

Requirements: Docker, [uv](https://docs.astral.sh/uv/), Python 3.12. No API key is needed for the scripted run.

```bash
make setup           # install dependencies and pre-commit hooks
make sandbox-image   # build the sandbox image
uv run longhaul run-local \
  --path tests/fixtures/repos/calc \
  --issue-file tests/fixtures/repos/calc/ISSUE.md \
  --provider scripted --script tests/fixtures/scripts/calc_fix.yaml
```

It prints each turn and the final diff. With a key, `--provider anthropic` runs the real model on the same fixture.

Other commands:

```bash
make check      # lint, format check, mypy, unit tests with coverage
make up         # start postgres, temporal, minio, otel, jaeger, prometheus, grafana
make migrate    # create the database tables
make itest      # integration tests against the local stack
make e2e        # end-to-end tests with the scripted provider
make down       # stop the stack and remove its volumes
```

If another project already uses a port, copy `.env.example` to `.env` and change the `LH_*_PORT` values.

## Repo layout

| Path | What it holds |
|---|---|
| `src/longhaul/` | The package: contracts, providers, sandbox, tools, agent loop, CLI, settings and database |
| `tests/` | Unit, integration and end-to-end tests, plus toy fixture repos with planted bugs |
| `config/` | Example `longhaul.yaml` and the pricing table |
| `docker-compose.yml` | Local backing services |
| `docs/adr/` | Architecture decision records |
| `docs/gates/` | Phase gate reports |
| `scripts/` | Backlog import, branch protection, fixture repo builder |
| `spikes/` | Throwaway scripts that verified API and SDK facts |

## Development

One story per branch (`feat/lh-<id>-<slug>`), merged into `dev`. `main` only moves when a phase gate passes, and each gate is tagged. Contribution rules are in [CONTRIBUTING.md](CONTRIBUTING.md). CI details are in [docs/runbook.md](docs/runbook.md).

## License

MIT
