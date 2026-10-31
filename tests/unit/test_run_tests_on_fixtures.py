"""run_tests and a checkpoint add on the fixture repos, in a fake sandbox.

The fixtures use a src layout. A sandbox without a setup step, as the fake one is,
has not installed the package, so pytest has to find it from the repo's own config.
What a test run leaves behind must also stay out of `git add -A`, which the
checkpoint store runs after a step that changed files.
"""

import shlex
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from longhaul.contracts import ToolCall, ToolContext, ToolResult
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

FIXTURES = Path(__file__).parents[1] / "fixtures" / "repos"
CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")
# What pytest reports at base. Written out here and not read from the fixtures.
AT_BASE = {
    "calc": "1 failed, 2 passed",
    "textkit": "1 failed, 2 passed",
    "inventory": "1 failed, 4 passed",
}
# The interpreter running this test, so it does not matter whether pytest is on PATH.
TEST_COMMAND = f"{shlex.quote(sys.executable)} -m pytest -q"
# Never part of a workspace: the reference fix, and debris from running a fixture by hand.
SKIP_PARTS = {"REFERENCE.patch", "__pycache__", ".pytest_cache"}
DEBRIS_DIRS = {"__pycache__", ".pytest_cache", ".venv"}


@pytest.fixture
async def sbx() -> AsyncIterator[FakeSandbox]:
    box = FakeSandbox()
    yield box
    await box.destroy()


async def seed(box: FakeSandbox, name: str) -> None:
    source = FIXTURES / name
    for f in sorted(source.rglob("*")):
        parts = set(f.relative_to(source).parts)
        if (
            f.is_file()
            and not parts & SKIP_PARTS
            and not any(p.endswith(".egg-info") for p in parts)
        ):
            await box.write_file(str(f.relative_to(source)), f.read_bytes())


async def sh(box: FakeSandbox, cmd: str) -> str:
    done = await box.exec(cmd, timeout_s=60)
    assert done.exit_code == 0, f"{cmd}: {done.stderr}"
    return done.stdout


async def run_tests(box: FakeSandbox) -> ToolResult:
    call = ToolCall(id="c", name="run_tests", input={})
    return await default_registry(test_command=TEST_COMMAND).run(call, box, CTX)


def is_debris(path: str) -> bool:
    parts = path.split("/")
    return bool(DEBRIS_DIRS & set(parts)) or any(p.endswith(".egg-info") for p in parts)


@pytest.mark.parametrize("name", AT_BASE)
async def test_run_tests_runs_the_fixture_where_it_stands(name: str, sbx: FakeSandbox) -> None:
    await seed(sbx, name)
    r = await run_tests(sbx)
    assert r.ok, r.output
    # One failing test, as the issue says. A package pytest cannot import fails at
    # collection instead, with nothing run.
    assert "during collection" not in r.output and "ModuleNotFoundError" not in r.output, r.output
    assert r.exit_code == 1
    assert f"summary: {AT_BASE[name]}" in r.output, r.output


@pytest.mark.parametrize("name", AT_BASE)
async def test_a_test_run_leaves_nothing_for_git_add_to_pick_up(
    name: str, sbx: FakeSandbox
) -> None:
    await seed(sbx, name)
    await sh(sbx, "git init -q")
    await run_tests(sbx)
    # What a run can leave, spelled out, because the fake may not write bytecode. The venv and
    # the egg-info directory are what a setup step with pip install -e leaves in the workspace.
    for debris in (
        f"src/{name}/__pycache__/__init__.cpython-312.pyc",
        "tests/__pycache__/test_x.cpython-312-pytest-9.0.0.pyc",
        ".pytest_cache/README.md",
        f"src/{name}.egg-info/PKG-INFO",
        ".venv/bin/python",
    ):
        await sbx.write_file(debris, b"x")
    await sh(sbx, "git add -A")
    tracked = (await sh(sbx, "git ls-files")).split()
    assert [p for p in tracked if is_debris(p)] == []
    # The checks above mean something only when the real files were added.
    assert {"pyproject.toml", "ISSUE.md", ".gitignore"} <= set(tracked)
    assert any(p.startswith(f"src/{name}/") for p in tracked)
