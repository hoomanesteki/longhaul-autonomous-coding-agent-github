import json
import subprocess
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.phase1]

IMAGE = "longhaul-sandbox:py312"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def _run_in_image(*argv: str) -> str:
    # The image idles with CMD, so a command given to `docker run` replaces it and
    # runs as written, with no --entrypoint needed.
    result = _docker("run", "--rm", IMAGE, *argv)
    assert result.returncode == 0, f"{' '.join(argv)} failed: {result.stderr.strip()}"
    return result.stdout.strip()


def _config() -> dict[str, Any]:
    inspect = _docker("image", "inspect", IMAGE)
    if inspect.returncode != 0:
        pytest.fail(f"image {IMAGE} is missing; run make sandbox-image first")
    config: dict[str, Any] = json.loads(inspect.stdout)[0]["Config"]
    return config


def test_image_tools_present() -> None:
    _config()  # fails with a clear message when the image is missing

    assert _run_in_image("id", "-u") == "10001"
    assert _run_in_image("python", "--version").startswith("Python 3.12")
    assert _run_in_image("rg", "--version").startswith("ripgrep")
    assert _run_in_image("git", "--version").startswith("git version")
    assert _run_in_image("patch", "--version").startswith("GNU patch")
    assert _run_in_image("ps", "--version")
    assert _run_in_image("git", "config", "--system", "user.name") == "longhaul"


def test_the_acceptance_command_runs_as_written() -> None:
    # LH-106 says `docker run --rm longhaul-sandbox:py312 id -u` prints 10001. With
    # ENTRYPOINT ["sleep", "infinity"] that ran `sleep infinity id -u` and failed.
    result = _docker("run", "--rm", IMAGE, "id", "-u")
    assert (result.returncode, result.stdout.strip()) == (0, "10001"), result.stderr


def test_the_image_idles_with_cmd_so_the_manager_passes_no_command() -> None:
    config = _config()
    assert config.get("Entrypoint") is None  # inspect leaves the key out when it is null
    assert config["Cmd"] == ["sleep", "infinity"]
    assert config["User"] == "agent"


def test_the_image_does_not_write_bytecode() -> None:
    # An edit that keeps the byte size keeps the source mtime and size Python checks, so
    # a .pyc from before the edit would still be trusted.
    assert "PYTHONDONTWRITEBYTECODE=1" in _config()["Env"]
    assert _run_in_image("python", "-c", "import sys; print(sys.dont_write_bytecode)") == "True"
