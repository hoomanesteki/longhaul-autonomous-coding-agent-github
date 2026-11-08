# Phase 4 gate: Context engineering

- Date: 2026-11-07
- Result: **Pass on code, pending two manual steps** that need an Anthropic key: the live cache test, and recorded usage samples for the token meter's calibration. See "Steps to close the gate".

## Test summary

| Check | Result |
|---|---|
| `make gate PHASE=4`, dev at 5c3a114 | `make gate PHASE=4` on dev at 5c3a114 covers phases 0 to 4. ruff, ruff format and mypy clean; 5224 unit tests passed, coverage 92%; the marker run had 4950 passed and 0 failed, `chaos_lite`, the long-task e2e and the issue-to-PR e2e included, in 29 minutes. The run before it on the same code failed 2 e2e tests and errored the 5 chaos runs, all with MinIO's RequestTimeTooSkewed: the Mac had slept and the Docker VM's clock was behind. With the clocks back in step the run was green. |
| GitHub Actions `ci` on 5c3a114 | All ten jobs green. |
| Mutation check | With the previous notes dropped in the compact activity, `tests/e2e/test_long_task.py::test_longlog_many_compactions` fails at the notes assertion. |
| Named tests in the plan for Phase 4 stories | All present. |

## Acceptance criteria

| Story | Criterion | Evidence | Status |
|---|---|---|---|
| LH-401 | After 3 observations, the projection is within 10% of the true count on recorded Anthropic usage samples (fixtures from the spike) | tests/unit/test_tokens.py::test_calibration_within_10pct passes (at least 30 samples, none off by more than 10%). But tests/fixtures/usage_samples.json says "synthetic": true: the samples come from a model of a transcript, not from recorded API calls. Recording real samples needs the live spike S4 with an API key. | Manual |
| LH-401 | decide returns compact at 0.61 and force at 0.86 | tests/unit/test_tokens.py::test_budget_decisions (122,000 and 172,000 of 200,000), plus test_budget_acts_when_a_threshold_is_reached and the 8k-window variant | Pass |
| LH-402 | A 10,000-line output becomes a shaped result under the token cap, with a valid handle | tests/unit/test_truncate.py::test_shape_large_output, and tests/integration/test_read_output.py::test_page_and_filter, which checks the shaped text the model reads and the handle marker | Pass |
| LH-402 | read_output(handle, pattern="FAILED") returns exactly the failing lines | tests/integration/test_read_output.py::test_page_and_filter (pattern part) | Pass |
| LH-402 | An unknown handle returns ok=False | tests/integration/test_read_output.py::test_unknown_handle, and test_a_handle_of_another_task_is_unknown | Pass |
| LH-403 | find_cut never splits a tool_use from its tool_result (hypothesis property test) | tests/unit/test_compact.py::test_find_cut_property | Pass |
| LH-403 | Given scripted compactor output, apply yields a transcript that passes the pairing validator and starts with a user message | tests/unit/test_compact.py::test_apply_valid_transcript | Pass |
| LH-403 | Notes from round 1 survive round 2 (a tried_and_failed item persists) | tests/unit/test_compact.py::test_notes_merge_keeps_failures; repair and fallback in ::test_repair_then_fallback | Pass |
| LH-404 | With window 8,000, no llm_turn request exceeds hard_limit * window in the long-task test (recorded projections) | tests/integration/test_compaction.py::test_never_exceeds_hard_limit (60 recorded projections, all at or under 6,800; 3 or more compactions, none forced). The LH-407 e2e also checks max_fraction < 0.85 from reported usage (0.59 in my smoke run). | Pass |
| LH-404 | A forced CONTEXT_TOO_LONG from the scripted provider triggers compaction and one retry, and the run continues | tests/integration/test_compaction.py::test_forced_compaction_on_overflow (one forced event at turn 6, 11 calls, retry keeps 2 turns behind the notes, run ends with PR_OPENED) | Pass |
| LH-404 | The log blob holds every turn, and view holds the elided and compacted form | tests/integration/test_compaction.py::test_log_and_view_split; docs/adr/0013-context-strategy.md is present (in v0.2.0 already) | Pass |
| LH-405 | A rendering test confirms the block order and that the first four blocks are byte-identical across turns | tests/unit/test_prompt_layout.py::test_stable_prefix, ::test_breakpoints_count_le_4; cache_ratio on the turn row in tests/unit/test_records.py::test_a_turn_row_has_the_cache_ratio_of_its_usage | Pass |
| LH-405 | With a live key (opt-in), turn 3+ shows cache_read_tokens > 0 | tests/live/test_cache_live.py::test_cache_hits_after_turn_2 was skipped: no ANTHROPIC_API_KEY on this machine | Manual |
| LH-406 | Each mode runs the calc scripted task, and the totals are written | tests/integration/test_context_modes.py::test_modes_run[full], [no_compaction], [none]: the totals in the tasks row match the provider usage and the ledger | Pass |
| LH-406 | none on the long-task fixture ends with outcome error, summary "context limit", and does not crash | tests/integration/test_context_modes.py::test_none_mode_hits_limit_cleanly (60-turn task, 8k window). Also checked by hand on the longlog fixture: longhaul run ... --window 8000 --context-mode none printed "stopped after 17 turns: context limit"; status showed failed with 0 compactions; nothing was left behind. | Pass |
| LH-407 | The task finishes with all 12 modules fixed | tests/e2e/test_long_task.py::test_longlog_many_compactions passed in gate4.log. The smoke run finished after 62 turns with 12 files in the diff and "12 passed". | Pass |
| LH-407 | At least 3 compactions ran, and max_fraction < 0.85 | The e2e asserts both. Smoke status showed "context: 16 compactions, max_fraction 0.59" (16 compaction events, none forced). | Pass |
| LH-407 | The notes after the last compaction still list modules fixed in the first 10 turns | The e2e notes assertion. The mutation that drops the previous notes in the compact activity makes it fail at test_long_task.py:312. | Pass |
| Phase gate 4 | All 7 stories Done. ADR-0013 merged | docs/progress.md rows LH-401 to LH-407 are Done; docs/adr/0013-context-strategy.md is on dev and in v0.2.0 | Pass |
| Phase gate 4 | The long-task e2e passes, and Phase 3 e2e still passes | gate4.log: tests/e2e/test_long_task.py and tests/e2e/test_issue_to_pr.py (test_label_to_pr_fake_github) passed; 4950 passed, 0 failed | Pass |
| Phase gate 4 | The live cache test was run once, with cache_ratio noted in the gate report | Needs ANTHROPIC_API_KEY: run uv run pytest tests/live/test_cache_live.py and note cache_ratio | Manual |
| Phase gate 4 | Tag v0.4.0 | Done after the gate PR merges into main. Not done yet. | Manual |

