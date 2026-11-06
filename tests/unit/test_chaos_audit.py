"""What a chaos run leaves on GitHub, and how long it took to start again (LH-904).

The side effects are counted from the state of FakeGitHub, the stand-in for GitHub that the run
wrote to. Our own side_effects table is where a retry decides whether to write, so it cannot also
be the witness that it decided right. The resume latency is read from Temporal's history, because
the history has the server's time for the start of every activity, including a retried one.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from tests.fakes.fake_github import FakeGitHub

from eval.chaos import audit, history
from eval.chaos.results import SideEffects
from longhaul.contracts import RepoSettings, TaskSpec
from longhaul.cost.pricing import Pricing
from longhaul.github import lifecycle

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

REPO = "acme/calc"
NOON = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


def spec(issue_number: int | None = 1, repo: str = REPO) -> TaskSpec:
    return TaskSpec(
        task_id="lh-chaos-calc-01-ab12cd34",
        repo=repo,
        issue_number=issue_number,
        title="add() returns the difference",
        body="`calc.add` subtracts.",
        settings=RepoSettings(repo=repo),
    )


# ---------- the side effects of a finished run ----------
async def delivered(fake: FakeGitHub, *, pickup_comments: int = 1, pulls: int = 1) -> None:
    """The writes of one task, made through the REST API the way the worker makes them."""
    async with fake.client() as http:
        for n in range(pickup_comments):
            await http.post(f"/repos/{REPO}/issues/1/comments", json={"body": f"picked up {n}"})
        await http.post(f"/repos/{REPO}/issues/1/labels", json={"labels": [lifecycle.WORKING]})
        await http.delete(f"/repos/{REPO}/issues/1/labels/{lifecycle.WORKING}")
        await http.post(f"/repos/{REPO}/issues/1/labels", json={"labels": [lifecycle.PR_OPEN]})
    for n in range(pulls):
        fake.seed_pull(REPO, f"longhaul-pr/lh-chaos-{n}", title="fix")


@pytest.fixture
def fake() -> FakeGitHub:
    world = FakeGitHub()
    world.seed_repo(REPO)
    world.seed_issue(REPO, "add() returns the difference", labels=["longhaul"])
    return world


async def test_side_effects_are_counted_from_the_state_of_fakegithub(fake: FakeGitHub) -> None:
    await delivered(fake)

    counted = audit.count_side_effects(fake, REPO, 1)

    assert counted == SideEffects(comments=1, label_events=3, pull_requests=1)


async def test_the_comments_of_a_person_before_the_run_are_not_the_runs(fake: FakeGitHub) -> None:
    fake.seed_comment(REPO, 1, "looking into it")
    await delivered(fake)

    assert audit.count_side_effects(fake, REPO, 1).comments == 2


def test_a_repo_where_nothing_happened_has_no_side_effects(fake: FakeGitHub) -> None:
    assert audit.count_side_effects(fake, REPO, 1) == SideEffects(
        comments=0, label_events=0, pull_requests=0
    )


def test_the_expected_side_effects_follow_the_lifecycle_of_a_task() -> None:
    expected = audit.expected_side_effects(spec())

    picked = lifecycle.on_pickup(spec())
    opened = lifecycle.on_pr_opened(spec())
    assert expected == SideEffects(
        comments=1, label_events=len(picked) + len(opened), pull_requests=1
    )
    assert expected == SideEffects(comments=1, label_events=3, pull_requests=1)


def test_a_task_without_an_issue_has_nothing_to_expect() -> None:
    with pytest.raises(ValueError, match="issue"):
        audit.expected_side_effects(spec(issue_number=None))


def test_a_task_of_a_local_folder_has_nothing_to_expect() -> None:
    with pytest.raises(ValueError, match="issue"):
        audit.expected_side_effects(spec(repo="local:/tmp/calc"))


async def test_a_run_with_the_expected_writes_has_no_duplicates_and_nothing_missing(
    fake: FakeGitHub,
) -> None:
    await delivered(fake)
    counted = audit.count_side_effects(fake, REPO, 1)
    expected = audit.expected_side_effects(spec())

    assert audit.duplicates(counted, expected) == 0
    assert audit.missing(counted, expected) == 0


async def test_a_comment_written_twice_is_a_duplicate(fake: FakeGitHub) -> None:
    await delivered(fake, pickup_comments=2)
    counted = audit.count_side_effects(fake, REPO, 1)

    assert audit.duplicates(counted, audit.expected_side_effects(spec())) == 1
    assert audit.missing(counted, audit.expected_side_effects(spec())) == 0


async def test_a_second_pull_request_is_a_duplicate(fake: FakeGitHub) -> None:
    await delivered(fake, pulls=2)
    counted = audit.count_side_effects(fake, REPO, 1)

    assert audit.duplicates(counted, audit.expected_side_effects(spec())) == 1


async def test_a_pull_request_that_was_never_opened_is_missing(fake: FakeGitHub) -> None:
    await delivered(fake, pulls=0)
    counted = audit.count_side_effects(fake, REPO, 1)

    assert audit.missing(counted, audit.expected_side_effects(spec())) == 1
    assert audit.duplicates(counted, audit.expected_side_effects(spec())) == 0


def test_duplicates_and_missing_add_up_over_the_three_kinds() -> None:
    expected = SideEffects(comments=1, label_events=3, pull_requests=1)
    observed = SideEffects(comments=3, label_events=2, pull_requests=2)

    assert audit.duplicates(observed, expected) == 2 + 0 + 1
    assert audit.missing(observed, expected) == 0 + 1 + 0


# ---------- Temporal's history ----------
def at(seconds: float) -> datetime:
    return NOON + timedelta(seconds=seconds)


def event(kind: EventType.ValueType, when: datetime, **fields: Any) -> HistoryEvent:
    stamp = Timestamp()
    stamp.FromDatetime(when)
    return HistoryEvent(event_type=kind, event_time=stamp, **fields)


def started(seconds: float) -> HistoryEvent:
    return event(EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED, at(seconds))


def test_the_start_times_are_those_of_the_activity_started_events_in_order() -> None:
    events = [
        event(EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED, at(0)),
        started(1),
        event(EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED, at(2)),
        event(EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED, at(3)),
        started(4),
    ]

    assert history.activity_starts(events) == [at(1), at(4)]


def test_the_latency_runs_from_the_kill_to_the_next_start() -> None:
    assert history.resume_latency_s([at(1), at(9), at(12)], at(5)) == 4.0


def test_a_start_before_the_kill_is_not_the_resume() -> None:
    assert history.resume_latency_s([at(1), at(2)], at(5)) is None


def test_a_run_that_ended_with_the_kill_has_no_latency() -> None:
    assert history.resume_latency_s([], at(5)) is None


def test_a_start_at_the_moment_of_the_kill_is_not_the_resume() -> None:
    assert history.resume_latency_s([at(5), at(8)], at(5)) == 3.0


def test_the_clock_of_the_server_is_put_next_to_the_clock_of_the_host() -> None:
    # The server's clock is 2 s ahead, so a kill at host time 5 was at server time 7.
    assert history.resume_latency_s([at(8), at(9)], at(5), clock_offset_s=2.0) == 1.0


def test_the_offset_of_a_clock_is_read_from_a_reading_between_two_of_the_host() -> None:
    # The container said 102.0 while the host read 99.9 before and 100.1 after, so it is 2 s ahead.
    assert history.clock_offset_s(102.0, 99.9, 100.1) == pytest.approx(2.0)


async def test_the_history_of_every_run_of_a_task_comes_oldest_first() -> None:
    def first_event(previous: str) -> HistoryEvent:
        found = event(EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED, at(0))
        found.workflow_execution_started_event_attributes.continued_execution_run_id = previous
        return found

    runs = {
        None: [first_event("run-2"), started(30)],
        "run-2": [first_event("run-1"), started(20)],
        "run-1": [first_event(""), started(10)],
    }

    async def read(run_id: str | None) -> list[HistoryEvent]:
        return runs[run_id]

    found = await history.chain_history(read)

    assert history.activity_starts(found) == [at(10), at(20), at(30)]


async def test_a_task_that_never_continued_has_one_run() -> None:
    only = [event(EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED, at(0)), started(1)]

    async def read(run_id: str | None) -> list[HistoryEvent]:
        assert run_id is None
        return only

    assert await history.chain_history(read) == only


# ---------- what the ledger rows would cost ----------
PRICES = Path(__file__).resolve().parents[2] / "config" / "pricing.yaml"


def test_ledger_rows_are_priced_at_the_published_rates() -> None:
    # A replayed run bills nothing, so its ledger has no dollars. The rows still say how many
    # tokens each call used, and those are priced here with the rates of config/pricing.yaml.
    rows = [
        audit.LedgerRow("claude-sonnet-5-5", 1_000_000, 100_000, 0, 0),
        audit.LedgerRow("claude-sonnet-5-5", 0, 0, 1_000_000, 1_000_000),
    ]

    cost = audit.ledger_cost(rows, Pricing.load(PRICES))

    assert cost == pytest.approx(2.00 + 1.00 + 0.20 + 2.50)


def test_no_ledger_rows_cost_nothing() -> None:
    assert audit.ledger_cost([], Pricing.load(PRICES)) == 0.0
