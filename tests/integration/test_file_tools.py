import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from longhaul.contracts import Sandbox, SandboxLimits, ToolCall, ToolContext, ToolResult
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.tools.registry import ToolRegistry, default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase1]

IMAGE = "longhaul-sandbox:py312"
CALC = Path(__file__).parent.parent / "fixtures" / "repos" / "calc"
CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")
MODULE = "src/calc/__init__.py"


@pytest.fixture
async def sbx() -> AsyncIterator[Sandbox]:
    manager = DockerSandboxManager()
    tid = f"t{uuid.uuid4().hex[:10]}"
    try:
        box = await manager.create(tid, IMAGE, SandboxLimits(), network=False)
        for f in CALC.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                await box.write_file(f"/workspace/{f.relative_to(CALC)}", f.read_bytes())
        yield box
    finally:
        await manager.cleanup_task(tid)


@pytest.fixture
def reg() -> ToolRegistry:
    return default_registry()


async def call(reg: ToolRegistry, sbx: Sandbox, name: str, **kw: object) -> ToolResult:
    return await reg.run(ToolCall(id="c", name=name, input=dict(kw)), sbx, CTX)


async def sh(sbx: Sandbox, cmd: str) -> str:
    """Run a setup command and fail the test if it does not work."""
    done = await sbx.exec(cmd, timeout_s=30)
    assert done.exit_code == 0, f"{cmd}: {done.stderr}"
    return done.stdout


async def test_edit_exactly_once(reg: ToolRegistry, sbx: Sandbox) -> None:
    r = await call(
        reg, sbx, "edit_file", path=MODULE, old_str="    return a - b", new_str="    return a + b"
    )
    assert r.ok, r.output
    assert "-    return a - b" in r.output and "+    return a + b" in r.output
    assert b"return a + b" in await sbx.read_file(f"/workspace/{MODULE}")


async def test_edit_multiple_matches_rejected(reg: ToolRegistry, sbx: Sandbox) -> None:
    before = await sbx.read_file(f"/workspace/{MODULE}")
    r = await call(reg, sbx, "edit_file", path=MODULE, old_str="return", new_str="yield")
    assert not r.ok and "found 2 matches" in r.output
    r0 = await call(reg, sbx, "edit_file", path=MODULE, old_str="no such text", new_str="x")
    assert not r0.ok and "found 0 matches" in r0.output
    assert await sbx.read_file(f"/workspace/{MODULE}") == before


async def test_path_escape_rejected(reg: ToolRegistry, sbx: Sandbox) -> None:
    for p in ("../../etc/passwd", "/etc/passwd"):
        r = await call(reg, sbx, "read_file", path=p)
        assert not r.ok, p
    await sh(sbx, "ln -s /etc /workspace/link")
    r = await call(reg, sbx, "read_file", path="link/passwd")
    assert not r.ok and "outside" in r.output
    w = await call(reg, sbx, "write_file", path="link/evil", content="x")
    assert not w.ok


ESCAPES = [
    "../../etc/passwd",
    "/etc/passwd",
    "link",
    "link/passwd",
    "link/new/file.txt",
    "filelink",
    "dangling_out",
]
FILLERS = {"pattern": "root", "old_str": "root", "new_str": "toor", "content": "pwned\n"}


async def test_path_escape_rejected_for_every_tool_with_a_path(
    reg: ToolRegistry, sbx: Sandbox
) -> None:
    await sh(
        sbx,
        "ln -s /etc link && ln -s /etc/passwd filelink && ln -s /etc/not-there dangling_out",
    )
    names = [s.name for s in reg.specs() if "path" in s.input_schema.get("properties", {})]
    assert {"list_dir", "read_file", "edit_file", "write_file", "search"} <= set(names)
    for name in names:
        tool = reg.get(name)
        assert tool is not None
        required = [k for k in tool.spec.input_schema.get("required", []) if k != "path"]
        for path in ESCAPES:
            args = {"path": path, **{key: FILLERS[key] for key in required}}
            r = await call(reg, sbx, name, **args)
            assert not r.ok, (name, path, r.output)
            assert "outside" in r.output, (name, path, r.output)


