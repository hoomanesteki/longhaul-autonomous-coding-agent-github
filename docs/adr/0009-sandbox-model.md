# ADR-0009: Sandbox model

- Status: Accepted
- Date: 2026-11-04

## Context

Longhaul runs code it did not write and takes instructions from text it does not control. An issue body, a comment and every file of the target repo can carry instructions to the agent. The agent has a shell (`run_command`), so anything that persuades it can run any command the sandbox allows. The worker, in the same moment, holds the two most useful secrets in the system: the GitHub token, which can push code (ADR-0012), and the LLM key, which spends money.

Four facts decide the model:

- **The setup command needs the network.** `pip install` has to reach a package index. After setup the agent needs no network at all: it reads files, edits them and runs tests.
- **A push is the dangerous step.** A branch on GitHub is visible to everyone with read access, and a push can start workflows that run with the repo's secrets. A review happens at the pull request, after the push. So the target of a push has to be right before it happens, and only trusted code may choose it.
- **The checkpoint repo is shared with the agent.** The sandbox mounts the task's bare repo at `/ckpt` read-write (ADR-0006), so the agent can rewrite its config, hooks and refs.
- **Git needs the credential.** Cloning a private repo and pushing a branch both authenticate. The token has to be somewhere git can read it, and that must not be a place the agent can read.

## Decision

**One container per task, with no secret in it and no way out after setup.**

- The container runs as user `agent` (uid 10001) with `cap_drop=["ALL"]`, `no-new-privileges`, and limits on pids, memory and CPU. Its root filesystem is read-only. `/workspace` is a per-task volume, `/tmp` and the agent's home folder are tmpfs mounts, and `/ckpt` is the task's bare repo.
- The network is on for the setup command only. The manager creates the container on the bridge network and disconnects it when setup ends, so a command that the agent runs later has no route out.
- The environment comes from an allowlist of five fixed names (`PATH`, `HOME`, `LANG`, `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`), never from the worker's own environment (invariant I3). No GitHub token, no LLM key and no Docker proxy setting is in it.

**The clone happens on the host.** `prepare_workspace` asks the REST API for the default branch of the repo and the commit it holds (`base_sha`). It then runs `git clone --bare` as a process of the worker, with the token as a setting of that one process (`git_auth_header`: `-c http.https://github.com/.extraheader=...`, placed before the subcommand). The result is the task's bare repo, which is then mounted into the sandbox. The clone is the only step that needs the token, and it ends before the container exists. The sandbox gets a repo and no way to fetch another.

- The token never appears in a URL, in the repo's config (the clone's `remote.origin` section is removed), in a file, or in an error message. git's own output is scrubbed of it before it reaches an exception, because exception text is stored in Temporal's history.
- The default branch and `base_sha` come from the API, not from the clone. The branch `longhaul/<n>` is put on `base_sha` and the tag `base` marks it, so a default branch that moves while a task runs does not change what the task started from.
- The header is passed on the command line, so it shows in the host's process list while git runs (ADR-0012 describes the trade and the alternative for shared hosts).

**The push happens on the host.** `push_branch(task_id, local_ref, remote_branch)` runs on the worker, after the branch guard, and signs in the same way.

- `assert_not_default` refuses the repo's default branch and any of `main`, `master`, `develop` and `release/*`, in any letter case, before git starts (failure mode F15, invariant I5). The default branch is read from the API on each push, because the checkpoint repo cannot be trusted to name it.
- The push is never forced. The refspec carries a commit id and no `+`, so a remote branch that moved makes the push fail. Pushing the same commit again changes nothing, which makes a retried push activity safe.
- Git does not read the agent's repo config. The push runs from an empty scratch repo on the host that borrows the task repo's objects through `objects/info/alternates`. Without this, a `url.<base>.insteadOf` setting in `/ckpt/config` could send the push, and the header with it, to another repo that the token can write to.

**What the agent can still do.** It can leave any commit it likes on the task branch in `/ckpt`, and it can break its own checkpoints. It cannot reach a credential, cannot talk to GitHub, and cannot choose where a push goes. The worst case is a bad diff in a pull request that a person reviews. Because each task has its own repo and its own container, a damaged checkpoint fails that task and no other.

## Consequences

**Easier**

- The sandbox needs no secret management. A leak from inside it, by an agent that was talked into printing its environment, gives away nothing, and tests can check that (`tests/integration/test_clone_push.py::test_no_token_in_sandbox_or_ckpt_config`).
- One code path serves local folders and GitHub repos after the bare repo exists. Recovery (`ensure_sandbox`) reads only that repo, so a worker without GitHub access can still bring a sandbox back.
- The rule against pushing to the default branch is in one function with one test file, and it fails closed: if the API cannot say what the default branch is, nothing is pushed.

**Harder**

- The worker needs git and network access to GitHub, and a clone of a large repo runs on the host with the activity's 15 minute limit and heartbeats. A shallow or partial clone is the way out if that limit becomes a problem.
- Anything that the agent might want from the network after setup (a documentation page, a second package index) is not available. An allowlisted package proxy is a post-v1 item in the plan.
- A task that pushes needs a token with write access, and that token sits in the worker's environment. A GitHub App with short-lived installation tokens (ADR-0012) limits how long a leaked token is useful.
- Pushing runs git against a repo the agent can write to. It is done with hooks switched off and from a scratch repo, and `tests/unit/test_push_branch.py` plants a hook and a URL rewrite to check that neither takes effect. Each new host-side git command on that repo needs the same care.

**To watch**

- A repo whose default branch has an unusual name is covered by the API lookup. A repo that protects branches by another convention (`prod`, `stable`) is not covered by the name patterns, and relies on GitHub's own branch protection. Say so in the runbook when the first such repo is added.
- gVisor (`runsc`) is an optional stronger runtime for hosts that run repos they do not know. It changes nothing above.
- If a second sandbox manager appears, it must keep the three properties: no secret in the environment, no network after setup, and no credential reachable from a mount.

## Alternatives

- **Clone inside the sandbox with a token.** Rejected: the token would be in the container's environment or on its disk, and the agent can read both. A deploy key or a credential helper has the same problem.
- **Mount the token as a file that only git can read.** Rejected: the agent runs as the same user as git, so it can read the file too.
- **Run an authenticating proxy that the sandbox calls.** Rejected for v1: it is a service to build and run, and it moves the problem to the proxy's rules. The host-side clone and push give the same result with git alone.
- **Let the sandbox push through the host's git with a one-off credential.** Rejected: the credential would still enter the container for as long as the push takes, and a fast agent can use that window.
- **Keep the network on for the whole task.** Rejected: prompt injection from the repo or the issue could then send files anywhere. With the network cut, the worst case is a bad diff.
- **Branch protection on GitHub alone.** Kept as a second layer, not a replacement. It is the owner's setting and can be missing, and the guard has to hold on a repo that has none.
- **Read the default branch from the checkpoint repo.** Rejected: the agent can rewrite that file, so it could name an unprotected branch as the default and push over the real one.
