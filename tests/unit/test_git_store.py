"""GitCheckpointStore with the fake sandbox (LH-205).

The fake sandbox is a temp directory that stands in for /workspace. The store reaches the bare
repo through `mount_point`, which these tests point at the repo's real path, so the same shell
scripts run here and in the Docker tests of tests/integration/test_checkpoints.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import shutil
import stat
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from longhaul.blobs.base import BlobIntegrityError
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import (
    EXCLUDES,
    CheckpointError,
    GitCheckpointStore,
    RepoNotFoundError,
    _script,
    branch_name,
    bundle_key,
    check_clone_url,
    check_sha,
)
from longhaul.checkpoints.git_store import git as git_command
from longhaul.contracts import ExecResult
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.git import NOISE_GLOBS

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
TASK = "lh-acme-calc-42"
BRANCH = "longhaul/42"
MODULE = "src/calc/__init__.py"
FIXED = "def add(a: int, b: int) -> int:\n    return a + b\n"

_spec = importlib.util.spec_from_file_location(
    "make_fixture_git", ROOT / "scripts" / "make_fixture_git.py"
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
make_fixture_git = _module.make_fixture_git


def git(*args: str) -> str:
    """Git on the host with no GIT_* variables from a running hook."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, env=env, timeout=60
    )
    return result.stdout.strip()


async def sh(box: FakeSandbox, script: str) -> str:
    result = await box.exec(script, timeout_s=60)
    assert result.exit_code == 0, result.stderr
    return result.stdout


def snapshot(root: Path) -> dict[str, tuple[str, str]]:
    """Every file and folder below root except .git and .venv: kind and content hash."""
    found: dict[str, tuple[str, str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] in (".git", ".venv"):
            continue
        mode = f"{stat.S_IMODE(path.lstat().st_mode):o}"
        if path.is_dir():
            found[relative.as_posix()] = ("dir", mode)
        else:
            found[relative.as_posix()] = (hashlib.sha256(path.read_bytes()).hexdigest(), mode)
    return found


@dataclass
class Env:
    store: GitCheckpointStore
    blobs: MemoryBlobStore
    source: Path
    base_sha: str
    tmp_path: Path

    @property
    def repo(self) -> Path:
        return self.store.repo_path(TASK)

    def bare(self, *args: str) -> str:
        return git(f"--git-dir={self.repo}", *args)

    def head(self) -> str:
        return self.bare("rev-parse", f"refs/heads/{BRANCH}")


@dataclass(frozen=True)
class Template:
    """Work that every test would repeat, done once: the source repo, the bare repo that
    init_task leaves, and the workspace that attach leaves. Tests get copies. The tests that
    are about init_task and attach run them for real."""

    source: Path
    base_sha: str
    repo: Path
    workspace: Path


def _attached(base: Path, source: Path, base_sha: str) -> tuple[Path, Path]:
    store = GitCheckpointStore(MemoryBlobStore(), root=base / "ckpt")
    sandbox = FakeSandbox()

    async def build() -> None:
        await store.init_task(TASK, str(source), base_sha)
        store.mount_point = str(store.repo_path(TASK))
        await store.attach(sandbox, TASK)

    asyncio.run(build())
    return store.repo_path(TASK), sandbox.root


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Template]:
    base = tmp_path_factory.mktemp("template")
    source = make_fixture_git(CALC, base / "source")
    base_sha = git("-C", str(source), "rev-parse", "HEAD")
    repo, workspace = _attached(base, source, base_sha)
    try:
        yield Template(source, base_sha, repo, workspace)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


@pytest.fixture
def env(template: Template, tmp_path: Path) -> Env:
    root = tmp_path / "ckpt"
    root.mkdir(mode=0o700)
    shutil.copytree(template.repo, root / f"{TASK}.git", symlinks=True)
    source = shutil.copytree(template.source, tmp_path / "source", symlinks=True)
    blobs = MemoryBlobStore()
    store = GitCheckpointStore(blobs, root=root)
    # The fake sandbox cannot mount anything, so it reads the repo from its host path.
    store.mount_point = str(store.repo_path(TASK))
    return Env(store, blobs, source, template.base_sha, tmp_path)


@pytest.fixture
async def box(template: Template) -> AsyncIterator[FakeSandbox]:
    sandbox = FakeSandbox()
    shutil.copytree(template.workspace, sandbox.root, symlinks=True, dirs_exist_ok=True)
    try:
        yield sandbox
    finally:
        await sandbox.destroy()


@pytest.fixture
def plain_store(tmp_path: Path) -> GitCheckpointStore:
    """A store with no repo, for the checks that must fail before anything runs."""
    return GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "plain-root")


@pytest.fixture
async def empty_box() -> AsyncIterator[FakeSandbox]:
    sandbox = FakeSandbox()
    try:
        yield sandbox
    finally:
        await sandbox.destroy()


@pytest.fixture
async def fresh(template: Template, tmp_path: Path) -> Env:
    """A repo that init_task builds in this test, in a root it creates."""
    blobs = MemoryBlobStore()
    store = GitCheckpointStore(blobs, root=tmp_path / "fresh-root")
    await store.init_task(TASK, str(template.source), template.base_sha)
    store.mount_point = str(store.repo_path(TASK))
    return Env(store, blobs, template.source, template.base_sha, tmp_path)


# ---------- names, shas and urls ----------


