"""LH-401: the token meter and the context budget.

The calibration test reads tests/fixtures/usage_samples.json. Those samples are synthetic
until the live spike S4 records real ones, so the 10 percent result shows that the method
works on realistic numbers, not that it holds for the live API. The file says so itself.
"""

import ast
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from longhaul.context import tokens
from longhaul.context.tokens import (
    DEFAULT_CHARS_PER_TOKEN,
    ContextBudget,
    TokenMeter,
    count_request_chars,
    estimate_tokens,
    prompt_tokens,
)
from longhaul.contracts import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from longhaul.providers.scripted import ScriptedProvider

pytestmark = [pytest.mark.unit, pytest.mark.phase4]

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "usage_samples.json"
# LH-401 acceptance: after 3 observations the projection is within 10% of the true count.
CALIBRATION_OBSERVATIONS = 3
TOLERANCE = 0.10

Request = tuple[str, list[Message], list[ToolSpec]]


def _request(chars: int) -> Request:
    """A request of exactly `chars` characters: a system prompt and one user message."""
    system_chars = chars // 4
    message = Message(role="user", content=[TextBlock(text="m" * (chars - system_chars))])
    request = ("s" * system_chars, [message], [])
    assert count_request_chars(*request) == chars
    return request


# ---------- the usage samples ----------
# The fields of the API usage object that the tests read. A real sample from the spike carries
# more, such as the cache split by TTL, and the API sends null for a cache counter when no
# cache was involved. So a counter that is null or missing reads as zero, as the Anthropic
# provider reads it, and the file can be replaced by recorded usage without changing the tests.
COUNTERS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _counter(raw: dict[str, Any], name: str) -> int:
    value = raw.get(name)
    return value if isinstance(value, int) else 0


def _sessions() -> list[dict[str, Any]]:
    data = json.loads(SAMPLES.read_text(encoding="utf-8"))
    sessions: list[dict[str, Any]] = data["sessions"]
    return sessions


def _usage_from_api(raw: dict[str, Any]) -> Usage:
    """The field mapping of ADR-0003, appendix section 5, as the Anthropic provider applies it."""
    return Usage(
        input_tokens=_counter(raw, "input_tokens"),
        output_tokens=_counter(raw, "output_tokens"),
        cache_read_tokens=_counter(raw, "cache_read_input_tokens"),
        cache_write_tokens=_counter(raw, "cache_creation_input_tokens"),
    )


def _true_prompt_tokens(raw: dict[str, Any]) -> int:
    # Written out here, not taken from prompt_tokens(), so the test does not check the code
    # against itself.
    return (
        _counter(raw, "input_tokens")
        + _counter(raw, "cache_read_input_tokens")
        + _counter(raw, "cache_creation_input_tokens")
    )


def _errors(
    session: dict[str, Any], to_usage: Callable[[dict[str, Any]], Usage]
) -> list[tuple[int, float]]:
    """(turn, relative error) of the projection for each request after the calibration ones."""
    meter = TokenMeter()
    errors: list[tuple[int, float]] = []
    for index, sample in enumerate(session["samples"]):
        chars = sample["request_chars"]
        if index >= CALIBRATION_OBSERVATIONS:
            truth = _true_prompt_tokens(sample["usage"])
            projected = meter.projected(*_request(chars))
            errors.append((sample["turn"], (projected - truth) / truth))
        meter.observe(to_usage(sample["usage"]), chars)
    return errors


def test_calibration_within_10pct() -> None:
    checked = 0
    misses: list[str] = []
    for session in _sessions():
        for turn, error in _errors(session, _usage_from_api):
            checked += 1
            if abs(error) > TOLERANCE:
                misses.append(f"{session['name']} turn {turn}: {error:+.1%}")
    # A fixture that shrank to a few samples would pass without proving much.
    assert checked >= 30
    assert not misses, "projection more than 10% off the true prompt size:\n" + "\n".join(misses)


def test_a_meter_fed_only_input_tokens_misses_the_samples() -> None:
    # input_tokens is only the uncached remainder. If the samples cannot tell that mistake
    # from the right calibration, the test above proves nothing about the cache fields.
    def input_only(raw: dict[str, Any]) -> Usage:
        return Usage(input_tokens=_counter(raw, "input_tokens"))

    worst = max(abs(error) for s in _sessions() for _, error in _errors(s, input_only))
    assert worst > TOLERANCE


