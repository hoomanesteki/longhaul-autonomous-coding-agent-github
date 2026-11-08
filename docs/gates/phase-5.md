# Phase 5 gate: Planner, verifier and stuck handling

- Date: 2026-11-08
- Result: **Pass on code after one fix story, pending one manual step.** The real-model smoke on `lh-demo-inventory` needs a GitHub token and an Anthropic key. See "Steps to close the gate".

## Test summary

| Check | Result |
|---|---|
| First `make gate PHASE=5`, dev at c1d2796 | ruff, ruff format and mypy clean; 6269 unit tests passed and 1 failed, so the marker run did not start. The failure is issue 1 below. |
| Second `make gate PHASE=5`, dev at 7efd944 | Green. 6271 unit tests passed, coverage 91%; the marker run (phases 0 to 5, `chaos_lite`, the issue-to-PR e2e through planner, coder and verifier, the long task and the stuck flow e2e) had 5775 passed and 0 failed, in 51 minutes. |
| GitHub Actions `ci` on 7efd944 | All ten jobs green. |
| Mutation checks | With the verdict taken on the model's word alone, `test_precheck_overrides_model` fails. With the cleanup step removed from the wind-up, all four `test_no_leftover_containers` cases fail. |
| Named tests in the plan for Phase 5 stories | All present. |

## Acceptance criteria

