import re
from collections.abc import AsyncIterator

import pytest

from longhaul.contracts import ExecResult, ToolCall, ToolContext
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.exec import (
    DEFAULT_MAX_TIMEOUT_S,
    DEFAULT_TIMEOUT_S,
    HEAD_CHARS,
    MAX_STREAM_CHARS,
    TAIL_CHARS,
    RunCommand,
    RunTests,
    build_test_command,
    cap_text,
    clamp_timeout,
)
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")


def exec_result(
    stdout: str = "", stderr: str = "", code: int = 0, timed_out: bool = False
) -> ExecResult:
    return ExecResult(
        exit_code=code, stdout=stdout, stderr=stderr, timed_out=timed_out, duration_ms=5
    )


class StubSandbox:
    """Replays one canned result and remembers what it was asked to run."""

    id = "stub"
    workdir = "/workspace"

    def __init__(self, result: ExecResult | None = None) -> None:
        self.result = result or exec_result()
        self.calls: list[tuple[str, int]] = []

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        self.calls.append((cmd, timeout_s))
        return self.result

    async def read_file(self, path: str) -> bytes:
        raise AssertionError("unexpected read_file")

    async def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError("unexpected write_file")

    async def destroy(self) -> None:
        return None


@pytest.fixture
async def sbx() -> AsyncIterator[FakeSandbox]:
    s = FakeSandbox()
    yield s
    await s.destroy()


# ---------- output cap ----------


def test_cap_constants_match_the_story() -> None:
    assert (HEAD_CHARS, TAIL_CHARS, MAX_STREAM_CHARS) == (8_000, 12_000, 20_000)


def test_cap_text_leaves_text_at_the_limit_alone() -> None:
    assert cap_text("") == ""
    assert cap_text("x" * MAX_STREAM_CHARS) == "x" * MAX_STREAM_CHARS


def test_cap_text_says_how_many_characters_were_cut() -> None:
    one_over = cap_text("x" * (MAX_STREAM_CHARS + 1))
    assert "[output truncated: 1 characters cut]" in one_over
    long = cap_text("x" * 50_000)
    assert "[output truncated: 30000 characters cut]" in long


def test_cap_text_keeps_the_head_and_the_tail() -> None:
    text = "H" * HEAD_CHARS + "M" * 30_000 + "T" * TAIL_CHARS
    out = cap_text(text)
    head, marker, tail = out.partition("\n[output truncated: 30000 characters cut]\n")
    assert head == "H" * HEAD_CHARS
    assert tail == "T" * TAIL_CHARS
    assert marker
    assert "M" not in out


# ---------- timeout clamp and command building ----------


@pytest.mark.parametrize(
    ("requested", "maximum", "want"),
    [
        (None, 600, DEFAULT_TIMEOUT_S),
        (None, 120, 120),
        (45, 600, 45),
        (9999, 600, 600),
        (0, 600, 1),
        (-5, 600, 1),
        (30.0, 600, 30),
        (10, 0, 1),
    ],
)
def test_clamp_timeout(requested: float | None, maximum: int, want: int) -> None:
    assert clamp_timeout(requested, maximum) == want


@pytest.mark.parametrize(
    ("target", "want"),
    [
        (None, "pytest -q"),
        ("", "pytest -q"),
        ("   ", "pytest -q"),
        ("tests/test_calc.py", "pytest -q tests/test_calc.py"),
        ("tests/test_calc.py::test_add", "pytest -q tests/test_calc.py::test_add"),
        ("tests/a.py tests/b.py", "pytest -q tests/a.py tests/b.py"),
        ("-k 'add and not slow'", "pytest -q -k 'add and not slow'"),
        ('"tests/my file.py"', "pytest -q 'tests/my file.py'"),
        ("x; rm -rf /", "pytest -q 'x;' rm -rf /"),
        ("$(whoami)", "pytest -q '$(whoami)'"),
    ],
)
def test_build_test_command(target: str | None, want: str) -> None:
    assert build_test_command("pytest -q", target) == want


def test_build_test_command_rejects_unbalanced_quotes() -> None:
    with pytest.raises(ValueError, match="quotation"):
        build_test_command("pytest -q", "tests/'oops")


# ---------- run_command ----------


async def test_run_command_reports_exit_code_and_both_streams(sbx: FakeSandbox) -> None:
    r = await RunCommand().run(sbx, {"command": "echo hi; echo oops >&2; exit 3"}, CTX)
    assert r.ok
    assert r.exit_code == 3
    lines = r.output.splitlines()
    assert lines[0] == "exit code: 3"
    assert "stdout:" in lines and "hi" in lines
    assert "stderr:" in lines and "oops" in lines


