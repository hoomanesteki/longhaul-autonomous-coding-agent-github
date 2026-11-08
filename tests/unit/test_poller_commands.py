"""The slash commands in poll_once: who may steer a task, and what each command does (LH-308).

GitHub is FakeGitHub behind the real GitHubClient. Temporal is a fake client that records the
workflows it was asked to start and the signals it was asked to send. The task rows and the
idempotency ledger are in memory. tests/integration/test_poller.py runs the same activity on a
Temporal server and Postgres, and checks the signals in the history of a real workflow.

A command reaches a task through a signal. The cursor of the task (the time and id of the newest
comment handled) moves only after the signal is sent, so a poll that is retried sends each command
once. Several tests below fail a signal on purpose to show that.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio import workflow
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from tests.fakes.fake_github import FAKE_BASE_URL, FakeGitHub

from longhaul.contracts import CarryState
from longhaul.db.tasks import MemoryTaskStore
from longhaul.durable import acts
from longhaul.durable.activities.github import PollerActivities
from longhaul.durable.activities.steering import SIGNALS
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.github.client import GitHubClient, PatAuth
from longhaul.github.commands import CommandKind
from longhaul.github.side_effects import MemoryLedger, has_marker, marker
from longhaul.settings import AppConfig, RepoEntry, load_config

pytestmark = [pytest.mark.unit, pytest.mark.phase3]

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "longhaul.example.yaml"
REPO = "acme/calc"
LABEL = "longhaul"
QUEUE = "lh-unit-tasks"
TOKEN = "ghp_unit_test_token_0123456789abcdef"
DENIED = "Only maintainers can steer this task"


# --- a fake Temporal --------------------------------------------------------------------------


@dataclass(frozen=True)
class Sent:
    task_id: str
    name: str
    args: tuple[Any, ...]


class FakeHandle:
    def __init__(self, temporal: FakeTemporal, task_id: str) -> None:
        self._temporal = temporal
        self._task_id = task_id

    async def describe(self) -> Any:
        temporal = self._temporal
        temporal.described.append(self._task_id)
        if self._task_id not in temporal.taken or self._task_id in temporal.forgotten:
            raise RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")
        status = temporal.ended.get(self._task_id, WorkflowExecutionStatus.RUNNING)
        return SimpleNamespace(status=status)

    async def signal(self, signal: Any, arg: Any = None, *, args: Sequence[Any] = ()) -> None:
        temporal = self._temporal
        # The server refuses a signal to a workflow that has closed, or that it has forgotten.
        if (
            self._task_id in temporal.forgotten
            or temporal.ended.get(self._task_id, WorkflowExecutionStatus.RUNNING)
            is not WorkflowExecutionStatus.RUNNING
        ):
            raise RPCError("workflow execution already completed", RPCStatusCode.NOT_FOUND, b"")
        if temporal.signal_error is not None and len(temporal.sent) >= temporal.fail_after:
            raise temporal.signal_error
        name = signal if isinstance(signal, str) else signal.__name__
        temporal.sent.append(Sent(self._task_id, name, tuple(args)))


class FakeTemporal:
    """The client: it notes each start and each signal, and refuses an id that it has seen."""

    def __init__(self) -> None:
        self.started: list[tuple[CarryState, dict[str, Any]]] = []
        self.taken: set[str] = set()
        self.ended: dict[str, WorkflowExecutionStatus] = {}
        self.forgotten: set[str] = set()
        self.sent: list[Sent] = []
        self.described: list[str] = []
        self.signal_error: Exception | None = None
        self.fail_after = 0

    async def start_workflow(self, workflow: Any, state: CarryState, **kwargs: Any) -> str:
        assert workflow == TaskWorkflow.run
        task_id = kwargs["id"]
        if task_id in self.taken:
            raise WorkflowAlreadyStartedError(task_id, "TaskWorkflow")
        self.taken.add(task_id)
        self.started.append((state, kwargs))
        return "handle"

    def get_workflow_handle_for(self, workflow: Any, workflow_id: str, **kwargs: Any) -> FakeHandle:
        assert workflow == TaskWorkflow.run
        return FakeHandle(self, workflow_id)

    @property
    def ids(self) -> list[str]:
        return [kwargs["id"] for _, kwargs in self.started]

    def finish(
        self, task_id: str, how: WorkflowExecutionStatus = WorkflowExecutionStatus.COMPLETED
    ) -> None:
        self.ended[task_id] = how

    def sent_to(self, task_id: str) -> list[tuple[str, tuple[Any, ...]]]:
        return [(s.name, s.args) for s in self.sent if s.task_id == task_id]


# --- the world --------------------------------------------------------------------------------


def entry(repo: str = REPO, **fields: Any) -> RepoEntry:
    return RepoEntry(repo=repo, allowlisted=True, **fields)


def config_with(*entries: RepoEntry) -> AppConfig:
    return load_config(EXAMPLE).model_copy(update={"repos": list(entries)})


@pytest.fixture
def fake() -> FakeGitHub:
    github = FakeGitHub()
    github.seed_repo(REPO)
    github.seed_collaborator(REPO, "maintainer", "maintain")
    github.seed_collaborator(REPO, "writer", "write")
    github.seed_collaborator(REPO, "boss", "admin")
    github.seed_collaborator(REPO, "reader", "read")
    return github


@pytest.fixture
async def github(fake: FakeGitHub) -> AsyncIterator[GitHubClient]:
    async with GitHubClient(FAKE_BASE_URL, PatAuth(TOKEN), transport=fake.transport()) as client:
        yield client


@pytest.fixture
def store() -> MemoryTaskStore:
    return MemoryTaskStore()


@pytest.fixture
def ledger() -> MemoryLedger:
    return MemoryLedger()


@pytest.fixture
def temporal() -> FakeTemporal:
    return FakeTemporal()


@pytest.fixture
def poller(github: GitHubClient, store: MemoryTaskStore, ledger: MemoryLedger) -> PollerActivities:
    return PollerActivities(github, store, config_with(entry()), task_queue=QUEUE, ledger=ledger)


async def poll(activities: PollerActivities, temporal: FakeTemporal) -> acts.PollResult:
    return await activities.poll(cast(Client, temporal))


def issue_with_task(fake: FakeGitHub) -> int:
    return fake.seed_issue(REPO, "add() is wrong", labels=[LABEL])


def task_of(number: int, attempt: int = 1) -> str:
    return f"lh-acme-calc-{number}" + ("" if attempt == 1 else f"-a{attempt}")


async def picked_up(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> tuple[int, str]:
    """An open issue with a running task, after the first poll. Returns the number and the id."""
    number = issue_with_task(fake)
    await poll(poller, temporal)
    assert temporal.ids == [task_of(number)]
    return number, task_of(number)


# --- each command is a signal -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "name", "args"),
    [
        ("/longhaul approve", "approve_plan", ("maintainer",)),
        ("/longhaul reject use a loop", "reject_plan", ("maintainer", "use a loop")),
        ("/longhaul hint look at add()", "hint", ("maintainer", "look at add()")),
        ("/longhaul budget +5", "add_budget", ("maintainer", 5.0, 0)),
        ("/longhaul budget +2.5 turns +20", "add_budget", ("maintainer", 2.5, 20)),
        ("/longhaul cancel", "cancel", ("maintainer", "")),
        ("/longhaul cancel wrong issue", "cancel", ("maintainer", "wrong issue")),
        ("/longhaul status", "report_status", ("maintainer",)),
    ],
)
async def test_a_command_of_a_maintainer_is_the_signal_of_the_task(
    fake: FakeGitHub,
    poller: PollerActivities,
    temporal: FakeTemporal,
    body: str,
    name: str,
    args: tuple[Any, ...],
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, body, user="maintainer")

    result = await poll(poller, temporal)

    assert temporal.sent_to(task_id) == [(name, args)]
    assert result.commands == 1


@pytest.mark.parametrize("user", ["maintainer", "writer", "boss"])
async def test_write_maintain_and_admin_may_steer(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal, user: str
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul approve", user=user)

    await poll(poller, temporal)

    assert temporal.sent_to(task_id) == [("approve_plan", (user,))]


async def test_the_command_comes_back_in_the_case_the_user_wrote_it(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    fake.seed_collaborator(REPO, "Mixed-Case", "write")
    fake.seed_comment(REPO, number, "/LongHaul HINT Use Decimal", user="Mixed-Case")

    await poll(poller, temporal)

    assert temporal.sent_to(task_id) == [("hint", ("Mixed-Case", "Use Decimal"))]


async def test_commands_are_sent_in_the_order_they_were_written(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    for body in ("/longhaul hint one", "/longhaul budget +1", "/longhaul hint two"):
        fake.seed_comment(REPO, number, body, user="maintainer")

    result = await poll(poller, temporal)

    assert [name for name, _ in temporal.sent_to(task_id)] == ["hint", "add_budget", "hint"]
    assert [args[-1] for name, args in temporal.sent_to(task_id) if name == "hint"] == [
        "one",
        "two",
    ]
    assert result.commands == 3


# --- who may steer ----------------------------------------------------------------------------


async def test_a_command_from_a_user_without_write_access_is_answered_and_not_sent(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="reader")

    result = await poll(poller, temporal)

    assert temporal.sent == []
    assert result.commands == 0
    replies = [b for b in fake.comment_bodies(REPO, number) if DENIED in b]
    assert len(replies) == 1
    assert "@reader" in replies[0]
    assert has_marker(replies[0]), "the reply is a comment of ours"


async def test_a_user_who_is_not_a_collaborator_is_answered_too(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, _ = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul approve", user="stranger")

    await poll(poller, temporal)

    assert temporal.sent == []
    assert fake.count_comments(REPO, number, containing=DENIED) == 1


async def test_a_user_gets_one_reply_however_many_commands_they_send(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, _ = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="reader")
    fake.seed_comment(REPO, number, "/longhaul approve", user="reader")
    await poll(poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul hint please", user="reader")

    await poll(poller, temporal)
    await poll(poller, temporal)

    assert fake.count_comments(REPO, number, containing=DENIED) == 1
    assert temporal.sent == []


async def test_each_user_gets_a_reply_of_their_own(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, _ = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="reader")
    fake.seed_comment(REPO, number, "/longhaul cancel", user="stranger")

    await poll(poller, temporal)

    bodies = [b for b in fake.comment_bodies(REPO, number) if DENIED in b]
    assert sorted(("@reader" in b, "@stranger" in b) for b in bodies) == [
        (False, True),
        (True, False),
    ]


async def test_the_reply_is_a_side_effect_with_a_key_for_the_user_and_the_task(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal, ledger: MemoryLedger
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="Reader")

    await poll(poller, temporal)

    (row,) = ledger.rows.values()
    assert (row.task_id, row.kind) == (task_id, "comment")
    assert row.key.startswith(f"{task_id}:")
    assert "reader" in row.key
    (body,) = [b for b in fake.comment_bodies(REPO, number) if DENIED in b]
    assert marker(row.key) in body


async def test_a_reply_that_was_posted_and_not_recorded_is_found_again(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal, ledger: MemoryLedger
) -> None:
    # The ledger can be lost. GitHub holds the marker, so the second poll finds the reply.
    number, _ = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="reader")
    await poll(poller, temporal)
    ledger.rows.clear()
    fake.seed_comment(REPO, number, "/longhaul cancel", user="reader")

    await poll(poller, temporal)

    assert fake.count_comments(REPO, number, containing=DENIED) == 1


async def test_a_permission_that_github_will_not_tell_counts_as_none(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, _ = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="maintainer")
    fake.fail_next(r"/collaborators/maintainer/permission", status=403, count=1)

    await poll(poller, temporal)

    assert temporal.sent == []
    assert fake.count_comments(REPO, number, containing=DENIED) == 1


async def test_a_permission_check_that_fails_for_now_stops_before_the_command_is_lost(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="maintainer")
    fake.fail_next(r"/collaborators/maintainer/permission", status=502, count=1)

    with pytest.raises(ApplicationError):
        await poll(poller, temporal)
    assert temporal.sent == []
    assert fake.count_comments(REPO, number, containing=DENIED) == 0, "no reply for a 5xx"

    await poll(poller, temporal)  # the retry

    assert temporal.sent_to(task_id) == [("cancel", ("maintainer", ""))]


async def test_a_user_is_asked_about_once_per_poll(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, _ = await picked_up(fake, poller, temporal)
    for body in ("/longhaul hint a", "/longhaul hint b", "/longhaul hint c"):
        fake.seed_comment(REPO, number, body, user="maintainer")
    before = len([r for r in fake.requests if "/collaborators/" in r.path])

    await poll(poller, temporal)

    asked = [r for r in fake.requests if "/collaborators/" in r.path]
    assert len(asked) - before == 1


# --- what is not a command --------------------------------------------------------------------


async def test_a_comment_that_is_not_a_command_costs_no_permission_lookup(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, _ = await picked_up(fake, poller, temporal)
    before = len([r for r in fake.requests if "/collaborators/" in r.path])
    fake.seed_comment(REPO, number, "thanks, looks good", user="stranger")
    fake.seed_comment(REPO, number, "```\n/longhaul cancel\n```", user="stranger")

    await poll(poller, temporal)

    assert len([r for r in fake.requests if "/collaborators/" in r.path]) == before
    assert temporal.sent == []
    assert fake.count_comments(REPO, number, containing=DENIED) == 0


async def test_our_own_comments_are_never_commands(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, _ = await picked_up(fake, poller, temporal)
    # A personal token makes our comments come from a user with write access.
    fake.seed_comment(
        REPO, number, f"/longhaul cancel\n\n{marker('lh-acme-calc-1:status:1')}", user="maintainer"
    )

    await poll(poller, temporal)

    assert temporal.sent == []


@pytest.mark.parametrize("login", ["dependabot[bot]", "github-actions[bot]"])
async def test_a_bot_is_ignored_without_a_reply(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal, login: str
) -> None:
    number, _ = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul cancel", user=login)

    await poll(poller, temporal)

    assert temporal.sent == []
    assert fake.count_comments(REPO, number, containing=DENIED) == 0


async def test_a_comment_from_before_the_pickup_is_not_a_command(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number = issue_with_task(fake)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="maintainer")
    fake.seed_comment(REPO, number, "/longhaul retry", user="maintainer")

    await poll(poller, temporal)
    await poll(poller, temporal)

    assert temporal.sent == []
    assert temporal.ids == [task_of(number)], "no retry either"


async def test_a_comment_right_after_the_pickup_is_a_command_even_in_the_same_second(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)  # the issue and the comment
    fake.seed_comment(REPO, number, "/longhaul approve", user="maintainer")  # share a second

    await poll(poller, temporal)

    assert temporal.sent_to(task_id) == [("approve_plan", ("maintainer",))]


async def test_a_comment_in_the_same_second_as_an_earlier_command_is_not_lost(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul hint first", user="maintainer")
    await poll(poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul hint second", user="maintainer")  # the same second

    await poll(poller, temporal)

    assert [args[-1] for _, args in temporal.sent_to(task_id)] == ["first", "second"]


async def test_an_issue_without_the_label_is_not_steered(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, _ = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="maintainer")
    async with fake.client() as http:
        await http.delete(f"/repos/{REPO}/issues/{number}/labels/{LABEL}")

    result = await poll(poller, temporal)

    assert temporal.sent == []
    assert result.commands == 0


async def test_a_closed_issue_is_not_steered(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, _ = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="maintainer")
    fake.close_issue(REPO, number)

    await poll(poller, temporal)

    assert temporal.sent == []


# --- once only --------------------------------------------------------------------------------


async def test_a_command_is_sent_once_however_often_the_poll_runs(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul budget +5", user="maintainer")

    for _ in range(4):
        await poll(poller, temporal)

    assert temporal.sent_to(task_id) == [("add_budget", ("maintainer", 5.0, 0))]


async def test_the_cursor_moves_to_the_comment_after_its_signal(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal, store: MemoryTaskStore
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    comment = fake.seed_comment(REPO, number, "/longhaul approve", user="maintainer")

    await poll(poller, temporal)

    cursor = store.rows[task_id].cursor
    assert cursor is not None and cursor.comment_id == comment


async def test_a_signal_that_fails_leaves_the_cursor_so_the_retry_sends_the_command(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal, store: MemoryTaskStore
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul approve", user="maintainer")
    before = store.rows[task_id].cursor
    temporal.signal_error = RPCError("temporal is down", RPCStatusCode.UNAVAILABLE, b"")

    with pytest.raises(RPCError):
        await poll(poller, temporal)
    assert store.rows[task_id].cursor == before
    assert temporal.sent == []

    temporal.signal_error = None
    await poll(poller, temporal)

    assert temporal.sent_to(task_id) == [("approve_plan", ("maintainer",))]


async def test_a_retried_poll_sends_only_the_commands_it_has_not_sent(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    for body in ("/longhaul hint one", "/longhaul hint two", "/longhaul hint three"):
        fake.seed_comment(REPO, number, body, user="maintainer")
    temporal.signal_error = RPCError("temporal is down", RPCStatusCode.UNAVAILABLE, b"")
    temporal.fail_after = 1  # the first signal goes through, the second fails

    with pytest.raises(RPCError):
        await poll(poller, temporal)
    assert [args[-1] for _, args in temporal.sent_to(task_id)] == ["one"]

    temporal.signal_error = None
    await poll(poller, temporal)

    assert [args[-1] for _, args in temporal.sent_to(task_id)] == ["one", "two", "three"]


async def test_a_reply_does_not_wait_for_the_signals_after_it(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="reader")
    fake.seed_comment(REPO, number, "/longhaul hint go on", user="maintainer")

    await poll(poller, temporal)

    assert fake.count_comments(REPO, number, containing=DENIED) == 1
    assert temporal.sent_to(task_id) == [("hint", ("maintainer", "go on"))]


async def test_a_task_row_without_a_cursor_reads_from_the_start_of_the_task(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal, store: MemoryTaskStore
) -> None:
    # A row from before the cursor existed. Comments older than the task are still not commands.
    number = issue_with_task(fake)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="maintainer")
    await poll(poller, temporal)
    task_id = task_of(number)
    store.rows[task_id].last_comment_at = None
    store.rows[task_id].last_comment_id = None
    fake.seed_comment(REPO, number, "/longhaul approve", user="maintainer")

    await poll(poller, temporal)

    sent = [name for name, _ in temporal.sent_to(task_id)]
    assert "approve_plan" in sent


# --- a task that has ended --------------------------------------------------------------------


@pytest.mark.parametrize(
    "how",
    [
        WorkflowExecutionStatus.COMPLETED,
        WorkflowExecutionStatus.FAILED,
        WorkflowExecutionStatus.CANCELED,
        WorkflowExecutionStatus.TERMINATED,
        WorkflowExecutionStatus.TIMED_OUT,
    ],
)
async def test_a_command_for_an_ended_task_is_dropped_and_not_kept_for_later(
    fake: FakeGitHub,
    poller: PollerActivities,
    temporal: FakeTemporal,
    how: WorkflowExecutionStatus,
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    temporal.finish(task_id, how)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="maintainer")
    fake.seed_comment(REPO, number, "/longhaul status", user="maintainer")

    result = await poll(poller, temporal)

    assert temporal.sent == []
    assert result.commands == 0


async def test_a_signal_that_finds_the_workflow_closed_is_dropped(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul cancel", user="maintainer")
    temporal.signal_error = RPCError(
        "workflow execution already completed", RPCStatusCode.NOT_FOUND, b""
    )

    result = await poll(poller, temporal)

    assert result.commands == 0
    temporal.signal_error = None
    await poll(poller, temporal)
    assert temporal.sent == [], "and it is not tried again"


# --- retry ------------------------------------------------------------------------------------


async def test_retry_on_a_finished_task_starts_the_next_attempt_of_the_same_issue(
    fake: FakeGitHub,
    poller: PollerActivities,
    temporal: FakeTemporal,
    store: MemoryTaskStore,
) -> None:
    number, first = await picked_up(fake, poller, temporal)
    temporal.finish(first, WorkflowExecutionStatus.FAILED)
    comment = fake.seed_comment(REPO, number, "/longhaul retry", user="maintainer")

    result = await poll(poller, temporal)

    second = task_of(number, 2)
    assert temporal.ids == [first, second]
    state, kwargs = temporal.started[1]
    assert (state.spec.task_id, state.spec.issue_number, state.spec.repo) == (second, number, REPO)
    assert kwargs["task_queue"] == QUEUE
    assert result.commands == 1
    row = store.rows[second]
    assert (row.repo, row.issue_number) == (REPO, number)
    assert row.cursor is not None and row.cursor.comment_id == comment


async def test_a_retry_keeps_the_draft_on_stuck_switch_of_the_repo(
    fake: FakeGitHub, github: GitHubClient, store: MemoryTaskStore, temporal: FakeTemporal
) -> None:
    config = config_with(entry(open_draft_on_stuck=True))
    activities = PollerActivities(github, store, config, task_queue=QUEUE, ledger=MemoryLedger())
    number, first = await picked_up(fake, activities, temporal)
    temporal.finish(first, WorkflowExecutionStatus.FAILED)
    fake.seed_comment(REPO, number, "/longhaul retry", user="maintainer")

    await poll(activities, temporal)

    (_, kwargs) = temporal.started[1]
    assert kwargs["memo"] == {acts.PLAN_MEMO_KEY: True, acts.OPEN_DRAFT_MEMO_KEY: True}


async def test_the_new_attempt_reads_the_issue_as_it_is_now(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, first = await picked_up(fake, poller, temporal)
    temporal.finish(first)
    fake.seed_comment(REPO, number, "use a loop", user="maintainer")
    fake.seed_comment(REPO, number, "/longhaul retry", user="maintainer")

    await poll(poller, temporal)

    state, _ = temporal.started[1]
    assert "use a loop" in state.spec.comments


async def test_a_second_poll_does_not_start_another_attempt(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, first = await picked_up(fake, poller, temporal)
    temporal.finish(first)
    fake.seed_comment(REPO, number, "/longhaul retry", user="maintainer")

    for _ in range(3):
        await poll(poller, temporal)

    assert temporal.ids == [first, task_of(number, 2)]


async def test_retry_while_the_task_runs_does_nothing(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, _ = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul retry", user="maintainer")

    result = await poll(poller, temporal)
    await poll(poller, temporal)

    assert temporal.ids == [task_of(number)]
    assert result.commands == 0


async def test_two_retries_in_one_poll_start_one_attempt(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, first = await picked_up(fake, poller, temporal)
    temporal.finish(first)
    fake.seed_comment(REPO, number, "/longhaul retry", user="maintainer")
    fake.seed_comment(REPO, number, "/longhaul retry", user="writer")

    await poll(poller, temporal)

    assert temporal.ids == [first, task_of(number, 2)]


async def test_a_command_after_a_retry_goes_to_the_new_attempt(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, first = await picked_up(fake, poller, temporal)
    temporal.finish(first)
    fake.seed_comment(REPO, number, "/longhaul retry", user="maintainer")
    fake.seed_comment(REPO, number, "/longhaul hint try the other file", user="maintainer")

    await poll(poller, temporal)

    assert temporal.sent_to(first) == []
    assert temporal.sent_to(task_of(number, 2)) == [("hint", ("maintainer", "try the other file"))]


async def test_later_commands_go_to_the_latest_attempt(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, first = await picked_up(fake, poller, temporal)
    temporal.finish(first)
    fake.seed_comment(REPO, number, "/longhaul retry", user="maintainer")
    await poll(poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul status", user="maintainer")

    await poll(poller, temporal)

    assert temporal.sent_to(first) == []
    assert temporal.sent_to(task_of(number, 2)) == [("report_status", ("maintainer",))]


async def test_attempts_count_up(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, first = await picked_up(fake, poller, temporal)
    for attempt in (2, 3, 4):
        temporal.finish(task_of(number, attempt - 1))
        fake.seed_comment(REPO, number, "/longhaul retry", user="maintainer")
        await poll(poller, temporal)

    assert temporal.ids == [task_of(number, n) for n in (1, 2, 3, 4)]


async def test_a_retry_that_died_after_the_start_is_finished_by_the_next_poll(
    fake: FakeGitHub,
    poller: PollerActivities,
    temporal: FakeTemporal,
    store: MemoryTaskStore,
) -> None:
    number, first = await picked_up(fake, poller, temporal)
    temporal.finish(first)
    fake.seed_comment(REPO, number, "/longhaul retry", user="maintainer")
    temporal.taken.add(task_of(number, 2))  # the workflow was started, the row was not written

    await poll(poller, temporal)

    assert temporal.ids == [first], "no second start"
    assert task_of(number, 2) in store.rows
    await poll(poller, temporal)
    assert temporal.ids == [first]


async def test_a_task_that_the_server_forgot_counts_as_ended(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, first = await picked_up(fake, poller, temporal)
    temporal.forgotten.add(first)
    temporal.taken.discard(first)
    fake.seed_comment(REPO, number, "/longhaul retry", user="maintainer")

    await poll(poller, temporal)

    assert temporal.ids == [first, task_of(number, 2)]


async def test_a_retry_from_a_user_without_write_access_starts_nothing(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, first = await picked_up(fake, poller, temporal)
    temporal.finish(first)
    fake.seed_comment(REPO, number, "/longhaul retry", user="reader")

    await poll(poller, temporal)

    assert temporal.ids == [first]
    assert fake.count_comments(REPO, number, containing=DENIED) == 1


# --- failures ---------------------------------------------------------------------------------


async def test_one_issue_that_fails_does_not_stop_the_next_and_the_poll_fails_after_it(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    first = issue_with_task(fake)
    second = issue_with_task(fake)
    await poll(poller, temporal)
    fake.seed_comment(REPO, first, "/longhaul approve", user="maintainer")
    fake.seed_comment(REPO, second, "/longhaul approve", user="maintainer")
    fake.fail_next(rf"/issues/{first}/comments", status=502, count=1)

    with pytest.raises(ApplicationError) as caught:
        await poll(poller, temporal)

    assert temporal.sent_to(task_of(second)) == [("approve_plan", ("maintainer",))]
    assert f"{REPO}#{first}" in str(caught.value)
    await poll(poller, temporal)
    assert temporal.sent_to(task_of(first)) == [("approve_plan", ("maintainer",))]


async def test_a_failure_is_logged_with_the_issue(
    fake: FakeGitHub,
    poller: PollerActivities,
    temporal: FakeTemporal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    number, _ = await picked_up(fake, poller, temporal)
    fake.fail_next(rf"/issues/{number}/comments", status=502, count=1)

    with caplog.at_level(logging.WARNING), pytest.raises(ApplicationError):
        await poll(poller, temporal)

    assert any(f"#{number}" in record.getMessage() for record in caplog.records)


async def test_the_poller_posts_nothing_but_the_reply_to_a_stranger(
    fake: FakeGitHub, poller: PollerActivities, temporal: FakeTemporal
) -> None:
    number, task_id = await picked_up(fake, poller, temporal)
    fake.seed_comment(REPO, number, "/longhaul approve", user="maintainer")
    fake.seed_comment(REPO, number, "/longhaul status", user="maintainer")
    fake.seed_comment(REPO, number, "/longhaul retry", user="maintainer")
    temporal.finish(task_id)

    await poll(poller, temporal)

    writes = {r.method for r in fake.requests if r.method != "GET"}
    assert writes == set(), "the signals and the retry are Temporal's, and GitHub is only read"
    assert fake.label_events(REPO, number) == []


# --- the names of the signals -----------------------------------------------------------------


def test_every_command_but_retry_has_a_signal_that_the_workflow_takes() -> None:
    signals = workflow._Definition.must_from_class(TaskWorkflow).signals

    assert set(SIGNALS) == set(CommandKind) - {CommandKind.RETRY}
    assert set(SIGNALS.values()) <= set(signals)
