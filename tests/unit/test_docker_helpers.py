"""Pure helpers of the Docker sandbox. Nothing here talks to Docker."""

import io
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from longhaul.contracts import SandboxLimits
from longhaul.sandbox import docker_manager, supervisor
from longhaul.sandbox.common import OUTPUT_CAP, OUTPUT_HEAD, OUTPUT_TAIL, BoundedOutput
from longhaul.sandbox.docker_manager import (
    AGENT_UID,
    ENV_ALLOWLIST,
    SUPERVISOR_SOURCE,
    DockerSandbox,
    DockerSandboxManager,
    build_tar,
    exec_argv,
    merge_env,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase1]


def members(raw: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        return tar.getmembers()


# ---------- build_tar ----------


def test_build_tar_stamps_files_and_directories_with_the_current_time() -> None:
    before = int(time.time())
    raw = build_tar("d/f.txt", b"x")
    after = time.time()
    found = members(raw)
    assert [m.name for m in found] == ["d", "d/f.txt"]
    # Python trusts a .pyc when the source mtime and size match, and mtime 0 never changes.
    assert all(before <= m.mtime <= after for m in found)


def test_build_tar_uses_the_given_file_mode_and_keeps_directories_at_755() -> None:
    d, f = members(build_tar("d/run.sh", b"#!/bin/sh\n", mode=0o755))
    assert (d.mode, f.mode) == (0o755, 0o755)
    d, f = members(build_tar("d/plain.txt", b"x"))
    assert (d.mode, f.mode) == (0o755, 0o644)


def test_build_tar_without_parents_holds_just_the_file() -> None:
    (only,) = members(build_tar("f.txt", b"hey"))
    assert only.name == "f.txt" and only.isfile() and only.size == 3
    assert only.uid == only.gid == AGENT_UID


# ---------- BoundedOutput ----------


def test_the_default_bounds_are_the_one_megabyte_cap_from_the_story() -> None:
    assert OUTPUT_HEAD + OUTPUT_TAIL == OUTPUT_CAP == 1_000_000


def test_a_short_stream_comes_back_whole() -> None:
    out = BoundedOutput()
    assert out.text() == ""
    out.feed(b"abc")
    out.feed(b"def\n")
    assert out.text() == "abcdef\n"


def test_a_long_stream_keeps_its_start_and_its_end() -> None:
    out = BoundedOutput(head=10, tail=10)
    for chunk in (b"0123456789", b"abcdefghij", b"KLMNOPQRST", b"uvwxyz"):
        out.feed(chunk)
    text = out.text()
    assert text.startswith("0123456789\n[... 16 bytes omitted ...]\n")
    assert text.endswith("QRSTuvwxyz")
    assert out.total == 36


def test_where_the_stream_is_cut_does_not_depend_on_how_it_was_chunked() -> None:
    data = bytes(range(256)) * 40
    whole = BoundedOutput(head=100, tail=300)
    whole.feed(data)
    for size in (1, 7, 99, 100, 101, 299, 300, 301, 5000):
        pieces = BoundedOutput(head=100, tail=300)
        for i in range(0, len(data), size):
            pieces.feed(data[i : i + size])
        assert pieces.text() == whole.text(), size


def test_memory_stays_near_the_bounds_however_much_is_printed() -> None:
    out = BoundedOutput(head=100, tail=100)
    for _ in range(5000):
        out.feed(b"x" * 1000)  # 5 MB in all
    assert out.total == 5_000_000
    assert len(out._head) == 100 and len(out._tail) <= 300
    text = out.text()
    assert len(text) < 300 and "4999800 bytes omitted" in text


def test_text_that_is_not_cut_decodes_across_chunk_boundaries() -> None:
    sample = "héllo wörld ✓ naïve"
    out = BoundedOutput()
    for byte in sample.encode():  # one byte at a time splits every multi-byte character
        out.feed(bytes([byte]))
    assert out.text() == sample


def test_bytes_that_are_not_utf8_are_replaced_not_raised() -> None:
    out = BoundedOutput()
    out.feed(b"ok \xff\xfe done")
    assert out.text() == "ok " + "\N{REPLACEMENT CHARACTER}" * 2 + " done"


# ---------- exec_argv ----------


def test_exec_argv_calls_every_program_by_absolute_path() -> None:
    # /workspace/.venv/bin leads PATH and the agent can write there, so a file named
    # timeout or bash must not be picked up in place of the real one.
    argv = exec_argv("echo hi", 30)
    assert argv[0] == "/usr/local/bin/python3"
    assert argv[1:4] == ["-I", "-S", "-c"]  # isolated: no PYTHONPATH, no cwd on sys.path
    assert argv[4] == SUPERVISOR_SOURCE
    assert argv[5:] == ["/usr/bin/timeout", "--signal=KILL", "30", "/bin/bash", "-c", "echo hi"]


@pytest.mark.parametrize("bad", [0, -1, -30])
def test_exec_argv_rejects_a_timeout_that_is_not_positive(bad: int) -> None:
    # GNU timeout reads 0 as "no limit", which would switch the F7 guarantee off.
    with pytest.raises(ValueError, match="timeout_s"):
        exec_argv("true", bad)


def test_the_supervisor_sent_to_the_container_is_the_module_text() -> None:
    assert Path(supervisor.__file__).read_text(encoding="utf-8") == SUPERVISOR_SOURCE
    compile(SUPERVISOR_SOURCE, "supervisor.py", "exec")


# ---------- DockerSandbox.exec against a stand-in for the low-level API ----------


class FakeStream:
    """What exec_start(stream=True, demux=True) returns: (stdout, stderr) pairs."""

    def __init__(self, frames: list[tuple[bytes | None, bytes | None]], fail: bool = False) -> None:
        self.frames = frames
        self.fail = fail
        self.closed = False

    def __iter__(self) -> Any:
        yield from self.frames
        if self.fail:
            raise RuntimeError("the connection broke")

    def close(self) -> None:
        self.closed = True


class FakeApi:
    def __init__(self, stream: FakeStream, statuses: list[dict[str, Any]]) -> None:
        self.stream = stream
        self.statuses = statuses
        self.created: list[tuple[str, list[str], dict[str, Any]]] = []
        self.started: list[tuple[str, bool, bool]] = []
        self.inspected = 0

    def exec_create(self, container: str, cmd: list[str], **kwargs: Any) -> dict[str, str]:
        self.created.append((container, cmd, kwargs))
        return {"Id": "exec-1"}

    def exec_start(self, exec_id: str, *, stream: bool, demux: bool) -> FakeStream:
        self.started.append((exec_id, stream, demux))
        return self.stream

    def exec_inspect(self, exec_id: str) -> dict[str, Any]:
        self.inspected += 1
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


def sandbox_with(api: FakeApi) -> DockerSandbox:
    return DockerSandbox(SimpleNamespace(api=api), "c-1")  # type: ignore[arg-type]


DONE = {"Running": False, "ExitCode": 0}


async def test_exec_starts_a_streaming_exec_with_the_supervisor_and_the_allowlist() -> None:
    api = FakeApi(FakeStream([(b"hi\n", None)]), [DONE])
    r = await sandbox_with(api).exec("echo hi", timeout_s=30, env={"X": "1"})
    assert (r.exit_code, r.stdout, r.stderr, r.timed_out) == (0, "hi\n", "", False)
    ((container, argv, kwargs),) = api.created
    assert container == "c-1" and argv == exec_argv("echo hi", 30)
    assert kwargs == {
        "workdir": "/workspace",
        "user": "agent",
        "environment": merge_env({"X": "1"}),
    }
    assert api.started == [("exec-1", True, True)]
    assert api.stream.closed


async def test_exec_keeps_only_the_head_and_the_tail_of_each_stream() -> None:
    frames: list[tuple[bytes | None, bytes | None]] = [(b"HEAD\n", None), (None, b"ERR-HEAD\n")]
    frames += [(b"a" * 65536, None)] * 120  # 7.8 MB on stdout
    frames += [(b"\nTAIL\n", b"\nERR-TAIL\n")]
    api = FakeApi(FakeStream(frames), [DONE])
    r = await sandbox_with(api).exec("noisy", timeout_s=30)
    assert r.stdout.startswith("HEAD\n") and r.stdout.endswith("\nTAIL\n")
    assert r.stderr == "ERR-HEAD\n\nERR-TAIL\n"  # short enough to come back whole
    assert "bytes omitted" in r.stdout
    assert OUTPUT_CAP < len(r.stdout) < OUTPUT_CAP + 100


async def test_exec_refuses_a_timeout_that_is_not_positive_before_it_calls_docker() -> None:
    api = FakeApi(FakeStream([]), [DONE])
    for bad in (0, -1):
        with pytest.raises(ValueError, match="timeout_s"):
            await sandbox_with(api).exec("true", timeout_s=bad)
    assert api.created == [] and api.started == []


async def test_exec_waits_until_the_daemon_has_recorded_the_exit_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The output stream can end before the exit status is stored: it reads as None.
    monkeypatch.setattr(docker_manager, "EXIT_POLL_S", 0)
    statuses = [
        {"Running": True, "ExitCode": None},
        {"Running": False, "ExitCode": None},
        {"Running": False, "ExitCode": 3},
    ]
    api = FakeApi(FakeStream([]), statuses)
    r = await sandbox_with(api).exec("exit 3", timeout_s=30)
    assert r.exit_code == 3 and api.inspected == 3


async def test_exec_fails_loudly_when_no_exit_status_ever_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_manager, "EXIT_POLL_S", 0)
    monkeypatch.setattr(docker_manager, "EXIT_POLLS", 4)
    api = FakeApi(FakeStream([]), [{"Running": True, "ExitCode": None}])
    with pytest.raises(RuntimeError, match="exit status"):
        await sandbox_with(api).exec("true", timeout_s=30)
    assert api.inspected == 4