@pytest.mark.parametrize(
    ("task_id", "branch"),
    [
        ("lh-acme-calc-42", "longhaul/42"),
        ("lh-acme-widgets-v2-1007", "longhaul/1007"),
        ("local-calc", "longhaul/local-calc"),
        ("t3fa9b2c871", "longhaul/t3fa9b2c871"),
    ],
)
def test_branch_is_named_for_the_issue_or_else_the_task(task_id: str, branch: str) -> None:
    assert branch_name(task_id) == branch


@pytest.mark.parametrize(
    "task_id",
    ["", ".", "..", "../x", "a/b", "a\\b", ".hidden", "-flag", "has space", "x" * 129, "tab\t"],
)
def test_task_ids_that_could_leave_the_root_are_refused(tmp_path: Path, task_id: str) -> None:
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path)
    with pytest.raises(ValueError, match="task id"):
        store.repo_path(task_id)
    with pytest.raises(ValueError, match="task id"):
        bundle_key(task_id, "a" * 40)
    with pytest.raises(ValueError, match="task id"):
        branch_name(task_id)


@pytest.mark.parametrize(
    "sha", ["", "abc123", "HEAD", "main", "A" * 40, "g" * 40, "a" * 41, " " * 40]
)
def test_only_full_lowercase_shas_are_accepted(sha: str) -> None:
    with pytest.raises(ValueError, match="sha"):
        check_sha(sha)


def test_full_shas_of_both_lengths_are_accepted() -> None:
    assert check_sha("a" * 40) == "a" * 40
    assert check_sha("0123456789abcdef" * 4) == "0123456789abcdef" * 4


def test_bundle_key_is_the_documented_one() -> None:
    sha = "ab" * 20
    assert bundle_key(TASK, sha) == f"tasks/{TASK}/ckpt/{sha}.bundle"
    with pytest.raises(ValueError, match="sha"):
        bundle_key(TASK, "../../etc/passwd")


@pytest.mark.parametrize(
    "url",
    [
        "/srv/repos/calc",
        "./calc",
        "file:///srv/repos/calc",
        "https://github.com/acme/calc.git",
        "http://git.internal/acme/calc.git",
        "ssh://git@github.com/acme/calc.git",
        "git@github.com:acme/calc.git",
        "git://example.org/calc.git",
    ],
)
def test_clone_urls_without_credentials_pass(url: str) -> None:
    assert check_clone_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://x-access-token:ghs_SECRET@github.com/acme/calc.git",
        "https://ghp_SECRET@github.com/acme/calc.git",
        "http://user:SECRET@host/calc.git",
        "ssh://git:SECRET@host/calc.git",
        "ext::sh -c 'touch /tmp/pwned'",
        "fd::17/foo",
        "--upload-pack=touch /tmp/pwned",
        "-c core.sshCommand=evil",
        "",
        "  ",
        "https://host/calc.git\nsecond",
        "http://[bad/calc.git",
    ],
)
def test_clone_urls_that_carry_credentials_or_run_commands_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match="clone_url") as caught:
        check_clone_url(url)
    # The message must not repeat the secret.
    assert "SECRET" not in str(caught.value)


def test_script_values_are_filled_in_once_and_a_missing_one_is_an_error() -> None:
    text = _script("A=@FIRST@ B=@SECOND@", first="@SECOND@", second="'x'", ckpt="/ckpt")
    assert text.endswith("A=@SECOND@ B='x'")
    with pytest.raises(KeyError):
        _script("A=@NOPE@", ckpt="/ckpt")


def test_the_exclude_list_matches_what_the_diff_tool_leaves_out() -> None:
    # tools/git.py asks workspace preparation to keep these out of the index as well.
    noise = {glob.removeprefix("**/").removesuffix("/**") for glob in NOISE_GLOBS}
    assert noise - {".git"} == set(EXCLUDES)  # git never lists its own folder


# ---------- the host path ----------


def test_the_root_defaults_to_the_environment_then_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LONGHAUL_CKPT_DIR", raising=False)
    assert GitCheckpointStore(MemoryBlobStore()).root == tmp_path / "home" / ".longhaul" / "ckpt"

    monkeypatch.setenv("LONGHAUL_CKPT_DIR", str(tmp_path / "from-env"))
    assert GitCheckpointStore(MemoryBlobStore()).root == tmp_path / "from-env"

    explicit = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "explicit")
    assert explicit.root == tmp_path / "explicit"


def test_repo_path_and_mounts_name_the_task(tmp_path: Path) -> None:
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path)
    assert store.repo_path(TASK) == tmp_path / f"{TASK}.git"
    assert store.mounts(TASK) == {str(tmp_path / f"{TASK}.git"): "/ckpt"}


# ---------- init_task ----------


async def test_init_task_builds_a_bare_repo_on_the_task_branch(fresh: Env) -> None:
    assert fresh.bare("rev-parse", "--is-bare-repository") == "true"
    assert fresh.head() == fresh.base_sha
    assert fresh.bare("symbolic-ref", "HEAD") == f"refs/heads/{BRANCH}"
    assert fresh.bare("rev-parse", "refs/tags/base") == fresh.base_sha
    assert fresh.bare("config", "longhaul.task-id") == TASK
    assert fresh.bare("config", "longhaul.base-sha") == fresh.base_sha
    assert await fresh.store.head(TASK) == fresh.base_sha
    assert fresh.store.has_repo(TASK)
    # Only the repo is left in the root: no temporary clone.
    assert sorted(p.name for p in fresh.store.root.iterdir()) == [f"{TASK}.git"]


