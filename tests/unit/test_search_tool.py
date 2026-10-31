"""search: how the tool reads the exit code and the output of rg.

A sandbox answers the rg command with a canned result, so these tests need no
ripgrep on the machine and can put matches in any order. The integration tests
run the real thing.
"""

import base64
import json
import shlex
from collections.abc import AsyncIterator, Callable

import pytest

from longhaul.contracts import ExecResult, ToolCall, ToolContext, ToolResult
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")


def field(value: str | bytes) -> dict[str, str]:
    """How rg writes a path or a line: as text, or as base64 when it is not valid UTF-8."""
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode()}
    return {"text": value}


def hit(path: str | bytes, line: int, text: str | bytes) -> str:
    """One match event of `rg --json`."""
    body = text + b"\n" if isinstance(text, bytes) else text + "\n"
    data = {"path": field(path), "lines": field(body), "line_number": line, "submatches": []}
    return json.dumps({"type": "match", "data": data})


def rg_result(*events: str, stderr: str = "", code: int = 0, timed_out: bool = False) -> ExecResult:
    return ExecResult(
        exit_code=code,
        stdout="\n".join(events),
        stderr=stderr,
        timed_out=timed_out,
        duration_ms=3,
    )


class RgSandbox(FakeSandbox):
    """A FakeSandbox that answers `rg` with a canned result and remembers the command."""

    def __init__(self, result: ExecResult) -> None:
        super().__init__()
        self.result = result
        self.rg_commands: list[str] = []

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        if cmd.startswith("rg "):
            self.rg_commands.append(cmd)
            return self.result
        return await super().exec(cmd, timeout_s=timeout_s, env=env)


MakeBox = Callable[[ExecResult], RgSandbox]


@pytest.fixture
async def make_box() -> AsyncIterator[MakeBox]:
    boxes: list[RgSandbox] = []

    def make(result: ExecResult) -> RgSandbox:
        box = RgSandbox(result)
        boxes.append(box)
        return box

    yield make
    for box in boxes:
        await box.destroy()


async def search(box: RgSandbox, **args: object) -> ToolResult:
    call = ToolCall(id="c", name="search", input={"pattern": "target", **args})
    return await default_registry().run(call, box, CTX)


# ---------- order ----------


async def test_matches_come_back_sorted_by_path_and_then_line_number(
    make_box: MakeBox,
) -> None:
    # rg searches on several threads and prints a file when its thread is done, so
    # the same search can print its files in a different order each time.
    out = [hit("b.py", 3, "b3"), hit("a.py", 10, "a10"), hit("a.py", 9, "a9"), hit("a.py", 2, "a2")]
    r = await search(make_box(rg_result(*out)))
    assert r.ok
    assert r.output.splitlines() == ["a.py:2: a2", "a.py:9: a9", "a.py:10: a10", "b.py:3: b3"]


async def test_the_same_matches_in_any_order_give_the_same_text(make_box: MakeBox) -> None:
    events = [hit(f"d{i % 7}/f{i}.py", i, f"def target_{i}") for i in range(60)]
    first = await search(make_box(rg_result(*events)))
    second = await search(make_box(rg_result(*reversed(events))))
    third = await search(make_box(rg_result(*events[30:], *events[:30])))
    assert first.output == second.output == third.output


async def test_the_cut_at_max_results_keeps_the_first_matches_in_order(
    make_box: MakeBox,
) -> None:
    # Cutting before sorting would keep whichever files rg happened to finish first.
    out = [hit("z.py", 1, "z"), hit("m.py", 1, "m"), hit("a.py", 2, "a2"), hit("a.py", 1, "a1")]
    r = await search(make_box(rg_result(*out)), max_results=2)
    assert r.ok
    assert r.output.splitlines() == ["a.py:1: a1", "a.py:2: a2", "... 2 more matches not shown"]


# ---------- exit codes ----------


