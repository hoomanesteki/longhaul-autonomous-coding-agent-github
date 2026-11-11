"""The run report, read from Postgres and the blob store (LH-705).

Every test works on a throwaway database that it creates, migrates and drops, like the other
table tests. The rows are the ones a scripted run of the inventory fixture leaves: a plan that a
person approved, turns of three roles, tool calls, a pause, a compaction, a verifier round that
sent the work back, and the cost ledger of all of it. The report is built from those rows by the
code that `longhaul report` and `deliver` use, and its numbers are compared with sums that the
test takes from the database by itself.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from longhaul.blobs.base import report_key
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.squash import FileStat
from longhaul.contracts import Plan, PlanStep, Usage
from longhaul.cost.ledger import SqlLedger
from longhaul.cost.pricing import Pricing
from longhaul.db.models import Event, PlanRow, Task
from longhaul.db.records import PlanRecord, SqlRecorder, ToolCallRecord, TurnRecord
from longhaul.db.session import make_engine
from longhaul.durable import acts
from longhaul.durable.activities.report import ReportActivities, RunReports, sql_reader
from longhaul.github.pr_body import MAX_BODY_CHARS, LastTestRun, render_pr_body
from longhaul.reports.data import read_report_data
from longhaul.reports.render import LESSONS_EVENT, MAX_TIMELINE_ROWS
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase7]

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
PRICING = ROOT / "config" / "pricing.yaml"
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_reporttest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
SONNET = "claude-sonnet-5-5"
TASK = "lh-acme-inventory-7"
REPO = "acme/inventory"
T0 = datetime(2026, 11, 3, 9, 0, 0, tzinfo=UTC)

Sessions = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class Scratch:
    name: str
    url: str

    def config(self) -> Config:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        return cfg

    def rows(self, statement: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url) as conn:
            return conn.execute(statement, params).fetchall()

    def one(self, statement: LiteralString, *params: Any) -> Any:
        ((value,),) = self.rows(statement, *params)
        return value


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[Scratch]:
    secret = EnvSettings().database_url
    server = make_url(secret.get_secret_value() if secret else LOCAL_DSN)
    admin = server.set(database="postgres").render_as_string(hide_password=False)
    name = SCRATCH_PREFIX + uuid.uuid4().hex[:12]
    url = server.set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", UNREACHABLE_DSN)
    get_settings.cache_clear()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    scratch = Scratch(name, url)
    try:
        command.upgrade(scratch.config(), "head")
        yield scratch
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
async def sessions(database: Scratch) -> AsyncIterator[Sessions]:
    engine = make_engine(database.url)
    try:
        yield async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def dollars(value: Decimal) -> Decimal:
    """What the report prints for an amount: ten thousandths, half rounded up as Postgres does."""
    return Decimal(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def a_plan() -> Plan:
    return Plan(
        summary="Reserve stock without going negative",
        steps=[
            PlanStep(id="s1", description="Reject a reservation above stock", done_when="ok"),
            PlanStep(id="s2", description="Release stock when a reservation ends", done_when="ok"),
            PlanStep(id="s3", description="Document the new errors", done_when="ok"),
        ],
        acceptance_checks=["pytest -q"],
    )


async def add_task(sessions: Sessions, **fields: Any) -> None:
    values: dict[str, Any] = {
        "id": TASK,
        "repo": REPO,
        "issue_number": 7,
        "title": "Reservations can drive stock negative",
        "status": "active",
        "phase": "coding",
        "started_at": at(0),
    }
    async with sessions.begin() as session:
        session.add(Task(**{**values, **fields}))


async def add_event(
    sessions: Sessions, seconds: int, type: str, task_id: str = TASK, **payload: Any
) -> None:
    async with sessions.begin() as session:
        session.add(Event(task_id=task_id, ts=at(seconds), type=type, payload=payload))


async def add_turn(
    sessions: Sessions,
    role: str,
    index: int,
    start: int,
    *,
    task_id: str = TASK,
    usage: Usage | None = None,
) -> None:
    """One billed turn: its row in `turns` and its row on the cost ledger."""
    usage = usage or Usage(
        input_tokens=1000 + index, output_tokens=200 + index, cache_read_tokens=3000 + index
    )
    cost = await SqlLedger(sessions, Pricing.load(PRICING)).record(
        task_id, REPO, SONNET, index, 1, usage
    )
    await SqlRecorder(sessions).record_turn(
        TurnRecord(
            task_id=task_id,
            turn_index=index,
            attempt=1,
            role=role,
            started_at=at(start),
            ended_at=at(start + 20),
            usage=usage,
            cost_usd=float(cost),
            stop_reason="tool_use",
        )
    )


async def add_call(
    sessions: Sessions,
    call_id: str,
    name: str,
    *,
    ok: bool = True,
    ms: int = 100,
    turn: int = 0,
    task_id: str = TASK,
) -> None:
    await SqlRecorder(sessions).record_tool_call(
        ToolCallRecord(
            id=call_id, task_id=task_id, turn_index=turn, name=name, ok=ok, duration_ms=ms
        )
    )


async def seed_inventory_run(sessions: Sessions) -> None:
    """The rows of the scripted inventory run, in the order they happened."""
    await add_task(sessions, outcome="pr_opened", phase="done", ended_at=at(900))
    recorder = SqlRecorder(sessions)
    for index, start in enumerate((5, 30)):
        await add_turn(sessions, "planner", index, start)
    await recorder.record_plan(PlanRecord(TASK, 1, a_plan()))
    async with sessions.begin() as session:
        await session.execute(
            update(PlanRow)
            .where(PlanRow.task_id == TASK)
            .values(status="approved", decided_by="alice", decided_at=at(240))
        )
        await session.execute(
            update(PlanRow).where(PlanRow.task_id == TASK).values(created_at=at(55))
        )
    # A turn index belongs to one turn of the task, whatever its role (the key of `turns`).
    for index, start in enumerate((250, 280, 310, 340, 370, 400), start=2):
        await add_turn(sessions, "coder", index, start)
    await add_turn(sessions, "verifier", 8, 500)
    await add_turn(sessions, "coder", 9, 540)
    await add_turn(sessions, "verifier", 10, 600)
    for n in range(5):
        await add_call(sessions, f"t{n}", "edit_file", ms=40 + n)
    for n in range(3):
        await add_call(sessions, f"r{n}", "run_tests", ok=n != 1, ms=8_000 + n)
    await add_call(sessions, "g0", "read_file", ms=12)
    await add_event(sessions, 320, "paused", reason="repeated_failure", pause=1, question="Which?")
    await add_event(sessions, 380, "resumed", pause=1, via="hint", by="alice")
    await add_event(
        sessions, 450, "compaction", tokens_before=80_000, tokens_after=9_000, folded_turns=4
    )
    await add_event(sessions, 510, "verdict", round=1, passed=False, issues=["no test"])
    await add_event(sessions, 610, "verdict", round=2, passed=True, issues=[])
    await add_event(sessions, 900, "outcome", kind="pr_opened", repo=REPO, turns=9, cost_usd=0.1)


def reports(sessions: Sessions, blobs: MemoryBlobStore | None = None) -> RunReports:
    return RunReports(sql_reader(sessions), blobs or MemoryBlobStore(), templates=TEMPLATES)


def section(text: str, name: str) -> str:
    lines = text.splitlines()
    start = lines.index(f"### {name}") + 1
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("### "):
            break
        body.append(line)
    return "\n".join(body)


async def test_sections_and_numbers(database: Scratch, sessions: Sessions) -> None:
    await seed_inventory_run(sessions)

    text = await reports(sessions).build(TASK)

    headings = [line[4:] for line in text.splitlines() if line.startswith("### ")]
    assert headings == [
        "Overview",
        "Timeline",
        "Tool usage",
        "Plan",
        "Interventions",
        "Candidate lessons",
    ]
    overview = section(text, "Overview")
    # The numbers are summed from the tables by the test, not by the report.
    cost = database.one("SELECT sum(usd) FROM cost_ledger WHERE task_id = %s", TASK)
    assert f"| Cost | ${dollars(cost)} |" in overview
    # The column that the ledger keeps for the task says the same.
    assert f"| Cost | ${database.one('SELECT cost_usd FROM tasks WHERE id = %s', TASK)} |" in (
        overview
    )
    tokens = database.rows(
        "SELECT sum(input_tokens), sum(output_tokens), sum(cache_read_tokens), "
        "sum(cache_write_tokens) FROM cost_ledger WHERE task_id = %s",
        TASK,
    )[0]
    assert f"{tokens[0]:,} input" in overview
    assert f"{tokens[1]:,} output" in overview
    assert f"{tokens[2]:,} cache read" in overview
    assert f"{tokens[3]:,} cache write" in overview
    ratio = tokens[2] / (tokens[0] + tokens[2])
    assert f"| Cache ratio | {ratio:.1%} |" in overview
    turns = database.one(
        "SELECT count(DISTINCT (role, turn_index)) FROM turns WHERE task_id = %s", TASK
    )
    assert f"| Turns | {turns} (planner 2, coder 7, verifier 2) |" in overview
    assert "| Wall time | 15m 0s |" in overview
    assert "| Outcome | pr opened |" in overview
    assert "acme/inventory" in overview and "issue 7" in overview

    tools = section(text, "Tool usage")
    for name, calls, errors in (("edit_file", 5, 0), ("run_tests", 3, 1), ("read_file", 1, 0)):
        row = next(line for line in tools.splitlines() if line.startswith(f"| {name} |"))
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert cells[1:3] == [str(calls), str(errors)]
    total = database.one("SELECT sum(duration_ms) FROM tool_calls WHERE task_id = %s", TASK)
    assert f"| Total | 9 | 1 | {(total + 50) // 100 / 10:.1f}s |" in tools

    plan = section(text, "Plan")
    assert "Version 1, approved by alice." in plan
    assert "- [ ] **s1** (todo): Reject a reservation above stock" in plan

    timeline = section(text, "Timeline")
    for label in ("Planning", "Approval wait", "Coding", "Paused", "Compaction", "Verify round 2"):
        assert label in timeline
    assert "| 2026-11-03 09:04:00 | alice | Approved plan version 1 |" in section(
        text, "Interventions"
    )


async def test_the_report_is_stored_in_the_blob_store(
    database: Scratch, sessions: Sessions
) -> None:
    await seed_inventory_run(sessions)
    blobs = MemoryBlobStore()

    ref = await reports(sessions, blobs).write(TASK)

    assert ref.key == report_key(TASK)
    stored = (await blobs.get(ref)).decode()
    assert stored == await reports(sessions).build(TASK)


async def test_a_local_task_with_no_tasks_row_has_a_report(
    database: Scratch, sessions: Sessions
) -> None:
    local = "lh-local-inventory-0a1b2c3d"
    await add_turn(sessions, "coder", 0, 5, task_id=local)
    await add_turn(sessions, "coder", 1, 40, task_id=local)
    await add_call(sessions, "c1", "run_tests", task_id=local, ms=2_000)
    await add_event(sessions, 90, "outcome", local, kind="pr_opened", repo="local:/tmp/x", turns=2)

    data = await read_report_data(sessions, local)
    text = await reports(sessions).build(local)

    assert data.task is None
    overview = section(text, "Overview")
    assert f"local task {local}" in overview
    assert "| Turns | 2 (coder 2) |" in overview
    assert "| Outcome | pr opened |" in overview
    assert "| Wall time | 1m 25s |" in overview
    assert "| run_tests | 1 | 0 | 2.0s |" in section(text, "Tool usage")
    cost = database.one("SELECT sum(usd) FROM cost_ledger WHERE task_id = %s", local)
    assert f"| Cost | ${dollars(cost)} |" in overview


async def test_a_task_the_database_has_never_heard_of_is_an_error(
    database: Scratch, sessions: Sessions
) -> None:
    with pytest.raises(LookupError, match="lh-nope"):
        await read_report_data(sessions, "lh-nope")


async def test_a_task_that_is_still_running_has_a_report_with_no_outcome(
    database: Scratch, sessions: Sessions
) -> None:
    await add_task(sessions)
    await add_turn(sessions, "coder", 0, 5)

    text = await reports(sessions).build(TASK)

    assert "| Outcome | running |" in section(text, "Overview")


async def test_lessons_that_a_later_story_recorded_are_in_the_report(
    database: Scratch, sessions: Sessions
) -> None:
    await seed_inventory_run(sessions)
    await add_event(
        sessions,
        930,
        LESSONS_EVENT,
        items=[{"kind": "pitfall", "content": "reserve() must check stock under the lock"}],
    )

    text = await reports(sessions).build(TASK)

    assert "- **pitfall**: reserve() must check stock under the lock" in section(
        text, "Candidate lessons"
    )


async def test_the_stored_report_names_the_final_outcome_and_lessons_after_the_wind_up(
    database: Scratch, sessions: Sessions
) -> None:
    """`deliver` stores the report before the outcome and the lessons exist (LH-991).

    The wind-up asks write_run_report again once both are recorded. The stored copy then has them,
    and the body that the pull request got from the first copy is not touched.
    """
    await add_task(sessions)
    await add_turn(sessions, "coder", 0, 5)
    blobs = MemoryBlobStore()
    run_reports = reports(sessions, blobs)
    delivered = await run_reports.stored_or_new(TASK, outcome="pr_opened")
    body = render_pr_body(
        7, "Fixed it.", [], LastTestRun(None, None), templates=TEMPLATES, report=delivered
    )
    assert "No lessons were recorded for this task." in section(delivered, "Candidate lessons")

    await add_event(
        sessions,
        60,
        LESSONS_EVENT,
        items=[{"kind": "pitfall", "content": "reserve() must check stock under the lock"}],
    )
    await add_event(sessions, 90, "outcome", kind="stuck", repo=REPO, turns=1, cost_usd=0.1)
    written = await ReportActivities(run_reports).write_run_report(
        acts.RunReportInput(task_id=TASK, outcome="stuck")
    )

    stored = (await blobs.get(written)).decode()
    assert written.key == report_key(TASK)
    assert "| Outcome | stuck |" in section(stored, "Overview")
    assert "- **pitfall**: reserve() must check stock under the lock" in section(
        stored, "Candidate lessons"
    )
    assert "| Outcome | pr opened |" in section(delivered, "Overview")
    assert len(body) <= MAX_BODY_CHARS
    assert (
        len(
            render_pr_body(
                7, "Fixed it.", [], LastTestRun(None, None), templates=TEMPLATES, report=stored
            )
        )
        <= MAX_BODY_CHARS
    )


async def test_the_report_of_a_task_is_not_mixed_with_another_task(
    database: Scratch, sessions: Sessions
) -> None:
    await seed_inventory_run(sessions)
    await add_turn(sessions, "coder", 0, 5, task_id="lh-other-1")
    await add_call(sessions, "o1", "grep", task_id="lh-other-1")

    text = await reports(sessions).build(TASK)

    assert "grep" not in text


# ---------- the pull request body ----------
async def test_body_cap(database: Scratch, sessions: Sessions) -> None:
    """500 turns, a compaction for each fifth of them, and the body stays under 60,000."""
    await add_task(sessions, outcome="pr_opened", ended_at=at(30_000))
    async with sessions.begin() as session:
        session.add_all(
            [
                *(
                    Event(
                        task_id=TASK,
                        ts=at(60 * n),
                        type="compaction",
                        payload={
                            "tokens_before": 90_000 + n,
                            "tokens_after": 9_000,
                            "folded_turns": 5,
                        },
                    )
                    for n in range(1, 101)
                ),
                *(
                    Event(
                        task_id=TASK,
                        ts=at(60 * n + 5),
                        type="sandbox_recreated",
                        payload={"sandbox_id": f"sb-{n}"},
                    )
                    for n in range(1, 41)
                ),
            ]
        )
    for index in range(500):
        await add_turn(sessions, "coder", index, 10 * index, usage=Usage(input_tokens=900))
    for index in range(0, 500, 5):
        await add_call(sessions, f"c{index}", f"tool_{index % 40}", turn=index)

    text = await reports(sessions).build(TASK)
    files = [FileStat(f"src/pkg/module_{n}/" + "y" * 120 + ".py", n, n) for n in range(100)]
    body = render_pr_body(
        7, "Fixed it. " * 400, files, LastTestRun("3 passed", 0), templates=TEMPLATES, report=text
    )

    assert len(body) < MAX_BODY_CHARS
    timeline = section(text, "Timeline")
    shown = [line for line in timeline.splitlines() if line.startswith("| ") and "---" not in line]
    assert len(shown) - 1 == MAX_TIMELINE_ROWS
    assert "left out" in timeline
    assert "<summary>Run report</summary>" in body
    assert body.rstrip().splitlines()[-1] == "Closes #7"
    turns = database.one("SELECT count(*) FROM turns WHERE task_id = %s", TASK)
    assert turns == 500 and "| Turns | 500 (coder 500) |" in section(text, "Overview")
