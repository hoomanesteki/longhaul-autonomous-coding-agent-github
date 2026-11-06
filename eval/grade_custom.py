"""Grade the patch of an agent on a custom task (plan section 8.2, LH-901).

The grader starts a fresh sandbox at the `base_sha` of the task, applies the patch, copies in the
hidden tests, and runs the repo's own tests and then the hidden ones. The task is resolved when
both runs pass. Nothing of the repo runs on the host. The host builds the base commit with git,
which only stores files, and everything else happens in the sandbox.

The sandbox is set up before the patch is applied, with the network on, and then cut off. That is
the state the agent worked in: its sandbox had the packages of the base commit and no network.
A failed setup is the grader's problem and not the patch's, so it raises GraderError and the
runner records the task as not graded. A patch that does not apply, or that breaks a test, is an
unresolved result with a reason.

`python eval/grade_custom.py TASK_ID --patch FILE` grades a patch by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # started as `python eval/grade_custom.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval import custom_tasks  # noqa: E402
from longhaul.agent.workspace import (  # noqa: E402
    DEFAULT_TEST_COMMAND,
    WorkspaceManager,
    pip_install_command,
)
from longhaul.checkpoints.git_store import check_task_id  # noqa: E402
from longhaul.contracts import ExecResult, Sandbox, SandboxLimits  # noqa: E402

log = logging.getLogger(__name__)

SETUP_TIMEOUT_S = 900
TEST_TIMEOUT_S = 600
GIT_TIMEOUT_S = 120
TAIL_LINES = 40
TAIL_CHARS = 4_000

# Inside /workspace, which is the only place the file API reaches. It stays out of `git status`.
SCRATCH = ".grading"
BUNDLE_PATH = f"{SCRATCH}/base.bundle"
PATCH_PATH = f"{SCRATCH}/agent.patch"
HIDDEN_DIR = f"{SCRATCH}/hidden"

_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")
_TIMING = re.compile(r"\bin \d+(?:\.\d+)?s\b")


class GraderError(RuntimeError):
    """The grader could not do its job. This says nothing about the patch."""


@dataclass(frozen=True)
class GradeResult:
    resolved: bool
    reason: str
    hidden_passed: int = 0
    hidden_failed: int = 0
    repo_passed: int = 0
    repo_failed: int = 0
    output_tail: str = ""


@dataclass(frozen=True)
class SuiteRun:
    passed: int
    failed: int
    timed_out: bool
    tail: str

    @property
    def ok(self) -> bool:
        return self.passed > 0 and self.failed == 0 and not self.timed_out


def parse_counts(output: str) -> tuple[int, int]:
    """(passed, failed) from the closing line of a pytest run. An error counts as a failure."""
    for line in reversed(output.splitlines()):
        if _TIMING.search(line) and _COUNT.search(line):
            counts: dict[str, int] = {}
            for number, word in _COUNT.findall(line):
                key = "passed" if word == "passed" else "failed"
                counts[key] = counts.get(key, 0) + int(number)
            return counts.get("passed", 0), counts.get("failed", 0)
    return 0, 0


def tail_of(result: ExecResult) -> str:
    text = (result.stdout + "\n" + result.stderr).strip()
    return "\n".join(text.splitlines()[-TAIL_LINES:])[-TAIL_CHARS:]


def summarize_run(result: ExecResult) -> SuiteRun:
    passed, failed = parse_counts(result.stdout + "\n" + result.stderr)
    # A run that crashed before it counted anything is a failure, not an empty success.
    if result.exit_code != 0 and failed == 0:
        failed = 1
    return SuiteRun(passed, failed, result.timed_out, tail_of(result))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def describe(hidden: SuiteRun, repo: SuiteRun) -> str:
    if hidden.ok and repo.ok:
        return f"all tests passed ({hidden.passed} hidden, {repo.passed} repo)"
    problems: list[str] = []
    if hidden.timed_out or repo.timed_out:
        problems.append("a test run timed out")
    if hidden.failed:
        problems.append(f"{_plural(hidden.failed, 'hidden test')} failed")
    if repo.failed:
        problems.append(f"{_plural(repo.failed, 'repo test')} failed")
    if not problems:
        problems.append("no tests ran")
    return ", ".join(problems)


class Grader:
    """Grades patches in sandboxes of its own. One sandbox per patch, removed afterwards."""

    def __init__(
        self,
        manager: WorkspaceManager,
        *,
        image: str,
        limits: SandboxLimits,
        setup_timeout_s: int = SETUP_TIMEOUT_S,
        test_timeout_s: int = TEST_TIMEOUT_S,
    ) -> None:
        self._manager = manager
        self._image = image
        self._limits = limits
        self._setup_timeout_s = setup_timeout_s
        self._test_timeout_s = test_timeout_s

    async def grade(
        self, task: custom_tasks.CustomTask, patch: str, *, name: str | None = None
    ) -> GradeResult:
        """Grade `patch` against the hidden tests of `task`.

        `name` is the task id of the sandbox. Callers that clean up by prefix pass one that
        starts with their own prefix. Raises GraderError when the grading itself fails.
        """
        if not patch.strip():
            return GradeResult(False, "empty patch: the agent changed nothing")
        return await self._run(task, patch, name)

    async def grade_base(
        self, task: custom_tasks.CustomTask, *, name: str | None = None
    ) -> GradeResult:
        """Run the repo tests and the hidden tests on the base commit, with no patch.

        A task is only worth setting if its hidden tests fail here and its own tests pass.
        """
        return await self._run(task, None, name)

    async def _run(
        self, task: custom_tasks.CustomTask, patch: str | None, name: str | None
    ) -> GradeResult:
        sandbox_name = check_task_id(name or f"lh-grade-{task.id}-{uuid.uuid4().hex[:8]}")
        try:
            bundle = await asyncio.to_thread(custom_tasks.base_bundle, task)
        except ValueError as exc:
            raise GraderError(str(exc)) from exc
        sandbox = await self._manager.create(sandbox_name, self._image, self._limits, network=True)
        try:
            return await self._grade_in(sandbox, task, patch, bundle)
        finally:
            try:
                await self._manager.cleanup_task(sandbox_name)
            except Exception as exc:  # the result is more useful than a failed cleanup
                log.warning("could not remove the grading sandbox %s: %s", sandbox_name, exc)

    async def _grade_in(
        self, sandbox: Sandbox, task: custom_tasks.CustomTask, patch: str | None, bundle: bytes
    ) -> GradeResult:
        await self._check_out_base(sandbox, task, bundle)
        await self._set_up(sandbox)
        await self._manager.set_network(sandbox.id, False)
        if patch is not None:
            refused = await self._apply(sandbox, patch)
            if refused is not None:
                return refused
        hidden_file = custom_tasks.hidden_tests_path(task)
        await sandbox.write_file(f"{HIDDEN_DIR}/{hidden_file.name}", hidden_file.read_bytes())
        repo = summarize_run(
            await sandbox.exec(DEFAULT_TEST_COMMAND, timeout_s=self._test_timeout_s)
        )
        hidden_command = f"{DEFAULT_TEST_COMMAND} {HIDDEN_DIR}/{hidden_file.name}"
        hidden = summarize_run(await sandbox.exec(hidden_command, timeout_s=self._test_timeout_s))
        shown = [f"--- hidden tests ---\n{hidden.tail}"] if not hidden.ok else []
        if not repo.ok:
            shown.append(f"--- repo tests ---\n{repo.tail}")
        return GradeResult(
            resolved=hidden.ok and repo.ok,
            reason=describe(hidden, repo),
            hidden_passed=hidden.passed,
            hidden_failed=hidden.failed,
            repo_passed=repo.passed,
            repo_failed=repo.failed,
            output_tail="\n".join(shown),
        )

    async def _apply(self, sandbox: Sandbox, patch: str) -> GradeResult | None:
        """Apply the patch. None when it applied, else the result that says why not."""
        text = patch if patch.endswith("\n") else patch + "\n"
        await sandbox.write_file(PATCH_PATH, text.encode("utf-8"))
        applied = await sandbox.exec(
            f"git apply --whitespace=nowarn {PATCH_PATH}", timeout_s=GIT_TIMEOUT_S
        )
        if applied.exit_code == 0:
            return None
        detail = " ".join(applied.stderr.split())[:300]
        return GradeResult(False, f"the patch does not apply: {detail}")

    async def _check_out_base(
        self, sandbox: Sandbox, task: custom_tasks.CustomTask, bundle: bytes
    ) -> None:
        await sandbox.write_file(BUNDLE_PATH, bundle)
        steps = (
            "git init -q -b main",
            f"git fetch -q {BUNDLE_PATH} main",
            "git checkout -q --detach FETCH_HEAD",
            f"printf '{SCRATCH}/\\n' >> .git/info/exclude",
        )
        for step in steps:
            done = await sandbox.exec(step, timeout_s=GIT_TIMEOUT_S)
            if done.exit_code != 0:
                raise GraderError(f"{step!r} failed in the grading sandbox: {tail_of(done)}")
        head = await sandbox.exec("git rev-parse HEAD", timeout_s=GIT_TIMEOUT_S)
        if head.stdout.strip() != task.base_sha:
            raise GraderError(f"the sandbox is at {head.stdout.strip()}, not at {task.base_sha}")

    async def _set_up(self, sandbox: Sandbox) -> None:
        try:
            pyproject = (await sandbox.read_file("pyproject.toml")).decode("utf-8")
        except FileNotFoundError:
            return  # nothing to install
        done = await sandbox.exec(pip_install_command(pyproject), timeout_s=self._setup_timeout_s)
        if done.exit_code != 0 or done.timed_out:
            raise GraderError(f"the setup failed in the grading sandbox: {tail_of(done)}")


def main(argv: list[str] | None = None) -> int:
    from longhaul.sandbox.docker_manager import DockerSandboxManager
    from longhaul.settings import EnvSettings, config_path, load_config

    parser = argparse.ArgumentParser(description="Grade a patch on a custom task.")
    parser.add_argument("task_id", help="for example calc-01")
    parser.add_argument("--patch", type=Path, required=True, help="a unified diff, as git makes it")
    args = parser.parse_args(argv)
    tasks = {t.id: t for t in custom_tasks.load_tasks()}
    if args.task_id not in tasks:
        parser.error(f"unknown task {args.task_id}; the ids are {', '.join(sorted(tasks))}")
    sandbox_config = load_config(config_path(EnvSettings())).sandbox
    grader = Grader(
        DockerSandboxManager(), image=sandbox_config.image, limits=sandbox_config.limits
    )
    graded = asyncio.run(grader.grade(tasks[args.task_id], args.patch.read_text(encoding="utf-8")))
    print(f"{'resolved' if graded.resolved else 'not resolved'}: {graded.reason}")
    if graded.output_tail:
        print(graded.output_tail)
    return 0 if graded.resolved else 1


if __name__ == "__main__":
    raise SystemExit(main())
