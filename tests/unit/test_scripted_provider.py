import json
import re
from pathlib import Path

import pytest

from longhaul.contracts import (
    LLMResponse,
    Message,
    ProviderError,
    ProviderErrorKind,
    Role,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from longhaul.providers.base import (
    ROLE_LINE_FORMAT,
    format_role_line,
    format_turn_offset,
    get_provider,
    parse_role_line,
    read_turn_offset,
    turn_index,
)
from longhaul.providers.scripted import ScriptedProvider
from longhaul.settings import EnvSettings

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

SCRIPT = Path(__file__).parent.parent / "fixtures" / "scripts" / "calc_fix.yaml"
SYSTEM = "Role: coder\nFix the bug."


def _tool(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"The {name} tool.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )


def _spec_chars(spec: ToolSpec) -> int:
    return len(spec.name) + len(spec.description) + len(json.dumps(spec.input_schema))


# Every tool the scripts in this file call, so a request offers them unless a test says otherwise.
TOOL_NAMES = ("list_dir", "read_file", "edit_file", "run_tests", "run_command", "finish")
TOOLS = [_tool(n) for n in (*TOOL_NAMES, "record_notes", "x")]


async def _call(
    p: ScriptedProvider,
    messages: list[Message],
    system: str = SYSTEM,
    tools: list[ToolSpec] | None = None,
) -> LLMResponse:
    offered = TOOLS if tools is None else tools
    return await p.complete(
        model="m", system=system, messages=messages, tools=offered, max_tokens=100
    )


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "s.yaml"
    path.write_text(body)
    return path


def _turn(i: int, result: str = "ok") -> list[Message]:
    """Transcript with i completed assistant turns, ending on a user tool_result."""
    msgs = [Message(role="user", content=[TextBlock(text="go")])]
    for n in range(i):
        msgs.append(
            Message(
                role="assistant",
                content=[ToolUseBlock(id=f"toolu_{n:03d}_0", name="x", input={})],
            )
        )
        msgs.append(
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id=f"toolu_{n:03d}_0", content=result)],
            )
        )
    return msgs


def _assistant(resp: LLMResponse) -> Message:
    return Message(role="assistant", content=resp.content)


def _results(resp: LLMResponse, output: str = "ok") -> Message:
    blocks = [ToolResultBlock(tool_use_id=c.id, content=output) for c in resp.tool_calls]
    return Message(role="user", content=list(blocks))


async def _play(
    p: ScriptedProvider,
    turns: int,
    messages: list[Message] | None = None,
    system: str = SYSTEM,
    output: str = "ok",
) -> tuple[list[Message], list[str]]:
    """Play more turns with the provider's own answers, as the agent loop would.

    Returns the transcript and the `command` input each turn chose. The scripts
    that use it call run_command with a command.
    """
    msgs = list(messages) if messages else [Message(role="user", content=[TextBlock(text="go")])]
    chosen: list[str] = []
    for _ in range(turns):
        resp = await _call(p, msgs, system)
        chosen.append(str(resp.tool_calls[0].input["command"]))
        msgs += [_assistant(resp), _results(resp, output)]
    return msgs, chosen


def _compact(
    messages: list[Message], keep_recent_turns: int, already_folded: int = 0
) -> list[Message]:
    """What LH-403's apply() returns: [user: task and notes] + messages[cut:].

    The notes message carries the turn marker, which counts every turn folded so far.
    """
    assistants = [i for i, m in enumerate(messages) if m.role == "assistant"]
    cut = assistants[-keep_recent_turns]
    folded = already_folded + sum(1 for m in messages[:cut] if m.role == "assistant")
    notes = f"task: go\n\nProgress notes\n{format_turn_offset(folded)}\ngoal: fix the repo"
    return [Message(role="user", content=[TextBlock(text=notes)]), *messages[cut:]]


def _tool_use_ids(messages: list[Message]) -> list[str]:
    return [b.id for m in messages for b in m.content if isinstance(b, ToolUseBlock)]


def _ten_steps() -> str:
    """Ten steps without a match: step 0 to step 9, run by turn."""
    return "steps:\n" + "".join(
        f"  - respond: {{tool_calls: [{{name: run_command, input: {{command: 'step {i}'}}}}]}}\n"
        for i in range(10)
    )


