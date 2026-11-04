"""Output handles and read_output: reading the full text of a long tool output (LH-402).

When a tool prints more than the model should read at once, the model gets a head, a tail and a
marker with a handle (context/truncate.py). The handle is `out_<call id>`, and read_output turns
it back into text, a page or a filtered list of lines at a time.

Where the text lives. tool_exec stores the full text of every call under the turn that made it
(`store_output`). A handle does not say which turn that was, and it has to resolve without the
database: after Continue-As-New or a restart, a worker has the blob store and nothing else. So
`store_output` also writes a small index blob at a key that needs only the task and the call
(blobs.base.handle_key). The index holds the BlobRef of the text. read_output reads the index,
then the text by its ref, so the text is still checked against its hash.

Scope. A handle resolves inside the task of the ToolContext it is used in. Another task's
handle is unknown, and a handle with slashes or dots in it is cleaned into a name inside the
task's own prefix, so it cannot reach another key.

No sandbox. read_output reads the blob store, never the sandbox, so the text of an old call
cannot change because the agent edited a file since. That is all it promises: in the durable
path tool_exec looks the sandbox up before it runs any tool, so a sandbox that is gone fails this
call with SANDBOX_GONE like every other one, and the workflow recreates it first.

Paging and filtering. A page is at most MAX_LINES lines and also fits the ToolContext's token
cap, because a page that was over the cap would itself be cut and get a new handle. When a page
ends early, its footer names the line to go on from. `pattern` is plain text that a line must
contain, not a regular expression. The text is model input, and a regular expression written
by a model, or taken from a repo file, can make Python's matcher run for minutes. That would
block the worker's event loop and with it the heartbeats of the activity.

The blob store is given when the tool is built, because the Tool protocol (contracts.py) runs a
tool with a sandbox, its arguments and a context, and has no place for one. default_registry
builds the tool with the registry's store.
"""

from __future__ import annotations

from typing import Any

from longhaul.blobs.base import (
    BlobError,
    BlobNotFoundError,
    BlobStoreBase,
    blob_key,
    call_slug,
    handle_key,
)
from longhaul.context.truncate import split_lines
from longhaul.contracts import BlobRef, Sandbox, ToolContext, ToolResult, ToolSpec
from longhaul.tools import ToolError
from longhaul.tools.files import (
    CHARS_PER_TOKEN,
    FOOTER_RESERVE,
    MIN_PAGE_CHARS,
    fail,
    number_lines,
    numbered_line,
    ok,
    page_end,
)

HANDLE_PREFIX = "out_"
# The most lines one read_output call returns, for a page and for a list of matches.
MAX_LINES = 400


def output_handle(call_id: str) -> str:
    """The handle the model is given for the output of the tool call `call_id`."""
    return f"{HANDLE_PREFIX}{call_id}"


async def store_output(
    blobs: BlobStoreBase, task_id: str, turn_index: int, call_id: str, text: str
) -> BlobRef:
    """Store the full text of a call, and the index that lets its handle find it again.

    The text goes first and the index second, so an index never names a blob that is not
    there. Running it again for the same call, as a retried activity does, writes the same
    bytes under the same keys.
    """
    ref = await blobs.put(
        text.encode("utf-8", errors="replace"),
        key=blob_key(task_id, "turns", turn_index, f"tool-{call_slug(call_id)}-output.txt"),
    )
    await blobs.put_json(ref, handle_key(task_id, call_id))
    return ref


def _unknown(handle: str) -> ToolError:
    return ToolError(
        f"unknown output handle: {handle}. A handle comes from the note at the end of a cut "
        "tool result."
    )


class ReadOutput:
    spec = ToolSpec(
        name="read_output",
        description=(
            "Read lines of a long tool output by its handle. "
            "Use pattern to get only the lines that contain some text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The handle from the note at the end of a cut result.",
                },
                "start_line": {"type": "integer", "minimum": 1, "default": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Only lines that contain this text. Plain text, not a regex.",
                },
            },
            "required": ["handle"],
            "additionalProperties": False,
        },
    )
    mutates = False

    def __init__(self, blobs: BlobStoreBase) -> None:
        self.blobs = blobs

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # The sandbox argument is deliberately unused: the text is in the blob store.
        start = int(args.get("start_line", 1))
        end = args.get("end_line")
        end = None if end is None else int(end)
        max_chars = int(ctx.output_max_tokens * CHARS_PER_TOKEN)
        try:
            text = await self._text(ctx.task_id, args["handle"])
            pattern = args.get("pattern")
            if pattern is None:
                page = number_lines(text, start, end, max_chars, max_lines=MAX_LINES, what="output")
            else:
                page = matching_lines(text, pattern, start, end, max_chars)
        except ToolError as exc:
            return fail(str(exc))
        return ok(page)

    async def _text(self, task_id: str, handle: str) -> str:
        call_id = handle.removeprefix(HANDLE_PREFIX) if handle.startswith(HANDLE_PREFIX) else ""
        if not call_id:
            raise _unknown(handle)
        try:
            index = await self.blobs.get_by_key(handle_key(task_id, call_id))
            data = await self.blobs.get(BlobRef.model_validate_json(index))
        except BlobNotFoundError:
            raise _unknown(handle) from None
        except (BlobError, ValueError) as exc:
            # A damaged index or text is not the model's doing, and the model must not read
            # bytes that failed their check. It gets a result that says so.
            raise ToolError(f"the stored text for {handle} could not be read: {exc}") from None
        return data.decode("utf-8", errors="replace")


def matching_lines(text: str, pattern: str, start: int, end: int | None, max_chars: int) -> str:
    """The lines from `start` to `end` that contain `pattern`, each with its line number.

    At most MAX_LINES lines are shown, and fewer when they would not fit `max_chars`. A footer
    then says how many lines matched and the line to go on from. A pattern that matches
    nothing is an answer and not an error.
    """
    lines = split_lines(text)
    last = page_end(len(lines), start, end, "output")
    if last is None:
        return "(empty output)"
    found = [n for n in range(start, last + 1) if pattern in lines[n - 1]]
    if not found:
        return f'no line contains "{pattern}" (searched lines {start}-{last})'
    room = max(max_chars - FOOTER_RESERVE, MIN_PAGE_CHARS)
    rows: list[str] = []
    used = 0
    for n in found[:MAX_LINES]:
        row = numbered_line(n, lines[n - 1], room)
        if rows and used + len(row) + 1 > room:
            break
        rows.append(row)
        used += len(row) + 1
    if len(rows) < len(found):
        rows.append(
            f"[showing {len(rows)} of {len(found)} matching lines; "
            f"call again with start_line={found[len(rows)]} to continue]"
        )
    return "\n".join(rows)
