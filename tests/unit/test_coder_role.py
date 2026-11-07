"""The coder role of a task with a plan: its tools, its prompt and the plan in its first message.

A coder with a plan works the steps one by one and says so with `update_step` (LH-503). The
pure parts are here: how a step update is read and applied, how the calls of a turn are handled,
and what the model is told. tests/integration/test_coder_steps.py runs the same rules inside
TaskWorkflow. A coder without a plan keeps `finish`, its prompt and its first message, and the
tests below show that those did not move.
"""

import re
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from longhaul.agent.prompts import (
    FIRST_USER_MESSAGE,
    PLAN_HEADING,
    SECTION_HEADINGS,
    build_planned_system_prompt,
    build_system_prompt,
    first_user_message,
    is_plan_block,
    render_plan,
    with_plan,
)
from longhaul.context.compact import NOTES_HEADING
from longhaul.contracts import (
    BlobRef,
    Message,
    OutcomeKind,
    Plan,
    PlanStep,
    RepoSettings,
    Role,
    TaskSpec,
    TextBlock,
    ToolResult,
)
from longhaul.durable import acts
from longhaul.providers.base import parse_role_line
from longhaul.roles import coder
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

MEMORY = "Tests run with pytest -q."


def step(n: int, status: str = "todo", **fields: Any) -> PlanStep:
    return PlanStep(
        id=f"s{n}",
        description=f"Change module {n}",
        files=[f"src/calc/m{n}.py"],
        done_when=f"the tests of module {n} pass",
        status=status,  # type: ignore[arg-type]
        **fields,
    )


def make_plan(*statuses: str) -> Plan:
    steps = [step(n, status) for n, status in enumerate(statuses or ("todo",) * 3, start=1)]
    return Plan(
        summary="add() subtracts, so change the operator and test it",
        steps=steps,
        acceptance_checks=["pytest -q", "add(2, 3) returns 5"],
        risks=["other callers may rely on the old result"],
    )


def spec() -> TaskSpec:
    return TaskSpec(
        task_id="lh-acme-calc-42",
        repo="acme/calc",
        issue_number=42,
        title="add() returns the difference",
        body="`calc.add` subtracts its arguments.",
        settings=RepoSettings(repo="acme/calc", test_command="pytest -q"),
    )


BY_REF = BlobRef(key="tasks/lh-x/turns/0001/args-t1.json", sha256="0" * 64, size=20_000)


def call(name: str, call_id: str, arguments: dict[str, Any] | None) -> acts.ToolCallSummary:
    """A call of the workflow's kind. `None` is a call whose arguments were over 16 KB."""
    if arguments is None:
        return acts.ToolCallSummary(id=call_id, name=name, args_ref=BY_REF)
    return acts.ToolCallSummary(id=call_id, name=name, input=arguments)


class Turn:
    """The collaborators of run_calls: a tool runner and a step recorder that keep their calls."""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.ran: list[str] = []
        self.updates: list[coder.StepUpdate] = []

    async def run(self, summary: acts.ToolCallSummary) -> ToolResult:
        self.ran.append(summary.name)
        return ToolResult(call_id=summary.id, ok=True, output=f"ran {summary.name}")

    async def update(self, update: coder.StepUpdate) -> None:
        self.updates.append(update)
        self.plan = coder.with_status(self.plan, update)

    def current(self) -> Plan:
        return self.plan

    async def go(self, *calls: acts.ToolCallSummary) -> coder.CoderCalls:
        return await coder.run_calls(list(calls), self.run, self.update, self.current)


# ---------- the tools ----------
def test_the_control_tools_are_the_three_of_the_story() -> None:
    assert coder.CONTROL_TOOLS == ("update_step", "submit_for_review", "ask_human")
    assert (coder.UPDATE_STEP, coder.SUBMIT_FOR_REVIEW, coder.ASK_HUMAN) == coder.CONTROL_TOOLS


