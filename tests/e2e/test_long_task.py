"""The long-task test (LH-407): a run that has to compact many times to finish.

`longhaul worker` and `longhaul run` run as processes, on the durable stack from `make up` and on
the local Docker daemon, as in tests/e2e/test_durable_run.py. The command is the smoke command of
Integration checkpoint 4: the longlog fixture, the scripted provider, and `--window 8000`. The
fixture has twelve modules with one planted bug each, and its test command prints 5,000 lines of
noise. The script (tests/fixtures/scripts/longlog.yaml) fixes one module in five turns, so the
task takes 62 turns, and 62 turns do not fit in a window of 8,000 tokens.

The worker gets a copy of the example config with `compact_at` 0.6 and `keep_recent_turns` 4. The
window stays at the config's 200,000, so that only the flag of `run` can make the task compact,
and a run that ignored the flag would fail the test.

What the test reads is what the run left behind. The final diff, printed by the command, is applied
to a fresh copy of the fixture, and the fixture's own tests run on it. The `events` and `turns`
tables of a scratch database say how often the task compacted and how full the window got. The
notes after the last compaction are read from the blob store.

Run `make up` and `make sandbox-image` first. The workspace setup builds a venv and installs
pytest from PyPI, so the machine needs outbound network. The test takes a few minutes: the
duration is in the failure message and in `pytest --durations`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import docker
import psycopg
import pytest
import yaml
from pydantic import TypeAdapter
from tests.chaos.harness import LONGHAUL, Stack, require_image, scratch_database

from longhaul.blobs.s3 import S3BlobStore
from longhaul.contracts import Message, TextBlock

pytestmark = [pytest.mark.e2e, pytest.mark.phase4]

ROOT = Path(__file__).resolve().parents[2]
LONGLOG = ROOT / "tests" / "fixtures" / "repos" / "longlog"
SCRIPT = ROOT / "tests" / "fixtures" / "scripts" / "longlog.yaml"
CONFIG = ROOT / "config" / "longhaul.example.yaml"

WINDOW = 8_000
COMPACT_AT = 0.6
KEEP_RECENT_TURNS = 4
HARD_LIMIT = 0.85
TURNS = 62
MODULES = [
    "chunks",
    "clamp",
    "dedupe",
    "initials",
    "leap",
    "ordinal",
    "pages",
    "percent",
    "ranges",
    "slug",
    "stats",
    "words",
]
# The first ten turns are a full run, and the fixes of chunks (turn 2) and clamp (turn 7).
FIXED_IN_THE_FIRST_10_TURNS = MODULES[:2]
RUN_TIMEOUT_S = 1500
HEADER = re.compile(r"^task (lh-local-longlog-[0-9a-f]{8}):", re.MULTILINE)
DIFF_MARKER = "final diff against base:"

_spec = importlib.util.spec_from_file_location(
    "make_fixture_git", ROOT / "scripts" / "make_fixture_git.py"
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
make_fixture_git = _module.make_fixture_git
isolated_git_env = _module.isolated_git_env


class LonglogStack(Stack):
    """The worker side of the chaos harness, with the config of this test instead of the example.

    The harness starts the worker, removes the sandboxes, blobs and workflows of the tasks it was
    told about (`task_ids`), and knows where the services are. The run itself is started here, as
    the command, and not through the harness's own client.
    """

    def __init__(self, root: Path, database: str) -> None:
        super().__init__(root, database)
        self.config = self._write_config()

    def _write_config(self) -> Path:
        config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        config["context"]["compact_at"] = COMPACT_AT
        config["context"]["keep_recent_turns"] = KEEP_RECENT_TURNS
        path = self.root / "longhaul.yaml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return path

    def environment(self) -> dict[str, str]:
        return {**super().environment(), "LONGHAUL_CONFIG": str(self.config)}

    def run_command(self) -> list[str]:
        return [
            str(LONGHAUL),
            "run",
            "--path",
            str(LONGLOG),
            "--issue-file",
            str(LONGLOG / "ISSUE.md"),
            "--provider",
            "scripted",
            "--script",
            str(SCRIPT),
            "--window",
            str(WINDOW),
        ]


# ---------- what the run left behind ----------
def rows(database: str, statement: str, *params: object) -> list[tuple[Any, ...]]:
    with psycopg.connect(database) as conn:
        return conn.execute(statement, params).fetchall()


def compactions(database: str, task_id: str) -> list[dict[str, Any]]:
    found = rows(
        database,
        "SELECT payload FROM events WHERE task_id = %s AND type = 'compaction' ORDER BY id",
        task_id,
    )
    return [payload for (payload,) in found]


def max_fraction(database: str, task_id: str) -> float:
    """The share of the window that the fullest request took, by the usage the provider reported.

    Only the coder's turns are counted. The compactor's requests are small, and the plan's limit is
    about the requests that carry the task.
    """
    found = rows(
        database,
        "SELECT max(input_tokens + cache_read_tokens + cache_write_tokens) FROM turns "
        "WHERE task_id = %s AND role = 'coder'",
        task_id,
    )
    ((fullest,),) = found
    return float(fullest) / WINDOW


def notes_after_the_last_compaction(
    stack: Stack, database: str, task_id: str, last: dict[str, Any]
) -> str:
    """The text of the first message of the view that the next model call was sent.

    The call after the last compaction starts from the compacted view, and its row in `turns`
    names that view by blob key. The first message holds the task and the notes.
    """
    ((key,),) = rows(
        database,
        "SELECT request_ref FROM turns WHERE task_id = %s AND role = 'coder' AND turn_index = %s",
        task_id,
        last["turn_index"] + 1,
    )
    s = stack.settings
    blobs = S3BlobStore(
        s["blob_endpoint"], s["blob_bucket"], s["blob_access_key"], s["blob_secret_key"]
    )
    raw = asyncio.run(blobs.get_by_key(key))
    view = TypeAdapter(list[Message]).validate_json(raw)
    return "\n".join(b.text for b in view[0].content if isinstance(b, TextBlock))


def git(repo: Path, *args: str) -> None:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=isolated_git_env(),
        check=False,
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"


def fixture_tests_on(diff: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Apply the printed diff to a fresh copy of the fixture, and run the fixture's tests."""
    patch = tmp_path / "printed.patch"
    patch.write_text(diff.rstrip("\n") + "\n", encoding="utf-8")
    fixed = make_fixture_git(LONGLOG, tmp_path / "fixed")
    git(fixed, "apply", str(patch))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:asyncio"],
        cwd=fixed,
        env={**os.environ, "PYTHONPATH": str(fixed / "src")},
        capture_output=True,
        text=True,
        check=False,
    )


