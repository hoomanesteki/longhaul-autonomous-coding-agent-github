"""The custom task set (LH-901): fifteen tasks on three repos, and what keeps their tests hidden.

None of this runs a repo's code or needs Docker. The checks read the files, and build each
repo's base commit with host git the way scripts/make_fixture_git.py does. The grader and the
runner are in tests/integration/test_eval_runner.py.
"""

import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from eval import custom_tasks
from eval.custom_tasks import CustomTask, HiddenTestsVisibleError

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

REPO_NAMES = ["calc", "textkit", "inventory"]
CATEGORY_MIX = {"bugfix": 5, "feature": 3, "refactor": 3, "dependency": 2, "flaky": 2}


def task_data(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "calc-01",
        "repo": "calc",
        "base_sha": "a" * 40,
        "issue_title": "add() returns the difference",
        "issue_body": "calc.add subtracts.",
        "category": "bugfix",
        "hidden_tests": "eval/custom/hidden_tests/calc/test_calc_01_add.py",
    }
    return {**data, **changes}


def write_tasks(path: Path, *items: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(list(items)), encoding="utf-8")
    return path


# ---------- the shipped task file ----------
def test_the_set_has_fifteen_tasks_with_five_on_each_repo() -> None:
    tasks = custom_tasks.load_tasks()
    assert len(tasks) == 15
    assert Counter(t.repo for t in tasks) == dict.fromkeys(REPO_NAMES, 5)


def test_the_categories_follow_the_plan() -> None:
    # Plan section 8.2: bugfix 5, feature 3, refactor 3, dependency 2, flaky 2.
    assert Counter(t.category for t in custom_tasks.load_tasks()) == CATEGORY_MIX


def test_the_first_two_tasks_are_the_small_calc_bugfixes() -> None:
    # `--limit 2` takes the first two tasks, and the dry run scripts fix exactly these.
    assert [t.id for t in custom_tasks.load_tasks()[:2]] == ["calc-01", "calc-02"]


@pytest.mark.parametrize("name", REPO_NAMES)
def test_base_sha_is_the_commit_of_the_repo_folder(name: str) -> None:
    # A change to a repo folder changes every task on it. The message says how to fix the file.
    actual = custom_tasks.compute_base_sha(custom_tasks.REPOS_DIR / name)
    for task in custom_tasks.load_tasks():
        if task.repo == name:
            assert task.base_sha == actual, (
                f"{task.id}: base_sha is out of date, set it to {actual} "
                "(python eval/custom_tasks.py prints the current ones)"
            )


def test_every_task_has_hidden_tests_and_a_reference_patch() -> None:
    for task in custom_tasks.load_tasks():
        assert custom_tasks.hidden_tests_path(task).is_file(), task.id
        assert custom_tasks.reference_patch_path(task).is_file(), task.id


def test_hidden_test_files_are_not_shared_between_tasks() -> None:
    paths = [t.hidden_tests for t in custom_tasks.load_tasks()]
    assert len(set(paths)) == len(paths)


@pytest.mark.parametrize("name", REPO_NAMES)
def test_no_repo_folder_holds_a_hidden_test_or_a_reference_patch(name: str) -> None:
    custom_tasks.check_agent_view(custom_tasks.REPOS_DIR / name)


def test_issue_text_does_not_point_at_the_hidden_tests() -> None:
    for task in custom_tasks.load_tasks():
        text = f"{task.issue_title}\n{task.issue_body}".lower()
        assert "hidden" not in text, task.id
        assert "reference" not in text, task.id


# ---------- the loader ----------
def test_a_task_file_loads(tmp_path: Path) -> None:
    (task,) = custom_tasks.load_tasks(write_tasks(tmp_path / "tasks.yaml", task_data()))
    assert task == CustomTask.model_validate(task_data())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"category": "chore"}, "category"),
        ({"base_sha": "abc"}, "base_sha"),
        ({"id": "Calc 01"}, "id"),
        ({"hidden_tests": "tests/test_calc.py"}, "hidden_tests"),
        ({"hidden_tests": "eval/custom/hidden_tests/../repos/calc/tests/test_calc.py"}, "hidden"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_a_bad_task_is_refused(tmp_path: Path, changes: dict[str, Any], message: str) -> None:
    path = write_tasks(tmp_path / "tasks.yaml", task_data(**changes))
    with pytest.raises(ValueError, match=message):
        custom_tasks.load_tasks(path)


def test_two_tasks_with_one_id_are_refused(tmp_path: Path) -> None:
    path = write_tasks(tmp_path / "tasks.yaml", task_data(), task_data())
    with pytest.raises(ValueError, match="calc-01"):
        custom_tasks.load_tasks(path)


def test_a_file_that_is_not_a_list_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "tasks.yaml"
    path.write_text("tasks: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="list"):
        custom_tasks.load_tasks(path)


# ---------- keeping the hidden tests out of the agent's view ----------
def copy_repo(tmp_path: Path, name: str = "calc") -> Path:
    return shutil.copytree(custom_tasks.REPOS_DIR / name, tmp_path / name)


def test_a_copied_hidden_test_is_found_under_any_name(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    task = custom_tasks.load_tasks()[0]
    shutil.copy(custom_tasks.hidden_tests_path(task), repo / "tests" / "test_harmless.py")
    with pytest.raises(HiddenTestsVisibleError, match="test_harmless.py"):
        custom_tasks.check_agent_view(repo)


def test_a_file_with_the_name_of_a_hidden_test_is_found(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    task = custom_tasks.load_tasks()[0]
    name = custom_tasks.hidden_tests_path(task).name
    (repo / "tests" / name).write_text("def test_nothing() -> None:\n    pass\n", encoding="utf-8")
    with pytest.raises(HiddenTestsVisibleError, match=name):
        custom_tasks.check_agent_view(repo)


def test_a_copied_reference_patch_is_found(tmp_path: Path) -> None:
    repo = copy_repo(tmp_path)
    task = custom_tasks.load_tasks()[0]
    shutil.copy(custom_tasks.reference_patch_path(task), repo / "notes.txt")
    with pytest.raises(HiddenTestsVisibleError, match="notes.txt"):
        custom_tasks.check_agent_view(repo)


def test_a_repo_that_contains_the_hidden_folder_is_refused() -> None:
    with pytest.raises(HiddenTestsVisibleError, match="contains"):
        custom_tasks.check_agent_view(custom_tasks.CUSTOM_DIR)


def test_a_repo_inside_the_hidden_folder_is_refused() -> None:
    with pytest.raises(HiddenTestsVisibleError, match="inside"):
        custom_tasks.check_agent_view(custom_tasks.HIDDEN_DIR / "calc")


def test_a_hidden_test_that_the_ignore_file_hides_is_not_a_leak(tmp_path: Path) -> None:
    # The check reads what the worker would copy, so a file that .longhaulignore keeps out of
    # the sandbox is not in the agent's view.
    repo = copy_repo(tmp_path)
    task = custom_tasks.load_tasks()[0]
    shutil.copy(custom_tasks.hidden_tests_path(task), repo / "private.py")
    (repo / ".longhaulignore").write_text("private.py\n", encoding="utf-8")
    custom_tasks.check_agent_view(repo)
