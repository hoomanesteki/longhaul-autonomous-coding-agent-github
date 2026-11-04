"""read_output and the blobs behind output handles (LH-402).

The tool reads the stored text of an earlier tool call by its handle. It never touches the
sandbox, so every test here hands it one that fails on any use. The integration test in
tests/integration/test_read_output.py runs the same flow against MinIO.
"""

import pytest

from longhaul.blobs.base import BlobStoreBase, handle_key
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import ExecResult, ToolCall, ToolContext, ToolResult
from longhaul.tools.output import (
    HANDLE_PREFIX,
    MAX_LINES,
    ReadOutput,
    output_handle,
    store_output,
)
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase4]

TASK = "lh-402-unit"
CTX = ToolContext(task_id=TASK, turn_index=7, blob_prefix=f"tasks/{TASK}")


def ctx_with(output_max_tokens: int) -> ToolContext:
    return ToolContext(
        task_id=TASK, turn_index=7, blob_prefix=f"tasks/{TASK}", output_max_tokens=output_max_tokens
    )


class UntouchableSandbox:
    """Fails the test on any use. read_output reads the blob store and nothing else."""

    id = "untouchable"
    workdir = "/workspace"

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        raise AssertionError(f"read_output ran a command: {cmd}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_output read a file: {path}")

    async def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"read_output wrote a file: {path}")

    async def destroy(self) -> None:
        raise AssertionError("read_output destroyed the sandbox")


def row(n: int, line: str) -> str:
    return f"{n:>6}\t{line}"


def numbered(count: int) -> str:
    return "".join(f"line {n}\n" for n in range(1, count + 1))


async def keep(blobs: BlobStoreBase, text: str, call_id: str = "toolu_1", task: str = TASK) -> str:
    """Store `text` as the output of a call, and return its handle."""
    await store_output(blobs, task, 3, call_id, text)
    return output_handle(call_id)


async def read(blobs: BlobStoreBase, ctx: ToolContext = CTX, **args: object) -> ToolResult:
    return await ReadOutput(blobs).run(UntouchableSandbox(), args, ctx)


# ---------- handles and where the text is kept ----------
def test_a_handle_is_out_and_the_call_id() -> None:
    assert HANDLE_PREFIX == "out_"
    assert output_handle("toolu_01ABC") == "out_toolu_01ABC"


async def test_the_full_text_is_stored_under_the_turn_and_found_again_by_the_handle() -> None:
    blobs = MemoryBlobStore()
    ref = await store_output(blobs, TASK, 3, "toolu_1", "the whole text\n")
    assert ref.key == f"tasks/{TASK}/turns/0003/tool-toolu_1-output.txt"
    assert await blobs.get(ref) == b"the whole text\n"
    # The handle does not say which turn made the call. A small index blob at a key that
    # only needs the task and the call says it.
    assert handle_key(TASK, "toolu_1") == f"tasks/{TASK}/outputs/toolu_1.json"
    assert (await read(blobs, handle="out_toolu_1")).output == row(1, "the whole text")


async def test_storing_the_output_of_a_call_again_changes_nothing() -> None:
    # A retried activity runs store_output again for the same call.
    blobs = MemoryBlobStore()
    first = await store_output(blobs, TASK, 3, "toolu_1", "same\n")
    second = await store_output(blobs, TASK, 3, "toolu_1", "same\n")
    assert first == second
    assert len(blobs.objects) == 2


async def test_a_call_id_with_odd_characters_still_has_a_working_handle() -> None:
    # Some OpenAI-compatible servers send ids such as "functions.get:0".
    blobs = MemoryBlobStore()
    handle = await keep(blobs, "odd id\n", call_id="functions.get:0")
    assert handle == "out_functions.get:0"
    assert (await read(blobs, handle=handle)).output == row(1, "odd id")
    other = await keep(blobs, "second\n", call_id="functions.get:1")
    assert (await read(blobs, handle=other)).output == row(1, "second")
    assert (await read(blobs, handle=handle)).output == row(1, "odd id")


