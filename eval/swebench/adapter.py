"""Run Longhaul on SWE-bench Verified instances and hand the patches to the official harness.

    uv run python -m eval.swebench.adapter gold  --out gold.jsonl  ID...
    uv run python -m eval.swebench.adapter empty --out empty.jsonl ID...
    uv run python -m eval.swebench.adapter predictions RUN_DIR

`eval/run.py --set swebench` uses the `SwebenchDriver` of this module. For each instance it:

1. builds the sandbox image of the instance and copies its repo to a folder (images.py), so the
   task is a `local:` task like the ones of the custom set;
2. starts the task with the problem statement as the issue. Hints are not shown. The test command
   of the benchmark is given as the task's test command, without the test files, so the agent
   knows how the repo runs its tests and is not told which tests will be used;
3. cuts the test files of the instance's own test patch out of the agent's diff. The harness
   applies that test patch to the repo before it runs the tests. A model patch that touches the
   same file makes `git apply` fail, and the instance would count as unresolved for a reason that
   has nothing to do with the fix;
4. writes `predictions.jsonl`, one row per task with `instance_id`, `model_name_or_path` and
   `model_patch`, which `grade.sh` hands to `python -m swebench.harness.run_evaluation`.

The tasks are graded after the run, not during it: the harness works through all predictions
together, so the runner records each task as not graded, and `grade.sh` fills in `resolved`.
`gold` and `empty` write predictions without an agent, to check the grader itself: the reference
patches must come out 2 of 2 resolved, and empty patches 0 of 2.

Nothing here imports the swebench package. It is only run through `grade.sh`, in a folder of its
own, because this package lives in a folder called `swebench` and would hide the real one.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import shlex
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from eval import results
from eval.swebench import select_subset
from longhaul.agent.workspace import IMAGE_FILE
from longhaul.contracts import Budgets, RepoSettings, TaskSpec

DATASET = select_subset.DATASET
PREDICTIONS_FILE = "predictions.jsonl"
# Plan section 8.4: the dollar cap on SWE-bench is 4. The turns and the time are the custom set's.
BUDGETS = Budgets(task_usd=4.0, task_turns=150, task_wall_minutes=120)

START_MARK = ">>>>> Start Test Output"
END_MARK = ">>>>> End Test Output"
NOT_GRADED = "not graded yet: run eval/swebench/grade.sh on this run"

_SHA = re.compile(r"[0-9a-f]{40}")
_INSTANCE_ID = re.compile(r"[A-Za-z0-9][\w.-]*__[\w.-]+-\d+")
# A Django test label such as `validators.tests` or `model_fields.test_charfield`, or a bare module
# name such as `runtests`, which only counts after the runtests.py script of Django.
_LABEL = re.compile(r"[A-Za-z_]\w*(?:\.\w+)+")
_BARE_LABEL = re.compile(r"[A-Za-z_]\w*")
_HEADER = "diff --git "
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


# ---------- the instance ----------
class Instance(BaseModel):
    """The fields of a dataset row that this adapter reads."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    test_patch: str
    patch: str
    image: str
    eval_script: str

    @property
    def id(self) -> str:
        return self.instance_id

    @field_validator("instance_id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        # The id becomes a workflow id, a container name and a file name.
        if not _INSTANCE_ID.fullmatch(value):
            raise ValueError(f"instance_id {value!r} is not like django__django-11099")
        return value

    @field_validator("base_commit")
    @classmethod
    def _full_sha(cls, value: str) -> str:
        if not _SHA.fullmatch(value):
            raise ValueError(f"base_commit {value!r} is not a full lowercase sha")
        return value


def instance_from_row(row: dict[str, Any]) -> Instance:
    """An Instance from a row of SWE-bench/SWE-bench_Verified. ValueError names a bad field."""
    wanted = sorted(Instance.model_fields)
    missing = [name for name in wanted if name not in row]
    if missing:
        raise ValueError(f"a row of the dataset lacks {', '.join(missing)}")
    return Instance.model_validate({name: row[name] for name in wanted})


def load_instances(ids: Sequence[str], *, dataset: str = DATASET) -> list[Instance]:
    """The instances with these ids, in the order of `ids`. Needs the eval extra and the network."""
    from datasets import load_dataset  # lazy: CI and the unit tests do not install it

    wanted = set(ids)
    found: dict[str, Instance] = {}
    for row in load_dataset(dataset, split="test"):
        if row["instance_id"] in wanted:
            found[row["instance_id"]] = instance_from_row(dict(row))
    absent = [i for i in ids if i not in found]
    if absent:
        raise ValueError(f"not in {dataset}: {', '.join(absent)}")
    return [found[i] for i in ids]


def select_instances(
    limit: int | None, *, subset_file: Path = select_subset.SUBSET_FILE
) -> list[Instance]:
    """The instances of the frozen subset in file order, cut to the first `limit`."""
    ids = select_subset.read_subset(subset_file)
    return load_instances(ids[:limit] if limit is not None else ids)


# ---------- the diff of the agent ----------
def _sections(patch: str) -> list[str]:
    """The per-file parts of a git diff. A part starts at a line that begins `diff --git `."""
    if not patch:
        return []
    if not patch.startswith(_HEADER):
        raise ValueError(f"a patch must start with 'diff --git', not {patch[:40]!r}")
    parts: list[list[str]] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith(_HEADER):
            parts.append([])
        parts[-1].append(line)
    return ["".join(part) for part in parts]


def _section_paths(section: str) -> list[str]:
    """Every path a file part of a diff names, from its header lines."""
    found: list[str] = []
    for line in section.splitlines():
        for prefix in ("--- a/", "+++ b/", "rename from ", "rename to ", "copy from ", "copy to "):
            if line.startswith(prefix):
                found.append(line[len(prefix) :])
        if line.startswith("@@"):
            break
    if found:
        return list(dict.fromkeys(found))
    # A part with no hunks (a mode change, a binary file) names its path in the first line only.
    header = section.splitlines()[0][len(_HEADER) :]
    left, _, right = header.partition(" b/")
    return list(dict.fromkeys([left.removeprefix("a/"), right]))


def test_patch_files(test_patch: str) -> list[str]:
    """The files the test patch touches, in order of appearance and without repeats."""
    names: list[str] = []
    for section in _sections(test_patch):
        names += _section_paths(section)
    return list(dict.fromkeys(names))


def model_patch(patch: str, test_patch: str) -> str:
    """The agent's diff without the files that the instance's test patch touches."""
    excluded = set(test_patch_files(test_patch))
    kept = [s for s in _sections(patch) if not excluded.intersection(_section_paths(s))]
    return "".join(kept)


# ---------- the test command ----------
def test_command_hint(eval_script: str, test_patch: str) -> str | None:
    """The command that the harness runs for the tests, minus the tests it names.

    The eval script of an instance has one command between two marker lines. It ends with the
    test files of the instance (or, for Django, test labels). Those say which tests grade the
    fix, so they are taken off. What stays is how the repo runs its tests.
    """
    lines = eval_script.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if START_MARK in line)
        end = next(i for i, line in enumerate(lines) if i > start and END_MARK in line)
    except StopIteration:
        return None
    commands = [line.strip() for line in lines[start + 1 : end] if line.strip()]
    if not commands:
        return None
    words = shlex.split(commands[0])
    named = set(test_patch_files(test_patch))
    django = words[0].endswith("runtests.py")
    while len(words) > 1 and (
        words[-1] in named
        or _LABEL.fullmatch(words[-1])
        or (django and _BARE_LABEL.fullmatch(words[-1]))
    ):
        words.pop()
    return shlex.join(words)


