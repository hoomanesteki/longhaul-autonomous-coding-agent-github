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


async def _call(p: ScriptedProvider, messages: list[Message], system: str = SYSTEM) -> LLMResponse:
    return await p.complete(model="m", system=system, messages=messages, tools=[], max_tokens=100)


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
  - match: {role: verifier}
    respond: {text: "verifier"}
  - respond: {text: "one", stop_reason: max_tokens}
""",
    )
    p = ScriptedProvider(path)
    assert (await _call(p, _turn(0))).content[0] == TextBlock(text="zero")
    last = await _call(p, _turn(1))
    assert last.content[0] == TextBlock(text="one")
    assert last.stop_reason == "max_tokens"


async def test_usage_is_deterministic() -> None:
    p = ScriptedProvider(SCRIPT)
    resp = await _call(p, _turn(0))
    again = await _call(p, _turn(0))
    assert resp.usage == again.usage
    assert resp.usage.output_tokens == 50 + len("Looking at the repo layout.") // 4
    expected = await p.count_tokens(model="m", system=SYSTEM, messages=_turn(0), tools=[])
    assert resp.usage.input_tokens == expected
    assert expected == (len(SYSTEM) + len("go")) // 4


async def test_no_match_raises() -> None:
    p = ScriptedProvider(SCRIPT)
    with pytest.raises(ProviderError) as exc:
        await _call(p, _turn(9, "the last result " + "z" * 600))
    assert exc.value.kind is ProviderErrorKind.INVALID_REQUEST
    msg = exc.value.message
    assert "turn_index=9" in msg
    assert "Role: coder" in msg
    assert "the last result" in msg
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
