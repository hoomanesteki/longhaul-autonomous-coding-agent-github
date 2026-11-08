# Phase 3 gate: GitHub integration

- Date: 2026-11-07
- Result: **Pass on code, pending one manual step.** The real-GitHub smoke on `lh-demo-calc` needs a GitHub token and an Anthropic key, which this machine does not have. See "Steps to close the gate".

## Test summary

| Check | Result |
|---|---|
| `make gate PHASE=4`, dev at 5c3a114 | `make gate PHASE=4` on dev at 5c3a114 covers phases 0 to 4. ruff, ruff format and mypy clean; 5224 unit tests passed, coverage 92%; the marker run had 4950 passed and 0 failed, `chaos_lite`, the long-task e2e and the issue-to-PR e2e included, in 29 minutes. The run before it on the same code failed 2 e2e tests and errored the 5 chaos runs, all with MinIO's RequestTimeTooSkewed: the Mac had slept and the Docker VM's clock was behind. With the clocks back in step the run was green. |
| GitHub Actions `ci` on 5c3a114 | All ten jobs green (`chaos-lite` is skipped on a push; it runs nightly and on dispatch). |
| Mutation checks | With the marker search taken out of `post_comment`, the database-loss idempotency test fails. With the write-permission check always true, `test_unauthorized_cancel_ignored` fails. |
| Named tests in the plan for Phase 3 stories | All present. |

## Acceptance criteria

