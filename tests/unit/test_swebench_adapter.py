"""The SWE-bench adapter: the instance, the task spec, the hint and the predictions (LH-902).

No network, Docker or dataset here. The rows are written in the shape of the dataset
SWE-bench/SWE-bench_Verified, which carries the image name and the eval script of each instance.
"""

from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eval import results
from eval.results import TaskResult, Tokens
from eval.swebench import adapter
from eval.swebench.adapter import Instance
from longhaul.durable import acts
from longhaul.github.intake import MAX_BODY_BYTES

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

BASE = "d16bfe05a744909de4b27f5875fe0d4ed41ce607"
TEST_PATCH = """\
diff --git a/astropy/modeling/tests/test_separable.py b/astropy/modeling/tests/test_separable.py
--- a/astropy/modeling/tests/test_separable.py
+++ b/astropy/modeling/tests/test_separable.py
@@ -28,6 +28,7 @@
 p1 = models.Polynomial1D(1, name='p1')
+cm_4d_expected = 1
"""
GOLD = """\
diff --git a/astropy/modeling/separable.py b/astropy/modeling/separable.py
--- a/astropy/modeling/separable.py
+++ b/astropy/modeling/separable.py
@@ -242,7 +242,7 @@
-        cright[-right.shape[0]:, -right.shape[1]:] = 1
+        cright[-right.shape[0]:, -right.shape[1]:] = right
"""


def eval_script(command: str, *, base: str = BASE) -> str:
    """The shape of eval_script in the dataset: the setup, the test patch, one marked command."""
    return "\n".join(
        [
            "#!/bin/bash",
            "set -uxo pipefail",
            "source /opt/miniconda3/bin/activate",
            "conda activate testbed",
            "cd /testbed",
            f"git -c core.fileMode=false diff {base}",
            "python -m pip install -e .[test] --verbose",
            f"git checkout {base} astropy/modeling/tests/test_separable.py",
            "git apply -v - <<'EOF_114329324912'",
            TEST_PATCH.rstrip("\n"),
            "EOF_114329324912",
            ": '>>>>> Start Test Output'",
            command,
            ": '>>>>> End Test Output'",
            f"git checkout {base} astropy/modeling/tests/test_separable.py",
            "",
        ]
    )


def make_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "instance_id": "astropy__astropy-12907",
        "repo": "astropy/astropy",
        "base_commit": BASE,
        "problem_statement": (
            "Modeling's separability_matrix is wrong\nConsider the model:\n\nit fails"
        ),
        "test_patch": TEST_PATCH,
        "patch": GOLD,
        "image": "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest",
        "eval_script": eval_script("pytest -rA astropy/modeling/tests/test_separable.py"),
        "difficulty": "15 min - 1 hour",
        "FAIL_TO_PASS": ["astropy/modeling/tests/test_separable.py::test_separable"],
        "hints_text": "a hint that must not reach the agent",
    }
    return {**row, **changes}


def make_instance(**changes: Any) -> Instance:
    return adapter.instance_from_row(make_row(**changes))


# ---------- the instance ----------
def test_a_row_of_the_dataset_becomes_an_instance() -> None:
    instance = make_instance()

    assert instance.instance_id == instance.id == "astropy__astropy-12907"
    assert instance.base_commit == BASE
    assert instance.image == "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"
    assert instance.test_patch == TEST_PATCH
    assert instance.patch == GOLD


def test_a_row_that_misses_a_field_names_it() -> None:
    row = make_row()
    del row["image"]
    del row["eval_script"]

    with pytest.raises(ValueError, match="eval_script.*image|image.*eval_script"):
        adapter.instance_from_row(row)


def test_an_instance_id_that_could_leave_a_folder_is_refused() -> None:
    with pytest.raises(ValueError, match="instance_id"):
        make_instance(instance_id="../../etc")


def test_a_base_commit_must_be_a_full_sha() -> None:
    with pytest.raises(ValueError, match="base_commit"):
        make_instance(base_commit="main")


def test_the_dataset_name_is_the_one_that_carries_the_eval_script() -> None:
    # princeton-nlp/SWE-bench_Verified has the same 500 rows but no image or eval_script column,
    # which the harness of swebench 5.x reads.
    assert adapter.DATASET == "SWE-bench/SWE-bench_Verified"


