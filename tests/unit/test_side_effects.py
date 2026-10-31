"""Idempotent GitHub writes (LH-304), against FakeGitHub and an in-memory ledger.

Everything here runs without a database. tests/integration/test_github_side_effects.py runs
the story's three cases again on Postgres, through the activities and a Temporal worker.

The cases follow the three steps of the story: a ledger row ends the call, otherwise GitHub is
searched for the earlier attempt (the hidden marker in a comment, an open pull request from the
same head), otherwise the write happens. FakeGitHub's `after_write_fail` makes the write land
and answers 502, which is the case the whole module exists for.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any, get_args

import httpx2
import pytest
from sqlalchemy import CheckConstraint
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from tests.fakes.fake_github import FAKE_BASE_URL, FakeGitHub

from longhaul.db.models import SideEffect
from longhaul.github.client import GitHubClient, GitHubFatal, GitHubRetryable, PatAuth
from longhaul.github.side_effects import (
    MARKER_PREFIX,
    CommentInput,
    Effect,
    EffectKind,
    InvalidKeyError,
    KeyReuseError,
    LabelInput,
    Ledger,
    MemoryLedger,
    PullRequestInput,
    SideEffectActivities,
    SideEffectError,
    Wanted,
    _lock_id,
    add_label,
    create_pr,
    effect_key,
    has_marker,
    marker,
    payload_hash,
    post_comment,
    remove_label,
    run_once,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase3]

REPO = "acme/calc"
TASK = "lh-acme-calc-1"
TOKEN = "ghp_unit_test_token_0123456789abcdef"
KEY = effect_key(TASK, "picked_up", 1)
LABEL = "longhaul:working"
HEAD = "longhaul-pr/1"


# --- fixtures and small builders --------------------------------------------------------------


@pytest.fixture
def fake() -> FakeGitHub:
    github = FakeGitHub()
    github.seed_repo(REPO)
    github.seed_issue(REPO, "add() is wrong")  # issue 1
    return github


def client_on(transport: httpx2.AsyncBaseTransport) -> GitHubClient:
    return GitHubClient(FAKE_BASE_URL, PatAuth(TOKEN), transport=transport)


@pytest.fixture
async def gh(fake: FakeGitHub) -> AsyncIterator[GitHubClient]:
    async with client_on(fake.transport()) as client:
        yield client


@pytest.fixture
def ledger() -> MemoryLedger:
    return MemoryLedger()


def comment(body: str = "Picked up", key: str = KEY, number: int = 1) -> CommentInput:
    return CommentInput(task_id=TASK, repo=REPO, number=number, body=body, idempotency_key=key)


def label(name: str = LABEL, key: str | None = None, number: int = 1) -> LabelInput:
    key = key or effect_key(TASK, "label", 1)
    return LabelInput(task_id=TASK, repo=REPO, number=number, label=name, idempotency_key=key)


def pull(head: str = HEAD, **changes: Any) -> PullRequestInput:
    fields: dict[str, Any] = {
        "task_id": TASK,
        "repo": REPO,
        "head": head,
        "base": "main",
        "title": "fix(calc): add returns the sum",
        "body": "Closes #1",
        "draft": True,
        "idempotency_key": effect_key(TASK, "pr", 1),
    }
    return PullRequestInput(**{**fields, **changes})


class SlowTransport(httpx2.AsyncBaseTransport):
    """Delays every request, so that callers who overlap in time really overlap.

    `peak` is the most requests that were in flight at once.
    """

    def __init__(self, inner: httpx2.AsyncBaseTransport, delay_s: float = 0.02) -> None:
        self._inner = inner
        self._delay_s = delay_s
        self._in_flight = 0
        self.peak = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self._in_flight += 1
        self.peak = max(self.peak, self._in_flight)
        try:
            await asyncio.sleep(self._delay_s)
            return await self._inner.handle_async_request(request)
        finally:
            self._in_flight -= 1


# --- the key and the marker -------------------------------------------------------------------


def test_effect_key_follows_the_format_of_section_3_7() -> None:
    assert effect_key("lh-acme-calc-42", "plan_comment", 1) == "lh-acme-calc-42:plan_comment:1"
    # A retry attempt of a task has its own workflow id, so its keys are its own.
    assert effect_key("lh-acme-calc-42-a2", "picked.up", 0) == "lh-acme-calc-42-a2:picked.up:0"


@pytest.mark.parametrize(
    ("workflow_id", "purpose", "seq", "complaint"),
    [
        ("", "plan_comment", 1, "workflow id"),
        ("lh-acme-calc-42", "", 1, "purpose"),
        ("lh:acme", "plan_comment", 1, "workflow id"),
        ("lh-acme-calc-42", "plan:comment", 1, "purpose"),
        ("lh acme", "plan_comment", 1, "workflow id"),
        ("lh-acme-calc-42", "plan\ncomment", 1, "purpose"),
        ("lh-acme-calc-42", "plan-->", 1, "purpose"),  # would end the hidden comment early
        ("lh-acme-calc-42", "plan_comment", -1, "sequence number"),
        ("lh-acme-calc-42", "plan_comment", True, "sequence number"),  # a bool is not a count
        ("lh-acmé-calc-42", "plan_comment", 1, "workflow id"),
        ("w" * 200, "plan_comment", 1, "at most 200"),
    ],
)
def test_effect_key_refuses_parts_that_break_the_format(
    workflow_id: str, purpose: str, seq: int, complaint: str
) -> None:
    with pytest.raises(InvalidKeyError, match=complaint):
        effect_key(workflow_id, purpose, seq)


def test_an_error_quotes_only_the_start_of_a_long_value() -> None:
    # The text of an error is stored in the workflow history.
    with pytest.raises(InvalidKeyError) as failed:
        effect_key("w" * 5000, "plan_comment", 1)
    assert len(str(failed.value)) < 500


def test_the_errors_are_value_errors_with_one_base() -> None:
    assert issubclass(InvalidKeyError, SideEffectError)
    assert issubclass(KeyReuseError, SideEffectError)
    assert issubclass(SideEffectError, ValueError)


def test_the_marker_is_the_hidden_comment_of_section_3_7() -> None:
    assert marker(KEY) == "<!-- longhaul:key=lh-acme-calc-1:picked_up:1 -->"
    assert marker(KEY).startswith(MARKER_PREFIX)
    assert has_marker(f"Picked up\n\n{marker(KEY)}")
    assert not has_marker("Picked up")
    assert not has_marker("a human wrote longhaul:key=x")


def test_the_ledger_kinds_are_the_kinds_the_table_allows() -> None:
    [check] = [c for c in SideEffect.__table__.constraints if isinstance(c, CheckConstraint)]
    assert set(get_args(EffectKind)) == set(re.findall(r"'(\w+)'", str(check.sqltext)))


def test_payload_hash_pins_the_recipe_that_the_ledger_rows_were_made_with() -> None:
    # A row written by one release is compared with the hash of the next. Changing the recipe
    # would make every key that is still in use look like a reused one.
    canonical = '{"body":"hi","kind":"comment","number":1,"repo":"acme/calc"}'
    expected = hashlib.sha256(canonical.encode()).hexdigest()
    assert payload_hash("comment", {"repo": "acme/calc", "number": 1, "body": "hi"}) == expected
    assert payload_hash("comment", {"body": "hi", "number": 1, "repo": "acme/calc"}) == expected


def test_payload_hash_changes_with_the_kind_and_every_value() -> None:
    base = payload_hash("comment", {"repo": REPO, "number": 1, "body": "hi"})
    assert len(base) == 64
    assert payload_hash("pr", {"repo": REPO, "number": 1, "body": "hi"}) != base
    assert payload_hash("comment", {"repo": REPO, "number": 2, "body": "hi"}) != base
    assert payload_hash("comment", {"repo": REPO, "number": 1, "body": "ho"}) != base
    assert payload_hash("comment", {"repo": REPO, "number": 1, "body": "hé"}) != base


def test_the_lock_id_is_a_stable_signed_64_bit_number() -> None:
    assert _lock_id(KEY) == _lock_id(KEY)
    assert _lock_id(KEY) != _lock_id(effect_key(TASK, "picked_up", 2))
    ids = [_lock_id(effect_key(TASK, "p", n)) for n in range(500)]
    assert all(-(2**63) <= n < 2**63 for n in ids)
    assert any(n < 0 for n in ids), "the sign must be allowed, bigint is signed"


# --- run_once: the three steps, without GitHub ------------------------------------------------


class Steps:
    """A finder and a writer that count their calls."""

    def __init__(self, found: str | None = None, written: str = "w-1") -> None:
        self.found, self.written = found, written
        self.calls: list[str] = []

    async def find(self) -> str | None:
        self.calls.append("find")
        return self.found

    async def write(self) -> str:
        self.calls.append("write")
        return self.written


def wanted(payload: dict[str, Any] | None = None, key: str = KEY) -> Wanted:
    return Wanted(key, TASK, "comment", payload or {"body": "hi"})


async def test_run_once_writes_and_records_when_nothing_is_known(ledger: MemoryLedger) -> None:
    steps = Steps()

    assert await run_once(ledger, wanted(), find=steps.find, write=steps.write) == "w-1"

    assert steps.calls == ["find", "write"]
    row = ledger.rows[KEY]
    assert (row.task_id, row.kind, row.external_id) == (TASK, "comment", "w-1")
    assert row.payload_hash == payload_hash("comment", {"body": "hi"})


async def test_run_once_takes_the_earlier_attempt_instead_of_writing(
    ledger: MemoryLedger, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="longhaul.github.side_effects")
    steps = Steps(found="earlier-7")

    assert await run_once(ledger, wanted(), find=steps.find, write=steps.write) == "earlier-7"

    assert steps.calls == ["find"]
    assert ledger.rows[KEY].external_id == "earlier-7"
    assert KEY in caplog.text and "earlier" in caplog.text


async def test_run_once_ends_at_the_ledger_row(ledger: MemoryLedger) -> None:
    first = Steps()
    await run_once(ledger, wanted(), find=first.find, write=first.write)
    again = Steps(found="other", written="w-2")

    assert await run_once(ledger, wanted(), find=again.find, write=again.write) == "w-1"

    assert again.calls == []  # neither GitHub search nor write


async def test_run_once_needs_no_finder(ledger: MemoryLedger) -> None:
    steps = Steps()
    assert await run_once(ledger, wanted(), write=steps.write) == "w-1"
    assert steps.calls == ["write"]


async def test_run_once_records_nothing_when_the_write_fails(ledger: MemoryLedger) -> None:
    async def write() -> str:
        raise GitHubRetryable("GitHub POST /x returned 502: Bad Gateway", 502)

    with pytest.raises(GitHubRetryable):
        await run_once(ledger, wanted(), write=write)

    assert ledger.rows == {}


async def test_run_once_refuses_a_key_that_names_another_effect(ledger: MemoryLedger) -> None:
    steps = Steps()
    await run_once(ledger, wanted({"body": "hi"}), write=steps.write)

    with pytest.raises(KeyReuseError, match=KEY):
        await run_once(ledger, wanted({"body": "something else"}), write=steps.write)
    other_kind = Wanted(KEY, TASK, "label_add", {"body": "hi"})
    with pytest.raises(KeyReuseError, match="comment"):
        await run_once(ledger, other_kind, write=steps.write)

    assert steps.calls == ["write"]


async def test_run_once_checks_the_key_before_it_does_anything(ledger: MemoryLedger) -> None:
    steps = Steps()
    with pytest.raises(InvalidKeyError):
        await run_once(ledger, wanted(key="not a key"), find=steps.find, write=steps.write)
    assert steps.calls == [] and ledger.rows == {}


async def test_the_memory_ledger_keeps_the_first_row_of_a_key(ledger: MemoryLedger) -> None:
    first = Effect("k:p:1", TASK, "comment", "1", "a" * 64)
    second = Effect("k:p:1", TASK, "comment", "2", "a" * 64)
    async with ledger.locked("k:p:1") as entry:
        assert await entry.get("k:p:1") is None
        assert await entry.record(first) == first
        assert await entry.record(second) == first  # the first writer wins
        assert await entry.get("k:p:1") == first


# --- post_comment -----------------------------------------------------------------------------


async def test_post_comment_writes_once_and_appends_the_marker(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    first = await post_comment(gh, ledger, comment("Picked up  \n"))

    # The text keeps its own words, then a blank line, then the hidden marker.
    assert fake.comment_bodies(REPO, 1) == [f"Picked up\n\n{marker(KEY)}"]
    row = ledger.rows[KEY]
    assert (row.task_id, row.kind, row.external_id) == (TASK, "comment", first)
    assert first.isdigit()

    before = len(fake.requests)
    assert await post_comment(gh, ledger, comment("Picked up  \n")) == first
    assert len(fake.requests) == before, "a ledger row ends the call before any request"
    assert fake.count_comments(REPO, 1) == 1


async def test_post_comment_finds_its_comment_when_the_ledger_is_lost(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="longhaul.github.side_effects")
    first = await post_comment(gh, ledger, comment())
    ledger.rows.clear()  # the database was lost or rolled back
    before = len(fake.requests)

    assert await post_comment(gh, ledger, comment()) == first

    assert fake.count_comments(REPO, 1) == 1
    assert [(r.method, r.path.rsplit("/", 1)[1]) for r in fake.requests[before:]] == [
        ("GET", "comments")
    ]
    assert ledger.rows[KEY].external_id == first, "the row is back for the next retry"
    assert "earlier" in caplog.text


async def test_a_502_after_the_write_landed_does_not_post_twice(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    fake.after_write_fail(r"/issues/1/comments$")

    with pytest.raises(GitHubRetryable) as failed:
        await post_comment(gh, ledger, comment())
    assert failed.value.status == 502
    assert fake.count_comments(REPO, 1) == 1, "the comment landed although the answer was a 502"
    assert ledger.rows == {}

    retried = await post_comment(gh, ledger, comment())

    assert fake.count_comments(REPO, 1) == 1
    assert ledger.rows[KEY].external_id == retried
    [posted] = [r for r in fake.requests if r.method == "POST"]
    assert posted.status == 502, "the retry searched and found the comment, so it never posted"


async def test_a_failure_before_the_write_is_written_by_the_retry(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    fake.fail_next(r"/comments$", 503, method="POST")

    with pytest.raises(GitHubRetryable):
        await post_comment(gh, ledger, comment())
    assert fake.count_comments(REPO, 1) == 0

    await post_comment(gh, ledger, comment())
    assert fake.count_comments(REPO, 1) == 1


async def test_the_marker_search_reads_every_page_of_comments(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    for n in range(119):
        fake.seed_comment(REPO, 1, f"talk {n}")
    earlier = fake.seed_comment(REPO, 1, f"Picked up\n\n{marker(KEY)}", user="longhaul-bot")
    fake.seed_comment(REPO, 1, "more talk")  # the marker comment sits at 120 of 121, page 2

    assert await post_comment(gh, ledger, comment()) == str(earlier)

    assert fake.count_comments(REPO, 1) == 121
    assert not [r for r in fake.requests if r.method == "POST"]


async def test_the_first_of_two_marker_comments_is_the_one_that_counts(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    older = fake.seed_comment(REPO, 1, f"one\n\n{marker(KEY)}")
    fake.seed_comment(REPO, 1, f"two\n\n{marker(KEY)}")
    assert await post_comment(gh, ledger, comment()) == str(older)


async def test_a_key_that_starts_like_another_key_does_not_match_its_marker(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    ten = effect_key(TASK, "picked_up", 10)
    fake.seed_comment(REPO, 1, f"Picked up\n\n{marker(ten)}")

    one = await post_comment(gh, ledger, comment(key=KEY))

    assert fake.count_comments(REPO, 1) == 2, "key ...:1 is not found inside the marker of ...:10"
    assert one.isdigit()
    assert marker(KEY) in fake.comment_bodies(REPO, 1)[1]


async def test_a_comment_without_a_body_in_the_listing_is_skipped(ledger: MemoryLedger) -> None:
    def listing(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(200, json=[{"id": 1, "body": None}, {"id": 2}])
        return httpx2.Response(201, json={"id": 3})

    async with client_on(httpx2.MockTransport(listing)) as client:
        assert await post_comment(client, ledger, comment()) == "3"


async def test_non_ascii_text_survives_and_hashes_the_same_every_time(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    first = await post_comment(gh, ledger, comment("café ✓"))
    assert await post_comment(gh, ledger, comment("café ✓")) == first
    assert fake.comment_bodies(REPO, 1)[0].startswith("café ✓\n\n")


async def test_an_empty_comment_is_refused_before_anything_happens(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    # With the marker appended an empty text would still be accepted, and the result would be
    # a comment that shows nothing.
    for body in ("", "   \n\t"):
        with pytest.raises(SideEffectError, match="text"):
            await post_comment(gh, ledger, comment(body))
    assert fake.requests == [] and ledger.rows == {}


async def test_a_bad_key_is_refused_before_anything_happens(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    for key in ("", "plain", "lh-acme-calc-1:picked_up", "a b:c:1", "x:y:1 --> <script>"):
        with pytest.raises(InvalidKeyError):
            await post_comment(gh, ledger, comment(key=key))
    assert fake.requests == [] and ledger.rows == {}


async def test_the_same_key_for_another_comment_is_refused(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    await post_comment(gh, ledger, comment("Picked up"))

    with pytest.raises(KeyReuseError, match="different"):
        await post_comment(gh, ledger, comment("Plan ready"))

    assert fake.count_comments(REPO, 1) == 1
    assert fake.comment_bodies(REPO, 1)[0].startswith("Picked up")


async def test_a_comment_on_a_missing_issue_is_fatal_and_leaves_no_row(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    with pytest.raises(GitHubFatal) as failed:
        await post_comment(gh, ledger, comment(number=99))
    assert failed.value.status == 404
    assert ledger.rows == {}


async def test_a_repo_that_is_not_owner_slash_name_is_fatal_before_any_request(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    bad = CommentInput(task_id=TASK, repo="calc", number=1, body="hi", idempotency_key=KEY)
    with pytest.raises(GitHubFatal, match="owner/name"):
        await post_comment(gh, ledger, bad)
    assert fake.requests == []


async def test_the_same_key_for_another_issue_is_refused(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    fake.seed_issue(REPO, "second issue")
    await post_comment(gh, ledger, comment("Picked up", number=1))
    with pytest.raises(KeyReuseError):
        await post_comment(gh, ledger, comment("Picked up", number=2))
    assert fake.count_comments(REPO, 2) == 0


async def test_a_write_answered_without_an_id_is_retried_and_then_found(
    ledger: MemoryLedger,
) -> None:
    # A 201 whose body is not a comment may still have created one. The retry must look first.
    seen: list[str] = []

    def route(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.method)
        if request.method == "POST":
            return httpx2.Response(201, json=["not", "a", "comment"])
        if "POST" in seen:  # the comment exists by the time of the second attempt
            return httpx2.Response(200, json=[{"id": 9, "body": f"hi\n\n{marker(KEY)}"}])
        return httpx2.Response(200, json=[])

    async with client_on(httpx2.MockTransport(route)) as client:
        with pytest.raises(GitHubRetryable, match="id"):
            await post_comment(client, ledger, comment("hi"))
        assert ledger.rows == {}
        assert await post_comment(client, ledger, comment("hi")) == "9"


# --- overlapping attempts ---------------------------------------------------------------------


async def test_two_attempts_at_the_same_time_post_once(
    fake: FakeGitHub, ledger: MemoryLedger
) -> None:
    # A timed-out attempt can still be running when Temporal starts the next one. Both look
    # for the comment before either has posted, so the marker alone cannot stop a duplicate.
    slow = SlowTransport(fake.transport())
    async with client_on(slow) as client:
        first, second = await asyncio.gather(
            post_comment(client, ledger, comment()), post_comment(client, ledger, comment())
        )

    assert first == second
    assert fake.count_comments(REPO, 1) == 1
    assert slow.peak == 1, "the second attempt waited for the first"


async def test_different_keys_do_not_wait_for_each_other(
    fake: FakeGitHub, ledger: MemoryLedger
) -> None:
    slow = SlowTransport(fake.transport())
    async with client_on(slow) as client:
        await asyncio.gather(
            post_comment(client, ledger, comment("one", key=effect_key(TASK, "a", 1))),
            post_comment(client, ledger, comment("two", key=effect_key(TASK, "b", 1))),
        )

    assert fake.count_comments(REPO, 1) == 2
    assert slow.peak == 2


# --- labels -----------------------------------------------------------------------------------


async def test_add_label_adds_it_once_and_records_a_row(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    request = label()

    assert await add_label(gh, ledger, request) == LABEL

    assert fake.label_names(REPO, 1) == [LABEL]
    row = ledger.rows[request.idempotency_key]
    assert (row.task_id, row.kind, row.external_id) == (TASK, "label_add", LABEL)
    before = len(fake.requests)
    assert await add_label(gh, ledger, request) == LABEL
    assert len(fake.requests) == before
    assert [e.action for e in fake.label_events(REPO, 1)] == ["labeled"]


async def test_add_label_after_a_lost_ledger_changes_nothing_on_github(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    request = label()
    await add_label(gh, ledger, request)
    ledger.rows.clear()

    assert await add_label(gh, ledger, request) == LABEL

    assert fake.label_names(REPO, 1) == [LABEL]
    assert [e.action for e in fake.label_events(REPO, 1)] == ["labeled"]
    assert request.idempotency_key in ledger.rows


async def test_add_label_after_a_502_that_landed_succeeds_on_retry(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    fake.after_write_fail(r"/issues/1/labels$")

    with pytest.raises(GitHubRetryable):
        await add_label(gh, ledger, label())
    assert fake.label_names(REPO, 1) == [LABEL]

    assert await add_label(gh, ledger, label()) == LABEL
    assert fake.label_names(REPO, 1) == [LABEL]


@pytest.mark.parametrize("name", ["good first issue", "type/bug", "longhaul:needs-human", "q&a"])
async def test_labels_with_spaces_slashes_and_colons_work_both_ways(
    name: str, fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    await add_label(gh, ledger, label(name, key=effect_key(TASK, "add", 1)))
    assert fake.label_names(REPO, 1) == [name]

    await remove_label(gh, ledger, label(name, key=effect_key(TASK, "remove", 1)))

    assert fake.label_names(REPO, 1) == []
    assert ledger.rows[effect_key(TASK, "remove", 1)].kind == "label_remove"


@pytest.mark.parametrize(
    ("name", "encoded"),
    [
        ("longhaul:working", "longhaul%3Aworking"),
        ("good first issue", "good%20first%20issue"),
        ("type/bug", "type%2Fbug"),
        ("q&a", "q%26a"),
        ("100%", "100%25"),
    ],
)
async def test_remove_label_sends_the_name_as_one_encoded_path_segment(
    name: str, encoded: str, ledger: MemoryLedger
) -> None:
    # FakeGitHub reads a slash inside a name as part of it, and GitHub does not: a label called
    # type/bug has to be written type%2Fbug, or the request names another URL.
    seen: list[bytes] = []

    def route(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.raw_path)
        return httpx2.Response(200, json=[])

    async with client_on(httpx2.MockTransport(route)) as client:
        await remove_label(client, ledger, label(name))

    assert seen == [f"/repos/acme/calc/issues/1/labels/{encoded}".encode()]


async def test_remove_label_removes_it_once_and_records_a_row(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    await add_label(gh, ledger, label(key=effect_key(TASK, "add", 1)))
    request = label(key=effect_key(TASK, "remove", 1))

    assert await remove_label(gh, ledger, request) == LABEL

    assert fake.label_names(REPO, 1) == []
    row = ledger.rows[request.idempotency_key]
    assert (row.task_id, row.kind, row.external_id) == (TASK, "label_remove", LABEL)
    before = len(fake.requests)
    assert await remove_label(gh, ledger, request) == LABEL
    assert len(fake.requests) == before


async def test_remove_label_after_a_502_that_landed_succeeds_on_retry(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    # The first DELETE removed the label and the answer was lost. The second one gets a 404
    # from GitHub because the label is gone, which is the result that was wanted.
    await add_label(gh, ledger, label(key=effect_key(TASK, "add", 1)))
    request = label(key=effect_key(TASK, "remove", 1))
    fake.after_write_fail(r"/labels/")

    with pytest.raises(GitHubRetryable):
        await remove_label(gh, ledger, request)
    assert fake.label_names(REPO, 1) == []

    assert await remove_label(gh, ledger, request) == LABEL
    assert request.idempotency_key in ledger.rows


async def test_remove_label_that_was_never_there_counts_as_removed(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    assert await remove_label(gh, ledger, label()) == LABEL
    assert fake.label_names(REPO, 1) == []


@pytest.mark.parametrize("asked", [LABEL, "LongHaul:Working"])
async def test_a_404_while_the_label_is_still_there_is_not_swallowed(
    asked: str, fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    # The 404 is only "already removed" if the label is gone. GitHub ignores case in names.
    await add_label(gh, ledger, label(key=effect_key(TASK, "add", 1)))
    fake.fail_next(r"/labels/", 404, method="DELETE")

    with pytest.raises(GitHubFatal) as failed:
        await remove_label(gh, ledger, label(asked, key=effect_key(TASK, "remove", 1)))

    assert failed.value.status == 404
    assert fake.label_names(REPO, 1) == [LABEL]
    assert effect_key(TASK, "remove", 1) not in ledger.rows


async def test_remove_label_on_a_missing_issue_stays_fatal(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    with pytest.raises(GitHubFatal) as failed:
        await remove_label(gh, ledger, label(number=99))
    assert failed.value.status == 404
    assert "/labels/" in str(failed.value), "the error is the DELETE's, not the check's"
    assert ledger.rows == {}


async def test_a_failing_check_after_a_404_is_retried(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    fake.fail_next(r"/labels/", 404, method="DELETE")
    fake.fail_next(r"/issues/1$", 502)

    with pytest.raises(GitHubRetryable):
        await remove_label(gh, ledger, label())
    assert ledger.rows == {}


async def test_other_errors_on_remove_label_are_not_swallowed(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    fake.fail_next(r"/labels/", 403, method="DELETE")
    with pytest.raises(GitHubFatal) as failed:
        await remove_label(gh, ledger, label())
    assert failed.value.status == 403


@pytest.mark.parametrize("operation", [add_label, remove_label])
async def test_an_empty_label_is_refused_before_anything_happens(
    operation: Callable[[GitHubClient, Ledger, LabelInput], Awaitable[str]],
    fake: FakeGitHub,
    gh: GitHubClient,
    ledger: MemoryLedger,
) -> None:
    for name in ("", "  "):
        with pytest.raises(SideEffectError, match="label"):
            await operation(gh, ledger, label(name))
    assert fake.requests == [] and ledger.rows == {}


# --- create_pr --------------------------------------------------------------------------------


async def test_create_pr_opens_one_pull_request_and_records_its_url(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    request = pull()

    url = await create_pr(gh, ledger, request)

    [opened] = fake.pulls(REPO)
    assert url == opened["html_url"] == f"https://github.com/{REPO}/pull/2"
    assert (opened["title"], opened["body"], opened["draft"]) == (
        "fix(calc): add returns the sum",
        "Closes #1",
        True,
    )
    assert (opened["head"]["ref"], opened["base"]["ref"]) == (HEAD, "main")
    row = ledger.rows[request.idempotency_key]
    assert (row.task_id, row.kind, row.external_id) == (TASK, "pr", url)
    before = len(fake.requests)
    assert await create_pr(gh, ledger, request) == url
    assert len(fake.requests) == before
    assert fake.count_pulls(REPO) == 1


async def test_create_pr_can_open_a_pull_request_that_is_ready_for_review(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    await create_pr(gh, ledger, pull(draft=False))
    assert fake.pulls(REPO)[0]["draft"] is False


async def test_create_pr_returns_the_open_pull_request_of_the_head_when_the_ledger_is_lost(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    url = await create_pr(gh, ledger, pull())
    ledger.rows.clear()
    before = len(fake.requests)

    assert await create_pr(gh, ledger, pull()) == url

    assert fake.count_pulls(REPO) == 1
    assert [r.method for r in fake.requests[before:]] == ["GET"]
    assert ledger.rows[pull().idempotency_key].external_id == url


async def test_create_pr_takes_a_pull_request_that_a_person_opened_from_the_head(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    number = fake.seed_pull(REPO, HEAD, title="by hand")

    url = await create_pr(gh, ledger, pull())

    assert url == f"https://github.com/{REPO}/pull/{number}"
    assert fake.count_pulls(REPO) == 1


async def test_create_pr_is_not_fooled_by_other_heads_or_closed_pull_requests(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    fake.seed_pull(REPO, "longhaul-pr/2")
    closed = fake.seed_pull(REPO, HEAD)
    fake.close_pr(REPO, closed)
    merged = fake.seed_pull(REPO, HEAD)
    fake.merge_pr(REPO, merged)

    url = await create_pr(gh, ledger, pull())

    assert fake.count_pulls(REPO, "open") == 2
    assert url not in {f"https://github.com/{REPO}/pull/{n}" for n in (closed, merged)}
    assert url == fake.pulls(REPO, "open")[-1]["html_url"]


async def test_a_502_after_the_pull_request_landed_does_not_open_a_second(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    fake.after_write_fail(r"/pulls$")

    with pytest.raises(GitHubRetryable):
        await create_pr(gh, ledger, pull())
    assert fake.count_pulls(REPO) == 1

    url = await create_pr(gh, ledger, pull())

    assert fake.count_pulls(REPO) == 1
    assert url == fake.pulls(REPO)[0]["html_url"]


async def test_create_pr_accepts_a_head_written_with_its_owner(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    request = pull(f"acme:{HEAD}")
    url = await create_pr(gh, ledger, request)
    ledger.rows.clear()
    assert await create_pr(gh, ledger, request) == url
    assert fake.count_pulls(REPO) == 1


async def test_the_same_key_for_another_pull_request_is_refused(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    await create_pr(gh, ledger, pull())
    with pytest.raises(KeyReuseError):
        await create_pr(gh, ledger, pull(title="something else"))
    assert fake.count_pulls(REPO) == 1


@pytest.mark.parametrize("changes", [{"head": ""}, {"head": "  "}, {"base": ""}])
async def test_create_pr_refuses_an_empty_head_or_base(
    changes: dict[str, str], fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    with pytest.raises(SideEffectError, match="head|base"):
        await create_pr(gh, ledger, pull(**changes))
    assert fake.requests == [] and ledger.rows == {}


async def test_a_refused_pull_request_is_fatal_and_leaves_no_row(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    fake.fail_next(r"/pulls$", 422, method="POST", body={"message": "Validation Failed"})
    with pytest.raises(GitHubFatal) as failed:
        await create_pr(gh, ledger, pull())
    assert failed.value.status == 422
    assert ledger.rows == {}


async def test_a_pull_request_answered_without_a_url_is_retried_and_then_found(
    ledger: MemoryLedger,
) -> None:
    url = "https://github.com/acme/calc/pull/5"
    head = {"ref": HEAD, "user": {"login": "acme"}}
    seen: list[str] = []

    def route(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.method)
        if request.method == "POST":
            return httpx2.Response(201, json={"number": 5})  # created, but no html_url
        if "POST" in seen:
            return httpx2.Response(200, json=[{"number": 5, "html_url": url, "head": head}])
        return httpx2.Response(200, json=[])

    async with client_on(httpx2.MockTransport(route)) as client:
        with pytest.raises(GitHubRetryable, match="html_url"):
            await create_pr(client, ledger, pull())
        assert await create_pr(client, ledger, pull()) == url


async def test_the_oldest_of_several_open_pull_requests_from_a_head_is_returned(
    fake: FakeGitHub, gh: GitHubClient, ledger: MemoryLedger
) -> None:
    older = fake.seed_pull(REPO, HEAD)
    fake.seed_pull(REPO, HEAD, base="release")

    url = await create_pr(gh, ledger, pull())

    assert url == f"https://github.com/{REPO}/pull/{older}"


# --- the activities ---------------------------------------------------------------------------


@pytest.fixture
def activities(gh: GitHubClient, ledger: MemoryLedger) -> SideEffectActivities:
    return SideEffectActivities(gh, ledger)


async def run_activity(fn: Callable[..., Awaitable[str]], inp: Any) -> str:
    return await ActivityEnvironment().run(fn, inp)


def test_the_four_activities_are_listed_for_registration(
    activities: SideEffectActivities,
) -> None:
    assert [a.__name__ for a in activities.activities] == [
        "post_comment",
        "add_label",
        "remove_label",
        "create_pr",
    ]


async def test_the_activities_return_what_the_functions_return(
    fake: FakeGitHub, activities: SideEffectActivities, ledger: MemoryLedger
) -> None:
    comment_id = await run_activity(activities.post_comment, comment())
    assert comment_id == ledger.rows[KEY].external_id

    added = await run_activity(activities.add_label, label(key=effect_key(TASK, "add", 1)))
    removed = await run_activity(activities.remove_label, label(key=effect_key(TASK, "rm", 1)))
    url = await run_activity(activities.create_pr, pull())

    assert (added, removed) == (LABEL, LABEL)
    assert url == fake.pulls(REPO)[0]["html_url"]
    assert {row.kind for row in ledger.rows.values()} == {
        "comment",
        "label_add",
        "label_remove",
        "pr",
    }


async def test_a_502_becomes_an_application_error_that_temporal_retries(
    fake: FakeGitHub, activities: SideEffectActivities
) -> None:
    fake.fail_next(r"/comments$", 502)

    with pytest.raises(ApplicationError) as failed:
        await run_activity(activities.post_comment, comment())

    error = failed.value
    assert (error.type, error.non_retryable) == ("GitHubRetryable", False)
    assert error.next_retry_delay is None, "the retry policy's own backoff applies"
    assert "502" in error.message


async def test_a_rate_limit_wait_becomes_the_delay_before_the_retry(
    fake: FakeGitHub, activities: SideEffectActivities
) -> None:
    fake.fail_next(r"/comments$", 429, headers={"retry-after": "42"})

    with pytest.raises(ApplicationError) as failed:
        await run_activity(activities.post_comment, comment())

    assert failed.value.non_retryable is False
    assert failed.value.next_retry_delay == timedelta(seconds=42)


async def test_a_404_becomes_an_application_error_that_is_not_retried(
    activities: SideEffectActivities,
) -> None:
    with pytest.raises(ApplicationError) as failed:
        await run_activity(activities.post_comment, comment(number=99))

    assert (failed.value.type, failed.value.non_retryable) == ("GitHubFatal", True)
    assert "404" in failed.value.message


@pytest.mark.parametrize(
    ("key", "expected"), [("not a key", "InvalidKeyError"), (KEY, "KeyReuseError")]
)
async def test_a_request_that_retrying_cannot_fix_is_not_retried(
    key: str, expected: str, activities: SideEffectActivities
) -> None:
    await run_activity(activities.post_comment, comment("Picked up"))

    with pytest.raises(ApplicationError) as failed:
        await run_activity(activities.post_comment, comment("Something else", key=key))

    assert (failed.value.type, failed.value.non_retryable) == (expected, True)


async def test_an_empty_comment_is_not_retried(activities: SideEffectActivities) -> None:
    with pytest.raises(ApplicationError) as failed:
        await run_activity(activities.post_comment, comment(" "))
    assert (failed.value.type, failed.value.non_retryable) == ("SideEffectError", True)


async def test_an_oversized_input_is_not_retried(
    fake: FakeGitHub, activities: SideEffectActivities
) -> None:
    # Invariant I2. The type name is the one acts.py lists as non-retryable.
    with pytest.raises(ApplicationError) as failed:
        await run_activity(activities.post_comment, comment("x" * 300_000))

    assert (failed.value.type, failed.value.non_retryable) == ("PayloadTooLargeError", True)
    assert fake.requests == []


async def test_an_unexpected_error_is_left_for_temporal_to_retry(fake: FakeGitHub) -> None:
    # A lost database connection says nothing about the request, so it keeps its own type and
    # Temporal's default applies: retry.
    class Broken(MemoryLedger):
        def locked(self, key: str) -> Any:
            raise ConnectionError("the database went away")

    async with client_on(fake.transport()) as client:
        broken = SideEffectActivities(client, Broken())
        with pytest.raises(ConnectionError):
            await run_activity(broken.post_comment, comment())
    assert fake.requests == []
