"""Path safety: no tool leaves /workspace, and every tool uses the real path.

The sandbox here fails any read or write whose path crosses a symlink. Docker's
get_archive does not follow a link in the last part of a path and put_archive
replaces a linked directory with a real one, while FakeSandbox follows links and
would hide both. A tool that hands the sandbox the resolved path passes.
"""

import shlex
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from longhaul.contracts import ExecResult, ToolCall, ToolContext, ToolResult
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.paths import PathError, normalize, resolve, safe_path
from longhaul.tools.registry import ToolRegistry, default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")


class AuditedSandbox(FakeSandbox):
    """FakeSandbox that refuses what Docker would mishandle, and remembers the commands."""

    def __init__(self) -> None:
        super().__init__()
        self.commands: list[str] = []

    def _audit(self, path: str) -> Path:
        current = self.root
        for part in PurePosixPath(path.removeprefix(self.workdir).lstrip("/")).parts:
            current = current / part
            if current.is_symlink():
                raise AssertionError(f"the tool handed the sandbox a path through a link: {path}")
        return current

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        self.commands.append(cmd)
        return await super().exec(cmd, timeout_s=timeout_s, env=env)

    async def read_file(self, path: str) -> bytes:
        target = self._audit(path)
        if not target.is_file():
            # Opening a fifo would block forever, and a directory has no bytes.
            raise AssertionError(f"the tool read something that is not a regular file: {path}")
        return await super().read_file(path)

    async def write_file(self, path: str, data: bytes) -> None:
        self._audit(path)
        await super().write_file(path, data)


