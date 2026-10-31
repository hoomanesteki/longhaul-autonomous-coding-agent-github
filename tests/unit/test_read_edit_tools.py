"""read_file and edit_file on a fake sandbox: what the model sees is what the file holds."""

import re
from collections.abc import AsyncIterator

import pytest

from longhaul.contracts import ToolCall, ToolContext, ToolResult
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.files import EditFile
from longhaul.tools.registry import ToolRegistry, default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")
CRLF = b"alpha\r\nbeta\r\ngamma\r\n"


@pytest.fixture
async def sbx() -> AsyncIterator[FakeSandbox]:
    box = FakeSandbox()
    yield box
    await box.destroy()


@pytest.fixture
def reg() -> ToolRegistry:
    return default_registry()


async def call(
    reg: ToolRegistry, box: FakeSandbox, name: str, ctx: ToolContext = CTX, **args: object
) -> ToolResult:
    return await reg.run(ToolCall(id="c", name=name, input=dict(args)), box, ctx)


# ---------- ranges and empty files ----------


async def test_read_file_rejects_an_inverted_range(sbx: FakeSandbox, reg: ToolRegistry) -> None:
    await sbx.write_file("a.py", b"1\n2\n3\n")
    r = await call(reg, sbx, "read_file", path="a.py", start_line=3, end_line=1)
    assert not r.ok
    assert "end_line 1 is before start_line 3" in r.output


async def test_read_file_rejects_a_start_past_the_end(sbx: FakeSandbox, reg: ToolRegistry) -> None:
    await sbx.write_file("a.py", b"1\n2\n3\n")
    r = await call(reg, sbx, "read_file", path="a.py", start_line=9)
    assert not r.ok
    assert "start_line 9 is beyond the end of the file (3 lines)" in r.output


async def test_an_end_past_the_end_is_cut_to_the_file(sbx: FakeSandbox, reg: ToolRegistry) -> None:
    # The model cannot know how long the file is, so asking for too much is not a mistake.
    await sbx.write_file("a.py", b"1\n2\n3\n")
    r = await call(reg, sbx, "read_file", path="a.py", start_line=2, end_line=99)
    assert r.ok
    assert r.output == "     2\t2\n     3\t3"


async def test_an_empty_file_says_so(sbx: FakeSandbox, reg: ToolRegistry) -> None:
    await sbx.write_file("empty.txt", b"")
    r = await call(reg, sbx, "read_file", path="empty.txt")
    assert r.ok and r.output == "(empty file)"
    past = await call(reg, sbx, "read_file", path="empty.txt", start_line=2)
    assert not past.ok and "beyond the end of the file" in past.output


async def test_an_integral_float_is_a_fine_line_number(sbx: FakeSandbox, reg: ToolRegistry) -> None:
    # Some servers send 2.0 for an integer. The schema accepts it, so the tool must too.
    await sbx.write_file("a.py", b"1\n2\n3\n")
    r = await call(reg, sbx, "read_file", path="a.py", start_line=1.0, end_line=2.0)
    assert r.ok, r.output
    assert r.output == "     1\t1\n     2\t2"
    # The same number reads as 1 in a message, not as 1.0.
    inverted = await call(reg, sbx, "read_file", path="a.py", start_line=3.0, end_line=1.0)
    assert not inverted.ok and "end_line 1 is before start_line 3" in inverted.output


# ---------- line numbers agree with the file ----------