async def test_sequence() -> None:
    p = ScriptedProvider(SCRIPT)
    assert p.name == "scripted"
    names = []
    ids = []
    for i in range(5):
        resp = await _call(p, _turn(i))
        names.append(resp.tool_calls[0].name)
        ids.append(resp.tool_calls[0].id)
        assert resp.stop_reason == "tool_use"
        assert resp.latency_ms == 0
    assert names == ["list_dir", "read_file", "edit_file", "run_tests", "finish"]
    assert len(set(ids)) == 5


async def test_match_on_tool_result(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
steps:
  - match: {last_tool_result_contains: "FAILED"}
    respond: {tool_calls: [{name: edit_file, input: {}}]}
  - match: {role: coder}
    respond: {text: "done"}
""",
    )
    p = ScriptedProvider(path)
    hit = await _call(p, _turn(1, "1 FAILED"))
    assert hit.tool_calls[0].name == "edit_file"
    other = await _call(p, _turn(1, "all good"))
    assert other.tool_calls == []
    assert other.stop_reason == "end_turn"
    assert other.content[0] == TextBlock(text="done")
    # role mismatch skips the step
    with pytest.raises(ProviderError):
        await _call(p, _turn(1, "all good"), system="Role: verifier\nx")


async def test_unmatched_steps_use_their_position(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
steps:
  - respond: {text: "zero"}
  - match: {last_tool_result_contains: "boom"}
    respond: {text: "boom"}
  - respond: {text: "one", stop_reason: max_tokens}
""",
    )
    p = ScriptedProvider(path)
    assert (await _call(p, _turn(0))).content[0] == TextBlock(text="zero")
    last = await _call(p, _turn(1))
    assert last.content[0] == TextBlock(text="one")
    assert last.stop_reason == "max_tokens"
    # The step in the middle has a match, so it does not take a position among the bare steps.
    assert (await _call(p, _turn(1, "boom"))).content[0] == TextBlock(text="boom")


async def test_usage_is_deterministic() -> None:
    p = ScriptedProvider(SCRIPT)
    tools = [_tool("list_dir")]
    resp = await _call(p, _turn(0), tools=tools)
    again = await _call(p, _turn(0), tools=tools)
    assert resp.usage == again.usage
    assert resp.usage.output_tokens == 50 + len("Looking at the repo layout.") // 4
    expected = await p.count_tokens(model="m", system=SYSTEM, messages=_turn(0), tools=tools)
    assert resp.usage.input_tokens == expected
    assert expected == (len(SYSTEM) + len("go") + _spec_chars(tools[0])) // 4


async def test_no_match_raises() -> None:
    p = ScriptedProvider(SCRIPT)
    with pytest.raises(ProviderError) as exc:
        await _call(p, _turn(9, "the first line " + "z" * 600 + " 1 failed, 2 passed"))
    assert exc.value.kind is ProviderErrorKind.INVALID_REQUEST
    msg = exc.value.message
    assert "turn_index=9" in msg
    assert "Role: coder" in msg
    # The failures and the summary are at the end of an output, so the end is what is shown.
    assert "1 failed, 2 passed" in msg
    assert "the first line" not in msg
    assert "z" * 500 not in msg


async def test_no_match_without_tool_result_or_role() -> None:
    p = ScriptedProvider(SCRIPT)
    first = [Message(role="user", content=[TextBlock(text="hi")])]
    with pytest.raises(ProviderError) as exc:
        await _call(p, first + _turn(7)[1:], system="plain prompt")
    assert "role=None" in exc.value.message
    with pytest.raises(ProviderError, match="last_tool_result=None"):
        await _call(p, [Message(role="assistant", content=[TextBlock(text="a")])] * 9)


