# Security

This page says what Longhaul protects, from whom, and which test shows that each protection holds. It also lists what is not covered and how to rotate each secret. Plan section 4.2 and ADR-0009 give the design. This page is the check against them.

Every row of the control table names a test. A control without a test is listed apart, under "Controls without a test", so nobody reads a claim as proven when it is only configured.

## Threat model

### Assets

| Asset | Why it matters |
|---|---|
| GitHub token or GitHub App key | Write access to the allowlisted repos is the power to push code (ADR-0012). |
| LLM API keys | They spend money, and a leaked key does so for anyone. |
| Console admin token | It approves plans, sends hints and changes memory, and it reads transcripts. |
| The default branch of each target repo | A push there skips review (invariant I5). |
| The worker host | The worker holds the Docker socket, which is the same as root on the host. |
| Postgres and the blob store | Transcripts, tool outputs, diffs and memory notes of every task. |
| The budget | A loop that does not stop is a cost, not only a bug. |

### Actors

| Actor | Trust |
|---|---|
| The people who run Longhaul | Trusted. They hold every secret. |
| An author of an issue or a comment | Not trusted. The text goes to the model. |
| The target repo's code and files | Not trusted. They run in the sandbox and are read by the model. |
| The model | Not trusted to resist instructions that arrive in text. It can run any command the sandbox allows. |
| A console user with the token | Trusted as an operator. |
| Anyone who can reach the API or a published port | Not trusted. |
| Another container on the same Docker host | Not trusted. On Docker Desktop it can reach ports that are published on loopback. |
| A package index or a base image | Not trusted beyond what pinning and the audit catch. |

### Entry points

| Entry point | What comes in | Where it is handled |
|---|---|---|
| GitHub issues and comments (poller) | Task text, commands in comments | The text is data for the model. Commands come from people with the right to give them. Output back to GitHub is escaped so it cannot notify or close anything. |
| Repo files and the setup command | Code that runs in the sandbox | The sandbox: no secret, no capability, no network after setup. |
| Tool calls from the model | Commands, paths, edits | `run_command` runs in the sandbox only. File tools refuse paths outside the workspace. |
| The model's diff and summary | Text that ends in a commit and a pull request | Built on the host, pushed from the host, never to a default branch. |
| The console API | HTTP requests | Bearer token, rate limit on actions, redaction on reads. |
| Published ports (Postgres, Temporal, MinIO, metrics) | Connections | Bound to `127.0.0.1`. See the gaps below. |
| Memory notes | Text the agent wrote from a task | Only `approved` items reach a prompt (invariant I7). |
| Dependencies and images | Third-party code | Locked versions, `pip-audit` and gitleaks in CI. |

## Controls and the tests that prove them

Paths are relative to `tests/`. A test marked `integration` needs `make up` and the sandbox image.

### The sandbox