# ---------- paging ----------
async def test_the_first_page_starts_at_line_one() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, numbered(10))
    result = await read(blobs, handle=handle)
    assert result.ok
    assert result.output.split("\n") == [row(n, f"line {n}") for n in range(1, 11)]


async def test_start_line_and_end_line_pick_the_lines() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, numbered(100))
    result = await read(blobs, handle=handle, start_line=41, end_line=43)
    assert result.ok
    assert result.output.split("\n") == [row(n, f"line {n}") for n in (41, 42, 43)]


async def test_a_page_is_at_most_400_lines_and_says_where_to_go_on() -> None:
    assert MAX_LINES == 400
    blobs = MemoryBlobStore()
    handle = await keep(blobs, numbered(1000))
    result = await read(blobs, handle=handle, end_line=900)
    rows = result.output.split("\n")
    assert rows[:400] == [row(n, f"line {n}") for n in range(1, 401)]
    assert rows[400] == "[showing lines 1-400 of 1000; call again with start_line=401 to continue]"
    assert len(rows) == 401
    nxt = await read(blobs, handle=handle, start_line=401)
    assert nxt.output.split("\n")[0] == row(401, "line 401")
    last = await read(blobs, handle=handle, start_line=801)
    assert last.output.split("\n")[-1] == row(1000, "line 1000")
    assert "showing" not in last.output


async def test_an_end_line_past_the_end_is_cut_to_the_end() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, numbered(5))
    result = await read(blobs, handle=handle, start_line=4, end_line=500)
    assert result.output.split("\n") == [row(4, "line 4"), row(5, "line 5")]


async def test_a_start_line_past_the_end_is_an_error_the_model_can_read() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, numbered(5))
    result = await read(blobs, handle=handle, start_line=6)
    assert not result.ok
    assert result.output == "start_line 6 is beyond the end of the output (5 lines)"


async def test_an_end_line_before_the_start_line_is_an_error() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, numbered(5))
    result = await read(blobs, handle=handle, start_line=4, end_line=2)
    assert not result.ok
    assert "end_line 2 is before start_line 4" in result.output


async def test_an_empty_output_reads_as_empty() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, "")
    result = await read(blobs, handle=handle)
    assert (result.ok, result.output) == (True, "(empty output)")


async def test_lines_are_counted_like_the_marker_counts_them() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, "one\r\ntwo\r\nthree\r\n")
    result = await read(blobs, handle=handle, start_line=2)
    assert result.output.split("\n") == [row(2, "two"), row(3, "three")]


async def test_a_page_fits_the_token_cap_of_the_context() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, numbered(1000))
    small = ctx_with(output_max_tokens=200)
    result = await read(blobs, small, handle=handle)
    assert result.ok
    assert len(result.output) <= int(200 * 3.5)
    rows = result.output.split("\n")
    assert rows[0] == row(1, "line 1")
    assert rows[-1].startswith("[showing lines 1-") and "call again with start_line=" in rows[-1]


async def test_a_line_longer_than_the_page_is_cut_so_the_page_still_fits() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, "x" * 20_000 + "\nnext\n")
    result = await read(blobs, ctx_with(output_max_tokens=200), handle=handle)
    assert result.ok
    assert len(result.output) <= int(200 * 3.5)
    assert "[line cut: " in result.output
    assert "start_line=2" in result.output


# ---------- filtering ----------
FAILURES = (
    "============ test session starts ============\n"
    "tests/test_a.py::test_one PASSED\n"
    "tests/test_a.py::test_two FAILED\n"
    "tests/test_b.py::test_three PASSED\n"
    "tests/test_b.py::test_four FAILED\n"
    "============ 2 failed, 2 passed ============\n"
)


