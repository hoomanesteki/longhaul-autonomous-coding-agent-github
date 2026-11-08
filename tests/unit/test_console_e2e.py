"""The browser test of the console and the stack it runs on (LH-808).

The Playwright spec (console/e2e/main-path.spec.ts) needs a browser, a worker, Docker and the
compose services, so it runs in the console-e2e job and not here. What can go wrong without any
of that is a name that two files must share: the titles and repos the spec looks for, the lessons
it approves, the marker that picks the scripted answers of the stuck task, the port and the CI
job. These tests keep each pair in step, so a rename fails here in a second and not after a
five-minute stack start.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from tests.fakes.scripted_runs import SCRIPTS, without_waits

from longhaul.contracts import (
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from longhaul.providers.scripted import ScriptedProvider
from longhaul.settings import AppConfig, load_config

pytestmark = [pytest.mark.unit, pytest.mark.phase8]

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "scripts" / "seed_demo.sh"
DRIVER = ROOT / "scripts" / "demo_stack.py"
SPEC = ROOT / "console" / "e2e" / "main-path.spec.ts"
PLAYWRIGHT = ROOT / "console" / "playwright.config.ts"
SCRIPT = SCRIPTS / "console_demo.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = ROOT / "Makefile"


def load_driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("demo_stack", DRIVER)
    assert spec and spec.loader, "scripts/demo_stack.py is missing"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def demo() -> ModuleType:
    return load_driver()


# ---------- the seed script ----------
def test_the_seed_script_is_a_runnable_bash_script() -> None:
    assert SEED.is_file(), "scripts/seed_demo.sh is missing"
    assert os.access(SEED, os.X_OK), "scripts/seed_demo.sh is not executable"
    assert SEED.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")
    checked = subprocess.run(["bash", "-n", str(SEED)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_the_seed_script_hands_over_to_the_driver_with_a_signal_it_can_catch() -> None:
    text = SEED.read_text(encoding="utf-8")
    # exec, so the SIGTERM that Playwright sends reaches the driver and not a shell around it.
    assert re.search(r"^exec .*scripts/demo_stack\.py", text, re.M), text
    assert "set -euo pipefail" in text


def test_the_seed_script_and_the_playwright_config_use_one_port(demo: ModuleType) -> None:
    config = PLAYWRIGHT.read_text(encoding="utf-8")
    assert f'process.env.LH_DEMO_PORT ?? "{demo.DEFAULT_PORT}"' in config
    assert "LH_DEMO_PORT" in SEED.read_text(encoding="utf-8") + DRIVER.read_text(encoding="utf-8")


# ---------- the spec and the seed agree ----------
def test_the_spec_looks_for_the_tasks_the_seed_makes(demo: ModuleType) -> None:
    spec = SPEC.read_text(encoding="utf-8")
    assert [s.key for s in demo.SCENARIOS] == ["shipped", "stuck", "plan"]
    for scenario in demo.SCENARIOS:
        assert f'title: "{scenario.title}"' in spec, scenario.key
    shipped = demo.SCENARIOS[0]
    assert f'repo: "{shipped.repo}"' in spec


def test_the_spec_approves_the_lessons_the_seed_makes(demo: ModuleType) -> None:
    spec = SPEC.read_text(encoding="utf-8")
    assert len(demo.LESSONS) == 2
    for lesson in demo.LESSONS:
        assert f'"{lesson}"' in spec


def test_the_spec_logs_in_with_the_token_the_seed_uses(demo: ModuleType) -> None:
    assert f'?? "{demo.DEFAULT_TOKEN}"' in SPEC.read_text(encoding="utf-8")


def test_the_spec_names_the_four_steps_of_the_plan() -> None:
    steps = re.findall(r'test\.step\("([^"]+)"', SPEC.read_text(encoding="utf-8"))
    assert steps == [
        "log in and see the three tasks",
        "send a hint to the stuck task and see it code again",
        "approve the plan",
        "approve a lesson",
    ]


def test_the_hint_step_waits_at_most_fifteen_seconds_for_coding() -> None:
    spec = SPEC.read_text(encoding="utf-8")
    assert "const SEEN = { timeout: 15_000 };" in spec
    assert re.search(r'toHaveText\("coding", SEEN\)', spec)


# ---------- the config the worker runs on ----------
def test_only_the_plan_task_waits_for_approval(demo: ModuleType) -> None:
    base = yaml.safe_load((ROOT / "config" / "longhaul.example.yaml").read_text(encoding="utf-8"))
    config = demo.demo_config(base, ROOT / "config" / "pricing.yaml")
    waiting = {r["repo"] for r in config["repos"] if r["plan_approval_required"]}
    assert waiting == {s.repo for s in demo.SCENARIOS if s.key == "plan"}
    assert {r["repo"] for r in config["repos"]} == {s.repo for s in demo.SCENARIOS}
    assert all(r["allowlisted"] for r in config["repos"])
    assert config["memory"]["embedder"] == "hash"


def test_the_demo_config_loads_like_any_other(demo: ModuleType, tmp_path: Path) -> None:
    base = yaml.safe_load((ROOT / "config" / "longhaul.example.yaml").read_text(encoding="utf-8"))
    path = tmp_path / "longhaul.yaml"
    path.write_text(
        yaml.safe_dump(demo.demo_config(base, ROOT / "config" / "pricing.yaml")), encoding="utf-8"
    )
    loaded: AppConfig = load_config(path)
    assert [r.repo for r in loaded.repos] == [s.repo for s in demo.SCENARIOS]


def test_the_issues_carry_the_marker_only_where_the_script_keys_on_it(demo: ModuleType) -> None:
    bodies = {s.key: s.body("The issue text.") for s in demo.SCENARIOS}
    assert demo.STUCK_MARKER in bodies["stuck"]
    assert demo.STUCK_MARKER not in bodies["shipped"] + bodies["plan"]
    assert all(body.startswith("The issue text.") for body in bodies.values())


# ---------- the scripted model ----------
TOOLS = [
    ToolSpec(name=name, description=f"The {name} tool.", input_schema={"type": "object"})
    for name in (
        "list_dir",
        "read_file",
        "edit_file",
        "run_tests",
        "update_step",
        "submit_for_review",
        "submit_plan",
        "record_stuck_summary",
    )
]


async def ask(
    provider: ScriptedProvider, role: str, history: list[str], issue: str, hint: str | None = None
) -> LLMResponse:
    """The answer to a request after `len(history)` turns, each with the given tool result."""
    messages = [Message(role="user", content=[TextBlock(text=issue)])]
    for n, result in enumerate(history):
        messages.append(
            Message(role="assistant", content=[ToolUseBlock(id=f"toolu_{n}", name="x", input={})])
        )
        messages.append(
            Message(
                role="user", content=[ToolResultBlock(tool_use_id=f"toolu_{n}", content=result)]
            )
        )
    if hint is not None:
        messages[-1].content.append(TextBlock(text=hint))
    return await provider.complete(
        model="m",
        system=f"Role: {role}\nDo the task.",
        messages=messages,
        tools=TOOLS,
        max_tokens=100,
    )


def names(reply: LLMResponse) -> list[str]:
    return [call.name for call in reply.tool_calls]


@pytest.fixture
def provider(tmp_path: Path) -> ScriptedProvider:
    return ScriptedProvider(without_waits(SCRIPT, tmp_path))


async def test_a_task_without_the_marker_plans_and_fixes_the_bug(
    provider: ScriptedProvider, demo: ModuleType
) -> None:
    issue = demo.SCENARIOS[0].body("add() returns the difference")
    assert names(await ask(provider, "planner", [], issue)) == ["list_dir"]
    assert names(await ask(provider, "planner", ["ok", "ok"], issue)) == ["submit_plan"]
    assert names(await ask(provider, "coder", [], issue)) == ["list_dir"]
    assert names(await ask(provider, "coder", ["ok"] * 2, issue)) == ["edit_file"]
    done = await ask(provider, "coder", ["ok", "ok", "ok", "3 passed"], issue)
    assert names(done) == ["update_step", "update_step", "submit_for_review"]


async def test_the_task_with_the_marker_runs_the_suite_until_it_is_stuck(
    provider: ScriptedProvider, demo: ModuleType
) -> None:
    issue = demo.SCENARIOS[1].body("add() returns the difference")
    for turns in range(3):
        assert names(await ask(provider, "coder", ["1 failed"] * turns, issue)) == ["run_tests"]


async def test_the_hint_lets_the_stuck_task_fix_the_bug_and_wait_before_it_does(
    provider: ScriptedProvider, demo: ModuleType, tmp_path: Path
) -> None:
    issue = demo.SCENARIOS[1].body("add() returns the difference")
    failed = ["1 failed"] * 3
    hinted = await ask(provider, "coder", failed, issue, hint="Hint from console: use +")
    assert names(hinted) == ["edit_file"]
    # The wait is what keeps the phase at coding long enough for the console to show it. The
    # copy in the provider fixture has it taken out, so read it from the file.
    steps: list[dict[str, Any]] = yaml.safe_load(SCRIPT.read_text(encoding="utf-8"))["steps"]
    hint_step = next(s for s in steps if s["match"].get("user_text_contains") == "Hint from")
    assert hint_step["respond"]["delay_s"] >= 20
    after = await ask(provider, "coder", [*failed, "ok"], issue)
    assert names(after) == ["run_tests"]
    done = await ask(provider, "coder", [*failed, "ok", "3 passed"], issue)
    assert names(done)[-1] == "submit_for_review"


async def test_the_compactor_writes_the_question_of_the_stuck_task(
    provider: ScriptedProvider,
) -> None:
    reply = await ask(provider, "compactor", [], "Why the agent is stuck: the same failure.")
    assert names(reply) == ["record_stuck_summary"]


# ---------- the make target and the CI job ----------
def test_make_has_a_target_that_builds_the_console_and_runs_the_spec() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    recipe = re.search(r"^console-e2e:\n((?:\t.+\n)+)", makefile, re.M)
    assert recipe, "the Makefile has no console-e2e target"
    assert "npm --prefix console run build" in recipe.group(1)
    assert "npm --prefix console run e2e" in recipe.group(1)


def test_the_ci_job_runs_the_spec_on_the_services_of_the_e2e_job() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    job, e2e = jobs["console-e2e"], jobs["e2e"]
    assert job["services"] == e2e["services"]
    assert job["env"] == e2e["env"]
    runs = [" ".join(str(s.get("run", "")).split()) for s in job["steps"]]

    def at(fragment: str) -> int:
        found = [i for i, run in enumerate(runs) if fragment in run]
        assert found, f"the console-e2e job never runs {fragment!r}"
        return found[0]

    assert at("make sandbox-image") < at("npm run e2e")
    assert at("npm run build") < at("npm run e2e")
    assert at("playwright install") < at("npm run e2e")
    assert "chromium" in runs[at("playwright install")]
    assert job["defaults"]["run"]["working-directory"] == "console"


def test_the_ci_job_keeps_the_report_when_the_spec_fails() -> None:
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["console-e2e"]
    uploads = [
        s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert uploads and all(s["if"] == "failure()" for s in uploads)
