"""Idempotent GitHub writes: comments, labels and pull requests (LH-304).

A write can land on GitHub and still look like a failure to us. GitHub stores a comment and
then answers 502, or the reply gets lost on the way. Temporal runs the activity again, and a
plain retry would post the comment a second time (failure modes F10 and F11). Every write here
takes an idempotency key (`effect_key`, plan section 3.7) and runs three steps:

1. A row for the key in the `side_effects` ledger ends the call. The row's `external_id` is
   the answer.
2. Otherwise GitHub is searched for the earlier attempt. A comment carries the hidden marker
   `<!-- longhaul:key=KEY -->`, so it is found even if the ledger was lost or rolled back. A
   pull request is found by its head branch, because Longhaul uses one head branch per task.
   What is found is recorded in the ledger and returned.
3. Otherwise the write happens, the row is recorded, and the id is returned.

Labels are idempotent on GitHub already, so they skip step 2 and keep a row for the audit
trail. Removing a label that is not on the issue is a 404 from GitHub. It counts as done when
the issue exists and no longer has the label.

The same key is held from step 1 to the row. A timed-out attempt can still be running when
Temporal starts the next one, and both would otherwise search before either has posted. With
Postgres the hold is an advisory lock that lasts for the transaction, so it is let go if the
worker dies or the attempt is cancelled.

A key names one effect. Its row keeps a hash of the payload, and a second use of the key with
other text raises KeyReuseError instead of returning the first effect's answer without a word.
So a retry must send the same request: an activity that renders a body from a template has to
render the same text on every attempt, with no clock reading in it.

The plain async functions (`post_comment`, `add_label`, `remove_label`, `create_pr`) are for
code that already runs in an activity, such as the poller and `deliver`. `SideEffectActivities`
wraps them for a Temporal worker. The wrapper turns GitHubFatal and every error that retrying
cannot fix into a non-retryable ApplicationError, and GitHubRetryable into a retryable one that
carries GitHub's wait as the delay before the next attempt. Anything else, such as a lost
database connection, keeps its own type and gets Temporal's default: retry.

Timeouts and retry policies are not set here. Plan section 3.7 gives each name two minutes and
ten attempts, and they belong in acts.py with the workflow that calls the activities. A comment
or pull request body travels inside the activity input, which invariant I2 caps at 256 KB. A
text that may pass 16 KB (CLAUDE.md rule 5) is built inside an activity that calls the plain
functions, as the plan has `deliver` do with the pull request body (LH-306).

Only this module calls the client's write methods (invariant I4,
tests/unit/test_invariants.py::test_github_writes_only_in_side_effects).

The marker is not proof of authorship. Anyone who can comment can copy it, and then Longhaul
finds that comment and posts none of its own. The comment API does not say which login is ours
for an installation token, and a missing status comment does less harm than a duplicate.
"""

import asyncio
import hashlib
import json
import logging
import re
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Protocol, Self, cast
from urllib.parse import quote

from pydantic import BaseModel
from sqlalchemy import Row, Select, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from longhaul.db.models import SideEffect
from longhaul.durable.payload_guard import PayloadTooLargeError, assert_small
from longhaul.github.client import GitHubClient, GitHubFatal, GitHubRetryable, repo_path

__all__ = [
    "MARKER_PREFIX",
    "CommentInput",
    "Effect",
    "EffectKind",
    "InvalidKeyError",
    "KeyReuseError",
    "LabelInput",
    "Ledger",
    "LedgerEntry",
    "MemoryLedger",
    "PullRequestInput",
    "SideEffectActivities",
    "SideEffectError",
    "SqlLedger",
    "Wanted",
    "add_label",
    "create_pr",
    "effect_key",
    "has_marker",
    "marker",
    "payload_hash",
    "post_comment",
    "remove_label",
    "run_once",
]

log = logging.getLogger(__name__)

