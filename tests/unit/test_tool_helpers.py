import json

import pytest

from longhaul.contracts import ToolContext
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools import ToolError
from longhaul.tools.files import (
    EditFile,
    ListDir,
    ReadFile,
    WriteFile,
    find_all,
    format_diff,
    looks_numbered,
    number_lines,
    plan_edit,
    render_tree,
    split_lines,
    to_crlf,
    uses_crlf,
)
from longhaul.tools.paths import PathError, check_resolved, normalize
from longhaul.tools.registry import default_registry
from longhaul.tools.search import format_matches, parse_rg_json

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="p")


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (".", "."),
        ("", "."),
        ("a/b.py", "a/b.py"),
        ("./a//b.py", "a/b.py"),
        ("/workspace", "."),
        ("/workspace/src/x.py", "src/x.py"),
        ("a/../b", "b"),
    ],
)
def test_normalize_ok(raw: str, want: str) -> None:
    assert normalize(raw) == want


@pytest.mark.parametrize(
    "raw", ["../../etc/passwd", "/etc/passwd", "a/../../x", "/workspace/../etc"]
)
def test_normalize_rejects(raw: str) -> None:
    with pytest.raises(PathError):
        normalize(raw)


def test_check_resolved() -> None:
    check_resolved("/workspace/a", "/workspace")
    check_resolved("/workspace", "/workspace")
    with pytest.raises(PathError):
        check_resolved("/etc", "/workspace")
    with pytest.raises(PathError):
        check_resolved("/workspace-evil/x", "/workspace")


def test_format_diff() -> None:
    d = format_diff("a.py", "x\ny\nz\n", "x\nY\nz\n")
    assert "--- a/a.py" in d and "+++ b/a.py" in d
    assert "-y" in d and "+Y" in d


def test_format_diff_truncates() -> None:
    old = "".join(f"{i}\n" for i in range(500))
    new = "".join(f"{i}x\n" for i in range(500))
    assert "diff truncated" in format_diff("f", old, new)


def test_number_lines_and_paging() -> None:
    text = "".join(f"l{i}\n" for i in range(1, 11))
    out = number_lines(text, 3, 5)
    assert out.splitlines()[0].endswith("l3") and "l5" in out and "l6" not in out
    big = "".join(f"l{i}\n" for i in range(1, 1001))
    out = number_lines(big, 1, None)
    assert "l800" in out and "l801" not in out
    assert "start_line=801" in out


def test_number_lines_out_of_range() -> None:
    # An unreadable range is an error the caller reports, not a sentence in the output.
    with pytest.raises(ToolError, match=r"start_line 5 is beyond the end of the file \(1 line\)"):
        number_lines("a\n", 5, None)
    with pytest.raises(ToolError, match=r"\(3 lines\)"):
        number_lines("a\nb\nc\n", 4, None)


def test_number_lines_rejects_an_inverted_range() -> None:
    with pytest.raises(ToolError, match="end_line 1 is before start_line 3"):
        number_lines("a\nb\nc\n", 3, 1)


def test_number_lines_of_an_empty_file() -> None:
    assert number_lines("", 1, None) == "(empty file)"
    with pytest.raises(ToolError, match="it is empty"):
        number_lines("", 2, None)


def test_number_lines_cuts_an_end_line_that_is_past_the_end() -> None:
    assert number_lines("a\nb\n", 2, 50) == "     2\tb"


def test_number_lines_stops_at_the_character_budget() -> None:
    text = "".join(f"{'x' * 40}\n" for _ in range(100))
    out = number_lines(text, 1, None, max_chars=1000)
    rows = out.splitlines()
    assert len(out) <= 1000
    # 47 characters and a newline per row, after 120 are kept back for the footer.
    assert rows[-1] == "[showing lines 1-18 of 100; call again with start_line=19 to continue]"
    assert len(rows) == 19
    rest = number_lines(text, 19, None, max_chars=1000)
    assert rest.startswith("    19\t")


def test_a_page_always_has_a_line_even_when_the_budget_is_tiny() -> None:
    out = number_lines("a\nb\nc\n", 1, None, max_chars=10)
    assert out.splitlines()[0] == "     1\ta"


def test_number_lines_cuts_a_line_that_alone_exceeds_the_budget() -> None:
    out = number_lines("x" * 5000 + "\nnext\n", 1, None, max_chars=1000)
    first = out.splitlines()[0]
    assert len(first) < 1000 and first.endswith(" more characters]")
    assert "[line cut: " in first
    assert out.splitlines()[-1].endswith("start_line=2 to continue]")


def test_split_lines_breaks_on_newlines_only() -> None:
    assert split_lines("") == []
    assert split_lines("a") == ["a"]
    assert split_lines("a\n") == ["a"]
    assert split_lines("a\n\n") == ["a", ""]
    assert split_lines("\n") == [""]
    # Form feed, vertical tab, NEL, line and paragraph separators, a lone carriage return.
    odd = "a\x0cb\x0bc\x85d e f\rg"
    assert split_lines(odd + "\nnext\n") == [odd, "next"]
    assert split_lines("a\r\nb\r\n") == ["a", "b"]
    assert split_lines("a\r\nb") == ["a", "b"]


