"""ADR-0002 lists the key Python packages with the version uv.lock holds.

Dependabot bumps change pyproject.toml and uv.lock but not the ADR, so a table row can fall
behind without anyone noticing. These tests compare each row with the lock file. The
Terraform rows are checked in test_deploy_aws.py and the container images are not in uv.lock.
"""

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
ADR = ROOT / "docs" / "adr" / "0002-pin-dependencies.md"
LOCK = ROOT / "uv.lock"

# A row of a two-column table, such as `| sqlalchemy | 2.1.8 |`.
_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$")


def python_pins(adr: str) -> dict[str, str]:
    """The package version of each Python row, keyed by package name.

    Only the tables before the container images section list Python packages. A row may name
    several packages that share a version, and the version may be followed by a note.
    """
    pins: dict[str, str] = {}
    for line in adr.split("### Container images")[0].splitlines():
        found = _ROW.match(line.strip())
        if not found or found.group(1) == "Package" or set(found.group(1)) <= {"-"}:
            continue
        version = found.group(2).split()[0]
        for name in found.group(1).split(","):
            pins[name.strip()] = version
    return pins


def locked_versions(lock: str) -> dict[str, set[str]]:
    """Every version uv.lock holds for each package name."""
    versions: dict[str, set[str]] = {}
    for package in tomllib.loads(lock)["package"]:
        if "version" in package:
            versions.setdefault(package["name"], set()).add(package["version"])
    return versions


def stale_rows(pins: dict[str, str], locked: dict[str, set[str]]) -> list[str]:
    """The rows whose version the lock file does not hold, as `name: listed, locked`."""
    return [
        f"{name}: ADR lists {version}, uv.lock has {sorted(locked.get(name, set()))}"
        for name, version in sorted(pins.items())
        if version not in locked.get(name, set())
    ]


def test_the_row_reader_splits_names_and_drops_notes() -> None:
    adr = (
        "| Package | Pinned version |\n"
        "|---|---|\n"
        "| sqlalchemy | 2.1.8 |\n"
        "| opentelemetry-api, opentelemetry-sdk | 1.45.2 (runtime since LH-703) |\n"
        "### Container images\n"
        "| postgres | pgvector/pgvector:0.8.0-pg17 |\n"
    )
    assert python_pins(adr) == {
        "sqlalchemy": "2.1.8",
        "opentelemetry-api": "1.45.2",
        "opentelemetry-sdk": "1.45.2",
    }


def test_the_pin_check_names_a_row_behind_the_lock_file() -> None:
    locked = {"sqlalchemy": {"2.1.8"}}
    assert stale_rows({"sqlalchemy": "2.1.6"}, locked) == [
        "sqlalchemy: ADR lists 2.1.6, uv.lock has ['2.1.8']"
    ]
    assert stale_rows({"sqlalchemy": "2.1.8"}, locked) == []


def test_every_python_row_of_adr_0002_matches_uv_lock() -> None:
    pins = python_pins(ADR.read_text(encoding="utf-8"))
    # The table is long; a reader that matched nothing would pass without checking anything.
    assert {"temporalio", "sqlalchemy", "types-docker"} <= set(pins)
    assert stale_rows(pins, locked_versions(LOCK.read_text(encoding="utf-8"))) == []