| Story | Criterion | Evidence | Status |
|---|---|---|---|
| LH-301 | Each endpoint has a unit test | tests/unit/test_fake_github.py::test_endpoints plus the per-endpoint tests in that file; run in the unit batch (636 passed, log at scratchpad/g3-unit.log) | Pass |
| LH-301 | after_write_fail stores the comment and still returns 502 | tests/unit/test_fake_github.py::test_after_write_fail_mode (passed) | Pass |
| LH-302 | Against FakeGitHub, the read methods return parsed dicts | tests/unit/test_github_client.py::test_reads (passed) | Pass |
| LH-302 | 502 maps to GitHubRetryable, 404 to GitHubFatal, 403 with x-ratelimit-remaining: 0 to GitHubRetryable with the right retry_after_s | tests/unit/test_github_client.py::test_error_mapping, ::test_rate_limit_retry_after (passed) | Pass |
| LH-302 | AppAuth caches the token (two calls, one exchange), mocked token endpoint | tests/unit/test_github_client.py::test_app_token_cached asserts len(endpoint.requests) == 1 after two calls (passed); ::test_git_auth_header_not_in_url also passed | Pass |
| LH-303 | An issue on a non-allowlisted repo produces no spec, and a log line says why | tests/unit/test_intake.py::test_non_allowlisted_ignored, which reads caplog and asserts 'not allowlisted' (passed) | Pass |
| LH-303 | Comments from users without write permission are excluded from the spec | tests/unit/test_intake.py::test_comment_permission_filter (passed) | Pass |
| LH-303 | task_id format is stable for weird repo names (My.Repo_v2) | tests/unit/test_intake.py::test_task_id_format (passed) | Pass |
| LH-304 | after_write_fail on comments: a retried post_comment leaves exactly one comment | tests/integration/test_github_side_effects.py::test_retry_after_5xx_no_duplicate (passed; 54/54 in the integration batch, scratchpad/g3-integ.log). Mutation in a copy (marker search in post_comment removed): this test fails | Pass |
| LH-304 | side_effects truncated (DB loss): post_comment with the same key posts no new comment | tests/integration/test_github_side_effects.py::test_comment_idempotent_after_db_loss (passed). Same mutation makes it fail: assert '1000004' == '1000003' | Pass |
| LH-304 | Existing open PR for the head: create_pr returns its URL without creating another | tests/integration/test_github_side_effects.py::test_pr_idempotent_by_head, which also covers DB loss (passed) | Pass |
| LH-305 | prepare_workspace from FakeGitHub plus a local bare remote gives a sandbox at base_sha with no token in env, git config or /ckpt/config | tests/integration/test_clone_push.py::test_prepare_from_remote, ::test_no_token_in_sandbox_or_ckpt_config (passed) | Pass |
| LH-305 | push_branch(..., 'main') raises ForbiddenBranchError and no push happens | tests/unit/test_branch_guard.py::test_refuses_default_branch, ::test_refuses_protected_patterns, ::test_push_branch_to_main_raises_and_nothing_is_pushed (passed) | Pass |
| LH-305 | A pushed branch appears in the fake remote | tests/integration/test_clone_push.py::test_a_checkpoint_is_pushed_from_the_host_and_the_default_branch_is_not (passed) | Pass |
| LH-306 | PR branch has exactly one commit on top of base, and its diff equals the scratch head diff | tests/integration/test_deliver.py::test_pr_branch_single_commit_equals_scratch (passed) | Pass |
| LH-306 | PR body contains Closes #<n> and the test summary | tests/integration/test_deliver.py::test_pr_body_fields (passed); the e2e also asserts 'Closes #n' and '3 passed' in the body | Pass |
| LH-306 | Labels follow the transitions in FakeGitHub | tests/integration/test_deliver.py::test_label_transitions (passed); e2e asserts the label events labeled/unlabeled working, labeled pr-open | Pass |
| LH-307 | Two labeled issues and one unlabeled, poll_once twice: exactly two TaskWorkflows | tests/integration/test_poller.py::test_starts_one_task_per_issue (passed) | Pass |
| LH-307 | Labeled issue on a non-allowlisted repo starts no task | tests/integration/test_poller.py::test_ignores_non_allowlisted (passed) | Pass |
| LH-307 | Schedule exists after poller install, and install again changes nothing | tests/integration/test_poller.py::test_schedule_install_idempotent (passed) | Pass |
| LH-308 | Each command parses, and junk returns None | tests/unit/test_commands.py::test_parse_all, ::test_ignores_code_blocks (passed) | Pass |
| LH-308 | A cancel comment from a read-only user is ignored, and the task keeps running | tests/integration/test_poller.py::test_unauthorized_cancel_ignored (passed). Mutation in a copy (_may_steer returns True): the test fails, extra signal ('cancel', ['reader', '']) | Pass |
| LH-308 | e2e: labeled issue to PR with one Picked up comment, one PR and longhaul:pr-open, under 3 minutes in CI | tests/e2e/test_issue_to_pr.py::test_label_to_pr_fake_github passed in 42.6 s locally under an asyncio.timeout(180) that covers the whole path, and passed in gate4.log. Since LH-501 it asserts exactly two comments: comments[0] starts with 'Picked up' and comments[1] is the plan with key <task>:plan_comment:1. So there is still exactly one Picked up comment, plus (1,) = fake.pulls(repo), count_pulls == 1 after a ... | Pass |
| LH-391 | A summary such as 'fixes #7, see @alice' gives a title that closes nothing and notifies no one | tests/unit/test_squash.py::test_the_subject_cannot_close_an_issue_or_notify_a_person, ::test_a_mark_in_the_subject_gets_a_zero_width_space_like_the_body; tests/integration/test_deliver.py::test_the_title_and_the_commit_cannot_close_another_issue_or_notify_anyone (all passed) | Pass |
| LH-391 | Every subject is at most 72 characters for any summary and scope (property test) | tests/unit/test_squash.py::test_every_subject_is_one_line_under_the_limit_with_no_live_marks (hypothesis, 300 examples, conventional and marked summaries, scopes up to 200 chars) plus ::test_a_header_longer_than_the_limit_cannot_push_the_subject_past_it (passed) | Pass |
| LH-391 | Moving the base tag in the bare repo does not change the base of the PR branch | tests/unit/test_squash.py::test_a_base_tag_that_the_agent_moves_does_not_move_the_base_of_the_branch[to a checkpoint/to a stray commit/away]; tests/integration/test_deliver.py::test_a_base_tag_that_the_agent_moves_does_not_move_the_base_of_the_pull_request; tests/integration/test_task_workflow.py::test_the_base_of_the_pull_request_is_kept_across_a_hand_over. The pause/resume label helpers are run by ... | Pass |
| Phase gate 3 | All 8 stories Done; ADR-0008, 0009 and 0012 merged | docs/progress.md rows LH-301 to LH-308 and LH-391 are Done; docs/adr/0008-polling.md, 0009-sandbox-model.md and 0012-github-auth.md exist on dev with Status: Accepted (0008 updated for LH-308 commands) | Pass |
| Phase gate 3 | Idempotency tests pass, including DB loss | tests/integration/test_github_side_effects.py, 15/15 passed, including test_comment_idempotent_after_db_loss and the DB-loss half of test_pr_idempotent_by_head; mutation check above shows the DB-loss test depends on the marker search | Pass |
| Phase gate 3 | Real-GitHub smoke produced exactly one PR on lh-demo-calc, with a screenshot in the gate report | No GitHub token or Anthropic key on this machine. The owner runs this from the repo root on dev: `export LONGHAUL_GITHUB_TOKEN=<fine-grained PAT, contents/issues/pull requests read-write on the lh-demo repos> LONGHAUL_PROVIDER=anthropic ANTHROPIC_API_KEY=<key> LONGHAUL_CONFIG=config/longhaul.example.yaml` (it allowlists hoomanesteki/lh-demo-calc with label longhaul, 60 s interval); `make up && make ... | Manual |
| Phase gate 3 | make gate is green | make gate PHASE=4 on 5c3a114 (scratchpad/gate4.log): ruff, format, mypy clean, 5224 unit passed, marker run phase0..phase4 with chaos_lite: 4950 passed, 907 deselected, gate exit=0. Not rerun here, as instructed | Pass |

## Invariants

- I4 (pass): tests/unit/test_invariants.py::test_github_writes_only_in_side_effects (AST scan for _post/_patch/_delete outside github/side_effects.py) and ::test_the_write_scan_sees_the_methods_and_their_one_caller, plus the parametrized form tests, all pass. LH-308's only new write, the reply to a user who may not steer, goes through side_effects.post_comment with a per-login key. A grep for raw POST/PATCH/PUT/DELETE in src/longhaul finds only the GitHub App token exchange in client.py:467, which is not ...
- I5 (pass): tests/unit/test_branch_guard.py (default branch, main/master/develop/release/* in any case, push to main raises and nothing is pushed) passes. assert_not_default runs in push_branch and in build_pr_branch before any ref is written. test_deliver.py::test_a_push_to_the_default_branch_is_refused_and_recorded passes. The e2e asserts the remote's branches are exactly {main, longhaul-pr/<n>} with main unchanged
- I9 (pass): make gate PHASE=4 on 5c3a114 ran the cumulative markers phase0 to phase4, chaos_lite included: 4950 passed, 0 failed (scratchpad/gate4.log); every Phase 3 test file carries the phase3 marker and appears in that log; CI green on the commit

## Smoke output

The checkpoint's smoke runs against real GitHub with a real model, so it is the manual step below. Its scripted twin, `tests/e2e/test_issue_to_pr.py::test_label_to_pr_fake_github`, passed in the gate run (42.6 s against the 180 s limit): one "Picked up" comment, then the plan comment, one draft PR that closes the issue, and the labels `longhaul` and `longhaul:pr-open`.

## Issues found

1. **LH-306 review findings.** Model text could close an issue or notify a person through the commit subject and the PR title; a long scope broke the subject length; the base of the PR came from a tag the sandbox can move. Fixed by LH-391.
2. **Since LH-501 every task the poller starts plans first.** The issue-to-PR e2e now expects the plan comment after the pickup comment. LH-308's criterion still holds: there is one "Picked up" comment, one PR and `longhaul:pr-open`.
3. **Three commit subjects in the range are 72 characters or longer** (one docs commit of 75, and two later-phase commits). History is not rewritten.

## Fix stories created

- LH-391 · PR title and base hardening (from the review of LH-306, before this gate)

## Steps to close the gate

No GitHub token or Anthropic key on this machine. The owner runs this from the repo root on dev: `export LONGHAUL_GITHUB_TOKEN=<fine-grained PAT, contents/issues/pull requests read-write on the lh-demo repos> LONGHAUL_PROVIDER=anthropic ANTHROPIC_API_KEY=<key> LONGHAUL_CONFIG=config/longhaul.example.yaml` (it allowlists hoomanesteki/lh-demo-calc with label longhaul, 60 s interval); `make up && make sandbox-image`; `uv run longhaul worker &`; `uv run longhaul poller install`; `gh issue edit 1 --repo hoomanesteki/lh-demo-calc --add-label longhaul`; `sleep 90 && gh issue view 1 --repo hoomanesteki/lh-demo-calc --comments / tail -20` (expect 'Picked up', then 'Plan, version 1', label longhaul:working); once the task ends: `gh pr list --repo hoomanesteki/lh-demo-calc` (expect exactly one draft PR from longhaul-pr/1 with 'Closes #1', and label longhaul:pr-open on the issue); `gh issue comment 1 --repo hoomanesteki/lh-demo-calc --body "/longhaul status"` (expect a status comment on the next poll). Paste the output tail and a screenshot of the PR list and the issue into docs/gates/phase-3.md