| Story | Criterion | Evidence | Status |
|---|---|---|---|
| LH-501 | With the scripted planner, the plan is stored, posted once and visible in status() | tests/integration/test_planner.py::test_plan_stored_and_posted (also test_the_phase_is_planning_until_the_plan_is_stored). Run on dev at c1d2796 from the main checkout, in the 419-test run of the Phase 5 files | Pass |
| LH-501 | A planner run_command that writes a file leaves the tree clean before coding starts | tests/integration/test_planner.py::test_planner_cannot_mutate | Pass |
| LH-501 | An invalid plan triggers one repair turn, and a second failure ends with an error outcome and a clear summary | tests/unit/test_roles.py::test_plan_repair_turn, plus tests/integration/test_planner.py::test_an_invalid_plan_gets_one_repair_turn and ::test_a_second_invalid_plan_ends_with_an_error_outcome | Pass |
| LH-502 | Time-skipping env: approve after 2 h proceeds, reject then approve yields plan v2, no signal for 24 h ends with stuck | tests/integration/test_plan_gate.py::test_approve_proceeds (env.sleep 2 h, then approve_plan), ::test_reject_revises, ::test_timeout_stuck | Pass |
| LH-502 | Repos with the gate off go straight to coding | tests/integration/test_plan_gate.py::test_gate_off_skips | Pass |
| LH-503 | A 3-step scripted run produces 3 step checkpoints and a PR branch with 3 commits whose combined diff equals the scratch head diff | tests/integration/test_coder_steps.py::test_step_checkpoints, ::test_pr_commits_per_step, tests/unit/test_squash.py::test_group_by_step | Pass |
| LH-503 | Plan statuses show in the system prompt and in status() | tests/integration/test_coder_steps.py::test_plan_statuses_show_in_the_first_message_and_in_status. The plan goes in the first message of the transcript rather than the system prompt, which keeps the cached prefix stable. This is documented in the task.py docstring and the runbook. | Pass |
| LH-504 | First review fails, the coder fixes it, the second review passes, and the PR is ready, not draft | tests/integration/test_verifier.py::test_send_back_then_pass and ::test_a_pull_request_is_ready_when_a_command_backed_the_verdict; tests/e2e/test_issue_to_pr.py::test_label_to_pr_fake_github (draft is False, 'Verified: 3 passed' in the body), 1 passed in 80 s | Pass |
| LH-504 | If the pre-check fails, a model verdict of passed=true still counts as fail | tests/integration/test_verifier.py::test_precheck_overrides_model. Mutation check in a copy: with judge_review set to `passed = verdict.passed`, the test fails ([(1, True)] != [(1, False), (2, True)]) | Pass |
| LH-504 | Three failed rounds end with stuck | tests/integration/test_verifier.py::test_max_rounds_stuck | Pass |
| LH-505 | Each rule has a positive and a negative unit test | tests/unit/test_stuck.py::test_no_progress[pos/neg], ::test_repeated_failure[pos/neg], ::test_edit_churn[pos/neg], ::test_tool_errors[pos/neg] | Pass |
| LH-505 | The detector state serializes to under 8 KB at a full window | tests/unit/test_stuck.py::test_state_size_bound and ::test_state_size_bound_holds_for_paths_that_take_many_bytes[7 cases] | Pass |
| LH-505 | It doesn't fire during the planning phase | tests/unit/test_stuck.py::test_the_detector_does_not_fire_while_planning, tests/integration/test_stuck_detector.py::test_a_planner_that_repeats_a_failure_is_not_stuck | Pass |
| LH-506 | e2e on the scripted provider: a planted loop triggers REPEATED_FAILURE, the comment is posted once, the hint comes in via /longhaul hint, the task resumes and finishes | tests/e2e/test_stuck_flow.py::test_stuck_pause_hint_resume | Pass |
| LH-506 | The comment has all four sections and is under 250 words | tests/unit/test_templates.py::test_stuck_comment_sections and ::test_the_longest_summary_still_fits_in_the_word_limit | Pass |
| LH-507 | For pr_opened, stuck, cancelled and error, exactly one final comment and the right labels | tests/integration/test_outcomes.py::test_each_outcome_reports_once[pr_opened/stuck/cancelled/error] | Pass |
| LH-507 | No sandbox container remains after any outcome | tests/integration/test_outcomes.py::test_no_leftover_containers[pr_opened/error/cancelled/stuck] on real Docker. Mutation check in a copy: with the cleanup step removed from _wind_up, all 4 cases fail at `assert machine.containers() == []`. Afterwards no container labeled longhaul.task_id=lh-out* was left | Pass |
| Phase gate 5 | All 7 stories Done, ADR-0007 merged | docs/progress.md rows LH-501 to LH-507 are Done. docs/adr/0007-roles-in-one-workflow.md is on dev, Accepted, and covers the planner, coder steps and verifier | Pass |
| Phase gate 5 | Every outcome path tested, with no leftover containers | tests/integration/test_outcomes.py (all passed), including the replay tests for each ending and for histories from before LH-507 | Pass |
| Phase gate 5 | make gate PHASE=5 is green | First run: one unit test failed (see Issues). After LH-591: 6271 unit passed and the marker run 5775 passed, 0 failed. | Pass |
| Phase gate 5 | Real-model smoke on hoomanesteki/lh-demo-inventory: plan comment, approval, verified ready PR, with links | Needs an Anthropic key and a GitHub token, neither on this machine. The owner runs: (1) add hoomanesteki/lh-demo-inventory to the repos list of the config in LONGHAUL_CONFIG with allowlisted: true, plan_approval_required: true and its setup_command and test_command. config/longhaul.example.yaml lists only lh-demo-calc. (2) export LONGHAUL_PROVIDER=anthropic ANTHROPIC_API_KEY=... ... | Manual |
| Phase gate 5 | Tag v0.5.0 | Done by the owner after the gate PR merges to main | Manual |

## Invariants