async def test_raise_step(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
steps:
  - respond: {raise: overloaded, retry_after_s: 2.5}
""",
    )
    with pytest.raises(ProviderError) as exc:
        await _call(ScriptedProvider(path), _turn(0))
    assert exc.value.kind is ProviderErrorKind.OVERLOADED
    assert exc.value.retryable is True
    assert exc.value.retry_after_s == 2.5


@pytest.mark.parametrize(
    "body",
    [
        "steps:\n  - respnd: {text: hi}\n",
        "steps:\n  - match: {turn_idx: 1}\n    respond: {text: hi}\n",
        "steps:\n  - respond: {raise: nonsense}\n",
        "stepz: []\n",
    ],
)
def test_bad_script_key_rejected(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValueError):
        ScriptedProvider(_write(tmp_path, body))


def test_bad_script_names_the_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="respnd"):
        ScriptedProvider(_write(tmp_path, "steps:\n  - respnd: {text: hi}\n"))


def test_get_provider_scripted_and_unimplemented() -> None:
    scripted = get_provider(EnvSettings(provider="scripted"), script_path=SCRIPT)
    assert scripted.name == "scripted"
    with pytest.raises(ValueError, match="script_path"):
        get_provider(EnvSettings(provider="scripted"))
    for kind, story in (("anthropic", "LH-104"), ("replay", "LH-209")):
        with pytest.raises(NotImplementedError, match=story):
            get_provider(EnvSettings(provider=kind))  # type: ignore[arg-type]


async def test_match_on_turn_index(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "steps:\n  - match: {turn_index: 2}\n    respond: {text: two}\n",
    )
    p = ScriptedProvider(path)
    assert (await _call(p, _turn(2))).content[0] == TextBlock(text="two")
    with pytest.raises(ProviderError):
        await _call(p, _turn(1))


# ---------- turn counting survives compaction ----------
async def test_turn_survives_compaction(tmp_path: Path) -> None:
    p = ScriptedProvider(_write(tmp_path, _ten_steps()))
    msgs, chosen = await _play(p, 6)
    assert chosen == [f"step {i}" for i in range(6)]
    view = _compact(msgs, keep_recent_turns=2)
    assert sum(m.role == "assistant" for m in view) == 2
    _, later = await _play(p, 4, view)
    assert later == ["step 6", "step 7", "step 8", "step 9"]


async def test_turn_offset_adds_up_over_two_compactions(tmp_path: Path) -> None:
    p = ScriptedProvider(_write(tmp_path, _ten_steps()))
    msgs, _ = await _play(p, 4)
    msgs, middle = await _play(p, 3, _compact(msgs, keep_recent_turns=2))
    _, last = await _play(p, 3, _compact(msgs, keep_recent_turns=2, already_folded=2))
    assert middle == ["step 4", "step 5", "step 6"]
    assert last == ["step 7", "step 8", "step 9"]


async def test_step_keyed_on_turn_5_matches_after_compaction(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
steps:
  - match: {turn_index: 5}
    respond: {text: "five"}
  - match: {role: coder}
    respond: {tool_calls: [{name: run_command, input: {command: filler}}]}
""",
    )
    p = ScriptedProvider(path)
    msgs, chosen = await _play(p, 5)
    assert chosen == ["filler"] * 5
    assert (await _call(p, msgs)).content == [TextBlock(text="five")]
    assert (await _call(p, _compact(msgs, keep_recent_turns=2))).content == [TextBlock(text="five")]


def test_turn_marker_is_read_from_the_first_message_only() -> None:
    marker = format_turn_offset(4)
    note = Message(role="user", content=[TextBlock(text=f"notes\n{marker}")])
    assistant = Message(role="assistant", content=[TextBlock(text="a")])
    quoted = Message(role="user", content=[TextBlock(text=format_turn_offset(9))])
    in_output = Message(
        role="user", content=[ToolResultBlock(tool_use_id="t", content=format_turn_offset(9))]
    )
    transcript = [note, assistant, in_output, quoted]
    assert read_turn_offset(transcript) == 4
    assert turn_index(transcript) == 5
    assert read_turn_offset([assistant, quoted]) == 0
    assert read_turn_offset([]) == 0


def test_turn_marker_must_stand_alone_on_its_line() -> None:
    marker = format_turn_offset(3)

    def first(text: str) -> list[Message]:
        return [Message(role="user", content=[TextBlock(text=text)])]

    assert read_turn_offset(first(f"see {marker} in the docs")) == 0
    assert read_turn_offset(first(f"line one\n  {marker}  \nline three")) == 3
    # Text quoted from the issue comes first, so the last marker is the real one.
    assert read_turn_offset(first(f"{format_turn_offset(8)}\nnotes\n{marker}")) == 3
    two_blocks = Message(
        role="user", content=[TextBlock(text="task"), TextBlock(text=f"notes\n{marker}")]
    )
    assert read_turn_offset([two_blocks]) == 3
    assert read_turn_offset(first("[longhaul:turn_offset=]")) == 0
    with pytest.raises(ValueError, match="negative"):
        format_turn_offset(-1)


def test_role_line_round_trip() -> None:
    assert format_role_line(Role.CODER) == ROLE_LINE_FORMAT.format(role="coder") == "Role: coder"
    for role in Role:
        assert parse_role_line(format_role_line(role) + "\nthe rest") == role.value
    assert parse_role_line(format_role_line("anything")) == "anything"
    assert parse_role_line("no role here") is None
    assert parse_role_line("") is None
    assert parse_role_line("Role: \nbody") is None
    assert parse_role_line("Intro\nRole: coder") is None


