"""The verifier role: its tools, its prompt, its pre-check and how a verdict is judged (LH-504).

The judging is pure, so these tests need no workflow. tests/integration/test_verifier.py runs the
same rules inside TaskWorkflow. The two halves of a review are joined here: the pre-check runs
commands and has the last word on the tests, and the verdict is the model's reading of the code.
The work passes only when both say so.
"""

import re
from dataclasses import dataclass
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from longhaul.agent.prompts import (
    FIRST_USER_MESSAGE,
    SECTION_HEADINGS,
    build_system_prompt,
    first_user_message,
)
from longhaul.contracts import (
    OutcomeKind,
    Plan,
    PlanStep,
    RepoSettings,
    Role,
    TaskSpec,
    ToolResult,
    VerifierVerdict,
)
from longhaul.durable import acts
from longhaul.providers.base import parse_role_line
from longhaul.roles import verifier
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

TOOLS_OF_THE_STORY = {"list_dir", "read_file", "search", "git_diff", "run_tests", "read_output"}


def step(n: int, status: str = "done") -> PlanStep:
    return PlanStep(
        id=f"s{n}",
        description=f"Change module {n}",
        files=[f"src/calc/m{n}.py"],
        done_when=f"the tests of module {n} pass",
        status=status,  # type: ignore[arg-type]
    )


def make_plan(*checks: str, steps: int = 2) -> Plan:
    return Plan(
        summary="add() subtracts, so change the operator and test it",
        steps=[step(n) for n in range(1, steps + 1)],
        acceptance_checks=list(checks) or ["pytest -q", "add(2, 3) returns 5"],
    )


def verdict_input(**fields: Any) -> dict[str, Any]:
    return {
        "passed": True,
        "tests_run": "pytest -q",
        "tests_summary": "41 passed",
        "issues": [],
        **fields,
    }


def failing(**fields: Any) -> dict[str, Any]:
    return verdict_input(
        **{
            "passed": False,
            "tests_summary": "40 passed, 1 failed",
            "issues": ["add() does not handle negative numbers"],
            "send_back_step": "s1",
            **fields,
        }
    )


def call(name: str, call_id: str, arguments: dict[str, Any] | None) -> acts.ToolCallSummary:
    return acts.ToolCallSummary(id=call_id, name=name, input=arguments)


async def fake_run(summary: acts.ToolCallSummary) -> ToolResult:
    return ToolResult(call_id=summary.id, ok=True, output=f"ran {summary.name}")


def spec() -> TaskSpec:
    return TaskSpec(
        task_id="lh-acme-calc-42",
        repo="acme/calc",
        issue_number=42,
        title="add() returns the difference",
        body="`calc.add` subtracts its arguments.",
        settings=RepoSettings(repo="acme/calc", test_command="pytest -q"),
    )


@dataclass(frozen=True)
class Ran:
    """A command of the pre-check as the review reads it."""

    command: str
    passed: bool
    exit_code: int | None = 0


GREEN = Ran("pytest -q", True, 0)
RED = Ran("pytest -q", False, 1)


# ---------- the tools ----------
def test_the_verifier_has_the_read_tools_and_run_tests_and_none_that_write() -> None:
    assert set(verifier.TOOLS) == TOOLS_OF_THE_STORY
    assert not {"edit_file", "write_file", "run_command", "finish"} & set(verifier.TOOLS)


def test_every_verifier_tool_is_in_the_default_registry_and_none_mutates() -> None:
    registry = default_registry()
    for name in verifier.TOOLS:
        tool = registry.get(name)
        assert tool is not None, name
        assert tool.mutates is False, name


def test_the_verifier_is_offered_its_tools_and_submit_verdict_last() -> None:
    offered = verifier.tool_specs(default_registry().specs())
    assert [t.name for t in offered] == [*verifier.TOOLS, verifier.SUBMIT_VERDICT]


def test_a_registry_without_a_verifier_tool_is_refused() -> None:
    specs = [s for s in default_registry().specs() if s.name != "git_diff"]
    with pytest.raises(ValueError, match="git_diff"):
        verifier.tool_specs(specs)


