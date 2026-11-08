"""What a task of an issue says on the issue: a pickup comment and three labels (LH-306).

A reviewer should see the state of a task from the issue list, so the task sets a label as it
goes:

| When | Label |
|---|---|
| picked up | `longhaul:working` is added |
| the pull request is open | `longhaul:working` is removed, `longhaul:pr-open` is added |
| paused for a person | `longhaul:needs-human` is added |
| resumed | `longhaul:needs-human` is removed |
| ended, stuck or error | `longhaul:working` is removed, `longhaul:needs-human` is added |
| ended, cancelled | `longhaul:working` and `longhaul:needs-human` are removed |

The pause for a hint (LH-506) uses the middle two rows and `stuck_comment`. The pause for a plan
or a budget (Phases 5 and 7) will use the same helpers. The end of a task (LH-507) uses `on_end`
and `final_comment`: a task that opened its pull request has its labels already, a task that gave
up or failed asks for a person, and a task that was cancelled asks for nobody.

The commands of LH-308 add two comments. `status_comment` is what a task posts when a
maintainer asks `/longhaul status`, and `denied_comment` is the answer to a command from
someone who may not steer the task. The workflow posts the first, as the `post_comment`
activity. The poller posts the second, through the same function of side_effects.py.

A person who acts from the console (LH-805) leaves nothing on the issue, so `console_comment` is
the line that repeats the action there, for the people who read the issue.

This module only says what to write and under which key. The workflow runs each step as the
`post_comment`, `add_label` or `remove_label` activity, so every write goes through
github/side_effects.py (CONTRIBUTING.md rule 6). The keys are stable: the same step of the same
task has the same key on every attempt and in every run of a Continue-As-New chain, so a repeat
changes nothing on GitHub. A task can pause more than once, and each pause is a separate effect,
so the pause and resume keys carry the number of the pause.

The module has no I/O and imports only the pure inputs, so the workflow can import it.
"""

import re
from typing import Literal, NamedTuple

from longhaul.contracts import OutcomeKind, TaskSpec, TaskStatus
from longhaul.github.inputs import CommentInput, LabelInput, key_of

__all__ = [
    "NEEDS_HUMAN",
    "PR_OPEN",
    "WORKING",
    "ConsoleAction",
    "LabelStep",
    "console_comment",
    "denied_comment",
    "final_comment",
    "is_github_task",
    "on_end",
    "on_pause",
    "on_pickup",
    "on_pr_opened",
    "on_resume",
    "pickup_comment",
    "status_comment",
    "stuck_comment",
]

WORKING = "longhaul:working"
PR_OPEN = "longhaul:pr-open"
NEEDS_HUMAN = "longhaul:needs-human"

# The prefix of a repo that is a folder of the worker (github.intake.LOCAL_PREFIX). It is not
# imported because intake pulls in SQLAlchemy, which a workflow may not load. A unit test keeps
# the two equal.
LOCAL_PREFIX = "local:"

# A checkpoint is a 40 or 64 character hash. Seven characters name it well enough on an issue.
SHORT_SHA_CHARS = 7
# The key of a reply is built from a login, and a key part holds letters, digits, dot, underscore
# and hyphen (side_effects.effect_key). A login is made of letters, digits and hyphens already,
# so this only guards the key against a value that GitHub would never send.
_NOT_KEY_CHAR = re.compile(r"[^a-z0-9._-]")
MAX_LOGIN_CHARS = 60

# The longest quote of a hint or a piece of feedback in a mirror comment. The signal keeps the
# whole text for the model. The issue only needs enough to show what the person asked.
MAX_QUOTE_CHARS = 1_000


class LabelStep(NamedTuple):
    """One label change: add it when `add` is true, else remove it."""

    add: bool
    label: LabelInput


def is_github_task(spec: TaskSpec) -> bool:
    """True for the task of an issue: an owner/name repo and an issue number.

    Other tasks, such as `longhaul run` on a folder, have nowhere to comment and nothing to
    close, so they are not picked up on GitHub and do not end in a pull request.
    """
    return not spec.repo.startswith(LOCAL_PREFIX) and spec.issue_number is not None


def _issue(spec: TaskSpec) -> int:
    if spec.issue_number is None:
        raise ValueError(f"task {spec.task_id} has no issue to write to")
    return spec.issue_number


