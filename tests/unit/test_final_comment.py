"""The final comment (LH-507): one report on the issue when a task ends, from final_comment.md.j2.

Every task ends in one outcome and says so once. The text is the pull request link for a task
that delivered, and for any other end a report with what was done, what is left and the diff
stats. The comment is posted under the key `final:1`, and a retried post renders it again, so the
same inputs have to give the same bytes.
"""

from pathlib import Path

import pytest
from longhaul.github.final_comment import render_final_comment

from longhaul.checkpoints.squash import FileStat
from longhaul.contracts import Outcome, OutcomeKind, Plan, PlanStep
from longhaul.github import final_comment

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
PR_URL = "https://github.com/acme/calc/pull/7"
FILES = [FileStat("src/calc/__init__.py", 3, 1), FileStat("tests/test_calc.py", 4, 0)]


def a_plan(*statuses: str) -> Plan:
    steps = [
        PlanStep(
            id=f"s{n}",
            description=f"Step number {n}",
            done_when="the tests pass",
            status=status,  # type: ignore[arg-type]
        )
        for n, status in enumerate(statuses, start=1)
    ]
    return Plan(summary="Fix add().", steps=steps, acceptance_checks=["pytest -q"])


def outcome(kind: OutcomeKind, summary: str = "it ended", pr_url: str | None = None) -> Outcome:
    return Outcome(kind=kind, summary=summary, pr_url=pr_url)


def render(
    ended: Outcome,
    *,
    plan: Plan | None = None,
    files: list[FileStat] | None = None,
    draft: bool = True,
) -> str:
    return render_final_comment(
        ended, plan=plan, files=files, turns=12, cost_usd=0.4321, draft=draft, templates=TEMPLATES
    )


# ---------- a task that delivered ----------
def test_a_pull_request_is_linked_with_the_summary() -> None:
    text = render(
        outcome(OutcomeKind.PR_OPENED, "Changed add() to return a + b.", PR_URL), files=FILES
    )
    assert text.startswith("## Longhaul opened a draft pull request")
    assert PR_URL in text
    assert "Changed add() to return a + b." in text
    assert "2 files changed, 7 additions, 1 deletion" in text


def test_a_pull_request_that_is_ready_says_so() -> None:
    text = render(outcome(OutcomeKind.PR_OPENED, "Done.", PR_URL), draft=False)
    assert text.startswith("## Longhaul opened a pull request")
    assert "draft" not in text.lower()


def test_a_delivered_task_has_no_list_of_what_is_left() -> None:
    text = render(outcome(OutcomeKind.PR_OPENED, "Done.", PR_URL), plan=a_plan("done", "done"))
    assert "What is left" not in text


# ---------- a task that stopped ----------
def test_a_stuck_report_says_what_was_done_and_what_is_left() -> None:
    text = render(
        outcome(OutcomeKind.STUCK, "no hint in 24 hours"),
        plan=a_plan("done", "doing", "todo", "skipped"),
        files=FILES,
    )
    assert text.startswith("## Longhaul is stuck and has stopped")
    assert "no hint in 24 hours" in text
    done = text.split("### What was done", 1)[1].split("###", 1)[0]
    left = text.split("### What is left", 1)[1].split("###", 1)[0]
    assert "- [x] **s1**: Step number 1" in done
    assert "s2" not in done
    assert "- [ ] **s2**: Step number 2" in left
    assert "- [ ] **s3**: Step number 3" in left
    # A skipped step is not work that remains.
    assert "s4" not in left


def test_the_report_has_the_diff_stats() -> None:
    text = render(outcome(OutcomeKind.STUCK, "x"), plan=a_plan("todo"), files=FILES)
    changes = text.split("### Changes", 1)[1]
    assert "2 files changed, 7 additions, 1 deletion" in changes
    assert "- `src/calc/__init__.py`: +3 -1" in changes
    assert "- `tests/test_calc.py`: +4 -0" in changes


def test_a_task_that_changed_nothing_says_so() -> None:
    text = render(outcome(OutcomeKind.STUCK, "x"), plan=a_plan("todo"), files=[])
    assert "No files changed." in text


