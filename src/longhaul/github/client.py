"""The GitHub client, its auth, and the git arguments for host-side clone and push (LH-302).

One client for every GitHub call, so every call fails the same way. A failed call raises one of
two errors, and the activity retry policy (failure mode F10) decides what to do with them:

* GitHubRetryable: the same call may work later. That covers 5xx, 408, network errors and
  timeouts, 429, a 403 that carries a sign of a rate limit, and a 200 whose body is not
  GitHub's. When the response says how long to wait, `retry_after_s` carries it.
* GitHubFatal: waiting will not help. That covers any other 4xx, a redirect, and a repo name
  that is not an owner/name pair.

The client never retries on its own. A hidden retry would repeat a write before the
idempotency key of LH-304 had a chance to look for it, and it would hide the failure from the
workflow history.

Redirects are not followed. GitHub redirects the old name of a transferred repo to its new
owner, who never allowlisted it, so acting on the redirect would leave the allowlist. The same
reasoning applies to pagination: a next-page link must stay on the API origin, because every
request carries the token.

Tokens come from an auth object, one call per request, so an expiring App token renews itself.
A token is never put in a URL, a log line, a repr or an exception message. See ADR-0012.

The write methods (_post, _patch, _delete) are here so the client is complete, but only
github/side_effects.py may call them (invariant I4).
"""

import asyncio
import base64
import email.utils
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, Self
from urllib.parse import quote

import httpx2
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from pydantic import SecretStr

__all__ = [
    "GITHUB_API_URL",
    "MAX_PAGES",
    "AppAuth",
    "GitHubAuth",
    "GitHubClient",
    "GitHubError",
    "GitHubFatal",
    "GitHubRetryable",
    "PatAuth",
    "git_auth_header",
    "repo_path",
]

GITHUB_API_URL = "https://api.github.com"
# Pinned so that a change of GitHub's default version cannot change responses under us.
API_VERSION = "2022-11-28"
USER_AGENT = "longhaul"  # GitHub rejects a request that has none

DEFAULT_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 10.0
PAGE_SIZE = 100  # the most GitHub returns per page
# A stop for a Link header that never ends. 100 pages of 100 is far more than any issue
# thread or label has, so reaching it means the server is looping.
MAX_PAGES = 100

# GitHub tells a client with a secondary limit to wait at least a minute when nothing else
# says how long.
RATE_LIMIT_WAIT_S = 60.0
# A primary limit resets within the hour. A longer wait is more likely a fault than a real
# one, and a huge float would overflow timedelta in the retry policy.
MAX_RETRY_AFTER_S = 3600.0
MAX_DETAIL_CHARS = 300

# A GitHub App JWT may live ten minutes. It is dated a minute back for clock drift and ends
# nine minutes from now, which leaves a minute of drift against GitHub's limit as well.
JWT_BACKDATE_S = 60
JWT_LIFETIME_S = 9 * 60
# The installation token is replaced this long before it expires, so a request that is
# already on its way never carries a token that dies in flight.
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)