def test_submit_verdict_takes_the_verdict_contract_as_its_input() -> None:
    assert verifier.SUBMIT_VERDICT == "submit_verdict"
    assert verifier.SUBMIT_VERDICT_SPEC.name == "submit_verdict"
    assert verifier.SUBMIT_VERDICT_SPEC.input_schema == VerifierVerdict.model_json_schema()
    Draft202012Validator.check_schema(verifier.SUBMIT_VERDICT_SPEC.input_schema)


def test_submit_verdict_is_not_a_tool_of_the_registry() -> None:
    # The workflow judges the call itself, as it does for finish, and no other role sees it.
    assert default_registry().get(verifier.SUBMIT_VERDICT) is None


# ---------- which acceptance checks are commands ----------
@pytest.mark.parametrize(
    ("check", "command"),
    [
        pytest.param("$ ruff check .", "ruff check .", id="dollar sign"),
        pytest.param("  $   python -m mypy src  ", "python -m mypy src", id="dollar and spaces"),
        pytest.param("`make lint`", "make lint", id="backticks"),
        pytest.param("add(2, 3) returns 5", None, id="a check to observe"),
        pytest.param("pytest -q", None, id="a bare command is not marked"),
        pytest.param("`pytest -q` passes", None, id="a command with words after it"),
        pytest.param("Run `pytest -q` and read the result", None, id="a command inside a sentence"),
        pytest.param("$ ", None, id="a dollar sign alone"),
        pytest.param("``", None, id="empty backticks"),
        pytest.param("", None, id="blank"),
    ],
)
def test_only_a_marked_acceptance_check_is_a_command(check: str, command: str | None) -> None:
    assert verifier.command_of(check) == command


def test_the_test_command_comes_first_and_runs_through_run_command() -> None:
    plan = make_plan("pytest -q")
    (first,) = verifier.precheck_commands(plan, "pytest -q", 1)
    assert first.tool == "run_command"
    assert first.arguments == {"command": "pytest -q", "timeout_s": verifier.COMMAND_TIMEOUT_S}
    assert first.label == "pytest -q"


def test_the_acceptance_commands_follow_in_the_order_of_the_plan() -> None:
    plan = make_plan("$ ruff check .", "add(2, 3) returns 5", "`mypy src`")
    commands = verifier.precheck_commands(plan, "pytest -q", 2)
    assert [c.arguments["command"] for c in commands] == ["pytest -q", "ruff check .", "mypy src"]


def test_a_check_that_repeats_the_test_command_runs_once() -> None:
    plan = make_plan("$ pytest -q", "`pytest -q`", "$ ruff check .", "$ ruff check .")
    commands = verifier.precheck_commands(plan, "pytest -q", 1)
    assert [c.arguments["command"] for c in commands] == ["pytest -q", "ruff check ."]


def test_a_repo_without_a_test_command_runs_the_acceptance_commands_only() -> None:
    plan = make_plan("$ ruff check .", "add(2, 3) returns 5")
    commands = verifier.precheck_commands(plan, None, 1)
    assert [c.arguments["command"] for c in commands] == ["ruff check ."]
    assert verifier.precheck_commands(make_plan("add(2, 3) returns 5"), None, 1) == []
    assert verifier.precheck_commands(make_plan("add(2, 3) returns 5"), "  ", 1) == []


def test_at_most_five_acceptance_commands_run() -> None:
    plan = make_plan(*(f"$ check-{n}" for n in range(9)))
    commands = verifier.precheck_commands(plan, None, 1)
    assert [c.arguments["command"] for c in commands] == [f"check-{n}" for n in range(5)]
    assert len(commands) == verifier.MAX_CHECKS


def test_call_ids_are_stable_and_differ_between_rounds() -> None:
    plan = make_plan("$ ruff check .")
    one = [c.call_id for c in verifier.precheck_commands(plan, "pytest -q", 1)]
    again = [c.call_id for c in verifier.precheck_commands(plan, "pytest -q", 1)]
    two = [c.call_id for c in verifier.precheck_commands(plan, "pytest -q", 2)]
    assert one == again == ["verify1-tests", "verify1-check1"]
    assert two == ["verify2-tests", "verify2-check1"]
    assert verifier.diff_call_id(1) == "verify1-diff" != verifier.diff_call_id(2)


def test_a_very_long_command_is_cut_in_its_label_and_not_in_the_call() -> None:
    long_command = "echo " + "x" * 1000
    (cmd,) = verifier.precheck_commands(make_plan(f"$ {long_command}"), None, 1)
    assert cmd.arguments["command"] == long_command
    assert len(cmd.label) <= verifier.MAX_LABEL_CHARS
    assert cmd.label.endswith("...")