def test_find_all_counts_overlapping_matches() -> None:
    assert find_all("aaa", "aa") == [0, 1]
    assert find_all("ababa", "aba") == [0, 2]
    assert find_all("abc", "d") == []
    assert find_all("a.b.c", ".") == [1, 3]
    assert find_all("abab", "ab") == [0, 2]


def test_line_ending_helpers() -> None:
    assert to_crlf("a\nb\r\nc") == "a\r\nb\r\nc"
    assert to_crlf("no break") == "no break"
    assert uses_crlf("a\r\nb\r\n") and not uses_crlf("a\nb\n")
    assert not uses_crlf("") and not uses_crlf("single line")
    assert uses_crlf("a\r\nb\r\nc\n") and not uses_crlf("a\r\nb\nc\n")


def test_looks_numbered() -> None:
    assert looks_numbered("     4\tdef add():\n     5\t    return 1")
    assert looks_numbered("4\tx\n\n5\ty")
    assert not looks_numbered("4\tx\nplain")
    assert not looks_numbered("")
    assert not looks_numbered("   \n")
    assert not looks_numbered("4 spaces instead of a tab")


def test_plan_edit_finds_one_place_and_keeps_the_rest_of_the_file() -> None:
    edit = plan_edit("f.txt", "one two three", "two", "2")
    assert edit.apply("one two three") == "one 2 three"
    assert edit.note is None


def test_plan_edit_refuses_none_and_many() -> None:
    with pytest.raises(ToolError, match="found 0 matches for old_str in f.txt"):
        plan_edit("f.txt", "abc", "x", "y")
    with pytest.raises(ToolError, match="found 2 matches.*add more context"):
        plan_edit("f.txt", "abab", "ab", "y")
    with pytest.raises(ToolError, match="found 3 matches"):
        plan_edit("f.txt", "aaaa", "aa", "y")


def test_plan_edit_uses_the_line_endings_of_the_file() -> None:
    text = "a\r\nb\r\nc\r\n"
    edit = plan_edit("f.txt", text, "a\nb", "A\nB")
    assert edit.apply(text) == "A\r\nB\r\nc\r\n"
    assert edit.note is not None and "CRLF" in edit.note
    # Nothing to convert, nothing to say.
    quiet = plan_edit("f.txt", text, "c", "C")
    assert quiet.apply(text) == "a\r\nb\r\nC\r\n" and quiet.note is None


def test_render_tree_caps() -> None:
    paths = [f"d/f{i}" for i in range(500)]
    lines = render_tree(paths).splitlines()
    # 400 entries and the marker. A limit of 399 or 401 changes this count.
    assert len(lines) == 401
    assert lines[399] == "  f399"
    assert lines[-1] == "... 100 more entries not shown"


def test_render_tree_does_not_cut_at_the_limit() -> None:
    paths = [f"f{i:03d}" for i in range(401)]
    exactly = render_tree(paths[:400]).splitlines()
    assert len(exactly) == 400 and exactly[-1] == "f399"
    assert render_tree(paths).splitlines()[-1] == "... 1 more entries not shown"
    assert render_tree([]) == "(empty)"


def test_parse_rg_json() -> None:
    match = {
        "type": "match",
        "data": {
            "path": {"text": "src/calc/__init__.py"},
            "lines": {"text": "def add(a, b):\n"},
            "line_number": 4,
        },
    }
    raw = "\n".join([json.dumps({"type": "begin", "data": {}}), json.dumps(match), "not json"])
    ms = parse_rg_json(raw)
    assert ms == [("src/calc/__init__.py", 4, "def add(a, b):")]
    assert format_matches(ms, 200) == "src/calc/__init__.py:4: def add(a, b):"
    assert "more matches" in format_matches(ms * 3, 2)


async def test_file_tools_on_fake_sandbox() -> None:
    sbx = FakeSandbox()
    try:
        await sbx.write_file("pkg/a.py", b"x = 1\n")
        await sbx.write_file("node_modules/z.js", b"")
        r = await ListDir().run(sbx, {}, CTX)
        assert r.ok and "pkg/" in r.output and "a.py" in r.output
        assert "node_modules" not in r.output
        w = await WriteFile().run(sbx, {"path": "n/b.txt", "content": "hi\n"}, CTX)
        assert w.ok
        e = await EditFile().run(sbx, {"path": "n/b.txt", "old_str": "hi", "new_str": "yo"}, CTX)
        assert e.ok and "+yo" in e.output
        rd = await ReadFile().run(sbx, {"path": "n/b.txt"}, CTX)
        assert "yo" in rd.output
        bad = await ReadFile().run(sbx, {"path": "../../etc/passwd"}, CTX)
        assert not bad.ok
        missing = await ReadFile().run(sbx, {"path": "nope.txt"}, CTX)
        assert not missing.ok and "not found" in missing.output
    finally:
        await sbx.destroy()


def test_default_registry_names() -> None:
    names = [s.name for s in default_registry().specs()]
    assert names == [
        "list_dir",
        "read_file",
        "edit_file",
        "write_file",
        "search",
        "run_command",
        "run_tests",
        "git_diff",
        "finish",
    ]
