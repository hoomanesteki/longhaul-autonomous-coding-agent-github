"""Output handles and read_output against a real blob store (LH-402).

A command prints ten thousand lines. The real tool_exec and append_results run on it with the
S3 store of the compose stack (MinIO), and the model's side of the transcript must hold a head,
a tail and a handle. Then read_output pages through the stored text and filters it by the
handle alone: no recorder, no sandbox, only the blob store.

Every test works under its own random task id and deletes what it wrote, so tests can run in any
order next to other builds that share the bucket.
"""

import shlex
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import boto3
import pytest
from botocore.config import Config
from temporalio.testing import ActivityEnvironment

from longhaul.blobs.base import blob_key
from longhaul.blobs.s3 import S3BlobStore
from longhaul.context.tokens import estimate_tokens
from longhaul.contracts import (
    Message,
    Sandbox,
    TextBlock,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolResultBlock,
    ToolUseBlock,
)
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.tools import NoCheckpoints, ToolActivities
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.settings import EnvSettings
from longhaul.tools.output import MAX_LINES
from longhaul.tools.registry import ToolRegistry, default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase4]

CALL_ID = "toolu_noisy"
HANDLE = f"out_{CALL_ID}"
LINES = 10_000
CAP = 4000
# The command prints these line numbers with FAILED in them and every other line with ok.
FAILING = [137, 2_048, 5_000, 7_777, 9_999]


# ---------- the store and the task ----------
@pytest.fixture
def store() -> S3BlobStore:
    """The stack's MinIO. LONGHAUL_BLOB_* wins and the compose defaults fill the gaps."""
    settings = EnvSettings()
    return S3BlobStore(
        settings.blob_endpoint or "http://localhost:9000",
        settings.blob_bucket or "longhaul",
        settings.blob_access_key.get_secret_value() if settings.blob_access_key else "longhaul",
        settings.blob_secret_key.get_secret_value()
        if settings.blob_secret_key
        else "longhaul-secret",
    )


@pytest.fixture
def task_id(store: S3BlobStore) -> Iterator[str]:
    task = f"lh-402-{uuid.uuid4().hex[:12]}"
    yield task
    settings = EnvSettings()
    client: Any = boto3.client(
        "s3",
        endpoint_url=settings.blob_endpoint or "http://localhost:9000",
        aws_access_key_id=settings.blob_access_key.get_secret_value()
        if settings.blob_access_key
        else "longhaul",
        aws_secret_access_key=settings.blob_secret_key.get_secret_value()
        if settings.blob_secret_key
        else "longhaul-secret",
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )
    bucket = settings.blob_bucket or "longhaul"
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"tasks/{task}/")
    for page in pages:
        for item in page.get("Contents", []):
            client.delete_object(Bucket=bucket, Key=item["Key"])


@pytest.fixture
async def sandbox() -> AsyncIterator[FakeSandbox]:
    box = FakeSandbox("sbx-402")
    yield box
    await box.destroy()


class Lookup:
    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return self.sandbox if sandbox_id == self.sandbox.id else None