# ---------- the task ----------
def build_spec(instance: Instance, workflow_id: str, folder: Path) -> TaskSpec:
    """The task as the workflow gets it: a local folder at the base commit, no plan gate."""
    first, _, rest = instance.problem_statement.strip().partition("\n")
    title = first.strip() or instance.instance_id
    repo = f"local:{folder}"
    return TaskSpec(
        task_id=workflow_id,
        repo=repo,
        issue_number=None,
        title=title,
        body=rest.strip() or title,
        base_sha=instance.base_commit,
        settings=RepoSettings(
            repo=repo,
            plan_approval_required=False,
            # The instance image has its environment and the project installed.
            setup_command="",
            test_command=test_command_hint(instance.eval_script, instance.test_patch),
            budgets=BUDGETS,
        ),
    )


class ImageTools(Protocol):
    def prepare(self, instance: Instance, folder: Path) -> str: ...


class SwebenchDriver:
    """What eval/run.py asks of a task set: the spec, the patch and the grading of a task."""

    def __init__(self, images: ImageTools | None = None, *, work_root: Path | None = None) -> None:
        if images is None:
            from eval.swebench.images import DockerImages

            images = DockerImages()
        self._images = images
        self._work_root = work_root

    @contextlib.asynccontextmanager
    async def prepared(self, instance: Instance, workflow_id: str) -> AsyncIterator[TaskSpec]:
        """The spec of a task whose folder exists. The folder is removed when the body ends.

        The worker reads the folder when it prepares the workspace, and again to find the image
        when it has to make a new sandbox, so it has to stay until the task is over.
        """
        root = self._work_root
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
        folder = Path(
            await asyncio.to_thread(
                tempfile.mkdtemp, prefix=f"lh-{_SAFE_NAME.sub('-', instance.id)}-", dir=root
            )
        )
        try:
            tag = await asyncio.to_thread(self._images.prepare, instance, folder)
            (folder / IMAGE_FILE).write_text(f"{tag}\n", encoding="utf-8")
            yield build_spec(instance, workflow_id, folder)
        finally:
            await asyncio.to_thread(shutil.rmtree, folder, True)

    def patch(self, instance: Instance, patch: str) -> str:
        return model_patch(patch, instance.test_patch)

    async def grade(self, instance: Instance, patch: str) -> tuple[bool | None, str]:
        """The harness grades all predictions together, after the run."""
        return None, NOT_GRADED


