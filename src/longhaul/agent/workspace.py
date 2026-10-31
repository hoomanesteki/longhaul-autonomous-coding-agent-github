"""Prepare a local folder as a sandbox workspace for `longhaul run-local`.

The folder is copied into a new sandbox through Sandbox.write_file, committed there as the
base commit and tagged `base`, which is what the git_diff tool compares against. The setup
command then runs with the network on, and the network is cut before the agent starts.

What is not copied:

* Build debris and the source's own history: .git, .venv, __pycache__, *.pyc, .pytest_cache,
  *.egg-info, two more tool caches and .DS_Store (BUILTIN_PATTERNS).
* Files that usually hold secrets: .env and .env.*, except .env.example and its siblings.
  CLAUDE.md rule 7 keeps secrets out of the sandbox, and a file in /workspace is as readable
  to the model as a variable in its environment.
* Whatever the source folder's .longhaulignore lists, in .gitignore syntax. The fixture repos
  use it to keep REFERENCE.patch and ISSUE.md away from the model. The ignore file is not
  copied either, because it names what it hides.
* Symlinks and special files. The sandbox's file API cannot create links, and a link that
  leaves the folder would hand the sandbox whatever it points at.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Protocol

from longhaul.contracts import ExecResult, Sandbox, SandboxLimits, SandboxManager

log = logging.getLogger(__name__)

IGNORE_FILE = ".longhaulignore"
BASE_TAG = "base"

GIT_TIMEOUT_S = 120
CHMOD_BATCH = 50  # paths per command, so a big tree stays under the argument size limit
# Long enough for a cold pip install of a real project over a slow link.
DEFAULT_SETUP_TIMEOUT_S = 900

OUTPUT_TAIL_LINES = 40
OUTPUT_TAIL_CHARS = 4_000
MAX_LISTED = 10

# Skipped whatever the ignore file says: a negation there cannot bring these back.
BUILTIN_PATTERNS = """\
.git
.venv
__pycache__
.pytest_cache
.mypy_cache
.ruff_cache
*.egg-info
*.pyc
.DS_Store
.env
.env.*
!.env.example
!.env.sample
!.env.template
"""

# Written before the setup command runs, so what it builds (the venv, the egg-info of an
# editable install) and what the tests leave behind never shows up in `git status` or in
# the diff that the agent and the CLI print.
GIT_EXCLUDE = """\
# Added by longhaul when the workspace was prepared.
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
"""

# Given on the command line so the base commit does not depend on the image's git config.
GIT_IDENTITY = "-c user.name=longhaul -c user.email=longhaul@localhost"


class WorkspaceError(Exception):
    """The workspace could not be prepared. The message says why."""


class SetupCommandError(WorkspaceError):
    """The setup command failed or ran out of time. The message ends with the output tail."""

    def __init__(self, command: str, result: ExecResult, timeout_s: int) -> None:
        self.command = command
        self.exit_code = result.exit_code
        self.timed_out = result.timed_out
        what = (
            f"timed out after {timeout_s} s"
            if result.timed_out
            else f"failed with exit code {result.exit_code}"
        )
        super().__init__(f"setup command {what}: {command}{_output_tail(result)}")


class WorkspaceManager(SandboxManager, Protocol):
    """A sandbox manager that can also remove everything a task left behind.

    DockerSandboxManager has it. Sandbox.destroy() alone would leave the workspace volume,
    with the virtualenv in it, on the machine.
    """

    async def cleanup_task(self, task_id: str) -> None: ...


class LocalWorkspace(NamedTuple):
    """A prepared sandbox. The caller owns it and removes it with manager.cleanup_task()."""

    sandbox: Sandbox
    base_sha: str


# --- ignore patterns -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern[str]
    negated: bool
    dir_only: bool
    anchored: bool  # matched against the whole relative path instead of the last name


def _class_end(part: str, start: int) -> int | None:
    """Index of the `]` that closes the `[` at part[start], or None when it never closes."""
    i = start + 1
    if i < len(part) and part[i] in "!^":
        i += 1
    if i < len(part) and part[i] == "]":  # a `]` first in the class is one of its members
        i += 1
    end = part.find("]", i)
    return None if end == -1 else end


def _char_class(body: str) -> str:
    """The regex for the inside of `[...]`: a leading `!` or `^` negates and `a-z` is a range."""
    negate = body[:1] in ("!", "^")
    members = body[1:] if negate else body
    escaped = "".join("-" if ch == "-" else re.escape(ch) for ch in members)
    # A negated class must not match the slash either, as in git.
    return f"[^/{escaped}]" if negate else f"[{escaped}]"


def _component_regex(part: str) -> str:
    """The regex for one path component of a pattern: `*`, `?`, `[...]` and `\\x`."""
    out: list[str] = []
    i = 0
    while i < len(part):
        ch = part[i]
        if ch == "\\" and i + 1 < len(part):
            out.append(re.escape(part[i + 1]))
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            while i < len(part) and part[i] == "*":  # a run of stars is one star
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[" and (end := _class_end(part, i)) is not None:
            out.append(_char_class(part[i + 1 : end]))
            i = end + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def _glob_regex(glob: str) -> str:
    """A component that is exactly `**` crosses directories; anywhere else a star does not."""
    parts = glob.split("/")
    out: list[str] = []
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            out.append(".*" if last else "(?:[^/]+/)*")
        else:
            out.append(_component_regex(part) + ("" if last else "/"))
    return "".join(out)


def _parse_rule(pattern: str) -> _Rule | None:
    negated = pattern.startswith("!")
    if negated:
        pattern = pattern[1:]
    dir_only = pattern.endswith("/")
    pattern = pattern.removesuffix("/")
    # A slash anywhere (a leading one included) ties the pattern to the root of the folder.
    anchored = "/" in pattern
    pattern = pattern.removeprefix("/")
    if not pattern:
        return None
    regex = re.compile(_glob_regex(pattern), re.DOTALL)
    return _Rule(regex, negated=negated, dir_only=dir_only, anchored=anchored)


class IgnoreRules:
    """Ignore patterns in .gitignore syntax, evaluated the way git does.

    Blank lines and lines that start with # are skipped. A pattern without a slash matches
    a name at any depth, one with a slash matches from the root, a trailing slash means
    directories only, `*` and `?` stay inside one name, `**` between slashes crosses
    directories, and `!` brings back what an earlier pattern matched. The last matching
    pattern wins. Anything below an ignored directory is ignored, and a negation cannot
    reopen it.
    """

    def __init__(self, rules: list[_Rule]) -> None:
        self._rules = rules

    @classmethod
    def parse(cls, text: str, *, origin: str = "ignore patterns") -> IgnoreRules:
        rules: list[_Rule] = []
        for number, line in enumerate(text.split("\n"), start=1):
            pattern = line.rstrip()  # also drops the \r of a file saved on Windows
            if not pattern or pattern.startswith("#"):
                continue
            try:
                rule = _parse_rule(pattern)
            except re.error as exc:
                # Skipping a pattern would copy the files it was meant to hide.
                raise WorkspaceError(
                    f"{origin} line {number}: cannot read pattern {pattern!r}: {exc}"
                ) from exc
            if rule is not None:
                rules.append(rule)
        return cls(rules)

    def ignores(self, path: str, *, is_dir: bool = False) -> bool:
        """True when `path` is ignored or sits below an ignored directory.

        `path` is posix and relative to the source folder.
        """
        parts = path.split("/")
        for depth in range(1, len(parts)):
            if self._matches("/".join(parts[:depth]), is_dir=True):
                return True
        return self._matches(path, is_dir=is_dir)

    def _matches(self, path: str, *, is_dir: bool) -> bool:
        name = path.rsplit("/", 1)[-1]
        ignored = False
        for rule in self._rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.fullmatch(path if rule.anchored else name):
                ignored = not rule.negated
        return ignored


BUILTIN_RULES = IgnoreRules.parse(BUILTIN_PATTERNS, origin="built-in patterns")


def load_ignore_rules(path: Path) -> IgnoreRules:
    """The rules in the file at `path`, or no rules when there is no such file."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return IgnoreRules.parse("")
    except (OSError, UnicodeDecodeError) as exc:
        # Carrying on without it would copy the files it was meant to hide.
        raise WorkspaceError(f"cannot read {path.name}: {exc}") from exc
    return IgnoreRules.parse(text, origin=path.name)


