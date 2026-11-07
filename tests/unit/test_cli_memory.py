"""The `longhaul memory` commands, with a fake store in place of Postgres (LH-603).

The commands start their own event loop with asyncio.run, so these tests are plain functions.
tests/integration/test_memory_store.py runs the store that they call.
"""

from __future__ import annotations

import getpass
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner, Result

from longhaul import cli
from longhaul.db import session as db_session
from longhaul.memory.store import MemoryEntry, MemoryItemNotFoundError
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.unit, pytest.mark.phase6]

RUNNER = CliRunner()
REPO = "acme/inventory"
ID = "6f1c1c0e-5b7e-4b0a-9b1e-0f4c1f7d2a11"
WHEN = datetime(2026, 11, 3, 9, 30, tzinfo=UTC)


def entry(content: str, **fields: Any) -> MemoryEntry:
    values: dict[str, Any] = {
        "id": ID,
        "repo": REPO,
        "kind": "lesson",
        "status": "candidate",
        "source": "task",
        "source_ref": "42",
        "approved_by": None,
        "uses": 0,
        "last_used_at": None,
        "created_at": WHEN,
    }
    values.update(fields)
    return MemoryEntry(content=content, **values)


class FakeStore:
    def __init__(self, entries: list[MemoryEntry] | None = None) -> None:
        self.entries = entries or []
        self.calls: list[tuple[Any, ...]] = []
        self.fail: Exception | None = None

    async def list_items(
        self, repo: str, *, status: str | None = None, kind: str | None = None
    ) -> list[MemoryEntry]:
        self.calls.append(("list", repo, status, kind))
        if self.fail:
            raise self.fail
        return self.entries

    async def promote(self, item_id: str, by: str) -> None:
        self.calls.append(("promote", item_id, by))
        if self.fail:
            raise self.fail

    async def reject(self, item_id: str, by: str) -> None:
        self.calls.append(("reject", item_id, by))
        if self.fail:
            raise self.fail


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fake = FakeStore()

    @asynccontextmanager
    async def opened() -> AsyncIterator[FakeStore]:
        yield fake

    monkeypatch.setattr(cli, "open_memory", opened)
    return fake


def run(*args: str) -> Result:
    return RUNNER.invoke(cli.app, ["memory", *args])


# ---------- list ----------
def test_list_prints_a_table_of_the_items(store: FakeStore) -> None:
    store.entries = [
        entry("Raise InventoryError, not ValueError.", uses=3),
        entry(
            "pytest -q",
            id="0a9e8c20-3b0e-4f43-8d57-1d9c5d1f0a77",
            kind="command",
            status="approved",
            source="default_branch",
            approved_by="default_branch",
        ),
    ]

    result = run("list", "--repo", REPO)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["ID", "STATUS", "KIND", "SOURCE", "USES", "CONTENT"]
    assert lines[1].split()[:5] == [ID, "candidate", "lesson", "task", "3"]
    assert lines[1].endswith("Raise InventoryError, not ValueError.")
    assert lines[2].split()[:5] == [
        "0a9e8c20-3b0e-4f43-8d57-1d9c5d1f0a77",
        "approved",
        "command",
        "default_branch",
        "0",
    ]
    assert lines[2].endswith("pytest -q")
    assert len(lines) == 3
    assert store.calls == [("list", REPO, None, None)]


def test_list_passes_the_filters_on(store: FakeStore) -> None:
    result = run("list", "--repo", REPO, "--status", "approved", "--kind", "command")
    assert result.exit_code == 0, result.output
    assert store.calls == [("list", REPO, "approved", "command")]


def test_list_says_when_there_is_nothing_to_list(store: FakeStore) -> None:
    result = run("list", "--repo", REPO)
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "no memory items" in result.stderr


