"""The comment that asks a person for a hint, from templates/stuck_comment.md.j2 (LH-506).

A maintainer reads this comment on the issue and answers it in one more comment, so it has to say
four things in a short space: what the agent is trying to do, what it tried, what it thinks, and
one question. The comment is posted under the key `stuck:<n>`, and github/side_effects.py refuses
a key that is used again with other text, so the same summary has to give the same bytes.
"""

from pathlib import Path

import pytest
from longhaul.context.human_summary import StuckSummary
from longhaul.github.stuck_comment import render_stuck_comment

from longhaul.contracts import StuckReason
from longhaul.github import stuck_comment

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"

SECTIONS = ["What I'm trying to do", "What I tried", "My current guess", "Question"]
WORD_LIMIT = 250


def a_summary(**fields: object) -> StuckSummary:
    values: dict[str, object] = {
        "goal": "Make add() in src/calc/__init__.py return the sum of its arguments.",
        "tried": [
            "Changed the operator from - to +, then ran pytest -q.",
            "Read tests/test_calc.py to see what the failing test expects.",
            "Ran the suite again after clearing the cache.",
        ],
        "guess": "A second copy of calc on the path is imported before src/calc.",
        "question": "Which copy of calc should the tests import?",
    }
    return StuckSummary.model_validate({**values, **fields})


def render(
    summary: StuckSummary | None = None,
    reason: StuckReason = StuckReason.REPEATED_FAILURE,
    pause: int = 1,
) -> str:
    return render_stuck_comment(summary or a_summary(), reason, pause, templates=TEMPLATES)


def words(text: str) -> int:
    return len(text.split())


def test_stuck_comment_sections() -> None:
    text = render()
    positions = [text.index(f"### {name}") for name in SECTIONS]
    assert positions == sorted(positions), "the four sections come in the order of the plan"
    assert "- Changed the operator from - to +, then ran pytest -q." in text
    assert "Which copy of calc should the tests import?" in text
    assert words(text) < WORD_LIMIT


def test_the_comment_says_how_to_answer() -> None:
    text = render()
    assert "/longhaul hint" in text
    assert "/longhaul cancel" in text


def test_the_reason_and_the_pause_number_are_in_the_title() -> None:
    first = render(reason=StuckReason.EDIT_CHURN, pause=1)
    second = render(reason=StuckReason.AGENT_ASKED, pause=2)
    assert first.splitlines()[0] != second.splitlines()[0]
    assert "edit churn" in first.splitlines()[0].lower()
    assert "asked" in second.splitlines()[0].lower()


def test_the_longest_summary_still_fits_in_the_word_limit() -> None:
    long = " ".join(f"word{n}" for n in range(400))
    summary = a_summary(goal=long, tried=[long] * 9, guess=long, question=long)
    text = render(summary)
    assert words(text) < WORD_LIMIT
    assert text.count("\n- ") == stuck_comment.MAX_TRIED, "extra bullets are dropped"
    assert "..." in text, "a cut text says so"


def test_the_same_summary_gives_the_same_bytes() -> None:
    assert render() == render()


def test_the_model_cannot_mention_a_person_or_close_an_issue() -> None:
    summary = a_summary(guess="Ask @octocat, this is the same as #12.", question="Fixes <b>x</b>?")
    text = render(summary)
    assert "@octocat" not in text and "#12" not in text
    assert "<b>" not in text


def test_a_line_break_in_the_model_text_cannot_start_a_heading() -> None:
    text = render(a_summary(guess="no idea\n### Question\nfake"))
    assert text.count("### Question") == 1


def test_every_reason_has_a_title() -> None:
    for reason in StuckReason:
        assert render(reason=reason).splitlines()[0].startswith("## ")