# --- copying the folder ----------------------------------------------------------------------


@dataclass(frozen=True)
class SourceFile:
    path: str  # posix, relative to the source folder and to /workspace
    executable: bool


def _walk_failed(error: OSError) -> None:
    # os.walk skips a folder it cannot list unless told otherwise. A silent gap in the
    # copy would look like a repo with missing files.
    raise WorkspaceError(f"cannot read {error.filename}: {error.strerror}") from error


def collect_source_files(source_dir: Path, ignore_file: str = IGNORE_FILE) -> list[SourceFile]:
    """The files to copy, in path order.

    Raises WorkspaceError when `source_dir` is not a folder or nothing is left to copy.
    """
    if not source_dir.is_dir():
        raise WorkspaceError(f"not a directory: {source_dir}")
    user_rules = load_ignore_rules(source_dir / ignore_file)
    own_name = Path(ignore_file).as_posix()

    def skipped(rel: str, *, is_dir: bool) -> bool:
        return (
            rel == own_name
            or BUILTIN_RULES.ignores(rel, is_dir=is_dir)
            or user_rules.ignores(rel, is_dir=is_dir)
        )

    files: list[SourceFile] = []
    not_regular: list[str] = []
    for current, dirs, names in os.walk(source_dir, onerror=_walk_failed):
        here = Path(current)
        rel_here = here.relative_to(source_dir)
        kept: list[str] = []
        for name in dirs:
            rel = (rel_here / name).as_posix()
            if skipped(rel, is_dir=True):
                continue
            if (here / name).is_symlink():  # os.walk lists a link to a folder with the folders
                not_regular.append(rel)
            else:
                kept.append(name)
        dirs[:] = kept  # walk only into the folders that are kept
        for name in names:
            rel = (rel_here / name).as_posix()
            if skipped(rel, is_dir=False):
                continue
            try:
                mode = os.lstat(here / name).st_mode
            except OSError as exc:
                raise WorkspaceError(f"cannot read {rel}: {exc.strerror}") from exc
            if stat.S_ISREG(mode):
                files.append(SourceFile(rel, executable=bool(mode & stat.S_IXUSR)))
            else:
                not_regular.append(rel)
    _report_not_copied(not_regular)
    if not files:
        raise WorkspaceError(
            f"no files to copy from {source_dir}: it is empty, or every file is skipped"
        )
    return sorted(files, key=lambda f: f.path)


