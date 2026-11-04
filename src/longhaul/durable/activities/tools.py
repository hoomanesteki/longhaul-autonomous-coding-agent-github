"""The tool_exec and append_results activities.

`tool_exec` runs one tool call in the task's sandbox, stores the text that came out, and writes
the `tool_calls` row. Its `ToolResult` keeps a short preview of the text in `output` and names
the whole text in `output_ref`, so a long test log never sits in the workflow history.

`append_results` answers the tool_use blocks of the last assistant message. It reads each
result's text from its blob, applies the pairing rules of turns.py (D9) and stores the new
transcript. It also adds the harness text, such as the nudge, which goes after the results.

Every `tool_exec` call starts from a known commit and saves what it changed as a new one
(ADR-0006, failure mode F1), so Temporal can run an attempt again after the worker died in the
middle of it:

1. If the workspace is not at `pre_sha` with a clean tree, `restore(pre_sha)` puts it there.
   This is what undoes the edit of an attempt that died after its tool ran. A workspace that is
   already there is left alone, because a restore also deletes the ignored files that the agent
   made on purpose, such as the output of a build.
2. The tool runs.
3. If the tool mutates, or it runs a shell command (SHELL_TOOLS), `commit` saves the tree. A
   clean tree gives no commit and no `post_sha`. Otherwise `post_sha` is the new commit, and a
   `checkpoints` row of kind `tool` names it.

A sandbox that is gone fails the call with SandboxGoneError, which is not retried: asking again
finds the same empty place. The workflow recreates the sandbox (workspace.py) and runs the call
again from the same `pre_sha`. The error is raised for a container that was gone before the
call, and also for one that vanished during it, because docker raises various things for that:
when any step fails and the sandbox is no longer there, the sandbox is the cause.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from pydantic_core import to_json
from temporalio import activity
from temporalio.exceptions import ApplicationError

from longhaul.agent.turns import (
    TranscriptError,
    answer_tool_calls,
    append_user_text,
    tool_step,
    validate_pairs,
)
from longhaul.blobs.base import BlobStoreBase, blob_key
from longhaul.contracts import (
    BlobRef,
    CheckpointStore,
    Message,
    Sandbox,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolUseBlock,
)
from longhaul.db.records import CheckpointRecord, Recorder, ToolCallRecord
from longhaul.durable import acts
from longhaul.durable.activities.support import call_slug, heartbeating, not_retried, small
from longhaul.tools.registry import ToolRegistry

# The two tools that run a shell command. Whether a command changes files is not known in
# advance, so `mutates` is False for both (tools/exec.py) and the tree is looked at after them.
SHELL_TOOLS = frozenset({"run_command", "run_tests"})


class SandboxLookup(Protocol):
    """The part of a SandboxManager that tool_exec uses: find a running sandbox by its id."""

    async def get(self, sandbox_id: str) -> Sandbox | None: ...


class WorkspaceCheckpoints(CheckpointStore, Protocol):
    """A CheckpointStore that can also say whether the workspace already is at a commit.

    GitCheckpointStore has `is_at`. The contracts do not, and they stay as they are, so the
    question is added here, where it is asked.
    """

    async def is_at(self, sandbox: Sandbox, sha: str) -> bool: ...


class NoCheckpoints:
    """A checkpoint store that stores nothing, for tests of the tools. A worker passes a real one.

    Nothing is ever saved and the workspace always counts as being where it should be, so a
    call neither restores, commits nor leaves a `post_sha`.
    """

    async def init_task(self, task_id: str, clone_url: str, base_sha: str) -> str:
        return base_sha

    async def commit(self, sandbox: Sandbox, message: str) -> str | None:
        return None  # the tree counts as clean

    async def restore(self, sandbox: Sandbox, sha: str) -> None:
        return None

    async def bundle(self, task_id: str) -> BlobRef:
        raise NotImplementedError("there is no checkpoint store to bundle")

    async def is_at(self, sandbox: Sandbox, sha: str) -> bool:
        return True


def _gone(sandbox_id: str) -> ApplicationError:
    # Asking again finds the same empty place, so the activity does not retry. The workflow
    # recreates the sandbox and runs the call again.
    return ApplicationError(
        f"sandbox {sandbox_id} is gone", type=acts.SANDBOX_GONE, non_retryable=True
    )


class ToolActivities:
    """The tool_exec and append_results activities, with what they need.

    `checkpoints` has no default on purpose: a worker that forgot it would run every call on
    whatever the last attempt left, and nothing would say so.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        sandboxes: SandboxLookup,
        blobs: BlobStoreBase,
        recorder: Recorder,
        *,
        checkpoints: WorkspaceCheckpoints,
        tool_output_max_tokens: int = 4000,
    ) -> None:
        self._registry = registry
        self._sandboxes = sandboxes
        self._blobs = blobs
        self._recorder = recorder
        self._checkpoints = checkpoints
        self._output_max_tokens = tool_output_max_tokens

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.tool_exec, self.append_results]

    # ---------- tool_exec ----------
    @activity.defn(name=acts.TOOL_EXEC.name)
    async def tool_exec(self, inp: acts.ToolExecInput) -> ToolResult:
        inp = small(inp)
        sandbox = await self._sandbox(inp.sandbox_id)
        call, args_ref = await self._call(inp)
        ctx = ToolContext(
            task_id=inp.task_id,
            turn_index=inp.turn_index,
            blob_prefix=f"tasks/{inp.task_id}",
            output_max_tokens=self._output_max_tokens,
        )
        try:
            async with heartbeating():
                await self._start_from(sandbox, inp.pre_sha)
                result = await tool_step(self._registry, sandbox, call, ctx)
                post_sha = await self._save(sandbox, inp, call)
        except Exception as exc:
            if await self._is_gone(inp.sandbox_id):
                raise _gone(inp.sandbox_id) from exc
            raise
        slug = call_slug(call.id)
        output_ref = await self._blobs.put(
            result.output.encode("utf-8", errors="replace"),
            key=blob_key(inp.task_id, "turns", inp.turn_index, f"tool-{slug}-output.txt"),
        )
        if args_ref is None:
            args_ref = await self._blobs.put(
                to_json(call.input),
                key=blob_key(inp.task_id, "turns", inp.turn_index, f"tool-{slug}-args.json"),
            )
        returned = result.model_copy(
            update={
                "output": _preview(result.output, output_ref),
                "output_ref": output_ref,
                "pre_sha": inp.pre_sha,
                "post_sha": post_sha,
            }
        )
        await self._recorder.record_tool_call(
            ToolCallRecord(
                id=call.id,
                task_id=inp.task_id,
                turn_index=inp.turn_index,
                name=call.name,
                ok=result.ok,
                attempt=activity.info().attempt,
                args_ref=args_ref.key,
                exit_code=result.exit_code,
                output_ref=output_ref.key,
                duration_ms=result.duration_ms,
                pre_sha=inp.pre_sha,
                post_sha=post_sha,
            )
        )
        return small(returned)

    async def _sandbox(self, sandbox_id: str) -> Sandbox:
        sandbox = await self._sandboxes.get(sandbox_id)
        if sandbox is None:
            raise _gone(sandbox_id)
        return sandbox

    async def _is_gone(self, sandbox_id: str) -> bool:
        """True when the sandbox is not there, so that a failed call can be told to be its fault."""
        try:
            return await self._sandboxes.get(sandbox_id) is None
        except Exception:
            return False  # no way to tell, so the error of the call stands

    async def _start_from(self, sandbox: Sandbox, pre_sha: str | None) -> None:
        """Put the workspace on `pre_sha` unless it is there already with nothing to save.

        Reset to pre_sha first so that a retried call never runs on half-applied edits. A run
        that has no checkpoint yet has nothing to go back to.
        """
        if pre_sha is None:
            return
        if not await self._checkpoints.is_at(sandbox, pre_sha):
            await self._checkpoints.restore(sandbox, pre_sha)

    async def _save(self, sandbox: Sandbox, inp: acts.ToolExecInput, call: ToolCall) -> str | None:
        """Commit what the call changed and record the checkpoint. None when it changed nothing."""
        tool = self._registry.get(call.name)
        if not (tool is not None and tool.mutates) and call.name not in SHELL_TOOLS:
            return None
        message = f"lh: {call.name} turn {inp.turn_index} call {call_slug(call.id)}"
        sha = await self._checkpoints.commit(sandbox, message)
        if sha is not None:
            await self._recorder.record_checkpoint(
                CheckpointRecord(inp.task_id, sha, "tool", inp.turn_index)
            )
        return sha

    async def _call(self, inp: acts.ToolExecInput) -> tuple[ToolCall, BlobRef | None]:
        """The call with its arguments, and the ref they came from when they were held by ref."""
        summary = inp.call
        if summary.args_ref is None:
            return ToolCall(id=summary.id, name=summary.name, input=summary.input or {}), None
        arguments = await self._blobs.get_json(summary.args_ref, dict[str, Any])
        return ToolCall(id=summary.id, name=summary.name, input=arguments), summary.args_ref

    # ---------- append_results ----------
    @activity.defn(name=acts.APPEND_RESULTS.name)
    async def append_results(self, inp: acts.AppendResultsInput) -> BlobRef:
        inp = small(inp)
        transcript = await self._blobs.get_json(inp.transcript_ref, list[Message])
        results = await asyncio.gather(*(self._result(r) for r in inp.results))
        try:
            grown = self._answer(transcript, list(results), inp.user_text)
        except (
            ValueError
        ) as exc:  # TranscriptError: retrying with the same input fails the same way
            raise not_retried(exc) from exc
        key = blob_key(inp.task_id, "turns", inp.turn_index, "transcript-next.json")
        return small(await self._blobs.put_json(grown, key))

    async def _result(self, summary: acts.ToolResultSummary) -> ToolResult:
        if summary.output_ref is not None:
            text = (await self._blobs.get(summary.output_ref)).decode("utf-8", errors="replace")
        else:
            text = summary.output or ""
        return ToolResult(call_id=summary.call_id, ok=summary.ok, output=text)

    @staticmethod
    def _answer(
        transcript: list[Message], results: list[ToolResult], user_text: str | None
    ) -> list[Message]:
        calls = _calls_of_last_message(transcript)
        if results and not calls:
            raise TranscriptError(
                f"results for {[r.call_id for r in results]}, but the last message of the "
                "transcript has no tool_use"
            )
        grown = answer_tool_calls(transcript, calls, results)
        if user_text:
            grown = append_user_text(grown, user_text)
        validate_pairs(grown)
        return grown


def _calls_of_last_message(transcript: list[Message]) -> list[ToolCall]:
    last = transcript[-1] if transcript else None
    if last is None or last.role != "assistant":
        return []
    return [
        ToolCall(id=b.id, name=b.name, input=b.input)
        for b in last.content
        if isinstance(b, ToolUseBlock)
    ]


def _preview(text: str, ref: BlobRef) -> str:
    """The start of the text, with a note of how much is left and where it is."""
    if len(text) <= acts.OUTPUT_PREVIEW_CHARS:
        return text
    cut = len(text) - acts.OUTPUT_PREVIEW_CHARS
    return f"{text[: acts.OUTPUT_PREVIEW_CHARS]}\n[preview: {cut} more characters in {ref.key}]"
