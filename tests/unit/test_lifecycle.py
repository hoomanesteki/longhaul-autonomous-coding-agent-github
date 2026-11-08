"""The labels and the pickup comment of a GitHub task (LH-306).

These helpers are pure: they say what to write and under which key. The workflow runs the steps
as activities, and tests/integration/test_deliver.py::test_label_transitions runs them against
FakeGitHub.
"""

import pytest

from longhaul.contracts import Outcome, OutcomeKind, Phase, RepoSettings, TaskSpec, TaskStatus
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


# --- the comments of the commands (LH-308) -----------------------------------------------------


def snapshot(**fields: object) -> TaskStatus:
    values: dict[str, object] = {
        "task_id": "lh-acme-calc-42",
        "phase": Phase.CODING,
        "turn_index": 7,
        "cost_usd": 0.01234,
        "last_checkpoint": "a" * 40,
        "plan_version": None,
    }
    return TaskStatus.model_validate({**values, **fields})


def test_a_status_comment_gives_the_phase_the_turn_the_spend_and_the_checkpoint() -> None:
    comment = lifecycle.status_comment(spec(), snapshot(), by="maintainer", n=1)

    assert (comment.task_id, comment.repo, comment.number) == ("lh-acme-calc-42", "acme/calc", 42)
    assert "lh-acme-calc-42" in comment.body
    assert "coding" in comment.body
    assert "turn 7" in comment.body
    assert "$0.0123" in comment.body
    assert "aaaaaaa" in comment.body and "a" * 12 not in comment.body, "a short checkpoint"
    assert not has_marker(comment.body), "the marker is added by post_comment"


def test_a_status_comment_names_who_asked_without_notifying_them() -> None:
    comment = lifecycle.status_comment(spec(), snapshot(), by="maintainer", n=1)

    assert "`maintainer`" in comment.body
    assert "@maintainer" not in comment.body


def test_a_status_comment_before_the_first_checkpoint_says_so() -> None:
    comment = lifecycle.status_comment(spec(), snapshot(last_checkpoint=None), by="m", n=1)

    assert "no checkpoint yet" in comment.body


def test_a_status_comment_of_an_ended_task_gives_the_outcome() -> None:
    outcome = Outcome(kind=OutcomeKind.PR_OPENED, pr_url="https://example.test/p/1", summary="done")

    comment = lifecycle.status_comment(
        spec(), snapshot(phase=Phase.DONE, outcome=outcome), by="m", n=1
    )

    assert "pr_opened" in comment.body


def test_each_status_request_has_a_key_of_its_own() -> None:
    first = lifecycle.status_comment(spec(), snapshot(), by="m", n=1)
    second = lifecycle.status_comment(spec(), snapshot(), by="m", n=2)

    assert first.idempotency_key == effect_key("lh-acme-calc-42", "status", 1)
    assert second.idempotency_key == effect_key("lh-acme-calc-42", "status", 2)


def test_a_denial_is_the_polite_sentence_on_the_issue() -> None:
    comment = lifecycle.denied_comment("lh-acme-calc-42", "acme/calc", 42, "Stranger")

    assert (comment.task_id, comment.repo, comment.number) == ("lh-acme-calc-42", "acme/calc", 42)
    assert "Only maintainers can steer this task" in comment.body
    assert "@Stranger" in comment.body
    assert not has_marker(comment.body)


def test_a_denial_has_one_key_for_each_user_and_task() -> None:
    def key(task: str, login: str) -> str:
        return lifecycle.denied_comment(task, "acme/calc", 42, login).idempotency_key

    assert key("lh-acme-calc-42", "Stranger") == key("lh-acme-calc-42", "stranger")
    assert key("lh-acme-calc-42", "stranger") != key("lh-acme-calc-42", "other")
    assert key("lh-acme-calc-42", "stranger") != key("lh-acme-calc-42-a2", "stranger")
    assert key("lh-acme-calc-42", "a-b") != key("lh-acme-calc-42", "a_b")