# ---------- predictions ----------
def model_name(config: str, model: str) -> str:
    """The model_name_or_path of a run. The harness uses it in file names, so it has no slash."""
    return _SAFE_NAME.sub("-", f"longhaul-{config}-{model}")


def prediction(instance_id: str, name: str, patch: str) -> dict[str, str]:
    return {"instance_id": instance_id, "model_name_or_path": name, "model_patch": patch}


def write_predictions(path: Path, rows: Iterable[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def read_predictions(path: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def gold_predictions(instances: Sequence[Instance]) -> list[dict[str, str]]:
    """The reference patch of each instance, as if the agent had written it."""
    return [prediction(i.instance_id, "gold", i.patch) for i in instances]


def empty_predictions(instances: Sequence[Instance]) -> list[dict[str, str]]:
    return [prediction(i.instance_id, "empty", "") for i in instances]


def predictions_for_run(folder: Path) -> list[dict[str, str]]:
    """One row per task of a run, from its result files and the `<task>.patch` files.

    A task that did not end as `pr_opened` delivered nothing, so its patch is not submitted, as
    in the custom set. It still gets a row, with an empty patch, so the harness counts it.
    """
    found = results.read_results(folder)
    if not found:
        raise ValueError(f"no result files in {folder}")
    for result in found:
        if result.task_set != "swebench":
            raise ValueError(f"{folder} is a run of the {result.task_set} set, not of swebench")
    name = model_name(found[0].config, found[0].model)
    rows = []
    for result in found:
        patch_file = folder / f"{results.check_task_id(result.task_id)}.patch"
        delivered = result.outcome == "pr_opened" and patch_file.is_file()
        patch = patch_file.read_text(encoding="utf-8") if delivered else ""
        rows.append(prediction(result.task_id, name, patch))
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Predictions for the official SWE-bench harness.")
    modes = parser.add_subparsers(dest="mode", required=True)
    for mode, text in (("gold", "the reference patches"), ("empty", "empty patches")):
        sub = modes.add_parser(mode, help=f"write predictions with {text}")
        sub.add_argument("ids", nargs="+", metavar="ID")
        sub.add_argument("--out", type=Path, required=True)
    from_run = modes.add_parser("predictions", help="write predictions.jsonl for a finished run")
    from_run.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "predictions":
        rows = predictions_for_run(args.run_dir)
        out = args.run_dir / PREDICTIONS_FILE
    else:
        instances = load_instances(args.ids)
        rows = gold_predictions(instances) if args.mode == "gold" else empty_predictions(instances)
        out = args.out
    write_predictions(out, rows)
    print(f"wrote {len(rows)} predictions to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
