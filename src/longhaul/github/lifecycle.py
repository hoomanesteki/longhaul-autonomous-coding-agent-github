"""What a task of an issue says on the issue: a pickup comment and three labels (LH-306).

A reviewer should see the state of a task from the issue list, so the task sets a label as it
goes:

| When | Label |
|---|---|
| picked up | `longhaul:working` is added |
| the pull request is open | `longhaul:working` is removed, `longhaul:pr-open` is added |
| paused for a person | `longhaul:needs-human` is added |
| resumed | `longhaul:needs-human` is removed |

Pause and resume arrive in Phases 5 and 7. Their helpers are here so that those stories only
have to call them.

This module only says what to write and under which key. The workflow runs each step as the
`post_comment`, `add_label` or `remove_label` activity, so every write goes through
github/side_effects.py (CONTRIBUTING.md rule 6). The keys are stable: the same step of the same
task has the same key on every attempt and in every run of a Continue-As-New chain, so a repeat
changes nothing on GitHub. A task can pause more than once, and each pause is a separate effect,
so the pause and resume keys carry the number of the pause.

The module has no I/O and imports only the pure inputs, so the workflow can import it.
"""

from typing import NamedTuple

from longhaul.contracts import TaskSpec
from longhaul.github.inputs import CommentInput, LabelInput, key_of

__all__ = [
    "NEEDS_HUMAN",
    "PR_OPEN",
    "WORKING",
    "LabelStep",
    "is_github_task",
    "on_pause",
    "on_pickup",
    "on_pr_opened",
    "on_resume",
    "pickup_comment",
]

WORKING = "longhaul:working"
PR_OPEN = "longhaul:pr-open"
NEEDS_HUMAN = "longhaul:needs-human"

# The prefix of a repo that is a folder of the worker (github.intake.LOCAL_PREFIX). It is not
# imported because intake pulls in SQLAlchemy, which a workflow may not load. A unit test keeps
# the two equal.
LOCAL_PREFIX = "local:"


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
