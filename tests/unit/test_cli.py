"""The `longhaul run-local` command, with a fake sandbox manager and the scripted provider.

The real Docker run is tests/e2e/test_run_local.py. Here the manager hands out FakeSandboxes,
and the setup step that would build a venv from PyPI is answered without running, so no test
here needs Docker or a network. These tests are plain functions on purpose: the command starts
its own event loop with asyncio.run, which cannot happen inside a running one.
"""

import asyncio
import hashlib
import os
import re
import shlex
import signal
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import docker
import pytest
import typer
import yaml
from docker.errors import DockerException, ImageNotFound
from typer.testing import CliRunner, Result

from longhaul import cli
from longhaul.agent.loop import CallEvent, TurnEvent
from longhaul.agent.workspace import default_setup_command
from longhaul.contracts import (
    CallOptions,
    ExecResult,
    LLMResponse,
    Message,
    OutcomeKind,
    Role,
    Sandbox,
    SandboxLimits,
    ToolSpec,
)
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.settings import EnvSettings

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
SCRIPTS = ROOT / "tests" / "fixtures" / "scripts"
EXAMPLE_CONFIG = ROOT / "config" / "longhaul.example.yaml"

# The interpreter running this test, so it does not matter whether pytest is on PATH.
TEST_COMMAND = f"{shlex.quote(sys.executable)} -m pytest -q"
DIFF_MARKER = "final diff against base:"
FINISH_SUMMARY = "Changed add() to return a + b. All tests pass."

OK = ExecResult(exit_code=0, stdout="", stderr="", timed_out=False, duration_ms=1)
RUNNER = CliRunner()


def calc_args(script: str = "calc_fix.yaml", *extra: str) -> list[str]:
    return [
        "run-local",
        "--path",
        str(CALC),
        "--issue-file",
        str(CALC / "ISSUE.md"),
        "--provider",
        "scripted",
        "--script",
        str(SCRIPTS / script),
        "--test-command",
        TEST_COMMAND,
        *extra,
    ]


def invoke(args: list[str]) -> Result:
    return RUNNER.invoke(cli.app, args)


def assert_cleaned(manager: "FakeManager") -> None:
    """Every task was cleaned up, and its sandbox is gone. A failed preparation cleans up on its
    own and the command does it again, so the manager can see the call twice."""
    assert manager.task_ids, "no task reached the manager"
    assert set(manager.cleaned) == set(manager.task_ids)
    assert all(not box.root.exists() for box in manager.sandboxes)


