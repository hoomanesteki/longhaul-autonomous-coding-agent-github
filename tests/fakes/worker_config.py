"""The config of a worker that a test starts as a process (LH-604).

The example config names the local embedder, which needs the memory extra and a model download.
CI has neither, and a test that kills and restarts workers must not wait for a model. So these
tests run on a copy of the example with the hash embedder: the worker still builds the memory
store and keeps the map and the commands in it, and finds notes by shared words.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "longhaul.example.yaml"


def hash_embedder_config(folder: Path) -> Path:
    """Write the example config to `folder` with memory.embedder set to hash. Returns the path.

    The pricing file is named by a path relative to the checkout, which a copy elsewhere would
    lose, so it is made absolute.
    """
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["memory"]["embedder"] = "hash"
    data["pricing_file"] = str(ROOT / data["pricing_file"])
    path = folder / "longhaul.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path
