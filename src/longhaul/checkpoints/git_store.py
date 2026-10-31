"""Git checkpoints: one bare repo per task on the worker host, mounted into the sandbox.

The design and the reasons are in ADR-0006. In short:

* `init_task` clones the repo into <root>/<task_id>.git on the host and puts the branch
  longhaul/<issue> at the base commit. No token, URL or host path is written into the repo.
* The sandbox gets that folder read-write at /ckpt (DockerSandboxManager.create(mounts=...)).
  `attach` clones it into /workspace, so every later step needs nothing but the sandbox.
* `commit` snapshots the workspace as a commit and pushes it to /ckpt. `restore` puts the
  workspace back on any commit, byte for byte, so a retried tool call never runs on a half
  changed tree.
* `bundle` and `restore_from_bundle` move a task to another host through the blob store.

The agent can write to /ckpt, so the host treats everything in that repo as untrusted: it runs
git with hooks switched off, and only for a few read-only or ref-only commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import shutil
import stat
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from longhaul.blobs.base import check_key
from longhaul.contracts import BlobRef, BlobStore, CheckpointStore, ExecResult, Sandbox
from longhaul.settings import EnvSettings

# Where the bare repo appears inside the sandbox. The image trusts it in its git config.
CKPT_MOUNT = "/ckpt"
BASE_TAG = "base"  # what the git_diff tool compares with (tools/git.py)
# Folders that survive a restore. The venv is built once, by the setup command.
KEEP = (".venv",)
# Kept out of every checkpoint through .git/info/exclude, so a repo whose own .gitignore misses
# them still commits only the agent's work. Same list as tools/git.py leaves out of its diff.
EXCLUDES = (
    ".venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "*.egg-info",
    "*.pyc",
)
# A bundle holds the whole history, and the blob store takes bytes, so the worker holds it in
# memory. This keeps a huge repo from taking the worker down.
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_MESSAGE_CHARS = 4000

CLONE_TIMEOUT_S = 900
HOST_GIT_TIMEOUT_S = 120
ATTACH_TIMEOUT_S = 900
RESTORE_TIMEOUT_S = 300
COMMIT_TIMEOUT_S = 180

_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_ISSUE = re.compile(r"-(\d+)$")
_HELPER_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*::")
_OUTPUT = re.compile(r"LH-(?:CLEAN|SHA (?P<sha>[0-9a-f]{40,64}))")
_MARKER = re.compile(r"@([A-Z]+)@")

# The git that runs on the host. The sandbox can rewrite the config and hooks of the repo it
# has mounted, so none of that may run code here: no hooks, no fsmonitor program, no gc.
_HOST_FLAGS = (
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "core.fsmonitor=false",
    "-c", "gc.auto=0",
    "-c", "protocol.ext.allow=never",
)  # fmt: skip
# Variables a git hook exports. Left in place they would point git at another repository.
_REPO_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_QUARANTINE_PATH",
)


class CheckpointError(RuntimeError):
    """A checkpoint step failed."""


class RepoNotFoundError(CheckpointError):
    """The task has no bare repo on this host (failure mode F4)."""


# ---------- names ----------


def check_task_id(task_id: str) -> str:
    """Return task_id if it is safe as a folder name, else raise ValueError."""
    if not _TASK_ID.fullmatch(task_id) or ".." in task_id:
        raise ValueError(
            f"invalid task id {task_id[:80]!r}: use letters, digits, '.', '_' and '-', start "
            "with a letter or digit, never use '..', and stay within 128 characters"
        )
    return task_id


def check_sha(sha: str) -> str:
    """Return sha if it is a full lowercase hex object name, else raise ValueError."""
    if not _SHA.fullmatch(sha):
        raise ValueError(f"invalid sha {sha[:80]!r}: expected 40 or 64 lowercase hex characters")
    return sha


def branch_name(task_id: str) -> str:
    """longhaul/<issue>: a task id such as lh-acme-calc-42 ends in the issue number."""
    check_task_id(task_id)
    issue = _ISSUE.search(task_id)
    return f"longhaul/{issue.group(1) if issue else task_id}"


def bundle_key(task_id: str, sha: str) -> str:
    return check_key(f"tasks/{check_task_id(task_id)}/ckpt/{check_sha(sha)}.bundle")


def check_clone_url(url: str) -> str:
    """Return url if git may clone it without a secret in it, else raise ValueError.

    The URL is never repeated in the message, because the reason for refusing it may be a token.
    Credentials belong in the environment of the clone, not in a URL that git would write into
    the repo's config.
    """
    problem = _url_problem(url)
    if problem:
        raise ValueError(f"clone_url {problem}")
    return url


def _url_problem(url: str) -> str | None:
    if not url.strip() or url != url.strip() or any(ord(c) < 32 or c == "\x7f" for c in url):
        return "is empty or has stray whitespace"
    if url.startswith("-"):
        return "starts with '-', so git would read it as an option"
    if _HELPER_URL.match(url):
        return "names a transport helper, which can run commands"
    try:
        parts = urlsplit(url)
        username, password = parts.username, parts.password
    except ValueError:
        return "is not a valid URL"
    if password or (username and parts.scheme in ("http", "https")):
        return "carries credentials; pass them in the environment of the clone"
    return None


# ---------- git on the host ----------


def _host_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _REPO_VARS}
    # A missing credential must fail the clone, not wait for someone at a terminal.
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", LC_ALL="C")
    return env


async def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


async def _run_git(
    *args: str, git_dir: Path | None = None, timeout_s: float = HOST_GIT_TIMEOUT_S
) -> tuple[int, str, str]:
    """Run git on the host and return its exit code, stdout and stderr. Never raises on exit."""
    where = ("--git-dir", str(git_dir)) if git_dir is not None else ()
    proc = await asyncio.create_subprocess_exec(
        "git",
        *_HOST_FLAGS,
        *where,
        *args,
        env=_host_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError as exc:
        await _kill(proc)
        raise CheckpointError(f"git {args[0]} timed out after {timeout_s:.0f} s") from exc
    except BaseException:
        await _kill(proc)  # a cancelled activity must not leave git running
        raise
    return proc.returncode or 0, out.decode(errors="replace").strip(), err.decode(errors="replace")


async def git(
    *args: str, git_dir: Path | None = None, timeout_s: float = HOST_GIT_TIMEOUT_S
) -> str:
    """Like _run_git, but a failure raises CheckpointError and the result is stdout."""
    code, out, err = await _run_git(*args, git_dir=git_dir, timeout_s=timeout_s)
    if code != 0:
        raise CheckpointError(f"git {args[0]} failed (exit {code}): {_tail(err or out)}")
    return out


async def git_value(*args: str, git_dir: Path | None = None) -> str | None:
    """stdout of a git call that answers by its exit code, such as `config --get`."""
    code, out, _ = await _run_git(*args, git_dir=git_dir)
    return out if code == 0 else None


def _tail(text: str, limit: int = 800) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


# ---------- the files of a repo ----------


def _add_bits(path: str, bits: int) -> None:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode | bits != mode:
        os.chmod(path, mode | bits)


def _open_to_sandbox(repo: Path) -> None:
    """Let every user write in the repo, so the sandbox user can write whoever owns the folder.

    ADR-0006: this is safe because the root above the repo is private (0700), so only the worker
    user can reach the path on the host. A container reaches the repo through its mount.
    """
    for directory, _dirs, files in os.walk(repo):
        _add_bits(directory, 0o777)
        for name in files:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                _add_bits(path, 0o666)


def _remove_tree(path: Path) -> None:
    """Delete a tree, including files and folders that lost their write bit."""

    def retry_writable(func: Any, failed: str, _error: BaseException) -> None:
        # Git writes objects read-only, and deleting a file needs write access to its folder.
        with contextlib.suppress(OSError):
            os.chmod(os.path.dirname(failed), stat.S_IRWXU)
            os.chmod(failed, stat.S_IRWXU)
        func(failed)

    if path.exists() or path.is_symlink():
        shutil.rmtree(path, onexc=retry_writable)


def default_root() -> Path:
    """LONGHAUL_CKPT_DIR, else ~/.longhaul/ckpt."""
    configured = EnvSettings().ckpt_dir
    return configured if configured is not None else Path.home() / ".longhaul" / "ckpt"


def _local_source(url: str) -> str:
    """A path that exists is made absolute, so the clone does not depend on the working dir."""
    return str(Path(url).resolve()) if os.path.exists(url) else url


# ---------- the scripts that run in the sandbox ----------
#
# Plain bash, written to run on bash 3.2 too, because the unit tests run them in the fake
# sandbox on a Mac. @NAME@ marks a value that Python fills in, already quoted for the shell.
# `g` is git with everything the agent could have changed in its own config switched off, and
# an identity set, so the scripts do not depend on the image.

_PRELUDE = r"""
set -eu
# The workspace venv leads PATH, and the agent can put a program there. The system folders
# come first, so a file called git or find in it cannot stand in for the real one.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
CKPT=@CKPT@
g() {
  git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.autocrlf=false \
    -c gc.auto=0 -c 'safe.directory=*' -c user.name=longhaul -c user.email=longhaul@localhost \
    -c commit.gpgsign=false -c tag.gpgsign=false "$@"
}
clear_locks() {
  find .git -path .git/objects -prune -o -name '*.lock' -type f -exec rm -f {} +
}
BRANCH=$(g --git-dir="$CKPT" symbolic-ref --short HEAD 2>/dev/null) || {
  echo "$CKPT is not a checkpoint repository" >&2
  exit 3
}
"""

# A workspace that is a clone of /ckpt, on the task branch. It starts a repo if there is none
# or the agent broke it, and it clears the locks that a killed command leaves behind.
_ENSURE_REPO = r"""
ensure_repo() {
  if [ "$(g rev-parse --git-dir 2>/dev/null || true)" != .git ]; then
    rm -rf .git
    g init -q
  fi
  mkdir -p .git/info
  for pattern in @EXCLUDES@; do
    grep -qxF -- "$pattern" .git/info/exclude 2>/dev/null ||
      printf '%s\n' "$pattern" >> .git/info/exclude
  done
  clear_locks
  g symbolic-ref HEAD "refs/heads/$BRANCH"
  g fetch -q --no-tags --force "$CKPT" \
    "+refs/heads/$BRANCH:refs/remotes/ckpt/$BRANCH" "+refs/tags/base:refs/tags/base"
}
"""

_ATTACH = r"""
OWNER=$(g --git-dir="$CKPT" config --get longhaul.task-id || true)
[ "$OWNER" = @TASK@ ] || {
  echo "$CKPT holds the checkpoints of task '$OWNER', not of task @TASK@" >&2
  exit 3
}
REFDIR="$CKPT/refs/heads/$(dirname "$BRANCH")"
while [ ! -d "$REFDIR" ]; do REFDIR=$(dirname "$REFDIR"); done  # refs may be packed
[ -w "$CKPT/objects" ] && [ -w "$REFDIR" ] || {
  echo "the sandbox user cannot write to $CKPT; see ADR-0006 for the host permissions" >&2
  exit 5
}
ensure_repo
g rev-parse -q --verify HEAD >/dev/null || g reset -q --hard "refs/remotes/ckpt/$BRANCH"
"""

# The checks come first, so a restore that cannot succeed has not touched the work yet.
_RESTORE = r"""
SHA=@SHA@
ensure_repo
g cat-file -e "${SHA}^{commit}" 2>/dev/null ||
  g fetch -q --no-tags "$CKPT" "$SHA" 2>/dev/null || true
