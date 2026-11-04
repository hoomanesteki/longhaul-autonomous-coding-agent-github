"""A plain folder as a repo with one commit, the same on every machine (LH-206).

prepare_workspace turns a `local:` folder into a repo before init_task clones it. A retried
prepare_workspace has to land on the base it had before, so the commit id may depend on the
files and on nothing else. These tests run git on the host, with the user's own git config
replaced by hostile settings where that matters.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from longhaul.agent.workspace import WorkspaceError, collect_source_files
from longhaul.checkpoints.snapshot import MESSAGE, snapshot_folder

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"


def make_source(root: Path, files: dict[str, str | bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode() if isinstance(content, str) else content)
    return root


def host_git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return done.stdout.strip()


async def snap(source: Path, dest: Path) -> str:
    return await snapshot_folder(source, collect_source_files(source), dest)


def tree(repo: Path) -> dict[str, str]:
    """{path: "mode type"} of the commit, as git ls-tree prints it (the blob id left out)."""
    found: dict[str, str] = {}
    for line in host_git(repo, "ls-tree", "-r", "HEAD").splitlines():
        meta, path = line.split("\t", 1)
        found[path] = meta.rsplit(" ", 1)[0]
    return found


async def test_the_same_files_give_the_same_commit_in_any_place(tmp_path: Path) -> None:
    first = await snap(CALC, tmp_path / "one")
    second = await snap(CALC, tmp_path / "deeper" / "than" / "two")
    assert first == second
    assert len(first) == 40
    assert host_git(tmp_path / "one", "rev-parse", "HEAD") == first


async def test_the_repo_has_one_commit_on_main_with_a_fixed_author(tmp_path: Path) -> None:
    sha = await snap(CALC, tmp_path / "repo")
    repo = tmp_path / "repo"
    assert host_git(repo, "rev-list", "--count", "HEAD") == "1"
    assert host_git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert host_git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%aI|%s", sha) == (
        "longhaul-fixture <fixture@localhost>|longhaul-fixture <fixture@localhost>|"
        f"2000-01-01T00:00:00Z|{MESSAGE}"
    )
    assert host_git(repo, "status", "--porcelain") == ""


async def test_the_commit_does_not_depend_on_file_times_or_permissions(tmp_path: Path) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "a\n", "sub/b.txt": "b\n"})
    first = await snap(source, tmp_path / "one")

    os.utime(source / "a.txt", (1_000_000_000, 1_000_000_000))
    (source / "sub" / "b.txt").chmod(0o600)  # only the execute bit may matter
    second = await snap(source, tmp_path / "two")

    assert first == second


async def test_the_commit_does_not_depend_on_the_users_git_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(tmp_path / "src", {"a.txt": "line\r\nline\r\n", "b.log": "kept\n"})
    plain = await snap(source, tmp_path / "plain")

    hooks = tmp_path / "template" / "hooks"
    hooks.mkdir(parents=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'a hook ran' > hook-ran.txt\nexit 1\n")
    hook.chmod(0o755)
    ignore = tmp_path / "global-ignore"
    ignore.write_text("*.log\n")
    config = tmp_path / "gitconfig"
    config.write_text(
        "[user]\n\tname = Someone Else\n\temail = else@example.com\n"
        "[core]\n\tautocrlf = true\n\texcludesFile = " + str(ignore) + "\n"
        "[commit]\n\tgpgsign = true\n"
        "[init]\n\ttemplateDir = " + str(tmp_path / "template") + "\n\tdefaultBranch = trunk\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # A git hook exports these, and they would point git at the repo that is running the hook.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "somewhere-else"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Not Us")

    hostile = await snap(source, tmp_path / "hostile")

    assert hostile == plain
    assert not (tmp_path / "hostile" / "hook-ran.txt").exists()
    # The empty template leaves no hooks folder at all, whatever init.templateDir says.
    assert not (tmp_path / "hostile" / ".git" / "hooks").exists()


async def test_line_endings_and_binary_bytes_are_committed_as_they_are(tmp_path: Path) -> None:
    blob = bytes(range(256))
    source = make_source(tmp_path / "src", {"crlf.txt": b"a\r\nb\r\n", "data.bin": blob})
    repo = tmp_path / "repo"
    await snap(source, repo)

    shown = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", "HEAD:crlf.txt"],
        capture_output=True,
        check=True,
    ).stdout
    assert shown == b"a\r\nb\r\n"
    data = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", "HEAD:data.bin"],
        capture_output=True,
        check=True,
    ).stdout
    assert data == blob


async def test_other_content_gives_another_commit(tmp_path: Path) -> None:
    one = await snap(make_source(tmp_path / "a", {"f.txt": "one\n"}), tmp_path / "ra")
    two = await snap(make_source(tmp_path / "b", {"f.txt": "two\n"}), tmp_path / "rb")
    renamed = await snap(make_source(tmp_path / "c", {"g.txt": "one\n"}), tmp_path / "rc")
    assert len({one, two, renamed}) == 3


async def test_the_execute_bit_is_part_of_the_commit(tmp_path: Path) -> None:
    source = make_source(tmp_path / "src", {"run.sh": "#!/bin/sh\n", "plain.txt": "x\n"})
    plain = await snap(source, tmp_path / "plain")

    (source / "run.sh").chmod(0o755)
    executable = await snap(source, tmp_path / "exec")

    assert plain != executable
    assert tree(tmp_path / "exec")["run.sh"].startswith("100755 blob")
    assert tree(tmp_path / "exec")["plain.txt"].startswith("100644 blob")
    assert tree(tmp_path / "plain")["run.sh"].startswith("100644 blob")


async def test_every_file_the_copy_rules_kept_is_committed_even_if_gitignore_names_it(
    tmp_path: Path,
) -> None:
    # A restore deletes whatever the commit lacks, so a file that the sandbox of a local run
    # would have must be in the base commit.
    source = make_source(
        tmp_path / "src",
        {".gitignore": "generated/\n*.cfg\n", "generated/out.py": "x = 1\n", "app.cfg": "a\n"},
    )
    await snap(source, tmp_path / "repo")
    assert set(tree(tmp_path / "repo")) == {".gitignore", "generated/out.py", "app.cfg"}


async def test_what_the_copy_rules_skip_is_not_committed(tmp_path: Path) -> None:
    source = make_source(
        tmp_path / "src",
        {
            ".longhaulignore": "REFERENCE.patch\nnotes/\n",
            "REFERENCE.patch": "the answer\n",
            "notes/todo.md": "x\n",
            ".env": "SECRET=1\n",
            ".env.example": "SECRET=\n",
            "__pycache__/m.pyc": "x",
            "keep.py": "x = 1\n",
        },
    )
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    await snap(source, tmp_path / "repo")

    assert set(tree(tmp_path / "repo")) == {".env.example", "keep.py"}


async def test_names_with_spaces_and_non_ascii_characters_survive(tmp_path: Path) -> None:
    source = make_source(tmp_path / "src", {"a file.txt": "x\n", "café/über.py": "y\n"})
    await snap(source, tmp_path / "repo")
    names = host_git(
        tmp_path / "repo", "-c", "core.quotepath=off", "ls-tree", "-r", "--name-only", "HEAD"
    )
    assert sorted(names.splitlines()) == ["a file.txt", "café/über.py"]


async def test_a_destination_that_exists_is_refused(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    with pytest.raises(FileExistsError):
        await snap(CALC, tmp_path / "taken")


def deny_reading(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make the copy of the file called `name` fail the way an unreadable file does.

    A chmod of 0 stops every user but root, and root is who runs these tests in some
    containers. Raising the error from the copy takes the same path in the code under test and
    works for any user.
    """
    real_copy = shutil.copyfile

    def copyfile(src: Any, dst: Any, **kwargs: Any) -> Any:
        if Path(src).name == name:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(src))
        return real_copy(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", copyfile)


async def test_a_file_that_cannot_be_read_is_a_workspace_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(tmp_path / "src", {"ok.txt": "x\n", "locked.txt": "y\n"})
    files = collect_source_files(source)
    deny_reading(monkeypatch, "locked.txt")

    with pytest.raises(WorkspaceError, match=r"cannot read locked\.txt: Permission denied"):
        await snapshot_folder(source, files, tmp_path / "repo")


async def test_a_failure_leaves_no_half_built_repo_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Files are copied in path order, so first.txt is already in the new folder when the error
    # comes. That is the half-built repo the failure has to remove.
    source = make_source(tmp_path / "src", {"first.txt": "x\n", "locked.txt": "y\n"})
    files = collect_source_files(source)
    deny_reading(monkeypatch, "locked.txt")

    with pytest.raises(WorkspaceError):
        await snapshot_folder(source, files, tmp_path / "repo")

    assert not (tmp_path / "repo").exists()
