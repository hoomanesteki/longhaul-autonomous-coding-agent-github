import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from longhaul.contracts import Sandbox, SandboxLimits, ToolCall, ToolContext, ToolResult
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.tools.git import NO_CHANGES
from longhaul.tools.registry import ToolRegistry, default_registry

# One sandbox serves the whole module because building the venv is slow. Every
# test starts from a reset tree, so they do not depend on each other.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.phase1,
    pytest.mark.asyncio(loop_scope="module"),
]

IMAGE = "longhaul-sandbox:py312"
CALC = Path(__file__).parent.parent / "fixtures" / "repos" / "calc"
CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")
MODULE = "src/calc/__init__.py"
TEST_COMMAND = "pytest -q"
# The agent never sees the reference fix or build debris from running the fixture locally.
SKIP_PARTS = {"REFERENCE.patch", "__pycache__", ".pytest_cache"}


def fixture_files() -> list[Path]:
    return [
        f
        for f in sorted(CALC.rglob("*"))
        if f.is_file()
        and not SKIP_PARTS & set(f.relative_to(CALC).parts)
        and not any(p.endswith(".egg-info") for p in f.relative_to(CALC).parts)
    ]


async def prepare_workspace(box: Sandbox) -> None:
    """Copy calc in, commit it and tag `base`, as workspace preparation does."""
    for f in fixture_files():
        await box.write_file(f"/workspace/{f.relative_to(CALC)}", f.read_bytes())
    r = await box.exec(
        "git init -q && git add -A && git commit -q -m initial && git tag base", timeout_s=60
    )
    assert r.exit_code == 0, r.stderr


