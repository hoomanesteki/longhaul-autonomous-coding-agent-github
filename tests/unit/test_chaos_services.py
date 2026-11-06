"""The private Temporal and Postgres of the full chaos suite (LH-904).

The suite restarts both of them on purpose, and the machine's own pair serves other builds, so the
suite starts a second compose project of its own. These tests cover what can be checked without
Docker: that the project can never be the default one, which commands start it, and that the
compose file keeps the state of Temporal across the restarts that the suite makes.
"""

import socket
from pathlib import Path

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
