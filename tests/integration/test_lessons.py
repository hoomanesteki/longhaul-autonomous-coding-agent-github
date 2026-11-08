"""Lesson extraction on a real Postgres with pgvector (LH-605).

The real extract_lessons activity runs against a scripted compactor model, the SQL recorder that
holds the task's events and PgMemoryStore (the embedder is HashEmbedder, which needs no
download). The candidates it writes are read back from the table: they are linked to the pull
request, they are not approved, and the prompt side of memory does not see them (invariant I7).
Each test works on a throwaway database that it creates, migrates to head and drops, so the
application database is never touched.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
import yaml
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import ActivityEnvironment

from longhaul.agent.loop import LoopConfig
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import (
    Outcome,
    OutcomeKind,
    Plan,
    PlanStep,
    RepoSettings,
    Role,
    TaskSpec,
)
from longhaul.db.models import MemoryItemRow
from longhaul.db.records import SqlRecorder
from longhaul.db.session import make_engine
from longhaul.durable import acts
from longhaul.durable.activities.memory import LessonActivities
from longhaul.durable.scripts import upload_script
from longhaul.memory.embedder import HashEmbedder
from longhaul.memory.lessons import TOOL_NAME
from longhaul.memory.store import PgMemoryStore
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase6]

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_lessontest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
REPO = "acme/inventory"
TASK_ID = "lh-acme-inventory-7"
PR_URL = "https://github.com/acme/inventory/pull/17"

REMOVE = "Inventory.remove raises InventoryError, not KeyError, and test_inventory.py checks it."
SESSION = "tests/conftest.py builds one Inventory per session, so a test that adds items leaks."
STYLE = "New modules in src/inventory/ start with a docstring that says why the module exists."

SPEC = TaskSpec(
    task_id=TASK_ID,
    repo=REPO,
    issue_number=7,
    title="remove() hides a KeyError",
    body="remove('x') raises KeyError",
    settings=RepoSettings(repo=REPO),
)
PLAN = Plan(
    summary="Make remove raise InventoryError.",
    steps=[
        PlanStep(
            id="s1",
            description="Raise InventoryError in remove",
            files=["src/inventory/core.py"],
            done_when="test_remove_missing passes",
            status="done",
        )
    ],
    acceptance_checks=["pytest -q"],
)


# ---------- the database ----------
@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The URL of a fresh database at the head revision."""
    secret = EnvSettings().database_url
    server = make_url(secret.get_secret_value() if secret else LOCAL_DSN)
    admin = server.set(database="postgres").render_as_string(hide_password=False)
    name = SCRATCH_PREFIX + uuid.uuid4().hex[:12]
    url = server.set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", UNREACHABLE_DSN)
    get_settings.cache_clear()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        yield url
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
async def sessions(database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(database)
    try:
        yield async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def store(sessions: async_sessionmaker[AsyncSession]) -> PgMemoryStore:
    return PgMemoryStore(sessions, HashEmbedder())


def write_script(path: Path, items: list[dict[str, str]]) -> Path:
    """A script whose compactor answers the lesson request with `items`."""
    script = {
        "steps": [
            {
                "match": {"role": "compactor", "user_text_contains": "finished task"},
                "respond": {"tool_calls": [{"name": TOOL_NAME, "input": {"items": items}}]},
            }
        ]
    }
    path.write_text(yaml.safe_dump(script))
    return path


async def run(
    store: PgMemoryStore,
    sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    items: list[dict[str, str]],
    outcome: Outcome,
) -> acts.ExtractLessonsOutput:
    blobs = MemoryBlobStore()
    script_ref = await upload_script(blobs, TASK_ID, write_script(tmp_path / "script.yaml", items))
    activities = LessonActivities(
        store,
        SqlRecorder(sessions),
        None,
        blobs,
        lambda role: LoopConfig(model="scripted-compactor" if role is Role.COMPACTOR else "m"),
    )
    out: acts.ExtractLessonsOutput = await ActivityEnvironment().run(
        activities.extract_lessons,
        acts.ExtractLessonsInput(
            task_id=TASK_ID,
            turn_index=9,
            spec=SPEC,
            outcome=outcome,
            plan=PLAN,
            script_ref=script_ref,
        ),
    )
    return out


async def rows(sessions: async_sessionmaker[AsyncSession]) -> list[MemoryItemRow]:
    async with sessions() as session:
        return list(await session.scalars(select(MemoryItemRow).order_by(MemoryItemRow.created_at)))


async def test_candidates_created(
    store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    items = [
        {"kind": "lesson", "content": REMOVE},
        {"kind": "pitfall", "content": SESSION},
        {"kind": "convention", "content": STYLE},
        {"kind": "lesson", "content": "Write tests."},
    ]
    opened = Outcome(kind=OutcomeKind.PR_OPENED, pr_url=PR_URL, summary="Done.")

    out = await run(store, sessions, tmp_path, items, opened)

    stored = await rows(sessions)
    assert [(r.kind, r.content) for r in stored] == [
        ("lesson", REMOVE),
        ("pitfall", SESSION),
        ("convention", STYLE),
    ]
    for row in stored:
        assert (row.repo, row.status, row.source, row.source_ref) == (
            REPO,
            "candidate",
            "task",
            "17",
        )
        assert row.approved_by is None
    assert (out.status, out.stored, out.source_ref) == ("ok", 3, "17")
    # Candidates stay out of the prompt until a merge or a person approves them.
    assert await store.search(REPO, "remove raises InventoryError", 5) == []


async def test_a_task_without_a_pull_request_is_linked_by_its_id(
    store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    stuck = Outcome(kind=OutcomeKind.STUCK, summary="no hint in 24 hours")

    await run(store, sessions, tmp_path, [{"kind": "pitfall", "content": SESSION}], stuck)

    (row,) = await rows(sessions)
    assert (row.status, row.source, row.source_ref) == ("candidate", "task", TASK_ID)


async def test_a_second_run_of_the_same_activity_adds_nothing(
    store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    # Temporal runs an activity again after a crash. The store keeps one row for one note.
    opened = Outcome(kind=OutcomeKind.PR_OPENED, pr_url=PR_URL, summary="Done.")
    items = [{"kind": "lesson", "content": REMOVE}]

    await run(store, sessions, tmp_path, items, opened)
    await run(store, sessions, tmp_path, items, opened)

    assert len(await rows(sessions)) == 1