def test_a_coder_with_a_plan_is_offered_the_registry_without_finish_and_the_control_tools() -> None:
    specs = default_registry().specs()
    offered = coder.tool_specs(specs)

    names = [t.name for t in offered]
    expected = [t.name for t in specs if t.name != "finish"]
    assert names == [*expected, *coder.CONTROL_TOOLS]
    assert "finish" not in names
    assert "submit_plan" not in names


def test_the_control_tools_are_not_tools_of_the_registry() -> None:
    # The workflow handles them, as it does finish, and a coder without a plan never sees them.
    registry = default_registry()
    for name in coder.CONTROL_TOOLS:
        assert registry.get(name) is None


def test_every_spec_has_a_valid_schema_and_a_description() -> None:
    for spec_ in (coder.UPDATE_STEP_SPEC, coder.SUBMIT_FOR_REVIEW_SPEC, coder.ASK_HUMAN_SPEC):
        Draft202012Validator.check_schema(spec_.input_schema)
        assert spec_.input_schema["type"] == "object"
        assert spec_.input_schema["additionalProperties"] is False
        assert spec_.description.strip()


def test_update_step_takes_a_step_id_a_status_and_a_note() -> None:
    schema = coder.UPDATE_STEP_SPEC.input_schema
    assert set(schema["properties"]) == {"step_id", "status", "note"}
    assert schema["required"] == ["step_id", "status"]
    # The statuses are those of the plan contract, so the tool and the contract cannot drift.
    plan_statuses = PlanStep.model_json_schema()["properties"]["status"]["enum"]
    assert schema["properties"]["status"]["enum"] == plan_statuses == list(coder.STATUSES)


def test_submit_for_review_and_ask_human_each_take_one_text() -> None:
    review = coder.SUBMIT_FOR_REVIEW_SPEC.input_schema
    assert review["required"] == ["summary"] and set(review["properties"]) == {"summary"}
    ask = coder.ASK_HUMAN_SPEC.input_schema
    assert ask["required"] == ["question"] and set(ask["properties"]) == {"question"}


def test_a_registry_without_finish_still_gives_the_list() -> None:
    specs = [s for s in default_registry().specs() if s.name != "finish"]
    assert [t.name for t in coder.tool_specs(specs)][-3:] == list(coder.CONTROL_TOOLS)


# ---------- reading a step update ----------
def test_a_valid_update_is_read_with_its_note() -> None:
    plan = make_plan()
    got = coder.read_update(plan, {"step_id": "s2", "status": "done", "note": "  tests pass  "})
    assert got == coder.StepUpdate("s2", "done", "tests pass")


def test_the_note_is_optional() -> None:
    assert coder.read_update(make_plan(), {"step_id": "s1", "status": "doing"}) == (
        coder.StepUpdate("s1", "doing", "")
    )


def test_a_long_note_is_cut() -> None:
    got = coder.read_update(make_plan(), {"step_id": "s1", "status": "done", "note": "word " * 500})
    assert isinstance(got, coder.StepUpdate)
    assert len(got.note) <= coder.MAX_NOTE_CHARS


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        pytest.param({"step_id": "s9", "status": "done"}, "unknown step 's9'", id="unknown step"),
        pytest.param({"step_id": "s9", "status": "done"}, "s1, s2, s3", id="lists the steps"),
        pytest.param(
            {"step_id": "s1", "status": "finished"},
            "status must be one of todo, doing, done, skipped",
            id="bad status",
        ),
        pytest.param({"status": "done"}, "step_id", id="no step id"),
        pytest.param({"step_id": "s1"}, "status", id="no status"),
        pytest.param({"step_id": 1, "status": "done"}, "step_id", id="step id is not text"),
        pytest.param({"step_id": "s1", "status": "done", "note": 3}, "note", id="note not text"),
        pytest.param(None, "too long", id="arguments by reference"),
    ],
)
def test_an_update_that_does_not_fit_the_plan_is_answered_with_the_problem(
    arguments: dict[str, Any] | None, problem: str
) -> None:
    got = coder.read_update(make_plan(), arguments)
    assert isinstance(got, str)
    assert problem in got