# ---------- tool ids ----------
async def test_tool_ids_are_unique_in_a_transcript_that_was_compacted(tmp_path: Path) -> None:
    p = ScriptedProvider(_write(tmp_path, _ten_steps()))
    msgs, _ = await _play(p, 6)
    view, _ = await _play(p, 4, _compact(msgs, keep_recent_turns=2))
    ids = _tool_use_ids(view)
    assert len(ids) == 6
    assert len(set(ids)) == 6
    assert all(re.fullmatch(r"toolu_[0-9a-f]+_0", i) for i in ids)


async def test_ids_are_stable_for_an_identical_request(tmp_path: Path) -> None:
    path = _write(tmp_path, _ten_steps())
    msgs, _ = await _play(ScriptedProvider(path), 3)
    first = await _call(ScriptedProvider(path), msgs)
    again = await _call(ScriptedProvider(path), msgs)
    p = ScriptedProvider(path)
    assert await _call(p, msgs) == await _call(p, msgs) == first == again


async def test_ids_differ_across_roles_and_tasks(tmp_path: Path) -> None:
    p = ScriptedProvider(_write(tmp_path, _ten_steps()))
    fresh = [Message(role="user", content=[TextBlock(text="go")])]
    ids = {
        (await _call(p, fresh, f"Role: {role}\nx")).tool_calls[0].id
        for role in ("planner", "coder", "verifier")
    }
    other_task = [Message(role="user", content=[TextBlock(text="a different issue")])]
    ids.add((await _call(p, other_task)).tool_calls[0].id)
    assert len(ids) == 4


async def test_calls_in_one_response_get_distinct_ids(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "steps:\n  - respond: {tool_calls: [{name: x, input: {}}, {name: x, input: {}}]}\n",
    )
    resp = await _call(ScriptedProvider(path), _turn(0))
    ids = [c.id for c in resp.tool_calls]
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert _tool_use_ids([_assistant(resp)]) == ids


# ---------- matching on user text ----------
COMPACTOR = "Role: compactor\nWrite the notes."
COMPACTOR_SCRIPT = """
steps:
  - match: {role: compactor, user_text_contains: "fixed mod3"}
    respond: {tool_calls: [{name: record_notes, input: {files_touched: [mod1, mod2, mod3]}}]}
  - match: {role: compactor, user_text_contains: "fixed mod2"}
    respond: {tool_calls: [{name: record_notes, input: {files_touched: [mod1, mod2]}}]}
  - match: {role: compactor}
    respond: {tool_calls: [{name: record_notes, input: {files_touched: [mod1]}}]}
"""


def _user(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


async def test_compactor_rounds_are_told_apart_by_user_text(tmp_path: Path) -> None:
    p = ScriptedProvider(_write(tmp_path, COMPACTOR_SCRIPT))
    rounds = [
        "Previous notes: none\nTranscript: fixed mod1",
        "Previous notes: mod1\nTranscript: fixed mod1, fixed mod2",
        "Previous notes: mod1, mod2\nTranscript: fixed mod2, fixed mod3",
    ]
    files = [
        (await _call(p, [_user(text)], COMPACTOR)).tool_calls[0].input["files_touched"]
        for text in rounds
    ]
    assert files == [["mod1"], ["mod1", "mod2"], ["mod1", "mod2", "mod3"]]


async def test_user_text_comes_from_the_last_user_message(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
steps:
  - match: {user_text_contains: "Hint from"}
    respond: {text: "thanks for the hint"}
  - match: {role: coder}
    respond: {tool_calls: [{name: x, input: {}}]}
""",
    )
    p = ScriptedProvider(path)
    hint = _user("Hint from @sam: try the other file")
    thanks = [TextBlock(text="thanks for the hint")]
    # The hint is a user message of its own, so there is no tool result in the last message.
    assert (await _call(p, [*_turn(1), hint])).content == thanks
    assert (await _call(p, _turn(1))).tool_calls[0].name == "x"
    # Once newer turns follow the hint, it is no longer the last user message.
    later = [*_turn(1), hint, *_turn(2)[3:]]
    assert (await _call(p, later)).tool_calls[0].name == "x"


async def test_user_text_and_tool_result_can_be_in_one_message(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
steps:
  - match: {last_tool_result_contains: "FAILED", user_text_contains: "Hint from"}
    respond: {text: "hint and failure"}
  - match: {role: coder}
    respond: {tool_calls: [{name: x, input: {}}]}
""",
    )
    p = ScriptedProvider(path)
    merged = Message(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="toolu_000_0", content="1 FAILED"),
            TextBlock(text="Hint from @sam: try the other file"),
        ],
    )
    msgs = [*_turn(1)[:-1], merged]
    assert (await _call(p, msgs)).content == [TextBlock(text="hint and failure")]
    no_hint = [*_turn(1)[:-1], Message(role="user", content=merged.content[:1])]
    assert (await _call(p, no_hint)).tool_calls[0].name == "x"


