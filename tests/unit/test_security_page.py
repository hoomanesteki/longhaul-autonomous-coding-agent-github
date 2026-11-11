"""The checks behind docs/security.md.

The page names a test for each control, and a name that no longer exists would read as proof
that is gone. The first test reads the page and finds every test it names. The others are the
small tests that stand behind rows which had none: where the stack listens, where the API
listens, and what the worker runs on the host.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from longhaul.checkpoints import git_store, snapshot
from longhaul.settings import EnvSettings

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "security.md"
TESTS = ROOT / "tests"
SRC = ROOT / "src" / "longhaul"
COMPOSE = ROOT / "docker-compose.yml"
LOOPBACK = "127.0.0.1"


# ---------- every test the page names exists ----------
def named_tests(page: str) -> list[tuple[str, str]]:
    """The (file, function) pairs of the page, from `path::name` and the `::name` that follows.

    A row names a file once and then lists more of its tests as `::name`, so the file is the last
    one named in the row.
    """
    found: list[tuple[str, str]] = []
    for line in page.splitlines():
        current: str | None = None
        for token in re.findall(r"`([^`]+)`", line):
            path, sep, name = token.partition("::")
            if not sep:
                continue
            current = path or current
            assert current, f"{token!r} names a test of no file"
            found.append((current, name))
    return found


def functions_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def test_every_test_named_on_the_page_exists() -> None:
    names = named_tests(PAGE.read_text(encoding="utf-8"))
    assert len(names) > 50, "the page names many tests, so a short list means the reader broke"

    missing = []
    for file, name in names:
        path = TESTS / file
        if not path.is_file() or name not in functions_of(path):
            missing.append(f"{file}::{name}")

    assert missing == []


def test_the_reader_follows_a_row_that_names_the_file_once() -> None:
    page = "| a | `unit/test_a.py::test_one`, `::test_two`, `integration/test_b.py::test_three` |\n"

    assert named_tests(page) == [
        ("unit/test_a.py", "test_one"),
        ("unit/test_a.py", "test_two"),
        ("integration/test_b.py", "test_three"),
    ]


# ---------- CI audits what the page says it audits ----------
def test_the_ci_audit_covers_every_extra() -> None:
    # The eval extra is large and CI does not install it, but an audit only reads versions. A
    # dependency of an extra that nobody installs in CI is still one that a developer installs.
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["security"]["steps"]

    exports = [step["run"] for step in steps if "uv export" in step.get("run", "")]

    assert len(exports) == 1
    assert "--all-extras" in exports[0]
    assert "pip-audit" in exports[0]


# ---------- the stack listens on this machine only ----------
def published_ports(compose: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (service, str(port))
        for service, spec in compose["services"].items()
        for port in spec.get("ports", [])
    ]


def test_every_compose_port_is_published_on_loopback_only() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    ports = published_ports(compose)

    assert ports, "the stack publishes ports, so an empty list means the reader broke"
    # "127.0.0.1:5432:5432". A short form such as "5432:5432" binds every interface.
    assert [(s, p) for s, p in ports if not p.startswith(f"{LOOPBACK}:")] == []


def test_a_port_without_an_address_would_be_caught() -> None:
    compose = {"services": {"db": {"ports": ["5432:5432"]}, "ok": {"ports": ["127.0.0.1:1:1"]}}}

    ports = published_ports(compose)

    assert [(s, p) for s, p in ports if not p.startswith(f"{LOOPBACK}:")] == [("db", "5432:5432")]


# ---------- the API and the metrics endpoint listen on this machine only ----------
def test_the_api_and_the_metrics_endpoint_default_to_this_machine() -> None:
    fields = EnvSettings.model_fields

    assert fields["api_host"].default == LOOPBACK
    assert fields["metrics_host"].default == LOOPBACK


# ---------- the worker never runs repo code ----------
# The modules of the package that start a process. Each is on the list for a reason:
# git_store and snapshot run git on the host, with hooks and the other config switches that run
# code turned off. supervisor.py runs inside the sandbox container and never on the worker. The
# local manager is a fake for unit tests that runs a shell on the machine, and no code of the
# package imports it.
SPAWNERS = {
    "checkpoints/git_store.py",
    "checkpoints/snapshot.py",
    "sandbox/local_manager.py",
    "sandbox/supervisor.py",
}
SPAWN_CALLS = {
    "create_subprocess_exec",
    "create_subprocess_shell",
    "Popen",
    "system",
    "popen",
    "execv",
    "execve",
    "execvp",
}


def spawns_processes(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in SPAWN_CALLS:
                return True
            if isinstance(func, ast.Attribute) and getattr(func.value, "id", "") == "subprocess":
                return True
    return False


def test_only_the_known_modules_start_a_process() -> None:
    found = {
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if spawns_processes(path.read_text(encoding="utf-8"))
    }

    assert found == SPAWNERS, (
        "a module that starts a process has to be one that runs no repo code, "
        "and then it goes on SPAWNERS and on the page"
    )


def test_the_spawn_scan_sees_each_way_to_start_a_process() -> None:
    assert spawns_processes("await asyncio.create_subprocess_exec('git')")
    assert spawns_processes("subprocess.run(['ls'])")
    assert spawns_processes("os.system('ls')")
    assert spawns_processes("os.execv('/bin/sh', [])")
    assert not spawns_processes("print('subprocess')")


def test_the_fake_sandbox_is_imported_by_no_module_of_the_package() -> None:
    importers = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "local_manager" in path.read_text(encoding="utf-8") and path.name != "local_manager.py"
    ]

    assert importers == []


def test_git_on_the_host_runs_no_hook_and_no_fsmonitor_program() -> None:
    # The sandbox can rewrite the config and the hooks of the repo it has mounted.
    host = " ".join(git_store._HOST_FLAGS)
    assert "core.hooksPath=/dev/null" in host
    assert "core.fsmonitor=false" in host
    assert "core.hooksPath=/dev/null" in " ".join(snapshot._SETTINGS)