def test_the_usage_samples_are_well_formed() -> None:
    data = json.loads(SAMPLES.read_text(encoding="utf-8"))
    assert isinstance(data["synthetic"], bool)
    assert data["note"], "the file has to say where its numbers come from"
    sessions = _sessions()
    names = [s["name"] for s in sessions]
    assert len(names) == len(set(names)) >= 3
    for session in sessions:
        samples = session["samples"]
        assert [s["turn"] for s in samples] == list(range(1, len(samples) + 1))
        assert len(samples) > CALIBRATION_OBSERVATIONS
        for sample in samples:
            assert sample["request_chars"] > 0
            for name in COUNTERS:
                value = sample["usage"].get(name)
                assert value is None or (isinstance(value, int) and value >= 0), name
            assert _true_prompt_tokens(sample["usage"]) > 0
    usages = [s["usage"] for session in sessions for s in session["samples"]]
    assert any(_counter(u, "cache_read_input_tokens") > 0 for u in usages)
    assert any(_counter(u, "cache_creation_input_tokens") > 0 for u in usages)


# ---------- estimate_tokens ----------
@pytest.mark.parametrize(
    ("length", "expected"),
    [(0, 0), (1, 1), (3, 1), (4, 2), (7, 2), (8, 3), (70, 20), (71, 21), (3_500, 1_000)],
)
def test_estimate_tokens_is_the_ceiling_of_length_over_3_5(length: int, expected: int) -> None:
    assert estimate_tokens("x" * length) == expected


def test_estimate_tokens_counts_characters_not_bytes() -> None:
    # Seven euro signs are 7 characters and 21 bytes.
    assert estimate_tokens("€" * 7) == 2


# ---------- a meter with no observations ----------
def test_a_new_meter_projects_with_the_default_ratio() -> None:
    meter = TokenMeter()
    assert DEFAULT_CHARS_PER_TOKEN == 3.5
    assert meter.chars_per_token == DEFAULT_CHARS_PER_TOKEN
    assert meter.observations == 0
    system, messages, tools = _request(3_501)
    assert meter.projected(system, messages, tools) == estimate_tokens("x" * 3_501) == 1_001


def test_a_new_meter_has_no_fill_fraction_yet() -> None:
    assert TokenMeter().fraction(8_000) == 0.0


def test_the_starting_ratio_can_be_set_and_the_first_observation_replaces_it() -> None:
    meter = TokenMeter(prior=4.0)
    assert meter.projected(*_request(4_000)) == 1_000
    assert meter.observe(Usage(input_tokens=1_000), request_chars=3_000)
    assert meter.chars_per_token == pytest.approx(3.0)


@pytest.mark.parametrize("prior", [0.0, -1.0, 0.49, 10.01, float("nan"), float("inf")])
def test_the_starting_ratio_has_to_be_believable(prior: float) -> None:
    with pytest.raises(ValueError, match="chars_per_token"):
        TokenMeter(prior=prior)


# ---------- observe ----------
def test_the_first_observation_replaces_the_default_ratio() -> None:
    # With alpha 0.3 the default would still weigh 34 percent after three observations, and
    # a default that is off would keep the first projections off with it.
    meter = TokenMeter()
    assert meter.observe(Usage(input_tokens=1_000), request_chars=2_500)
    assert meter.chars_per_token == pytest.approx(2.5)
    assert meter.observations == 1


def test_later_observations_move_the_ratio_by_alpha_0_3() -> None:
    meter = TokenMeter()
    meter.observe(Usage(input_tokens=1_000), request_chars=3_000)  # 3.0
    meter.observe(Usage(input_tokens=2_000), request_chars=4_000)  # 2.0
    assert meter.chars_per_token == pytest.approx(0.7 * 3.0 + 0.3 * 2.0)
    meter.observe(Usage(input_tokens=1_000), request_chars=5_000)  # 5.0
    assert meter.chars_per_token == pytest.approx(0.7 * 2.7 + 0.3 * 5.0)
    assert meter.observations == 3