def test_list_keeps_one_item_on_one_line(store: FakeStore) -> None:
    store.entries = [entry("First line.\n\nSecond   line, " + "long " * 60)]
    lines = run("list", "--repo", REPO).stdout.splitlines()
    assert len(lines) == 2
    assert "First line. Second line, long long" in lines[1]
    assert lines[1].endswith("...")
    assert len(lines[1]) < 200


@pytest.mark.parametrize(
    ("option", "value", "choices"),
    [
        ("--status", "pending", "candidate, approved, rejected"),
        ("--kind", "note", "repo_map, command, convention, pitfall, lesson"),
    ],
)
def test_list_refuses_a_value_the_table_does_not_have(
    store: FakeStore, option: str, value: str, choices: str
) -> None:
    result = run("list", "--repo", REPO, option, value)
    assert result.exit_code == 2
    # The error box wraps its lines, so each choice is looked for on its own.
    assert all(choice in result.output for choice in choices.split(", "))
    assert store.calls == []


def test_list_needs_a_repo(store: FakeStore) -> None:
    assert run("list").exit_code == 2


# ---------- approve and reject ----------
@pytest.mark.parametrize(("verb", "past"), [("approve", "approved"), ("reject", "rejected")])
def test_a_decision_names_who_made_it(store: FakeStore, verb: str, past: str) -> None:
    result = run(verb, ID, "--by", "alice")

    assert result.exit_code == 0, result.output
    assert f"{past} {ID} (by alice)" in result.stdout
    call = "promote" if verb == "approve" else "reject"
    assert store.calls == [(call, ID, "alice")]


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_without_by_the_decision_names_the_user_of_the_machine(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    monkeypatch.setattr(getpass, "getuser", lambda: "hooman")
    assert run(verb, ID).exit_code == 0
    assert store.calls[0][2] == "hooman"


def test_a_machine_with_no_user_name_still_names_someone(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def nobody() -> str:
        raise OSError("no user")

    monkeypatch.setattr(getpass, "getuser", nobody)
    assert run("approve", ID).exit_code == 0
    assert store.calls[0][2] == cli.FALLBACK_APPROVER


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_an_unknown_item_is_an_error_with_a_message(store: FakeStore, verb: str) -> None:
    store.fail = MemoryItemNotFoundError(f"no memory item {ID}")
    result = run(verb, ID, "--by", "alice")
    assert result.exit_code == cli.EXIT_ERROR
    assert f"error: no memory item {ID}" in result.stderr


def test_a_database_that_cannot_be_reached_is_an_error_with_a_message(store: FakeStore) -> None:
    store.fail = OperationalError("SELECT 1", {}, ConnectionRefusedError("refused"))
    result = run("list", "--repo", REPO)
    assert result.exit_code == cli.EXIT_ERROR
    assert result.stderr.startswith("error: ")
    assert "Traceback" not in result.output


def test_a_blank_name_is_refused_by_the_store_and_reported(store: FakeStore) -> None:
    store.fail = ValueError("who approved this? the name must not be blank")
    result = run("approve", ID, "--by", " ")
    assert result.exit_code == cli.EXIT_ERROR
    assert "error: who approved this" in result.stderr


# ---------- the real store behind the commands ----------
def test_without_a_database_url_the_commands_say_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LONGHAUL_DATABASE_URL", raising=False)
    # A developer's .env can set the URL too, so the settings are read without it.
    monkeypatch.setattr(db_session, "get_settings", lambda: EnvSettings(_env_file=None))
    get_settings.cache_clear()
    try:
        result = run("list", "--repo", REPO)
    finally:
        get_settings.cache_clear()
    assert result.exit_code == cli.EXIT_ERROR
    assert "LONGHAUL_DATABASE_URL" in result.stderr


def test_memory_without_a_command_shows_help() -> None:
    result = RUNNER.invoke(cli.app, ["memory"])
    assert "list" in result.output and "approve" in result.output and "reject" in result.output


def test_the_top_level_help_lists_the_memory_commands() -> None:
    result = RUNNER.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "memory" in result.output and "run-local" in result.output