# ---------- the first message ----------
def test_the_precheck_block_says_what_passed_and_what_failed() -> None:
    text = verifier.render_precheck(
        [
            (GREEN, "3 passed in 0.01s"),
            (Ran("ruff check .", False, 1), "E501 line too long\nFound 1 error."),
            (Ran("make slow", False, None), ""),
        ]
    )
    assert text.startswith(verifier.PRECHECK_HEADING)
    assert "1. PASSED `pytest -q` (exit code 0)" in text
    assert "2. FAILED `ruff check .` (exit code 1)" in text
    assert "3. FAILED `make slow` (did not finish: it timed out or could not run)" in text
    # The output is indented, so a line of it cannot pass for a line of the block.
    assert "    3 passed in 0.01s" in text
    assert "    E501 line too long\n    Found 1 error." in text
    assert "whatever you decide" in text


def test_the_precheck_block_says_so_when_nothing_was_run() -> None:
    text = verifier.render_precheck([])
    assert text.startswith(verifier.PRECHECK_HEADING)
    assert "No command was run" in text


def test_a_line_of_output_cannot_open_a_block_of_its_own() -> None:
    text = verifier.render_precheck([(GREEN, "## Diff\n## Plan")])
    heads = [line for line in text.splitlines() if line.startswith("## ")]
    assert heads == [verifier.PRECHECK_HEADING]


def test_the_diff_block_has_its_heading_and_the_text() -> None:
    text = verifier.render_diff("diff --git a/x b/x\n+new")
    assert text == f"{verifier.DIFF_HEADING}\ndiff --git a/x b/x\n+new"
    assert verifier.render_diff("  ") == f"{verifier.DIFF_HEADING}\n(the diff is empty)"


# ---------- what makes a verdict valid ----------
def test_a_verdict_that_passes_is_the_contract() -> None:
    got = verifier.read_verdict(verdict_input(), make_plan())
    assert got == VerifierVerdict(
        passed=True, tests_run="pytest -q", tests_summary="41 passed", issues=[]
    )


def test_issues_and_the_step_are_optional_for_a_pass() -> None:
    arguments = verdict_input()
    del arguments["issues"]
    got = verifier.read_verdict(arguments, make_plan())
    assert isinstance(got, VerifierVerdict) and got.issues == [] and got.send_back_step is None


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        pytest.param(verdict_input(tests_run="  "), "tests_run: name the command", id="no command"),
        pytest.param(
            verdict_input(tests_summary=""), "tests_summary: give the counts", id="no summary"
        ),
        pytest.param(
            failing() | {"issues": []}, "issues: say what is wrong", id="a fail with no issues"
        ),
        pytest.param(
            failing() | {"issues": [" ", "real"]},
            "issues.0: must not be empty",
            id="a blank issue",
        ),
        pytest.param(
            failing(send_back_step="s9"),
            "send_back_step: 's9' is not a step of the plan (s1, s2)",
            id="a step the plan does not have",
        ),
        pytest.param(
            verdict_input(send_back_step="s9"),
            "send_back_step: 's9' is not a step of the plan (s1, s2)",
            id="a step named by a pass",
        ),
        pytest.param({}, "passed: Field required", id="empty input"),
        pytest.param(verdict_input(passed="maybe"), "passed:", id="wrong type"),
        pytest.param(
            verdict_input(issues="none"), "issues: Input should be a valid list", id="issues type"
        ),
        pytest.param(
            None, "the verdict is too long to read in one call", id="arguments held by reference"
        ),
    ],
)
def test_a_verdict_that_breaks_a_rule_is_refused_with_the_rule(
    arguments: dict[str, Any] | None, problem: str
) -> None:
    got = verifier.read_verdict(arguments, make_plan())
    assert isinstance(got, list)
    assert any(problem in item for item in got), got


def test_a_failed_review_may_leave_the_step_out() -> None:
    arguments = failing()
    del arguments["send_back_step"]
    got = verifier.read_verdict(arguments, make_plan())
    assert isinstance(got, VerifierVerdict) and got.send_back_step is None


def test_a_blank_step_is_read_as_no_step() -> None:
    got = verifier.read_verdict(failing(send_back_step="  "), make_plan())
    assert isinstance(got, VerifierVerdict) and got.send_back_step is None


