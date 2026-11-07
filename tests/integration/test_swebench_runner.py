"""A SWE-bench task on the real stack, with a stand-in for the instance image (LH-902).

The images of the harness are several GB each and are built for x86_64, so these tests build a
small image of their own that has the same layout: the repo at /testbed with a commit, a conda
style environment at /opt/miniconda3/envs/testbed with the project installed in editable mode,
and git. Everything after that is the code under test: the image made from it
(eval/swebench/images.py), the copy of the repo, the sandbox image that the worker takes from the
folder, and the patch that goes to the harness.

Run `make up` and `make sandbox-image` first. The stand-in installs pytest and ripgrep from the
network, so the machine needs outbound network. At the end the tests remove the images they
built, the containers and volumes of their tasks, and nothing else.
"""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
import textwrap
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import docker
import pytest
from docker.errors import ImageNotFound
from tests.integration.test_eval_runner import (  # noqa: F401  (fixtures)
    EvalStack,
    database,
    remove_leftovers,
    require_image,
    stack,
)

from eval import results, run
from eval.swebench import adapter, images
from longhaul.contracts import SandboxLimits
from longhaul.sandbox.docker_manager import DockerSandboxManager

pytestmark = [pytest.mark.integration, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "tests" / "fixtures" / "scripts" / "swebench"
INSTANCE_ID = "standin__calc-1"
LIMITS = SandboxLimits(cpus=1.0, memory_mb=1024, pids=256)
BUILD_TIMEOUT_S = 900

BUGGY = (
    '"""A tiny package."""\n\nfrom calc import _generated  # noqa: F401\n\n\n'
    "def add(a, b):\n    return a - b\n"
)
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
PROJECT = (
    '[project]\nname = "calc"\nversion = "0.1"\n\n'
    '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n\n'
    '[tool.setuptools]\npackages = ["calc"]\n'
)
DOCKERFILE = """\
FROM python:3.12.15-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/miniconda3/envs && python -m venv /opt/miniconda3/envs/testbed
COPY repo /testbed
RUN /opt/miniconda3/envs/testbed/bin/pip install --no-cache-dir -q pytest \\
    && /opt/miniconda3/envs/testbed/bin/pip install --no-cache-dir -q -e /testbed
# The clone also holds a later commit, as the clone of a harness image does. The agent must not
# be able to read it. The install leaves a module that git neither tracks nor ignores, which
# calc imports, and it changes a tracked file.
RUN cd /testbed && git init -q -b main && git add -A \\
    && git -c user.name=setup -c user.email=setup@localhost commit -q -m base \\
    && git checkout -q -b upstream-fix \\
    && echo "the upstream fix" > UPSTREAM_FIX.txt && git add -A \\
    && git -c user.name=setup -c user.email=setup@localhost commit -q -m "the upstream fix" \\
    && git checkout -q main \\
    && echo "VALUE = 1" > calc/_generated.py \\
    && echo "# touched by the install" >> pyproject.toml
# The harness images start a shell, which ends at once without a terminal.
CMD ["bash"]
"""
# What the instance's test patch does to the test file, in the form of the dataset.
TEST_PATCH = textwrap.dedent(
    """\
    diff --git a/tests/test_calc.py b/tests/test_calc.py
    --- a/tests/test_calc.py
    +++ b/tests/test_calc.py
    @@ -1,5 +1,8 @@
     from calc import add
     
     
    +def test_add_negative():
    +    assert add(-1, 1) == 0
    +
    +
     def test_add():
         assert add(2, 3) == 5
    """
)


def host_platform() -> str:
    done = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


@dataclass
class StandIn:
    instance: adapter.Instance
    image: str


@pytest.fixture(scope="module")
def standin(tmp_path_factory: pytest.TempPathFactory) -> Iterator[StandIn]:
    """An image laid out like the one of a harness instance, and the dataset row for it."""
    require_image()
    context = tmp_path_factory.mktemp("standin")
    repo = context / "repo"
    (repo / "calc").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "calc" / "__init__.py").write_text(BUGGY, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(TEST, encoding="utf-8")
    (repo / "pyproject.toml").write_text(PROJECT, encoding="utf-8")
    (context / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    tag = f"lh-standin-{uuid.uuid4().hex[:8]}:latest"
    subprocess.run(
        ["docker", "build", "--quiet", "--tag", tag, str(context)],
        check=True,
        capture_output=True,
        timeout=BUILD_TIMEOUT_S,
    )
    head = subprocess.run(
        ["docker", "run", "--rm", tag, "git", "-C", "/testbed", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    row = {
        "instance_id": INSTANCE_ID,
        "repo": "standin/calc",
        "base_commit": head,
        "problem_statement": "add() subtracts\nadd(2, 3) returns -1 and must return 5.",
        "test_patch": TEST_PATCH,
        "patch": "",
        "image": tag,
        "eval_script": "\n".join(
            [
                "#!/bin/bash",
                ": '>>>>> Start Test Output'",
                "python -m pytest -q tests/test_calc.py",
                ": '>>>>> End Test Output'",
            ]
        ),
    }
    built: list[str] = []
    try:
        yield StandIn(adapter.instance_from_row(row), tag)
    finally:
        client = docker.from_env()
        for image in client.images.list(name=images.TAG_REPOSITORY):
            built += [t for t in image.tags if INSTANCE_ID in t]
        for name in [tag, *built]:
            with contextlib.suppress(ImageNotFound):
                client.images.remove(name, force=True)


@pytest.fixture(autouse=True)
def stand_in_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stand-in is built for this machine, not for x86_64 like the images of the harness."""
    monkeypatch.setenv(images.PLATFORM_VARIABLE, host_platform())


async def sh(box: object, script: str) -> str:
    result = await box.exec(script, timeout_s=120)  # type: ignore[attr-defined]
    assert result.exit_code == 0, f"{script}\n{result.stdout}\n{result.stderr}"
    return str(result.stdout).strip()


# ---------- the sandbox made from an instance image ----------
async def test_the_image_made_from_an_instance_works_as_a_sandbox(
    standin: StandIn, tmp_path: Path
) -> None:
    folder = tmp_path / "repo"
    folder.mkdir()
    tag = images.DockerImages().prepare(standin.instance, folder)
    # The working tree of the image is on the host, ready to become the local repo of a task:
    # the tracked files as the image has them, and the module its install made.
    assert (folder / "calc" / "__init__.py").read_text(encoding="utf-8") == BUGGY
    assert (folder / "calc" / "_generated.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert "touched by the install" in (folder / "pyproject.toml").read_text(encoding="utf-8")
    assert not (folder / ".git").exists()
    assert not (folder / "UPSTREAM_FIX.txt").exists()

    manager = DockerSandboxManager()
    task_id = f"lh-eval-sb-{uuid.uuid4().hex[:8]}"
    box = await manager.create(task_id, tag, LIMITS, network=False)
    try:
        assert await sh(box, "id -u") == "10001"
        # The task volume starts with the repo of the image, and the user can write in it. The
        # history is gone, so the commit after the base commit cannot be read.
        assert await sh(box, "cat calc/__init__.py") == BUGGY.strip()
        assert await sh(box, "test ! -e .git && echo no-history") == "no-history"
        await box.write_file("calc/__init__.py", BUGGY.replace("a - b", "a + b").encode())
        # The conda-style environment leads PATH, and the editable install resolves through
        # /testbed to the same files, even from another directory.
        assert await sh(box, "which python") == "/workspace/.venv/bin/python"
        probe = "cd /tmp && python -c 'import calc; print(calc.__file__, calc.add(2, 3))'"
        assert await sh(box, probe) == "/testbed/calc/__init__.py 5"
        assert await sh(box, "readlink /testbed") == "/workspace"
        assert (await sh(box, "rg --version")).startswith("ripgrep")
        assert "passed" in await sh(box, "python -m pytest -q tests/test_calc.py")
    finally:
        await manager.cleanup_task(task_id)


# ---------- the runner, from the instance to the predictions ----------
async def test_a_swebench_task_runs_in_its_image_and_leaves_a_clean_patch(
    stack: EvalStack,  # noqa: F811
    standin: StandIn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "select_instances", lambda limit: [standin.instance])
    options = run.parse_args(
        [
            "--set",
            "swebench",
            "--config",
            "full",
            "--run-id",
            stack.run_id,
            "--results-dir",
            str(stack.results_dir),
            "--script",
            str(SCRIPTS),
        ]
    )

    summary = await run.run_eval(options, stack.settings)

    folder = stack.results_dir / stack.run_id
    assert sorted(p.name for p in folder.iterdir()) == [
        "predictions.jsonl",
        f"{INSTANCE_ID}.json",
        f"{INSTANCE_ID}.patch",
        "summary.json",
    ]
    (result,) = results.read_results(folder)
    assert (result.task_set, result.outcome) == ("swebench", "pr_opened")
    # The harness grades the run later, so the file says so and does not guess.
    assert result.resolved is None
    assert "grade.sh" in result.grading
    assert (summary.attempted, summary.resolved, summary.ungraded) == (1, 0, 1)
    # The agent fixed the bug and edited the test file. The patch has the first and not the second.
    patch = (folder / f"{INSTANCE_ID}.patch").read_text(encoding="utf-8")
    assert "+    return a + b" in patch
    assert "tests/test_calc.py" not in patch
    # What the install made is part of the base, so it is not in the diff, and it survived: the
    # tests import it.
    assert "calc/_generated.py" not in patch
    assert "pyproject.toml" not in patch
    (row,) = adapter.read_predictions(folder / "predictions.jsonl")
    assert row["instance_id"] == INSTANCE_ID
    assert row["model_patch"] == patch
    assert row["model_name_or_path"].startswith("longhaul-full-")
    # What the runner made for the task is gone: no work folder is left in the temp directory.
    assert not list(Path(tempfile.gettempdir()).glob(f"lh-{INSTANCE_ID}-*"))
