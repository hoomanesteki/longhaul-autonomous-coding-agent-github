"""list_dir: what it hides, how deep it goes and where it stops.

Each case puts noise where a listing would reach it. A __pycache__ below the depth
that is listed proves nothing, so the noise sits at every level the calls look at.
"""

from collections.abc import AsyncIterator

import pytest

from longhaul.contracts import ExecResult, ToolCall, ToolContext, ToolResult
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import ToolRegistry, default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")

# The plan names these four. They are written out here and not read from the tool.
NOISE = (".git", "__pycache__", ".venv", "node_modules")


@pytest.fixture
async def sbx() -> AsyncIterator[FakeSandbox]:
    box = FakeSandbox()
    yield box
    await box.destroy()


@pytest.fixture
def reg() -> ToolRegistry:
    return default_registry()


async def list_dir(reg: ToolRegistry, box: FakeSandbox, **args: object) -> ToolResult:
    return await reg.run(ToolCall(id="c", name="list_dir", input=dict(args)), box, CTX)


def entries(output: str) -> set[str]:
    """The names in a listing, without the indent and the slash of a directory."""
    return {line.strip().rstrip("/") for line in output.splitlines()}


# ---------- noise ----------


@pytest.mark.parametrize("noise", NOISE)
async def test_noise_is_hidden_at_every_level(
    noise: str, sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("keep.py", b"")
    await sbx.write_file("src/pkg/real.py", b"")
    for where in (noise, f"src/{noise}", f"src/pkg/{noise}"):
        await sbx.write_file(f"{where}/inside.txt", b"")
        await sbx.write_file(f"{where}/sub/deeper.txt", b"")
    hidden = {noise, "inside.txt", "sub", "deeper.txt"}
    for path, depth in ((".", 1), (".", 2), (".", 6), ("src", 1), ("src", 3), ("src/pkg", 1)):
        r = await list_dir(reg, sbx, path=path, depth=depth)
        assert r.ok, r.output
        assert not entries(r.output) & hidden, (path, depth, r.output)
    # The calls above can only pass for the right reason when real files are listed.
    top = await list_dir(reg, sbx, depth=6)
    assert {"keep.py", "src", "pkg", "real.py"} <= entries(top.output)
    below = await list_dir(reg, sbx, path="src/pkg")
    assert entries(below.output) == {"real.py"}


async def test_names_that_only_look_like_noise_are_listed(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    lookalikes = ["my_node_modules", "venv", ".github", "git", "__pycache__.txt", ".venv.md"]
    for name in lookalikes:
        await sbx.write_file(f"{name}/f.txt", b"")
    r = await list_dir(reg, sbx)
    assert set(lookalikes) <= entries(r.output)


# ---------- depth ----------


async def test_depth_decides_how_far_down_the_listing_goes(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("l1/l2/l3/l4/l5/l6/l7/bottom.txt", b"")
    for depth in range(1, 7):
        names = entries((await list_dir(reg, sbx, depth=depth)).output)
        assert f"l{depth}" in names, depth
        assert f"l{depth + 1}" not in names, depth
    # Two levels are listed when depth is left out.
    default = entries((await list_dir(reg, sbx)).output)
    assert "l2" in default and "l3" not in default


async def test_depth_counts_from_the_directory_that_was_asked_for(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("a/b/c/d/e.txt", b"")
    names = entries((await list_dir(reg, sbx, path="a/b", depth=1)).output)
    assert names == {"c"}
    names = entries((await list_dir(reg, sbx, path="a/b", depth=2)).output)
    assert names == {"c", "d"}


# ---------- the entry limit ----------


async def fill(box: FakeSandbox, count: int) -> None:
    for i in range(count):
        await box.write_file(f"many/f{i:03d}.txt", b"")


async def test_a_listing_stops_at_400_entries_and_says_how_many_are_left(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await fill(sbx, 450)
    lines = (await list_dir(reg, sbx, path="many")).output.splitlines()
    assert len(lines) == 401
    assert lines[0] == "f000.txt" and lines[399] == "f399.txt"
    assert lines[-1] == "... 50 more entries not shown"


async def test_a_listing_of_exactly_400_entries_is_not_cut(
    sbx: FakeSandbox, reg: ToolRegistry
) -> None:
    await fill(sbx, 400)
    lines = (await list_dir(reg, sbx, path="many")).output.splitlines()
    assert len(lines) == 400
    assert lines[-1] == "f399.txt"


async def test_one_entry_over_the_limit_is_counted(sbx: FakeSandbox, reg: ToolRegistry) -> None:
    await fill(sbx, 401)
    lines = (await list_dir(reg, sbx, path="many")).output.splitlines()
    assert len(lines) == 401
    assert lines[-1] == "... 1 more entries not shown"


async def test_an_empty_directory_says_so(sbx: FakeSandbox, reg: ToolRegistry) -> None:
    await sbx.exec("mkdir empty", timeout_s=10)
    r = await list_dir(reg, sbx, path="empty")
    assert r.ok and r.output == "(empty)"


async def test_a_listing_that_fails_says_why(reg: ToolRegistry) -> None:
    class Unreadable(FakeSandbox):
        async def exec(
            self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
        ) -> ExecResult:
            if cmd.startswith("cd -- "):
                return ExecResult(
                    exit_code=1,
                    stdout="",
                    stderr="bash: cd: locked: Permission denied\n",
                    timed_out=False,
                    duration_ms=1,
                )
            return await super().exec(cmd, timeout_s=timeout_s, env=env)

    box = Unreadable()
    try:
        await box.write_file("locked/f.txt", b"")
        r = await list_dir(reg, box, path="locked")
        assert not r.ok
        assert r.output == "list_dir failed: bash: cd: locked: Permission denied"
    finally:
        await box.destroy()