async def test_a_pattern_returns_exactly_the_matching_lines_with_their_numbers() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, FAILURES)
    result = await read(blobs, handle=handle, pattern="FAILED")
    assert result.ok
    assert result.output.split("\n") == [
        row(3, "tests/test_a.py::test_two FAILED"),
        row(5, "tests/test_b.py::test_four FAILED"),
    ]


async def test_a_pattern_is_text_to_find_and_not_a_regular_expression() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, "a.c\nabc\na+c\n(x)\n")
    assert (await read(blobs, handle=handle, pattern="a.c")).output == row(1, "a.c")
    assert (await read(blobs, handle=handle, pattern="a+c")).output == row(3, "a+c")
    assert (await read(blobs, handle=handle, pattern="(x)")).output == row(4, "(x)")


async def test_a_pattern_is_case_sensitive() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, "Failed\nFAILED\n")
    result = await read(blobs, handle=handle, pattern="FAILED")
    assert result.output == row(2, "FAILED")


async def test_a_pattern_with_no_match_is_a_result_and_not_an_error() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, FAILURES)
    result = await read(blobs, handle=handle, pattern="SEGFAULT")
    assert result.ok
    assert result.output == 'no line contains "SEGFAULT" (searched lines 1-6)'


async def test_a_pattern_can_be_limited_to_a_range_of_lines() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, FAILURES)
    result = await read(blobs, handle=handle, pattern="FAILED", start_line=4)
    assert result.output == row(5, "tests/test_b.py::test_four FAILED")
    result = await read(blobs, handle=handle, pattern="FAILED", end_line=3)
    assert result.output == row(3, "tests/test_a.py::test_two FAILED")
    none = await read(blobs, handle=handle, pattern="FAILED", start_line=4, end_line=4)
    assert none.output == 'no line contains "FAILED" (searched lines 4-4)'


async def test_matches_are_at_most_400_lines_and_say_where_to_go_on() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, "".join(f"FAILED case {n}\n" for n in range(1, 601)))
    result = await read(blobs, handle=handle, pattern="FAILED")
    rows = result.output.split("\n")
    assert rows[:400] == [row(n, f"FAILED case {n}") for n in range(1, 401)]
    assert rows[400] == (
        "[showing 400 of 600 matching lines; call again with start_line=401 to continue]"
    )
    rest = await read(blobs, handle=handle, pattern="FAILED", start_line=401)
    assert rest.output.split("\n") == [row(n, f"FAILED case {n}") for n in range(401, 601)]


async def test_a_pattern_over_a_start_line_past_the_end_is_an_error() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, FAILURES)
    result = await read(blobs, handle=handle, pattern="FAILED", start_line=50)
    assert not result.ok
    assert "beyond the end of the output (6 lines)" in result.output


# ---------- handles that do not resolve ----------
async def test_an_unknown_handle_is_an_error_result() -> None:
    result = await read(MemoryBlobStore(), handle="out_toolu_missing")
    assert not result.ok
    assert "unknown output handle: out_toolu_missing" in result.output


@pytest.mark.parametrize("handle", ["toolu_1", "out_", "OUT_toolu_1", "tasks/x/outputs/a.json"])
async def test_a_string_that_is_not_a_handle_is_an_error_result(handle: str) -> None:
    blobs = MemoryBlobStore()
    await keep(blobs, "text\n")
    result = await read(blobs, handle=handle)
    assert not result.ok
    assert "unknown output handle" in result.output


async def test_a_handle_from_another_task_does_not_resolve() -> None:
    blobs = MemoryBlobStore()
    handle = await keep(blobs, "secret of the other task\n", task="lh-other-task")
    result = await read(blobs, handle=handle)
    assert not result.ok
    assert "secret" not in result.output


@pytest.mark.parametrize(
    "handle", ["out_../../other-task/outputs/toolu_1", "out_a/b", "out_\\x", "out_ a", "out_\x00"]
)
async def test_a_handle_cannot_name_a_key_outside_the_task(handle: str) -> None:
    blobs = MemoryBlobStore()
    await keep(blobs, "text\n", task="other-task")
    result = await read(blobs, handle=handle)
    assert not result.ok
    assert "\ttext" not in result.output


