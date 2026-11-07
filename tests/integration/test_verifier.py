"""The verifier checks a coder's work before a pull request is marked ready (LH-504).

A scripted planner submits a plan and a scripted coder works it. When the coder calls
`submit_for_review`, the phase becomes `verifying`: the workflow runs the test command and the
acceptance commands of the plan (the pre-check), then a verifier with a fresh transcript reads the
plan, the diff and the pre-check and calls `submit_verdict`. The work passes only when the
pre-check passed and the verdict says so. Otherwise the coder is sent back with the issues, up to
`max_verify_rounds` times, and then the task ends as `stuck`.

Two kinds of worker are used. Most tests use a fake sandbox that holds a small git repo (so that
`git_diff` has something to show), the memory stores and the Temporal test server. The test that
the story names for the whole path (test_send_back_then_pass) runs a task of an issue on a real
container, a real bare repo and a FakeGitHub whose remote is a local bare repo, so the pull request
is built and opened for real. Run `make sandbox-image` first. The setup command is empty, so
nothing is installed and the tests need no outbound network.

Every task id, container, volume, task queue and bare repo has a random suffix, and each test
removes what it made.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from temporalio import activity
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from tests.fakes.calc_remote import Github, github_for, seed_calc
from tests.fakes.fake_github import FakeGitHub, git

from longhaul.agent.loop import LoopConfig
from longhaul.agent.prompts import render_plan
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.contracts import (
    BlobRef,
    Budgets,
    LLMResponse,
    Message,
    Outcome,
    OutcomeKind,
    Phase,
    Plan,
    RepoSettings,
    Role,
    Sandbox,
    SandboxLimits,
    TaskSpec,
    TextBlock,
    ToolResultBlock,
)
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.delivery import DeliveryActivities
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.plan import (
    PlanActivities,
    PlanCommentActivities,
    StepActivities,
)
from longhaul.durable.activities.tools import NoCheckpoints, ToolActivities
from longhaul.durable.activities.workspace import WorkspaceActivities
from longhaul.durable.client import start_task
from longhaul.durable.payload_converter import guarded_data_converter
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.github.side_effects import MemoryLedger, SideEffectActivities
from longhaul.providers.base import parse_role_line
from longhaul.providers.scripted import ScriptedProvider
from longhaul.roles import coder, planner
from longhaul.roles import verifier as verifier_role
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase5]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
TEMPLATES = ROOT / "templates"
IMAGE = "longhaul-sandbox:py312"
MODULE = "src/calc/__init__.py"
NEW_TEST_FILE = "tests/test_negative.py"
CHANGELOG = "CHANGELOG.md"
REPO = "acme/calc"
BASE = "b" * 40
AUTHOR = "longhaul <longhaul@users.noreply.github.com>"
WAIT_S = 60.0
DOCKER_WAIT_S = 600.0
CFG = LoopConfig(model="test-model", max_turns=40, max_tokens_per_turn=1000, max_tokens_retry=5000)
SHORT_HEARTBEAT = acts.Tuning(heartbeat_timeout_s=2)
SUMMARY = "Fixed add() so it returns the sum of its arguments. All tests pass."
NEW_TEST = (
    "from calc import add\n\n\ndef test_add_negative() -> None:\n    assert add(-2, -3) == -5\n"
)

# The image has no pytest and the sandbox has no network, so the test command of the real run is a
# stdlib one-liner that prints a pytest-like line. The fake sandbox runs commands on this machine.
TEST_COMMAND = (
    "python -c \"import sys; sys.path.insert(0, 'src'); from calc import add; "
    "assert add(2, 3) == 5; print('1 passed')\""
)
BUG_GONE = 'grep -q "return a + b" src/calc/__init__.py'
HAS_ADD = 'grep -q "def add" src/calc/__init__.py'

PLAN_INPUT: dict[str, Any] = {
    "summary": "add() subtracts. Change the operator, then cover negative numbers.",
    "steps": [
        {
            "id": "s1",
            "description": "Change the operator in add()",
            "files": [MODULE],
            "done_when": "add(2, 3) returns 5",
        },
        {
            "id": "s2",
            "description": "Add a test for negative numbers",
            "files": [NEW_TEST_FILE],
            "done_when": "the new test passes",
        },
    ],
    "acceptance_checks": [f"$ {HAS_ADD}", "add(-2, -3) returns -5"],
    "risks": [],
}


# ---------- the script ----------
def tool(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "input": arguments}


def turn(role: str, index: int, *calls: dict[str, Any], **match: Any) -> dict[str, Any]:
    return {
        "match": {"role": role, "turn_index": index, **match},
        "respond": {"tool_calls": list(calls)},
    }


def coder_turn(index: int, *calls: dict[str, Any]) -> dict[str, Any]:
    return turn("coder", index, *calls)


def verifier_turn(index: int, *calls: dict[str, Any], **match: Any) -> dict[str, Any]:
    return turn("verifier", index, *calls, **match)


def step(step_id: str, status: str, note: str | None = None) -> dict[str, Any]:
    arguments = {"step_id": step_id, "status": status}
    if note is not None:
        arguments["note"] = note
    return tool("update_step", **arguments)


def verdict(
    passed: bool,
    summary: str,
    *issues: str,
    send_back_step: str | None = None,
    run: str = "pytest -q",
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "passed": passed,
        "tests_run": run,
        "tests_summary": summary,
        "issues": list(issues),
    }
    if send_back_step is not None:
        arguments["send_back_step"] = send_back_step
    return tool("submit_verdict", **arguments)


FIX = tool("edit_file", path=MODULE, old_str="    return a - b", new_str="    return a + b")
ADD_TEST = tool("write_file", path=NEW_TEST_FILE, content=NEW_TEST)
TOUCH = tool("write_file", path=CHANGELOG, content="- add() adds again\n")
SUBMIT = tool("submit_for_review", summary=SUMMARY)
PASS = verdict(True, "2 passed")
UNTESTED = "add() is not tested with negative numbers, so the sign handling is unproven"
EDGE_CASE = "the empty-string case is not handled"


def write_script(tmp_path: Path, steps: list[dict[str, Any]]) -> Path:
    path = tmp_path / f"script-{uuid.uuid4().hex[:6]}.yaml"
    path.write_text(yaml.safe_dump({"steps": steps}))
    return path


def plan_run() -> list[dict[str, Any]]:
    return [turn("planner", 0, tool("submit_plan", **PLAN_INPUT))]


def clean_run() -> list[dict[str, Any]]:
    """Both steps done and a first review that passes."""
    return [
        *plan_run(),
        coder_turn(0, step("s1", "doing"), FIX),
        coder_turn(1, step("s1", "done", "add() adds"), ADD_TEST),
        coder_turn(2, step("s2", "done", "negative numbers are tested")),
        coder_turn(3, SUBMIT),
        verifier_turn(0, PASS),
    ]


def stuck_run() -> list[dict[str, Any]]:
    """Three submissions, each one sent back for the same edge case."""
    fail = verdict(False, "1 failed", EDGE_CASE, send_back_step="s1")
    return [
        *plan_run(),
        coder_turn(0, step("s1", "doing"), FIX),
        coder_turn(1, SUBMIT),
        coder_turn(2, TOUCH),
        coder_turn(3, SUBMIT),
        coder_turn(4, TOUCH),
        coder_turn(5, SUBMIT),
        verifier_turn(0, fail),
    ]


# ---------- the provider ----------
@dataclass
class Request:
    role: str | None
    messages: list[Message]
    tool_names: list[str]
    system: str


class Probe(ScriptedProvider):
    """A scripted provider that keeps its requests and can hold one call of a role.

    `hold` is a role and the number of its calls to let through before the next one waits for
    `gate`, so a test can look at the workflow while that role is between two turns.
    """

    def __init__(self, script: Path, hold: tuple[str, int] | None = None) -> None:
        super().__init__(script)
        self.requests: list[Request] = []
        self.gate = asyncio.Event()
        self.hold = hold

    async def complete(self, **kwargs: Any) -> LLMResponse:
        role = parse_role_line(kwargs["system"])
        names = [t.name for t in kwargs["tools"]]
        self.requests.append(Request(role, list(kwargs["messages"]), names, kwargs["system"]))
        if (
            self.hold is not None
            and role == self.hold[0]
            and len(self.of(role)) - 1 == self.hold[1]
        ):
            await self.gate.wait()
        return await super().complete(**kwargs)

    def of(self, role: str) -> list[Request]:
        return [r for r in self.requests if r.role == role]


def blocks(message: Message) -> list[str]:
    return [b.text for b in message.content if isinstance(b, TextBlock)]


def results_of(message: Message) -> list[str]:
    return [b.content for b in message.content if isinstance(b, ToolResultBlock)]


def verdict_events(recorder: MemoryRecorder) -> list[dict[str, Any]]:
    return [e.payload for e in recorder.events if e.type == "verdict"]


def plan_with(*now: str) -> Plan:
    """The plan of the scripts, with the status of each step as given."""
    plan = Plan.model_validate(PLAN_INPUT)
    steps = [
        s.model_copy(update={"status": status}) for s, status in zip(plan.steps, now, strict=True)
    ]
    return plan.model_copy(update={"steps": steps})


# ---------- a worker with a fake sandbox ----------
class Boxes:
    """The workspace activities of the light tests: one fake sandbox per task, with calc in it.

    The sandbox is a git repo with the commit tagged `base`, which is what `git_diff` compares
    with. The checkpoints are not real here: tool calls are not committed, and nothing restores.
    """

    def __init__(self) -> None:
        self.boxes: dict[str, FakeSandbox] = {}

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return self.boxes.get(sandbox_id)

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        box = FakeSandbox(f"sbx-{spec.task_id}")
        for path in sorted(CALC.rglob("*")):
            parts = set(path.relative_to(CALC).parts)
            if path.is_file() and not parts & {"REFERENCE.patch", "__pycache__", ".pytest_cache"}:
                await box.write_file(str(path.relative_to(CALC)), path.read_bytes())
        committed = await box.exec(
            "git init -q && git add -A && git -c user.name=t -c user.email=t@localhost "
            "-c commit.gpgsign=false -c core.hooksPath=/dev/null commit -q -m base "
            "&& git tag base",
            timeout_s=60,
        )
        assert committed.exit_code == 0, committed.stderr
        self.boxes[box.id] = box
        return acts.PrepareWorkspaceOutput(sandbox_id=box.id, base_sha=BASE, head_sha=BASE)

    @activity.defn(name=acts.CLEANUP.name)
    async def cleanup(self, inp: acts.CleanupInput) -> None:
        return None

    @activity.defn(name=acts.ENSURE_SANDBOX.name)
    async def ensure_sandbox(self, inp: acts.EnsureSandboxInput) -> str:
        return inp.sandbox_id or f"sbx-{inp.task_id}"

    @activity.defn(name=acts.BUNDLE_CHECKPOINT.name)
    async def bundle_checkpoint(self, inp: acts.BundleCheckpointInput) -> BlobRef:
        key = f"tasks/{inp.task_id}/ckpt/{inp.turn_index}.bundle"
        return BlobRef(key=key, sha256="0" * 64, size=1)

    async def destroy_all(self) -> None:
        for box in self.boxes.values():
            await box.destroy()


@dataclass
class Light:
    queue: str
    provider: Probe
    recorder: MemoryRecorder
    blobs: MemoryBlobStore
    boxes: Boxes


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


def local_spec(test_command: str | None = None, **fields: Any) -> TaskSpec:
    """A task on a folder: it is not picked up on GitHub, and it ends without a pull request."""
    repo = "local:/work/calc"
    return TaskSpec(
        task_id=f"lh-local-calc-{uuid.uuid4().hex[:8]}",
        repo=repo,
        issue_number=None,
        title="add() subtracts",
        body="`calc.add` subtracts its arguments instead of adding them.",
        settings=RepoSettings(repo=repo, test_command=test_command, budgets=Budgets(**fields)),
    )


@asynccontextmanager
async def light(
    env: WorkflowEnvironment, script: Path, *, hold: tuple[str, int] | None = None
) -> AsyncIterator[Light]:
    blobs = MemoryBlobStore()
    recorder = MemoryRecorder()
    provider = Probe(script, hold)
    registry = default_registry(test_command="true", blobs=blobs)
    llm = LlmActivities(
        provider,
        blobs,
        recorder,
        registry.specs(),
        lambda role: CFG,
        role_tools={
            Role.PLANNER: planner.tool_specs(registry.specs()),
            Role.VERIFIER: verifier_role.tool_specs(registry.specs()),
        },
        planned_tools=coder.tool_specs(registry.specs()),
    )
    boxes = Boxes()
    tools = ToolActivities(registry, boxes, blobs, recorder, checkpoints=NoCheckpoints())
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    try:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[TaskWorkflow],
            activities=[
                *llm.activities,
                *tools.activities,
                *EventActivities(recorder).activities,
                *PlanActivities(recorder).activities,
                *StepActivities(recorder).activities,
                boxes.prepare_workspace,
                boxes.cleanup,
                boxes.ensure_sandbox,
                boxes.bundle_checkpoint,
            ],
            workflow_runner=workflow_runner(),
        ):
            yield Light(queue, provider, recorder, blobs, boxes)
    finally:
        await boxes.destroy_all()


async def wait_until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def result_of(handle: WorkflowHandle[Any, Outcome], wait_s: float = WAIT_S) -> Outcome:
    return await asyncio.wait_for(handle.result(), wait_s)


async def planned_status(handle: WorkflowHandle[Any, Outcome]) -> acts.PlannedStatus:
    # The result type can be set only for a query asked for by name.
    return await handle.query("status", result_type=acts.PlannedStatus)


async def run_light(
    env: WorkflowEnvironment,
    tmp_path: Path,
    steps: list[dict[str, Any]],
    spec: TaskSpec | None = None,
    **options: Any,
) -> tuple[Light, Outcome, acts.PlannedStatus]:
    """Run a planned task to its end on a light worker and return the world, outcome and status."""
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(
            env.client, spec or local_spec(), task_queue=world.queue, plan=True, **options
        )
        outcome = await result_of(handle)
        status = await planned_status(handle)
    return world, outcome, status


# ---------- send back, fix, pass ----------
@dataclass
class Real:
    client: Client
    task_id: str
    number: int
    queue: str
    fake: FakeGitHub
    github: Github
    store: GitCheckpointStore
    manager: DockerSandboxManager
    recorder: MemoryRecorder
    provider: Probe
    base_sha: str
    spec: TaskSpec = field(init=False)

    def __post_init__(self) -> None:
        self.spec = TaskSpec(
            task_id=self.task_id,
            repo=REPO,
            issue_number=self.number,
            title="add() subtracts",
            body="`calc.add` subtracts its arguments instead of adding them.",
            settings=RepoSettings(repo=REPO, test_command=TEST_COMMAND, setup_command=""),
        )

    def bare(self, *args: str) -> str:
        return git(f"--git-dir={self.store.repo_path(self.task_id)}", *args)


@asynccontextmanager
async def real(env: WorkflowEnvironment, tmp_path: Path, script: Path) -> AsyncIterator[Real]:
    fake = FakeGitHub()
    base_sha = seed_calc(fake, tmp_path / "gh")
    number = fake.seed_issue(REPO, "add() subtracts")
    task_id = f"lh-verify{uuid.uuid4().hex[:8]}-{number}"
    github = github_for(fake)
    blobs = MemoryBlobStore()
    store = GitCheckpointStore(blobs, root=tmp_path / "ckpt")
    manager = DockerSandboxManager()
    recorder = MemoryRecorder()
    provider = Probe(script)
    registry = default_registry(test_command=TEST_COMMAND, blobs=blobs)
    llm = LlmActivities(
        provider,
        blobs,
        recorder,
        registry.specs(),
        lambda role: CFG,
        role_tools={
            Role.PLANNER: planner.tool_specs(registry.specs()),
            Role.VERIFIER: verifier_role.tool_specs(registry.specs()),
        },
        planned_tools=coder.tool_specs(registry.specs()),
    )
    tools = ToolActivities(registry, manager, blobs, recorder, checkpoints=store)
    workspace = WorkspaceActivities(
        manager, store, recorder, image=IMAGE, limits=SandboxLimits(), github=github.remote
    )
    ledger = MemoryLedger()
    delivery = DeliveryActivities(
        store,
        github.remote,
        github.client,
        ledger,
        recorder,
        blobs,
        author=AUTHOR,
        templates=TEMPLATES,
    )
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    try:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[TaskWorkflow],
            activities=[
                *llm.activities,
                *tools.activities,
                *workspace.activities,
                *EventActivities(recorder).activities,
                *PlanActivities(recorder).activities,
                *StepActivities(recorder).activities,
                *PlanCommentActivities(github.client, ledger, templates=TEMPLATES).activities,
                *SideEffectActivities(github.client, ledger).activities,
                *delivery.activities,
            ],
            workflow_runner=workflow_runner(),
        ):
            yield Real(
                env.client,
                task_id,
                number,
                queue,
                fake,
                github,
                store,
                manager,
                recorder,
                provider,
                base_sha,
            )
    finally:
        await manager.cleanup_task(task_id)
        await store.cleanup_task(task_id)
        await github.aclose()


def send_back_run() -> list[dict[str, Any]]:
    """The coder skips the test for negative numbers. The first review sends it back for that.

    The review of the second round tells itself apart by the diff, which holds the new test. The
    verifier reads the diff first (a tool call) and then answers, in both rounds.
    """
    return [
        *plan_run(),
        coder_turn(0, step("s1", "doing"), FIX),
        coder_turn(1, step("s1", "done", "add() adds")),
        coder_turn(2, step("s2", "skipped", "the fix is one line"), SUBMIT),
        coder_turn(3, ADD_TEST, step("s2", "done", "negative numbers are tested")),
        coder_turn(4, SUBMIT),
        verifier_turn(1, PASS, request_contains="+def test_add_negative"),
        verifier_turn(1, verdict(False, "1 passed", UNTESTED, send_back_step="s2")),
        verifier_turn(0, tool("git_diff")),
    ]


async def test_send_back_then_pass(env: WorkflowEnvironment, tmp_path: Path) -> None:
    """The first review fails on a planted missing edge case, the coder fixes it, the second
    passes, and the pull request is ready for review, not a draft."""
    async with real(env, tmp_path, write_script(tmp_path, send_back_run())) as world:
        handle = await start_task(
            world.client, world.spec, task_queue=world.queue, plan=True, tuning=SHORT_HEARTBEAT
        )
        outcome = await result_of(handle, DOCKER_WAIT_S)
        status = await planned_status(handle)

        assert outcome.kind is OutcomeKind.PR_OPENED, outcome.summary
        assert outcome.pr_url is not None
        assert status.phase is Phase.DONE

        # Two reviews: the first failed, and the second passed.
        reviews = verdict_events(world.recorder)
        assert [(r["round"], r["passed"]) for r in reviews] == [(1, False), (2, True)]
        assert reviews[0]["issues"] == [UNTESTED]
        assert reviews[0]["send_back_step"] == "s2"
        assert reviews[0]["precheck_passed"] is True
        assert len(world.provider.of("verifier")) == 4

        # The coder was told what to fix, in the transcript it already had.
        asked = world.provider.of("coder")
        assert len(asked) == 5
        sent_back = asked[3].messages[-1]
        assert sent_back.role == "user"
        assert blocks(sent_back)[-1] == f"Review failed: {UNTESTED}. Fix step s2."
        assert len(results_of(sent_back)) == 2  # the answers to the calls of its last turn
        # Past the first message, whose plan block moved with the step updates, the transcript is
        # the one it had: the earlier turns, its last reply and the answers to that reply.
        assert asked[3].messages[1:-1] == [*asked[2].messages[1:], asked[3].messages[-2]]

        # The pull request is ready for review, and says what was verified.
        (pull,) = world.fake.pulls(REPO)
        assert pull["draft"] is False
        assert "Verified: 2 passed" in pull["body"]
        assert pull["body"].rstrip().endswith(f"Closes #{world.number}")

        # Checking the work changed nothing in it: the pushed tree is the tree of the last
        # checkpoint, and each step made its commit.
        branch = f"longhaul-pr/{world.number}"
        pushed = world.fake.branches(REPO)[branch]
        scratch = await world.store.head(world.task_id)
        assert world.bare("rev-parse", f"{pushed}^{{tree}}") == world.bare(
            "rev-parse", f"{scratch}^{{tree}}"
        )
        commits = world.bare("rev-list", "--reverse", f"{world.base_sha}..{pushed}").splitlines()
        subjects = [world.bare("log", "-1", "--format=%s", sha) for sha in commits]
        assert subjects == [
            "fix(calc): change the operator in add()",
            "fix(calc): add a test for negative numbers",
        ]


# ---------- the pre-check has the last word ----------
async def test_precheck_overrides_model(env: WorkflowEnvironment, tmp_path: Path) -> None:
    """A failing pre-check counts as a fail, though the model says the work passes.

    The test command is true only once add() adds. The coder submits too early, the pre-check
    fails, and the verifier, which is scripted to pass everything, is overruled. The coder fixes
    the code and the second review passes.
    """
    steps = [
        *plan_run(),
        coder_turn(0, step("s1", "doing")),
        coder_turn(1, SUBMIT),
        coder_turn(2, FIX, step("s1", "done", "add() adds")),
        coder_turn(3, SUBMIT),
        verifier_turn(0, verdict(True, "3 passed", run=BUG_GONE)),
    ]
    world, outcome, status = await run_light(env, tmp_path, steps, local_spec(BUG_GONE))

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert status.phase is Phase.DONE
    reviews = verdict_events(world.recorder)
    assert [(r["round"], r["passed"]) for r in reviews] == [(1, False), (2, True)]
    assert (reviews[0]["model_passed"], reviews[0]["precheck_passed"]) == (True, False)
    assert (reviews[1]["model_passed"], reviews[1]["precheck_passed"]) == (True, True)

    # The verifier was shown the failure, and the coder is told which command to fix.
    seen = [blocks(r.messages[0]) for r in world.provider.of("verifier")]
    assert f"1. FAILED `{BUG_GONE}` (exit code 1)" in seen[0][2]
    assert f"1. PASSED `{BUG_GONE}` (exit code 0)" in seen[1][2]
    coder_asked = world.provider.of("coder")
    assert len(coder_asked) == 4
    message = blocks(coder_asked[2].messages[-1])[-1]
    assert message == f"Review failed: the command `{BUG_GONE}` failed with exit code 1."


async def test_an_acceptance_command_that_fails_fails_the_review(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """A command of the plan's acceptance checks is run like the test command is.

    `exit 3` fails in every round, so the review can never pass, whatever the verifier says. The
    task ends as stuck and the summary names the command.
    """
    plan = {**PLAN_INPUT, "acceptance_checks": ["`exit 3`", "add(-2, -3) returns -5"]}
    steps = [
        turn("planner", 0, tool("submit_plan", **plan)),
        *stuck_run()[1:-1],
        verifier_turn(0, PASS),
    ]
    world, outcome, _ = await run_light(env, tmp_path, steps, local_spec(HAS_ADD))

    reviews = verdict_events(world.recorder)
    assert [(r["passed"], r["model_passed"], r["precheck_passed"]) for r in reviews] == [
        (False, True, False)
    ] * 3
    assert reviews[0]["issues"] == ["the command `exit 3` failed with exit code 3"]
    seen = blocks(world.provider.of("verifier")[0].messages[0])[2]
    assert f"1. PASSED `{HAS_ADD}` (exit code 0)" in seen
    assert "2. FAILED `exit 3` (exit code 3)" in seen
    assert outcome.kind is OutcomeKind.STUCK
    assert "the command `exit 3` failed with exit code 3" in outcome.summary


# ---------- three failed rounds ----------
async def test_max_rounds_stuck(env: WorkflowEnvironment, tmp_path: Path) -> None:
    """Three failed rounds end the task as stuck, with the reason and the last evidence."""
    world, outcome, status = await run_light(env, tmp_path, stuck_run())

    assert acts.MAX_VERIFY_ROUNDS == 3
    assert outcome.kind is OutcomeKind.STUCK
    assert outcome.pr_url is None
    assert outcome.summary.startswith("the verifier sent the work back 3 times")
    assert EDGE_CASE in outcome.summary
    assert status.phase is Phase.FAILED
    assert status.outcome == outcome

    reviews = verdict_events(world.recorder)
    assert [(r["round"], r["passed"]) for r in reviews] == [(1, False), (2, False), (3, False)]
    # The coder was sent back twice and not a third time: the third failure ended the task.
    asked = world.provider.of("coder")
    assert len(asked) == 6
    expected = f"Review failed: {EDGE_CASE}. Fix step s1."
    assert blocks(asked[2].messages[-1])[-1] == expected
    assert blocks(asked[4].messages[-1])[-1] == expected
    assert len(world.provider.of("verifier")) == 3


async def test_the_rounds_cross_a_hand_over(env: WorkflowEnvironment, tmp_path: Path) -> None:
    """The count of failed rounds is part of the state that a new run starts with."""
    world, outcome, _ = await run_light(
        env, tmp_path, stuck_run(), tuning=acts.Tuning(continue_as_new_every_turns=2)
    )

    assert outcome.kind is OutcomeKind.STUCK
    assert any(e.type == "continue_as_new" for e in world.recorder.events)
    assert [r["round"] for r in verdict_events(world.recorder)] == [1, 2, 3]
    assert len(world.provider.of("verifier")) == 3


# ---------- what the verifier is shown ----------
async def test_the_verifier_starts_a_fresh_transcript_with_the_plan_the_precheck_and_the_diff(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    world, outcome, status = await run_light(env, tmp_path, clean_run(), local_spec(BUG_GONE))

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert status.phase is Phase.DONE
    (asked,) = world.provider.of("verifier")
    # Nothing of the coder's transcript: one user message, and no tool calls in it.
    (first,) = asked.messages
    assert first.role == "user"
    parts = blocks(first)
    assert parts[0] == verifier_role.FIRST_USER_MESSAGE
    assert parts[1] == render_plan(plan_with("done", "done"))
    assert parts[2].startswith("## Pre-check")
    assert f"1. PASSED `{BUG_GONE}` (exit code 0)" in parts[2]
    assert f"2. PASSED `{HAS_ADD}` (exit code 0)" in parts[2]
    assert parts[3].startswith("## Diff")
    assert "-    return a - b" in parts[3] and "+    return a + b" in parts[3]
    assert "+def test_add_negative" in parts[3]
    assert len(parts) == 4

    assert asked.tool_names == [*verifier_role.TOOLS, "submit_verdict"]
    assert parse_role_line(asked.system) == "verifier"
    coder_system = world.provider.of("coder")[0].system
    assert asked.system != coder_system


async def test_the_status_says_verifying_while_the_verifier_reads(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with light(env, write_script(tmp_path, clean_run()), hold=("verifier", 0)) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        await wait_until(lambda: len(world.provider.of("verifier")) == 1, "the verifier")

        shown = await planned_status(handle)
        assert shown.phase is Phase.VERIFYING
        assert shown.outcome is None

        world.provider.gate.set()
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED


async def test_the_coder_goes_back_to_its_own_transcript_after_the_review(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    world, _, _ = await run_light(env, tmp_path, stuck_run())

    asked = world.provider.of("coder")
    # The plan block of the first message is still the coder's: the review did not replace it.
    assert blocks(asked[2].messages[0])[0] == coder.FIRST_USER_MESSAGE
    assert "## Pre-check" not in str([blocks(m) for m in asked[2].messages])
    assert asked[2].system == asked[0].system
    assert asked[2].tool_names == asked[0].tool_names
    assert [m.role for m in asked[2].messages] == ["user", "assistant", "user", "assistant", "user"]


# ---------- what the verifier may do ----------
async def test_a_verifier_that_calls_a_tool_it_does_not_have_changes_nothing(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    edit = tool("edit_file", path=MODULE, old_str="    return a + b", new_str="    return a * b")
    steps = [*clean_run()[:-1], verifier_turn(0, edit), verifier_turn(1, PASS)]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        outcome = await result_of(handle)

        assert outcome.kind is OutcomeKind.PR_OPENED
        second = world.provider.of("verifier")[1]
        assert "edit_file is not available while verifying" in results_of(second.messages[-1])[0]
        (box,) = world.boxes.boxes.values()
        assert b"return a + b" in await box.read_file(MODULE)


async def test_an_invalid_verdict_gets_one_repair_turn(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    steps = [
        *clean_run()[:-1],
        verifier_turn(0, tool("submit_verdict", passed=True)),
        verifier_turn(1, PASS),
    ]
    world, outcome, _ = await run_light(env, tmp_path, steps)

    assert outcome.kind is OutcomeKind.PR_OPENED
    repair = results_of(world.provider.of("verifier")[1].messages[-1])[0]
    assert "tests_run: Field required" in repair
    assert "call submit_verdict again" in repair


async def test_a_second_invalid_verdict_ends_the_task_with_an_error(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    bad = tool("submit_verdict", passed=True)
    steps = [*clean_run()[:-1], verifier_turn(0, bad), verifier_turn(1, bad)]
    world, outcome, status = await run_light(env, tmp_path, steps)

    assert outcome.kind is OutcomeKind.ERROR
    assert outcome.summary.startswith("verifier submitted an invalid verdict twice: ")
    assert status.phase is Phase.FAILED
    assert len(world.provider.of("verifier")) == 2
    assert verdict_events(world.recorder) == []


async def test_a_verifier_that_never_submits_ends_the_task_with_an_error(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    look = {"match": {"role": "verifier"}, "respond": {"tool_calls": [tool("list_dir", path=".")]}}
    steps = [*clean_run()[:-1], look]
    world, outcome, status = await run_light(env, tmp_path, steps)

    assert outcome.kind is OutcomeKind.ERROR
    assert outcome.summary == f"verifier submitted no verdict in {verifier_role.MAX_TURNS} turns"
    assert status.phase is Phase.FAILED
    assert len(world.provider.of("verifier")) == verifier_role.MAX_TURNS


async def test_the_task_turn_budget_counts_the_turns_of_the_review(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    # The planner and the coder use five turns, so none is left for the verifier.
    world, outcome, _ = await run_light(env, tmp_path, clean_run(), local_spec(task_turns=5))

    assert outcome.kind is OutcomeKind.ERROR
    assert outcome.summary == "turn limit"
    assert world.provider.of("verifier") == []


# ---------- tasks that are not verified ----------
async def test_a_task_without_a_plan_is_not_verified(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    steps = [coder_turn(0, tool("finish", summary="Nothing to change."))]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, local_spec(HAS_ADD), task_queue=world.queue)
        outcome = await result_of(handle)
        status = await handle.query(TaskWorkflow.status)

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Nothing to change.")
    assert status.phase is Phase.DONE
    assert world.provider.of("verifier") == []
    assert verdict_events(world.recorder) == []


async def test_finish_in_a_task_with_a_plan_still_ends_it_without_a_review(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """The coder is not offered `finish`. A history that ends on it replays as it was."""
    steps = [*plan_run(), coder_turn(0, tool("finish", summary="Nothing to change."))]
    world, outcome, status = await run_light(env, tmp_path, steps)

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Nothing to change.")
    assert status.phase is Phase.DONE
    assert world.provider.of("verifier") == []
    assert verdict_events(world.recorder) == []


# ---------- replay ----------
async def test_histories_of_a_verified_run_replay(env: WorkflowEnvironment, tmp_path: Path) -> None:
    """The workflow makes the same commands when the history of a verified run is replayed (F13)."""
    histories = []
    for steps, options in (
        (clean_run(), {}),
        (stuck_run(), {}),
        (stuck_run(), {"tuning": acts.Tuning(continue_as_new_every_turns=2)}),
    ):
        async with light(env, write_script(tmp_path, steps)) as world:
            handle = await start_task(
                env.client, local_spec(HAS_ADD), task_queue=world.queue, plan=True, **options
            )
            await result_of(handle)
            histories.append(await handle.fetch_history())
            if options:
                first = env.client.get_workflow_handle(
                    handle.id, run_id=handle.first_execution_run_id
                )
                histories.append(await first.fetch_history())

    replayer = Replayer(
        workflows=[TaskWorkflow],
        workflow_runner=workflow_runner(),
        data_converter=guarded_data_converter,
    )
    for history in histories:
        await replayer.replay_workflow(history)
