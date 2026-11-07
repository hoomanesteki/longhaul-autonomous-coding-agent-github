"""The turns, tool_calls, checkpoints and events tables and the SQL recorder, against Postgres.

Every test works on a throwaway database that the test creates and drops, like the migration
tests do. The migration test in test_migrations.py already checks that the migrated schema
matches the models, which covers the two new tables. These tests add what it cannot see: that
the migration is one step after the baseline, that it drops cleanly, and that the recorder
writes what the plan's data model says.
"""

import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import errors as pg_errors
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from longhaul.contracts import BlobRef, Usage
from longhaul.db.records import CheckpointRecord, SqlRecorder, ToolCallRecord, TurnRecord
from longhaul.db.session import make_engine
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_rectest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect,
# instead of reaching a real database.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
BASELINE = "0001_baseline"
REVISION = "0003_turns_tool_calls"
CACHE_RATIO_REVISION = "0009_turn_cache_ratio"
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 30, 12, 0, 3, tzinfo=UTC)


def _server_url() -> Any:
    secret = EnvSettings().database_url
    return make_url(secret.get_secret_value() if secret else LOCAL_DSN)


def _dsn(url: Any) -> str:
    return str(url.render_as_string(hide_password=False))


@dataclass(frozen=True)
class Scratch:
    name: str
    url: str

    def config(self) -> Config:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        return cfg

    def query(self, statement: LiteralString) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url) as conn:
            return conn.execute(statement).fetchall()

    def tables(self) -> set[str]:
        rows = self.query("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row[0] for row in rows}


@pytest.fixture
def make_scratch(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], Scratch]]:
    server = _server_url()
    admin_dsn = _dsn(server.set(database="postgres"))
    created: list[str] = []

    def make() -> Scratch:
        name = SCRATCH_PREFIX + uuid.uuid4().hex[:12]
        assert name != server.database
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        return Scratch(name=name, url=_dsn(server.set(database=name)))

    monkeypatch.setenv("LONGHAUL_DATABASE_URL", UNREACHABLE_DSN)
    get_settings.cache_clear()
    try:
        yield make
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            for name in created:
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )


@pytest.fixture
def scratch(make_scratch: Callable[[], Scratch]) -> Scratch:
    db = make_scratch()
    command.upgrade(db.config(), "head")
    return db


