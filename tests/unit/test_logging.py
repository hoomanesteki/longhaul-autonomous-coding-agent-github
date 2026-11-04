"""telemetry/logging.py: the JSON lines, the redaction of everything that reaches them, and the
context that the lines carry.

tests/integration/test_logging.py runs the same pipeline inside an activity on a worker.
Secrets here are put together from parts, as in test_redaction.py.
"""

import dataclasses
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from pydantic import BaseModel
from temporalio.testing import ActivityEnvironment
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput
from tests.fakes.log_capture import Captured, captured_logs

from longhaul.telemetry import logging as longhaul_logging
from longhaul.telemetry.logging import REDACTED

pytestmark = [pytest.mark.unit, pytest.mark.phase7]

GITHUB_TOKEN = "ghp_" + "a" * 36
OPENAI_KEY = "sk-" + "Ab3_" * 8
KEY_BLOCK = (
    "-----BEGIN " + "RSA PRIVATE KEY-----\n" + "x" * 64 + "\n-----END " + "RSA PRIVATE KEY-----"
)


@pytest.fixture
def out() -> Iterator[Captured]:
    with captured_logs() as captured:
        yield captured


# ---------- the shape of a line ----------
def test_a_structlog_event_is_one_json_line(out: Captured) -> None:
    structlog.get_logger("longhaul.demo").info("checked out", sha="abc123", files=3)

    (line,) = out.lines()
    assert line["event"] == "checked out"
    assert line["sha"] == "abc123"
    assert line["files"] == 3
    assert line["level"] == "info"
    assert line["logger"] == "longhaul.demo"
    assert line["timestamp"].endswith("Z")  # UTC, so logs from two hosts sort together
    assert list(line)[:4] == ["timestamp", "level", "logger", "event"]


def test_standard_library_records_use_the_same_output(out: Captured) -> None:
    # temporalio and the modules of this repo log through the standard library.
    logging.getLogger("temporalio.worker").warning("poll failed after %s tries", 3)

    (line,) = out.lines()
    assert line["event"] == "poll failed after 3 tries"
    assert line["level"] == "warning"
    assert line["logger"] == "temporalio.worker"
    assert line["timestamp"].endswith("Z")


def test_the_level_decides_what_is_written() -> None:
    with captured_logs(logging.WARNING) as out:
        logging.getLogger("a").info("quiet")
        structlog.get_logger("b").info("also quiet")
        structlog.get_logger("b").warning("loud")

    assert [line["event"] for line in out.lines()] == ["loud"]


def test_calling_setup_twice_does_not_write_a_line_twice(out: Captured) -> None:
    longhaul_logging.setup_logging(stream=out.stream)

    logging.getLogger("a").info("once")
    structlog.get_logger("b").info("once too")

    assert [line["event"] for line in out.lines()] == ["once", "once too"]


def test_bound_context_reaches_both_kinds_of_record(out: Captured) -> None:
    structlog.contextvars.bind_contextvars(task_id="lh-acme-calc-7", turn_index=4)

    structlog.get_logger("a").info("from structlog")
    logging.getLogger("b").info("from the standard library")

    for line in out.lines():
        assert line["task_id"] == "lh-acme-calc-7"
        assert line["turn_index"] == 4


# ---------- redaction ----------
def test_secrets_are_redacted_wherever_they_sit(out: Captured) -> None:
    log = structlog.get_logger("a")
    log.info(f"token {GITHUB_TOKEN}", header=f"Bearer {OPENAI_KEY}")
    log.info("nested", payload={"keys": [OPENAI_KEY, {"deep": KEY_BLOCK}], "n": 1})
    logging.getLogger("b").info("pushing with %s", GITHUB_TOKEN)

    first, second, third = out.lines()
    assert first["event"] == f"token {REDACTED}"
    assert first["header"] == f"Bearer {REDACTED}"
    assert second["payload"] == {"keys": [REDACTED, {"deep": REDACTED}], "n": 1}
    assert third["event"] == f"pushing with {REDACTED}"
    for secret in (GITHUB_TOKEN, OPENAI_KEY, "x" * 64):
        assert secret not in out.text()