def test_with_status_changes_one_step_and_leaves_the_plan_it_was_given() -> None:
    plan = make_plan()
    changed = coder.with_status(plan, coder.StepUpdate("s2", "done", ""))

    assert [s.status for s in changed.steps] == ["todo", "done", "todo"]
    assert [s.status for s in plan.steps] == ["todo", "todo", "todo"]
    assert changed.summary == plan.summary
    assert changed.acceptance_checks == plan.acceptance_checks
    assert changed.steps[0] == plan.steps[0]


# ---------- the calls of a turn ----------
async def test_the_calls_are_run_in_order_and_the_updates_are_applied_as_they_come() -> None:
    turn = Turn(make_plan())
    got = await turn.go(
        call("edit_file", "t1", {"path": "a.py"}),
        call("update_step", "t2", {"step_id": "s1", "status": "done"}),
        call("update_step", "t3", {"step_id": "s2", "status": "doing"}),
        call("run_tests", "t4", {}),
    )

    assert turn.ran == ["edit_file", "run_tests"]
    assert [(u.step_id, u.status) for u in turn.updates] == [("s1", "done"), ("s2", "doing")]
    assert [(r.call_id, r.ok) for r in got.results] == [
        ("t1", True),
        ("t2", True),
        ("t3", True),
        ("t4", True),
    ]
    assert "s1" in got.results[1].output and "done" in got.results[1].output
    assert got.submitted is None and got.question is None


async def test_an_update_that_is_wrong_is_answered_and_nothing_is_recorded() -> None:
    turn = Turn(make_plan())
    got = await turn.go(call("update_step", "t1", {"step_id": "s7", "status": "done"}))

    assert turn.updates == []
    (answer,) = got.results
    assert (answer.call_id, answer.ok) == ("t1", False)
    assert "unknown step 's7'" in answer.output


async def test_a_later_update_sees_the_statuses_the_earlier_one_set() -> None:
    # The same step twice in one reply: the second call is judged against the plan as it is now.
    turn = Turn(make_plan())
    await turn.go(
        call("update_step", "t1", {"step_id": "s1", "status": "doing"}),
        call("update_step", "t2", {"step_id": "s1", "status": "done"}),
    )
    assert [s.status for s in turn.plan.steps] == ["done", "todo", "todo"]


async def test_submit_for_review_ends_the_turn_and_answers_the_calls_after_it() -> None:
    turn = Turn(make_plan("done", "done", "done"))
    got = await turn.go(
        call("submit_for_review", "t1", {"summary": "  Fixed add().  "}),
        call("run_tests", "t2", {}),
        call("update_step", "t3", {"step_id": "s1", "status": "todo"}),
    )

    assert got.submitted == "Fixed add()."
    assert got.question is None
    assert turn.ran == [] and turn.updates == []
    assert [(r.call_id, r.ok) for r in got.results] == [("t1", True), ("t2", False), ("t3", False)]
    assert "submit_for_review" in got.results[1].output


async def test_finish_still_ends_a_task_that_has_a_plan() -> None:
    # A coder with a plan is not offered finish, but a call to it must not run a tool that does
    # nothing. It ends the task as submit_for_review does, so a worker or a history from before
    # the story keeps working.
    turn = Turn(make_plan())
    got = await turn.go(call("finish", "t1", {"summary": "Fixed add()."}))
    assert got.submitted == "Fixed add()."
    assert turn.ran == []


async def test_only_submit_for_review_sends_the_work_to_the_verifier() -> None:
    # A history from before the verifier ends on finish, and it must end the way it did. The
    # work of a submit_for_review is reviewed before a pull request is marked ready (LH-504).
    submitted = await Turn(make_plan()).go(call("submit_for_review", "t1", {"summary": "Done."}))
    finished = await Turn(make_plan()).go(call("finish", "t1", {"summary": "Done."}))
    asked = await Turn(make_plan()).go(call("ask_human", "t1", {"question": "Which one?"}))

    assert (submitted.submitted, submitted.review) == ("Done.", True)
    assert (finished.submitted, finished.review) == ("Done.", False)
    assert (asked.submitted, asked.review) == (None, False)


