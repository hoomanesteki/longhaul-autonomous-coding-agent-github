# Phase 6 gate: Memory

- Date: 2026-11-08
- Result: **Pass on code, pending one manual step.** The memory smoke on `lh-demo-inventory` needs a GitHub token and an Anthropic key. See "Steps to close the gate".

## Test summary

| Check | Result |
|---|---|
| `make gate PHASE=7`, dev at 4e23dd9 (covers phases 0 to 7) | Green. ruff, ruff format and mypy clean; 6523 unit tests passed; the marker run had 6634 passed and 0 failed, in 38 minutes, with every e2e file (memory poisoning included) run with memory on. |
| The slow LocalEmbedder test | 3 passed, in a copy with the memory extra installed. |
| GitHub Actions `ci` on 4e23dd9 | All jobs green. |
| Mutation checks | With the approved-only filters removed from retrieval, the I7 test fails; with the store's approved-only search filter removed, 5 tests fail; with a closed PR set to approve its lessons, `test_closed_rejects` fails; with memory.mode off ignored, `test_off_mode` fails. |
| Named tests in the plan for Phase 6 stories | All present. |

## Acceptance criteria

| Story | Criterion | Evidence | Status |
|---|---|---|---|
| LH-601 | Inserting and nearest-neighbour querying work in Postgres with HashEmbedder | tests/integration/test_memory_schema.py::test_insert_and_knn passed (plus the HNSW index tests test_the_embedding_index_is_hnsw_on_cosine_distance and test_a_cosine_query_can_be_answered_by_the_index), in the phase6 marker run on dev at 4e23dd9 (scratchpad g6-phase6.log, 486 passed, 0 failed, 0 skipped). tests/unit/test_embedder.py::test_hash_embedder_deterministic also passed. | Pass |
| LH-601 | LocalEmbedder returns 384-dim unit vectors (opt-in test, marker slow) | tests/slow/test_local_embedder.py::test_dim and its 2 siblings: 3 passed in 14 s. This ran in a git archive copy of 4e23dd9 with `uv sync --locked --extra memory` into a scratch venv and the cached bge-small-en-v1.5 model (scratchpad g6-slow.log). The main .venv does not have the memory extra installed. | Pass |
| LH-602 | On the inventory fixture the map lists Inventory.remove with its signature and stays under 3,000 tokens | tests/integration/test_repo_map.py::test_inventory_map passed. It also checks that the sandbox's git status is clean after the script runs, and that the stored item is approved, source default_branch, source_ref base_sha. | Pass |
| LH-602 | A second task on the same base sha reuses the cached map (no rebuild, checked via a counter) | tests/integration/test_repo_map.py::test_cached_by_sha passed (script_runs stays 1, and a new commit makes it 2) | Pass |
| LH-602 | Syntax errors in one file don't break the map (the file is skipped and logged) | tests/integration/test_repo_map.py::test_syntax_error_skipped passed (WARNING record checked, the 'Not parsed' line, and the skipped list in the stored structure) | Pass |
| LH-603 | On calc, pytest -q is discovered and stored as approved; a bogus make test with no Makefile is not stored | tests/integration/test_command_discovery.py::test_calc_commands passed (112 s on real Docker). verify_commands(['make test','no-such-tool --check','pytest -q']) == ['pytest -q'] | Pass |
| LH-603 | Duplicates are not inserted twice | tests/integration/test_memory_store.py::test_dedupe and ::test_adds_that_race_make_one_item passed | Pass |
| LH-604 | The block never includes candidate or rejected items (I7) | tests/unit/test_memory_retrieve.py::test_only_approved_in_prompt passed, including the leaky-store case. Mutation in a copy: with the status checks removed from retrieve._approved_commands and _nearest_lessons, the test fails with "assert 'PROPOSED' not in ...". A second mutation turns off the approved_only filter in PgMemoryStore.search; then 5 tests fail, among them ... | Pass |
| LH-604 | The block stays under budget with 50 approved items present | tests/unit/test_memory_retrieve.py::test_budget_respected[4000/2500/1000/400/120] and ::test_the_default_budget_is_4000_tokens_with_50_approved_items passed | Pass |
| LH-604 | The system prompt has the memory block in position 4, so the cached prefix stays stable | tests/unit/test_prompt_layout.py::test_memory_block_position passed (system == bare + FACTS_HEADING + block; the block sits under the system cache marker and comes before the plan in the first message), plus ::test_an_empty_memory_block_leaves_the_system_prompt_as_it_was | Pass |
| LH-605 | Scripted output produces candidates linked to the PR number | tests/integration/test_lessons.py::test_candidates_created passed, along with ::test_a_task_without_a_pull_request_is_linked_by_its_id and ::test_a_second_run_of_the_same_activity_adds_nothing | Pass |
| LH-605 | Items over 300 chars are trimmed, and generic items matching a small blocklist are dropped | tests/unit/test_lessons.py::test_generic_filtered, ::test_an_item_over_300_characters_is_trimmed and ::test_a_trimmed_item_is_still_checked_for_being_generic passed | Pass |
| LH-606 | In FakeGitHub, merging the PR promotes all its candidates on the next poll, and closing it unmerged rejects them | tests/integration/test_memory_promotion.py::test_merged_promotes and ::test_closed_rejects passed. Mutation in a copy: with the closed branch of Promoter.settle set to approve=True, test_closed_rejects fails with {'approved'} != {'rejected'}. The file was restored afterwards. | Pass |
| LH-606 | Human approval via the CLI works for candidates without a PR | tests/integration/test_memory_promotion.py::test_cli_approve passed. Also ran `python -m longhaul.cli memory list --repo hoomanesteki/lh-demo-inventory` against the compose database; it printed 'no memory items for hoomanesteki/lh-demo-inventory'. | Pass |
| LH-607 | The poisoning e2e passes | tests/e2e/test_memory_poisoning.py::test_unmerged_lesson_never_in_prompt passed in 160 s in the phase6 run, and again in the make gate PHASE=7 marker run. It checks that the rejected row stays rejected, and it scans every blob of task B (the memory block and the view), not only the block. | Pass |
| LH-607 | memory.mode=off produces a system prompt with no memory block | tests/integration/test_memory_modes.py::test_off_mode, ::test_on_mode and ::test_off_mode_still_learns_a_test_command_from_the_verified_commands passed. Mutation in a copy: with notes_on forced to True in worker.build_parts, test_off_mode fails (the block is not ''). The file was restored afterwards. | Pass |
| Phase gate 6 | All 7 stories Done. ADR-0010 merged | docs/progress.md rows LH-601 to LH-607 are Done. docs/adr/0010-memory-write-policy.md is on dev, Status Accepted, and covers the write paths, the store, promotion, embeddings, retrieval and the mode switch. No phase 6 merge touched src/longhaul/contracts.py (git log v0.4.0..4e23dd9 -- contracts.py shows only fab170e, from LH-505). | Pass |
| Phase gate 6 | I7 holds, and the poisoning e2e passes | See the LH-604 and LH-607 rows, the mutation checks, and the gate run below | Pass |
| Phase gate 6 | Earlier e2e tests still pass with memory on | make gate PHASE=7 (scratchpad gate7.log), dev at 4e23dd9. ruff, format and mypy are clean. 6523 unit tests passed, coverage 88%. The marker run for phases 0 to 7 plus chaos_lite had 6634 passed and 0 failed in 38 min. Every e2e file passed: budget_flow, durable_run (3), issue_to_pr, long_task, memory_poisoning, run_local (2), stuck_flow. The e2e Stack, test_long_task and test_durable_run write the example config ... | Pass |
| Integration checkpoint 6 | Manual smoke on hoomanesteki/lh-demo-inventory: run task 1, merge its PR, check the lesson is approved, the second task's prompt shows it | Needs a GitHub token, a real repo on GitHub and a model key; none are on this machine. Its scripted twin passes: the poisoning e2e, with an approved lesson in task B's block. | Manual |
| Phase gate 6 | Tag v0.6.0 | The owner tags it after the gate PR merges to main | Manual |