_RATE_LIMIT_WORDS = ("rate limit", "abuse detection")
# The characters a token may have. Anything else cannot go in a header, and the error that
# httpx2 raises for an illegal header value quotes the whole header, token included.
_TOKEN_SHAPE = re.compile(r"[\x21-\x7e]+")

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ---------- errors ----------
class GitHubError(Exception):
    """A GitHub call failed. Catch GitHubRetryable or GitHubFatal, not this."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        # None when no response came back (network error, timeout, unusable key).
        self.status = status

    def __reduce__(self) -> tuple[Any, ...]:
        # Exception pickling replays only self.args, which would drop the status.
        return (type(self), (self.message, self.status))


class GitHubRetryable(GitHubError):
    """The same call may succeed later. `retry_after_s` is how long GitHub asked us to wait."""

    def __init__(
        self, message: str, status: int | None = None, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message, status)
        # None sends the retry policy back to its own backoff.
        self.retry_after_s = retry_after_s

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.message, self.status, self.retry_after_s))


class GitHubFatal(GitHubError):
    """Waiting will not help: the request is wrong, refused, or aimed at the wrong place."""


# ---------- reading what GitHub sent back ----------
def _float_or_none(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _retry_after(raw: str | None, now: datetime) -> float | None:
    # Seconds, as GitHub sends it, or an HTTP date, which the standard also allows.
    seconds = _float_or_none(raw)
    if seconds is not None or raw is None:
        return seconds
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return (when.replace(tzinfo=when.tzinfo or UTC) - now).total_seconds()


def _until_reset(raw: str | None, now: datetime) -> float | None:
    reset = _float_or_none(raw)  # epoch seconds
    return None if reset is None else reset - now.timestamp()


def _quota_is_spent(headers: httpx2.Headers) -> bool:
    return headers.get("x-ratelimit-remaining", "").strip() == "0"


def _wait_hint(headers: httpx2.Headers, now: datetime, *, rate_limited: bool) -> float | None:
    """Seconds GitHub asks us to wait, or None when it says nothing usable.

    When both retry-after and the quota reset time are there, the longer one wins, because
    a retry before either has passed would fail again. The reset time counts only for a rate
    limit answer, and only while the quota is spent: on a server fault it says when a window
    renews, which is no reason to wait.
    """
    waits = [_retry_after(headers.get("retry-after"), now)]
    if rate_limited and _quota_is_spent(headers):
        waits.append(_until_reset(headers.get("x-ratelimit-reset"), now))
    usable = [wait for wait in waits if wait is not None and math.isfinite(wait) and wait > 0]
    return min(max(usable), MAX_RETRY_AFTER_S) if usable else None


def _error_text(item: object) -> str:
    # GitHub's `errors` array holds strings, or objects with a message or a resource/field/code.
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return ""
    if item.get("message"):
        return str(item["message"])
    return " ".join(str(item[key]) for key in ("resource", "field", "code") if item.get(key))


def _detail(response: httpx2.Response) -> str:
    """GitHub's own words for a failure, on one line.

    A 422 says only "Validation Failed" in `message`. The reason is in `errors`.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        errors = body.get("errors")
        listed = errors if isinstance(errors, list) else []
        parts = [str(body.get("message") or ""), *map(_error_text, listed)]
        text = "; ".join(part for part in parts if part)
    else:
        text = response.text
    return " ".join(text.split()) or response.reason_phrase


def _redact(text: str, secret: str) -> str:
    # Belt and braces: nothing we send is meant to come back, but the text here is a server's
    # or a transport's, and an exception message must stay free of the token whatever it says.
    return text.replace(secret, "[redacted]") if secret else text


def _shorten(text: str) -> str:
    # After _redact, never before: a cut through the middle of a token would leave a piece of
    # it that the replacement no longer recognizes.
    return text if len(text) <= MAX_DETAIL_CHARS else text[:MAX_DETAIL_CHARS] + "..."


def _is_rate_limit(headers: httpx2.Headers, detail: str) -> bool:
    # A 403 is also what GitHub answers for plain missing permission, so it counts as a rate
    # limit only with a sign of one: a retry-after header, a spent quota, or the words.
    return (
        "retry-after" in headers
        or _quota_is_spent(headers)
        or any(word in detail.lower() for word in _RATE_LIMIT_WORDS)
    )


def _failure(response: httpx2.Response, where: str, secret: str, now: datetime) -> GitHubError:
    """Turn a response that is not a success into the error the retry policy understands."""
    status = response.status_code
    detail = _shorten(_redact(_detail(response), secret))
    if 300 <= status < 400:
        location = _redact(response.headers.get("location", "somewhere else"), secret)
        return GitHubFatal(
            f"GitHub {where} returned {status}: redirected to {location}. Redirects are not "
            "followed, because a moved repo may now belong to someone who never allowlisted it",
            status,
        )
    text = f"GitHub {where} returned {status}: {detail}"
    headers = response.headers
    if status == 429 or (status == 403 and _is_rate_limit(headers, detail)):
        wait = _wait_hint(headers, now, rate_limited=True)
        return GitHubRetryable(text, status, RATE_LIMIT_WAIT_S if wait is None else wait)
    if status == 408 or status >= 500:
        return GitHubRetryable(text, status, _wait_hint(headers, now, rate_limited=False))
    return GitHubFatal(text, status)