async def test_request_contains_searches_text_and_tool_results(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
steps:
  - match: {request_contains: "needle"}
    respond: {text: "found"}
  - match: {role: coder}
    respond: {text: "not found"}
""",
    )
    p = ScriptedProvider(path)
    found, missing = [TextBlock(text="found")], [TextBlock(text="not found")]
    in_task = [_user("the needle is in the task"), *_turn(1)[1:]]
    assert (await _call(p, in_task)).content == found
    assert (await _call(p, _turn(2, "a needle in the output"))).content == found
    # Tool inputs and the system prompt are not searched.
    use = Message(role="assistant", content=[ToolUseBlock(id="t", name="x", input={"q": "needle"})])
    assert (await _call(p, [*_turn(0), use, _user("ok")])).content == missing
    assert (await _call(p, _turn(1), system="Role: coder\nneedle")).content == missing


@pytest.mark.parametrize(
    "key", ["last_tool_result_contains", "user_text_contains", "request_contains"]
)
def test_text_matchers_reject_an_empty_string(tmp_path: Path, key: str) -> None:
    body = f'steps:\n  - match: {{{key}: ""}}\n    respond: {{text: hi}}\n'
    with pytest.raises(ValueError, match=rf"{key}[\s\S]*at least 1 character"):
        ScriptedProvider(_write(tmp_path, body))


# ---------- bounded raise steps ----------
async def test_raise_with_times_stops_after_that_many_identical_requests(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
steps:
  - match: {role: coder}
    respond: {raise: overloaded, retry_after_s: 1.5, times: 2}
  - match: {role: coder}
    respond: {text: "recovered"}
""",
    )
    p = ScriptedProvider(path)
    request = _turn(2)
    for _ in range(2):
        with pytest.raises(ProviderError) as exc:
            await _call(p, request)
        assert exc.value.kind is ProviderErrorKind.OVERLOADED
        assert exc.value.retry_after_s == 1.5
    assert (await _call(p, request)).content == [TextBlock(text="recovered")]
    assert (await _call(p, request)).content == [TextBlock(text="recovered")]
    # A different request has its own budget.
    with pytest.raises(ProviderError):
        await _call(p, _turn(3))


async def test_raise_without_times_never_stops(tmp_path: Path) -> None:
    path = _write(tmp_path, "steps:\n  - respond: {raise: overloaded}\n")
    p = ScriptedProvider(path)
    for _ in range(8):
        with pytest.raises(ProviderError) as exc:
            await _call(p, _turn(0))
        assert exc.value.kind is ProviderErrorKind.OVERLOADED


async def test_a_spent_raise_that_leaves_no_step_says_so(tmp_path: Path) -> None:
    path = _write(tmp_path, "steps:\n  - respond: {raise: overloaded, times: 1}\n")
    p = ScriptedProvider(path)
    with pytest.raises(ProviderError) as first:
        await _call(p, _turn(0))
    assert first.value.kind is ProviderErrorKind.OVERLOADED
    with pytest.raises(ProviderError) as second:
        await _call(p, _turn(0))
    assert second.value.kind is ProviderErrorKind.INVALID_REQUEST
    assert "already raised" in second.value.message
    assert "[0]" in second.value.message


OVERFLOW_ONCE = """
steps:
  - match:
      role: coder
      request_contains: "longhaul:turn_offset="
      last_tool_result_contains: "5000 lines of noise"
    respond: {tool_calls: [{name: run_command, input: {command: after compaction}}]}
  - match: {role: coder, last_tool_result_contains: "5000 lines of noise"}
    respond: {raise: context_too_long, times: 1}
  - match: {role: coder}
    respond: {tool_calls: [{name: run_command, input: {command: normal}}]}
"""