def _label(spec: TaskSpec, name: str, purpose: str, seq: int = 1) -> LabelInput:
    return LabelInput(
        task_id=spec.task_id,
        repo=spec.repo,
        number=_issue(spec),
        label=name,
        idempotency_key=key_of(spec.task_id, purpose, seq),
    )


def pickup_comment(spec: TaskSpec) -> CommentInput:
    """The "Picked up" comment. The task posts it, and not the poller (LH-307)."""
    return CommentInput(
        task_id=spec.task_id,
        repo=spec.repo,
        number=_issue(spec),
        body=(
            f"Picked up by Longhaul as task `{spec.task_id}`. "
            "A draft pull request will follow when the work is done."
        ),
        idempotency_key=key_of(spec.task_id, "picked_up", 1),
    )


def on_pickup(spec: TaskSpec) -> list[LabelStep]:
    return [LabelStep(True, _label(spec, WORKING, "label_working"))]


def on_pr_opened(spec: TaskSpec) -> list[LabelStep]:
    """Swap working for pr-open. The old label goes first, so the issue never has both."""
    return [
        LabelStep(False, _label(spec, WORKING, "label_working_off")),
        LabelStep(True, _label(spec, PR_OPEN, "label_pr_open")),
    ]


def on_pause(spec: TaskSpec, pause: int) -> list[LabelStep]:
    """Ask for a person. `pause` counts the pauses of the task from 1."""
    return [LabelStep(True, _label(spec, NEEDS_HUMAN, "label_needs_human_on", pause))]


def on_resume(spec: TaskSpec, pause: int) -> list[LabelStep]:
    """The person answered. `pause` is the number that on_pause was given."""
    return [LabelStep(False, _label(spec, NEEDS_HUMAN, "label_needs_human_off", pause))]


def on_end(spec: TaskSpec, kind: OutcomeKind, *, pr_opened: bool) -> list[LabelStep]:
    """The labels a task ends with, whatever way it ended.

    `longhaul:working` always comes off, with the key that the swap at the pull request used, so a
    task that ends after it opened its pull request finds the write on the ledger and changes
    nothing. A task that delivered has its labels already (`on_pr_opened`), so it gets none here.
    One that gave up or failed asks for a person. A cancelled one asks for nobody, and takes the
    request back if it was paused when it was cancelled: removing a label the issue does not have
    is not an error. `pr_opened` says that a pull request exists for a task that did not deliver,
    which is a draft of the partial work of a stuck task.
    """
    if kind is OutcomeKind.PR_OPENED:
        return []
    steps = [LabelStep(False, _label(spec, WORKING, "label_working_off"))]
    if kind is OutcomeKind.CANCELLED:
        steps.append(LabelStep(False, _label(spec, NEEDS_HUMAN, "label_needs_human_off_end")))
        return steps
    steps.append(LabelStep(True, _label(spec, NEEDS_HUMAN, "label_needs_human_on_end")))
    if pr_opened:
        steps.append(LabelStep(True, _label(spec, PR_OPEN, "label_pr_open")))
    return steps


def final_comment(spec: TaskSpec, body: str) -> CommentInput:
    """The report that a task leaves on its issue when it ends, under the key `<task>:final:1`.

    A task ends once, so there is one such comment. The body is rendered by the post_final
    activity (github/final_comment.py), which builds this input itself, so that a retry sends the
    same bytes. The key lives here with the others, and a unit test keeps the two equal.
    """
    return CommentInput(
        task_id=spec.task_id,
        repo=spec.repo,
        number=_issue(spec),
        body=body,
        idempotency_key=key_of(spec.task_id, "final", 1),
    )


def stuck_comment(spec: TaskSpec, pause: int, body: str) -> CommentInput:
    """The question that a paused task asks on its issue. `pause` counts the pauses from 1.

    Each pause is a separate effect, so the key holds its number: `<task>:stuck:<n>`. The body is
    rendered by the summarize_for_human activity (github/stuck_comment.py), and the workflow
    passes it on as it is.
    """
    return CommentInput(
        task_id=spec.task_id,
        repo=spec.repo,
        number=_issue(spec),
        body=body,
        idempotency_key=key_of(spec.task_id, "stuck", pause),
    )


