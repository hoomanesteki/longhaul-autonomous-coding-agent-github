"""LH-403: structured compaction.

Nothing here calls a model. compact() runs against ScriptedProvider, which answers from a
script, or against FakeProvider below, which answers from a list and keeps every request so a
test can read what was sent. One test drives the real AnthropicProvider over httpx2's mock
transport, because the choice between a forced tool and an automatic one is made by that
provider and not by compact().

The cut point is the rule the Messages API enforces with a 400 that is never retried, so it
gets a property test over random transcripts.
"""

import ast
import copy
import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import jsonschema
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from longhaul.agent.turns import TranscriptError, validate_pairs
from longhaul.context import compact as compact_module
from longhaul.context.compact import (
    COMPACTOR_SYSTEM,
    FIELD_GUIDE,
    NO_PREVIOUS_NOTES,
    NOTES_HEADING,
    NOTES_TOOL,
    NOTES_TOOL_NAME,
    OUTPUT_LINES,
    REPAIR_PREFIX,
    Compaction,
    apply,
    compact,
    find_cut,
    halve_cut,
    keep_failures,
    render_for_compaction,
    render_notes,
)
from longhaul.contracts import (
    Block,
    CallOptions,
    LLMResponse,
    Message,
    PlanStep,
    ProgressNotes,
    ProviderError,
    ProviderErrorKind,
    Role,
    StopDetails,
    StopReason,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from longhaul.providers.anthropic import AnthropicProvider
from longhaul.providers.base import (
    format_role_line,
    format_turn_offset,
    read_turn_offset,
    turn_index,
)
from longhaul.providers.scripted import ScriptedProvider

pytestmark = [pytest.mark.unit, pytest.mark.phase4]

FIRST = "Start on the task in your instructions. Call `finish` once the tests pass."


# ---------- builders ----------
def text(body: str) -> TextBlock:
    return TextBlock(text=body)


def use(call_id: str, name: str = "run_command", **args: Any) -> ToolUseBlock:
    return ToolUseBlock(id=call_id, name=name, input=args)


def res(call_id: str, content: str = "ok", is_error: bool = False) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=call_id, content=content, is_error=is_error)


def user(*blocks: Block) -> Message:
    return Message(role="user", content=list(blocks))


def assistant(*blocks: Block) -> Message:
    return Message(role="assistant", content=list(blocks))


def turn(n: int, output: str = "ok") -> list[Message]:
    """One finished tool round: the model runs a command and gets its output."""
    return [assistant(use(f"t{n}", command=f"step {n}")), user(res(f"t{n}", output))]


def transcript(turns: int) -> list[Message]:
    messages = [user(text(FIRST))]
    for n in range(1, turns + 1):
        messages += turn(n)
    return messages


def call_ids(messages: list[Message]) -> set[str]:
    return {b.id for m in messages for b in m.content if isinstance(b, ToolUseBlock)}


def result_ids(messages: list[Message]) -> set[str]:
    return {b.tool_use_id for m in messages for b in m.content if isinstance(b, ToolResultBlock)}


def assistants(messages: list[Message]) -> int:
    return sum(m.role == "assistant" for m in messages)


STEPS = [
    PlanStep(
        id="s1",
        description="Reproduce the overflow",
        files=["tests/test_calc.py"],
        done_when="a test fails",
        status="done",
    ),
    PlanStep(
        id="s2",
        description="Fix add()",
        files=["src/calc/__init__.py"],
        done_when="the tests pass",
        status="doing",
    ),
]
NOTES = ProgressNotes(
    goal="Fix add() in calc so it no longer overflows",
    plan_status=STEPS,
    files_touched=["tests/test_calc.py", "src/calc/__init__.py"],
    decisions=["Keep the public API as it is"],
    failing_tests=["tests/test_calc.py::test_add_overflow: OverflowError"],
    open_hypotheses=["The cast happens before the addition"],
    tried_and_failed=["Casting both operands to int still overflows"],
    covers_turns_up_to=6,
)


def notes(**changes: Any) -> ProgressNotes:
    return NOTES.model_copy(update=changes)


USAGE = Usage(input_tokens=900, output_tokens=120)


def llm(
    *blocks: Block,
    stop: StopReason = "end_turn",
    usage: Usage = USAGE,
    details: StopDetails | None = None,
) -> LLMResponse:
    calls = [
        ToolCall(id=b.id, name=b.name, input=b.input) for b in blocks if isinstance(b, ToolUseBlock)
    ]
    return LLMResponse(
        content=list(blocks),
        tool_calls=calls,
        stop_reason=stop,
        stop_details=details,
        usage=usage,
        model="m",
        latency_ms=0,
    )


def payload(value: ProgressNotes | dict[str, Any]) -> dict[str, Any]:
    return value.model_dump(mode="json") if isinstance(value, ProgressNotes) else value


def notes_reply(value: ProgressNotes | dict[str, Any], call_id: str = "toolu_n1") -> LLMResponse:
    return llm(use(call_id, NOTES_TOOL_NAME, **payload(value)), stop="tool_use")


def chatter() -> LLMResponse:
    """A reply that talks and never calls the tool."""
    return llm(text("Here are the notes: the add function was fixed."))


class FakeProvider:
    """Answers from a list, one reply per call, and keeps every request it gets.

    A reply that is an exception is raised. `refuses_forced` makes a call that forces a tool
    fail with INVALID_REQUEST, as AnthropicProvider does for claude-sonnet-5-5. Such a call
    uses up no reply.
    """

    name = "fake"

    def __init__(self, *replies: LLMResponse | Exception, refuses_forced: bool = False) -> None:
        self._replies = list(replies)
        self._refuses_forced = refuses_forced
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        cache_system: bool = True,
        options: CallOptions | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "model": model,
                "system": system,
                "messages": list(messages),
                "tools": tools,
                "max_tokens": max_tokens,
                "options": options,
            }
        )
        if self._refuses_forced and options is not None and options.force_tool is not None:
            raise ProviderError(ProviderErrorKind.INVALID_REQUEST, "force_tool is not supported")
        assert self._replies, "compact() made more model calls than the test has replies for"
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def count_tokens(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        options: CallOptions | None = None,
    ) -> int:
        return 0

    def forced(self) -> list[str | None]:
        """The tool each call forced, in call order. None is a call on tool_choice auto."""
        return [c["options"].force_tool for c in self.calls]


