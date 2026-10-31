"""The repo_settings upsert of LH-303, against a throwaway Postgres.

Each test gets a database that it creates, migrates to head and drops. Nothing here touches the
application database, so a sync that went wrong cannot change real allowlist rows.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from longhaul.contracts import RepoSettings
from longhaul.db.models import RepoSetting
from longhaul.db.session import make_engine
from longhaul.github.intake import sync_repo_settings
from longhaul.settings import AppConfig, EnvSettings, RepoEntry, get_settings, load_config

pytestmark = [pytest.mark.integration, pytest.mark.phase3]

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "longhaul.example.yaml"
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_intake_"
# If anything ignored the scratch URL it would fall back to this and fail to connect, instead
# of reaching a real database.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"

Stored = dict[str, tuple[bool, dict[str, Any], datetime]]


def entry(repo: str, *, allowlisted: bool = True, **fields: Any) -> RepoEntry:
    return RepoEntry(repo=repo, allowlisted=allowlisted, **fields)


def config_with(*entries: RepoEntry) -> AppConfig:
    return load_config(EXAMPLE).model_copy(update={"repos": list(entries)})


def three_repos() -> AppConfig:
    return config_with(
        entry("acme/calc", plan_approval_required=True, test_command="pytest -q"),
        entry("acme/textkit", allowlisted=False),
        entry("local:/work/inventory", budgets={"task_usd": 1.5}),
    )


# --- a database per test ----------------------------------------------------------------------


@pytest.fixture
def scratch_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    secret = EnvSettings().database_url
    server = make_url(secret.get_secret_value() if secret else LOCAL_DSN)
    admin = server.set(database="postgres").render_as_string(hide_password=False)
    name = SCRATCH_PREFIX + uuid.uuid4().hex[:12]
    assert name != server.database
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", UNREACHABLE_DSN)
    get_settings.cache_clear()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield server.set(database=name).render_as_string(hide_password=False)
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _upgrade_to_head(connection: Connection) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")


@pytest.fixture
async def engine(scratch_url: str) -> AsyncIterator[AsyncEngine]:
    # Alembic cannot start its own event loop inside a running one, so it gets a connection.
    engine = make_engine(scratch_url, pooled=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade_to_head)
        yield engine
    finally:
        await engine.dispose()


async def stored(engine: AsyncEngine) -> Stored:
    """Every repo_settings row, read in a session of its own."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        rows = (await session.execute(select(RepoSetting))).scalars().all()
    return {row.repo: (row.allowlisted, row.settings, row.created_at) for row in rows}