| Control | Test |
|---|---|
| The sandbox environment holds no secret (I3). It comes from an allowlist of five names, never from the worker's environment. | `integration/test_sandbox.py::test_no_secrets_in_container_env`, `::test_host_proxy_settings_do_not_reach_the_container` |
| No token is in the sandbox or in the checkpoint repo's config. | `integration/test_clone_push.py::test_no_token_in_sandbox_or_ckpt_config` |
| The container drops all capabilities, adds none, is not privileged, sets `no-new-privileges`, and has a read-only root filesystem. | `integration/test_sandbox_hardening.py::test_inspect_flags`, `integration/test_sandbox.py::test_hardening_flags` |
| The effective capability set, the `NoNewPrivs` bit and the uid are what the flags promise, read from inside. | `integration/test_sandbox_hardening.py::test_the_flags_hold_inside_the_container` |
| The agent runs as user `agent`, uid 10001, not root. | `integration/test_sandbox_hardening.py::test_inspect_flags`, `integration/test_sandbox.py::test_runs_as_non_root` |
| Pids, memory and CPU limits come from the `SandboxLimits` of the call. | `integration/test_sandbox_hardening.py::test_inspect_flags` |
| Nothing of the host is shared except the task's own volume, its checkpoint repo and two tmpfs folders. No Docker socket, no host namespaces. | `integration/test_sandbox_hardening.py::test_inspect_flags`, `integration/test_sandbox_mounts.py::test_a_mount_leaves_the_other_guards_in_place` |
| A mount that would cover a writable folder, overlap another mount or take the host root is refused before a container exists. | `integration/test_sandbox_mounts.py::test_a_bad_mount_is_refused_before_any_container_exists` |
| The network is on for the setup command only. Afterwards no network is attached and there is no route. | `integration/test_sandbox_hardening.py::test_inspect_flags`, `integration/test_sandbox.py::test_no_network_when_disabled`, `::test_network_toggle` |
| The workspace code cuts the network after setup, and removes the sandbox when it cannot. | `unit/test_workspace.py::test_prepare_runs_setup_with_the_network_on_and_then_cuts_it`, `::test_the_sandbox_is_cleaned_up_when_the_network_cannot_be_cut` |
| A command ends at its time limit, with what it left running. | `integration/test_sandbox.py::test_timeout_kills`, `::test_exec_kills_what_a_command_leaves_running`, `::test_a_timeout_also_kills_children_that_left_the_process_group` |
| A flood of output cannot grow the worker's memory. | `integration/test_sandbox.py::test_a_flood_of_output_ends_at_the_timeout_with_bounded_memory` |
| Orphaned processes cannot use up the pid limit. | `integration/test_sandbox.py::test_pid_1_is_an_init_that_reaps_orphans`, `::test_orphans_do_not_pile_up_against_the_pid_limit` |
| File tools and the file API stay inside the workspace, also through symlinks. | `unit/test_path_safety.py::test_every_tool_with_a_path_refuses_to_leave_the_workspace`, `integration/test_sandbox.py::test_read_file_is_confined_to_the_writable_roots`, `::test_write_file_does_not_follow_a_symlink_out_of_the_workspace` |

### GitHub and the default branch

| Control | Test |
|---|---|
| The push runs on the host, after the branch guard, and refuses the default branch and `main`, `master`, `develop` and `release/*` in any letter case (I5, F15). | `unit/test_branch_guard.py::test_refuses_default_branch`, `::test_refuses_protected_patterns`, `::test_push_branch_to_main_raises_and_nothing_is_pushed` |
| The default branch is read from the API, not from the repo the agent can write, and the guard fails closed. | `unit/test_branch_guard.py::test_a_default_branch_written_into_the_repo_config_is_not_believed`, `::test_the_guard_fails_closed_when_github_cannot_say_what_the_default_is` |
| A push is never forced. | `unit/test_push_branch.py::test_a_push_is_never_forced` |
| A URL rewrite or a hook in the agent's repo cannot redirect the push or run code on the host. | `unit/test_push_branch.py::test_a_url_rewrite_in_the_task_repo_config_cannot_redirect_the_push`, `::test_a_hook_in_the_task_repo_does_not_run` |
| The token is not left in the task repo and is not in the error of a failed push. | `unit/test_push_branch.py::test_nothing_of_the_token_is_left_in_the_task_repo`, `::test_the_error_of_a_failed_push_does_not_carry_the_header` |
| A checkpoint is pushed from the host, and the default branch is not touched. | `integration/test_clone_push.py::test_a_checkpoint_is_pushed_from_the_host_and_the_default_branch_is_not` |
| Every GitHub write goes through `github/side_effects.py` (I4). | `unit/test_invariants.py::test_github_writes_only_in_side_effects` |
| A retried write posts once, also after the database was lost. | `integration/test_github_side_effects.py::test_retry_after_5xx_no_duplicate`, `::test_comment_idempotent_after_db_loss`, `::test_pr_idempotent_by_head` |
| Tokens do not show in reprs, errors or logs of the GitHub client. | `unit/test_github_client.py::test_tokens_never_appear_in_reprs_errors_or_logs` |
| Text from the model cannot mention a person or close an issue, in the pull request, the final comment, the commit or the title. | `unit/test_pr_body.py::test_a_mention_in_the_summary_does_not_notify_anyone`, `::test_an_issue_reference_in_the_summary_cannot_close_another_issue`, `unit/test_final_comment.py::test_text_from_the_model_cannot_mention_a_person_or_close_an_issue`, `integration/test_deliver.py::test_the_title_and_the_commit_cannot_close_another_issue_or_notify_anyone` |