def scripted(tmp_path: Path, *steps: dict[str, Any]) -> ScriptedProvider:
    # JSON is valid YAML, so a script can be built from plain dicts.
    path = tmp_path / "compactor.yaml"
    path.write_text(json.dumps({"steps": list(steps)}))
    return ScriptedProvider(path)


def says(value: ProgressNotes | dict[str, Any], **match: Any) -> dict[str, Any]:
    """A script step: when the compactor is asked (and `match` holds), it calls the tool."""
    call = {"name": NOTES_TOOL_NAME, "input": payload(value)}
    return {"match": {"role": "compactor", **match}, "respond": {"tool_calls": [call]}}


def last_user_text(call: dict[str, Any]) -> str:
    last = call["messages"][-1]
    assert last.role == "user"
    return "\n".join(b.text for b in last.content if isinstance(b, TextBlock))


# ---------- find_cut ----------
@st.composite
def transcripts(draw: st.DrawFn) -> list[Message]:
    """A transcript as the loop builds it: a user message, then rounds of assistant and user.

    A round has up to three parallel calls, answered in one user message that may carry a hint
    after the results. A turn without calls is followed by the harness nudge.
    """
    messages = [user(text(FIRST))]
    ids = iter(range(10_000))
    for _ in range(draw(st.integers(min_value=0, max_value=14))):
        ids_in_turn = [f"toolu_{next(ids)}" for _ in range(draw(st.integers(0, 3)))]
        blocks: list[Block] = []
        if not ids_in_turn or draw(st.booleans()):
            blocks.append(text("thinking aloud"))
        blocks += [use(i) for i in ids_in_turn]
        messages.append(assistant(*blocks))
        if ids_in_turn:
            answers: list[Block] = [res(i) for i in ids_in_turn]
            if draw(st.booleans()):
                answers.append(text("Hint from @sam: try the other file"))
            messages.append(user(*answers))
        else:
            messages.append(user(text("Use tools or call finish.")))
    return messages


@settings(
    max_examples=300,
    deadline=None,
    # A loaded CI runner must not fail the build over speed.
    suppress_health_check=[HealthCheck.too_slow],
)
@given(transcripts(), st.integers(min_value=1, max_value=10))
def test_find_cut_property(messages: list[Message], keep: int) -> None:
    cut = find_cut(messages, keep)

    if assistants(messages) <= keep:
        assert cut == 0  # nothing is older than the turns to keep
        return

    assert 0 < cut < len(messages)
    assert messages[cut].role == "assistant"
    before, after = messages[:cut], messages[cut:]
    assert assistants(after) == keep
    assert assistants(before) >= 1  # a cut that folds no turn would only add notes
    # The rule: no tool_use before the cut waits for a result after it, and no result after
    # it answers a call from before.
    assert call_ids(before) == result_ids(before)
    assert not result_ids(after) & call_ids(before)
    validate_pairs(before)
    validate_pairs(after)
    # And what apply makes of it is a transcript the API takes.
    applied = apply(messages, cut, NOTES)
    validate_pairs(applied)
    assert applied[0].role == "user"
    assert applied[1:] == after
    assert turn_index(applied) == turn_index(messages)


def test_find_cut_keeps_the_most_recent_turns() -> None:
    messages = transcript(10)

    cut = find_cut(messages, 3)

    assert cut == len(messages) - 6  # three turns of two messages each
    assert messages[cut] == messages[-6]
    assert assistants(messages[cut:]) == 3


@pytest.mark.parametrize(("turns", "keep"), [(0, 1), (1, 1), (3, 3), (3, 5)])
def test_find_cut_returns_zero_when_nothing_is_older_than_the_kept_turns(
    turns: int, keep: int
) -> None:
    assert find_cut(transcript(turns), keep) == 0


@pytest.mark.parametrize("keep", [0, -1])
def test_find_cut_has_to_keep_at_least_one_turn(keep: int) -> None:
    with pytest.raises(ValueError, match="keep_recent_turns"):
        find_cut(transcript(4), keep)


def test_find_cut_leaves_an_open_round_with_the_turns_it_keeps() -> None:
    # The last assistant message has called a tool and waits for the result. It counts as a turn
    # to keep, and the cut goes in front of it, where every call is answered.
    messages = [*transcript(5), assistant(use("t6", command="step 6"))]

    cut = find_cut(messages, 1)

    assert cut == len(messages) - 1
    validate_pairs(messages[:cut])


def test_find_cut_never_cuts_between_a_call_and_its_result_even_in_a_bad_transcript() -> None:
    # A transcript the loop would not build: a second assistant message sits between a call
    # and its result. The cut that keeps one turn would fall inside that pair, so it moves
    # earlier, to the nearest place where every call is answered.
    messages = [
        user(text(FIRST)),
        assistant(text("Reading the issue.")),
        user(text("Use tools or call finish.")),
        assistant(use("a")),
        assistant(text("still thinking")),
        user(res("a")),
    ]

    cut = find_cut(messages, 1)

    assert cut == 3
    validate_pairs(messages[:cut])


def test_find_cut_does_not_cut_in_front_of_the_first_turn() -> None:
    # That cut would fold no turn: the task message would be replaced by itself and an empty
    # set of notes. Better to cut nothing.
    messages = [
        user(text(FIRST)),
        assistant(use("a")),
        assistant(text("still thinking")),
        user(res("a")),
    ]

    assert find_cut(messages, 1) == 0


