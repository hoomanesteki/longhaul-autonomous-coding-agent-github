"""The replay provider: record the calls of a run, then play them back (LH-209).

The recordings in these tests come from a ScriptedProvider, because CI has no key. A recording of
a real model has the same shape, so what is proven here holds for it.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from longhaul.providers.replay import ReplayProvider, canonical_request, request_hash
from tests.fakes.scripted_runs import SCRIPTS, play, without_waits

from longhaul.contracts import (
    LLMResponse,
    Message,
    OutcomeKind,
    ProviderError,
    ProviderErrorKind,
    TextBlock,
    ToolCall,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from longhaul.providers.base import get_provider
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.settings import EnvSettings

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

SYSTEM = "Role: coder\nFix the bug."
TOOLS = [
    ToolSpec(
        name="read_file",
        description="Read a file.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
]


def user(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


def assistant(text: str) -> Message:
    return Message(role="assistant", content=[TextBlock(text=text)])


def reply(text: str, *, latency_ms: int = 900) -> LLMResponse:
    return LLMResponse(
        content=[TextBlock(text=text)],
        tool_calls=[],
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=3),
        model="m",
        latency_ms=latency_ms,
        request_id="req_1",
    )


class Canned:
    """A provider that answers `text` for every call and counts them."""

    name = "canned"

    def __init__(self, text: str = "recorded") -> None:
        self.text = text
        self.calls = 0

    async def complete(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        return reply(f"{self.text} {self.calls}")

    async def count_tokens(self, **kwargs: Any) -> int:
        return 4242


async def ask(
    provider: Any,
    messages: list[Message],
    *,
    system: str = SYSTEM,
    max_tokens: int = 100,
    model: str = "m",
) -> LLMResponse:
    resp: LLMResponse = await provider.complete(
        model=model, system=system, messages=messages, tools=TOOLS, max_tokens=max_tokens
    )
    return resp


def recorded(folder: Path) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.json"))
    ]
    return loaded


# ---------- the request hash ----------
def test_the_hash_is_the_sha256_of_the_canonical_request() -> None:
    canonical = canonical_request("m", SYSTEM, [user("go")], TOOLS, 100)
    assert request_hash("m", SYSTEM, [user("go")], TOOLS, 100) == (
        hashlib.sha256(canonical.encode()).hexdigest()
    )


def test_the_canonical_request_is_json_with_sorted_keys() -> None:
    canonical = canonical_request("m", SYSTEM, [user("go")], TOOLS, 100)
    parsed = json.loads(canonical)
    assert list(parsed) == sorted(parsed) == ["max_tokens", "messages", "model", "system", "tools"]
    assert canonical == json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    assert parsed["messages"] == [{"role": "user", "content": [{"type": "text", "text": "go"}]}]


@pytest.mark.parametrize(
    "changed",
    [
        {"model": "other"},
        {"system": "Role: coder\nFix another bug."},
        {"messages": [user("stop")]},
        {"tools": []},
        {"max_tokens": 101},
    ],
    ids=["model", "system", "messages", "tools", "max_tokens"],
)
def test_each_part_of_the_request_changes_the_hash(changed: dict[str, Any]) -> None:
    base: dict[str, Any] = {
        "model": "m",
        "system": SYSTEM,
        "messages": [user("go")],
        "tools": TOOLS,
        "max_tokens": 100,
    }
    assert request_hash(**base) != request_hash(**{**base, **changed})


# ---------- recording ----------
async def test_recording_saves_one_pair_per_call_and_returns_the_reply_unchanged(
    tmp_path: Path,
) -> None:
    inner = Canned()
    provider = ReplayProvider(tmp_path / "rec", record_with=inner)

    first = await ask(provider, [user("go")])
    second = await ask(provider, [user("go"), assistant("a"), user("next")])

    assert first.content == [TextBlock(text="recorded 1")]
    assert second.content == [TextBlock(text="recorded 2")]
    pairs = recorded(tmp_path / "rec")
    assert [p["turn_index"] for p in pairs] == [0, 1]
    assert pairs[0]["request_sha256"] == request_hash("m", SYSTEM, [user("go")], TOOLS, 100)
    assert pairs[0]["request"] == json.loads(
        canonical_request("m", SYSTEM, [user("go")], TOOLS, 100)
    )
    assert LLMResponse.model_validate(pairs[0]["response"]) == first


async def test_a_recording_provider_goes_by_the_name_of_the_one_it_wraps(tmp_path: Path) -> None:
    assert ReplayProvider(tmp_path / "rec", record_with=Canned()).name == "canned"


async def test_recording_makes_the_folder_and_leaves_no_half_written_file(tmp_path: Path) -> None:
    provider = ReplayProvider(tmp_path / "a" / "b", record_with=Canned())
    await ask(provider, [user("go")])
    assert [p.suffix for p in (tmp_path / "a" / "b").iterdir()] == [".json"]


async def test_the_first_recording_of_a_request_is_kept(tmp_path: Path) -> None:
    # A call that Temporal ran twice sends the same request twice. Replaying must give the first
    # answer, or the requests after it would no longer match the recording.
    provider = ReplayProvider(tmp_path / "rec", record_with=Canned())
    await ask(provider, [user("go")])
    again = await ask(provider, [user("go")])

    assert again.content == [TextBlock(text="recorded 2")]  # the caller still gets the live reply
    (pair,) = recorded(tmp_path / "rec")
    assert pair["response"]["content"][0]["text"] == "recorded 1"


async def test_recording_goes_on_after_a_restart_without_overwriting(tmp_path: Path) -> None:
    folder = tmp_path / "rec"
    await ask(ReplayProvider(folder, record_with=Canned("first")), [user("go")])
    await ask(ReplayProvider(folder, record_with=Canned("second")), [user("go"), assistant("a")])

    assert [len(list(folder.glob("*.json")))] == [2]
    names = sorted(p.name for p in folder.glob("*.json"))
    assert names[0] < names[1]  # the order of the calls is the order of the names


async def test_counting_tokens_is_left_to_the_provider_that_is_wrapped(tmp_path: Path) -> None:
    provider = ReplayProvider(tmp_path / "rec", record_with=Canned())
    count = await provider.count_tokens(
        model="m", system=SYSTEM, messages=[user("go")], tools=TOOLS
    )
    assert count == 4242


# ---------- replaying ----------
async def test_a_recorded_call_is_played_back(tmp_path: Path) -> None:
    folder = tmp_path / "rec"
    live = await ask(ReplayProvider(folder, record_with=Canned()), [user("go")])

    played = await ask(ReplayProvider(folder), [user("go")])

    assert played.content == live.content
    assert played.usage == live.usage
    assert played.request_id == "req_1"
    # The time of the original call is not the time of this one.
    assert played.latency_ms == 0


async def test_a_matching_request_is_played_without_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = tmp_path / "rec"
    await ask(ReplayProvider(folder, record_with=Canned()), [user("go")])
    with caplog.at_level(logging.WARNING):
        await ask(ReplayProvider(folder), [user("go")])
    assert caplog.records == []


async def test_a_request_that_differs_falls_back_to_the_turn_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Risk R6: the prompt changed after the recording was made, or a tool printed a time.
    folder = tmp_path / "rec"
    await ask(ReplayProvider(folder, record_with=Canned()), [user("go")])

    with caplog.at_level(logging.WARNING):
        played = await ask(ReplayProvider(folder), [user("go, but worded differently")])

    assert played.content == [TextBlock(text="recorded 1")]
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "turn 0" in warning.getMessage()
    assert request_hash("m", SYSTEM, [user("go, but worded differently")], TOOLS, 100)[:12] in (
        warning.getMessage()
    )


async def test_the_turn_is_counted_in_assistant_messages(tmp_path: Path) -> None:
    folder = tmp_path / "rec"
    recorder = ReplayProvider(folder, record_with=Canned())
    await ask(recorder, [user("go")])
    await ask(recorder, [user("go"), assistant("a"), user("next")])
    third = [user("go"), assistant("a"), user("next"), assistant("b"), user("more")]
    await ask(recorder, third)

    played = await ask(ReplayProvider(folder), third)

    assert played.content == [TextBlock(text="recorded 3")]
    assert [p["turn_index"] for p in recorded(folder)] == [0, 1, 2]


async def test_a_turn_with_no_recording_is_an_error_that_is_not_retried(tmp_path: Path) -> None:
    folder = tmp_path / "rec"
    await ask(ReplayProvider(folder, record_with=Canned()), [user("go")])
    later = [user("go"), assistant("a"), user("next")]

    with pytest.raises(ProviderError, match="turn 1") as raised:
        await ask(ReplayProvider(folder), later)

    assert raised.value.kind is ProviderErrorKind.INVALID_REQUEST
    assert not raised.value.retryable


async def test_a_replay_gives_the_same_answer_every_time(tmp_path: Path) -> None:
    folder = tmp_path / "rec"
    await ask(ReplayProvider(folder, record_with=Canned()), [user("go")])
    provider = ReplayProvider(folder)

    assert await ask(provider, [user("go")]) == await ask(provider, [user("go")])


async def test_counting_tokens_in_a_replay_is_a_fixed_estimate(tmp_path: Path) -> None:
    folder = tmp_path / "rec"
    await ask(ReplayProvider(folder, record_with=Canned()), [user("go")])
    provider = ReplayProvider(folder)

    short = await provider.count_tokens(
        model="m", system=SYSTEM, messages=[user("go")], tools=TOOLS
    )
    long = await provider.count_tokens(
        model="m", system=SYSTEM, messages=[user("go" * 500)], tools=TOOLS
    )
    assert 0 < short < long
    assert short == await provider.count_tokens(
        model="m", system=SYSTEM, messages=[user("go")], tools=TOOLS
    )


def test_a_replay_provider_is_called_replay(tmp_path: Path) -> None:
    folder = tmp_path / "rec"
    folder.mkdir()
    (folder / "0001-turn000-aaaaaaaaaaaa.json").write_text(
        json.dumps(
            {
                "turn_index": 0,
                "request_sha256": "a" * 64,
                "request": {},
                "response": reply("x").model_dump(mode="json"),
            }
        )
    )
    assert ReplayProvider(folder).name == "replay"


# ---------- bad folders ----------
def test_a_missing_folder_is_an_error_that_names_it(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nowhere"):
        ReplayProvider(tmp_path / "nowhere")


def test_a_folder_with_no_recordings_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no recordings"):
        ReplayProvider(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        '{"turn_index": 0}',
        '{"turn_index": "x", "request_sha256": "a", "response": {}}',
    ],
    ids=["not-json", "not-an-object", "missing-fields", "wrong-type"],
)
def test_a_damaged_recording_is_an_error_that_names_the_file(tmp_path: Path, body: str) -> None:
    (tmp_path / "0001-turn000-bad.json").write_text(body)
    with pytest.raises(ValueError, match="0001-turn000-bad.json"):
        ReplayProvider(tmp_path)


# ---------- the provider factory ----------
def settings(**fields: Any) -> EnvSettings:
    return EnvSettings(_env_file=None, **fields)  # type: ignore[call-arg]


def test_get_provider_builds_a_replay_provider_from_the_recordings_folder(tmp_path: Path) -> None:
    folder = tmp_path / "rec"
    folder.mkdir()
    (folder / "0001-turn000-aaaaaaaaaaaa.json").write_text(
        json.dumps(
            {
                "turn_index": 0,
                "request_sha256": "a" * 64,
                "request": {},
                "response": reply("x").model_dump(mode="json"),
            }
        )
    )
    provider = get_provider(settings(provider="replay", recordings_dir=folder))
    assert isinstance(provider, ReplayProvider)
    assert provider.name == "replay"


def test_get_provider_for_replay_needs_the_recordings_folder() -> None:
    with pytest.raises(ValueError, match="LONGHAUL_RECORDINGS_DIR"):
        get_provider(settings(provider="replay"))


async def test_get_provider_records_the_provider_it_builds_when_record_is_set(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "rec"
    script = SCRIPTS / "calc_fix.yaml"
    provider = get_provider(
        settings(provider="scripted", script=script, record=True, recordings_dir=folder)
    )
    assert isinstance(provider, ReplayProvider)
    assert provider.name == "scripted"

    resp = await provider.complete(
        model="m",
        system=SYSTEM,
        messages=[user("go")],
        tools=[
            ToolSpec(name="list_dir", description="List.", input_schema={"type": "object"}),
        ],
        max_tokens=100,
    )

    assert resp.tool_calls[0].name == "list_dir"
    (pair,) = recorded(folder)
    assert pair["turn_index"] == 0


def test_recording_needs_the_recordings_folder(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="LONGHAUL_RECORDINGS_DIR"):
        get_provider(settings(provider="scripted", script=SCRIPTS / "calc_fix.yaml", record=True))


def test_a_replay_cannot_be_recorded(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="LONGHAUL_RECORD"):
        get_provider(settings(provider="replay", record=True, recordings_dir=tmp_path))


# ---------- record a scripted run, replay it ----------
async def test_a_scripted_run_that_was_recorded_replays_to_the_same_run(tmp_path: Path) -> None:
    script = without_waits(SCRIPTS / "inventory_fix.yaml", tmp_path)
    folder = tmp_path / "recordings"
    first_box, second_box = FakeSandbox("first"), FakeSandbox("second")
    try:
        live = await play(
            "inventory", ReplayProvider(folder, record_with=ScriptedProvider(script)), first_box
        )
        replayed = await play("inventory", ReplayProvider(folder), second_box)
        files_live = {
            name: await first_box.read_file(name)
            for name in ("src/inventory/store.py", "src/inventory/errors.py", "CHANGES.txt")
        }
        files_replayed = {name: await second_box.read_file(name) for name in files_live}
    finally:
        await first_box.destroy()
        await second_box.destroy()

    assert live.outcome.kind is OutcomeKind.PR_OPENED, live.outcome.summary
    assert replayed.outcome == live.outcome
    assert replayed.turns == live.turns == len(recorded(folder))
    assert replayed.transcript == live.transcript
    assert files_replayed == files_live


async def test_a_replay_does_not_need_the_script_or_the_provider_that_made_it(
    tmp_path: Path,
) -> None:
    script = without_waits(SCRIPTS / "inventory_fix.yaml", tmp_path)
    folder = tmp_path / "recordings"
    box = FakeSandbox("record")
    try:
        await play("inventory", ReplayProvider(folder, record_with=ScriptedProvider(script)), box)
    finally:
        await box.destroy()
    script.unlink()

    again = FakeSandbox("again")
    try:
        result = await play("inventory", ReplayProvider(folder), again)
    finally:
        await again.destroy()

    assert result.outcome.kind is OutcomeKind.PR_OPENED


async def test_tool_calls_come_back_with_their_recorded_ids(tmp_path: Path) -> None:
    folder = tmp_path / "rec"
    inner = ScriptedProvider(SCRIPTS / "calc_fix.yaml")
    tools = [ToolSpec(name="list_dir", description="List.", input_schema={"type": "object"})]
    live = await ReplayProvider(folder, record_with=inner).complete(
        model="m", system=SYSTEM, messages=[user("go")], tools=tools, max_tokens=100
    )

    played = await ReplayProvider(folder).complete(
        model="m", system=SYSTEM, messages=[user("go")], tools=tools, max_tokens=100
    )

    assert played.tool_calls == live.tool_calls != []
    assert isinstance(played.content[-1], ToolUseBlock)
    assert played.tool_calls[0] == ToolCall(
        id=played.content[-1].id, name="list_dir", input={"path": "."}
    )
