import pytest

from longhaul.tools.exec import parse_pytest_summary

pytestmark = [pytest.mark.unit, pytest.mark.phase1]


def framed(body: str) -> str:
    """The default verbosity pads the summary with '=' on both sides."""
    return f"{'=' * 20} {body} {'=' * 20}"


# The shapes below were captured from real pytest 9 runs. Framed lines are the
# default verbosity, bare lines are what -q prints.
SHAPES = [
    pytest.param(
        framed("1 failed, 2 passed in 0.03s"), "1 failed, 2 passed", id="framed-failed-passed"
    ),
    pytest.param("1 failed, 2 passed in 0.03s", "1 failed, 2 passed", id="quiet-failed-passed"),
    pytest.param(framed("3 passed in 0.01s"), "3 passed", id="framed-all-passed"),
    pytest.param("3 passed in 0.01s", "3 passed", id="quiet-all-passed"),
    pytest.param("1 passed in 0.00s", "1 passed", id="single-pass"),
    pytest.param(framed("1 error in 0.13s"), "1 error", id="collection-error"),
    pytest.param("2 errors in 0.20s", "2 errors", id="plural-errors"),
    pytest.param(
        "1 failed, 1 passed, 2 errors in 0.30s", "1 failed, 1 passed, 2 errors", id="mixed-errors"
    ),
    pytest.param("2 passed, 1 skipped in 0.02s", "2 passed, 1 skipped", id="skipped"),
    pytest.param(
        "1 passed, 1 xfailed, 1 xpassed in 0.02s",
        "1 passed, 1 xfailed, 1 xpassed",
        id="xfailed-and-xpassed",
    ),
    pytest.param("2 passed, 1 warning in 0.10s", "2 passed, 1 warning", id="one-warning"),
    pytest.param("2 passed, 3 warnings in 0.10s", "2 passed, 3 warnings", id="many-warnings"),
    pytest.param(
        "= 3 failed, 3 passed, 1 skipped, 1 xfailed, 1 xpassed, 1 warning, 1 error in 0.02s =",
        "3 failed, 3 passed, 1 skipped, 1 xfailed, 1 xpassed, 1 warning, 1 error",
        id="every-kind-framed",
    ),
    pytest.param(
        "1 failed, 1 passed, 1 error, 2 subtests passed in 0.01s",
        "1 failed, 1 passed, 1 error, 2 subtests passed",
        id="subtests",
    ),
    pytest.param("1 passed, 2 deselected in 0.00s", "1 passed, 2 deselected", id="deselected"),
    pytest.param("3 deselected in 0.00s", "3 deselected", id="all-deselected"),
    pytest.param("no tests ran in 0.00s", "no tests ran", id="no-tests-ran"),
    pytest.param(framed("no tests ran in 0.01s"), "no tests ran", id="framed-no-tests-ran"),
    pytest.param("1 failed, 1 rerun in 0.50s", "1 failed, 1 rerun", id="rerun-plugin"),
    pytest.param(
        "1 failed, 4 passed in 62.50s (0:01:02)", "1 failed, 4 passed", id="minutes-suffix"
    ),
    pytest.param(framed("1 passed in 90000.00s (1 day, 1:00:00)"), "1 passed", id="days-suffix"),
    # Things that look close but are not a final summary.
    pytest.param("3 tests collected in 0.00s", None, id="collect-only"),
    pytest.param(
        f"{'!' * 20} Interrupted: 1 error during collection {'!' * 20}", None, id="interrupted"
    ),
    pytest.param(f"{'!' * 20} stopping after 1 failures {'!' * 20}", None, id="maxfail-banner"),
    pytest.param("FAILED tests/test_calc.py::test_add - assert -1 == 5", None, id="failed-line"),
    pytest.param("Ran 3 tests in 0.001s\n\nOK", None, id="unittest"),
    pytest.param("Tests: 1 failed, 2 passed, 3 total", None, id="jest"),
    pytest.param("2 passed in the sun", None, id="prose"),
    pytest.param("", None, id="empty"),
]


@pytest.mark.parametrize(("text", "want"), SHAPES)
def test_parse_summary(text: str, want: str | None) -> None:
    assert parse_pytest_summary(text) == want


FAILING_RUN = """\
.F.                                                                      [100%]
=================================== FAILURES ===================================
___________________________ test_add_positive_numbers ___________________________

    def test_add_positive_numbers() -> None:
>       assert add(2, 3) == 5
E       assert -1 == 5
E        +  where -1 = add(2, 3)

tests/test_calc.py:5: AssertionError
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_add_positive_numbers - assert -1 == 5
1 failed, 2 passed in 0.03s
"""


def test_parse_summary_in_a_full_quiet_run() -> None:
    assert parse_pytest_summary(FAILING_RUN) == "1 failed, 2 passed"


def test_parse_summary_handles_crlf_and_no_trailing_newline() -> None:
    assert parse_pytest_summary("..\r\n2 passed in 0.01s") == "2 passed"


def test_last_summary_line_wins() -> None:
    text = "1 failed in 0.10s\nsecond run\n2 passed in 0.20s\n"
    assert parse_pytest_summary(text) == "2 passed"


def test_colour_codes_are_ignored() -> None:
    red, green, reset = "\x1b[31m", "\x1b[32m", "\x1b[0m"
    text = f"{red}1 failed{reset}, {green}2 passed{reset}{red} in 0.03s{reset}"
    assert parse_pytest_summary(text) == "1 failed, 2 passed"
    assert parse_pytest_summary(f"{red}{framed('1 failed in 0.03s')}{reset}") == "1 failed"


def test_summary_inside_test_output_is_not_a_match() -> None:
    # A test that prints the words is not a pytest summary: it has no 'in 0.0s' tail.
    assert parse_pytest_summary("printing 3 passed and 1 failed\n") is None
