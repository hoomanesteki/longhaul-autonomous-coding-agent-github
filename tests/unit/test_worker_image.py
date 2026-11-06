"""The worker image and the setup steps in the runbook match what the example config asks for.

The example config turns memory on with the local embedder (LH-604), and a worker with that
config stops at its start unless fastembed is installed. The image runs on that config by
default, so it has to carry the extra, and the model too: a worker that has to download 66 MB
before its first search needs the network at a bad moment and takes whatever revision of the
model the hub serves that day (ADR-0010).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from longhaul.memory.embedder import LOCAL_MODEL

pytestmark = [pytest.mark.unit, pytest.mark.phase6]

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "docker" / "worker" / "Dockerfile"
EXAMPLE = ROOT / "config" / "longhaul.example.yaml"
RUNBOOK = ROOT / "docs" / "runbook.md"
EXTRA = "--extra memory"


def instructions(dockerfile: Path) -> list[str]:
    """The instructions of a Dockerfile, with continued lines joined and comments dropped."""
    joined = re.sub(r"\\\n", " ", dockerfile.read_text(encoding="utf-8"))
    lines = (line.strip() for line in joined.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def example_needs_the_extra() -> bool:
    memory = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["memory"]
    return bool(memory["enabled"]) and memory["embedder"] == "local"


def test_every_uv_sync_of_the_image_installs_the_memory_extra() -> None:
    # A sync without the extra removes what an earlier one installed, so one missing flag is
    # enough to take fastembed out of the final image.
    assert example_needs_the_extra(), "this test follows the example config"
    syncs = [line for line in instructions(DOCKERFILE) if "uv sync" in line]
    assert len(syncs) >= 2
    assert all(EXTRA in line for line in syncs), syncs


def test_the_image_carries_the_model_in_a_folder_that_stays() -> None:
    lines = instructions(DOCKERFILE)
    paths = [
        line.split("FASTEMBED_CACHE_PATH=", 1)[1].split()[0].strip('"')
        for line in lines
        if line.startswith("ENV") and "FASTEMBED_CACHE_PATH=" in line
    ]
    assert len(paths) == 1, "the image sets FASTEMBED_CACHE_PATH once"
    cache = paths[0]
    # The system temp directory is what fastembed falls back to, and a reboot may clear it.
    assert cache.startswith("/") and not cache.startswith("/tmp")
    warm_up = [i for i, line in enumerate(lines) if line.startswith("RUN") and LOCAL_MODEL in line]
    assert len(warm_up) == 1, "one build step loads the model into the cache"
    cache_set = next(i for i, line in enumerate(lines) if "FASTEMBED_CACHE_PATH=" in line)
    last_sync = max(i for i, line in enumerate(lines) if "uv sync" in line)
    first_sync = min(i for i, line in enumerate(lines) if "uv sync" in line)
    # After fastembed is installed and the path is set, and before the source is copied, so that
    # a change to the source does not download the model again.
    assert cache_set < warm_up[0]
    assert first_sync < warm_up[0] < last_sync


def test_the_durable_stack_steps_of_the_runbook_install_the_memory_extra() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index("## Run tasks on the durable stack")
    end = text.index("\n## ", start + 1)
    section = text[start:end]
    assert example_needs_the_extra(), "this test follows the example config"
    assert "uv sync --extra memory" in section
    assert section.index("uv sync --extra memory") < section.index("uv run longhaul worker")
