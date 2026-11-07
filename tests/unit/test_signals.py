"""What tool_exec tells the stuck detector about one call (LH-505).

The facts are read from the text a tool made, so the tests build that text with the same
functions the tools use (render_exit, render_timeout and format_diff). A change to the layout of
a result then breaks a test here and not the detector in a run.
"""

import hashlib
import re

import pytest

from longhaul.contracts import ExecResult, ToolCall, ToolResult
from longhaul.durable.activities import signals
from longhaul.roles.stuck import CallSignal
from longhaul.tools.exec import (
    TESTS_TIMEOUT_HINT,
    render_exit,
    render_timeout,
    split_streams,
    to_result,
)
from longhaul.tools.files import format_diff

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

PYTEST_FAILURE = """\
============================= test session starts ==============================
collected 3 items

tests/test_calc.py F..                                                   [100%]

=================================== FAILURES ===================================
__________________________ test_add_positive_numbers ___________________________

    def test_add_positive_numbers() -> None:
>       assert add(2, 3) == 5
E       assert -1 == 5
E        +  where -1 = add(2, 3)

tests/test_calc.py:5: AssertionError
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_add_positive_numbers - assert -1 == 5
{closing}
"""
TRACEBACK = """\
Traceback (most recent call last):
  File "/workspace/src/inv.py", line 42, in lookup
    return stock[sku]
           ~~~~~^^^^^
KeyError: 'sku-7'
"""


def ran(stdout: str = "", stderr: str = "", code: int = 0, summary: str | None = None) -> str:
    res = ExecResult(exit_code=code, stdout=stdout, stderr=stderr, timed_out=False, duration_ms=5)
    return render_exit(res, summary)


def result(output: str, *, ok: bool = True, code: int | None = 0) -> ToolResult:
    return ToolResult(call_id="c1", ok=ok, output=output, exit_code=code)


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(id="c1", name=name, input=dict(arguments))


def read(
    name: str, output: str, *, code: int | None = 0, ok: bool = True, **arguments: object
) -> CallSignal | None:
    return signals.call_signal(
        call(name, **arguments), result(output, ok=ok, code=code), test_command="pytest -q"
    )


# ---------- tests passed ----------
@pytest.mark.parametrize(
    ("closing", "passed"),
    [
        ("1 failed, 2 passed in 1.57s", 2),
        ("5 passed in 0.10s", 5),
        ("3 failed in 0.20s", 0),
        ("1 error in 0.30s", 0),
        ("no tests ran in 0.01s", 0),
        ("2 passed, 1 skipped, 4 warnings in 0.50s", 2),
    ],
)
def test_tests_passed_is_read_from_the_closing_line(closing: str, passed: int) -> None:
    out = ran(PYTEST_FAILURE.format(closing=closing), code=1, summary=closing.split(" in ")[0])
    assert signals.passed_count(out) == passed


def test_a_command_that_prints_no_pytest_line_has_no_count() -> None:
    assert signals.passed_count(ran("hello\n")) is None
    assert signals.passed_count("") is None


def test_run_command_counts_too_when_it_runs_pytest() -> None:
    out = ran(PYTEST_FAILURE.format(closing="1 failed, 7 passed in 1.00s"), code=1)
    fact = read("run_command", out, code=1, command="python -m pytest")
    assert fact is not None and fact.tests_passed == 7


# ---------- failed commands ----------
def test_a_command_that_exits_zero_failed_nothing() -> None:
    fact = read("run_command", ran("fine\n"), command="make lint")
    assert fact is None or fact.failed_cmd_hash is None


def test_a_failed_command_gets_a_hash_a_label_and_the_error() -> None:
    out = ran(stderr=TRACEBACK, code=1)
    fact = read("run_command", out, code=1, command="python  src/inv.py")
    assert fact is not None
    assert fact.failed_command == "python src/inv.py"  # blanks collapsed
    assert fact.failed_error == "KeyError at src/inv.py:42"  # the /workspace prefix is dropped
    tail = "\n".join(TRACEBACK.rstrip("\n").splitlines()[-20:])
    want = hashlib.sha1(f"python  src/inv.py\n1\n{tail}".encode()).hexdigest()
    assert fact.failed_cmd_hash == want


def test_the_hash_is_the_same_for_the_same_failure_and_not_for_another() -> None:
    first = read("run_command", ran(stderr=TRACEBACK, code=1), code=1, command="python a.py")
    again = read("run_command", ran(stderr=TRACEBACK, code=1), code=1, command="python a.py")
    other_exit = read("run_command", ran(stderr=TRACEBACK, code=2), code=2, command="python a.py")
    other_error = read(
        "run_command",
        ran(stderr=TRACEBACK.replace("sku-7", "sku-8"), code=1),
        code=1,
        command="python a.py",
    )
    other_command = read(
        "run_command", ran(stderr=TRACEBACK, code=1), code=1, command="python b.py"
    )
    assert first and again and other_exit and other_error and other_command
    assert first.failed_cmd_hash == again.failed_cmd_hash
    hashes = {first.failed_cmd_hash, other_exit.failed_cmd_hash, other_error.failed_cmd_hash}
    assert len(hashes | {other_command.failed_cmd_hash}) == 4


