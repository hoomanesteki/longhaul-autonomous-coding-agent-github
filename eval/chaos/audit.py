"""Count what a run wrote to GitHub, from the state of the GitHub it wrote to (LH-904).

Our own `side_effects` table is where a retry decides whether a write already happened. If that
table were also the witness, a bug in how it is read or written would hide itself: it would say
"one comment" because it believes it wrote one. So the count is taken from FakeGitHub, which holds
every comment, label change and pull request that reached it.

The expected counts come from the lifecycle of a task (`longhaul.github.lifecycle`): one pickup
comment, a label added when the task starts, and the label swapped when the pull request opens.
A run that finds more than that has written something twice. A run that finds less has lost a
write.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple, Protocol

from eval.chaos.results import SideEffects
from longhaul.contracts import TaskSpec, Usage
from longhaul.cost.pricing import Pricing
from longhaul.github import lifecycle


class GitHubState(Protocol):
    """The reads of FakeGitHub that a count needs."""

    def comment_bodies(self, repo: str, number: int) -> list[str]: ...

    def label_events(self, repo: str, number: int | None = None) -> list[Any]: ...

    def pulls(self, repo: str, state: str = "all") -> list[dict[str, Any]]: ...


def count_side_effects(state: GitHubState, repo: str, issue: int) -> SideEffects:
    """The comments on the issue, the label changes on it and the pull requests of the repo."""
    return SideEffects(
        comments=len(state.comment_bodies(repo, issue)),
        label_events=len(state.label_events(repo, issue)),
        pull_requests=len(state.pulls(repo)),
    )


def expected_side_effects(spec: TaskSpec) -> SideEffects:
    """What a task of an issue writes when nothing goes wrong."""
    if not lifecycle.is_github_task(spec):
        raise ValueError(f"task {spec.task_id} has no issue to write to, so it writes nothing")
    steps = len(lifecycle.on_pickup(spec)) + len(lifecycle.on_pr_opened(spec))
    return SideEffects(comments=1, label_events=steps, pull_requests=1)


def _differences(observed: SideEffects, expected: SideEffects) -> list[int]:
    return [getattr(observed, name) - getattr(expected, name) for name in SideEffects.model_fields]


def duplicates(observed: SideEffects, expected: SideEffects) -> int:
    """Writes beyond the expected ones, summed over comments, label changes and pull requests."""
    return sum(max(0, d) for d in _differences(observed, expected))


def missing(observed: SideEffects, expected: SideEffects) -> int:
    """Expected writes that never reached GitHub, summed over the same three kinds."""
    return sum(max(0, -d) for d in _differences(observed, expected))


class LedgerRow(NamedTuple):
    """The tokens of one model call on the cost ledger."""

    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


def ledger_cost(rows: Sequence[LedgerRow], pricing: Pricing) -> float:
    """What the calls would cost at the published rates, in dollars.

    A replayed run bills nothing (the provider is free), so the `usd` column of its ledger is
    zero. The tokens of each call are real, recorded with the call, and pricing them with the
    rates of config/pricing.yaml gives the cost that the same calls would have had.
    """
    total = sum(
        pricing.cost(
            Usage(
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_read_tokens=row.cache_read_tokens,
                cache_write_tokens=row.cache_write_tokens,
            ),
            row.model,
        )
        for row in rows
    )
    return float(total)
