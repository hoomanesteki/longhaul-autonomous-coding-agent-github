"""Collection rules for every test layer.

pytest runs these before it filters by `-m`, so a broken test cannot hide behind a filter:

* The fixture repos carry their own tests. They run in temp copies, never here.
* Each test gets the layer marker of its directory. The CI integration job selects by
  `-m integration`, so a test that forgot the marker would be skipped without a word.
* Each test names the phase that introduced it. `make gate PHASE=N` selects by phase, so a
  test without one would never run in a gate.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import pytest

if TYPE_CHECKING:
    from longhaul.telemetry.metrics import Metrics

collect_ignore = ["fixtures"]

TESTS_ROOT = Path(__file__).resolve().parent

# Directory under tests/ -> the marker every test in it carries.
LAYER_DIRS = {
    "unit": "unit",
    "integration": "integration",
    "e2e": "e2e",
    "chaos": "chaos",
    "live": "live",
    "slow": "slow",
}
# A test belongs to one of these only. live and slow are opt-in extras that can sit on top
# of a layer, for example a slow integration test.
EXCLUSIVE_LAYERS = frozenset({"unit", "integration", "e2e", "chaos"})
PHASE_MARKERS = frozenset(f"phase{n}" for n in range(10))

NO_LAYER = "is not under a layer directory (" + ", ".join(f"tests/{d}" for d in LAYER_DIRS) + ")"
NO_PHASE = "has no phase marker; gates select by phase, so this test would never run in one"


class _Item(Protocol):
    """The slice of pytest.Item that the rules use."""

    nodeid: str
    path: Path

    def iter_markers(self) -> Iterable[pytest.Mark]: ...

    def add_marker(self, marker: str) -> None: ...


@cache
def _relative_parts(path: Path, root: Path) -> tuple[str, ...] | None:
    try:
        return path.resolve().relative_to(root).parts
    except ValueError:
        return None


def check_items(items: Iterable[_Item], root: Path = TESTS_ROOT) -> list[str]:
    """Add each test's layer marker and describe every rule it breaks.

    Tests outside `root` belong to someone else and are left alone. Problems are grouped
    per file and message, so a module without markers is one line, not one per test.
    """
    found: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item in items:
        parts = _relative_parts(item.path, root)
        if parts is None:
            continue
        where = "/".join(parts)
        names = {mark.name for mark in item.iter_markers()}
        layer = LAYER_DIRS.get(parts[0]) if len(parts) > 1 else None
        if layer is None:
            found[(where, NO_LAYER)].append(item.nodeid)
        else:
            if layer in EXCLUSIVE_LAYERS:
                for other in sorted((names & EXCLUSIVE_LAYERS) - {layer}):
                    message = f"carries the {other!r} marker but lives under tests/{parts[0]}"
                    found[(where, message)].append(item.nodeid)
            if layer not in names:
                item.add_marker(layer)
        if not names & PHASE_MARKERS:
            found[(where, NO_PHASE)].append(item.nodeid)
    return [_describe(message, ids) for (_, message), ids in found.items()]


def _describe(message: str, nodeids: list[str]) -> str:
    extra = f" (and {len(nodeids) - 1} more in this file)" if len(nodeids) > 1 else ""
    return f"{nodeids[0]} {message}{extra}"


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # tryfirst: this has to run before the -m and -k filters drop items.
    problems = check_items(items)
    if problems:
        raise pytest.UsageError(
            "tests/conftest.py found tests that a gate or CI job would silently skip:\n  "
            + "\n  ".join(problems)
            + "\nPut each test file in one layer directory and give it a phase marker, for "
            "example pytestmark = [pytest.mark.unit, pytest.mark.phase1]."
        )


@pytest.fixture
def counters() -> Iterator[Metrics]:
    """A fresh set of metrics for the code under test, so a test reads only what it made.

    The activities update the process-wide metrics (telemetry/metrics.py). Without this, a count
    would carry over from one test to the next.
    """
    from longhaul.telemetry import metrics

    fresh = metrics.Metrics()
    with metrics.configured(fresh):
        yield fresh


@pytest.fixture(autouse=True)
def _activity_stand_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give a worker that runs TaskWorkflow a no-op for each best-effort activity it lacks.

    Many harnesses build their own worker, and none of them cares about the row of the task
    (LH-891) or the stored report (LH-991). Without the activities, the workflow logs a warning for
    every call it tries. One place fixes all of them, and a worker that registers its own copy is
    left as it is (tests/fakes/stand_ins.py).
    """
    from temporalio.worker import Worker
    from tests.fakes.stand_ins import with_stand_ins

    real_init = Worker.__init__

    def init(self: Worker, *args: Any, **kwargs: Any) -> None:
        if "workflows" in kwargs:
            kwargs["activities"] = with_stand_ins(kwargs["workflows"], kwargs.get("activities", ()))
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(Worker, "__init__", init)
