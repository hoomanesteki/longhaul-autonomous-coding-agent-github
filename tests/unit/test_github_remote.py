"""GitHubRemote: where the host clones from and pushes to, and how it signs in (LH-305)."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.calc_remote import REPO, TOKEN, Github, github_for, seed_calc
from tests.fakes.fake_github import FakeGitHub

from longhaul.github.client import GitHubFatal, GitHubRetryable, PatAuth
from longhaul.github.remote import GitHubRemote, github_clone_url

pytestmark = [pytest.mark.unit, pytest.mark.phase3]


@pytest.mark.parametrize(
    ("repo", "url"),
    [
        ("acme/calc", "https://github.com/acme/calc.git"),
        ("My.Repo_v2/odd-name.x", "https://github.com/My.Repo_v2/odd-name.x.git"),
        ("a/b", "https://github.com/a/b.git"),
    ],
)
def test_the_clone_url_of_a_repo_is_its_https_address_without_credentials(
    repo: str, url: str
) -> None:
    assert github_clone_url(repo) == url


@pytest.mark.parametrize(
    "repo",
    [
        "",
        "calc",
        "acme/",
        "/calc",
        "acme/calc/extra",
        "../calc",
        "acme/..",
        "acme/ca lc",
        "acme/calc?x=1",
        "acme/calc#top",
        "user@host/calc",
        "acme:pw/calc",
        "acme/calc\n",
        "local:/abs/path",
    ],
)
def test_a_repo_name_that_could_change_the_address_is_refused(repo: str) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        github_clone_url(repo)


@pytest.fixture
async def github(tmp_path: Path) -> AsyncIterator[tuple[FakeGitHub, Github, str]]:
    fake = FakeGitHub()
    sha = seed_calc(fake, tmp_path, default_branch="trunk")
    built = github_for(fake)
    try:
        yield fake, built, sha
    finally:
        await built.aclose()


async def test_resolve_base_gives_the_default_branch_and_its_head(
    github: tuple[FakeGitHub, Github, str],
) -> None:
    _, built, sha = github

    base = await built.remote.resolve_base(REPO)

    assert (base.default_branch, base.sha) == ("trunk", sha)


async def test_default_branch_asks_only_for_the_repo(
    github: tuple[FakeGitHub, Github, str],
) -> None:
    fake, built, _ = github

    assert await built.remote.default_branch(REPO) == "trunk"
    assert [r.path for r in fake.requests] == ["/repos/acme/calc"]


async def test_a_bad_name_is_refused_before_any_request(
    github: tuple[FakeGitHub, Github, str],
) -> None:
    fake, built, _ = github

    for call in (built.remote.resolve_base, built.remote.default_branch):
        with pytest.raises(ValueError, match="owner/name"):
            await call("acme/calc/extra")
    with pytest.raises(ValueError, match="owner/name"):
        built.remote.clone_url("acme/calc/extra")

    assert fake.requests == []


async def test_github_errors_come_through_as_they_are(
    github: tuple[FakeGitHub, Github, str],
) -> None:
    fake, built, _ = github
    with pytest.raises(GitHubFatal):
        await built.remote.resolve_base("acme/missing")
    fake.fail_next(r"/branches/trunk$", 502)
    with pytest.raises(GitHubRetryable):
        await built.remote.resolve_base(REPO)


class Canned:
    """A client that answers with fixed JSON, for bodies that GitHub should never send."""

    def __init__(self, repo: Any, branch: Any) -> None:
        self._repo, self._branch = repo, branch

    async def get_repo(self, repo: str) -> Any:
        return self._repo

    async def get_branch(self, repo: str, branch: str) -> Any:
        return self._branch


@pytest.mark.parametrize(
    ("repo_body", "branch_body"),
    [
        ({}, {"commit": {"sha": "a" * 40}}),
        ({"default_branch": ""}, {"commit": {"sha": "a" * 40}}),
        ({"default_branch": 7}, {"commit": {"sha": "a" * 40}}),
        ({"default_branch": "main"}, {}),
        ({"default_branch": "main"}, {"commit": None}),
        ({"default_branch": "main"}, {"commit": {"sha": 5}}),
        ({"default_branch": "main"}, {"commit": {"sha": "not-a-sha"}}),
        ({"default_branch": "main"}, {"commit": {"sha": "A" * 40}}),
    ],
)
async def test_a_body_of_the_wrong_shape_is_retryable_and_nothing_is_guessed(
    repo_body: Any, branch_body: Any
) -> None:
    remote = GitHubRemote(Canned(repo_body, branch_body), PatAuth(TOKEN))
    with pytest.raises(GitHubRetryable):
        await remote.resolve_base(REPO)


async def test_the_clone_url_comes_from_the_function_the_remote_was_given() -> None:
    remote = GitHubRemote(Canned({}, {}), PatAuth(TOKEN))
    assert remote.clone_url("acme/calc") == "https://github.com/acme/calc.git"

    elsewhere = GitHubRemote(Canned({}, {}), PatAuth(TOKEN), clone_url=lambda repo: f"/r/{repo}")
    assert elsewhere.clone_url("acme/calc") == "/r/acme/calc"


async def test_the_auth_config_is_the_git_header_of_the_current_token() -> None:
    remote = GitHubRemote(Canned({}, {}), PatAuth(TOKEN))

    config = await remote.auth_config()

    login = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert config == ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {login}"]


async def test_every_call_asks_the_auth_again_so_an_expiring_token_renews() -> None:
    class Rotating:
        def __init__(self) -> None:
            self.count = 0

        async def token(self) -> str:
            self.count += 1
            return f"ghs_token{self.count}"

    auth = Rotating()
    remote = GitHubRemote(Canned({}, {}), auth)

    first, second = await remote.auth_config(), await remote.auth_config()

    assert first != second


def test_the_repr_shows_no_token() -> None:
    remote = GitHubRemote(Canned({}, {}), PatAuth(TOKEN))
    assert TOKEN not in repr(remote)