def test_halve_cut_goes_to_the_latest_turn_boundary_at_or_below_half() -> None:
    messages = transcript(10)
    cut = find_cut(messages, 2)

    half = halve_cut(messages, cut)

    assert half <= cut // 2
    assert messages[half].role == "assistant"
    # The first turn (message 1) is not a cut point, so the lowest one is message 3.
    assert half == max(i for i in range(3, cut // 2 + 1) if messages[i].role == "assistant")
    validate_pairs(messages[:half])
    assert assistants(messages[:half]) >= 1


@pytest.mark.parametrize(("cut", "expected"), [(0, 0), (1, 0), (3, 0), (5, 0), (7, 3), (9, 3)])
def test_halve_cut_gives_up_when_no_cut_point_is_that_low(cut: int, expected: int) -> None:
    # Half of 3 is message 1, which is the first turn and folds nothing. Half of 7 is message 3,
    # the first place where a turn comes before the cut.
    assert halve_cut(transcript(5), cut) == expected


# ---------- render_for_compaction ----------
def test_render_puts_each_call_on_one_line_with_its_arguments() -> None:
    messages = [
        user(text(FIRST)),
        assistant(
            text("Looking at the code."),
            use("a", "read_file", path="src/calc/__init__.py"),
            use("b", "run_tests", target="tests/test_calc.py::test_add"),
        ),
        user(res("a", "def add(a, b): ..."), res("b", "exit code: 1\nsummary: 1 failed")),
    ]

    lines = render_for_compaction(messages).splitlines()

    assert lines[:2] == ["[user]", f"  {FIRST}"]
    assert "[turn 1]" in lines
    assert "  said: Looking at the code." in lines
    assert '  call read_file {"path": "src/calc/__init__.py"}' in lines
    assert '  call run_tests {"target": "tests/test_calc.py::test_add"}' in lines
    # Each result sits under the turn that made the calls, labelled with the tool it answers.
    assert "  result of read_file (ok):" in lines
    assert "    def add(a, b): ..." in lines
    assert "  result of run_tests (ok):" in lines
    assert "    exit code: 1" in lines
    assert "    summary: 1 failed" in lines


def test_render_cuts_a_long_output_to_its_first_twenty_lines() -> None:
    output = "\n".join(f"line {n}" for n in range(1, 51))
    messages = [user(text(FIRST)), assistant(use("a")), user(res("a", output))]

    lines = render_for_compaction(messages).splitlines()

    assert OUTPUT_LINES == 20
    shown = [line for line in lines if line.startswith("    line ")]
    assert shown == [f"    line {n}" for n in range(1, 21)]
    assert "    ... 30 more lines" in lines


def test_render_keeps_a_short_output_and_an_elided_stub_whole() -> None:
    stub = "[elided: run_tests output, 4200 lines, handle out_t3]"
    exactly_twenty = "\n".join(f"row {n}" for n in range(20))
    messages = [
        user(text(FIRST)),
        assistant(use("a"), use("b")),
        user(res("a", stub), res("b", exactly_twenty)),
    ]

    out = render_for_compaction(messages)

    assert f"    {stub}" in out.splitlines()
    assert "    row 19" in out.splitlines()
    assert "more lines" not in out


def test_render_flags_an_error_and_an_empty_output() -> None:
    messages = [
        user(text(FIRST)),
        assistant(use("a", "run_tests"), use("b", "git_diff")),
        user(res("a", "timed out after 300 s and was killed.", is_error=True), res("b", "")),
    ]

    lines = render_for_compaction(messages).splitlines()

    assert "  result of run_tests (error):" in lines
    assert "  result of git_diff (ok): no output" in lines


def test_render_clips_wide_lines_long_arguments_and_long_assistant_text() -> None:
    wide = "x" * 5_000
    messages = [
        user(text(FIRST)),
        assistant(text("t" * 5_000), use("a", "edit_file", path="a.py", new_str="n" * 5_000)),
        user(res("a", wide)),
    ]

    out = render_for_compaction(messages)

    assert len(out) < 4_000
    assert "more characters" in out
    # The path is short, so it stays exact.
    assert '"path": "a.py"' in out


def test_render_clips_long_strings_inside_nested_arguments_too() -> None:
    edits = [{"old": "o" * 5_000, "new": "short"}, ["n" * 5_000, 7, None, True]]
    messages = [
        user(text(FIRST)),
        assistant(use("a", "multi_edit", path="a.py", edits=edits, dry_run=False)),
        user(res("a")),
    ]

    out = render_for_compaction(messages)

    (call,) = [line for line in out.splitlines() if line.startswith("  call ")]
    assert len(call) < 1_200
    assert call.count("more characters") == 2
    # Values that are not text pass through as they are.
    assert '"short"' in call
    assert "7, null, true]" in call
    assert '"dry_run": false' in call


def test_render_keeps_what_a_person_said_in_full() -> None:
    hint = "Hint from @sam: " + " ".join(["check the other module first."] * 100)
    messages = [user(text(FIRST)), assistant(use("a")), user(res("a"), text(hint))]

    out = render_for_compaction(messages)

    assert hint in out
    assert out.splitlines()[-2] == "[user]"


def test_render_numbers_turns_after_an_earlier_compaction() -> None:
    messages = transcript(8)
    once = apply(messages, find_cut(messages, 2), NOTES)  # folds turns 1 to 6

    lines = render_for_compaction(once).splitlines()

    assert "[turn 7]" in lines
    assert "[turn 8]" in lines
    assert "[turn 1]" not in lines


def test_render_leaves_out_the_notes_of_an_earlier_compaction() -> None:
    # compact() gets those notes as notes_prev, so they are not repeated in the transcript.
    messages = transcript(8)
    once = apply(messages, find_cut(messages, 2), NOTES)

    out = render_for_compaction(once)

    assert FIRST in out
    assert NOTES_HEADING not in out
    assert NOTES.goal not in out


# ---------- apply ----------
def test_apply_keeps_the_task_and_adds_the_notes_in_front_of_the_recent_turns() -> None:
    messages = transcript(8)
    cut = find_cut(messages, 3)

    applied = apply(messages, cut, NOTES)

    assert applied[0].role == "user"
    assert applied[1:] == messages[cut:]
    assert applied[0].content[0] == text(FIRST)
    body = applied[0].content[-1]
    assert isinstance(body, TextBlock)
    assert body.text.startswith(NOTES_HEADING)
    for fact in (
        NOTES.goal,
        "src/calc/__init__.py",
        "tests/test_calc.py::test_add_overflow: OverflowError",
        "s2 [doing] Fix add()",
        "Keep the public API as it is",
        "The cast happens before the addition",
        "Casting both operands to int still overflows",
    ):
        assert fact in body.text


async def test_apply_valid_transcript(tmp_path: Path) -> None:
    # The acceptance case: scripted compactor output goes in, and what comes out passes the
    # pairing check and starts with a user message.
    provider = scripted(tmp_path, says(NOTES))
    messages = transcript(12)
    cut = find_cut(messages, 4)

    result = await compact(provider, "m", None, render_for_compaction(messages[:cut]))
    applied = apply(messages, cut, result.notes)

    assert result.notes == NOTES
    validate_pairs(applied)
    assert applied[0].role == "user"
    assert applied[1:] == messages[cut:]
    assert assistants(applied) == 4
    # The turn counter does not move. The marker carries the eight turns that were folded.
    assert read_turn_offset(applied) == 8
    assert turn_index(applied) == turn_index(messages) == 12
    assert len(applied) < len(messages)


def test_a_second_compaction_replaces_the_notes_and_adds_up_the_turns() -> None:
    messages = transcript(10)
    once = apply(messages, find_cut(messages, 6), NOTES)  # folds 4 turns
    grown = [*once, *(m for n in range(11, 15) for m in turn(n))]
    cut = find_cut(grown, 3)

    twice = apply(grown, cut, notes(goal="Second goal"))

    validate_pairs(twice)
    blocks = [b for b in twice[0].content if isinstance(b, TextBlock)]
    assert blocks[0] == text(FIRST)
    assert sum(b.text.startswith(NOTES_HEADING) for b in blocks) == 1
    assert "Second goal" in blocks[-1].text
    assert NOTES.goal not in blocks[-1].text
    assert read_turn_offset(twice) == 11
    assert turn_index(twice) == turn_index(grown) == 14
    # Exactly one marker is left, so the turn counter cannot read an old one.
    assert sum(b.text.count("longhaul:turn_offset") for b in blocks) == 1


def test_apply_without_notes_still_records_the_folded_turns() -> None:
    # The fallback with no earlier notes: turns are cut, and the model is told there are no notes.
    messages = transcript(8)
    cut = find_cut(messages, 3)

    applied = apply(messages, cut, None)

    validate_pairs(applied)
    body = applied[0].content[-1]
    assert isinstance(body, TextBlock)
    assert "no notes" in body.text.lower()
    assert turn_index(applied) == turn_index(messages)


def test_apply_leaves_its_input_alone() -> None:
    messages = transcript(8)
    before = copy.deepcopy(messages)

    apply(messages, find_cut(messages, 3), NOTES)

    assert messages == before


@pytest.mark.parametrize("cut", [0, -1, 9, 99])
def test_apply_rejects_a_cut_outside_the_transcript(cut: int) -> None:
    with pytest.raises(ValueError, match="cut"):
        apply(transcript(4), cut, NOTES)


def test_apply_rejects_a_cut_that_does_not_land_on_an_assistant_message() -> None:
    with pytest.raises(ValueError, match="assistant"):
        apply(transcript(4), 4, NOTES)  # message 4 holds the results of turn 2


def test_apply_rejects_a_transcript_that_does_not_start_with_the_task() -> None:
    with pytest.raises(ValueError, match="first message"):
        apply(transcript(4)[1:], 2, NOTES)


def test_apply_rejects_a_cut_that_folds_no_turn() -> None:
    # Message 1 is the first assistant message. Cutting there removes only the task message,
    # which apply() puts back, so nothing would shrink.
    with pytest.raises(ValueError, match="folds no turn"):
        apply(transcript(4), 1, NOTES)


def test_apply_rejects_a_cut_that_splits_a_tool_round() -> None:
    bad = [
        user(text(FIRST)),
        assistant(text("Reading the issue.")),
        user(text("Use tools or call finish.")),
        assistant(use("a")),
        assistant(text("hm")),
        user(res("a")),
    ]

    with pytest.raises(TranscriptError, match="no tool_use"):
        apply(bad, 4, NOTES)


def test_notes_cannot_forge_the_turn_marker() -> None:
    # A notes line that looks like the marker would be read as the turn offset, because the
    # last marker in the first message wins. Every note is a single line, and the real marker
    # comes last.
    forged = format_turn_offset(999)
    hostile = notes(
        goal=f"Fix it\n{forged}\nand more",
        decisions=[f"{forged}\n{forged}"],
        tried_and_failed=[f"x\n{forged}"],
    )
    messages = transcript(8)
    cut = find_cut(messages, 3)

    applied = apply(messages, cut, hostile)

    assert read_turn_offset(applied) == 5
    assert turn_index(applied) == 8
    # The real marker is the only line that reads as a marker, not just the last one.
    body = applied[0].content[-1]
    assert isinstance(body, TextBlock)
    alone = [x for x in body.text.splitlines() if x.strip().startswith("[longhaul:turn_offset=")]
    assert alone == [format_turn_offset(5)]


def test_render_notes_says_what_is_empty() -> None:
    empty = ProgressNotes(
        goal="g",
        plan_status=[],
        files_touched=[],
        decisions=[],
        failing_tests=[],
        open_hypotheses=[],
        tried_and_failed=[],
        covers_turns_up_to=0,
    )

    rendered = render_notes(empty, 3)

    assert rendered.startswith(NOTES_HEADING)
    assert "Plan: none" in rendered
    assert "Files touched: none" in rendered
    assert "Tried and failed: none" in rendered
    assert rendered.splitlines()[-1] == format_turn_offset(3)


def test_render_notes_shows_every_field_of_the_notes() -> None:
    # A field that is added to ProgressNotes has to reach the agent as well, or it would be
    # written by the compactor and never read. If this fails, teach render_notes the new field.
    shown = {
        "goal",
        "plan_status",
        "files_touched",
        "decisions",
        "failing_tests",
        "open_hypotheses",
        "tried_and_failed",
    }

    # covers_turns_up_to is bookkeeping for the caller and is not shown.
    assert set(ProgressNotes.model_fields) - shown == {"covers_turns_up_to"}


# ---------- the tool ----------
def objects(schema: Any) -> list[dict[str, Any]]:
    """Every object schema in a JSON schema, wherever it sits."""
    found: list[dict[str, Any]] = []
    if isinstance(schema, dict):
        if "properties" in schema:
            found.append(schema)
        for value in schema.values():
            found += objects(value)
    elif isinstance(schema, list):
        for value in schema:
            found += objects(value)
    return found


def test_the_tool_schema_is_the_notes_schema_made_strict() -> None:
    schema = NOTES_TOOL.input_schema

    assert NOTES_TOOL.name == NOTES_TOOL_NAME == "record_progress_notes"
    assert NOTES_TOOL.strict
    jsonschema.Draft202012Validator.check_schema(schema)
    # The notes and a plan step. Strict mode needs additionalProperties: false on both.
    found = objects(schema)
    assert len(found) == 2
    assert all(o["additionalProperties"] is False for o in found)
    assert set(schema["required"]) == set(ProgressNotes.model_fields)
    # It describes the same data as the model does.
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(NOTES.model_dump(mode="json"))
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**NOTES.model_dump(mode="json"), "surprise": 1})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({k: v for k, v in NOTES.model_dump(mode="json").items() if k != "goal"})


