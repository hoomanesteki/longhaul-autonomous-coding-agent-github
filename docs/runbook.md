# Runbook

## Start here

Run these from the repo root on a fresh clone. You need `uv`, Docker and `gh`.

```bash
make setup    # uv sync and pre-commit hooks
make up       # local stack via docker compose
make check    # ruff, format check, mypy, unit tests
```

The plan is in `docs/plan/longhaul-build-plan.md`. Story status is in `docs/progress.md`. Working rules for coding agents are in `CLAUDE.md`. Voice rules are in `docs/style.md`.

## Commit attribution

Claude Code adds co-author trailers to commits and pull requests by default. This repo turns that off in `.claude/settings.json`:

```json
{"attribution": {"commit": "", "pr": ""}, "includeCoAuthoredBy": false}
```

The `attribution` key is the current setting. The older `includeCoAuthoredBy: false` key was its predecessor. It stays in the file so that older Claude Code versions also leave out the trailer.