async def test_matches_survive_a_file_rg_could_not_read(make_box: MakeBox) -> None:
    # rg exits 2 for any error, also after it printed matches from the files it could read.
    box = make_box(
        rg_result(
            hit("real.py", 1, "target = 1"),
            stderr="rg: ./no.py: Permission denied (os error 13)\n",
            code=2,
        )
    )
    r = await search(box)
    assert r.ok, r.output
    lines = r.output.splitlines()
    assert lines[0] == "real.py:1: target = 1"
    assert lines[-1].startswith("note: some files could not be searched")
    assert "no.py: Permission denied" in lines[-1]


async def test_the_note_about_unreadable_files_stays_short(make_box: MakeBox) -> None:
    errors = "".join(f"rg: ./f{i}.py: Permission denied (os error 13)\n" for i in range(40))
    r = await search(make_box(rg_result(hit("real.py", 1, "x"), stderr=errors, code=2)))
    assert r.ok
    note = r.output.splitlines()[-1]
    assert "f0.py" in note and "f39.py" not in note
    assert "37 more" in note
    assert len(note) < 400


async def test_a_partial_result_without_stderr_text_still_warns(make_box: MakeBox) -> None:
    r = await search(make_box(rg_result(hit("real.py", 1, "x"), code=2)))
    assert r.ok
    assert r.output.splitlines()[-1].startswith("note: ")


async def test_a_bad_pattern_is_a_failure_with_rgs_message(make_box: MakeBox) -> None:
    complaint = "rg: regex parse error:\n    (?:()\n    ^\nerror: unclosed group\n"
    r = await search(make_box(rg_result(stderr=complaint, code=2)), pattern="(")
    assert not r.ok
    assert r.output.startswith("rg failed: ") and "unclosed group" in r.output


async def test_no_matches_is_a_result_and_not_a_failure(make_box: MakeBox) -> None:
    r = await search(make_box(rg_result(code=1)))
    assert r.ok and r.output == "no matches"


async def test_an_unexpected_exit_code_is_a_failure(make_box: MakeBox) -> None:
    r = await search(make_box(rg_result(stderr="bash: rg: command not found", code=127)))
    assert not r.ok
    assert "command not found" in r.output


async def test_a_search_that_ran_out_of_time_says_so(make_box: MakeBox) -> None:
    r = await search(make_box(rg_result(code=137, timed_out=True)))
    assert not r.ok
    assert "timed out" in r.output and "narrower" in r.output


# ---------- what rg is asked ----------


async def test_pattern_path_and_glob_reach_rg_as_separate_words(make_box: MakeBox) -> None:
    box = make_box(rg_result(code=1))
    await box.write_file("src/a.py", b"x\n")
    await search(box, pattern="-v 'x'", path="./src", glob="*.py")
    # The pattern follows -e and the path follows --, so neither can be read as an option.
    assert shlex.split(box.rg_commands[-1]) == [
        "rg",
        "--json",
        "-e",
        "-v 'x'",
        "-g",
        "*.py",
        "--",
        "src",
    ]


async def test_a_path_that_is_not_there_is_not_searched(make_box: MakeBox) -> None:
    box = make_box(rg_result(code=1))
    r = await search(box, path="nope")
    assert not r.ok and "not found: nope" in r.output
    assert box.rg_commands == []


# ---------- text that cannot be sent ----------


@pytest.mark.parametrize(
    "args", [{"pattern": "a\ud800"}, {"glob": "\ud800"}], ids=["pattern", "glob"]
)
async def test_a_lone_surrogate_in_the_pattern_or_glob_is_a_result(
    make_box: MakeBox, args: dict[str, object]
) -> None:
    box = make_box(rg_result(code=1))
    r = await search(box, **args)
    assert not r.ok and "UTF-8" in r.output
    assert box.rg_commands == []


# ---------- text that is not UTF-8 ----------


async def test_text_that_is_not_utf8_is_shown_with_replacement_characters(
    make_box: MakeBox,
) -> None:
    # rg sends such a path or line as base64 under "bytes" and not as "text".
    box = make_box(rg_result(hit(b"n\xffame.txt", 1, b"x\xff target")))
    r = await search(box)
    assert r.ok, r.output
    assert r.output == "n�ame.txt:1: x� target"
