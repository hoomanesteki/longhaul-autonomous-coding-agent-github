"""What the custom grader reads out of a pytest run (LH-901).

The grader itself needs Docker and is tested in tests/integration/test_eval_runner.py. These tests
cover the part that decides, which is reading the closing line of pytest and wording the result.
"""

import pytest

from eval import grade_custom
from eval.grade_custom import SuiteRun
from longhaul.contracts import ExecResult

pytestmark = [pytest.mark.unit, pytest.mark.phase9]


def run_of(stdout: str, exit_code: int = 0, *, timed_out: bool = False) -> ExecResult:
    return ExecResult(
        exit_code=exit_code, stdout=stdout, stderr="", timed_out=timed_out, duration_ms=10
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("3 passed in 0.02s", (3, 0)),
        ("1 failed, 3 passed in 0.12s", (3, 1)),
        ("2 failed, 1 passed, 1 warning in 1.50s", (1, 2)),
        ("1 error in 0.30s", (0, 1)),
        ("1 failed, 2 errors, 4 passed in 2.00s", (4, 3)),
        ("5 passed, 2 skipped in 0.40s", (5, 0)),
        ("====== 7 passed in 0.50s ======", (7, 0)),
        ("no tests ran in 0.01s", (0, 0)),
    ],
)
def test_the_closing_line_gives_the_counts(line: str, expected: tuple[int, int]) -> None:
    assert grade_custom.parse_counts(f"collected some items\n.F.\n{line}\n") == expected


def test_a_line_in_the_middle_of_the_output_does_not_count() -> None:
    text = "test_a.py::test_x prints 9 passed in 1s\n2 failed in 0.10s\n"
    assert grade_custom.parse_counts(text) == (0, 2)


def test_output_without_a_closing_line_has_no_counts() -> None:
    assert grade_custom.parse_counts("Traceback (most recent call last):\n  boom\n") == (0, 0)


def test_a_run_that_crashed_before_counting_is_a_failure() -> None:
    suite = grade_custom.summarize_run(run_of("ImportError: no module named x\n", exit_code=2))
    assert (suite.passed, suite.failed) == (0, 1)
    assert not suite.ok


def test_a_run_that_passed_is_ok() -> None:
    suite = grade_custom.summarize_run(run_of("4 passed in 0.10s\n"))
    assert suite.ok
    assert (suite.passed, suite.failed) == (4, 0)


def test_a_run_with_a_failing_exit_code_is_never_ok_even_when_the_text_says_passed() -> None:
    # A conftest can print what it likes. The exit code of pytest is the last word.
    suite = grade_custom.summarize_run(run_of("4 passed in 0.10s\n", exit_code=1))
    assert not suite.ok


def test_a_run_that_collected_nothing_is_not_ok() -> None:
    assert not grade_custom.summarize_run(run_of("no tests ran in 0.01s\n", exit_code=5)).ok


def test_a_run_that_timed_out_is_not_ok() -> None:
    suite = grade_custom.summarize_run(run_of("2 passed in 1s\n", exit_code=124, timed_out=True))
    assert suite.timed_out
    assert not suite.ok


def test_the_tail_keeps_the_end_of_a_long_output() -> None:
    lines = [f"line {n}" for n in range(500)]
    suite = grade_custom.summarize_run(run_of("\n".join(lines) + "\n1 failed in 0.1s\n", 1))
    assert suite.tail.endswith("1 failed in 0.1s")
    assert "line 0\n" not in suite.tail


def test_the_reason_of_a_pass_counts_both_suites() -> None:
    reason = grade_custom.describe(SuiteRun(3, 0, False, ""), SuiteRun(4, 0, False, ""))
    assert reason == "all tests passed (3 hidden, 4 repo)"


def test_the_reason_names_what_failed() -> None:
    reason = grade_custom.describe(SuiteRun(1, 2, False, ""), SuiteRun(3, 1, False, ""))
    assert reason == "2 hidden tests failed, 1 repo test failed"


def test_the_reason_of_a_timeout_says_so() -> None:
    reason = grade_custom.describe(SuiteRun(0, 1, True, ""), SuiteRun(3, 0, False, ""))
    assert "timed out" in reason


def test_the_reason_when_nothing_ran_says_so() -> None:
    assert grade_custom.describe(SuiteRun(0, 0, False, ""), SuiteRun(0, 0, False, "")) == (
        "no tests ran"
    )
