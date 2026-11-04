"""Push a branch of a task's bare repo to GitHub, from the host (LH-305).

Only the worker pushes, and only after the branch guard has looked at the target. The sandbox
holds no token and has no network once its setup is done, so the agent cannot push at all
(ADR-0009). The worst a hostile agent can do is leave a bad commit in /ckpt, and a person reviews
what reaches a pull request.

Three rules shape this module:

* **The default branch never moves.** `assert_not_default` refuses the repo's default branch and
  any of main, master, develop and release/*, in any letter case (failure mode F15, invariant
  I5). The default branch is read from the API on each push. The checkpoint repo cannot be
  trusted to say it, because the agent can rewrite its config.
* **A push is never forced.** The refspec has no `+` and git gets no force option, so a branch
  that moved on the remote makes the push fail instead of replacing the remote's commits. The
  same commit pushed twice is a no-op, which is what makes a retried push activity safe.
* **Git does not read the agent's repo config.** The push runs from an empty scratch repo that
  borrows the task repo's objects through `objects/info/alternates`. A `url.<base>.insteadOf`
  or `pushurl` in /ckpt/config could otherwise send the push, and the header with it, to
  another repo that the token can write to.

Recording the refusal as an event, and turning it into a non-retryable activity failure, is up
to the activity that calls push_branch.
"""

import asyncio
import re

from longhaul.checkpoints.git_store import (
    PUSH_TIMEOUT_S,
    GitCheckpointStore,
    RepoNotFoundError,
    check_clone_url,
    check_task_id,
    git,
)
from longhaul.github.remote import GitHubRemote

__all__ = ["PROTECTED_BRANCHES", "ForbiddenBranchError", "assert_not_default", "push_branch"]

HEADS = "refs/heads/"
# Names that are never a push target, whatever the repo's default branch is called.
PROTECTED_BRANCHES = re.compile(r"main|master|develop|release/.*")
# A branch name that git takes and that cannot be read as an option or a path trick. Narrower
# than git's own rules on purpose: ASCII only, so a look-alike of "main" cannot slip through.
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


class ForbiddenBranchError(Exception):
    """A push to the default branch or to a protected name was asked for (F15, I5).

    No retry can change the answer, so the activity that sees it must not be retried.
    """


def _name(branch: str) -> str:
    """A branch as the guard and the refspec both see it: no spaces, no `refs/heads/` prefix."""
    return branch.strip().removeprefix(HEADS)


def assert_not_default(branch: str, default_branch: str) -> None:
    """Raise ForbiddenBranchError unless `branch` is a safe place to push.

    Names compare in lower case. GitHub treats Main and main as two branches, but a person who
    reads the branch list would not, and nothing is lost by refusing both.
    """
    name = _name(branch).lower()
    default = _name(default_branch).lower()
    if default and name == default:
        raise ForbiddenBranchError(
            f"refusing to push to {name}: it is the default branch of the repo"
        )
    if PROTECTED_BRANCHES.fullmatch(name):
        raise ForbiddenBranchError(
            f"refusing to push to {name}: main, master, develop and release/* are protected"
        )


def check_branch_name(name: str) -> str:
    """Return name if it is a branch name that git takes, else raise ValueError."""
    parts = name.split("/")
    if (
        not _BRANCH.fullmatch(name)
        or ".." in name
        or name.endswith(".")
        or any(not part or part.startswith(".") or part.endswith(".lock") for part in parts)
    ):
        raise ValueError(
            f"{name[:80]!r} is not a branch name git accepts: use letters, digits, '.', '_', "
            "'-' and '/', with no empty or dotted parts"
        )
    return name


async def push_branch(
    task_id: str,
    local_ref: str,
    remote_branch: str,
    *,
    repo: str,
    store: GitCheckpointStore,
    remote: GitHubRemote,
) -> str:
    """Push branch `local_ref` of the task's bare repo to `remote_branch` of `repo`.

    It returns the sha that was pushed. `repo` is `owner/name` and comes from the task, never
    from the checkpoint repo. Raises ForbiddenBranchError before any push when the target is
    the default branch or a protected name, ValueError for a name git would not take, and
    CheckpointError when git refuses the push, for example because the remote branch moved.
    """
    check_task_id(task_id)
    target = _name(remote_branch)
    assert_not_default(target, await remote.default_branch(repo))
    check_branch_name(target)
    source = check_branch_name(_name(local_ref))
    url = check_clone_url(remote.clone_url(repo))
    if not store.has_repo(task_id):
        raise RepoNotFoundError(f"task {task_id} has no checkpoint repo under {store.root}")
    task_repo = store.repo_path(task_id)
    sha = await git("rev-parse", "--verify", f"{HEADS}{source}^{{commit}}", git_dir=task_repo)
    config = await remote.auth_config()
    async with store.scratch(task_id, "push") as scratch:
        await git("init", "--bare", "--quiet", "--template=", str(scratch))
        alternates = scratch / "objects" / "info" / "alternates"
        await asyncio.to_thread(alternates.write_text, f"{task_repo / 'objects'}\n")
        # `sha:ref` and no `+`: a branch that is not a descendant of the remote's is refused.
        await git(
            "push", "--quiet", url, f"{sha}:{HEADS}{target}",
            git_dir=scratch, timeout_s=PUSH_TIMEOUT_S, config=config,
        )  # fmt: skip
    return sha
