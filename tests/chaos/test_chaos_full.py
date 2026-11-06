"""The full chaos suite, run short, and what it writes (LH-904).

`eval/chaos/chaos.py --runs 20 --seed 7` starts a Temporal and a Postgres of its own, plays four
recorded custom tasks with one to three kills each, and writes `summary.json`. The kills are of
four kinds: the worker (SIGKILL), the sandbox (`docker kill`), the Temporal server and Postgres
(a restart of the container). The runs deliver to a FakeGitHub, so the writes to GitHub are
counted from its state.

This test runs the first two runs of a plan and checks that the summary has every field and
that the numbers in it agree with each other. The 20 runs are run by hand (docs/runbook.md, "The
full chaos suite"); they take about an hour.

Run `make sandbox-image` first, and have Docker and the compose stack's MinIO up (`make up`). The
suite starts and removes its own Temporal and Postgres.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import docker
import pytest

from eval.chaos import chaos, results
from eval.chaos.plan import KILL_TYPES, RECORDED_TASKS

pytestmark = [pytest.mark.chaos, pytest.mark.phase9]

RUNS = 2
# The first two runs of seed 7 both kill the sandbox. This seed makes calc-01 and calc-02 meet
# the four kinds between them (Postgres and Temporal, then the sandbox and the worker), so the
# short suite goes through every kind of strike. test_the_seed_of_the_short_suite_hits_every_kind
# in tests/unit/test_chaos_plan.py keeps it so.
SEED = 43


@pytest.fixture(scope="module")
def summary(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    folder = tmp_path_factory.mktemp("results")

    assert chaos.main(["--runs", str(RUNS), "--seed", str(SEED), "--results-dir", str(folder)]) == 0

    (written,) = folder.glob("chaos-*/summary.json")
    data: dict[str, Any] = json.loads(written.read_text(encoding="utf-8"))
    data["_folder"] = str(written.parent)
    return data


def test_suite_summary_fields(summary: dict[str, Any]) -> None:
    assert set(results.SUMMARY_FIELDS) <= set(summary)
    assert (summary["runs"], summary["seed"]) == (RUNS, SEED)
    assert len(summary["results"]) == RUNS
    for found in summary["results"]:
        assert set(results.RUN_FIELDS) <= set(found), found.keys()
        for kill in found["kills"]:
            assert set(results.KILL_FIELDS) <= set(kill)
            assert kill["type"] in KILL_TYPES
    for stats in (summary["resume_latency"], *summary["resume_latency_by_type"].values()):
        assert set(stats) == {"count", "median_s", "p95_s", "max_s"}


def test_the_totals_of_the_summary_agree_with_its_runs(summary: dict[str, Any]) -> None:
    found = summary["results"]

    assert summary["completed"] == sum(r["completed"] for r in found)
    assert summary["diff_matches"] == sum(r["diff_equals_reference"] for r in found)
    assert summary["extra_ledger_rows"] == sum(r["extra_ledger_rows"] for r in found)
    assert summary["duplicate_side_effects"] == sum(r["duplicate_side_effects"] for r in found)
    assert sum(summary["kills_planned"].values()) == sum(len(r["kills_planned"]) for r in found)
    assert sum(summary["kills_landed"].values()) == sum(
        k["landed"] for r in found for k in r["kills"]
    )
    assert summary["resume_latency"]["count"] == sum(len(r["resume_latency_s"]) for r in found)


def test_the_runs_follow_the_plan_of_the_seed(summary: dict[str, Any]) -> None:
    assert [r["task_id"] for r in summary["results"]] == list(RECORDED_TASKS[:RUNS])
    assert summary["tasks"] == list(RECORDED_TASKS[:RUNS])
    assert set(summary["recordings"]) == set(summary["tasks"])


def test_the_side_effects_are_the_ones_fakegithub_holds(summary: dict[str, Any]) -> None:
    for found in summary["results"]:
        assert found["completed"], found["problems"]
        assert found["side_effects"] == found["expected_side_effects"]
        assert found["duplicate_side_effects"] == found["missing_side_effects"] == 0
    assert summary["duplicate_side_effects"] == 0


def test_the_runs_survived_their_kills(summary: dict[str, Any]) -> None:
    for found in summary["results"]:
        assert found["problems"] == []
        assert found["diff_equals_reference"]
    assert summary["survived"] == RUNS


def test_a_kill_that_landed_has_a_time_and_a_place(summary: dict[str, Any]) -> None:
    landed = [k for r in summary["results"] for k in r["kills"] if k["landed"]]

    assert landed, "no kill landed in two runs"
    for kill in landed:
        assert kill["struck_at"] and kill["place"]


def test_the_suite_removes_its_own_services(summary: dict[str, Any]) -> None:
    client = docker.from_env()
    try:
        label = f"com.docker.compose.project={summary['compose_project']}"
        assert client.containers.list(all=True, filters={"label": label}) == []
        assert client.volumes.list(filters={"label": label}) == []
    finally:
        client.close()


def test_the_summary_is_written_to_a_folder_named_for_the_day(summary: dict[str, Any]) -> None:
    folder = Path(summary["_folder"])

    assert folder.name.startswith("chaos-")
    assert sorted(p.name for p in folder.glob("run-*.json")) == ["run-01.json", "run-02.json"]