@pytest.mark.parametrize(
    "login", ["octo-cat", "OctoCat", "x", "a" * 39, "app[bot]", "we ird/na:me"]
)
def test_whatever_the_login_the_denial_key_is_a_valid_key(login: str) -> None:
    key = lifecycle.denied_comment("lh-acme-calc-42", "acme/calc", 42, login).idempotency_key

    workflow_id, purpose, seq = key.split(":")
    assert effect_key(workflow_id, purpose, int(seq)) == key


# ---------- LH-502: the plan gate ----------
def test_waiting_for_a_plan_decision_adds_needs_human_and_deciding_removes_it() -> None:
    (waiting,) = lifecycle.on_plan_review(spec(), 1)
    (decided,) = lifecycle.on_plan_decided(spec(), 1)

    assert (waiting.add, waiting.label.label) == (True, "longhaul:needs-human")
    assert (decided.add, decided.label.label) == (False, "longhaul:needs-human")
    assert (waiting.label.number, decided.label.number) == (42, 42)


def test_the_plan_gate_keys_cannot_meet_the_keys_of_a_pause() -> None:
    # A later pause counts from 1 and adds the same label. If its key matched the one of the
    # first plan review, the ledger would call the write done and the label would never be set.
    s = spec()
    gate = [*lifecycle.on_plan_review(s, 1), *lifecycle.on_plan_decided(s, 1)]
    pause = [*lifecycle.on_pause(s, 1), *lifecycle.on_resume(s, 1)]
    gate_keys = {step.label.idempotency_key for step in gate}
    pause_keys = {step.label.idempotency_key for step in pause}
    assert gate_keys.isdisjoint(pause_keys)


def test_each_version_of_the_plan_has_review_keys_of_its_own() -> None:
    s = spec()
    first, second = lifecycle.on_plan_review(s, 1)[0], lifecycle.on_plan_review(s, 2)[0]
    assert first.label.idempotency_key == effect_key("lh-acme-calc-42", "label_plan_review_on", 1)
    assert first.label.idempotency_key != second.label.idempotency_key
    assert lifecycle.on_plan_decided(s, 1)[0].label.idempotency_key == effect_key(
        "lh-acme-calc-42", "label_plan_review_off", 1
    )
    assert lifecycle.on_plan_decided(s, 1)[0].label.idempotency_key != (
        lifecycle.on_plan_decided(s, 2)[0].label.idempotency_key
    )


def test_the_review_comment_tells_the_maintainer_how_to_answer() -> None:
    comment = lifecycle.plan_review_comment(spec(), 2, hours=24)

    assert (comment.task_id, comment.repo, comment.number) == ("lh-acme-calc-42", "acme/calc", 42)
    assert "version 2" in comment.body
    assert "/longhaul approve" in comment.body
    assert "/longhaul reject <feedback>" in comment.body
    assert "24 hours" in comment.body
    assert not has_marker(comment.body)
    assert comment.idempotency_key == effect_key("lh-acme-calc-42", "plan_review", 2)


def test_the_review_comment_does_not_start_with_a_command() -> None:
    # The poller reads the first line of a comment. A comment of ours has the marker and is
    # dropped anyway, but the text must not be able to pass for an order if it is quoted.
    body = lifecycle.plan_review_comment(spec(), 1, hours=24).body
    assert not body.lstrip().lower().startswith("/longhaul")


def test_every_plan_gate_key_is_valid_and_distinct() -> None:
    s = spec()
    keys = [
        *(step.label.idempotency_key for step in lifecycle.on_plan_review(s, 1)),
        *(step.label.idempotency_key for step in lifecycle.on_plan_decided(s, 1)),
        lifecycle.plan_review_comment(s, 1, hours=24).idempotency_key,
    ]
    assert len(set(keys)) == len(keys)
    for key in keys:
        workflow_id, purpose, seq = key.split(":")
        assert effect_key(workflow_id, purpose, int(seq)) == key


