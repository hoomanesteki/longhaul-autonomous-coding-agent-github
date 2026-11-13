"""The launch docs of LH-907: the README, the architecture page, the runbook, the case study and
the demo script, and the `make demo` target that the quickstart ends on.

These checks keep the pages true to the repo. The README quotes the chaos results, so a test reads
the committed summary and looks for the same numbers. The runbook lists the workflow patch
markers, so a test reads them from the workflow code and fails when a new marker has no entry.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
DOCS = ROOT / "docs"
CHAOS_SUMMARY = ROOT / "eval" / "results" / "chaos-2026-11-07" / "summary.json"

# Section 11.1 of the plan, in order. Extra sections may sit between these, none may be missing.
README_SECTIONS = [
    "Headline numbers",
    "Demo",
    "How it works",
    "Quickstart",
    "Run on your repo",
    "Results",
    "Design decisions",
    "Safety",
    "Limitations",
    "License",
]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def headings(text: str, level: int = 2) -> list[str]:
    """The headings of one level, leaving out lines inside a code fence."""
    found: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#" * level + " "):
            found.append(line[level + 1 :].strip())
    return found


def section(text: str, title: str, level: int = 2) -> str:
    """The body of the section with this heading, up to the next heading of the same level."""
    marker = "#" * level + " "
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"{marker}{title}")
    body: list[str] = []
    fenced = False
    for line in lines[start + 1 :]:
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith(marker):
            break
        body.append(line)
    return "\n".join(body)


def mermaid_blocks(text: str) -> list[str]:
    return re.findall(r"```mermaid\n(.*?)```", text, flags=re.DOTALL)


# ---------- README ----------
def test_readme_follows_the_launch_outline_in_order() -> None:
    found = headings(read(README))
    positions = [found.index(title) for title in README_SECTIONS]
    assert positions == sorted(positions), found


def test_readme_title_status_and_site_link_stay_near_the_top() -> None:
    text = read(README)
    top = text.splitlines()[:12]
    assert top[0] == "# Longhaul"
    status = next(line for line in top if line.startswith("Status:"))
    # Phase 0 keeps two stories that wait on the owner on GitHub, so the status names them.
    assert "phases 1 to 9 are done" in status and "LH-003" in status and "LH-006" in status
    assert "v1.0.0" in status
    assert any("actions/workflows/ci.yml/badge.svg" in line for line in top)


def test_readme_quickstart_is_three_commands_with_no_key() -> None:
    quickstart = section(read(README), "Quickstart")
    assert "make setup && make up && make demo" in quickstart
    assert "ANTHROPIC_API_KEY" not in quickstart
    assert "scripted" in quickstart


def test_readme_embeds_the_gif_only_once_the_file_exists() -> None:
    # Before the recording the section names the path and embeds nothing. The recording checklist
    # tells the owner to save the file and add the image, and this check must accept that step.
    text = read(README)
    demo = section(text, "Demo")
    assert "docs/media/demo.gif" in text
    if (ROOT / "docs" / "media" / "demo.gif").exists():
        assert "![" in demo
        assert "docs/media/demo.gif" in demo.split("![", 1)[1]
    else:
        assert "![" not in demo


def test_readme_diagram_is_mermaid_with_the_four_ideas_beside_it() -> None:
    how = section(read(README), "How it works")
    assert len(mermaid_blocks(how)) == 1
    bullets = [line for line in how.splitlines() if line.startswith("- ")]
    assert len(bullets) == 4


def test_readme_run_on_your_repo_uses_commands_the_cli_has() -> None:
    body = section(read(README), "Run on your repo")
    assert "longhaul poller install" in body
    assert "longhaul.yaml" in body
    for scope in ("Contents", "Pull requests", "Issues"):
        assert scope in body


def test_readme_links_every_adr_with_one_line_each() -> None:
    decisions = section(read(README), "Design decisions")
    for adr in sorted((DOCS / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")):
        if adr.name.startswith("0000-"):
            continue
        assert f"docs/adr/{adr.name}" in decisions, adr.name


def test_readme_relative_links_point_at_files_that_exist() -> None:
    missing = []
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", read(README)):
        if "://" in target or target.startswith("mailto:"):
            continue
        if not (ROOT / target).exists():
            missing.append(target)
    assert not missing, missing


def readme_images(text: str) -> list[str]:
    """The images of a page: Markdown ones and the src of an HTML img tag."""
    markdown = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text)
    return markdown + re.findall(r'<img [^>]*src="([^"]+)"', text)


def test_readme_images_are_files_in_the_repo() -> None:
    images = readme_images(read(README))
    # The chaos chart and the console screenshots.
    assert "docs/media/chaos-results.svg" in images
    assert any(image.startswith("docs/gates/media/") for image in images)
    for image in images:
        if "actions/workflows/ci.yml/badge.svg" in image:
            continue  # the CI badge is the one image GitHub serves
        assert "://" not in image, image
        assert (ROOT / image).is_file(), image


def test_readme_shows_the_console_with_its_scripted_costs() -> None:
    console = section(read(README), "The console")
    assert "scripted provider" in console
    assert "$0.00" in console


def test_readme_shows_the_chaos_chart_next_to_the_headline_numbers() -> None:
    headline = section(read(README), "Headline numbers")
    assert "](docs/media/chaos-results.svg)" in headline
    assert "scripts/chaos_chart.py" in headline


def test_readme_docs_map_links_every_doc_with_a_line_each() -> None:
    docs_map = section(read(README), "Docs map")
    rows = [line for line in docs_map.splitlines() if line.startswith("| [")]
    for doc in (
        "docs/case-study.md",
        "docs/architecture.md",
        "docs/adr/",
        "docs/results.md",
        "docs/gates/",
        "docs/runbook.md",
        "docs/security.md",
        "docs/deploy-aws.md",
        "docs/demo-script.md",
        "docs/plan/longhaul-build-plan.md",
        "docs/progress.md",
    ):
        (row,) = [line for line in rows if f"]({doc})" in line]
        assert row.rstrip(" |").split(" | ")[-1].strip(), doc


def test_readme_says_what_is_not_made_yet_and_why() -> None:
    text = read(README)
    headline = section(text, "Headline numbers")
    assert "API key" in headline and "eval VM" in headline
    demo = section(text, "Demo")
    assert "recorded yet" in demo and "API key" in demo
    status = text.splitlines()[6]
    assert (
        "Docs site:" in status and "hoomanesteki.github.io" in status and "not live" not in status
    )


def test_readme_quotes_the_chaos_results_as_the_summary_has_them() -> None:
    summary = json.loads(read(CHAOS_SUMMARY))
    text = read(README)
    assert f"{summary['completed']} of {summary['runs']} runs" in text
    assert f"{summary['duplicate_side_effects']} duplicate" in text
    assert f"{summary['missing_side_effects']} missing" in text
    assert f"{summary['survived']} of {summary['runs']}" in text
    kills_landed = sum(summary["kills_landed"].values())
    kills_planned = sum(summary["kills_planned"].values())
    assert f"{kills_landed} / {kills_planned}" in text
    # The recordings were scripted, and the README says so next to the numbers.
    assert set(summary["recordings"].values()) == {"scripted"}
    assert "scripted" in section(text, "Headline numbers")


KILL_TYPES = {
    "worker": "Worker SIGKILL",
    "sandbox": "Sandbox kill",
    "temporal": "Temporal restart",
    "postgres": "Postgres restart",
}


def seconds(value: float) -> str:
    return f"{value:.1f} s"


def chaos_table_rows(summary: dict[str, object], *, runs_hit: bool) -> list[str]:
    """The rows of the per-kill-type table, built from the summary and its per-run results.

    The README table has a "Runs hit" column and the case study table does not.
    """
    results = summary["results"]
    assert isinstance(results, list)
    rows: list[str] = []
    for kind, label in KILL_TYPES.items():
        hit = [r for r in results if any(k["type"] == kind and k["landed"] for k in r["kills"])]
        landed = summary["kills_landed"][kind]  # type: ignore[index]
        planned = summary["kills_planned"][kind]  # type: ignore[index]
        latency = summary["resume_latency_by_type"][kind]  # type: ignore[index]
        cells = [label, f"{landed} / {planned}"]
        if runs_hit:
            cells.append(str(len(hit)))
        cells += [
            f"{sum(1 for r in hit if r['completed'])} / {len(hit)}",
            f"{sum(1 for r in hit if r['diff_equals_reference'])} / {len(hit)}",
            str(sum(r["duplicate_side_effects"] for r in hit)),
            f"{seconds(latency['median_s'])} / {seconds(latency['p95_s'])}",
        ]
        rows.append("| " + " | ".join(cells) + " |")
    total = summary["runs"]
    # A run counts as hit when at least one of its kills landed, as in the per-type rows.
    hit_any = [r for r in results if any(k["landed"] for k in r["kills"])]
    overall = summary["resume_latency"]
    cells = [
        "All",
        f"{sum(summary['kills_landed'].values())} / {sum(summary['kills_planned'].values())}",  # type: ignore[attr-defined]
    ]
    if runs_hit:
        cells.append(str(len(hit_any)))
    cells += [
        f"{summary['completed']} / {total}",
        f"{summary['diff_matches']} / {total}",
        str(summary["duplicate_side_effects"]),
        f"{seconds(overall['median_s'])} / {seconds(overall['p95_s'])}",  # type: ignore[index]
    ]
    rows.append("| " + " | ".join(cells) + " |")
    return rows


def table_lines(text: str) -> list[str]:
    """The table rows of a page with the bold marks removed, so a bold total row compares equal."""
    return [line.replace("**", "") for line in text.splitlines() if line.startswith("|")]


def test_readme_chaos_table_matches_the_summary_row_by_row() -> None:
    summary = json.loads(read(CHAOS_SUMMARY))
    lines = table_lines(section(read(README), "Results"))
    for row in chaos_table_rows(summary, runs_hit=True):
        assert row in lines, row


def test_case_study_chaos_table_matches_the_summary_row_by_row() -> None:
    summary = json.loads(read(CHAOS_SUMMARY))
    lines = table_lines(section(read(DOCS / "case-study.md"), "Proof"))
    for row in chaos_table_rows(summary, runs_hit=False):
        assert row in lines, row


def test_readme_says_where_the_eval_numbers_will_come_from() -> None:
    headline = section(read(README), "Headline numbers")
    assert "docs/results.md" in headline
    assert "pending" in headline


# ---------- make demo ----------
def make_target(name: str) -> tuple[list[str], str]:
    """(prerequisites, recipe) of a Makefile target."""
    text = read(ROOT / "Makefile")
    found = re.search(rf"^{name}:([^\n]*)\n((?:\t[^\n]*\n|\n(?=\t))*)", text, flags=re.MULTILINE)
    assert found, f"no target {name}"
    return found.group(1).split(), found.group(2)


def test_make_demo_builds_what_it_needs_and_runs_the_script() -> None:
    prerequisites, recipe = make_target("demo")
    assert {"sandbox-image", "migrate"} <= set(prerequisites)
    assert "scripts/demo.sh" in recipe
    assert "demo" in read(ROOT / "Makefile").split(".PHONY:")[1].splitlines()[0].split()


def test_make_setup_makes_the_env_file_a_fresh_clone_needs() -> None:
    # The database URL has no built-in default, so `make migrate` needs a .env on a fresh clone.
    _, recipe = make_target("setup")
    assert ".env.example" in recipe
    assert "test -f .env" in recipe or "[ -f .env ]" in recipe


def test_demo_script_uses_the_pattern_of_the_chaos_script() -> None:
    path = ROOT / "scripts" / "demo.sh"
    text = read(path)
    assert os.access(path, os.X_OK)
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    # `uv run` swallows signals, so the worker is the console script of the venv and the script
    # keeps its pid, and stops it by that pid and no other.
    assert ".venv/bin/longhaul" in text
    assert "WORKER_PID=$!" in text
    assert 'kill "$WORKER_PID"' in text
    assert "trap cleanup EXIT" in text
    commands = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("uv run" in line for line in commands)
    assert "calc_fix.yaml" in text
    assert "--provider scripted" in text
    # Nothing here may reach a model, GitHub or a key.
    assert "ANTHROPIC_API_KEY" in text and "unset" in text
    assert "docker compose" not in text


# ---------- architecture ----------
def test_architecture_has_the_three_diagrams_of_plan_section_3() -> None:
    text = read(DOCS / "architecture.md")
    for title in ("System context", "Task workflow", "Data stores"):
        body = section(text, title)
        assert len(mermaid_blocks(body)) == 1, title


def test_architecture_diagrams_name_what_was_built_after_the_plan() -> None:
    text = read(DOCS / "architecture.md")
    context = section(text, "System context")
    for part in ("Console", "FastAPI", "Keystone", "Budgets", "Memory"):
        assert part in context, part
    workflow = section(text, "Task workflow")
    for part in ("check_budget", "load_memory", "plan", "verifier", "stuck", "continue"):
        assert part.lower() in workflow.lower(), part
    stores = section(text, "Data stores")
    for part in ("Postgres", "MinIO", "Temporal", "checkpoint", "cost_ledger", "memory_items"):
        assert part in stores, part


# ---------- runbook ----------
PATCH_MARKER = re.compile(r'^[A-Z_]+_PATCH = "([a-z0-9-]+)"', re.MULTILINE)


def workflow_patch_markers() -> set[str]:
    found: set[str] = set()
    for name in ("task.py", "poller.py"):
        found |= set(PATCH_MARKER.findall(read(ROOT / "src/longhaul/durable/workflows" / name)))
    return found


def test_the_workflow_code_has_the_markers_this_page_expects() -> None:
    # If this fails, a marker was renamed or removed. A removed marker breaks the histories that
    # carry it, which is the thing the runbook section warns about.
    assert {
        "lh-502-plan-gate",
        "lh-506-pause-for-hint",
        "lh-507-final-outcome",
        "lh-604-load-memory",
    } <= workflow_patch_markers()


def test_runbook_has_the_operations_sections() -> None:
    text = read(DOCS / "runbook.md")
    found = headings(text, level=3)
    for title in (
        "Start and stop",
        "Drain a worker",
        "Cancel stuck tasks",
        "Rotate tokens",
        "The replay test",
        "Upgrade with workflow versioning",
    ):
        assert title in found, title


def test_runbook_lists_every_patch_marker_of_the_workflows() -> None:
    upgrading = section(read(DOCS / "runbook.md"), "Upgrade with workflow versioning", level=3)
    for marker in sorted(workflow_patch_markers()):
        assert f"`{marker}`" in upgrading, marker


def test_runbook_points_token_rotation_at_the_security_page() -> None:
    rotate = section(read(DOCS / "runbook.md"), "Rotate tokens", level=3)
    assert "docs/security.md" in rotate


# ---------- case study and demo script ----------
def test_case_study_has_the_sections_of_plan_section_11_2() -> None:
    text = read(DOCS / "case-study.md")
    found = headings(text)
    expected = [
        "The problem",
        "What I built",
        "Proof",
        "What did not work",
        "Decisions I would defend",
        "Links",
    ]
    positions = [found.index(title) for title in expected]
    assert positions == sorted(positions), found
    assert "docs/results.md" in section(text, "Proof")
    assert "pending" in section(text, "Proof")


def test_demo_script_has_the_timeline_of_plan_section_11_3() -> None:
    text = read(DOCS / "demo-script.md")
    times = re.findall(r"^\| (\d:\d\d) \|", text, flags=re.MULTILINE)
    assert times == [
        "0:00",
        "0:12",
        "0:28",
        "0:45",
        "0:52",
        "1:10",
        "1:25",
        "1:40",
        "1:55",
    ]


def test_demo_script_lists_the_owner_steps_as_open() -> None:
    # The repo is public, its secrets check is done, the site is live and v1.0.0 is tagged. The
    # video and the GIF are not made, and the page must not claim they are.
    text = read(DOCS / "demo-script.md")
    checklist = section(text, "Recording checklist")
    done = [line for line in checklist.splitlines() if line.startswith("- [x] ")]
    assert len(done) == 3
    assert any("public" in line and "gitleaks" in line for line in done)
    assert any("GitHub Pages" in line for line in done)
    assert any("v1.0.0" in line for line in done)
    open_steps = [line for line in checklist.splitlines() if line.startswith("- [ ] ")]
    for word in ("Record the video", "30-second GIF"):
        assert any(word in line for line in open_steps), word
    assert "docs/media/demo.gif" in checklist
    assert "not public" not in text


def test_demo_script_voice_over_counts_the_runs_that_survived_a_kill() -> None:
    # 18 of the 20 runs had a kill land. The voice-over must not say that all 20 survived kills.
    text = read(DOCS / "demo-script.md")
    assert "18 runs survived one to three kills" in text
    assert "all 20 ended with the same diff and no duplicate write" in text


def test_readme_ties_a_moved_port_to_its_connection_setting() -> None:
    text = read(ROOT / "README.md")
    for setting in ("LONGHAUL_DATABASE_URL", "LONGHAUL_TEMPORAL_ADDRESS", "LONGHAUL_BLOB_ENDPOINT"):
        assert setting in text, setting


def test_architecture_says_the_api_writes_memory_decisions() -> None:
    text = read(DOCS / "architecture.md")
    stores = section(text, "Data stores")
    assert "API -->|reads, approves, rejects| MEMT" in stores
    assert "The API only reads. |" not in stores
