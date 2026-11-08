"""Lesson extraction (LH-605): what a finished task teaches, cleaned before it is stored.

Nothing here calls a model. extract() runs against a fake provider that answers from a list and
keeps every request. The rules under test are the ones of the story: at most 5 items, each under
300 characters, typed lesson, pitfall or convention, with generic advice dropped.
"""

from typing import Any

import pytest
from longhaul.memory.lessons import (
    LESSON_TOOL,
    MAX_CHARS,
    MAX_ITEMS,
    TOOL_NAME,
    Lesson,
    build_request,
    clean_items,
    extract,
    is_generic,
    source_ref,
)

from longhaul.contracts import (
    LLMResponse,
    Outcome,
    OutcomeKind,
    Plan,
    PlanStep,
    ProgressNotes,
    ProviderError,
    ProviderErrorKind,
    TextBlock,
    ToolCall,
    ToolUseBlock,
    Usage,
)
from longhaul.memory import lessons

pytestmark = [pytest.mark.unit, pytest.mark.phase6]

USAGE = Usage(input_tokens=800, output_tokens=100)
REMOVE = "Inventory.remove raises InventoryError, not KeyError, and test_inventory.py checks it."
PYTEST = "Run `pytest -q` from the repo root: src/ is on the path only through pyproject.toml."
STYLE = "New modules in src/inventory/ start with a docstring that says why the module exists."


def reply(items: Any, usage: Usage = USAGE) -> LLMResponse:
    block = ToolUseBlock(id="c1", name=TOOL_NAME, input={"items": items})
    return LLMResponse(
        content=[block],
        tool_calls=[ToolCall(id=block.id, name=block.name, input=block.input)],
        stop_reason="tool_use",
        usage=usage,
        model="m",
        latency_ms=1,
    )


class FakeProvider:
    name = "fake"

    def __init__(self, *steps: LLMResponse | ProviderError) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, ProviderError):
            raise step
        return step

    async def count_tokens(self, **kwargs: Any) -> int:
        return 0


# ---------- cleaning ----------
def test_generic_filtered() -> None:
    raw = [
        {"kind": "lesson", "content": "Write tests for your changes."},
        {"kind": "convention", "content": "Read the code before you change it."},
        {"kind": "pitfall", "content": "Always run the tests before you finish."},
        {"kind": "lesson", "content": "Follow best practices and keep the code clean."},
        {"kind": "lesson", "content": REMOVE},
        {"kind": "convention", "content": PYTEST},
    ]

    kept = clean_items(raw)

    assert [item.content for item in kept] == [REMOVE, PYTEST]


@pytest.mark.parametrize(
    "text",
    [
        "Write tests.",
        "  READ THE CODE first!  ",
        "It helps to write tests and read the code.",
        "Check edge cases.",
    ],
)
def test_the_blocklist_ignores_case_and_punctuation(text: str) -> None:
    assert is_generic(text)


@pytest.mark.parametrize(
    "text",
    [
        "Write tests in tests/unit/test_inventory.py, one per public method.",
        "Run the tests with `pytest -q` from the repo root.",
        "Read the code of Inventory.remove: it hides a KeyError.",
        REMOVE,
    ],
)
def test_a_note_that_names_something_of_the_repo_is_kept(text: str) -> None:
    assert not is_generic(text)


def test_an_item_over_300_characters_is_trimmed() -> None:
    long = "Inventory.remove raises InventoryError when the sku is unknown. " * 10

    (kept,) = clean_items([{"kind": "lesson", "content": long}])

    assert len(kept.content) <= MAX_CHARS
    assert kept.content.startswith("Inventory.remove raises InventoryError")
    # The cut falls between words, so a note never ends in half a name.
    assert kept.content.endswith("unknown.") or kept.content.endswith("InventoryError")


def test_a_trimmed_item_is_still_checked_for_being_generic() -> None:
    filler = "write tests " * 40

    assert clean_items([{"kind": "lesson", "content": filler}]) == []


def test_at_most_five_items_are_kept_in_order() -> None:
    raw = [
        {"kind": "lesson", "content": f"Inventory.method_{n} keeps its state in _items."}
        for n in range(9)
    ]

    kept = clean_items(raw)

    assert len(kept) == MAX_ITEMS == 5
    assert [item.content for item in kept] == [item["content"] for item in raw[:5]]


