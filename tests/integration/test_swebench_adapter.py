"""The SWE-bench Verified adapter against the official harness (LH-902).

These tests run the real `swebench` package. They need the eval extra (`uv sync --extra eval`),
a Docker daemon, and the Hugging Face dataset (network). Both tests are marked `slow`, so
`make itest` and the gates leave them out. Run them by hand on the eval VM:

    uv sync --extra eval
    uv run pytest -m slow tests/integration/test_swebench_adapter.py

`test_gold_patch_sanity` also pulls the images of two instances, which are several GB each, and
the harness leaves them on the machine. It stops before it starts when less than 30 GB of disk
is free, because a full disk makes the harness fail in ways that look like bad patches.
`test_empty_patch_reports_zero_of_two` pulls no image: the harness does not start a container
for an empty patch.

Every file the tests make is in a temporary folder, and the harness is given that folder as its
working directory, so its logs/ folder does not land in the repo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eval import results
from eval.results import TaskResult, Tokens
from eval.swebench import adapter, report

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
GRADE = ROOT / "eval" / "swebench" / "grade.sh"
# Two small repos of the subset's dataset: the only flask instance, and a requests one. Their
# images are among the smaller ones. The ids can be changed without touching the test logic.
IDS = ["pallets__flask-5014", "psf__requests-2317"]
MIN_FREE_GB = 30
GRADING_TIMEOUT_S = 3600


def require_swebench() -> None:
    try:
        import swebench  # noqa: F401
    except ImportError:
        pytest.fail("the swebench package is missing; run `uv sync --extra eval` first")


def require_docker() -> None:
    import docker

    try:
        docker.from_env().ping()
    except Exception as exc:
        pytest.fail(f"Docker is not reachable: {exc}")


def make_run(folder: Path, ids: list[str], predictions: list[dict[str, str]]) -> None:
    """The folder that eval/run.py leaves for a SWE-bench run: result files and predictions."""
    start = datetime(2026, 11, 5, tzinfo=UTC)
    for task_id in ids:
        results.write_result(
            folder,
            TaskResult(
                run_id=folder.name,
                task_id=task_id,
                workflow_id=f"lh-eval-{folder.name}-{task_id}",
                task_set="swebench",
                config="full",
                config_hash="a" * 64,
                seed=42,
                model="sanity",
                git_sha="f" * 40,
                provider=None,
                scripted=False,
                outcome="pr_opened",
                outcome_summary="a stand-in for the agent",
                resolved=None,
                grading="not graded yet",
                cost_usd=0.0,
                tokens=Tokens(),
                cache_ratio=0.0,
                started_at=start,
                ended_at=start,
                wall_time_s=0.0,
                turns=0,
            ),
        )
    adapter.write_predictions(folder / "predictions.jsonl", predictions)


def grade(run_dir: Path, work_dir: Path) -> None:
    done = subprocess.run(
        ["bash", str(GRADE), str(run_dir)],
        cwd=work_dir,
        env={**os.environ, "SWEBENCH_WORK_DIR": str(work_dir)},
        capture_output=True,
        text=True,
        timeout=GRADING_TIMEOUT_S,
    )
    assert done.returncode == 0, f"grade.sh failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def test_gold_patch_sanity(tmp_path: Path) -> None:
    """The reference patch as the agent's output: the grader must say 2 of 2 resolved."""
    require_swebench()
    require_docker()
    docker_root = Path("/var/lib/docker")
    free_gb = shutil.disk_usage(docker_root if docker_root.exists() else Path.home()).free / 1e9
    assert free_gb >= MIN_FREE_GB, (
        f"only {free_gb:.0f} GB of disk is free, and the images of two instances need several GB; "
        f"leave at least {MIN_FREE_GB} GB free, or run this on the eval VM"
    )
    instances = adapter.load_instances(IDS)
    run_dir = tmp_path / "gold-sanity"
    make_run(run_dir, IDS, adapter.gold_predictions(instances))

    grade(run_dir, tmp_path)

    found = results.read_results(run_dir)
    assert [(r.task_id, r.resolved) for r in found] == [(i, True) for i in sorted(IDS)]
    summary = results.Summary.model_validate_json((run_dir / "summary.json").read_text())
    assert (summary.attempted, summary.resolved, summary.ungraded) == (2, 2, 0)


def test_empty_patch_reports_zero_of_two(tmp_path: Path) -> None:
    """The same two instances with an empty patch: nothing is resolved and nothing is ungraded."""
    require_swebench()
    require_docker()
    instances = adapter.load_instances(IDS)
    run_dir = tmp_path / "empty-sanity"
    make_run(run_dir, IDS, adapter.empty_predictions(instances))

    grade(run_dir, tmp_path)

    found = results.read_results(run_dir)
    assert [(r.task_id, r.resolved) for r in found] == [(i, False) for i in sorted(IDS)]
    assert all("empty" in r.grading for r in found)
    summary = results.Summary.model_validate_json((run_dir / "summary.json").read_text())
    assert (summary.attempted, summary.resolved, summary.ungraded) == (2, 0, 0)


def test_the_report_of_the_harness_has_the_fields_the_parser_reads() -> None:
    """Guards the shape of swebench's report against a version change of the package."""
    require_swebench()
    from swebench.harness import reporting

    source = Path(reporting.__file__).read_text(encoding="utf-8")
    for field in report.REPORT_FIELDS:
        assert f'"{field}"' in source, f"swebench no longer writes {field}; update report.py"
