"""The Grafana files of LH-704, checked without Grafana.

tests/integration/test_grafana.py loads them into a real Grafana. These checks run everywhere
and catch the common slips early: a metric without a panel, a datasource uid that does not match
the provisioned datasource, and a provider file whose path is not the folder compose mounts.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.phase7]

ROOT = Path(__file__).resolve().parents[2]
GRAFANA = ROOT / "docker" / "grafana"
DASHBOARD = GRAFANA / "dashboards" / "longhaul.json"
PROVIDER = GRAFANA / "provisioning" / "dashboards" / "longhaul.yaml"
DATASOURCE = GRAFANA / "provisioning" / "datasources" / "prometheus.yaml"
COMPOSE = ROOT / "docker-compose.yml"

METRICS = (
    "longhaul_tasks_total",
    "longhaul_active_tasks",
    "longhaul_turns_total",
    "longhaul_llm_cost_usd_total",
    "longhaul_llm_errors_total",
    "longhaul_tool_duration_seconds",
    "longhaul_compactions_total",
    "longhaul_stuck_total",
    "longhaul_sandbox_recreated_total",
    "longhaul_activity_retries_total",
    "longhaul_cache_ratio",
)


def dashboard() -> dict[str, Any]:
    loaded = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def panels() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for panel in dashboard()["panels"]:
        found.append(panel)
        found.extend(panel.get("panels", []))
    return found


def expressions(panel: dict[str, Any]) -> list[str]:
    return [target["expr"] for target in panel.get("targets", [])]


def test_every_metric_of_the_story_has_a_panel() -> None:
    shown = " ".join(e for panel in panels() for e in expressions(panel))
    assert [name for name in METRICS if name not in shown] == []


def test_the_dashboard_has_a_cost_per_repo_per_day_panel() -> None:
    (panel,) = [p for p in panels() if "per day" in p["title"].lower()]
    (expr,) = expressions(panel)
    assert "longhaul_llm_cost_usd_total" in expr and "by (repo)" in expr
    assert "increase(" in expr, "a counter shows a day's spend as its increase over the day"


def test_the_dashboard_has_an_outcome_rate_panel() -> None:
    (panel,) = [p for p in panels() if "outcome" in p["title"].lower()]
    (expr,) = expressions(panel)
    assert "longhaul_tasks_total" in expr and "by (outcome)" in expr


def test_panels_have_titles_and_do_not_overlap() -> None:
    cells: dict[tuple[int, int], str] = {}
    for panel in panels():
        assert panel["title"].strip(), panel
        box = panel["gridPos"]
        for x in range(box["x"], box["x"] + box["w"]):
            for y in range(box["y"], box["y"] + box["h"]):
                other = cells.setdefault((x, y), panel["title"])
                assert other == panel["title"], f"{other!r} and {panel['title']!r} overlap"
        assert box["x"] + box["w"] <= 24, f"{panel['title']!r} is wider than the 24 columns"


def test_panel_ids_are_unique() -> None:
    ids = [p["id"] for p in panels()]
    assert len(ids) == len(set(ids))


def test_every_query_reads_the_provisioned_prometheus_datasource() -> None:
    wanted = yaml.safe_load(DATASOURCE.read_text(encoding="utf-8"))["datasources"][0]
    assert wanted["type"] == "prometheus" and wanted["uid"]
    for panel in panels():
        for target in panel.get("targets", []):
            source = target.get("datasource") or panel["datasource"]
            assert source == {"type": "prometheus", "uid": wanted["uid"]}, panel["title"]


def test_the_dashboard_has_the_fields_grafana_needs_to_provision_it() -> None:
    loaded = dashboard()
    assert loaded["uid"] and loaded["title"] and loaded["schemaVersion"]
    # The file is the only copy: an id from another Grafana would clash, and a version would
    # make Grafana refuse the file as older than the one it holds.
    assert "id" not in loaded or loaded["id"] is None
    assert "version" not in loaded


def test_the_provider_reads_the_folder_that_compose_mounts() -> None:
    provider = yaml.safe_load(PROVIDER.read_text(encoding="utf-8"))
    (entry,) = provider["providers"]
    assert entry["type"] == "file"
    compose = COMPOSE.read_text(encoding="utf-8")
    mount = re.search(r"\./docker/grafana/dashboards:(\S+?)(?::ro)?\s*$", compose, re.M)
    assert mount, "docker-compose.yml does not mount docker/grafana/dashboards"
    assert entry["options"]["path"] == mount.group(1)


def test_a_dashboard_is_not_edited_in_grafana_and_lost() -> None:
    # The file is the source. Edits in the UI would be overwritten, so the provider says so.
    (entry,) = yaml.safe_load(PROVIDER.read_text(encoding="utf-8"))["providers"]
    assert entry["allowUiUpdates"] is False