@pytest.fixture
async def sbx() -> AsyncIterator[AuditedSandbox]:
    box = AuditedSandbox()
    yield box
    await box.destroy()


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory next to the sandbox that no tool may read, list or change."""
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "secret.txt").write_text("top secret\n")
    return folder


@pytest.fixture
def reg() -> ToolRegistry:
    return default_registry()


async def sh(box: FakeSandbox, cmd: str) -> str:
    done = await box.exec(cmd, timeout_s=30)
    assert done.exit_code == 0, f"{cmd}: {done.stderr}"
    return done.stdout


async def call(reg: ToolRegistry, box: FakeSandbox, name: str, **args: object) -> ToolResult:
    return await reg.run(ToolCall(id="c", name=name, input=dict(args)), box, CTX)


async def link_outside(box: FakeSandbox, outside: Path) -> None:
    target = shlex.quote(str(outside))
    await sh(
        box,
        f"ln -s {target} outlink && ln -s {target}/secret.txt outfile "
        f"&& ln -s {target}/not-there.txt dangling_out",
    )


def snapshot(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())}


def file_args(name: str, path: str) -> dict[str, object]:
    """Valid input for the tools that work on one file."""
    return {"path": path, "old_str": "a", "new_str": "b"} if name == "edit_file" else {"path": path}


# ---------- resolving ----------


def test_normalize_rejects_a_nul_byte() -> None:
    with pytest.raises(PathError, match="NUL"):
        normalize("a\0b")


async def test_resolve_reports_the_real_path_and_the_kind(sbx: AuditedSandbox) -> None:
    await sbx.write_file("src/calc/mod.py", b"x = 1\n")
    await sh(
        sbx,
        "ln -s src/calc/mod.py alias.py && ln -s src srclink && ln -s nothing dangling "
        "&& mkfifo pipe",
    )
    cases = {
        "src/calc/mod.py": ("src/calc/mod.py", "file"),
        "alias.py": ("src/calc/mod.py", "file"),
        "/workspace/alias.py": ("src/calc/mod.py", "file"),
        "./srclink/calc": ("src/calc", "dir"),
        "srclink/calc/new.py": ("src/calc/new.py", "missing"),
        ".": (".", "dir"),
        "": (".", "dir"),
        "dangling": ("nothing", "missing"),
        "pipe": ("pipe", "other"),
    }
    for raw, (rel, kind) in cases.items():
        found = await resolve(sbx, raw)
        assert (found.rel, found.kind) == (rel, kind), raw


async def test_safe_path_returns_the_real_relative_path(sbx: AuditedSandbox) -> None:
    await sbx.write_file("src/mod.py", b"x = 1\n")
    await sh(sbx, "ln -s src/mod.py alias.py")
    assert await safe_path(sbx, "alias.py", must_exist=True) == "src/mod.py"
    assert await safe_path(sbx, "src/new.py") == "src/new.py"
    with pytest.raises(FileNotFoundError, match="not found"):
        await safe_path(sbx, "nope.py", must_exist=True)


@pytest.mark.parametrize("raw", ["../x", "/etc/passwd", "a/../../x"])
async def test_resolve_refuses_lexical_escapes(sbx: AuditedSandbox, raw: str) -> None:
    with pytest.raises(PathError, match="outside"):
        await resolve(sbx, raw)


# ---------- names the filesystem cannot hold ----------

# A name is at most 255 bytes and a path at most 4096. The sandbox raises an OSError, or
# on Docker an APIError, for more, and the registry lets both through as infrastructure.
TOO_LONG = [
    pytest.param("x" * 256, id="name-of-256-bytes"),
    pytest.param("a/" + "x" * 256, id="second-name-of-256-bytes"),
    pytest.param("\u00e9" * 128, id="name-of-128-two-byte-characters"),
    pytest.param("/".join(["abcdefghij"] * 500), id="path-of-5499-bytes"),
    pytest.param("/".join(["x" * 255] * 15 + ["y" * 200, "z" * 56]), id="path-of-4097-bytes"),
]
FITS = [
    pytest.param("x" * 255, id="name-of-255-bytes"),
    pytest.param("a/" + "x" * 255, id="second-name-of-255-bytes"),
    pytest.param("\u00e9" * 127, id="name-of-127-two-byte-characters"),
    pytest.param("/".join(["abcdefghij"] * 300), id="path-of-3299-bytes"),
    pytest.param("/".join(["x" * 255] * 15 + ["y" * 200, "z" * 55]), id="path-of-4096-bytes"),
]


@pytest.mark.parametrize("raw", TOO_LONG)
def test_normalize_rejects_names_and_paths_that_are_too_long(raw: str) -> None:
    with pytest.raises(PathError, match="longer than"):
        normalize(raw)


@pytest.mark.parametrize("raw", FITS)
def test_normalize_accepts_the_longest_names_that_fit(raw: str) -> None:
    assert normalize(raw) == raw


def test_normalize_rejects_a_lone_surrogate() -> None:
    # A reply cut off in the middle of an emoji can leave half of a surrogate pair.
    with pytest.raises(PathError, match="UTF-8"):
        normalize("a\ud800.txt")


@pytest.mark.parametrize(
    "path", [*TOO_LONG[:2], pytest.param("bad\ud800.txt", id="lone-surrogate")]
)
async def test_a_path_no_filesystem_can_hold_is_a_result_for_every_tool(
    path: str, sbx: AuditedSandbox, reg: ToolRegistry
) -> None:
    for name in tools_with_a_path(reg):
        r = await call(reg, sbx, name, **args_for(reg, name, path))
        assert not r.ok, name
        assert "longer than" in r.output or "UTF-8" in r.output, (name, r.output)


# ---------- every tool with a path ----------

ESCAPES = [
    "../../etc/passwd",
    "/etc/passwd",
    "outlink",
    "outlink/secret.txt",
    "outlink/new/file.txt",
    "outfile",
    "dangling_out",
]
FILLERS = {"pattern": "secret", "old_str": "top", "new_str": "TOP", "content": "pwned\n"}


def tools_with_a_path(reg: ToolRegistry) -> list[str]:
    return [s.name for s in reg.specs() if "path" in s.input_schema.get("properties", {})]


def args_for(reg: ToolRegistry, name: str, path: str) -> dict[str, Any]:
    tool = reg.get(name)
    assert tool is not None
    required = [k for k in tool.spec.input_schema.get("required", []) if k != "path"]
    # A KeyError here means a new tool needs a filler value for its other arguments.
    return {"path": path, **{key: FILLERS[key] for key in required}}


async def test_every_tool_with_a_path_refuses_to_leave_the_workspace(
    sbx: AuditedSandbox, outside: Path, reg: ToolRegistry
) -> None:
    await link_outside(sbx, outside)
    names = tools_with_a_path(reg)
    assert {"list_dir", "read_file", "edit_file", "write_file", "search"} <= set(names)
    before = snapshot(outside)
    for name in names:
        for path in ESCAPES:
            r = await call(reg, sbx, name, **args_for(reg, name, path))
            assert not r.ok, (name, path, r.output)
            assert "outside" in r.output, (name, path, r.output)
    assert snapshot(outside) == before


# ---------- a path that cannot be resolved ----------


def answer(stdout: str = "", stderr: str = "", code: int = 0) -> ExecResult:
    return ExecResult(exit_code=code, stdout=stdout, stderr=stderr, timed_out=False, duration_ms=1)


class BrokenResolver(FakeSandbox):
    """A FakeSandbox whose path check, the python3 one-liner, gives a fixed answer."""

    def __init__(self, result: ExecResult) -> None:
        super().__init__()
        self.result = result

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        if cmd.startswith("python3 -c "):
            return self.result
        return await super().exec(cmd, timeout_s=timeout_s, env=env)


@pytest.mark.parametrize(
    "result",
    [
        answer(stderr="python3: command not found", code=127),
        answer(stdout="not json at all"),
        answer(stdout='{"only": "one key"}'),
        answer(stdout='["/workspace/a.py", "/workspace", "nonsense"]'),
        answer(stdout='[1, "/workspace", "file"]'),
    ],
    ids=["no-python", "not-json", "wrong-shape", "unknown-kind", "wrong-type"],
)
async def test_a_path_that_cannot_be_resolved_is_refused_by_every_tool(
    result: ExecResult, reg: ToolRegistry
) -> None:
    # Failing to learn where a path leads must never fall back to trusting the spelling.
    box = BrokenResolver(result)
    try:
        await box.write_file("a.py", b"x\n")
        for name in tools_with_a_path(reg):
            r = await call(reg, box, name, **args_for(reg, name, "a.py"))
            assert not r.ok, name
            assert "could not resolve path: a.py" in r.output, (name, r.output)
        assert await box.read_file("a.py") == b"x\n"
    finally:
        await box.destroy()


# ---------- dash-leading names ----------


async def test_a_dash_leading_directory_is_listed_like_any_other(
    sbx: AuditedSandbox, outside: Path, reg: ToolRegistry
) -> None:
    # `find -L ...` used to run for list_dir("./-L"): find read the dash as an option,
    # walked "." and followed outlink, so the listing showed files outside the workspace.
    await link_outside(sbx, outside)
    await sh(sbx, "mkdir -p -- ./-L/inner && touch ./-L/inner/f")
    for raw in ("./-L", "-L", "/workspace/-L"):
        r = await call(reg, sbx, "list_dir", path=raw)
        assert r.ok, r.output
        assert r.output == "inner/\n  f", (raw, r.output)


async def test_dash_leading_files_can_be_read_edited_and_written(
    sbx: AuditedSandbox, reg: ToolRegistry
) -> None:
    await sh(sbx, "printf 'one\\n' > ./-n")
    assert "one" in (await call(reg, sbx, "read_file", path="-n")).output
    edit = await call(reg, sbx, "edit_file", path="-n", old_str="one", new_str="two")
    assert edit.ok, edit.output
    made = await call(reg, sbx, "write_file", path="-x/-y", content="z")
    assert made.ok, made.output
    assert await sbx.read_file("-x/-y") == b"z"
    assert await sbx.read_file("-n") == b"two\n"


async def test_search_gets_the_path_after_the_end_of_options_marker(
    sbx: AuditedSandbox, reg: ToolRegistry
) -> None:
    await sh(sbx, "mkdir -p -- ./-L")
    await call(reg, sbx, "search", pattern="x", path="-L")
    command = sbx.commands[-1]
    assert command.startswith("rg ")
    assert " -- " in command and command.endswith("-L")


# ---------- links that stay inside the workspace ----------


async def test_a_symlinked_directory_is_listed_through_its_target(
    sbx: AuditedSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("src/calc/mod.py", b"x = 1\n")
    await sh(sbx, "ln -s src srclink")
    r = await call(reg, sbx, "list_dir", path="srclink")
    assert r.ok, r.output
    assert r.output == "calc/\n  mod.py"


async def test_read_edit_and_write_reach_the_real_file_through_links(
    sbx: AuditedSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("src/calc/mod.py", b"return a - b\n")
    await sh(sbx, "ln -s src/calc/mod.py alias.py && ln -s src srclink")
    read = await call(reg, sbx, "read_file", path="alias.py")
    assert read.ok, read.output
    assert "return a - b" in read.output
    edit = await call(
        reg, sbx, "edit_file", path="srclink/calc/mod.py", old_str="a - b", new_str="a + b"
    )
    assert edit.ok, edit.output
    assert "src/calc/mod.py" in edit.output
    made = await call(reg, sbx, "write_file", path="srclink/calc/new.py", content="y = 2\n")
    assert made.ok, made.output
    assert await sbx.read_file("src/calc/mod.py") == b"return a + b\n"
    assert await sbx.read_file("src/calc/new.py") == b"y = 2\n"
    await sh(sbx, "test -L alias.py && test -L srclink")


async def test_search_looks_in_the_real_directory_behind_a_link(
    sbx: AuditedSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("src/mod.py", b"x = 1\n")
    await sh(sbx, "ln -s src srclink")
    await call(reg, sbx, "search", pattern="x", path="srclink")
    assert sbx.commands[-1].endswith(" -- src")


# ---------- things that are not the kind of path the tool needs ----------


async def test_a_directory_is_not_a_file(sbx: AuditedSandbox, reg: ToolRegistry) -> None:
    await sbx.write_file("src/a.py", b"x\n")
    for name, extra in (
        ("read_file", {}),
        ("edit_file", {"old_str": "a", "new_str": "b"}),
        ("write_file", {"content": "x"}),
    ):
        r = await call(reg, sbx, name, path="src", **extra)
        assert not r.ok, name
        assert "src is a directory" in r.output, (name, r.output)
        assert str(sbx.root) not in r.output, (name, r.output)
    # The two tools that read the file also say where to look instead.
    assert "list_dir" in (await call(reg, sbx, "read_file", path="src")).output


async def test_list_dir_on_a_file_points_to_read_file(
    sbx: AuditedSandbox, reg: ToolRegistry
) -> None:
    await sbx.write_file("a.py", b"x\n")
    r = await call(reg, sbx, "list_dir", path="a.py")
    assert not r.ok
    assert "a.py is a file" in r.output and "read_file" in r.output


async def test_a_missing_path_is_reported_by_the_name_the_model_used(
    sbx: AuditedSandbox, reg: ToolRegistry
) -> None:
    await sh(sbx, "ln -s elsewhere.txt dangling")
    for name in ("read_file", "edit_file"):
        r = await call(reg, sbx, name, **file_args(name, "dangling"))
        assert not r.ok and "not found: dangling" in r.output, (name, r.output)
    assert "not found" in (await call(reg, sbx, "list_dir", path="nope")).output
    assert "not found" in (await call(reg, sbx, "search", pattern="x", path="nope")).output


async def test_a_fifo_is_never_opened(sbx: AuditedSandbox, reg: ToolRegistry) -> None:
    await sh(sbx, "mkfifo pipe")
    listing = await call(reg, sbx, "list_dir", path="pipe")
    assert not listing.ok and "pipe is not a directory" in listing.output
    for name in ("read_file", "edit_file"):
        r = await call(reg, sbx, name, **file_args(name, "pipe"))
        assert not r.ok and "not a regular file" in r.output, (name, r.output)
    made = await call(reg, sbx, "write_file", path="pipe", content="x")
    assert not made.ok and "not a regular file" in made.output