# ---------- LH-506: the pause for a hint ----------
def test_each_pause_has_a_question_key_of_its_own() -> None:
    first = lifecycle.stuck_comment(spec(), 1, "the question")
    second = lifecycle.stuck_comment(spec(), 2, "the question")

    assert (first.task_id, first.repo, first.number) == ("lh-acme-calc-42", "acme/calc", 42)
    assert first.body == "the question"
    assert first.idempotency_key == effect_key("lh-acme-calc-42", "stuck", 1)
    assert second.idempotency_key == effect_key("lh-acme-calc-42", "stuck", 2)
    assert not has_marker(first.body), "the marker is added by post_comment"


# ---------- LH-507: the labels a task ends with ----------
def ending(kind: OutcomeKind, *, draft: bool = False) -> list[tuple[bool, str]]:
    steps = lifecycle.on_end(spec(), kind, pr_opened=draft)
    return [(step.add, step.label.label) for step in steps]


def test_a_task_that_opened_its_pull_request_has_its_labels_already() -> None:
    # on_pr_opened ran when the pull request opened, with the same keys.
    assert ending(OutcomeKind.PR_OPENED) == []


@pytest.mark.parametrize("kind", [OutcomeKind.STUCK, OutcomeKind.BUDGET_EXHAUSTED])
def test_a_task_that_gave_up_asks_for_a_person(kind: OutcomeKind) -> None:
    assert ending(kind) == [(False, lifecycle.WORKING), (True, lifecycle.NEEDS_HUMAN)]


def test_a_stuck_task_with_a_partial_draft_also_shows_the_pull_request() -> None:
    assert ending(OutcomeKind.STUCK, draft=True) == [
        (False, lifecycle.WORKING),
        (True, lifecycle.NEEDS_HUMAN),
        (True, lifecycle.PR_OPEN),
    ]


def test_a_task_that_failed_asks_for_a_person() -> None:
    assert ending(OutcomeKind.ERROR) == [(False, lifecycle.WORKING), (True, lifecycle.NEEDS_HUMAN)]


def test_a_cancelled_task_asks_for_nobody() -> None:
    # A task that was paused when it was cancelled still carries needs-human.
    assert ending(OutcomeKind.CANCELLED) == [
        (False, lifecycle.WORKING),
        (False, lifecycle.NEEDS_HUMAN),
    ]


def test_the_working_label_comes_off_with_the_key_it_always_had() -> None:
    # The label was taken off when the pull request opened, so a task that ends later asks the
    # ledger for the same write and gets the answer it recorded.
    (first, *_) = lifecycle.on_end(spec(), OutcomeKind.STUCK, pr_opened=False)
    (swap, *_) = lifecycle.on_pr_opened(spec())
    assert first.label.idempotency_key == swap.label.idempotency_key


def test_every_ending_label_key_is_valid_and_distinct() -> None:
    keys = [
        step.label.idempotency_key
        for kind in OutcomeKind
        for step in lifecycle.on_end(spec(), kind, pr_opened=True)
    ]
    for key in set(keys):
        workflow_id, purpose, seq = key.split(":")
        assert effect_key(workflow_id, purpose, int(seq)) == key
    # needs-human on and off must not share a key, or the second write would be skipped.
    on = lifecycle.on_end(spec(), OutcomeKind.STUCK, pr_opened=False)[1].label.idempotency_key
    off = lifecycle.on_end(spec(), OutcomeKind.CANCELLED, pr_opened=False)[1].label.idempotency_key
    assert on != off


def test_the_final_comment_is_on_the_issue_under_one_key() -> None:
    comment = lifecycle.final_comment(spec(), "the report")

    assert (comment.task_id, comment.repo, comment.number) == ("lh-acme-calc-42", "acme/calc", 42)
    assert comment.body == "the report"
    assert comment.idempotency_key == effect_key("lh-acme-calc-42", "final", 1)
