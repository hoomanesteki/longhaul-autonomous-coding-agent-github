"""Behaviour of scripts/demo.sh when the run is cut short.

The script runs against stand-ins for the `longhaul` command, `docker` and the Python that talks
to Temporal, so no worker, container or workflow is started. The stand-ins write what they were
asked to do into a folder, and the tests read it back.
"""

from __future__ import annotations

import contextlib
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "scripts" / "demo.sh"
TASK_ID = "lh-local-fake-1"

# One stand-in for the longhaul command. `worker` prints its ready line and runs until it is told
# to stop. Like the real worker, it installs its own SIGINT handler and stops at once, which
# replaces the "ignore" that bash gives a background job. So a Ctrl-C that reaches it ends it.
# `run` prints the task line and follows the "workflow" until a cancel arrives, as the real
# command does.
FAKE_LONGHAUL = """#!/usr/bin/env bash
dir="$FAKE_DIR"
case "$1" in
  worker)
    echo $$ >"$dir/worker.pid"
    echo "worker running"
    if [ -n "${FAKE_WORKER_LIFE_S:-}" ]; then sleep "$FAKE_WORKER_LIFE_S"; exit 1; fi
    # Bash cannot trap a signal that a background job started with ignored, but perl can
    # replace it, as the real worker does. The pid stays the one written above.
    exec perl -e '$SIG{INT} = $SIG{TERM} = sub { exit 0 }; sleep 1 while 1'
    ;;
  run)
    echo $$ >"$dir/run.pid"
    echo "task lh-local-fake-1: Fix the calculator"
    while [ ! -e "$dir/closed" ]; do sleep 0.1; done
    echo "task ended as cancelled"
    exit 1
    ;;
  cancel)
    shift
    echo "cancel $*" >>"$dir/calls.log"
    if kill -0 "$(cat "$dir/worker.pid")" 2>/dev/null; then
      echo "worker up at cancel" >>"$dir/calls.log"
    fi
    if [ "${FAKE_CANCEL_WORKS:-1}" = 1 ]; then touch "$dir/closed"; fi
    ;;
esac
"""

# The docker stand-in finds no container, so the sandbox checks pass.
FAKE_DOCKER = "#!/usr/bin/env bash\nexit 0\n"

# The Python stand-in records the id it was asked to terminate and closes the workflow.
FAKE_PYTHON = """#!/usr/bin/env bash
echo "terminate $*" >>"$FAKE_DIR/calls.log"
touch "$FAKE_DIR/closed"
"""


def executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@dataclass
class Demo:
    process: subprocess.Popen[bytes]
    folder: Path

    @property
    def output(self) -> str:
        return (self.folder / "out.log").read_text(encoding="utf-8", errors="replace")

    @property
    def calls(self) -> str:
        path = self.folder / "calls.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def wait_for_output(self, text: str, seconds: float = 20) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if text in self.output:
                return
            time.sleep(0.1)
        raise AssertionError(f"{text!r} never appeared in:\n{self.output}")

    def interrupt(self) -> None:
        # Ctrl-C reaches every process of the foreground group, not just the script.
        os.killpg(self.process.pid, signal.SIGINT)


@pytest.fixture
def start_demo(tmp_path: Path) -> Iterator[object]:
    started: list[Demo] = []

    def start(**extra: str) -> Demo:
        folder = tmp_path / f"case{len(started)}"
        bin_dir = folder / "bin"
        bin_dir.mkdir(parents=True)
        longhaul = executable(bin_dir / "longhaul", FAKE_LONGHAUL)
        executable(bin_dir / "docker", FAKE_DOCKER)
        python = executable(bin_dir / "python", FAKE_PYTHON)
        tmp = folder / "tmp"
        tmp.mkdir()
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "TMPDIR": str(tmp),
            "FAKE_DIR": str(folder),
            "LONGHAUL": str(longhaul),
            "DEMO_PYTHON": str(python),
            "DEMO_WAIT_S": "20",
            "DEMO_CANCEL_WAIT_S": "2",
            **extra,
        }
        out = (folder / "out.log").open("wb")
        process = subprocess.Popen(
            [str(DEMO)], env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True
        )
        demo = Demo(process, folder)
        started.append(demo)
        return demo

    yield start
    for demo in started:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(demo.process.pid, signal.SIGKILL)
        demo.process.wait()
        for name in ("worker.pid", "run.pid"):
            path = demo.folder / name
            if path.exists():
                with contextlib.suppress(ProcessLookupError, ValueError):
                    os.kill(int(path.read_text()), signal.SIGKILL)