async def test_exec_closes_the_stream_when_reading_it_fails() -> None:
    api = FakeApi(FakeStream([(b"partial", None)], fail=True), [DONE])
    with pytest.raises(RuntimeError, match="connection broke"):
        await sandbox_with(api).exec("true", timeout_s=30)
    assert api.stream.closed


async def test_a_kill_signal_before_the_deadline_is_not_a_timeout() -> None:
    api = FakeApi(FakeStream([]), [{"Running": False, "ExitCode": 137}])
    r = await sandbox_with(api).exec("kill -9 $$", timeout_s=30)
    assert (r.exit_code, r.timed_out) == (137, False)


async def test_a_kill_signal_after_the_deadline_is_a_timeout() -> None:
    class Slow(FakeStream):
        def __iter__(self) -> Any:
            time.sleep(1.05)  # the command "ran" past its 1 second limit
            return super().__iter__()

    api = FakeApi(Slow([]), [{"Running": False, "ExitCode": 137}])
    r = await sandbox_with(api).exec("sleep 30", timeout_s=1)
    assert (r.exit_code, r.timed_out) == (137, True)


# ---------- DockerSandboxManager.create against a stand-in for the client ----------


class FakeClient:
    def __init__(self) -> None:
        self.runs: list[tuple[str, dict[str, Any]]] = []
        self.detached: list[object] = []
        self.containers = SimpleNamespace(run=self._run)
        bridge = SimpleNamespace(disconnect=self.detached.append)
        self.networks = SimpleNamespace(get=lambda name: bridge if name == "bridge" else None)

    def _run(self, image: str, **kwargs: Any) -> SimpleNamespace:
        self.runs.append((image, kwargs))
        return SimpleNamespace(id="c-1")


