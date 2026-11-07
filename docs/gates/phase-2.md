# Phase 2 gate: Temporal durability and checkpoints

- Date: 2026-11-04
- Result: **Pass after two fix stories.** The first gate run failed one chaos run, and the independent review found the I2 guard in the payload converter missing. Fix stories LH-291 and LH-292 closed both, and the second gate run was green. The real-model recording for the full chaos suite and the live API checks still need an Anthropic key. See "Steps to close the gate".

## Test summary

| Check | Result |
|---|---|
| First `make gate PHASE=2`, dev at 7aaff72 | ruff, ruff format and mypy clean. 3887 unit tests passed, coverage 92.52%. The marker run (phases 0 to 2, `chaos_lite` included) had 3008 passed and 1 failed: `test_kill_worker.py::test_a_kill_at_a_random_moment_resumes[19]`, "0 of 1 planned kills happened". |
| Second `make gate PHASE=2`, dev at 0530be5 | Green. ruff, ruff format and mypy clean. 4520 unit tests passed, coverage 93%. The marker run had 3259 passed and 0 failed, `chaos_lite` included, in 34 minutes. The run before it, at 3749a09, had 2 failures in the new payload guard tests: their task had an issue number on an owner/name repo, which LH-306 delivers as a pull request, and the test registers no GitHub activities. 0530be5 makes that task issue-less, as the other workflow tests are. |
| `uv run pytest -m chaos_lite` | 5/5 at 7aaff72 when run alone (517 s). After LH-291, 5/5 three times in a row on the builder's branch, and 5/5 in the second gate run. |
| Mutation check on `restore(pre_sha)` | Done twice. With the restore in `ToolActivities._start_from` replaced by `pass`, `test_kill_during_tool_exec_resumes` fails both times: the line that the killed command appended to CHANGES.txt appears twice in the final diff. |
| Named tests in the plan for phases 0 to 2 | All present (checked by script against the story blocks) but one: LH-109's `test_exec_tools.py::test_output_cap_marker`. LH-402 replaced the Phase 1 output cap, as its story block says, and `test_a_long_output_comes_back_whole` checks the new behavior in the same file. |
| GitHub Actions `ci` on `dev` | At 0530be5 every job passed except `unit`: `test_cli_durable.py::test_a_context_mode_that_does_not_exist_is_a_usage_error_and_starts_nothing` read the usage error with rich's color codes in it, as a Phase 1 test once did. 7a748e4 reads it as plain text, and the unit tests pass locally with GITHUB_ACTIONS=true (4520 passed). The next run failed `integration` on a race in `test_a_burst_of_hints_survives_many_hand_overs` (see Issues), fixed in e91d011. CI on e91d011: all ten jobs green (`chaos-lite` is skipped on a push; it runs nightly and on dispatch). |

## Acceptance criteria