def test_the_whole_prompt_is_calibrated_not_input_tokens() -> None:
    # A cache hit leaves a handful of tokens in input_tokens. The prompt is all three.
    usage = Usage(
        input_tokens=12, cache_read_tokens=3_000, cache_write_tokens=600, output_tokens=400
    )
    assert prompt_tokens(usage) == 3_612
    meter = TokenMeter()
    meter.observe(usage, request_chars=10_836)
    assert meter.chars_per_token == pytest.approx(3.0)


def test_output_tokens_do_not_change_the_ratio() -> None:
    # Thinking is billed as output, and it is stripped before the next request (ADR-0003,
    # D1). It never appears in a prompt, so it cannot be part of its size.
    quiet, thinking = TokenMeter(), TokenMeter()
    quiet.observe(Usage(input_tokens=1_000, output_tokens=10), request_chars=3_000)
    thinking.observe(Usage(input_tokens=1_000, output_tokens=60_000), request_chars=3_000)
    assert quiet.chars_per_token == thinking.chars_per_token == pytest.approx(3.0)


UNUSABLE_USAGE = {
    "all zero": Usage(),
    "output only": Usage(output_tokens=500),
    "negative input": Usage(input_tokens=-1_000),
    "negative cache read": Usage(input_tokens=1_000, cache_read_tokens=-1),
    "negative cache write": Usage(input_tokens=1_000, cache_write_tokens=-1),
    "negative part that cancels a positive one": Usage(input_tokens=500, cache_read_tokens=-500),
    "negative part next to a large positive one": Usage(input_tokens=3_000, cache_write_tokens=-1),
}


@pytest.mark.parametrize("usage", UNUSABLE_USAGE.values(), ids=UNUSABLE_USAGE.keys())
def test_usage_that_is_zero_or_negative_is_ignored(usage: Usage) -> None:
    meter = TokenMeter()
    meter.observe(Usage(input_tokens=1_000), request_chars=3_000)
    assert meter.observe(usage, request_chars=9_000) is False
    assert meter.chars_per_token == pytest.approx(3.0)
    assert meter.observations == 1


@pytest.mark.parametrize("chars", [0, -1, -3_000])
def test_a_request_without_characters_is_ignored(chars: int) -> None:
    meter = TokenMeter()
    assert meter.observe(Usage(input_tokens=1_000), request_chars=chars) is False
    assert meter.chars_per_token == DEFAULT_CHARS_PER_TOKEN
    assert meter.observations == 0


@pytest.mark.parametrize(
    ("chars", "prompt"),
    [(1_000_000, 1), (1, 1_000_000), (49, 100), (101, 10)],
    ids=["one token for a huge prompt", "huge count for one character", "below 0.5", "above 10"],
)
def test_a_ratio_no_tokenizer_produces_is_ignored(chars: int, prompt: int) -> None:
    # One bad usage report would otherwise swing the average further than a few good ones
    # bring it back.
    meter = TokenMeter()
    meter.observe(Usage(input_tokens=1_000), request_chars=3_000)
    assert meter.observe(Usage(input_tokens=prompt), request_chars=chars) is False
    assert meter.chars_per_token == pytest.approx(3.0)


@pytest.mark.parametrize(("chars", "prompt"), [(50, 100), (100, 10)], ids=["0.5", "10"])
def test_the_edges_of_the_believable_range_are_accepted(chars: int, prompt: int) -> None:
    assert TokenMeter().observe(Usage(input_tokens=prompt), request_chars=chars) is True


# ---------- projected and fraction ----------
def test_the_projection_uses_the_calibrated_ratio() -> None:
    meter = TokenMeter()
    meter.observe(Usage(input_tokens=1_000), request_chars=2_500)
    assert meter.projected(*_request(5_000)) == 2_000
    assert meter.projected(*_request(5_001)) == 2_001  # rounded up


def test_the_projection_follows_the_request_when_it_shrinks() -> None:
    # After a compaction the request is far smaller than the last prompt measured. The
    # projection comes from the characters about to be sent, never from the old size.
    meter = TokenMeter()
    meter.observe(Usage(input_tokens=30_000), request_chars=90_000)
    assert meter.projected(*_request(30_000)) == 10_000