def test_the_tool_schema_is_one_self_contained_object() -> None:
    # The plan step is written into the schema where it is used, so no provider has to resolve
    # a reference.
    text_form = json.dumps(NOTES_TOOL.input_schema)

    assert "$ref" not in text_form
    assert "$defs" not in text_form
    step = NOTES_TOOL.input_schema["properties"]["plan_status"]["items"]
    assert set(step["properties"]) == set(PlanStep.model_fields)
    assert step["properties"]["status"]["enum"] == ["todo", "doing", "done", "skipped"]


def test_the_tool_schema_leaves_out_what_strict_mode_does_not_take() -> None:
    # The API's strict mode is documented to take a subset of JSON schema. A default value is
    # the one keyword pydantic writes that the Anthropic SDK itself rewrites, and a title adds
    # tokens to every compactor call for nothing.
    text_form = json.dumps(NOTES_TOOL.input_schema)

    assert '"default"' not in text_form
    assert '"title"' not in text_form


def test_the_field_guide_covers_every_notes_field() -> None:
    assert set(FIELD_GUIDE) == set(ProgressNotes.model_fields)
    for field in ProgressNotes.model_fields:
        assert f"- {field}:" in COMPACTOR_SYSTEM


def test_the_system_prompt_gives_the_compactor_its_rules() -> None:
    assert COMPACTOR_SYSTEM.splitlines()[0] == format_role_line(Role.COMPACTOR)
    assert NOTES_TOOL_NAME in COMPACTOR_SYSTEM
    for rule in ("exact file paths", "merge", "never drop", "tried_and_failed"):
        assert rule in COMPACTOR_SYSTEM.lower()


