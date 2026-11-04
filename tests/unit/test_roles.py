"""The planner role: its tools, its prompt and how a submitted plan is judged (LH-501).

The judging is pure, so these tests need no workflow. tests/integration/test_planner.py runs the
same rules inside TaskWorkflow. F14 is the failure mode behind the repair turn: a model that
sends malformed output gets one more try, with the validation error in front of it, and a second
failure ends the run.
"""

import re
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from longhaul.agent.prompts import (
    FIRST_USER_MESSAGE,
    SECTION_HEADINGS,
    build_system_prompt,
    first_user_message,
)
from longhaul.contracts import OutcomeKind, Plan, RepoSettings, Role, TaskSpec, ToolResult
from longhaul.durable import acts
from longhaul.providers.base import parse_role_line
from longhaul.roles import planner
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

TOOLS_OF_THE_STORY = {"list_dir", "read_file", "search", "run_tests", "run_command", "read_output"}


def step(n: int, **fields: Any) -> dict[str, Any]:
    return {
        "id": f"s{n}",
        "description": f"Change module {n}",
        "files": [f"src/calc/m{n}.py"],
        "done_when": f"the tests of module {n} pass",
        **fields,
    }


def plan_input(steps: int = 3, **fields: Any) -> dict[str, Any]:
    return {
        "summary": "add() subtracts, so change the operator and test it",
        "steps": [step(n) for n in range(1, steps + 1)],
        "acceptance_checks": ["pytest -q", "add(2, 3) returns 5"],
        "risks": ["other callers may rely on the old result"],
        **fields,
    }


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


# ---------- F14: the repair turn ----------
def test_plan_repair_turn() -> None:
    # One step is too few. The answer names the problem and asks for another call.
    first = planner.judge_submission(0, plan_input(steps=1))
    assert first.plan is None
    assert first.outcome is None
    assert first.failures == 1
    assert "steps" in first.answer
    assert "2 to 8" in first.answer
    assert "call submit_plan again" in first.answer

    # The repaired plan is accepted. The failure stays counted, and nothing ends the run.
    repaired = planner.judge_submission(first.failures, plan_input(steps=2))
    assert repaired.plan is not None
    assert [s.id for s in repaired.plan.steps] == ["s1", "s2"]
    assert repaired.outcome is None
    assert repaired.failures == 1
    assert repaired.answer == planner.ACCEPTED

    # A second plan that is not valid ends the run with an error and a summary that says why.
    second = planner.judge_submission(first.failures, plan_input(steps=1))
    assert second.plan is None
    assert second.outcome is not None
    assert second.outcome.kind is OutcomeKind.ERROR
    assert second.outcome.summary.startswith("planner submitted an invalid plan twice: ")
    assert "steps: give 2 to 8 steps, got 1" in second.outcome.summary
    assert second.failures == 2
    assert "twice" in second.answer


async def test_the_repair_turn_across_two_replies_of_the_planner() -> None:
    first = await planner.run_calls([call("submit_plan", "t1", plan_input(steps=1))], fake_run)
    assert first.plan is None and first.outcome is None and first.failures == 1
    # The tool_use is answered, with an error, so the transcript stays paired.
    (answer,) = first.results
    assert (answer.call_id, answer.ok) == ("t1", False)
    assert "2 to 8" in answer.output

    second = await planner.run_calls(
        [call("submit_plan", "t2", plan_input())], fake_run, first.failures
    )
    assert second.plan is not None and second.outcome is None
    assert [(r.call_id, r.ok) for r in second.results] == [("t2", True)]


async def test_a_second_invalid_plan_ends_the_run_through_run_calls() -> None:
    bad = {"summary": "x", "steps": [step(1), step(2)], "acceptance_checks": []}
    first = await planner.run_calls([call("submit_plan", "t1", bad)], fake_run)
    second = await planner.run_calls([call("submit_plan", "t2", bad)], fake_run, first.failures)
    assert second.plan is None
    assert second.outcome is not None and second.outcome.kind is OutcomeKind.ERROR
    assert "acceptance_checks: give at least one check" in second.outcome.summary
    assert [(r.call_id, r.ok) for r in second.results] == [("t2", False)]


