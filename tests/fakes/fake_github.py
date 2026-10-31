"""An in-memory GitHub for tests (LH-301).

FakeGitHub is a FastAPI app plus the helpers a test needs around it. It answers only the
REST calls Longhaul makes: issues, comments, labels, pulls and collaborator permission. It
copies GitHub's response shapes and the behaviours that catch clients out:

* An issue and a pull request share one number sequence. A pull request also appears in the
  issue list (with a `pull_request` key) and takes comments and labels at its number.
* Lists page with `per_page` (default 30, at most 100) and `page`, and send a `Link` header.
* `since` on comments keeps those updated at or after the time, so the edge is inclusive.
* Timestamps are UTC to the second, like "2026-03-01T12:00:00Z".
* The `head` filter on pulls needs `owner:branch`. A bare branch name matches nothing.
* A pull request in a list has no `merged` field. Only `GET /pulls/{n}` has it.
* An issue without a description has `body: null`.
* Removing a label that the issue does not have is a 404. A second open pull request from
  the same head into the same base is a 422.

A test builds the world with the `seed_*` helpers, mounts the app and reads the result back
with the `count_*` helpers:

    fake = FakeGitHub()
    fake.seed_repo("acme/calc")
    fake.seed_issue("acme/calc", "add() is wrong", labels=["longhaul"])
    async with fake.client() as http:  # httpx2 on the ASGI transport: no server, no port
        issues = (await http.get("/repos/acme/calc/issues")).json()

To test a client class instead, build its httpx2.AsyncClient with `base_url=FAKE_BASE_URL` and
`transport=fake.transport()`. `serve_in_thread` runs the same app on a real port for runs that
need one, and `python tests/fakes/fake_github.py` (`make fake-github`) serves an empty one on
port 8099.

Faults: `fail_next` answers with an error and applies nothing. `after_write_fail` applies a
write and then answers 502, which is the "did my write land?" case.

A repo can have a local bare git repo as its remote. The fake does not copy refs. It reads
them from the bare repo when asked, so a branch pushed with git shows up at once.

State sits behind a lock, so the helpers are safe to call while `serve_in_thread` is serving.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import re
import subprocess
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any, Literal

import httpx2
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import URL
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "FAKE_BASE_URL",
    "FakeGitHub",
    "GitHubError",
    "Invalid",
    "LabelEvent",
    "ManualClock",
    "NotFound",
    "RecordedRequest",
    "RunningServer",
    "git",
    "main",
    "serve_in_thread",
]

API_URL = "https://api.github.com"
WEB_URL = "https://github.com"
DOCS_URL = "https://docs.github.com/rest"
# The host that `FakeGitHub.client()` uses. Nothing resolves it: the ASGI transport skips DNS.
FAKE_BASE_URL = "http://fakegithub.test"

DEFAULT_PER_PAGE = 30
MAX_PER_PAGE = 100
MAX_COMMENT_CHARS = 65536
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Weakest first, so the position is the rank.
ROLES = ("none", "read", "triage", "write", "maintain", "admin")
# The older `permission` field has four levels. The role names are the real ones.
LEGACY_PERMISSION = {
    "none": "none",
    "read": "read",
    "triage": "read",
    "write": "write",
    "maintain": "write",
    "admin": "admin",
}
STATES = ("open", "closed", "all")

Clock = Callable[[], datetime]
FaultWhen = Literal["before", "after"]


# --- errors -----------------------------------------------------------------------------------


class GitHubError(Exception):
    """An error that the HTTP layer sends as a GitHub-shaped JSON body.

    `detail` is for the test author when the same code runs from a seed helper.
    """

    def __init__(
        self,
        status: int,
        message: str,
        errors: list[dict[str, str]] | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(detail or message)
        self.status = status
        self.message = message
        self.errors = errors


class NotFound(GitHubError, LookupError):
    def __init__(self, detail: str, message: str = "Not Found") -> None:
        super().__init__(404, message, detail=detail)


class Invalid(GitHubError, ValueError):
    def __init__(
        self,
        resource: str | None = None,
        field: str | None = None,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        parts = {"resource": resource, "field": field, "code": code, "message": message}
        error = {key: value for key, value in parts.items() if value}
        super().__init__(
            422, "Validation Failed", [error], detail=f"GitHub would reject this: {error}"
        )


# --- time -------------------------------------------------------------------------------------


class ManualClock:
    """A clock that moves only when a test says so.

    GitHub timestamps have one-second resolution, so a test that reads `since` or `updated_at`
    needs to place events on known seconds instead of sleeping.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 1.0) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = moment


def _system_clock() -> datetime:
    return datetime.now(UTC)


