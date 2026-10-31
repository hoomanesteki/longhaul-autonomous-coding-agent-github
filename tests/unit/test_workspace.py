"""Local workspace preparation, on a fake sandbox.

The Docker side (the real base tag, the venv, the detached network) is in
tests/integration/test_local_workspace.py. These tests cover the parts that need no
container: which files are copied, what the ignore file means, the order of the steps,
and what happens to the sandbox when a step fails.
"""

from __future__ import annotations

import errno
import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from longhaul.agent.workspace import (
    IGNORE_FILE,
    IgnoreRules,
    LocalWorkspace,
    SetupCommandError,
    WorkspaceError,
    collect_source_files,
    copy_source,
    default_setup_command,
    prepare_local_workspace,
)
from longhaul.contracts import ExecResult, Sandbox, SandboxLimits
from longhaul.sandbox.local_manager import FakeSandbox

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parents[1] / "fixtures" / "repos"
LIMITS = SandboxLimits()


def make_source(root: Path, files: dict[str, str | bytes]) -> Path:
    """A source folder holding `files`, keyed by relative path."""
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode() if isinstance(content, str) else content)
    return root


def landed(sbx: FakeSandbox) -> list[str]:
    """Every file in the sandbox's workspace, as relative posix paths."""
    return sorted(p.relative_to(sbx.root).as_posix() for p in sbx.root.rglob("*") if p.is_file())


async def copy_to_fake(source: Path, ignore_file: str = IGNORE_FILE) -> list[str]:
    box = FakeSandbox()
    try:
        await copy_source(box, source, collect_source_files(source, ignore_file))
        return landed(box)
    finally:
        await box.destroy()


@pytest.fixture
async def sbx() -> AsyncIterator[FakeSandbox]:
    box = FakeSandbox()
    yield box
    await box.destroy()


# --- the ignore file's patterns ------------------------------------------------------------

# (patterns, path, is a directory, ignored). One pattern per case unless a case has several lines.
RULE_CASES = [
    ("REFERENCE.patch", "REFERENCE.patch", False, True),
    ("REFERENCE.patch", "sub/REFERENCE.patch", False, True),  # no slash: any depth
    ("REFERENCE.patch", "REFERENCE.patch.bak", False, False),  # the whole name has to match
    ("REFERENCE.patch", "my-REFERENCE.patch", False, False),
    ("/REFERENCE.patch", "REFERENCE.patch", False, True),
    ("/REFERENCE.patch", "sub/REFERENCE.patch", False, False),  # a leading slash: the root only
    ("docs/notes.md", "docs/notes.md", False, True),
    ("docs/notes.md", "other/docs/notes.md", False, False),  # a slash inside anchors it too
    ("*.log", "run.log", False, True),
    ("*.log", "a/b/run.log", False, True),
    ("*.log", "run.logs", False, False),
    ("*.log", "logs/run.txt", False, False),
    ("x/a*b", "x/aXXb", False, True),
    ("x/a*b", "x/a/b", False, False),  # a star does not cross a slash
    ("build/", "build", True, True),
    ("build/", "build", False, False),  # a trailing slash means directories only
    ("build/", "build/out/x.bin", False, True),  # and everything below one
    ("build/", "src/build/x.bin", False, True),
    ("src/**/gen.py", "src/gen.py", False, True),
    ("src/**/gen.py", "src/a/b/gen.py", False, True),
    ("src/**/gen.py", "lib/gen.py", False, False),
    ("**/cache", "cache", True, True),
    ("**/cache", "a/b/cache", True, True),
    ("data/**", "data/x.csv", False, True),
    ("data/**", "data/deep/x.csv", False, True),
    ("data/**", "other/data/x.csv", False, False),
    ("file?.txt", "file1.txt", False, True),
    ("file?.txt", "file10.txt", False, False),
    ("file[0-9].txt", "file7.txt", False, True),
    ("file[0-9].txt", "filex.txt", False, False),
    ("file[!0-9].txt", "file7.txt", False, False),
    ("file[!0-9].txt", "filex.txt", False, True),
    ("[]]x", "]x", False, True),  # a closing bracket first in a class is one of its members
    ("[!]]x", "ax", False, True),
    ("[!]]x", "]x", False, False),
    ("[open", "[open", False, True),  # a bracket that never closes is a plain character
    ("*.txt\n!keep.txt", "drop.txt", False, True),
    ("*.txt\n!keep.txt", "keep.txt", False, False),  # a later negation wins
    ("!keep.txt\n*.txt", "keep.txt", False, True),  # and so does a later match
    ("# REFERENCE.patch", "REFERENCE.patch", False, False),  # a comment
    ("  # indented", "  # indented", False, True),  # only a hash in column 0 starts a comment
    ("\n\n\n", "anything", False, False),  # blank lines
    ("REFERENCE.patch   ", "REFERENCE.patch", False, True),  # trailing spaces are trimmed
    ("REFERENCE.patch\r\nISSUE.md\r\n", "ISSUE.md", False, True),  # a file saved on Windows
    ("\\#file", "#file", False, True),  # an escaped hash is a name
    ("\\!bang", "!bang", False, True),
]


