"""The Temporal Schedule of the poller: what it holds and when an install changes it (LH-307).

These tests build `Schedule` objects and compare them, so they need no server. The calls to a
server (create, update, pause, trigger) are in tests/integration/test_poller.py, which also
checks that the server hands back the fields the comparison reads.
"""

import dataclasses
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    ScheduleSpec,
    ScheduleState,
)
from temporalio.service import RPCError, RPCStatusCode

from longhaul.durable import acts, schedule
from longhaul.durable.workflows.poller import PollerWorkflow

pytestmark = [pytest.mark.unit, pytest.mark.phase3]

QUEUE = "longhaul"


def action_of(wanted: Schedule) -> ScheduleActionStartWorkflow:
    assert isinstance(wanted.action, ScheduleActionStartWorkflow)
    return wanted.action


def test_the_schedule_runs_the_poller_workflow_on_the_task_queue() -> None:
    action = action_of(schedule.poller_schedule(60, QUEUE))
    assert action.workflow == "PollerWorkflow"
    assert action.task_queue == QUEUE
    assert action.id == "longhaul-poller-run"
    assert action.execution_timeout == acts.POLLER_RUN_TIMEOUT


def test_the_runs_of_another_schedule_id_are_named_after_it() -> None:
    # A test installs a schedule under its own id. Its runs must not look like the real ones.
    action = action_of(schedule.poller_schedule(60, QUEUE, schedule_id="mine"))
    assert action.id == "mine-run"


def test_the_workflow_name_is_the_one_the_class_registers() -> None:
    assert action_of(schedule.poller_schedule(60, QUEUE)).workflow == PollerWorkflow.__name__


@pytest.mark.parametrize("seconds", [1, 60, 3600])
def test_the_schedule_fires_at_the_configured_interval(seconds: int) -> None:
    wanted = schedule.poller_schedule(seconds, QUEUE)
    assert [spec.every for spec in wanted.spec.intervals] == [timedelta(seconds=seconds)]
    assert wanted.spec.calendars == [] and wanted.spec.cron_expressions == []


def test_a_poll_that_is_still_open_makes_the_next_tick_skip() -> None:
    # Two polls at once would both see an issue as new. SKIP drops the tick instead of queueing it.
    wanted = schedule.poller_schedule(60, QUEUE)
    assert wanted.policy.overlap is ScheduleOverlapPolicy.SKIP
    assert wanted.policy.catchup_window == acts.POLLER_CATCHUP_WINDOW


def test_a_schedule_with_another_kind_of_action_never_matches() -> None:
    other = Schedule(action=object(), spec=ScheduleSpec())  # type: ignore[arg-type]
    assert schedule.fingerprint(other) != schedule.fingerprint(schedule.poller_schedule(60, QUEUE))


def test_a_new_schedule_runs_until_someone_pauses_it() -> None:
    assert schedule.poller_schedule(60, QUEUE).state.paused is False


@pytest.mark.parametrize("seconds", [0, -5])
def test_an_interval_below_a_second_is_refused(seconds: int) -> None:
    with pytest.raises(ValueError, match="interval"):
        schedule.poller_schedule(seconds, QUEUE)


def test_an_empty_task_queue_is_refused() -> None:
    with pytest.raises(ValueError, match="task queue"):
        schedule.poller_schedule(60, "")


# --- what an install changes ------------------------------------------------------------------


def test_the_same_config_needs_no_update() -> None:
    assert (
        schedule.reconcile(schedule.poller_schedule(60, QUEUE), schedule.poller_schedule(60, QUEUE))
        is None
    )


@pytest.mark.parametrize(
    ("seconds", "queue"), [(30, QUEUE), (60, "other")], ids=["interval", "task queue"]
)
def test_a_changed_setting_is_written(seconds: int, queue: str) -> None:
    current = schedule.poller_schedule(60, QUEUE)
    wanted = schedule.poller_schedule(seconds, queue)

    written = schedule.reconcile(current, wanted)

    assert written is not None
    assert schedule.fingerprint(written) == schedule.fingerprint(wanted)


def test_a_paused_schedule_stays_paused_when_its_interval_changes() -> None:
    # An operator paused the poller on purpose. Changing the interval is not a reason to resume.
    current = dataclasses.replace(
        schedule.poller_schedule(60, QUEUE), state=ScheduleState(note="maintenance", paused=True)
    )

    written = schedule.reconcile(current, schedule.poller_schedule(30, QUEUE))

    assert written is not None
    assert written.state.paused is True and written.state.note == "maintenance"
    assert [spec.every for spec in written.spec.intervals] == [timedelta(seconds=30)]


def test_the_pause_and_the_note_are_not_part_of_what_install_compares() -> None:
    paused = dataclasses.replace(
        schedule.poller_schedule(60, QUEUE), state=ScheduleState(note="x", paused=True)
    )
    assert schedule.reconcile(paused, schedule.poller_schedule(60, QUEUE)) is None


def test_a_changed_overlap_policy_is_put_back() -> None:
    current = schedule.poller_schedule(60, QUEUE)
    current.policy.overlap = ScheduleOverlapPolicy.ALLOW_ALL

    written = schedule.reconcile(current, schedule.poller_schedule(60, QUEUE))

    assert written is not None
    assert written.policy.overlap is ScheduleOverlapPolicy.SKIP


def test_a_schedule_with_another_action_is_replaced() -> None:
    other = dataclasses.replace(
        schedule.poller_schedule(60, QUEUE),
        action=ScheduleActionStartWorkflow("SomethingElse", id="x", task_queue=QUEUE),
    )
    assert schedule.reconcile(other, schedule.poller_schedule(60, QUEUE)) is not None


