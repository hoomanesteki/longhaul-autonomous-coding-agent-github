from collections.abc import AsyncIterator

import pytest

from longhaul.contracts import ToolCall, ToolContext
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.exec import HEAD_CHARS, TAIL_CHARS
from longhaul.tools.git import BASE_REF, NO_CHANGES, GitDiff
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")
IDENTITY = "-c user.name=test -c user.email=test@example.com -c commit.gpgsign=false"
MODULE = "src/calc.py"
BEFORE = b"def add(a, b):\n    return a - b\n"
AFTER = b"def add(a, b):\n    return a + b\n"


@pytest.fixture
async def repo() -> AsyncIterator[FakeSandbox]:
    """A sandbox holding a one-commit git repo tagged `base`, like a prepared workspace."""
    sbx = FakeSandbox()
    await sbx.write_file(MODULE, BEFORE)
    await sbx.write_file("README.md", b"calc\n")
    await sbx.write_file(".gitignore", b"ignored.log\n")
    prepared = await sbx.exec(
        f"git init -q && git add -A && git {IDENTITY} commit -q -m initial && git tag base",
        timeout_s=30,
    )
    assert prepared.exit_code == 0, prepared.stderr
    yield sbx
    await sbx.destroy()


async def diff(repo: FakeSandbox, **args: object) -> str:
    r = await GitDiff().run(repo, dict(args), CTX)
    assert r.ok, r.output
    return r.output


def test_base_ref_names_the_tag_unambiguously() -> None:
    # refs/tags/base cannot be confused with a branch or a file called base.
    assert BASE_REF == "refs/tags/base"


async def test_diff_is_empty_at_base(repo: FakeSandbox) -> None:
    assert await diff(repo) == NO_CHANGES
    assert await diff(repo, staged=True) == NO_CHANGES


async def test_diff_shows_an_edit_against_base(repo: FakeSandbox) -> None:
    await repo.write_file(MODULE, AFTER)
    out = await diff(repo)
    assert "diff --git a/src/calc.py b/src/calc.py" in out
    assert "-    return a - b" in out and "+    return a + b" in out


async def test_diff_stays_against_base_after_a_later_commit(repo: FakeSandbox) -> None:
    # Phase 2 commits after every mutating tool, so HEAD moves while base stays put.
    await repo.write_file(MODULE, AFTER)
    committed = await repo.exec(f"git add -A && git {IDENTITY} commit -q -m step", timeout_s=30)
    assert committed.exit_code == 0, committed.stderr
    out = await diff(repo)
    assert "+    return a + b" in out


async def test_staged_flag_only_shows_the_index(repo: FakeSandbox) -> None:
    await repo.write_file(MODULE, AFTER)
    assert await diff(repo, staged=True) == NO_CHANGES
    await repo.exec(f"git add {MODULE}", timeout_s=30)
    staged = await diff(repo, staged=True)
    assert "+    return a + b" in staged
    # The default view is the working tree against base, so it shows the same change.
    assert "+    return a + b" in await diff(repo)


async def test_new_files_show_up_as_new_file_diffs(repo: FakeSandbox) -> None:
    await repo.write_file("src/helper.py", b"X = 1\n")
    out = await diff(repo)
    assert "diff --git a/src/helper.py b/src/helper.py" in out
    assert "new file mode" in out and "+X = 1" in out
    # An untracked file is not in the index, so the staged view leaves it out.
    assert await diff(repo, staged=True) == NO_CHANGES


async def test_edits_and_new_files_appear_together(repo: FakeSandbox) -> None:
    await repo.write_file(MODULE, AFTER)
    await repo.write_file("docs/notes.md", b"hello\n")
    out = await diff(repo)
    assert out.index("a/src/calc.py") < out.index("a/docs/notes.md")


async def test_noise_files_are_not_part_of_the_diff(repo: FakeSandbox) -> None:
    for path in (
        "src/__pycache__/calc.cpython-312.pyc",
        "top.pyc",
        ".venv/bin/pytest",
        "node_modules/x/index.js",
        "src/calc.egg-info/PKG-INFO",
        ".pytest_cache/v/cache/lastfailed",
        ".mypy_cache/3.12/x.json",
        ".ruff_cache/0.1/y",
        "ignored.log",
    ):
        await repo.write_file(path, b"noise")
    assert await diff(repo) == NO_CHANGES


async def test_a_file_name_that_needs_quoting_is_still_shown(repo: FakeSandbox) -> None:
    await repo.write_file("it's here.txt", b"quoted\n")
    out = await diff(repo)
    assert "it's here.txt" in out and "+quoted" in out


async def test_many_new_files_are_listed_up_to_a_limit(repo: FakeSandbox) -> None:
    for n in range(4):
        await repo.write_file(f"new/f{n}.txt", f"{n}\n".encode())
    r = await GitDiff(max_new_files=2).run(repo, {}, CTX)
    assert r.ok
    assert "b/new/f0.txt" in r.output and "b/new/f1.txt" in r.output
    assert "b/new/f2.txt" not in r.output
    assert "... 2 more untracked files not shown" in r.output


async def test_a_huge_diff_is_capped_with_a_marker(repo: FakeSandbox) -> None:
    await repo.write_file("big.txt", ("x" * 99 + "\n").encode() * 600)
    out = await diff(repo)
    assert "characters cut]" in out
    assert len(out) < HEAD_CHARS + TAIL_CHARS + 200


async def test_missing_base_tag_is_a_clear_error(repo: FakeSandbox) -> None:
    await repo.exec("git tag -d base", timeout_s=30)
    r = await GitDiff().run(repo, {}, CTX)
    assert not r.ok
    assert "git diff failed" in r.output and "refs/tags/base" in r.output


async def test_outside_a_repository_is_a_clear_error() -> None:
    sbx = FakeSandbox()
    try:
        r = await GitDiff().run(sbx, {}, CTX)
        assert not r.ok and "git diff failed" in r.output
    finally:
        await sbx.destroy()


async def test_git_diff_is_registered_and_read_only(repo: FakeSandbox) -> None:
    reg = default_registry()
    tool = reg.get("git_diff")
    assert tool is not None and tool.mutates is False
    await repo.write_file(MODULE, AFTER)
    r = await reg.run(ToolCall(id="c", name="git_diff", input={"staged": False}), repo, CTX)
    assert r.ok and "+    return a + b" in r.output
    bad = await reg.run(ToolCall(id="d", name="git_diff", input={"staged": "yes"}), repo, CTX)
    assert not bad.ok and "staged" in bad.output
    # The call must not have staged or committed anything.
    status = await repo.exec("git diff --cached --quiet && echo clean", timeout_s=30)
    assert status.stdout.strip() == "clean"