MARKER_PREFIX = "<!-- longhaul:key="
MARKER_SUFFIX = " -->"
# A key is three parts joined by colons. Its characters can sit inside an HTML comment without
# ending it, and none of them is a colon, so the parts cannot be mixed up.
MAX_KEY_CHARS = 200
_PART = re.compile(r"[A-Za-z0-9._-]+")
_KEY = re.compile(r"[A-Za-z0-9._-]+:[A-Za-z0-9._-]+:[0-9]+")
# Error text ends up in the workflow history, so a value that an error quotes is cut.
MAX_SHOWN_CHARS = 60

EffectKind = Literal["comment", "label_add", "label_remove", "pr", "push"]
Finder = Callable[[], Awaitable[str | None]]
Writer = Callable[[], Awaitable[str]]


# ---------- errors ----------
class SideEffectError(ValueError):
    """The request is wrong in a way that retrying cannot fix."""


class InvalidKeyError(SideEffectError):
    """The idempotency key is not written workflow_id:purpose:seq."""


class KeyReuseError(SideEffectError):
    """The key was used before for a different effect."""


# ---------- keys, markers and hashes ----------
def _shown(value: str) -> str:
    return repr(value[:MAX_SHOWN_CHARS])


def _check_key(key: str) -> None:
    if len(key) > MAX_KEY_CHARS or not _KEY.fullmatch(key):
        raise InvalidKeyError(
            f"{_shown(key)} is not an idempotency key. Write workflow_id:purpose:seq with "
            "letters, digits, dot, underscore or hyphen in the first two parts and a number "
            f"in the last, at most {MAX_KEY_CHARS} characters"
        )


def effect_key(workflow_id: str, purpose: str, seq: int) -> str:
    """The idempotency key `{workflow_id}:{purpose}:{seq}` of plan section 3.7.

    For example `lh-acme-calc-42:plan_comment:1`. A second effect of the same purpose in one
    workflow takes the next sequence number.
    """
    if isinstance(seq, bool) or seq < 0:
        raise InvalidKeyError(f"the sequence number of a key is 0 or more, not {seq!r}")
    for name, part in (("workflow id", workflow_id), ("purpose", purpose)):
        if not _PART.fullmatch(part):
            raise InvalidKeyError(
                f"the {name} of a key has letters, digits, dot, underscore or hyphen, "
                f"not {_shown(part)}"
            )
    key = f"{workflow_id}:{purpose}:{seq}"
    _check_key(key)
    return key


def marker(key: str) -> str:
    """The hidden comment that a Longhaul comment ends with, so that a retry can find it."""
    return f"{MARKER_PREFIX}{key}{MARKER_SUFFIX}"


def has_marker(body: str) -> bool:
    """True if the text carries a marker of any key, which is what a Longhaul comment does."""
    return MARKER_PREFIX in body


