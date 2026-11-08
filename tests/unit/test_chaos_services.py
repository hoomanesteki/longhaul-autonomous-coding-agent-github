"""The private Temporal and Postgres of the full chaos suite (LH-904).

The suite restarts both of them on purpose, and the machine's own pair serves other builds, so the
suite starts a second compose project of its own. These tests cover what can be checked without
Docker: that the project can never be the default one, which commands start it, and that the
compose file keeps the state of Temporal across the restarts that the suite makes.
"""

import os
import socket
from pathlib import Path
from typing import Any

import pytest
import yaml

from eval.chaos import services
from eval.chaos.services import Ports

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
PROJECT = "longhaul-chaos-ab12cd34"
PORTS = Ports(postgres=50471, temporal=50472, temporal_ui=50473)


# ---------- the name of the project ----------
def test_a_new_project_has_the_prefix_and_a_suffix_of_its_own() -> None:
    names = {services.new_project() for _ in range(20)}

    assert len(names) == 20
    assert all(name.startswith(services.PROJECT_PREFIX) for name in names)
    assert all(services.check_project(name) == name for name in names)


@pytest.mark.parametrize("name", ["longhaul", "longhaul-chaos-", "keystone", "", "longhaul-chaos"])
def test_the_default_project_and_other_projects_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="project"):
        services.check_project(name)


def test_a_project_name_cannot_carry_a_shell_or_a_label_filter() -> None:
    with pytest.raises(ValueError, match="project"):
        services.check_project("longhaul-chaos-a;rm -rf")


# ---------- the commands ----------
def test_the_project_starts_postgres_and_temporal_and_nothing_else() -> None:
    argv = services.up_command(PROJECT)

    assert argv == [
        "docker",
        "compose",
        "-p",
        PROJECT,
        "-f",
        str(ROOT / "docker-compose.yml"),
        "up",
        "-d",
        "--wait",
        "postgres",
        "temporal",
    ]


def test_the_default_project_cannot_be_started_by_this_module() -> None:
    with pytest.raises(ValueError, match="project"):
        services.up_command("longhaul")


def test_the_ports_reach_compose_through_the_variables_its_file_reads() -> None:
    env = services.compose_env(PORTS)

    assert env == {
        "LH_POSTGRES_PORT": "50471",
        "LH_TEMPORAL_PORT": "50472",
        "LH_TEMPORAL_UI_PORT": "50473",
        "LH_POSTGRES_PASSWORD": services.POSTGRES_PASSWORD,
    }
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for name in env:
        assert f"${{{name}:-" in text, f"docker-compose.yml does not read {name}"


def test_what_the_suite_touches_is_found_by_the_label_of_its_own_project() -> None:
    assert services.label_filters(PROJECT) == {"label": [f"com.docker.compose.project={PROJECT}"]}
    assert services.label_filters(PROJECT, "temporal") == {
        "label": [
            f"com.docker.compose.project={PROJECT}",
            "com.docker.compose.service=temporal",
        ]
    }


def test_only_postgres_and_temporal_can_be_restarted() -> None:
    assert services.SERVICES == ("postgres", "temporal")
    with pytest.raises(ValueError, match="minio"):
        services.check_service("minio")


# ---------- the ports ----------
def test_free_ports_are_distinct_and_can_be_bound() -> None:
    found = services.free_ports(3)

    assert len(set(found)) == 3
    for port in found:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))


def test_the_database_address_is_the_private_postgres_with_the_compose_credentials() -> None:
    assert services.database_url(PORTS) == "postgresql://longhaul:longhaul@127.0.0.1:50471/longhaul"
    assert services.temporal_address(PORTS) == "127.0.0.1:50472"


# ---------- the compose file ----------
def test_temporal_keeps_its_state_in_a_file_on_a_volume() -> None:
    # Without this a restart of the server loses every workflow, and the suite measures nothing.
    temporal = COMPOSE["services"]["temporal"]
    command = temporal["command"]

    assert "--db-filename" in command
    path = command[command.index("--db-filename") + 1]
    mounts = [v.split(":") for v in temporal["volumes"]]
    assert any(path.startswith(target.rstrip("/") + "/") for _, target, *_ in mounts), path
    assert "temporal-data" in COMPOSE["volumes"]


def test_postgres_keeps_its_data_on_a_volume() -> None:
    mounts = [v.split(":") for v in COMPOSE["services"]["postgres"]["volumes"]]

    assert any(target == "/var/lib/postgresql/data" for _, target, *_ in mounts)


def test_every_published_port_of_the_two_services_is_bound_to_this_machine() -> None:
    for name in services.SERVICES:
        for published in COMPOSE["services"][name]["ports"]:
            assert published.startswith("127.0.0.1:"), (name, published)


# ---------- the watchdog ----------
class FakeContainer:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def restart(self, timeout: int) -> None:
        self.log.append("restart")


class FakeDocker:
    """Enough of the docker client for a restart: it holds the containers it is asked for."""

    def __init__(self, count: int = 1) -> None:
        self.log: list[str] = []
        self.count = count
        self.filters: list[Any] = []
        self.containers = self

    def list(self, filters: Any = None, **_: Any) -> list[FakeContainer]:
        self.filters.append(filters)
        return [FakeContainer(self.log) for _ in range(self.count)]

    def close(self) -> None:
        pass


def test_the_services_have_a_watchdog_that_is_not_running_until_they_start() -> None:
    private = services.PrivateServices(PROJECT, PORTS)

    assert private.watchdog.heals == 0
    assert not private.watchdog.running


def test_a_restart_that_a_kill_makes_resets_the_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDocker()
    monkeypatch.setattr(services.docker, "from_env", lambda: fake)
    private = services.PrivateServices(PROJECT, PORTS)
    resets: list[str] = []
    monkeypatch.setattr(private.watchdog, "reset", lambda: resets.append("reset"))

    assert private.restart("temporal") is True
    assert private.restart("postgres") is True

    assert fake.log == ["restart", "restart"]
    assert resets == ["reset"], "only a restart of Temporal says anything about the watchdog"


def test_the_watchdog_restarts_the_container_of_its_own_project_and_counts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDocker()
    monkeypatch.setattr(services.docker, "from_env", lambda: fake)
    private = services.PrivateServices(PROJECT, PORTS)

    private.heal_temporal()

    assert fake.log == ["restart"]
    assert fake.filters == [services.label_filters(PROJECT, "temporal")]


def test_a_missing_container_is_a_failed_restart_and_not_a_counted_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(services.docker, "from_env", lambda: FakeDocker(count=0))
    private = services.PrivateServices(PROJECT, PORTS)

    with pytest.raises(RuntimeError, match="no Temporal container"):
        private.heal_temporal()


def test_the_watchdog_probes_the_private_temporal_and_not_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        services.watchdog, "temporal_answers", lambda address: asked.append(address) or True
    )
    private = services.PrivateServices(PROJECT, PORTS)

    assert private.watchdog.check() is False

    assert asked == ["127.0.0.1:50472"]


def test_the_password_the_suite_connects_with_is_the_one_compose_gives_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LH_POSTGRES_PASSWORD", "from-the-owner-env")

    env = {**os.environ, **services.compose_env(PORTS)}

    assert env["LH_POSTGRES_PASSWORD"] == services.POSTGRES_PASSWORD
    assert f":{services.POSTGRES_PASSWORD}@" in services.database_url(PORTS)
