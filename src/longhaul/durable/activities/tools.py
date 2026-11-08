"""The tool_exec and append_results activities.

`tool_exec` runs one tool call in the task's sandbox, stores the full text that came out, and
writes the `tool_calls` row. Its `ToolResult` keeps a short preview of the text in `output` and
names the whole text in `output_ref`, so a long test log never sits in the workflow history. It
also writes the small index that lets the handle `out_<call id>` find the text again without the
database (tools/output.py), which is how read_output pages through a cut output (LH-402).

`append_results` answers the tool_use blocks of the last assistant message. It reads the full
text of each result from its blob and gives the model that text when it is within
`tool_output_max_tokens`, or else its first and last lines and the handle (context/truncate.py).
Then it applies the pairing rules of turns.py (D9) and stores the new transcript. It also adds
the harness text, such as the nudge, which goes after the results.

`append_results` also writes the `results` part of the turn to the log of the task (context/log.py),
with the text the model was given, so the log holds the whole transcript while the view that the
next call reads is elided and compacted (LH-404).

The cut is made in append_results and not in tool_exec, for three reasons. It is a plain
function of the stored text and the cap, so a retried append_results builds the same transcript
and no second blob has to be kept in step with the first. What the model reads can be as long as
the cap allows, about 14,000 characters at the default, while the result of tool_exec goes into
the workflow history, which keeps previews and refs only (ADR-0005). And one place sees every
result of a turn, so the cap holds for all of them.

Every `tool_exec` call starts from a known commit and saves what it changed as a new one
(ADR-0006, failure mode F1), so Temporal can run an attempt again after the worker died in the
middle of it:

1. If the workspace is not at `pre_sha` with a clean tree, `restore(pre_sha)` puts it there.
   This is what undoes the edit of an attempt that died after its tool ran. A workspace that is
   already there is left alone, because a restore also deletes the ignored files that the agent
   made on purpose, such as the output of a build.
2. The tool runs.
3. If the tool mutates, or it runs a shell command (SHELL_TOOLS), `commit` saves the tree as a
   child of `pre_sha`. A tree equal to `pre_sha` gives no commit and no `post_sha`. Otherwise
   `post_sha` is the new commit, and a `checkpoints` row of kind `tool` names it. The tree is
   compared with `pre_sha` and not with HEAD, because a shell command can make commits of its
   own, or revert or reset, and the work would then look clean against the new HEAD and be
   undone by the next restore.

The rows are written after the commit, the `tool_calls` row first and the `checkpoints` row
last. A worker that dies between the commit and the rows leaves a commit that nothing names,
and its retry writes the rows for the commit it makes. A worker that dies after the last write,
before Temporal has the result, leaves a `checkpoints` row for a commit that the retry replaces
on the branch. That commit can still be restored by its sha, and the `tool_calls` row and the
result name the commit that counts. The row means "this commit was saved", not "this commit is
on the branch".

A run that asked for a window of its own (`longhaul run --window`) has a smaller cap. Both
activities get the window in their input and use result_cap(), the cap of the worker cut to a share
of that window (context/tokens.py). `llm_turn` keeps room for the results of a turn by the same
number, so the three have to agree.

Each run that reached its tool is timed into longhaul_tool_duration_seconds (LH-704), the failing
ones too. A call that found its sandbox gone never ran, and is not timed.

The planning phase (LH-501) changes nothing. A call with `read_only` set runs from `pre_sha` like
any other, and then the tree is looked at: if the call changed it, `restore(pre_sha)` puts it
back, and no commit is made, so the result has no `post_sha` and no checkpoint row is written.
Files that git ignores do not count, as for `is_at` before a call, so the caches of a test run are
left alone. The text of the result ends with PLANNING_NOTE when something was undone, so the
planner does not build on a file that is gone. The planner is not offered `edit_file` or
`write_file`, and the workflow answers a call outside its tool set without running it. This reset
is the guarantee under both: whatever a shell command did, the next call and the coder start from
the checkpoint. A planner call that has no `pre_sha` has nothing to go back to and is left as it is.

A sandbox that is gone fails the call with SandboxGoneError, which is not retried: asking again
finds the same empty place. The workflow recreates the sandbox (workspace.py) and runs the call
again from the same `pre_sha`. The error is raised for a container that was gone before the
call, and also for one that vanished during it, because docker raises various things for that:
when any step fails and the sandbox is no longer there, the sandbox is the cause.
"""