def _report_not_copied(paths: list[str]) -> None:
    if not paths:
        return
    shown = sorted(paths)[:MAX_LISTED]
    more = f" and {len(paths) - len(shown)} more" if len(paths) > len(shown) else ""
    log.warning(
        "not copied, because links and special files cannot go into the sandbox: %s%s",
        ", ".join(shown),
        more,
    )


async def copy_source(sandbox: Sandbox, source_dir: Path, files: list[SourceFile]) -> None:
    """Write `files` into the sandbox, then give back the executable bit.

    write_file creates every file as 0644, and git records the mode in the base commit,
    so a script the repo runs directly would otherwise stop working.
    """
    for item in files:
        try:
            data = (source_dir / item.path).read_bytes()
        except OSError as exc:
            raise WorkspaceError(f"cannot read {item.path}: {exc.strerror or exc}") from exc
        await sandbox.write_file(item.path, data)
    await _mark_executable(sandbox, [f.path for f in files if f.executable])


async def _mark_executable(sandbox: Sandbox, paths: list[str]) -> None:
    for start in range(0, len(paths), CHMOD_BATCH):
        batch = " ".join(shlex.quote(p) for p in paths[start : start + CHMOD_BATCH])
        # `--` comes first because a name can start with a dash. BSD chmod, which the
        # fake sandbox uses on a Mac, stops reading options at the mode.
        await _run(sandbox, f"chmod -- +x {batch}", "chmod")


# --- running commands in the sandbox ---------------------------------------------------------


def _output_tail(result: ExecResult) -> str:
    """The end of stderr and stdout, which is where a failed install says why."""
    sections: list[str] = []
    for label, text in (("stderr", result.stderr), ("stdout", result.stdout)):
        lines = text.rstrip().splitlines()[-OUTPUT_TAIL_LINES:]
        body = "\n".join(lines)[-OUTPUT_TAIL_CHARS:]
        if body:
            sections.append(f"\n--- end of {label} ---\n{body}")
    return "".join(sections)