async def create(client: FakeClient, network: bool) -> DockerSandbox:
    manager = DockerSandboxManager(client)  # type: ignore[arg-type]
    box = await manager.create("t1", "img:tag", SandboxLimits(), network=network)
    assert isinstance(box, DockerSandbox)
    return box


async def test_create_runs_an_init_so_orphans_are_reaped() -> None:
    client = FakeClient()
    await create(client, network=True)
    ((image, kwargs),) = client.runs
    assert image == "img:tag" and kwargs["init"] is True
    # No command either: the image's CMD keeps the container idle.
    assert "command" not in kwargs and "entrypoint" not in kwargs


async def test_create_keeps_the_hosts_docker_proxy_settings_out_of_the_container() -> None:
    # docker-py copies the proxies block of ~/.docker/config.json into the env of every
    # container it creates unless told not to. That block can hold credentials (I3).
    client = FakeClient()
    await create(client, network=True)
    ((_, kwargs),) = client.runs
    assert kwargs["use_config_proxy"] is False
    assert kwargs["environment"] == dict(ENV_ALLOWLIST)


@pytest.mark.parametrize(("network", "detached"), [(False, 1), (True, 0)])
async def test_create_leaves_the_bridge_only_when_the_network_is_off(
    network: bool, detached: int
) -> None:
    client = FakeClient()
    await create(client, network=network)
    assert len(client.detached) == detached
    ((_, kwargs),) = client.runs
    assert kwargs["network_mode"] == "bridge"  # a `none` container could never get one later
