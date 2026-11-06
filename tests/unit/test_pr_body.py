"""The pull request body: what it says, and that it says the same thing every time (LH-306).

The body comes from templates/pr_body.md.j2. The create_pr call refuses a reused key with other
text (github/side_effects.py), and a retried deliver renders the body again, so the rendering
may not read a clock or anything else that changes between attempts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from longhaul.checkpoints.squash import FileStat
from longhaul.github.pr_body import (
    MAX_FILES,
    MAX_SUMMARY_CHARS,
    LastTestRun,
    defuse,
    render_pr_body,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase3]

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
ZWSP = "\N{ZERO WIDTH SPACE}"
PASSED = LastTestRun(summary="3 passed", exit_code=0)


def render(
    summary: str = "Fixed add().",
    files: list[FileStat] | None = None,
    tests: LastTestRun | None = PASSED,
    issue: int = 42,
) -> str:
    stats = files if files is not None else [FileStat("src/calc.py", 1, 1)]
    return render_pr_body(issue, summary, stats, tests, templates=TEMPLATES)


def test_the_template_is_in_the_repo() -> None:
    assert (TEMPLATES / "pr_body.md.j2").is_file()


def test_the_body_has_a_summary_the_changes_the_tests_and_the_closing_line() -> None:
    body = render()

    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings == ["## Summary", "## Changes", "## How it was tested"]
    assert "Fixed add()." in body
    assert "- `src/calc.py`: +1 -1" in body
    assert "3 passed" in body
    assert body.endswith("\n")
    assert body.rstrip().splitlines()[-1] == "Closes #42"


def test_the_body_is_the_same_bytes_every_time() -> None:
    assert render() == render()
    assert render(summary="x" * 100) == render(summary="x" * 100)


def test_the_changes_say_how_many_files_and_lines() -> None:
    body = render(files=[FileStat("a.py", 10, 2), FileStat("b.py", 0, 5)])

    assert "2 files changed, 10 additions, 7 deletions" in body


def test_one_file_is_not_plural() -> None:
    assert "1 file changed, 1 addition, 1 deletion" in render()


def test_a_binary_file_is_listed_without_counts() -> None:
    body = render(files=[FileStat("logo.png", None, None)])

    assert "- `logo.png`: binary" in body
    assert "1 file changed" in body


def test_a_change_with_no_files_says_so() -> None:
    assert "No files changed." in render(files=[])


def test_a_long_list_of_files_is_cut_and_says_how_many_are_left() -> None:
    files = [FileStat(f"f{n:03d}.py", 1, 0) for n in range(MAX_FILES + 7)]

    body = render(files=files)

    assert f"`f{MAX_FILES - 1:03d}.py`" in body
    assert f"`f{MAX_FILES:03d}.py`" not in body
    assert "and 7 more files" in body
    assert f"{MAX_FILES + 7} files changed" in body


def test_a_file_name_cannot_break_out_of_its_code_span() -> None:
    body = render(files=[FileStat("a`b\nc|d\x00.py", 1, 0)])

    (line,) = [line for line in body.splitlines() if line.startswith("- `")]
    assert line == "- `a'b c|d .py`: +1 -0"


def test_a_very_long_file_name_is_cut() -> None:
    body = render(files=[FileStat("d/" * 300 + "x.py", 1, 0)])

    (line,) = [line for line in body.splitlines() if line.startswith("- `")]
    assert len(line) < 260
    assert "..." in line


@pytest.mark.parametrize(
    ("tests", "expected"),
    [
        (LastTestRun(summary="2 failed, 3 passed", exit_code=1), "2 failed, 3 passed"),
        (LastTestRun(summary=None, exit_code=0), "no pytest summary line"),
        (LastTestRun(summary=None, exit_code=None, ran=False), "could not run its tests"),
        (None, "No test run was recorded"),
    ],
)
def test_the_test_line_follows_the_last_run(tests: LastTestRun | None, expected: str) -> None:
    assert expected in render(tests=tests)


def test_the_exit_code_is_part_of_the_test_line() -> None:
    assert "exit code 1" in render(tests=LastTestRun(summary="1 failed", exit_code=1))


# ---------- text the model wrote ----------
def test_a_mention_in_the_summary_does_not_notify_anyone() -> None:
    body = render(summary="Thanks @octocat and @acme/team for the report.")

    assert f"@{ZWSP}octocat" in body
    assert f"@{ZWSP}acme/team" in body
    assert "@octocat" not in body


def test_an_issue_reference_in_the_summary_cannot_close_another_issue() -> None:
    body = render(summary="This also closes #7 and fixes acme/other#9.", issue=4)

    assert f"#{ZWSP}7" in body
    assert f"#{ZWSP}9" in body
    assert "#7" not in body
    assert "#9" not in body
    # The only closing keyword that GitHub can read is the line that the template wrote.
    assert re.findall(r"(?i)\bcloses\s+#\d+", body) == ["Closes #4"]


def test_a_gh_reference_in_the_summary_cannot_close_another_issue() -> None:
    # GitHub reads "GH-7" as a reference to issue 7 in the same repo, so "fixes GH-7" closes it.
    body = render(summary="This also fixes GH-7 and gh-8.", issue=4)

    assert f"GH-{ZWSP}7" in body
    assert f"gh-{ZWSP}8" in body
    assert not re.search(r"(?i)\bGH-\d", body)


def test_a_link_to_an_issue_in_the_summary_cannot_close_it() -> None:
    body = render(summary="Resolves https://github.com/acme/other/issues/9")

    assert f"https://{ZWSP}github.com/acme/other/issues/9" in body
    assert "://github" not in body


def test_html_in_the_summary_is_shown_as_text() -> None:
    body = render(summary="<!-- hidden --> <img src=x onerror=y>")

    assert "<!--" not in body
    assert "<img" not in body
    assert "&lt;!-- hidden -->" in body


def test_a_long_summary_is_cut_and_says_so() -> None:
    body = render(summary="word " * 5000)

    summary = body.split("## Changes")[0]
    assert len(summary) < MAX_SUMMARY_CHARS + 200
    assert "(summary cut)" in summary


def test_defuse_leaves_ordinary_text_alone() -> None:
    text = "Changed add() to return a + b; mail a@b.com about v1.2 (section 3) and C# code."
    assert defuse(text) == text
    assert defuse("A GH-style gate and a GHz clock") == "A GH-style gate and a GHz clock"


def test_defuse_ends_each_kind_of_reference() -> None:
    assert defuse("closes #7 fixes o/r#8 resolves GH-9 see @alice at https://x.dev") == (
        f"closes #{ZWSP}7 fixes o/r#{ZWSP}8 resolves GH-{ZWSP}9 see @{ZWSP}alice "
        f"at https://{ZWSP}x.dev"
    )
