import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "repos"

_spec = importlib.util.spec_from_file_location(
    "make_fixture_git", ROOT / "scripts" / "make_fixture_git.py"
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
make_fixture_git = _module.make_fixture_git

FIXTURE_NAMES = ["calc", "textkit", "inventory"]

# The failing tests each fixture must have at base, and that ISSUE.md names.
EXPECTED_FAILURES = {
    "calc": {"tests/test_calc.py::test_add_positive_numbers"},
    "textkit": {"tests/test_slug.py::test_slugify_accented_latin"},
    "inventory": {"tests/test_store.py::test_remove_more_than_stock_is_rejected"},
}


def _run_pytest(repo: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(repo / "src")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rf", "--tb=no", "-p", "no:cacheprovider"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _failed_ids(output: str) -> set[str]:
    return set(re.findall(r"^FAILED (\S+)", output, flags=re.MULTILINE))


def _git_out(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_fails_at_base(name: str, tmp_path: Path) -> None:
    repo = make_fixture_git(FIXTURES / name, tmp_path / name)
    result = _run_pytest(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _failed_ids(result.stdout) == EXPECTED_FAILURES[name]
    issue = (FIXTURES / name / "ISSUE.md").read_text(encoding="utf-8")
    for test_id in EXPECTED_FAILURES[name]:
        assert test_id.split("::")[1] in issue


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_reference_patch_passes(name: str, tmp_path: Path) -> None:
    repo = make_fixture_git(FIXTURES / name, tmp_path / name)
    subprocess.run(
        ["git", "-C", str(repo), "apply", str(FIXTURES / name / "REFERENCE.patch")],
        check=True,
        capture_output=True,
        text=True,
    )
    result = _run_pytest(repo)
    assert result.returncode == 0, result.stdout


def test_make_fixture_git_tags_base(tmp_path: Path) -> None:
    repo = make_fixture_git(FIXTURES / "calc", tmp_path / "calc")
    assert _git_out(repo, "tag", "--list", "base") == "base"
    assert _git_out(repo, "log", "-1", "--format=%an <%ae>|%s") == (
        "longhaul-fixture <fixture@localhost>|initial"
    )
    assert not (repo / "REFERENCE.patch").exists()
