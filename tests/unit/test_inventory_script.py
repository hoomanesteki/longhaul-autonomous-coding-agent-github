"""inventory_fix.yaml: the script that the chaos tests play.

The chaos tests aim kills at it, so they depend on its shape: enough turns for kills to land in
different places, a model turn that waits, and a command that is not safe to run twice. These
tests keep that shape, and play the script once in a FakeSandbox to show that it fixes the repo.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.fakes.scripted_runs import SCRIPTS, TEST_COMMAND, play, waitless, without_waits

from longhaul.contracts import OutcomeKind, ProviderError
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.local_manager import FakeSandbox

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

SCRIPT = SCRIPTS / "inventory_fix.yaml"
# The turn that waits for the model, and the shortest sleep that gives a kill time to land in
# the command. tests/chaos/test_kill_worker.py aims at both.
SLOW_TURN = 3
MIN_SLEEP_S = 4


def steps() -> list[dict[str, Any]]:
    loaded = yaml.safe_load(SCRIPT.read_text(encoding="utf-8"))
    found: list[dict[str, Any]] = loaded["steps"]
    return found


def calls() -> list[dict[str, Any]]:
    return [call for step in steps() for call in step["respond"].get("tool_calls", [])]


@pytest.fixture
async def sbx() -> Any:
    box = FakeSandbox()
    yield box
    await box.destroy()


def test_the_script_loads() -> None:
    ScriptedProvider(SCRIPT)


def test_the_script_is_long_enough_for_kills_to_land_in_different_places() -> None:
    assert len(steps()) >= 8
    assert {"list_dir", "read_file", "edit_file", "run_tests", "finish"} <= {
        call["name"] for call in calls()
    }


def test_one_model_turn_waits_so_that_a_kill_can_land_in_it() -> None:
    delays = {n: step["respond"].get("delay_s") for n, step in enumerate(steps())}
    assert delays[SLOW_TURN] is not None and delays[SLOW_TURN] >= 2
    assert [n for n, delay in delays.items() if delay] == [SLOW_TURN]


def audit_command() -> str:
    commands = [call["input"]["command"] for call in calls() if call["name"] == "run_command"]
    assert len(commands) == 1
    return str(commands[0])


def test_the_command_appends_to_a_file_and_then_sleeps() -> None:
    command = audit_command()
    append, _, sleep = command.partition("&&")
    assert ">>" in append
    assert sleep.split() == ["sleep", sleep.split()[1]]
    assert int(sleep.split()[1]) >= MIN_SLEEP_S


async def test_running_the_command_twice_writes_the_line_twice(sbx: FakeSandbox) -> None:
    # This is what makes the restore to pre_sha matter: a retried call that starts from the
    # half done tree of the first attempt leaves two lines, and the diff gives it away.
    command = waitless(audit_command())
    for _ in range(2):
        done = await sbx.exec(command, timeout_s=30)
        assert done.exit_code == 0, done.stderr
    written = (await sbx.read_file("CHANGES.txt")).decode().splitlines()
    assert len(written) == 2 and written[0] == written[1]


async def test_the_script_fixes_inventory(sbx: FakeSandbox, tmp_path: Path) -> None:
    provider = ScriptedProvider(without_waits(SCRIPT, tmp_path))

    result = await play("inventory", provider, sbx)

    assert result.outcome.kind is OutcomeKind.PR_OPENED, result.outcome.summary
    assert result.turns == len(steps())
    done = await sbx.exec(TEST_COMMAND, timeout_s=60)
    assert done.exit_code == 0, done.stdout
    assert "6 passed" in done.stdout
    assert (await sbx.read_file("CHANGES.txt")).decode().splitlines() == [
        "remove() rejects an overdraft"
    ]


async def test_the_script_does_not_finish_while_a_test_fails(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    text = SCRIPT.read_text(encoding="utf-8")
    # Without a working check in remove(), the test of the issue still fails.
    cut = text.replace("if quantity > current:", "if quantity > current + 99:")
    assert cut != text
    broken.write_text(waitless(cut), encoding="utf-8")

    box = FakeSandbox()
    try:
        # The finish step waits for "6 passed", so it is never offered a reply: the run stops
        # with a provider error instead of a finish call that claims success.
        with pytest.raises(ProviderError, match="no scripted step matches"):
            await play("inventory", ScriptedProvider(broken), box)
    finally:
        await box.destroy()