def test_every_problem_of_a_verdict_is_listed_so_one_repair_can_fix_them_all() -> None:
    arguments = failing(tests_run="", tests_summary="") | {"issues": []}
    got = verifier.read_verdict(arguments, make_plan())
    assert isinstance(got, list) and len(got) == 3
    answer = verifier.judge_submission(0, arguments, make_plan()).answer
    assert all(problem in answer for problem in got)


# ---------- F14: the repair turn ----------
def test_verdict_repair_turn() -> None:
    plan = make_plan()
    first = verifier.judge_submission(0, failing() | {"issues": []}, plan)
    assert first.verdict is None
    assert first.outcome is None
    assert first.failures == 1
    assert "issues: say what is wrong" in first.answer
    assert "call submit_verdict again" in first.answer

    # The repaired verdict is accepted. The failure stays counted, and nothing ends the run.
    repaired = verifier.judge_submission(first.failures, failing(), plan)
    assert repaired.verdict is not None and repaired.verdict.passed is False
    assert repaired.outcome is None
    assert repaired.failures == 1
    assert repaired.answer == verifier.ACCEPTED

    # A second verdict that is not valid ends the run with an error that says why.
    second = verifier.judge_submission(first.failures, {"passed": True}, plan)
    assert second.verdict is None
    assert second.outcome is not None
    assert second.outcome.kind is OutcomeKind.ERROR
    assert second.outcome.summary.startswith("verifier submitted an invalid verdict twice: ")
    assert "tests_run: Field required" in second.outcome.summary
    assert second.failures == 2
    assert "twice" in second.answer


def test_the_summary_of_an_ended_review_stays_short_and_plain() -> None:
    bad = {"passed": True, "tests_run": "", "tests_summary": "", "issues": ["x" * 400]}
    verifier.judge_submission(0, bad, make_plan())
    ended = verifier.judge_submission(1, bad, make_plan()).outcome
    assert ended is not None
    assert len(ended.summary) <= verifier.MAX_SUMMARY_CHARS
    assert ended.summary.isascii()


def test_a_long_list_of_problems_is_cut() -> None:
    arguments = verdict_input(issues=[""] * 9)
    answer = verifier.judge_submission(0, arguments, make_plan()).answer
    assert answer.count("\n- ") == verifier.MAX_PROBLEMS_SHOWN
    assert "and 4 more" in answer


# ---------- the calls of a turn ----------
async def test_a_tool_outside_the_set_is_answered_and_not_run() -> None:
    ran: list[str] = []

    async def run(summary: acts.ToolCallSummary) -> ToolResult:
        ran.append(summary.name)
        return await fake_run(summary)

    calls = [
        call("edit_file", "t1", {"path": "a.py", "old_str": "a", "new_str": "b"}),
        call("read_file", "t2", {"path": "a.py"}),
        call("run_command", "t3", {"command": "ls"}),
        call("finish", "t4", {"summary": "done"}),
    ]
    got = await verifier.run_calls(calls, run, make_plan())

    assert ran == ["read_file"]
    assert [(r.call_id, r.ok) for r in got.results] == [
        ("t1", False),
        ("t2", True),
        ("t3", False),
        ("t4", False),
    ]
    assert "edit_file" in got.results[0].output
    assert "not available" in got.results[0].output
    assert got.verdict is None and got.outcome is None


async def test_the_calls_are_run_in_order_and_a_good_verdict_ends_the_turn() -> None:
    order: list[str] = []

    async def run(summary: acts.ToolCallSummary) -> ToolResult:
        order.append(summary.id)
        return await fake_run(summary)

    calls = [
        call("read_file", "t1", {"path": "a.py"}),
        call("submit_verdict", "t2", verdict_input()),
        call("run_tests", "t3", {}),
    ]
    got = await verifier.run_calls(calls, run, make_plan())

    assert order == ["t1"]
    assert got.verdict is not None and got.verdict.passed
    # Every call is answered, so the stored transcript has no tool_use without a result.
    assert [(r.call_id, r.ok) for r in got.results] == [("t1", True), ("t2", True), ("t3", False)]
    assert got.results[2].output == verifier.SKIPPED_AFTER_VERDICT


