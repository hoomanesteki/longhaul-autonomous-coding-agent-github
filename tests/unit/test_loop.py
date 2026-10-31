"""The in-process loop: finish, the turn limit, nudges, and the stop reasons of D8 and D9.

The model is a ScriptedProvider. Edge cases are small scripts in tests/fixtures/scripts/ named
loop_*.yaml, each with a comment that says what it plays. calc_fix.yaml plays the happy path
against the calc fixture in a FakeSandbox.
"""

import shlex
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from longhaul.agent.loop import (
    MAX_PAUSES,
    NUDGE,
    LoopConfig,
    LoopResult,
    TurnEvent,
    run_in_process,
    run_loop,
)
from longhaul.agent.prompts import FIRST_USER_MESSAGE, build_system_prompt
from longhaul.agent.turns import validate_pairs
from longhaul.contracts import (
    Budgets,
    CallOptions,
    ExecResult,
    LLMResponse,
    Message,
    Outcome,
    OutcomeKind,
    ProviderError,
    ProviderErrorKind,
    RepoSettings,
    Role,
    StopReason,
    TaskSpec,
    TextBlock,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.settings import load_config
from longhaul.tools.control import Finish
from longhaul.tools.files import ListDir
from longhaul.tools.registry import ToolRegistry, default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
SCRIPTS = FIXTURES / "scripts"
REPOS = FIXTURES / "repos"
EXAMPLE_CONFIG = ROOT / "config" / "longhaul.example.yaml"

# The interpreter running this test, so it does not matter whether pytest is on PATH.
TEST_COMMAND = f"{shlex.quote(sys.executable)} -m pytest -q"
SKIP_PARTS = {"REFERENCE.patch", "__pycache__", ".pytest_cache"}

CFG = LoopConfig(
    model="test-model",
    max_turns=10,
    max_tokens_per_turn=1000,
    max_tokens_retry=5000,
    tool_output_max_tokens=4000,
)
FINISH_SUMMARY = "Changed add() to return a + b. All tests pass."


# ---------- helpers ----------
def make_spec() -> TaskSpec:
    return TaskSpec(
        task_id="lh-local-calc-1",
        repo="local:/calc",
        issue_number=None,
        title="add() returns the difference",
        body="`calc.add` subtracts its arguments instead of adding them.",
        settings=RepoSettings(repo="local:/calc", test_command=TEST_COMMAND),
    )


class SpySandbox(FakeSandbox):
    """A FakeSandbox that remembers every command it was asked to run."""

    def __init__(self) -> None:
        super().__init__()
        self.commands: list[str] = []

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        self.commands.append(cmd)
        return await super().exec(cmd, timeout_s=timeout_s, env=env)


class Recorder(ScriptedProvider):
    """A ScriptedProvider that also keeps what each call was given.

    The scripted provider records the options of each call, and nothing else about it. It
    also stops a loop that would never end: a test about a limit should fail when the limit
    is broken, not hang.
    """

    MAX_CALLS = 50

    def __init__(self, script: Path) -> None:
        super().__init__(script)
        self.caps: list[int] = []
        self.systems: list[str] = []
        self.requests: list[list[Message]] = []

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
        assert len(self.requests) < self.MAX_CALLS, "the loop is not ending"
        self.caps.append(max_tokens)
        self.systems.append(system)
        self.requests.append(list(messages))
        return await super().complete(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            cache_system=cache_system,
            options=options,
        )


class CapRouter:
    """Answers from one script below a max_tokens value and from another at or above it.

    A scripted reply depends on the request alone, and a retry of a cut turn is the same
    request with a higher cap. Without this a script could not play "cut, then fine".
    """

    name = "cap-router"

    def __init__(self, low: Recorder, high: Recorder, cap: int) -> None:
        self.low, self.high, self.cap = low, high, cap
        self.caps: list[int] = []

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
        self.caps.append(max_tokens)
        target = self.high if max_tokens >= self.cap else self.low
        return await target.complete(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            cache_system=cache_system,
            options=options,
        )

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


class Canned:
    """Answers with the replies it was given, in order.

    For replies a script cannot play: the scripted provider refuses a tool call on an
    end_turn reply, as no model sends one.
    """

    name = "canned"

    def __init__(self, *replies: LLMResponse) -> None:
        self.replies = list(replies)
        self.requests: list[list[Message]] = []

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
        assert self.replies, "the loop asked for more replies than the test gave"
        self.requests.append(list(messages))
        return self.replies.pop(0)

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


def llm_reply(stop_reason: StopReason, *blocks: Any) -> LLMResponse:
    calls = [
        ToolCall(id=b.id, name=b.name, input=b.input) for b in blocks if isinstance(b, ToolUseBlock)
    ]
    return LLMResponse(
        content=list(blocks),
        tool_calls=calls,
        stop_reason=stop_reason,
        usage=Usage(),
        model="m",
        latency_ms=0,
    )


class SpyListDir(ListDir):
    """list_dir that keeps the context of every call."""

    def __init__(self) -> None:
        self.contexts: list[ToolContext] = []

    async def run(self, sandbox: Any, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.contexts.append(ctx)
        return await super().run(sandbox, args, ctx)


@pytest.fixture
async def sbx() -> AsyncIterator[FakeSandbox]:
    box = FakeSandbox()
    yield box
    await box.destroy()


@pytest.fixture
async def spy_sbx() -> AsyncIterator[SpySandbox]:
    box = SpySandbox()
    yield box
    await box.destroy()


async def seed(box: FakeSandbox, name: str) -> None:
    source = REPOS / name
    for f in sorted(source.rglob("*")):
        parts = set(f.relative_to(source).parts)
        if (
            f.is_file()
            and not parts & SKIP_PARTS
            and not any(p.endswith(".egg-info") for p in parts)
        ):
            await box.write_file(str(f.relative_to(source)), f.read_bytes())


async def drive(
    script: str,
    box: FakeSandbox,
    *,
    cfg: LoopConfig = CFG,
    registry: ToolRegistry | None = None,
    on_event: Callable[[TurnEvent], None] | None = None,
) -> tuple[LoopResult, Recorder]:
    provider = Recorder(SCRIPTS / script)
    tools = registry if registry is not None else default_registry(test_command=TEST_COMMAND)
    return await run_loop(make_spec(), provider, tools, box, cfg, on_event), provider


def error(summary: str) -> Outcome:
    return Outcome(kind=OutcomeKind.ERROR, summary=summary)


def finished(summary: str) -> Outcome:
    return Outcome(kind=OutcomeKind.PR_OPENED, summary=summary)


def blocks_of(message: Message) -> list[Any]:
    return list(message.content)


def tool_results(message: Message) -> list[ToolResultBlock]:
    return [b for b in message.content if isinstance(b, ToolResultBlock)]


def assistant_messages(transcript: list[Message]) -> list[Message]:
    return [m for m in transcript if m.role == "assistant"]


# ---------- the happy path ----------
async def test_scripted_calc_fix(sbx: FakeSandbox) -> None:
    await seed(sbx, "calc")
    provider = ScriptedProvider(SCRIPTS / "calc_fix.yaml")
    registry = default_registry(test_command=TEST_COMMAND)

    outcome = await run_in_process(make_spec(), provider, registry, sbx, CFG)

    # No pull request exists in-process, so the outcome is the pr_opened kind with no url.
    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, pr_url=None, summary=FINISH_SUMMARY)
    # The change is in the workspace, and the suite passes there.
    assert b"return a + b" in await sbx.read_file("src/calc/__init__.py")
    done = await sbx.exec(TEST_COMMAND, timeout_s=60)
    assert done.exit_code == 0, done.stdout
    assert "3 passed" in done.stdout


async def test_pairs_valid(sbx: FakeSandbox) -> None:
    await seed(sbx, "calc")
    result, _ = await drive("calc_fix.yaml", sbx)
    assert result.outcome.kind is OutcomeKind.PR_OPENED
    transcript = result.transcript

    validate_pairs(transcript)
    # Five calls and five results with the same ids in the same order, finish included. The
    # loop adds the last result before it returns, so the final transcript is complete too.
    use_ids = [b.id for m in transcript for b in m.content if isinstance(b, ToolUseBlock)]
    result_ids = [b.tool_use_id for m in transcript for b in tool_results(m)]
    assert len(use_ids) == 5 and len(set(use_ids)) == 5
    assert result_ids == use_ids
    assert transcript[0].role == "user" and transcript[-1].role == "user"
    assert [m.role for m in transcript] == ["user", *["assistant", "user"] * 5]


async def test_a_run_is_repeatable(sbx: FakeSandbox) -> None:
    # Tool ids come from a hash of the request, so a loop that adds anything of its own
    # (a clock, a counter that restarts) shows up here as two different transcripts.
    first, _ = await drive("loop_nudge.yaml", sbx)
    second, _ = await drive("loop_nudge.yaml", sbx)
    assert first.transcript == second.transcript
    assert first.outcome == second.outcome


async def test_the_system_prompt_is_built_once_and_the_first_message_starts_the_work(
    sbx: FakeSandbox,
) -> None:
    result, provider = await drive("loop_nudge.yaml", sbx)
    assert len(provider.systems) == 2
    # D7: one string for the life of the transcript.
    assert set(provider.systems) == {build_system_prompt(Role.CODER, make_spec())}
    assert provider.requests[0] == [
        Message(role="user", content=[TextBlock(text=FIRST_USER_MESSAGE)])
    ]
    assert result.outcome == finished("Nothing needed changing.")


async def test_effort_goes_out_with_every_call(sbx: FakeSandbox) -> None:
    _, provider = await drive("loop_nudge.yaml", sbx, cfg=CFG.model_copy(update={"effort": "high"}))
    assert provider.options_seen == [CallOptions(effort="high")] * 2
    _, plain = await drive("loop_nudge.yaml", sbx)
    assert plain.options_seen == [None] * 2


async def test_the_model_and_the_caps_come_from_the_config(sbx: FakeSandbox) -> None:
    seen: list[str] = []

    class Spy(Recorder):
        async def complete(self, **kwargs: Any) -> LLMResponse:
            seen.append(kwargs["model"])
            return await super().complete(**kwargs)

    provider = Spy(SCRIPTS / "loop_nudge.yaml")
    await run_loop(make_spec(), provider, default_registry(), sbx, CFG)
    assert set(seen) == {"test-model"}
    assert provider.caps == [CFG.max_tokens_per_turn] * 2


async def test_tool_context_follows_the_turn(sbx: FakeSandbox) -> None:
    spy = SpyListDir()
    registry = ToolRegistry()
    registry.register(spy)
    registry.register(Finish())
    cfg = CFG.model_copy(update={"max_turns": 3, "tool_output_max_tokens": 1234})
    await drive("loop_never_finishes.yaml", sbx, cfg=cfg, registry=registry)
    assert [c.turn_index for c in spy.contexts] == [0, 1, 2]
    assert {c.task_id for c in spy.contexts} == {"lh-local-calc-1"}
    assert {c.blob_prefix for c in spy.contexts} == {"tasks/lh-local-calc-1"}
    assert {c.output_max_tokens for c in spy.contexts} == {1234}


# ---------- the turn limit ----------
async def test_turn_limit(sbx: FakeSandbox) -> None:
    cfg = CFG.model_copy(update={"max_turns": 3})
    result, provider = await drive("loop_never_finishes.yaml", sbx, cfg=cfg)
    assert result.outcome == error("turn limit")
    assert result.turns == 3
    assert len(provider.requests) == 3
    # The last turn's tools ran and were answered, so the transcript is complete.
    validate_pairs(result.transcript)
    assert len(assistant_messages(result.transcript)) == 3
    assert result.transcript[-1].role == "user"


async def test_finish_on_the_last_allowed_turn_still_counts(sbx: FakeSandbox) -> None:
    await seed(sbx, "calc")
    # calc_fix.yaml finishes on its fifth turn.
    exact, _ = await drive("calc_fix.yaml", sbx, cfg=CFG.model_copy(update={"max_turns": 5}))
    assert exact.outcome == finished(FINISH_SUMMARY)
    assert exact.turns == 5

    await sbx.write_file("src/calc/__init__.py", (REPOS / "calc/src/calc/__init__.py").read_bytes())
    short, _ = await drive("calc_fix.yaml", sbx, cfg=CFG.model_copy(update={"max_turns": 4}))
    assert short.outcome == error("turn limit")
    validate_pairs(short.transcript)


# ---------- events ----------
async def test_events_describe_each_turn(sbx: FakeSandbox) -> None:
    await seed(sbx, "calc")
    events: list[TurnEvent] = []
    await drive("calc_fix.yaml", sbx, on_event=events.append)

    assert [e.turn_index for e in events] == [0, 1, 2, 3, 4]
    assert {e.role for e in events} == {Role.CODER}
    names = [[c.name for c in e.calls] for e in events]
    assert names == [["list_dir"], ["read_file"], ["edit_file"], ["run_tests"], ["finish"]]
    summaries = [[c.args for c in e.calls] for e in events]
    assert summaries == [
        ["."],
        ["src/calc/__init__.py"],
        ["src/calc/__init__.py"],
        [""],
        [FINISH_SUMMARY],
    ]
    assert all(c.ok for e in events for c in e.calls)


async def test_long_arguments_are_cut_in_the_summary(sbx: FakeSandbox) -> None:
    events: list[TurnEvent] = []
    await drive("loop_long_command.yaml", sbx, on_event=events.append)
    shown = events[0].calls[0].args
    assert shown.startswith("echo aaaa")
    assert shown.endswith("...")
    assert len(shown) <= 60


async def test_a_failed_call_shows_as_not_ok_in_the_event(sbx: FakeSandbox) -> None:
    events: list[TurnEvent] = []
    await drive("loop_tool_error.yaml", sbx, on_event=events.append)
    assert [c.ok for c in events[0].calls] == [False]
    assert [c.ok for c in events[1].calls] == [True]


async def test_no_callback_is_fine(sbx: FakeSandbox) -> None:
    result, _ = await drive("loop_nudge.yaml", sbx)
    assert result.outcome.kind is OutcomeKind.PR_OPENED


async def test_run_in_process_passes_the_callback_on(sbx: FakeSandbox) -> None:
    events: list[TurnEvent] = []
    provider = ScriptedProvider(SCRIPTS / "loop_nudge.yaml")
    outcome = await run_in_process(
        make_spec(), provider, default_registry(), sbx, CFG, on_event=events.append
    )
    assert outcome == finished("Nothing needed changing.")
    assert [e.turn_index for e in events] == [0, 1]


# ---------- tool results ----------
async def test_a_failed_call_goes_back_to_the_model_and_the_loop_goes_on(
    sbx: FakeSandbox,
) -> None:
    result, _ = await drive("loop_tool_error.yaml", sbx)
    assert result.outcome == finished("Recovered after a bad call.")
    [first] = tool_results(result.transcript[2])
    assert first.is_error
    assert "invalid input for read_file" in first.content
    validate_pairs(result.transcript)


async def test_a_finish_without_a_usable_summary_does_not_end_the_task(
    sbx: FakeSandbox,
) -> None:
    result, _ = await drive("loop_bad_finish.yaml", sbx)
    assert result.outcome == finished("Finished on the third try.")
    assert result.turns == 3
    answers = [tool_results(m)[0] for m in result.transcript if tool_results(m)]
    assert [a.is_error for a in answers] == [True, True, False]
    assert all("needs a summary" in a.content for a in answers[:2])
    validate_pairs(result.transcript)


async def test_calls_after_finish_are_not_run(spy_sbx: SpySandbox) -> None:
    result, _ = await drive("loop_finish_midway.yaml", spy_sbx)
    assert result.outcome == finished("Nothing needed changing.")
    # list_dir came first and ran. run_tests came after finish and did not.
    assert any("find" in c for c in spy_sbx.commands)
    assert not any("pytest" in c for c in spy_sbx.commands)

    listing, ended, skipped = tool_results(result.transcript[-1])
    assert not listing.is_error and not ended.is_error
    assert skipped.is_error
    assert skipped.content.startswith("not run: ")
    assert "finish" in skipped.content
    validate_pairs(result.transcript)


# ---------- nudges and empty replies ----------
async def test_nudge_then_finish(sbx: FakeSandbox) -> None:
    result, provider = await drive("loop_nudge.yaml", sbx)
    assert result.outcome == finished("Nothing needed changing.")
    kickoff, idle, nudge, call, answer = result.transcript
    assert idle == Message(role="assistant", content=[TextBlock(text="The code looks fine to me.")])
    # No tool call came before it, so the nudge is a new user message.
    assert nudge == Message(role="user", content=[TextBlock(text=NUDGE)])
    assert NUDGE == "Use tools or call finish."
    assert call.role == "assistant" and answer.role == "user"
    assert len(provider.requests) == 2
    validate_pairs(result.transcript)


async def test_two_turns_in_a_row_without_a_tool_call_end_the_run(sbx: FakeSandbox) -> None:
    result, provider = await drive("loop_always_idle.yaml", sbx)
    assert result.outcome == error("no tool calls")
    # One nudge, not two: the second idle turn is the last.
    assert len(provider.requests) == 2
    nudges = [
        b
        for m in result.transcript
        for b in m.content
        if isinstance(b, TextBlock) and b.text == NUDGE
    ]
    assert len(nudges) == 1
    assert result.turns == 2


async def test_idle_turns_that_are_not_in_a_row_do_not_end_the_run(sbx: FakeSandbox) -> None:
    result, provider = await drive("loop_idle_twice_apart.yaml", sbx)
    assert result.outcome == finished("Finished after two separate nudges.")
    assert len(provider.requests) == 4
    texts = [b.text for m in result.transcript for b in m.content if isinstance(b, TextBlock)]
    assert texts.count(NUDGE) == 2
    validate_pairs(result.transcript)


async def test_idle_turns_say_so_in_their_events(sbx: FakeSandbox) -> None:
    events: list[TurnEvent] = []
    await drive("loop_always_idle.yaml", sbx, on_event=events.append)
    assert [e.calls for e in events] == [(), ()]
    assert events[0].note is not None and "nudged" in events[0].note
    assert events[1].note is not None and "stopping" in events[1].note


async def test_an_empty_first_reply_is_not_stored(sbx: FakeSandbox) -> None:
    result, provider = await drive("loop_empty_first.yaml", sbx)
    assert result.outcome == finished("Finished after an empty reply.")
    # The empty reply left no assistant message, so the nudge joined the first user message.
    first = result.transcript[0]
    assert blocks_of(first) == [TextBlock(text=FIRST_USER_MESSAGE), TextBlock(text=NUDGE)]
    assert len(assistant_messages(result.transcript)) == 1
    for message in result.transcript:
        assert message.content, "no message may be empty"
    assert len(provider.requests) == 2
    validate_pairs(result.transcript)


async def test_an_empty_reply_after_tool_results_gets_the_nudge_after_the_results(
    sbx: FakeSandbox,
) -> None:
    result, _ = await drive("loop_empty_after_tools.yaml", sbx)
    assert result.outcome == finished("Finished after an empty reply.")
    # user, assistant (list_dir), user (result + nudge), assistant (finish), user (result)
    assert [m.role for m in result.transcript] == ["user", "assistant", "user", "assistant", "user"]
    results_and_nudge = result.transcript[2].content
    assert isinstance(results_and_nudge[0], ToolResultBlock)
    assert results_and_nudge[1] == TextBlock(text=NUDGE)
    assert len(results_and_nudge) == 2
    validate_pairs(result.transcript)


async def test_a_tool_call_on_an_end_turn_reply_is_neither_run_nor_stored(
    sbx: FakeSandbox,
) -> None:
    # The stop reason is read before the content (D9). No provider should send this, but a
    # mapping mistake in one would otherwise run a call the model never finished asking for.
    stray = ToolUseBlock(
        id="toolu_a", name="write_file", input={"path": "stray.py", "content": "x"}
    )
    done = ToolUseBlock(id="toolu_b", name="finish", input={"summary": "Done."})
    provider = Canned(
        llm_reply("end_turn", TextBlock(text="All done."), stray),
        llm_reply("tool_use", done),
    )

    result = await run_loop(make_spec(), provider, default_registry(), sbx, CFG)

    assert result.outcome == finished("Done.")
    with pytest.raises(FileNotFoundError):
        await sbx.read_file("stray.py")
    idle, nudge = result.transcript[1], result.transcript[2]
    assert idle == Message(role="assistant", content=[TextBlock(text="All done.")])
    assert nudge == Message(role="user", content=[TextBlock(text=NUDGE)])
    validate_pairs(result.transcript)


# ---------- D8: max_tokens ----------
async def test_a_cut_turn_is_retried_once_at_the_higher_cap(sbx: FakeSandbox) -> None:
    cut = Recorder(SCRIPTS / "loop_max_tokens_cut.yaml")
    retry = Recorder(SCRIPTS / "loop_max_tokens_retry.yaml")
    router = CapRouter(cut, retry, cap=CFG.max_tokens_retry)
    events: list[TurnEvent] = []

    result = await run_loop(
        make_spec(), router, default_registry(), sbx, CFG, on_event=events.append
    )

    assert result.outcome == finished("Finished on the retry.")
    assert router.caps == [CFG.max_tokens_per_turn, CFG.max_tokens_retry]
    # The cut turn was neither stored nor run. Only the retried turn is in the transcript.
    assert [m.role for m in result.transcript] == ["user", "assistant", "user"]
    stored = [b.text for m in result.transcript for b in m.content if isinstance(b, TextBlock)]
    assert "Writing the whole module in one go." not in stored
    with pytest.raises(FileNotFoundError):
        await sbx.read_file("big.py")
    # It is one turn, and its event says it was retried.
    assert result.turns == 1 and len(events) == 1
    assert events[0].note is not None and str(CFG.max_tokens_retry) in events[0].note
    validate_pairs(result.transcript)


async def test_a_second_max_tokens_stop_ends_the_run_with_output_limit(sbx: FakeSandbox) -> None:
    result, provider = await drive("loop_max_tokens_cut.yaml", sbx)
    assert result.outcome == error("output limit")
    assert provider.caps == [CFG.max_tokens_per_turn, CFG.max_tokens_retry]
    # Nothing from the cut turns was stored or run.
    assert result.transcript == [Message(role="user", content=[TextBlock(text=FIRST_USER_MESSAGE)])]
    with pytest.raises(FileNotFoundError):
        await sbx.read_file("big.py")


# ---------- D9: refusal, context, pause ----------
@pytest.mark.parametrize(
    ("script", "summary"),
    [
        (
            "loop_refusal.yaml",
            "model declined (cyber): The request looks like malware development.",
        ),
        ("loop_refusal_category_only.yaml", "model declined (bio): no explanation given"),
        ("loop_refusal_bare.yaml", "model declined (unknown): no explanation given"),
    ],
)
async def test_a_refusal_ends_the_run_and_runs_nothing(
    script: str, summary: str, sbx: FakeSandbox
) -> None:
    result, provider = await drive(script, sbx)
    assert result.outcome == error(summary)
    # No nudge and no second request, whatever the reply held.
    assert len(provider.requests) == 1
    assert result.transcript == [Message(role="user", content=[TextBlock(text=FIRST_USER_MESSAGE)])]
    with pytest.raises(FileNotFoundError):
        await sbx.read_file("refused.py")


async def test_a_full_context_window_ends_the_run(sbx: FakeSandbox) -> None:
    result, provider = await drive("loop_context_window.yaml", sbx)
    assert result.outcome == error("context limit")
    assert len(provider.requests) == 1
    assert len(assistant_messages(result.transcript)) == 0


async def test_a_prompt_over_the_window_ends_the_run_the_same_way(sbx: FakeSandbox) -> None:
    # Compaction would handle this from Phase 4 on. Until then it ends the task.
    result, _ = await drive("loop_context_too_long.yaml", sbx)
    assert result.outcome == error("context limit")


async def test_other_provider_errors_are_not_swallowed(sbx: FakeSandbox) -> None:
    # The in-process loop has no retry policy. Temporal owns retries from Phase 2.
    with pytest.raises(ProviderError) as caught:
        await drive("loop_overloaded.yaml", sbx)
    assert caught.value.kind is ProviderErrorKind.OVERLOADED
    assert caught.value.retry_after_s == 2.0


async def test_pause_turn_continues_the_same_turn(sbx: FakeSandbox) -> None:
    events: list[TurnEvent] = []
    result, provider = await drive("loop_pause_turn.yaml", sbx, on_event=events.append)

    assert result.outcome == finished("Finished after one pause.")
    # The continuation request ends with the text that came before the pause.
    first, second = provider.requests
    assert first == [Message(role="user", content=[TextBlock(text=FIRST_USER_MESSAGE)])]
    assert second[-1] == Message(
        role="assistant", content=[TextBlock(text="Checking the repo first.")]
    )
    # The tool call that came with the pause is not run and not stored.
    with pytest.raises(FileNotFoundError):
        await sbx.read_file("paused.py")
    # One turn, one assistant message: the paused text and the continuation together.
    assert result.turns == 1 and len(events) == 1
    [stored] = assistant_messages(result.transcript)
    assert [type(b).__name__ for b in stored.content] == ["TextBlock", "TextBlock", "ToolUseBlock"]
    assert [b.name for b in stored.content if isinstance(b, ToolUseBlock)] == ["finish"]
    assert events[0].note is not None and "pause_turn" in events[0].note
    validate_pairs(result.transcript)


async def test_pause_turn_cannot_go_on_for_ever(sbx: FakeSandbox) -> None:
    result, provider = await drive("loop_pause_forever.yaml", sbx)
    assert result.outcome == error("pause limit")
    assert len(provider.requests) == MAX_PAUSES + 1
    assert len(assistant_messages(result.transcript)) == 0


async def test_a_stop_reason_with_no_rule_ends_the_run(sbx: FakeSandbox) -> None:
    result, provider = await drive("loop_unexpected_stop.yaml", sbx)
    assert result.outcome == error("unexpected stop reason: other")
    assert len(provider.requests) == 1


# ---------- the role ----------
async def test_a_role_without_a_prompt_is_refused_before_any_call(sbx: FakeSandbox) -> None:
    provider = Recorder(SCRIPTS / "loop_nudge.yaml")
    cfg = CFG.model_copy(update={"role": Role.PLANNER})
    with pytest.raises(ValueError, match="planner"):
        await run_loop(make_spec(), provider, default_registry(), sbx, cfg)
    assert provider.requests == []


# ---------- LoopConfig ----------
def test_from_config_reads_the_example_config() -> None:
    app = load_config(EXAMPLE_CONFIG)
    cfg = LoopConfig.from_config(app, Role.CODER)
    assert cfg.model == app.models.coder == "claude-sonnet-5-5"
    assert cfg.effort == app.effort.coder == "medium"
    assert cfg.max_tokens_per_turn == app.loop.max_tokens_per_turn == 8192
    assert cfg.max_tokens_retry == app.loop.max_tokens_retry == 64000
    assert cfg.tool_output_max_tokens == app.context.tool_output_max_tokens == 4000
    assert cfg.max_turns == Budgets().task_turns
    assert cfg.role is Role.CODER


@pytest.mark.parametrize("role", list(Role))
def test_from_config_picks_the_model_and_effort_of_the_role(role: Role) -> None:
    app = load_config(EXAMPLE_CONFIG)
    cfg = LoopConfig.from_config(app, role)
    assert cfg.model == getattr(app.models, role.value)
    assert cfg.effort == getattr(app.effort, role.value)
    assert cfg.role is role


def test_the_compactor_runs_a_smaller_model_at_low_effort() -> None:
    cfg = LoopConfig.from_config(load_config(EXAMPLE_CONFIG), Role.COMPACTOR)
    assert (cfg.model, cfg.effort) == ("claude-haiku-5-5", "low")


def test_the_caller_sets_the_turn_limit() -> None:
    # The turn budget is per task (RepoSettings.budgets), which AppConfig does not hold.
    cfg = LoopConfig.from_config(load_config(EXAMPLE_CONFIG), Role.CODER, max_turns=7)
    assert cfg.max_turns == 7


@pytest.mark.parametrize(
    "bad",
    [
        {"max_turns": 0},
        {"max_tokens_per_turn": 0},
        {"tool_output_max_tokens": 0},
        {"model": ""},
        {"effort": "extreme"},
        {"surprise": 1},
    ],
)
def test_loop_config_rejects_values_that_cannot_work(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LoopConfig(**{"model": "m", **bad})


def test_the_retry_cap_cannot_be_lower_than_the_first_cap() -> None:
    # Same rule as the loop section of the config: a lower cap could not finish a reply the
    # first try cut off.
    with pytest.raises(ValidationError, match="max_tokens_retry"):
        LoopConfig(model="m", max_tokens_per_turn=8192, max_tokens_retry=4096)


def test_loop_config_does_not_change_under_a_running_loop() -> None:
    with pytest.raises(ValidationError):
        CFG.max_turns = 99  # type: ignore[misc]
