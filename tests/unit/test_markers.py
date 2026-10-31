"""Marker rules: scripts/markers.py, the Makefile selections and tests/conftest.py."""

import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# Opt-in tests: paid live calls, slow tests and the full chaos suite. Gates, `make itest`
# and the CI integration job leave them out. chaos_lite is the short run they keep.
OPT_IN = "not live and not slow and not (chaos and not chaos_lite)"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


markers = _load("markers", ROOT / "scripts" / "markers.py")
rules = _load("collection_rules", ROOT / "tests" / "conftest.py")


def _clean_env(**extra: str) -> dict[str, str]:
    """The caller's environment without anything that would leak the outer pytest or make."""
    drop = ("PYTEST_", "COV_CORE_", "COVERAGE_", "MAKEFLAGS", "MAKELEVEL", "MFLAGS")
    env = {k: v for k, v in os.environ.items() if not k.startswith(drop)}
    env.update(extra)
    return env


def test_marker_expression() -> None:
    assert markers.marker_expression(0) == "phase0"
    assert markers.marker_expression(2) == "phase0 or phase1 or phase2"
    assert markers.marker_expression(9).endswith("phase9")
    with pytest.raises(ValueError):
        markers.marker_expression(10)
    with pytest.raises(ValueError):
        markers.marker_expression(-1)


# --- the command line that `make gate` calls -------------------------------------------


def _markers_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "markers.py"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_prints_the_phase_expression() -> None:
    result = _markers_cli("2")
    assert result.returncode == 0
    assert result.stdout == "phase0 or phase1 or phase2\n"


@pytest.mark.parametrize(
    "args",
    [["10"], ["-1"], ["abc"], [""], [], ["2", "3"]],
    ids=["too-big", "negative", "not-a-number", "empty", "missing", "extra-argument"],
)
def test_cli_rejects_bad_input_with_exit_2_and_no_output(args: list[str]) -> None:
    result = _markers_cli(*args)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage" in result.stderr


# --- the Makefile recipes, run against a stand-in for uv -------------------------------

UV_STUB = """\
#!/bin/sh
# Stands in for uv: python scripts run for real, pytest calls are only recorded.
[ "$1" = run ] && shift
cmd="$1"
shift
case "$cmd" in
  python)
    [ -n "$STUB_SILENT" ] && exit 0
    exec "$STUB_PYTHON" "$@" ;;
  pytest)
    printf '%s\\n' "$@" > "$STUB_LOG" ;;
  *)
    echo "stub uv: unexpected command: $cmd" >&2
    exit 99 ;;
esac
"""


@dataclass
class MakeRun:
    returncode: int
    stderr: str
    pytest_args: list[str] | None

    @property
    def expression(self) -> str:
        assert self.pytest_args and self.pytest_args[0] == "-m", self.pytest_args
        return self.pytest_args[1]