# --- install, with a fake client --------------------------------------------------------------


class FakeHandle:
    """Runs the updater the way the server does: with the schedule as it is now."""

    def __init__(self, owner: "FakeClient") -> None:
        self.owner = owner

    async def update(self, updater: Any) -> None:
        current = SimpleNamespace(schedule=self.owner.current)
        self.owner.updates.append(updater(SimpleNamespace(description=current)))


class FakeClient:
    def __init__(self, current: Schedule | None = None) -> None:
        self.current = current
        self.created: list[tuple[str, Schedule]] = []
        self.updates: list[Any] = []
        self.handle_ids: list[str] = []

    async def create_schedule(self, schedule_id: str, wanted: Schedule) -> None:
        if self.current is not None:
            raise ScheduleAlreadyRunningError()
        self.created.append((schedule_id, wanted))

    def get_schedule_handle(self, schedule_id: str) -> FakeHandle:
        self.handle_ids.append(schedule_id)
        return FakeHandle(self)


async def test_install_creates_the_schedule_under_its_id() -> None:
    client = FakeClient()

    result = await schedule.install_poller(
        cast(Client, client), interval_seconds=45, task_queue=QUEUE
    )

    assert result is schedule.Installed.CREATED
    ((schedule_id, wanted),) = client.created
    assert schedule_id == acts.POLLER_SCHEDULE_ID == "longhaul-poller"
    assert schedule.fingerprint(wanted) == schedule.fingerprint(schedule.poller_schedule(45, QUEUE))


async def test_install_leaves_a_schedule_that_already_matches_alone() -> None:
    client = FakeClient(current=schedule.poller_schedule(45, QUEUE))

    result = await schedule.install_poller(
        cast(Client, client), interval_seconds=45, task_queue=QUEUE
    )

    assert result is schedule.Installed.UNCHANGED
    assert client.updates == [None], "None tells the server to send no update"
    assert client.created == []


async def test_install_rewrites_a_schedule_whose_interval_changed() -> None:
    client = FakeClient(current=schedule.poller_schedule(60, QUEUE, schedule_id="mine"))

    result = await schedule.install_poller(
        cast(Client, client), interval_seconds=45, task_queue=QUEUE, schedule_id="mine"
    )

    assert result is schedule.Installed.UPDATED
    assert client.handle_ids == ["mine"]
    [update] = client.updates
    wanted = schedule.poller_schedule(45, QUEUE, schedule_id="mine")
    assert schedule.fingerprint(update.schedule) == schedule.fingerprint(wanted)


async def test_install_checks_its_arguments_before_it_talks_to_the_server() -> None:
    client = FakeClient()

    with pytest.raises(ValueError, match="interval"):
        await schedule.install_poller(cast(Client, client), interval_seconds=0, task_queue=QUEUE)

    assert client.created == []


# --- pause, resume and trigger ----------------------------------------------------------------


class ManagedHandle:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]], error: Exception | None) -> None:
        self.calls = calls
        self.error = error

    async def pause(self, **kwargs: Any) -> None:
        self.calls.append(("pause", kwargs))
        if self.error is not None:
            raise self.error

    async def unpause(self, **kwargs: Any) -> None:
        self.calls.append(("unpause", kwargs))
        if self.error is not None:
            raise self.error

    async def trigger(self, **kwargs: Any) -> None:
        self.calls.append(("trigger", kwargs))
        if self.error is not None:
            raise self.error


class ManagedClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error = error
        self.ids: list[str] = []

    def get_schedule_handle(self, schedule_id: str) -> ManagedHandle:
        self.ids.append(schedule_id)
        return ManagedHandle(self.calls, self.error)


async def test_pause_stops_the_ticks_and_keeps_the_schedule() -> None:
    client = ManagedClient()

    await schedule.pause_poller(cast(Client, client), note="maintenance")

    assert client.ids == ["longhaul-poller"]
    assert client.calls == [("pause", {"note": "maintenance"})]


async def test_pause_without_a_note_says_who_paused() -> None:
    client = ManagedClient()

    await schedule.pause_poller(cast(Client, client))

    ((name, kwargs),) = client.calls
    assert name == "pause" and "longhaul poller pause" in kwargs["note"]


async def test_resume_starts_the_ticks_again() -> None:
    client = ManagedClient()

    await schedule.resume_poller(cast(Client, client))

    ((name, kwargs),) = client.calls
    assert name == "unpause" and "longhaul poller resume" in kwargs["note"]


async def test_trigger_asks_for_one_poll_under_the_policy_of_the_schedule() -> None:
    client = ManagedClient()

    await schedule.trigger_poller(cast(Client, client), schedule_id="mine")

    # No overlap argument, so a poll that is already open makes the server skip this one.
    assert client.calls == [("trigger", {})]
    assert client.ids == ["mine"]


@pytest.mark.parametrize("action", ["pause_poller", "resume_poller", "trigger_poller"])
async def test_a_missing_schedule_says_how_to_create_it(action: str) -> None:
    missing = RPCError("schedule not found", RPCStatusCode.NOT_FOUND, b"")
    client = ManagedClient(error=missing)

    with pytest.raises(schedule.ScheduleMissingError, match="longhaul poller install"):
        await getattr(schedule, action)(cast(Client, client))


async def test_any_other_server_error_is_not_turned_into_a_missing_schedule() -> None:
    down = RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")

    with pytest.raises(RPCError):
        await schedule.trigger_poller(cast(Client, ManagedClient(error=down)))
