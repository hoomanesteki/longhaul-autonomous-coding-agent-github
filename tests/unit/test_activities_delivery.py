"""build_pr_branch and deliver: how each failure reaches Temporal (LH-306).

The collaborators are fakes, and the git steps are replaced, so these tests are about the
decisions of the activities and nothing else: which failures are retried, which are recorded, and
what the retry policies can rely on. tests/integration/test_deliver.py runs the same activities
with real git against a FakeGitHub remote.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import CheckpointError, RepoNotFoundError
from longhaul.checkpoints.push import ForbiddenBranchError
from longhaul.checkpoints.squash import FileStat, NothingToDeliverError, PrBranch
from longhaul.contracts import Plan, PlanStep
from longhaul.db.records import CheckpointRecord, MemoryRecorder, ToolCallRecord
from longhaul.durable import acts
from longhaul.durable.activities import delivery
from longhaul.durable.activities.delivery import DeliveryActivities
from longhaul.github.client import GitHubFatal, GitHubRetryable
from longhaul.github.side_effects import KeyReuseError, MemoryLedger

pytestmark = [pytest.mark.unit, pytest.mark.phase3]

ROOT = Path(__file__).resolve().parents[2]
TASK = "lh-acme-calc-42"
SHA = "a" * 40
BASE = "b" * 40
AUTHOR = "longhaul <longhaul@users.noreply.github.com>"
PR_URL = "https://github.com/acme/calc/pull/7"


class FakeRemote:
    def __init__(self, default: str | Exception = "main") -> None:
        self.default = default

    async def default_branch(self, repo: str) -> str:
        if isinstance(self.default, Exception):
            raise self.default
        return self.default


class BrokenRecorder(MemoryRecorder):
    async def record_event(self, task_id: str, type: str, payload: Any) -> None:
        raise ConnectionError("the database is down")


def make(
    remote: FakeRemote | None = None,
    recorder: MemoryRecorder | None = None,
    blobs: MemoryBlobStore | None = None,
    templates: Path = ROOT / "templates",
) -> DeliveryActivities:
    return DeliveryActivities(
        None,  # type: ignore[arg-type]
        remote or FakeRemote(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        MemoryLedger(),
        recorder or MemoryRecorder(),
        blobs or MemoryBlobStore(),
        author=AUTHOR,
        templates=templates,
    )


def build_input(summary: str = "Fixed add().") -> acts.BuildPrBranchInput:
    return acts.BuildPrBranchInput(
        task_id=TASK, repo="acme/calc", summary=summary, base_sha=BASE, head_sha=SHA
    )


def deliver_input(branch: str = "longhaul-pr/42") -> acts.DeliverInput:
    return acts.DeliverInput(
        task_id=TASK,
        repo="acme/calc",
        issue_number=42,
        summary="Fixed add().",
        branch=branch,
        base_branch="main",
        base_sha=BASE,
        head_sha=SHA,
        title="fix(calc): fix add()",
    )


def raising(error: Exception) -> Any:
    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    return fail


def returning(value: Any) -> Any:
    async def give(*args: Any, **kwargs: Any) -> Any:
        return value

    return give


async def run(fn: Any, inp: Any) -> Any:
    return await ActivityEnvironment().run(fn, inp)


# ---------- the registration ----------
def test_the_activities_are_registered_under_the_names_of_acts() -> None:
    names = {fn.__name__ for fn in make().activities}
    assert names == {acts.BUILD_PR_BRANCH.name, acts.DELIVER.name}


def test_an_author_git_would_misread_is_refused_when_the_activities_are_built() -> None:
    with pytest.raises(ValueError, match="LONGHAUL_GIT_AUTHOR"):
        DeliveryActivities(
            None,  # type: ignore[arg-type]
            FakeRemote(),  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            MemoryLedger(),
            MemoryRecorder(),
            MemoryBlobStore(),
            author="no address",
            templates=ROOT / "templates",
        )


# ---------- build_pr_branch ----------
async def test_the_branch_is_built_from_the_title_the_summary_gives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def build(task_id: str, groups: Any, **kwargs: Any) -> PrBranch:
        seen.update(task_id=task_id, groups=groups, **kwargs)
        return PrBranch("longhaul-pr/42", SHA, BASE)

    monkeypatch.setattr(delivery, "build_pr_branch", build)

    out = await run(make().build_pr_branch, build_input())

    assert out == acts.PrBranchOutput(
        branch="longhaul-pr/42",
        head_sha=SHA,
        base_sha=BASE,
        base_branch="main",
        title="fix(calc): fix add()",
    )
    assert [tuple(g) for g in seen["groups"]] == [("fix(calc): fix add()", SHA)]
    assert (seen["default_branch"], seen["author"]) == ("main", AUTHOR)
    assert seen["base_sha"] == BASE, "the base is the one the workflow handed over"


def three_steps(*statuses: str) -> Plan:
    steps = [
        PlanStep(
            id=f"s{n}",
            description=f"Change file {n}",
            done_when="the tests pass",
            status=status,  # type: ignore[arg-type]
        )
        for n, status in enumerate(statuses, start=1)
    ]
    return Plan(summary="plan", steps=steps, acceptance_checks=["pytest -q"])


async def groups_built_for(
    recorder: MemoryRecorder, plan: Plan | None, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, str]]:
    seen: dict[str, Any] = {}

    async def build(task_id: str, groups: Any, **kwargs: Any) -> PrBranch:
        seen["groups"] = groups
        return PrBranch("longhaul-pr/42", SHA, BASE)

    monkeypatch.setattr(delivery, "build_pr_branch", build)
    inp = build_input().model_copy(update={"plan": plan})
    await run(make(recorder=recorder).build_pr_branch, inp)
    return [tuple(g) for g in seen["groups"]]


@pytest.mark.phase5
async def test_a_task_with_a_plan_gets_one_group_for_each_done_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = MemoryRecorder()
    for n, sha in enumerate(["1" * 40, "2" * 40], start=1):
        await recorder.record_checkpoint(
            CheckpointRecord(TASK, sha, "step", turn_index=n, step_id=f"s{n}")
        )

    groups = await groups_built_for(recorder, three_steps("done", "done", "todo"), monkeypatch)

    assert groups == [
        ("fix(calc): change file 1", "1" * 40),
        ("fix(calc): change file 2", "2" * 40),
        ("fix(calc): fix add()", SHA),
    ]


@pytest.mark.phase5
async def test_the_tool_checkpoints_of_a_task_are_not_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = MemoryRecorder()
    await recorder.record_checkpoint(CheckpointRecord(TASK, "1" * 40, "tool", turn_index=1))

    groups = await groups_built_for(recorder, three_steps("done", "todo"), monkeypatch)

    assert groups == [("fix(calc): fix add()", SHA)]


@pytest.mark.phase5
async def test_a_task_without_a_plan_never_reads_the_step_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoSteps(MemoryRecorder):
        async def step_checkpoints(self, task_id: str) -> list[CheckpointRecord]:
            raise AssertionError("a task without a plan has no steps to read")

    groups = await groups_built_for(NoSteps(), None, monkeypatch)

    assert groups == [("fix(calc): fix add()", SHA)]


@pytest.mark.phase5
async def test_the_title_of_the_pull_request_is_still_the_summary_with_a_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def build(task_id: str, groups: Any, **kwargs: Any) -> PrBranch:
        return PrBranch("longhaul-pr/42", SHA, BASE)

    monkeypatch.setattr(delivery, "build_pr_branch", build)
    inp = build_input().model_copy(update={"plan": three_steps("done", "done")})

    out = await run(make().build_pr_branch, inp)

    assert out.title == "fix(calc): fix add()"


async def test_a_task_that_changed_nothing_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "build_pr_branch", raising(NothingToDeliverError("no change")))

    with pytest.raises(ApplicationError) as caught:
        await run(make().build_pr_branch, build_input())

    assert (caught.value.type, caught.value.non_retryable) == (acts.NOTHING_TO_DELIVER, True)


async def test_a_branch_that_would_be_the_default_is_recorded_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = MemoryRecorder()
    monkeypatch.setattr(delivery, "build_pr_branch", raising(ForbiddenBranchError("no")))

    with pytest.raises(ApplicationError) as caught:
        await run(make(recorder=recorder).build_pr_branch, build_input())

    assert (caught.value.type, caught.value.non_retryable) == (acts.FORBIDDEN_BRANCH, True)
    (event,) = recorder.events
    assert (event.type, event.payload["branch"]) == ("branch_refused", "longhaul-pr/42")


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (GitHubRetryable("GitHub answered 502"), True),
        (GitHubFatal("Not Found"), False),
    ],
)
async def test_a_github_error_keeps_the_advice_of_the_client(
    error: Exception, retryable: bool
) -> None:
    with pytest.raises(ApplicationError) as caught:
        await run(make(FakeRemote(error)).build_pr_branch, build_input())

    assert caught.value.non_retryable is not retryable
    assert caught.value.type == type(error).__name__


@pytest.mark.parametrize(
    "error",
    [ValueError("bad task id"), RepoNotFoundError("no repo on this host")],
    ids=["a bad name", "the host lost the repo"],
)
async def test_an_input_that_no_retry_can_fix_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(delivery, "build_pr_branch", raising(error))

    with pytest.raises(ApplicationError) as caught:
        await run(make().build_pr_branch, build_input())

    assert caught.value.non_retryable


async def test_a_git_failure_is_left_to_the_retry_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "build_pr_branch", raising(CheckpointError("git failed")))

    with pytest.raises(CheckpointError):
        await run(make().build_pr_branch, build_input())


async def test_a_summary_over_the_payload_limit_is_not_retried() -> None:
    with pytest.raises(ApplicationError) as caught:
        await run(make().build_pr_branch, build_input("x" * 300_000))

    assert (caught.value.type, caught.value.non_retryable) == ("PayloadTooLargeError", True)


# ---------- deliver ----------
def patch_ready(monkeypatch: pytest.MonkeyPatch, *, pushed: str = SHA) -> None:
    """Replace the git steps and the pull request call with ones that succeed."""
    monkeypatch.setattr(delivery, "push_branch", returning(pushed))
    monkeypatch.setattr(delivery, "file_stats", returning([FileStat("a.py", 1, 1)]))
    monkeypatch.setattr(delivery, "create_pr", returning(PR_URL))


async def test_deliver_returns_the_url_of_the_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_ready(monkeypatch)
    activities = make()

    assert await run(activities.deliver, deliver_input()) == PR_URL
    assert list(activities._ledger.rows) == [f"{TASK}:push:1"]  # type: ignore[attr-defined]


async def test_the_pull_request_is_a_draft_into_the_default_branch_under_a_stable_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_ready(monkeypatch)
    seen: list[Any] = []

    async def create(client: Any, ledger: Any, request: Any) -> str:
        seen.append(request)
        return PR_URL

    monkeypatch.setattr(delivery, "create_pr", create)

    await run(make().deliver, deliver_input())

    (request,) = seen
    assert (request.head, request.base, request.draft) == ("longhaul-pr/42", "main", True)
    assert request.title == "fix(calc): fix add()"
    assert request.idempotency_key == f"{TASK}:pr:1"
    assert "Closes #42" in request.body


async def test_a_pull_request_whose_work_was_verified_is_ready_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_ready(monkeypatch)
    seen: list[Any] = []

    async def create(client: Any, ledger: Any, request: Any) -> str:
        seen.append(request)
        return PR_URL

    monkeypatch.setattr(delivery, "create_pr", create)
    inp = deliver_input().model_copy(update={"verified_tests": "41 passed"})

    await run(make().deliver, inp)

    (request,) = seen
    assert request.draft is False
    assert "Verified: 41 passed" in request.body
    assert request.body.rstrip().endswith("Closes #42")
    assert request.idempotency_key == f"{TASK}:pr:1"


async def test_a_pull_request_of_partial_work_refers_to_the_issue_and_stays_a_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Merging it must not close an issue that the task did not finish.
    patch_ready(monkeypatch)
    seen: list[Any] = []

    async def create(client: Any, ledger: Any, request: Any) -> str:
        seen.append(request)
        return PR_URL

    monkeypatch.setattr(delivery, "create_pr", create)
    inp = deliver_input().model_copy(update={"partial": True})

    await run(make().deliver, inp)

    (request,) = seen
    assert request.draft is True
    assert request.body.rstrip().endswith("Refs #42")
    assert "Closes" not in request.body


async def test_the_body_of_a_verified_pull_request_is_the_same_on_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # create_pr refuses a reused key with other text, and a retried deliver builds the body again.
    patch_ready(monkeypatch)
    seen: list[Any] = []

    async def create(client: Any, ledger: Any, request: Any) -> str:
        seen.append(request)
        return PR_URL

    monkeypatch.setattr(delivery, "create_pr", create)
    inp = deliver_input().model_copy(update={"verified_tests": "41 passed"})
    activities = make()

    await run(activities.deliver, inp)
    await run(activities.deliver, inp)

    assert seen[0] == seen[1]
    assert seen[0].body.encode() == seen[1].body.encode()


async def test_a_push_to_a_protected_name_is_recorded_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_ready(monkeypatch)
    monkeypatch.setattr(delivery, "push_branch", raising(ForbiddenBranchError("no")))
    recorder = MemoryRecorder()

    with pytest.raises(ApplicationError) as caught:
        await run(make(recorder=recorder).deliver, deliver_input("main"))

    assert (caught.value.type, caught.value.non_retryable) == (acts.FORBIDDEN_BRANCH, True)
    assert [(e.type, e.payload["branch"]) for e in recorder.events] == [("branch_refused", "main")]


async def test_an_event_that_cannot_be_written_does_not_hide_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_ready(monkeypatch)
    monkeypatch.setattr(delivery, "push_branch", raising(ForbiddenBranchError("no")))

    with pytest.raises(ApplicationError) as caught:
        await run(make(recorder=BrokenRecorder()).deliver, deliver_input("main"))

    assert caught.value.type == acts.FORBIDDEN_BRANCH


async def test_a_branch_that_is_not_the_one_that_was_built_is_not_pushed_as_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The agent can write refs in its repo. A moved branch fails the attempt, and the
    # next attempt looks again.
    patch_ready(monkeypatch, pushed="c" * 40)
    activities = make()

    with pytest.raises(CheckpointError, match="not at"):
        await run(activities.deliver, deliver_input())

    assert not activities._ledger.rows  # type: ignore[attr-defined]


async def test_a_key_that_was_used_for_another_effect_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_ready(monkeypatch)
    monkeypatch.setattr(delivery, "create_pr", raising(KeyReuseError("used before")))

    with pytest.raises(ApplicationError) as caught:
        await run(make().deliver, deliver_input())

    assert (caught.value.type, caught.value.non_retryable) == ("KeyReuseError", True)


async def test_a_missing_template_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_ready(monkeypatch)

    with pytest.raises(ApplicationError) as caught:
        await run(make(templates=tmp_path).deliver, deliver_input())

    assert (caught.value.type, caught.value.non_retryable) == ("TemplateNotFound", True)


async def test_a_github_failure_while_opening_the_pull_request_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_ready(monkeypatch)
    monkeypatch.setattr(delivery, "create_pr", raising(GitHubRetryable("502")))

    with pytest.raises(ApplicationError) as caught:
        await run(make().deliver, deliver_input())

    assert (caught.value.type, caught.value.non_retryable) == ("GitHubRetryable", False)


# ---------- the test line of the body ----------
async def last_request(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Any:
    patch_ready(monkeypatch)
    seen: list[Any] = []

    async def create(client: Any, ledger: Any, request: Any) -> str:
        seen.append(request)
        return PR_URL

    monkeypatch.setattr(delivery, "create_pr", create)
    await run(make(**kwargs).deliver, deliver_input())
    return seen[0]


def a_tests_row(**fields: Any) -> ToolCallRecord:
    values: dict[str, Any] = {
        "id": "t1",
        "task_id": TASK,
        "turn_index": 2,
        "name": "run_tests",
        "ok": True,
        "exit_code": 1,
    }
    return ToolCallRecord(**{**values, **fields})


async def test_the_body_reports_the_counts_of_the_last_test_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blobs, recorder = MemoryBlobStore(), MemoryRecorder()
    await blobs.put(
        b"exit code: 1\nsummary: 1 failed, 4 passed\n1 failed, 4 passed in 0.5s",
        key="tasks/t/out.txt",
    )
    await recorder.record_tool_call(a_tests_row(output_ref="tasks/t/out.txt"))

    request = await last_request(monkeypatch, blobs=blobs, recorder=recorder)

    assert "1 failed, 4 passed (exit code 1)" in request.body


async def test_a_test_call_with_no_output_reference_has_no_summary_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = MemoryRecorder()
    await recorder.record_tool_call(a_tests_row(output_ref=None))

    request = await last_request(monkeypatch, recorder=recorder)

    assert "no pytest summary line" in request.body