| Story | Criterion | Evidence | Status |
|---|---|---|---|
| LH-201 | The ADR-0004 appendix answers all seven points with code snippets | docs/adr/0004-temporal.md, "Appendix: SDK facts", sections 1 to 7 | Pass |
| LH-202 | Put and get round-trip a 5 MB payload against MinIO | `tests/integration/test_blobs.py::test_roundtrip_large` | Pass |
| LH-202 | A tampered object raises BlobIntegrityError | `tests/integration/test_blobs.py::test_integrity_check` (same length, longer, shorter, empty) | Pass |
| LH-202 | The payload guard raises on a 300 KB object | `tests/unit/test_invariants.py::test_payload_size_guard` | Pass |
| LH-203 | status() reports PREPARING then CODING | `tests/integration/test_task_workflow.py::test_status_query_phases` | Pass |
| LH-203 | cancel gives CANCELLED and calls a cleanup activity | `test_cancel_signal` (outcome, the CleanupInput payload, cleanup after the cancelled turn) | Pass |
| LH-203 | A second start with the same task_id fails with a clear error | `test_duplicate_start_rejected` and `test_duplicate_start_rejected_after_the_first_run_finished` | Pass |
| LH-203 | The I1 test passes | `test_workflows_have_no_io_imports`, `test_workflow_import_closure_has_no_io_imports` | Pass |
| LH-204 | Scripted provider and a real sandbox fix calc through activities | `tests/integration/test_activities.py::test_workflow_fixes_calc_via_activities` | Pass |
| LH-204 | No history payload over 256 KB | `test_history_payloads_small` (a 31-call turn, every payload walked) | Pass |
| LH-204 | OVERLOADED twice is retried and the run completes | `test_retries_on_overloaded` (started on attempt 3) | Pass |
| LH-204 | INVALID_REQUEST fails the activity without retry | `test_invalid_request_not_retried` | Pass |
| LH-205 | After edit_file and commit, the branch head equals the returned sha | `tests/integration/test_checkpoints.py::test_commit_and_head` | Pass |
| LH-205 | restore(base) brings the workspace back byte for byte | `test_restore_exact` (status, hashes, modes, staged, ignored, untracked) | Pass |
| LH-205 | commit on a clean tree returns None | `test_clean_tree_no_commit` | Pass |
| LH-205 | A bundle restored into an empty folder has the same head | `test_restore_from_bundle`. Using the bundle when a host has no repo (F4) is not wired into ensure_sandbox yet; see Issues. | Pass |
| LH-206 | docker rm -f of the sandbox: the next call recreates it at head_sha | `tests/integration/test_sandbox_recovery.py::test_container_removed_is_recreated_at_head` and the container plus volume case | Pass |
| LH-206 | A tool_exec killed after an edit applies the edit once on retry | `test_retry_after_partial_edit_applies_once`, and `test_kill_during_tool_exec_resumes` with a real SIGKILL | Pass |
| LH-206 | Every mutating call has a post_sha row | `test_every_mutation_checkpointed` | Pass |
| LH-207 | Hand-over every 3 turns: 4 runs in one workflow id and the reference diff | `test_continue_as_new_preserves_state` with stubs. The reviewer also ran the real stack (inventory, 11 turns): 4 runs, outcome finish, the same diff as a run with no hand-over. | Pass |
| LH-207 | turn_index, head_sha, cost_usd and hints carry over | `test_continue_as_new_preserves_state` | Pass |
| LH-207 | A hint signalled right before the hand-over reaches the next prompt | `test_signal_not_lost_across_can`, `test_a_task_hands_over_and_the_hint_reaches_the_next_run` | Pass |
| LH-208 | A worker and `longhaul run` fix calc with the scripted provider | `tests/e2e/test_durable_run.py::test_run_calc_durable` | Pass |
| LH-208 | Ctrl-C on the worker, then a restart, completes the run | `test_a_worker_stopped_with_sigint_mid_run_is_replaced_and_the_run_completes` | Pass |
| LH-209 | `pytest -m chaos_lite` passes 5/5 locally and in CI | Failed once in the first gate run (seed 19, see Issues). Passes after LH-291 locally. The CI job is nightly and on dispatch, and runs from main, so its first run comes after this gate's merge. | Pass locally, CI pending |
| LH-209 | The test fails when restore(pre_sha) is removed | See the mutation check above | Pass |
| LH-209 | Kills in llm_turn, in tool_exec and between activities are each hit | The three aimed tests assert the kind from the worker's last activity line | Pass |
| LH-291 | Fix story: seeded kills planned by progress, lint without the ruff cache | See its story block | Pass |
| LH-292 | Fix story: payload guard in the converter, local issue cap, root-proof tests | See its story block | Pass |

## Invariants

