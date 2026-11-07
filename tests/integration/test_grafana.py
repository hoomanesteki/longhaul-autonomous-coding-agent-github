"""LH-704: Grafana loads the Longhaul dashboard from the provisioning files in docker/grafana.

Grafana and Prometheus come from the compose file. The fixture runs
`docker compose up -d --wait prometheus grafana`, which does nothing for services that already
run with the current files and recreates a service whose mounts or ports differ. That includes a
run from another checkout of the repo, because the compose project is the same (docs/runbook.md,
"Metrics and the dashboard"). It never runs `down` or `stop`, and it names the two services, so
the rest of the stack is not touched.

Everything is read through Grafana's own HTTP API, as a person with a browser would see it:

* the dashboard is there, provisioned from a file, and has a panel for every metric of the story;
* every query in it is valid PromQL, which Prometheus says when Grafana forwards the query.
  A dashboard that loads with a broken query would show an error in the panel and not at load.

The host port of Grafana is LH_GRAFANA_PORT, as it is for compose. The variable can come from the
environment or from the .env file next to docker-compose.yml, the two places compose reads it.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.phase7]

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_FILE = ROOT / "docker" / "grafana" / "dashboards" / "longhaul.json"
DEFAULT_GRAFANA_PORT = "3000"
COMPOSE_WAIT_S = 600
HEALTH_WAIT_S = 120

# The metrics of the story. Each one needs a panel that reads it.
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

# Grafana fills these in before it sends a query. Prometheus needs a real duration.
GRAFANA_VARIABLES = {
    "$__rate_interval": "5m",
    "${__rate_interval}": "5m",
    "$__interval": "1h",
    "${__interval}": "1h",
    "$__range": "1d",
    "${__range}": "1d",
}


# ---------- finding and starting Grafana ----------
def compose_setting(name: str, default: str) -> str:
    """The value compose would use for ${name}: the environment first, then the .env file."""
    if os.environ.get(name):
        return os.environ[name]
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.strip().partition("=")
            if key == name and value:
                return value
    return default


def get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=30) as reply:
        return json.loads(reply.read())


@pytest.fixture(scope="module")
def grafana() -> Iterator[str]:
    """The base URL of a Grafana that was started from this checkout's compose file."""
    started = subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", "prometheus", "grafana"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=COMPOSE_WAIT_S,
        check=False,
    )
    if started.returncode != 0:
        pytest.fail(
            f"docker compose up grafana prometheus failed:\n{started.stdout}{started.stderr}"
        )
    base = f"http://127.0.0.1:{compose_setting('LH_GRAFANA_PORT', DEFAULT_GRAFANA_PORT)}"
    deadline = time.monotonic() + HEALTH_WAIT_S
    while True:
        try:
            if get(f"{base}/api/health").get("database") == "ok":
                break
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if time.monotonic() > deadline:
            pytest.fail(f"Grafana at {base} did not become healthy in {HEALTH_WAIT_S} s")
        time.sleep(1)
    yield base


# ---------- reading the dashboard ----------
def panels_of(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Every panel, also the ones inside a collapsed row."""
    found: list[dict[str, Any]] = []
    for panel in dashboard.get("panels", []):
        found.append(panel)
        found.extend(panel.get("panels", []))
    return found


def queries_of(panel: dict[str, Any]) -> list[tuple[str, str]]:
    """(datasource uid, expression) of each Prometheus query in a panel."""
    found = []
    for target in panel.get("targets", []):
        source = target.get("datasource") or panel.get("datasource") or {}
        found.append((source.get("uid", ""), target["expr"]))
    return found


def promql(expression: str) -> str:
    for variable, value in GRAFANA_VARIABLES.items():
        expression = expression.replace(variable, value)
    assert "$" not in expression, f"a Grafana variable that this test cannot fill in: {expression}"
    return expression


def ask_prometheus(base: str, datasource_uid: str, expression: str) -> dict[str, Any]:
    """Send an instant query through Grafana's datasource proxy, as a panel does."""
    query = urllib.parse.urlencode({"query": promql(expression)})
    try:
        answer: dict[str, Any] = get(
            f"{base}/api/datasources/proxy/uid/{datasource_uid}/api/v1/query?{query}"
        )
    except urllib.error.HTTPError as err:
        pytest.fail(f"Prometheus refused {expression!r}: {err.code} {err.read().decode()[:300]}")
    return answer


# ---------- LH-704 acceptance criteria ----------
def test_dashboard_provisioned(grafana: str) -> None:
    """The dashboard loads from its file without errors, and every query in it parses."""
    wanted = json.loads(DASHBOARD_FILE.read_text(encoding="utf-8"))
    uid = wanted["uid"]

    hits = get(f"{grafana}/api/search?{urllib.parse.urlencode({'type': 'dash-db'})}")
    assert [hit["title"] for hit in hits if hit["uid"] == uid] == [wanted["title"]], (
        "Grafana lists no dashboard with the uid of docker/grafana/dashboards/longhaul.json; "
        "is the dashboards folder mounted, and did the provider file load?"
    )

    served = get(f"{grafana}/api/dashboards/uid/{uid}")
    assert served["meta"]["provisioned"] is True
    assert served["meta"]["provisionedExternalId"] == DASHBOARD_FILE.name
    dashboard = served["dashboard"]
    assert dashboard["title"] == wanted["title"]
    assert len(panels_of(dashboard)) == len(panels_of(wanted)) > 0

    queries = [(panel["title"], q) for panel in panels_of(dashboard) for q in queries_of(panel)]
    assert queries, "the dashboard has no queries"

    # A panel for each metric of the story.
    for name in METRICS:
        assert any(name in expression for _, (_, expression) in queries), f"no panel shows {name}"

    # Cost per repo per day, and the rates of the outcomes.
    cost = [e for t, (_, e) in queries if "per day" in t.lower() and "repo" in t.lower()]
    assert cost, "no panel is titled as the cost per repo per day"
    assert all("longhaul_llm_cost_usd_total" in e and "by (repo)" in e for e in cost)
    outcomes = [e for t, (_, e) in queries if "outcome" in t.lower()]
    assert outcomes, "no panel is titled as the outcomes"
    assert all("longhaul_tasks_total" in e and "by (outcome)" in e for e in outcomes)

    # Every query reaches the Prometheus datasource of the provisioning, and parses there. A
    # metric that nothing has written yet gives an empty result, which is a success.
    for title, (datasource_uid, expression) in queries:
        assert datasource_uid, f"panel {title!r} names no datasource uid"
        answer = ask_prometheus(grafana, datasource_uid, expression)
        assert answer["status"] == "success", f"panel {title!r}: {answer}"
