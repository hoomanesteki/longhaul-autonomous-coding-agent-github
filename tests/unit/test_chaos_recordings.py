"""The recorded tasks of the full chaos suite and the scripts that were recorded (LH-904).

The suite plays eval/chaos/recordings/<task id>/ with the replay provider, so every run sees the
same answers and a difference in a final diff is a durability bug. No key is available to record
a real model, so each recording is a scripted run of tests/fixtures/scripts/eval/<task id>.yaml.
These tests check that the scripts fix their tasks, that a recording holds one call for each
turn of its script, and that eval/chaos/README.md says how to replace them with a real model.
"""

import json
import shlex
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from tests.fakes.scripted_runs import CONFIG

from eval import custom_tasks
from eval.chaos.plan import RECORDED_TASKS
from longhaul.agent.loop import run_loop
from longhaul.agent.workspace import collect_source_files
from longhaul.contracts import OutcomeKind, RepoSettings, TaskSpec
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
RECORDINGS = ROOT / "eval" / "chaos" / "recordings"
SCRIPTS = ROOT / "tests" / "fixtures" / "scripts" / "eval"
README = ROOT / "eval" / "chaos" / "README.md"
TASKS = {task.id: task for task in custom_tasks.load_tasks()}
TEST_COMMAND = f"{shlex.quote(sys.executable)} -m pytest -q"
# What a recording may say about where it came from: a script, or the id of a model.
SOURCES = ("scripted", "claude-")


def test_the_recorded_tasks_are_four_tasks_of_the_custom_set() -> None:
    assert len(RECORDED_TASKS) == 4
    assert set(RECORDED_TASKS) <= set(TASKS)


@pytest.mark.parametrize("task_id", RECORDED_TASKS)
def test_every_recorded_task_has_a_script_to_record_from(task_id: str) -> None:
    assert (SCRIPTS / f"{task_id}.yaml").is_file()


@pytest.fixture
async def box() -> AsyncIterator[FakeSandbox]:
    sandbox = FakeSandbox()
    yield sandbox
    await sandbox.destroy()


@pytest.mark.parametrize("task_id", RECORDED_TASKS)
async def test_the_script_of_a_task_fixes_it(task_id: str, box: FakeSandbox) -> None:
    task = TASKS[task_id]
    folder = custom_tasks.REPOS_DIR / task.repo
    for item in collect_source_files(folder):
        await box.write_file(item.path, (folder / item.path).read_bytes())
    spec = TaskSpec(
        task_id=f"lh-local-{task_id}-1",
        repo=f"local:{task.repo}",
        issue_number=None,
        title=task.issue_title,
        body=task.issue_body,
        settings=RepoSettings(repo=f"local:{task.repo}", test_command=TEST_COMMAND),
    )
    registry = default_registry(test_command=TEST_COMMAND)

    done = await run_loop(
        spec, ScriptedProvider(SCRIPTS / f"{task_id}.yaml"), registry, box, CONFIG
    )

    assert done.outcome.kind is OutcomeKind.PR_OPENED, done.outcome
    hidden = custom_tasks.hidden_tests_path(task)
    await box.write_file(f"tests/{hidden.name}", hidden.read_bytes())
    ran = await box.exec(TEST_COMMAND, timeout_s=120)
    assert ran.exit_code == 0, ran.stdout + ran.stderr


@pytest.mark.parametrize("task_id", RECORDED_TASKS)
def test_every_recorded_task_has_a_recording_with_a_call_for_each_turn(task_id: str) -> None:
    calls = sorted((RECORDINGS / task_id).glob("*.json"))

    assert calls, f"{RECORDINGS / task_id} holds no recorded call"
    turns = [json.loads(path.read_text(encoding="utf-8"))["turn_index"] for path in calls]
    assert turns == list(range(len(turns)))
    assert len(turns) >= 5


@pytest.mark.parametrize("task_id", RECORDED_TASKS)
def test_every_recording_says_where_it_came_from(task_id: str) -> None:
    source = (RECORDINGS / task_id / "SOURCE").read_text(encoding="utf-8").strip()

    assert source.startswith(SOURCES), source


@pytest.mark.parametrize("task_id", RECORDED_TASKS)
def test_a_recording_keeps_nothing_of_this_machine_or_of_a_key(task_id: str) -> None:
    for path in (RECORDINGS / task_id).glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text and "/private/" not in text, path.name
        assert "sk-ant" not in text and "ghp_" not in text, path.name


def test_the_readme_says_a_recording_of_a_real_model_replaces_the_scripted_ones() -> None:
    text = README.read_text(encoding="utf-8")

    assert "scripted" in text
    assert "replaces" in text
    assert "--record --provider anthropic" in text
    assert "eval/chaos/chaos.py --runs 20 --seed 7" in text
