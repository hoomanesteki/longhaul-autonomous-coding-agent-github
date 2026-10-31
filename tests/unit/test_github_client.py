"""GitHubClient and its auth (LH-302).

Reads run against FakeGitHub on its ASGI transport, so the client sees what GitHub sends:
Link headers, null bodies, pull requests inside the issue list. FakeGitHub has no GitHub App
endpoints, so the AppAuth tests answer the token exchange with an httpx2.MockTransport.

Nothing here touches the network. The RSA key for the App tests is generated once per module.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
import pickle
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
from tests.fakes.fake_github import FAKE_BASE_URL, FakeGitHub, ManualClock, git

from longhaul.github.client import (
    MAX_PAGES,
    AppAuth,
    GitHubAuth,
    GitHubClient,
    GitHubError,
    GitHubFatal,
    GitHubRetryable,
    PatAuth,
    git_auth_header,
    repo_path,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase3]

REPO = "acme/calc"
TOKEN = "ghp_unit_test_token_0123456789abcdef"
T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
NOW = int(T0.timestamp())
APP_ID = 12345
INSTALLATION_ID = 777

Reply = Callable[[httpx2.Request], httpx2.Response]


# --- fixtures and test doubles ----------------------------------------------------------------


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def fake(clock: ManualClock) -> FakeGitHub:
    github = FakeGitHub(clock=clock)
    github.seed_repo(REPO)
    return github


def client_on(
    transport: httpx2.AsyncBaseTransport, clock: ManualClock, auth: GitHubAuth | None = None
) -> GitHubClient:
    return GitHubClient(FAKE_BASE_URL, auth or PatAuth(TOKEN), transport=transport, clock=clock)


@pytest.fixture
async def gh(fake: FakeGitHub, clock: ManualClock) -> AsyncIterator[GitHubClient]:
    async with client_on(fake.transport(), clock) as client:
        yield client


class Script:
    """A MockTransport handler that plays one reply per request and repeats the last one."""

    def __init__(self, *replies: Reply) -> None:
        self.requests: list[httpx2.Request] = []
        self._replies = replies

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._replies[min(len(self.requests), len(self._replies)) - 1](request)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self)


def answer(status: int, **kwargs: Any) -> Reply:
    return lambda request: httpx2.Response(status, **kwargs)


def raising(error: Exception) -> Reply:
    def reply(request: httpx2.Request) -> httpx2.Response:
        raise error

    return reply


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TokenEndpoint:
    """GitHub's POST /app/installations/{id}/access_tokens as a MockTransport handler.

    `failures` are played first, one per request. After that every request gets a new token
    that expires one hour after the clock's now, which is what GitHub issues.
    """

    def __init__(
        self,
        clock: ManualClock,
        failures: Sequence[Reply] = (),
        lifetime: timedelta = timedelta(hours=1),
    ) -> None:
        self.clock = clock
        self.lifetime = lifetime
        self.requests: list[httpx2.Request] = []
        self.issued = 0
        self._failures = list(failures)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self._failures:
            return self._failures.pop(0)(request)
        self.issued += 1
        body = {
            "token": f"ghs_installation_{self.issued}",
            "expires_at": stamp(self.clock() + self.lifetime),
            "permissions": {"issues": "write"},
        }
        return httpx2.Response(201, json=body)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self)

    def assertions(self) -> list[str]:
        """The JWT that each request carried."""
        return [r.headers["authorization"].removeprefix("Bearer ") for r in self.requests]


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def key_path(tmp_path: Path, private_key: rsa.RSAPrivateKey) -> Path:
    # GitHub hands out App keys as PKCS#1 PEM, the "BEGIN RSA PRIVATE KEY" form.
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    path = tmp_path / "app.private-key.pem"
    path.write_bytes(pem)
    return path


def app_auth(key_path: Path, endpoint: TokenEndpoint, clock: ManualClock) -> AppAuth:
    return AppAuth(
        APP_ID,
        key_path,
        INSTALLATION_ID,
        base_url=FAKE_BASE_URL,
        transport=endpoint.transport(),
        clock=clock,
    )


def assert_app_jwt(assertion: str, private_key: rsa.RSAPrivateKey, issued_at: datetime) -> None:
    assert jwt.get_unverified_header(assertion)["alg"] == "RS256"
    claims = jwt.decode(
        assertion,
        private_key.public_key(),
        algorithms=["RS256"],
        # The signature is checked. The times are compared by hand below, because PyJWT would
        # compare them with the real clock and the test runs on a pretend one.
        options={"verify_exp": False, "verify_iat": False},
    )
    now = int(issued_at.timestamp())
    # iat sits a minute in the past for clock drift, and exp stays under GitHub's 10 minutes.
    assert claims == {"iss": str(APP_ID), "iat": now - 60, "exp": now + 9 * 60}


# --- test_reads: every read method, against FakeGitHub --------------------------------------


async def test_reads(fake: FakeGitHub, gh: GitHubClient, clock: ManualClock) -> None:
    fake.seed_collaborator(REPO, "maria", "maintain")
    fake.seed_collaborator(REPO, "rey", "read")
    await _check_issue_reads(fake, gh, clock)
    await _check_comment_reads(fake, gh)
    await _check_pull_reads(fake, gh)
    await _check_permission_reads(fake, gh)


async def _check_issue_reads(fake: FakeGitHub, gh: GitHubClient, clock: ManualClock) -> None:
    first = fake.seed_issue(
        REPO, "add() is wrong", body="Only add.", user="hooman", labels=["longhaul", "bug"]
    )
    fake.seed_issue(REPO, "docs typo")
    fake.seed_issue(REPO, "old report", labels=["longhaul"], state="closed")
    clock.advance(60)
    second = fake.seed_issue(REPO, "sub() is wrong", labels=["longhaul"])
    pull = fake.seed_pull(REPO, "longhaul-pr/1")
    async with fake.client() as http:  # label the pull request the way a person would
        labels = {"labels": ["longhaul"]}
        assert (await http.post(f"/repos/{REPO}/issues/{pull}/labels", json=labels)).is_success

    issues = await gh.list_labeled_issues(REPO, "longhaul")

    # Open and labeled only, oldest first. GitHub lists pull requests as issues, and a labeled
    # pull request must not turn into a task.
    assert [issue["number"] for issue in issues] == [first, second]
    assert all(isinstance(issue, dict) for issue in issues)
    oldest = issues[0]
    assert oldest["title"] == "add() is wrong"
    assert oldest["body"] == "Only add."
    assert oldest["user"]["login"] == "hooman"
    assert [label["name"] for label in oldest["labels"]] == ["longhaul", "bug"]
    assert "pull_request" not in oldest

    assert await gh.get_issue(REPO, first) == oldest
    # An issue without a description has a null body. The client passes that on.
    assert (await gh.get_issue(REPO, second))["body"] is None
    # An issue number can name a pull request, and get_issue returns it as GitHub does.
    assert "pull_request" in await gh.get_issue(REPO, pull)
    assert await gh.list_labeled_issues(REPO, "no-such-label") == []


async def _check_comment_reads(fake: FakeGitHub, gh: GitHubClient) -> None:
    minute = timedelta(minutes=1)
    fake.seed_comment(REPO, 1, "first", user="maria", created_at=T0)
    fake.seed_comment(REPO, 1, "second", user="rey", created_at=T0 + minute)
    fake.seed_comment(REPO, 1, "third", user="maria", created_at=T0 + 2 * minute)

    # GitHub's `since` is inclusive: the comment made at that second comes back.
    got = await gh.list_comments_since(REPO, 1, T0 + minute)
    assert [comment["body"] for comment in got] == ["second", "third"]
    assert got[0]["user"]["login"] == "rey"
    assert await gh.list_comments_since(REPO, 1, T0 + timedelta(hours=1)) == []
    # An offset other than UTC names the same instant, and the request says it in UTC.
    local = (T0 + 2 * minute).astimezone(timezone(timedelta(hours=-6)))
    assert [c["body"] for c in await gh.list_comments_since(REPO, 1, local)] == ["third"]
    assert parse_qs(fake.requests[-1].query)["since"] == ["2026-03-01T12:02:00Z"]
    with pytest.raises(ValueError, match="timezone"):
        await gh.list_comments_since(REPO, 1, datetime(2026, 3, 1, 12, 0, 0))


async def _check_pull_reads(fake: FakeGitHub, gh: GitHubClient) -> None:
    number = fake.seed_pull(REPO, "longhaul-pr/2")
    assert await gh.pr_merged(REPO, number) is False
    fake.merge_pr(REPO, number)
    assert await gh.pr_merged(REPO, number) is True
    closed = fake.seed_pull(REPO, "longhaul-pr/3")
    fake.close_pr(REPO, closed)
    assert await gh.pr_merged(REPO, closed) is False
    with pytest.raises(GitHubFatal):  # issue 1 is an issue, not a pull request
        await gh.pr_merged(REPO, 1)


async def _check_permission_reads(fake: FakeGitHub, gh: GitHubClient) -> None:
    maria = await gh.get_collaborator_permission(REPO, "maria")
    # `permission` is the old four-level field, `role_name` the real role.
    assert (maria["permission"], maria["role_name"]) == ("write", "maintain")
    assert (await gh.get_collaborator_permission(REPO, "rey"))["permission"] == "read"
    # A user who is not a collaborator gets a 200 with "none", not a 404.
    assert (await gh.get_collaborator_permission(REPO, "stranger"))["permission"] == "none"
    assert (await gh.get_collaborator_permission(REPO, "MARIA"))["permission"] == "write"
    # Bot logins hold brackets, which have to be escaped inside a path.
    await gh.get_collaborator_permission(REPO, "dependabot[bot]")
    assert fake.requests[-1].path == "/repos/acme/calc/collaborators/dependabot[bot]/permission"


# --- test_error_mapping: status codes to GitHubRetryable and GitHubFatal -------------------

PRIMARY_LIMIT = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(NOW + 90)}
SECONDARY_LIMIT = {"message": "You have exceeded a secondary rate limit. Please wait a minute."}
ABUSE_LIMIT = {"message": "You have triggered an abuse detection mechanism."}
NOT_ACCESSIBLE = {"message": "Resource not accessible by personal access token"}

# (status, response headers, response body or None for the default, error class, retry_after_s)
ERROR_CASES = [
    pytest.param(502, {}, None, GitHubRetryable, None, id="502-bad-gateway"),
    pytest.param(404, {}, None, GitHubFatal, None, id="404-not-found"),
    pytest.param(
        403,
        PRIMARY_LIMIT,
        {"message": "API rate limit exceeded for user ID 1."},
        GitHubRetryable,
        90.0,
        id="403-primary-rate-limit",
    ),
    pytest.param(429, {"retry-after": "17"}, None, GitHubRetryable, 17.0, id="429-retry-after"),
    pytest.param(500, {}, None, GitHubRetryable, None, id="500"),
    pytest.param(503, {"retry-after": "12"}, None, GitHubRetryable, 12.0, id="503-retry-after"),
    pytest.param(504, {}, None, GitHubRetryable, None, id="504"),
    pytest.param(408, {}, None, GitHubRetryable, None, id="408-request-timeout"),
    pytest.param(403, {}, SECONDARY_LIMIT, GitHubRetryable, 60.0, id="403-secondary-by-message"),
    pytest.param(403, {}, ABUSE_LIMIT, GitHubRetryable, 60.0, id="403-abuse-by-message"),
    pytest.param(429, {}, None, GitHubRetryable, 60.0, id="429-without-hints"),
    pytest.param(400, {}, None, GitHubFatal, None, id="400"),
    pytest.param(401, {}, {"message": "Bad credentials"}, GitHubFatal, None, id="401"),
    pytest.param(403, {}, NOT_ACCESSIBLE, GitHubFatal, None, id="403-permission"),
    pytest.param(
        403,
        {"x-ratelimit-remaining": "4999"},
        NOT_ACCESSIBLE,
        GitHubFatal,
        None,
        id="403-quota-left",
    ),
    pytest.param(409, {}, None, GitHubFatal, None, id="409"),
    pytest.param(410, {}, None, GitHubFatal, None, id="410-gone"),
    pytest.param(422, {}, None, GitHubFatal, None, id="422"),
]


@pytest.mark.parametrize(("status", "headers", "body", "error", "retry_after_s"), ERROR_CASES)
async def test_error_mapping(
    fake: FakeGitHub,
    gh: GitHubClient,
    status: int,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    error: type[GitHubError],
    retry_after_s: float | None,
) -> None:
    fake.fail_next(r"/issues/1$", status, headers=headers, body=body)
    with pytest.raises(error) as info:
        await gh.get_issue(REPO, 1)
    caught = info.value
    assert type(caught) is error
    assert caught.status == status
    assert str(status) in str(caught)
    if isinstance(caught, GitHubRetryable):
        assert caught.retry_after_s == retry_after_s
    # The client reports the failure and leaves the retry to the activity, so there is one try.
    assert len(fake.requests) == 1


async def test_real_not_found_answers_are_fatal(fake: FakeGitHub, gh: GitHubClient) -> None:
    with pytest.raises(GitHubFatal, match="404.*Not Found"):
        await gh.get_issue(REPO, 999)
    with pytest.raises(GitHubFatal, match="404"):
        await gh.get_issue("acme/other", 1)


async def test_error_text_carries_the_validation_errors(fake: FakeGitHub, gh: GitHubClient) -> None:
    errors = [{"resource": "PullRequest", "code": "custom", "message": "A pull request exists."}]
    fake.fail_next(r"/issues/1$", 422, body={"message": "Validation Failed", "errors": errors})
    with pytest.raises(GitHubFatal) as info:
        await gh.get_issue(REPO, 1)
    assert "Validation Failed" in str(info.value)
    assert "A pull request exists." in str(info.value)


@pytest.mark.parametrize(
    ("errors", "expected"),
    [
        pytest.param(["name already taken"], "name already taken", id="string"),
        pytest.param(
            [{"resource": "Issue", "field": "title", "code": "missing"}],
            "Issue title missing",
            id="resource-field-code",
        ),
        pytest.param([42, None, {}], "Validation Failed", id="junk-is-skipped"),
        pytest.param("not a list", "Validation Failed", id="not-a-list"),
    ],
)
async def test_error_text_copes_with_every_shape_of_errors(
    fake: FakeGitHub, gh: GitHubClient, errors: object, expected: str
) -> None:
    fake.fail_next(r"/issues/1$", 422, body={"message": "Validation Failed", "errors": errors})
    with pytest.raises(GitHubFatal) as info:
        await gh.get_issue(REPO, 1)
    assert expected in str(info.value)


async def test_a_long_error_page_is_cut_to_one_short_line(clock: ManualClock) -> None:
    page = "<html>\n" + "gateway down " * 200 + "\n</html>"
    server = Script(answer(502, text=page))
    async with client_on(server.transport(), clock) as client:
        with pytest.raises(GitHubRetryable) as info:
            await client.get_issue(REPO, 1)
    assert "\n" not in str(info.value)
    assert len(str(info.value)) < 400 and str(info.value).endswith("...")


@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ConnectError("connection refused"),
        httpx2.ConnectTimeout("connect timed out"),
        httpx2.ReadTimeout("read timed out"),
        httpx2.PoolTimeout("no free connection"),
        httpx2.ReadError("connection reset"),
        httpx2.RemoteProtocolError("peer closed the connection"),
        httpx2.DecodingError("bad gzip data"),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_network_failures_are_retryable(failure: Exception, clock: ManualClock) -> None:
    server = Script(raising(failure))
    async with client_on(server.transport(), clock) as client:
        with pytest.raises(GitHubRetryable) as info:
            await client.get_issue(REPO, 1)
    assert info.value.status is None
    assert info.value.retry_after_s is None
    assert type(failure).__name__ in str(info.value)
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        pytest.param(answer(200, text="<html>Sign in</html>"), "object", id="html-page"),
        pytest.param(answer(200, json=[{"number": 1}]), "object", id="list-for-an-object"),
        pytest.param(
            answer(200, content=b"null", headers={"content-type": "application/json"}),
            "object",
            id="null-for-an-object",
        ),
        pytest.param(answer(200, json={"message": "hi"}), "list", id="object-for-a-list"),
        pytest.param(answer(200, text=""), "list", id="empty-for-a-list"),
    ],
)
async def test_a_body_of_the_wrong_shape_is_retryable(
    reply: Reply, expected: str, clock: ManualClock
) -> None:
    # A captive portal or a broken proxy can answer 200 with something that is not GitHub's.
    server = Script(reply)
    async with client_on(server.transport(), clock) as client:
        with pytest.raises(GitHubRetryable, match=f"not a JSON {expected}") as info:
            if expected == "object":
                await client.get_issue(REPO, 1)
            else:
                await client.list_labeled_issues(REPO, "longhaul")
    assert info.value.status == 200


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_redirects_are_not_followed(status: int, clock: ManualClock) -> None:
    # A repo that was transferred redirects to its new owner, who never allowlisted it.
    location = "https://api.github.com/repositories/9/issues/1"
    server = Script(answer(status, headers={"location": location}))
    async with client_on(server.transport(), clock) as client:
        with pytest.raises(GitHubFatal, match=f"{status}.*redirect") as info:
            await client.get_issue(REPO, 1)
    assert info.value.status == status
    assert len(server.requests) == 1


# --- test_rate_limit_retry_after: what retry_after_s is, in detail ---------------------------

HTTP_DATE_45S = format_datetime(T0 + timedelta(seconds=45), usegmt=True)
# A body that says nothing about rate limits, so a 403 is told apart by its headers alone.
PLAIN_403 = {"message": "Forbidden"}


def reset_in(seconds: int, remaining: str = "0") -> dict[str, str]:
    return {"x-ratelimit-remaining": remaining, "x-ratelimit-reset": str(NOW + seconds)}


# (status, headers, expected retry_after_s). The default wait is a minute, which is what
# GitHub documents for a limit that comes with no other hint.
RETRY_AFTER_CASES = [
    pytest.param(429, {"retry-after": "17"}, 17.0, id="retry-after-seconds"),
    pytest.param(403, {"retry-after": "30"}, 30.0, id="retry-after-on-403"),
    pytest.param(429, {"retry-after": "2.5"}, 2.5, id="retry-after-fraction"),
    pytest.param(429, {"retry-after": HTTP_DATE_45S}, 45.0, id="retry-after-http-date"),
    pytest.param(403, reset_in(300), 300.0, id="reset-when-remaining-is-zero"),
    pytest.param(429, reset_in(300), 300.0, id="reset-on-429"),
    pytest.param(403, {**reset_in(300), "retry-after": "30"}, 300.0, id="both-reset-is-later"),
    pytest.param(403, {**reset_in(300), "retry-after": "500"}, 500.0, id="both-retry-after-later"),
    pytest.param(403, reset_in(-10), 60.0, id="reset-already-passed"),
    pytest.param(429, reset_in(300, remaining="4999"), 60.0, id="reset-ignored-while-quota-left"),
    pytest.param(429, {"x-ratelimit-remaining": "0"}, 60.0, id="zero-remaining-without-reset"),
    pytest.param(429, {"retry-after": "soon"}, 60.0, id="retry-after-garbage"),
    pytest.param(429, {"retry-after": "0"}, 60.0, id="retry-after-zero"),
    pytest.param(429, {"retry-after": "-5"}, 60.0, id="retry-after-negative"),
    pytest.param(429, {"retry-after": "nan"}, 60.0, id="retry-after-nan"),
    pytest.param(429, {"retry-after": "inf"}, 60.0, id="retry-after-infinite"),
    pytest.param(
        429,
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "tomorrow"},
        60.0,
        id="reset-garbage",
    ),
    pytest.param(429, {"retry-after": "999999"}, 3600.0, id="capped-at-an-hour"),
    pytest.param(403, reset_in(86_400), 3600.0, id="reset-capped-at-an-hour"),
]


@pytest.mark.parametrize(("status", "headers", "expected"), RETRY_AFTER_CASES)
async def test_rate_limit_retry_after(
    fake: FakeGitHub, gh: GitHubClient, status: int, headers: dict[str, str], expected: float
) -> None:
    fake.fail_next(r"/issues/1$", status, headers=headers, body=PLAIN_403)
    with pytest.raises(GitHubRetryable) as info:
        await gh.get_issue(REPO, 1)
    assert info.value.status == status
    assert info.value.retry_after_s == expected


async def test_retry_after_moves_with_the_clock(
    fake: FakeGitHub, gh: GitHubClient, clock: ManualClock
) -> None:
    # The same reset header gives a shorter wait as time passes.
    for waited, expected in ((0, 300.0), (100, 200.0), (299, 1.0)):
        clock.set(T0 + timedelta(seconds=waited))
        fake.fail_next(r"/issues/1$", 403, headers=reset_in(300), body=PLAIN_403)
        with pytest.raises(GitHubRetryable) as info:
            await gh.get_issue(REPO, 1)
        assert info.value.retry_after_s == expected


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        pytest.param(503, {"retry-after": "12"}, 12.0, id="503-with-retry-after"),
        pytest.param(502, {"retry-after": HTTP_DATE_45S}, 45.0, id="502-with-http-date"),
        pytest.param(500, {"retry-after": "soon"}, None, id="garbage-is-no-hint"),
        pytest.param(500, {}, None, id="no-header"),
        # A reset time says when a quota returns, which has nothing to do with a server fault.
        pytest.param(502, reset_in(300), None, id="reset-is-ignored-on-5xx"),
    ],
)
async def test_server_errors_use_only_retry_after(
    fake: FakeGitHub, gh: GitHubClient, status: int, headers: dict[str, str], expected: float | None
) -> None:
    fake.fail_next(r"/issues/1$", status, headers=headers)
    with pytest.raises(GitHubRetryable) as info:
        await gh.get_issue(REPO, 1)
    assert info.value.retry_after_s == expected


async def test_the_default_clock_is_the_real_time() -> None:
    # No clock is injected here, so the wait comes from the machine's own now.
    reset = int(time.time()) + 120
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}
    server = Script(answer(403, headers=headers, json={"message": "Forbidden"}))
    async with GitHubClient(FAKE_BASE_URL, PatAuth(TOKEN), transport=server.transport()) as client:
        with pytest.raises(GitHubRetryable) as info:
            await client.get_issue(REPO, 1)
    assert info.value.retry_after_s is not None
    assert 118 <= info.value.retry_after_s <= 120


def test_error_classes() -> None:
    assert issubclass(GitHubRetryable, GitHubError)
    assert issubclass(GitHubFatal, GitHubError)
    assert not issubclass(GitHubFatal, GitHubRetryable)
    assert not issubclass(GitHubRetryable, GitHubFatal)
    bare = GitHubRetryable("no hint")
    assert (bare.status, bare.retry_after_s, str(bare)) == (None, None, "no hint")


@pytest.mark.parametrize(
    "copier",
    [lambda e: pickle.loads(pickle.dumps(e)), copy.copy, copy.deepcopy],
    ids=["pickle", "copy", "deepcopy"],
)
def test_errors_survive_pickling_and_copying(copier: Callable[[Any], Any]) -> None:
    # An exception that crosses a process or activity boundary is rebuilt from its arguments,
    # and a rebuild that kept only the message would lose the retry hint.
    retryable = copier(GitHubRetryable("slow down", 429, 17.0))
    assert type(retryable) is GitHubRetryable
    assert (retryable.message, retryable.status, retryable.retry_after_s) == (
        "slow down",
        429,
        17.0,
    )
    assert str(retryable) == "slow down"
    fatal = copier(GitHubFatal("no such repo", 404))
    assert type(fatal) is GitHubFatal
    assert (fatal.message, fatal.status) == ("no such repo", 404)


# --- pagination -------------------------------------------------------------------------------


async def test_pagination_follows_the_link_header(fake: FakeGitHub, gh: GitHubClient) -> None:
    for n in range(230):
        fake.seed_issue(REPO, f"issue {n}", labels=["longhaul"])

    issues = await gh.list_labeled_issues(REPO, "longhaul")

    assert [issue["number"] for issue in issues] == list(range(1, 231))
    queries = [parse_qs(request.query) for request in fake.requests]
    assert [q.get("page", ["1"])[0] for q in queries] == ["1", "2", "3"]
    assert all(q["per_page"] == ["100"] and q["labels"] == ["longhaul"] for q in queries)
    assert all(q["state"] == ["open"] for q in queries)


async def test_pagination_stops_when_there_is_no_next_link(
    fake: FakeGitHub, gh: GitHubClient
) -> None:
    for n in range(100):  # exactly one full page: GitHub sends no Link header
        fake.seed_issue(REPO, f"issue {n}", labels=["longhaul"])
    assert len(await gh.list_labeled_issues(REPO, "longhaul")) == 100
    assert len(fake.requests) == 1


async def test_pagination_reads_every_page_of_comments(fake: FakeGitHub, gh: GitHubClient) -> None:
    number = fake.seed_issue(REPO, "a long thread")
    for n in range(130):
        fake.seed_comment(REPO, number, f"comment {n}", created_at=T0 + timedelta(seconds=n))
    comments = await gh.list_comments_since(REPO, number, T0 - timedelta(days=1))
    assert [c["body"] for c in comments] == [f"comment {n}" for n in range(130)]
    assert len(fake.requests) == 2


def page_with_next(url: str) -> Reply:
    return answer(200, json=[{"number": 1}], headers={"link": f'<{url}>; rel="next"'})


async def test_pagination_refuses_a_link_to_another_host(clock: ManualClock) -> None:
    # The Authorization header goes with every request, so a next link that points away from
    # the API host must not be followed.
    server = Script(page_with_next("https://evil.example/repos/acme/calc/issues?page=2"))
    async with client_on(server.transport(), clock) as client:
        with pytest.raises(GitHubFatal, match="evil.example"):
            await client.list_labeled_issues(REPO, "longhaul")
    assert [request.url.host for request in server.requests] == ["fakegithub.test"]


@pytest.mark.parametrize(
    "next_url", ["https://fakegithub.test/x?page=2", "http://fakegithub.test:8443/x?page=2"]
)
async def test_pagination_refuses_a_link_to_another_scheme_or_port(
    next_url: str, clock: ManualClock
) -> None:
    server = Script(page_with_next(next_url))
    async with client_on(server.transport(), clock) as client:
        with pytest.raises(GitHubFatal, match="outside"):
            await client.list_labeled_issues(REPO, "longhaul")
    assert len(server.requests) == 1


async def test_pagination_gives_up_on_a_link_loop(clock: ManualClock) -> None:
    server = Script(page_with_next(f"{FAKE_BASE_URL}/repos/acme/calc/issues?page=2"))
    async with client_on(server.transport(), clock) as client:
        with pytest.raises(GitHubFatal, match=str(MAX_PAGES)):
            await client.list_labeled_issues(REPO, "longhaul")
    assert len(server.requests) == MAX_PAGES


# --- writes: meant for side_effects.py, still held to the same rules --------------------------


async def test_write_verbs_send_json_and_parse_the_answer(
    fake: FakeGitHub, gh: GitHubClient
) -> None:
    fake.seed_issue(REPO, "needs a comment")
    fake.seed_token(TOKEN, "hooman")  # FakeGitHub acts as the owner of the bearer token

    comment = await gh._post("/repos/acme/calc/issues/1/comments", {"body": "Picked up"})
    assert comment["user"]["login"] == "hooman"
    assert fake.comment_bodies(REPO, 1) == ["Picked up"]

    added = await gh._post("/repos/acme/calc/issues/1/labels", {"labels": ["longhaul:working"]})
    assert [label["name"] for label in added] == ["longhaul:working"]
    left = await gh._delete("/repos/acme/calc/issues/1/labels/longhaul%3Aworking")
    assert left == []
    assert fake.label_names(REPO, 1) == []


async def test_a_write_answered_with_something_that_is_not_json_is_retryable(
    clock: ManualClock,
) -> None:
    server = Script(answer(201, text="<html>created, probably</html>"))
    async with client_on(server.transport(), clock) as client:
        with pytest.raises(GitHubRetryable, match="not a JSON document") as info:
            await client._post("/repos/acme/calc/issues/1/comments", {"body": "hi"})
    assert info.value.status == 201


async def test_patch_and_an_empty_answer(clock: ManualClock) -> None:
    server = Script(answer(200, json={"state": "closed"}), answer(204))
    async with client_on(server.transport(), clock) as client:
        closed = await client._patch("/repos/acme/calc/issues/1", {"state": "closed"})
        nothing = await client._delete("/repos/acme/calc/issues/comments/5")
    assert closed == {"state": "closed"}
    assert nothing is None
    patch, delete = server.requests
    assert (patch.method, json.loads(patch.content)) == ("PATCH", {"state": "closed"})
    assert (delete.method, delete.url.path) == ("DELETE", "/repos/acme/calc/issues/comments/5")


async def test_a_write_that_landed_but_failed_is_retryable(
    fake: FakeGitHub, gh: GitHubClient
) -> None:
    # The "did my write land?" case. The client cannot know, so it calls the failure retryable
    # and does not retry on its own. The idempotent activity of LH-304 decides what to do.
    fake.seed_issue(REPO, "needs a comment")
    fake.after_write_fail(r"/comments$")
    with pytest.raises(GitHubRetryable) as info:
        await gh._post("/repos/acme/calc/issues/1/comments", {"body": "hello"})
    assert info.value.status == 502
    assert fake.count_comments(REPO, 1) == 1
    assert len(fake.requests) == 1


# --- requests ---------------------------------------------------------------------------------


async def test_requests_carry_the_standard_headers(fake: FakeGitHub, gh: GitHubClient) -> None:
    fake.seed_issue(REPO, "anything")
    await gh.get_issue(REPO, 1)
    await gh._post("/repos/acme/calc/issues/1/comments", {"body": "hi"})
    for request in fake.requests:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["accept"] == "application/vnd.github+json"
        assert request.headers["x-github-api-version"] == "2022-11-28"
        assert request.headers["user-agent"].startswith("longhaul")
    assert fake.requests[1].headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    "repo", ["", "acme", "acme/", "/calc", "acme/calc/extra", "local:/abs/path", "acme/calc?x=1"]
)
async def test_a_repo_name_that_is_not_owner_slash_name_is_refused(
    repo: str, fake: FakeGitHub, gh: GitHubClient
) -> None:
    # The name goes into a URL path, so it is checked and escaped. Nothing may reach the wire
    # in a form that changes the path or adds a query.
    with pytest.raises(GitHubFatal):
        await gh.get_issue(repo, 1)
    assert not any("x=1" in request.query for request in fake.requests)


def test_repo_path() -> None:
    assert repo_path("acme/calc") == "/repos/acme/calc"
    assert repo_path("My.Repo_v2/odd-name.git") == "/repos/My.Repo_v2/odd-name.git"
    assert repo_path("a b/c") == "/repos/a%20b/c"
    with pytest.raises(GitHubFatal):
        repo_path("not a repo")


async def test_the_client_can_be_used_without_a_context_manager(
    fake: FakeGitHub, clock: ManualClock
) -> None:
    fake.seed_issue(REPO, "anything")
    client = client_on(fake.transport(), clock)
    assert (await client.get_issue(REPO, 1))["number"] == 1
    await client.aclose()


# --- PatAuth ----------------------------------------------------------------------------------


async def test_pat_auth_returns_the_token() -> None:
    assert await PatAuth(TOKEN).token() == TOKEN
    # Settings keep tokens as SecretStr, and passing one on unchanged must work.
    assert await PatAuth(SecretStr(TOKEN)).token() == TOKEN
    # A token read from a file or a Docker secret usually ends in a newline.
    assert await PatAuth(f"  {TOKEN}\n").token() == TOKEN


@pytest.mark.parametrize(
    "bad", ["", "   ", "\n", "two words", "tab\tinside", "line\nbreak", "café"]
)
def test_a_token_that_cannot_be_sent_is_refused(bad: str) -> None:
    # An illegal header value makes httpx2 raise an error that quotes the whole header, token
    # included. Refusing the token at the start keeps it out of that message.
    for build in (PatAuth, git_auth_header):
        with pytest.raises(ValueError, match="GitHub token") as info:
            build(bad)
        if bad.strip():
            assert bad.strip() not in str(info.value)


# --- test_app_token_cached and the rest of AppAuth -------------------------------------------


async def test_app_token_cached(
    key_path: Path, private_key: rsa.RSAPrivateKey, clock: ManualClock
) -> None:
    endpoint = TokenEndpoint(clock)
    auth = app_auth(key_path, endpoint, clock)

    first = await auth.token()
    clock.advance(30 * 60)  # half of the token's life
    second = await auth.token()

    assert first == second == "ghs_installation_1"
    assert len(endpoint.requests) == 1  # two calls, one exchange
    request = endpoint.requests[0]
    assert (request.method, request.url.path) == ("POST", "/app/installations/777/access_tokens")
    assert request.headers["accept"] == "application/vnd.github+json"
    assert_app_jwt(endpoint.assertions()[0], private_key, T0)


async def test_app_token_is_renewed_five_minutes_before_it_expires(
    key_path: Path, private_key: rsa.RSAPrivateKey, clock: ManualClock
) -> None:
    endpoint = TokenEndpoint(clock)  # tokens live for an hour
    auth = app_auth(key_path, endpoint, clock)
    assert await auth.token() == "ghs_installation_1"

    clock.advance(54 * 60 + 59)  # one second before the margin starts
    assert await auth.token() == "ghs_installation_1"
    assert len(endpoint.requests) == 1

    clock.advance(1)  # 55 minutes in: five minutes left, so the token is replaced
    assert await auth.token() == "ghs_installation_2"
    assert len(endpoint.requests) == 2
    assert_app_jwt(endpoint.assertions()[1], private_key, T0 + timedelta(minutes=55))


async def test_app_token_exchange_is_serialized(key_path: Path, clock: ManualClock) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    requests: list[httpx2.Request] = []

    async def slow_endpoint(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        entered.set()
        await release.wait()  # the first exchange stays open while the others arrive
        body = {"token": "ghs_slow", "expires_at": stamp(clock() + timedelta(hours=1))}
        return httpx2.Response(201, json=body)

    auth = AppAuth(
        APP_ID,
        key_path,
        INSTALLATION_ID,
        base_url=FAKE_BASE_URL,
        transport=httpx2.MockTransport(slow_endpoint),
        clock=clock,
    )
    callers = [asyncio.create_task(auth.token()) for _ in range(5)]
    await entered.wait()
    for _ in range(10):  # let the other four callers reach the lock
        await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(*callers) == ["ghs_slow"] * 5
    assert len(requests) == 1


EXCHANGE_FAILURES = [
    pytest.param(
        answer(401, json={"message": "A JSON web token could not be decoded"}),
        GitHubFatal,
        id="401-bad-jwt",
    ),
    pytest.param(answer(404, json={"message": "Not Found"}), GitHubFatal, id="404-installation"),
    pytest.param(answer(403, json={"message": "Forbidden"}), GitHubFatal, id="403"),
    pytest.param(answer(502, json={"message": "Bad Gateway"}), GitHubRetryable, id="502"),
    pytest.param(answer(429, headers={"retry-after": "5"}), GitHubRetryable, id="429"),
    pytest.param(raising(httpx2.ConnectError("refused")), GitHubRetryable, id="network"),
    pytest.param(answer(201, json={}), GitHubRetryable, id="201-without-a-token"),
    pytest.param(
        answer(201, json={"token": "ghs_x", "expires_at": "next tuesday"}),
        GitHubRetryable,
        id="201-with-a-bad-expiry",
    ),
    pytest.param(
        answer(201, json={"token": "", "expires_at": "2026-03-01T13:00:00Z"}),
        GitHubRetryable,
        id="201-with-an-empty-token",
    ),
]


@pytest.mark.parametrize(("failure", "error"), EXCHANGE_FAILURES)
async def test_app_token_exchange_failures_use_the_same_mapping(
    failure: Reply, error: type[GitHubError], key_path: Path, clock: ManualClock
) -> None:
    endpoint = TokenEndpoint(clock, failures=[failure])
    auth = app_auth(key_path, endpoint, clock)
    with pytest.raises(error) as info:
        await auth.token()
    assert type(info.value) is error
    # Nothing was cached and the lock was released, so the next call exchanges again.
    assert await auth.token() == "ghs_installation_1"
    assert len(endpoint.requests) == 2


async def test_an_expiry_without_a_zone_is_read_as_utc(key_path: Path, clock: ManualClock) -> None:
    naive = answer(201, json={"token": "ghs_naive", "expires_at": "2026-03-01T13:00:00"})
    endpoint = TokenEndpoint(clock, failures=[naive])
    auth = app_auth(key_path, endpoint, clock)
    assert await auth.token() == "ghs_naive"
    clock.advance(54 * 60 + 59)  # 12:54:59, still before 13:00 minus the margin
    assert await auth.token() == "ghs_naive"
    clock.advance(1)
    assert await auth.token() == "ghs_installation_1"


async def test_app_rate_limit_hint_comes_through(key_path: Path, clock: ManualClock) -> None:
    endpoint = TokenEndpoint(clock, failures=[answer(429, headers={"retry-after": "5"})])
    with pytest.raises(GitHubRetryable) as info:
        await app_auth(key_path, endpoint, clock).token()
    assert info.value.retry_after_s == 5.0


@pytest.mark.parametrize(
    "content", [None, "", "super-secret-garbage", "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n"]
)
async def test_a_missing_or_broken_app_key_is_fatal(
    content: str | None, tmp_path: Path, clock: ManualClock
) -> None:
    path = tmp_path / "key.pem"
    if content is not None:
        path.write_text(content)
    endpoint = TokenEndpoint(clock)
    auth = app_auth(path, endpoint, clock)
    with pytest.raises(GitHubFatal) as info:
        await auth.token()
    assert str(path) in str(info.value)
    assert "super-secret-garbage" not in str(info.value)
    assert endpoint.requests == []  # no request goes out without a signed JWT


async def test_a_client_sends_the_installation_token(
    fake: FakeGitHub, key_path: Path, clock: ManualClock
) -> None:
    fake.seed_issue(REPO, "anything")
    endpoint = TokenEndpoint(clock)
    async with client_on(fake.transport(), clock, app_auth(key_path, endpoint, clock)) as client:
        await client.get_issue(REPO, 1)
        await client.get_issue(REPO, 1)
        clock.advance(55 * 60)  # the cached token is about to expire
        await client.get_issue(REPO, 1)
    seen = [request.headers["authorization"] for request in fake.requests]
    assert seen == ["Bearer ghs_installation_1"] * 2 + ["Bearer ghs_installation_2"]
    assert len(endpoint.requests) == 2


# --- test_git_auth_header_not_in_url ----------------------------------------------------------

HEADER_KEY = "http.https://github.com/.extraheader"
CLONE_URL = "https://github.com/acme/calc.git"


def run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def test_git_auth_header_not_in_url() -> None:
    args = git_auth_header(TOKEN)

    # Two arguments for git, ahead of the subcommand. Each is one argv entry, so the space in
    # the header value needs no quoting.
    assert len(args) == 2 and args[0] == "-c"
    key, _, value = args[1].partition("=")
    assert key == HEADER_KEY
    assert value.startswith("AUTHORIZATION: basic ")
    encoded = value.removeprefix("AUTHORIZATION: basic ")
    assert base64.b64decode(encoded).decode() == f"x-access-token:{TOKEN}"

    # The URL stays clean and the token appears nowhere in the command in plain form.
    argv = ["git", *args, "clone", "--bare", CLONE_URL, "/work/calc.git"]
    assert CLONE_URL in argv and TOKEN not in CLONE_URL
    assert not [part for part in argv if TOKEN in part]
    assert git_auth_header(TOKEN) == args  # nothing random or stateful
    assert git_auth_header("another") != args


def test_git_accepts_the_header_and_scopes_it_to_github(tmp_path: Path) -> None:
    args = git_auth_header(TOKEN)
    shown = git(*args, "config", "--get-urlmatch", "http.extraheader", CLONE_URL, cwd=tmp_path)
    assert shown == args[1].partition("=")[2]
    # Another host gets no header, so a redirect cannot carry the token away.
    other = "https://example.com/x.git"
    elsewhere = run_git(*args, "config", "--get-urlmatch", "http.extraheader", other, cwd=tmp_path)
    assert elsewhere.returncode == 1 and elsewhere.stdout == ""


def test_git_does_not_save_the_header(tmp_path: Path) -> None:
    repo = tmp_path / "work"
    git("init", "-q", str(repo))
    git(*git_auth_header(TOKEN), "remote", "add", "origin", CLONE_URL, cwd=repo)
    saved = (repo / ".git" / "config").read_text()
    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert CLONE_URL in saved  # the remote was written, so a leaked header would have been too
    assert TOKEN not in saved and encoded not in saved


# --- tokens stay out of reprs, errors and logs ------------------------------------------------


async def test_tokens_never_appear_in_reprs_errors_or_logs(
    fake: FakeGitHub, key_path: Path, clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake.seed_issue(REPO, "anything")
    endpoint = TokenEndpoint(clock)
    pat, app = PatAuth(TOKEN), app_auth(key_path, endpoint, clock)
    by_pat, by_app = (
        client_on(fake.transport(), clock, pat),
        client_on(fake.transport(), clock, app),
    )
    failures: list[GitHubError] = []

    async with by_pat, by_app:
        for client, token in ((by_pat, TOKEN), (by_app, "ghs_installation_1")):
            await client.get_issue(REPO, 1)
            bodies = [None, None, {"message": "API rate limit exceeded"}]
            # The last server echoes the token it was sent, which a real one should not do.
            bodies.append({"message": f"Bad credentials: {token}"})
            for status, body in zip((404, 502, 403, 401), bodies, strict=True):
                fake.fail_next(r"/issues/1$", status, headers=reset_in(5), body=body)
                with pytest.raises(GitHubError) as info:
                    await client.get_issue(REPO, 1)
                failures.append(info.value)
        objects = [pat, app, by_pat, by_app, vars(pat), vars(app)]

    # An illegal header makes the transport quote it, token included. The message is scrubbed.
    leaky = httpx2.LocalProtocolError(f"Illegal header value b'Bearer {TOKEN}'")
    async with client_on(Script(raising(leaky)).transport(), clock, pat) as broken:
        with pytest.raises(GitHubRetryable) as caught:
            await broken.get_issue(REPO, 1)
        failures.append(caught.value)

    # The same for the JWT of an exchange that fails with an echo.
    def echo(request: httpx2.Request) -> httpx2.Response:
        message = f"Invalid token {request.headers['authorization']}"
        return httpx2.Response(401, json={"message": message})

    failing = TokenEndpoint(clock, failures=[echo])
    with pytest.raises(GitHubFatal) as rejected:
        await app_auth(key_path, failing, clock).token()
    failures.append(rejected.value)

    secrets = [TOKEN, "ghs_installation_1", endpoint.assertions()[0], failing.assertions()[0]]
    shown = [repr(obj) for obj in objects] + [str(obj) for obj in objects]
    for error in failures:
        shown += [str(error), repr(error), repr(error.args), str(vars(error))]
    shown.append(caplog.text)
    for secret in secrets:
        assert not [text for text in shown if secret in text], secret
        # Nor a piece of it. A long echo cut for length before it was scrubbed leaves one.
        assert not [text for text in shown if secret[:40] in text], secret
    for echoed in (failures[3], failures[7], failures[-1]):
        assert "[redacted]" in str(echoed)  # the echoed token was replaced, not dropped
    assert all(error.__cause__ is None for error in failures)
