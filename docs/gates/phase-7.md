# Phase 7 gate: Cost and observability

- Date: 2026-11-08
- Result: **Pass on code, pending the manual steps.** The budget pause on real GitHub and the dashboard and trace screenshots need a GitHub token and an Anthropic key. See "Steps to close the gate".

## Test summary

| Check | Result |
|---|---|
| `make gate PHASE=7`, dev at 4e23dd9 (first run) | Green: 6523 unit tests; the marker run had 6634 passed and 0 failed. The review of that run found the issues below. |
| `make gate PHASE=7`, dev at 4e67be4 with LH-791 (second run) | Green. ruff, ruff format and mypy clean; 6543 unit tests passed; the marker run had 6653 passed and 0 failed, in 63 minutes. The three small fixes of 9e7eb1a (below) landed while it ran, and CI covers them. |
| Each finding of the first review, checked again on 4e67be4 | Fixed, each shown by a probe that fails on the code before LH-791 and passes on dev: budgets in eval runs and in `longhaul run`; a hint pause and a budget pause with the same number; the midnight wait; the first request of each role; the smoke ports. |
| Regression review of the LH-791 merge | No blocker or major. Event payloads gained `kind`, which is activity input only, so old histories replay. |
| GitHub Actions `ci` | All jobs green on ba68f16 (unit, integration, e2e, lint, types, security, docs, compose, terraform). The runs on 4e67be4 and 9e7eb1a failed in integration (issue 7). |
| Named tests in the plan for Phase 7 stories | All present. |

## Acceptance criteria

