"""The GitHub that one chaos run delivers to (LH-904).

A world is a FakeGitHub served on a port of its own, with the repo of a custom task as its remote
and the issue of that task open. The worker is told where it is through two settings, so the side
effects of the run can be counted from the fake and not from our own tables.
"""

import subprocess
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from eval import custom_tasks
from eval.chaos.world import World
from longhaul.github import lifecycle

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

TASK = next(t for t in custom_tasks.load_tasks() if t.id == "calc-01")


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    opened = World.open(tmp_path, TASK)
    try:
        yield opened
    finally:
        opened.close()


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
        data: bytes = response.read()
    return data


def test_the_remote_holds_the_repo_of_the_task_at_its_base_commit(world: World) -> None:
    remote = Path(world.env()["LONGHAUL_GITHUB_CLONE_BASE"]) / f"{world.repo}.git"

    head = subprocess.run(
        ["git", f"--git-dir={remote}", "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert head == TASK.base_sha


def test_the_worker_is_told_where_github_and_its_repos_are(world: World) -> None:
    env = world.env()

    assert set(env) == {
        "LONGHAUL_GITHUB_TOKEN",
        "LONGHAUL_GITHUB_API_URL",
        "LONGHAUL_GITHUB_CLONE_BASE",
    }
    assert env["LONGHAUL_GITHUB_API_URL"].startswith("http://127.0.0.1:")
    assert b'"default_branch"' in fetch(f"{env['LONGHAUL_GITHUB_API_URL']}/repos/{world.repo}")


def test_the_issue_is_the_issue_of_the_task(world: World) -> None:
    url = f"{world.env()['LONGHAUL_GITHUB_API_URL']}/repos/{world.repo}/issues/{world.issue}"

    body = fetch(url).decode("utf-8")

    assert TASK.issue_title in body


def test_the_spec_is_the_task_of_that_issue_with_no_plan_gate(world: World) -> None:
    spec = world.spec("lh-chaos-calc-01-ab12cd34")

    assert spec.repo == world.repo
    assert spec.issue_number == world.issue
    assert (spec.title, spec.body) == (TASK.issue_title, TASK.issue_body)
    assert spec.settings.plan_approval_required is False
    assert lifecycle.is_github_task(spec)


def test_nothing_has_been_written_before_the_run(world: World) -> None:
    effects = world.side_effects()

    assert (effects.comments, effects.label_events, effects.pull_requests) == (0, 0, 0)


def test_two_worlds_share_nothing(tmp_path: Path) -> None:
    first = World.open(tmp_path / "one", TASK)
    second = World.open(tmp_path / "two", TASK)
    try:
        assert first.env()["LONGHAUL_GITHUB_API_URL"] != second.env()["LONGHAUL_GITHUB_API_URL"]
        assert first.env()["LONGHAUL_GITHUB_TOKEN"] != second.env()["LONGHAUL_GITHUB_TOKEN"]
        first.fake.seed_comment(first.repo, first.issue, "only here")
        assert first.side_effects().comments == 1
        assert second.side_effects().comments == 0
    finally:
        first.close()
        second.close()


def test_closing_the_world_stops_its_server(tmp_path: Path) -> None:
    opened = World.open(tmp_path, TASK)
    url = opened.env()["LONGHAUL_GITHUB_API_URL"]
    assert fetch(f"{url}/repos/{opened.repo}")

    opened.close()

    with pytest.raises(OSError):
        fetch(f"{url}/repos/{opened.repo}")


def test_a_task_whose_repo_folder_changed_is_refused(tmp_path: Path) -> None:
    stale = TASK.model_copy(update={"base_sha": "0" * 40})

    with pytest.raises(ValueError, match="base_sha"):
        World.open(tmp_path, stale)