async def test_init_task_returns_the_path_to_mount(template: Template, tmp_path: Path) -> None:
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "root")
    path = await store.init_task(TASK, str(template.source), template.base_sha)
    assert path == str(tmp_path / "root" / f"{TASK}.git") == str(store.repo_path(TASK))
    assert list(store.mounts(TASK)) == [path]


async def test_the_repo_keeps_no_remote_and_no_trace_of_where_it_came_from(fresh: Env) -> None:
    config = (fresh.repo / "config").read_text()
    assert str(fresh.source) not in config
    assert "[remote" not in config and "url" not in config.lower()
    listing = subprocess.run(
        ["git", f"--git-dir={fresh.repo}", "config", "--local", "--list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert str(fresh.source) not in listing


async def test_objects_are_copied_and_not_hard_linked_to_the_source(fresh: Env) -> None:
    # The sandbox can write to this repo. A hard link would let it change the source's files.
    objects = (fresh.repo / "objects").rglob("*")
    assert [p for p in objects if p.is_file() and p.stat().st_nlink > 1] == []


async def test_repo_is_open_to_the_sandbox_user_inside_a_private_root(fresh: Env) -> None:
    # Decision in ADR-0006: the repo is world writable, and the root above it is private.
    assert stat.S_IMODE(fresh.store.root.stat().st_mode) == 0o700
    assert fresh.bare("config", "core.sharedRepository") == "0666"
    closed = []
    for path in [fresh.repo, *fresh.repo.rglob("*")]:
        if path.is_symlink():
            continue
        needed = 0o777 if path.is_dir() else 0o666
        if stat.S_IMODE(path.stat().st_mode) & needed != needed:
            closed.append(path.relative_to(fresh.repo).as_posix())
    assert closed == []


async def test_an_existing_root_keeps_its_mode(template: Template, tmp_path: Path) -> None:
    root = tmp_path / "shared-root"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    store = GitCheckpointStore(MemoryBlobStore(), root=root)
    await store.init_task(TASK, str(template.source), template.base_sha)
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


async def test_init_task_again_keeps_the_commits_made_since(env: Env, box: FakeSandbox) -> None:
    await box.write_file(MODULE, FIXED.encode())
    sha = await env.store.commit(box, "lh: edit_file turn 1 call c1")

    await env.store.init_task(TASK, str(env.source), env.base_sha)

    assert env.head() == sha


async def test_init_task_refuses_another_base_for_an_existing_repo(env: Env) -> None:
    with pytest.raises(CheckpointError, match="already exists"):
        await env.store.init_task(TASK, str(env.source), "1" * 40)


@pytest.mark.parametrize(
    "url", ["https://x-access-token:SECRET@github.com/a/b.git", "ext::sh -c x"]
)
async def test_init_task_refuses_urls_with_credentials_before_touching_the_disk(
    tmp_path: Path, url: str
) -> None:
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "ckpt")
    with pytest.raises(ValueError, match="clone_url") as caught:
        await store.init_task(TASK, url, "a" * 40)
    assert "SECRET" not in str(caught.value)
    assert not (tmp_path / "ckpt").exists()


@pytest.mark.parametrize("sha", ["main", "abc", "A" * 40])
async def test_init_task_wants_a_full_sha(tmp_path: Path, template: Template, sha: str) -> None:
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "other")
    with pytest.raises(ValueError, match="sha"):
        await store.init_task("lh-x-1", str(template.source), sha)


async def test_init_task_with_a_base_that_is_not_in_the_source_leaves_nothing(
    tmp_path: Path, template: Template
) -> None:
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "other")
    with pytest.raises(CheckpointError, match="not in the repository"):
        await store.init_task("lh-x-1", str(template.source), "1" * 40)
    assert list((tmp_path / "other").iterdir()) == []
    assert not store.has_repo("lh-x-1")


async def test_init_task_with_an_unreachable_source_is_an_error_and_leaves_nothing(
    tmp_path: Path,
) -> None:
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "other")
    with pytest.raises(CheckpointError, match="clone"):
        await store.init_task("lh-x-1", str(tmp_path / "missing"), "1" * 40)
    assert list((tmp_path / "other").iterdir()) == []


async def test_a_branch_of_the_source_with_the_task_branch_name_is_moved_to_base(
    tmp_path: Path, env: Env
) -> None:
    # A re-run of an issue can find an old longhaul/<n> branch on the remote.
    (env.source / "later.txt").write_text("later\n")
    git("-C", str(env.source), "add", "-A")
    git(
        "-C", str(env.source), "-c", "user.name=t", "-c", "user.email=t@t",
        "-c", "commit.gpgsign=false", "commit", "-qm", "later",
    )  # fmt: skip
    git("-C", str(env.source), "branch", "longhaul/7")
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "other")

    await store.init_task("lh-acme-calc-7", str(env.source), env.base_sha)

    assert await store.head("lh-acme-calc-7") == env.base_sha


# ---------- attach ----------


async def test_attach_clones_the_branch_and_tags_base(env: Env) -> None:
    box = FakeSandbox()
    try:
        await env.store.attach(box, TASK)

        assert (box.root / "pyproject.toml").is_file()
        assert (await sh(box, "git rev-parse HEAD")).strip() == env.base_sha
        assert (await sh(box, "git symbolic-ref HEAD")).strip() == f"refs/heads/{BRANCH}"
        assert (await sh(box, "git rev-parse refs/tags/base")).strip() == env.base_sha
        assert (await sh(box, "git status --porcelain")).strip() == ""
        # The workspace keeps no remote and no path of the host.
        assert (await sh(box, "git config --local --list")).count("remote") == 0
        exclude = (box.root / ".git" / "info" / "exclude").read_text().splitlines()
        assert set(EXCLUDES) <= set(exclude)
    finally:
        await box.destroy()