def test_the_same_note_twice_is_kept_once() -> None:
    raw = [
        {"kind": "lesson", "content": REMOVE},
        {"kind": "pitfall", "content": REMOVE.upper()},
        {"kind": "lesson", "content": "  " + REMOVE + "\n"},
    ]

    assert [item.kind for item in clean_items(raw)] == ["lesson", "pitfall"]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "a string",
        {"kind": "lesson", "content": REMOVE},
        [],
        ["just text"],
        [{"kind": "joke", "content": REMOVE}],
        [{"kind": "lesson"}],
        [{"kind": "lesson", "content": 7}],
        [{"kind": "lesson", "content": "   "}],
    ],
)
def test_input_that_is_not_a_list_of_items_gives_nothing(raw: Any) -> None:
    assert clean_items(raw) == []


def test_one_bad_item_does_not_cost_the_good_ones() -> None:
    raw = [{"kind": "joke", "content": "x"}, {"kind": "pitfall", "content": REMOVE}, 5]

    assert clean_items(raw) == [Lesson(kind="pitfall", content=REMOVE)]


def test_an_at_sign_or_a_hash_with_a_name_is_removed() -> None:
    # A note goes into a prompt later, and a mention in it would ping someone on every reuse.
    (kept,) = clean_items(
        [{"kind": "lesson", "content": "Ask @alice about #42 before touching src/inventory.py."}]
    )

    assert "@alice" not in kept.content
    assert "#42" not in kept.content
    assert "src/inventory.py" in kept.content


# ---------- the source ----------
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/inventory/pull/17", "17"),
        ("https://github.com/acme/inventory/pull/17/", "17"),
        ("https://github.com/acme/inventory/pull/17#issuecomment-1", "17"),
        (None, "lh-acme-inventory-3"),
        ("", "lh-acme-inventory-3"),
        ("https://example.com/not-a-pull-request", "lh-acme-inventory-3"),
    ],
)
def test_the_source_is_the_pr_number_else_the_task_id(url: str | None, expected: str) -> None:
    assert source_ref(url, "lh-acme-inventory-3") == expected


# ---------- the request ----------
PLAN = Plan(
    summary="Make remove raise InventoryError.",
    steps=[
        PlanStep(
            id="s1",
            description="Raise InventoryError in remove",
            files=["src/inventory/core.py"],
            done_when="test_remove_missing passes",
            status="done",
        ),
        PlanStep(
            id="s2", description="Update the docs", files=[], done_when="docs match", status="todo"
        ),
    ],
    acceptance_checks=["pytest -q"],
    risks=["Callers catch KeyError."],
)
NOTES = ProgressNotes(
    goal="remove() raises InventoryError",
    plan_status=PLAN.steps,
    files_touched=["src/inventory/core.py"],
    decisions=["Subclass KeyError so old callers still work."],
    failing_tests=["tests/test_inventory.py::test_remove_missing"],
    open_hypotheses=[],
    tried_and_failed=["Catching KeyError in the caller: the test imports the old module."],
    covers_turns_up_to=9,
)


def request(**fields: Any) -> str:
    values: dict[str, Any] = {
        "title": "remove() hides a KeyError",
        "outcome": Outcome(kind=OutcomeKind.PR_OPENED, summary="Done."),
        "plan": PLAN,
        "notes": NOTES,
        "stuck": [],
        "issues": [],
    }
    return build_request(**{**values, **fields})


def test_the_request_carries_the_plan_the_notes_the_stuck_lines_and_the_issues() -> None:
    text = request(
        stuck=["repeated_failure: run_tests failed 3 times with E assert -1 == 5"],
        issues=["remove still raises KeyError for a sku with a space"],
    )

    assert "remove() hides a KeyError" in text
    assert "Raise InventoryError in remove" in text
    assert "[done]" in text
    assert "Subclass KeyError so old callers still work." in text
    assert "Catching KeyError in the caller" in text
    assert "repeated_failure: run_tests failed 3 times" in text
    assert "remove still raises KeyError for a sku with a space" in text
    assert "pr_opened" in text
    assert TOOL_NAME in text


def test_a_request_without_notes_or_issues_says_so_and_still_builds() -> None:
    text = request(notes=None, plan=None)

    assert "no plan" in text.lower()
    assert "no progress notes" in text.lower()


