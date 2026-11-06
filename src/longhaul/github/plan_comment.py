"""The comment that shows the plan on the issue: a checklist of its steps (LH-501).

The text comes from templates/plan_comment.md.j2. This module fills it in and keeps the promises
that github/pr_body.py keeps for the body of a pull request, because the plan is written by a
model and read by people:

* **The same bytes every time.** `post_comment` refuses a key that is used again with other
  text, and a retried `post_plan` renders the comment again. Nothing here reads a clock or a
  random number.
* **Only the template can mention a person or close an issue.** The model's words go through
  `defuse`, which ends the effect of "@name", "#7" and "<" and leaves the text readable.
* **One line for each item.** A step or a check is a list item. Line breaks inside the model's
  text are folded into spaces, so a step cannot start a heading, a second checklist or a fenced
  block. A file name is put in a code span, and a backtick or a control character in it is
  replaced.

The checklist starts unchecked and is not edited as the work goes on. The plan is a promise of
what will be done, and the pull request says what was done. A later story that wants a live
checklist can update the comment under the same key.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from longhaul.contracts import Plan
from longhaul.github.pr_body import defuse

__all__ = ["TEMPLATE", "render_plan_comment"]

TEMPLATE = "plan_comment.md.j2"

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


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


def _line(text: str) -> str:
    """The text on one line, safe to put on a page that people read."""
    return defuse(" ".join(text.split()))


def _code(path: str) -> str:
    """A file name in a code span that stays on one line."""
    return "`" + _CONTROL.sub(" ", path).replace("`", "'").strip() + "`"


def _files(paths: Sequence[str]) -> str:
    return ", ".join(_code(path) for path in paths)


def render_plan_comment(plan: Plan, version: int, *, templates: Path) -> str:
    """The comment for version `version` of `plan`.

    `templates` is the folder that holds plan_comment.md.j2 (settings.templates_path).
    """
    steps = [
        {
            "id": _line(step.id),
            "description": _line(step.description),
            "done_when": _line(step.done_when),
            "files": _files(step.files),
        }
        for step in plan.steps
    ]
    page = (
        _environment(templates)
        .get_template(TEMPLATE)
        .render(
            version=int(version),
            summary=_line(plan.summary),
            steps=steps,
            checks=[_line(check) for check in plan.acceptance_checks],
            risks=[_line(risk) for risk in plan.risks],
        )
    )
    return str(page)