async def test_overflow_raises_once_then_the_compacted_retry_succeeds(tmp_path: Path) -> None:
    p = ScriptedProvider(_write(tmp_path, OVERFLOW_ONCE))
    msgs, chosen = await _play(p, 4)
    assert chosen == ["normal"] * 4
    resp = await _call(p, msgs)
    big = [*msgs, _assistant(resp), _results(resp, "...5000 lines of noise...")]
    with pytest.raises(ProviderError) as exc:
        await _call(p, big)
    assert exc.value.kind is ProviderErrorKind.CONTEXT_TOO_LONG
    # LH-404 compacts to the last two turns and sends the turn again.
    retry = await _call(p, _compact(big, keep_recent_turns=2))
    assert retry.tool_calls[0].input == {"command": "after compaction"}
    # The raise was one-shot: sending the first request again no longer fails.
    assert (await _call(p, big)).tool_calls[0].input == {"command": "normal"}


# ---------- offered tools and token counts ----------
async def test_call_to_a_tool_that_was_not_offered_is_rejected() -> None:
    p = ScriptedProvider(SCRIPT)
    with pytest.raises(ProviderError) as exc:
        await _call(p, _turn(0), tools=[])
    assert exc.value.kind is ProviderErrorKind.INVALID_REQUEST
    assert not exc.value.retryable
    assert "'list_dir'" in exc.value.message
    assert "not offered" in exc.value.message
    # The wrong subset is caught too, and the message lists what was offered.
    with pytest.raises(ProviderError, match=r"offered tools: \['read_file'\]"):
        await _call(p, _turn(0), tools=[_tool("read_file")])
    assert (await _call(p, _turn(0), tools=[_tool("list_dir")])).tool_calls[0].name == "list_dir"


async def test_every_call_in_a_reply_must_be_offered(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "steps:\n  - respond: {tool_calls: [{name: read_file, input: {}}, {name: edit_file}]}\n",
    )
    p = ScriptedProvider(path)
    with pytest.raises(ProviderError, match="'edit_file'"):
        await _call(p, _turn(0), tools=[_tool("read_file")])
    assert len((await _call(p, _turn(0))).tool_calls) == 2


async def test_replies_without_tool_calls_need_no_tools(tmp_path: Path) -> None:
    text = ScriptedProvider(_write(tmp_path, "steps:\n  - respond: {text: hello}\n"))
    assert (await _call(text, _turn(0), tools=[])).content == [TextBlock(text="hello")]
    raising = ScriptedProvider(_write(tmp_path, "steps:\n  - respond: {raise: overloaded}\n"))
    with pytest.raises(ProviderError) as exc:
        await _call(raising, _turn(0), tools=[])
    assert exc.value.kind is ProviderErrorKind.OVERLOADED


async def test_tool_specs_count_towards_input_tokens() -> None:
    p = ScriptedProvider(SCRIPT)
    specs = [_tool("list_dir"), _tool("read_file"), _tool("finish")]
    chars = len(SYSTEM) + len("go")

    async def counted(tools: list[ToolSpec]) -> int:
        resp = await _call(p, _turn(0), tools=tools)
        by_count = await p.count_tokens(model="m", system=SYSTEM, messages=_turn(0), tools=tools)
        assert resp.usage.input_tokens == by_count
        return by_count

    without = await p.count_tokens(model="m", system=SYSTEM, messages=_turn(0), tools=[])
    assert without == chars // 4
    assert await counted(specs[:1]) == (chars + _spec_chars(specs[0])) // 4
    assert await counted(specs) == (chars + sum(map(_spec_chars, specs))) // 4
    assert without < await counted(specs[:1]) < await counted(specs)


# ---------- roles ----------
ROLE_SCRIPT = """
steps:
  - match: {role: planner}
    respond: {tool_calls: [{name: list_dir, input: {}}]}
  - match: {role: verifier}
    respond: {tool_calls: [{name: run_tests, input: {}}]}
  - match: {role: coder}
    respond: {tool_calls: [{name: read_file, input: {path: a.py}}]}
"""


@pytest.mark.parametrize("role", ["verfier", "Coder", "CODER", "reviewer", "''"])
def test_unknown_role_is_rejected_when_the_script_loads(tmp_path: Path, role: str) -> None:
    body = f"steps:\n  - match: {{role: {role}}}\n    respond: {{text: hi}}\n"
    with pytest.raises(ValueError, match="'planner', 'coder', 'verifier' or 'compactor'"):
        ScriptedProvider(_write(tmp_path, body))


def test_every_role_value_loads(tmp_path: Path) -> None:
    for role in Role:
        body = f"steps:\n  - match: {{role: {role.value}}}\n    respond: {{text: hi}}\n"
        ScriptedProvider(_write(tmp_path, body))