def noisy_command() -> str:
    code = (
        f"for n in range(1, {LINES} + 1): "
        f"print('case', n, 'FAILED' if n in {FAILING!r} else 'ok', 'x' * 30)"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def context(task_id: str, output_max_tokens: int = CAP) -> ToolContext:
    return ToolContext(
        task_id=task_id,
        turn_index=1,
        blob_prefix=f"tasks/{task_id}",
        output_max_tokens=output_max_tokens,
    )


async def run_noisy_command(
    store: S3BlobStore, task_id: str, sandbox: Sandbox
) -> tuple[ToolResult, ToolRegistry, ToolActivities]:
    """The real tool_exec, on a command with a very long output."""
    registry = default_registry(blobs=store)
    tools = ToolActivities(
        registry,
        Lookup(sandbox),
        store,
        MemoryRecorder(),
        checkpoints=NoCheckpoints(),
        tool_output_max_tokens=CAP,
    )
    call = acts.ToolCallSummary(id=CALL_ID, name="run_command", input={"command": noisy_command()})
    inp = acts.ToolExecInput(
        task_id=task_id, turn_index=0, call=call, pre_sha=None, sandbox_id=sandbox.id
    )
    result = await ActivityEnvironment().run(tools.tool_exec, inp)
    return result, registry, tools


async def what_the_model_reads(
    store: S3BlobStore, tools: ToolActivities, task_id: str, result: ToolResult
) -> str:
    """Run append_results on the result and return the text of the tool_result it wrote."""
    first = Message(role="user", content=[TextBlock(text="go")])
    asked = Message(
        role="assistant",
        content=[ToolUseBlock(id=CALL_ID, name="run_command", input={"command": "noisy"})],
    )
    transcript = await store.put_json([first, asked], blob_key(task_id, "turns", 0, "t.json"))
    inp = acts.AppendResultsInput(
        task_id=task_id,
        turn_index=0,
        transcript_ref=transcript,
        results=[acts.summarize_result(result)],
    )
    out = await ActivityEnvironment().run(tools.append_results, inp)
    grown = await store.get_json(out, list[Message])
    (block,) = [b for b in grown[-1].content if isinstance(b, ToolResultBlock)]
    return block.content


def row(n: int, line: str) -> str:
    return f"{n:>6}\t{line}"


# ---------- the tests the story names ----------
async def test_page_and_filter(store: S3BlobStore, task_id: str, sandbox: FakeSandbox) -> None:
    result, registry, tools = await run_noisy_command(store, task_id, sandbox)
    assert result.ok and result.output_ref is not None
    full = (await store.get(result.output_ref)).decode()
    # run_command drops the final newline, so each item of the split is a line of the text.
    source = full.split("\n")
    total = len(source)
    assert total > LINES

    # A 10,000-line output becomes a shaped result under the cap, with a valid handle.
    seen = await what_the_model_reads(store, tools, task_id, result)
    assert estimate_tokens(seen) <= CAP
    rows = seen.split("\n")
    assert rows[:60] == source[:60]
    assert rows[61:] == source[total - 60 : total]
    assert rows[60].startswith(f"[output truncated: {total} lines, ")
    assert f"Full text: handle {HANDLE}. Use read_output to page or filter.]" in rows[60]

    async def read_output(output_max_tokens: int = CAP, **arguments: Any) -> ToolResult:
        # The registry is the model's way in. It is given no sandbox that it could use.
        call = ToolCall(id="r1", name="read_output", input={"handle": HANDLE, **arguments})
        return await registry.run(call, sandbox, context(task_id, output_max_tokens))

    # Paging: a range, then the default page, which stops at the token cap, and then a page
    # that is cut by the 400 line limit because the cap is raised above it.
    middle = await read_output(start_line=5_001, end_line=5_003)
    assert middle.ok
    assert middle.output.split("\n") == [row(n, source[n - 1]) for n in (5_001, 5_002, 5_003)]

    first_page = await read_output()
    rows = first_page.output.split("\n")
    shown = len(rows) - 1
    assert 0 < shown <= MAX_LINES
    assert len(first_page.output) <= int(CAP * 3.5)
    assert rows[:shown] == [row(n, source[n - 1]) for n in range(1, shown + 1)]
    assert rows[shown] == (
        f"[showing lines 1-{shown} of {total}; call again with start_line={shown + 1} to continue]"
    )
    following = await read_output(start_line=shown + 1)
    assert following.output.split("\n")[0] == row(shown + 1, source[shown])

    long_page = await read_output(output_max_tokens=20_000)
    assert long_page.output.split("\n")[:MAX_LINES] == [
        row(n, source[n - 1]) for n in range(1, MAX_LINES + 1)
    ]
    assert long_page.output.endswith(f"call again with start_line={MAX_LINES + 1} to continue]")

    last_page = await read_output(start_line=total - 4)
    assert last_page.output.split("\n") == [
        row(n, source[n - 1]) for n in range(total - 4, total + 1)
    ]

    # Filtering: exactly the failing lines, with their numbers.
    failed = await read_output(pattern="FAILED")
    assert failed.ok
    wanted = [row(n, line) for n, line in enumerate(source, 1) if "FAILED" in line]
    assert len(wanted) == len(FAILING)
    assert failed.output.split("\n") == wanted


async def test_unknown_handle(store: S3BlobStore, task_id: str, sandbox: FakeSandbox) -> None:
    registry = default_registry(blobs=store)
    call = ToolCall(id="r1", name="read_output", input={"handle": "out_toolu_never_made"})
    result = await registry.run(call, sandbox, context(task_id))
    assert not result.ok
    assert "unknown output handle: out_toolu_never_made" in result.output


async def test_a_handle_of_another_task_is_unknown(
    store: S3BlobStore, task_id: str, sandbox: FakeSandbox
) -> None:
    result, _, _ = await run_noisy_command(store, task_id, sandbox)
    assert result.output_ref is not None
    registry = default_registry(blobs=store)
    other = context(f"lh-402-{uuid.uuid4().hex[:12]}")
    call = ToolCall(id="r1", name="read_output", input={"handle": HANDLE})
    answer = await registry.run(call, sandbox, other)
    assert not answer.ok
    assert "case" not in answer.output