# ---------- the test command hint ----------
@pytest.mark.parametrize(
    ("command", "hint"),
    [
        (
            "pytest -rA astropy/modeling/tests/test_separable.py",
            "pytest -rA",
        ),
        (
            "pytest -rA -vv -o console_output_style=classic --tb=no "
            "astropy/modeling/tests/test_separable.py",
            "pytest -rA -vv -o console_output_style=classic --tb=no",
        ),
        (
            "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 "
            "validators.tests model_fields.test_charfield",
            "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1",
        ),
        (
            "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 "
            "cache.tests deprecation.test_middleware_mixin runtests",
            "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1",
        ),
        (
            "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose "
            "astropy/modeling/tests/test_separable.py",
            "PYTHONWARNINGS=ignore::UserWarning,ignore::SyntaxWarning bin/test -C --verbose",
        ),
        (
            "tox --current-env -epy39 -v -- astropy/modeling/tests/test_separable.py",
            "tox --current-env -epy39 -v --",
        ),
    ],
)
def test_the_hint_is_the_test_command_without_the_files_of_the_test_patch(
    command: str, hint: str
) -> None:
    assert adapter.test_command_hint(eval_script(command), TEST_PATCH) == hint


def test_a_command_without_targets_is_the_hint_as_it_is() -> None:
    command = "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1"

    assert adapter.test_command_hint(eval_script(command), TEST_PATCH) == command


def test_the_hint_is_a_command_the_shell_reads_as_the_script_does() -> None:
    hint = adapter.test_command_hint(
        eval_script(
            "PYTHONWARNINGS='ignore::UserWarning' bin/test -C "
            "astropy/modeling/tests/test_separable.py"
        ),
        TEST_PATCH,
    )

    assert hint is not None
    assert shlex.split(hint) == ["PYTHONWARNINGS=ignore::UserWarning", "bin/test", "-C"]


def test_a_script_without_the_markers_gives_no_hint() -> None:
    assert adapter.test_command_hint("#!/bin/bash\npytest -rA\n", TEST_PATCH) is None


def test_the_hint_names_none_of_the_files_the_agent_must_not_be_told_about() -> None:
    hint = adapter.test_command_hint(eval_script("pytest -rA a.py"), TEST_PATCH)

    assert hint is not None
    assert "test_separable" not in hint


# ---------- the task spec ----------
def test_the_spec_is_a_local_task_at_the_base_commit(tmp_path: Path) -> None:
    instance = make_instance()

    spec = adapter.build_spec(instance, "lh-eval-r1-astropy__astropy-12907", tmp_path / "repo")

    assert spec.task_id == "lh-eval-r1-astropy__astropy-12907"
    assert spec.repo == f"local:{tmp_path / 'repo'}"
    assert spec.settings.repo == spec.repo
    assert spec.base_sha == BASE
    assert spec.issue_number is None
    assert spec.comments == []


def test_the_problem_statement_is_the_issue_and_the_hints_stay_out(tmp_path: Path) -> None:
    spec = adapter.build_spec(make_instance(), "lh-eval-r1-a", tmp_path)

    assert spec.title == "Modeling's separability_matrix is wrong"
    assert spec.body == "Consider the model:\n\nit fails"
    assert "hint" not in spec.title + spec.body + "".join(spec.comments)


def test_a_one_line_problem_statement_is_its_own_title_and_body(tmp_path: Path) -> None:
    spec = adapter.build_spec(
        make_instance(problem_statement="Only a title"), "lh-eval-r1-a", tmp_path
    )

    assert spec.title == "Only a title"
    assert spec.body == "Only a title"


def test_a_long_problem_statement_is_cut_to_the_limit_of_every_other_task(tmp_path: Path) -> None:
    # CONTRIBUTING rule 5: the spec goes inline into many activities, so it stays under 16 KB.
    # One instance of the subset (pylint-dev__pylint-7080) has a problem statement of 24 KB.
    text = "Title\n" + "x" * (MAX_BODY_BYTES + 20_000)

    spec = adapter.build_spec(make_instance(problem_statement=text), "lh-eval-r1-a", tmp_path)

    assert spec.title == "Title"
    assert spec.body == "x" * MAX_BODY_BYTES + adapter.PROBLEM_CUT_NOTE
    assert len(spec.model_dump_json().encode()) < acts.INLINE_LIMIT_BYTES


