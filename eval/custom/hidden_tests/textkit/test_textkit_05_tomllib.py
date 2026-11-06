import importlib
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


def project_root() -> Path:
    here = Path(__file__).resolve()
    return next(p for p in here.parents if (p / "pyproject.toml").is_file())


@pytest.fixture
def without_tomli(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make `import tomli` fail and import textkit again from scratch."""
    before = set(sys.modules)
    monkeypatch.setitem(sys.modules, "tomli", None)
    for name in [n for n in sys.modules if n == "textkit" or n.startswith("textkit.")]:
        monkeypatch.delitem(sys.modules, name)
    yield
    for name in set(sys.modules) - before:
        sys.modules.pop(name, None)


def test_config_loads_without_tomli(without_tomli: None, tmp_path: Path) -> None:
    config: Any = importlib.import_module("textkit.config")
    path = tmp_path / "textkit.toml"
    path.write_text('width = 60\nsuffix = "~"\n', encoding="utf-8")
    assert config.load_defaults(path) == {"width": 60, "suffix": "~"}


def test_config_still_returns_nothing_for_a_missing_file(
    without_tomli: None, tmp_path: Path
) -> None:
    config: Any = importlib.import_module("textkit.config")
    assert config.load_defaults(tmp_path / "missing.toml") == {}


def test_tomli_is_not_a_declared_dependency() -> None:
    text = (project_root() / "pyproject.toml").read_text(encoding="utf-8")
    project = tomllib.loads(text)["project"]
    declared = [str(item).lower() for item in project.get("dependencies", [])]
    assert not [item for item in declared if item.startswith("tomli")]