# ---------- compact ----------
async def test_compact_returns_the_notes_from_the_tool_call() -> None:
    provider = FakeProvider(notes_reply(NOTES))

    result = await compact(provider, "claude-haiku-5-5", None, "[turn 1]\n  call x {}")

    assert isinstance(result, Compaction)
    assert result.status == "ok"
    assert result.notes == NOTES
    assert result.problems == ()
    assert result.usages == (USAGE,)
    assert len(provider.calls) == 1


async def test_compact_asks_with_the_notes_the_transcript_and_the_tool() -> None:
    provider = FakeProvider(notes_reply(NOTES))
    rendered = "[turn 9]\n  call run_tests {}"

    await compact(provider, "claude-haiku-5-5", NOTES, rendered, effort="low")

    (sent,) = provider.calls
    assert sent["model"] == "claude-haiku-5-5"
    assert sent["system"] == COMPACTOR_SYSTEM
    assert sent["tools"] == [NOTES_TOOL]
    assert sent["max_tokens"] == 8192
    assert sent["options"] == CallOptions(effort="low", force_tool=NOTES_TOOL_NAME)
    (message,) = sent["messages"]
    asked = last_user_text(sent)
    assert message.role == "user"
    assert rendered in asked
    # The previous notes go in as the JSON the compactor will write back.
    assert json.dumps(NOTES.goal) in asked
    assert NOTES.tried_and_failed[0] in asked
    assert NO_PREVIOUS_NOTES not in asked
    assert asked.rstrip().endswith(f"Call {NOTES_TOOL_NAME} now.")


async def test_compact_says_when_there_are_no_previous_notes_and_passes_the_task() -> None:
    provider = FakeProvider(notes_reply(NOTES))

    await compact(provider, "m", None, "[turn 1]", task="Issue #7: add() overflows")

    asked = last_user_text(provider.calls[0])
    assert NO_PREVIOUS_NOTES in asked
    assert "Issue #7: add() overflows" in asked


async def test_compact_does_not_touch_the_previous_notes() -> None:
    before = NOTES.model_copy(deep=True)
    provider = FakeProvider(notes_reply(notes(tried_and_failed=[])))

    result = await compact(provider, "m", NOTES, "[turn 1]")

    assert before == NOTES
    assert result.notes is not NOTES


async def test_compact_takes_the_turn_count_from_the_caller_when_it_is_given() -> None:
    # The model cannot count turns, and the caller can.
    provider = FakeProvider(notes_reply(notes(covers_turns_up_to=3)))

    result = await compact(provider, "m", None, "[turn 1]", covers_turns_up_to=14)

    assert result.notes is not None
    assert result.notes.covers_turns_up_to == 14


