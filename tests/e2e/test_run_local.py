"""The first end-to-end run: the `longhaul` command on the calc fixture, scripted, on real Docker.

Run `make sandbox-image` first. The workspace setup builds a venv and installs pytest and calc
from PyPI, so the machine needs outbound network, as the integration tests do.

The command is started the way a user starts it, as the `longhaul` script of the project's
virtualenv, so the `[project.scripts]` entry is tested as well. The result is not compared with
REFERENCE.patch as text, because two correct patches can differ in their context lines. The
printed diff and the reference patch are applied to two fresh copies of calc at base instead.
The copies must end up with the same files, and the calc tests must pass on the patched copy.

A second test stops a run with SIGTERM, which a plain process would not survive, and checks
that the container and the volume are gone.
"""

import importlib.util
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import docker
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.phase1]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
SCRIPT = ROOT / "tests" / "fixtures" / "scripts" / "calc_fix.yaml"
IMAGE = "longhaul-sandbox:py312"
# `uv run` and `uv sync` put the script next to the interpreter.
LONGHAUL = Path(sys.executable).with_name("longhaul")
DIFF_MARKER = "final diff against base:"

_spec = importlib.util.spec_from_file_location(
    "make_fixture_git", ROOT / "scripts" / "make_fixture_git.py"
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
make_fixture_git = _module.make_fixture_git
isolated_git_env = _module.isolated_git_env


def require_image() -> None:
    inspect = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, check=False
    )
    if inspect.returncode != 0:
        pytest.fail(f"image {IMAGE} is missing; run make sandbox-image first")


def cli_environment() -> dict[str, str]:
    """The machine's environment without any longhaul setting or model key.

    CI has no keys, and a scripted run must not need one. A LONGHAUL_CONFIG left in the
    shell would also change what the run does.
    """
    drop = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"}
    env = {k: v for k, v in os.environ.items() if not k.startswith("LONGHAUL_") and k not in drop}
    # The console script finds the package through the editable install's .pth file, which
    # Python on macOS skips when the file carries the hidden flag. Name src explicitly.
    src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH")]))
    return env


def cli_command(script: Path, *extra: str) -> list[str]:
    if not LONGHAUL.exists():
        pytest.fail(f"{LONGHAUL} is missing; run uv sync, which installs the [project.scripts]")
    return [
        str(LONGHAUL),
        "run-local",
        "--path",
        str(CALC),
        "--issue-file",
        str(CALC / "ISSUE.md"),
        "--provider",
        "scripted",
        "--script",
        str(script),
        *extra,
    ]


def run_cli(cwd: Path) -> subprocess.CompletedProcess[str]:
    # Another working directory than the repo root, to show that the config is found anyway.
    return subprocess.run(
        cli_command(SCRIPT),
        cwd=cwd,
        env=cli_environment(),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
    }


def git(repo: Path, *args: str) -> None:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=isolated_git_env(),
        check=False,
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"


def files_of(repo: Path) -> dict[str, bytes]:
    return {
        p.relative_to(repo).as_posix(): p.read_bytes()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    }


def leftovers(task_id: str) -> tuple[list[str], list[str]]:
    client = docker.from_env()
    containers = client.containers.list(all=True, filters={"label": f"longhaul.task_id={task_id}"})
    volumes = client.volumes.list(filters={"name": f"lh-ws-{task_id}"})
    return [str(c.name) for c in containers], [str(v.name) for v in volumes]


def remove_sandbox(task_id: str) -> None:
    """Clean up after a test that failed before the command could."""
    client = docker.from_env()
    for container in client.containers.list(
        all=True, filters={"label": f"longhaul.task_id={task_id}"}
    ):
        container.remove(force=True)
    for volume in client.volumes.list(filters={"name": f"lh-ws-{task_id}"}):
        volume.remove(force=True)


def wait_for(path: Path, text: str, proc: "subprocess.Popen[str]", seconds: float = 300) -> None:
    """Wait until the file holds `text`, and fail early if the process has ended."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if text in path.read_text(encoding="utf-8"):
            return
        assert proc.poll() is None, f"the command ended first:\n{path.read_text(encoding='utf-8')}"
        time.sleep(0.5)
    pytest.fail(f"{text!r} did not appear in {seconds:.0f} s")


def test_calc_scripted(tmp_path: Path) -> None:
    require_image()
    before = snapshot(CALC)

    done = run_cli(tmp_path)

    shown = f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
    assert done.returncode == 0, shown
    assert "Traceback" not in done.stderr, shown

    # Each turn is one compact line, in the order the script plays them.
    turns = re.findall(r"^turn (\d+) coder: (\w+)(?: (.*))?$", done.stdout, re.M)
    assert [(n, name) for n, name, _ in turns] == [
        ("1", "list_dir"),
        ("2", "read_file"),
        ("3", "edit_file"),
        ("4", "run_tests"),
        ("5", "finish"),
    ], shown
    assert "turn 3 coder: edit_file src/calc/__init__.py" in done.stdout.splitlines()
    assert re.search(r"^finished after 5 turns: Changed add\(\)", done.stdout, re.M), shown

    # The printed diff is the fix, and nothing else: not the venv, not the egg-info.
    diff = done.stdout.split(f"{DIFF_MARKER}\n", 1)[1]
    assert re.findall(r"^diff --git .*$", diff, re.M) == [
        "diff --git a/src/calc/__init__.py b/src/calc/__init__.py"
    ], shown
    patch = tmp_path / "printed.patch"
    patch.write_text(diff.rstrip("\n") + "\n", encoding="utf-8")

    # Applied to calc at base, it gives the same files as the reference patch, and the tests pass.
    fixed = make_fixture_git(CALC, tmp_path / "fixed")
    reference = make_fixture_git(CALC, tmp_path / "reference")
    git(fixed, "apply", str(patch))
    git(reference, "apply", str(CALC / "REFERENCE.patch"))
    assert files_of(fixed) == files_of(reference)
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=fixed,
        env={**os.environ, "PYTHONPATH": str(fixed / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert tests.returncode == 0, tests.stdout
    assert "3 passed" in tests.stdout

    # The source folder was only read, and the sandbox is gone.
    assert snapshot(CALC) == before
    found = re.search(r"^task (lh-local-calc-[0-9a-f]{8}):", done.stdout, re.M)
    assert found, shown
    assert leftovers(found.group(1)) == ([], [])


def test_a_terminated_run_still_removes_its_sandbox(tmp_path: Path) -> None:
    require_image()
    stdout, stderr = tmp_path / "stdout.txt", tmp_path / "stderr.txt"
    # A script that never finishes, so the run is still going when the signal arrives.
    never = ROOT / "tests" / "fixtures" / "scripts" / "loop_never_finishes.yaml"
    with stdout.open("w") as out, stderr.open("w") as err:
        proc = subprocess.Popen(
            cli_command(never, "--max-turns", "100000"),
            cwd=tmp_path,
            env=cli_environment(),
            stdout=out,
            stderr=err,
            text=True,
        )
    task_id = None
    try:
        wait_for(stdout, "\nturn 3 ", proc)
        found = re.search(r"^task (lh-local-calc-[0-9a-f]{8}):", stdout.read_text(), re.M)
        assert found
        task_id = found.group(1)
        assert leftovers(task_id)[0], "the sandbox should be running by now"

        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=120)

        assert code == 2, stderr.read_text()
        assert "interrupted" in stderr.read_text()
        assert leftovers(task_id) == ([], [])
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if task_id is not None:
            remove_sandbox(task_id)