## Invariants

- I2 (pass): Every activity's input and output goes through assert_small/small(), and since LH-292 the guarded data converter refuses any payload over 256,000 bytes. The new LlmTurnInput, ToolExecInput and AppendResultsInput.window_tokens fields are ints. Proven by tests/unit/test_invariants.py::test_payload_size_guard and its variants, and by tests/integration/test_payload_guard_history.py (an oversized payload never reaches the history). All passed.
- I6 (pass): llm_turn charges the ledger per call (_charge). The compact activity charges each compactor call, repair attempts included, through on_billed. Proven by tests/integration/test_cost_ledger.py::test_every_call_ledgered (passed; it is a phase7 test, so I ran it directly) and tests/integration/test_compaction.py::test_every_compactor_call_has_a_ledger_row_of_its_own (passed). The smoke run had 62 sonnet rows and 16 haiku rows, one per compaction.
- I9 (pass): make gate PHASE=4 at 5c3a114 (gate4.log): markers phase0 to phase4, chaos_lite included (tests/chaos/test_kill_worker.py 5/5), 4950 passed, 0 failed; unit run 5224 passed. All Phase 4 integration and e2e files carry the phase4 marker and show up in the run.

## Smoke output

Integration checkpoint 4, with a worker on its own task queue, a config with `memory.embedder: hash` (fastembed is not installed here) and a database at head:

```
task lh-local-longlog-b07dae99: Twelve helpers in longlog return wrong results
repo local:.../tests/fixtures/repos/longlog, provider scripted, context window 8000 tokens, task queue p4review-smoke
status: coding, 61 turns done, cost $0.0000, checkpoint f5ea8c3
finished after 62 turns: Fixed the planted bug in each of the 12 longlog modules. 12 passed.
$ longhaul status lh-local-longlog-b07dae99
task lh-local-longlog-b07dae99: completed
status: done, 62 turns done, cost $0.0000, checkpoint f5ea8c3
context: 16 compactions, max_fraction 0.59
finished after 62 turns: Fixed the planted bug in each of the 12 longlog modules. 12 passed.
```

The same run with `--context-mode none` ended "stopped after 17 turns: context limit", with 0 compactions and nothing left behind.

## Issues found

1. **The checkpoint's smoke text started no worker** and left out two steps the code now needs: the database at head (`make migrate`, for `tasks.max_fraction`) and either `uv sync --extra memory` or a config with `memory.embedder: hash` (LH-604). The checkpoint text now has them.
2. **A gate run hit MinIO's RequestTimeTooSkewed** after the Mac slept. Not a code problem; the run after the clocks agreed was green.
3. **Chaos workers took the default metrics port 9464**, so a chaos run could fight another worker for it. The harness and `scripts/chaos_kill.sh` now ask for a free port (on the integration line, merged after this gate).
4. Known and open, not new: a task started with `longhaul run` has no `tasks` row, so `status` derives the context totals from events and coder turns; `run_tests` uses the worker-wide test command, not the repo's fallback command (LH-604 review); `compact` counts tokens with the coder's tool list for a planner transcript.

## Fix stories created

None. The smoke text is a docs fix in this commit.

## Steps to close the gate

- With an Anthropic key: `uv run pytest -m live tests/live/test_cache_live.py`, and note `cache_ratio` from turn 3 on in this report.
- With an Anthropic key: record real usage samples for `tests/fixtures/usage_samples.json` (spike S4 in `spikes/messages_api.py`), which is synthetic today, and rerun `tests/unit/test_tokens.py::test_calibration_within_10pct`.