async def install_dependencies(box: Sandbox) -> None:
    """Build /workspace/.venv with pytest and calc. This is the only step that needs the network."""
    r = await box.exec(
        "python -m venv .venv && "
        ".venv/bin/pip install --disable-pip-version-check --no-cache-dir -q -e '.[dev]'",
        timeout_s=300,
    )
    assert r.exit_code == 0, (
        "could not build the test venv; this test needs outbound network to reach PyPI:\n"
        + r.stderr[-1500:]
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def box() -> AsyncIterator[Sandbox]:
    manager = DockerSandboxManager()
    tid = f"t{uuid.uuid4().hex[:10]}"
    try:
        sandbox = await manager.create(tid, IMAGE, SandboxLimits(), network=True)
        await prepare_workspace(sandbox)
        await install_dependencies(sandbox)
        await manager.set_network(sandbox.id, False)
        yield sandbox
    finally:
        await manager.cleanup_task(tid)


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def fresh_tree(box: Sandbox) -> None:
    # Same idea as the checkpoint restore: back to base, but keep the venv and
    # the editable-install metadata that live in the workspace.
    r = await box.exec(
        "git reset -q --hard refs/tags/base && git clean -fdq -e .venv -e '*.egg-info'",
        timeout_s=60,
    )
    assert r.exit_code == 0, r.stderr


@pytest.fixture
def reg() -> ToolRegistry:
    return default_registry(test_command=TEST_COMMAND)


async def call(reg: ToolRegistry, box: Sandbox, name: str, **kw: object) -> ToolResult:
    return await reg.run(ToolCall(id="c", name=name, input=dict(kw)), box, CTX)


async def test_network_is_cut_before_the_tools_run(box: Sandbox) -> None:
    r = await box.exec(
        "python -c 'import urllib.request as u; u.urlopen(\"https://pypi.org\", timeout=5)'",
        timeout_s=30,
    )
    assert r.exit_code != 0


async def test_venv_tools_come_first_on_path(reg: ToolRegistry, box: Sandbox) -> None:
    r = await call(reg, box, "run_command", command="command -v pytest python")
    assert r.ok, r.output
    assert "/workspace/.venv/bin/pytest" in r.output
    assert "/workspace/.venv/bin/python" in r.output


async def test_run_tests_summary(reg: ToolRegistry, box: Sandbox) -> None:
    r = await call(reg, box, "run_tests")
    assert r.ok, r.output
    assert r.exit_code == 1
    assert "summary: 1 failed, 2 passed" in r.output
    assert "test_add_positive_numbers" in r.output


async def test_run_tests_with_a_target(reg: ToolRegistry, box: Sandbox) -> None:
    r = await call(reg, box, "run_tests", target="tests/test_calc.py::test_multiply")
    assert r.ok, r.output
    assert r.exit_code == 0
    assert "summary: 1 passed" in r.output


async def test_run_tests_passes_after_the_fix(reg: ToolRegistry, box: Sandbox) -> None:
    fix = await call(
        reg, box, "edit_file", path=MODULE, old_str="    return a - b", new_str="    return a + b"
    )
    assert fix.ok, fix.output
    r = await call(reg, box, "run_tests")
    assert r.ok and r.exit_code == 0
    assert "summary: 3 passed" in r.output


async def test_output_cap_marker(reg: ToolRegistry, box: Sandbox) -> None:
    # 50,000 characters out, 25,000 on stderr. Letters that appear nowhere else in
    # the result make the head, the cut and the tail easy to tell apart.
    script = (
        "import sys; "
        "sys.stdout.write('H' * 8000 + 'M' * 30000 + 'T' * 12000); "
        "sys.stderr.write('E' * 25000)"
    )
    r = await call(reg, box, "run_command", command=f'python -c "{script}"')
    assert r.ok, r.output
    assert r.exit_code == 0
    assert "[output truncated: 30000 characters cut]" in r.output
    assert "[output truncated: 5000 characters cut]" in r.output
    assert "H" * 8000 in r.output and "H" * 8001 not in r.output
    assert "T" * 12000 in r.output and "T" * 12001 not in r.output
    assert "M" not in r.output
    assert "E" * 8000 in r.output and "E" * 12001 not in r.output
    assert len(r.output) < 2 * 20_000 + 500


async def test_short_output_is_left_alone(reg: ToolRegistry, box: Sandbox) -> None:
    r = await call(reg, box, "run_command", command="echo hello; echo problem >&2; exit 4")
    assert r.ok
    assert r.exit_code == 4
    assert r.output.splitlines() == ["exit code: 4", "stdout:", "hello", "stderr:", "problem"]


async def test_git_diff_after_edit(reg: ToolRegistry, box: Sandbox) -> None:
    # Run the tests first so the tree holds the usual debris: __pycache__, the
    # venv and the egg-info directory. None of it belongs in the diff.
    await call(reg, box, "run_tests")
    at_base = await call(reg, box, "git_diff")
    assert at_base.ok, at_base.output
    assert at_base.output == NO_CHANGES

    edit = await call(
        reg, box, "edit_file", path=MODULE, old_str="    return a - b", new_str="    return a + b"
    )
    assert edit.ok, edit.output
    after = await call(reg, box, "git_diff")
    assert after.ok, after.output
    assert "diff --git a/src/calc/__init__.py b/src/calc/__init__.py" in after.output
    assert "-    return a - b" in after.output and "+    return a + b" in after.output
    assert "__pycache__" not in after.output and ".venv" not in after.output

    # Nothing is staged yet. Once it is, the staged view shows the same change.
    assert (await call(reg, box, "git_diff", staged=True)).output == NO_CHANGES
    await box.exec(f"git add {MODULE}", timeout_s=30)
    staged = await call(reg, box, "git_diff", staged=True)
    assert "+    return a + b" in staged.output


async def test_git_diff_shows_a_new_file(reg: ToolRegistry, box: Sandbox) -> None:
    made = await call(reg, box, "write_file", path="src/calc/extra.py", content="LIMIT = 10\n")
    assert made.ok, made.output
    r = await call(reg, box, "git_diff")
    assert r.ok, r.output
    assert "diff --git a/src/calc/extra.py b/src/calc/extra.py" in r.output
    assert "new file mode" in r.output and "+LIMIT = 10" in r.output


async def test_run_command_timeout_kills_process(reg: ToolRegistry, box: Sandbox) -> None:
    started = time.monotonic()
    r = await call(reg, box, "run_command", command="sleep 1234 & sleep 1234", timeout_s=2)
    elapsed = time.monotonic() - started
    assert not r.ok
    assert r.exit_code == 137
    assert r.output.startswith("timed out after 2 s and was killed.")
    assert elapsed < 8
    # The pattern's first letter is bracketed so pgrep does not match its own shell.
    left = await box.exec("pgrep -f '[s]leep 1234'; echo rc=$?", timeout_s=15)
    assert left.stdout.strip().endswith("rc=1"), f"processes survived the timeout: {left.stdout}"


async def test_run_command_timeout_is_clamped_to_the_configured_maximum(box: Sandbox) -> None:
    short = default_registry(test_command=TEST_COMMAND, max_timeout_s=3)
    started = time.monotonic()
    r = await call(short, box, "run_command", command="sleep 60", timeout_s=600)
    assert not r.ok
    assert "timed out after 3 s" in r.output and "(max 3)" in r.output
    assert time.monotonic() - started < 10


async def test_a_venv_script_cannot_shadow_the_timeout_wrapper(box: Sandbox) -> None:
    # /workspace/.venv/bin leads PATH, so the sandbox must call timeout by absolute path.
    await box.write_file(".venv/bin/timeout", b'#!/bin/sh\nshift 2\nexec "$@"\n')
    await box.exec("chmod +x .venv/bin/timeout", timeout_s=10)
    try:
        r = await box.exec("sleep 30", timeout_s=2)
        assert r.timed_out
        assert r.duration_ms < 8000
    finally:
        await box.exec("rm -f .venv/bin/timeout", timeout_s=10)
