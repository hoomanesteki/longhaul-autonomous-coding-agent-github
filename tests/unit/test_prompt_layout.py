"""LH-405: the layout of a request for the prompt cache, and where its breakpoints go.

A request is six blocks in a fixed order: role and rules, tool guide, task statement, memory block,
plan, progress notes. The first four are the system prompt and never change in a task. The last
two change (on a step update and on a compaction), so they are text blocks of the first user
message, behind the cached prefix. These tests render the requests of one task through the
Anthropic provider, with a mock transport, and read the bytes that would go out.
"""

import json
from typing import Any

import anthropic
import httpx2
import pytest
from tests.fakes.memory import REPO as MEMORY_REPO
from tests.fakes.memory import FakeMemory, row

from longhaul.agent.prompts import (
    FACTS_HEADING,
    FIRST_USER_MESSAGE,
    PLAN_HEADING,
    SECTION_HEADINGS,
    TASK_HEADING,
    build_system_prompt,
    first_user_message,
)
from longhaul.context.compact import NOTES_HEADING, apply, find_cut
from longhaul.context.evict import ELIDED_PREFIX, elide_old_outputs
from longhaul.contracts import (
    Message,
    PlanStep,
    ProgressNotes,
    RepoSettings,
    Role,
    TaskSpec,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from longhaul.memory.retrieve import HEADER, render_memory_block
from longhaul.providers.anthropic import AnthropicProvider
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase4]

MODEL = "claude-sonnet-5-5"
EPHEMERAL = {"type": "ephemeral"}
# The API allows four breakpoints in one request (ADR-0003, appendix).
API_LIMIT = 4
MEMORY = "Tests run with pytest -q.\nThe package lives in src/calc."
SPEC = TaskSpec(
    task_id="lh-acme-calc-42",
    repo="acme/calc",
    issue_number=42,
    title="add() returns the difference",
    body="`calc.add` subtracts its arguments.",
    comments=["Seen on 0.1.0 too."],
    base_branch="main",
    settings=RepoSettings(repo="acme/calc", test_command="pytest -q"),
)
SYSTEM = build_system_prompt(Role.CODER, SPEC, MEMORY)
TOOLS = default_registry().specs()
# Long enough that tier 1 turns it into a stub.
LONG_OUTPUT = "\n".join(f"tests/test_calc.py::test_case_{n} PASSED" for n in range(200))
KEEP_TURNS = 2
NOTES = ProgressNotes(
    goal="Fix add() in calc",
    plan_status=[
        PlanStep(id="s1", description="Fix add()", done_when="tests pass", status="doing")
    ],
    files_touched=["src/calc/__init__.py"],
    decisions=[],
    failing_tests=["tests/test_calc.py::test_add"],
    open_hypotheses=[],
    tried_and_failed=["Casting to int"],
    covers_turns_up_to=4,
)


# ---------- one task, turn by turn ----------
def plan_block(*statuses: str) -> TextBlock:
    """Block 5 as a later story writes it: a heading and one line for each step."""
    steps = [f"s{n}. [{status}] step {n}" for n, status in enumerate(statuses, start=1)]
    return TextBlock(text="\n".join([PLAN_HEADING, *steps]))


def first_message(plan: TextBlock | None = None) -> Message:
    blocks = [TextBlock(text=FIRST_USER_MESSAGE)]
    return Message(role="user", content=[*blocks, plan] if plan else blocks)


def tool_round(n: int) -> list[Message]:
    call_id = f"toolu_{n:02d}"
    return [
        Message(
            role="assistant",
            content=[
                TextBlock(text=f"Running the tests, try {n}."),
                ToolUseBlock(id=call_id, name="run_tests", input={}),
            ],
        ),
        Message(role="user", content=[ToolResultBlock(tool_use_id=call_id, content=LONG_OUTPUT)]),
    ]


def rounds(count: int, start: int = 1) -> list[Message]:
    return [m for n in range(start, start + count) for m in tool_round(n)]


