"""Finding and checking the commands that build, test and lint a repo (LH-603).

find_candidates reads the text of a repo's files and names the commands they suggest.
verify_commands runs each in a sandbox and keeps those that printed a summary of a run.
Nothing here needs Docker: the sandbox is a temp folder, as in the other unit tests.
"""

from __future__ import annotations

import pytest

from longhaul.contracts import ExecResult
from longhaul.memory.commands import (
    MAX_CANDIDATES,
    MAX_COMMAND_CHARS,
    find_candidates,
    read_repo_files,
    summary_of,
    verify_commands,
)
from longhaul.sandbox.local_manager import FakeSandbox

pytestmark = [pytest.mark.unit, pytest.mark.phase6]

PYTEST_CONFIG = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'


def workflow(*runs: str) -> str:
    """A CI file with one step for each `run` text, indented as YAML wants it."""
    steps = "".join(
        "      - run: |\n" + "".join(f"          {x}\n" for x in r.splitlines()) for r in runs
    )
    return f"name: ci\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n{steps}"


def result(
    stdout: str = "", stderr: str = "", *, code: int = 0, timed_out: bool = False
) -> ExecResult:
    return ExecResult(
        exit_code=code, stdout=stdout, stderr=stderr, timed_out=timed_out, duration_ms=1
    )


# ---------- where candidates come from ----------
def test_pytest_config_in_pyproject_gives_pytest() -> None:
    assert find_candidates({"pyproject.toml": PYTEST_CONFIG}) == ["pytest -q"]


def test_the_native_pytest_table_counts_as_config() -> None:
    assert find_candidates({"pyproject.toml": '[tool.pytest]\naddopts = ["-ra"]\n'}) == [
        "pytest -q"
    ]


@pytest.mark.parametrize(
    "text",
    ['[project]\nname = "x"\n', "[tool.ruff]\nline-length = 100\n", "not [ toml", ""],
    ids=["no pytest table", "other tool", "not toml", "empty"],
)
def test_a_pyproject_without_pytest_config_gives_nothing(text: str) -> None:
    assert find_candidates({"pyproject.toml": text}) == []


def test_tox_ini_gives_tox() -> None:
    assert find_candidates({"tox.ini": "[tox]\nenvlist = py312\n"}) == ["tox"]


def test_makefile_targets_test_and_lint_give_make_commands() -> None:
    makefile = (
        ".PHONY: test lint build\n"
        "VERSION := 1\n"
        "build:\n\tpython -m build\n"
        "test:\n\tpytest\n"
        "lint :\n\truff check .\n"
        "testing:\n\techo not this\n"
        "test-all:\n\techo nor this\n"
        "\ttest:\n"
    )
    assert find_candidates({"Makefile": makefile}) == ["make test", "make lint"]


def test_a_makefile_without_those_targets_gives_nothing() -> None:
    assert find_candidates({"Makefile": ".PHONY: test\nbuild:\n\techo\ntest := 1\n"}) == []


def test_ci_steps_that_run_pytest_ruff_or_tox_are_taken_line_by_line() -> None:
    ci = workflow(
        "pip install -e . pytest ruff\npytest tests/unit -q\nruff check .\nruff format --check .",
        "tox -e py312",
        "echo done",
    )
    found = find_candidates({".github/workflows/ci.yml": ci})
    assert found == [
        "pytest tests/unit -q",
        "ruff check .",
        "ruff format --check .",
        "tox -e py312",
    ]


@pytest.mark.parametrize(
    "line",
    [
        "uv run pytest -q --cov=pkg",
        "python -m pytest tests",
        "python3 -m ruff check src",
        "poetry run pytest",
        'pytest -k "not slow"',
    ],
)
def test_a_runner_in_front_of_the_tool_is_kept_as_written(line: str) -> None:
    assert find_candidates({".github/workflows/ci.yml": workflow(line)}) == [line]


@pytest.mark.parametrize(
    "line",
    [
        "pip install pytest",
        "pytest -q && echo done",
        "cd sub && pytest",
        "pytest | tee out.txt",
        "pytest > out.txt",
        "pytest ${{ matrix.args }}",
        "pytest $ARGS",
        "echo pytest",
        "export PATH=x pytest",
        "pytestx -q",
        "pytest " + "x" * MAX_COMMAND_CHARS,
    ],
)
def test_a_ci_line_that_depends_on_its_surroundings_is_left_out(line: str) -> None:
    assert find_candidates({".github/workflows/ci.yml": workflow(line)}) == []


