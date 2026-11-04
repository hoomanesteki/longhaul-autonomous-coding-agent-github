"""A FakeGitHub repo whose bare remote holds the calc fixture, for clone and push tests (LH-305).

The remote is a local bare repo, so a clone and a push in a test are the real git commands
against a path, with no network. `github_for` builds the host-side pieces around it: a
GitHubClient on the fake's transport, a token, and the clone URL that points at the bare repo.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from longhaul.github.remote import GitHubRemote
from tests.fakes.fake_github import FAKE_BASE_URL, FakeGitHub, git

from longhaul.github.client import GitHubClient, PatAuth

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
REPO = "acme/calc"
# Shaped like a real token, so a leak of it is easy to search for. It is not one.
TOKEN = "ghp_clone_push_test_token_0123456789abcdef"

_spec = importlib.util.spec_from_file_location(
    "make_fixture_git", ROOT / "scripts" / "make_fixture_git.py"
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
make_fixture_git = _module.make_fixture_git


def seed_calc(
    fake: FakeGitHub, root: Path, repo: str = REPO, *, default_branch: str = "main"
) -> str:
    """Seed `repo` with a bare remote that holds calc on `default_branch`. Returns its sha."""
    source = make_fixture_git(CALC, root / "source")
    if default_branch != "main":
        git("branch", "-m", default_branch, cwd=source)
    fake.seed_repo(repo, default_branch=default_branch, remote=root / "remote.git", source=source)
    return git("rev-parse", "HEAD", cwd=source)


@dataclass
class Github:
    """What the host needs to reach the fake: the remote object, and the client behind it."""

    remote: GitHubRemote
    client: GitHubClient

    async def aclose(self) -> None:
        await self.client.aclose()


def github_for(fake: FakeGitHub, token: str = TOKEN) -> Github:
    client = GitHubClient(FAKE_BASE_URL, PatAuth(token), transport=fake.transport())
    return Github(GitHubRemote(client, PatAuth(token), clone_url=fake.clone_url), client)


def commit_on(repo: Path, parent: str, branch: str, message: str = "fix add") -> str:
    """Make a commit in a bare repo on top of `parent` and point `branch` at it.

    It reuses the tree of the parent, which is enough for a push test: only the commit id
    has to differ.
    """
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@localhost",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@localhost",
    }

    def run(*args: str) -> str:
        done = subprocess.run(
            ["git", f"--git-dir={repo}", *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        return done.stdout.strip()

    tree = run("rev-parse", f"{parent}^{{tree}}")
    sha = run("commit-tree", tree, "-p", parent, "-m", message)
    run("update-ref", f"refs/heads/{branch}", sha)
    return sha
