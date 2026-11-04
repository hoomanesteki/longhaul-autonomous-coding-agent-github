"""Where the host clones from and pushes to, and how it signs in (LH-305).

The worker talks to GitHub with git for two jobs: the clone that starts a task and the push that
ends it. Both run on the host, with the token on the command line of that one process
(`git_auth_header`), so the sandbox never sees it (ADR-0009, ADR-0012). `GitHubRemote` holds the
pieces both jobs share:

* the address to clone from and push to, which is built from the repo name and never carries a
  credential;
* the default branch and its head commit, which come from the REST API and not from the repo on
  disk. The agent can rewrite the checkpoint repo, so nothing in it decides what the base is or
  which branch is protected;
* the git arguments that sign in, built from a fresh token on every call, so an installation
  token that expires renews itself between the clone and the push.

The address is a function so a test can point it at a local bare repo.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from longhaul.github.client import GitHubAuth, GitHubRetryable, git_auth_header

__all__ = [
    "GITHUB_WEB_URL",
    "BaseCommit",
    "GitHubRemote",
    "RepoReads",
    "check_repo_name",
    "clone_url_under",
    "github_clone_url",
]

GITHUB_WEB_URL = "https://github.com"

# The rule of github/intake.py for a name that may be allowlisted: letters, digits, dot,
# underscore and hyphen, with at least one letter or digit. It rules out "..".
_NAME_PART = re.compile(r"[A-Za-z0-9._-]+")
_HAS_ALNUM = re.compile(r"[A-Za-z0-9]")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class RepoReads(Protocol):
    """The two reads that GitHubClient answers and that a base commit needs."""

    async def get_repo(self, repo: str) -> dict[str, Any]: ...

    async def get_branch(self, repo: str, branch: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class BaseCommit:
    """The head of a repo's default branch at one moment."""

    default_branch: str
    sha: str


def _valid_part(part: str) -> bool:
    return _NAME_PART.fullmatch(part) is not None and _HAS_ALNUM.search(part) is not None


def check_repo_name(repo: str) -> str:
    """Return repo if it is written owner/name with safe parts, else raise ValueError.

    The name goes into a URL that git is given, so it may not hold anything that changes the
    address: a second slash, a query, a user name or a space.
    """
    owner, slash, name = repo.partition("/")
    if not slash or not _valid_part(owner) or not _valid_part(name):
        raise ValueError(f"{repo[:80]!r} is not a GitHub repo written owner/name")
    return repo


def github_clone_url(repo: str) -> str:
    """The https address of a repo on github.com, with no credential in it."""
    return f"{GITHUB_WEB_URL}/{check_repo_name(repo)}.git"


def clone_url_under(base: str) -> Callable[[str], str]:
    """A clone address builder for a server that is not github.com: `<base>/<owner>/<name>.git`.

    A GitHub Enterprise server has its own host, and a test points it at a folder of bare repos.
    """
    root = base.rstrip("/")

    def clone_url(repo: str) -> str:
        return f"{root}/{check_repo_name(repo)}.git"

    return clone_url


class GitHubRemote:
    """What the host needs to clone from and push to GitHub.

    `client` answers the reads, `auth` gives the current token, and `clone_url` builds the
    address from a repo name.
    """

    def __init__(
        self,
        client: RepoReads,
        auth: GitHubAuth,
        *,
        clone_url: Callable[[str], str] = github_clone_url,
    ) -> None:
        self._client = client
        self._auth = auth
        self._clone_url = clone_url

    def clone_url(self, repo: str) -> str:
        """The address to clone from and push to. The name is checked first."""
        return self._clone_url(check_repo_name(repo))

    async def default_branch(self, repo: str) -> str:
        """The default branch as GitHub reports it now.

        A push asks again each time instead of keeping the answer from the clone, because the
        owner can rename the default branch while a task runs.
        """
        info = await self._client.get_repo(check_repo_name(repo))
        name = info.get("default_branch")
        if not isinstance(name, str) or not name:
            raise _wrong_shape(repo, "a repo record with a default_branch")
        return name

    async def resolve_base(self, repo: str) -> BaseCommit:
        """The default branch and the commit it holds, from two reads.

        The commit can move before the clone runs. That is fine: the clone fetches every
        branch, so the commit is in it unless a force push removed it, and the clone step
        checks for that.
        """
        name = await self.default_branch(repo)
        branch = await self._client.get_branch(repo, name)
        commit = branch.get("commit")
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise _wrong_shape(repo, f"a branch record for {name} with a commit sha")
        return BaseCommit(name, sha)

    async def auth_config(self) -> list[str]:
        """Git arguments that sign in for one command, from the token as it is now."""
        return git_auth_header(await self._auth.token())


def _wrong_shape(repo: str, expected: str) -> GitHubRetryable:
    # Same stance as the client for a 200 that is not GitHub's: the next attempt may differ.
    return GitHubRetryable(f"GitHub did not send {expected} for {repo}")