async def test_attach_twice_leaves_the_work_alone(env: Env, box: FakeSandbox) -> None:
    await box.write_file(MODULE, FIXED.encode())

    await env.store.attach(box, TASK)

    assert (box.root / MODULE).read_text() == FIXED
    assert (await sh(box, "git rev-parse HEAD")).strip() == env.base_sha
    exclude = (box.root / ".git" / "info" / "exclude").read_text().splitlines()
    assert sorted(exclude) == sorted(set(exclude)), "the exclude list gained duplicates"


async def test_attach_refuses_the_repo_of_another_task(env: Env) -> None:
    sandbox = FakeSandbox()
    try:
        with pytest.raises(CheckpointError, match="lh-other-9"):
            await env.store.attach(sandbox, "lh-other-9")
    finally:
        await sandbox.destroy()


async def test_attach_without_a_repo_says_so(env: Env) -> None:
    sandbox = FakeSandbox()
    env.store.mount_point = str(env.tmp_path / "nothing-here")
    try:
        with pytest.raises(CheckpointError, match="checkpoint repository"):
            await env.store.attach(sandbox, TASK)
    finally:
        await sandbox.destroy()


async def test_attach_says_when_the_sandbox_user_cannot_write(env: Env) -> None:
    sandbox = FakeSandbox()
    try:
        for name in ("objects", "refs/heads/longhaul"):
            (env.repo / name).chmod(0o555)
        if os.geteuid() == 0:
            # Root writes anywhere, so there is nothing to refuse.
            await env.store.attach(sandbox, TASK)
        else:
            with pytest.raises(CheckpointError, match="cannot write"):
                await env.store.attach(sandbox, TASK)
    finally:
        for name in ("objects", "refs/heads/longhaul"):
            (env.repo / name).chmod(0o777)
        await sandbox.destroy()


# ---------- commit ----------


async def test_commit_pushes_the_checkpoint_and_returns_its_sha(env: Env, box: FakeSandbox) -> None:
    await box.write_file(MODULE, FIXED.encode())

    sha = await env.store.commit(box, "lh: edit_file turn 1 call c1")

    assert sha is not None and sha != env.base_sha
    assert env.head() == sha == await env.store.head(TASK)
    assert env.bare("rev-parse", f"{sha}^") == env.base_sha
    assert env.bare("show", f"{sha}:{MODULE}") == FIXED.strip()
    assert env.bare("log", "-1", "--format=%s%n%an <%ae>", sha).splitlines() == [
        "lh: edit_file turn 1 call c1",
        "longhaul <longhaul@localhost>",
    ]


async def test_commit_on_a_clean_tree_returns_none_and_adds_nothing(
    env: Env, box: FakeSandbox
) -> None:
    assert await env.store.commit(box, "lh: nothing") is None
    assert env.head() == env.base_sha
    assert env.bare("rev-list", "--count", f"refs/heads/{BRANCH}") == "1"

    # Files git ignores are not changes: the venv, caches and other build debris.
    await sh(
        box,
        "mkdir -p .venv/bin src/calc/__pycache__ build.egg-info"
        " && echo x > .venv/bin/python && echo y > src/calc/__pycache__/m.pyc"
        " && echo z > build.egg-info/PKG-INFO",
    )
    assert await env.store.commit(box, "lh: nothing") is None
    assert env.bare("rev-list", "--count", f"refs/heads/{BRANCH}") == "1"


async def test_the_venv_is_never_committed_even_when_the_repo_does_not_ignore_it(
    env: Env, box: FakeSandbox
) -> None:
    # The fixture's .gitignore lists .venv. A repo whose own file does not is the risk.
    await sh(box, "echo > .gitignore && mkdir -p .venv/lib && echo x > .venv/lib/a.py")
    sha = await env.store.commit(box, "lh: write_file turn 1 call c1")
    assert sha is not None
    assert env.bare("ls-tree", "-r", "--name-only", sha).count(".venv") == 0


async def test_commit_message_reaches_git_as_plain_text(env: Env, box: FakeSandbox) -> None:
    # Quotes, command substitution, backslashes, and the markers the scripts are filled in by.
    message = (
        'lh: it\'s "quoted"\n$(touch pwned) `touch pwned2` $HOME \\n end'
        "\n@CKPT@ @SHA@ @MSG@ @EXCLUDES@ @KEEP@ @TASK@ ';touch pwned3;'"
    )
    await box.write_file("a.txt", b"a\n")

    sha = await env.store.commit(box, message)

    assert sha is not None
    assert env.bare("log", "-1", "--format=%B", sha) == message
    for name in ("pwned", "pwned2", "pwned3"):
        assert not (box.root / name).exists()


@pytest.mark.parametrize("message", ["", "   ", "x" * 5000, "has\x00nul"])
async def test_commit_refuses_messages_that_are_empty_or_unusable(
    plain_store: GitCheckpointStore, empty_box: FakeSandbox, message: str
) -> None:
    with pytest.raises(ValueError, match="message"):
        await plain_store.commit(empty_box, message)
    assert list(empty_box.root.iterdir()) == [], "nothing ran in the sandbox"