### Workflows and payloads

| Control | Test |
|---|---|
| Workflow modules import no I/O library, directly or through what they import (I1). | `unit/test_invariants.py::test_workflows_have_no_io_imports`, `::test_workflow_import_closure_has_no_io_imports` |
| No payload is over 256 KB, and an oversized one never reaches the history (I2). | `unit/test_invariants.py::test_payload_size_guard`, `integration/test_payload_guard_history.py::test_an_activity_input_over_the_limit_fails_the_workflow_and_is_never_written` |

### Secrets in logs, reports and API output

| Control | Test |
|---|---|
| `redact` removes `sk-`, GitHub (`ghp_`, `ghs_`, `ghu_`, `gho_`, `ghr_`, `github_pat_`), AWS key ids and PEM private keys. | `unit/test_redaction.py::test_patterns` |
| Logs are redacted last, in fields, exceptions and reprs. | `unit/test_logging.py::test_secrets_are_redacted_wherever_they_sit`, `::test_an_exception_message_is_redacted` |
| The trace of an unexpected error is redacted before it is stored in an event. | `unit/test_activities_events.py::test_the_trace_of_an_error_is_written_without_a_secret` |
| Run reports are redacted. | `unit/test_run_report.py::test_a_secret_in_a_hint_never_reaches_the_report` |
| The console API redacts turns, outputs, events and memory notes when it reads them. | `unit/test_api_redaction.py::test_every_string_of_a_nested_value_is_redacted`, `integration/test_api.py::test_outputs_redacted` |
| A secret-looking key in the YAML config is refused. | `unit/test_settings.py::test_secret_keys_rejected_in_yaml` |
| CI uses no secrets context. | `unit/test_ci_config.py::test_no_secrets_in_ci` |

### The console API

| Control | Test |
|---|---|
| Every `/api` route needs the bearer token, before the request body is read. | `integration/test_api_auth.py::test_requires_token`, `::test_requires_token_before_it_reads_the_request` |
| A server without a token refuses everyone, and does not start. | `integration/test_api_auth.py::test_a_server_with_no_token_set_refuses_everyone`, `unit/test_api_auth.py::test_a_deployment_without_a_token_does_not_start` |
| The token is compared in constant time, and a non-ASCII token does not raise. | `unit/test_api_auth.py::test_a_token_matches_only_itself`, `::test_a_token_with_a_non_ascii_character_does_not_raise` |
| `/metrics` needs the token unless the server is bound to this machine. `/healthz` is open. | `integration/test_api_auth.py::test_metrics_need_the_token_unless_the_server_is_local`, `::test_healthz_is_public` |
| An action token may send 30 actions a minute. A refused request does not use up the limit. | `integration/test_api_auth.py::test_rate_limit_actions`, `::test_a_refused_request_does_not_use_up_the_limit`, `unit/test_api_auth.py::test_the_limiter_allows_the_limit_and_then_says_how_long_to_wait` |

### Memory

| Control | Test |
|---|---|
| Prompts include only `approved` memory items (I7). | `unit/test_memory_retrieve.py::test_only_approved_in_prompt` |

### Supply chain

