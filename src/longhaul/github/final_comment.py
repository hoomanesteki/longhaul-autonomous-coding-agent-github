"""The comment a task leaves on its issue when it ends (LH-507).

Every task of an issue ends in one outcome and says so once. The text comes from
templates/final_comment.md.j2. A task that delivered gets the pull request link and the summary
of the work. Any other end gets a report with what was done, what is left and the diff stats, so
a maintainer can decide whether to retry, take over or close the issue without opening the log.

This module fills the template in and keeps the promises that github/plan_comment.py and
github/stuck_comment.py keep:

* **The same bytes every time.** `post_comment` refuses a key that is used again with other text,
  and a retried `post_final` renders the comment again. Nothing here reads a clock or a random
  number, and the file list keeps the order git gave it.
* **Only the template can mention a person or close an issue.** The summary and the step texts
  are written by a model, and the file names are the repo's. `defuse` ends the effect of "@name",
  "#7" and "<". Line breaks inside a text are folded into spaces, so a text cannot start a
  heading or a list of its own.
* **A short comment.** The summary is cut to MAX_SUMMARY_CHARS, a step to MAX_STEP_CHARS, the
  plan has at most eight steps by contract, and the file list stops at MAX_FILES.

`files` is None when the diff could not be read, for example because the task never made a
checkpoint, and the comment says so. An empty list means that the work changed no file.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from longhaul.checkpoints.squash import FileStat
from longhaul.contracts import Outcome, OutcomeKind, Plan
from longhaul.github.pr_body import MAX_FILES, MAX_PATH_CHARS, defuse

__all__ = ["MAX_FILES", "MAX_STEP_CHARS", "MAX_SUMMARY_CHARS", "TEMPLATE", "render_final_comment"]

TEMPLATE = "final_comment.md.j2"
MAX_SUMMARY_CHARS = 1500
MAX_STEP_CHARS = 200

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# The first line of the comment for each outcome.
HEADLINES = {
    OutcomeKind.STUCK: "Longhaul is stuck and has stopped",
    OutcomeKind.CANCELLED: "Longhaul stopped: the task was cancelled",
    OutcomeKind.ERROR: "Longhaul hit an error and stopped",
    OutcomeKind.BUDGET_EXHAUSTED: "Longhaul stopped: the budget is spent",
}
# What a maintainer can do next. A task that delivered, or was cancelled on purpose, needs no
# advice.
NEXT_STEPS = {
    OutcomeKind.STUCK: (
        "Comment `/longhaul retry` to start a new attempt, or take over from the checkpoints."
    ),
    OutcomeKind.ERROR: (
        "The full trace is in the `error` event of the task in the `events` table. "
        "Comment `/longhaul retry` to start a new attempt."
    ),
    OutcomeKind.BUDGET_EXHAUSTED: (
        "Comment `/longhaul retry` to start a new attempt with the budget of the repo."
    ),
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


def _line(text: str, limit: int) -> str:
    """The text on one line, cut to `limit` characters, safe to put on a page that people read."""
    folded = " ".join(text.split())
    cut = len(folded) > limit
    line = folded[:limit].rstrip() + ("..." if cut else "")
    # A line that starts with "#" or ">" would be a heading or a quote.
    return defuse(line.lstrip("#> "))


def _path(path: str) -> str:
    """A file name that fits inside a code span on one line."""
    path = _CONTROL.sub(" ", path).replace("`", "'")
    if len(path) > MAX_PATH_CHARS:
        path = "..." + path[-(MAX_PATH_CHARS - 3) :]
    return path


def _counts(file: FileStat) -> str:
    if file.added is None or file.deleted is None:
        return "binary"
    return f"+{file.added} -{file.deleted}"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _changes(files: Sequence[FileStat] | None) -> str:
    if files is None:
        return "The diff stats could not be read."
    if not files:
        return "No files changed."
    added = sum(f.added or 0 for f in files)
    deleted = sum(f.deleted or 0 for f in files)
    return (
        f"{_plural(len(files), 'file')} changed, "
        f"{_plural(added, 'addition')}, {_plural(deleted, 'deletion')}"
    )


def _steps(plan: Plan, *, done: bool) -> list[dict[str, str]]:
    """The steps that are done, or the steps that are not done and not skipped."""
    wanted = [s for s in plan.steps if (s.status == "done") == done and s.status != "skipped"]
    return [
        {"id": _line(s.id, MAX_STEP_CHARS), "description": _line(s.description, MAX_STEP_CHARS)}
        for s in wanted
    ]


def _pr_line(delivered: bool, draft: bool) -> str:
    if delivered:
        return "Pull request"
    # A task that gave up opens a pull request only when the repo asked for its partial work.
    return (
        "The partial work is in a draft pull request"
        if draft
        else "The partial work is in a pull request"
    )


def _headline(outcome: Outcome, draft: bool) -> str:
    if outcome.kind is OutcomeKind.PR_OPENED:
        return "Longhaul opened a draft pull request" if draft else "Longhaul opened a pull request"
    return HEADLINES[outcome.kind]


def render_final_comment(
    outcome: Outcome,
    *,
    plan: Plan | None,
    files: Sequence[FileStat] | None,
    turns: int,
    cost_usd: float,
    draft: bool = True,
    templates: Path,
) -> str:
    """The comment for a task that ended with `outcome`.

    `plan` has the status of each step, and a task without one gets no step lists. `files` is
    the diff stat of the work, or None when it could not be read. `draft` says whether the pull
    request of the outcome, if there is one, is still a draft. `templates` is the folder that
    holds final_comment.md.j2 (settings.templates_path).
    """
    delivered = outcome.kind is OutcomeKind.PR_OPENED
    shown = list(files or [])[:MAX_FILES]
    page = (
        _environment(templates)
        .get_template(TEMPLATE)
        .render(
            headline=_headline(outcome, draft),
            summary=_line(outcome.summary, MAX_SUMMARY_CHARS),
            pr_url=outcome.pr_url,
            pr_line=_pr_line(delivered, draft),
            show_plan=plan is not None and not delivered,
            done=_steps(plan, done=True) if plan is not None else [],
            left=_steps(plan, done=False) if plan is not None else [],
            changes=_changes(files),
            files=[{"path": _path(f.path), "counts": _counts(f)} for f in shown],
            hidden=max(len(files or []) - MAX_FILES, 0),
            next_step=NEXT_STEPS.get(outcome.kind, ""),
            turns=_plural(int(turns), "turn"),
            cost=f"${cost_usd:.2f}",
        )
    )
    return str(page)