async def test_the_repair_turn_across_two_replies_of_the_verifier() -> None:
    plan = make_plan()
    bad = {"passed": True}
    first = await verifier.run_calls([call("submit_verdict", "t1", bad)], fake_run, plan)
    assert first.verdict is None and first.outcome is None and first.failures == 1
    (answer,) = first.results
    assert (answer.call_id, answer.ok) == ("t1", False)

    second = await verifier.run_calls(
        [call("submit_verdict", "t2", verdict_input())], fake_run, plan, first.failures
    )
    assert second.verdict is not None and second.outcome is None
    assert [(r.call_id, r.ok) for r in second.results] == [("t2", True)]


async def test_a_second_invalid_verdict_ends_the_run_through_run_calls() -> None:
    bad = {"passed": True}
    plan = make_plan()
    first = await verifier.run_calls([call("submit_verdict", "t1", bad)], fake_run, plan)
    calls = [call("submit_verdict", "t2", bad), call("read_file", "t3", {"path": "a.py"})]
    second = await verifier.run_calls(calls, fake_run, plan, first.failures)
    assert second.verdict is None
    assert second.outcome is not None and second.outcome.kind is OutcomeKind.ERROR
    assert [(r.call_id, r.ok) for r in second.results] == [("t2", False), ("t3", False)]
    assert second.results[1].output == verifier.SKIPPED_AFTER_VERDICT


async def test_a_bad_verdict_does_not_stop_the_other_calls_of_the_turn() -> None:
    calls = [call("submit_verdict", "t1", {"passed": True}), call("read_file", "t2", {"path": "a"})]
    got = await verifier.run_calls(calls, fake_run, make_plan())
    assert [(r.call_id, r.ok) for r in got.results] == [("t1", False), ("t2", True)]
    assert got.failures == 1 and got.outcome is None and got.verdict is None


# ---------- the pre-check has the last word ----------
def test_the_work_passes_only_when_the_precheck_and_the_verdict_both_say_so() -> None:
    plan = make_plan()
    good = verifier.read_verdict(verdict_input(), plan)
    bad = verifier.read_verdict(failing(), plan)
    assert isinstance(good, VerifierVerdict) and isinstance(bad, VerifierVerdict)

    assert verifier.judge_review([GREEN], good).passed is True
    assert verifier.judge_review([GREEN], bad).passed is False
    assert verifier.judge_review([RED], good).passed is False
    assert verifier.judge_review([RED], bad).passed is False


def test_a_failing_precheck_overrides_a_verdict_that_passes() -> None:
    good = VerifierVerdict(passed=True, tests_run="pytest -q", tests_summary="41 passed")
    review = verifier.judge_review([GREEN, Ran("ruff check .", False, 1)], good)

    assert review.passed is False
    assert review.precheck_passed is False and review.model_passed is True
    assert review.issues == ("the command `ruff check .` failed with exit code 1",)
    assert review.send_back_step is None


def test_a_command_that_did_not_finish_is_a_failure_that_names_it() -> None:
    good = VerifierVerdict(passed=True, tests_run="pytest -q", tests_summary="41 passed")
    review = verifier.judge_review([Ran("make slow", False, None)], good)
    assert review.passed is False
    assert review.issues == (
        "the command `make slow` did not finish: it timed out or could not run",
    )


def test_the_failed_commands_come_before_the_issues_of_the_verifier() -> None:
    bad = VerifierVerdict(
        passed=False,
        tests_run="pytest -q",
        tests_summary="1 failed",
        issues=["negative numbers are not handled"],
        send_back_step="s2",
    )
    review = verifier.judge_review([RED], bad)
    assert review.issues == (
        "the command `pytest -q` failed with exit code 1",
        "negative numbers are not handled",
    )
    assert review.send_back_step == "s2"


def test_a_pass_has_no_issues_to_send_back() -> None:
    good = VerifierVerdict(
        passed=True, tests_run="pytest -q", tests_summary="41 passed", issues=["a nit"]
    )
    review = verifier.judge_review([GREEN], good)
    assert review.passed is True
    assert review.issues == ()
    assert review.tests_summary == "41 passed"


def test_no_command_in_the_precheck_leaves_the_verdict_to_decide() -> None:
    good = VerifierVerdict(passed=True, tests_run="pytest -q", tests_summary="41 passed")
    assert verifier.judge_review([], good).passed is True
    assert verifier.judge_review([], good).precheck_passed is True