from __future__ import annotations

import asyncio
import time
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
from longhaul.blobs.base import BlobStoreBase, blob_key, call_slug
from longhaul.context.log import added_messages, append_to_log
from longhaul.context.tokens import result_cap
from longhaul.context.truncate import shape_output
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
from longhaul.durable.activities.support import heartbeating, not_retried, small
from longhaul.telemetry import metrics
from longhaul.telemetry.otel import tool_span
from longhaul.tools.output import output_handle, store_output
from longhaul.tools.registry import ToolRegistry

# The two tools that run a shell command. Whether a command changes files is not known in
# advance, so `mutates` is False for both (tools/exec.py) and the tree is looked at after them.
SHELL_TOOLS = frozenset({"run_command", "run_tests"})

# What the planner reads at the end of a call that wrote files, which were then undone (LH-501).
PLANNING_NOTE = (
    "[planning phase: files cannot be changed here, so what this command wrote was undone]"
)


class SandboxLookup(Protocol):
    """The part of a SandboxManager that tool_exec uses: find a running sandbox by its id."""

    async def get(self, sandbox_id: str) -> Sandbox | None: ...


class WorkspaceCheckpoints(CheckpointStore, Protocol):
    """A CheckpointStore that can also say whether the workspace already is at a commit, that
    can build a commit on a named parent, and that can restore without deleting ignored files.

    GitCheckpointStore has `is_at`, the `parent` argument of `commit` and the `keep_ignored`
    argument of `restore`. The contracts do not, and they stay as they are, so what tool_exec
    needs beyond them is added here, where it is used.
    """

    async def commit(
        self, sandbox: Sandbox, message: str, *, parent: str | None = None
    ) -> str | None: ...

    async def restore(self, sandbox: Sandbox, sha: str, *, keep_ignored: bool = False) -> None: ...

    async def is_at(self, sandbox: Sandbox, sha: str) -> bool: ...


