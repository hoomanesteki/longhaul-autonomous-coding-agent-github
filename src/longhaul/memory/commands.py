"""Verified commands: how to run a repo's tests and linter, found and then checked (LH-603).

An agent that guesses the test command wastes turns, and one that guesses wrong reports a pass
it never saw. So prepare_workspace works the commands out before the first turn, in three steps:

1. `read_repo_files` reads the few files that name them.
2. `find_candidates` turns their text into commands. The sources are the pytest config in
   pyproject.toml, tox.ini, the `test` and `lint` targets of a Makefile, and the lines of CI
   steps that run pytest, ruff or tox.
3. `verify_commands` runs each candidate once in the sandbox and keeps the ones that worked.

A command worked when it ran to the end and printed a summary that a person would call the
result: the counts line of pytest, the verdict of ruff or the verdict of tox. Failing tests do
not disqualify it, because the issue may be a failing test. A target that does not exist, a tool
that is not installed, a run that found no tests and a run that timed out print no such summary,
and those commands are dropped. What is kept is the text of the command, which the caller
stores as an approved item of the default branch (ADR-0010).

Candidates come from the repo, so they are limited before anything runs them: a CI line is taken
whole or not at all, it must be one plain command without pipes, redirects, substitutions or
variables, and it must be short. A line that depends on its surroundings, such as `cd sub &&
pytest` or `pytest ${{ matrix.args }}`, would not mean the same thing in the sandbox.
"""

from __future__ import annotations

import logging
import re
import time
import tomllib
from collections.abc import Callable, Mapping

import yaml

from longhaul.contracts import ExecResult, Sandbox
from longhaul.tools.exec import parse_pytest_summary

log = logging.getLogger(__name__)

__all__ = [
    "COMMAND_TIMEOUT_S",
    "DISCOVERY_BUDGET_S",
    "MAX_CANDIDATES",
    "MAX_COMMAND_CHARS",
    "discover_commands",
    "find_candidates",
    "read_repo_files",
    "summary_of",
    "verify_commands",
]

# How many commands are tried at most. A CI file can name a dozen, and each run costs time.
MAX_CANDIDATES = 6
# A longer line is not a command anyone types, and the text goes into prompts later.
MAX_COMMAND_CHARS = 200
# The longest file that is read. A bigger one is not a config file.
MAX_FILE_BYTES = 200_000
MAX_WORKFLOW_FILES = 20
# What one candidate may run for, and what all of them may run for together. The caller sits in
# an activity with a limit of its own, which these and the setup have to fit into.
COMMAND_TIMEOUT_S = 180
DISCOVERY_BUDGET_S = 300
LIST_TIMEOUT_S = 30

PYTEST_COMMAND = "pytest -q"
WORKFLOWS_DIR = ".github/workflows"
_FILES = ("pyproject.toml", "tox.ini", "Makefile")
# The only names that are read from the workflows folder: plain file names with a YAML suffix.
_WORKFLOW_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml")
_MAKE_TARGETS = ("test", "lint")

# One CI line is one command: pytest, ruff or tox, maybe behind a runner, with plain arguments.
# The characters left out of the arguments are the ones that chain, redirect, substitute or
# start a comment.
_CI_COMMAND = re.compile(
    r"(?:(?:uv run |poetry run |python3? -m ){0,2})(?:pytest|ruff|tox)(?: [^&|;<>$`(){}\\#]*)?"
)

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# A pytest line counts as a run when at least one test reached a verdict. "N deselected" alone is
# what `-k` or `-m` prints when nothing matched, and a run with only skips tested nothing either.
_PYTEST_VERDICT = re.compile(
    r"\d+ (?:passed|failed|errors?|xfailed|xpassed|subtests (?:passed|failed))"
)
_RUFF_COUNT = r"\d+ files? (?:would be reformatted|already formatted|left unchanged)"
# ruff check ends with one of the first two lines, and ruff format --check with the third.
_RUFF = re.compile(rf"All checks passed!|Found \d+ errors?\.|{_RUFF_COUNT}(?:, {_RUFF_COUNT})?")
# One line for each environment of tox 4 (and tox 3), and then a verdict for all of them.
_TOX_ENV = re.compile(r"[\w.-]+: (?:OK|FAIL)\b.*|[\w.-]+: commands (?:succeeded|failed)")
_TOX_VERDICT = re.compile(r"congratulations :\)(?: \(.*\))?|evaluation failed :\(.*")


# ---------- step 2: the candidates ----------
def find_candidates(files: Mapping[str, str]) -> list[str]:
    """The commands that the text of the repo's files suggests, once each and in a fixed order.

    `files` maps a path in the repo to its text, as read_repo_files gives it. The order is
    pyproject.toml, tox.ini, the Makefile and then the CI files by name, so the same repo
    always yields the same list.
    """
    found: list[str] = []
    found += _from_pyproject(files.get("pyproject.toml"))
    if "tox.ini" in files:
        found.append("tox")
    found += _from_makefile(files.get("Makefile"))
    for path in sorted(files):
        if path.startswith(WORKFLOWS_DIR + "/"):
            found += _from_workflow(files[path])
    return list(dict.fromkeys(found))[:MAX_CANDIDATES]


