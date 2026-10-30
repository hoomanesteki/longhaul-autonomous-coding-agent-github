from pathlib import Path

import pytest

from longhaul.contracts import (
    LLMResponse,
    Message,
    ProviderError,
    ProviderErrorKind,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from longhaul.providers.base import get_provider
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


async def test_sequence() -> None:
    p = ScriptedProvider(SCRIPT)
    assert p.name == "scripted"
    names = []
    for i in range(5):
        resp = await _call(p, _turn(i))
        names.append(resp.tool_calls[0].name)
        assert resp.tool_calls[0].id == f"toolu_{i:03d}_0"
        assert resp.stop_reason == "tool_use"
        assert resp.latency_ms == 0
    assert names == ["list_dir", "read_file", "edit_file", "run_tests", "finish"]


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