def test_interrupt_cancels_the_workflow_while_the_worker_is_still_up(start_demo) -> None:  # type: ignore[no-untyped-def]
    demo = start_demo()
    demo.wait_for_output(f"task {TASK_ID}")
    time.sleep(1.0)
    demo.interrupt()
    assert demo.process.wait(timeout=60) != 0
    assert f"cancel {TASK_ID} --reason demo interrupted" in demo.calls
    # The workflow removes its sandbox on cancel, which only the worker can do.
    assert "worker up at cancel" in demo.calls
    assert "terminate" not in demo.calls


def test_interrupt_terminates_the_workflow_when_the_cancel_does_not_close_it(start_demo) -> None:  # type: ignore[no-untyped-def]
    demo = start_demo(FAKE_CANCEL_WORKS="0")
    demo.wait_for_output(f"task {TASK_ID}")
    time.sleep(1.0)
    demo.interrupt()
    assert demo.process.wait(timeout=60) != 0
    assert f"terminate - {TASK_ID}" in demo.calls


def test_the_run_output_reaches_the_terminal_while_the_task_runs(start_demo) -> None:  # type: ignore[no-untyped-def]
    demo = start_demo()
    demo.wait_for_output(f"task {TASK_ID}")
    assert demo.process.poll() is None


def test_a_worker_that_dies_during_the_run_ends_the_demo_with_an_error(start_demo) -> None:  # type: ignore[no-untyped-def]
    demo = start_demo(FAKE_WORKER_LIFE_S="1")
    code = demo.process.wait(timeout=60)
    assert code != 0
    assert "worker stopped" in demo.output
    # The task is not left open: the demo cancels or terminates it on the way out.
    assert TASK_ID in demo.calls


def test_the_terminate_helper_reads_the_address_from_dotenv(tmp_path: Path) -> None:
    # A stand-in for temporalio records where the helper connects. The helper runs in a folder
    # whose .env names a non-default address, as the demo does from the repo root.
    fake = tmp_path / "fake" / "temporalio"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("", encoding="utf-8")
    (fake / "client.py").write_text(
        "import os\n"
        "class _Handle:\n"
        "    async def terminate(self, reason=None):\n"
        "        pass\n"
        "class Client:\n"
        "    @classmethod\n"
        "    async def connect(cls, address, namespace='default'):\n"
        "        open(os.environ['FAKE_OUT'], 'w').write(address + ' ' + namespace)\n"
        "        return cls()\n"
        "    def get_workflow_handle(self, workflow_id):\n"
        "        return _Handle()\n",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_text(
        "LONGHAUL_TEMPORAL_ADDRESS=localhost:7999\nLONGHAUL_TEMPORAL_NAMESPACE=demo-ns\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.txt"
    env = {k: v for k, v in os.environ.items() if not k.startswith("LONGHAUL_")}
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path / "fake"), str(ROOT / "src")])
    env["FAKE_OUT"] = str(out)
    helper = ROOT / "scripts" / "demo_terminate.py"
    subprocess.run(
        [sys.executable, str(helper), TASK_ID], cwd=work, env=env, check=True, timeout=60
    )
    assert out.read_text(encoding="utf-8") == "localhost:7999 demo-ns"