def _body_error(response: httpx2.Response, expected: str) -> GitHubRetryable:
    # A captive portal or a broken proxy can answer 200 with a page that is not GitHub's.
    request = response.request
    return GitHubRetryable(
        f"GitHub {request.method} {request.url.path} answered {response.status_code} with a "
        f"body that was not a JSON {expected}",
        response.status_code,
    )


def _json_object(response: httpx2.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise _body_error(response, "object")
    return data


def _json_list(response: httpx2.Response) -> list[dict[str, Any]]:
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, list):
        raise _body_error(response, "list")
    return data


# ---------- sending ----------
def _new_http(
    base_url: str, transport: httpx2.AsyncBaseTransport | None, timeout_s: float
) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url=base_url,
        transport=transport,
        timeout=httpx2.Timeout(timeout_s, connect=min(timeout_s, CONNECT_TIMEOUT_S)),
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        },
        follow_redirects=False,
    )


async def _send(
    http: httpx2.AsyncClient,
    method: str,
    url: str,
    *,
    token: str,
    clock: Clock,
    params: Mapping[str, str | int] | None = None,
    body: Mapping[str, Any] | None = None,
) -> httpx2.Response:
    """Send one request and return the response, or raise GitHubRetryable or GitHubFatal.

    The one place that turns what GitHub does into an error class. The client and the App
    token exchange both send through it, so they fail the same way.
    """
    where = f"{method} {httpx2.URL(url).path}"
    try:
        response = await http.request(
            method, url, params=params, json=body, headers={"Authorization": f"Bearer {token}"}
        )
    except httpx2.HTTPError as exc:
        # `from None`: the transport's own text can quote the Authorization header (httpx2 does
        # for an illegal value), and a chained cause would print that text beside our scrubbed one.
        reason = _redact(f"{type(exc).__name__}: {exc}", token)
        raise GitHubRetryable(f"GitHub {where} failed: {reason}") from None
    if response.is_success:
        return response
    raise _failure(response, where, token, clock())


def repo_path(repo: str) -> str:
    """The API path of a repo, `/repos/owner/name`, with each part escaped for a URL."""
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        raise GitHubFatal(f"{repo!r} is not a GitHub repo written owner/name")
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