def _to_second(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(microsecond=0)


def _stamp(moment: datetime | None) -> str | None:
    return None if moment is None else moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_since(raw: str) -> datetime:
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise Invalid("Search", "since", "invalid") from None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


# --- state ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelEvent:
    """One label change made through the API. Seeding a label records nothing."""

    number: int
    action: Literal["labeled", "unlabeled"]
    label: str
    actor: str
    created_at: datetime


@dataclass(frozen=True)
class RecordedRequest:
    """A request as the fake saw it, with the status that went back to the caller."""

    method: str
    path: str
    query: str
    headers: Mapping[str, str]
    status: int
    fault: FaultWhen | None = None


@dataclass
class _Label:
    id: int
    name: str
    color: str = "ededed"
    description: str | None = None


@dataclass
class _Comment:
    id: int
    user: str
    body: str
    created_at: datetime
    updated_at: datetime


@dataclass
class _Pull:
    id: int
    head_owner: str
    head_ref: str
    base_ref: str
    draft: bool
    head_sha: str
    base_sha: str
    merged_at: datetime | None = None
    merged_by: str | None = None
    merge_commit_sha: str | None = None


@dataclass
class _Issue:
    id: int
    number: int
    title: str
    body: str
    user: str
    created_at: datetime
    updated_at: datetime
    state: str = "open"
    closed_at: datetime | None = None
    labels: list[str] = field(default_factory=list)
    comments: list[_Comment] = field(default_factory=list)
    pull: _Pull | None = None


@dataclass
class _Repo:
    id: int
    owner: str
    name: str
    default_branch: str
    remote: Path | None
    # Keyed by lower-cased login or label name, because GitHub ignores case when it matches.
    collaborators: dict[str, tuple[str, str]] = field(default_factory=dict)
    labels: dict[str, _Label] = field(default_factory=dict)
    issues: dict[int, _Issue] = field(default_factory=dict)
    label_events: list[LabelEvent] = field(default_factory=list)
    next_number: int = 1

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


# eq=False: a rule is found again by identity, and two rules with the same fields are still two.
@dataclass(eq=False)
class _Fault:
    when: FaultWhen
    pattern: re.Pattern[str]
    status: int
    remaining: int
    method: str | None = None
    headers: Mapping[str, str] | None = None
    body: Mapping[str, Any] | None = None


# --- git --------------------------------------------------------------------------------------

_GIT_SETTINGS = [
    arg
    for pair in (
        "core.hooksPath=" + os.devnull,
        "core.excludesFile=" + os.devnull,
        "core.attributesFile=" + os.devnull,
        "commit.gpgsign=false",
    )
    for arg in ("-c", pair)
]


def _git_env() -> dict[str, str]:
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return {
        **inherited,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "fake-github",
        "GIT_AUTHOR_EMAIL": "fake-github@localhost",
        "GIT_COMMITTER_NAME": "fake-github",
        "GIT_COMMITTER_EMAIL": "fake-github@localhost",
    }


def git(*args: str, cwd: Path | None = None) -> str:
    """Run git without the user's or the system's config and return stdout, stripped.

    Tests use it to build the repos that sit behind a FakeGitHub, so a signing key or a
    global hook on the developer's machine cannot change the result.
    """
    result = subprocess.run(
        ["git", *_GIT_SETTINGS, *args],
        cwd=cwd,
        env=_git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _read_refs_at(path: Path) -> dict[str, str]:
    out = git("for-each-ref", "--format=%(refname) %(objectname)", cwd=path)
    return dict(line.split(" ", 1) for line in out.splitlines() if line)


def _make_remote(remote: Path, default_branch: str, source: Path | None) -> None:
    if remote.exists() and any(remote.iterdir()):
        raise ValueError(f"remote {remote} exists and is not empty")
    if source is not None and f"refs/heads/{default_branch}" not in _read_refs_at(source):
        raise ValueError(f"source {source} has no {default_branch!r} branch")
    remote.parent.mkdir(parents=True, exist_ok=True)
    if source is None:
        git("init", "-q", "--bare", "--template=", "-b", default_branch, str(remote))
        return
    git("clone", "-q", "--bare", "--template=", str(source), str(remote))
    git("symbolic-ref", "HEAD", f"refs/heads/{default_branch}", cwd=remote)


def _stand_in_sha(*parts: str) -> str:
    return hashlib.sha1(":".join(parts).encode(), usedforsecurity=False).hexdigest()


# --- GitHub-shaped JSON -----------------------------------------------------------------------


def _user(login: str) -> dict[str, Any]:
    return {
        "login": login,
        "id": zlib.crc32(login.lower().encode()),
        "type": "Bot" if login.endswith("[bot]") else "User",
        "site_admin": False,
    }


def _permissions(role: str) -> dict[str, bool]:
    rank = ROLES.index(role)
    return {
        "admin": rank >= ROLES.index("admin"),
        "maintain": rank >= ROLES.index("maintain"),
        "push": rank >= ROLES.index("write"),
        "triage": rank >= ROLES.index("triage"),
        "pull": rank >= ROLES.index("read"),
    }


def _repo_json(repo: _Repo) -> dict[str, Any]:
    return {
        "id": repo.id,
        "name": repo.name,
        "full_name": repo.full_name,
        "html_url": f"{WEB_URL}/{repo.full_name}",
        "default_branch": repo.default_branch,
        "owner": _user(repo.owner),
    }


def _label_json(repo: _Repo, name: str) -> dict[str, Any]:
    label = repo.labels[name.lower()]
    return {
        "id": label.id,
        "url": f"{API_URL}/repos/{repo.full_name}/labels/{label.name}",
        "name": label.name,
        "color": label.color,
        "description": label.description,
        "default": False,
    }


def _issue_json(repo: _Repo, issue: _Issue) -> dict[str, Any]:
    kind = "pull" if issue.pull else "issues"
    html_url = f"{WEB_URL}/{repo.full_name}/{kind}/{issue.number}"
    data: dict[str, Any] = {
        "id": issue.id,
        "number": issue.number,
        "url": f"{API_URL}/repos/{repo.full_name}/issues/{issue.number}",
        "html_url": html_url,
        "title": issue.title,
        "body": issue.body or None,
        "state": issue.state,
        "locked": False,
        "user": _user(issue.user),
        "labels": [_label_json(repo, name) for name in issue.labels],
        "comments": len(issue.comments),
        "created_at": _stamp(issue.created_at),
        "updated_at": _stamp(issue.updated_at),
        "closed_at": _stamp(issue.closed_at),
    }
    if issue.pull:
        data["pull_request"] = {
            "url": f"{API_URL}/repos/{repo.full_name}/pulls/{issue.number}",
            "html_url": html_url,
            "diff_url": f"{html_url}.diff",
            "patch_url": f"{html_url}.patch",
            "merged_at": _stamp(issue.pull.merged_at),
        }
    return data


def _comment_json(repo: _Repo, issue: _Issue, comment: _Comment) -> dict[str, Any]:
    html = f"{WEB_URL}/{repo.full_name}/issues/{issue.number}"
    return {
        "id": comment.id,
        "url": f"{API_URL}/repos/{repo.full_name}/issues/comments/{comment.id}",
        "html_url": f"{html}#issuecomment-{comment.id}",
        "issue_url": f"{API_URL}/repos/{repo.full_name}/issues/{issue.number}",
        "body": comment.body,
        "user": _user(comment.user),
        "created_at": _stamp(comment.created_at),
        "updated_at": _stamp(comment.updated_at),
    }


def _branch_json(repo: _Repo, owner: str, ref: str, sha: str) -> dict[str, Any]:
    return {
        "label": f"{owner}:{ref}",
        "ref": ref,
        "sha": sha,
        "user": _user(owner),
        "repo": _repo_json(repo),
    }


def _pull_json(
    repo: _Repo, issue: _Issue, refs: Mapping[str, str], *, full: bool
) -> dict[str, Any]:
    """A pull request as GitHub sends it. `full` is the single-pull shape, with `merged`."""
    pull = issue.pull
    assert pull is not None
    head_sha = refs.get(f"refs/heads/{pull.head_ref}", pull.head_sha)
    base_sha = refs.get(f"refs/heads/{pull.base_ref}", pull.base_sha)
    shared = _issue_json(repo, issue)
    data: dict[str, Any] = {
        "id": pull.id,
        "number": issue.number,
        "url": f"{API_URL}/repos/{repo.full_name}/pulls/{issue.number}",
        "html_url": shared["html_url"],
        "state": issue.state,
        "locked": False,
        "title": issue.title,
        "body": shared["body"],
        "user": shared["user"],
        "labels": shared["labels"],
        "draft": pull.draft,
        "created_at": shared["created_at"],
        "updated_at": shared["updated_at"],
        "closed_at": shared["closed_at"],
        "merged_at": _stamp(pull.merged_at),
        "merge_commit_sha": pull.merge_commit_sha,
        "head": _branch_json(repo, pull.head_owner, pull.head_ref, head_sha),
        "base": _branch_json(repo, repo.owner, pull.base_ref, base_sha),
    }
    if full:
        data["merged"] = pull.merged_at is not None
        data["merged_by"] = _user(pull.merged_by) if pull.merged_by else None
        data["comments"] = len(issue.comments)
    return data


def _error(status: int, message: str, errors: list[dict[str, str]] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"message": message}
    if errors:
        body["errors"] = errors
    body |= {"documentation_url": DOCS_URL, "status": str(status)}
    return JSONResponse(body, status_code=status)


# --- the fake ---------------------------------------------------------------------------------


class FakeGitHub:
    """In-memory GitHub state, the helpers around it, and `app`, the FastAPI app that serves it.

    `clock` places events in time, see ManualClock. `bot_login` is the author of writes that
    arrive without a token that `seed_token` knows. `requests` logs every request received.
    """

    def __init__(self, *, clock: Clock | None = None, bot_login: str = "longhaul-bot") -> None:
        self._clock = clock or _system_clock
        self.bot_login = bot_login
        self._lock = threading.RLock()
        self._repos: dict[str, _Repo] = {}
        self._tokens: dict[str, str] = {}
        self._faults: list[_Fault] = []
        self._last_id = 1_000_000
        self.requests: list[RecordedRequest] = []
        self.app = _build_app(self)

    # -- seeding

    def seed_repo(
        self,
        full_name: str,
        *,
        default_branch: str = "main",
        remote: Path | None = None,
        source: Path | None = None,
    ) -> None:
        """Add a repo. Its owner starts as an admin collaborator.

        `remote` is where a bare git repo is created for it. `source`, a path to any git repo,
        fills that bare repo with the source's branches and tags.
        """
        owner, _, name = full_name.partition("/")
        if not owner or not name or "/" in name:
            raise ValueError(f"a repo is written owner/name, not {full_name!r}")
        if source is not None and remote is None:
            raise ValueError("source needs remote, the path of the bare repo to create")
        with self._lock:
            if full_name.lower() in self._repos:
                raise ValueError(f"repo {full_name} already exists")
            if remote is not None:
                _make_remote(remote, default_branch, source)
            repo = _Repo(self._next_id(), owner, name, default_branch, remote)
            repo.collaborators[owner.lower()] = (owner, "admin")
            self._repos[full_name.lower()] = repo

    def seed_issue(
        self,
        repo: str,
        title: str,
        *,
        body: str = "",
        user: str = "octocat",
        labels: Iterable[str] = (),
        state: str = "open",
        number: int | None = None,
        created_at: datetime | None = None,
    ) -> int:
        """Add an issue and return its number. Labels are created in the repo as needed."""
        if state not in ("open", "closed"):
            raise ValueError(f"state is open or closed, not {state!r}")
        with self._lock:
            target = self._get_repo(repo)
            issue = self._new_issue(target, title, body, user, number, created_at)
            self._add_labels(target, issue, labels, actor=None)
            if state == "closed":
                issue.state, issue.closed_at = "closed", issue.created_at
            return issue.number

    def seed_comment(
        self,
        repo: str,
        number: int,
        body: str,
        *,
        user: str = "octocat",
        created_at: datetime | None = None,
    ) -> int:
        """Add a comment as `user`, the way a person would, and return its id."""
        with self._lock:
            target = self._get_repo(repo)
            issue = self._get_issue(target, number)
            return self._add_comment(issue, user, body, created_at).id

    def seed_collaborator(self, repo: str, login: str, permission: str = "write") -> None:
        """Give `login` a role: none, read, triage, write, maintain or admin."""
        if permission not in ROLES:
            raise ValueError(f"permission is one of {', '.join(ROLES)}, not {permission!r}")
        with self._lock:
            self._get_repo(repo).collaborators[login.lower()] = (login, permission)

    def seed_token(self, token: str, login: str) -> None:
        """Make requests that carry this token act as `login`."""
        with self._lock:
            self._tokens[token] = login

    def seed_pull(
        self,
        repo: str,
        head: str,
        *,
        base: str | None = None,
        title: str = "Change",
        body: str = "",
        user: str | None = None,
        draft: bool = False,
    ) -> int:
        """Open a pull request, as `POST /pulls` would, and return its number."""
        with self._lock:
            target = self._get_repo(repo)
            issue = self._new_pull(
                target, title, body, head, base or target.default_branch, draft, user
            )
            return issue.number

    def merge_pr(self, repo: str, number: int, *, by: str = "maintainer") -> None:
        """Merge an open pull request. `GET /pulls/{n}` then reports `merged: true`."""
        with self._lock:
            target = self._get_repo(repo)
            self._close(self._get_pull(target, number), merged_by=by)

    def close_pr(self, repo: str, number: int) -> None:
        """Close an open pull request without merging it."""
        with self._lock:
            target = self._get_repo(repo)
            self._close(self._get_pull(target, number), merged_by=None)

    # -- reading state back

    def count_comments(self, repo: str, number: int | None = None, *, containing: str = "") -> int:
        """Comments in the repo, or on one issue, that contain `containing`."""
        with self._lock:
            target = self._get_repo(repo)
            issues = list(target.issues.values())
            if number is not None:
                issues = [self._get_issue(target, number)]
            return sum(containing in c.body for issue in issues for c in issue.comments)

    def comment_bodies(self, repo: str, number: int) -> list[str]:
        with self._lock:
            return [c.body for c in self._get_issue(self._get_repo(repo), number).comments]

    def label_names(self, repo: str, number: int) -> list[str]:
        with self._lock:
            return list(self._get_issue(self._get_repo(repo), number).labels)

    def label_events(self, repo: str, number: int | None = None) -> list[LabelEvent]:
        with self._lock:
            events = self._get_repo(repo).label_events
            return [e for e in events if number is None or e.number == number]

    def pulls(self, repo: str, state: str = "all") -> list[dict[str, Any]]:
        """Pull requests in list shape, oldest first."""
        with self._lock:
            target = self._get_repo(repo)
            refs = self._read_refs(target)
            found = [i for i in target.issues.values() if i.pull and _has_state(i, state)]
            return [_pull_json(target, i, refs, full=False) for i in found]

    def count_pulls(self, repo: str, state: str = "all") -> int:
        return len(self.pulls(repo, state))

    def refs(self, repo: str) -> dict[str, str]:
        """Every ref in the repo's bare remote, as refname to commit id."""
        with self._lock:
            return _read_refs_at(self._remote(self._get_repo(repo)))

    def branches(self, repo: str) -> dict[str, str]:
        """Branch name to commit id in the repo's bare remote."""
        prefix = "refs/heads/"
        return {
            r.removeprefix(prefix): sha
            for r, sha in self.refs(repo).items()
            if r.startswith(prefix)
        }

    def clone_url(self, repo: str) -> str:
        """Where to `git clone` and `git push`: the path of the bare remote."""
        with self._lock:
            return str(self._remote(self._get_repo(repo)))

    # -- faults

    def fail_next(
        self,
        path_regex: str,
        status: int = 500,
        count: int = 1,
        *,
        method: str | None = None,
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> None:
        """Answer the next `count` matching requests with `status`, applying nothing.

        `path_regex` is searched in the request path, without the query. Give `method` to
        match one verb only, `headers` for response headers such as `retry-after`, and `body`
        to replace the default error body. Rules are used in the order they were added.
        """
        self._add_fault("before", path_regex, status, count, method, headers, body)

    def after_write_fail(
        self, path_regex: str = ".*", count: int = 1, *, status: int = 502
    ) -> None:
        """Apply the next `count` matching writes, then answer `status` instead of the result.

        This is the "did my write land?" case. Reads never use up a rule, and neither does a
        write that GitHub would have refused: its own error goes back, as it would for real.
        A `fail_next` rule that matches the same request is used first.
        """
        self._add_fault("after", path_regex, status, count, None, None, None)

    def clear_faults(self) -> None:
        with self._lock:
            self._faults.clear()

    # -- plumbing

    def transport(self) -> httpx2.ASGITransport:
        """An httpx2 transport that calls the app directly, with no server."""
        return httpx2.ASGITransport(app=self.app)

    def client(self, **options: Any) -> httpx2.AsyncClient:
        """An `httpx2.AsyncClient` wired to this fake. `options` go to the client."""
        return httpx2.AsyncClient(
            **{"transport": self.transport(), "base_url": FAKE_BASE_URL, **options}
        )

    # -- internals shared with the HTTP layer

    def _next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    def _now(self) -> datetime:
        return _to_second(self._clock())

    def _get_repo(self, full_name: str) -> _Repo:
        repo = self._repos.get(full_name.lower())
        if repo is None:
            raise NotFound(f"repo {full_name} is not seeded")
        return repo

    def _get_issue(self, repo: _Repo, number: int) -> _Issue:
        issue = repo.issues.get(number)
        if issue is None:
            raise NotFound(f"{repo.full_name} has no issue or pull request {number}")
        return issue

    def _get_pull(self, repo: _Repo, number: int) -> _Issue:
        issue = self._get_issue(repo, number)
        if issue.pull is None:
            raise NotFound(f"{repo.full_name}#{number} is an issue, not a pull request")
        return issue

    def _remote(self, repo: _Repo) -> Path:
        if repo.remote is None:
            raise ValueError(f"{repo.full_name} has no remote; pass remote= to seed_repo")
        return repo.remote

    def _read_refs(self, repo: _Repo) -> dict[str, str]:
        return _read_refs_at(repo.remote) if repo.remote else {}

    def _new_issue(
        self,
        repo: _Repo,
        title: str,
        body: str,
        user: str,
        number: int | None = None,
        at: datetime | None = None,
    ) -> _Issue:
        if number is None:
            number = repo.next_number
        elif number < 1 or number in repo.issues:
            raise ValueError(f"issue {number} already exists in {repo.full_name}")
        repo.next_number = max(repo.next_number, number + 1)
        moment = _to_second(at) if at else self._now()
        issue = _Issue(self._next_id(), number, title, body, user, moment, moment)
        repo.issues[number] = issue
        return issue

    def _add_comment(
        self, issue: _Issue, user: str, body: str, at: datetime | None = None
    ) -> _Comment:
        moment = _to_second(at) if at else self._now()
        comment = _Comment(self._next_id(), user, body, moment, moment)
        issue.comments.append(comment)
        issue.updated_at = max(issue.updated_at, moment)
        return comment

    def _add_labels(
        self, repo: _Repo, issue: _Issue, names: Iterable[str], actor: str | None
    ) -> None:
        """Attach labels, creating any the repo lacks. `actor=None` is seeding: no events."""
        for name in names:
            label = repo.labels.get(name.lower())
            if label is None:
                label = repo.labels[name.lower()] = _Label(self._next_id(), name)
            if label.name in issue.labels:
                continue
            issue.labels.append(label.name)
            if actor is not None:
                self._record_label(repo, issue, "labeled", label.name, actor)

    def _remove_label(self, repo: _Repo, issue: _Issue, name: str, actor: str) -> None:
        label = repo.labels.get(name.lower())
        if label is None or label.name not in issue.labels:
            raise NotFound(
                f"{repo.full_name}#{issue.number} has no label {name!r}", "Label does not exist"
            )
        issue.labels.remove(label.name)
        self._record_label(repo, issue, "unlabeled", label.name, actor)

    def _record_label(
        self,
        repo: _Repo,
        issue: _Issue,
        action: Literal["labeled", "unlabeled"],
        label: str,
        actor: str,
    ) -> None:
        now = self._now()
        repo.label_events.append(LabelEvent(issue.number, action, label, actor, now))
        issue.updated_at = now

    def _new_pull(
        self,
        repo: _Repo,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool,
        user: str | None,
    ) -> _Issue:
        head_owner, _, head_ref = head.rpartition(":")
        head_owner = head_owner or repo.owner
        refs = self._read_refs(repo)
        if repo.remote is not None:
            same_repo = head_owner.lower() == repo.owner.lower()
            if same_repo and f"refs/heads/{head_ref}" not in refs:
                raise Invalid("PullRequest", "head", "invalid")
            if f"refs/heads/{base}" not in refs:
                raise Invalid("PullRequest", "base", "invalid")
        if _has_open_pull(repo, head_owner, head_ref, base):
            message = f"A pull request already exists for {head_owner}:{head_ref}."
            raise Invalid("PullRequest", code="custom", message=message)
        issue = self._new_issue(repo, title, body, user or self.bot_login)
        issue.pull = _Pull(
            id=self._next_id(),
            head_owner=head_owner,
            head_ref=head_ref,
            base_ref=base,
            draft=draft,
            head_sha=refs.get(f"refs/heads/{head_ref}", _stand_in_sha(repo.full_name, head_ref)),
            base_sha=refs.get(f"refs/heads/{base}", _stand_in_sha(repo.full_name, base)),
        )
        return issue

    def _close(self, issue: _Issue, merged_by: str | None) -> None:
        pull = issue.pull
        assert pull is not None
        if issue.state != "open":
            raise ValueError(f"pull request {issue.number} is not open")
        now = self._now()
        issue.state, issue.closed_at, issue.updated_at = "closed", now, now
        if merged_by is not None:
            pull.merged_at, pull.merged_by = now, merged_by
            pull.merge_commit_sha = _stand_in_sha("merge", str(issue.number), str(pull.id))

    def _actor(self, authorization: str) -> str:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() in ("bearer", "token"):
            return self._tokens.get(token.strip(), self.bot_login)
        return self.bot_login

    def _add_fault(
        self,
        when: FaultWhen,
        path_regex: str,
        status: int,
        count: int,
        method: str | None,
        headers: Mapping[str, str] | None,
        body: Mapping[str, Any] | None,
    ) -> None:
        if count < 1:
            raise ValueError(f"count is at least 1, not {count}")
        verb = method.upper() if method else None
        rule = _Fault(when, re.compile(path_regex), status, count, verb, headers, body)
        with self._lock:
            self._faults.append(rule)

    def _find_fault(self, when: FaultWhen, method: str, path: str) -> _Fault | None:
        """The first rule that fits this request. Finding a rule does not use it up."""
        with self._lock:
            for rule in self._faults:
                fits = rule.when == when and rule.method in (None, method)
                if (
                    fits
                    and rule.pattern.search(path)
                    and (when == "before" or method in WRITE_METHODS)
                ):
                    return rule
        return None

    def _use_fault(self, rule: _Fault) -> bool:
        """Count one use of `rule`. False when another request used its last one first."""
        with self._lock:
            if rule not in self._faults:
                return False
            rule.remaining -= 1
            if rule.remaining == 0:
                self._faults.remove(rule)
            return True

    def _record(self, scope: Scope, status: int, fault: FaultWhen | None) -> None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        record = RecordedRequest(
            scope["method"], scope["path"], scope["query_string"].decode(), headers, status, fault
        )
        with self._lock:
            self.requests.append(record)


def _has_state(issue: _Issue, state: str) -> bool:
    return state == "all" or issue.state == state


def _has_open_pull(repo: _Repo, head_owner: str, head_ref: str, base: str) -> bool:
    return any(
        issue.pull is not None
        and issue.state == "open"
        and issue.pull.base_ref == base
        and issue.pull.head_ref == head_ref
        and issue.pull.head_owner.lower() == head_owner.lower()
        for issue in repo.issues.values()
    )


# --- the HTTP layer ---------------------------------------------------------------------------


class _FaultMiddleware:
    """ASGI middleware that applies the fault rules and logs every request.

    It sits outside the router so that unknown paths are logged too.
    """

    def __init__(self, app: ASGIApp, fake: FakeGitHub) -> None:
        self.app = app
        self.fake = fake

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status, fault = 500, None
        try:
            status, fault = await self._handle(scope, receive, send)
        finally:
            self.fake._record(scope, status, fault)

    async def _handle(
        self, scope: Scope, receive: Receive, send: Send
    ) -> tuple[int, FaultWhen | None]:
        method, path = scope["method"], scope["path"]
        before = self.fake._find_fault("before", method, path)
        if before is not None and self.fake._use_fault(before):
            await self._fail(before)(scope, receive, send)
            return before.status, "before"
        after = self.fake._find_fault("after", method, path)
        if after is None:
            return await self._relay(scope, receive, send), None
        return await self._write_then_fail(after, scope, receive, send)

    async def _relay(self, scope: Scope, receive: Receive, send: Send) -> int:
        status = 500

        async def watch(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await self.app(scope, receive, watch)
        return status

    async def _write_then_fail(
        self, rule: _Fault, scope: Scope, receive: Receive, send: Send
    ) -> tuple[int, FaultWhen | None]:
        held: list[Message] = []

        async def hold(message: Message) -> None:
            held.append(message)

        await self.app(scope, receive, hold)  # the write is applied here
        status = next(m["status"] for m in held if m["type"] == "http.response.start")
        if status < 400 and self.fake._use_fault(rule):
            await self._fail(rule)(scope, receive, send)  # and the caller never hears of it
            return rule.status, "after"
        for message in held:  # GitHub refused the write, so its answer goes back as it is
            await send(message)
        return status, None

    @staticmethod
    def _fail(fault: _Fault) -> JSONResponse:
        body = fault.body
        if body is None:
            try:
                reason = HTTPStatus(fault.status).phrase
            except ValueError:
                reason = "Error"
            body = {"message": reason, "documentation_url": DOCS_URL, "status": str(fault.status)}
        return JSONResponse(dict(body), status_code=fault.status, headers=dict(fault.headers or {}))


def _bounded(raw: str | None, default: int, high: int | None = None) -> int:
    """A positive int from a query value. Anything else gives the default, as GitHub does."""
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        return default
    if value < 1:
        return default
    return min(value, high) if high else value


def _choice(raw: str | None, allowed: tuple[str, ...], default: str, name: str) -> str:
    if raw is None:
        return default
    if raw not in allowed:
        raise Invalid(field=name, code="invalid")
    return raw


def _link_header(url: URL, page: int, last: int) -> str | None:
    """Link targets in GitHub's order: prev, next, last, first. None when there is one page."""
    targets: list[tuple[str, int]] = []
    if page > 1:
        targets.append(("prev", min(page - 1, last)))
    if page < last:
        targets += [("next", page + 1), ("last", last)]
    if page > 1:
        targets.append(("first", 1))
    if not targets:
        return None
    return ", ".join(f'<{url.include_query_params(page=n)}>; rel="{rel}"' for rel, n in targets)


def _sorted(issues: Iterable[_Issue], sort: str | None, direction: str | None) -> list[_Issue]:
    def key(issue: _Issue) -> tuple[datetime, int]:
        return (issue.updated_at if sort == "updated" else issue.created_at, issue.number)

    return sorted(issues, key=key, reverse=direction != "asc")


async def _json_body(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError:
        raise GitHubError(400, "Problems parsing JSON") from None


def _issue_number(request: Request) -> int:
    raw = request.path_params["number"]
    if not (raw.isascii() and raw.isdigit()):
        raise NotFound(f"{raw!r} is not an issue number")
    return int(raw)


def _label_names(payload: Any) -> list[str]:
    """The names in a label request: {"labels": [...]}, a bare array, strings or {"name": ..}."""
    items = payload.get("labels") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise Invalid("Label", "labels", "invalid")
    names: list[str] = []
    for item in items:
        name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str) or not name.strip():
            raise Invalid("Label", "name", "invalid")
        names.append(name)
    return names


class _Api:
    """One method per endpoint. Each reads the request, works under the lock and shapes JSON."""

    def __init__(self, fake: FakeGitHub) -> None:
        self.fake = fake

    # -- helpers

    def _repo(self, request: Request) -> _Repo:
        params = request.path_params
        return self.fake._get_repo(f"{params['owner']}/{params['repo']}")

    def _issue(self, request: Request) -> tuple[_Repo, _Issue]:
        repo = self._repo(request)
        return repo, self.fake._get_issue(repo, _issue_number(request))

    def _actor(self, request: Request) -> str:
        return self.fake._actor(request.headers.get("authorization", ""))

    @staticmethod
    def _page(request: Request, items: list[dict[str, Any]]) -> JSONResponse:
        query = request.query_params
        per_page = _bounded(query.get("per_page"), DEFAULT_PER_PAGE, MAX_PER_PAGE)
        page = _bounded(query.get("page"), 1)
        last = max(1, -(-len(items) // per_page))
        link = _link_header(request.url, page, last)
        headers = {"link": link} if link else None
        return JSONResponse(items[(page - 1) * per_page : page * per_page], headers=headers)

    # -- issues

    async def list_issues(self, request: Request) -> Response:
        query = request.query_params
        state = _choice(query.get("state"), STATES, "open", "state")
        wanted = {n.strip().lower() for n in query.get("labels", "").split(",") if n.strip()}
        since = _parse_since(query["since"]) if "since" in query else None
        with self.fake._lock:
            repo = self._repo(request)
            chosen = [
                issue
                for issue in repo.issues.values()
                if _has_state(issue, state)
                and wanted <= {name.lower() for name in issue.labels}
                and (since is None or issue.updated_at >= since)
            ]
            ordered = _sorted(chosen, query.get("sort"), query.get("direction"))
            items = [_issue_json(repo, issue) for issue in ordered]
        return self._page(request, items)

    async def get_issue(self, request: Request) -> Response:
        with self.fake._lock:
            repo, issue = self._issue(request)
            return JSONResponse(_issue_json(repo, issue))

    # -- comments

    async def list_comments(self, request: Request) -> Response:
        raw = request.query_params.get("since")
        since = _parse_since(raw) if raw is not None else None
        with self.fake._lock:
            repo, issue = self._issue(request)
            items = [
                _comment_json(repo, issue, comment)
                for comment in issue.comments
                if since is None or comment.updated_at >= since
            ]
        return self._page(request, items)

    async def create_comment(self, request: Request) -> Response:
        payload = await _json_body(request)
        with self.fake._lock:
            repo, issue = self._issue(request)
            body = payload.get("body") if isinstance(payload, dict) else None
            if not isinstance(body, str) or not body.strip():
                raise Invalid("IssueComment", "body", "missing_field")
            if len(body) > MAX_COMMENT_CHARS:
                message = f"Body is too long (maximum is {MAX_COMMENT_CHARS} characters)"
                raise Invalid("IssueComment", "body", "custom", message)
            comment = self.fake._add_comment(issue, self._actor(request), body)
            return JSONResponse(_comment_json(repo, issue, comment), status_code=201)

    # -- labels

    async def add_labels(self, request: Request) -> Response:
        payload = await _json_body(request)
        with self.fake._lock:
            repo, issue = self._issue(request)
            self.fake._add_labels(repo, issue, _label_names(payload), actor=self._actor(request))
            return JSONResponse([_label_json(repo, name) for name in issue.labels])

    async def remove_label(self, request: Request) -> Response:
        with self.fake._lock:
            repo, issue = self._issue(request)
            self.fake._remove_label(repo, issue, request.path_params["name"], self._actor(request))
            return JSONResponse([_label_json(repo, name) for name in issue.labels])

    # -- pulls

    async def list_pulls(self, request: Request) -> Response:
        query = request.query_params
        state = _choice(query.get("state"), STATES, "open", "state")
        head, base = query.get("head"), query.get("base")
        with self.fake._lock:
            repo = self._repo(request)
            refs = self.fake._read_refs(repo)
            chosen = [
                issue
                for issue in repo.issues.values()
                if issue.pull
                and _has_state(issue, state)
                and (head is None or _head_matches(head, issue.pull))
                and (base is None or base == issue.pull.base_ref)
            ]
            ordered = _sorted(chosen, query.get("sort"), query.get("direction"))
            items = [_pull_json(repo, issue, refs, full=False) for issue in ordered]
        return self._page(request, items)

    async def get_pull(self, request: Request) -> Response:
        with self.fake._lock:
            repo = self._repo(request)
            issue = self.fake._get_pull(repo, _issue_number(request))
            refs = self.fake._read_refs(repo)
            return JSONResponse(_pull_json(repo, issue, refs, full=True))

    async def create_pull(self, request: Request) -> Response:
        payload = await _json_body(request)
        fields = payload if isinstance(payload, dict) else {}
        with self.fake._lock:
            repo = self._repo(request)
            for required in ("title", "head", "base"):
                if not isinstance(fields.get(required), str) or not fields[required]:
                    raise Invalid("PullRequest", required, "missing_field")
            issue = self.fake._new_pull(
                repo,
                fields["title"],
                str(fields.get("body") or ""),
                fields["head"],
                fields["base"],
                bool(fields.get("draft", False)),
                self._actor(request),
            )
            refs = self.fake._read_refs(repo)
            return JSONResponse(_pull_json(repo, issue, refs, full=True), status_code=201)

    # -- collaborators

    async def get_permission(self, request: Request) -> Response:
        username = request.path_params["username"]
        with self.fake._lock:
            repo = self._repo(request)
            login, role = repo.collaborators.get(username.lower(), (username, "none"))
        user = {**_user(login), "permissions": _permissions(role), "role_name": role}
        return JSONResponse(
            {"permission": LEGACY_PERMISSION[role], "role_name": role, "user": user}
        )


def _head_matches(head: str, pull: _Pull) -> bool:
    """GitHub filters on `owner:branch`. A value without the owner matches nothing."""
    owner, colon, ref = head.partition(":")
    return bool(colon) and owner.lower() == pull.head_owner.lower() and ref == pull.head_ref


async def _on_github_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, GitHubError)
    return _error(exc.status, exc.message, exc.errors)


async def _on_http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    # An unknown route and a known path with the wrong verb are both a 404 on GitHub.
    if exc.status_code in (404, 405):
        return _error(404, "Not Found")
    return _error(exc.status_code, str(exc.detail))


def _build_app(fake: FakeGitHub) -> FastAPI:
    # No docs routes and no slash redirects: the only endpoints are the ones in the story.
    app = FastAPI(
        title="FakeGitHub", docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False
    )
    app.add_exception_handler(GitHubError, _on_github_error)
    app.add_exception_handler(StarletteHTTPException, _on_http_error)
    app.add_middleware(_FaultMiddleware, fake=fake)

    api, repo = _Api(fake), "/repos/{owner}/{repo}"
    routes: list[tuple[str, Callable[..., Any], str]] = [
        (f"{repo}/issues", api.list_issues, "GET"),
        (f"{repo}/issues/{{number}}", api.get_issue, "GET"),
        (f"{repo}/issues/{{number}}/comments", api.list_comments, "GET"),
        (f"{repo}/issues/{{number}}/comments", api.create_comment, "POST"),
        (f"{repo}/issues/{{number}}/labels", api.add_labels, "POST"),
        (f"{repo}/issues/{{number}}/labels/{{name:path}}", api.remove_label, "DELETE"),
        (f"{repo}/pulls", api.list_pulls, "GET"),
        (f"{repo}/pulls", api.create_pull, "POST"),
        (f"{repo}/pulls/{{number}}", api.get_pull, "GET"),
        (f"{repo}/collaborators/{{username}}/permission", api.get_permission, "GET"),
    ]
    for path, endpoint, method in routes:
        app.add_api_route(
            path, endpoint, methods=[method], response_model=None, include_in_schema=False
        )
    return app


# --- running it -------------------------------------------------------------------------------


class RunningServer:
    """A FakeGitHub served on a real port from a background thread. Use it as a context manager."""

    def __init__(
        self, host: str, port: int, server: uvicorn.Server, thread: threading.Thread
    ) -> None:
        self.host = host
        self.port = port
        self._server = server
        self._thread = thread

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
        if self._thread.is_alive():  # a client is holding a connection open
            self._server.force_exit = True
            self._thread.join(timeout=5)

    def __enter__(self) -> RunningServer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def _run_quietly(server: uvicorn.Server) -> None:
    # uvicorn calls sys.exit when it cannot bind its port. In a thread that would only make
    # pytest warn about an unhandled thread exception, and the caller already gets an error.
    with contextlib.suppress(SystemExit):
        server.run()


def serve_in_thread(
    fake: FakeGitHub, *, host: str = "127.0.0.1", port: int = 0, start_timeout_s: float = 10.0
) -> RunningServer:
    """Serve `fake` on a background thread and return once it accepts connections.

    Port 0 picks a free port, which `RunningServer.port` reports. The server shares state with
    `fake`, so the seed and count helpers keep working while it runs.
    """
    config = uvicorn.Config(
        fake.app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=_run_quietly, args=(server,), name="fake-github", daemon=True)
    thread.start()
    deadline = time.monotonic() + start_timeout_s
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError(f"FakeGitHub did not start on port {port} of {host}; is it taken?")
        time.sleep(0.01)
    bound = server.servers[0].sockets[0].getsockname()[1]
    return RunningServer(host, bound, server, thread)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve an empty FakeGitHub for end-to-end runs.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args(argv)
    uvicorn.run(FakeGitHub().app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