async def test_commit_comes_back_to_the_task_branch_after_a_checkout(
    env: Env, box: FakeSandbox
) -> None:
    await sh(box, "git checkout -q -b feature && echo f > f.txt")

    sha = await env.store.commit(box, "lh: run_command turn 1 call c1")

    assert sha is not None and env.head() == sha
    assert (await sh(box, "git symbolic-ref HEAD")).strip() == f"refs/heads/{BRANCH}"
    assert env.bare("show", f"{sha}:f.txt") == "f"


async def test_programs_the_agent_puts_first_on_the_path_do_not_replace_git(
    env: Env, box: FakeSandbox
) -> None:
    # The venv leads PATH in the sandbox, and the agent writes there.
    await sh(
        box,
        "mkdir -p .venv/bin && for name in git find grep rm; do"
        " printf '#!/bin/sh\\necho LH-SHA %s\\n' " + "a" * 40 + " > .venv/bin/$name;"
        " chmod +x .venv/bin/$name; done",
    )
    await box.write_file("a.txt", b"a\n")

    sha = await env.store.commit(box, "lh: write_file turn 1 call c1")
    assert sha is not None and sha != "a" * 40 and env.head() == sha

    await env.store.restore(box, env.base_sha)
    assert not (box.root / "a.txt").exists()


async def test_commit_in_a_workspace_without_a_repo_asks_for_a_restore(
    env: Env, box: FakeSandbox
) -> None:
    await sh(box, "rm -rf .git")
    with pytest.raises(CheckpointError, match="restore"):
        await env.store.commit(box, "lh: edit_file turn 1 call c1")
    assert env.head() == env.base_sha


async def test_a_push_that_is_refused_takes_the_local_commit_back(
    env: Env, box: FakeSandbox
) -> None:
    await box.write_file(MODULE, FIXED.encode())
    first = await env.store.commit(box, "lh: edit_file turn 1 call c1")
    assert first is not None
    # The branch can no longer be rewritten, and the next commit is not a child of it.
    env.bare("config", "receive.denyNonFastForwards", "true")
    await env.store.restore(box, env.base_sha)
    await box.write_file("other.txt", b"other\n")

    with pytest.raises(CheckpointError, match="push"):
        await env.store.commit(box, "lh: write_file turn 2 call c2")

    # Nothing was committed locally, and the change is still waiting to be saved.
    assert (await sh(box, "git rev-parse HEAD")).strip() == env.base_sha
    assert "other.txt" in await sh(box, "git status --porcelain")
    assert env.head() == first

    env.bare("config", "receive.denyNonFastForwards", "false")
    second = await env.store.commit(box, "lh: write_file turn 2 call c2")
    assert second is not None and env.head() == second
    assert env.bare("rev-parse", f"{second}^") == env.base_sha


# ---------- restore ----------


async def test_restore_brings_the_workspace_back_byte_for_byte(env: Env, box: FakeSandbox) -> None:
    before = snapshot(box.root)
    await sh(box, "mkdir -p .venv/bin && echo keep > .venv/bin/marker")
    await box.write_file(MODULE, FIXED.encode())
    first = await env.store.commit(box, "lh: edit_file turn 1 call c1")
    assert first is not None
    committed = snapshot(box.root)
    await sh(
        box,
        "echo drift >> src/calc/__init__.py && rm tests/test_calc.py && echo new > notes.txt"
        " && mkdir -p src/calc/__pycache__ && echo x > src/calc/__pycache__/m.pyc"
        " && mkdir -p deep/empty/dir && chmod +x pyproject.toml && git add notes.txt"
        " && git init -q nested && echo n > nested/n.txt",
    )
    assert (await sh(box, "git status --porcelain")).strip() != ""

    await env.store.restore(box, first)
    assert snapshot(box.root) == committed
    await env.store.restore(box, env.base_sha)

    assert snapshot(box.root) == before
    assert (await sh(box, "git status --porcelain")).strip() == ""
    assert (await sh(box, "git rev-parse HEAD")).strip() == env.base_sha
    assert (box.root / ".venv" / "bin" / "marker").read_text() == "keep\n"
    assert env.head() == first, "restore must not touch the checkpoint branch"


async def test_restore_fetches_a_commit_this_workspace_has_never_seen(
    env: Env, box: FakeSandbox
) -> None:
    other = FakeSandbox()
    try:
        await env.store.attach(other, TASK)
        await other.write_file(MODULE, FIXED.encode())
        sha = await env.store.commit(other, "lh: edit_file turn 1 call c1")
        assert sha is not None

        await env.store.restore(box, sha)

        assert (box.root / MODULE).read_text() == FIXED
        assert (await sh(box, "git rev-parse HEAD")).strip() == sha
    finally:
        await other.destroy()


async def test_restore_reaches_a_commit_that_the_branch_no_longer_holds(
    env: Env, box: FakeSandbox
) -> None:
    # A retry replaces the branch head, and the replaced commit may still be on record.
    other = FakeSandbox()
    try:
        await env.store.attach(other, TASK)
        await other.write_file(MODULE, FIXED.encode())
        replaced = await env.store.commit(other, "lh: edit_file turn 1 call c1")
        assert replaced is not None
        await env.store.restore(other, env.base_sha)
        await other.write_file("other.txt", b"other\n")
        current = await env.store.commit(other, "lh: write_file turn 1 call c1")
        assert current is not None and env.head() == current
        assert replaced != current

        await env.store.restore(box, replaced)

        assert (await sh(box, "git rev-parse HEAD")).strip() == replaced
        assert (box.root / MODULE).read_text() == FIXED
        assert not (box.root / "other.txt").exists()
    finally:
        await other.destroy()


