"""scripts/chaos_kill.sh: the manual demo of a worker that is killed in the middle of a run.

The script needs the stack from `make up`, so these tests read it. They check that it is valid
shell and that it keeps to the rules of a shared machine: it kills the worker it started by pid,
never by name, and it removes only the containers of its own task.
"""

import re
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "chaos_kill.sh"


def code() -> str:
    """The script without its comment lines."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def test_the_script_is_executable() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR


def test_the_script_is_valid_bash() -> None:
    done = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_the_script_stops_on_the_first_error() -> None:
    assert re.search(r"^set -euo pipefail$", code(), re.MULTILINE)


@pytest.mark.parametrize("word", ["pkill", "killall", "xargs kill", "kill -9 -1"])
def test_the_script_never_kills_by_name(word: str) -> None:
    assert word not in code()


def test_the_script_kills_only_the_pids_of_the_processes_it_started() -> None:
    kills = re.findall(r"\bkill\b[^\n]*", code())
    assert kills, "the script kills nothing"
    assert all('"$WORKER_PID"' in k or '"$RUN_PID"' in k for k in kills), kills


def test_the_script_removes_only_the_containers_of_its_own_task() -> None:
    text = code()
    assert "docker rm" not in text
    assert "label=longhaul.task_id=$TASK_ID" in text


def test_the_script_uses_a_task_queue_of_its_own() -> None:
    assert re.search(r"LONGHAUL_TASK_QUEUE=\S*\$\$", code()) or "mktemp" in code()


def test_the_script_cleans_up_when_it_is_interrupted() -> None:
    assert re.search(r"^trap \S+ EXIT", code(), re.MULTILINE)