@pytest.fixture
async def recorder(scratch: Scratch) -> AsyncIterator[SqlRecorder]:
    engine = make_engine(scratch.url)
    try:
        yield SqlRecorder(async_sessionmaker[AsyncSession](engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def turn(attempt: int = 1, **fields: Any) -> TurnRecord:
    return TurnRecord(
        task_id="lh-acme-calc-42",
        turn_index=0,
        attempt=attempt,
        role="coder",
        started_at=T0,
        **fields,
    )


# ---------- the migration ----------
def test_the_new_tables_come_in_one_migration_and_the_history_has_one_head(
    scratch: Scratch,
) -> None:
    script = ScriptDirectory.from_config(scratch.config())
    heads = script.get_heads()
    assert len(heads) == 1, f"two heads: {heads}"
    assert script.get_revision(REVISION) is not None
    # It chains back to the baseline, whatever other migrations sit between the two.
    chain = [r.revision for r in script.walk_revisions(base="base", head=REVISION)]
    assert BASELINE in chain
    assert {"turns", "tool_calls"} <= scratch.tables()


def test_the_migration_downgrades_to_the_baseline_and_upgrades_again(scratch: Scratch) -> None:
    cfg = scratch.config()
    command.downgrade(cfg, BASELINE)
    assert not scratch.tables() & {"turns", "tool_calls"}
    assert {"repo_settings", "tasks"} <= scratch.tables()  # the baseline stays

    command.upgrade(cfg, "head")
    assert {"turns", "tool_calls"} <= scratch.tables()


def test_the_cache_ratio_comes_in_its_own_migration(scratch: Scratch) -> None:
    script = ScriptDirectory.from_config(scratch.config())
    revision = script.get_revision(CACHE_RATIO_REVISION)
    assert revision is not None
    assert len(script.get_heads()) == 1
    assert scratch.query(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'turns' AND column_name = 'cache_ratio'"
    ) == [("double precision", "NO", "0")]

    cfg = scratch.config()
    command.downgrade(cfg, str(revision.down_revision))
    assert not scratch.query(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'turns' AND column_name = 'cache_ratio'"
    )
    command.upgrade(cfg, "head")
    assert scratch.query(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'turns' AND column_name = 'cache_ratio'"
    )


def test_turns_that_were_recorded_before_get_their_ratio_from_their_counts(
    scratch: Scratch,
) -> None:
    cfg = scratch.config()
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision(CACHE_RATIO_REVISION)
    assert revision is not None
    command.downgrade(cfg, str(revision.down_revision))
    with psycopg.connect(scratch.url) as conn:
        for index, (plain, cached) in enumerate([(10, 30), (50, 0), (0, 0), (0, 20)]):
            conn.execute(
                "INSERT INTO turns (task_id, turn_index, attempt, role, started_at, "
                "input_tokens, cache_read_tokens) VALUES ('t', %s, 1, 'coder', now(), %s, %s)",
                (index, plain, cached),
            )

    command.upgrade(cfg, CACHE_RATIO_REVISION)

    ratios = scratch.query("SELECT cache_ratio FROM turns ORDER BY turn_index")
    assert [row[0] for row in ratios] == [0.75, 0.0, 0.0, 1.0]


def test_the_primary_keys_and_the_task_index_exist(scratch: Scratch) -> None:
    constraints = scratch.query("SELECT conname FROM pg_constraint WHERE contype = 'p'")
    assert {("pk_turns",), ("pk_tool_calls",)} <= set(constraints)
    indexes = scratch.query("SELECT tablename, indexname FROM pg_indexes WHERE schemaname='public'")
    assert ("tool_calls", "ix_tool_calls_task_turn") in set(indexes)


def test_a_second_row_for_the_same_attempt_is_refused(scratch: Scratch) -> None:
    insert = (
        "INSERT INTO turns (task_id, turn_index, attempt, role, started_at) "
        "VALUES ('t', 0, 1, 'coder', now())"
    )
    with psycopg.connect(scratch.url) as conn:
        conn.execute(insert)  # type: ignore[arg-type]
        with pytest.raises(pg_errors.UniqueViolation):
            conn.execute(insert)  # type: ignore[arg-type]


# ---------- the recorder ----------
async def test_a_turn_round_trips(scratch: Scratch, recorder: SqlRecorder) -> None:
    await recorder.record_turn(
        turn(
            request_ref="tasks/lh-acme-calc-42/turns/0000/transcript-1.json",
            response_ref="tasks/lh-acme-calc-42/turns/0000/response-1.json",
            usage=Usage(
                input_tokens=10, output_tokens=5, cache_read_tokens=3, cache_write_tokens=2
            ),
            cost_usd=0.012345,
            stop_reason="tool_use",
            latency_ms=1234,
            ended_at=T1,
        )
    )
    (row,) = scratch.query(
        "SELECT task_id, turn_index, attempt, role, request_ref, response_ref, input_tokens, "
        "output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, stop_reason, "
        "latency_ms, started_at, ended_at, created_at, cache_ratio FROM turns"
    )
    assert row[:5] == (
        "lh-acme-calc-42",
        0,
        1,
        "coder",
        "tasks/lh-acme-calc-42/turns/0000/transcript-1.json",
    )
    assert row[5] == "tasks/lh-acme-calc-42/turns/0000/response-1.json"
    assert row[6:10] == (10, 5, 3, 2)
    assert row[10] == Decimal("0.012345")
    assert row[11:13] == ("tool_use", 1234)
    assert (row[13], row[14]) == (T0, T1)
    assert row[15] is not None  # created_at comes from the server
    assert row[16] == pytest.approx(3 / 13)  # cache_read / (input + cache_read), LH-405


async def test_a_billed_call_that_failed_has_usage_and_no_reply(
    scratch: Scratch, recorder: SqlRecorder
) -> None:
    await recorder.record_turn(turn(usage=Usage(input_tokens=700, output_tokens=0)))
    ((response_ref, stop_reason, input_tokens, ended_at),) = scratch.query(
        "SELECT response_ref, stop_reason, input_tokens, ended_at FROM turns"
    )
    assert (response_ref, stop_reason, input_tokens, ended_at) == (None, None, 700, None)


async def test_the_next_attempt_follows_the_rows_of_the_turn(recorder: SqlRecorder) -> None:
    assert await recorder.next_attempt("lh-acme-calc-42", 0) == 1
    await recorder.record_turn(turn(1))
    await recorder.record_turn(turn(2))
    assert await recorder.next_attempt("lh-acme-calc-42", 0) == 3
    assert await recorder.next_attempt("lh-acme-calc-42", 1) == 1
    assert await recorder.next_attempt("lh-other", 0) == 1


async def test_recording_an_attempt_again_updates_its_row(
    scratch: Scratch, recorder: SqlRecorder
) -> None:
    await recorder.record_turn(turn(response_ref="first"))
    await recorder.record_turn(turn(response_ref="second", stop_reason="end_turn"))
    assert scratch.query("SELECT response_ref, stop_reason FROM turns") == [("second", "end_turn")]


async def test_a_tool_call_round_trips(scratch: Scratch, recorder: SqlRecorder) -> None:
    await recorder.record_tool_call(
        ToolCallRecord(
            id="toolu_01",
            task_id="lh-acme-calc-42",
            turn_index=3,
            name="run_tests",
            ok=True,
            attempt=2,
            args_ref="tasks/lh-acme-calc-42/turns/0003/tool-toolu_01-args.json",
            exit_code=1,
            output_ref="tasks/lh-acme-calc-42/turns/0003/tool-toolu_01-output.txt",
            duration_ms=812,
            pre_sha="a" * 40,
            post_sha="b" * 40,
        )
    )
    (row,) = scratch.query(
        "SELECT id, task_id, turn_index, name, args_ref, ok, exit_code, output_ref, "
        "duration_ms, pre_sha, post_sha, attempt FROM tool_calls"
    )
    assert row == (
        "toolu_01",
        "lh-acme-calc-42",
        3,
        "run_tests",
        "tasks/lh-acme-calc-42/turns/0003/tool-toolu_01-args.json",
        True,
        1,
        "tasks/lh-acme-calc-42/turns/0003/tool-toolu_01-output.txt",
        812,
        "a" * 40,
        "b" * 40,
        2,
    )


async def test_a_retried_tool_call_replaces_its_row(
    scratch: Scratch, recorder: SqlRecorder
) -> None:
    first = ToolCallRecord(
        id="toolu_01", task_id="lh-x", turn_index=0, name="read_file", ok=False, attempt=1
    )
    await recorder.record_tool_call(first)
    await recorder.record_tool_call(
        ToolCallRecord(
            id="toolu_01", task_id="lh-x", turn_index=0, name="read_file", ok=True, attempt=2
        )
    )
    assert scratch.query("SELECT ok, attempt FROM tool_calls") == [(True, 2)]


# ---------- checkpoints and events (LH-206) ----------
SHA = "a" * 40


async def test_a_checkpoint_round_trips(scratch: Scratch, recorder: SqlRecorder) -> None:
    await recorder.record_checkpoint(
        CheckpointRecord(
            task_id="lh-acme-calc-42", sha=SHA, kind="step", turn_index=7, step_id="s2"
        )
    )
    (row,) = scratch.query(
        "SELECT task_id, sha, kind, step_id, turn_index, bundle_ref, created_at FROM checkpoints"
    )
    assert row[:6] == ("lh-acme-calc-42", SHA, "step", "s2", 7, None)
    assert row[6] is not None  # created_at comes from the server


async def test_a_bundle_checkpoint_keeps_the_whole_ref(
    scratch: Scratch, recorder: SqlRecorder
) -> None:
    ref = BlobRef(key=f"tasks/lh-acme-calc-42/ckpt/{SHA}.bundle", sha256="b" * 64, size=1234)
    await recorder.record_checkpoint(
        CheckpointRecord(
            task_id="lh-acme-calc-42", sha=SHA, kind="bundle", turn_index=40, bundle_ref=ref
        )
    )
    ((stored,),) = scratch.query("SELECT bundle_ref FROM checkpoints")
    assert BlobRef.model_validate(stored) == ref


async def test_saving_a_checkpoint_again_keeps_one_row(
    scratch: Scratch, recorder: SqlRecorder
) -> None:
    # A retried tool call can make the same commit as its first try.
    record = CheckpointRecord(task_id="lh-acme-calc-42", sha=SHA, kind="tool", turn_index=3)
    await recorder.record_checkpoint(record)
    await recorder.record_checkpoint(record)
    assert scratch.query("SELECT count(*) FROM checkpoints") == [(1,)]


async def test_checkpoints_of_other_shas_kinds_and_tasks_are_all_kept(
    scratch: Scratch, recorder: SqlRecorder
) -> None:
    for task, sha, kind in [
        ("lh-a-1", SHA, "tool"),
        ("lh-a-1", SHA, "step"),
        ("lh-a-1", "b" * 40, "tool"),
        ("lh-b-2", SHA, "tool"),
    ]:
        await recorder.record_checkpoint(
            CheckpointRecord(task_id=task, sha=sha, kind=kind, turn_index=1)
        )
    assert scratch.query("SELECT count(*) FROM checkpoints") == [(4,)]


@pytest.mark.phase5
async def test_two_steps_done_at_the_same_sha_are_two_rows_and_a_repeat_is_not_a_third(
    scratch: Scratch, recorder: SqlRecorder
) -> None:
    # LH-503: a step marked done with no new work has the sha of the step before it.
    for step_id in ("s1", "s2", "s2"):
        await recorder.record_checkpoint(
            CheckpointRecord(task_id="lh-a-1", sha=SHA, kind="step", turn_index=4, step_id=step_id)
        )
    assert scratch.query("SELECT step_id FROM checkpoints ORDER BY id") == [("s1",), ("s2",)]


@pytest.mark.phase5
async def test_the_step_checkpoints_of_a_task_come_back_in_the_order_they_were_saved(
    recorder: SqlRecorder,
) -> None:
    saved = [
        ("lh-a-1", "a" * 40, "tool", None),
        ("lh-a-1", "b" * 40, "step", "s2"),
        ("lh-a-1", "c" * 40, "step", "s1"),
        ("lh-b-2", "d" * 40, "step", "s1"),
    ]
    for task, sha, kind, step_id in saved:
        await recorder.record_checkpoint(
            CheckpointRecord(task_id=task, sha=sha, kind=kind, turn_index=2, step_id=step_id)
        )

    found = await recorder.step_checkpoints("lh-a-1")

    assert [(c.step_id, c.sha[0], c.kind, c.turn_index) for c in found] == [
        ("s2", "b", "step", 2),
        ("s1", "c", "step", 2),
    ]
    assert await recorder.step_checkpoints("lh-none") == []


async def test_a_kind_outside_the_plan_is_refused(recorder: SqlRecorder) -> None:
    with pytest.raises(IntegrityError, match="ck_checkpoints_kind"):
        await recorder.record_checkpoint(
            CheckpointRecord(task_id="lh-a-1", sha=SHA, kind="snapshot", turn_index=1)
        )


async def test_an_event_round_trips(scratch: Scratch, recorder: SqlRecorder) -> None:
    payload = {"head_sha": SHA, "sandbox_id": "abc", "previous": None}
    await recorder.record_event("lh-acme-calc-42", "sandbox_recreated", payload)
    (row,) = scratch.query("SELECT task_id, type, payload, ts, created_at FROM events")
    assert row[:3] == ("lh-acme-calc-42", "sandbox_recreated", payload)
    assert row[3] is not None and row[4] is not None  # both times come from the server


async def test_events_keep_their_order_and_the_same_event_is_not_merged(
    scratch: Scratch, recorder: SqlRecorder
) -> None:
    await recorder.record_event("lh-a-1", "sandbox_recreated", {"n": 1})
    await recorder.record_event("lh-a-1", "hint", {})
    await recorder.record_event("lh-a-1", "sandbox_recreated", {"n": 1})
    rows = scratch.query("SELECT type, payload FROM events ORDER BY id")
    assert rows == [("sandbox_recreated", {"n": 1}), ("hint", {}), ("sandbox_recreated", {"n": 1})]


# ---------- the last call of a tool (LH-306) ----------
def named(call_id: str, name: str, turn_index: int, task_id: str = "lh-acme-calc-42") -> Any:
    return ToolCallRecord(
        id=call_id,
        task_id=task_id,
        turn_index=turn_index,
        name=name,
        ok=True,
        exit_code=0,
        output_ref=f"tasks/{task_id}/turns/{turn_index:04d}/tool-{call_id}-output.txt",
    )


async def test_the_last_call_of_a_tool_is_the_one_of_the_latest_turn(recorder: SqlRecorder) -> None:
    await recorder.record_tool_call(named("t1", "run_tests", 1))
    await recorder.record_tool_call(named("t3", "run_tests", 3))
    await recorder.record_tool_call(named("t2", "run_tests", 2))
    await recorder.record_tool_call(named("t4", "read_file", 4))

    found = await recorder.last_tool_call("lh-acme-calc-42", "run_tests")

    assert found == named("t3", "run_tests", 3)


async def test_two_calls_in_one_turn_give_the_one_that_was_written_last(
    recorder: SqlRecorder,
) -> None:
    await recorder.record_tool_call(named("first", "run_tests", 5))
    await recorder.record_tool_call(named("second", "run_tests", 5))

    found = await recorder.last_tool_call("lh-acme-calc-42", "run_tests")

    assert found is not None
    assert found.id == "second"


async def test_no_call_of_the_tool_gives_none(recorder: SqlRecorder) -> None:
    await recorder.record_tool_call(named("t1", "read_file", 1))
    await recorder.record_tool_call(named("t2", "run_tests", 1, task_id="lh-other"))

    assert await recorder.last_tool_call("lh-acme-calc-42", "run_tests") is None


async def test_a_call_comes_back_with_every_field_it_was_written_with(
    recorder: SqlRecorder,
) -> None:
    written = ToolCallRecord(
        id="toolu_01",
        task_id="lh-acme-calc-42",
        turn_index=3,
        name="run_tests",
        ok=False,
        attempt=2,
        args_ref="tasks/lh-acme-calc-42/turns/0003/tool-toolu_01-args.json",
        exit_code=1,
        output_ref="tasks/lh-acme-calc-42/turns/0003/tool-toolu_01-output.txt",
        duration_ms=812,
        pre_sha="a" * 40,
        post_sha="b" * 40,
    )
    await recorder.record_tool_call(written)

    assert await recorder.last_tool_call("lh-acme-calc-42", "run_tests") == written