@pytest.mark.parametrize(("patterns", "path", "is_dir", "ignored"), RULE_CASES)
def test_ignore_rules(patterns: str, path: str, is_dir: bool, ignored: bool) -> None:
    assert IgnoreRules.parse(patterns).ignores(path, is_dir=is_dir) is ignored


def test_the_runbook_example_means_what_the_runbook_says() -> None:
    text = (ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    found = re.search(r"Its syntax is the one of `\.[a-z]+ignore`:\n\n```\n(.*?)\n```", text, re.S)
    assert found, "the .longhaulignore example is gone from the runbook"
    rules = IgnoreRules.parse(found.group(1))
    hidden = ["REFERENCE.patch", "ISSUE.md", "sub/ISSUE.md", "build/out.bin", "run.log"]
    hidden += ["docs/private/notes.md", "docs/private/deep/x.md"]
    for path in hidden:
        assert rules.ignores(path), path
    for path in ("README.md", "docs/guide.md", "docs/private/keep.md"):
        assert not rules.ignores(path), path


def test_a_pattern_that_cannot_be_read_is_an_error() -> None:
    # Skipping it would copy files the user meant to hide, so the preparation stops instead.
    with pytest.raises(WorkspaceError, match=r"line 2.*\[z-a\]x"):
        IgnoreRules.parse("fine\n[z-a]x\n", origin=IGNORE_FILE)


def test_an_ignored_directory_cannot_be_reopened_by_a_negation() -> None:
    # The same as git: a file below an excluded directory stays excluded.
    rules = IgnoreRules.parse("secret/\n!secret/keep.txt")
    assert rules.ignores("secret/keep.txt")


def test_a_lone_slash_or_bang_is_not_a_pattern() -> None:
    assert not IgnoreRules.parse("/\n!\n//\n").ignores("anything")


# --- what is copied ------------------------------------------------------------------------


async def test_every_file_is_copied_with_its_bytes(tmp_path: Path, sbx: FakeSandbox) -> None:
    binary = bytes(range(256)) + b"\r\n\x00end"
    source = make_source(
        tmp_path / "src",
        {"a.txt": "one\n", "pkg/b.py": "x = 1\n", "pkg/deep/er/c.bin": binary, "crlf.txt": "a\r\n"},
    )
    await copy_source(sbx, source, collect_source_files(source))
    assert landed(sbx) == ["a.txt", "crlf.txt", "pkg/b.py", "pkg/deep/er/c.bin"]
    assert await sbx.read_file("pkg/deep/er/c.bin") == binary
    assert await sbx.read_file("crlf.txt") == b"a\r\n"


@pytest.mark.parametrize(
    "skipped",
    [
        ".git/config",
        ".git/objects/ab/cdef",
        ".venv/bin/python",
        "__pycache__/x.cpython-312.pyc",
        "pkg/__pycache__/x.cpython-312.pyc",
        "stray.pyc",
        "pkg/stray.pyc",
        ".pytest_cache/README.md",
        "pkg.egg-info/PKG-INFO",
        "src/pkg.egg-info/PKG-INFO",
        ".mypy_cache/3.12/x.json",
        ".ruff_cache/CACHEDIR.TAG",
        ".DS_Store",
        "pkg/.DS_Store",
        ".env",
        ".env.local",
        ".env.production",
        "app/.env",
    ],
)
async def test_built_in_skips(tmp_path: Path, skipped: str) -> None:
    source = make_source(tmp_path / "src", {"keep.txt": "k", skipped: "x"})
    assert await copy_to_fake(source) == ["keep.txt"]


async def test_a_gitdir_file_is_skipped_too(tmp_path: Path) -> None:
    # In a worktree or a submodule `.git` is a file that points at the real repository.
    source = make_source(tmp_path / "src", {"keep.txt": "k", ".git": "gitdir: /elsewhere\n"})
    assert await copy_to_fake(source) == ["keep.txt"]


@pytest.mark.parametrize(
    "kept",
    [".env.example", ".env.sample", ".env.template", ".gitignore", "environment.py", "envfile"],
)
async def test_names_that_only_look_like_secrets_are_kept(tmp_path: Path, kept: str) -> None:
    source = make_source(tmp_path / "src", {kept: "x"})
    assert await copy_to_fake(source) == [kept]


async def test_the_ignore_file_hides_what_it_lists(tmp_path: Path) -> None:
    patterns = "# the reference fix and the issue text\nREFERENCE.patch\nISSUE.md\nbuild/\n"
    source = make_source(
        tmp_path / "src",
        {
            IGNORE_FILE: patterns,
            "REFERENCE.patch": "--- a\n+++ b\n",
            "ISSUE.md": "# bug\n",
            "sub/ISSUE.md": "# nested\n",
            "build/out.txt": "o",
            "src/app.py": "print()\n",
            "README.md": "# readme\n",
        },
    )
    assert await copy_to_fake(source) == ["README.md", "src/app.py"]


async def test_the_ignore_file_is_not_copied_because_it_names_what_it_hides(
    tmp_path: Path,
) -> None:
    # A model that listed the workspace would learn that a REFERENCE.patch exists.
    source = make_source(tmp_path / "src", {IGNORE_FILE: "REFERENCE.patch\n", "a.txt": "a"})
    assert await copy_to_fake(source) == ["a.txt"]


async def test_a_folder_without_an_ignore_file_copies_everything_but_the_built_in_skips(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path / "src", {"REFERENCE.patch": "p", "ISSUE.md": "i"})
    assert await copy_to_fake(source) == ["ISSUE.md", "REFERENCE.patch"]


async def test_the_ignore_file_name_can_be_changed(tmp_path: Path) -> None:
    source = make_source(
        tmp_path / "src",
        {".skipme": "notes.txt\n", IGNORE_FILE: "other.txt\n", "notes.txt": "n", "other.txt": "o"},
    )
    # The default name is an ordinary file when another one is asked for.
    assert await copy_to_fake(source, ignore_file=".skipme") == [IGNORE_FILE, "other.txt"]


async def test_the_built_in_skips_cannot_be_turned_off_by_a_negation(tmp_path: Path) -> None:
    source = make_source(
        tmp_path / "src",
        {
            IGNORE_FILE: "!.env\n!.venv\n!*.pyc\n",
            ".env": "KEY=1",
            ".venv/x": "x",
            "a.pyc": "x",
            "keep.txt": "k",
        },
    )
    assert await copy_to_fake(source) == ["keep.txt"]


@pytest.mark.parametrize("name", ["calc", "textkit", "inventory"])
async def test_the_fixture_repos_hide_their_reference_fix(name: str) -> None:
    # A real model must not see the fix that the repo's own tests are checked against.
    files = await copy_to_fake(FIXTURES / name)
    assert "pyproject.toml" in files and ".gitignore" in files
    assert any(f.startswith("src/") for f in files) and any(f.startswith("tests/") for f in files)
    for hidden in ("REFERENCE.patch", "ISSUE.md", IGNORE_FILE):
        assert hidden not in files, f"{name}: {hidden} was copied"


async def test_links_and_special_files_are_skipped_and_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A link out of the folder would hand the sandbox whatever it points at, and the
    # sandbox's file API cannot create links. Reading a pipe would block for good.
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n")
    source = make_source(tmp_path / "src", {"keep.txt": "k", "real/inner.txt": "i"})
    (source / "link.txt").symlink_to(outside)
    (source / "dirlink").symlink_to(source / "real", target_is_directory=True)
    (source / "dangling").symlink_to(tmp_path / "missing")
    os.mkfifo(source / "pipe")
    with caplog.at_level(logging.WARNING, logger="longhaul.agent.workspace"):
        files = await copy_to_fake(source)
    assert files == ["keep.txt", "real/inner.txt"]
    for name in ("link.txt", "dirlink", "dangling", "pipe"):
        assert name in caplog.text


async def test_the_executable_bit_survives_the_copy(tmp_path: Path, sbx: FakeSandbox) -> None:
    source = make_source(tmp_path / "src", {"run.sh": "#!/bin/sh\necho hi\n", "data.txt": "x"})
    (source / "run.sh").chmod(0o755)
    await copy_source(sbx, source, collect_source_files(source))
    ran = await sbx.exec("./run.sh", timeout_s=10)
    assert (ran.exit_code, ran.stdout.strip()) == (0, "hi")
    assert (await sbx.exec("test -x data.txt", timeout_s=10)).exit_code != 0


async def test_many_executables_and_odd_names_are_handled(tmp_path: Path, sbx: FakeSandbox) -> None:
    names = {f"bin/tool {i}'s.sh": "#!/bin/sh\n" for i in range(120)} | {"-dash.sh": "#!/bin/sh\n"}
    source = make_source(tmp_path / "src", names)
    for name in names:
        (source / name).chmod(0o755)
    await copy_source(sbx, source, collect_source_files(source))
    check = await sbx.exec("find . -type f ! -perm -u+x | wc -l", timeout_s=30)
    assert check.stdout.strip() == "0"
    assert len(landed(sbx)) == 121


async def test_files_come_in_a_stable_order(tmp_path: Path) -> None:
    source = make_source(tmp_path / "src", {"b.txt": "b", "a/z.txt": "z", "a/b.txt": "b", "c": "c"})
    assert [f.path for f in collect_source_files(source)] == ["a/b.txt", "a/z.txt", "b.txt", "c"]


async def test_a_folder_that_is_not_a_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="not a directory"):
        collect_source_files(tmp_path / "missing")
    (tmp_path / "file.txt").write_text("x")
    with pytest.raises(WorkspaceError, match="not a directory"):
        collect_source_files(tmp_path / "file.txt")