async def test_each_role_gets_its_own_steps(tmp_path: Path) -> None:
    p = ScriptedProvider(_write(tmp_path, ROLE_SCRIPT))
    for role, tool in (
        (Role.PLANNER, "list_dir"),
        (Role.VERIFIER, "run_tests"),
        (Role.CODER, "read_file"),
    ):
        resp = await _call(p, _turn(0), system=f"{format_role_line(role)}\nWork.")
        assert resp.tool_calls[0].name == tool
    # A role the script has no step for gets the no-match error, not another role's answer.
    with pytest.raises(ProviderError, match="no scripted step matches.*role=compactor"):
        await _call(p, _turn(0), system=format_role_line(Role.COMPACTOR))


async def test_role_keyed_steps_need_a_role_line_in_the_prompt(tmp_path: Path) -> None:
    p = ScriptedProvider(_write(tmp_path, ROLE_SCRIPT))
    # The role line only counts on the first line.
    system = "You are a careful assistant.\nRole: coder"
    with pytest.raises(ProviderError) as exc:
        await _call(p, _turn(0), system=system)
    assert exc.value.kind is ProviderErrorKind.INVALID_REQUEST
    message = exc.value.message
    assert format_role_line(Role.CODER) in message
    assert "You are a careful assistant." in message
    assert "no scripted step matches" not in message


async def test_a_script_without_role_keys_does_not_need_a_role_line() -> None:
    p = ScriptedProvider(SCRIPT)
    resp = await _call(p, _turn(0), system="plain prompt")
    assert resp.tool_calls[0].name == "list_dir"


def test_bare_steps_are_rejected_next_to_role_keyed_steps(tmp_path: Path) -> None:
    body = """
steps:
  - match: {role: verifier}
    respond: {text: "verdict"}
  - respond: {text: "meant for the coder"}
"""
    with pytest.raises(ValueError, match=r"steps \[1\] have no match"):
        ScriptedProvider(_write(tmp_path, body))


def test_steps_with_a_match_may_leave_out_the_role(tmp_path: Path) -> None:
    body = """
steps:
  - match: {role: verifier}
    respond: {text: "verdict"}
  - match: {last_tool_result_contains: "FAILED"}
    respond: {text: "any role"}
"""
    ScriptedProvider(_write(tmp_path, body))


# ---------- the no-match message ----------
PYTEST_OUTPUT = (
    "exit code: 1\nsummary: 1 failed, 2 passed\nstdout:\n"
    + "============================= test session starts ==============================\n"
    + "platform linux -- Python 3.12.1, pytest-8.3.2, pluggy-1.5.0\n" * 8
    + "tests/test_calc.py F..                                                   [100%]\n"
    + "FAILED tests/test_calc.py::test_add_positive_numbers - assert -1 == 5\n"
    + "1 failed, 2 passed in 0.05s"
)


async def test_no_match_message_shows_the_end_of_a_long_pytest_output() -> None:
    assert len(PYTEST_OUTPUT) > 700  # longer than the 500 characters the message keeps
    p = ScriptedProvider(SCRIPT)
    with pytest.raises(ProviderError) as exc:
        await _call(p, _turn(9, PYTEST_OUTPUT))
    message = exc.value.message
    assert "FAILED tests/test_calc.py::test_add_positive_numbers" in message
    assert "1 failed, 2 passed in 0.05s" in message
    assert "test session starts" not in message


async def test_no_match_message_shows_a_short_result_in_full() -> None:
    p = ScriptedProvider(SCRIPT)
    with pytest.raises(ProviderError, match="last_tool_result='all good'"):
        await _call(p, _turn(9, "all good"))


async def test_no_match_message_shows_the_user_text(tmp_path: Path) -> None:
    p = ScriptedProvider(
        _write(tmp_path, "steps:\n  - match: {turn_index: 99}\n    respond: {text: hi}\n")
    )
    with pytest.raises(ProviderError, match="user_text='Hint from @sam: look at mod3'"):
        await _call(p, [*_turn(1), _user("Hint from @sam: look at mod3")])