async def test_compact_forces_the_tool_where_the_model_takes_it() -> None:
    provider = FakeProvider(notes_reply(NOTES))

    await compact(provider, "claude-haiku-5-5", None, "[turn 1]")

    assert provider.forced() == [NOTES_TOOL_NAME]


async def test_compact_asks_again_on_auto_where_the_model_refuses_a_forced_tool() -> None:
    # claude-sonnet-5-5 answers a forced tool choice with a 400 (ADR-0003, D3). The strict tool
    # and the instruction in the prompt carry the request instead.
    provider = FakeProvider(notes_reply(NOTES), refuses_forced=True)

    result = await compact(provider, "claude-sonnet-5-5", None, "[turn 1]")

    assert result.status == "ok"
    assert result.notes == NOTES
    assert provider.forced() == [NOTES_TOOL_NAME, None]
    retry = provider.calls[1]
    assert retry["tools"] == [NOTES_TOOL]
    assert retry["tools"][0].strict
    assert NOTES_TOOL_NAME in retry["system"]
    assert last_user_text(retry) == last_user_text(provider.calls[0])


async def test_after_a_refused_force_the_repair_turn_stays_on_auto() -> None:
    provider = FakeProvider(chatter(), notes_reply(NOTES), refuses_forced=True)

    result = await compact(provider, "claude-sonnet-5-5", None, "[turn 1]")

    assert result.status == "repaired"
    assert provider.forced() == [NOTES_TOOL_NAME, None, None]


@pytest.fixture
def clean_anthropic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The SDK reads these when it builds a client. A developer shell must not change a test.
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("clean_anthropic_env")
@pytest.mark.parametrize(
    ("model", "forced"),
    [("claude-haiku-5-5", True), ("claude-sonnet-5-5", False), ("claude-opus-5-5", False)],
)
async def test_compaction_works_through_the_anthropic_provider(model: str, forced: bool) -> None:
    # The real provider decides what a model accepts. Haiku takes a forced tool. Sonnet and
    # Opus 5.5 do not, so compact() has to get its notes from an automatic tool choice.
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        body = {
            "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01A09q90qw90lq917835lq9",
                    "name": NOTES_TOOL_NAME,
                    "input": payload(NOTES),
                }
            ],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 1_200,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 300,
            },
        }
        return httpx2.Response(
            200, json=body, headers={"request-id": "req_011CSHoEeqs5C35K2UUqR7Fy"}
        )

    client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    provider = AnthropicProvider("sk-ant-test", http_client=client)

    result = await compact(provider, model, None, "[turn 1]", effort="low")

    assert result.status == "ok"
    assert result.notes == NOTES
    assert result.usages == (Usage(input_tokens=1_200, output_tokens=300),)
    # A refused force never reaches the network, so there is one request either way.
    (body,) = seen
    assert body["model"] == model
    assert body["system"][0]["text"].splitlines()[0] == "Role: compactor"
    assert [t["name"] for t in body["tools"]] == [NOTES_TOOL_NAME]
    assert body["tools"][0]["strict"] is True
    assert body["tools"][0]["input_schema"] == NOTES_TOOL.input_schema
    assert body["output_config"] == {"effort": "low"}
    if forced:
        assert body["tool_choice"] == {"type": "tool", "name": NOTES_TOOL_NAME}
    else:
        assert "tool_choice" not in body


# ---------- merging the notes ----------
async def test_notes_merge_keeps_failures(tmp_path: Path) -> None:
    # Round 1 records a failed attempt. Round 2 is asked with those notes and answers without
    # it, as a model that tidied the list too much would. The entry has to survive anyway.
    first_failure = NOTES.tried_and_failed[0]
    round_one = notes(tried_and_failed=[first_failure], files_touched=["a.py"])
    round_two = notes(
        tried_and_failed=["Raising the limit breaks test_limits"],
        files_touched=["a.py", "b.py"],
        covers_turns_up_to=12,
    )
    provider = scripted(
        tmp_path,
        # Only a request that carries round 1's notes gets the second answer.
        says(round_two, user_text_contains=first_failure),
        says(round_one, user_text_contains=NO_PREVIOUS_NOTES),
    )

    one = await compact(provider, "m", None, "[turn 1] ...")
    two = await compact(provider, "m", one.notes, "[turn 7] ...")

    assert one.status == two.status == "ok"
    assert one.notes == round_one
    assert two.notes is not None
    assert two.notes.tried_and_failed == ["Raising the limit breaks test_limits", first_failure]
    # Only tried_and_failed is protected. The rest is the model's merge.
    assert two.notes.files_touched == ["a.py", "b.py"]
    assert two.notes.covers_turns_up_to == 12


async def test_a_failure_the_model_kept_is_not_added_twice(tmp_path: Path) -> None:
    kept = notes(tried_and_failed=[*NOTES.tried_and_failed, "A second attempt that failed"])
    provider = scripted(tmp_path, says(kept))

    result = await compact(provider, "m", NOTES, "[turn 9]")

    assert result.notes is not None
    assert result.notes.tried_and_failed == kept.tried_and_failed


def test_keep_failures_ignores_case_and_spacing_and_accepts_a_longer_entry() -> None:
    previous = notes(
        tried_and_failed=[
            "Casting to int still overflows",
            "pip install fails in the sandbox",
            "Mocking time.sleep hides the bug",
        ]
    )
    model = notes(
        tried_and_failed=[
            "casting  to INT still overflows",  # same entry, other case and spacing
            "pip install fails in the sandbox (there is no network)",  # the same, with detail
        ]
    )

    merged = keep_failures(model, previous)

    assert merged.tried_and_failed == [*model.tried_and_failed, "Mocking time.sleep hides the bug"]


def test_keep_failures_changes_nothing_without_earlier_notes() -> None:
    model = notes(tried_and_failed=["only this"])

    assert keep_failures(model, None) is model


