"""The body of a pull request: what changed, how it was tested, and which issue it closes (LH-306).

The text comes from templates/pr_body.md.j2. This module fills it in and keeps three promises.

* **The same bytes every time.** `create_pr` refuses a key that is used again with other text
  (github/side_effects.py), and a retried `deliver` renders the body again. So nothing here reads
  a clock, a random number or anything else that changes from one attempt to the next.
* **Only the template can close an issue or mention a person.** The summary is what the model
  wrote when it finished, and the file names are the repo's. On GitHub "closes #7" in a pull
  request body closes issue 7 when the request merges, and "@name" notifies a person, so the
  model's text could close an unrelated issue or ping anyone. `defuse` ends each such mark
  (github/marks.py puts an invisible space after the `@`, after the `#` of a number, after `GH-`
  and after `://`), which leaves the text readable. It also turns `<` into `&lt;`, so the model
  cannot hide text in a comment or add markup. The one line that closes an issue is the last
  line of the template. The commit subject, which titles the pull request, gets the same
  treatment in checkpoints/squash.py.
* **A short body.** GitHub refuses more than 65,536 characters. The summary is cut, the list of
  files stops at MAX_FILES and a file name is cut, so the body stays far under it. The run report
  (LH-705) is the one long part. It is cut to the room that is left under MAX_BODY_CHARS, at the
  end of a line, with a note, and a report that has no room at all is left out.

The run report goes inside a `<details>` block, above the line that closes the issue. The caller
passes the finished markdown (reports/render.py has made every text in it safe), and this module
only fits it into the body.

The test line is built from the last `run_tests` call of the task, which the caller reads from the
`tool_calls` table and the blob store. Only the closing line of pytest's output goes in, as the
parser of the tool reads it, never the log itself, so the output of the repo's own code does not
reach a page that people read.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from longhaul.checkpoints.squash import FileStat
from longhaul.github.marks import defuse_marks

__all__ = [
    "MAX_BODY_CHARS",
    "MAX_FILES",
    "MAX_PATH_CHARS",
    "MAX_SUMMARY_CHARS",
    "REPORT_CUT_NOTE",
    "TEMPLATE",
    "LastTestRun",
    "defuse",
    "render_pr_body",
]

TEMPLATE = "pr_body.md.j2"
MAX_FILES = 100
MAX_PATH_CHARS = 200
MAX_SUMMARY_CHARS = 4000
SUMMARY_CUT = "(summary cut)"
# The whole body stays under this, a little below the 65,536 that GitHub accepts (LH-705).
MAX_BODY_CHARS = 60_000
REPORT_CUT_NOTE = (
    "(The report is cut here to keep the pull request short. `longhaul report` prints it all.)"
)
# The least room worth a report. Less than this leaves a heading and nothing to read.
MIN_REPORT_CHARS = 400

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class LastTestRun:
    """What the last `run_tests` call of the task came to.

    `summary` is the counts from pytest's closing line, or None if it printed none. `exit_code`
    is the code of the command. `ran` is False when the call failed before it ran any test, for
    example because it timed out or the repo has no test command.
    """

    summary: str | None
    exit_code: int | None
    ran: bool = True


def defuse(text: str) -> str:
    """Text from the model, safe to put on a page that people read. See the module docstring."""
    return defuse_marks(text.replace("<", "&lt;"))


def _summary(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_SUMMARY_CHARS:
        return defuse(text)
    cut = text[:MAX_SUMMARY_CHARS]
    if not text[MAX_SUMMARY_CHARS].isspace():
        cut = cut.rsplit(None, 1)[0] if len(cut.split()) > 1 else cut
    return f"{defuse(cut.rstrip())}\n\n{SUMMARY_CUT}"


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


def _changes(files: Sequence[FileStat]) -> str:
    if not files:
        return "No files changed."
    added = sum(f.added or 0 for f in files)
    deleted = sum(f.deleted or 0 for f in files)
    return (
        f"{_plural(len(files), 'file')} changed, "
        f"{_plural(added, 'addition')}, {_plural(deleted, 'deletion')}"
    )


def _tested(tests: LastTestRun | None) -> str:
    if tests is None:
        return "No test run was recorded for this task."
    if not tests.ran:
        return "The last `run_tests` call could not run its tests."
    code = "unknown" if tests.exit_code is None else str(tests.exit_code)
    if tests.summary:
        return f"Last `run_tests` run: {defuse(tests.summary)} (exit code {code})."
    return (
        f"Last `run_tests` run finished with exit code {code} and printed no pytest summary line."
    )


@lru_cache(maxsize=4)
def _environment(templates: Path) -> Environment:
    # Markdown, not HTML, so nothing is escaped by the engine. The values are made safe above.
    # StrictUndefined turns a field that the template names and the code forgot into an error.
    return Environment(  # noqa: S701
        loader=FileSystemLoader(templates),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _fit_report(report: str, room: int) -> str | None:
    """The report, cut at the end of a line to `room` characters with a note, or None.

    The note is part of the room. A cut inside a table row would show a broken row, so the cut
    falls on a line break.
    """
    report = report.strip()
    if len(report) <= room:
        return report
    allowed = room - len(REPORT_CUT_NOTE) - 2
    if allowed < MIN_REPORT_CHARS:
        return None
    kept = report[:allowed].rsplit("\n", 1)[0].rstrip()
    return f"{kept}\n\n{REPORT_CUT_NOTE}"


def render_pr_body(
    issue_number: int,
    summary: str,
    files: Sequence[FileStat],
    tests: LastTestRun | None,
    *,
    templates: Path,
    report: str | None = None,
    limit: int = MAX_BODY_CHARS,
) -> str:
    """The body for a pull request that closes issue `issue_number`.

    `files` is the diff stat of the branch, in the order git gave it. `templates` is the folder
    that holds pr_body.md.j2 (settings.templates_path). `report` is the run report (LH-705), put
    in a `<details>` block. The body stays under `limit` characters by cutting the report.
    """
    listed = [{"path": _path(f.path), "counts": _counts(f)} for f in files[:MAX_FILES]]
    template = _environment(templates).get_template(TEMPLATE)
    fields: dict[str, Any] = {
        "summary": _summary(summary),
        "changes": _changes(files),
        "files": listed,
        "hidden": max(len(files) - MAX_FILES, 0),
        "tested": _tested(tests),
        "issue_number": int(issue_number),
        "report": None,
    }
    bare = str(template.render(**fields))
    if not report:
        return bare
    # The block adds its own tags and blank lines, which the first render shows by difference.
    framed = len(str(template.render(**{**fields, "report": "x"}))) - len(bare) - 1
    # One character less, so that the body is under the limit and not at it.
    fitted = _fit_report(report, limit - len(bare) - framed - 1)
    if fitted is None:
        return bare
    return str(template.render(**{**fields, "report": fitted}))