@pytest.mark.parametrize("name", ["submit_for_review", "finish"])
@pytest.mark.parametrize("arguments", [{}, {"summary": "  "}, {"summary": 5}, None])
async def test_a_submission_without_a_summary_is_answered_and_the_task_goes_on(
    name: str, arguments: dict[str, Any] | None
) -> None:
    turn = Turn(make_plan())
    got = await turn.go(call(name, "t1", arguments), call("run_tests", "t2", {}))

    assert got.submitted is None
    assert [(r.call_id, r.ok) for r in got.results] == [("t1", False), ("t2", True)]
    assert "summary" in got.results[0].output
    assert turn.ran == ["run_tests"]


async def test_ask_human_ends_the_turn_with_the_question() -> None:
    turn = Turn(make_plan())
    got = await turn.go(
        call("edit_file", "t1", {"path": "a.py"}),
        call("ask_human", "t2", {"question": " Should add() accept floats? "}),
        call("run_tests", "t3", {}),
    )

    assert got.question == "Should add() accept floats?"
    assert got.submitted is None
    assert turn.ran == ["edit_file"]
    assert [(r.call_id, r.ok) for r in got.results] == [("t1", True), ("t2", True), ("t3", False)]
    assert "ask_human" in got.results[2].output


@pytest.mark.parametrize("arguments", [{}, {"question": ""}, {"question": 3}, None])
async def test_a_question_without_text_is_answered_and_the_task_goes_on(
    arguments: dict[str, Any] | None,
) -> None:
    turn = Turn(make_plan())
    got = await turn.go(call("ask_human", "t1", arguments))
    assert got.question is None
    assert [(r.call_id, r.ok) for r in got.results] == [("t1", False)]
    assert "question" in got.results[0].output


async def test_an_error_from_a_tool_is_not_caught() -> None:
    turn = Turn(make_plan())

    async def broken(summary: acts.ToolCallSummary) -> ToolResult:
        raise ConnectionError("the sandbox is gone")

    with pytest.raises(ConnectionError):
        await coder.run_calls([call("run_tests", "t1", {})], broken, turn.update, turn.current)


async def test_an_error_from_the_recorder_is_not_caught() -> None:
    turn = Turn(make_plan())

    async def broken(update: coder.StepUpdate) -> None:
        raise ConnectionError("the database is down")

    with pytest.raises(ConnectionError):
        await coder.run_calls(
            [call("update_step", "t1", {"step_id": "s1", "status": "done"})],
            turn.run,
            broken,
            turn.current,
        )


def test_the_summary_of_a_task_that_asked_a_person_names_the_question() -> None:
    outcome = coder.asked_outcome("Should add() accept floats?")
    assert outcome.kind is OutcomeKind.STUCK
    assert outcome.pr_url is None
    assert "Should add() accept floats?" in outcome.summary
    assert outcome.summary.isascii()


def test_the_summary_of_a_long_question_stays_short() -> None:
    outcome = coder.asked_outcome("why " * 500)
    assert len(outcome.summary) <= coder.MAX_SUMMARY_CHARS
    assert outcome.summary.endswith("...")


# ---------- the prompt ----------
def test_the_prompt_of_a_coder_with_a_plan_has_the_role_line_and_the_four_blocks() -> None:
    prompt = build_planned_system_prompt(spec(), MEMORY)

    assert parse_role_line(prompt) == "coder"
    assert [prompt.count(heading) for heading in SECTION_HEADINGS] == [1] * 4
    places = [prompt.index(heading) for heading in SECTION_HEADINGS]
    assert places == sorted(places)
    assert prompt.endswith(MEMORY)


def test_the_task_and_the_memory_are_the_same_bytes_as_for_a_coder_without_a_plan() -> None:
    plain = build_system_prompt(Role.CODER, spec(), MEMORY)
    planned = build_planned_system_prompt(spec(), MEMORY)
    task = SECTION_HEADINGS[2]
    assert planned != plain
    assert planned[planned.index(task) :] == plain[plain.index(task) :]