| Story | Criterion | Evidence | Status |
|---|---|---|---|
| LH-701 | Cost math matches hand-computed values for 3 usage samples | tests/unit/test_pricing.py::test_cost_math[sonnet writes the cache], [sonnet reads the cache], [haiku compaction]: passed in the scratch copy run (182 passed) and in make gate PHASE=7 | Pass |
| LH-701 | Every provider call in the scripted e2e has a ledger row (I6), including compaction calls | tests/integration/test_cost_ledger.py::test_every_call_ledgered (3 calls, 3 rows, usd matches pricing, turns and task totals agree) plus tests/integration/test_compaction.py::test_every_compactor_call_has_a_ledger_row_of_its_own. compact, summarize_for_human and extract_lessons all write rows through on_billed. Both passed in the gate | Pass |
| LH-701 | A retried llm_turn produces two ledger rows with attempts 1 and 2 | tests/integration/test_cost_ledger.py::test_retry_ledgered_twice (rows (0,1),(0,2), one turns row, history shows attempt 2): passed | Pass |
| LH-702 | Each reason triggers a pause with a correct comment (time-skipping env) | tests/integration/test_budgets.py::test_task_usd_pause_and_topup, ::test_turn_limit_pause, ::test_wall_clock_pause, ::test_daily_waits_until_midnight on the time-skipping server. Comment text checked by ::test_a_task_of_an_issue_asks_once_with_a_key_and_moves_the_label, ::test_the_daily_pause_of_an_issue_says_so_and_asks_for_no_one, tests/unit/test_budget_comment.py and ... | Pass |
| LH-702 | add_budget(usd=1) resumes, and the new limit is used | test_task_usd_pause_and_topup (top_up usd=1.0, resumed via budget, budget_added usd 1.0, PR_OPENED), ::test_the_new_limit_is_the_one_in_force_after_a_top_up (a top-up too small pauses again as budget:2), e2e test_pause_and_top_up through the poller: passed | Pass |
| LH-702 | The daily limit waits until midnight UTC and then continues without a signal | test_daily_waits_until_midnight: resume_at is 00:00 UTC, no signal:add_budget in history, timer_started < timer_fired < llm_turn, resumed via midnight: passed | Pass |
| LH-702 | No llm_turn starts while paused (assert from history) | assert_checked_before_every_turn on the history in every pause test, and provider.calls unchanged while paused. Mutation check in a copy (a scratch copy with the _afford call removed from TaskWorkflow._turn, 5 of 5 selected budget tests fail (task_usd, turn limit, two wall clock, daily) | Pass |
| LH-703 | With an in-memory span exporter, a scripted run produces 1 workflow span tree with N chat spans and M tool spans | tests/integration/test_tracing.py::test_span_counts_match: passed (scratch run and gate) | Pass |
| LH-703 | No span attribute contains prompt text when capture_content is false | tests/integration/test_tracing.py::test_no_content_by_default and ::test_failed_activity_text_stays_out_of_spans: passed | Pass |
| LH-703 | Jaeger shows the trace in the manual smoke | Needs a screenshot. Longhaul's Jaeger is not running here (LH_JAEGER_UI_PORT=16687; port 16686 belongs to keystone-jaeger-1). docker compose up -d jaeger otel-collector, run a task with a worker, open http://localhost:16687 and search service longhaul-worker. | Manual |
| LH-704 | After a scripted e2e run, /metrics exposes every metric listed, with non-zero values where expected | tests/integration/test_metrics.py::test_metrics_exposed_after_run (all 11 TYPE lines, turns 4, overloaded error 1, retries 1, cost equals ledger, sandbox_recreated 1): passed | Pass |
| LH-704 | The dashboard JSON loads in Grafana without errors (provisioning test via the Grafana API in compose) | tests/integration/test_grafana.py::test_dashboard_provisioned passed in make gate PHASE=7. After the gate, curl http://127.0.0.1:3300/api/search?query=Longhaul returns uid longhaul, title Longhaul (longhaul-grafana-1 on LH_GRAFANA_PORT=3300) | Pass |
| LH-705 | The report for the scripted inventory run contains every section, and the numbers match the DB | tests/integration/test_run_report.py::test_sections_and_numbers: passed. The pause pairing finding (issue 2) is fixed by LH-791 | Pass |
| LH-705 | The PR body stays under 60,000 chars even with 500 turns | tests/integration/test_run_report.py::test_body_cap: passed | Pass |
| LH-706 | Each pattern is redacted in logs and in report output | tests/unit/test_redaction.py::test_patterns (31 cases covering sk-, ghp_, github_pat_, AKIA, private key blocks), tests/unit/test_logging.py, tests/unit/test_run_report.py (ghp_ in the report becomes [redacted]). render_run_report passes the whole page through the same redact(): passed | Pass |
| LH-706 | Log lines carry task_id and trace_id inside activities | tests/integration/test_logging.py::test_context_bound: passed | Pass |
| Phase gate 7 | All 6 stories Done. I6 holds | docs/progress.md lists LH-701 to LH-706 as Done. Every named test exists and passes. I6 is shown below | Pass |
| Phase gate 7 | Budget pause and resume on real GitHub, with a screenshot in the gate report | No GitHub token or API key here. Set budgets.task_usd: 0.50 for hoomanesteki/lh-demo-textkit, label an issue, run gh issue view 1 --repo hoomanesteki/lh-demo-textkit --comments / tail -15 (the budget:1 comment), then gh issue comment 1 --repo hoomanesteki/lh-demo-textkit --body "/longhaul budget +1", take a screenshot of the issue, then uv run longhaul report lh-hoomanesteki-lh-demo-textkit-1 | Manual |
| Phase gate 7 | Dashboard and trace screenshots in the gate report | Checked locally: the dashboard is provisioned (test_grafana and the API search above) and the metrics test passes. After the real run, take screenshots of http://localhost:3300/d/longhaul/longhaul (not :3000, which is keystone-grafana-1 here) and of the trace at http://localhost:16687 | Manual |
| Phase gate 7 | Tag v0.7.0 | Made at the dev to main merge of this gate. | Pass |

## Invariants

- I6 (pass): Every activity that calls a provider writes a row: llm_turn (LlmActivities via self._ledger.record), compact and summarize_for_human (ContextActivities._charge through on_billed), extract_lessons (memory.py on_billed). Proven by tests/integration/test_cost_ledger.py::test_every_call_ledgered, ::test_retry_ledgered_twice, ::test_a_billed_call_that_failed_is_ledgered_and_so_is_its_retry and tests/integration/test_compaction.py::test_every_compactor_call_has_a_ledger_row_of_its_own, all green ...
- I9 (pass): make gate PHASE=7 at 4e23dd9: ruff and format clean (516 files), mypy clean (179 files), 6523 unit tests passed with 88.43% coverage, marker run phase0..phase7 with chaos_lite: 6634 passed, 764 deselected, 0 failed in 38 min, gate exit 0. The second run on 4e67be4 was green as well (see the test summary).

## Issues found

1. **Budgets were checked only for tasks the poller starts.** The eval runner and `longhaul run` started tasks without the check, so an eval task ran past its dollar cap. Fixed by LH-791: both enforce budgets, and `longhaul run --no-budgets` turns the check off.
2. **The run report paired a pause with its resume by number alone.** The hint and budget counters both start at 1, so a task with one of each showed a made-up wait for one and "not answered" for the other. Fixed by LH-791: every pause event carries `kind`, and a resume answers the newest open pause of its number and kind.
3. **A daily-limit pause did not say it waited until midnight UTC.** Fixed by LH-791.
4. **The first request of each role was priced as output only.** Fixed by LH-791: it is estimated from the system prompt, tools, first message, plan, memory block and review brief. The check of that fix found that a cut review output was counted in tokens while the request cuts it in characters; 275a704 counts the cut text at the task's own ratio.
5. **The checkpoint 7 smoke opened fixed ports.** It now starts Jaeger and the collector and opens the ports compose published (`docker compose port`).
6. **`longhaul run` showed a bare "paused" line.** With budgets on by default a local run can pause on its cap; the status line now says what it waits for (b8be997).
7. **CI's integration job failed after the LH-791 merge, with no test named.** LH-791 gave the shared `stack` fixture of `tests/integration/test_eval_runner.py` a `worker_prices` parameter. Two Phase 9 modules reuse `stack` by name (`test_baselines.py`, `test_swebench_runner.py`) and did not bring in the new fixture, so six tests errored at setup. The gate runs only the markers up to its phase, so it did not see them; CI runs every integration test and did. The annotation plugin writes a line only for a failure in the test body, which is why the job named no test. Fixed in 3920475 by importing the fixture in both modules.
8. **The runbook says the console tops up a paused task.** True once the Phase 8 console is merged; checked again at that gate.

## Fix stories created

- LH-791 · Budgets for every task and a report that pairs each pause

## Steps to close the gate

- Jaeger shows the trace in the manual smoke: Needs a screenshot. Longhaul's Jaeger is not running here (LH_JAEGER_UI_PORT=16687; port 16686 belongs to keystone-jaeger-1). docker compose up -d jaeger otel-collector, run a task with a worker, open http://localhost:16687 and search service longhaul-worker
- Budget pause and resume on real GitHub, with a screenshot in the gate report: No GitHub token or API key here. Set budgets.task_usd: 0.50 for hoomanesteki/lh-demo-textkit, label an issue, run gh issue view 1 --repo hoomanesteki/lh-demo-textkit --comments | tail -15 (the budget:1 comment), then gh issue comment 1 --repo hoomanesteki/lh-demo-textkit --body "/longhaul budget +1", take a screenshot of the issue, then uv run longhaul report lh-hoomanesteki-lh-demo-textkit-1
- Dashboard and trace screenshots in the gate report: Checked locally: the dashboard is provisioned (test_grafana and the API search above) and the metrics test passes. After the real run, take screenshots of http://localhost:3300/d/longhaul/longhaul (not :3000, which is keystone-grafana-1 here) and of the trace at http://localhost:16687