async def test_a_folder_with_nothing_to_copy_is_an_error(tmp_path: Path) -> None:
    source = make_source(tmp_path / "src", {"REFERENCE.patch": "p", IGNORE_FILE: "REFERENCE.patch"})
    with pytest.raises(WorkspaceError, match="no files to copy"):
        collect_source_files(source)


async def test_an_ignore_file_that_cannot_be_read_is_an_error(tmp_path: Path) -> None:
    # Carrying on without it would copy the files it was meant to hide.
    source = make_source(tmp_path / "src", {IGNORE_FILE: b"\xff\xfe\x00bad", "a.txt": "a"})
    with pytest.raises(WorkspaceError, match=re.escape(IGNORE_FILE)):
        collect_source_files(source)
    (source / IGNORE_FILE).unlink()
    (source / IGNORE_FILE).mkdir()
    with pytest.raises(WorkspaceError, match=re.escape(IGNORE_FILE)):
        collect_source_files(source)


async def test_a_file_that_cannot_be_read_is_an_error(
    tmp_path: Path, sbx: FakeSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(tmp_path / "src", {"ok.txt": "o", "secret.txt": "s"})
    files = collect_source_files(source)
    real = Path.read_bytes

    def deny(self: Path) -> bytes:
        if self.name == "secret.txt":
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", deny)
    with pytest.raises(WorkspaceError, match=r"cannot read secret\.txt"):
        await copy_source(sbx, source, files)


async def test_a_folder_that_cannot_be_listed_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # os.walk skips such a folder in silence unless it is given an error handler.
    source = make_source(tmp_path / "src", {"a.txt": "a"})

    def walk(
        top: Path, onerror: Callable[[OSError], None] | None = None, **_: Any
    ) -> Iterator[Any]:
        assert onerror is not None
        onerror(PermissionError(errno.EACCES, "Permission denied", str(Path(top) / "locked")))
        return iter(())

    monkeypatch.setattr(os, "walk", walk)
    with pytest.raises(WorkspaceError, match=r"cannot read .*locked: Permission denied"):
        collect_source_files(source)


async def test_a_file_that_vanishes_during_the_scan_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(tmp_path / "src", {"gone.txt": "g", "ok.txt": "o"})
    real = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if Path(path).name == "gone.txt":
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    with pytest.raises(WorkspaceError, match=r"cannot read gone\.txt"):
        collect_source_files(source)


# --- the default setup command -------------------------------------------------------------

VENV = "python -m venv .venv && .venv/bin/pip install"
PROJECT = '[project]\nname = "p"\nversion = "0"\n'


def pyproject_in(root: Path, text: str) -> Path:
    return make_source(root / "src", {"pyproject.toml": text})


def test_no_default_setup_without_a_pyproject(tmp_path: Path) -> None:
    source = make_source(tmp_path / "src", {"a.py": "x = 1\n"})
    assert default_setup_command(source) is None


@pytest.mark.parametrize("extra", ["dev", "Dev", "DEV"])
def test_the_default_setup_installs_the_dev_extra(tmp_path: Path, extra: str) -> None:
    source = pyproject_in(tmp_path, f'{PROJECT}[project.optional-dependencies]\n{extra} = ["x"]\n')
    command = default_setup_command(source)
    assert command is not None and command.startswith(VENV)
    assert command.endswith("-e '.[dev]'")


@pytest.mark.parametrize(
    "pyproject",
    [
        PROJECT,  # no extras at all
        f'{PROJECT}[project.optional-dependencies]\ndocs = ["x"]\n',
        '[dependency-groups]\ndev = ["pytest"]\n',  # pip install -e does not read these
        "this is [not toml",
        "",
    ],
    ids=["no-extras", "other-extra", "dependency-group", "broken-toml", "empty"],
)
def test_the_default_setup_falls_back_to_pytest_without_a_dev_extra(
    tmp_path: Path, pyproject: str
) -> None:
    command = default_setup_command(pyproject_in(tmp_path, pyproject))
    assert command is not None and command.startswith(VENV)
    assert command.endswith("-e . pytest")


def test_the_default_setup_keeps_the_pip_cache_off_the_small_home_tmpfs(tmp_path: Path) -> None:
    command = default_setup_command(pyproject_in(tmp_path, PROJECT))
    assert command is not None and "--no-cache-dir" in command


# --- the whole preparation, with a fake manager --------------------------------------------


class RecordingSandbox(FakeSandbox):
    """A FakeSandbox that logs each command, and answers the ones that need a network."""

    def __init__(self, sandbox_id: str, log: list[str], stub: dict[str, ExecResult]) -> None:
        super().__init__(sandbox_id)
        self._log = log
        self._stub = stub

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        self._log.append(f"exec {cmd}")
        for prefix, result in self._stub.items():
            if cmd.startswith(prefix):
                return result
        return await super().exec(cmd, timeout_s=timeout_s, env=env)


class FakeManager:
    """Hands out RecordingSandboxes and notes what the preparation asks of the manager."""

    def __init__(self, stub: dict[str, ExecResult] | None = None) -> None:
        self.log: list[str] = []
        self.sandboxes: list[RecordingSandbox] = []
        self.fail_create = False
        self._stub = stub or {}

    async def create(
        self, task_id: str, image: str, limits: SandboxLimits, network: bool
    ) -> Sandbox:
        self.log.append(f"create {task_id} {image} network={network}")
        if self.fail_create:
            raise RuntimeError("docker is down")
        box = RecordingSandbox(f"fake-{task_id}", self.log, self._stub)
        self.sandboxes.append(box)
        return box

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return next((s for s in self.sandboxes if s.id == sandbox_id), None)

    async def set_network(self, sandbox_id: str, enabled: bool) -> None:
        self.log.append(f"set_network {enabled}")

    async def cleanup_task(self, task_id: str) -> None:
        self.log.append(f"cleanup {task_id}")
        for box in self.sandboxes:
            await box.destroy()

    @property
    def commands(self) -> list[str]:
        return [e.removeprefix("exec ") for e in self.log if e.startswith("exec ")]


OK = ExecResult(exit_code=0, stdout="", stderr="", timed_out=False, duration_ms=1)


@pytest.fixture
async def manager() -> AsyncIterator[FakeManager]:
    fake = FakeManager()
    yield fake
    for box in fake.sandboxes:
        await box.destroy()


async def git(box: Sandbox, args: str) -> str:
    done = await box.exec(f"git {args}", timeout_s=30)
    assert done.exit_code == 0, done.stderr
    return done.stdout.strip()


async def prepare(manager: FakeManager, source: Path, **options: Any) -> LocalWorkspace:
    return await prepare_local_workspace(
        manager, source, task_id="t1", image="img:1", limits=LIMITS, **options
    )


async def test_prepare_commits_the_files_and_tags_the_base(
    tmp_path: Path, manager: FakeManager
) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n", "pkg/b.py": "x = 1\n"})
    box, base_sha = await prepare(manager, source, setup_command="true")
    assert len(base_sha) == 40
    assert await git(box, "rev-parse refs/tags/base") == base_sha
    assert await git(box, "rev-parse HEAD") == base_sha
    assert await git(box, "status --porcelain") == ""
    assert await git(box, "rev-list --count HEAD") == "1"
    assert (await git(box, "ls-files")).splitlines() == ["a.txt", "pkg/b.py"]
    assert await git(box, "log -1 --format=%an") == "longhaul"


async def test_prepare_returns_the_sandbox_and_the_sha_by_name_too(
    tmp_path: Path, manager: FakeManager
) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n"})
    workspace = await prepare(manager, source, setup_command="true")
    assert workspace.sandbox is manager.sandboxes[0]
    assert workspace.base_sha == await git(workspace.sandbox, "rev-parse HEAD")


async def test_prepare_keeps_the_hidden_files_out_of_the_workspace_and_the_commit(
    tmp_path: Path, manager: FakeManager
) -> None:
    source = make_source(
        tmp_path / "src",
        {
            IGNORE_FILE: "REFERENCE.patch\nISSUE.md\n",
            "REFERENCE.patch": "p",
            "ISSUE.md": "i",
            "a": "a",
        },
    )
    box, _ = await prepare(manager, source, setup_command="true")
    tree = [p for p in landed(manager.sandboxes[0]) if not p.startswith(".git/")]
    assert tree == ["a"]
    assert (await git(box, "log --all --name-only --format=")).splitlines() == ["a"]


async def test_prepare_excludes_what_a_setup_step_leaves_behind(
    tmp_path: Path, manager: FakeManager
) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n"})
    leave_debris = (
        "mkdir -p .venv/bin src/pkg.egg-info .pytest_cache sub/__pycache__ && "
        "touch .venv/bin/python src/pkg.egg-info/PKG-INFO .pytest_cache/README.md "
        "sub/__pycache__/x.pyc"
    )
    box, _ = await prepare(manager, source, setup_command=leave_debris)
    assert await git(box, "status --porcelain") == ""
    exclude = (await box.read_file(".git/info/exclude")).decode()
    patterns = [ln for ln in exclude.splitlines() if ln and not ln.startswith("#")]
    assert patterns == [".venv/", "__pycache__/", "*.egg-info/", ".pytest_cache/"]


async def test_prepare_runs_setup_with_the_network_on_and_then_cuts_it(
    tmp_path: Path, manager: FakeManager
) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n"})
    await prepare(manager, source, setup_command="echo SETUP")
    assert manager.log[0] == "create t1 img:1 network=True"
    commit = next(i for i, entry in enumerate(manager.log) if " commit " in entry)
    setup = manager.log.index("exec echo SETUP")
    cut = manager.log.index("set_network False")
    assert commit < setup < cut
    assert cut == len(manager.log) - 1
    # The caller owns the sandbox from here on.
    assert not any(entry.startswith("cleanup") for entry in manager.log)


async def test_setup_output_is_not_part_of_the_base_commit(
    tmp_path: Path, manager: FakeManager
) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n"})
    box, base_sha = await prepare(manager, source, setup_command="echo made > built.txt")
    assert await git(box, "ls-tree -r --name-only refs/tags/base") == "a.txt"
    assert base_sha == await git(box, "rev-parse HEAD")


async def test_no_pyproject_and_no_setup_command_means_no_setup(
    tmp_path: Path, manager: FakeManager
) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n"})
    await prepare(manager, source)
    assert not any("venv" in c for c in manager.commands)
    assert manager.log[-1] == "set_network False"


async def test_an_empty_setup_command_skips_setup_even_with_a_pyproject(
    tmp_path: Path, manager: FakeManager
) -> None:
    source = pyproject_in(tmp_path, PROJECT)
    await prepare(manager, source, setup_command="  ")
    assert not any("venv" in c for c in manager.commands)


async def test_a_pyproject_without_a_setup_command_gets_the_default_venv_setup(
    tmp_path: Path,
) -> None:
    source = pyproject_in(tmp_path, PROJECT)
    expected = default_setup_command(source)
    assert expected is not None
    stubbed = FakeManager(stub={"python -m venv": OK})  # the unit tests have no network
    try:
        await prepare(stubbed, source)
        assert expected in stubbed.commands
    finally:
        await stubbed.cleanup_task("t1")


async def test_an_explicit_setup_command_replaces_the_default(
    tmp_path: Path, manager: FakeManager
) -> None:
    source = pyproject_in(tmp_path, PROJECT)
    await prepare(manager, source, setup_command="echo mine")
    assert "echo mine" in manager.commands
    assert not any("venv" in c for c in manager.commands)


async def test_a_failing_setup_raises_with_the_command_and_the_output_tail(
    tmp_path: Path, manager: FakeManager
) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n"})
    noisy = (
        "for i in $(seq 1 200); do echo out-line-$i; done; "
        "for i in $(seq 1 200); do echo err-line-$i >&2; done; exit 7"
    )
    with pytest.raises(SetupCommandError) as caught:
        await prepare(manager, source, setup_command=noisy)
    message = str(caught.value)
    assert "exit code 7" in message and "seq 1 200" in message
    # The end of both streams is there, the start is not.
    for stream in ("out", "err"):
        assert re.search(rf"^\s*{stream}-line-200$", message, re.M)
        assert not re.search(rf"^\s*{stream}-line-1$", message, re.M)
    assert len(message) < 8000
    assert isinstance(caught.value, WorkspaceError)
    assert caught.value.exit_code == 7
    # The sandbox is cleaned up, and the network is not cut for a workspace nobody gets.
    assert manager.log[-1] == "cleanup t1"
    assert "set_network False" not in manager.log
    assert not manager.sandboxes[0].root.exists()


async def test_a_setup_that_times_out_says_so(tmp_path: Path, manager: FakeManager) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n"})
    with pytest.raises(SetupCommandError, match=r"timed out after 1 s") as caught:
        await prepare(manager, source, setup_command="echo started; sleep 30", setup_timeout_s=1)
    assert "started" in str(caught.value)
    assert manager.log[-1] == "cleanup t1"


async def test_the_sandbox_is_cleaned_up_when_the_copy_fails(
    tmp_path: Path, manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n"})

    async def broken_write(self: FakeSandbox, path: str, data: bytes) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(FakeSandbox, "write_file", broken_write)
    with pytest.raises(OSError, match="No space left"):
        await prepare(manager, source, setup_command="true")
    assert manager.log[-1] == "cleanup t1"


async def test_the_sandbox_is_cleaned_up_when_git_fails(tmp_path: Path) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n"})
    bad = ExecResult(
        exit_code=128, stdout="", stderr="fatal: no git here", timed_out=False, duration_ms=1
    )
    broken = FakeManager(stub={"git init": bad})
    with pytest.raises(WorkspaceError, match="fatal: no git here"):
        await prepare(broken, source, setup_command="true")
    assert broken.log[-1] == "cleanup t1"


async def test_a_git_step_that_times_out_says_so(tmp_path: Path) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n"})
    stuck = ExecResult(exit_code=137, stdout="", stderr="", timed_out=True, duration_ms=1)
    broken = FakeManager(stub={"git init": stuck})
    with pytest.raises(WorkspaceError, match=r"git init failed in the sandbox \(timed out after"):
        await prepare(broken, source, setup_command="true")
    assert broken.log[-1] == "cleanup t1"


async def test_the_sandbox_is_cleaned_up_when_the_network_cannot_be_cut(
    tmp_path: Path, manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n"})

    async def refuse(sandbox_id: str, enabled: bool) -> None:
        raise RuntimeError("cannot disconnect")

    monkeypatch.setattr(manager, "set_network", refuse)
    with pytest.raises(RuntimeError, match="cannot disconnect"):
        await prepare(manager, source, setup_command="true")
    assert manager.log[-1] == "cleanup t1"


async def test_a_failed_create_still_clears_whatever_it_left(
    tmp_path: Path, manager: FakeManager
) -> None:
    # Docker can make the volume and then fail to start the container.
    source = make_source(tmp_path / "src", {"a.txt": "a\n"})
    manager.fail_create = True
    with pytest.raises(RuntimeError, match="docker is down"):
        await prepare(manager, source, setup_command="true")
    assert manager.log[-1] == "cleanup t1"


async def test_a_bad_source_folder_fails_before_any_sandbox_exists(
    tmp_path: Path, manager: FakeManager
) -> None:
    with pytest.raises(WorkspaceError, match="not a directory"):
        await prepare(manager, tmp_path / "missing", setup_command="true")
    assert manager.log == []


async def test_a_cleanup_that_fails_does_not_hide_the_first_error(
    tmp_path: Path,
    manager: FakeManager,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n"})

    async def broken_cleanup(task_id: str) -> None:
        raise RuntimeError("cleanup broke")

    monkeypatch.setattr(manager, "cleanup_task", broken_cleanup)
    with (
        caplog.at_level(logging.WARNING, logger="longhaul.agent.workspace"),
        pytest.raises(SetupCommandError),
    ):
        await prepare(manager, source, setup_command="exit 1")
    assert "cleanup broke" in caplog.text