async def test_run_command_with_no_output_says_so(sbx: FakeSandbox) -> None:
    r = await RunCommand().run(sbx, {"command": "true"}, CTX)
    assert r.ok and r.exit_code == 0
    assert r.output == "exit code: 0\n(no output)"


async def test_a_failing_command_is_not_a_tool_error(sbx: FakeSandbox) -> None:
    # Only a timeout counts as a tool error. A failing test run is normal work.
    r = await RunCommand().run(sbx, {"command": "exit 1"}, CTX)
    assert r.ok and r.exit_code == 1


async def test_run_command_caps_each_stream_on_its_own() -> None:
    big = "H" * HEAD_CHARS + "M" * 30_000 + "T" * TAIL_CHARS
    sandbox = StubSandbox(exec_result(stdout=big + "\n", stderr="E" * 25_000, code=1))
    r = await RunCommand().run(sandbox, {"command": "noisy"}, CTX)
    assert r.ok
    assert "[output truncated: 30000 characters cut]" in r.output
    assert "[output truncated: 5000 characters cut]" in r.output
    assert "M" not in r.output
    assert "H" * HEAD_CHARS in r.output and "T" * TAIL_CHARS in r.output
    assert "E" * HEAD_CHARS in r.output and "E" * (TAIL_CHARS + 1) not in r.output


async def test_run_command_leaves_short_output_untouched() -> None:
    sandbox = StubSandbox(exec_result(stdout="line one\nline two\n"))
    r = await RunCommand().run(sandbox, {"command": "x"}, CTX)
    assert r.output == "exit code: 0\nstdout:\nline one\nline two"
    assert "truncated" not in r.output


async def test_run_command_timeout_is_an_error_with_a_hint(sbx: FakeSandbox) -> None:
    r = await RunCommand(max_timeout_s=60).run(sbx, {"command": "sleep 30", "timeout_s": 1}, CTX)
    assert not r.ok
    assert r.exit_code == 137
    assert r.output.startswith("timed out after 1 s and was killed.")
    assert "raise timeout_s (max 60)" in r.output


async def test_run_command_timeout_keeps_the_output_printed_before_the_kill() -> None:
    sandbox = StubSandbox(
        exec_result(stdout="started\n", stderr="warn\n", code=137, timed_out=True)
    )
    r = await RunCommand().run(sandbox, {"command": "slow", "timeout_s": 5}, CTX)
    assert not r.ok
    assert r.output.splitlines()[1:] == ["stdout:", "started", "stderr:", "warn"]


async def test_run_command_passes_the_clamped_timeout_to_the_sandbox() -> None:
    sandbox = StubSandbox()
    tool = RunCommand(max_timeout_s=120)
    await tool.run(sandbox, {"command": "a"}, CTX)
    await tool.run(sandbox, {"command": "b", "timeout_s": 9999}, CTX)
    await tool.run(sandbox, {"command": "c", "timeout_s": 15}, CTX)
    assert sandbox.calls == [("a", 120), ("b", 120), ("c", 15)]


def test_run_command_uses_a_default_maximum() -> None:
    assert RunCommand().max_timeout_s == DEFAULT_MAX_TIMEOUT_S
    assert "600" in RunCommand().spec.input_schema["properties"]["timeout_s"]["description"]


# ---------- run_tests ----------


async def test_run_tests_without_a_configured_command_is_an_error() -> None:
    sandbox = StubSandbox()
    r = await RunTests(None).run(sandbox, {}, CTX)
    assert not r.ok
    assert "no test command" in r.output and "run_command" in r.output
    assert sandbox.calls == []


async def test_run_tests_runs_the_configured_command_with_the_target() -> None:
    sandbox = StubSandbox()
    tool = RunTests("pytest -q", max_timeout_s=240)
    await tool.run(sandbox, {}, CTX)
    await tool.run(sandbox, {"target": "tests/test_calc.py::test_add"}, CTX)
    # Tests may run long, so they get the full configured allowance.
    assert sandbox.calls == [
        ("pytest -q", 240),
        ("pytest -q tests/test_calc.py::test_add", 240),
    ]


@pytest.mark.parametrize(
    ("name", "args"),
    [("run_command", {"command": "echo \ud800"}), ("run_tests", {"target": "\ud800"})],
)
async def test_a_lone_surrogate_in_a_command_is_a_result(
    name: str, args: dict[str, str], sbx: FakeSandbox
) -> None:
    # No sandbox can be handed text that has no UTF-8 form.
    reg = default_registry(test_command="pytest -q")
    r = await reg.run(ToolCall(id="c", name=name, input=args), sbx, CTX)
    assert not r.ok and "UTF-8" in r.output