g cat-file -e "${SHA}^{commit}" 2>/dev/null || {
  echo "commit $SHA is not in the checkpoint repository" >&2
  exit 6
}
g reset -q --hard "$SHA"
g clean -q -ffdx @KEEP@
"""

# HEAD goes back to the task branch first: the agent may have switched branches, and the
# snapshot has to be a child of the branch the checkpoints live on. A failed push takes the
# commit back, so the changes are still pending and the next call tries again.
_COMMIT = r"""
MSG=@MSG@
[ -d .git ] || {
  echo "the workspace is not a git repository; restore it first" >&2
  exit 4
}
clear_locks
g symbolic-ref HEAD "refs/heads/$BRANCH"
g rev-parse -q --verify HEAD >/dev/null || {
  echo "the workspace has no commit to build on; restore it first" >&2
  exit 4
}
g add -A
if g diff --cached --quiet; then
  echo LH-CLEAN
  exit 0
fi
PARENT=$(g rev-parse HEAD)
g commit -q -m "$MSG"
SHA=$(g rev-parse HEAD)
if ! g push -q "$CKPT" "+$SHA:refs/heads/$BRANCH"; then
  g reset -q --soft "$PARENT"
  echo "the push to $CKPT failed, so the commit was undone" >&2
  exit 7
