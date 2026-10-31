# Phase 1 gate: Agent core without durability

- Date: 2026-10-31
- Result: **Pass on code, pending owner actions** (a real-model run on calc and textkit, and the live API checks S1 to S10, both need an Anthropic key). See "Owner actions".

## Test summary

| Check | Result |
|---|---|
| `make gate PHASE=1` on the full compose stack | 1678 passed (second run). The first run had 1677 passed and one transient failure in `test_stack.py::test_services_reachable` while other Docker-heavy test runs shared the machine; it passed alone and in the full rerun. |
| Unit coverage | 93.5% (floor 80%), 1585 unit tests, also reproduced in a Linux container |
| GitHub Actions `ci` on `dev` | The `unit` job failed on 87e117c and 60f6b44 in `test_run_local_help_lists_every_option` only: rich adds color codes on GitHub Actions and they split the option names. The new failure annotations (`pytest-github-actions-annotate-failures`) named the test; it now compares plain text. Every other job, including the new `e2e` job, is green. |
| Scripted e2e `test_calc_scripted` | Passed locally and in the new CI `e2e` job |
| Named tests in the plan for Phase 0 and Phase 1 stories | all present (checked by script against the story blocks) |

## Acceptance criteria

| Story | Criterion | Evidence | Status |
|---|---|---|---|
| LH-101 | Every model round-trips through JSON | `test_roundtrip_all_models` (non-default values for every field since LH-198) | Pass |
| LH-101 | contracts.py imports only stdlib, pydantic and typing | `test_contracts_imports_are_pure` | Pass |
| LH-101 | mypy strict on the module | `mypy src` in CI `types` | Pass |
| LH-102 | ADR-0003 appendix lists the five facts with the SDK version | docs/adr/0003-own-agent-loop.md, appendix part 1 and part 3 (offline checks on anthropic 2.3.0) | Pass, live confirmation pending owner |
| LH-102 | Script under spikes/, excluded from mypy and the package | spikes/messages_api.py; `mypy src` only | Pass |
| LH-103 | Five calls return the five expected tool calls | `test_sequence` | Pass |
| LH-103 | A non-matching request raises with a helpful message | `test_no_match_raises` | Pass |
| LH-104 | Mocked tests for success, tool use, 429, 529, 500, timeout, context too long, 401 | `tests/unit/test_anthropic_provider.py` (httpx2 mock transport; a 32-mutation check) | Pass |
| LH-104 | Each error maps to the right kind, retry_after_s parsed | `test_error_mapping` and the retry-after tests, one HTTP request per error | Pass |
| LH-104 | Opt-in live round trip | `tests/live/test_anthropic_live.py::test_round_trip` (skipped without a key) | Pending owner |
| LH-105 | Mocked tool round trip and error mapping | `test_tool_roundtrip`, `test_error_mapping` | Pass |
| LH-105 | Transcript renders back without loss of tool ids | `test_tool_roundtrip` | Pass |
| LH-106 | Image builds in CI, `id -u` prints 10001 | CI `integration` builds it; `test_the_acceptance_command_runs_as_written` | Pass |
| LH-106 | rg and git work as user agent | `test_image_tools_present` | Pass |
| LH-107 | Runs as non-root, no network when disabled, timeout kills, no secrets (I3), cleanup | `tests/integration/test_sandbox.py` | Pass |
| LH-108 | edit_file exactly once, path escape rejected, schema errors are results, search finds a symbol | `tests/integration/test_file_tools.py`, `tests/unit/test_registry.py` | Pass |
| LH-109 | run_tests reports 1 failed on calc, output cap marker, git_diff after edit | `tests/integration/test_exec_tools.py` | Pass |
| LH-110 | Scripted calc fix on FakeSandbox, pairs valid, turn limit | `test_scripted_calc_fix`, `test_pairs_valid`, `test_turn_limit` plus the D8 and D9 rules (a 20-mutation check) | Pass |
| LH-111 | e2e run-local on calc with the scripted provider | `tests/e2e/test_run_local.py::test_calc_scripted`; the printed diff applied to a fresh copy matches REFERENCE.patch | Pass |
| LH-111 | Real-model run fixes calc and textkit | needs a key | Pending owner |
| LH-191 to LH-198 | Fix and contract stories from the pre-gate review | each finding reproduced with a failing test first; see their story blocks | Pass |

## Invariants

- I3: `tests/integration/test_sandbox.py::test_no_secrets_in_container_env`, plus `test_host_proxy_settings_do_not_reach_the_container` (LH-191).
- I8: `test_no_secrets_in_ci` scans every workflow file; `LONGHAUL_PROVIDER=scripted` in CI (`test_provider_is_scripted`).
- I1, I2, I4 to I7 cover code that arrives in Phase 2 and later. I1 already lists httpx2 and httpcore2 (ADR-0003, D10).
- I9: the cumulative gate run above.

## Smoke output

```
$ make up && make sandbox-image
$ uv run longhaul run-local --path tests/fixtures/repos/calc --issue-file tests/fixtures/repos/calc/ISSUE.md --provider scripted --script tests/fixtures/scripts/calc_fix.yaml
turn 1 coder: list_dir .
...
turn 5 coder: finish Changed add() to return a + b. All tests pass.
finished after 5 turns: Changed add() to return a + b. All tests pass.
$ docker ps -a --filter label=longhaul.task_id   # nothing left
```

## Issues found

1. A pre-gate adversarial review of everything merged in Phase 0 and Phase 1 produced about 120 findings across the sandbox, tools, providers, settings and db, CI and the backlog script. They became fix stories LH-191 to LH-197, and every finding was reproduced with a failing test before it was fixed or recorded as not a defect.
2. The LH-102 research found that the plan predates the current Messages API: thinking is on by default, forced tool choice is rejected on Sonnet 5.5, and edits to history invalidate thinking blocks. Decisions D1 to D11 in ADR-0003 and contracts v2 (LH-198) adapt the design.
3. `run-local` sent the absolute local path (with the user name) to the model in the system prompt. It now sends only the folder name, with a test.

## Fix stories created

LH-191 to LH-198, all Done.

## Owner actions to close the gate

1. With an Anthropic key: `uv run --with "anthropic>=2.3,<3" python spikes/messages_api.py --live` (S1 to S10, about 1 USD), then update the "confirmed live" markers in ADR-0003.
2. With an Anthropic key: `uv run longhaul run-local --path tests/fixtures/repos/calc --issue-file tests/fixtures/repos/calc/ISSUE.md --provider anthropic` and the same for textkit; paste the output tails here.
3. Still open from Phase 0: `gh auth login`, `scripts/protect_branches.sh`, and the backlog import.
