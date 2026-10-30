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
    await sbx.exec("ln -s /etc /workspace/link", timeout_s=10)
    r = await call(reg, sbx, "read_file", path="link/passwd")
    assert not r.ok and "outside" in r.output
    w = await call(reg, sbx, "write_file", path="link/evil", content="x")
    assert not w.ok


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