- I1 (pass): tests/unit/test_invariants.py::test_workflows_have_no_io_imports passed, along with the import-form cases. task.py imports only asyncio, contextlib, dataclasses, typing and temporalio, and passes through the pure modules: agent hints/rules, contracts, durable acts/wind_up, github lifecycle, and roles coder/planner/stuck/verifier, each of which imports only stdlib, pydantic and contracts. Time comes from workflow.now() only. Every new end path sits behind workflow.patched (PLAN_GATE_PATCH, ...
- I2 (pass): tests/unit/test_invariants.py::test_payload_size_guard and its 5 variants passed, and the converter guard from LH-292 is unchanged. New boundary payloads have bounds: the stuck window is under 8 KB (test_state_size_bound), the ask_human question is cut to 2000 chars, notes to 500, outcome summaries to 300, the error trace to ERROR_TRACE_CHARS, and pre-check and diff outputs travel as BlobRefs. The integration test tests/integration/test_payload_guard_history.py belongs to the marker run, ...
- I4 (pass): tests/unit/test_invariants.py::test_github_writes_only_in_side_effects passed. The new writes (post_final in durable/activities/outcome.py, the stuck comment, the plan review comment and labels) all go through side_effects.post_comment or the label activities with idempotency keys (<task>:final:1, stuck:<n>, plan_comment:<v>)
- I6 (pass): Planner and verifier turns use the same llm_turn activity as the coder, which tests/integration/test_cost_ledger.py::test_every_call_ledgered covers. The new compactor call summarize_for_human writes a row per billed call (tests/unit/test_activities_summary.py::test_every_billed_call_has_a_ledger_row_and_the_cost_is_their_sum passed, and tests/integration/test_pause_flow.py::test_the_summary_is_a_billed_call_with_a_ledger_row passed). test_cost_ledger.py runs in the marker run, which did not ...
- I9 (pass): the second gate run above, green.

## Smoke output

The checkpoint's smoke runs against real GitHub with a real model, so it is the manual step below. Its scripted twins passed in the gate run: `tests/e2e/test_issue_to_pr.py` (plan comment, a verified ready PR with "Verified: 3 passed") and `tests/e2e/test_stuck_flow.py` (a planted loop, one stuck comment, a `/longhaul hint`, the task resumes and finishes).

## Issues found

1. **The stuck evidence cut off the command that failed.** `test_a_failing_test_run_leaves_its_count_and_its_failure` failed in the main checkout: the label of a failed command keeps 80 characters, and the full path of the interpreter filled them, so "-m pytest -q" was lost. It passed in the shorter worktree paths. Fixed by LH-591: the label names the program, not its path; the failure hash still uses the whole command.
2. **The checkpoint smoke named a repo the example config did not list.** `config/longhaul.example.yaml` now has `hoomanesteki/lh-demo-inventory` with `plan_approval_required: true` (LH-591).
3. **A planned coder that calls `finish` is not reviewed.** LH-503 and LH-504 kept that on purpose, so histories from before the verifier end the way they did, and tests pin it. Left as it is and listed for the Phase 9 hardening pass.
4. **LH-505 bumped `contracts_version` to 3** with a `tool_calls` field the story did not list; it is in the changelog row of docs/architecture.md.
5. **Three commit subjects in the range are 72 characters or longer.** History is not rewritten.

## Fix stories created

- LH-591 · Stuck evidence names the command, and the demo repo

## Steps to close the gate

Needs an Anthropic key and a GitHub token, neither on this machine. The owner runs: (1) add hoomanesteki/lh-demo-inventory to the repos list of the config in LONGHAUL_CONFIG with allowlisted: true, plan_approval_required: true and its setup_command and test_command. config/longhaul.example.yaml lists only lh-demo-calc. (2) export LONGHAUL_PROVIDER=anthropic ANTHROPIC_API_KEY=... LONGHAUL_GITHUB_TOKEN=github_pat_...; make up && make sandbox-image && make migrate; uv run longhaul worker (shell 1); uv run longhaul poller install (shell 2). (3) gh issue edit 1 --repo hoomanesteki/lh-demo-inventory --add-label longhaul; wait for the 'Plan, version 1' comment; gh issue comment 1 --repo hoomanesteki/lh-demo-inventory --body "/longhaul approve". (4) Watch the Temporal UI (http://localhost:8233) for VERIFYING. Then run gh pr view --repo hoomanesteki/lh-demo-inventory --json isDraft,title,body / head -40, expect isDraft false and 'Verified:' in the body, and paste the plan comment, approval and PR links into docs/gates/phase-5.md