async def test_restore_rebuilds_a_destroyed_git_dir(env: Env, box: FakeSandbox) -> None:
    await box.write_file(MODULE, FIXED.encode())
    sha = await env.store.commit(box, "lh: edit_file turn 1 call c1")
    assert sha is not None
    committed = snapshot(box.root)
    await sh(box, "rm -rf .git && echo stray > stray.txt")

    await env.store.restore(box, sha)

    assert snapshot(box.root) == committed
    assert (await sh(box, "git rev-parse HEAD")).strip() == sha
    assert (await sh(box, "git symbolic-ref HEAD")).strip() == f"refs/heads/{BRANCH}"
    exclude = (box.root / ".git" / "info" / "exclude").read_text().splitlines()
    assert set(EXCLUDES) <= set(exclude)


async def test_restore_clears_locks_that_a_killed_command_left_behind(
    env: Env, box: FakeSandbox
) -> None:
    await sh(
        box,
        "mkdir -p .git/refs/heads/longhaul && touch .git/index.lock .git/HEAD.lock"
        f" .git/refs/heads/{BRANCH}.lock && echo drift >> pyproject.toml",
    )

    await env.store.restore(box, env.base_sha)

    assert (await sh(box, "git status --porcelain")).strip() == ""
    assert not list((box.root / ".git").rglob("*.lock"))


async def test_commit_clears_a_lock_that_a_killed_command_left_behind(
    env: Env, box: FakeSandbox
) -> None:
    await sh(box, "touch .git/index.lock")
    await box.write_file("a.txt", b"a\n")

    sha = await env.store.commit(box, "lh: write_file turn 1 call c1")

    assert sha is not None and env.head() == sha


async def test_restore_puts_the_workspace_back_on_the_task_branch(
    env: Env, box: FakeSandbox
) -> None:
    await sh(box, "git checkout -q --detach")

    await env.store.restore(box, env.base_sha)

    assert (await sh(box, "git symbolic-ref HEAD")).strip() == f"refs/heads/{BRANCH}"


async def test_restore_keeps_the_directories_named_in_keep(env: Env, tmp_path: Path) -> None:
    store = GitCheckpointStore(env.blobs, root=env.store.root, keep=(".venv", "node_modules"))
    store.mount_point = env.store.mount_point
    sandbox = FakeSandbox()
    try:
        await store.attach(sandbox, TASK)
        await sh(sandbox, "mkdir -p node_modules/x .venv/y other && touch node_modules/x/a other/b")

        await store.restore(sandbox, env.base_sha)

        assert (sandbox.root / "node_modules" / "x" / "a").exists()
        assert (sandbox.root / ".venv" / "y").exists()
        assert not (sandbox.root / "other").exists()
    finally:
        await sandbox.destroy()


async def test_restore_of_an_unknown_commit_names_it(env: Env, box: FakeSandbox) -> None:
    await sh(box, "echo keep >> pyproject.toml")
    with pytest.raises(CheckpointError, match="1" * 40):
        await env.store.restore(box, "1" * 40)
    # A failed restore must not have started: the work is still there.
    assert "keep" in (box.root / "pyproject.toml").read_text()


@pytest.mark.parametrize("sha", ["main", "HEAD", "abc", "a" * 40 + "; touch pwned", "$(id)"])
async def test_restore_only_takes_full_shas(
    plain_store: GitCheckpointStore, empty_box: FakeSandbox, sha: str
) -> None:
    with pytest.raises(ValueError, match="sha"):
        await plain_store.restore(empty_box, sha)
    assert list(empty_box.root.iterdir()) == [], "nothing ran in the sandbox"


async def test_a_timeout_in_the_sandbox_is_a_checkpoint_error(
    plain_store: GitCheckpointStore,
) -> None:
    class Hung(FakeSandbox):
        async def exec(self, cmd, *, timeout_s, env=None):  # type: ignore[no-untyped-def, override]
            return await super().exec("sleep 5", timeout_s=1)

    sandbox = Hung()
    try:
        with pytest.raises(CheckpointError, match="timed out"):
            await plain_store.commit(sandbox, "lh: edit_file turn 1 call c1")
    finally:
        await sandbox.destroy()


# ---------- bundles ----------


async def _two_commits(env: Env, box: FakeSandbox) -> str:
    await box.write_file(MODULE, FIXED.encode())
    assert await env.store.commit(box, "lh: edit_file turn 1 call c1")
    await box.write_file("NOTES.md", b"two\n")
    head = await env.store.commit(box, "lh: write_file turn 2 call c2")
    assert head is not None
    return head


async def test_bundle_is_stored_under_the_task_key(env: Env, box: FakeSandbox) -> None:
    head = await _two_commits(env, box)

    ref = await env.store.bundle(TASK)

    assert ref.key == f"tasks/{TASK}/ckpt/{head}.bundle"
    data = await env.blobs.get(ref)
    assert ref.size == len(data) > 0
    path = env.tmp_path / "x.bundle"
    path.write_bytes(data)
    git("bundle", "verify", str(path))
    # A bundle restored into an empty directory has the same head.
    empty = env.tmp_path / "empty"
    empty.mkdir()
    git("clone", "-q", str(path), str(empty))
    assert git("-C", str(empty), "rev-parse", "HEAD") == head
    assert git("-C", str(empty), "rev-parse", "refs/tags/base") == env.base_sha


