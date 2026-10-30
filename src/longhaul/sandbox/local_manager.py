"""In-process fake sandbox for unit tests. Never used outside tests."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from longhaul.contracts import ExecResult
from longhaul.sandbox.common import WORKDIR, PathEscapeError, resolve_path

# "/workspace" as a whole path word: not the start of /workspace-old or the end of /a/workspace.
_WORKDIR_WORD = re.compile(r"(?<![\w./-])" + re.escape(WORKDIR) + r"(?![\w.-])")


class FakeSandbox:
    """A temp directory that stands in for the container's /workspace.

    Commands run on the host with that directory as their working directory. The
    text of each command and of its output is rewritten so that, as in Docker,
    the directory is called /workspace: `pwd`, `realpath` and `cat /workspace/x`
    agree with read_file and write_file. Paths written inside files are not rewritten.
    """

    workdir = WORKDIR

    def __init__(self, sandbox_id: str = "fake") -> None:
        self.id = sandbox_id
        self.root = Path(tempfile.mkdtemp(prefix="lh-fake-"))

    def _path(self, path: str) -> Path:
        # The same rules as the Docker sandbox: paths are normalised first, and only
        # the workspace can be reached. Links that lead out of it are refused too.
        full = resolve_path(path)
        if full != WORKDIR and not full.startswith(WORKDIR + "/"):
            raise PathEscapeError(f"path is outside {WORKDIR}: {path}")
        root = self.root.resolve()
        target = (root / full[len(WORKDIR) :].lstrip("/")).resolve()
        if not target.is_relative_to(root):
            raise PathEscapeError(f"path leaves {WORKDIR} through a link: {path}")
        return target

    def _to_host(self, text: str) -> str:
        real = str(self.root.resolve())
        return _WORKDIR_WORD.sub(lambda _: real, text)

    def _to_workdir(self, text: str) -> str:
        # Longest first: on macOS the resolved path (/private/var/...) contains the other.
        for real in (str(self.root.resolve()), str(self.root)):
            text = text.replace(real, WORKDIR)
        return text

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        # Same rule as the Docker sandbox: the workspace venv leads PATH.
        venv_bin = str(self.root.resolve() / ".venv" / "bin")
        path = os.pathsep.join(filter(None, [venv_bin, os.environ.get("PATH")]))
        # No bytecode, as in Docker: an edit that keeps the byte size and lands in
        # the same second as the last import would otherwise run the old code.
        full_env = {"PATH": path, "HOME": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"}
        full_env.update({k: self._to_host(v) for k, v in (env or {}).items()})
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            self._to_host(cmd),
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
            stdout=self._to_workdir(out.decode("utf-8", errors="replace")),
            stderr=self._to_workdir(err.decode("utf-8", errors="replace")),
            timed_out=timed_out,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    async def read_file(self, path: str) -> bytes:
        return self._path(path).read_bytes()

    async def write_file(self, path: str, data: bytes) -> None:
        target = self._path(path)
        # The same refusals as the Docker sandbox, with the same error types.
        if target.is_dir():
            raise IsADirectoryError(errno.EISDIR, "is a directory", path)
        root = self.root.resolve()
        for ancestor in target.parents:
            if ancestor == root:
                break
            if ancestor.exists() and not ancestor.is_dir():
                raise NotADirectoryError(errno.ENOTDIR, "not a directory", path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def destroy(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
