"""The comment that asks a person for a hint when a task is stuck (LH-506).

The text comes from templates/stuck_comment.md.j2. This module fills it in and keeps the promises
that github/plan_comment.py keeps, because the summary is written by a model and read by people:

* **The same bytes every time.** `post_comment` refuses a key that is used again with other
  text. The comment is rendered once, by the activity, and its result is what the workflow posts
  on every attempt, but the same summary still gives the same text. Nothing here reads a clock
  or a random number.
* **Only the template can mention a person or close an issue.** The model's words go through
  `defuse`, which ends the effect of "@name", "#7" and "<" and leaves the text readable.
* **One line for each part.** Line breaks inside the model's text are folded into spaces, so a
  part cannot start a heading or a list of its own.
* **Under 250 words.** A maintainer reads it on the issue list, so each part has a word limit
  and an extra bullet is dropped. A text that was cut ends in "...". The limits add up to about
  225 words with the title and the line that says how to answer, and a unit test checks the
  longest comment that the limits allow.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from longhaul.context.human_summary import StuckSummary
from longhaul.contracts import StuckReason
from longhaul.github.pr_body import defuse

__all__ = ["MAX_TRIED", "TEMPLATE", "render_stuck_comment"]

TEMPLATE = "stuck_comment.md.j2"

# The words of each part, and the bullets of "what I tried".
GOAL_WORDS = 28
TRIED_WORDS = 16
MAX_TRIED = 5
GUESS_WORDS = 32
QUESTION_WORDS = 28

# How the title names each reason.
REASONS = {
    StuckReason.NO_PROGRESS: "no progress",
    StuckReason.REPEATED_FAILURE: "repeated failure",
    StuckReason.EDIT_CHURN: "edit churn",
    StuckReason.TOOL_ERRORS: "tool errors",
    StuckReason.AGENT_ASKED: "the agent asked a question",
}


@lru_cache(maxsize=4)
def _environment(templates: Path) -> Environment:
    # Markdown, not HTML, so the engine escapes nothing. The values are made safe below.
    # StrictUndefined turns a field that the template names and the code forgot into an error.
    return Environment(  # noqa: S701
        loader=FileSystemLoader(templates),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _part(text: str, words: int) -> str:
    """The text on one line, cut to `words` words, safe to put on a page that people read.

    A paragraph that starts with "#" or ">" would be a heading or a quote, so those marks are
    dropped from the front. They are the only ones that change a line on their own.
    """
    found = text.split()
    cut = len(found) > words
    line = " ".join(found[:words]) + ("..." if cut else "")
    return defuse(line.lstrip("#> "))


def render_stuck_comment(
    summary: StuckSummary, reason: StuckReason, pause: int, *, templates: Path
) -> str:
    """The comment for the `pause`th pause of a task. `templates` is settings.templates_path."""
    page = (
        _environment(templates)
        .get_template(TEMPLATE)
        .render(
            reason=REASONS[reason],
            pause=int(pause),
            goal=_part(summary.goal, GOAL_WORDS),
            tried=[_part(item, TRIED_WORDS) for item in summary.tried[:MAX_TRIED]],
            guess=_part(summary.guess, GUESS_WORDS),
            question=_part(summary.question, QUESTION_WORDS),
        )
    )
    return str(page)
