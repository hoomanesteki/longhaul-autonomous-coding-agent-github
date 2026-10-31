"""Token meter and context budget (LH-401).

The loop needs a token count before every model call, so that it compacts before the
provider rejects the request. The provider's own count is exact but costs a round trip, and
only the Anthropic provider counts for real (ScriptedProvider and the OpenAI provider
estimate). So the meter learns how many characters a token takes from the usage that real
responses report, and projects the next request from its characters alone.

This module is pure. It does no I/O, reads no clock and draws no random numbers, and it
imports only the standard library and longhaul.contracts, so workflow code may call it
(invariant I1). A test keeps the import list that short.

What a prompt weighs. The size of a prompt is input_tokens + cache_read_tokens +
cache_write_tokens. On the Messages API input_tokens is only the uncached remainder, and the
three fields do not overlap (ADR-0003, appendix section 5). With a warm cache input_tokens
can be a few dozen tokens of a prompt of 60,000, and a ratio built on it would be off by
orders of magnitude. prompt_tokens() is the one definition of that sum.

What it leaves out. output_tokens play no part. Thinking is billed as output, but thinking
blocks are stripped when a response is ingested (ADR-0003, D1), so they are never replayed.
The next prompt holds the reply's text and tool calls, which count_request_chars measures
directly, and not the output tokens that produced them.

Calibration. observe() takes the size of a prompt and the characters of the request that
produced it. The first accepted observation replaces the starting ratio, because with alpha
0.3 the starting ratio would still weigh 34 percent after three observations, and a starting
value that is wrong would drag the first projections with it. Later observations move the
ratio by 30 percent of the gap. A reading that cannot be real is ignored and observe()
returns False: a negative or empty count, a request with no characters, or a ratio outside
what any tokenizer produces. One such reading would swing the average further than a few good
ones bring it back.

The average lags when the mix of text changes. While the share of logs and code grows the
ratio falls, and the projection runs a little low until the average catches up. The margin of
0.05 below compact_at in needs_exact_count() leaves room for that.

Using it. A caller does this once per turn, in the code that holds the transcript (an
activity, because the transcript lives in the blob store and not in workflow state):

    chars = count_request_chars(system, messages, tools)
    projected = meter.projected(system, messages, tools)
    if budget.needs_exact_count(projected):
        try:
            projected = await provider.count_tokens(
                model=model, system=system, messages=messages, tools=tools, options=options
            )
        except ProviderError:
            pass  # keep the estimate
    decision = budget.decide(projected)
    ...  # compact when the decision is "compact" or "force"
    response = await provider.complete(...)
    meter.observe(response.usage, chars)

The meter never makes that call itself, because it would be I/O. The caller asks the provider
for an exact count only inside the band where precision matters (needs_exact_count). It
passes the same model, system, messages, tools and options as the complete() call that
follows, because counts are model specific and follow the same request rules. If the count
fails, the estimate stands. A request that really is too long fails with CONTEXT_TOO_LONG,
and that forces a compaction anyway (F6). After a compaction the request is a new one, so the
caller counts its characters and projects it again before the next call. The scripted and
OpenAI providers answer count_tokens with an estimate of their own, so the call adds little
there, and a caller may skip it for them.

The meter is a few numbers of plain state. A caller that starts every turn in a fresh
activity rebuilds it by replaying the last few (usage, request_chars) pairs through observe().
Three are enough for the projection to settle.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, assert_never

from longhaul.contracts import (
    Block,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)

__all__ = [
    "DEFAULT_CHARS_PER_TOKEN",
    "ContextBudget",
    "Decision",
    "TokenMeter",
    "count_request_chars",
    "estimate_tokens",
    "prompt_tokens",
]

# The rule before anything has been measured: ceil(len / 3.5).
DEFAULT_CHARS_PER_TOKEN = 3.5
# Weight of the newest observation in the moving average.
EMA_ALPHA = 0.3
# Real text sits between about 1 and 6 characters per token. The bounds are wide on purpose:
# they catch a bad usage report, not an unusual tokenizer.
MIN_CHARS_PER_TOKEN = 0.5
MAX_CHARS_PER_TOKEN = 10.0
# How far below compact_at an estimate can sit and still be close enough to the threshold that
# its error may flip the decision. It is about 8 percent of the default compact_at of 0.60,
# in line with the 10 percent the calibration is asked to reach.
PRECISION_MARGIN = 0.05

Decision = Literal["ok", "compact", "force"]


def _tokens_for(chars: int, chars_per_token: float) -> int:
    return math.ceil(max(chars, 0) / chars_per_token)


def _require_window(window: int) -> None:
    # A zero window would divide by zero, and a fill fraction against it means nothing.
    if window < 1:
        raise ValueError(f"window must be a positive number of tokens, got {window}")


def estimate_tokens(text: str) -> int:
    """ceil(len(text) / 3.5): the estimate for text when nothing has been measured yet.

    It counts characters, not bytes, so non-ASCII text is not over-counted.
    """
    return _tokens_for(len(text), DEFAULT_CHARS_PER_TOKEN)


def prompt_tokens(usage: Usage) -> int:
    """The size of the prompt a usage record describes: all three input counters."""
    return usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens


# ---------- request size ----------
def _json_chars(value: object) -> int:
    # Compact JSON with real characters is the closest cheap match to the text the model
    # reads. The default separators and \uXXXX escapes would add characters it never sees.
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _block_chars(block: Block) -> int:
    if isinstance(block, TextBlock):
        return len(block.text)
    if isinstance(block, ToolUseBlock):
        return len(block.id) + len(block.name) + _json_chars(block.input)
    if isinstance(block, ToolResultBlock):
        return len(block.tool_use_id) + len(block.content)
    # A block type added to the contracts has to be counted here, and mypy fails until it is.
    assert_never(block)


def _tool_chars(tool: ToolSpec) -> int:
    return len(tool.name) + len(tool.description) + _json_chars(tool.input_schema)


def count_request_chars(system: str, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> int:
    """Characters of a request: what observe() takes and what projected() converts.

    It counts the system prompt, message text, tool call ids, names and arguments, tool result
    ids and content, and the tool definitions (name, description and schema), because the
    provider bills all of them as input. Fixed framing per message is not counted. The
    calibrated ratio absorbs it on average.
    """
    return (
        len(system)
        + sum(_block_chars(block) for message in messages for block in message.content)
        + sum(_tool_chars(tool) for tool in tools)
    )


# ---------- the meter ----------
def _measured_prompt(usage: Usage) -> int | None:
    """The prompt size of a usage record, or None when it cannot be a real measurement."""
    counters = (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens)
    if min(counters) < 0:
        return None
    total = prompt_tokens(usage)
    return total if total > 0 else None


class TokenMeter:
    """Learns chars per token from real usage and projects the size of the next request.

    `prior` is the ratio used until the first observation arrives.
    """

    def __init__(self, prior: float = DEFAULT_CHARS_PER_TOKEN) -> None:
        if not MIN_CHARS_PER_TOKEN <= prior <= MAX_CHARS_PER_TOKEN:
            raise ValueError(
                f"prior chars_per_token must be between {MIN_CHARS_PER_TOKEN} and "
                f"{MAX_CHARS_PER_TOKEN}, got {prior}"
            )
        self._chars_per_token = prior
        self._observations = 0
        self._latest_tokens = 0

    @property
    def chars_per_token(self) -> float:
        """The ratio now in use: the prior, or the moving average once observations arrive."""
        return self._chars_per_token

    @property
    def observations(self) -> int:
        """How many observations were accepted."""
        return self._observations

    def observe(self, usage: Usage, request_chars: int) -> bool:
        """Calibrate on a real response. True when the reading was used, False when ignored.

        `request_chars` is count_request_chars() of the request that produced `usage`. The
        prompt size is all three input counters (see prompt_tokens), and output tokens are
        left out.
        """
        prompt = _measured_prompt(usage)
        if prompt is None:
            return False
        # A request with no characters has a ratio of zero or less, which the bounds reject.
        ratio = request_chars / prompt
        if not MIN_CHARS_PER_TOKEN <= ratio <= MAX_CHARS_PER_TOKEN:
            return False
        if self._observations == 0:
            self._chars_per_token = ratio
        else:
            self._chars_per_token += EMA_ALPHA * (ratio - self._chars_per_token)
        self._observations += 1
        self._latest_tokens = prompt
        return True

    def projected(self, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> int:
        """Tokens of this request at the current ratio. It also sets what fraction() reports.

        The result comes from the characters about to be sent, never from the size of the last
        prompt, so it follows a request that shrank after elision or compaction.
        """
        tokens = _tokens_for(count_request_chars(system, messages, tools), self._chars_per_token)
        self._latest_tokens = tokens
        return tokens

    def fraction(self, window: int) -> float:
        """The share of `window` taken by the newest figure the meter holds.

        That is the latest projection, or the prompt measured by an observation made after it.
        It is 0.0 until the meter has seen either. It can pass 1.0.
        """
        _require_window(window)
        return self._latest_tokens / window


# ---------- the budget ----------
@dataclass(frozen=True)
class ContextBudget:
    """Turns a token count into a decision.

    `window` is the budget Longhaul plans for, in tokens (`context.window_tokens`), not the
    model's own limit. `compact_at` and `hard_limit` are fractions of it, with
    0 < compact_at < hard_limit <= 1.
    """

    window: int
    compact_at: float
    hard_limit: float

    def __post_init__(self) -> None:
        _require_window(self.window)
        if not 0 < self.compact_at < self.hard_limit <= 1:
            raise ValueError(
                "need 0 < compact_at < hard_limit <= 1, "
                f"got compact_at={self.compact_at} and hard_limit={self.hard_limit}"
            )

    def fraction(self, tokens: int) -> float:
        """The share of the window that `tokens` takes."""
        return tokens / self.window

    def decide(self, projected: int) -> Decision:
        """Say what to do with a request of `projected` tokens: ok, compact or force.

        It is "force" at hard_limit or above, "compact" at compact_at or above, and "ok"
        below that. `projected` is a projection from TokenMeter or an exact count from the
        provider. Reaching a threshold counts as crossing it: a request that fills the window
        to exactly hard_limit is already a "force".
        """
        used = self.fraction(projected)
        if used >= self.hard_limit:
            return "force"
        if used >= self.compact_at:
            return "compact"
        return "ok"

    def needs_exact_count(self, projected: int) -> bool:
        """True when an exact count from the provider is worth its round trip.

        The band starts PRECISION_MARGIN below compact_at and ends at hard_limit, which is
        inside it. Below the band the estimate is far from any threshold. Above it the answer
        is already "force", and acting on an estimate there costs at worst a compaction that
        was a little early.
        """
        return self.compact_at - PRECISION_MARGIN <= self.fraction(projected) <= self.hard_limit