def test_stats_that_could_not_be_read_are_not_a_claim_of_no_change() -> None:
    text = render(outcome(OutcomeKind.ERROR, "x"), files=None)
    assert "No files changed." not in text
    assert "could not be read" in text


def test_a_task_without_a_plan_gets_the_stats_and_no_step_lists() -> None:
    text = render(outcome(OutcomeKind.STUCK, "no hint in 24 hours"), plan=None, files=FILES)
    assert "What was done" not in text
    assert "What is left" not in text
    assert "### Changes" in text


def test_a_partial_draft_pull_request_is_linked_in_the_report() -> None:
    text = render(
        outcome(OutcomeKind.STUCK, "no hint in 24 hours", PR_URL), plan=a_plan("done", "todo")
    )
    assert PR_URL in text
    assert "draft pull request" in text


def test_a_stuck_report_says_how_to_go_on() -> None:
    text = render(outcome(OutcomeKind.STUCK, "x"))
    assert "`/longhaul retry`" in text


def test_a_cancelled_task_says_who_stopped_it() -> None:
    text = render(outcome(OutcomeKind.CANCELLED, "cancelled by alice: not needed"))
    assert text.startswith("## Longhaul stopped: the task was cancelled")
    assert "cancelled by alice: not needed" in text


def test_an_error_names_the_summary_and_points_to_the_log() -> None:
    text = render(outcome(OutcomeKind.ERROR, "workflow failed: ApplicationError"))
    assert text.startswith("## Longhaul hit an error and stopped")
    assert "workflow failed: ApplicationError" in text
    assert "events" in text


def test_a_spent_budget_is_named() -> None:
    text = render(outcome(OutcomeKind.BUDGET_EXHAUSTED, "task budget of $5 is spent"))
    assert text.startswith("## Longhaul stopped: the budget is spent")


def test_the_report_says_how_long_it_ran_and_what_it_cost() -> None:
    text = render(outcome(OutcomeKind.STUCK, "x"))
    assert "12 turns" in text
    assert "$0.43" in text


# ---------- what the comment promises ----------
def test_the_same_inputs_give_the_same_bytes() -> None:
    ended = outcome(OutcomeKind.STUCK, "no hint in 24 hours")
    assert render(ended, plan=a_plan("done", "todo"), files=FILES) == render(
        ended, plan=a_plan("done", "todo"), files=FILES
    )


def test_text_from_the_model_cannot_mention_a_person_or_close_an_issue() -> None:
    plan = Plan(
        summary="x",
        steps=[PlanStep(id="s1", description="ping @alice, fixes #9", done_when="x")],
        acceptance_checks=[],
    )
    text = render(outcome(OutcomeKind.PR_OPENED, "Closes #9, thanks @bob <b>", PR_URL), plan=plan)
    assert "@alice" not in text
    assert "@bob" not in text
    assert "#9" not in text
    assert "<b>" not in text


def test_a_summary_with_line_breaks_stays_one_paragraph() -> None:
    text = render(outcome(OutcomeKind.STUCK, "line one\n\n# a heading\n- a bullet"))
    assert "\n# a heading" not in text
    assert "line one # a heading - a bullet" in text


def test_a_long_summary_is_cut() -> None:
    text = render(outcome(OutcomeKind.ERROR, "word " * 5000))
    assert len(text) < final_comment.MAX_SUMMARY_CHARS + 2000
    assert "..." in text


def test_a_long_plan_and_a_long_file_list_stay_short() -> None:
    many = [FileStat(f"src/file_{n}.py", 1, 1) for n in range(60)]
    text = render(outcome(OutcomeKind.STUCK, "x"), plan=a_plan(*["todo"] * 8), files=many)
    assert "60 files changed" in text
    assert "and 50 more files" in text
    assert text.count("\n- `src/file_") == final_comment.MAX_FILES


def test_a_file_name_with_a_backtick_stays_in_its_code_span() -> None:
    text = render(outcome(OutcomeKind.STUCK, "x"), files=[FileStat("a`b\nc.py", 1, 0)])
    assert "- `a'b c.py`: +1 -0" in text


def test_a_binary_file_has_no_counts() -> None:
    text = render(outcome(OutcomeKind.STUCK, "x"), files=[FileStat("logo.png", None, None)])
    assert "- `logo.png`: binary" in text