def test_the_request_is_capped() -> None:
    text = request(issues=[f"issue {n}: " + "x" * 400 for n in range(500)])

    assert len(text) < lessons.MAX_REQUEST_CHARS + 2_000


# ---------- the model call ----------
async def test_extract_returns_the_cleaned_items_and_reports_the_call() -> None:
    provider = FakeProvider(
        reply(
            [
                {"kind": "lesson", "content": "Write tests."},
                {"kind": "lesson", "content": REMOVE},
            ]
        )
    )
    billed: list[Usage] = []

    async def on_billed(usage: Usage) -> None:
        billed.append(usage)

    got = await extract(provider, "haiku", "the request", on_billed=on_billed)

    assert got.status == "ok"
    assert [item.content for item in got.lessons] == [REMOVE]
    assert billed == [USAGE]
    (sent,) = provider.calls
    assert sent["model"] == "haiku"
    assert sent["tools"] == [LESSON_TOOL]
    assert sent["options"].force_tool == TOOL_NAME
    assert sent["messages"][0].content[0].text == "the request"


async def test_the_prompt_forbids_generic_advice() -> None:
    provider = FakeProvider(reply([]))

    await extract(provider, "haiku", "the request")

    system = provider.calls[0]["system"]
    assert system.startswith("Role: compactor")
    assert "generic" in system.lower()
    assert f"{MAX_ITEMS}" in system
    assert f"{MAX_CHARS}" in system
    for kind in ("lesson", "pitfall", "convention"):
        assert kind in system


async def test_a_reply_with_no_call_gives_no_lessons() -> None:
    text = LLMResponse(
        content=[TextBlock(text="Nothing to add.")],
        tool_calls=[],
        stop_reason="end_turn",
        usage=USAGE,
        model="m",
        latency_ms=1,
    )
    provider = FakeProvider(text)

    got = await extract(provider, "haiku", "the request")

    assert (got.status, got.lessons) == ("unusable", ())


async def test_a_task_with_nothing_to_teach_gives_an_empty_list() -> None:
    got = await extract(FakeProvider(reply([])), "haiku", "the request")

    assert (got.status, got.lessons) == ("ok", ())


async def test_a_provider_that_refuses_the_forced_tool_is_asked_again_on_auto() -> None:
    refused = ProviderError(ProviderErrorKind.INVALID_REQUEST, "tool_choice not supported")
    provider = FakeProvider(refused, reply([{"kind": "lesson", "content": REMOVE}]))

    got = await extract(provider, "haiku", "the request")

    assert [item.content for item in got.lessons] == [REMOVE]
    assert [call["options"].force_tool for call in provider.calls] == [TOOL_NAME, None]


async def test_any_other_provider_error_goes_up() -> None:
    provider = FakeProvider(ProviderError(ProviderErrorKind.OVERLOADED, "busy"))

    with pytest.raises(ProviderError):
        await extract(provider, "haiku", "the request")


def test_the_tool_schema_caps_the_list() -> None:
    items = LESSON_TOOL.input_schema["properties"]["items"]

    assert items["maxItems"] == MAX_ITEMS
    assert items["items"]["properties"]["content"]["maxLength"] == MAX_CHARS
    assert items["items"]["properties"]["kind"]["enum"] == ["lesson", "pitfall", "convention"]


# ---------- the names that the workflow and this module share ----------
def test_the_event_names_are_those_the_workflow_records() -> None:
    from longhaul.durable import acts
    from longhaul.durable.workflows import task

    assert lessons.STUCK_EVENT == task.STUCK_EVENT
    assert lessons.PAUSED_EVENT == task.PAUSED_EVENT
    assert acts.LESSONS_EVENT == "lessons"
    assert lessons.EVENT_TYPES == (task.STUCK_EVENT, task.PAUSED_EVENT, lessons.VERDICT_EVENT)


def test_the_note_kinds_are_those_of_the_memory_table() -> None:
    from typing import get_args

    from longhaul.contracts import MemoryItem
    from longhaul.durable import acts

    kinds = get_args(MemoryItem.model_fields["kind"].annotation)
    assert set(get_args(acts.LessonKindName)) <= set(kinds)
    assert set(lessons.KINDS) == set(get_args(acts.LessonKindName))