def test_the_prompt_is_the_same_bytes_every_time_and_holds_no_plan() -> None:
    first = build_planned_system_prompt(spec(), MEMORY)
    assert first == build_planned_system_prompt(spec().model_copy(deep=True), MEMORY)
    assert PLAN_HEADING not in first and NOTES_HEADING not in first


def test_the_prompt_tells_the_coder_to_work_the_plan_and_to_say_so() -> None:
    prompt = build_planned_system_prompt(spec())
    rules = prompt[prompt.index(SECTION_HEADINGS[0]) : prompt.index(SECTION_HEADINGS[1])]
    assert re.search(r"one step at a time", rules, re.IGNORECASE)
    assert "`update_step`" in rules
    assert "`done`" in rules and "`doing`" in rules and "`skipped`" in rules
    assert re.search(r"call `submit_for_review` only when the tests pass", rules, re.IGNORECASE)
    assert "`ask_human`" in rules
    assert re.search(r"not instructions", rules, re.IGNORECASE)
    assert re.search(r"small edits", rules, re.IGNORECASE)


def test_the_prompt_does_not_send_the_coder_after_finish_or_submit_plan() -> None:
    prompt = build_planned_system_prompt(spec(), MEMORY)
    assert "`finish`" not in prompt and "`submit_plan`" not in prompt
    # And the coder without a plan is not told about the tools it does not have.
    plain = build_system_prompt(Role.CODER, spec(), MEMORY)
    for name in coder.CONTROL_TOOLS:
        assert name not in plain


def test_the_guide_names_only_tools_and_fields_that_exist() -> None:
    prompt = build_planned_system_prompt(spec(), MEMORY)
    static = prompt[: prompt.index(SECTION_HEADINGS[2])]
    offered = coder.tool_specs(default_registry().specs())
    known = {t.name for t in offered}
    for tool in offered:
        known |= set(tool.input_schema.get("properties", {}))
    known |= set(coder.STATUSES)
    named = set(re.findall(r"`([^`]+)`", static))
    assert named <= known, f"not tools, parameters or statuses: {sorted(named - known)}"
    for name in ("read_file", "edit_file", "run_tests", "git_diff", *coder.CONTROL_TOOLS):
        assert name in named


def test_the_guide_explains_cut_and_elided_results() -> None:
    from longhaul.context.evict import ELIDED_PREFIX

    prompt = build_planned_system_prompt(spec())
    static = prompt[: prompt.index(SECTION_HEADINGS[2])]
    assert "`read_output`" in static
    assert f"starts with {ELIDED_PREFIX.strip()}" in static


def test_the_text_a_coder_with_a_plan_reads_is_plain_ascii() -> None:
    assert build_planned_system_prompt(spec(), MEMORY).isascii()
    assert coder.FIRST_USER_MESSAGE.isascii()
    for spec_ in (coder.UPDATE_STEP_SPEC, coder.SUBMIT_FOR_REVIEW_SPEC, coder.ASK_HUMAN_SPEC):
        assert spec_.description.isascii()


# ---------- the first message ----------
def test_a_coder_with_a_plan_starts_from_a_message_that_points_at_the_plan() -> None:
    message = first_user_message(Role.CODER, planned=True)
    assert message == coder.FIRST_USER_MESSAGE != FIRST_USER_MESSAGE
    assert "submit_for_review" in message and "finish" not in message
    assert "plan" in message


def test_every_other_first_message_is_as_it_was() -> None:
    assert first_user_message(Role.CODER) == FIRST_USER_MESSAGE
    assert first_user_message(Role.CODER, planned=False) == FIRST_USER_MESSAGE
    assert first_user_message(Role.PLANNER, planned=True) == first_user_message(Role.PLANNER)