def payload_hash(kind: str, payload: Mapping[str, Any]) -> str:
    """A hash of what an effect does, kept in its ledger row.

    The recipe is fixed: a row written by one release is compared with the hash made by the
    next, and a changed recipe would make every key still in use look like a reused one.
    """
    canonical = json.dumps(
        {"kind": kind, **payload}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------- the ledger ----------
@dataclass(frozen=True)
class Effect:
    """A row of the ledger: what was done for a key."""

    key: str
    task_id: str
    kind: EffectKind
    # A comment id, a pull request URL or a label name. A pushed commit id, for a push.
    external_id: str
    payload_hash: str


@dataclass(frozen=True)
class Wanted:
    """An effect before it happens: its key, its task, and the payload that the hash covers."""

    key: str
    task_id: str
    kind: EffectKind
    payload: Mapping[str, Any]


class LedgerEntry(Protocol):
    """The ledger while one key is held."""

    async def get(self, key: str) -> Effect | None: ...

    async def record(self, effect: Effect) -> Effect:
        """Store the row and return the stored one. If the key has a row, that one stays."""
        ...


class Ledger(Protocol):
    def locked(self, key: str) -> AbstractAsyncContextManager[LedgerEntry]:
        """Hold the key until the block ends. Other holders of the same key wait."""
        ...


class MemoryLedger:
    """The ledger in a dict, for tests that run without a database. `rows` is public."""

    def __init__(self) -> None:
        self.rows: dict[str, Effect] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @asynccontextmanager
    async def locked(self, key: str) -> AsyncIterator[LedgerEntry]:
        async with self._locks[key]:
            yield self

    async def get(self, key: str) -> Effect | None:
        return self.rows.get(key)

    async def record(self, effect: Effect) -> Effect:
        return self.rows.setdefault(effect.key, effect)


# One name for all advisory locks of this module, so another user of advisory locks in the
# same database cannot collide with a key by accident.
_LOCK_NAMESPACE = "longhaul.side_effects:"
_LOCK = text("SELECT pg_advisory_xact_lock(CAST(:id AS bigint))")


def _lock_id(key: str) -> int:
    """A signed 64-bit number for the key. Two keys that share one only wait for each other."""
    digest = hashlib.blake2b(f"{_LOCK_NAMESPACE}{key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _by_key(key: str) -> Select[str, str, str, str, str]:
    return select(
        SideEffect.key,
        SideEffect.task_id,
        SideEffect.kind,
        SideEffect.external_id,
        SideEffect.payload_hash,
    ).where(SideEffect.key == key)


def _effect(row: Row[str, str, str, str, str]) -> Effect:
    key, task_id, kind, external_id, digest = row
    # The table's CHECK constraint allows only the kinds of EffectKind.
    return Effect(key, task_id, cast(EffectKind, kind), external_id, digest)


class _SqlEntry:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> Effect | None:
        row = (await self._session.execute(_by_key(key))).one_or_none()
        return None if row is None else _effect(row)

    async def record(self, effect: Effect) -> Effect:
        # DO NOTHING keeps the first row when two writers race, and the read after it returns
        # whichever row won.
        insert_row = (
            insert(SideEffect)
            .values(
                key=effect.key,
                task_id=effect.task_id,
                kind=effect.kind,
                external_id=effect.external_id,
                payload_hash=effect.payload_hash,
            )
            .on_conflict_do_nothing(index_elements=[SideEffect.key])
        )
        await self._session.execute(insert_row)
        return _effect((await self._session.execute(_by_key(effect.key))).one())


class SqlLedger:
    """The ledger in the `side_effects` table.

    `locked` opens one transaction, takes a Postgres advisory lock on the key inside it, and
    reads and writes the row in the same transaction. The block therefore uses one connection
    from the pool, however long the GitHub call inside it takes. A server that closes sessions
    left idle in a transaction must allow for the activity timeout of a couple of minutes.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: AsyncEngine) -> Self:
        return cls(async_sessionmaker(engine, expire_on_commit=False))

    @asynccontextmanager
    async def locked(self, key: str) -> AsyncIterator[LedgerEntry]:
        async with self._sessions.begin() as session:
            await session.execute(_LOCK, {"id": _lock_id(key)})
            yield _SqlEntry(session)


# ---------- the three steps ----------
def _same_effect(stored: Effect, wanted: Wanted, digest: str) -> str:
    if stored.payload_hash != digest:
        raise KeyReuseError(
            f"idempotency key {_shown(wanted.key)} was already used for a different "
            f"{stored.kind}. A key names one effect, so another effect needs the next "
            "sequence number"
        )
    return stored.external_id


async def run_once(
    ledger: Ledger, wanted: Wanted, *, write: Writer, find: Finder | None = None
) -> str:
    """Do the effect once, however often this is called, and return its external id.

    The three steps of the module docstring. `find` looks for the earlier attempt on GitHub
    and returns its id, or None. `write` does the write and returns the new id. A failure of
    either leaves no row, so the next call starts again at step 1.
    """
    _check_key(wanted.key)
    digest = payload_hash(wanted.kind, wanted.payload)
    async with ledger.locked(wanted.key) as entry:
        stored = await entry.get(wanted.key)
        if stored is not None:
            return _same_effect(stored, wanted, digest)
        external_id = await find() if find is not None else None
        if external_id is None:
            external_id = await write()
        else:
            log.info(
                "%s: found the earlier attempt on GitHub (%s) and recorded it",
                wanted.key,
                external_id,
            )
        done = Effect(wanted.key, wanted.task_id, wanted.kind, external_id, digest)
        return (await entry.record(done)).external_id


# ---------- the four writes ----------
class CommentInput(BaseModel):
    task_id: str
    repo: str
    number: int  # an issue or a pull request
    body: str
    idempotency_key: str


class LabelInput(BaseModel):
    task_id: str
    repo: str
    number: int
    label: str
    idempotency_key: str


class PullRequestInput(BaseModel):
    task_id: str
    repo: str
    head: str  # a branch of the repo, or owner:branch
    base: str
    title: str
    body: str
    draft: bool = True  # until a verifier has passed the work (Phase 5)
    idempotency_key: str


def _need(value: str, message: str) -> None:
    if not value.strip():
        raise SideEffectError(message)


def _issue_path(repo: str, number: int) -> str:
    return f"{repo_path(repo)}/issues/{number}"


def _identifier(item: object, field: str) -> str:
    """The `field` of what GitHub sent back for a write, as text.

    A reply without it is retryable: the write may have landed, and the retry looks first.
    """
    value = item.get(field) if isinstance(item, dict) else None
    if value is None or value == "":
        raise GitHubRetryable(
            f"GitHub answered a write without {field}, so it is not known whether it landed"
        )
    return str(value)


async def post_comment(client: GitHubClient, ledger: Ledger, request: CommentInput) -> str:
    """Post a comment once and return its id. The text gets the hidden marker after it."""
    _need(request.body, "a comment needs text")
    path = f"{_issue_path(request.repo, request.number)}/comments"
    tag = marker(request.idempotency_key)

    async def find() -> str | None:
        for item in await client.list_comments(request.repo, request.number):
            if tag in (item.get("body") or ""):
                return _identifier(item, "id")
        return None

    async def write() -> str:
        created = await client._post(path, {"body": f"{request.body.rstrip()}\n\n{tag}"})
        return _identifier(created, "id")

    payload = {"repo": request.repo, "number": request.number, "body": request.body}
    wanted = Wanted(request.idempotency_key, request.task_id, "comment", payload)
    return await run_once(ledger, wanted, find=find, write=write)


async def add_label(client: GitHubClient, ledger: Ledger, request: LabelInput) -> str:
    """Put a label on an issue and return its name.

    GitHub keeps one of each label on an issue, so a repeat changes nothing there.
    """
    _need(request.label, "a label needs a name")
    path = f"{_issue_path(request.repo, request.number)}/labels"

    async def write() -> str:
        await client._post(path, {"labels": [request.label]})
        return request.label

    payload = {"repo": request.repo, "number": request.number, "label": request.label}
    wanted = Wanted(request.idempotency_key, request.task_id, "label_add", payload)
    return await run_once(ledger, wanted, write=write)


def _label_name(item: object) -> str:
    # GitHub sends label objects. A bare string is accepted too, as it is in the request.
    return str(item.get("name", "") if isinstance(item, dict) else item)


async def _label_is_gone(client: GitHubClient, request: LabelInput) -> bool:
    try:
        issue = await client.get_issue(request.repo, request.number)
    except GitHubFatal:
        return False  # the 404 was about the issue or the repo, not about the label
    names = {_label_name(item).lower() for item in issue.get("labels") or []}
    return request.label.lower() not in names  # GitHub ignores case in label names


async def remove_label(client: GitHubClient, ledger: Ledger, request: LabelInput) -> str:
    """Take a label off an issue and return its name. A label that is not there is done."""
    _need(request.label, "a label needs a name")
    path = f"{_issue_path(request.repo, request.number)}/labels/{quote(request.label, safe='')}"

    async def write() -> str:
        try:
            await client._delete(path)
        except GitHubFatal as exc:
            # An earlier attempt may have removed it and lost the reply. GitHub then answers
            # 404 to the repeat, which is the state that was wanted. Any other 404 is real.
            if exc.status != 404 or not await _label_is_gone(client, request):
                raise
        return request.label

    payload = {"repo": request.repo, "number": request.number, "label": request.label}
    wanted = Wanted(request.idempotency_key, request.task_id, "label_remove", payload)
    return await run_once(ledger, wanted, write=write)


async def create_pr(client: GitHubClient, ledger: Ledger, request: PullRequestInput) -> str:
    """Open a pull request once and return its URL.

    An open pull request from the same head, made by an earlier attempt or by a person, is
    returned instead of a second one. If there are several, the oldest.
    """
    _need(request.head, "a pull request needs a head branch")
    _need(request.base, "a pull request needs a base branch")
    path = f"{repo_path(request.repo)}/pulls"
    fields = {
        "title": request.title,
        "head": request.head,
        "base": request.base,
        "body": request.body,
        "draft": request.draft,
    }

    async def find() -> str | None:
        pulls = await client.list_open_pulls_by_head(request.repo, request.head)
        if not pulls:
            return None
        return _identifier(min(pulls, key=lambda pull: pull.get("number", 0)), "html_url")

    async def write() -> str:
        return _identifier(await client._post(path, fields), "html_url")

    wanted = Wanted(
        request.idempotency_key, request.task_id, "pr", {"repo": request.repo, **fields}
    )
    return await run_once(ledger, wanted, find=find, write=write)


# ---------- the activities ----------
def _retry_delay(error: GitHubRetryable) -> timedelta | None:
    wait = error.retry_after_s
    return None if wait is None else timedelta(seconds=wait)


async def _guarded(inp: BaseModel, run: Callable[[], Awaitable[str]]) -> str:
    """Run an activity body and raise what Temporal needs to decide about a retry.

    The input is held to the 256 KB of invariant I2 first. Retrying cannot shrink a payload,
    and acts.py lists PayloadTooLargeError as non-retryable by this type name.
    """
    try:
        assert_small(inp)
        result = await run()
        assert_small(result)
        return result
    except GitHubFatal as exc:
        raise ApplicationError(str(exc), type="GitHubFatal", non_retryable=True) from None
    except GitHubRetryable as exc:
        raise ApplicationError(
            str(exc), type="GitHubRetryable", next_retry_delay=_retry_delay(exc)
        ) from None
    except (SideEffectError, PayloadTooLargeError) as exc:
        raise ApplicationError(str(exc), type=type(exc).__name__, non_retryable=True) from None


class SideEffectActivities:
    """The four writes as Temporal activities, sharing a client and a ledger.

    Build one on the worker and register `activities`. The names are those of plan section 3.7.
    Each method only guards and delegates, so the behaviour is in the plain functions above.
    """

    def __init__(self, client: GitHubClient, ledger: Ledger) -> None:
        self._client = client
        self._ledger = ledger

    @property
    def activities(self) -> list[Callable[..., Awaitable[str]]]:
        return [self.post_comment, self.add_label, self.remove_label, self.create_pr]

    @activity.defn(name="post_comment")
    async def post_comment(self, inp: CommentInput) -> str:
        return await _guarded(inp, lambda: post_comment(self._client, self._ledger, inp))

    @activity.defn(name="add_label")
    async def add_label(self, inp: LabelInput) -> str:
        return await _guarded(inp, lambda: add_label(self._client, self._ledger, inp))

    @activity.defn(name="remove_label")
    async def remove_label(self, inp: LabelInput) -> str:
        return await _guarded(inp, lambda: remove_label(self._client, self._ledger, inp))

    @activity.defn(name="create_pr")
    async def create_pr(self, inp: PullRequestInput) -> str:
        return await _guarded(inp, lambda: create_pr(self._client, self._ledger, inp))