def _from_pyproject(text: str | None) -> list[str]:
    if text is None:
        return []
    try:
        tool = tomllib.loads(text).get("tool", {})
    except tomllib.TOMLDecodeError:
        return []
    # [tool.pytest.ini_options] is the long-standing table and [tool.pytest] the native one.
    return [PYTEST_COMMAND] if isinstance(tool, dict) and "pytest" in tool else []


def _from_makefile(text: str | None) -> list[str]:
    if text is None:
        return []
    # A target starts in the first column. `test := 1` is a variable and a tab starts a recipe.
    return [
        f"make {name}"
        for name in _MAKE_TARGETS
        if re.search(rf"^{name}[ \t]*:(?!=)", text, re.MULTILINE)
    ]


def _from_workflow(text: str) -> list[str]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else None
    found: list[str] = []
    for job in jobs.values() if isinstance(jobs, dict) else []:
        steps = job.get("steps") if isinstance(job, dict) else None
        for step in steps if isinstance(steps, list) else []:
            run = step.get("run") if isinstance(step, dict) else None
            if isinstance(run, str):
                found += _commands_in(run)
    return found


def _commands_in(script: str) -> list[str]:
    # A backslash at the end of a line continues it, and spacing is not part of a command.
    lines = (" ".join(line.split()) for line in script.replace("\\\n", " ").splitlines())
    return [
        line
        for line in lines
        if len(line) <= MAX_COMMAND_CHARS and _CI_COMMAND.fullmatch(line) is not None
    ]


# ---------- step 1: the files ----------
async def read_repo_files(sandbox: Sandbox) -> dict[str, str]:
    """The text of the files that name commands, by path. A file that is not there is left out.

    A file that is not UTF-8 text, or is too big to be a config file, is left out as well.
    """
    paths = [*_FILES, *await _workflow_paths(sandbox)]
    files: dict[str, str] = {}
    for path in paths:
        text = await _read_text(sandbox, path)
        if text is not None:
            files[path] = text
    return files


async def _workflow_paths(sandbox: Sandbox) -> list[str]:
    # ls and not a read: the sandbox reads files by name and has no way to list a folder.
    listed = await sandbox.exec(f"ls -1 {WORKFLOWS_DIR} 2>/dev/null", timeout_s=LIST_TIMEOUT_S)
    names = sorted(n for n in listed.stdout.splitlines() if _WORKFLOW_NAME.fullmatch(n))
    return [f"{WORKFLOWS_DIR}/{name}" for name in names[:MAX_WORKFLOW_FILES]]


async def _read_text(sandbox: Sandbox, path: str) -> str | None:
    try:
        data = await sandbox.read_file(path)
        return data.decode("utf-8") if len(data) <= MAX_FILE_BYTES else None
    except (OSError, UnicodeDecodeError):
        # Missing, a folder, a link that leaves the workspace, or not text.
        return None


# ---------- step 3: running them ----------
def summary_of(result: ExecResult) -> str | None:
    """The line that reports the outcome of a pytest, ruff or tox run, or None if there is none.

    pytest's line counts what ran ("1 failed, 2 passed"). A run that found no tests prints
    "no tests ran", and one that deselected every test prints "N deselected" (exit code 5 in
    both cases). Neither says that the command tests anything here, so neither is a summary.
    The same goes for a run of skipped tests only.
    """
    texts = [_ANSI.sub("", result.stdout), _ANSI.sub("", result.stderr)]
    for text in texts:
        pytest_line = parse_pytest_summary(text)
        if pytest_line and _PYTEST_VERDICT.search(pytest_line):
            return pytest_line
    for pattern in (_RUFF, _TOX_ENV, _TOX_VERDICT):
        for text in texts:
            line = _last_line_matching(pattern, text)
            if line is not None:
                return line
    return None


def _last_line_matching(pattern: re.Pattern[str], text: str) -> str | None:
    found = None
    for line in text.splitlines():
        if pattern.fullmatch(line.strip()):
            found = line.strip()
    return found


async def verify_commands(
    sandbox: Sandbox,
    candidates: list[str],
    *,
    timeout_s: int = COMMAND_TIMEOUT_S,
    budget_s: int = DISCOVERY_BUDGET_S,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Run each candidate once in the sandbox and return those that worked, in order.

    A candidate gets `timeout_s`, but never more than what is left of `budget_s` for all of
    them, and once the budget is spent the rest are not run. The sandbox state is not restored
    here: commands can write files, and the caller puts the workspace back.
    """
    started = clock()
    kept: list[str] = []
    for i, command in enumerate(candidates):
        left = budget_s - (clock() - started)
        if left < 1:
            log.info(
                "the budget for verifying commands is spent; %d not tried", len(candidates) - i
            )
            break
        result = await sandbox.exec(command, timeout_s=max(1, int(min(timeout_s, left))))
        summary = None if result.timed_out else summary_of(result)
        if summary is None:
            reason = "timed out" if result.timed_out else "printed no summary"
            log.info(
                "candidate %r is not kept: it %s (exit code %d)", command, reason, result.exit_code
            )
            continue
        log.info("candidate %r works: %s", command, summary)
        kept.append(command)
    return kept


async def discover_commands(sandbox: Sandbox) -> list[str]:
    """The commands of the repo in the sandbox that work: read, find and verify."""
    candidates = find_candidates(await read_repo_files(sandbox))
    if not candidates:
        return []
    return await verify_commands(sandbox, candidates)