- I1: `tests/unit/test_invariants.py::test_workflows_have_no_io_imports` (an AST scan of `durable/workflows/`) and `test_workflow_import_closure_has_no_io_imports`, which follows the first-party imports. `durable/workflow_sandbox.py` refuses the same modules at run time.
- I2: `assert_small` wraps the input and output of every activity, and since LH-292 the data converter refuses any payload over 256,000 bytes before it is written (ADR-0005). A workflow that schedules an oversized input fails with PayloadTooLargeError and the payload never reaches the history (`tests/integration/test_payload_guard_history.py`).
- I3: `tests/integration/test_sandbox.py::test_no_secrets_in_container_env` and the proxy test. The `/ckpt` mount carries no URL or credential (`test_no_clone_url_or_credentials_in_the_repo_or_the_sandbox`).
- I9: the second gate run above.

## Smoke output

The checkpoint's script, run as `scripts/chaos_kill.sh 3`, which kills the worker's own pid:

```
$ scripts/chaos_kill.sh 3
worker started, pid 55838
killed worker 55838. Its last activity line:
activity tool_exec started: task lh-local-inventory-a0cb9b28, attempt 1, turn 0, list_dir
worker started, pid 64567
task lh-local-inventory-a0cb9b28: remove() allows negative stock
status: preparing, 0 turns done, cost $0.0000
status: coding, 0 turns done, cost $0.0000, checkpoint afd3f58
status: waiting for a worker to answer
status: coding, 0 turns done, cost $0.0000, checkpoint afd3f58
...
status: coding, 10 turns done, cost $0.0000, checkpoint 48b39a7
finished after 11 turns: remove() raises InventoryError when stock is too low. All tests pass.
final diff against base:
...
activity lines for a second attempt or later: 2
no leftovers
```

## Issues found

1. **A seeded chaos kill could miss its run.** `plan_kills` drew each pause in seconds from the reference run's wall time. On a loaded machine the reference was slow, the killed run was fast, and the run ended before its kill. Fixed by LH-291: a seeded kill waits for the k-th activity of the reference and a pause no longer than that activity took.
2. **The ruff cache hid a wrong import order.** A test linted before its new module existed kept its old import order in the cache, so `make check` passed where CI would fail. Found on the LH-306 branch. Fixed by LH-291: `make check` lints with `--no-cache`.
3. **No I2 guard in the payload converter.** Plan section 4.3 names one; only the activity-side check existed, so an oversized input reached the history before it was refused. A 300 KB `--issue-file` showed it. Fixed by LH-292.
4. **Two snapshot tests skipped as root with no issue link.** Fixed by LH-292: they raise the read error another way and run as any user.
5. **The checkpoint's smoke text killed the wrong process.** `uv run longhaul worker &` makes `$!` the pid of uv, and SIGKILL cannot be passed on, so the worker kept running as an orphan and nothing was retried. The checkpoint now runs `scripts/chaos_kill.sh`, which starts `.venv/bin/longhaul` and kills that pid.
6. **docs/progress.md was behind.** LH-209 and LH-307 were still Todo. Updated.
7. **Two tests failed only in CI.** `test_a_context_mode_that_does_not_exist_is_a_usage_error_and_starts_nothing` read rich's usage error with its color codes, which split the option name (7a748e4 reads it as plain text). `test_a_burst_of_hints_survives_many_hand_overs` held the last turn, but on a fast runner the last turn could start before the burst of hints was over, so its input had 20 of the 30; it failed on a docs-only commit too. It now holds the turn before the last one until every hint is sent (e91d011).
8. Known and open, not new: F4 (restore from the bundle on a host that has no repo) is not wired into ensure_sandbox (LH-206 notes); a transient failure of a cost ledger write loses that call's row (LH-701 review). Both are listed for the Phase 9 hardening pass.

## Fix stories created

- LH-291 · Chaos kills by progress, and lint without a cache
- LH-292 · Payload guard in the converter and root-proof tests

## Steps to close the gate

- With an Anthropic key: record the chaos suite's tasks with the real model (`tests/chaos/test_kill_worker.py`, the `chaos` marker records into `eval/chaos/recordings/inventory`) and run the live checks.
- After the merge to main: dispatch the `chaos-lite` CI job once and confirm 5/5.