async def test_run_tests_reports_a_bad_target_as_a_result() -> None:
    sandbox = StubSandbox()
    r = await RunTests("pytest -q").run(sandbox, {"target": "tests/'oops"}, CTX)
    assert not r.ok and "target" in r.output
    assert sandbox.calls == []


async def test_run_tests_adds_the_parsed_summary_line() -> None:
    out = ".F.   [100%]\nFAILED tests/test_calc.py::test_add - assert -1 == 5\n"
    out += "1 failed, 2 passed in 0.03s\n"
    sandbox = StubSandbox(exec_result(stdout=out, code=1))
    r = await RunTests("pytest -q").run(sandbox, {}, CTX)
    assert r.ok and r.exit_code == 1
    lines = r.output.splitlines()
    assert lines[:2] == ["exit code: 1", "summary: 1 failed, 2 passed"]
    assert "FAILED tests/test_calc.py::test_add - assert -1 == 5" in r.output


async def test_run_tests_reads_the_summary_from_stderr_too() -> None:
    sandbox = StubSandbox(exec_result(stderr="3 passed in 0.01s\n"))
    r = await RunTests("some-wrapper").run(sandbox, {}, CTX)
    assert "summary: 3 passed" in r.output


async def test_run_tests_without_pytest_output_has_no_summary_line() -> None:
    sandbox = StubSandbox(exec_result(stderr="Ran 3 tests in 0.001s\n\nOK\n"))
    r = await RunTests("python -m unittest").run(sandbox, {}, CTX)
    assert r.ok
    assert "summary:" not in r.output


async def test_run_tests_timeout_is_an_error_with_a_hint() -> None:
    sandbox = StubSandbox(exec_result(stdout="tests/test_slow.py ", code=137, timed_out=True))
    r = await RunTests("pytest -q", max_timeout_s=90).run(sandbox, {}, CTX)
    assert not r.ok and r.exit_code == 137
    assert r.output.startswith("timed out after 90 s and was killed.")
    assert "target" in r.output
    assert "summary:" not in r.output


async def test_run_tests_finds_pytest_in_the_workspace_venv(sbx: FakeSandbox) -> None:
    # The fake sandbox mirrors the Docker PATH rule: <workspace>/.venv/bin comes first.
    script = b'#!/bin/sh\necho "1 failed, 2 passed in 0.03s"\nexit 1\n'
    await sbx.write_file(".venv/bin/pytest", script)
    await sbx.exec("chmod +x .venv/bin/pytest", timeout_s=10)
    r = await RunTests("pytest -q").run(sbx, {}, CTX)
    assert r.ok and r.exit_code == 1
    assert "summary: 1 failed, 2 passed" in r.output


# ---------- registry wiring ----------


async def test_default_registry_hands_its_config_to_the_exec_tools() -> None:
    reg = default_registry(test_command="pytest -q", max_timeout_s=42)
    sandbox = StubSandbox()
    run = ToolCall(id="c1", name="run_command", input={"command": "ls", "timeout_s": 500})
    tests = ToolCall(id="c2", name="run_tests", input={"target": "tests/a.py"})
    assert (await reg.run(run, sandbox, CTX)).ok
    assert (await reg.run(tests, sandbox, CTX)).ok
    assert sandbox.calls == [("ls", 42), ("pytest -q tests/a.py", 42)]


async def test_default_registry_without_config_keeps_working() -> None:
    reg = default_registry()
    sandbox = StubSandbox()
    r = await reg.run(ToolCall(id="c", name="run_tests", input={}), sandbox, CTX)
    assert not r.ok and "no test command" in r.output
    await reg.run(ToolCall(id="c", name="run_command", input={"command": "ls"}), sandbox, CTX)
    assert sandbox.calls == [("ls", DEFAULT_TIMEOUT_S)]


async def test_exec_tool_input_is_validated_by_the_registry() -> None:
    reg = default_registry(test_command="pytest -q")
    sandbox = StubSandbox()
    missing = await reg.run(ToolCall(id="a", name="run_command", input={}), sandbox, CTX)
    assert not missing.ok and "'command' is a required property" in missing.output
    zero = await reg.run(
        ToolCall(id="b", name="run_command", input={"command": "ls", "timeout_s": 0}), sandbox, CTX
    )
    assert not zero.ok and "timeout_s" in zero.output
    extra = await reg.run(
        ToolCall(id="c", name="run_tests", input={"target": "x", "verbose": True}), sandbox, CTX
    )
    assert not extra.ok
    assert sandbox.calls == []


def test_every_default_tool_description_is_one_or_two_short_sentences() -> None:
    for spec in default_registry().specs():
        sentences = re.findall(r"[.!?](?:\s|$)", spec.description)
        assert 1 <= len(sentences) <= 2, spec.name
        assert len(spec.description) <= 160, spec.name