def test_a_problem_statement_at_the_limit_is_kept_whole(tmp_path: Path) -> None:
    text = "Title\n" + "x" * MAX_BODY_BYTES

    spec = adapter.build_spec(make_instance(problem_statement=text), "lh-eval-r1-a", tmp_path)

    assert spec.body == "x" * MAX_BODY_BYTES


def test_the_settings_follow_the_rules_of_an_eval_run(tmp_path: Path) -> None:
    settings = adapter.build_spec(make_instance(), "lh-eval-r1-a", tmp_path).settings

    assert settings.plan_approval_required is False
    # The instance image has its environment and the repo installed, so there is nothing to set up.
    assert settings.setup_command == ""
    assert settings.test_command == "pytest -rA"
    assert settings.budgets.task_usd == 4.0
    assert settings.budgets.task_turns == 150
    assert settings.budgets.task_wall_minutes == 120


# ---------- predictions ----------
def test_a_prediction_has_the_three_fields_the_harness_reads() -> None:
    row = adapter.prediction("a__b-1", "longhaul-full", "diff --git a/x b/x\n")

    assert row == {
        "instance_id": "a__b-1",
        "model_name_or_path": "longhaul-full",
        "model_patch": "diff --git a/x b/x\n",
    }


def test_predictions_are_written_one_json_object_per_line_and_read_back(tmp_path: Path) -> None:
    rows = [
        adapter.prediction("a__b-1", "m", "diff --git a/x b/x\n"),
        adapter.prediction("a__b-2", "m", ""),
    ]
    path = tmp_path / "predictions.jsonl"

    adapter.write_predictions(path, rows)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == rows
    assert adapter.read_predictions(path) == rows


def test_the_gold_mode_submits_the_reference_patch_of_each_instance() -> None:
    instances = [make_instance(), make_instance(instance_id="a__b-2", patch="gold two")]

    rows = adapter.gold_predictions(instances)

    assert [(r["instance_id"], r["model_patch"]) for r in rows] == [
        ("astropy__astropy-12907", GOLD),
        ("a__b-2", "gold two"),
    ]
    assert {r["model_name_or_path"] for r in rows} == {"gold"}


def test_the_empty_mode_submits_nothing_for_each_instance() -> None:
    rows = adapter.empty_predictions([make_instance(), make_instance(instance_id="a__b-2")])

    assert [r["model_patch"] for r in rows] == ["", ""]
    assert {r["model_name_or_path"] for r in rows} == {"empty"}


def test_the_model_name_says_the_config_and_the_model_and_is_a_safe_file_name() -> None:
    assert adapter.model_name("full", "claude-sonnet-5-5") == "longhaul-full-claude-sonnet-5-5"
    assert "/" not in adapter.model_name("full", "vendor/model:1")


def result_in(folder: Path, task_id: str, outcome: str, patch: str | None) -> None:
    start = datetime(2026, 11, 5, tzinfo=UTC)
    results.write_result(
        folder,
        TaskResult(
            run_id="r1",
            task_id=task_id,
            workflow_id=f"lh-eval-r1-{task_id}",
            task_set="swebench",
            config="full",
            config_hash="a" * 64,
            seed=42,
            model="claude-sonnet-5-5",
            git_sha="f" * 40,
            provider=None,
            scripted=False,
            outcome=outcome,
            outcome_summary="done",
            resolved=None,
            grading="not graded yet",
            cost_usd=0.1,
            tokens=Tokens(),
            cache_ratio=0.0,
            started_at=start,
            ended_at=start,
            wall_time_s=1.0,
            turns=3,
        ),
    )
    if patch is not None:
        (folder / f"{task_id}.patch").write_text(patch, encoding="utf-8")


def test_the_predictions_of_a_run_come_from_its_result_files(tmp_path: Path) -> None:
    result_in(tmp_path, "a__b-1", "pr_opened", "diff --git a/x b/x\n")
    result_in(tmp_path, "a__b-2", "pr_opened", "")
    result_in(tmp_path, "a__b-3", "stuck", "diff --git a/y b/y\n")
    result_in(tmp_path, "a__b-4", "error", None)

    rows = adapter.predictions_for_run(tmp_path)

    by_id = {r["instance_id"]: r["model_patch"] for r in rows}
    # A task that did not finish delivered nothing, so its patch is not submitted. Every task of
    # the run still has a row, so that the harness counts it.
    assert by_id == {
        "a__b-1": "diff --git a/x b/x\n",
        "a__b-2": "",
        "a__b-3": "",
        "a__b-4": "",
    }
    assert {r["model_name_or_path"] for r in rows} == {"longhaul-full-claude-sonnet-5-5"}


