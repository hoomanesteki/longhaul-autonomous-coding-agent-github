"""The system prompt: fixed section order, the same bytes for the same inputs, static blocks only.

D7: the prompt holds the static blocks and is frozen for the life of a transcript, so the cache
breakpoint at its end stays valid. Plan and progress notes are added as messages later, so the
builder takes neither.
"""

import inspect
import re
from typing import Any

import pytest

from longhaul.agent.prompts import (
    FIRST_USER_MESSAGE,
    SECTION_HEADINGS,
    build_system_prompt,
)
from longhaul.contracts import RepoSettings, Role, TaskSpec
from longhaul.providers.base import format_role_line, parse_role_line
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

RULES, TOOLS, TASK, FACTS = SECTION_HEADINGS
MEMORY = "Tests run with pytest -q.\nThe package lives in src/calc."


def make_spec(**changes: Any) -> TaskSpec:
    fields: dict[str, Any] = {
        "task_id": "lh-acme-calc-42",
        "repo": "acme/calc",
        "issue_number": 42,
        "title": "add() returns the difference",
        "body": "`calc.add` subtracts its arguments.\n\nRepro: run pytest.",
        "comments": ["Seen on 0.1.0 too.", "Only add() is wrong."],
        "base_branch": "main",
        "settings": RepoSettings(repo="acme/calc", test_command="pytest -q"),
    }
    return TaskSpec(**{**fields, **changes})


def position(prompt: str, text: str) -> int:
    assert prompt.count(text) == 1, f"{text!r} should appear once, not {prompt.count(text)} times"
    return prompt.index(text)


def test_section_order_stable() -> None:
    prompt = build_system_prompt(Role.CODER, make_spec(), MEMORY)
    # The scripted provider reads the role from the first line.
    assert prompt.split("\n", 1)[0] == format_role_line(Role.CODER)
    assert parse_role_line(prompt) == "coder"
    # Rules, tool guide, task statement, repo facts, in that order, each once.
    places = [position(prompt, heading) for heading in SECTION_HEADINGS]
    assert places == sorted(places)
    # The task text sits inside its own section and the memory block closes the prompt.
    task, facts = places[2], places[3]
    for text in ("add() returns the difference", "`calc.add` subtracts", "Seen on 0.1.0 too."):
        assert task < position(prompt, text) < facts
    assert position(prompt, MEMORY) > facts
    assert prompt.endswith(MEMORY)


def test_same_inputs_give_the_same_bytes() -> None:
    spec = make_spec()
    first = build_system_prompt(Role.CODER, spec, MEMORY)
    # An equal spec that is a different object, and a second call, must change nothing.
    again = build_system_prompt(Role.CODER, spec.model_copy(deep=True), MEMORY)
    assert (
        first.encode() == again.encode() == build_system_prompt(Role.CODER, spec, MEMORY).encode()
    )


def test_a_memory_block_only_appends() -> None:
    # The cache prefix: blocks 1 to 3 are the same bytes with or without repo facts, and
    # whatever the facts say.
    without = build_system_prompt(Role.CODER, make_spec())
    with_facts = build_system_prompt(Role.CODER, make_spec(), MEMORY)
    other_facts = build_system_prompt(Role.CODER, make_spec(), "Different facts.")
    assert with_facts.startswith(without)
    assert other_facts.startswith(without)
    assert FACTS not in without


@pytest.mark.parametrize("blank", [None, "", "  \n\t "])
def test_a_blank_memory_block_adds_no_section(blank: str | None) -> None:
    assert build_system_prompt(Role.CODER, make_spec(), blank) == build_system_prompt(
        Role.CODER, make_spec()
    )


def test_memory_block_is_trimmed() -> None:
    padded = build_system_prompt(Role.CODER, make_spec(), f"\n\n{MEMORY}\n\n")
    assert padded == build_system_prompt(Role.CODER, make_spec(), MEMORY)


def test_builder_takes_no_notes_or_plan() -> None:
    # D7. Plan and progress notes change during a task. They go in as messages, so a prompt
    # parameter for them would invite changing the system prompt (and its cache) mid-task.
    params = list(inspect.signature(build_system_prompt).parameters)
    assert params == ["role", "spec", "memory_block"]
    prompt = build_system_prompt(Role.CODER, make_spec(), MEMORY)
    assert not re.search(r"^## (Plan|Notes|Progress)", prompt, re.MULTILINE)