| Control | Test or check |
|---|---|
| Actions in CI are pinned to major versions and the Temporal image to a digest. | `unit/test_ci_config.py::test_actions_use_pinned_major_versions`, `::test_temporal_image_check_accepts_the_pinned_image` |
| Dependencies are locked, scanned by `pip-audit`, and the history is scanned by gitleaks. | The `security` job in `.github/workflows/ci.yml`. The results of the pass that wrote this page are below. |

### Controls without a test

| Control | How to check it |
|---|---|
| Every compose port is published on `127.0.0.1` only. | Read `docker-compose.yml`. The runbook explains the limit of loopback binding. |
| The API and the metrics endpoint default to `127.0.0.1`. | `EnvSettings.api_host` and `metrics_host` in `src/longhaul/settings.py`. |
| Branch protection on `main` and `dev`. | `scripts/protect_branches.sh`, and the settings of the repo on GitHub. It needs an admin with `gh`, so no test can run it. |
| The container user is `agent` by name, and the image fixes the uid at 10001. | `docker/sandbox/Dockerfile`. `test_inspect_flags` checks the name and `test_the_flags_hold_inside_the_container` checks the uid. |

## What the checks found

### Network mode

The plan asks for network mode `none` after setup. The manager cannot do that: Docker does not move a `none` container onto a network later, and setup needs the network. The container therefore starts on the bridge and the manager disconnects it when setup ends. After that `docker inspect` still shows `HostConfig.NetworkMode` as `bridge`, but `NetworkSettings.Networks` is empty and the container has no route. `test_inspect_flags` asserts the empty network list, and `test_no_network_when_disabled` asserts the empty route table. A sandbox that needs no setup is also created on the bridge and disconnected at once. The few milliseconds in between have no way out for a command, because the container runs `sleep` until the first exec.

### The user

`docker/sandbox/Dockerfile` creates the user `agent` with uid and gid 10001 and ends with `USER agent`. The manager passes `user="agent"` as well, so a different image cannot make the container run as root. The test checks the name in `Config.User` and the uid from inside.

### Redaction of GitHub App tokens (fixed)

ADR-0012 says the redaction filter must match `ghs_`, the prefix of installation tokens. It matched only `ghp_` and `github_pat_`. A worker that signs in as a GitHub App holds `ghs_` tokens, and a log line or a tool output that carried one would have been stored and served as it was. The pattern is now `gh[pousr]_[A-Za-z0-9]{30,}`, which also covers user, OAuth and refresh tokens. `unit/test_redaction.py::test_patterns` has a case for each prefix.

### gitleaks

`gitleaks git --log-opts="--all"` (version 8.30.1) over every branch and tag: 483 commits scanned, about 10 MB, **no leaks found**. The tests that need a secret-shaped string build it from parts, so the history has none. CI runs the same scan on each pull request.

### pip-audit

`uv export --frozen --all-extras --no-emit-project --no-hashes` gives 164 pinned packages, the dev group and the optional extras included. `uvx pip-audit -r <that file> --no-deps --disable-pip`: **No known vulnerabilities found.** CI runs the same audit.

### npm audit for the console

The console is not covered by the story's tools, so it was checked with `npm audit` on `console/package-lock.json`. The production dependencies (`react`, `react-dom`, `react-router-dom`, `@tanstack/react-query`) have **0 findings**. The full tree has 10 moderate findings, all one advisory: a denial of service in `minimatch` (GHSA-g3hv-75wg-m249), reached through `eslint` and `typescript-eslint`. These are development tools. They run on a developer's machine and in CI on files of this repo, they are not in the built console, and the only fix `npm audit` offers is a downgrade of `eslint` to 9. The finding is accepted until the `typescript-eslint` release that updates `minimatch`. Check again at the next dependency update.

## Known gaps