async def test_bundle_follows_the_branch_even_if_the_sandbox_moved_head(
    env: Env, box: FakeSandbox
) -> None:
    head = await _two_commits(env, box)
    env.bare("symbolic-ref", "HEAD", "refs/heads/somewhere-else")

    ref = await env.store.bundle(TASK)

    assert ref.key.endswith(f"/{head}.bundle")
    assert env.bare("symbolic-ref", "HEAD") == f"refs/heads/{BRANCH}"


async def test_bundle_without_a_repo_is_a_named_error(tmp_path: Path) -> None:
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path)
    with pytest.raises(RepoNotFoundError, match=TASK):
        await store.bundle(TASK)
    assert not store.has_repo(TASK)


async def test_bundle_over_the_size_cap_is_refused(env: Env, box: FakeSandbox) -> None:
    await _two_commits(env, box)
    small = GitCheckpointStore(env.blobs, root=env.store.root, max_bundle_bytes=100)
    with pytest.raises(CheckpointError, match="larger than"):
        await small.bundle(TASK)
    assert env.blobs.objects == {}


async def test_restore_from_bundle_builds_a_repo_that_a_new_sandbox_can_use(
    env: Env, box: FakeSandbox
) -> None:
    head = await _two_commits(env, box)
    committed = snapshot(box.root)
    ref = await env.store.bundle(TASK)

    new_host = GitCheckpointStore(env.blobs, root=env.tmp_path / "new-host")
    assert not new_host.has_repo(TASK)
    assert await new_host.restore_from_bundle(TASK, ref) == head

    assert await new_host.head(TASK) == head
    assert new_host.has_repo(TASK)
    repo = new_host.repo_path(TASK)
    assert git(f"--git-dir={repo}", "symbolic-ref", "HEAD") == f"refs/heads/{BRANCH}"
    assert git(f"--git-dir={repo}", "rev-parse", "refs/tags/base") == env.base_sha
    assert git(f"--git-dir={repo}", "config", "longhaul.task-id") == TASK
    assert git(f"--git-dir={repo}", "config", "longhaul.base-sha") == env.base_sha
    assert "[remote" not in (repo / "config").read_text()
    assert sorted(p.name for p in new_host.root.iterdir()) == [f"{TASK}.git"]

    new_host.mount_point = str(repo)
    fresh = FakeSandbox()
    try:
        await new_host.attach(fresh, TASK)
        await new_host.restore(fresh, head)
        assert snapshot(fresh.root) == committed
    finally:
        await fresh.destroy()


async def test_restore_from_bundle_does_not_replace_an_existing_repo(
    env: Env, box: FakeSandbox
) -> None:
    await _two_commits(env, box)
    ref = await env.store.bundle(TASK)
    with pytest.raises(CheckpointError, match="already exists"):
        await env.store.restore_from_bundle(TASK, ref)


async def test_restore_from_bundle_rejects_a_bundle_that_lacks_the_task_branch(
    tmp_path: Path, env: Env
) -> None:
    path = tmp_path / "other.bundle"
    git("-C", str(env.source), "bundle", "create", str(path), "main")
    ref = await env.blobs.put(path.read_bytes(), key=f"tasks/{TASK}/ckpt/{'a' * 40}.bundle")
    store = GitCheckpointStore(env.blobs, root=tmp_path / "new-host")

    with pytest.raises(CheckpointError, match=BRANCH):
        await store.restore_from_bundle(TASK, ref)

    assert not store.has_repo(TASK)
    assert list((tmp_path / "new-host").iterdir()) == []


async def test_restore_from_bundle_stops_on_a_bundle_that_was_tampered_with(
    env: Env, box: FakeSandbox
) -> None:
    await _two_commits(env, box)
    ref = await env.store.bundle(TASK)
    env.blobs.objects[ref.key] = env.blobs.objects[ref.key][:-8] + b"tampered"
    store = GitCheckpointStore(env.blobs, root=env.tmp_path / "new-host")

    with pytest.raises(BlobIntegrityError):
        await store.restore_from_bundle(TASK, ref)

    assert not store.has_repo(TASK)


async def test_restore_from_bundle_refuses_a_ref_that_is_too_large(
    env: Env, box: FakeSandbox
) -> None:
    await _two_commits(env, box)
    ref = await env.store.bundle(TASK)
    small = GitCheckpointStore(
        env.blobs, root=env.tmp_path / "new-host", max_bundle_bytes=ref.size - 1
    )
    with pytest.raises(CheckpointError, match="larger than"):
        await small.restore_from_bundle(TASK, ref)


async def test_restore_from_bundle_needs_the_base_tag(env: Env, box: FakeSandbox) -> None:
    await _two_commits(env, box)
    path = env.tmp_path / "no-tag.bundle"
    env.bare("bundle", "create", "--quiet", str(path), f"refs/heads/{BRANCH}")
    ref = await env.blobs.put(path.read_bytes(), key=f"tasks/{TASK}/ckpt/{'b' * 40}.bundle")
    store = GitCheckpointStore(env.blobs, root=env.tmp_path / "new-host")

    with pytest.raises(CheckpointError, match="'base' tag"):
        await store.restore_from_bundle(TASK, ref)

    assert not store.has_repo(TASK)


async def test_bundle_stops_when_the_branch_moves_while_it_is_made(
    env: Env, box: FakeSandbox
) -> None:
    await _two_commits(env, box)

    class Moving(GitCheckpointStore):
        calls = 0

        async def head(self, task_id: str) -> str:
            self.calls += 1
            return await super().head(task_id) if self.calls == 1 else "f" * 40

    store = Moving(env.blobs, root=env.store.root)
    with pytest.raises(CheckpointError, match="moved"):
        await store.bundle(TASK)
    assert not any(key.endswith(".bundle") for key in env.blobs.objects)


