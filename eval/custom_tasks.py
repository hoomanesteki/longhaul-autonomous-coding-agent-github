"""The custom task set: fifteen tasks on three small repos that we own (plan section 8.2).

Each repo is a folder under eval/custom/repos. It is the base of its five tasks, and the
`base_sha` of a task is the commit that scripts/make_fixture_git.py makes of that folder, so it
is the same on every machine. The tests that decide whether a task is solved are in
eval/custom/hidden_tests, and the fix that was used to check them is in eval/custom/reference.
Neither folder may ever reach the agent. `check_agent_view` is the guard that the runner and the
tests apply.

`python eval/custom_tasks.py` prints the current base_sha of each repo, for tasks.yaml.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from longhaul.agent.workspace import collect_source_files

ROOT = Path(__file__).resolve().parents[1]
CUSTOM_DIR = ROOT / "eval" / "custom"
REPOS_DIR = CUSTOM_DIR / "repos"
HIDDEN_DIR = CUSTOM_DIR / "hidden_tests"
REFERENCE_DIR = CUSTOM_DIR / "reference"
TASKS_FILE = CUSTOM_DIR / "tasks.yaml"

# The folders the agent must never see, as paths from the root of the checkout.
HIDDEN_PREFIX = "eval/custom/hidden_tests/"

Category = Literal["bugfix", "feature", "refactor", "dependency", "flaky"]


class HiddenTestsVisibleError(Exception):
    """A repo folder would hand the hidden tests, or a reference patch, to the agent."""


class CustomTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    repo: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    issue_title: str = Field(min_length=1)
    issue_body: str = Field(min_length=1)
    category: Category
    hidden_tests: str

    @field_validator("hidden_tests")
    @classmethod
    def _under_the_hidden_folder(cls, value: str) -> str:
        if not value.startswith(HIDDEN_PREFIX) or ".." in value.split("/"):
            raise ValueError(f"hidden_tests must be a path under {HIDDEN_PREFIX}, not {value!r}")
        if not value.endswith(".py"):
            raise ValueError(f"hidden_tests must be a Python test file, not {value!r}")
        return value


def load_tasks(path: Path = TASKS_FILE) -> list[CustomTask]:
    """The tasks in file order. A mistake in the file raises ValueError that names the task."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: the file must hold a list of tasks")
    tasks: list[CustomTask] = []
    for index, item in enumerate(data, start=1):
        try:
            tasks.append(CustomTask.model_validate(item))
        except ValidationError as exc:
            raise ValueError(f"{path}: task {index}: {exc}") from exc
    seen: set[str] = set()
    for task in tasks:
        if task.id in seen:
            raise ValueError(f"{path}: the id {task.id} is used twice")
        seen.add(task.id)
    return tasks


def hidden_tests_path(task: CustomTask) -> Path:
    return ROOT / task.hidden_tests


def reference_patch_path(task: CustomTask) -> Path:
    return REFERENCE_DIR / f"{task.id}.patch"


# ---------- the base commit of a repo ----------
def _fixture_module() -> ModuleType:
    """scripts/make_fixture_git.py. It is a script and not a package, so it is loaded by path."""
    path = ROOT / "scripts" / "make_fixture_git.py"
    spec = importlib.util.spec_from_file_location("make_fixture_git", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_base_repo(folder: Path, dest: Path) -> str:
    """Commit `folder` into a new git repo at `dest` and return the commit id.

    It is the repo of scripts/make_fixture_git.py: one commit, a `base` tag, and an id that
    depends on the files and on nothing else. Host git only stores the files. Nothing of the
    repo runs here, so no hook or config of the repo can act.
    """
    fixture = _fixture_module()
    fixture.make_fixture_git(folder, dest)
    head = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=fixture.isolated_git_env(),
    )
    return head.stdout.strip()


def compute_base_sha(folder: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="lh-base-") as scratch:
        return build_base_repo(folder, Path(scratch) / "repo")


def base_bundle(task: CustomTask) -> bytes:
    """A git bundle of the base commit of a task, to carry the repo into a fresh sandbox.

    The commit is built from the repo folder as it is now. When that is not the commit the
    task names, tasks.yaml is out of date and the task would be graded against other files
    than the ones in the folder, so this raises ValueError.
    """
    folder = REPOS_DIR / task.repo
    with tempfile.TemporaryDirectory(prefix="lh-base-") as scratch:
        repo = Path(scratch) / "repo"
        actual = build_base_repo(folder, repo)
        if actual != task.base_sha:
            raise ValueError(
                f"{task.id}: base_sha is out of date, the repo folder is at {actual} "
                f"(python eval/custom_tasks.py prints the current ones)"
            )
        bundle = Path(scratch) / "base.bundle"
        subprocess.run(
            ["git", "-C", str(repo), "bundle", "create", "--quiet", str(bundle), "main"],
            check=True,
            capture_output=True,
            env=_fixture_module().isolated_git_env(),
        )
        return bundle.read_bytes()


# ---------- keeping the hidden tests out of the agent's view ----------
def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _withheld_files() -> list[Path]:
    folders = (HIDDEN_DIR, REFERENCE_DIR)
    return sorted(
        path
        for folder in folders
        for path in folder.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.stat().st_size > 0
    )


def _is_inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def check_agent_view(folder: Path) -> None:
    """Raise HiddenTestsVisibleError when the agent would see a hidden test or a reference patch.

    The folder is read the way the worker reads it for a `local:` task, so a file that the
    ignore rules keep out of the sandbox is not in the agent's view. A hidden file is found by
    its name and by its content, because a copy may carry any name.
    """
    folder = folder.resolve()
    for withheld in (HIDDEN_DIR, REFERENCE_DIR):
        if _is_inside(folder, withheld):
            raise HiddenTestsVisibleError(
                f"{folder} is inside {withheld}, which the agent must not see"
            )
        if _is_inside(withheld, folder):
            raise HiddenTestsVisibleError(
                f"{folder} contains {withheld}, which the agent must not see"
            )
    files = _withheld_files()
    names = {path.name for path in files}
    digests = {_digest(path) for path in files}
    leaks = [
        item.path
        for item in collect_source_files(folder)
        if Path(item.path).name in names or _digest(folder / item.path) in digests
    ]
    if leaks:
        raise HiddenTestsVisibleError(
            f"{folder} holds files of the hidden set that the agent would see: {', '.join(leaks)}"
        )


def main() -> None:
    for folder in sorted(p for p in REPOS_DIR.iterdir() if p.is_dir()):
        print(f"{folder.name}: {compute_base_sha(folder)}")


if __name__ == "__main__":
    main()