def test_fraction_follows_the_latest_projection_or_measurement() -> None:
    meter = TokenMeter()
    assert meter.projected(*_request(2_800)) == 800
    assert meter.fraction(8_000) == pytest.approx(0.1)
    meter.observe(Usage(input_tokens=1_000, cache_read_tokens=1_000), request_chars=6_000)
    assert meter.fraction(8_000) == pytest.approx(0.25)
    assert meter.fraction(4_000) == pytest.approx(0.5)
    meter.observe(Usage(), request_chars=100)  # ignored, so the figure stays
    assert meter.fraction(8_000) == pytest.approx(0.25)
    meter.projected(*_request(3_000))  # 1,000 tokens now that the ratio is 3.0
    assert meter.fraction(8_000) == pytest.approx(0.125)


@pytest.mark.parametrize("window", [0, -1, -8_000])
def test_fraction_rejects_a_window_that_is_not_positive(window: int) -> None:
    with pytest.raises(ValueError, match="window"):
        TokenMeter().fraction(window)


# ---------- count_request_chars ----------
def test_count_request_chars_adds_system_messages_and_tools() -> None:
    system = "abc"  # 3
    messages = [
        Message(role="user", content=[TextBlock(text="hello")]),  # 5
        Message(
            role="assistant",
            # id 2 + name 9 + input {"path":"a.py"} 15
            content=[ToolUseBlock(id="t1", name="read_file", input={"path": "a.py"})],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="t1", content="ok")],  # id 2 + content 2
        ),
    ]
    # name 1 + description 2 + schema {"type":"object"} 17
    tools = [ToolSpec(name="n", description="dd", input_schema={"type": "object"})]
    assert count_request_chars(system, messages, tools) == 3 + 5 + (2 + 9 + 15) + (2 + 2) + 20


def test_count_request_chars_counts_text_as_the_model_reads_it() -> None:
    # {"note":"€"} is 12 characters. JSON's default escaping would write the euro sign as a
    # backslash, the letter u and four hex digits, six characters in all, and make it 17.
    block = ToolUseBlock(id="", name="", input={"note": "€"})
    assert count_request_chars("", [Message(role="assistant", content=[block])], []) == 12


def test_count_request_chars_of_nothing_is_zero() -> None:
    assert count_request_chars("", [], []) == 0
    assert count_request_chars("", [Message(role="user", content=[])], []) == 0


# ---------- ContextBudget ----------
BUDGET = ContextBudget(window=200_000, compact_at=0.60, hard_limit=0.85)


def test_budget_decisions() -> None:
    # LH-401 acceptance: compact at 0.61 and force at 0.86 of the window.
    assert BUDGET.decide(122_000) == "compact"
    assert BUDGET.decide(172_000) == "force"
    assert BUDGET.decide(60_000) == "ok"


@pytest.mark.parametrize(
    ("projected", "expected"),
    [
        (0, "ok"),
        (1, "ok"),
        (119_999, "ok"),
        (120_000, "compact"),
        (169_999, "compact"),
        (170_000, "force"),
        (200_000, "force"),
        (350_000, "force"),
        (-5, "ok"),
    ],
)
def test_budget_acts_when_a_threshold_is_reached(projected: int, expected: str) -> None:
    assert BUDGET.decide(projected) == expected


def test_budget_decisions_on_the_8k_window_of_the_long_task_test() -> None:
    budget = ContextBudget(window=8_000, compact_at=0.60, hard_limit=0.85)
    assert [budget.decide(n) for n in (4_799, 4_800, 6_799, 6_800)] == [
        "ok",
        "compact",
        "compact",
        "force",
    ]


def test_budget_fraction_is_the_share_of_the_window() -> None:
    assert BUDGET.fraction(50_000) == pytest.approx(0.25)
    assert BUDGET.fraction(250_000) == pytest.approx(1.25)


@pytest.mark.parametrize("window", [0, -1, -200_000])
def test_budget_rejects_a_window_that_is_not_positive(window: int) -> None:
    with pytest.raises(ValueError, match="window"):
        ContextBudget(window=window, compact_at=0.6, hard_limit=0.85)


