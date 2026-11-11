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
import re
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
from temporalio.client import Client, WorkflowHandle, WorkflowHistory
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
    ToolResult,
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
from longhaul.durable.workflows.task import FINISH_REVIEWED_PATCH, TaskWorkflow
from longhaul.github.inputs import CommentInput, LabelInput
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
    executed: list[acts.ToolExecInput]  # every tool call that ran, in order
    delivered: list[acts.DeliverInput] = field(default_factory=list)  # pull requests opened


class IssueActivities:
    """Stand-ins for the activities that talk to GitHub, for a task of an issue on a light worker.

    The review does not touch GitHub. What these tests need is the input `deliver` is given, which
    says whether the pull request is a draft (`verified_tests`).
    """

    def __init__(self) -> None:
        self.delivered: list[acts.DeliverInput] = []

    @activity.defn(name=acts.POST_COMMENT.name)
    async def post_comment(self, inp: CommentInput) -> str:
        return "comment-1"

    @activity.defn(name=acts.POST_PLAN.name)
    async def post_plan(self, inp: acts.PostPlanInput) -> str:
        return "comment-2"

    @activity.defn(name=acts.ADD_LABEL.name)
    async def add_label(self, inp: LabelInput) -> str:
        return inp.label

    @activity.defn(name=acts.REMOVE_LABEL.name)
    async def remove_label(self, inp: LabelInput) -> str:
        return inp.label

    @activity.defn(name=acts.BUILD_PR_BRANCH.name)
    async def build_pr_branch(self, inp: acts.BuildPrBranchInput) -> acts.PrBranchOutput:
        return acts.PrBranchOutput(
            branch="longhaul-pr/42",
            head_sha="c" * 40,
            base_sha=BASE,
            base_branch="main",
            title="fix(calc): fix add",
        )

    @activity.defn(name=acts.DELIVER.name)
    async def deliver(self, inp: acts.DeliverInput) -> str:
        self.delivered.append(inp)
        return "https://github.com/acme/calc/pull/7"

    @property
    def activities(self) -> list[Callable[..., Any]]:
        return [
            self.post_comment,
            self.post_plan,
            self.add_label,
            self.remove_label,
            self.build_pr_branch,
            self.deliver,
        ]


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


REVIEW_ID = re.compile(r"(^|-)verify\d+-(tests|check\d+|diff)$")


def review_calls(world: Light) -> list[acts.ToolExecInput]:
    """The calls of the pre-check and the diff, told apart by their ids."""
    return [c for c in world.executed if REVIEW_ID.search(c.call.id)]


def issue_spec(test_command: str | None = None) -> TaskSpec:
    """A task of an issue: it is picked up on GitHub and ends in a pull request."""
    return TaskSpec(
        task_id=f"lh-acme-calc-{uuid.uuid4().hex[:8]}",
        repo=REPO,
        issue_number=42,
        title="add() subtracts",
        body="`calc.add` subtracts its arguments instead of adding them.",
        settings=RepoSettings(repo=REPO, test_command=test_command),
    )