def test_pytest_failures_with_a_different_run_time_hash_alike() -> None:
    # pytest writes its report to stdout and leaves stderr empty, and its closing line has the
    # run time. Two runs of the same failure must not differ by a few milliseconds.
    one = ran(PYTEST_FAILURE.format(closing="1 failed, 2 passed in 1.57s"), code=1)
    two = ran(PYTEST_FAILURE.format(closing="1 failed, 2 passed in 0.98s"), code=1)
    a = read("run_tests", one, code=1)
    b = read("run_tests", two, code=1)
    assert a and b
    assert a.failed_cmd_hash == b.failed_cmd_hash


def test_a_different_pytest_failure_hashes_differently() -> None:
    one = ran(PYTEST_FAILURE.format(closing="1 failed, 2 passed in 1.57s"), code=1)
    two = ran(
        PYTEST_FAILURE.format(closing="1 failed, 2 passed in 1.57s").replace("-1 == 5", "7 == 5"),
        code=1,
    )
    a = read("run_tests", one, code=1)
    b = read("run_tests", two, code=1)
    assert a and b
    assert a.failed_cmd_hash != b.failed_cmd_hash


def test_a_pytest_failure_names_the_error_and_the_place() -> None:
    out = ran(PYTEST_FAILURE.format(closing="1 failed, 2 passed in 1.57s"), code=1)
    fact = read("run_tests", out, code=1)
    assert fact is not None
    assert fact.failed_error == "AssertionError at tests/test_calc.py:5"
    assert fact.tests_passed == 2


def test_run_tests_is_named_by_the_test_command_and_its_target() -> None:
    out = ran(PYTEST_FAILURE.format(closing="1 failed in 1.00s"), code=1)
    fact = read("run_tests", out, code=1, target="tests/test_inv.py -k lookup")
    assert fact is not None
    assert fact.failed_command == "pytest -q tests/test_inv.py -k lookup"
    bare = read("run_tests", out, code=1)
    assert bare is not None and bare.failed_command == "pytest -q"


def test_run_tests_without_a_test_command_is_named_run_tests() -> None:
    assert signals.command_of(call("run_tests"), None) == "run_tests"
    assert signals.command_of(call("run_tests", target="a.py"), None) == "run_tests a.py"


def test_a_target_with_unbalanced_quotes_still_has_a_name() -> None:
    name = signals.command_of(call("run_tests", target="tests/'x"), "pytest -q")
    assert name == "pytest -q tests/'x"


def test_the_error_falls_back_to_the_last_line_when_none_looks_like_one() -> None:
    out = ran(stderr="make: *** [lint] Failed\n", code=2)
    fact = read("run_command", out, code=2, command="make lint")
    assert fact is not None
    assert fact.failed_error == "make: *** [lint] Failed"


def test_a_failed_command_with_no_output_has_no_error_text() -> None:
    fact = read("run_command", ran(code=1), code=1, command="false")
    assert fact is not None
    assert fact.failed_cmd_hash is not None
    assert fact.failed_error is None


def test_a_timeout_is_a_failed_command() -> None:
    res = ExecResult(exit_code=-1, stdout="partial\n", stderr="", timed_out=True, duration_ms=9)
    out = render_timeout(res, 600, TESTS_TIMEOUT_HINT)
    fact = read("run_tests", out, code=-1, ok=False)
    assert fact is not None and fact.failed_cmd_hash is not None


def test_a_command_that_never_ran_failed_no_command() -> None:
    # "no test command is configured" is a tool error, which the detector counts apart.
    fact = read("run_tests", "no test command is configured", ok=False, code=None)
    assert fact is None or fact.failed_cmd_hash is None


# ---------- split_streams (tools/exec.py) ----------
@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [("out\n", "err\n"), ("out\n", ""), ("", "err\n"), ("", "")],
)
def test_split_streams_reads_back_what_render_exit_wrote(stdout: str, stderr: str) -> None:
    assert split_streams(ran(stdout=stdout, stderr=stderr, code=1)) == (
        stdout.rstrip("\n"),
        stderr.rstrip("\n"),
    )


def test_split_streams_reads_a_timeout() -> None:
    res = ExecResult(exit_code=-1, stdout="partial\n", stderr="", timed_out=True, duration_ms=9)
    assert split_streams(to_result(res, 5, hint="h").output) == ("partial", "")


def test_split_streams_keeps_blank_lines_inside_a_stream() -> None:
    assert split_streams(ran(stdout="a\n\nb\n", stderr="c\n\nd\n")) == ("a\n\nb", "c\n\nd")


# ---------- which stream the hash reads ----------
def hash_of(stdout: str, stderr: str, command: str = "python run.py") -> str | None:
    fact = read("run_command", ran(stdout=stdout, stderr=stderr, code=1), code=1, command=command)
    assert fact is not None
    return fact.failed_cmd_hash


def test_a_crash_is_told_by_its_stderr_and_not_by_what_was_printed_before_it() -> None:
    # A script that prints a timestamp and then crashes: the crash is what repeats.
    first = hash_of("started at 10:01:07\n", TRACEBACK)
    again = hash_of("started at 10:04:52\n", TRACEBACK)
    assert first == again


