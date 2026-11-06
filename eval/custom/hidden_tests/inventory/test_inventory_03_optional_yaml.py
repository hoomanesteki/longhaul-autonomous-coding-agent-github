import importlib
import json
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


def project_root() -> Path:
    here = Path(__file__).resolve()
    return next(p for p in here.parents if (p / "pyproject.toml").is_file())


def project_table() -> dict[str, Any]:
    text = (project_root() / "pyproject.toml").read_text(encoding="utf-8")
    table: dict[str, Any] = tomllib.loads(text)["project"]
    return table


def names(requirements: list[str]) -> list[str]:
    return [item.lower().replace("_", "-") for item in requirements]


@pytest.fixture
def without_pyyaml(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Make `import yaml` fail and import inventory again from scratch."""
    before = set(sys.modules)
    monkeypatch.setitem(sys.modules, "yaml", None)
    for name in [n for n in sys.modules if n == "inventory" or n.startswith("inventory.")]:
        monkeypatch.delitem(sys.modules, name)
    yield importlib.import_module("inventory")
    for name in set(sys.modules) - before:
        sys.modules.pop(name, None)


def test_the_package_imports_without_pyyaml(without_pyyaml: Any) -> None:
    assert without_pyyaml.Inventory is not None


def test_json_export_works_without_pyyaml(without_pyyaml: Any) -> None:
    inv = without_pyyaml.Inventory()
    inv.add("widget", 2)
    assert json.loads(without_pyyaml.dump_json(inv)) == {"widget": 2}


def test_yaml_export_raises_import_error_without_pyyaml(without_pyyaml: Any) -> None:
    inv = without_pyyaml.Inventory()
    inv.add("widget", 2)
    with pytest.raises(ImportError):
        without_pyyaml.dump_yaml(inv)


def test_pyyaml_is_no_longer_a_required_dependency() -> None:
    required = names(project_table().get("dependencies", []))
    assert not [item for item in required if item.startswith("pyyaml")]


def test_pyyaml_is_listed_in_the_yaml_extra() -> None:
    extras = project_table().get("optional-dependencies", {})
    assert [item for item in names(extras.get("yaml", [])) if item.startswith("pyyaml")]
