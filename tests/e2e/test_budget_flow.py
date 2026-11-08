"""A task that reaches its budget asks for more and goes on when it gets it (LH-702).

This runs on the whole stack. The stack is the one of test_issue_to_pr.py: a FakeGitHub served
over HTTP, and `longhaul worker` started the way an operator starts it, against the compose
Temporal, Postgres and MinIO and the local Docker daemon. The model is the scripted provider
with tests/fixtures/scripts/calc_fix_planned.yaml, which takes eight turns: three of the planner
and five of the coder. The repo in the config of the worker has a turn budget of five, so the
task stops after the planner and the first two turns of the coder. Run `make up` and
`make sandbox-image` first. The workspace setup builds a venv and installs pytest and calc from
PyPI, so the machine needs outbound network.

The poller runs twice, as the Schedule would. The first poll starts the task. The test then waits
for the budget comment on the issue, writes `/longhaul budget +1 turns +10` as a maintainer and
polls again, which turns the comment into the `add_budget` signal. The task resumes, fixes the bug
and opens the pull request. The issue shows exactly one budget comment, the label
`longhaul:needs-human` while the task waited, and the usual comments besides.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml
from tests.e2e.test_issue_to_pr import (  # noqa: F401
    CALC,
    LABEL,
    ROOT,
    Stack,
    database,
    make_fixture_git,
    require_image,
    serve_fake,
    service_settings,
)
from tests.fakes.fake_github import FakeGitHub

from longhaul.contracts import OutcomeKind, Phase
from longhaul.durable import acts
from longhaul.durable.client import connect, get_task
from longhaul.durable.workflows.poller import PollerWorkflow
from longhaul.durable.workflows.task import TaskWorkflow

pytestmark = [pytest.mark.e2e, pytest.mark.phase7]

MAINTAINER = "maintainer"
NEEDS_HUMAN = "longhaul:needs-human"
TURN_BUDGET = 5
# Two polls, the pause, and a task that plans and codes eight turns of a script, with the
# workspace setup. One deadline covers all of it, so slow stages cannot add up to more.
TOTAL_S = 300
COMMENT_WAIT_S = 240


@pytest.fixture
def budget_stack(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Stack]:
    request.getfixturevalue("database")  # the scratch database and its settings
    suffix = uuid.uuid4().hex[:8]
    repo = f"acme/calc-{suffix}"
    fake = FakeGitHub()
    source = make_fixture_git(CALC, tmp_path / "source")
    owner, _, name = repo.partition("/")
    fake.seed_repo(repo, remote=tmp_path / "remotes" / owner / f"{name}.git", source=source)
    fake.seed_collaborator(repo, MAINTAINER, "write")
    made = Stack(tmp_path, request.getfixturevalue("database"), fake, serve_fake(fake), repo)
    made.write_config()
    # The repo gets a small turn budget. The scripted provider costs nothing, so the turns are
    # what a script can run out of.
    config = yaml.safe_load(made.config_file.read_text(encoding="utf-8"))
    config["repos"][0]["budgets"]["task_turns"] = TURN_BUDGET
    made.config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    try:
        yield made
    finally:
        made.close()


async def test_pause_and_top_up(budget_stack: Stack) -> None:
    require_image()
    try:
        async with asyncio.timeout(TOTAL_S):
            await budget_to_pr(budget_stack)
    except TimeoutError:
        pytest.fail(
            f"no pull request in {TOTAL_S} s, the target:\n{budget_stack.worker_log()}",
        )


async def wait_for(stack: Stack, what: str, done: Callable[[], bool]) -> None:
    deadline = time.monotonic() + COMMENT_WAIT_S
    while not done():
        assert time.monotonic() < deadline, f"no {what} on the issue:\n{stack.worker_log()}"
        await asyncio.sleep(0.5)


async def budget_to_pr(stack: Stack) -> None:
    fake, repo = stack.fake, stack.repo
    issue = fake.seed_issue(
        repo,
        "add() returns the difference",
        body=(CALC / "ISSUE.md").read_text(encoding="utf-8"),
        labels=[LABEL],
    )
    task_id = f"lh-acme-calc-{repo.rsplit('-', 1)[1]}-{issue}"
    stack.task_ids.append(task_id)
    worker = stack.start_worker()
    await asyncio.to_thread(stack.wait_until_ready, worker)
    s = service_settings()
    client = await connect(s["temporal"], namespace=s["namespace"])

    async def poll() -> acts.PollResult:
        return await client.execute_workflow(
            PollerWorkflow.run,
            id=f"lh-e2e-poll-{uuid.uuid4().hex[:8]}",
            task_queue=stack.queue,
        )

    started = await poll()
    assert started == acts.PollResult(repos=1, issues=1, started=1, known=0), stack.worker_log()

    # The fifth turn is done, the sixth would pass the budget, and the task asks.
    key = f"<!-- longhaul:key={task_id}:budget:1 -->"
    await wait_for(
        stack, "budget comment", lambda: fake.count_comments(repo, issue, containing=key) > 0
    )
    # The label follows the comment, so the status is read once both are there.
    await wait_for(stack, "label", lambda: NEEDS_HUMAN in fake.label_names(repo, issue))
    handle = get_task(client, task_id)
    status = await handle.query(TaskWorkflow.status)
    assert status.phase is Phase.PAUSED, stack.worker_log()
    assert status.turn_index == TURN_BUDGET
    assert status.paused_reason == "waiting for a budget top-up: task turns"
    comment = next(c for c in fake.comment_bodies(repo, issue) if key in c)
    assert comment.startswith("## Longhaul paused: turn budget"), comment
    assert f"{TURN_BUDGET} of {TURN_BUDGET} turns" in comment
    assert "`/longhaul budget +1 turns +25`" in comment and "`/longhaul cancel`" in comment

    # A poll while the task waits changes nothing: the comment is posted once.
    assert (await poll()).started == 0
    assert fake.count_comments(repo, issue, containing=":budget:") == 1

    # The maintainer answers in one comment, and the next poll turns it into the signal.
    fake.seed_comment(repo, issue, "/longhaul budget +1 turns +10", user=MAINTAINER)
    await poll()
    outcome = await handle.result()

    shown = f"worker:\n{stack.worker_log()}"
    assert outcome.kind is OutcomeKind.PR_OPENED, shown
    (pull,) = fake.pulls(repo)
    assert pull["html_url"] == outcome.pr_url
    assert "3 passed" in pull["body"], pull["body"]
    assert fake.count_comments(repo, issue, containing=":budget:") == 1, shown

    # The label came with the comment, went with the answer, and the pull request label stayed.
    assert [(e.action, e.label) for e in fake.label_events(repo, issue)] == [
        ("labeled", "longhaul:working"),
        ("labeled", NEEDS_HUMAN),
        ("unlabeled", NEEDS_HUMAN),
        ("unlabeled", "longhaul:working"),
        ("labeled", "longhaul:pr-open"),
    ]
    assert sorted(fake.label_names(repo, issue)) == [LABEL, "longhaul:pr-open"]
