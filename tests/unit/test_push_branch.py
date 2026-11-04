"""push_branch: a branch goes from the task's bare repo to GitHub, from the host (LH-305).

The remote is a local bare repo behind FakeGitHub, so the push is the real git command against a
path. A spy on the subprocess call records the arguments of every git command, which is how
these tests check what a path remote cannot show: that the auth header is passed, that it comes
before the subcommand, and that no push is forced.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.calc_remote import REPO, TOKEN, Github, commit_on, github_for, seed_calc
from tests.fakes.fake_github import FakeGitHub, git

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints import git_store
from longhaul.checkpoints.git_store import CheckpointError, GitCheckpointStore, RepoNotFoundError
from longhaul.checkpoints.push import push_branch
from longhaul.github.client import PatAuth
from longhaul.github.remote import GitHubRemote

pytestmark = [pytest.mark.unit, pytest.mark.phase3]

TASK = "lh-acme-calc-42"
LOCAL = "longhaul-pr/42"
HEADER = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
# Options that would let a push replace or remove what the remote has.
FORCING = {"-f", "--force", "--force-with-lease", "--mirror", "--delete", "--prune"}


class Rig:
    def __init__(
        self,
        fake: FakeGitHub,
        github: Github,
        store: GitCheckpointStore,
        base: str,
        commands: list[tuple[Any, ...]],
    ) -> None:
        self.fake, self.github, self.store, self.base = fake, github, store, base
        self.commands = commands

    @property
    def repo(self) -> Path:
        return self.store.repo_path(TASK)

    def commit(self, branch: str = LOCAL, parent: str | None = None, message: str = "fix") -> str:
        return commit_on(self.repo, parent or self.base, branch, message)

    async def push(
        self, remote_branch: str, local: str = LOCAL, remote: GitHubRemote | None = None
    ) -> str:
        return await push_branch(
            TASK,
            local,
            remote_branch,
            repo=REPO,
            store=self.store,
            remote=remote or self.github.remote,
        )

    def pushes(self) -> list[list[str]]:
        """Every git command that pushed, as a list of words."""
        words = [[str(word) for word in args] for args in self.commands]
        return [found for found in words if "push" in found]


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """The arguments of every git command that the checkpoint code starts."""
    seen: list[tuple[Any, ...]] = []
    real = asyncio.create_subprocess_exec

    async def record(*args: Any, **kwargs: Any) -> Any:
        seen.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(git_store.asyncio, "create_subprocess_exec", record)
    return seen


@pytest.fixture
async def rig(tmp_path: Path, commands: list[tuple[Any, ...]]) -> AsyncIterator[Rig]:
    fake = FakeGitHub()
    base = seed_calc(fake, tmp_path)
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "ckpt")
    await store.init_task(TASK, fake.clone_url(REPO), base)
    github = github_for(fake)
    try:
        yield Rig(fake, github, store, base, commands)
    finally:
        await github.aclose()


async def test_a_pushed_branch_appears_in_the_remote(rig: Rig) -> None:
    sha = rig.commit()

    pushed = await rig.push("longhaul-pr/42")

    assert pushed == sha
    assert rig.fake.branches(REPO)["longhaul-pr/42"] == sha
    assert rig.fake.branches(REPO)["main"] == rig.base, "the default branch did not move"


async def test_the_local_ref_may_be_a_branch_name_or_a_full_ref(rig: Rig) -> None:
    sha = rig.commit()

    assert await rig.push("one", local=LOCAL) == sha
    assert await rig.push("two", local=f"refs/heads/{LOCAL}") == sha

    assert {"one", "two"} <= set(rig.fake.branches(REPO))


async def test_pushing_the_same_commit_again_is_a_no_op(rig: Rig) -> None:
    sha = rig.commit()
    await rig.push("longhaul-pr/42")
    before = rig.fake.refs(REPO)

    assert await rig.push("longhaul-pr/42") == sha

    assert rig.fake.refs(REPO) == before


async def test_a_new_commit_on_the_same_branch_is_pushed_as_a_fast_forward(rig: Rig) -> None:
    first = rig.commit()
    await rig.push("longhaul-pr/42")
    second = rig.commit(parent=first, message="second")

    await rig.push("longhaul-pr/42")

    assert rig.fake.branches(REPO)["longhaul-pr/42"] == second


async def test_a_push_is_never_forced(rig: Rig) -> None:
    first = rig.commit()
    await rig.push("longhaul-pr/42")
    rig.commit(message="rebuilt from the base, so it is not a child of the first")

    with pytest.raises(CheckpointError, match="push"):
        await rig.push("longhaul-pr/42")

    assert rig.fake.branches(REPO)["longhaul-pr/42"] == first
    pushes = rig.pushes()
    assert len(pushes) == 2
    for words in pushes:
        after = words[words.index("push") :]
        assert not FORCING & set(after)
        assert not any(word.startswith(("+", ":")) for word in after)


async def test_the_auth_header_goes_before_the_push_and_nowhere_else(rig: Rig) -> None:
    rig.commit()

    await rig.push("longhaul-pr/42")

    (words,) = rig.pushes()
    header = f"http.https://github.com/.extraheader={HEADER}"
    assert header in words[: words.index("push")]
    assert not any(TOKEN in word for word in words if word != header)


async def test_nothing_of_the_token_is_left_in_the_task_repo(rig: Rig) -> None:
    rig.commit()
    await rig.push("longhaul-pr/42")

    leaked = [
        path
        for path in rig.repo.rglob("*")
        if path.is_file()
        and (TOKEN.encode() in path.read_bytes() or HEADER.encode() in path.read_bytes())
    ]
    assert leaked == []


async def test_the_error_of_a_failed_push_does_not_carry_the_header(rig: Rig) -> None:
    rig.commit()
    gone = GitHubRemote(
        rig.github.client, PatAuth(TOKEN), clone_url=lambda repo: "/no/such/remote.git"
    )

    with pytest.raises(CheckpointError) as caught:
        await rig.push("longhaul-pr/42", remote=gone)

    assert TOKEN not in str(caught.value)
    assert HEADER not in str(caught.value)


async def test_a_url_rewrite_in_the_task_repo_config_cannot_redirect_the_push(
    rig: Rig, tmp_path: Path
) -> None:
    # The agent can write the config of /ckpt. A rewrite there would send the push, and with it
    # the header, to a repo of its choice, so git must not read that config.
    other = tmp_path / "other.git"
    git("init", "-q", "--bare", str(other))
    with rig.repo.joinpath("config").open("a") as config:
        config.write(f'[url "{other}"]\n\tinsteadOf = {rig.fake.clone_url(REPO)}\n')
    sha = rig.commit()

    await rig.push("longhaul-pr/42")

    assert rig.fake.branches(REPO)["longhaul-pr/42"] == sha
    assert git("for-each-ref", "--format=%(refname)", cwd=other) == ""


async def test_a_hook_in_the_task_repo_does_not_run(rig: Rig, tmp_path: Path) -> None:
    rig.commit()  # first: the helper runs git without the host's switches, and would fire a hook
    mark = tmp_path / "hook-ran"
    hooks = rig.repo / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("pre-push", "reference-transaction", "post-checkout"):
        (hooks / name).write_text(f"#!/bin/sh\ntouch {mark}\n")
        (hooks / name).chmod(0o755)

    await rig.push("longhaul-pr/42")

    assert not mark.exists()


async def test_the_scratch_repo_of_a_push_is_removed(rig: Rig) -> None:
    rig.commit()

    await rig.push("longhaul-pr/42")

    assert list(rig.store.root.glob(".*.tmp")) == []


@pytest.mark.parametrize(
    "name", ["", "-x", "a..b", "a b", "a//b", "a/", "x.lock", "a/.b", "a~1", "a:b", "a*", "@"]
)
async def test_a_remote_branch_name_that_git_would_reject_is_refused_before_any_push(
    rig: Rig, name: str
) -> None:
    rig.commit()

    with pytest.raises(ValueError, match="branch"):
        await rig.push(name)

    assert rig.pushes() == []


@pytest.mark.parametrize("local", ["", "-x", "HEAD~1", "a..b", "no-such-branch", "refs/tags/base"])
async def test_a_local_ref_that_is_not_a_branch_of_the_task_repo_is_refused(
    rig: Rig, local: str
) -> None:
    rig.commit()

    with pytest.raises((ValueError, CheckpointError)):
        await rig.push("longhaul-pr/42", local=local)

    assert rig.pushes() == []
    assert "longhaul-pr/42" not in rig.fake.branches(REPO)


async def test_a_task_without_a_repo_is_a_named_error(rig: Rig) -> None:
    with pytest.raises(RepoNotFoundError):
        await push_branch(
            "lh-other-repo-9",
            LOCAL,
            "longhaul-pr/9",
            repo=REPO,
            store=rig.store,
            remote=rig.github.remote,
        )


async def test_a_task_id_that_could_leave_the_root_is_refused(rig: Rig) -> None:
    with pytest.raises(ValueError, match="task id"):
        await push_branch(
            "../evil", LOCAL, "x", repo=REPO, store=rig.store, remote=rig.github.remote
        )
