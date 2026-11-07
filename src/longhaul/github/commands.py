"""Slash commands in issue comments, and who may send them (LH-308).

A maintainer steers a task from its issue with a comment that begins with `/longhaul`:

| Comment | What it asks |
|---|---|
| `/longhaul approve` | Approve the plan. |
| `/longhaul reject <feedback>` | Reject the plan and say why. |
| `/longhaul hint <text>` | Give the agent a hint. |
| `/longhaul budget +<usd> [turns +<n>]` | Raise the budget of the task. |
| `/longhaul cancel [reason]` | Stop the task. |
| `/longhaul status` | Ask the task to post its state on the issue. |
| `/longhaul retry` | Start the next attempt of a task that has ended. |

`parse_command` turns the body of a comment into a `Command`, or into None when the comment is
not a command. It does no I/O, and the poller does the rest: it checks who wrote the comment and
sends the signal (durable/activities/steering.py).

The rule is that a comment is a command when its first line is one. Nothing later in the comment
counts, and neither does a line that only looks like one:

* A command that sits in a fenced block, an indented block, a quote, a list or inline code is a
  command that someone is showing, not giving. The first line of such a comment starts with a
  fence, spaces, `>`, `-`, a number or a backtick, so it fails the test without a special case for
  each of them. Skipping a fenced block to look for a command after it would be the wrong fix: a
  comment that opens with a pasted log is a log with a note, not an order.
* The match is case-insensitive, and blank lines before the command are skipped.
* A command that takes no argument takes no text after it. `/longhaul approve now` is not
  `approve`, because a typo or a sentence that starts with a command word would otherwise change
  the state of a task. `cancel` is the exception, since its signal carries a reason.

Junk gets None. The poller answers nothing to a comment that is not a command, so a person who
mistypes sees nothing happen.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "MAX_TEXT_CHARS",
    "PREFIX",
    "STEERING_ROLES",
    "Command",
    "CommandKind",
    "may_steer",
    "parse_command",
]

PREFIX = "/longhaul"

# The text of a hint, a rejection or a cancel reason ends up in the prompt and in the workflow
# history. One line of a comment can be 65,000 characters, so the text is cut.
MAX_TEXT_CHARS = 2_000

# A line indented by this many spaces, or by a tab, is a code block in Markdown.
CODE_INDENT = 4

# The roles that may steer a task. GET /collaborators/{user}/permission answers with the legacy
# `permission` (admin, write, read or none, where maintain counts as write) and with the real
# `role_name`. Either one naming a role here is enough.
STEERING_ROLES = frozenset({"admin", "maintain", "write"})

_LINE_BREAK = re.compile(r"\r\n|\n|\r")
# ASCII digits only: \d also matches digits of other scripts, which float() would accept.
_BUDGET = re.compile(
    r"\+\$?(?P<usd>[0-9]+(?:\.[0-9]+)?)(?:\s+turns\s+\+(?P<turns>[0-9]+))?",
    re.IGNORECASE,
)


class CommandKind(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    HINT = "hint"
    BUDGET = "budget"
    CANCEL = "cancel"
    STATUS = "status"
    RETRY = "retry"


@dataclass(frozen=True)
class Command:
    """One parsed command.

    `text` is the feedback of `reject`, the text of `hint` and the reason of `cancel`. `usd` and
    `turns` are the top-up of `budget`. A field that the kind does not use stays empty.
    """

    kind: CommandKind
    text: str = ""
    usd: float = 0.0
    turns: int = 0


def parse_command(body: str) -> Command | None:
    """The command that a comment gives, or None when the comment is not a command."""
    line = _first_line(body)
    if line is None:
        return None
    split = _split(line)
    if split is None:
        return None
    word, argument = split
    try:
        kind = CommandKind(word.lower())
    except ValueError:
        return None
    return _with_argument(kind, argument)


def may_steer(permission: Mapping[str, Any]) -> bool:
    """True when the answer of the collaborator permission endpoint names a role that may steer."""
    return any(
        isinstance(value, str) and value.lower() in STEERING_ROLES
        for value in (permission.get("permission"), permission.get("role_name"))
    )


# ---------- reading the line ----------
def _first_line(body: str) -> str | None:
    """The first line that is not blank, or None for a body that is only space."""
    for line in _LINE_BREAK.split(body):
        if line.strip():
            return line
    return None


def _is_indented_code(line: str) -> bool:
    stripped = line.lstrip(" ")
    return stripped.startswith("\t") or len(line) - len(stripped) >= CODE_INDENT


def _split(line: str) -> tuple[str, str] | None:
    """(the command word, the rest of the line) of a line that starts with the prefix.

    None when the line does not start with `/longhaul` followed by a space, or when there is no
    word after it. `/longhaulx` is not the prefix.
    """
    if _is_indented_code(line):
        return None
    text = line.strip()
    if text[: len(PREFIX)].lower() != PREFIX:
        return None
    after = text[len(PREFIX) :]
    if after and not after[0].isspace():
        return None
    parts = after.split(None, 1)
    if not parts:
        return None
    return parts[0], parts[1].strip() if len(parts) > 1 else ""


# ---------- the argument of each kind ----------
def _with_argument(kind: CommandKind, argument: str) -> Command | None:
    match kind:
        case CommandKind.APPROVE | CommandKind.STATUS | CommandKind.RETRY:
            return Command(kind) if not argument else None
        case CommandKind.CANCEL:
            return Command(kind, text=_clip(argument))
        case CommandKind.REJECT | CommandKind.HINT:
            return Command(kind, text=_clip(argument)) if argument else None
        case CommandKind.BUDGET:
            return _budget(argument)


def _clip(text: str) -> str:
    return text[:MAX_TEXT_CHARS]


def _budget(argument: str) -> Command | None:
    found = _BUDGET.fullmatch(argument)
    if found is None:
        return None
    usd = float(found["usd"])
    turns = int(found["turns"]) if found["turns"] is not None else 0
    # A top-up of nothing is a typo, and an amount too large for a float is no amount at all.
    if not math.isfinite(usd) or usd <= 0:
        return None
    if found["turns"] is not None and turns <= 0:
        return None
    return Command(CommandKind.BUDGET, usd=usd, turns=turns)