def test_a_warning_on_stderr_does_not_hide_what_pytest_printed_on_stdout() -> None:
    # The same warning on stderr and a different failure on stdout are two different failures.
    warning = "PytestDeprecationWarning: the option is unset\n"
    one = PYTEST_FAILURE.format(closing="1 failed, 2 passed in 1.00s")
    two = one.replace("-1 == 5", "7 == 5")
    assert hash_of(one, warning) != hash_of(two, warning)
    assert hash_of(one, warning) == hash_of(one.replace("1.00s", "2.00s"), warning)


def test_a_failure_with_nothing_on_stderr_is_told_by_stdout() -> None:
    assert hash_of("boom 1\n", "") != hash_of("boom 2\n", "")


def test_the_hash_reads_only_the_last_20_lines() -> None:
    long = "".join(f"line {i}\n" for i in range(100))
    assert hash_of("", "early noise\n" + long) == hash_of("", "other early noise\n" + long)


# ---------- edited regions ----------
def edit_output(old: str, new: str, path: str = "src/inv.py") -> str:
    return format_diff(path, old, new)


def numbered(lines: int, changed: dict[int, str] | None = None) -> str:
    changed = changed or {}
    return "".join(f"{changed.get(n, f'line {n}')}\n" for n in range(1, lines + 1))


def test_a_one_line_edit_is_a_one_line_region() -> None:
    old = numbered(30)
    new = numbered(30, {12: "line twelve changed"})
    fact = read("edit_file", edit_output(old, new), path="src/inv.py", old_str="a", new_str="b")
    assert fact is not None
    assert fact.edited_regions == [("src/inv.py", 12, 12)]


def test_an_edit_that_adds_lines_covers_the_new_lines() -> None:
    old = numbered(30)
    new = old.replace("line 12\n", "line 12\nextra a\nextra b\nextra c\n")
    fact = read("edit_file", edit_output(old, new), path="src/inv.py", old_str="a", new_str="b")
    assert fact is not None
    assert fact.edited_regions == [("src/inv.py", 13, 15)]


def test_an_edit_that_deletes_lines_marks_the_place_they_were_in() -> None:
    old = numbered(30)
    new = old.replace("line 12\nline 13\n", "")
    fact = read("edit_file", edit_output(old, new), path="src/inv.py", old_str="a", new_str="")
    assert fact is not None
    assert fact.edited_regions == [("src/inv.py", 12, 12)]


def test_an_edit_in_two_hunks_is_one_region_from_the_first_to_the_last_change() -> None:
    old = numbered(60)
    new = numbered(60, {5: "five", 40: "forty"})
    fact = read("edit_file", edit_output(old, new), path="src/inv.py", old_str="a", new_str="b")
    assert fact is not None
    assert fact.edited_regions == [("src/inv.py", 5, 40)]


def test_the_path_is_the_one_in_the_diff() -> None:
    old, new = numbered(5), numbered(5, {2: "x"})
    fact = read("edit_file", edit_output(old, new, "./pkg/m.py"), path="./pkg/m.py")
    assert fact is not None
    assert fact.edited_regions[0][0] == "./pkg/m.py"


def test_an_edit_that_failed_has_no_region() -> None:
    fact = read("edit_file", "found 0 matches for old_str in a.py", ok=False, code=None)
    assert fact is None


def test_write_file_marks_the_whole_file() -> None:
    fact = read(
        "write_file", "wrote 18 bytes to src/new.py", path="src/new.py", content="a\nb\nc\n"
    )
    assert fact is not None
    assert fact.edited_regions == [("src/new.py", 1, 3)]


def test_write_file_of_an_empty_file_is_one_line() -> None:
    fact = read("write_file", "wrote 0 bytes to src/new.py", path="src/new.py", content="")
    assert fact is not None
    assert fact.edited_regions == [("src/new.py", 1, 1)]


@pytest.mark.parametrize("name", ["read_file", "list_dir", "search", "git_diff", "read_output"])
def test_tools_that_change_and_run_nothing_have_no_facts(name: str) -> None:
    assert read(name, "some text", path="a.py") is None


# ---------- limits on what is kept ----------
def test_the_texts_in_a_fact_are_short() -> None:
    out = ran(stderr=("KeyError: " + "k" * 900 + "\n"), code=1)
    fact = read("run_command", out, code=1, command="c " * 400)
    assert fact is not None
    assert fact.failed_command is not None and len(fact.failed_command) <= 80
    assert fact.failed_error is not None and len(fact.failed_error) <= 80
    assert not re.search(r"\s{2}", fact.failed_command)


def test_a_fact_is_small_enough_for_the_history() -> None:
    out = ran(stderr=("KeyError: " + "k" * 900 + "\n"), code=1)
    fact = read("run_command", out, code=1, command="c " * 400)
    assert fact is not None
    assert len(fact.model_dump_json()) < 600


def test_a_fact_is_a_call_signal() -> None:
    assert isinstance(read("write_file", "wrote 1 bytes to a", path="a", content="x"), CallSignal)
