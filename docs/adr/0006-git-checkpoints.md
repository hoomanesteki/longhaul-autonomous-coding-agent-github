# ADR-0006: Git checkpoints

- Status: Accepted
- Date: 2026-10-31

## Context

A retried tool call must never run on a workspace that an earlier attempt half changed. The workflow retries `tool_exec` after a worker crash (failure mode F1). A sandbox container can be removed (F3), and the whole worker host can be lost (F4). Each case needs the same thing: a copy of the workspace outside the container, at a known commit, that can be put back exactly.

The sandbox shapes the design (ADR-0009):

- The root filesystem is read-only. `/workspace` is a Docker volume, and the agent runs as uid 10001.
- No token or key ever enters the container, and the network is cut after setup.
- `run_command` runs arbitrary shell, so the agent can change any file it can write, `.git` included.

Two facts about Docker bind mounts decide the permission design:

- On Docker Desktop for Mac the file sharing layer maps ownership. A file that uid 10001 creates in a mounted folder shows up on the host as owned by the Mac user. This was measured here: uid 10001 could write a folder created by the host user, with no change to its mode.
- On Linux a bind mount keeps real uids. Uid 10001 is "other" to a folder owned by the worker user, and the files it creates are owned by 10001, not by the worker.

## Decision

**One bare repo per task, on the worker host.** The store keeps it at `<root>/<task_id>.git`. The root is the constructor argument `root`, else `LONGHAUL_CKPT_DIR`, else `~/.longhaul/ckpt`. `init_task` clones the source with `git clone --bare --no-hardlinks` into a scratch folder and renames it into place, so a repo is never visible half built, and a retried `init_task` finds the finished repo and changes nothing. The branch `longhaul/<issue>` starts at `base_sha`. The issue is the trailing number of the task id (`lh-acme-calc-42` gives `longhaul/42`), or the whole task id when there is none. The tag `base` marks the same commit, and `HEAD` points at the branch. `init_task` returns the repo path, which is what to mount.

**The repo holds no credential and no trace of its source.** `clone_url` is refused when it carries credentials or names a transport helper, and the error never repeats it. The clone's `remote.origin` is removed, because the sandbox can read the config file. Steps that talk to GitHub name the URL and the auth header on the command line. Objects are copied, never hard linked, so the sandbox cannot change files of the source repo.

**The sandbox gets the repo at `/ckpt`, read-write.** `DockerSandboxManager.create` takes an optional `mounts` argument (host folder to container path). It is not part of the `SandboxManager` protocol and `contracts.py` does not change (see the deferred item in `docs/architecture.md`). Missing folders, files and paths that cover the sandbox's own mounts are refused before a container exists, because Docker would create a missing source as root.

**Inside the sandbox, three operations do the work.** All of them read the branch from `/ckpt` itself (its `HEAD`), so the store keeps no state and survives a worker restart.

- `attach` makes `/workspace` a clone of `/ckpt` on the task branch, tags `base`, and adds `.venv`, caches and build debris to `.git/info/exclude`, so a repo whose own `.gitignore` misses them never commits them. On a workspace that already has work (the volume outlived the container) it leaves the work alone.
- `commit` runs `git add -A`. A clean tree returns `None` and creates nothing. Otherwise it commits and pushes to the branch with a forced update, because a retry moves the head back to a commit before the one it replaces. If the push fails, the local commit is taken back, so the changes stay pending.
- `restore` fetches from `/ckpt`, runs `git reset --hard <sha>`, and `git clean -ffdx -e .venv`. The second `f` also removes nested repositories. The folders in `keep` (default `.venv`) survive, everything else that is not in the commit is deleted, ignored files included. A restore also repairs what the agent can break: a deleted or corrupt `.git`, a stale lock from a killed command, a checked-out side branch. A sha that no longer sits on the branch is fetched by name (`uploadpack.allowAnySHA1InWant`).

**Bundles carry a task to another host.** `bundle` writes the full history up to the branch head, `HEAD` and the `base` tag, and stores it as `tasks/<id>/ckpt/<sha>.bundle`. `restore_from_bundle` builds the bare repo again from the stored blob, whose hash is checked on download. A plain `git clone` of the bundle works too. A bundle larger than 256 MiB is refused, because the blob store takes bytes and the worker would hold them in memory.

**Host path and permissions.** The root is created with mode 0700, so only the worker user can reach any repo on the host. An existing root keeps the mode its owner gave it. Inside a repo, every file and folder is opened to all users (`a+rwX`) and `core.sharedRepository` is `0666`, so files and folders that either side adds later stay open to the other.

- On Docker Desktop for Mac this works as it is: the integration tests push as uid 10001 into a repo created by the Mac user.
- On Linux the open mode is what lets uid 10001 write, and lets the worker read, bundle and delete what uid 10001 created. This was checked with a uid 1000 container as the worker and a uid 10001 container as the sandbox, sharing a volume on a Linux kernel. CI runs the integration tests on a Linux runner.
- The container reaches the repo through its bind mount, which does not need the parent folders to be traversable, so the private root costs nothing.
- Rootless Docker and user namespace remapping map uid 10001 to another host uid. That uid is "other" to the worker too, so the open mode should work unchanged. This was not tested.