## Invariants

- I7 (pass): Enforced in three layers. (1) PgMemoryStore.search filters status='approved' by default, add_candidate refuses approved items, and add_verified accepts only repo_map/command from default_branch. (2) render_memory_block filters again by status, repo and kind before the text leaves. (3) Promotion comes only from a merged PR, a person, or the default branch: promote_candidate/reject_candidate move only from candidate, and content-hash dedupe stops a rejected note from returning as a new ...
- I9 (pass): make gate PHASE=7 on dev at 4e23dd9 is green (gate7.log): 6523 unit tests passed, then the cumulative marker run for phase0 to phase7 with chaos_lite had 6634 passed, 0 failed, 764 deselected (live, slow, full chaos) in 2301 s. It ends with 'gate exit=0'. This covers phases 0 to 6.
- I1 (spot check) (pass): tests/unit/test_invariants.py: 90 passed. task.py's new code (load_memory behind LOAD_MEMORY_PATCH, lessons behind LESSONS_PATCH after the memory_ref test, and RunState as the workflow input) does no I/O and reads only recorded activity results

## Smoke output

The checkpoint's smoke runs against real GitHub with a real model, so it is the manual step below. Its scripted twin, `tests/e2e/test_memory_poisoning.py`, passed in the gate run: a lesson from a PR closed unmerged never reaches the next task's prompt, and an approved one does.

## Issues found

1. **LH-606 review minors, open:** no store-level test of the status guard in `promote_candidate` and `reject_candidate`; no test that the worker gives the poller a promoter; `memory_rejected` events store `approved_by`; an event with no lessons event is filed under `<repo>#<pr>`. Listed for the Phase 9 hardening pass.
2. **Known, open:** `run_tests` uses the worker-wide test command, not the repo's fallback command (LH-604 review).

## Fix stories created

None.

## Steps to close the gate

Needs a GitHub token, a real repo on GitHub and a model key; none are on this machine. Its scripted twin passes: the poisoning e2e, with an approved lesson in task B's block.
