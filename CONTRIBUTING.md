# Contributing to Longhaul

## What this is
Background coding agent on Temporal. Plan: docs/plan/longhaul-build-plan.md. Progress: docs/progress.md.

## Commands
- make setup        # uv sync, pre-commit install
- make up / make down   # local stack via docker compose
- make check        # ruff, format check, mypy, unit tests
- make itest        # integration tests (needs make up)
- make e2e          # end-to-end with scripted provider
- make gate PHASE=N # everything for phases 0..N

## Working rules
1. One story per branch: feat/lh-<id>-<slug> from dev. PR into dev. Squash merge.
2. Write the story's listed tests first. Make them fail. Then implement.
3. Do not edit src/longhaul/contracts.py unless the story says so. If you must, explain why in the PR.
4. Workflow code (src/longhaul/durable/workflows/) must stay deterministic: no I/O, no datetime.now(), no random, no imports of httpx/httpx2/httpcore2/docker/sqlalchemy/boto3/anthropic/openai/subprocess. Use workflow.now() and activities.
5. Anything bigger than 16 KB crossing an activity boundary goes to the blob store as a BlobRef.
6. All GitHub writes go through src/longhaul/github/side_effects.py with an idempotency key.
7. Never put secrets into the sandbox environment. Never push to a default branch.
8. Never delete or weaken a test to get green. Never mark a test skip without an issue link.
9. CI has no API keys. Use ScriptedProvider or ReplayProvider in tests.
10. Keep functions small and typed. mypy must stay clean.

## Style
- Plain English in docs, comments, commits and PRs. No hype words. No emoji.
- Commit messages: conventional commits, lowercase, imperative, under 72 chars. Body only when the why is not obvious.
- Comments explain why, not what.

## When unsure
Stop and leave a short note in the PR description under "Questions". Do not guess at shared contracts.