class NoCheckpoints:
    """A checkpoint store that stores nothing, for tests of the tools. A worker passes a real one.

    Nothing is ever saved and the workspace always counts as being where it should be, so a
    call neither restores, commits nor leaves a `post_sha`.
    """

    async def init_task(self, task_id: str, clone_url: str, base_sha: str) -> str:
        return base_sha

    async def commit(
        self, sandbox: Sandbox, message: str, *, parent: str | None = None
    ) -> str | None:
        return None  # the tree counts as clean

    async def restore(self, sandbox: Sandbox, sha: str, *, keep_ignored: bool = False) -> None:
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

    `registry` has to be built on the same blob store as `blobs` (default_registry(blobs=...)).
    The activities write the full text of every output there, and read_output, which belongs to
    the registry, reads it from its own store. With two different stores no handle resolves, and
    the model, which is told not to run the command again, could never read the middle of a long
    output. A registry that is on another store is refused here, when the worker starts, and not
    left to fail on every handle.
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
        if registry.blobs is not blobs:
            raise ValueError(
                "the tool registry reads output handles from a different blob store than the "
                "tool activities write to; build it with default_registry(blobs=<that store>)"
            )
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
            output_max_tokens=result_cap(self._output_max_tokens, inp.window_tokens),
        )
        try:
            async with heartbeating():
                await self._start_from(sandbox, inp.pre_sha)
                with tool_span(
                    name=call.name,
                    call_id=call.id,
                    task_id=inp.task_id,
                    turn_index=inp.turn_index,
                    arguments=call.input,
                ) as span:
                    began = time.monotonic()
                    result = await tool_step(self._registry, sandbox, call, ctx)
                    self._time(call.name, time.monotonic() - began)
                    span.record(result)
                if inp.read_only:
                    post_sha = None
                    if await self._discard(sandbox, inp.pre_sha):
                        result = result.model_copy(
                            update={"output": f"{result.output}\n{PLANNING_NOTE}"}
                        )
                else:
                    post_sha = await self._save(sandbox, inp, call)
        except Exception as exc:
            if await self._is_gone(inp.sandbox_id):
                raise _gone(inp.sandbox_id) from exc
            raise
        # The text and its handle index. A retry writes the same bytes under the same keys.
        output_ref = await store_output(
            self._blobs, inp.task_id, inp.turn_index, call.id, result.output
        )
        slug = call_slug(call.id)
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
        # Last, so that a checkpoint row is written only for a commit the call row names. A
        # worker that dies before this line leaves a commit and no row, and its retry writes the
        # one row for the commit it makes.
        if post_sha is not None:
            await self._recorder.record_checkpoint(
                CheckpointRecord(inp.task_id, post_sha, "tool", inp.turn_index)
            )
        return small(returned)

    def _time(self, tool: str, seconds: float) -> None:
        """Add a run to the duration histogram. The model chooses the name of the tool it calls,
        so a name that is not in the registry is one label and not a new series for each.
        """
        known = self._registry.get(tool) is not None
        metrics.current().observe_tool(tool if known else metrics.UNKNOWN, seconds)

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

    async def _discard(self, sandbox: Sandbox, pre_sha: str | None) -> bool:
        """Put the tree back on `pre_sha` after a call of the planning phase. True if it moved.

        Nothing is saved: the planner may look and may run commands, and what it leaves behind
        is not work. A tree that is at `pre_sha` with nothing to save is left alone, because a
        restore also deletes ignored files. When it does restore, what git ignores stays: that is
        what the setup built, the network is off by now, and nothing could build it again.
        """
        if pre_sha is None or await self._checkpoints.is_at(sandbox, pre_sha):
            return False
        await self._checkpoints.restore(sandbox, pre_sha, keep_ignored=True)
        return True

    async def _save(self, sandbox: Sandbox, inp: acts.ToolExecInput, call: ToolCall) -> str | None:
        """Commit what the call changed. None when it changed nothing since `pre_sha`.

        The commit builds on `pre_sha` and not on wherever HEAD is: a shell command can commit,
        revert or reset on its own, and the tree is then clean against the new HEAD while the
        work is still unsaved. The checkpoint row is written later, once the call row is.
        """
        tool = self._registry.get(call.name)
        if not (tool is not None and tool.mutates) and call.name not in SHELL_TOOLS:
            return None
        message = f"lh: {call.name} turn {inp.turn_index} call {call_slug(call.id)}"
        return await self._checkpoints.commit(sandbox, message, parent=inp.pre_sha)

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
        cap = result_cap(self._output_max_tokens, inp.window_tokens)
        results = await asyncio.gather(*(self._result(r, cap) for r in inp.results))
        try:
            grown = self._answer(transcript, list(results), inp.user_text)
        except (
            ValueError
        ) as exc:  # TranscriptError: retrying with the same input fails the same way
            raise not_retried(exc) from exc
        key = blob_key(inp.task_id, "turns", inp.turn_index, "transcript-next.json")
        ref = small(await self._blobs.put_json(grown, key))
        # The same step for the log, as the part of the turn it belongs to. A retry writes the
        # same part again.
        await append_to_log(
            self._blobs, inp.task_id, inp.turn_index, "results", added_messages(transcript, grown)
        )
        return ref

    async def _result(self, summary: acts.ToolResultSummary, cap: int) -> ToolResult:
        if summary.output_ref is not None:
            full = (await self._blobs.get(summary.output_ref)).decode("utf-8", errors="replace")
            text = shape_output(full, cap, output_handle(summary.call_id))
        else:
            text = summary.output or ""  # a short text that no tool made, such as a bad finish
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