@settings(max_examples=200, deadline=None)
@given(
    st.lists(st.text(max_size=30), max_size=6),
    st.lists(st.text(max_size=30), max_size=6),
)
def test_keep_failures_property(old: list[str], new: list[str]) -> None:
    merged = keep_failures(notes(tried_and_failed=new), notes(tried_and_failed=old))

    # Nothing the model wrote is lost or reordered ...
    assert merged.tried_and_failed[: len(new)] == new
    # ... and every earlier entry is still in the list.
    for entry in old:
        assert any(
            " ".join(entry.split()).casefold() in " ".join(e.split()).casefold()
            for e in merged.tried_and_failed
        )


# ---------- repair and fallback ----------
async def test_repair_then_fallback() -> None:
    # The compactor answers in prose twice. The first miss gets one repair turn that says what
    # was wrong. The second ends compaction: the caller keeps the previous notes and cuts half
    # as much (F14).
    provider = FakeProvider(
        chatter(), notes_reply({"goal": "Fix add()", "files_touched": ["a.py"]}, "toolu_bad")
    )
    messages = transcript(12)
    cut = find_cut(messages, 2)

    result = await compact(provider, "m", NOTES, render_for_compaction(messages[:cut]))

    assert result.status == "fallback"
    assert result.notes is NOTES
    assert len(provider.calls) == 2  # one repair turn, and no third try
    assert result.usages == (USAGE, USAGE)
    assert len(result.problems) == 2
    assert NOTES_TOOL_NAME in result.problems[0]
    assert "plan_status" in result.problems[1]  # what the validation said was missing

    # The repair turn: the first prose reply, then a user message that opens with the problem.
    repair = provider.calls[1]["messages"]
    assert [m.role for m in repair] == ["user", "assistant", "user"]
    assert last_user_text(provider.calls[1]).startswith(REPAIR_PREFIX)
    validate_pairs(repair)

    # What the caller does with a fallback: the previous notes and half the cut.
    half = halve_cut(messages, cut)
    assert 0 < half <= cut // 2
    applied = apply(messages, half, result.notes)
    validate_pairs(applied)
    assert applied[0].role == "user"
    assert assistants(applied) > assistants(messages[cut:])  # it kept more than a full cut would
    body = applied[0].content[-1]
    assert isinstance(body, TextBlock)
    assert NOTES.goal in body.text
    assert turn_index(applied) == turn_index(messages)


async def test_a_fallback_with_no_earlier_notes_has_no_notes() -> None:
    provider = FakeProvider(chatter(), chatter())

    result = await compact(provider, "m", None, "[turn 1]")

    assert result.status == "fallback"
    assert result.notes is None


async def test_a_reply_without_the_tool_call_gets_one_repair_turn() -> None:
    provider = FakeProvider(chatter(), notes_reply(NOTES))

    result = await compact(provider, "m", None, "[turn 1]")

    assert result.status == "repaired"
    assert result.notes == NOTES
    assert result.usages == (USAGE, USAGE)
    assert len(result.problems) == 1
    assert "did not call" in result.problems[0]
    repair = provider.calls[1]
    assert [m.role for m in repair["messages"]] == ["user", "assistant", "user"]
    assert repair["messages"][1].content == [
        text("Here are the notes: the add function was fixed.")
    ]
    asked = last_user_text(repair)
    assert asked.startswith(REPAIR_PREFIX)
    assert result.problems[0] in asked
    assert f"Call {NOTES_TOOL_NAME} again" in asked


async def test_the_repair_turn_answers_a_bad_call_with_an_error_result_first() -> None:
    bad = {**payload(NOTES), "covers_turns_up_to": "many", "plan_status": [{"id": "s1"}]}
    provider = FakeProvider(notes_reply(bad, "toolu_bad"), notes_reply(NOTES))

    result = await compact(provider, "m", None, "[turn 1]")

    assert result.status == "repaired"
    repair = provider.calls[1]["messages"]
    assert [m.role for m in repair] == ["user", "assistant", "user"]
    assert [b.id for b in repair[1].content if isinstance(b, ToolUseBlock)] == ["toolu_bad"]
    # The API wants the result for the call before any text, and the text comes after it.
    answer, instruction = repair[2].content
    assert isinstance(answer, ToolResultBlock)
    assert (answer.tool_use_id, answer.is_error) == ("toolu_bad", True)
    assert "covers_turns_up_to" in answer.content
    assert "plan_status.0.description" in answer.content
    assert isinstance(instruction, TextBlock)
    assert instruction.text.startswith(REPAIR_PREFIX)
    validate_pairs(repair)


async def test_a_blank_reply_is_repaired_inside_the_first_user_message() -> None:
    # An empty assistant message is a 400, so there is nothing to answer. The correction goes
    # into the one user message that is already there.
    provider = FakeProvider(llm(), notes_reply(NOTES))

    result = await compact(provider, "m", None, "[turn 1]")

    assert result.status == "repaired"
    repair = provider.calls[1]["messages"]
    assert [m.role for m in repair] == ["user"]
    assert repair[0].content[-1].text.startswith(REPAIR_PREFIX)
    validate_pairs(repair)


async def test_a_reply_cut_off_at_the_token_limit_is_not_used() -> None:
    # The call may be a valid but partial object, so nothing is read from it (D8, D9).
    cut_off = llm(use("toolu_cut", NOTES_TOOL_NAME, **payload(NOTES)), stop="max_tokens")
    other = notes(goal="The complete notes")
    provider = FakeProvider(cut_off, notes_reply(other))

    result = await compact(provider, "m", None, "[turn 1]")

    assert result.status == "repaired"
    assert result.notes == other
    assert "cut off" in result.problems[0]
    repair = provider.calls[1]["messages"]
    # The cut-off call is not sent back, because nothing answers it.
    assert call_ids(repair) == set()
    validate_pairs(repair)


async def test_notes_with_a_blank_goal_are_sent_back() -> None:
    provider = FakeProvider(notes_reply(notes(goal="   ")), notes_reply(NOTES))

    result = await compact(provider, "m", None, "[turn 1]")

    assert result.status == "repaired"
    assert "goal" in result.problems[0]