| Gap | Effect | Mitigation |
|---|---|---|
| **A shared Docker host.** Sandboxes are runc containers that share the host's kernel. The worker holds the Docker socket, which is root on the host. | A kernel or runtime escape from a sandbox, or a takeover of the worker, reaches the host and every other container on it. | Run the worker only on a machine given that trust, never on a host shared with other people. Keep the flags above. Use gVisor for repos you do not know. |
| **gVisor is optional.** `runsc` is documented, not required, and not tested here. | On the default runtime the kernel attack surface is the usual Docker one. | Install `runsc` and make it the default runtime of the Docker daemon (`daemon.json`) on a host that runs repos you do not know. The manager does not choose a runtime itself, the runbook has no gVisor section yet, and no test runs against a gVisor host. |
| **The rate limit is kept in one process.** | Two API processes each allow 30 actions a minute, and a restart clears the count. | Run one API process. A shared counter is post-v1. |
| **A bearer token over plain HTTP can be read on the path.** One token, no users, no audit of who acted. | A token on an open network is a full operator login. | Bind to loopback, or put the API behind TLS (runbook, "The console API"). |
| **Loopback ports can be reached from containers.** On Docker Desktop a container on the bridge reaches `host.docker.internal:<port>`. Temporal and MinIO have no real authentication, and the Postgres login is a superuser with the password `longhaul` by default. | A sandbox in its setup phase has the network and could reach these services. | Set a random `LH_POSTGRES_PASSWORD` for any run on a real repo (runbook, "Ports and the database password"). Setup runs repo build code, so use repos you trust. |
| **The network is open during setup.** `pip install` runs the repo's own build code with a route out. | Setup code can send out whatever the sandbox holds, which is the repo and a workspace volume, and can reach the loopback services above. | The sandbox holds no secret. An allowlisted package proxy is post-v1. |
| **The token is visible in the host's process list while git runs.** | Another user on the same host can read it for the length of a clone or push. | Single-user host, or the `GIT_CONFIG_*` variables of ADR-0012. |
| **Branch names other than the listed ones are not guarded by Longhaul.** | A repo that protects `prod` or `stable` relies on GitHub's own branch protection. | Turn on branch protection in the target repo. |
| **The agent can break its own checkpoints** in `/ckpt`. | It fails that task and no other. | One bare repo and one container per task. |
| **Stored blobs are not redacted, the output is.** | Redaction runs on reads and logs. A secret that the agent printed is in the blob store. | Redact on read stays. Treat the blob store as sensitive. |
| **Redaction matches shapes.** A password in a connection string is not caught. | Such a value in a log call would show. | Keep those out of log calls (architecture, "Redaction"). |
| **Local defaults are for one machine.** MinIO's root login is `longhaul` and `longhaul-secret` in `docker-compose.yml`. | The stack is not safe to expose. | Change them in any shared deployment. The AWS deploy uses Secrets Manager and an IAM user. |
| **Open items in the gate reports** (`docs/gates/`) | See below. | |

Open items from the gate reports that touch security or operations:

- Phase 0: `scripts/protect_branches.sh` and the backlog import need an authenticated `gh` and an admin. Until the script has run, the branch protection of `main` and `dev` is a setting nobody has applied.
- Phases 1 to 4: the live checks need an Anthropic key, so the providers were checked against scripted and recorded data and not against the real API.
- Phase 3: the smoke test on real GitHub (App token exchange, real push, real pull request) needs a token and has not run. The `AppAuth` tests use a mocked exchange.
- Phase 2: F4 (restore from the bundle on a host without the repo) is not wired into `ensure_sandbox`, and a transient failure of a cost ledger write loses that call's row. Neither is a leak. The second makes a spend figure low, not high.

## Rotating tokens

Rotate on a schedule, when a person who had a secret leaves, and at once when a secret may have shown up in a log, a screenshot, a paste or a commit. The order is the same each time: make the new secret, put it where the process reads it, restart that process, check it works, then revoke the old one. Revoke last, so there is no gap.

After any rotation, run `gitleaks git --log-opts="--all" --redact .` and search the run reports and logs of the period for the old value. A leaked secret is revoked even when it has expired in practice.

### GitHub token (personal access token)