# ---------- what the coder is told ----------
def test_the_message_to_the_coder_names_the_issues_and_the_step() -> None:
    review = verifier.judge_review(
        [GREEN],
        VerifierVerdict(
            passed=False,
            tests_run="pytest -q",
            tests_summary="40 passed, 1 failed",
            issues=["negative numbers are not handled.", "the test is too weak"],
            send_back_step="s2",
        ),
    )
    assert verifier.coder_message(review) == (
        "Review failed: negative numbers are not handled; the test is too weak. Fix step s2."
    )


def test_the_message_leaves_the_step_out_when_the_verifier_named_none() -> None:
    review = verifier.judge_review(
        [RED], VerifierVerdict(passed=True, tests_run="x", tests_summary="y")
    )
    assert verifier.coder_message(review) == (
        "Review failed: the command `pytest -q` failed with exit code 1."
    )


def test_the_message_is_one_short_line_per_issue_whatever_the_model_wrote() -> None:
    review = verifier.judge_review(
        [],
        VerifierVerdict(
            passed=False,
            tests_run="x",
            tests_summary="y",
            issues=["first\n\n   problem", "z" * 1000] + [f"extra {n}" for n in range(20)],
        ),
    )
    message = verifier.coder_message(review)
    assert "\n" not in message
    assert "first problem" in message
    assert len(message) <= verifier.MAX_MESSAGE_CHARS
    # The first five issues are written out, and the message counts the rest.
    assert message.count("; ") == verifier.MAX_ISSUES_IN_MESSAGE
    assert message.endswith(f"and {22 - verifier.MAX_ISSUES_IN_MESSAGE} more.")
    assert message.startswith("Review failed: ")
    assert message.isascii()


def test_a_review_that_passed_has_no_message_to_send() -> None:
    review = verifier.judge_review(
        [GREEN], VerifierVerdict(passed=True, tests_run="x", tests_summary="y")
    )
    with pytest.raises(ValueError, match="passed"):
        verifier.coder_message(review)


# ---------- how a run ends ----------
def test_three_failed_rounds_end_the_task_as_stuck_with_the_reason_and_the_evidence() -> None:
    review = verifier.judge_review(
        [GREEN],
        VerifierVerdict(
            passed=False,
            tests_run="pytest -q",
            tests_summary="1 failed",
            issues=["negative numbers are not handled"],
            send_back_step="s1",
        ),
    )
    outcome = verifier.stuck_outcome(3, review)
    assert outcome.kind is OutcomeKind.STUCK
    assert outcome.pr_url is None
    assert outcome.summary.startswith("the verifier sent the work back 3 times")
    assert "negative numbers are not handled" in outcome.summary
    assert len(outcome.summary) <= verifier.MAX_SUMMARY_CHARS
    assert outcome.summary.isascii()


def test_the_summary_of_a_stuck_task_is_cut_to_the_limit() -> None:
    review = verifier.judge_review(
        [],
        VerifierVerdict(passed=False, tests_run="x", tests_summary="y", issues=["word " * 200]),
    )
    summary = verifier.stuck_outcome(3, review).summary
    assert len(summary) <= verifier.MAX_SUMMARY_CHARS
    assert summary.endswith("...")


def test_a_verifier_that_never_submits_ends_the_run_with_a_clear_summary() -> None:
    outcome = verifier.no_verdict_outcome(15)
    assert outcome.kind is OutcomeKind.ERROR
    assert outcome.summary == "verifier submitted no verdict in 15 turns"


def test_a_review_is_recorded_as_a_small_event() -> None:
    review = verifier.judge_review(
        [RED],
        VerifierVerdict(
            passed=True,
            tests_run="pytest -q",
            tests_summary="41 passed",
            issues=["a" * 900],
            send_back_step="s2",
        ),
    )
    payload = verifier.verdict_event(2, review)
    assert payload["round"] == 2
    assert payload["passed"] is False
    assert payload["model_passed"] is True
    assert payload["precheck_passed"] is False
    assert payload["tests_run"] == "pytest -q"
    assert payload["tests_summary"] == "41 passed"
    assert payload["issues"] == list(review.issues)
    assert all(len(issue) <= verifier.MAX_ISSUE_CHARS for issue in payload["issues"])


