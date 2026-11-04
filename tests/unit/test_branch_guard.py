"""The branch guard: no push to the default branch, ever (LH-305, failure mode F15, invariant I5).

The pure checks come first. The last tests run push_branch against a local bare remote and
check that a refused push leaves the remote as it was: the guard has to stop the push, not
only say no afterwards.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from longhaul.checkpoints.push import ForbiddenBranchError, assert_not_default, push_branch
from tests.fakes.calc_remote import REPO, Github, commit_on, github_for, seed_calc
from tests.fakes.fake_github import FakeGitHub

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.github.client import GitHubRetryable

pytestmark = [pytest.mark.unit, pytest.mark.phase3]

TASK = "lh-acme-calc-42"
PR_BRANCH = "longhaul-pr/42"


# ---------- the check itself ----------
def test_refuses_default_branch() -> None:
    with pytest.raises(ForbiddenBranchError, match="main"):
        assert_not_default("main", "main")

    # A default branch with any other name is protected just the same, whatever its spelling.
    for spelling in ("trunk", "refs/heads/trunk", "TRUNK", " trunk"):
        with pytest.raises(ForbiddenBranchError, match="(?i)trunk"):
            assert_not_default(spelling, "trunk")


def test_the_error_names_the_branch_and_the_rule() -> None:
    with pytest.raises(ForbiddenBranchError) as caught:
        assert_not_default("trunk", "trunk")
    assert "trunk" in str(caught.value)
    assert "default" in str(caught.value)

    with pytest.raises(ForbiddenBranchError) as caught:
        assert_not_default("release/1.0", "trunk")
    assert "release/1.0" in str(caught.value)
    assert "protected" in str(caught.value)


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "master",
        "develop",
        "release/1.0",
        "release/2026/q1",
        # The same names in other spellings. GitHub treats them as different branches, but a
        # person reading the list of branches would not.
        "Main",
        "MASTER",
        "Develop",
        "Release/1.0",
        "refs/heads/main",
        "refs/heads/release/1.0",
        "  main",
        "main  ",
    ],
)
def test_refuses_protected_patterns(branch: str) -> None:
    # The default branch is something else, so only the patterns can be the reason.
    with pytest.raises(ForbiddenBranchError):
        assert_not_default(branch, "trunk")


@pytest.mark.parametrize(
    "branch",
    [
        "longhaul-pr/42",
        "longhaul/42",
        "mainline",
        "domain",
        "feature/main",
        "hotfix/release/1.0",
        "release",
        "releases/1.0",
        "develop-2",
        "maintenance",
    ],
)
def test_lets_other_branches_through(branch: str) -> None:
    assert_not_default(branch, "main")


# ---------- push_branch stops before any push ----------
class Rig:
    def __init__(self, fake: FakeGitHub, github: Github, store: GitCheckpointStore) -> None:
        self.fake, self.github, self.store = fake, github, store

    async def push(self, remote_branch: str) -> str:
        return await push_branch(
            TASK,
            PR_BRANCH,
            remote_branch,
            repo=REPO,
            store=self.store,
            remote=self.github.remote,
        )


async def build_rig(tmp_path: Path, default_branch: str) -> Rig:
    fake = FakeGitHub()
    base = seed_calc(fake, tmp_path, default_branch=default_branch)
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "ckpt")
    await store.init_task(TASK, fake.clone_url(REPO), base)
    commit_on(store.repo_path(TASK), base, PR_BRANCH)
    return Rig(fake, github_for(fake), store)


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    built = await build_rig(tmp_path, "main")
    try:
        yield built
    finally:
        await built.github.aclose()


@pytest.fixture
async def trunk_rig(tmp_path: Path) -> AsyncIterator[Rig]:
    built = await build_rig(tmp_path, "trunk")
    try:
        yield built
    finally:
        await built.github.aclose()


async def test_push_branch_to_main_raises_and_nothing_is_pushed(rig: Rig) -> None:
    before = rig.fake.refs(REPO)

    with pytest.raises(ForbiddenBranchError):
        await rig.push("main")

    assert rig.fake.refs(REPO) == before


@pytest.mark.parametrize("branch", ["master", "develop", "release/1.0", "refs/heads/main", "Main"])
async def test_push_branch_to_a_protected_name_raises_and_nothing_is_pushed(
    rig: Rig, branch: str
) -> None:
    before = rig.fake.refs(REPO)

    with pytest.raises(ForbiddenBranchError):
        await rig.push(branch)

    assert rig.fake.refs(REPO) == before


async def test_the_default_branch_is_the_one_github_reports_and_not_the_name_main(
    trunk_rig: Rig,
) -> None:
    before = trunk_rig.fake.refs(REPO)

    with pytest.raises(ForbiddenBranchError, match="trunk"):
        await trunk_rig.push("trunk")

    assert trunk_rig.fake.refs(REPO) == before


async def test_a_default_branch_written_into_the_repo_config_is_not_believed(rig: Rig) -> None:
    # The agent can edit the config of /ckpt, so the host takes the default branch from GitHub.
    # Here the config names another branch as the default one, and main is still refused.
    with rig.store.repo_path(TASK).joinpath("config").open("a") as config:
        config.write('[longhaul]\n\tdefaultBranch = "elsewhere"\n')
    before = rig.fake.refs(REPO)

    with pytest.raises(ForbiddenBranchError):
        await rig.push("main")

    assert rig.fake.refs(REPO) == before


async def test_the_guard_fails_closed_when_github_cannot_say_what_the_default_is(
    rig: Rig,
) -> None:
    before = rig.fake.refs(REPO)
    rig.fake.fail_next(r"/repos/acme/calc$", 502)

    with pytest.raises(GitHubRetryable):
        await rig.push(PR_BRANCH)

    assert rig.fake.refs(REPO) == before