async def test_an_index_whose_blob_is_gone_is_an_error_result() -> None:
    blobs = MemoryBlobStore()
    ref = await store_output(blobs, TASK, 3, "toolu_1", "text\n")
    del blobs.objects[ref.key]
    result = await read(blobs, handle="out_toolu_1")
    assert not result.ok
    assert "unknown output handle: out_toolu_1" in result.output


async def test_a_damaged_blob_is_an_error_result_and_never_text_for_the_model() -> None:
    blobs = MemoryBlobStore()
    ref = await store_output(blobs, TASK, 3, "toolu_1", "original text\n")
    blobs.objects[ref.key] = b"tampered text\n"
    result = await read(blobs, handle="out_toolu_1")
    assert not result.ok
    assert "tampered" not in result.output
    assert "out_toolu_1" in result.output


async def test_a_damaged_index_is_an_error_result() -> None:
    blobs = MemoryBlobStore()
    await store_output(blobs, TASK, 3, "toolu_1", "text\n")
    blobs.objects[handle_key(TASK, "toolu_1")] = b"{not json"
    result = await read(blobs, handle="out_toolu_1")
    assert not result.ok
    assert "out_toolu_1" in result.output


# ---------- as a tool of the registry ----------
def test_read_output_is_in_the_default_registry_and_changes_nothing() -> None:
    registry = default_registry()
    tool = registry.get("read_output")
    assert tool is not None and tool.mutates is False
    spec = tool.spec
    assert spec.name == "read_output"
    assert spec.input_schema["required"] == ["handle"]
    assert set(spec.input_schema["properties"]) == {"handle", "start_line", "end_line", "pattern"}
    assert spec.input_schema["additionalProperties"] is False


async def test_the_registry_reads_from_the_store_it_was_built_with() -> None:
    blobs = MemoryBlobStore()
    registry = default_registry(blobs=blobs)
    assert registry.blobs is blobs
    handle = await keep(blobs, numbered(3))
    call = ToolCall(id="c1", name="read_output", input={"handle": handle, "start_line": 2})
    result = await registry.run(call, UntouchableSandbox(), CTX)
    assert result.ok and result.call_id == "c1"
    assert result.output.split("\n") == [row(2, "line 2"), row(3, "line 3")]


async def test_a_registry_without_a_store_gets_a_memory_store_of_its_own() -> None:
    registry = default_registry()
    assert isinstance(registry.blobs, MemoryBlobStore)
    handle = await keep(registry.blobs, "kept\n")
    call = ToolCall(id="c1", name="read_output", input={"handle": handle})
    assert (await registry.run(call, UntouchableSandbox(), CTX)).output == row(1, "kept")


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"handle": ""},
        {"handle": "out_x", "start_line": 0},
        {"handle": "out_x", "start_line": "1"},
        {"handle": "out_x", "end_line": 0},
        {"handle": "out_x", "pattern": ""},
        {"handle": "out_x", "path": "a.txt"},
    ],
)
async def test_arguments_the_schema_refuses_never_reach_the_blob_store(
    arguments: dict[str, object],
) -> None:
    registry = default_registry()
    call = ToolCall(id="c1", name="read_output", input=arguments)
    result = await registry.run(call, UntouchableSandbox(), CTX)
    assert not result.ok
    assert "invalid input for read_output" in result.output


async def test_a_float_line_number_from_a_loose_server_is_accepted() -> None:
    blobs = MemoryBlobStore()
    registry = default_registry(blobs=blobs)
    handle = await keep(blobs, numbered(5))
    call = ToolCall(id="c", name="read_output", input={"handle": handle, "start_line": 4.0})
    result = await registry.run(call, UntouchableSandbox(), CTX)
    assert result.ok and result.output.split("\n")[0] == row(4, "line 4")