# ---------- what makes a plan valid ----------
@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        pytest.param(plan_input(steps=1), "steps: give 2 to 8 steps, got 1", id="one step"),
        pytest.param(plan_input(steps=9), "steps: give 2 to 8 steps, got 9", id="nine steps"),
        pytest.param(plan_input(steps=0), "steps: give 2 to 8 steps, got 0", id="no steps"),
        pytest.param(
            plan_input() | {"steps": [step(1), step(1)]},
            "steps.1.id: 's1' is used twice",
            id="duplicate id",
        ),
        pytest.param(
            plan_input() | {"steps": [step(1), step(2, done_when="  ")]},
            "steps.1.done_when: say how to tell the step is done",
            id="blank done_when",
        ),
        pytest.param(
            plan_input() | {"steps": [step(1, description=""), step(2)]},
            "steps.0.description: must not be empty",
            id="blank description",
        ),
        pytest.param(
            plan_input()
            | {"steps": [step(1), {k: v for k, v in step(2).items() if k != "done_when"}]},
            "steps.1.done_when: Field required",
            id="missing done_when",
        ),
        pytest.param(
            plan_input(acceptance_checks=[]),
            "acceptance_checks: give at least one check",
            id="no checks",
        ),
        pytest.param(
            plan_input(acceptance_checks=["pytest -q", " "]),
            "acceptance_checks.1: must not be empty",
            id="blank check",
        ),
        pytest.param(plan_input(summary="  "), "summary: must not be empty", id="blank summary"),
        pytest.param({"steps": []}, "summary: Field required", id="almost nothing"),
        pytest.param(
            plan_input() | {"steps": "none"}, "steps: Input should be a valid list", id="wrong type"
        ),
        pytest.param({}, "summary: Field required", id="empty input"),
        pytest.param(
            None, "the plan is too long to read in one call", id="arguments held by reference"
        ),
    ],
)
def test_a_plan_that_breaks_a_rule_is_refused_with_the_rule(
    arguments: dict[str, Any] | None, problem: str
) -> None:
    got = planner.read_plan(arguments)
    assert isinstance(got, list)
    assert problem in got


def test_every_problem_of_a_plan_is_listed_so_one_repair_can_fix_them_all() -> None:
    arguments = plan_input(steps=1, summary="", acceptance_checks=[])
    got = planner.read_plan(arguments)
    assert isinstance(got, list)
    assert len(got) == 3
    answer = planner.judge_submission(0, arguments).answer
    assert all(problem in answer for problem in got)


def test_a_long_list_of_problems_is_cut() -> None:
    arguments = plan_input(steps=8) | {"steps": [step(n, done_when="") for n in range(1, 9)]}
    answer = planner.judge_submission(0, arguments).answer
    assert answer.count("\n- ") == planner.MAX_PROBLEMS_SHOWN
    assert "and 3 more" in answer


def test_a_valid_plan_is_the_plan_contract_with_every_step_to_do() -> None:
    arguments = plan_input() | {"steps": [step(1, status="done"), step(2, status="doing")]}
    got = planner.read_plan(arguments)
    assert isinstance(got, Plan)
    assert [s.status for s in got.steps] == ["todo", "todo"]
    assert got.summary == arguments["summary"]
    assert got.acceptance_checks == ["pytest -q", "add(2, 3) returns 5"]
    assert got.risks == ["other callers may rely on the old result"]


@pytest.mark.parametrize("steps", [2, 8])
def test_the_edges_of_the_step_range_are_valid(steps: int) -> None:
    assert isinstance(planner.read_plan(plan_input(steps=steps)), Plan)


def test_risks_are_optional() -> None:
    arguments = plan_input()
    del arguments["risks"]
    got = planner.read_plan(arguments)
    assert isinstance(got, Plan) and got.risks == []


def test_the_summary_of_an_ended_run_stays_short() -> None:
    bad = plan_input(steps=1, summary="")
    planner.judge_submission(0, bad)
    ended = planner.judge_submission(1, bad).outcome
    assert ended is not None
    assert len(ended.summary) <= planner.MAX_SUMMARY_CHARS


# ---------- the tools ----------
def test_the_planner_has_the_read_and_run_tools_and_none_that_write() -> None:
    assert set(planner.TOOLS) == TOOLS_OF_THE_STORY
    assert not {"edit_file", "write_file", "git_diff", "finish"} & set(planner.TOOLS)