async def sync(engine: AsyncEngine, config: AppConfig) -> None:
    """What a caller does: a session with no transaction open, then the sync."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await sync_repo_settings(config, session)


# --- the upsert -------------------------------------------------------------------------------


async def test_the_sync_inserts_every_repo_with_its_flag_and_settings(engine: AsyncEngine) -> None:
    await sync(engine, three_repos())

    rows = await stored(engine)
    assert set(rows) == {"acme/calc", "acme/textkit", "local:/work/inventory"}
    calc_flag, calc_settings, calc_created = rows["acme/calc"]
    assert calc_flag is True
    assert calc_settings["plan_approval_required"] is True
    assert calc_settings["test_command"] == "pytest -q"
    assert calc_settings["budgets"]["task_usd"] == 5.0
    assert "allowlisted" not in calc_settings and "repo" not in calc_settings
    assert calc_created is not None  # the server default
    assert rows["acme/textkit"][0] is False
    assert rows["local:/work/inventory"][0] is True  # a local: repo keeps working
    assert rows["local:/work/inventory"][1]["budgets"]["task_usd"] == 1.5


async def test_a_stored_row_rebuilds_the_settings_of_its_entry(engine: AsyncEngine) -> None:
    config = three_repos()
    await sync(engine, config)

    rows = await stored(engine)

    for configured in config.repos:
        _, settings, _ = rows[configured.repo]
        expected = RepoSettings.model_validate(configured.model_dump(exclude={"allowlisted"}))
        assert RepoSettings(repo=configured.repo, **settings) == expected


async def test_syncing_twice_changes_nothing(engine: AsyncEngine) -> None:
    await sync(engine, three_repos())
    first = await stored(engine)

    await sync(engine, three_repos())

    assert await stored(engine) == first  # same rows, same created_at


async def test_a_changed_entry_is_updated_in_place(engine: AsyncEngine) -> None:
    await sync(engine, three_repos())
    before = await stored(engine)

    changed = config_with(
        entry("acme/calc", test_command="python -m pytest", budgets={"task_usd": 9}),
        entry("acme/textkit", allowlisted=True),
        entry("local:/work/inventory", allowlisted=False),
    )
    await sync(engine, changed)

    after = await stored(engine)
    assert set(after) == set(before)
    assert after["acme/calc"][1]["test_command"] == "python -m pytest"
    assert after["acme/calc"][1]["plan_approval_required"] is False  # back to the default
    assert after["acme/calc"][1]["budgets"]["task_usd"] == 9
    assert after["acme/textkit"][0] is True
    assert after["local:/work/inventory"][0] is False
    for repo in after:
        assert after[repo][2] == before[repo][2]  # created_at is the first insert's


async def test_a_repo_that_leaves_the_yaml_is_no_longer_allowlisted(engine: AsyncEngine) -> None:
    # The DB copy is what the API reads. A row that kept allowlisted = true after the operator
    # removed the repo from the file would still look like an invitation.
    config = three_repos()
    await sync(engine, config)
    before = await stored(engine)

    await sync(engine, config_with(config.repos[0]))  # acme/calc stays, with the same settings

    after = await stored(engine)
    assert set(after) == set(before)  # nothing is deleted, so tasks keep their repo row
    assert after["acme/calc"] == before["acme/calc"]
    assert after["local:/work/inventory"][0] is False
    assert after["acme/textkit"][0] is False
    # Only the flag changed on the revoked rows.
    assert after["local:/work/inventory"][1:] == before["local:/work/inventory"][1:]


async def test_an_empty_repos_list_revokes_everything(engine: AsyncEngine) -> None:
    await sync(engine, three_repos())

    await sync(engine, config_with())

    assert {flag for flag, _, _ in (await stored(engine)).values()} == {False}


async def test_a_repo_that_comes_back_is_allowlisted_again(engine: AsyncEngine) -> None:
    await sync(engine, three_repos())
    await sync(engine, config_with())

    await sync(engine, three_repos())

    rows = await stored(engine)
    assert rows["acme/calc"][0] is True
    assert rows["local:/work/inventory"][0] is True
    assert rows["acme/textkit"][0] is False  # the config says false


async def test_a_case_only_rename_moves_the_flag_to_the_new_row(engine: AsyncEngine) -> None:
    # The key is the name as written. A rename in case adds a row and revokes the old one, so
    # exactly one row for the repo is allowlisted at any time.
    await sync(engine, config_with(entry("acme/calc")))

    await sync(engine, config_with(entry("Acme/Calc")))

    rows = await stored(engine)
    assert {repo: flag for repo, (flag, _, _) in rows.items()} == {
        "acme/calc": False,
        "Acme/Calc": True,
    }


# --- transactions -----------------------------------------------------------------------------


async def test_the_sync_commits_when_the_caller_opened_no_transaction(
    engine: AsyncEngine,
) -> None:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await sync_repo_settings(three_repos(), session)
        assert not session.in_transaction()  # it finished the transaction it started

    assert set(await stored(engine)) == {"acme/calc", "acme/textkit", "local:/work/inventory"}


async def test_the_sync_leaves_the_commit_to_a_caller_that_began_a_transaction(
    engine: AsyncEngine,
) -> None:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        transaction = await session.begin()
        await sync_repo_settings(three_repos(), session)
        assert session.in_transaction()  # still the caller's
        await transaction.rollback()

    assert await stored(engine) == {}


async def test_a_caller_that_began_a_transaction_can_commit_it(engine: AsyncEngine) -> None:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session, session.begin():
        await sync_repo_settings(three_repos(), session)

    assert len(await stored(engine)) == 3


async def test_a_bad_config_changes_nothing(engine: AsyncEngine) -> None:
    await sync(engine, three_repos())
    before = await stored(engine)

    with pytest.raises(ValueError, match="twice"):
        await sync(engine, config_with(entry("acme/calc"), entry("Acme/Calc"), entry("acme/new")))

    assert await stored(engine) == before


async def test_two_workers_starting_together_end_with_one_consistent_copy(
    engine: AsyncEngine,
) -> None:
    await sync(engine, config_with(entry("acme/old")))

    await asyncio.gather(*(sync(engine, three_repos()) for _ in range(4)))

    rows = await stored(engine)
    assert {repo: flag for repo, (flag, _, _) in rows.items()} == {
        "acme/old": False,
        "acme/calc": True,
        "acme/textkit": False,
        "local:/work/inventory": True,
    }