def status_comment(spec: TaskSpec, status: TaskStatus, *, by: str, n: int) -> CommentInput:
    """The answer to `/longhaul status`. `n` counts the requests of the task from 1.

    Each request has a key of its own, so that two requests give two comments, and the same
    request after a replay gives none. The person who asked is named in code quotes and not with
    an @, because the comment is not meant to notify them.
    """
    checkpoint = status.last_checkpoint
    saved = f"`{checkpoint[:SHORT_SHA_CHARS]}`" if checkpoint else "no checkpoint yet"
    parts = [
        f"Status of `{spec.task_id}`, asked by `{by}`: phase `{status.phase.value}`, "
        f"turn {status.turn_index}, spend ${status.cost_usd:.4f}, last checkpoint {saved}."
    ]
    if status.paused_reason:
        parts.append(f"Paused: {status.paused_reason}.")
    if status.outcome is not None:
        parts.append(f"Outcome: {status.outcome.kind.value}.")
    return CommentInput(
        task_id=spec.task_id,
        repo=spec.repo,
        number=_issue(spec),
        body=" ".join(parts),
        idempotency_key=key_of(spec.task_id, "status", n),
    )


# The actions of the console that a task repeats on its issue. A cancel is not here: the final
# comment of the task already says "cancelled by console" and why.
ConsoleAction = Literal["approve_plan", "reject_plan", "hint", "add_budget"]

_CONSOLE_HEADINGS: dict[ConsoleAction, str] = {
    "approve_plan": "Plan approved from console",
    "reject_plan": "Plan rejected from console",
    "hint": "Hint from console",
    "add_budget": "Budget added from console",
}


def console_comment(
    spec: TaskSpec,
    action: ConsoleAction,
    *,
    n: int,
    text: str = "",
    usd: float = 0.0,
    turns: int = 0,
) -> CommentInput:
    """What a task writes on its issue when a person acts on it from the console (LH-805).

    A hint or a plan decision from the console leaves no trace on the issue, unlike a command
    typed there, so the task repeats it for the people who read the issue. `n` counts these
    comments from 1 and is part of the key, so each action gets one comment and a replay gets
    none.
    """
    heading = _CONSOLE_HEADINGS[action]
    if action == "approve_plan":
        body = f"{heading}."
    elif action == "add_budget":
        body = f"{heading}: {_amounts(usd, turns)}."
    else:
        body = f"{heading}:\n\n{_quoted(text)}"
    return CommentInput(
        task_id=spec.task_id,
        repo=spec.repo,
        number=_issue(spec),
        body=body,
        idempotency_key=key_of(spec.task_id, "console", n),
    )


def _amounts(usd: float, turns: int) -> str:
    parts = []
    if usd > 0:
        # A top-up under a cent would read as "$0.00", as if nothing was added.
        parts.append(f"${usd:.2f}" if usd >= 0.005 else "<$0.01")
    if turns > 0:
        parts.append(f"{turns} turn" if turns == 1 else f"{turns} turns")
    return " and ".join(parts)


def _quoted(text: str) -> str:
    """The text in a code fence, cut to MAX_QUOTE_CHARS.

    A fence keeps an @name in the text from notifying someone and keeps markdown in it from
    changing the comment. The fence is longer than any run of backticks in the text, so the text
    cannot close it early.
    """
    text = text.strip()
    if len(text) > MAX_QUOTE_CHARS:
        text = text[:MAX_QUOTE_CHARS].rstrip() + "..."
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def denied_comment(task_id: str, repo: str, number: int, login: str) -> CommentInput:
    """The one polite answer to a command from someone who may not steer the task.

    The key holds the login, so a user gets one answer for each task however many commands they
    send. GitHub ignores the case of a login, and so does the key.
    """
    who = _NOT_KEY_CHAR.sub("_", login.lower())[:MAX_LOGIN_CHARS]
    return CommentInput(
        task_id=task_id,
        repo=repo,
        number=number,
        body=(
            f"@{login} Only maintainers can steer this task. "
            f"A command counts only from people with write access to {repo}."
        ),
        idempotency_key=key_of(task_id, f"denied-{who}", 1),
    )
