"""A private durable stack for one chaos run, and the tools that kill its worker.

A chaos run starts `longhaul worker` as a process, starts a task on it, kills the worker with
SIGKILL, starts it again, and then reads what the run left behind: the outcome, the final diff and
the `tool_calls` rows. The worker is the real one, with the real activities, a real sandbox, and
Temporal, Postgres and MinIO from `make up`.

What a run owns, so that it shares nothing with other work on the machine: a task queue with a
random name, a scratch database migrated to head, a checkpoint folder, and a task id with a random
suffix. `Stack.close` removes the containers, volumes, blobs and workflow of its tasks and drops
the database. A kill goes to a pid that this module started, and never to a process found by
name.

Where a kill lands. The worker logs a line when an activity starts, ends or fails
(longhaul.durable.activity_log). After a kill the last of those lines says what the worker was
doing: a "started" line means it was inside that activity, and an "ended" or "failed" line means
it was between two activities. The kill points in a report come from that line and from nothing
else, so they say what happened and not what the test hoped for.

A kill waits for a line of the worker's log (`after`, the `nth` time it matches), then optionally
for a condition (`until`), then for a pause (`delay_s`). The wait runs in a thread with a short
poll, because an event loop that is busy with the run adds tens of milliseconds, which is the whole
window of a kill between two activities.

A seeded plan follows the progress of the worker and not the clock. It is drawn from the activities
of a run that was never killed (`plan_kills`): the kill waits for the k-th activity to start and
then pauses for no longer than that activity took in the reference. A run that is faster or slower
than the reference, because the machine is idle or loaded, still reaches that line, and the kill
still falls in or just after the same work. A plan made of seconds does not: a reference measured
under load gives pauses that outlast a run measured when the machine is quiet, and the task
finishes before its kill.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

import boto3
import docker
import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError

from longhaul import cli
from longhaul.blobs.s3 import S3BlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.contracts import Outcome, OutcomeKind, TaskStatus
from longhaul.durable.activity_log import ACTIVITY_LINE
from longhaul.durable.acts import Tuning
from longhaul.durable.client import connect, get_task, read_status, start_task
from longhaul.durable.scripts import upload_script
from longhaul.settings import EnvSettings

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "tests" / "fixtures" / "repos" / "inventory"
SCRIPT = ROOT / "tests" / "fixtures" / "scripts" / "inventory_fix.yaml"
CONFIG = ROOT / "config" / "longhaul.example.yaml"
IMAGE = "longhaul-sandbox:py312"
# `uv run` and `uv sync` put the script next to the interpreter.
LONGHAUL = Path(sys.executable).with_name("longhaul")
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_chaos_"

# After a kill the server notices a dead activity when its heartbeat timeout passes (ADR-0004
# section 3). The plan's values are 15 to 60 s, which would make five runs very slow, so a chaos
# run starts its task with this one. It is not shorter, because a worker that is busy on a loaded
# machine must not look dead.
HEARTBEAT_TIMEOUT_S = 5.0
RUN_TIMEOUT_S = 1200
WORKER_START_TIMEOUT_S = 180
# How long a kill waits for its line, its condition and the restart of its worker.
KILL_WAIT_S = 400
POLL_S = 0.002
SLOW_POLL_S = 0.25

# The first kill of a plan waits for one of the first WINDOW_SHARE of the activities of the
# reference to start for the first time. The activities after that are left out, so that work
# remains after the kill and a pause that runs a little long does not outlast the run.
WINDOW_SHARE = 0.85

# Where a worker was, as the kill points name it. An activity that was running is named by its
# own name (llm_turn, tool_exec, prepare_workspace ...).
BETWEEN = "between activities"
STARTUP = "startup"
KINDS = ("llm_turn", "tool_exec", BETWEEN)

WORKER_READY = "worker running"
_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) ")

# The lines the kills wait for. Each is a regular expression over one line of the worker's log.
ANY_STARTED = re.compile(r"activity \w+ started")
# A retried activity writes a started line for each attempt. A seeded kill counts only the first
# attempts, because the reference has the same first attempts in the same order whatever was
# retried on the way.
FIRST_ATTEMPT_STARTED = re.compile(r"activity \w+ started: task \S+, attempt 1\b")
ANY_ENDED = re.compile(r"activity \w+ ended")
# Turn 3 of inventory_fix.yaml waits 3 s before the model answers.
SLOW_TURN_STARTED = re.compile(r"activity llm_turn started: .*, turn 3$", re.MULTILINE)
# The one run_command of inventory_fix.yaml: it appends a line to CHANGES.txt, then sleeps.
COMMAND_STARTED = re.compile(r"activity tool_exec started: .*, run_command$", re.MULTILINE)
EDIT_ENDED = re.compile(r"activity tool_exec ended: .*, edit_file$", re.MULTILINE)
APPENDED_LINE = "remove() rejects an overdraft"


# ---------- where a worker was ----------
def kill_point(log: str) -> tuple[str, str]:
    """What a worker was doing when its log ended, and the line that says so.

    The kind is the name of the activity when the last line says it started. An activity that
    ended or failed puts the worker between two activities. A log with no activity line is a
    worker that had not taken any work.
    """
    found = list(ACTIVITY_LINE.finditer(log))
    if not found:
        return STARTUP, ""
    last = found[-1]
    kind = last["name"] if last["event"] == "started" else BETWEEN
    return kind, last.group(0)


def plain_line(line: str) -> str:
    """A JSON log line of the worker (telemetry/logging.py) as `<time> <level> <logger>: <event>`.

    The patterns here read that plain form: a time first, and the message at the end of the line.
    A line that is not one of the worker's JSON lines, such as one Temporal Core writes on its
    own, is kept as it is.
    """
    if not line.startswith("{"):
        return line
    try:
        record = json.loads(line)
        when = datetime.fromisoformat(str(record["timestamp"]).replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError):
        return line
    level = str(record.get("level", "")).upper()
    millis = f"{when.microsecond // 1000:03d}"
    head = f"{when:%Y-%m-%d %H:%M:%S},{millis} {level} {record.get('logger', '')}"
    return f"{head}: {record.get('event', '')}\n"


def stamp(line: str) -> datetime | None:
    found = _STAMP.match(line)
    return datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S,%f") if found else None


# ---------- kills ----------
@dataclass(frozen=True)
class Kill:
    """One kill of the worker that is running.

    It waits for the `nth` line that matches `after` in the log of that worker (the worker's own
    log, so a new worker counts from its first line), then for `until(task_id)` to hold, then for
    `delay_s` seconds, and then sends SIGKILL.
    """

    after: re.Pattern[str]
    nth: int = 1
    delay_s: float = 0.0
    until: Callable[[str], bool] | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.nth < 1:
            raise ValueError(f"nth counts from 1, not {self.nth}")


@dataclass(frozen=True)
class KillPoint:
    kind: str  # one of KINDS, another activity name, or STARTUP
    line: str  # the last activity line of the worker, as it was when the worker died
    after_s: float  # seconds from the start of the task


@dataclass(frozen=True)
class Activity:
    """One activity of a worker, as its log shows it."""

    name: str
    line: str  # the line that says it started, without the time
    took_s: float  # from that line to the line that ended or failed it; 0 if neither came
    attempt: int = 1


def activities_of(log: str) -> list[Activity]:
    """The activities in a worker's log, in the order they started, and how long each took.

    An activity is closed by the next "ended" or "failed" line of the same name, task and attempt.
    A worker runs one activity of a task at a time, so that key is enough. A failed line adds the
    name of the error after the detail, which is why the detail is not part of the key.
    """
    found: list[Activity] = []
    open_at: dict[tuple[str, str, str], tuple[int, datetime | None]] = {}
    for text in log.splitlines():
        match = ACTIVITY_LINE.search(text)
        if match is None:
            continue
        key = (match["name"], match["task"], match["attempt"])
        when = stamp(text)
        if match["event"] == "started":
            open_at[key] = (len(found), when)
            found.append(Activity(match["name"], match.group(0), 0.0, int(match["attempt"])))
        elif key in open_at:
            index, began = open_at.pop(key)
            if began is not None and when is not None:
                found[index] = replace(found[index], took_s=(when - began).total_seconds())
    return found


def plan_kills(seed: int, count: int, reference: Sequence[Activity]) -> list[Kill]:
    """`count` kills drawn from a seeded generator and the activities of a run that was not killed.

    The first kill waits for the k-th activity to start. Only first attempts count, on both
    sides: a worker that retries an activity writes more started lines than the reference did, and
    counting those would move the kill to an earlier activity, into the preparation of the sandbox
    when that was retried. The k-th first attempt of any worker is the k-th activity of the
    reference.

    The seed draws k from the first attempts from the first model turn to the first WINDOW_SHARE
    of them, so the long preparation of the sandbox is not part of the window and work is left
    after the kill. The kill then pauses for a seeded time that is no longer than that activity
    took in the reference, which puts it inside the activity or just after it.

    A later kill waits for the new worker to finish an activity, so that it falls in new work and
    not in the retry of the activity that was killed: an activity gives up after three attempts,
    and three kills on one tool call would end the run for a reason that is not a bug. Its pause
    is drawn the same way, from an activity of the window.

    The plan reads nothing but the seed and the reference's activities, so a seed is a repeatable
    plan and a run of any speed meets it.
    """
    firsts = [a for a in reference if a.attempt == 1]
    first_turn = next((n for n, a in enumerate(firsts, 1) if a.name == "llm_turn"), None)
    if first_turn is None:
        raise ValueError("the reference has no model turn to aim a kill after")
    last = max(first_turn, int(WINDOW_SHARE * len(firsts)))
    rng = random.Random(seed)
    kills: list[Kill] = []
    for n in range(count):
        nth = rng.randint(first_turn, last)
        delay_s = round(rng.uniform(0.0, firsts[nth - 1].took_s), 3)
        note = f"seed {seed}, kill {n + 1} of {count}"
        if n == 0:
            note += f", after activity {nth} started ({firsts[nth - 1].line})"
            kills.append(Kill(after=FIRST_ATTEMPT_STARTED, nth=nth, delay_s=delay_s, note=note))
        else:
            kills.append(Kill(after=ANY_ENDED, delay_s=delay_s, note=note))
    return kills


def kills_in_run(seed: int) -> int:
    """How many kills the full chaos run of a seed has: 1 to 3."""
    return random.Random(f"count-{seed}").randint(1, 3)


def appended(path: str = "CHANGES.txt", text: str = APPENDED_LINE) -> Callable[[str], bool]:
    """A condition that holds once the file in the sandbox of the task has the text.

    The kill that aims at the command waits for it, so that the kill comes after the command
    wrote its line and before the call ends.
    """

    def holds(task_id: str) -> bool:
        client = docker.from_env()
        try:
            label = {"label": f"longhaul.task_id={task_id}"}
            for container in client.containers.list(filters=label):
                code, output = container.exec_run(["cat", f"/workspace/{path}"])
                if code == 0 and text in output.decode("utf-8", errors="replace"):
                    return True
            return False
        finally:
            client.close()

    return holds


# ---------- the rows of a run ----------
@dataclass(frozen=True)
class CallRow:
    id: str
    turn_index: int
    name: str
    attempt: int
    pre_sha: str | None
    post_sha: str | None


def chain_problems(calls: Sequence[CallRow], head: str | None) -> list[str]:
    """Why the calls do not form one line of checkpoints from the first call to `head`.

    Every call must start where the one before it ended: its pre_sha is the post_sha of the last
    call that saved something, or the pre_sha of the first call. Two calls that both saved
    something from the same pre_sha are a fork. Calls are given in the order they ran, and a call
    that was retried appears once, as its last attempt.
    """
    problems: list[str] = []
    if not calls:
        return ["no tool_calls rows"]
    tip = calls[0].pre_sha
    parents: dict[str, str] = {}
    for call in calls:
        if call.pre_sha != tip:
            problems.append(
                f"{call.name} (turn {call.turn_index}) starts at {_short(call.pre_sha)}, "
                f"but the chain was at {_short(tip)}"
            )
        if call.post_sha is not None:
            if call.post_sha in parents:
                problems.append(f"{_short(call.post_sha)} is saved by two calls")
            if call.pre_sha in parents.values():
                problems.append(f"{_short(call.pre_sha)} has two children: the chain forks")
            parents[call.post_sha] = call.pre_sha or ""
            tip = call.post_sha
    if tip != head:
        problems.append(f"the chain ends at {_short(tip)}, but the run ended at {_short(head)}")
    return problems


def _short(sha: str | None) -> str:
    return "none" if sha is None else sha[:7]


# ---------- a worker ----------
@dataclass
class Worker:
    proc: subprocess.Popen[bytes]
    log: Path

    def text(self) -> str:
        raw = self.log.read_text(encoding="utf-8", errors="replace")
        return "".join(plain_line(line) for line in raw.splitlines(keepends=True))

    def wait_ready(self) -> None:
        deadline = time.monotonic() + WORKER_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if WORKER_READY in self.text():
                return
            if self.proc.poll() is not None:
                raise RuntimeError(f"the worker ended before it was ready:\n{self.text()}")
            time.sleep(SLOW_POLL_S)
        raise TimeoutError(
            f"the worker was not ready in {WORKER_START_TIMEOUT_S} s:\n{self.text()}"
        )


# ---------- what a run gives back ----------
@dataclass
class Run:
    task_id: str
    outcome: Outcome
    status: TaskStatus
    diff: str
    calls: list[CallRow]
    first_parent: list[str]  # the commits from the base to the head, oldest first
    ledger_attempts: dict[int, list[int]]  # turn index -> the attempts on the cost ledger
    kills: list[KillPoint]
    planned_kills: int
    activities: list[Activity]  # of the first worker, in the order they started
    logs: dict[str, str] = field(default_factory=dict)

    def report(self) -> str:
        kills = [f"{k.kind} at {k.after_s:.1f} s ({k.line})" for k in self.kills]
        tails = "\n".join(f"--- {name}\n{text[-3000:]}" for name, text in self.logs.items())
        return f"task {self.task_id}, kills: {kills or 'none'}\n{tails}"


# ---------- the services ----------
@functools.cache
def service_settings() -> dict[str, str]:
    """Where the compose services are. A .env that remaps a port applies here as it does to make."""
    settings = EnvSettings()
    database = settings.database_url.get_secret_value() if settings.database_url else LOCAL_DSN
    access, secret = settings.blob_access_key, settings.blob_secret_key
    return {
        "database": database,
        "temporal": settings.temporal_address,
        "namespace": settings.temporal_namespace,
        "blob_endpoint": settings.blob_endpoint or "http://localhost:9000",
        "blob_bucket": settings.blob_bucket or "longhaul",
        "blob_access_key": access.get_secret_value() if access else "longhaul",
        "blob_secret_key": secret.get_secret_value() if secret else "longhaul-secret",
    }


@contextlib.contextmanager
def scratch_database() -> Iterator[str]:
    """A fresh database at the head revision, dropped at the end. The application database is
    never touched: Alembic is given this URL explicitly."""
    server = make_url(service_settings()["database"])
    admin = str(server.set(database="postgres").render_as_string(hide_password=False))
    name = SCRATCH_PREFIX + uuid.uuid4().hex[:12]
    url = str(server.set(database=name).render_as_string(hide_password=False))
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        yield url
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def require_image() -> None:
    inspect = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, check=False
    )
    if inspect.returncode != 0:
        raise RuntimeError(f"image {IMAGE} is missing; run make sandbox-image first")
    if not LONGHAUL.exists():
        raise RuntimeError(f"{LONGHAUL} is missing; run uv sync, which installs the scripts")


# ---------- the stack ----------
class Stack:
    """One chaos run's workers, task and clean-up.

    `provider` is what the worker answers with: the scripted provider with the script that the
    task brings (the default), or whatever `worker_env` sets. `tuning` is added to the heartbeat
    timeout that every chaos run uses.
    """

    def __init__(
        self,
        root: Path,
        database: str,
        *,
        worker_env: dict[str, str] | None = None,
        script: Path | None = SCRIPT,
        continue_as_new_every: int | None = None,
    ) -> None:
        self.root = root
        self.database = database
        self.queue = f"lh-chaos-{uuid.uuid4().hex[:8]}"
        self.worker_env = worker_env or {}
        self.script = script
        self.tuning = Tuning(
            heartbeat_timeout_s=HEARTBEAT_TIMEOUT_S,
            continue_as_new_every_turns=continue_as_new_every,
        )
        self.workers: list[Worker] = []
        self.task_ids: list[str] = []
        root.mkdir(parents=True, exist_ok=True)

    # ----- processes -----
    @property
    def settings(self) -> dict[str, str]:
        return service_settings()

    def environment(self) -> dict[str, str]:
        """The machine's environment without any longhaul setting or model key, plus this stack.

        CI has no keys. The worker gets no script and no provider setting of its own unless the
        stack names them, so a scripted run can only get its script from the task.
        """
        drop = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"}
        env = {
            k: v for k, v in os.environ.items() if not k.startswith("LONGHAUL_") and k not in drop
        }
        # The console script finds the package through the editable install's .pth file, which
        # Python on macOS skips when the file carries the hidden flag. Name src explicitly.
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(ROOT / "src"), env.get("PYTHONPATH")])
        )
        s = self.settings
        env.update(
            LONGHAUL_DATABASE_URL=self.database,
            LONGHAUL_TEMPORAL_ADDRESS=s["temporal"],
            LONGHAUL_TEMPORAL_NAMESPACE=s["namespace"],
            LONGHAUL_TASK_QUEUE=self.queue,
            LONGHAUL_BLOB_ENDPOINT=s["blob_endpoint"],
            LONGHAUL_BLOB_BUCKET=s["blob_bucket"],
            LONGHAUL_BLOB_ACCESS_KEY=s["blob_access_key"],
            LONGHAUL_BLOB_SECRET_KEY=s["blob_secret_key"],
            LONGHAUL_CKPT_DIR=str(self.root / "ckpt"),
            LONGHAUL_CONFIG=str(CONFIG),
            **self.worker_env,
        )
        return env

    def start_worker(self) -> Worker:
        """Start `longhaul worker` in its own session, with stdout and stderr in one file."""
        log = self.root / f"worker-{len(self.workers) + 1}.log"
        with log.open("wb") as out:
            proc = subprocess.Popen(
                [str(LONGHAUL), "worker"],
                cwd=self.root,
                env=self.environment(),
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        worker = Worker(proc, log)
        self.workers.append(worker)
        return worker

    # ----- kills -----
    def _wait(
        self, holds: Callable[[], bool], stop: threading.Event, what: str, poll: float
    ) -> bool:
        """Wait until `holds()`. False when `stop` is set first. Waiting too long raises."""
        deadline = time.monotonic() + KILL_WAIT_S
        while not stop.is_set():
            if holds():
                return True
            if time.monotonic() > deadline:
                raise TimeoutError(f"gave up after {KILL_WAIT_S} s waiting for {what}")
            time.sleep(poll)
        return False

    def _kill_one(
        self, kill: Kill, worker: Worker, task_id: str, began: float, stop: threading.Event
    ) -> KillPoint | None:
        """Wait until the kill is due, then SIGKILL the worker by its pid and read where it was."""
        if not self._wait(
            lambda: len(kill.after.findall(worker.text())) >= kill.nth,
            stop,
            f"line {kill.nth} like {kill.after.pattern!r} ({kill.note})",
            POLL_S,
        ):
            return None
        if kill.until is not None:
            until = kill.until
            if not self._wait(
                lambda: until(task_id), stop, f"the condition ({kill.note})", SLOW_POLL_S
            ):
                return None
        if kill.delay_s and stop.wait(kill.delay_s):
            return None
        if stop.is_set():
            return None
        worker.proc.kill()  # SIGKILL, to the pid this stack started
        worker.proc.wait(timeout=60)
        kind, line = kill_point(worker.text())
        return KillPoint(kind=kind, line=line, after_s=time.monotonic() - began)

    def _kill_all(
        self, kills: Sequence[Kill], task_id: str, began: float, stop: threading.Event
    ) -> list[KillPoint]:
        points: list[KillPoint] = []
        for kill in kills:
            point = self._kill_one(kill, self.workers[-1], task_id, began, stop)
            if point is None:
                break
            points.append(point)
            self.start_worker()  # the restart the plan asks for, at once
        return points

    # ----- a run -----
    def run(self, kills: Sequence[Kill] = ()) -> Run:
        """Start a worker and a task, apply the kills, and return what the run left behind."""
        require_image()
        try:
            return asyncio.run(self._run(list(kills)))
        except Exception as exc:
            logs = "\n".join(f"--- {w.log.name}\n{w.text()[-3000:]}" for w in self.workers)
            raise AssertionError(f"the chaos run failed: {exc!r}\n{logs}") from exc

    async def _run(self, kills: list[Kill]) -> Run:
        s = self.settings
        title, body = cli.read_issue(INVENTORY / "ISSUE.md")
        task_id = cli.new_task_id(INVENTORY)
        self.task_ids.append(task_id)
        spec = cli.durable_spec(INVENTORY, task_id, title, body)
        blobs = S3BlobStore(
            s["blob_endpoint"], s["blob_bucket"], s["blob_access_key"], s["blob_secret_key"]
        )

        await asyncio.to_thread(self.start_worker().wait_ready)
        script_ref = await upload_script(blobs, task_id, self.script) if self.script else None
        client = await connect(s["temporal"], namespace=s["namespace"])
        handle = await start_task(
            client, spec, task_queue=self.queue, tuning=self.tuning, script=script_ref
        )
        began = time.monotonic()
        stop = threading.Event()
        killing = asyncio.create_task(
            asyncio.to_thread(self._kill_all, kills, task_id, began, stop)
        )
        # A kill that fails is raised by _until_done. This keeps asyncio from also reporting it
        # as an exception that nobody retrieved.
        killing.add_done_callback(lambda t: t.cancelled() or t.exception())
        result = asyncio.ensure_future(handle.result())
        try:
            async with asyncio.timeout(RUN_TIMEOUT_S):
                await self._until_done(result, killing)
            outcome = result.result()
        except WorkflowFailureError as exc:
            raise AssertionError(f"the workflow failed: {exc.cause or exc}") from exc
        finally:
            stop.set()  # a kill that is still waiting has nothing left to kill
        points = await killing

        status = await read_status(get_task(client, task_id), timeout_s=60)
        assert status.last_checkpoint is not None, status
        store = GitCheckpointStore(blobs, root=self.root / "ckpt")
        diff = await store.diff(task_id, status.last_checkpoint)
        return Run(
            task_id=task_id,
            outcome=outcome,
            status=status,
            diff=diff,
            calls=await asyncio.to_thread(self._calls, task_id),
            first_parent=await asyncio.to_thread(self._first_parent, store, task_id, status),
            ledger_attempts=await asyncio.to_thread(self._ledger, task_id),
            kills=points,
            planned_kills=len(kills),
            activities=activities_of(self.workers[0].text()),
            logs={w.log.name: w.text() for w in self.workers},
        )

    @staticmethod
    async def _until_done(
        result: asyncio.Future[Outcome], killing: asyncio.Future[list[KillPoint]]
    ) -> None:
        """Wait for the workflow. A kill that fails ends the wait, and the failure is raised."""
        while not result.done():
            waiting: set[asyncio.Future[object]] = {result}
            if not killing.done():
                waiting.add(killing)
            await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if killing.done() and killing.exception() is not None:
                raise killing.exception()  # type: ignore[misc]

    # ----- what the run left behind -----
    def _query(self, statement: str, *params: object) -> list[tuple[object, ...]]:
        with psycopg.connect(self.database) as conn:
            return conn.execute(statement, params).fetchall()

    def _calls(self, task_id: str) -> list[CallRow]:
        rows = self._query(
            "SELECT id, turn_index, name, attempt, pre_sha, post_sha FROM tool_calls "
            "WHERE task_id = %s ORDER BY turn_index, created_at, id",
            task_id,
        )
        return [CallRow(*row) for row in rows]  # type: ignore[arg-type]

    def _ledger(self, task_id: str) -> dict[int, list[int]]:
        rows = self._query(
            "SELECT turn_index, attempt FROM cost_ledger WHERE task_id = %s ORDER BY id", task_id
        )
        attempts: dict[int, list[int]] = {}
        for turn, attempt in rows:
            attempts.setdefault(int(turn), []).append(int(attempt))  # type: ignore[call-overload]
        return attempts

    @staticmethod
    def _first_parent(store: GitCheckpointStore, task_id: str, status: TaskStatus) -> list[str]:
        assert status.last_checkpoint is not None
        listed = subprocess.run(
            [
                "git",
                "--git-dir",
                str(store.repo_path(task_id)),
                "rev-list",
                "--first-parent",
                "--reverse",
                status.last_checkpoint,
                "^refs/tags/base",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return listed.stdout.split()

    # ----- clean-up -----
    def close(self) -> None:
        """Stop the workers this stack started and remove everything its tasks made."""
        for worker in self.workers:
            if worker.proc.poll() is None:
                worker.proc.kill()
            worker.proc.wait()
        for task_id in self.task_ids:
            self._remove_sandbox(task_id)
            self._remove_blobs(task_id)
        self._terminate_workflows()

    @staticmethod
    def _remove_sandbox(task_id: str) -> None:
        client = docker.from_env()
        try:
            label = {"label": f"longhaul.task_id={task_id}"}
            for container in client.containers.list(all=True, filters=label):
                container.remove(force=True)
            for volume in client.volumes.list(filters={"name": f"lh-ws-{task_id}"}):
                volume.remove(force=True)
        finally:
            client.close()

    def _remove_blobs(self, task_id: str) -> None:
        s = self.settings
        s3 = boto3.client(
            "s3",
            endpoint_url=s["blob_endpoint"],
            aws_access_key_id=s["blob_access_key"],
            aws_secret_access_key=s["blob_secret_key"],
            region_name="us-east-1",
        )
        listing = s3.list_objects_v2(Bucket=s["blob_bucket"], Prefix=f"tasks/{task_id}/")
        keys = [{"Key": item["Key"]} for item in listing.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=s["blob_bucket"], Delete={"Objects": keys})

    def _terminate_workflows(self) -> None:
        async def terminate() -> None:
            client = await connect(self.settings["temporal"], namespace=self.settings["namespace"])
            for task_id in self.task_ids:
                handle = get_task(client, task_id)
                if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                    await handle.terminate(reason="the chaos test ended")

        # Best effort: the server may be the thing that failed.
        with contextlib.suppress(Exception):
            asyncio.run(terminate())


# ---------- the checks every chaos run makes ----------
def survived(run: Run, reference: Run, *, kills: int = 1) -> list[str]:
    """Why a run does not count as having survived its kills. An empty list means it did.

    It ended with `finish`, its final diff is the diff of the run that was never killed, its
    tool_calls rows are one line of checkpoints, and the commits in the checkpoint repo are
    exactly the commits that line names (so no killed attempt left a fork behind).
    """
    problems: list[str] = []
    if run.outcome.kind is not OutcomeKind.PR_OPENED:
        problems.append(f"the run did not end with finish: {run.outcome}")
    if run.diff != reference.diff:
        problems.append(f"the final diff differs from the run that was not killed:\n{run.diff}")
    problems += chain_problems(run.calls, run.status.last_checkpoint)
    saved = [c.post_sha for c in run.calls if c.post_sha is not None]
    if run.first_parent != saved:
        problems.append(f"the commits {run.first_parent} are not the saved checkpoints {saved}")
    if len(run.kills) < kills:
        problems.append(f"{len(run.kills)} of {run.planned_kills} planned kills happened")
    return problems