async def test_read_file_numbers_lines_by_newlines_only(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    # Form feeds are common in GNU-style sources. rg, sed and tracebacks do not
    # break a line there, so neither may the numbers.
    await sbx.write_file("ff.txt", b"alpha\x0cbeta\ngamma\nlone\rcr\n")
    r = await call(reg, sbx, "read_file", path="ff.txt")
    assert r.ok
    assert r.output == "     1\talpha\x0cbeta\n     2\tgamma\n     3\tlone\rcr"
    only = await call(reg, sbx, "read_file", path="ff.txt", start_line=2, end_line=2)
    assert only.output == "     2\tgamma"


async def test_edit_diff_numbers_lines_by_newlines_only(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("ff.txt", b"a\x0cb\nc\n")
    r = await call(reg, sbx, "edit_file", path="ff.txt", old_str="c", new_str="C")
    assert r.ok, r.output
    assert "@@ -1,2 +1,2 @@" in r.output


async def test_read_file_shows_a_crlf_file_without_the_carriage_returns(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("win.txt", CRLF)
    r = await call(reg, sbx, "read_file", path="win.txt")
    assert r.output == "     1\talpha\n     2\tbeta\n     3\tgamma"


# ---------- editing a CRLF file ----------


async def test_a_multi_line_old_str_from_read_file_matches_a_crlf_file(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("win.txt", CRLF)
    r = await call(
        reg, sbx, "edit_file", path="win.txt", old_str="alpha\nbeta", new_str="ALPHA\nBETA\nextra"
    )
    assert r.ok, r.output
    assert await sbx.read_file("win.txt") == b"ALPHA\r\nBETA\r\nextra\r\ngamma\r\n"
    assert "CRLF" in r.output
    assert "\r" not in r.output


async def test_new_lines_in_a_crlf_file_get_crlf(sbx: FakeSandbox, reg: ToolRegistry) -> None:
    await sbx.write_file("win.txt", CRLF)
    r = await call(reg, sbx, "edit_file", path="win.txt", old_str="gamma", new_str="gamma\ndelta")
    assert r.ok, r.output
    assert await sbx.read_file("win.txt") == b"alpha\r\nbeta\r\ngamma\r\ndelta\r\n"


async def test_an_old_str_that_spells_out_the_carriage_return_still_matches(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("win.txt", CRLF)
    r = await call(reg, sbx, "edit_file", path="win.txt", old_str="alpha\r\nbeta", new_str="A\r\nB")
    assert r.ok, r.output
    assert await sbx.read_file("win.txt") == b"A\r\nB\r\ngamma\r\n"


async def test_an_lf_file_is_edited_as_it_is(sbx: FakeSandbox, reg: ToolRegistry) -> None:
    await sbx.write_file("unix.txt", b"alpha\nbeta\n")
    r = await call(reg, sbx, "edit_file", path="unix.txt", old_str="alpha\nbeta", new_str="A\nB")
    assert r.ok, r.output
    assert await sbx.read_file("unix.txt") == b"A\nB\n"
    assert "CRLF" not in r.output


async def test_a_crlf_stretch_in_a_mostly_lf_file_still_matches(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("mixed.txt", b"one\ntwo\r\nthree\nfour\n")
    r = await call(reg, sbx, "edit_file", path="mixed.txt", old_str="two\nthree", new_str="2\n3")
    assert r.ok, r.output
    assert await sbx.read_file("mixed.txt") == b"one\n2\r\n3\nfour\n"


async def test_an_ambiguous_old_str_in_a_crlf_file_is_still_refused(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("win.txt", b"a\r\nb\r\nx\r\na\r\nb\r\n")
    r = await call(reg, sbx, "edit_file", path="win.txt", old_str="a\nb", new_str="c")
    assert not r.ok and "found 2 matches" in r.output


# ---------- exactly one match, and a real change ----------


@pytest.mark.parametrize(
    ("text", "old", "matches"),
    [("ababa\n", "aba", 2), ("aaa\n", "aa", 2), ("}}}\n", "}}", 2), ("aaaa\n", "aa", 3)],
)
async def test_overlapping_matches_are_counted(
    sbx: FakeSandbox, reg: ToolRegistry, text: str, old: str, matches: int
) -> None:
    await sbx.write_file("f.txt", text.encode())
    r = await call(reg, sbx, "edit_file", path="f.txt", old_str=old, new_str="X")
    assert not r.ok
    assert f"found {matches} matches" in r.output
    assert await sbx.read_file("f.txt") == text.encode()


async def test_an_edit_that_changes_nothing_is_refused(sbx: FakeSandbox, reg: ToolRegistry) -> None:
    await sbx.write_file("f.txt", b"one\n")
    r = await call(reg, sbx, "edit_file", path="f.txt", old_str="one", new_str="one")
    assert not r.ok
    assert "identical" in r.output
    assert await sbx.read_file("f.txt") == b"one\n"


# ---------- line numbers pasted from read_file ----------


async def test_pasted_line_numbers_get_a_specific_error(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("a.py", b"def add(a, b):\n    return a - b\n")
    shown = (await call(reg, sbx, "read_file", path="a.py")).output
    r = await call(reg, sbx, "edit_file", path="a.py", old_str=shown, new_str="x")
    assert not r.ok
    assert "line numbers" in r.output and "not part of the file" in r.output
    assert "found 0 matches" not in r.output


async def test_text_that_looks_numbered_is_fine_when_the_file_has_it(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("t.tsv", b"1\talpha\n2\tbeta\n")
    r = await call(reg, sbx, "edit_file", path="t.tsv", old_str="1\talpha\n2\tbeta", new_str="x")
    assert r.ok, r.output
    assert await sbx.read_file("t.tsv") == b"x\n"


async def test_numbered_looking_text_that_matches_twice_is_reported_as_ambiguous(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    # The numbers hint is for text that matches nothing. Here the file really has it, twice.
    await sbx.write_file("t.tsv", b"1\talpha\nmid\n1\talpha\n")
    r = await call(reg, sbx, "edit_file", path="t.tsv", old_str="1\talpha", new_str="x")
    assert not r.ok and "found 2 matches" in r.output
    assert "line numbers" not in r.output


def test_edit_file_says_in_its_description_that_line_numbers_are_not_content() -> None:
    description = EditFile.spec.description
    assert "line number" in description and "read_file" in description
    props = EditFile.spec.input_schema["properties"]
    assert "line number" in props["old_str"]["description"]


# ---------- files that are not text ----------


async def test_a_file_that_is_not_text_is_reported_by_name(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("data.bin", b"\xff\xfe\x00\x01")
    for name, extra in (("read_file", {}), ("edit_file", {"old_str": "a", "new_str": "b"})):
        r = await call(reg, sbx, name, path="data.bin", **extra)
        assert not r.ok and r.output == "not a UTF-8 text file: data.bin", (name, r.output)
    assert await sbx.read_file("data.bin") == b"\xff\xfe\x00\x01"


# ---------- text that cannot be written ----------


async def test_text_that_is_not_valid_unicode_is_a_result(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    # A lone surrogate can come out of a garbled reply. It cannot be written as UTF-8.
    bad = "oops \ud800 oops"
    made = await call(reg, sbx, "write_file", path="a.txt", content=bad)
    assert not made.ok and "UTF-8" in made.output
    await sbx.write_file("b.txt", b"one\n")
    edit = await call(reg, sbx, "edit_file", path="b.txt", old_str="one", new_str=bad)
    assert not edit.ok and "UTF-8" in edit.output
    assert await sbx.read_file("b.txt") == b"one\n"


# ---------- the page budget ----------

BUDGET = int(CTX.output_max_tokens * 3.5)


def page_numbers(output: str) -> list[int]:
    return [int(m[1]) for ln in output.splitlines() if (m := re.match(r"\s*(\d+)\t", ln))]


async def test_a_page_stops_at_the_output_budget_and_says_where_to_go_on(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file(
        "big.py", "".join(f"line {i:04d} {'x' * 30}\n" for i in range(1, 401)).encode()
    )
    first = await call(reg, sbx, "read_file", path="big.py")
    assert first.ok
    assert len(first.output) <= BUDGET
    footer = first.output.splitlines()[-1]
    m = re.fullmatch(
        r"\[showing lines 1-(\d+) of 400; call again with start_line=(\d+) to continue\]", footer
    )
    assert m is not None, footer
    last_shown = int(m[1])
    assert int(m[2]) == last_shown + 1
    assert 200 < last_shown < 400
    assert page_numbers(first.output) == list(range(1, last_shown + 1))


async def test_paging_from_the_footer_reads_every_line_once(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file(
        "big.py", "".join(f"line {i:04d} {'x' * 30}\n" for i in range(1, 401)).encode()
    )
    seen: list[int] = []
    start = 1
    for _ in range(10):
        r = await call(reg, sbx, "read_file", path="big.py", start_line=start)
        assert r.ok and len(r.output) <= BUDGET
        seen += page_numbers(r.output)
        footer = re.search(r"start_line=(\d+) to continue\]$", r.output)
        if footer is None:
            break
        start = int(footer[1])
    assert seen == list(range(1, 401))


async def test_the_budget_comes_from_the_context(sbx: FakeSandbox, reg: ToolRegistry) -> None:
    await sbx.write_file(
        "big.py", "".join(f"line {i:04d} {'x' * 30}\n" for i in range(1, 401)).encode()
    )
    small = ToolContext(task_id="t", turn_index=0, blob_prefix="p", output_max_tokens=300)
    big = ToolContext(task_id="t", turn_index=0, blob_prefix="p", output_max_tokens=8000)
    few = await call(reg, sbx, "read_file", ctx=small, path="big.py")
    many = await call(reg, sbx, "read_file", ctx=big, path="big.py")
    assert len(few.output) <= int(300 * 3.5)
    assert 0 < len(page_numbers(few.output)) < 400
    assert page_numbers(many.output) == list(range(1, 401))
    assert "continue" not in many.output


async def test_a_line_longer_than_the_budget_is_cut_and_the_next_one_is_reachable(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("min.js", b"x" * 100_000 + b"\nnext\n")
    r = await call(reg, sbx, "read_file", path="min.js")
    assert r.ok
    assert len(r.output) <= BUDGET
    assert "[line cut: " in r.output
    assert "start_line=2" in r.output
    rest = await call(reg, sbx, "read_file", path="min.js", start_line=2)
    assert rest.output == "     2\tnext"