@asynccontextmanager
async def light(
    env: WorkflowEnvironment,
    script: Path,
    *,
    hold: tuple[str, int] | None = None,
    step_tools: bool = True,
) -> AsyncIterator[Light]:
    """A worker with a fake sandbox. `step_tools` False is a worker from before LH-503."""
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
        planned_tools=coder.tool_specs(registry.specs()) if step_tools else None,
    )
    boxes = Boxes()
    tools = ToolActivities(registry, boxes, blobs, recorder, checkpoints=NoCheckpoints())
    executed: list[acts.ToolExecInput] = []

    @activity.defn(name=acts.TOOL_EXEC.name)
    async def tool_exec(inp: acts.ToolExecInput) -> ToolResult:
        executed.append(inp)
        return await tools.tool_exec(inp)

    github = IssueActivities()
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    try:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[TaskWorkflow],
            activities=[
                *llm.activities,
                tool_exec,
                tools.append_results,
                *EventActivities(recorder).activities,
                *PlanActivities(recorder).activities,
                *StepActivities(recorder).activities,
                *github.activities,
                boxes.prepare_workspace,
                boxes.cleanup,
                boxes.ensure_sandbox,
                boxes.bundle_checkpoint,
            ],
            workflow_runner=workflow_runner(),
        ):
            yield Light(queue, provider, recorder, blobs, boxes, executed, github.delivered)
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
        # No short heartbeat timeout: nothing here kills a worker, and on a busy machine a timeout
        # of a few seconds starts a second attempt of a commit while the first still runs.
        handle = await start_task(world.client, world.spec, task_queue=world.queue, plan=True)
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
    assert outcome.summary.startswith("the work failed review 3 times; last review: ")
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
async def test_every_call_of_the_review_is_read_only(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """The pre-check, the diff and the verifier's own calls all run with `read_only` set.

    tool_exec puts the tree back after such a call (tests/integration/test_planner.py shows it for
    the planner), so checking the work cannot change the work. The coder's calls are not.
    """
    steps = [
        *clean_run()[:-1],
        verifier_turn(0, tool("read_file", path=MODULE), tool("run_tests")),
        verifier_turn(1, PASS),
    ]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(
            env.client, local_spec(BUG_GONE), task_queue=world.queue, plan=True
        )
        outcome = await result_of(handle)
        executed = list(world.executed)

    assert outcome.kind is OutcomeKind.PR_OPENED
    by_id = {call.call.id: call for call in executed}
    review = [c for c in executed if c.call.id.startswith(f"{c.task_id}-verify")]
    assert [c.call.name for c in review] == ["run_command", "run_command", "git_diff"]
    assert all(c.read_only for c in review)
    asked = [c for c in executed if c.call.name in ("read_file", "run_tests")]
    assert [c.call.name for c in asked] == ["read_file", "run_tests"]
    assert all(c.read_only for c in asked)
    coder_calls = [c for c in executed if c.call.name in ("edit_file", "write_file")]
    assert len(coder_calls) == 2 and not any(c.read_only for c in coder_calls)
    assert len(by_id) == len(executed)  # no id is used twice


async def test_the_verifier_cannot_change_the_files_it_reviews(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """What the worker offers the verifier has no tool that writes (roles/verifier.py)."""
    async with light(env, write_script(tmp_path, clean_run())) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        await result_of(handle)

    (asked,) = world.provider.of("verifier")
    assert not {"edit_file", "write_file", "run_command", "finish"} & set(asked.tool_names)
    assert "submit_for_review" not in asked.tool_names and "update_step" not in asked.tool_names


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


async def test_the_budget_ends_the_task_before_the_precheck_runs(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """A coder that submits on the last turn of the budget gets no review, and no command runs.

    The pre-check can take minutes for each command, and its result could not be used.
    """
    world, outcome, _ = await run_light(env, tmp_path, clean_run(), local_spec(task_turns=5))

    assert outcome.summary == "turn limit"
    assert review_calls(world) == []
    assert world.provider.of("verifier") == []


# ---------- the audit rows of a review ----------
async def test_two_tasks_keep_their_own_rows_for_the_precheck(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """The tool_calls table keys a row by the id of the call, so each task needs ids of its own.

    Both tasks review their round 1. When the ids are the same for every task, the second task
    takes over the rows of the first, and the exit code and the output of its pre-check are gone.
    """
    async with light(env, write_script(tmp_path, clean_run())) as world:
        first, second = local_spec(BUG_GONE), local_spec(BUG_GONE)
        for spec in (first, second):
            handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
            assert (await result_of(handle)).kind is OutcomeKind.PR_OPENED
        rows = list(world.recorder.tool_calls.values())

    for spec in (first, second):
        mine = [r for r in rows if r.task_id == spec.task_id and "-verify1-" in r.id]
        assert {r.id: r.name for r in mine} == {
            f"{spec.task_id}-verify1-tests": "run_command",
            f"{spec.task_id}-verify1-check1": "run_command",
            f"{spec.task_id}-verify1-diff": "git_diff",
        }
        assert all(r.exit_code in (0, None) and r.output_ref for r in mine)


async def test_the_ids_of_each_round_stay_the_same_across_hand_overs(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """A retry or a replay makes the same call ids, so its rows are written over and not added."""
    async with light(env, write_script(tmp_path, stuck_run())) as world:
        spec = local_spec(BUG_GONE)
        handle = await start_task(
            env.client,
            spec,
            task_queue=world.queue,
            plan=True,
            tuning=acts.Tuning(continue_as_new_every_turns=2),
        )
        await result_of(handle)
        ids = [c.call.id for c in review_calls(world)]

    assert ids == [
        f"{spec.task_id}-verify{n}-{part}"
        for n in (1, 2, 3)
        for part in ("tests", "check1", "diff")
    ]


# ---------- a review that ran no command ----------
async def test_a_pull_request_is_ready_when_a_command_backed_the_verdict(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with light(env, write_script(tmp_path, clean_run())) as world:
        spec = issue_spec(test_command=HAS_ADD)
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        outcome = await result_of(handle)
        (delivered,) = world.delivered

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert delivered.verified_tests == "2 passed"


async def test_a_pull_request_is_a_draft_when_the_review_ran_no_command(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """With no test command and no command in the plan, nothing ran, so the counts are claimed.

    The verdict can still pass the work. The pull request is a draft and does not say "Verified".
    """
    plan = {**PLAN_INPUT, "acceptance_checks": ["add(-2, -3) returns -5"]}
    steps = [turn("planner", 0, tool("submit_plan", **plan)), *clean_run()[1:]]
    async with light(env, write_script(tmp_path, steps)) as world:
        spec = issue_spec(test_command=None)
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        outcome = await result_of(handle)
        status = await planned_status(handle)
        (delivered,) = world.delivered
        review = review_calls(world)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert status.phase is Phase.DONE
    assert [c.call.name for c in review] == ["git_diff"]  # the diff only
    (event,) = verdict_events(world.recorder)
    assert event["passed"] is True  # the verdict decided alone
    assert delivered.verified_tests is None
    shown = blocks(world.provider.of("verifier")[0].messages[0])[2]
    assert verifier_role.NO_PRECHECK in shown


# ---------- the log of a harness without the row activity (LH-991) ----------
async def test_a_harness_without_the_row_activity_logs_no_warning_per_write(
    env: WorkflowEnvironment, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`light` registers no record_task_state. tests/conftest.py adds a no-op, so the workflow does
    not log "row of task ... not written" for every change of phase."""
    async with light(env, write_script(tmp_path, clean_run())) as world:
        handle = await start_task(
            env.client, local_spec(HAS_ADD), task_queue=world.queue, plan=True
        )
        await result_of(handle)

    warned = [r.getMessage() for r in caplog.records if "not written" in r.getMessage()]
    assert warned == []


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


FINISH = tool("finish", summary="Nothing to change.")


async def test_finish_in_a_task_with_a_plan_is_reviewed_like_a_submission(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """A planned coder that calls `finish` gets the review that `submit_for_review` gets (LH-991).

    This test asserted the old path for a new run: LH-504 let a planned `finish` end the task with
    no review, so that histories from before the verifier ended as they did. Those histories keep
    that path through the patch marker and have their own replay test below. A new run has to be
    reviewed before a ready pull request, so the same script now meets the verifier.
    """
    steps = [*plan_run(), coder_turn(0, FINISH), verifier_turn(0, PASS)]
    async with light(env, write_script(tmp_path, steps), step_tools=False) as world:
        handle = await start_task(
            env.client, local_spec(HAS_ADD), task_queue=world.queue, plan=True
        )
        outcome = await result_of(handle)
        status = await planned_status(handle)

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Nothing to change.")
    assert status.phase is Phase.DONE
    assert len(world.provider.of("verifier")) == 1
    (event,) = verdict_events(world.recorder)
    assert event["passed"] is True


async def test_a_finish_that_fails_its_review_goes_back_to_the_coder(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """The review of a `finish` can fail and send the coder back, as one of a submission can."""
    steps = [
        *plan_run(),
        coder_turn(0, FIX),
        coder_turn(1, FINISH),
        coder_turn(2, ADD_TEST),
        coder_turn(3, FINISH),
        verifier_turn(1, PASS, request_contains="+def test_add_negative"),
        verifier_turn(1, verdict(False, "1 passed", UNTESTED, send_back_step="s1")),
        verifier_turn(0, tool("git_diff")),
    ]
    async with light(env, write_script(tmp_path, steps), step_tools=False) as world:
        handle = await start_task(
            env.client, local_spec(HAS_ADD), task_queue=world.queue, plan=True
        )
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert [e["passed"] for e in verdict_events(world.recorder)] == [False, True]


async def test_a_task_without_a_plan_that_finishes_never_asks_for_the_marker(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """The marker belongs to planned tasks only: a task without a plan never asks for it."""
    steps = [coder_turn(0, FINISH)]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, local_spec(HAS_ADD), task_queue=world.queue)
        await result_of(handle)
        history = await handle.fetch_history()

    assert not any(FINISH_REVIEWED_PATCH in text for text in patch_markers(history))


def patch_markers(history: WorkflowHistory) -> set[str]:
    """The ids of the patches a history recorded: the texts in the details of its patch markers."""
    return {
        payload.data.decode()
        for e in history.events
        if e.HasField("marker_recorded_event_attributes")
        and e.marker_recorded_event_attributes.marker_name == "core_patch"
        for payloads in e.marker_recorded_event_attributes.details.values()
        for payload in payloads.payloads
    }


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


async def test_a_new_planned_finish_records_the_marker(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    steps = [*plan_run(), coder_turn(0, FINISH), verifier_turn(0, PASS)]
    async with light(env, write_script(tmp_path, steps), step_tools=False) as world:
        handle = await start_task(
            env.client, local_spec(HAS_ADD), task_queue=world.queue, plan=True
        )
        await result_of(handle)
        history = await handle.fetch_history()

    assert any(FINISH_REVIEWED_PATCH in text for text in patch_markers(history))


async def test_a_history_that_ends_on_a_planned_finish_from_before_the_review_replays() -> None:
    """Recorded with the workflow of dev at v0.8.0 plus the Phase 8 report, before LH-991.

    A planned coder of a worker from before LH-503 calls `finish`, and the task ends with no
    review and no verifier turn. The history has no marker for FINISH_REVIEWED_PATCH, so the new
    code must take the old path at the same place and make the same commands (F13).
    """
    path = ROOT / "tests" / "fixtures" / "histories" / "planned_finish_before_review.json"
    history = WorkflowHistory.from_json("replay-planned-finish", path.read_text(encoding="utf-8"))
    assert not any(FINISH_REVIEWED_PATCH in text for text in patch_markers(history))
    assert "llm_turn" in {
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    }
    replayer = Replayer(
        workflows=[TaskWorkflow],
        workflow_runner=workflow_runner(),
        data_converter=guarded_data_converter,
    )
    await replayer.replay_workflow(history)