def leftovers(task_id: str) -> tuple[list[str], list[str]]:
    client = docker.from_env()
    try:
        label = {"label": f"longhaul.task_id={task_id}"}
        containers = client.containers.list(all=True, filters=label)
        volumes = client.volumes.list(filters={"name": f"lh-ws-{task_id}"})
        return [str(c.name) for c in containers], [str(v.name) for v in volumes]
    finally:
        client.close()


# ---------- the test ----------
def test_longlog_many_compactions(tmp_path: Path) -> None:
    require_image()
    with scratch_database() as database:
        stack = LonglogStack(tmp_path, database)
        try:
            worker = stack.start_worker()
            worker.wait_ready()
            log = tmp_path / "run.log"
            began = time.monotonic()
            with log.open("w") as out:
                run = subprocess.Popen(
                    stack.run_command(),
                    cwd=tmp_path,
                    env=stack.environment(),
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                try:
                    code = run.wait(timeout=RUN_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    run.kill()
                    run.wait()
                    code = None
            took = time.monotonic() - began
            output = log.read_text(encoding="utf-8")
            found = HEADER.search(output)
            assert found, f"the run printed no task id:\n{output}"
            task_id = found.group(1)
            stack.task_ids.append(task_id)
            shown = f"after {took:.0f} s, run:\n{output}\nworker:\n{worker.text()[-3000:]}"

            assert code == 0, shown
            assert "Traceback" not in output, shown
            assert re.search(rf"^finished after {TURNS} turns: Fixed the planted bug", output, re.M)

            # The task finished with all twelve modules fixed: the printed diff is the fix of each
            # of them, and the fixture's tests pass on the tree it makes.
            diff = output.split(f"{DIFF_MARKER}\n", 1)[1]
            changed = re.findall(r"^diff --git a/(\S+) b/", diff, re.MULTILINE)
            assert changed == [f"src/longlog/{m}.py" for m in MODULES], shown
            tests = fixture_tests_on(diff, tmp_path)
            assert tests.returncode == 0, tests.stdout
            assert "12 passed" in tests.stdout, tests.stdout

            # At least three compactions ran, and none of them was forced: the window never
            # came near the hard limit.
            events = compactions(database, task_id)
            assert len(events) >= 3, f"{len(events)} compactions {shown}"
            assert all(e["forced"] is False for e in events), events
            assert all(e["tokens_after"] < e["tokens_before"] for e in events), events

            # The fullest request stayed under the limit, and it did get full: with the window
            # of the config (200,000 tokens) it would have taken 2 percent.
            fraction = max_fraction(database, task_id)
            assert fraction < HARD_LIMIT, f"max_fraction {fraction:.3f}"
            assert fraction > COMPACT_AT - 0.1, f"max_fraction {fraction:.3f}: was the window used?"

            # The notes after the last compaction still list what was fixed in the first 10 turns.
            notes = notes_after_the_last_compaction(stack, database, task_id, events[-1])
            for name in FIXED_IN_THE_FIRST_10_TURNS:
                assert f"src/longlog/{name}.py" in notes, notes
                assert f"Fixed {name}:" in notes, notes

            assert leftovers(task_id) == ([], [])
        finally:
            stack.close()
