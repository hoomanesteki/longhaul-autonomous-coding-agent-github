"""LH-703: the spans of a scripted run, read back from an in-memory exporter.

The run is TaskWorkflow with the real llm_turn and tool_exec activities, started the way
tests/integration/test_activities.py starts it: a time skipping test server, the scripted
provider, fake sandboxes and the memory stores. The difference is the Temporal tracing
interceptor on the client, which the worker takes from it. The tracer provider writes to an
InMemorySpanExporter, so a test reads the spans without a collector.

The script has three model turns and three tool calls. Its issue text, tool arguments, file
content and reply text each carry a marker, so a test can look for prompt and tool content in
the spans.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from longhaul.agent.loop import LoopConfig
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import (
    Budgets,
    Outcome,
    OutcomeKind,
    RepoSettings,
    Sandbox,
    TaskSpec,
)
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.tools import ToolActivities
from longhaul.durable.client import start_task
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.telemetry import otel
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase7]

MODEL = "test-model"
CFG = LoopConfig(model=MODEL, max_turns=10, max_tokens_per_turn=1000, max_tokens_retry=5000)
BASE = "b" * 40
WAIT_S = 30.0

# Text that must stay out of the spans unless content capture is on. Each one enters the run
# at a different place: the issue (and so the prompt), a tool's arguments, a tool's output
# (the file is read back) and the model's own reply.
PROMPT_MARKER = "PROMPT-MARKER-4f1c"
INPUT_MARKER = "TOOL-INPUT-MARKER-9b2e"
OUTPUT_MARKER = "TOOL-OUTPUT-MARKER-7d3a"
REPLY_MARKER = "REPLY-MARKER-2e8f"

TURNS = 3
TOOL_CALLS = 3


# ---------- the run ----------
def write_script(directory: Path) -> Path:
    steps: list[dict[str, Any]] = [
        {
            "respond": {
                "text": REPLY_MARKER,
                "tool_calls": [
                    {
                        "name": "write_file",
                        "input": {
                            "path": "notes.txt",
                            "content": f"{INPUT_MARKER} {OUTPUT_MARKER}",
                        },
                    }
                ],
            }
        },
        {
            "respond": {
                "tool_calls": [
                    {"name": "read_file", "input": {"path": "notes.txt"}},
                    {"name": "list_dir", "input": {"path": "."}},
                ]
            }
        },
        {"respond": {"tool_calls": [{"name": "finish", "input": {"summary": "Wrote notes."}}]}},
    ]
    path = directory / "traced.yaml"
    path.write_text(yaml.safe_dump({"steps": steps}))
    return path


def make_spec() -> TaskSpec:
    return TaskSpec(
        task_id=f"lh-acme-calc-{uuid.uuid4().hex[:8]}",
        repo="acme/calc",
        issue_number=42,
        title="Write the notes file",
        body=f"Please write notes.txt. {PROMPT_MARKER}",
        settings=RepoSettings(repo="acme/calc", budgets=Budgets(task_turns=CFG.max_turns)),
    )


class Boxes:
    """Fake sandboxes by id, with the prepare_workspace and cleanup that put them there."""

    def __init__(self) -> None:
        self.boxes: dict[str, FakeSandbox] = {}

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return self.boxes.get(sandbox_id)

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        box = FakeSandbox(f"sbx-{spec.task_id}")
        self.boxes[box.id] = box
        return acts.PrepareWorkspaceOutput(sandbox_id=box.id, base_sha=BASE, head_sha=BASE)

    @activity.defn(name=acts.CLEANUP.name)
    async def cleanup(self, inp: acts.CleanupInput) -> None:
        box = self.boxes.pop(f"sbx-{inp.task_id}", None)
        if box is not None:
            await box.destroy()


@asynccontextmanager
async def traced_run(
    exporter: InMemorySpanExporter, tmp_path: Path, *, capture_content: bool
) -> AsyncIterator[str]:
    """Run the script to its end with tracing on, and yield the task id."""
    provider = TracerProvider(resource=Resource.create({"service.name": otel.SERVICE_NAME}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    spec = make_spec()
    boxes = Boxes()
    toolbox = default_registry()
    blobs, recorder = MemoryBlobStore(), MemoryRecorder()
    llm = LlmActivities(
        ScriptedProvider(write_script(tmp_path)), blobs, recorder, toolbox.specs(), lambda r: CFG
    )
    tools = ToolActivities(toolbox, boxes, blobs, recorder)
    with otel.configured(provider, capture_content=capture_content):
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter, interceptors=otel.interceptors()
        ) as env:
            queue = f"lh-trace-{uuid.uuid4().hex[:8]}"
            # The worker takes the interceptor from env.client, which was made with it.
            async with Worker(
                env.client,
                task_queue=queue,
                workflows=[TaskWorkflow],
                activities=[
                    *llm.activities,
                    *tools.activities,
                    boxes.prepare_workspace,
                    boxes.cleanup,
                ],
                workflow_runner=workflow_runner(),
            ):
                handle = await start_task(env.client, spec, task_queue=queue)
                outcome = await handle.result()
            assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Wrote notes.")
            yield spec.task_id
    provider.shutdown()


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    yield memory
    memory.clear()


def by_name(spans: tuple[ReadableSpan, ...], prefix: str) -> list[ReadableSpan]:
    return sorted(
        (s for s in spans if s.name.startswith(prefix)),
        key=lambda s: s.start_time or 0,
    )


def everything_said(spans: tuple[ReadableSpan, ...]) -> str:
    """Every attribute value and event of every span, as one string to look for text in."""
    parts: list[str] = []
    for span in spans:
        parts.append(span.name)
        parts.extend(repr(value) for value in (span.attributes or {}).values())
        for event in span.events:
            parts.append(event.name)
            parts.extend(repr(value) for value in (event.attributes or {}).values())
    return "\n".join(parts)


# ---------- LH-703 acceptance criteria ----------
async def test_span_counts_match(exporter: InMemorySpanExporter, tmp_path: Path) -> None:
    async with traced_run(exporter, tmp_path, capture_content=False) as task_id:
        spans = exporter.get_finished_spans()

    # One trace, with one root: the client call that started the workflow.
    assert len({s.context.trace_id for s in spans}) == 1
    roots = [s for s in spans if s.parent is None]
    assert [s.name for s in roots] == ["StartWorkflow:TaskWorkflow"]

    chats = by_name(spans, "chat ")
    tool_runs = by_name(spans, "execute_tool ")
    assert [s.name for s in chats] == [f"chat {MODEL}"] * TURNS
    assert [s.name for s in tool_runs] == [
        "execute_tool write_file",
        "execute_tool read_file",
        "execute_tool list_dir",
    ]
    assert len(tool_runs) == TOOL_CALLS

    # The activity spans of Temporal's interceptor are the parents, so a workflow span leads
    # to each of them: workflow, then activity, then the model or tool span.
    by_id = {s.context.span_id: s for s in spans}
    for chat in chats:
        parent = by_id[chat.parent.span_id]  # type: ignore[union-attr]
        assert parent.name == "RunActivity:llm_turn"
    for run in tool_runs:
        parent = by_id[run.parent.span_id]  # type: ignore[union-attr]
        assert parent.name == "RunActivity:tool_exec"
    assert any(s.name.startswith("RunWorkflow:") for s in spans)

    # The attributes of the GenAI conventions, and the Longhaul ones.
    for turn, chat in enumerate(chats):
        attrs = dict(chat.attributes or {})
        assert attrs[gen_ai.GEN_AI_OPERATION_NAME] == "chat"
        assert attrs[gen_ai.GEN_AI_PROVIDER_NAME] == "scripted"
        assert attrs[gen_ai.GEN_AI_REQUEST_MODEL] == MODEL
        assert attrs[gen_ai.GEN_AI_RESPONSE_MODEL] == MODEL
        assert int(attrs[gen_ai.GEN_AI_USAGE_INPUT_TOKENS]) > 0  # type: ignore[call-overload]
        assert int(attrs[gen_ai.GEN_AI_USAGE_OUTPUT_TOKENS]) > 0  # type: ignore[call-overload]
        # Every scripted reply ends on a tool call, also the last one, which calls finish.
        assert tuple(attrs[gen_ai.GEN_AI_RESPONSE_FINISH_REASONS]) == ("tool_use",)  # type: ignore[arg-type]
        assert attrs[otel.TASK_ID] == task_id
        assert attrs[otel.TURN_INDEX] == turn
        assert attrs[otel.ROLE] == "coder"
        assert attrs[otel.COST_USD] == 0.0
        assert attrs[otel.CACHE_RATIO] == 0.0

    for run, name in zip(tool_runs, ["write_file", "read_file", "list_dir"], strict=True):
        attrs = dict(run.attributes or {})
        assert attrs[gen_ai.GEN_AI_OPERATION_NAME] == "execute_tool"
        assert attrs[gen_ai.GEN_AI_TOOL_NAME] == name
        assert str(attrs[gen_ai.GEN_AI_TOOL_CALL_ID]).startswith("toolu_")
        assert attrs[otel.TASK_ID] == task_id

    # Calls of one reply have different ids, and every id is a call the model made.
    call_ids = [str(dict(s.attributes or {})[gen_ai.GEN_AI_TOOL_CALL_ID]) for s in tool_runs]
    assert len(set(call_ids)) == TOOL_CALLS


async def test_no_content_by_default(exporter: InMemorySpanExporter, tmp_path: Path) -> None:
    async with traced_run(exporter, tmp_path, capture_content=False):
        spans = exporter.get_finished_spans()

    assert by_name(spans, "chat ") and by_name(spans, "execute_tool "), "no spans to look at"
    said = everything_said(spans)
    for marker in (PROMPT_MARKER, INPUT_MARKER, OUTPUT_MARKER, REPLY_MARKER):
        assert marker not in said, f"{marker} reached a span"
    for name in (
        gen_ai.GEN_AI_INPUT_MESSAGES,
        gen_ai.GEN_AI_OUTPUT_MESSAGES,
        gen_ai.GEN_AI_SYSTEM_INSTRUCTIONS,
        gen_ai.GEN_AI_TOOL_CALL_ARGUMENTS,
        gen_ai.GEN_AI_TOOL_CALL_RESULT,
    ):
        assert all(name not in (s.attributes or {}) for s in spans), name


async def test_content_is_captured_only_when_asked_for(
    exporter: InMemorySpanExporter, tmp_path: Path
) -> None:
    """The same run with capture on shows the markers, so the check above can see them."""
    async with traced_run(exporter, tmp_path, capture_content=True):
        spans = exporter.get_finished_spans()

    said = everything_said(spans)
    for marker in (PROMPT_MARKER, INPUT_MARKER, OUTPUT_MARKER, REPLY_MARKER):
        assert marker in said, f"{marker} did not reach a span"