def test_a_ci_line_continued_with_a_backslash_is_joined() -> None:
    ci = workflow("pytest -q \\\n  tests/unit")
    assert find_candidates({".github/workflows/ci.yml": ci}) == ["pytest -q tests/unit"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "- just\n- a list\n",
        "jobs: 3\n",
        "jobs:\n  a: 1\n",
        "jobs:\n  a:\n    steps: x\n",
        "jobs:\n  a:\n    steps:\n      - run: 5\n      - uses: actions/checkout@v4\n",
        "a: [unclosed",
    ],
)
def test_a_ci_file_of_another_shape_gives_nothing(text: str) -> None:
    assert find_candidates({".github/workflows/ci.yml": text}) == []


def test_the_sources_are_read_in_a_fixed_order_and_each_command_once() -> None:
    files = {
        ".github/workflows/ci.yml": workflow("pytest -q\nruff check ."),
        "Makefile": "test:\n\tpytest\nlint:\n\truff check .\n",
        "tox.ini": "[tox]\n",
        "pyproject.toml": PYTEST_CONFIG,
    }
    assert find_candidates(files) == ["pytest -q", "tox", "make test", "make lint", "ruff check ."]


def test_the_number_of_candidates_is_capped() -> None:
    lines = "\n".join(f"pytest tests/part{i}" for i in range(MAX_CANDIDATES + 5))
    found = find_candidates({".github/workflows/ci.yml": workflow(lines)})
    assert found == [f"pytest tests/part{i}" for i in range(MAX_CANDIDATES)]


# ---------- reading the files ----------
async def test_the_files_of_a_repo_are_read_from_the_workspace() -> None:
    box = FakeSandbox()
    try:
        await box.write_file("pyproject.toml", PYTEST_CONFIG.encode())
        await box.write_file("Makefile", b"test:\n\tpytest\n")
        await box.write_file(".github/workflows/b.yaml", workflow("ruff check .").encode())
        await box.write_file(".github/workflows/a.yml", workflow("tox").encode())
        await box.write_file(".github/workflows/notes.txt", b"pytest\n")

        files = await read_repo_files(box)
    finally:
        await box.destroy()

    assert sorted(files) == [
        ".github/workflows/a.yml",
        ".github/workflows/b.yaml",
        "Makefile",
        "pyproject.toml",
    ]
    assert files["Makefile"] == "test:\n\tpytest\n"


async def test_a_repo_with_none_of_the_files_reads_as_empty() -> None:
    box = FakeSandbox()
    try:
        assert await read_repo_files(box) == {}
    finally:
        await box.destroy()


async def test_a_file_that_is_not_text_or_is_too_big_is_skipped() -> None:
    box = FakeSandbox()
    try:
        await box.write_file("pyproject.toml", b"\xff\xfe\x00 not utf-8")
        await box.write_file("Makefile", b"test:\n" + b"#" * 300_000)
        await box.write_file("tox.ini", b"[tox]\n")
        assert list(await read_repo_files(box)) == ["tox.ini"]
    finally:
        await box.destroy()


# ---------- what counts as a run ----------
@pytest.mark.parametrize(
    ("stdout", "summary"),
    [
        ("....\n1 failed, 2 passed in 0.03s\n", "1 failed, 2 passed"),
        ("=== 3 passed in 0.01s ===\n", "3 passed"),
        ("\x1b[32m3 passed\x1b[0m in 0.01s\n", "3 passed"),
        ("All checks passed!\n", "All checks passed!"),
        ("src/x.py:1:1: F401 unused\nFound 3 errors.\n[*] 2 fixable\n", "Found 3 errors."),
        ("Found 1 error.\n", "Found 1 error."),
        ("12 files already formatted\n", "12 files already formatted"),
        ("2 files would be reformatted, 3 files already formatted\n",
         "2 files would be reformatted, 3 files already formatted"),
        ("1 file would be reformatted\n", "1 file would be reformatted"),
        ("  py312: OK (1.2=setup[0.4]+cmd[0.8] seconds)\n  congratulations :)\n",
         "py312: OK (1.2=setup[0.4]+cmd[0.8] seconds)"),
        ("  lint: FAIL code 1 (0.9 seconds)\n  evaluation failed :(\n",
         "lint: FAIL code 1 (0.9 seconds)"),
        ("  congratulations :)\n", "congratulations :)"),
    ],
)  # fmt: skip
def test_a_summary_line_of_a_tool_counts(stdout: str, summary: str | None) -> None:
    assert summary_of(result(stdout)) == summary