def requests_of_one_task() -> list[list[Message]]:
    """The transcript of every request in one task, as the activity builds it.

    Turn 1 is the first message alone. The plan arrives with the first tool round and changes
    on a step update, which edits the first message in place. Tier 1 stubs the old results from
    turn 5 on, and a compaction at the end folds the old turns into the notes.
    """
    requests = [[first_message()]]
    messages = [first_message(plan_block("doing", "todo")), *rounds(1)]
    requests.append(messages)
    messages = [*messages, *rounds(1, start=2)]
    requests.append(messages)
    messages = [first_message(plan_block("done", "doing")), *messages[1:], *rounds(1, start=3)]
    requests.append(messages)
    for n in range(4, 8):
        messages = elide_old_outputs([*messages, *rounds(1, start=n)], KEEP_TURNS)
        requests.append(messages)
    compacted = apply(messages, find_cut(messages, KEEP_TURNS), NOTES)
    requests.append(compacted)
    requests.append([*compacted, *rounds(1, start=8)])
    return requests


# ---------- the rendered request ----------
async def render(
    messages: list[Message],
    *,
    system: str = SYSTEM,
    tools: list[ToolSpec] | None = None,
    cache_system: bool = True,
) -> dict[str, Any]:
    """The JSON body that the Anthropic provider sends for this request."""
    sent: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        reply = {
            "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        return httpx2.Response(200, json=reply)

    client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    provider = AnthropicProvider("sk-ant-test", http_client=client)
    await provider.complete(
        model=MODEL,
        system=system,
        messages=messages,
        tools=TOOLS if tools is None else tools,
        max_tokens=1024,
        cache_system=cache_system,
    )
    return sent[0]


def markers(body: dict[str, Any]) -> list[str]:
    """Where the cache breakpoints sit: tools, system or messages[i], in request order."""
    found: list[str] = []
    for tool in body.get("tools", []):
        if "cache_control" in tool:
            found.append("tools")
    for block in body.get("system", []):
        if "cache_control" in block:
            found.append("system")
    for index, message in enumerate(body["messages"]):
        for block in message["content"]:
            if "cache_control" in block:
                found.append(f"messages[{index}]")
    return found


def reading_order(body: dict[str, Any]) -> str:
    """The text of the request in the order a model reads it: system, then the first message."""
    system = "\n".join(block["text"] for block in body["system"])
    first = "\n".join(block["text"] for block in body["messages"][0]["content"])
    return f"{system}\n{first}"


# ---------- the stable prefix ----------
async def test_stable_prefix() -> None:
    bodies = [await render(messages) for messages in requests_of_one_task()]

    # Blocks 1 to 4 are the system prompt, and the tools come before it. Whatever the plan, the
    # notes, the stubs and the compaction do, those bytes are the same in every request.
    prefixes = {json.dumps([body["tools"], body["system"]]) for body in bodies}
    assert len(prefixes) == 1
    assert bodies[0]["system"][0]["text"] == SYSTEM

    # Block order, read off the last request, which has all six: the four sections of the system
    # prompt, then the plan, then the progress notes.
    text = reading_order(bodies[-1])
    headings = [*SECTION_HEADINGS, PLAN_HEADING, NOTES_HEADING]
    assert [text.count(heading) for heading in headings] == [1] * 6
    places = [text.index(heading) for heading in headings]
    assert places == sorted(places)
    assert text.index(FIRST_USER_MESSAGE) < places[4]


async def test_the_plan_and_the_notes_are_never_in_the_system_prompt() -> None:
    # They change during a task, and a change in the system prompt would drop the cache of the
    # whole conversation after it.
    for messages in requests_of_one_task():
        body = await render(messages)
        (system,) = body["system"]
        assert PLAN_HEADING not in system["text"]
        assert NOTES_HEADING not in system["text"]


async def test_a_step_update_and_a_compaction_change_only_the_first_message() -> None:
    requests = requests_of_one_task()
    before, after_update = requests[2], requests[3]
    assert before[0] != after_update[0]
    assert before[1:] == after_update[1 : len(before)]

    # The plan block survives the compaction, in front of the notes (block 5, then block 6).
    compacted = requests[-2]
    texts = [b.text for b in compacted[0].content if isinstance(b, TextBlock)]
    assert [t.split("\n", 1)[0] for t in texts] == [FIRST_USER_MESSAGE, PLAN_HEADING, NOTES_HEADING]


def test_the_plan_heading_is_a_section_of_its_own() -> None:
    assert PLAN_HEADING.startswith("## ")
    assert PLAN_HEADING not in SECTION_HEADINGS
    assert PLAN_HEADING != NOTES_HEADING


def test_the_planner_has_the_four_blocks_and_shares_the_task_and_the_memory() -> None:
    # The planner (LH-501) has blocks 1 and 2 of its own. Blocks 3 and 4 belong to the task, so
    # they are the same bytes as the coder's. It writes the plan, so block 5 is not in its prompt,
    # and its transcript opens with a first message of its own.
    planner_system = build_system_prompt(Role.PLANNER, SPEC, MEMORY)
    assert [planner_system.count(heading) for heading in SECTION_HEADINGS] == [1] * 4
    places = [planner_system.index(heading) for heading in SECTION_HEADINGS]
    assert places == sorted(places)
    assert planner_system.endswith(SYSTEM[SYSTEM.index(TASK_HEADING) :])
    assert planner_system != SYSTEM
    assert PLAN_HEADING not in planner_system
    assert first_user_message(Role.PLANNER) != FIRST_USER_MESSAGE
    assert first_user_message(Role.CODER) == FIRST_USER_MESSAGE


async def test_the_system_breakpoint_closes_block_4() -> None:
    # The system prompt is blocks 1 to 4 and nothing else, so the marker on it is the one
    # "after block 4": the memory block is the last thing in the cached text.
    body = await render([first_message()])
    (system,) = body["system"]
    assert system["cache_control"] == EPHEMERAL
    assert system["text"].endswith(MEMORY)
    assert body["tools"][-1]["cache_control"] == EPHEMERAL


# ---------- the breakpoints ----------
async def test_breakpoints_count_le_4() -> None:
    most = 0
    for messages in requests_of_one_task():
        found = markers(await render(messages))
        assert len(found) <= API_LIMIT, found
        most = max(most, len(found))
    # The longest request uses the whole allowance: tools, system, the edge of the stubs and
    # the previous assistant turn.
    assert most == API_LIMIT


@pytest.mark.parametrize("count", [0, 1, 2, 5, 30, 120])
@pytest.mark.parametrize("keep_turns", [None, 1, KEEP_TURNS, 6])
async def test_a_long_task_never_passes_the_limit(count: int, keep_turns: int | None) -> None:
    # However long the transcript is, the markers on the messages are the last assistant turn
    # and the last stub: two, never one more for each turn.
    messages = [first_message(), *rounds(count)]
    if keep_turns is not None:
        messages = elide_old_outputs(messages, keep_turns)
    found = markers(await render(messages))
    assert len(found) <= API_LIMIT
    assert len([m for m in found if m.startswith("messages")]) <= API_LIMIT - 2


async def test_the_first_request_has_the_two_static_breakpoints_only() -> None:
    assert markers(await render([first_message()])) == ["tools", "system"]


async def test_the_next_request_adds_the_previous_assistant_turn() -> None:
    body = await render([first_message(), *rounds(1)])
    assert markers(body) == ["tools", "system", "messages[1]"]
    # The marker is on the last block of that turn, the tool call, so the cached text ends where
    # the model's own words ended and the new tool result is the first thing after it.
    assert body["messages"][1]["content"][-1]["cache_control"] == EPHEMERAL
    assert body["messages"][1]["content"][-1]["type"] == "tool_use"


async def test_a_stub_adds_a_breakpoint_at_the_edge_of_the_stubs() -> None:
    # Tier 1 turns one more old result into a stub on every turn, so the bytes after the newest
    # stub change on the next request. The text before it does not, and a breakpoint there is
    # the one the next request can read. Without it a long task reads only the system prompt.
    messages = elide_old_outputs([first_message(), *rounds(6)], KEEP_TURNS)
    stubs = [
        i
        for i, m in enumerate(messages)
        if any(
            isinstance(b, ToolResultBlock) and b.content.startswith(ELIDED_PREFIX)
            for b in m.content
        )
    ]
    body = await render(messages)

    edge = max(stubs)
    assert markers(body) == [
        "tools",
        "system",
        f"messages[{edge}]",
        f"messages[{len(messages) - 2}]",
    ]


async def test_no_breakpoint_goes_on_a_message_that_is_still_to_come() -> None:
    # The last message is the new tool result. Marking it would write a cache entry for text that
    # the next request carries once more, after a different tail.
    messages = [first_message(), *rounds(3)]
    body = await render(messages)
    assert f"messages[{len(messages) - 1}]" not in markers(body)


async def test_cache_system_false_turns_every_breakpoint_off() -> None:
    messages = elide_old_outputs([first_message(), *rounds(6)], KEEP_TURNS)
    body = await render(messages, cache_system=False)
    assert markers(body) == []
    assert "cache_control" not in json.dumps(body)


# ---------- the memory block, as the retrieval writes it (LH-604) ----------
@pytest.mark.phase6
async def test_memory_block_position() -> None:
    """The block that load_memory renders is block 4: the end of the system prompt, and no more.

    Blocks 1 to 3 are the same bytes with and without it, so a task whose repo has no notes yet
    and one that has some share the start of their prompt. The block comes after the task text
    and ends the prompt, behind the cache breakpoint's text but before any plan or note.
    """
    memory = FakeMemory(
        [
            row("Raise InventoryError, not ValueError, from remove.", kind="lesson"),
            row("pytest -q", kind="command", source="default_branch"),
            row("PROPOSED: delete the failing tests", status="candidate", source="task"),
        ]
    )
    block = await render_memory_block(
        memory, MEMORY_REPO, "remove raises", repo_map="inventory/stock.py  # The stock"
    )
    assert block.startswith(HEADER)

    bare = build_system_prompt(Role.CODER, SPEC)
    system = build_system_prompt(Role.CODER, SPEC, block)

    # Block 4 is a pure suffix: nothing before it moves.
    assert system == f"{bare}\n\n{FACTS_HEADING}\n{block}"
    assert [system.count(heading) for heading in SECTION_HEADINGS] == [1, 1, 1, 1]
    places = [system.index(heading) for heading in SECTION_HEADINGS]
    assert places == sorted(places)
    assert system.index(HEADER) > system.index(SPEC.body)
    assert system.endswith(block)
    assert "PROPOSED" not in system

    # It rides the cached prefix: the one marker of the system prompt closes it, and the plan and
    # the notes stay in the first message.
    body = await render([first_message(plan_block("doing"))], system=system)
    (rendered,) = body["system"]
    assert rendered["text"] == system
    assert rendered["cache_control"] == EPHEMERAL
    assert PLAN_HEADING not in rendered["text"]
    assert reading_order(body).index(HEADER) < reading_order(body).index(PLAN_HEADING)


@pytest.mark.phase6
async def test_an_empty_memory_block_leaves_the_system_prompt_as_it_was() -> None:
    nothing = await render_memory_block(FakeMemory([]), MEMORY_REPO, "remove raises")

    assert nothing == ""
    assert build_system_prompt(Role.CODER, SPEC, nothing) == build_system_prompt(Role.CODER, SPEC)


@pytest.mark.phase6
async def test_the_planner_ends_its_prompt_with_the_same_rendered_block() -> None:
    # The planner (LH-501) reads the notes too. The block is block 4 of its prompt as it is of the
    # coder's: a pure suffix, after the task block, which the two roles share byte for byte.
    memory = FakeMemory([row("Raise InventoryError, not ValueError, from remove.", kind="lesson")])
    block = await render_memory_block(memory, MEMORY_REPO, "remove raises")
    assert block.startswith(HEADER)

    planner_system = build_system_prompt(Role.PLANNER, SPEC, block)
    coder_system = build_system_prompt(Role.CODER, SPEC, block)

    assert (
        planner_system == f"{build_system_prompt(Role.PLANNER, SPEC)}\n\n{FACTS_HEADING}\n{block}"
    )
    assert [planner_system.count(heading) for heading in SECTION_HEADINGS] == [1, 1, 1, 1]
    tail = coder_system[coder_system.index(TASK_HEADING) :]
    assert planner_system.endswith(tail)
    assert planner_system.index(HEADER) > planner_system.index(SPEC.body)