@pytest.mark.parametrize(
    ("compact_at", "hard_limit"),
    [
        (0.0, 0.85),
        (-0.1, 0.85),
        (0.85, 0.85),
        (0.9, 0.85),
        (0.6, 1.01),
        (0.6, float("nan")),
        (float("nan"), 0.85),
    ],
)
def test_budget_rejects_thresholds_that_are_out_of_order_or_out_of_range(
    compact_at: float, hard_limit: float
) -> None:
    with pytest.raises(ValueError, match="compact_at"):
        ContextBudget(window=200_000, compact_at=compact_at, hard_limit=hard_limit)


def test_budget_accepts_a_hard_limit_of_the_whole_window() -> None:
    budget = ContextBudget(window=1_000, compact_at=0.5, hard_limit=1.0)
    assert [budget.decide(n) for n in (499, 500, 999, 1_000)] == [
        "ok",
        "compact",
        "compact",
        "force",
    ]


def test_an_exact_count_is_wanted_only_near_the_thresholds() -> None:
    # The zone runs from compact_at - 0.05 (110,000 tokens here) up to hard_limit (170,000).
    below = [BUDGET.needs_exact_count(n) for n in (0, 100_000, 109_000)]
    inside = [BUDGET.needs_exact_count(n) for n in (111_000, 120_000, 150_000, 170_000)]
    above = [BUDGET.needs_exact_count(n) for n in (171_000, 250_000)]
    assert (below, inside, above) == ([False] * 3, [True] * 4, [False] * 2)


def test_the_meter_and_the_budget_agree_on_a_whole_turn() -> None:
    # The sequence the module docstring describes, with the meter calibrated at 3.0.
    meter = TokenMeter()
    budget = ContextBudget(window=8_000, compact_at=0.60, hard_limit=0.85)
    meter.observe(Usage(input_tokens=2_000), request_chars=6_000)
    small = meter.projected(*_request(9_000))  # 3,000 tokens
    large = meter.projected(*_request(15_000))  # 5,000 tokens
    huge = meter.projected(*_request(21_000))  # 7,000 tokens
    assert [budget.decide(n) for n in (small, large, huge)] == ["ok", "compact", "force"]
    assert meter.fraction(8_000) == pytest.approx(0.875)


async def test_the_turn_recipe_in_the_module_docstring(tmp_path: Path) -> None:
    # The steps tokens.py tells a caller to follow, run against a real provider object so the
    # calls cannot drift from the LLMProvider protocol unnoticed.
    script = tmp_path / "script.yaml"
    script.write_text("steps:\n  - respond: {text: done}\n", encoding="utf-8")
    provider = ScriptedProvider(script)
    system = "Role: coder\nFix the bug."
    messages = [Message(role="user", content=[TextBlock(text="x" * 2_110)])]
    tools = [ToolSpec(name="finish", description="End the task.", input_schema={"type": "object"})]
    meter = TokenMeter()
    budget = ContextBudget(window=1_000, compact_at=0.60, hard_limit=0.85)

    chars = count_request_chars(system, messages, tools)
    projected = meter.projected(system, messages, tools)
    assert (chars, projected, budget.decide(projected)) == (2_170, 620, "compact")
    assert budget.needs_exact_count(projected)
    exact = await provider.count_tokens(
        model="m", system=system, messages=messages, tools=tools, options=None
    )
    response = await provider.complete(
        model="m", system=system, messages=messages, tools=tools, max_tokens=100
    )
    # ScriptedProvider reports the figure it counts, at 4 characters per token. That is fewer
    # tokens than the meter's first estimate at 3.5, enough to move the decision to "ok".
    assert response.usage.input_tokens == exact < projected
    assert budget.decide(exact) == "ok"

    assert meter.observe(response.usage, chars)
    assert meter.chars_per_token == pytest.approx(chars / exact)


# ---------- the module stays pure ----------
ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "json",
    "math",
    "typing",
    "longhaul.contracts",
}


def test_the_tokens_module_imports_nothing_that_does_io() -> None:
    # The workflow decides from these numbers (LH-404), and workflow code does no I/O
    # (invariant I1). Standard library helpers and the contracts are all it needs.
    tree = ast.parse(Path(tokens.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            found.add(node.module)
    assert found <= ALLOWED_IMPORTS