# ---------- inputs are copies ----------
async def test_returned_inputs_do_not_share_objects_with_the_script(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "steps:\n"
        "  - respond:\n"
        "      tool_calls:\n"
        "        - {name: run_tests, input: {target: [tests/a.py], opts: {k: 1}}}\n",
    )
    p = ScriptedProvider(path)
    original = {"target": ["tests/a.py"], "opts": {"k": 1}}
    first = await _call(p, _turn(0), tools=[_tool("run_tests")])
    block = first.content[0]
    assert isinstance(block, ToolUseBlock)
    # A consumer that normalises arguments in place must not change the script.
    first.tool_calls[0].input["target"].append("tests/b.py")
    first.tool_calls[0].input["opts"]["k"] = 2
    assert block.input == original
    block.input["target"].append("tests/c.py")
    second = await _call(p, _turn(0), tools=[_tool("run_tests")])
    assert second.tool_calls[0].input == original
    assert second == await _call(p, _turn(0), tools=[_tool("run_tests")])


# ---------- script validation ----------
@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("respond: {text: hi, stop_reason: tool_use}", "stop_reason tool_use needs a tool call"),
        (
            "respond: {tool_calls: [{name: x}], stop_reason: end_turn}",
            "stop_reason end_turn cannot come with tool calls",
        ),
        ("respond: {raise: overloaded, text: hi}", "raise replaces the reply.*text"),
        (
            "respond: {raise: overloaded, tool_calls: [{name: x}]}",
            "raise replaces the reply.*tool_calls",
        ),
        (
            "respond: {raise: overloaded, stop_reason: end_turn}",
            "raise replaces the reply.*stop_reason",
        ),
        ("respond: {text: hi, retry_after_s: 2}", "retry_after_s only applies to a raise step"),
        ("respond: {text: hi, times: 2}", "times only applies to a raise step"),
        ("respond: {raise: overloaded, retry_after_s: -5}", "greater than or equal to 0"),
        ("respond: {raise: overloaded, retry_after_s: .nan}", "finite number"),
        ("respond: {raise_: overloaded}", "raise_"),
        ("respond: {raise: nonsense}", r"respond\.raise\n"),
        ("respond: {raise: overloaded, times: 0}", "greater than or equal to 1"),
        ("respond: {}", "respond is empty"),
        ("match: {turn_index: -1}\n    respond: {text: hi}", "greater than or equal to 0"),
        ("respond: {tool_calls: [{name: ''}]}", "at least 1 character"),
    ],
)
def test_impossible_responses_are_rejected(tmp_path: Path, body: str, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        ScriptedProvider(_write(tmp_path, f"steps:\n  - {body}\n"))


@pytest.mark.parametrize(
    "body",
    [
        "respond: {text: hi}",
        "respond: {text: hi, stop_reason: max_tokens}",
        "respond: {tool_calls: [{name: x}], stop_reason: max_tokens}",
        "respond: {tool_calls: [{name: x}], stop_reason: tool_use}",
        "respond: {raise: rate_limit, retry_after_s: 0}",
        "respond: {raise: timeout, times: 3}",
    ],
)
def test_ordinary_responses_still_load(tmp_path: Path, body: str) -> None:
    ScriptedProvider(_write(tmp_path, f"steps:\n  - {body}\n"))


def test_a_repeated_key_is_an_error_not_a_silent_override(tmp_path: Path) -> None:
    path = _write(tmp_path, "steps:\n  - respond: {text: first}\n    respond: {text: second}\n")
    with pytest.raises(ValueError, match="duplicate key 'respond'") as exc:
        ScriptedProvider(path)
    assert str(path) in str(exc.value)
    nested = _write(
        tmp_path, "steps:\n  - match: {turn_index: 1, turn_index: 2}\n    respond: {text: hi}\n"
    )
    with pytest.raises(ValueError, match="duplicate key 'turn_index'"):
        ScriptedProvider(nested)


@pytest.mark.parametrize("body", ["steps: [unclosed\n", "steps:\n  - ? [a, b]\n    : c\n"])
def test_yaml_syntax_errors_are_value_errors_that_name_the_file(tmp_path: Path, body: str) -> None:
    path = _write(tmp_path, body)
    with pytest.raises(ValueError) as exc:
        ScriptedProvider(path)
    assert str(path) in str(exc.value)
    assert type(exc.value) is ValueError


async def test_anchors_and_merge_keys_still_work(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
steps:
  - respond: &say {text: "again"}
  - match: {turn_index: 5}
    respond: *say
  - match: {turn_index: 6}
    respond:
      <<: *say
      stop_reason: max_tokens
""",
    )
    p = ScriptedProvider(path)
    assert (await _call(p, _turn(0))).content == [TextBlock(text="again")]
    assert (await _call(p, _turn(5))).content == [TextBlock(text="again")]
    assert (await _call(p, _turn(6))).stop_reason == "max_tokens"