def test_the_predictions_of_a_run_in_the_custom_set_are_refused(tmp_path: Path) -> None:
    result_in(tmp_path, "a__b-1", "pr_opened", "")
    path = tmp_path / "a__b-1.json"
    path.write_text(path.read_text().replace('"swebench"', '"custom"'), encoding="utf-8")

    with pytest.raises(ValueError, match="swebench"):
        adapter.predictions_for_run(tmp_path)


# ---------- the driver that eval/run.py uses ----------
class FakeImages:
    """Stands in for the Docker side: no image is built and no container runs."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.prepared: list[tuple[str, Path]] = []

    def prepare(self, instance: Instance, folder: Path) -> str:
        if self.fail is not None:
            raise self.fail
        (folder / "pkg").mkdir()
        (folder / "pkg" / "core.py").write_text("x = 1\n", encoding="utf-8")
        self.prepared.append((instance.instance_id, folder))
        return "longhaul-swebench:astropy__astropy-12907-abc12345"


async def test_a_prepared_task_has_a_folder_with_the_tree_and_the_image_it_asks_for(
    tmp_path: Path,
) -> None:
    from longhaul.agent.workspace import IMAGE_FILE, read_image_file

    images = FakeImages()
    driver = adapter.SwebenchDriver(images, work_root=tmp_path)

    async with driver.prepared(make_instance(), "lh-eval-r1-astropy__astropy-12907") as spec:
        folder = Path(spec.repo.removeprefix("local:"))
        assert (folder / "pkg" / "core.py").is_file()
        assert (folder / IMAGE_FILE).read_text(encoding="utf-8").strip() == (
            "longhaul-swebench:astropy__astropy-12907-abc12345"
        )
        assert read_image_file(folder) == "longhaul-swebench:astropy__astropy-12907-abc12345"
        assert spec.base_sha == BASE
        assert folder.parent == tmp_path

    assert not folder.exists()


async def test_the_folder_is_removed_when_the_preparation_fails(tmp_path: Path) -> None:
    driver = adapter.SwebenchDriver(FakeImages(fail=RuntimeError("no docker")), work_root=tmp_path)

    with pytest.raises(RuntimeError, match="no docker"):
        async with driver.prepared(make_instance(), "lh-eval-r1-a"):
            pytest.fail("the body must not run")

    assert list(tmp_path.iterdir()) == []


async def test_the_folder_is_removed_when_the_task_fails(tmp_path: Path) -> None:
    driver = adapter.SwebenchDriver(FakeImages(), work_root=tmp_path)

    with pytest.raises(KeyboardInterrupt):
        async with driver.prepared(make_instance(), "lh-eval-r1-a"):
            raise KeyboardInterrupt

    assert list(tmp_path.iterdir()) == []


def test_the_driver_cuts_the_test_files_out_of_the_patch() -> None:
    driver = adapter.SwebenchDriver(FakeImages())
    agent = GOLD + TEST_PATCH

    assert driver.patch(make_instance(), agent) == GOLD


async def test_the_driver_leaves_the_grading_to_the_harness() -> None:
    driver = adapter.SwebenchDriver(FakeImages())

    resolved, line = await driver.grade(make_instance(), GOLD)

    assert resolved is None
    assert "grade.sh" in line


def test_the_subset_is_read_from_its_file_in_order_and_cut_at_the_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from eval.swebench import select_subset

    path = tmp_path / "subset.txt"
    select_subset.write_subset(["a__b-1", "a__b-2", "a__b-3"], path)
    seen: list[list[str]] = []

    def load(ids: list[str], **_kw: object) -> list[Instance]:
        seen.append(list(ids))
        return [make_instance(instance_id=i) for i in ids]

    monkeypatch.setattr(adapter, "load_instances", load)

    chosen = adapter.select_instances(2, subset_file=path)

    assert [i.instance_id for i in chosen] == ["a__b-1", "a__b-2"]
    assert seen == [["a__b-1", "a__b-2"]]