def tree_hash(root: Path) -> str:
    """One hash for every file name and content under `root`."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


# --- fakes ---------------------------------------------------------------------------------


class StubSandbox(FakeSandbox):
    """A FakeSandbox that answers the venv setup without building a venv."""

    def __init__(self, sandbox_id: str, manager: "FakeManager") -> None:
        super().__init__(sandbox_id)
        self._manager = manager

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        self._manager.commands.append(cmd)
        if cmd.startswith("python -m venv"):
            return self._manager.setup_result
        return await super().exec(cmd, timeout_s=timeout_s, env=env)


class FakeManager:
    """Stands in for DockerSandboxManager and notes what the command asks of it."""

    def __init__(self) -> None:
        self.setup_result = OK
        self.create_error: Exception | None = None
        self.commands: list[str] = []
        self.task_ids: list[str] = []
        self.images: list[str] = []
        self.sandboxes: list[StubSandbox] = []
        self.network: dict[str, bool] = {}
        self.cleaned: list[str] = []

    async def create(
        self, task_id: str, image: str, limits: SandboxLimits, network: bool
    ) -> Sandbox:
        self.task_ids.append(task_id)
        if self.create_error is not None:
            raise self.create_error
        self.images.append(image)
        box = StubSandbox(f"fake-{task_id}", self)
        self.sandboxes.append(box)
        self.network[box.id] = network
        return box

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return next((s for s in self.sandboxes if s.id == sandbox_id), None)

    async def set_network(self, sandbox_id: str, enabled: bool) -> None:
        self.network[sandbox_id] = enabled

    async def cleanup_task(self, task_id: str) -> None:
        self.cleaned.append(task_id)
        for box in self.sandboxes:
            await box.destroy()


class Recorder(ScriptedProvider):
    """A ScriptedProvider that keeps the model id and the system prompt of each call."""

    def __init__(self, script: Path) -> None:
        super().__init__(script)
        self.models: list[str] = []
        self.systems: list[str] = []

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        cache_system: bool = True,
        options: CallOptions | None = None,
    ) -> LLMResponse:
        self.models.append(model)
        self.systems.append(system)
        return await super().complete(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            cache_system=cache_system,
            options=options,
        )


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No settings from the machine: not its environment, and no .env in the working directory."""
    for name in list(os.environ):
        if name.startswith("LONGHAUL_") or name in {
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
        }:
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def unhandled_signals_fail() -> Iterator[None]:
    """A SIGTERM or SIGHUP that nothing handles would end the test run, so make it an error."""

    def refuse(signum: int, frame: Any) -> None:
        raise AssertionError(f"signal {signum} was not handled by the command")

    saved = {sig: signal.signal(sig, refuse) for sig in (signal.SIGTERM, signal.SIGHUP)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> FakeManager:
    fake = FakeManager()
    monkeypatch.setattr(cli, "make_manager", lambda: fake)
    return fake


# --- a run that finishes -------------------------------------------------------------------


def test_a_scripted_run_on_calc_prints_each_turn_and_the_final_diff(manager: FakeManager) -> None:
    before = tree_hash(CALC)
    result = invoke(calc_args())
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert [ln for ln in lines if ln.startswith("turn ")] == [
        "turn 1 coder: list_dir .",
        "turn 2 coder: read_file src/calc/__init__.py",
        "turn 3 coder: edit_file src/calc/__init__.py",
        "turn 4 coder: run_tests",
        f"turn 5 coder: finish {FINISH_SUMMARY}",
    ]
    assert f"finished after 5 turns: {FINISH_SUMMARY}" in lines
    diff = result.stdout.split(f"{DIFF_MARKER}\n", 1)[1]
    assert "-    return a - b" in diff and "+    return a + b" in diff
    # The diff comes last, and it is only the one file the script edited.
    assert [ln for ln in diff.splitlines() if ln.startswith("diff --git")] == [
        "diff --git a/src/calc/__init__.py b/src/calc/__init__.py"
    ]
    assert tree_hash(CALC) == before, "the source folder was changed"


def test_the_header_names_the_task_the_repo_the_provider_and_the_model(
    manager: FakeManager,
) -> None:
    result = invoke(calc_args())
    first, second, third = result.stdout.splitlines()[:3]
    assert first == f"task {manager.task_ids[0]}: add() returns the difference"
    assert second == f"repo local:{CALC}, provider scripted, model claude-sonnet-5-5"
    assert third.startswith("workspace ready, base ")
    sha = third.removeprefix("workspace ready, base ")
    assert len(sha) == 7 and all(c in "0123456789abcdef" for c in sha)


def test_the_sandbox_is_prepared_with_the_default_setup_and_removed_at_the_end(
    manager: FakeManager,
) -> None:
    result = invoke(calc_args())
    assert result.exit_code == 0, result.output
    # setup_command=None, so the default venv setup runs, with the network on and then cut.
    assert default_setup_command(CALC) in manager.commands
    box = manager.sandboxes[0]
    assert manager.network == {box.id: False}
    assert manager.images == ["longhaul-sandbox:py312"]
    assert_cleaned(manager)
    assert not box.root.exists()


def test_the_task_ids_are_safe_docker_names_and_differ_between_runs(manager: FakeManager) -> None:
    invoke(calc_args())
    invoke(calc_args())
    first, second = manager.task_ids
    assert first != second
    for task_id in (first, second):
        assert re.fullmatch(r"lh-local-calc-[0-9a-f]{8}", task_id), task_id
    assert cli.new_task_id(Path("/x/My Repo! (copy)")).startswith("lh-local-my-repo-copy-")
    assert cli.new_task_id(Path("/")).startswith("lh-local-repo-")
    assert len(cli.new_task_id(Path("/" + "a" * 200))) < 60


def test_the_model_sees_the_issue_the_repo_and_the_test_command(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = Recorder(SCRIPTS / "calc_fix.yaml")
    monkeypatch.setattr(cli, "build_provider", lambda *_: seen)
    result = invoke(calc_args())
    assert result.exit_code == 0, result.output
    system = seen.systems[0]
    assert "Issue: add() returns the difference" in system
    assert "`calc.add` subtracts its arguments" in system
    # Only the folder name reaches the model: the absolute path carries the user name.
    assert f"Repository: local:{CALC.name}" in system
    assert str(CALC.parent) not in system
    assert f"Test command: {TEST_COMMAND}" in system
    # The reference fix is not in the prompt: the issue file does not carry it.
    assert "return a + b" not in system
    assert set(seen.models) == {"claude-sonnet-5-5"}


def test_the_default_test_command_is_python_dash_m_pytest(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = Recorder(SCRIPTS / "loop_always_idle.yaml")
    monkeypatch.setattr(cli, "build_provider", lambda *_: seen)
    args = [a for a in calc_args() if a not in ("--test-command", TEST_COMMAND)]
    invoke(args)
    assert "Test command: python -m pytest -q" in seen.systems[0]
    assert cli.DEFAULT_TEST_COMMAND == "python -m pytest -q"


def test_the_model_option_replaces_the_configured_model(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = Recorder(SCRIPTS / "calc_fix.yaml")
    monkeypatch.setattr(cli, "build_provider", lambda *_: seen)
    result = invoke(calc_args("calc_fix.yaml", "--model", "my-model"))
    assert result.exit_code == 0, result.output
    assert set(seen.models) == {"my-model"}
    assert "model my-model" in result.stdout.splitlines()[1]


def test_the_model_comes_from_the_config_named_by_the_environment(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    raw["models"]["coder"] = "coder-from-my-config"
    config = tmp_path / "mine.yaml"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("LONGHAUL_CONFIG", str(config))
    seen = Recorder(SCRIPTS / "calc_fix.yaml")
    monkeypatch.setattr(cli, "build_provider", lambda *_: seen)
    assert invoke(calc_args()).exit_code == 0
    assert set(seen.models) == {"coder-from-my-config"}


def test_the_provider_and_the_script_can_come_from_the_environment(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LONGHAUL_PROVIDER", "scripted")
    monkeypatch.setenv("LONGHAUL_SCRIPT", str(SCRIPTS / "calc_fix.yaml"))
    args = [
        "run-local",
        "--path",
        str(CALC),
        "--issue-file",
        str(CALC / "ISSUE.md"),
        "--test-command",
        TEST_COMMAND,
    ]
    result = invoke(args)
    assert result.exit_code == 0, result.output
    assert f"finished after 5 turns: {FINISH_SUMMARY}" in result.stdout


def test_the_config_is_found_from_any_working_directory() -> None:
    # The fixture put the working directory in a folder with no config/ in it.
    assert not Path("config").exists()
    found = cli.config_path(EnvSettings())
    assert found.resolve() == EXAMPLE_CONFIG.resolve()


def test_a_config_in_the_working_directory_wins(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    local = tmp_path / "config" / "longhaul.example.yaml"
    local.write_text("{}", encoding="utf-8")
    assert cli.config_path(EnvSettings()) == Path("config/longhaul.example.yaml")
    assert local.exists()


# --- a run that stops short ----------------------------------------------------------------


def test_the_turn_limit_exits_2_and_still_prints_the_diff(manager: FakeManager) -> None:
    result = invoke(calc_args("loop_never_finishes.yaml", "--max-turns", "3"))
    assert result.exit_code == 2, result.output
    assert "stopped after 3 turns: turn limit" in result.stdout.splitlines()
    assert result.stdout.count("\nturn ") == 3
    assert f"{DIFF_MARKER}\nno changes against base" in result.stdout
    assert_cleaned(manager)


def test_no_tool_calls_twice_exits_2(manager: FakeManager) -> None:
    result = invoke(calc_args("loop_always_idle.yaml"))
    assert result.exit_code == 2, result.output
    assert "stopped after 2 turns: no tool calls" in result.stdout
    assert "turn 1 coder: (no tool calls; nudged)" in result.stdout
    assert_cleaned(manager)


def test_max_turns_must_be_a_positive_number(manager: FakeManager) -> None:
    result = invoke(calc_args("calc_fix.yaml", "--max-turns", "0"))
    assert result.exit_code == 2
    assert manager.task_ids == []


def test_a_failed_call_is_marked_in_the_turn_line(manager: FakeManager) -> None:
    # The first call has no path, so the registry answers with an error and the run goes on.
    result = invoke(calc_args("loop_tool_error.yaml"))
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert "turn 1 coder: read_file [failed]" in lines
    assert "turn 2 coder: finish Recovered after a bad call." in lines


# --- errors --------------------------------------------------------------------------------


def test_a_provider_error_exits_2_with_one_line_and_cleans_up(manager: FakeManager) -> None:
    result = invoke(calc_args("loop_overloaded.yaml"))
    assert result.exit_code == 2, result.output
    lines = result.stderr.splitlines()
    assert any(ln.startswith("error: the model provider failed (overloaded):") for ln in lines)
    assert_cleaned(manager)
    assert "Traceback" not in result.output


def test_a_failing_setup_exits_2_with_the_output_tail_and_cleans_up(manager: FakeManager) -> None:
    manager.setup_result = ExecResult(
        exit_code=1,
        stdout="Collecting pytest",
        stderr="ERROR: no matching distribution found for pytest",
        timed_out=False,
        duration_ms=1,
    )
    result = invoke(calc_args())
    assert result.exit_code == 2, result.output
    assert "setup command failed with exit code 1" in result.stderr
    assert "ERROR: no matching distribution found for pytest" in result.stderr
    assert "workspace ready" not in result.stdout
    assert_cleaned(manager)


def test_a_bad_ignore_file_exits_2_before_any_sandbox_exists(
    manager: FakeManager, tmp_path: Path
) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n")
    (source / ".longhaulignore").write_text("[z-a]\n")
    issue = tmp_path / "issue.md"
    issue.write_text("# bug\n\nit breaks\n")
    args = ["run-local", "--path", str(source), "--issue-file", str(issue)]
    result = invoke([*args, "--provider", "scripted", "--script", str(SCRIPTS / "calc_fix.yaml")])
    assert result.exit_code == 2
    assert "[z-a]" in result.stderr
    assert manager.task_ids == []


def test_the_scripted_provider_without_a_script_exits_2_before_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_docker() -> Any:
        raise AssertionError("docker was touched")

    monkeypatch.setattr(cli, "make_manager", no_docker)
    args = ["run-local", "--path", str(CALC), "--issue-file", str(CALC / "ISSUE.md")]
    result = invoke([*args, "--provider", "scripted"])
    assert result.exit_code == 2
    assert "script" in result.stderr


def test_anthropic_without_a_key_exits_2_before_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_docker() -> Any:
        raise AssertionError("docker was touched")

    monkeypatch.setattr(cli, "make_manager", no_docker)
    args = ["run-local", "--path", str(CALC), "--issue-file", str(CALC / "ISSUE.md")]
    result = invoke([*args, "--provider", "anthropic"])
    assert result.exit_code == 2
    assert "ANTHROPIC_API_KEY" in result.stderr


def test_a_provider_that_is_not_built_yet_exits_2_before_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_docker() -> Any:
        raise AssertionError("docker was touched")

    monkeypatch.setattr(cli, "make_manager", no_docker)
    monkeypatch.setenv("LONGHAUL_PROVIDER", "replay")  # a known name that has no provider yet
    args = ["run-local", "--path", str(CALC), "--issue-file", str(CALC / "ISSUE.md")]
    result = invoke(args)
    assert result.exit_code == 2
    assert "not implemented yet" in result.stderr


def test_a_missing_config_exits_2_and_says_where_to_set_it(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LONGHAUL_CONFIG", str(tmp_path / "nope.yaml"))
    result = invoke(calc_args())
    assert result.exit_code == 2
    assert "nope.yaml" in result.stderr
    assert manager.task_ids == []


def test_an_issue_file_with_no_text_exits_2(manager: FakeManager, tmp_path: Path) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("  \n\n")
    args = calc_args()
    args[args.index("--issue-file") + 1] = str(empty)
    result = invoke(args)
    assert result.exit_code == 2
    assert "empty.md" in result.stderr
    assert manager.task_ids == []


def test_docker_that_cannot_be_reached_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable() -> Any:
        raise DockerException("Error while fetching server API version: connection refused")

    monkeypatch.setattr(cli, "make_manager", unreachable)
    result = invoke(calc_args())
    assert result.exit_code == 2
    assert "docker: Error while fetching server API version" in result.stderr


def test_a_missing_sandbox_image_says_how_to_build_it(manager: FakeManager) -> None:
    manager.create_error = ImageNotFound("No such image: longhaul-sandbox:py312")
    result = invoke(calc_args())
    assert result.exit_code == 2
    assert "No such image: longhaul-sandbox:py312" in result.stderr
    assert "make sandbox-image" in result.stderr


def test_an_unexpected_error_cleans_up_and_is_not_swallowed(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("a bug in the loop")

    monkeypatch.setattr(cli, "run_loop", broken)
    result = invoke(calc_args())
    # A crash is not a failed run: it keeps its traceback and exits 1, apart from the 2 of a run
    # that ended in error.
    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert_cleaned(manager)


@pytest.mark.parametrize(
    "sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT], ids=["sigterm", "sighup", "sigint"]
)
@pytest.mark.usefixtures("unhandled_signals_fail")
def test_a_signal_stops_the_run_and_still_removes_the_sandbox(
    sig: signal.Signals, manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ctrl-C is handled by asyncio.run. SIGTERM (kill) and SIGHUP (a closed terminal) are
    # handled by the command, which turns them into the same cancel.
    async def stuck(*_: Any, **__: Any) -> Any:
        os.kill(os.getpid(), sig)
        await asyncio.sleep(30)

    monkeypatch.setattr(cli, "run_loop", stuck)
    result = invoke(calc_args())
    assert result.exit_code == 2, result.output
    assert "interrupted" in result.stderr
    assert_cleaned(manager)


def test_a_final_diff_that_cannot_be_read_is_reported_and_does_not_hide_the_outcome(
    manager: FakeManager, tmp_path: Path
) -> None:
    # The script deletes the tag that the diff is taken against, then finishes.
    script = tmp_path / "break_the_base.yaml"
    script.write_text(
        "steps:\n"
        "  - respond:\n"
        "      tool_calls:\n"
        "        - {name: run_command, input: {command: 'git tag -d base'}}\n"
        "  - respond:\n"
        "      tool_calls:\n"
        "        - {name: finish, input: {summary: 'Removed the tag.'}}\n",
        encoding="utf-8",
    )
    result = invoke(calc_args(str(script)))
    assert result.exit_code == 0, result.output
    assert "finished after 2 turns: Removed the tag." in result.stdout
    assert DIFF_MARKER not in result.stdout
    assert "could not read the final diff: git diff failed" in result.stderr


def test_a_cleanup_that_fails_is_reported_and_does_not_change_the_result(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(task_id: str) -> None:
        raise RuntimeError("volume is in use")

    monkeypatch.setattr(manager, "cleanup_task", broken)
    result = invoke(calc_args())
    assert result.exit_code == 0, result.output
    assert "could not remove the sandbox of task" in result.stderr
    assert "volume is in use" in result.stderr
    assert manager.task_ids[0] in result.stderr


# --- usage ---------------------------------------------------------------------------------


def test_no_arguments_print_the_help_and_name_the_command() -> None:
    result = invoke([])
    assert "run-local" in result.output


# Rich styles help text when it detects CI (GITHUB_ACTIONS) or FORCE_COLOR, and the escape
# codes split option names, so compare the help as plain text.
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def test_run_local_help_lists_every_option() -> None:
    result = invoke(["run-local", "--help"])
    assert result.exit_code == 0
    text = ANSI.sub("", result.output)
    for option in (
        "--path",
        "--issue-file",
        "--provider",
        "--script",
        "--model",
        "--test-command",
        "--max-turns",
    ):
        assert option in text
    # Rich wraps the list of choices inside a narrow column, so read them from the enum.
    assert [p.value for p in cli.ProviderName] == ["scripted", "anthropic", "openai"]


@pytest.mark.parametrize(
    "args",
    [
        ["run-local"],
        ["run-local", "--path", "/no/such/folder", "--issue-file", "/no/such/file"],
        ["run-local", "--path", str(CALC)],
        ["run-local", "--path", str(CALC), "--issue-file", str(CALC)],  # a folder, not a file
        ["run-local", "--path", str(CALC / "ISSUE.md"), "--issue-file", str(CALC / "ISSUE.md")],
        [
            "run-local",
            "--path",
            str(CALC),
            "--issue-file",
            str(CALC / "ISSUE.md"),
            "--provider",
            "x",
        ],
    ],
    ids=["nothing", "missing-paths", "no-issue", "issue-is-folder", "path-is-file", "bad-provider"],
)
def test_bad_usage_exits_2_without_touching_docker(
    args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_docker() -> Any:
        raise AssertionError("docker was touched")

    monkeypatch.setattr(cli, "make_manager", no_docker)
    result = invoke(args)
    assert result.exit_code == 2, result.output


def test_make_manager_gives_the_docker_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker, "from_env", lambda: object())  # no daemon is needed to build it
    assert isinstance(cli.make_manager(), DockerSandboxManager)


def test_the_module_runs_as_a_script() -> None:
    # A child interpreter does not get pytest's pythonpath, and on macOS Python skips an
    # editable-install .pth file that carries the hidden flag, so name src explicitly.
    src = str(Path(__file__).resolve().parents[2] / "src")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")])),
    }
    done = subprocess.run(
        [sys.executable, "-m", "longhaul.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    assert "run-local" in done.stdout


def test_the_project_script_entry_points_at_the_app() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["scripts"] == {"longhaul": "longhaul.cli:app"}
    assert isinstance(cli.app, typer.Typer)


# --- the pieces ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "title", "body"),
    [
        (
            "# add() returns the difference\n\nIt subtracts.\n",
            "add() returns the difference",
            "It subtracts.",
        ),
        ("## Two hashes\nbody", "Two hashes", "body"),
        ("Plain first line\n\nMore text\nand more\n", "Plain first line", "More text\nand more"),
        ("\n\n  # Leading blank lines  \n\nbody\n", "Leading blank lines", "body"),
        ("# Windows\r\n\r\nline one\r\nline two\r\n", "Windows", "line one\nline two"),
        ("﻿# With a byte order mark\nbody", "With a byte order mark", "body"),
        ("Only a title", "Only a title", ""),
        ("# Title\n\n\n\n", "Title", ""),
    ],
    ids=["heading", "h2", "plain", "blank-lines", "crlf", "bom", "title-only", "trailing-blanks"],
)
def test_split_issue(text: str, title: str, body: str) -> None:
    assert cli.split_issue(text) == (title, body)


@pytest.mark.parametrize("text", ["", "   \n\n  ", "#", "# \n\nbody"])
def test_an_issue_without_a_title_is_an_error(text: str) -> None:
    with pytest.raises(ValueError, match="title"):
        cli.split_issue(text)


def test_read_issue_reads_utf8_and_reports_the_file(tmp_path: Path) -> None:
    issue = tmp_path / "bug.md"
    issue.write_text("# Crème brûlée\n\nAccents survive.\n", encoding="utf-8")
    assert cli.read_issue(issue) == ("Crème brûlée", "Accents survive.")
    issue.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(ValueError, match=r"bug\.md.*UTF-8"):
        cli.read_issue(issue)
    (tmp_path / "blank.md").write_text("\n")
    with pytest.raises(ValueError, match=r"blank\.md"):
        cli.read_issue(tmp_path / "blank.md")


def event(*calls: CallEvent, note: str | None = None, index: int = 2) -> TurnEvent:
    return TurnEvent(turn_index=index, role=Role.CODER, calls=calls, note=note)


@pytest.mark.parametrize(
    ("turn", "line"),
    [
        (
            event(CallEvent("edit_file", "src/calc/__init__.py", True)),
            "turn 3 coder: edit_file src/calc/__init__.py",
        ),
        (event(CallEvent("run_tests", "", True), index=0), "turn 1 coder: run_tests"),
        (
            event(CallEvent("read_file", "a.py", True), CallEvent("read_file", "b.py", True)),
            "turn 3 coder: read_file a.py, read_file b.py",
        ),
        (
            event(CallEvent("edit_file", "a.py", False)),
            "turn 3 coder: edit_file a.py [failed]",
        ),
        (event(note="no tool calls; nudged"), "turn 3 coder: (no tool calls; nudged)"),
        (
            event(CallEvent("list_dir", ".", True), note="retried at max_tokens 64000"),
            "turn 3 coder: list_dir . (retried at max_tokens 64000)",
        ),
        (event(), "turn 3 coder: (no tool calls)"),
    ],
)
def test_format_turn(turn: TurnEvent, line: str) -> None:
    assert cli.format_turn(turn) == line


def test_the_outcome_line_counts_turns_in_the_singular_too() -> None:
    assert cli.outcome_line(OutcomeKind.PR_OPENED, 1, "done") == "finished after 1 turn: done"
    assert (
        cli.outcome_line(OutcomeKind.ERROR, 4, "turn limit") == "stopped after 4 turns: turn limit"
    )