# ---------- two attempts at once ----------


def test_publish_reports_a_repo_that_is_already_in_place(tmp_path: Path) -> None:
    scratch, final = tmp_path / "scratch", tmp_path / "final"
    scratch.mkdir()
    final.mkdir()
    (final / "HEAD").write_text("ref: refs/heads/x\n")
    assert GitCheckpointStore._publish(scratch, final) is False
    assert scratch.exists(), "the loser's clone is left for its owner to remove"


def test_publish_raises_when_the_move_fails_for_another_reason(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        GitCheckpointStore._publish(tmp_path / "missing", tmp_path / "final")


async def test_init_task_that_loses_the_race_keeps_the_winners_repo(
    env: Env, tmp_path: Path
) -> None:
    template_repo = env.repo

    class Late(GitCheckpointStore):
        @staticmethod
        def _publish(scratch: Path, final: Path) -> bool:
            # The other attempt finishes between this one's clone and its rename.
            shutil.copytree(template_repo, final, symlinks=True)
            return GitCheckpointStore._publish(scratch, final)

    root = tmp_path / "late-root"
    store = Late(env.blobs, root=root)

    path = await store.init_task(TASK, str(env.source), env.base_sha)

    assert path == str(root / f"{TASK}.git")
    assert await store.head(TASK) == env.base_sha
    assert sorted(p.name for p in root.iterdir()) == [f"{TASK}.git"], "the scratch clone is gone"


async def test_restore_from_bundle_that_loses_the_race_leaves_the_winners_repo(
    env: Env, box: FakeSandbox
) -> None:
    await _two_commits(env, box)
    ref = await env.store.bundle(TASK)
    root = env.tmp_path / "late-root"

    class Late(GitCheckpointStore):
        @staticmethod
        def _publish(scratch: Path, final: Path) -> bool:
            final.mkdir()
            (final / "HEAD").write_text("winner\n")
            return GitCheckpointStore._publish(scratch, final)

    with pytest.raises(CheckpointError, match="appeared"):
        await Late(env.blobs, root=root).restore_from_bundle(TASK, ref)

    assert (root / f"{TASK}.git" / "HEAD").read_text() == "winner\n"
    assert [p.name for p in root.iterdir()] == [f"{TASK}.git"]


# ---------- git on the host ----------


class HangingProcess:
    """Stands in for a git process that never ends."""

    returncode: int | None = None

    def __init__(self) -> None:
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(3600)
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return -9


@pytest.fixture
def hanging(monkeypatch: pytest.MonkeyPatch) -> HangingProcess:
    process = HangingProcess()

    async def start(*_argv: object, **_options: object) -> HangingProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    return process


async def test_a_git_command_that_hangs_is_killed_and_reported(hanging: HangingProcess) -> None:
    with pytest.raises(CheckpointError, match="timed out"):
        await git_command("version", timeout_s=0.05)
    assert hanging.killed


async def test_a_cancelled_git_command_is_killed_too(hanging: HangingProcess) -> None:
    task = asyncio.create_task(git_command("version"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hanging.killed


async def test_commit_output_that_is_not_a_result_is_an_error(
    plain_store: GitCheckpointStore,
) -> None:
    class Odd(FakeSandbox):
        output = ""

        async def exec(self, cmd, *, timeout_s, env=None):  # type: ignore[no-untyped-def, override]
            return ExecResult(
                exit_code=0, stdout=self.output, stderr="", timed_out=False, duration_ms=1
            )

    sandbox = Odd()
    try:
        for output in ("", "all done\n", "LH-SHA nothex\n"):
            sandbox.output = output
            with pytest.raises(CheckpointError, match="unexpected output"):
                await plain_store.commit(sandbox, "lh: edit_file turn 1 call c1")
    finally:
        await sandbox.destroy()


# ---------- the host side of a repo the sandbox can write ----------


async def test_host_git_ignores_hooks_and_config_that_the_sandbox_planted(
    env: Env, box: FakeSandbox
) -> None:
    await _two_commits(env, box)
    hooks = env.tmp_path / "planted-hooks"
    hooks.mkdir()
    marker = env.tmp_path / "hook-ran"
    hook = hooks / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    env.bare("config", "core.hooksPath", str(hooks))
    # Control: plain git does run the planted hook, so the checks below mean something.
    env.bare("update-ref", "refs/heads/control", env.base_sha)
    assert marker.exists(), "this git does not run reference-transaction hooks"
    marker.unlink()
    env.bare("update-ref", "-d", "refs/heads/control")
    marker.unlink(missing_ok=True)

    ref = await env.store.bundle(TASK)
    assert await env.store.head(TASK) == ref.key.rsplit("/", 1)[1].removesuffix(".bundle")

    assert not marker.exists()


# ---------- cleanup ----------


async def test_cleanup_removes_the_repo_even_when_its_files_are_read_only(
    env: Env, box: FakeSandbox
) -> None:
    await _two_commits(env, box)
    # Git writes objects read-only, and a folder can lose its write bit too.
    locked = env.repo / "objects" / "locked"
    locked.mkdir()
    (locked / "file").write_text("x")
    (locked / "file").chmod(0o444)
    locked.chmod(0o555)

    await env.store.cleanup_task(TASK)
    await env.store.cleanup_task(TASK)  # a second call changes nothing

    assert not env.repo.exists()
    assert not env.store.has_repo(TASK)
