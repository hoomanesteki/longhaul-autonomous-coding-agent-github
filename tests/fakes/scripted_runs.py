"""A scripted run of a fixture repo in a FakeSandbox, for tests that need a finished run.

The scripts of tests/fixtures/scripts that serve the chaos tests wait on purpose: a model that
takes seconds to answer (`delay_s`) and a command that sleeps. A unit test has no kill to aim at
those, so `without_waits` copies a script with the waits taken out. The steps are the same.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from longhaul.agent.loop import LoopConfig, LoopResult, run_loop
from longhaul.contracts import LLMProvider, RepoSettings, TaskSpec
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

ROOT = Path(__file__).resolve().parents[2]
REPOS = ROOT / "tests" / "fixtures" / "repos"
SCRIPTS = ROOT / "tests" / "fixtures" / "scripts"

# The interpreter running the test, so it does not matter whether pytest is on the PATH.
TEST_COMMAND = f"{shlex.quote(sys.executable)} -m pytest -q"
SKIP_PARTS = {"REFERENCE.patch", "__pycache__", ".pytest_cache"}

CONFIG = LoopConfig(
    model="test-model",
    max_turns=20,
    max_tokens_per_turn=1000,
    max_tokens_retry=5000,
    tool_output_max_tokens=4000,
)

_DELAY = re.compile(r"^\s*delay_s:.*\n", re.MULTILINE)
_SLEEP = re.compile(r"\bsleep \d+(?:\.\d+)?\b")


def waitless(text: str) -> str:
    """The text of a script, or of a command in it, without `delay_s` lines and with `sleep 0`."""
    return _SLEEP.sub("sleep 0", _DELAY.sub("", text))


def without_waits(script: Path, into: Path) -> Path:
    """A copy of the script in the folder `into`, without its waits."""
    copy = into / script.name
    copy.write_text(waitless(script.read_text(encoding="utf-8")), encoding="utf-8")
    return copy


async def seed(box: FakeSandbox, name: str) -> None:
    """Copy the fixture repo into the sandbox, without the files a real copy leaves out."""
    source = REPOS / name
    for found in sorted(source.rglob("*")):
        parts = set(found.relative_to(source).parts)
        if (
            found.is_file()
            and not parts & SKIP_PARTS
            and not any(p.endswith(".egg-info") for p in parts)
        ):
            await box.write_file(str(found.relative_to(source)), found.read_bytes())


def spec_for(name: str) -> TaskSpec:
    """The task a run of the fixture works on: its ISSUE.md, with the folder name as the repo."""
    issue = (REPOS / name / "ISSUE.md").read_text(encoding="utf-8").strip().split("\n")
    return TaskSpec(
        task_id=f"lh-local-{name}-1",
        repo=f"local:{name}",
        issue_number=None,
        title=issue[0].lstrip("#").strip(),
        body="\n".join(issue[1:]).strip(),
        settings=RepoSettings(repo=f"local:{name}", test_command=TEST_COMMAND),
    )


async def play(name: str, provider: LLMProvider, box: FakeSandbox) -> LoopResult:
    """Seed the fixture and run the agent loop on it with the provider."""
    await seed(box, name)
    registry = default_registry(test_command=TEST_COMMAND)
    return await run_loop(spec_for(name), provider, registry, box, CONFIG)
