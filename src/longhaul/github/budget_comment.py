"""The comment that tells a maintainer a budget has stopped a task (LH-702).

The text comes from templates/budget_comment.md.j2. This module picks the words for each limit
and fills the template in. A maintainer reads it on the issue and answers in one comment, so it
gives the numbers and the exact commands, and the commands are the ones the poller understands
(github/commands.py). The wording is checked against that parser in a unit test.

The comment is posted under the key `budget:<n>`, and github/side_effects.py refuses a key that is
used again with other text, so the same numbers give the same bytes. Every value in it is a number
or a fixed word. No text of the model reaches it, so unlike the stuck comment it needs no
defusing.

What the task does next depends on the limit. A limit of the task (dollars, turns, minutes) waits
for a top-up, and the comment says for how long and what the end is. The daily limit of the repo
cannot be lifted by a person, so the comment says when the task goes on by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from longhaul.cost.budget import BudgetNumbers, BudgetReason, usd
from longhaul.roles.stuck import waited_text

__all__ = ["BUDGET_COMMENT", "TEMPLATE", "render_budget_comment"]

TEMPLATE = "budget_comment.md.j2"
# The purpose part of the idempotency key: `<task>:budget:<n>` for the nth pause of the task.
BUDGET_COMMENT = "budget"

# What a comment suggests a person adds. The amounts are examples that parse, not rules: any
# amount works. A top-up always has dollars (github/commands.py refuses one of nothing), so the
# turns command carries a dollar as well.
SUGGESTED_USD = 2
SUGGESTED_RESUME_USD = 1
SUGGESTED_TURNS = 25
CANCEL = "`/longhaul cancel`"


@dataclass(frozen=True)
class _Row:
    label: str
    value: str


@dataclass(frozen=True)
class _Words:
    title: str
    headline: str
    rows: list[_Row]
    next_step: str
    answer: str


@lru_cache(maxsize=4)
def _environment(templates: Path) -> Environment:
    # Markdown, not HTML, so the engine escapes nothing. StrictUndefined turns a field that the
    # template names and the code forgot into an error.
    return Environment(  # noqa: S701
        loader=FileSystemLoader(templates),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _waiting(wait: timedelta | None) -> str:
    if wait is None:
        raise ValueError("a limit of the task needs a wait to say how long the task waits")
    return (
        f"The task waits {waited_text(wait)} for a top-up. If nobody answers, it ends with "
        "outcome `budget_exhausted`."
    )


def _task_usd(n: BudgetNumbers, wait: timedelta | None) -> _Words:
    return _Words(
        title="task budget",
        headline=(
            f"The next model call could take the task over its budget of {usd(n.task_usd_limit)}."
        ),
        rows=[
            _Row("Spent so far", usd(n.spent_task_usd)),
            _Row("Worst case of the next call", usd(n.projected_usd)),
            _Row("Budget, top-ups included", usd(n.task_usd_limit)),
        ],
        next_step=_waiting(wait),
        answer=(
            f"Reply with `/longhaul budget +{SUGGESTED_USD}` to add ${SUGGESTED_USD}, "
            f"or {CANCEL} to stop the task."
        ),
    )


def _task_turns(n: BudgetNumbers, wait: timedelta | None) -> _Words:
    return _Words(
        title="turn budget",
        headline=f"The task has used {n.turns_done} of {n.turns_limit} turns.",
        rows=[
            _Row("Turns used", str(n.turns_done)),
            _Row("Turn budget, top-ups included", str(n.turns_limit)),
            _Row("Spent so far", usd(n.spent_task_usd)),
        ],
        next_step=_waiting(wait),
        answer=(
            f"Reply with `/longhaul budget +{SUGGESTED_RESUME_USD} turns +{SUGGESTED_TURNS}` "
            f"to add {SUGGESTED_TURNS} turns, or {CANCEL} to stop the task."
        ),
    )


def _wall_clock(n: BudgetNumbers, wait: timedelta | None) -> _Words:
    worked = int(n.elapsed_minutes)  # whole minutes, rounded down
    return _Words(
        title="time budget",
        headline=(
            f"The task has worked {worked} minutes, and its time budget is "
            f"{n.wall_clock_minutes_limit} minutes."
        ),
        rows=[
            _Row("Worked", f"{worked} minutes"),
            _Row("Time budget", f"{n.wall_clock_minutes_limit} minutes"),
            _Row("Spent so far", usd(n.spent_task_usd)),
        ],
        next_step=(
            f"{_waiting(wait)} Any top-up starts a new window of "
            f"{n.wall_clock_minutes_limit} minutes."
        ),
        answer=(
            f"Reply with `/longhaul budget +{SUGGESTED_RESUME_USD}` to go on, "
            f"or {CANCEL} to stop the task."
        ),
    )


def _daily(n: BudgetNumbers, resume_at: datetime | None) -> _Words:
    if resume_at is None:
        raise ValueError("the daily limit needs resume_at to say when the task goes on")
    when = resume_at.strftime("%Y-%m-%d %H:%M UTC")
    return _Words(
        title="daily budget of the repo",
        headline=(
            f"The repo has spent {usd(n.spent_repo_today_usd)} of its daily budget of "
            f"{usd(n.daily_repo_usd_limit)}, and the next call could pass it."
        ),
        rows=[
            _Row("Spent by the repo today", usd(n.spent_repo_today_usd)),
            _Row("Worst case of the next call", usd(n.projected_usd)),
            _Row("Daily budget of the repo", usd(n.daily_repo_usd_limit)),
        ],
        next_step=(
            "Nothing is needed from you. The task goes on by itself at "
            f"{when}, when the day of the repo starts again. A top-up does not raise this limit."
        ),
        answer=f"Reply with {CANCEL} to stop the task.",
    )


def render_budget_comment(
    reason: BudgetReason,
    numbers: BudgetNumbers,
    pause: int,
    *,
    wait: timedelta | None,
    resume_at: datetime | None,
    templates: Path,
) -> str:
    """The comment for the `pause`th pause of a task. `templates` is settings.templates_path.

    A limit of the task needs `wait`, how long the task waits for a top-up. The daily limit needs
    `resume_at`, the moment the day starts again. A call without the one it needs is a mistake of
    the caller and raises ValueError.
    """
    words = {
        BudgetReason.TASK_USD: lambda: _task_usd(numbers, wait),
        BudgetReason.TASK_TURNS: lambda: _task_turns(numbers, wait),
        BudgetReason.WALL_CLOCK: lambda: _wall_clock(numbers, wait),
        BudgetReason.DAILY_REPO_USD: lambda: _daily(numbers, resume_at),
    }[reason]()
    page = (
        _environment(templates)
        .get_template(TEMPLATE)
        .render(
            title=words.title,
            pause=int(pause),
            headline=words.headline,
            rows=words.rows,
            next_step=words.next_step,
            answer=words.answer,
        )
    )
    return str(page)