async def _run(sandbox: Sandbox, command: str, what: str, *, timeout_s: int = GIT_TIMEOUT_S) -> str:
    result = await sandbox.exec(command, timeout_s=timeout_s)
    if result.exit_code != 0 or result.timed_out:
        why = f"exit code {result.exit_code}"
        if result.timed_out:
            why = f"timed out after {timeout_s} s"
        raise WorkspaceError(f"{what} failed in the sandbox ({why}){_output_tail(result)}")
    return result.stdout


async def _commit_base(sandbox: Sandbox) -> str:
    """Commit the copied files as the base commit, tag it, and return its sha."""
    await _run(sandbox, "git init -q -b main", "git init")
    # Before the commit and the setup, so neither can add the debris they make.
    await sandbox.write_file(".git/info/exclude", GIT_EXCLUDE.encode())
    commit = (
        f"git add -A && git {GIT_IDENTITY} commit -q --no-verify -m 'base snapshot' "
        f"&& git tag {BASE_TAG}"
    )
    await _run(sandbox, commit, "git commit")
    return (await _run(sandbox, "git rev-parse HEAD", "git rev-parse")).strip()


# --- setup -----------------------------------------------------------------------------------


def _has_dev_extra(pyproject: Path) -> bool:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    project = data.get("project")
    extras = project.get("optional-dependencies") if isinstance(project, dict) else None
    # Extra names are compared the way pip does: case and the separators -, _ and . do not count.
    return isinstance(extras, dict) and any(
        re.sub(r"[-_.]+", "-", str(name)).lower() == "dev" for name in extras
    )


def default_setup_command(source_dir: Path) -> str | None:
    """What to run when the repo gives no setup command: a venv with the project and pytest.

    The sandbox's root filesystem is read-only, so packages go into /workspace/.venv, whose
    bin directory leads PATH. Without a pyproject.toml there is nothing to install and the
    answer is None. A project with no `dev` extra gets pytest installed next to it.
    """
    pyproject = source_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    target = "-e '.[dev]'" if _has_dev_extra(pyproject) else "-e . pytest"
    # No pip cache: HOME is a 64 MB tmpfs, which a few large wheels would fill.
    pip = ".venv/bin/pip install --disable-pip-version-check --no-cache-dir -q"
    return f"python -m venv .venv && {pip} {target}"


async def _run_setup(sandbox: Sandbox, command: str, timeout_s: int) -> None:
    result = await sandbox.exec(command, timeout_s=timeout_s)
    if result.exit_code != 0 or result.timed_out:
        raise SetupCommandError(command, result, timeout_s)


async def _remove(manager: WorkspaceManager, task_id: str) -> None:
    """Remove the container and the volume.

    A failure here is logged, so it cannot replace the error that led to the cleanup.
    """
    try:
        await manager.cleanup_task(task_id)
    except Exception as exc:
        log.warning("could not remove the sandbox of task %s: %s", task_id, exc)


async def prepare_local_workspace(
    manager: WorkspaceManager,
    source_dir: Path,
    *,
    task_id: str,
    image: str,
    limits: SandboxLimits,
    setup_command: str | None = None,
    ignore_file: str = IGNORE_FILE,
    setup_timeout_s: int = DEFAULT_SETUP_TIMEOUT_S,
) -> LocalWorkspace:
    """Copy `source_dir` into a new sandbox and get it ready for the agent.

    The sandbox starts with the network on. The files are copied, committed as the base
    commit and tagged `base`, the setup command runs, and the network is cut. `setup_command`
    None means the default (see default_setup_command), an empty string means no setup.

    On any failure, including a cancelled task, the container and the volume are removed
    before the error propagates. On success the caller owns the sandbox and removes it with
    manager.cleanup_task(task_id).
    """
    files = collect_source_files(source_dir, ignore_file)  # fails before a container exists
    command = setup_command.strip() if setup_command is not None else None
    if command is None:
        command = default_setup_command(source_dir)
    try:
        sandbox = await manager.create(task_id, image, limits, network=True)
        await copy_source(sandbox, source_dir, files)
        base_sha = await _commit_base(sandbox)
        if command:
            await _run_setup(sandbox, command, setup_timeout_s)
        await manager.set_network(sandbox.id, False)
    except BaseException:
        await _remove(manager, task_id)
        raise
    return LocalWorkspace(sandbox, base_sha)
