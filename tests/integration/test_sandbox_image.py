import subprocess

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.phase1]

IMAGE = "longhaul-sandbox:py312"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def _run_in_image(*argv: str) -> str:
    # The image's ENTRYPOINT is `sleep infinity`, so each probe overrides it.
    # Passing the command as plain args would hand it to sleep instead.
    result = _docker("run", "--rm", "--entrypoint", argv[0], IMAGE, *argv[1:])
    assert result.returncode == 0, f"{' '.join(argv)} failed: {result.stderr.strip()}"
    return result.stdout.strip()


def test_image_tools_present() -> None:
    inspect = _docker("image", "inspect", IMAGE)
    if inspect.returncode != 0:
        pytest.fail(f"image {IMAGE} is missing; run make sandbox-image first")

    assert _run_in_image("id", "-u") == "10001"
    assert _run_in_image("python", "--version").startswith("Python 3.12")
    assert _run_in_image("rg", "--version").startswith("ripgrep")
    assert _run_in_image("git", "--version").startswith("git version")
    assert _run_in_image("patch", "--version").startswith("GNU patch")
    assert _run_in_image("ps", "--version")
    assert _run_in_image("git", "config", "--system", "user.name") == "longhaul"