def _utc_stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        # A naive time could be read as local time or as UTC, and the answer would differ.
        raise ValueError("a time sent to GitHub must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _origin(url: httpx2.URL) -> tuple[str, str, int | None]:
    return (url.scheme, url.host, url.port)


def _without_query(url: httpx2.URL) -> str:
    return f"{url.scheme}://{url.netloc.decode('ascii')}{url.path}"


# ---------- auth ----------
def _clean_token(raw: str) -> str:
    # A token read from a file or a Docker secret ends in a newline, which is not part of it.
    token = raw.strip()
    if not _TOKEN_SHAPE.fullmatch(token):
        raise ValueError("not a usable GitHub token: it must be printable ASCII with no spaces")
    return token


class GitHubAuth(Protocol):
    """Gives the client the token for its next request. The client asks once per request."""

    async def token(self) -> str: ...


class PatAuth:
    """A personal access token. A fine-grained one is the choice for local use (ADR-0012)."""

    def __init__(self, token: str | SecretStr) -> None:
        # Settings hold tokens as SecretStr. Taking one directly removes a mistake that would
        # otherwise send "Bearer **********" and show up as a 401.
        raw = token.get_secret_value() if isinstance(token, SecretStr) else token
        self._token = SecretStr(_clean_token(raw))

    async def token(self) -> str:
        return self._token.get_secret_value()

    def __repr__(self) -> str:
        return "PatAuth(token=<hidden>)"


@dataclass(frozen=True)
class _CachedToken:
    value: str = field(repr=False)
    refresh_at: datetime


class AppAuth:
    """A GitHub App installation token, minted from the App's private key (ADR-0012).

    The App signs a short JWT with its key, GitHub swaps that for an installation token that
    lasts an hour, and the token is kept until five minutes before it expires. The lock lets
    one caller do the exchange while the others wait for its result.

    `base_url`, `transport` and `clock` are for tests and for GitHub Enterprise. The key is
    read from `key_path` on each exchange, so a rotated key takes effect without a restart.
    """

    def __init__(
        self,
        app_id: int | str,
        key_path: Path | str,
        installation_id: int | str,
        *,
        base_url: str = GITHUB_API_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Clock = _utc_now,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._app_id = app_id
        self._key_path = Path(key_path)
        self._installation_id = installation_id
        self._base_url = base_url
        self._transport = transport
        self._clock = clock
        self._timeout_s = timeout_s
        self._lock = asyncio.Lock()
        self._cached: _CachedToken | None = None

    async def token(self) -> str:
        cached = self._cached
        if cached is not None and self._clock() < cached.refresh_at:
            return cached.value
        async with self._lock:
            cached = self._cached  # a caller that held the lock may have renewed it already
            if cached is not None and self._clock() < cached.refresh_at:
                return cached.value
            self._cached = await self._exchange()
            return self._cached.value

    async def _exchange(self) -> _CachedToken:
        assertion = self._sign_jwt()
        installation = quote(str(self._installation_id), safe="")
        path = f"/app/installations/{installation}/access_tokens"
        async with _new_http(self._base_url, self._transport, self._timeout_s) as http:
            response = await _send(http, "POST", path, token=assertion, clock=self._clock)
        return self._read_token(response)

    def _sign_jwt(self) -> str:
        try:
            pem = self._key_path.read_bytes()
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise GitHubFatal(
                f"cannot read the GitHub App key at {self._key_path}: {reason}"
            ) from None
        issued = int(self._clock().timestamp())
        claims = {
            "iss": str(self._app_id),
            "iat": issued - JWT_BACKDATE_S,
            "exp": issued + JWT_LIFETIME_S,
        }
        try:
            return jwt.encode(claims, pem, algorithm="RS256")
        except (ValueError, TypeError, jwt.PyJWTError, UnsupportedAlgorithm) as exc:
            # Only the class of the failure goes into the message, never anything of the key.
            raise GitHubFatal(
                f"the GitHub App key at {self._key_path} is not a usable RSA private key "
                f"({type(exc).__name__})"
            ) from None

    @staticmethod
    def _read_token(response: httpx2.Response) -> _CachedToken:
        body = _json_object(response)
        token, expires_at = body.get("token"), body.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(expires_at, str):
            raise GitHubRetryable(
                "GitHub's token exchange answered without a token and an expiry time",
                response.status_code,
            )
        try:
            expires = datetime.fromisoformat(expires_at)
        except ValueError:
            raise GitHubRetryable(
                f"GitHub's token exchange sent an expiry time that cannot be read: {expires_at!r}",
                response.status_code,
            ) from None
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return _CachedToken(token, expires - TOKEN_REFRESH_MARGIN)

    def __repr__(self) -> str:
        return f"AppAuth(app_id={self._app_id!r}, installation_id={self._installation_id!r})"


def git_auth_header(token: str) -> list[str]:
    """Arguments for `git` that sign in to github.com without saving the token anywhere.

    They go before the subcommand: `git <these> clone --bare <url> <dir>`, run without a
    shell. The setting lives for that one process, so the token never reaches `.git/config`
    or a remote URL, and it is scoped to github.com, so a redirect to another host gets no
    header. Basic auth with the user name x-access-token works for personal tokens and for
    installation tokens alike. The header is base64, not encryption, and it shows in the
    process list while git runs (ADR-0012).
    """
    login = base64.b64encode(f"x-access-token:{_clean_token(token)}".encode("ascii"))
    return [
        "-c",
        f"http.https://github.com/.extraheader=AUTHORIZATION: basic {login.decode('ascii')}",
    ]


# ---------- the client ----------
class GitHubClient:
    """Reads from GitHub's REST API with one auth, one error mapping and Link pagination.

    `transport` replaces the network, which is how tests mount FakeGitHub. `clock` is read
    when a rate-limit reset time has to become a wait.
    """

    def __init__(
        self,
        base_url: str,
        auth: GitHubAuth,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Clock = _utc_now,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url
        self._auth = auth
        self._clock = clock
        self._http = _new_http(base_url, transport, timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"GitHubClient(base_url={self._base_url!r})"

    # ----- reads: the read side of GitHubGateway, plus what intake and commands need -----

    async def list_labeled_issues(self, repo: str, label: str) -> list[dict[str, Any]]:
        """Open issues that carry `label`, oldest first.

        Oldest first keeps the pages steady while issues are added, and starts tasks in the
        order people filed them.
        """
        params = {"labels": label, "state": "open", "sort": "created", "direction": "asc"}
        items = await self._get_pages(f"{repo_path(repo)}/issues", params)
        # GitHub lists pull requests as issues. A labeled pull request is not a task.
        return [item for item in items if "pull_request" not in item]

    async def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        response = await self._request("GET", f"{repo_path(repo)}/issues/{number}")
        return _json_object(response)

    async def list_comments_since(
        self, repo: str, number: int, since: datetime
    ) -> list[dict[str, Any]]:
        """Comments updated at or after `since`. The edge is inclusive and edits count.

        The caller de-duplicates by comment id, because the newest comment it has seen comes
        back again.
        """
        path = f"{repo_path(repo)}/issues/{number}/comments"
        return await self._get_pages(path, {"since": _utc_stamp(since)})

    async def pr_merged(self, repo: str, pr_number: int) -> bool:
        response = await self._request("GET", f"{repo_path(repo)}/pulls/{pr_number}")
        return _json_object(response).get("merged") is True

    async def get_collaborator_permission(self, repo: str, login: str) -> dict[str, Any]:
        """The user's role on the repo. `permission` is admin, write, read or none.

        A user who is not a collaborator gets "none", not a 404. Commands are honoured only
        for admin and write, which includes the maintain role.
        """
        path = f"{repo_path(repo)}/collaborators/{quote(login, safe='')}/permission"
        return _json_object(await self._request("GET", path))

    # ----- writes: only github/side_effects.py may call these (invariant I4, LH-304) -----
    # That module adds the idempotency key and the search for an earlier attempt. A write from
    # anywhere else would be posted twice when a retry follows a response that never arrived.

    async def _post(self, path: str, body: Mapping[str, Any]) -> Any:
        return await self._write("POST", path, body)

    async def _patch(self, path: str, body: Mapping[str, Any]) -> Any:
        return await self._write("PATCH", path, body)

    async def _delete(self, path: str) -> Any:
        return await self._write("DELETE", path, None)

    async def _write(self, method: str, path: str, body: Mapping[str, Any] | None) -> Any:
        response = await self._request(method, path, body=body)
        if not response.content:  # 204 and friends
            return None
        try:
            return response.json()
        except ValueError:
            raise _body_error(response, "document") from None

    # ----- plumbing -----

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> httpx2.Response:
        """Send one authenticated request. Failures come out as GitHubRetryable or GitHubFatal."""
        token = await self._auth.token()
        return await _send(
            self._http, method, url, token=token, clock=self._clock, params=params, body=body
        )

    async def _get_pages(self, path: str, params: Mapping[str, str | int]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        url = path
        query: Mapping[str, str | int] | None = {**params, "per_page": PAGE_SIZE}
        for _ in range(MAX_PAGES):
            response = await self._request("GET", url, params=query)
            items.extend(_json_list(response))
            following = self._next_page(response)
            if following is None:
                return items
            url, query = following, None  # the link already carries the whole query
        raise GitHubFatal(
            f"GitHub still offered a next page after {MAX_PAGES} pages of {path}, so it is "
            "probably looping; stopping"
        )

    def _next_page(self, response: httpx2.Response) -> str | None:
        link = response.links.get("next")
        if link is None:
            return None
        base = self._http.base_url
        target = base.join(link["url"])
        if _origin(target) != _origin(base):
            raise GitHubFatal(
                f"GitHub sent a next-page link outside {_without_query(base)}: "
                f"{_without_query(target)}. It is not followed, because the request would "
                "carry the token"
            )
        return str(target)