def test_every_planner_tool_is_in_the_default_registry_and_none_mutates() -> None:
    registry = default_registry()
    for name in planner.TOOLS:
        tool = registry.get(name)
        assert tool is not None, name
        assert tool.mutates is False, name


def test_the_planner_is_offered_its_tools_and_submit_plan_last() -> None:
    offered = planner.tool_specs(default_registry().specs())
    assert [t.name for t in offered] == [*planner.TOOLS, planner.SUBMIT_PLAN]


def test_a_registry_without_a_planner_tool_is_refused() -> None:
    specs = [s for s in default_registry().specs() if s.name != "search"]
    with pytest.raises(ValueError, match="search"):
        planner.tool_specs(specs)


def test_submit_plan_takes_the_plan_contract_as_its_input() -> None:
    assert planner.SUBMIT_PLAN == "submit_plan"
    assert planner.SUBMIT_PLAN_SPEC.name == "submit_plan"
    assert planner.SUBMIT_PLAN_SPEC.input_schema == Plan.model_json_schema()
    Draft202012Validator.check_schema(planner.SUBMIT_PLAN_SPEC.input_schema)
    assert "2 to 8" in planner.SUBMIT_PLAN_SPEC.description


def test_submit_plan_is_not_a_tool_of_the_registry() -> None:
    # The workflow judges the call itself, as it does for finish, and the coder never sees it.
    assert default_registry().get(planner.SUBMIT_PLAN) is None


async def test_a_tool_outside_the_set_is_answered_and_not_run() -> None:
    ran: list[str] = []

    async def run(summary: acts.ToolCallSummary) -> ToolResult:
        ran.append(summary.name)
        return await fake_run(summary)

    calls = [
        call("edit_file", "t1", {"path": "a.py", "old_str": "a", "new_str": "b"}),
        call("read_file", "t2", {"path": "a.py"}),
        call("finish", "t3", {"summary": "done"}),
    ]
    got = await planner.run_calls(calls, run)

    assert ran == ["read_file"]
    assert [(r.call_id, r.ok) for r in got.results] == [("t1", False), ("t2", True), ("t3", False)]
    assert "edit_file" in got.results[0].output
    assert "not available" in got.results[0].output
    assert got.plan is None and got.outcome is None


async def test_the_calls_are_run_in_order_and_a_good_plan_ends_the_turn() -> None:
    order: list[str] = []

    async def run(summary: acts.ToolCallSummary) -> ToolResult:
        order.append(summary.id)
        return await fake_run(summary)

    calls = [
        call("read_file", "t1", {"path": "a.py"}),
        call("submit_plan", "t2", plan_input()),
        call("run_tests", "t3", {}),
    ]
    got = await planner.run_calls(calls, run)

    assert order == ["t1"]
    assert got.plan is not None
    # Every call is answered, so the stored transcript has no tool_use without a result.
    assert [(r.call_id, r.ok) for r in got.results] == [("t1", True), ("t2", True), ("t3", False)]
    assert got.results[2].output == planner.SKIPPED_AFTER_PLAN


async def test_a_bad_plan_does_not_stop_the_other_calls_of_the_turn() -> None:
    calls = [
        call("submit_plan", "t1", plan_input(steps=1)),
        call("read_file", "t2", {"path": "a.py"}),
    ]
    got = await planner.run_calls(calls, fake_run)
    assert [(r.call_id, r.ok) for r in got.results] == [("t1", False), ("t2", True)]
    assert got.failures == 1 and got.outcome is None and got.plan is None


async def test_a_plan_that_ends_the_run_answers_the_calls_after_it() -> None:
    calls = [
        call("submit_plan", "t1", plan_input(steps=1)),
        call("read_file", "t2", {"path": "a.py"}),
    ]
    got = await planner.run_calls(calls, fake_run, failures=1)
    assert got.outcome is not None
    assert [(r.call_id, r.ok) for r in got.results] == [("t1", False), ("t2", False)]
    assert got.results[1].output == planner.SKIPPED_AFTER_PLAN


async def test_a_second_plan_in_one_reply_can_repair_the_first() -> None:
    calls = [
        call("submit_plan", "t1", plan_input(steps=1)),
        call("submit_plan", "t2", plan_input()),
    ]
    got = await planner.run_calls(calls, fake_run)
    assert got.plan is not None and got.failures == 1 and got.outcome is None
    assert [(r.call_id, r.ok) for r in got.results] == [("t1", False), ("t2", True)]