def test_an_exception_message_is_redacted(out: Captured) -> None:
    try:
        raise RuntimeError(f"push rejected for {GITHUB_TOKEN}")
    except RuntimeError:
        structlog.get_logger("a").exception("push failed")
        logging.getLogger("b").exception("push failed again")

    first, second = out.lines()
    for line in (first, second):
        assert "RuntimeError" in line["exception"]  # the trace is there ...
        assert REDACTED in line["exception"]  # ... without the secret
    assert GITHUB_TOKEN not in out.text()


def test_an_object_that_is_not_json_is_redacted_too(out: Captured) -> None:
    class Holder:
        def __repr__(self) -> str:
            return f"Holder(token={GITHUB_TOKEN})"

    structlog.get_logger("a").info("holding", thing=Holder())

    (line,) = out.lines()
    assert line["thing"] == f"Holder(token={REDACTED})"


# ---------- trace_id ----------
def test_trace_id_comes_from_the_current_span(out: Captured) -> None:
    provider = TracerProvider()
    with provider.get_tracer("test").start_as_current_span("work") as span:
        structlog.get_logger("a").info("inside")
        logging.getLogger("b").info("inside too")
    structlog.get_logger("a").info("outside")

    inside, inside_too, outside = out.lines()
    expected = format(span.get_span_context().trace_id, "032x")
    assert inside["trace_id"] == expected
    assert inside_too["trace_id"] == expected
    assert "trace_id" not in outside  # no span, so no made-up id
    provider.shutdown()


def test_no_provider_means_no_trace_id(out: Captured) -> None:
    # The default tracer makes non-recording spans with an all zero id.
    with trace.get_tracer("test").start_as_current_span("work"):
        structlog.get_logger("a").info("inside")

    (line,) = out.lines()
    assert "trace_id" not in line


# ---------- the activity interceptor ----------
class Work(BaseModel):
    task_id: str
    turn_index: int


class PollInput(BaseModel):
    repo: str


class Final(ActivityInboundInterceptor):
    """The innermost interceptor: it runs the activity function, as the SDK's own does."""

    def __init__(self) -> None:  # nothing comes after it
        pass

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        return await input.fn(*input.args)


async def run_through_interceptor(activity_type: str, fn: Any, *args: Any) -> None:
    interceptor = longhaul_logging.interceptors()[0].intercept_activity(Final())
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, activity_type=activity_type)
    call = ExecuteActivityInput(fn=fn, args=args, executor=None, headers={})

    async def body() -> None:
        await interceptor.execute_activity(call)

    await env.run(body)


async def test_an_activity_logs_with_its_task_and_turn(out: Captured) -> None:
    async def llm_turn(inp: Work) -> None:
        structlog.get_logger("a").info("calling the model")
        logging.getLogger("b").info("and again")

    await run_through_interceptor(
        "llm_turn", llm_turn, Work(task_id="lh-acme-calc-7", turn_index=2)
    )
    structlog.get_logger("a").info("after the activity")

    inside, inside_too, after = out.lines()
    for line in (inside, inside_too):
        assert line["task_id"] == "lh-acme-calc-7"
        assert line["turn_index"] == 2
        assert line["activity"] == "llm_turn"
    # The SDK runs each activity in a task of its own, so the next one does not inherit these.
    assert not {"task_id", "turn_index", "activity"} & set(after)


async def test_an_activity_without_a_task_binds_only_its_name(out: Captured) -> None:
    async def poll_once(inp: PollInput) -> None:
        structlog.get_logger("a").info("polling")

    await run_through_interceptor("poll_once", poll_once, PollInput(repo="acme/calc"))

    (line,) = out.lines()
    assert line["activity"] == "poll_once"
    assert "task_id" not in line
    assert "turn_index" not in line
