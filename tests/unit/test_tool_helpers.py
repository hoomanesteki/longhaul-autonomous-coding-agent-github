import json

import pytest

from longhaul.contracts import ToolContext
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.files import (
    EditFile,
    ListDir,
    ReadFile,
    WriteFile,
    format_diff,
    number_lines,
    render_tree,
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
    assert "beyond" in number_lines("a\n", 5, None)


def test_render_tree_caps() -> None:
    paths = [f"d/f{i}" for i in range(500)]
    out = render_tree(paths)
    assert "more entries" in out
    assert len(out.splitlines()) <= 402


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