1. On GitHub, create a fine-grained token for the allowlisted repos only, with read and write on contents, issues and pull requests (ADR-0012). Give it the shortest lifetime you will keep up with.
2. Put it in `LONGHAUL_GITHUB_TOKEN` in `.env` or in the shell that starts the worker.
3. Restart the worker (`docker compose --profile full up -d worker`, or stop and start `longhaul worker`). The worker reads the token at start. A task in flight resumes on the new worker, because Temporal retries the activity and the token is not in the history.
4. Check: the next poll in the Temporal UI finishes with no error, and the next labeled issue is picked up.
5. Delete the old token on GitHub.

A 401 "Bad credentials" is fatal and shows on the task. It is the sign that a token expired or was revoked before the worker had the new one.

### GitHub App key

1. In the App's settings, generate a new private key. GitHub allows more than one at a time.
2. Replace the file named by `LONGHAUL_GITHUB_APP_KEY_PATH`. The worker reads the file on each token exchange, so no restart is needed. Installation tokens last an hour, so the old key is out of use within the hour.
3. Check that a poll succeeds after the next exchange.
4. Delete the old key in the App's settings.

### Console admin token

1. `openssl rand -hex 32`.
2. Put it in `LONGHAUL_ADMIN_TOKEN` (`.env`, the shell, or the secret of the deployment) and restart the API: `docker compose --profile full up -d api`, or stop and start `longhaul api`. The restart also clears the rate limit counts.
3. The console gets a 401 on the next call and shows the login screen. Sign in with the new token. Scripts that call the API need the new value.
4. The old token stops working at the restart. There is no overlap, so do this when nobody is mid-action.

Keep the token out of shell history. Read it from a file or a secret manager and do not paste it on a command line.

### LLM API keys

1. In the provider's console, create a new key. Use a separate key for Longhaul, with a spending limit, so a leak has a ceiling.
2. Put it in `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`) and restart the worker.
3. Check with a short scripted-then-live run, or watch the next `cost_ledger` row appear.
4. Revoke the old key. Do it after the restart, because model calls made by the old worker in between would otherwise fail with an authentication error.

### Database, blob store and Temporal

- **Postgres.** Change the role's password in the database (`ALTER ROLE longhaul PASSWORD '...'`), then set the same password in `LH_POSTGRES_PASSWORD` and in `LONGHAUL_DATABASE_URL`, and restart the worker and the API. The compose file reads the variable only when the volume is first created, so the `ALTER ROLE` is what changes the live password.
- **MinIO and S3.** Change `LONGHAUL_BLOB_ACCESS_KEY` and `LONGHAUL_BLOB_SECRET_KEY` together, restart the worker and the API, then remove the old key from the store. On AWS, create a new access key for the blob IAM user, update the secret, and delete the old key.
- **Temporal Cloud.** Issue a new client certificate, store it in the two Secrets Manager secrets, restart the services, and revoke the old certificate in the namespace.

### On AWS

The values live in Secrets Manager (`docs/deploy-aws.md`). Write the new value with `aws secretsmanager put-secret-value --secret-id longhaul/<name> --secret-string "$VALUE"`, then force a new deployment of the service that reads it, because ECS passes a secret to a task when the task starts. Do not put a secret in `terraform.tfvars` or in a command line that a shell keeps.

### If a secret was committed

Revoke it first. Removing it from history does not make it safe, because clones and forks keep it. Then remove it from the branch, and rewrite history only if you must and everyone who cloned agrees. Add a case to `unit/test_redaction.py` if its shape was not one `redact` knows.

## How to repeat this pass

```bash
uv run pytest tests/integration/test_sandbox_hardening.py -q
gitleaks git --log-opts="--all" --redact .
uv export --frozen --all-extras --no-emit-project --no-hashes -o /tmp/audit.txt
uvx pip-audit -r /tmp/audit.txt --no-deps --disable-pip
(cd console && npm audit --package-lock-only)
```