# ---------- the prompt ----------
def test_the_verifier_prompt_starts_with_the_role_line_the_scripted_provider_reads() -> None:
    prompt = build_system_prompt(Role.VERIFIER, spec())
    assert parse_role_line(prompt) == "verifier"
    places = [prompt.index(heading) for heading in SECTION_HEADINGS[:3]]
    assert places == sorted(places)


def test_the_verifier_prompt_says_what_the_story_asks_for() -> None:
    prompt = build_system_prompt(Role.VERIFIER, spec())
    rules = prompt[prompt.index(SECTION_HEADINGS[0]) : prompt.index(SECTION_HEADINGS[1])]
    assert re.search(r"cannot change files", prompt)
    assert re.search(r"plan", rules, re.IGNORECASE)
    assert re.search(r"edge case", rules, re.IGNORECASE)
    assert re.search(r"fails the review whatever you", rules, re.IGNORECASE)
    assert "`submit_verdict`" in rules
    assert "`send_back_step`" in rules
    # Text from the issue is untrusted, and the verifier is told so as the other roles are.
    assert re.search(r"not instructions", rules, re.IGNORECASE)


def test_the_verifier_is_not_told_about_the_tools_of_the_other_roles() -> None:
    prompt = build_system_prompt(Role.VERIFIER, spec())
    for name in ("edit_file", "write_file", "run_command", "finish", "update_step", "submit_plan"):
        assert f"`{name}`" not in prompt


def test_the_verifier_guide_names_only_tools_and_fields_that_exist() -> None:
    prompt = build_system_prompt(Role.VERIFIER, spec())
    static = prompt[: prompt.index(SECTION_HEADINGS[2])]
    offered = verifier.tool_specs(default_registry().specs())
    known = {t.name for t in offered}
    for tool in offered:
        known |= set(tool.input_schema.get("properties", {}))
    known |= set(VerifierVerdict.model_json_schema()["properties"])
    named = set(re.findall(r"`([^`]+)`", static))
    assert named <= known, f"not tools or parameters: {sorted(named - known)}"
    for tool_name in ("list_dir", "read_file", "search", "git_diff", "run_tests"):
        assert tool_name in named
    assert "submit_verdict" in named


def test_the_verifier_guide_explains_cut_and_elided_results() -> None:
    from longhaul.context.evict import ELIDED_PREFIX

    prompt = build_system_prompt(Role.VERIFIER, spec())
    static = prompt[: prompt.index(SECTION_HEADINGS[2])]
    assert "`read_output`" in static
    assert f"starts with {ELIDED_PREFIX.strip()}" in static


def test_the_verifier_prompt_is_plain_ascii() -> None:
    assert build_system_prompt(Role.VERIFIER, spec()).isascii()
    assert verifier.FIRST_USER_MESSAGE.isascii()
    assert verifier.NUDGE.isascii()
    assert verifier.render_precheck([(GREEN, "ok")]).isascii()


def test_the_verifier_shares_the_task_and_the_memory_with_the_other_roles() -> None:
    verifier_system = build_system_prompt(Role.VERIFIER, spec(), "Tests run with pytest -q.")
    coder_system = build_system_prompt(Role.CODER, spec(), "Tests run with pytest -q.")
    assert verifier_system != coder_system
    assert [verifier_system.count(heading) for heading in SECTION_HEADINGS] == [1] * 4
    assert verifier_system.endswith(coder_system[coder_system.index(SECTION_HEADINGS[2]) :])


def test_the_verifier_starts_from_its_own_first_message() -> None:
    assert first_user_message(Role.VERIFIER) == verifier.FIRST_USER_MESSAGE
    assert "submit_verdict" in verifier.FIRST_USER_MESSAGE
    assert "finish" not in verifier.FIRST_USER_MESSAGE
    assert first_user_message(Role.CODER) == FIRST_USER_MESSAGE


def test_the_nudge_asks_for_the_verdict_and_not_for_finish() -> None:
    assert "submit_verdict" in verifier.NUDGE
    assert "finish" not in verifier.NUDGE


def test_the_planner_tells_the_planner_how_to_mark_a_command() -> None:
    from longhaul.roles import planner

    rules = " ".join(planner.RULES)
    # The pipeline runs the checks that are marked, so the planner has to know the mark. The
    # example is not in backticks, because every name in backticks has to be a tool or a field.
    assert '"$ pytest -q"' in rules
    assert verifier.command_of("$ pytest -q") == "pytest -q"