@pytest.mark.parametrize(
    "text",
    [
        "",
        "make: *** No rule to make target 'test'.  Stop.\n",
        "make: *** No targets specified and no makefile found.  Stop.\n",
        "bash: pytest: command not found\n",
        "no tests ran in 0.01s\n",  # pytest found nothing to run, so the command tests nothing
        "1 deselected in 0.18s\n",  # -k or -m matched nothing: exit code 5, as for "no tests ran"
        "3 deselected, 1 warning in 0.18s\n",
        "2 skipped in 0.01s\n",
        "ERROR: usage: pytest [options]\n",
        "ruff: error: unrecognized option\n",
        "Interrupted: 1 error during collection\n",
        "Everything passed! Found nothing wrong\n",
    ],
)
def test_other_output_is_not_a_summary(text: str) -> None:
    assert summary_of(result(text)) is None
    assert summary_of(result(stderr=text)) is None


def test_a_run_that_deselected_some_tests_and_ran_others_is_a_summary() -> None:
    assert summary_of(result("1 passed, 2 deselected in 0.02s\n")) == "1 passed, 2 deselected"
    assert summary_of(result("1 xfailed, 1 deselected in 0.02s\n")) == "1 xfailed, 1 deselected"
    assert summary_of(result("1 error in 0.02s\n")) == "1 error"


def test_the_summary_may_be_on_stderr() -> None:
    assert summary_of(result("", "1 passed in 0.01s\n")) == "1 passed"


# ---------- running the candidates ----------
class Clocked:
    """A sandbox that answers from a script and moves a fake clock by the time each call takes."""

    id = "clocked"
    workdir = "/workspace"

    def __init__(self, answers: dict[str, tuple[float, ExecResult]]) -> None:
        self.answers = answers
        self.now = 0.0
        self.calls: list[tuple[str, int]] = []

    def clock(self) -> float:
        return self.now

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        self.calls.append((cmd, timeout_s))
        took, answer = self.answers[cmd]
        self.now += took
        return answer

    async def read_file(self, path: str) -> bytes:
        raise FileNotFoundError(path)

    async def write_file(self, path: str, data: bytes) -> None:
        raise NotImplementedError

    async def destroy(self) -> None:
        return None


PASSED = result("1 passed in 0.01s\n")
FAILED = result("1 failed in 0.01s\n", code=1)


async def test_each_candidate_runs_once_and_those_with_a_summary_are_kept() -> None:
    box = Clocked(
        {
            "pytest -q": (1, FAILED),  # failing tests are still a run
            "make test": (1, result("make: *** No rule to make target 'test'.\n", code=2)),
            "ruff check .": (1, result("All checks passed!\n")),
            "tox": (1, result("", "tox: command not found\n", code=127)),
        }
    )

    kept = await verify_commands(box, list(box.answers), clock=box.clock)

    assert kept == ["pytest -q", "ruff check ."]
    assert [cmd for cmd, _ in box.calls] == ["pytest -q", "make test", "ruff check .", "tox"]


async def test_a_command_that_ran_out_of_time_is_not_kept() -> None:
    box = Clocked({"pytest -q": (5, result("1 passed in 0.01s\n", code=137, timed_out=True))})
    assert await verify_commands(box, ["pytest -q"], clock=box.clock) == []


async def test_a_command_gets_its_own_limit_and_never_more_than_what_is_left_of_the_budget() -> (
    None
):
    box = Clocked({"a": (40, PASSED), "b": (40, PASSED), "c": (40, PASSED)})

    kept = await verify_commands(box, ["a", "b", "c"], timeout_s=60, budget_s=100, clock=box.clock)

    # a has 60 s, b has 60 s, and c would start 80 s in, with 20 s left.
    assert box.calls == [("a", 60), ("b", 60), ("c", 20)]
    assert kept == ["a", "b", "c"]


async def test_candidates_after_the_budget_is_spent_are_not_run() -> None:
    box = Clocked({"a": (100, PASSED), "b": (1, PASSED)})

    kept = await verify_commands(box, ["a", "b"], timeout_s=100, budget_s=100, clock=box.clock)

    assert kept == ["a"]
    assert [cmd for cmd, _ in box.calls] == ["a"]


async def test_nothing_to_verify_runs_nothing() -> None:
    box = Clocked({})
    assert await verify_commands(box, [], clock=box.clock) == []
    assert box.calls == []


async def test_a_real_sandbox_runs_the_command_in_the_workspace() -> None:
    box = FakeSandbox()
    try:
        await box.write_file("Makefile", b"test:\n\t@echo '2 passed in 0.01s'\n")
        kept = await verify_commands(box, ["make test", "make lint"], timeout_s=30)
    finally:
        await box.destroy()
    assert kept == ["make test"]