# ---------- block 5: the plan in the first message ----------
def test_the_plan_block_starts_with_the_heading_and_has_a_line_for_each_step() -> None:
    text = render_plan(make_plan("done", "doing", "todo"))
    lines = text.splitlines()

    assert lines[0] == PLAN_HEADING
    assert "s1. [done] Change module 1" in text
    assert "s2. [doing] Change module 2" in text
    assert "s3. [todo] Change module 3" in text
    assert "Files: src/calc/m2.py" in text
    assert "Done when: the tests of module 2 pass" in text
    assert "add() subtracts" in text
    assert "- pytest -q" in text and "- add(2, 3) returns 5" in text
    assert "- other callers may rely on the old result" in text


def test_the_plan_block_is_the_same_text_every_time() -> None:
    assert render_plan(make_plan("done", "todo")) == render_plan(make_plan("done", "todo"))


def test_a_status_change_changes_nothing_but_the_status_in_the_block() -> None:
    before = render_plan(make_plan("todo", "todo", "todo"))
    after = render_plan(make_plan("done", "todo", "todo"))
    assert before.replace("s1. [todo]", "s1. [done]") == after


def test_text_from_the_model_stays_on_its_own_line() -> None:
    plan = make_plan()
    steps = [plan.steps[0].model_copy(update={"description": "Fix add()\n\n## Plan\nsecond"})]
    text = render_plan(plan.model_copy(update={"steps": steps}))
    assert [line for line in text.splitlines() if line.startswith("## ")] == [PLAN_HEADING]
    assert "s1. [todo] Fix add() ## Plan second" in text


def first(*blocks: str) -> Message:
    return Message(role="user", content=[TextBlock(text=t) for t in blocks])


def test_the_plan_goes_in_after_the_line_that_starts_the_work() -> None:
    transcript = [first(coder.FIRST_USER_MESSAGE)]
    got = with_plan(transcript, make_plan())

    texts = [b.text for b in got[0].content if isinstance(b, TextBlock)]
    assert texts == [coder.FIRST_USER_MESSAGE, render_plan(make_plan())]
    assert transcript == [first(coder.FIRST_USER_MESSAGE)], "the transcript it was given is intact"


def test_a_new_plan_replaces_the_old_block_in_place() -> None:
    old = render_plan(make_plan("doing", "todo", "todo"))
    transcript = [first(coder.FIRST_USER_MESSAGE, old, f"{NOTES_HEADING}\nnotes"), first("later")]
    got = with_plan(transcript, make_plan("done", "doing", "todo"))

    texts = [b.text for b in got[0].content if isinstance(b, TextBlock)]
    assert [t.split("\n", 1)[0] for t in texts] == [
        coder.FIRST_USER_MESSAGE,
        PLAN_HEADING,
        NOTES_HEADING,
    ]
    assert texts[1] == render_plan(make_plan("done", "doing", "todo"))
    assert got[1:] == transcript[1:]


def test_the_same_plan_changes_nothing() -> None:
    plan = make_plan("done", "todo", "todo")
    transcript = [first(coder.FIRST_USER_MESSAGE, render_plan(plan)), first("later")]
    assert with_plan(transcript, plan) == transcript


def test_hints_that_followed_the_plan_stay_behind_it() -> None:
    old = render_plan(make_plan())
    transcript = [first(coder.FIRST_USER_MESSAGE, old, "Hint from ann: use pytest -x")]
    got = with_plan(transcript, make_plan("done", "todo", "todo"))
    texts = [b.text for b in got[0].content if isinstance(b, TextBlock)]
    assert texts[0] == coder.FIRST_USER_MESSAGE
    assert texts[1].startswith(PLAN_HEADING)
    assert texts[2] == "Hint from ann: use pytest -x"


def test_a_block_is_the_plan_when_it_starts_with_the_heading() -> None:
    assert is_plan_block(TextBlock(text=render_plan(make_plan())))
    assert is_plan_block(TextBlock(text=f"\n{PLAN_HEADING}\nx"))
    assert not is_plan_block(TextBlock(text=coder.FIRST_USER_MESSAGE))
    assert not is_plan_block(TextBlock(text=f"{NOTES_HEADING}\n{PLAN_HEADING}"))
