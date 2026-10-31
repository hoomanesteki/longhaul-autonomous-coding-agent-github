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

## CI

Workflow: `.github/workflows/ci.yml`. It runs on pull requests into `dev` and `main`, on pushes to those branches and to `feat/`, `fix/`, `spike/` and `chore/` branches, and by hand. A new push to a pull request cancels the older run. CI has no secrets and always uses the scripted provider. A unit test scans every file in `.github/workflows/` and fails on any use of the `secrets` context, including `secrets['NAME']`, `toJSON(secrets)` and `secrets: inherit`.

| Job | What it does |
|---|---|
| `lint` | `ruff check` and `ruff format --check` |
| `types` | `mypy src`. Strict checks apply to `longhaul.contracts`, `longhaul.durable`, `longhaul.context` and `longhaul.cost` only |
| `unit` | unit tests with coverage, failing under 80 percent; uploads `coverage.xml` |
| `integration` | Postgres with pgvector and MinIO as service containers, a Temporal dev server started in the job, then the integration tests (see "Test markers and `make gate`" below) |
| `security` | gitleaks over the full git history, and `pip-audit` over the locked dependencies |
| `compose` | `docker compose config -q`, so a broken compose file fails |
| `ci` | waits for all the jobs above and fails unless every one succeeded |

The required check is `ci`. Branch protection points at that one name, so a new job must be added to the `needs` list of `ci`. `tests/unit/test_ci_config.py` fails if any job is missing from that list.

To run the quick jobs on your machine, use `make ci-local`.

### Test markers and `make gate`

Every test carries two markers:

- The layer comes from the directory. `tests/conftest.py` adds it, so `tests/unit` gives `unit`, and `integration`, `e2e`, `chaos`, `live` and `slow` work the same way.
- The phase, `phase0` to `phase9`, names the phase that introduced the test. You write it, for example `pytestmark = [pytest.mark.unit, pytest.mark.phase2]`.

A test with no phase marker, or a test file outside a layer directory, fails collection with a message that names it. Without the phase marker, `make gate PHASE=N` would never run the test. Test files in different layers may share a name, because pytest imports them by path (`--import-mode=importlib`).

Some tests run only on request: `live` (paid model calls), `slow`, and the full `chaos` suite. `make gate`, `make itest`, `make e2e` and the `integration` job all leave them out. `chaos_lite` stays in, because gates run it. To run the others, name the marker, for example `uv run pytest -m slow`.

`make gate PHASE=N` runs `make check`, then pytest with the markers `phase0` to `phaseN`. It stops with an error if `scripts/markers.py` rejects `N`, so a typo cannot select every test.

### Branch protection

To protect `main` and `dev`, run this once as a repo admin with `gh` logged in:

```bash
scripts/protect_branches.sh            # uses the repo of the current checkout
scripts/protect_branches.sh OWNER/REPO # or name it
```

It requires a pull request and an up-to-date `ci` check, and blocks force pushes and deletions.

`enforce_admins` is false on purpose. The owner merges the phase-gate pull requests and the gatekeeper pushes `dev` as the admin, so GitHub has to let admins through. The rules bind collaborators and bots, and they do not bind admins: a direct push to `dev` is rejected for a token without admin rights and accepted for an admin. A fine-grained token that the owner mints acts with the owner's rights, so these rules do not stop it. The branch guard in the agent's own code (invariant I5) is what keeps the agent off the default branch. `tests/unit/test_ci_config.py` pins this value, so changing it means changing this paragraph too.
