"""The labels and the pickup comment of a GitHub task (LH-306).

These helpers are pure: they say what to write and under which key. The workflow runs the steps
as activities, and tests/integration/test_deliver.py::test_label_transitions runs them against
FakeGitHub.
"""

import pytest

from longhaul.contracts import RepoSettings, TaskSpec
from longhaul.github import lifecycle
from longhaul.github.intake import is_local
from longhaul.github.lifecycle import LabelStep
from longhaul.github.side_effects import effect_key, has_marker

pytestmark = [pytest.mark.unit, pytest.mark.phase3]


def spec(
    repo: str = "acme/calc", issue: int | None = 42, task_id: str = "lh-acme-calc-42"
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        repo=repo,
        issue_number=issue,
        title="add() subtracts",
        body="",
        settings=RepoSettings(repo=repo),
    )


@pytest.mark.parametrize(
    ("repo", "issue", "expected"),
    [
        ("acme/calc", 42, True),
        ("acme/calc", None, False),
        ("local:/home/me/calc", 42, False),
        ("local:calc", None, False),
    ],
)
def test_a_github_task_has_an_owner_name_repo_and_an_issue(
    repo: str, issue: int | None, expected: bool
) -> None:
    assert lifecycle.is_github_task(spec(repo, issue)) is expected


def test_the_local_prefix_is_the_one_of_intake() -> None:
    # The workflow cannot import intake, which pulls in SQLAlchemy, so it keeps its own copy.
    for repo in ("local:/x", "acme/calc", "local:"):
        assert lifecycle.is_github_task(spec(repo, 1)) is (not is_local(repo))


def test_the_label_names_are_the_ones_of_the_plan() -> None:
    assert lifecycle.WORKING == "longhaul:working"
    assert lifecycle.PR_OPEN == "longhaul:pr-open"
    assert lifecycle.NEEDS_HUMAN == "longhaul:needs-human"


def test_the_pickup_comment_is_on_the_issue_under_a_stable_key() -> None:
    comment = lifecycle.pickup_comment(spec())

    assert (comment.task_id, comment.repo, comment.number) == ("lh-acme-calc-42", "acme/calc", 42)
    assert comment.idempotency_key == effect_key("lh-acme-calc-42", "picked_up", 1)
    assert comment.body.startswith("Picked up")
    assert "lh-acme-calc-42" in comment.body
    assert not has_marker(comment.body), "the marker is added by post_comment"


def test_pickup_adds_the_working_label() -> None:
    (step,) = lifecycle.on_pickup(spec())

    assert step.add is True
    assert step.label.label == "longhaul:working"
    assert (step.label.repo, step.label.number) == ("acme/calc", 42)
    assert step.label.idempotency_key == effect_key("lh-acme-calc-42", "label_working", 1)


def test_a_pull_request_swaps_working_for_pr_open_in_that_order() -> None:
    steps = lifecycle.on_pr_opened(spec())

    assert [(s.add, s.label.label) for s in steps] == [
        (False, "longhaul:working"),
        (True, "longhaul:pr-open"),
    ]
    keys = [s.label.idempotency_key for s in steps]
    assert keys == [
        effect_key("lh-acme-calc-42", "label_working_off", 1),
        effect_key("lh-acme-calc-42", "label_pr_open", 1),
    ]


def test_a_pause_adds_needs_human_and_a_resume_removes_it() -> None:
    (pause,) = lifecycle.on_pause(spec(), 1)
    (resume,) = lifecycle.on_resume(spec(), 1)

    assert (pause.add, pause.label.label) == (True, "longhaul:needs-human")
    assert (resume.add, resume.label.label) == (False, "longhaul:needs-human")
    assert pause.label.idempotency_key == effect_key("lh-acme-calc-42", "label_needs_human_on", 1)
    assert resume.label.idempotency_key == effect_key("lh-acme-calc-42", "label_needs_human_off", 1)


def test_each_pause_has_a_key_of_its_own() -> None:
    first, second = lifecycle.on_pause(spec(), 1)[0], lifecycle.on_pause(spec(), 2)[0]
    assert first.label.idempotency_key != second.label.idempotency_key
    assert lifecycle.on_resume(spec(), 1)[0].label.idempotency_key != (
        lifecycle.on_resume(spec(), 2)[0].label.idempotency_key
    )


def test_every_key_is_distinct_and_valid() -> None:
    s = spec()
    steps: list[LabelStep] = [
        *lifecycle.on_pickup(s),
        *lifecycle.on_pr_opened(s),
        *lifecycle.on_pause(s, 1),
        *lifecycle.on_resume(s, 1),
    ]
    keys = [step.label.idempotency_key for step in steps] + [
        lifecycle.pickup_comment(s).idempotency_key
    ]

    assert len(set(keys)) == len(keys)
    # effect_key validates its parts, and the key built here is the same text.
    for key in keys:
        workflow_id, purpose, seq = key.split(":")
        assert effect_key(workflow_id, purpose, int(seq)) == key


def test_a_task_without_an_issue_has_nothing_to_label() -> None:
    with pytest.raises(ValueError, match="issue"):
        lifecycle.on_pickup(spec(issue=None))
    with pytest.raises(ValueError, match="issue"):
        lifecycle.pickup_comment(spec(issue=None))
