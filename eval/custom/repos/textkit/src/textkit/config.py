"""Defaults for textkit, kept in a small TOML file."""

from pathlib import Path
from typing import Any

import tomli


def load_defaults(path: Path | str) -> dict[str, Any]:
    """Return the settings in the TOML file at path, or an empty dict when it is missing."""
    file = Path(path)
    if not file.exists():
        return {}
    with file.open("rb") as handle:
        return tomli.load(handle)