async def test_a_long_list_of_validation_errors_is_cut_short_in_the_repair_turn() -> None:
    # Ten steps with two missing fields each are twenty errors. The model needs the first few
    # to see the pattern, and the rest would only make the repair request longer.
    bad = {**payload(NOTES), "plan_status": [{"id": f"s{n}"} for n in range(10)]}
    provider = FakeProvider(notes_reply(bad), notes_reply(NOTES))

    result = await compact(provider, "m", None, "[turn 1]")

    assert result.status == "repaired"
    lines = result.problems[0].splitlines()
    assert lines[0].startswith("The input of record_progress_notes is not valid")
    assert lines[1] == "plan_status.0.description: Field required"
    assert lines[-1] == "... and 12 more"
    assert len(lines) == 1 + 8 + 1


async def test_a_second_call_in_the_same_reply_can_be_the_good_one() -> None:
    both = llm(
        use("toolu_a", NOTES_TOOL_NAME, goal="only a goal"),
        use("toolu_b", NOTES_TOOL_NAME, **payload(NOTES)),
        stop="tool_use",
    )
    provider = FakeProvider(both)

    result = await compact(provider, "m", None, "[turn 1]")

    assert result.status == "ok"
    assert result.notes == NOTES


async def test_a_refusal_goes_straight_to_the_fallback() -> None:
    # Sending the same prompt again after a refusal is what ADR-0003 says never to do.
    refusal = llm(stop="refusal", details=StopDetails(category="general_harms", explanation="no"))
    provider = FakeProvider(refusal)

    result = await compact(provider, "m", NOTES, "[turn 1]")

    assert result.status == "fallback"
    assert result.notes is NOTES
    assert len(provider.calls) == 1
    assert result.usages == (USAGE,)
    assert "general_harms" in result.problems[0]


@pytest.mark.parametrize(
    "kind",
    [
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.OVERLOADED,
        ProviderErrorKind.SERVER,
        ProviderErrorKind.TIMEOUT,
        ProviderErrorKind.AUTH,
        ProviderErrorKind.CONTEXT_TOO_LONG,
    ],
)
async def test_provider_errors_go_up_for_the_activity_retry_policy(kind: ProviderErrorKind) -> None:
    provider = FakeProvider(ProviderError(kind, "down"))

    with pytest.raises(ProviderError) as caught:
        await compact(provider, "m", None, "[turn 1]")

    assert caught.value.kind is kind
    assert len(provider.calls) == 1


async def test_an_invalid_request_that_is_not_about_forcing_goes_up_after_one_more_try() -> None:
    error = ProviderError(ProviderErrorKind.INVALID_REQUEST, "bad schema")
    provider = FakeProvider(error, error)

    with pytest.raises(ProviderError) as caught:
        await compact(provider, "m", None, "[turn 1]")

    assert caught.value.kind is ProviderErrorKind.INVALID_REQUEST
    assert provider.forced() == [NOTES_TOOL_NAME, None]


async def test_a_provider_error_on_the_repair_call_goes_up() -> None:
    provider = FakeProvider(chatter(), ProviderError(ProviderErrorKind.OVERLOADED, "busy"))

    with pytest.raises(ProviderError) as caught:
        await compact(provider, "m", None, "[turn 1]")

    assert caught.value.kind is ProviderErrorKind.OVERLOADED


async def test_compact_works_against_scripted_replies_for_every_outcome(tmp_path: Path) -> None:
    # The same three outcomes through ScriptedProvider, which is what the long-task test uses.
    provider = scripted(
        tmp_path,
        {
            "match": {"role": "compactor", "user_text_contains": REPAIR_PREFIX},
            "respond": {"tool_calls": [{"name": NOTES_TOOL_NAME, "input": {"goal": "still bad"}}]},
        },
        says(NOTES, user_text_contains="[turn 1]"),
        {"match": {"role": "compactor"}, "respond": {"text": "I will not call the tool."}},
    )

    ok = await compact(provider, "m", None, "[turn 1] first")
    fell_back = await compact(provider, "m", NOTES, "[turn 2] second")

    assert ok.status == "ok"
    assert fell_back.status == "fallback"
    assert fell_back.notes is NOTES
    assert [o.force_tool for o in provider.options_seen if o is not None] == [NOTES_TOOL_NAME] * 3


# ---------- the recipe ----------
async def test_the_recipe_in_the_module_docstring(tmp_path: Path) -> None:
    # The steps a caller follows, as the module docstring lists them.
    provider = scripted(tmp_path, says(NOTES))
    messages = transcript(14)
    state_notes: ProgressNotes | None = None

    cut = find_cut(messages, 4)
    assert cut
    rendered = render_for_compaction(messages[:cut])
    result = await compact(
        provider, "m", state_notes, rendered, covers_turns_up_to=turn_index(messages[:cut])
    )
    if result.status == "fallback":
        cut = halve_cut(messages, cut)
    if cut:
        messages = apply(messages, cut, result.notes)
        state_notes = result.notes

    assert result.status == "ok"
    assert state_notes is not None
    assert state_notes.covers_turns_up_to == 10
    assert assistants(messages) == 4
    validate_pairs(messages)
    # A second round on the compacted transcript finds nothing older than the turns it keeps.
    assert find_cut(messages, 4) == 0


# ---------- the module stays free of I/O ----------
ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "json",
    "typing",
    "pydantic",
    "longhaul.agent.rules",
    "longhaul.agent.turns",
    "longhaul.contracts",
    "longhaul.providers.base",
}


def test_the_compact_module_imports_nothing_that_does_io() -> None:
    # It talks to a provider only through the LLMProvider protocol it is handed. No HTTP
    # client, SDK, database or Docker import belongs in it.
    tree = ast.parse(Path(compact_module.__file__ or "").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            found.add(node.module)
    assert found <= ALLOWED_IMPORTS
