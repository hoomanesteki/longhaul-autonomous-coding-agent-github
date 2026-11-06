"""The plan comment (LH-501): the plan as a checklist, from templates/plan_comment.md.j2.

The comment is the one thing a person reads before the coder starts, and it is posted under an
idempotency key. A retried post_plan renders it again and sends it with the same key, which
github/side_effects.py refuses when the text differs, so the same plan has to give the same bytes.
"""

from pathlib import Path

import pytest

from longhaul.contracts import Plan, PlanStep
from longhaul.github import plan_comment
from longhaul.github.plan_comment import render_plan_comment

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"


def a_plan(**fields: object) -> Plan:
    values: dict[str, object] = {
        "summary": "add() subtracts, so change the operator and test it.",
        "steps": [
            PlanStep(
                id="s1",
                description="Change the operator in add()",
                files=["src/calc/__init__.py"],
                done_when="add(2, 3) returns 5",
            ),
            PlanStep(
                id="s2",
                description="Add a test for negative numbers",
                files=["tests/test_calc.py", "tests/conftest.py"],
                done_when="the new test passes",
            ),
        ],
        "acceptance_checks": ["pytest -q", "add(-2, -3) returns -5"],
        "risks": ["Other callers may rely on the old result."],
    }
    return Plan.model_validate({**values, **fields})


def render(plan: Plan | None = None, version: int = 1) -> str:
    return render_plan_comment(plan or a_plan(), version, templates=TEMPLATES)


def test_the_steps_are_a_checklist() -> None:
    text = render()
    assert "- [ ] **s1**: Change the operator in add()" in text
    assert "- [ ] **s2**: Add a test for negative numbers" in text
    assert text.index("**s1**") < text.index("**s2**")


def test_each_step_says_when_it_is_done_and_which_files_it_touches() -> None:
    text = render()
    assert "  - Done when: add(2, 3) returns 5" in text
    assert "  - Files: `src/calc/__init__.py`" in text
    assert "  - Files: `tests/test_calc.py`, `tests/conftest.py`" in text


def test_a_step_without_files_has_no_files_line() -> None:
    plan = a_plan(
        steps=[
            PlanStep(id="s1", description="Look around", done_when="the cause is known"),
            PlanStep(id="s2", description="Fix it", done_when="it is fixed"),
        ]
    )
    assert "Files:" not in render(plan)


def test_the_acceptance_checks_are_a_checklist_in_the_order_given() -> None:
    text = render()
    assert "- [ ] `pytest -q`" not in text  # a check is text, not necessarily a command
    assert "- [ ] pytest -q" in text
    assert text.index("- [ ] pytest -q") < text.index("- [ ] add(-2, -3) returns -5")
    assert text.index("**s2**") < text.index("pytest -q")


def test_the_comment_names_the_version_and_the_summary() -> None:
    text = render(version=2)
    assert text.startswith("## Plan, version 2\n")
    assert "add() subtracts, so change the operator and test it." in text


def test_risks_are_listed_when_there_are_some() -> None:
    text = render()
    assert "### Risks" in text
    assert "- Other callers may rely on the old result." in text


def test_a_plan_without_risks_has_no_risks_section() -> None:
    assert "Risks" not in render(a_plan(risks=[]))


def test_the_same_plan_gives_the_same_bytes() -> None:
    assert render().encode() == render(a_plan()).encode()
    assert render(version=1) != render(version=2)


def test_the_comment_ends_with_one_newline() -> None:
    text = render()
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_text_of_the_model_cannot_mention_a_person_or_close_an_issue() -> None:
    # The plan is model text on a page that people read. Mentions and issue references are
    # defused as in the pull request body.
    plan = a_plan(
        summary="Ping @alice and close #7 <script>",
        acceptance_checks=["ask @bob to check #8"],
    )
    text = render(plan)
    assert "@alice" not in text and "@\N{ZERO WIDTH SPACE}alice" in text
    assert "#7" not in text and "#\N{ZERO WIDTH SPACE}7" in text
    assert "@bob" not in text and "#8" not in text
    assert "<script>" not in text and "&lt;script>" in text


def test_a_step_stays_on_one_line_whatever_the_model_wrote() -> None:
    plan = a_plan(
        steps=[
            PlanStep(id="s1", description="First line\n\n## Second\n- [x] fake", done_when="a\nb"),
            PlanStep(id="s2", description="Two", done_when="done"),
        ]
    )
    text = render(plan)
    assert "- [ ] **s1**: First line ## Second - [x] fake" in text
    assert "  - Done when: a b" in text
    assert "\n## Second" not in text


def test_a_file_name_cannot_break_out_of_its_code_span() -> None:
    plan = a_plan(
        steps=[
            PlanStep(id="s1", description="One", files=["a`b.py\nc"], done_when="done"),
            PlanStep(id="s2", description="Two", done_when="done"),
        ]
    )
    text = render(plan)
    assert "  - Files: `a'b.py c`" in text


def test_a_missing_template_is_an_error_and_not_an_empty_comment(tmp_path: Path) -> None:
    from jinja2 import TemplateNotFound

    with pytest.raises(TemplateNotFound):
        render_plan_comment(a_plan(), 1, templates=tmp_path)


def test_the_template_is_the_one_named_in_the_story() -> None:
    assert plan_comment.TEMPLATE == "plan_comment.md.j2"
    assert (TEMPLATES / plan_comment.TEMPLATE).is_file()


def test_the_template_is_plain_ascii() -> None:
    assert (TEMPLATES / plan_comment.TEMPLATE).read_text(encoding="utf-8").isascii()