def _make(work: Path, *args: str, silent_python: bool = False) -> MakeRun:
    """Run a Makefile target with `check` skipped and uv replaced by a recording stub."""
    bin_dir = work / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "uv"
    stub.write_text(UV_STUB, encoding="utf-8")
    stub.chmod(0o755)
    log = work / "pytest-args.txt"
    log.unlink(missing_ok=True)
    env = _clean_env(
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        STUB_PYTHON=sys.executable,
        STUB_LOG=str(log),
    )
    if silent_python:
        env["STUB_SILENT"] = "1"
    result = subprocess.run(
        ["make", "--no-print-directory", "-C", str(ROOT), "-o", "check", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    recorded = log.read_text(encoding="utf-8").splitlines() if log.exists() else None
    return MakeRun(result.returncode, result.stderr, recorded)


def test_gate_selects_phases_and_leaves_out_opt_in_tests(tmp_path: Path) -> None:
    run = _make(tmp_path, "gate", "PHASE=2")
    assert run.returncode == 0, run.stderr
    assert run.pytest_args == ["-m", f"(phase0 or phase1 or phase2) and {OPT_IN}"]


@pytest.mark.parametrize("phase", ["10", "abc", "", "-1"])
def test_gate_stops_when_the_markers_script_fails(tmp_path: Path, phase: str) -> None:
    run = _make(tmp_path, "gate", f"PHASE={phase}")
    assert run.returncode != 0
    assert run.pytest_args is None, f"pytest ran with {run.pytest_args}"
    assert "usage" in run.stderr


def test_gate_stops_when_the_markers_script_prints_nothing(tmp_path: Path) -> None:
    run = _make(tmp_path, "gate", "PHASE=2", silent_python=True)
    assert run.returncode != 0
    assert run.pytest_args is None, f"pytest ran with {run.pytest_args}"


def test_gate_passes_phase_to_the_script_as_one_quoted_word(tmp_path: Path) -> None:
    marker = tmp_path / "injected"
    run = _make(tmp_path, "gate", f"PHASE=2; touch {marker}")
    assert run.returncode != 0
    assert not marker.exists()


def test_itest_and_e2e_leave_out_opt_in_tests(tmp_path: Path) -> None:
    assert _make(tmp_path, "itest").pytest_args == ["-m", f"integration and {OPT_IN}"]
    assert _make(tmp_path, "e2e").pytest_args == ["-m", f"e2e and {OPT_IN}"]


def test_gate_still_runs_check_first() -> None:
    dry = subprocess.run(
        ["make", "--no-print-directory", "-C", str(ROOT), "-n", "gate", "PHASE=1"],
        env=_clean_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry.returncode == 0, dry.stderr
    assert dry.stdout.index("mypy src") < dry.stdout.index("pytest -m")


# --- scratch trees: real pytest runs with the repo's own config and conftest -----------

_CONFIG_BLOCK = re.compile(r"^\[tool\.pytest\.ini_options\]\n.*?(?=^\[|\Z)", re.S | re.M)


def _module(*marks: str) -> str:
    """Source of a test module with one passing test and the given markers."""
    if not marks:
        return "def test_it() -> None:\n    pass\n"
    uses = ", ".join(f"pytest.mark.{name}" for name in marks)
    return f"import pytest\n\npytestmark = [{uses}]\n\n\ndef test_it() -> None:\n    pass\n"


def _make_tree(base: Path, files: dict[str, str]) -> Path:
    block = _CONFIG_BLOCK.search((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert block, "pyproject.toml has no [tool.pytest.ini_options] block"
    (base / "tests").mkdir(parents=True)
    (base / "pyproject.toml").write_text(block.group(0), encoding="utf-8")
    (base / "tests" / "conftest.py").write_text(
        (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    for rel, source in files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return base


def _pytest(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=cwd,
        env=_clean_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def _collected(result: subprocess.CompletedProcess[str]) -> list[str]:
    return sorted(line for line in result.stdout.splitlines() if "::" in line)


SELECTION_FILES = {
    "tests/unit/test_core.py": ("unit", "phase0"),
    # A later phase: a gate for an earlier phase must not run it.
    "tests/unit/test_later.py": ("unit", "phase3"),
    # Same basename as the integration file below, on purpose.
    "tests/unit/test_stack.py": ("unit", "phase0"),
    "tests/integration/test_stack.py": ("integration", "phase1"),
    # No layer marker: the directory has to supply it.
    "tests/integration/test_forgot.py": ("phase1",),
    "tests/integration/test_swebench.py": ("integration", "slow", "phase1"),
    "tests/e2e/test_flow.py": ("e2e", "phase1"),
    "tests/live/test_provider.py": ("live", "phase1"),
    "tests/slow/test_embedder.py": ("slow", "phase1"),
    "tests/chaos/test_full.py": ("chaos", "phase2"),
    "tests/chaos/test_lite.py": ("chaos", "chaos_lite", "phase2"),
}
GATE_2 = [
    "tests/chaos/test_lite.py::test_it",
    "tests/e2e/test_flow.py::test_it",
    "tests/integration/test_forgot.py::test_it",
    "tests/integration/test_stack.py::test_it",
    "tests/unit/test_core.py::test_it",
    "tests/unit/test_stack.py::test_it",
]
ITEST = [
    "tests/integration/test_forgot.py::test_it",
    "tests/integration/test_stack.py::test_it",
]


@pytest.fixture(scope="module")
def selection_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    files = {rel: _module(*marks) for rel, marks in SELECTION_FILES.items()}
    return _make_tree(tmp_path_factory.mktemp("selection") / "tree", files)


@pytest.mark.parametrize(
    ("target", "expected"),
    [(("gate", "PHASE=2"), GATE_2), (("itest",), ITEST)],
    ids=["gate-2", "itest"],
)
def test_makefile_expressions_select_the_right_tests(
    tmp_path: Path, selection_tree: Path, target: tuple[str, ...], expected: list[str]
) -> None:
    expression = _make(tmp_path, *target).expression
    result = _pytest(selection_tree, "--collect-only", "-q", "-m", expression)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _collected(result) == expected


def test_ci_integration_job_uses_the_itest_selection(tmp_path: Path) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["integration"]["steps"]
    runs = [step["run"] for step in steps if "pytest" in step.get("run", "")]
    assert len(runs) == 1, runs
    found = re.search(r'-m\s+"([^"]+)"', runs[0])
    assert found, runs[0]
    assert found.group(1) == _make(tmp_path, "itest").expression


# --- tests/conftest.py: layer marker from the directory, phase marker required ---------


class FakeItem:
    """The slice of pytest.Item that the collection rules touch."""

    def __init__(self, rel: str, *marks: str) -> None:
        self.path = rules.TESTS_ROOT / rel
        self.nodeid = f"tests/{rel}::test_it"
        self._marks = list(marks)
        self.added: list[str] = []

    def iter_markers(self) -> Iterator[SimpleNamespace]:
        for name in (*self._marks, *self.added):
            yield SimpleNamespace(name=name)

    def add_marker(self, name: str) -> None:
        self.added.append(name)


@pytest.mark.parametrize("layer", ["unit", "integration", "e2e", "chaos", "live", "slow"])
def test_layer_marker_comes_from_the_directory(layer: str) -> None:
    item = FakeItem(f"{layer}/test_x.py", "phase1")
    assert rules.check_items([item]) == []
    assert item.added == [layer]


def test_layer_marker_is_not_added_twice() -> None:
    item = FakeItem("integration/test_x.py", "integration", "phase1")
    assert rules.check_items([item]) == []
    assert item.added == []


def test_nested_directories_keep_the_layer() -> None:
    item = FakeItem("integration/sandbox/test_x.py", "phase1")
    assert rules.check_items([item]) == []
    assert item.added == ["integration"]


@pytest.mark.parametrize("phase", range(10))
def test_every_phase_marker_is_accepted(phase: int) -> None:
    assert rules.check_items([FakeItem("unit/test_x.py", f"phase{phase}")]) == []


@pytest.mark.parametrize(
    "marks", [(), ("integration",), ("integration", "slow")], ids=["none", "layer", "layer-slow"]
)
def test_a_test_without_a_phase_marker_is_reported(marks: tuple[str, ...]) -> None:
    problems = rules.check_items([FakeItem("integration/test_x.py", *marks)])
    assert len(problems) == 1
    assert "tests/integration/test_x.py::test_it" in problems[0]
    assert "no phase marker" in problems[0]


@pytest.mark.parametrize("rel", ["test_x.py", "helpers/test_x.py", "console/test_x.py"])
def test_a_test_outside_the_layer_directories_is_reported(rel: str) -> None:
    problems = rules.check_items([FakeItem(rel, "phase0")])
    assert len(problems) == 1
    assert "layer directory" in problems[0]


@pytest.mark.parametrize(
    ("rel", "other"),
    [
        ("unit/test_x.py", "integration"),
        ("integration/test_x.py", "unit"),
        ("chaos/test_x.py", "e2e"),
    ],
)
def test_two_layers_on_one_test_are_reported(rel: str, other: str) -> None:
    problems = rules.check_items([FakeItem(rel, other, "phase1")])
    assert len(problems) == 1
    assert f"'{other}'" in problems[0]


@pytest.mark.parametrize(
    ("rel", "marks"),
    [
        ("integration/test_x.py", ("slow", "phase9")),
        ("integration/test_x.py", ("live", "phase1")),
        ("chaos/test_x.py", ("chaos_lite", "phase2")),
    ],
)
def test_opt_in_markers_can_sit_on_top_of_a_layer(rel: str, marks: tuple[str, ...]) -> None:
    assert rules.check_items([FakeItem(rel, *marks)]) == []


def test_items_outside_tests_are_left_alone() -> None:
    item = FakeItem("unit/test_x.py")
    item.path = ROOT / "src" / "longhaul" / "settings.py"
    assert rules.check_items([item]) == []
    assert item.added == []


def test_one_file_with_many_bad_tests_is_one_problem() -> None:
    items = [FakeItem("unit/test_x.py") for _ in range(5)]
    assert len(rules.check_items(items)) == 1


def test_registered_markers_cover_layers_phases_and_import_mode() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ini = config["tool"]["pytest"]["ini_options"]
    registered = {entry.split(":")[0].strip() for entry in ini["markers"]}
    assert {*rules.LAYER_DIRS.values(), "chaos_lite"} <= registered
    assert registered >= rules.PHASE_MARKERS
    assert set(markers.marker_expression(9).split(" or ")) == rules.PHASE_MARKERS
    assert "--import-mode=importlib" in ini["addopts"]


# --- the same rules under a real pytest ------------------------------------------------


def test_broken_tests_fail_collection_even_when_the_filter_hides_them(tmp_path: Path) -> None:
    files = {
        "tests/unit/test_nomark.py": _module("unit"),
        "tests/test_stray.py": _module("phase0"),
    }
    tree = _make_tree(tmp_path / "tree", files)
    result = _pytest(tree, "--collect-only", "-q", "-m", "phase0 or phase1")
    output = result.stdout + result.stderr
    assert result.returncode == 4, output
    assert "tests/unit/test_nomark.py::test_it has no phase marker" in output
    assert "tests/test_stray.py::test_it is not under a layer directory" in output


def test_same_basename_in_two_layers_collects(selection_tree: Path) -> None:
    result = _pytest(selection_tree, "--collect-only", "-q")
    assert result.returncode == 0, result.stdout + result.stderr
    stacks = [line for line in _collected(result) if line.endswith("test_stack.py::test_it")]
    assert stacks == [
        "tests/integration/test_stack.py::test_it",
        "tests/unit/test_stack.py::test_it",
    ]

    # Control: with pytest's default import mode the same tree cannot be collected. If this
    # ever collects, the clash is gone for another reason and this test proves nothing.
    control = _pytest(selection_tree, "--collect-only", "-q", "-o", "addopts=--strict-markers")
    assert control.returncode != 0
    assert "import file mismatch" in control.stdout + control.stderr


def test_layers_the_unit_job_skips_still_collect() -> None:
    # CI's unit job only collects tests/unit. The integration job and the gate collect the
    # rest, and a collection error there stays hidden until a PR is pushed.
    skipped = {"unit", "fixtures", "__pycache__"}
    layers = [p for p in sorted((ROOT / "tests").iterdir()) if p.is_dir() and p.name not in skipped]
    assert layers, "expected at least tests/integration"
    result = _pytest(ROOT, "--collect-only", "-q", *(str(p) for p in layers))
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
