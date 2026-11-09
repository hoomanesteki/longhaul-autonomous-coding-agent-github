"""`make gate` and the console checks (LH-891).

Integration checkpoint 8 says the gate of phase 8 covers the console unit tests and the Playwright
main path, and the same holds for phase 9, which includes them. The target used to be `check` and
pytest only, so a gate could pass with a broken console. These tests read the Makefile, as the CI
config tests do, and ask `make -n` what a gate would run.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase8]

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"


def recipe(target: str) -> str:
    pattern = rf"^{re.escape(target)}:[^\n]*\n((?:\t.*\n|\n(?=\t))*)"
    found = re.search(pattern, MAKEFILE.read_text(), re.M)
    assert found, f"the Makefile has no {target} target"
    return found.group(1)


def prerequisites(target: str) -> list[str]:
    found = re.search(rf"^{re.escape(target)}:([^\n]*)", MAKEFILE.read_text(), re.M)
    assert found, f"the Makefile has no {target} target"
    return found.group(1).split()


def test_the_gate_runs_the_checks_and_then_the_tests_as_before() -> None:
    assert prerequisites("gate") == ["check"]
    assert "pytest" in recipe("gate")


def test_the_gate_names_the_console_checks_after_pytest_for_phases_8_and_9() -> None:
    text = recipe("gate")

    assert text.index("pytest") < text.index("console-check") < text.index("console-e2e")
    assert re.search(r"\b8\b", text) and re.search(r"\b9\b", text)


def test_the_browser_test_builds_the_sandbox_image_first() -> None:
    # The stack that Playwright starts runs real sandboxes, so a gate on a clean machine needs the
    # image. The build is cached when the image is there.
    assert "sandbox-image" in prerequisites("console-e2e")


def test_both_console_targets_are_phony() -> None:
    first = MAKEFILE.read_text().splitlines()[0]
    assert first.startswith(".PHONY:")
    assert {"console-check", "console-e2e", "sandbox-image", "gate"} <= set(first.split())


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
@pytest.mark.parametrize(
    ("phase", "console"),
    [("0", False), ("5", False), ("7", False), ("8", True), ("9", True)],
)
def test_a_dry_run_of_the_gate_includes_the_console_for_phases_8_and_9_only(
    phase: str, console: bool
) -> None:
    dry = subprocess.run(
        ["make", "-n", "gate", f"PHASE={phase}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert ("npm --prefix console run e2e" in dry) is console
    assert ("npm --prefix console test" in dry) is console
    assert ("docker build -t longhaul-sandbox:py312" in dry) is console