A host that wants less than that has two options. Both replace the open mode:

- Run the worker as uid 10001, or `chown -R 10001:10001` the repos (needs root).
- Use a shared group. Create the root with `install -d -m 2770 -g 10001`, add the worker user to group 10001, and set `core.sharedRepository` to `group`. A user that cannot join a group cannot use this, which is the case on a hosted CI runner.

`attach` tests that uid 10001 can write to `/ckpt/objects` and to the folder that holds the branch ref, and stops with a message that points here when it cannot. SELinux hosts also need a relabel on the mount, which the store does not do.

**The host treats the repo as untrusted input.** The agent can rewrite `config`, `hooks` and the refs in `/ckpt`. Host-side git therefore runs with `core.hooksPath` set to the null device, `core.fsmonitor=false`, `gc.auto=0` and `protocol.ext.allow=never`, and only for `clone`, `update-ref`, `symbolic-ref`, `config`, `rev-parse` and `bundle create`. A test plants a hook and a hooks path in the repo and checks that none of them runs. The sandbox side runs git with the same switches, an identity and `safe.directory` set on the command line, and it puts the system folders first on `PATH`, because the workspace venv leads `PATH` and the agent can write there. The agent can still damage its own checkpoints, for example by rewriting `HEAD` in `/ckpt`. That fails its own task and reaches no other task, because each task has its own repo.

**Records.** The `checkpoints` table (migration 0002) has one row per saved state: the sha, the kind (`tool`, `step`, `compaction`, `bundle`), the turn, an optional plan step, and an optional `bundle_ref` that holds the whole `BlobRef` as JSON, so a reader can verify the bundle it downloads. LH-206 writes the rows.

The plan summary of this ADR said "a Docker volume". It is a host folder here, because the worker runs `git bundle` and, later, `git push` on it, and a folder can be inspected, backed up and deleted with ordinary tools.

## Consequences

**Easier**

- A retried tool call restores `pre_sha` first and starts from an exact tree. The mutation check at the phase gate can break `restore` and the tests will notice.
- A removed container, or a removed volume as well, comes back at the last checkpoint. A lost host comes back from the latest bundle.
- `git_diff` keeps working. It compares with the `base` tag, which `attach` fetches into the workspace.
- No token is in the sandbox or in `/ckpt/config`.

**Harder**

- Every checkpoint is a few git processes in the sandbox plus a push. That is small next to a model turn, which takes seconds, but it is paid on every mutating tool call.
- A restore deletes untracked and ignored files, except the folders in `keep`. A `node_modules` folder is lost unless the story that installs it adds it to `keep`.
- A bundle holds the whole history, not just the task's commits. Large repos hit the cap, and an incremental bundle on top of a fresh clone is the way out.
- A retried commit replaces the branch head. The replaced commit stays in the repo and can still be restored by sha, but it is no longer on the branch.
- Auto gc is off, so a repo only grows. `cleanup_task` removes it when the task ends, and the leftovers of a crashed `init_task` are removed by the next `cleanup_task` of that task.
- The repo is world writable inside a private root. A second user on the same host cannot reach it, but root and anyone who gets the worker's account can.

**To watch**

- LH-305 pushes with a token from the host. It must not trust this repo's config for that: push from a host-private repo, or pass the URL and the auth header on the command line with the same git switches. The sandbox can edit `config` here.
- If the worker runs in a container, the root must be a bind mount that has the same path on the host, because the Docker daemon resolves mount sources on the host.
- Moving the repo into a volume or a network share later changes the permission story above. Redo the Linux check then.
- If the shared writable repo ever becomes a problem, the fallback is to pull each change out of the container as an archive and apply it on the host, with no writable mount.

## Alternatives

- **A Docker volume for the bare repo, as the plan summary said.** The host cannot run `git bundle` or `git push` on a volume without a helper container, and a volume is harder to inspect and clean. A host folder gives the same isolation from the container's root filesystem.
- **Commit inside `/workspace` only.** It dies with the volume, so F3 with a lost volume and F4 cannot be recovered.
- **Copy each change out through the Docker archive API and apply it on the host.** It needs no writable mount and removes the trust question. It also needs exact handling of deletions, modes and renames for every call. Git's own push does that already. Kept as the fallback.
- **Snapshot the volume or commit the container.** Slow and large, and it gives no sha to record and nothing to diff.
- **A shared group or `chown` as the default.** Both need root or a group that a hosted runner cannot join, and they stop the integration tests from running in CI. They stay as options for hosts that want a tighter mode.
- **`git clone --shared` or alternates.** The sandbox could then change objects that belong to the source repo.
- **Rewrite the branch only with fast-forward pushes.** A retry restores an earlier commit, so its next commit is not a descendant of the stored head. Refusing it would turn every retry after a lost result into an error.
