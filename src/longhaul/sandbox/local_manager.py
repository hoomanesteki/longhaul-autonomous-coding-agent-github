"""In-process fake sandbox for unit tests. Never used outside tests."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import time
from pathlib import Path

from longhaul.contracts import ExecResult


class FakeSandbox:
    workdir = "/workspace"

    def __init__(self, sandbox_id: str = "fake") -> None:
        self.id = sandbox_id
        self.root = Path(tempfile.mkdtemp(prefix="lh-fake-"))

    def _path(self, path: str) -> Path:
        # Absolute and relative paths both resolve under the root, so a test
        # can never write outside the temp dir.
        rel = path.removeprefix(self.workdir).lstrip("/")
        target = (self.root / rel).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError(f"path escapes sandbox: {path}")
        return target

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        # Same rule as the Docker sandbox: the workspace venv leads PATH.
        venv_bin = str(self.root / ".venv" / "bin")
        path = os.pathsep.join(filter(None, [venv_bin, os.environ.get("PATH")]))
        full_env = {"PATH": path, "HOME": str(self.root)}
        full_env.update(env or {})
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            cmd,
            cwd=self.root,
            env=full_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, 9)
            out, err = await proc.communicate()
        return ExecResult(
            exit_code=137 if timed_out else (proc.returncode or 0),
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    async def read_file(self, path: str) -> bytes:
        return self._path(path).read_bytes()

    async def write_file(self, path: str, data: bytes) -> None:
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def destroy(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