fi
echo "LH-SHA $SHA"
"""


def _script(body: str, **values: str) -> str:
    """The prelude and `body` with every @NAME@ replaced by its value, in a single pass.

    One pass matters: a value such as a commit message is not scanned again, so text in it that
    looks like a marker stays text. A marker without a value is a bug and raises KeyError.
    """
    wanted = {name.upper(): value for name, value in values.items()}
    return _MARKER.sub(lambda found: wanted[found.group(1)], _PRELUDE + body)


def _words(items: Sequence[str]) -> str:
    return " ".join(shlex.quote(item) for item in items)


def _commit_message(message: str) -> str:
    if not message.strip() or "\x00" in message or len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(
            f"commit message must have text, no NUL character and at most {MAX_MESSAGE_CHARS} "
            "characters"
        )
    return message


def _outcome(result: ExecResult, what: str, timeout_s: int) -> str:
    if result.timed_out:
        raise CheckpointError(f"{what} timed out after {timeout_s} s")
    if result.exit_code != 0:
        raise CheckpointError(f"{what} failed (exit {result.exit_code}): {_tail(result.stderr)}")
    return result.stdout


# ---------- the store ----------


class GitCheckpointStore(CheckpointStore):
    """CheckpointStore on bare repos under `root`, one per task.

    `mount_point` is where the repo appears in the sandbox. Tests that use the fake sandbox,
    which cannot mount anything, point it at the repo's path on the host.
    """

    def __init__(
        self,
        blobs: BlobStore,
        root: Path | str | None = None,
        *,
        mount_point: str = CKPT_MOUNT,
        keep: Sequence[str] = KEEP,
        max_bundle_bytes: int = MAX_BUNDLE_BYTES,
    ) -> None:
        self._blobs = blobs
        self.root = (Path(root) if root is not None else default_root()).expanduser().absolute()
        self.mount_point = mount_point
        self._keep = tuple(keep)
        self._max_bundle_bytes = max_bundle_bytes

    # -- where things are --

    def repo_path(self, task_id: str) -> Path:
        return self.root / f"{check_task_id(task_id)}.git"

    def mounts(self, task_id: str) -> dict[str, str]:
        """The `mounts` argument for DockerSandboxManager.create."""
        return {str(self.repo_path(task_id)): self.mount_point}

    def has_repo(self, task_id: str) -> bool:
        return self.repo_path(task_id).is_dir()

    def _require_repo(self, task_id: str) -> Path:
        repo = self.repo_path(task_id)
        if not repo.is_dir():
            raise RepoNotFoundError(f"task {task_id} has no checkpoint repo under {self.root}")
        return repo

    async def head(self, task_id: str) -> str:
        """The commit the task branch holds in the bare repo."""
        repo = self._require_repo(task_id)
        return await git(
            "rev-parse", "--verify", f"refs/heads/{branch_name(task_id)}^{{commit}}", git_dir=repo
        )

    # -- the host side --

    async def init_task(self, task_id: str, clone_url: str, base_sha: str) -> str:
        """Create the bare repo of a task and return its path, which is what to mount.

        It clones `clone_url`, puts the branch longhaul/<issue> and the tag `base` on
        `base_sha`, and keeps nothing of the URL. Called again for a task that has its repo,
        it checks that the base is the same and changes nothing, so a retry is safe.
        """
        check_task_id(task_id)
        check_sha(base_sha)
        check_clone_url(clone_url)
        final = self.repo_path(task_id)
        if final.exists():
            await self._check_existing(task_id, base_sha)
            return str(final)
        await asyncio.to_thread(self._make_root)
        scratch = self.root / f".{task_id}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            await git(
                "clone", "--bare", "--no-hardlinks", "--quiet", "--template=", "--",
                _local_source(clone_url), str(scratch), timeout_s=CLONE_TIMEOUT_S,
            )  # fmt: skip
            await self._place_branch(scratch, task_id, base_sha)
            await self._seal(scratch, task_id, base_sha)
            if not await asyncio.to_thread(self._publish, scratch, final):
                await self._check_existing(task_id, base_sha)  # another attempt got there first
        finally:
            await asyncio.to_thread(_remove_tree, scratch)
        return str(final)

    def _make_root(self) -> None:
        # Private to the worker user. An existing folder keeps the mode its owner chose.
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    @staticmethod
    def _publish(scratch: Path, final: Path) -> bool:
        """Move a finished repo into place. A repo is never visible half built."""
        try:
            os.rename(scratch, final)
        except OSError:
            if not final.exists():
                raise
            return False
        return True

    async def _check_existing(self, task_id: str, base_sha: str) -> None:
        repo = self.repo_path(task_id)
        owner = await git_value("config", "--get", "longhaul.task-id", git_dir=repo)
        recorded = await git_value("config", "--get", "longhaul.base-sha", git_dir=repo)
        if owner != task_id or recorded != base_sha:
            raise CheckpointError(
                f"{repo} already exists and is not the repo of task {task_id} at base {base_sha}"
            )

    async def _place_branch(self, repo: Path, task_id: str, base_sha: str) -> None:
        code, _, _ = await _run_git("cat-file", "-e", f"{base_sha}^{{commit}}", git_dir=repo)
        if code != 0:
            raise CheckpointError(f"commit {base_sha} is not in the repository that was cloned")
        # A re-run can find an old branch of the same name on the remote. It is moved to base.
        await git("update-ref", f"refs/heads/{branch_name(task_id)}", base_sha, git_dir=repo)
        await git("update-ref", f"refs/tags/{BASE_TAG}", base_sha, git_dir=repo)

    async def _seal(self, repo: Path, task_id: str, base_sha: str) -> None:
        """Last steps for a new repo, whether it came from a clone or from a bundle."""
        await git("symbolic-ref", "HEAD", f"refs/heads/{branch_name(task_id)}", git_dir=repo)
        # The clone left its URL in the config. The sandbox can read this file, and later steps
        # that push to GitHub name their URL on the command line.
        await _run_git("config", "--remove-section", "remote.origin", git_dir=repo)
        settings = {
            "longhaul.task-id": task_id,
            "longhaul.base-sha": base_sha,
            "core.sharedRepository": "0666",  # files the sandbox adds stay open to the host
            "uploadpack.allowAnySHA1InWant": "true",  # restore can fetch a replaced commit
            "receive.denyNonFastForwards": "false",  # a retry moves the branch back
            "gc.auto": "0",
        }
        for key, value in settings.items():
            await git("config", key, value, git_dir=repo)
        await asyncio.to_thread(_open_to_sandbox, repo)

    async def cleanup_task(self, task_id: str) -> None:
        """Remove the bare repo, and any half-built clone, of a task. Safe to call twice."""
        repo = self.repo_path(task_id)
        leftovers = list(self.root.glob(f".{task_id}.*.tmp")) if self.root.is_dir() else []
        for path in [repo, *leftovers]:
            await asyncio.to_thread(_remove_tree, path)

    # -- the sandbox side --

    async def _in_sandbox(
        self, sandbox: Sandbox, body: str, what: str, timeout_s: int, **values: str
    ) -> str:
        script = _script(
            body, ckpt=shlex.quote(self.mount_point), excludes=_words(EXCLUDES), **values
        )
        result = await sandbox.exec(script, timeout_s=timeout_s)
        return _outcome(result, what, timeout_s)

    async def attach(self, sandbox: Sandbox, task_id: str) -> None:
        """Make /workspace a clone of the task's repo, on its branch. Safe to repeat.

        On a workspace that already has work in it, the work is left alone: call `restore`
        next to bring it to a commit.
        """
        check_task_id(task_id)
        await self._in_sandbox(
            sandbox, _ENSURE_REPO + _ATTACH, "attach the workspace", ATTACH_TIMEOUT_S,
            task=shlex.quote(task_id),
        )  # fmt: skip

    async def commit(self, sandbox: Sandbox, message: str) -> str | None:
        """Save the workspace as a commit on the task branch and return its sha.

        Returns None, and creates nothing, when the tree matches the last commit.
        """
        out = await self._in_sandbox(
            sandbox, _COMMIT, "commit the workspace", COMMIT_TIMEOUT_S,
            msg=shlex.quote(_commit_message(message)),
        )  # fmt: skip
        lines = out.strip().splitlines()
        found = _OUTPUT.fullmatch(lines[-1]) if lines else None
        if found is None:
            raise CheckpointError(f"commit gave unexpected output: {_tail(out)}")
        return found.group("sha")  # None for LH-CLEAN

    async def restore(self, sandbox: Sandbox, sha: str) -> None:
        """Put the workspace on `sha`, byte for byte: tracked files, no extras, no leftovers.

        Everything not in the commit is deleted, ignored files included, except the folders in
        `keep`. The checkpoint branch in /ckpt is not changed.
        """
        keep = " ".join(f"-e {shlex.quote(name)}" for name in self._keep)
        await self._in_sandbox(
            sandbox, _ENSURE_REPO + _RESTORE, "restore the workspace", RESTORE_TIMEOUT_S,
            sha=shlex.quote(check_sha(sha)), keep=keep,
        )  # fmt: skip

    # -- bundles --

    async def bundle(self, task_id: str) -> BlobRef:
        """Upload the whole history up to the branch head as tasks/<id>/ckpt/<sha>.bundle."""
        repo = self._require_repo(task_id)
        branch = f"refs/heads/{branch_name(task_id)}"
        head = check_sha(await self.head(task_id))
        key = bundle_key(task_id, head)
        # The sandbox may have moved HEAD. A bundle with a HEAD lets a plain `git clone` work.
        await git("symbolic-ref", "HEAD", branch, git_dir=repo)
        with tempfile.TemporaryDirectory(prefix="lh-bundle-") as scratch:
            path = Path(scratch) / "checkpoint.bundle"
            await git(
                "bundle", "create", "--quiet", str(path), "HEAD", branch, f"refs/tags/{BASE_TAG}",
                git_dir=repo, timeout_s=CLONE_TIMEOUT_S,
            )  # fmt: skip
            if path.stat().st_size > self._max_bundle_bytes:
                raise self._too_large(path.stat().st_size)
            data = await asyncio.to_thread(path.read_bytes)
        if await self.head(task_id) != head:
            raise CheckpointError(f"the branch of task {task_id} moved while the bundle was made")
        return await self._blobs.put(data, key=key)

    def _too_large(self, size: int) -> CheckpointError:
        return CheckpointError(
            f"the bundle is {size} bytes, larger than the limit of {self._max_bundle_bytes}"
        )

    async def restore_from_bundle(self, task_id: str, ref: BlobRef) -> str:
        """Rebuild the bare repo of a task on this host from a bundle. Returns the branch head.

        It never replaces a repo that exists. After it, mount `mounts(task_id)` into a new
        sandbox, call `attach`, and `restore` to the head.
        """
        final = self.repo_path(task_id)
        if final.exists():
            raise CheckpointError(f"{final} already exists; remove it before restoring a bundle")
        if ref.size > self._max_bundle_bytes:
            raise self._too_large(ref.size)
        data = await self._blobs.get(ref)  # checked against the sha256 in the ref
        await asyncio.to_thread(self._make_root)
        scratch = self.root / f".{task_id}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            with tempfile.TemporaryDirectory(prefix="lh-bundle-") as source:
                path = Path(source) / "checkpoint.bundle"
                await asyncio.to_thread(path.write_bytes, data)
                await git(
                    "clone", "--bare", "--no-hardlinks", "--quiet", "--template=", "--",
                    str(path), str(scratch), timeout_s=CLONE_TIMEOUT_S,
                )  # fmt: skip
            base_sha = await self._bundle_base(scratch, task_id)
            await self._seal(scratch, task_id, base_sha)
            if not await asyncio.to_thread(self._publish, scratch, final):
                raise CheckpointError(f"{final} appeared while the bundle was restored")
        finally:
            await asyncio.to_thread(_remove_tree, scratch)
        return await self.head(task_id)

    @staticmethod
    async def _bundle_base(repo: Path, task_id: str) -> str:
        branch = f"refs/heads/{branch_name(task_id)}"
        if await git_value("rev-parse", "--verify", f"{branch}^{{commit}}", git_dir=repo) is None:
            raise CheckpointError(f"the bundle has no branch {branch_name(task_id)}")
        base = await git_value(
            "rev-parse", "--verify", f"refs/tags/{BASE_TAG}^{{commit}}", git_dir=repo
        )
        if base is None:
            raise CheckpointError(f"the bundle has no '{BASE_TAG}' tag")
        return check_sha(base)