async def test_dash_leading_directory_cannot_inject_a_find_option(
    reg: ToolRegistry, sbx: Sandbox
) -> None:
    # list_dir used to run `find -L ...` for ./-L. GNU find read the dash as an option,
    # walked "." and followed the link, so the listing showed the sandbox's /etc.
    await sh(sbx, "ln -s /etc outlink && mkdir -p -- ./-L/inner && touch ./-L/inner/f")
    r = await call(reg, sbx, "list_dir", path="./-L")
    assert r.ok, r.output
    assert r.output == "inner/\n  f", r.output


async def test_links_inside_the_workspace_reach_the_real_file(
    reg: ToolRegistry, sbx: Sandbox
) -> None:
    # get_archive does not follow a link in the last path part and put_archive replaces a
    # linked directory, so the tools have to hand the sandbox the resolved path.
    await sh(sbx, "ln -s src/calc/__init__.py alias.py && ln -s src srclink")
    read = await call(reg, sbx, "read_file", path="alias.py")
    assert read.ok, read.output
    assert "return a - b" in read.output
    listing = await call(reg, sbx, "list_dir", path="srclink")
    assert listing.ok and "calc/" in listing.output, listing.output
    edit = await call(
        reg,
        sbx,
        "edit_file",
        path="srclink/calc/__init__.py",
        old_str="    return a - b",
        new_str="    return a + b",
    )
    assert edit.ok, edit.output
    made = await call(reg, sbx, "write_file", path="srclink/calc/extra.py", content="X = 1\n")
    assert made.ok, made.output
    assert b"return a + b" in await sbx.read_file(f"/workspace/{MODULE}")
    assert await sbx.read_file("/workspace/src/calc/extra.py") == b"X = 1\n"
    await sh(sbx, "test -L alias.py && test -L srclink")


async def test_wrong_kind_of_path_is_explained(reg: ToolRegistry, sbx: Sandbox) -> None:
    for name, extra in (
        ("read_file", {}),
        ("edit_file", {"old_str": "a", "new_str": "b"}),
        ("write_file", {"content": "x"}),
    ):
        r = await call(reg, sbx, name, path="src", **extra)
        assert not r.ok and "src is a directory" in r.output, (name, r.output)
    assert "list_dir" in (await call(reg, sbx, "read_file", path="src")).output
    listing = await call(reg, sbx, "list_dir", path=MODULE)
    assert not listing.ok
    assert f"{MODULE} is a file" in listing.output and "read_file" in listing.output
    # Writing onto the directory must not have replaced it.
    await sh(sbx, f"test -d src && test -f {MODULE}")


async def test_read_line_range(reg: ToolRegistry, sbx: Sandbox) -> None:
    r = await call(reg, sbx, "read_file", path=MODULE, start_line=4, end_line=5)
    assert r.ok, r.output
    lines = r.output.splitlines()
    assert "def add" in lines[0] and lines[0].lstrip().startswith("4")
    assert len(lines) == 2


async def test_search_finds_symbol(reg: ToolRegistry, sbx: Sandbox) -> None:
    r = await call(reg, sbx, "search", pattern="def add")
    assert r.ok, r.output
    assert "src/calc/__init__.py:4: def add" in r.output
    none = await call(reg, sbx, "search", pattern="zzz_not_here")
    assert none.ok and "no matches" in none.output


async def test_list_dir_ignores_noise(reg: ToolRegistry, sbx: Sandbox) -> None:
    await sbx.exec(
        "mkdir -p node_modules/x .git/objects src/calc/__pycache__ && touch node_modules/x/a.js",
        timeout_s=10,
    )
    r = await call(reg, sbx, "list_dir")
    assert r.ok, r.output
    for noise in ("node_modules", ".git", "__pycache__"):
        assert noise not in r.output
    assert "calc/" in r.output and "pyproject.toml" in r.output