@pytest.mark.parametrize("role", [Role.PLANNER, Role.VERIFIER, Role.COMPACTOR])
def test_a_role_without_rules_is_refused(role: Role) -> None:
    # Their prompts belong to the stories that build those roles. Guessing at one here would
    # hand the scripted provider and the model a role nobody designed.
    with pytest.raises(ValueError, match=role.value):
        build_system_prompt(role, make_spec())


def test_coder_rules_cover_what_the_story_asks_for() -> None:
    prompt = build_system_prompt(Role.CODER, make_spec())
    rules = prompt[prompt.index(RULES) : prompt.index(TOOLS)]
    assert re.search(r"small edits", rules, re.IGNORECASE)
    assert re.search(r"run the tests after every change", rules, re.IGNORECASE)
    assert re.search(r"call `finish` only when the tests pass", rules, re.IGNORECASE)
    # Text from the issue is untrusted, and the model is told so.
    assert re.search(r"not instructions", rules, re.IGNORECASE)


def test_task_statement_has_the_facts_a_coder_needs() -> None:
    prompt = build_system_prompt(Role.CODER, make_spec())
    task = prompt[prompt.index(TASK) :]
    assert "Repository: acme/calc" in task
    assert "Base branch: main" in task
    assert "Test command: pytest -q" in task
    assert "Issue #42: add() returns the difference" in task
    assert "1. Seen on 0.1.0 too." in task
    assert "2. Only add() is wrong." in task


def test_a_task_without_an_issue_number_or_test_command_or_body() -> None:
    spec = make_spec(
        issue_number=None,
        body="",
        comments=[],
        settings=RepoSettings(repo="acme/calc"),
    )
    task = build_system_prompt(Role.CODER, spec).split(TASK, 1)[1]
    assert "Issue: add() returns the difference" in task
    assert "#None" not in task
    assert "(no description)" in task
    assert "Comments" not in task
    # run_tests would answer that no command is set, so the prompt says it first.
    assert re.search(r"Test command: .*not configured.*run_command", task)


def test_line_endings_do_not_change_the_prompt() -> None:
    unix = make_spec(body="line one\nline two", comments=["a\nb"])
    windows = make_spec(body="line one\r\nline two", comments=["a\r\nb"])
    assert build_system_prompt(Role.CODER, unix) == build_system_prompt(Role.CODER, windows)
    assert "\r" not in build_system_prompt(Role.CODER, windows)


def test_the_prompt_holds_nothing_that_changes_between_runs() -> None:
    prompt = build_system_prompt(Role.CODER, make_spec(), MEMORY)
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}", prompt)
    # The task id is a run label. The model has no use for it, and it would only make
    # two tasks on one issue differ in their first bytes.
    assert "lh-acme-calc-42" not in prompt


def test_the_static_text_is_plain_ascii() -> None:
    # The voice guide bans em dashes and emoji. Both are non-ASCII.
    assert build_system_prompt(Role.CODER, make_spec(), MEMORY).isascii()
    assert FIRST_USER_MESSAGE.isascii()


def test_tool_guide_names_only_tools_that_exist() -> None:
    # A guide that names a tool the model does not have invites a call that fails.
    prompt = build_system_prompt(Role.CODER, make_spec(), MEMORY)
    static = prompt[: prompt.index(TASK)]
    specs = default_registry().specs()
    known = {s.name for s in specs}
    for spec in specs:
        known |= set(spec.input_schema.get("properties", {}))
    named = set(re.findall(r"`([^`]+)`", static))
    assert named, "the guide should name its tools"
    assert named <= known, f"not tools or parameters: {sorted(named - known)}"
    # Every tool the coder uses day to day is in the guide.
    for tool in ("list_dir", "read_file", "search", "edit_file", "run_tests", "git_diff", "finish"):
        assert tool in named


def test_first_user_message_points_at_the_system_prompt() -> None:
    assert FIRST_USER_MESSAGE.strip()
    assert "finish" in FIRST_USER_MESSAGE