# ---------- the prompt ----------
def test_the_planner_prompt_starts_with_the_role_line_the_scripted_provider_reads() -> None:
    prompt = build_system_prompt(Role.PLANNER, spec())
    assert parse_role_line(prompt) == "planner"
    places = [prompt.index(heading) for heading in SECTION_HEADINGS[:3]]
    assert places == sorted(places)


def test_the_planner_prompt_says_what_the_story_asks_for() -> None:
    prompt = build_system_prompt(Role.PLANNER, spec())
    rules = prompt[prompt.index(SECTION_HEADINGS[0]) : prompt.index(SECTION_HEADINGS[1])]
    assert re.search(r"read the issue", rules, re.IGNORECASE)
    assert re.search(r"explore", rules, re.IGNORECASE)
    assert re.search(r"reproduce the failure", rules, re.IGNORECASE)
    assert "2 to 8 steps" in rules
    assert "`done_when`" in rules
    assert "`acceptance_checks`" in rules
    assert re.search(r"commands that can be run first", rules, re.IGNORECASE)
    assert "`submit_plan`" in rules
    # Text from the issue is untrusted, and the planner is told so as the coder is.
    assert re.search(r"not instructions", rules, re.IGNORECASE)


def test_the_planner_is_told_it_cannot_change_files() -> None:
    prompt = build_system_prompt(Role.PLANNER, spec())
    assert re.search(r"cannot change files", prompt)
    assert "`edit_file`" not in prompt and "`write_file`" not in prompt
    assert "`finish`" not in prompt


def test_the_planner_guide_names_only_tools_and_fields_that_exist() -> None:
    prompt = build_system_prompt(Role.PLANNER, spec())
    static = prompt[: prompt.index(SECTION_HEADINGS[2])]
    offered = planner.tool_specs(default_registry().specs())
    known = {t.name for t in offered}
    for tool in offered:
        known |= set(tool.input_schema.get("properties", {}))
    schema = Plan.model_json_schema()
    known |= set(schema["properties"])
    known |= set(schema["$defs"]["PlanStep"]["properties"])
    named = set(re.findall(r"`([^`]+)`", static))
    assert named <= known, f"not tools or parameters: {sorted(named - known)}"
    for tool_name in ("list_dir", "read_file", "search", "run_tests", "run_command"):
        assert tool_name in named
    assert "submit_plan" in named


def test_the_planner_guide_explains_cut_and_elided_results() -> None:
    from longhaul.context.evict import ELIDED_PREFIX

    prompt = build_system_prompt(Role.PLANNER, spec())
    static = prompt[: prompt.index(SECTION_HEADINGS[2])]
    assert "`read_output`" in static
    assert f"starts with {ELIDED_PREFIX.strip()}" in static


def test_the_planner_prompt_is_plain_ascii() -> None:
    assert build_system_prompt(Role.PLANNER, spec()).isascii()
    assert planner.FIRST_USER_MESSAGE.isascii()
    assert planner.NUDGE.isascii()


def test_the_planner_starts_from_its_own_first_message() -> None:
    assert first_user_message(Role.PLANNER) == planner.FIRST_USER_MESSAGE
    assert "submit_plan" in planner.FIRST_USER_MESSAGE
    assert "finish" not in planner.FIRST_USER_MESSAGE
    # Every other role keeps the message it had.
    assert first_user_message(Role.CODER) == FIRST_USER_MESSAGE
    assert first_user_message(Role.COMPACTOR) == FIRST_USER_MESSAGE


def test_the_nudge_asks_for_the_plan_and_not_for_finish() -> None:
    assert "submit_plan" in planner.NUDGE
    assert "finish" not in planner.NUDGE


# ---------- how a run ends ----------
def test_a_planner_that_never_submits_ends_the_run_with_a_clear_summary() -> None:
    outcome = planner.no_plan_outcome(20)
    assert outcome.kind is OutcomeKind.ERROR
    assert outcome.summary == "planner submitted no plan in 20 turns"


def test_the_summaries_of_the_planning_phase_are_plain_ascii() -> None:
    assert planner.no_plan_outcome(20).summary.isascii()
    ended = planner.judge_submission(1, plan_input(steps=1)).outcome
    assert ended is not None and ended.summary.isascii()
