import importlib

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

PACKAGES = [
    "longhaul",
    "longhaul.providers",
    "longhaul.agent",
    "longhaul.tools",
    "longhaul.sandbox",
    "longhaul.checkpoints",
    "longhaul.blobs",
    "longhaul.context",
    "longhaul.roles",
    "longhaul.memory",
    "longhaul.cost",
    "longhaul.github",
    "longhaul.durable",
    "longhaul.durable.workflows",
    "longhaul.durable.activities",
    "longhaul.telemetry",
    "longhaul.api",
    "longhaul.db",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name: str) -> None:
    assert importlib.import_module(name) is not None
