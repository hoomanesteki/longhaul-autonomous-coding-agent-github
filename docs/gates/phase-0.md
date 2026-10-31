# Phase 0 gate: Foundation and backlog import

- Date: 2026-10-25
- Result: **Pass on code, pending two owner actions** (branch protection and the GitHub issue import need an authenticated `gh`). `main` and the `v0.0.1` tag move once both are done.

## Test summary

| Check | Result |
|---|---|
| `make gate PHASE=0` on the full compose stack | 59 passed |
| Unit coverage | 100% (floor 80%) |
| GitHub Actions `ci` on `dev` (809db48) | lint, types, unit, integration, security, compose all green |
| gitleaks over full history | no leaks |
| pip-audit on locked deps | no known vulnerabilities |
| actionlint on `ci.yml` | no findings |

## Acceptance criteria

| Story | Criterion | Evidence | Status |
|---|---|---|---|
| LH-001 | Fresh clone, `make setup && make check` exits 0 | CI `lint`, `types`, `unit` jobs on a clean runner | Pass |
| LH-001 | `markers.py 2` prints `phase0 or phase1 or phase2` | `test_marker_expression` | Pass |
| LH-001 | `dev` and `main` exist on the remote | `git ls-remote` | Pass |
| LH-002 | `make up` returns only after healthchecks pass | Manual run: 8 services healthy | Pass |
| LH-002 | Temporal UI, MinIO bucket, `vector` extension | `test_services_reachable`, curl 8233 returned 200 | Pass |
| LH-002 | `make down` removes volumes | `docker volume ls` empty after down | Pass |
| LH-003 | PR into `dev` runs all jobs, `ci` is the required check | Jobs run on push; required check needs `scripts/protect_branches.sh` | Pending owner |
| LH-003 | Direct push to `dev` rejected | Needs `scripts/protect_branches.sh` | Pending owner |
| LH-003 | No secrets in the workflow | `test_no_secrets_in_ci` | Pass |
| LH-004 | Files exist, progress lists 80 stories | `test_progress_lists_all_stories` | Pass |
| LH-004 | No banned phrases or long dashes in docs | `test_no_banned_phrases`, `test_no_long_dashes` | Pass |
| LH-005 | Example YAML loads, unknown key and secret keys rejected | `test_settings.py` | Pass |
| LH-006 | Dry run prints 80 stories and 10 milestones, no API calls | `test_dry_run_makes_no_calls`, manual dry run | Pass |
| LH-006 | Second import creates nothing | `test_idempotent_plan` (fake gh) | Pass |
| LH-006 | Malformed heading fails loudly | `test_malformed_heading_raises` | Pass |
| LH-006 | 80 issues and 10 milestones on GitHub | Needs authenticated `gh` | Pending owner |
| LH-007 | Fixtures fail at base, reference patches pass | `test_fixtures.py` | Pass |
| LH-008 | `make migrate` creates tables, rerun is a no-op, roundtrip works | `test_upgrade_downgrade_roundtrip`, manual migrate | Pass |

## Invariants

Only I8 applies in Phase 0: the CI workflow sets `LONGHAUL_PROVIDER=scripted` and references no secrets (`test_no_secrets_in_ci`, `test_provider_is_scripted`). I1 to I7 cover code that does not exist yet. I9 is the cumulative gate run above.

## Smoke output

```
$ make up && make migrate && make gate PHASE=0
 Container longhaul-grafana-1 Healthy
uv run alembic upgrade head
============================= 59 passed in 12.61s ==============================
$ python scripts/import_backlog.py docs/plan/longhaul-build-plan.md --dry-run | tail -1
80 stories, 10 milestones
```

## Issues found

1. Another local project holds ports 3000, 9090, 16686, 4317 and 4318. Host ports are now set through `LH_*_PORT` variables in `.env.example`.
2. MinIO no longer publishes `minio/minio`. The stack uses `bitnamilegacy/minio`, which has no web console, so only port 9000 is exposed (ADR-0002).
3. Python 3.12.13 on macOS skips `.pth` files that carry the hidden flag, which broke the editable install. Pytest now sets `pythonpath = ["src"]`.
4. The first CI run failed on unit coverage (75%) after the database models landed without unit tests. Fixed with model and session tests, and `make check` now enforces the same floor as CI.

## Fix stories created

None open. The coverage fix above was done on `fix/lh-091-unit-coverage`.

## Owner actions to close the gate

1. `gh auth login`
2. `scripts/protect_branches.sh`
3. `uv run python scripts/import_backlog.py docs/plan/longhaul-build-plan.md`
