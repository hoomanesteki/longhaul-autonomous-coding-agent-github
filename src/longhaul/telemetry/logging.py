"""Logs for the worker: JSON lines with task context, and no secrets in them.

Two parts, small enough for the rest of the code to use without knowing structlog:

- `setup_logging()` is the one call the worker makes at startup. After it, every log line
  that Python code writes is one JSON object on stderr: structlog events, the standard library
  records of this repo's modules, and temporalio's own loggers. All of them pass through the
  same processors, so they carry the same fields and the same redaction. Temporal Core (the Rust
  library under the SDK) is not Python code. It writes its WARN and ERROR lines to stderr in its
  own format unless the worker's Runtime forwards them (docs/architecture.md, "Logs").
- `redact(text)` is the redaction itself. Run reports (LH-705) and the console (Phase 8) call it
  on text that does not go through a logger, so one function decides what counts as a secret.

A line carries `task_id`, `turn_index`, `activity` and `trace_id` when they apply. The first
three are bound for the task of an activity by `interceptors()`, which reads the activity
type from `activity.info()` and the ids from the input the activity handles (every task
activity takes a model with a `task_id`, and the per-turn ones a `turn_index` too). The
`trace_id` is read from the current OpenTelemetry span at the moment of the log call, so it
matches the `RunActivity` span in Jaeger. `interceptors()` binds the same id as well, for the one
line the SDK writes after that span has closed. A line from outside an activity has none of
the four unless a caller binds them with structlog.contextvars.

Redaction runs last, just before the JSON is written, over the event text, every field (nested
dicts and lists too), the formatted exception, and the repr of any value JSON cannot encode.
That way a secret in `logger.info("pushing", url=url)` and a secret in a traceback are both
caught, and a field added later is covered without anyone remembering to.

`sk-` has to start a word. Without that, any text with "sk-" inside a longer word and 20 more
characters after it would be cut, and the workflow id `lh-acme-my-task-tracker-pro-edition-5`
is one: "task-tracker-pro-edition-5" is `sk-` plus 25. A real key follows a space, a quote, an
equals sign, the start of the line, or an escaped newline, tab or carriage return (`\\n` as two
characters, which is how a repr and JSON show a line break).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

import structlog
import temporalio.client
import temporalio.worker
from opentelemetry import trace
from structlog.contextvars import bind_contextvars, merge_contextvars
from structlog.typing import EventDict, Processor, WrappedLogger
from temporalio import activity

REDACTED = "[redacted]"

# Where "sk-" may begin a key: after anything but a letter or digit, or after an escaped newline,
# tab or carriage return. A repr, a %r argument, str() of a list and JSON all write those as a
# backslash and a letter, so the character before the key is the n, t or r of the escape.
_WORD_START = r"(?:(?<![A-Za-z0-9])|(?<=\\[nrt]))"

# The private key comes first and ends at its END line. A key that was cut off (a tool output
# is truncated, a log line is clipped) has no END line, and then everything after the header is
# the key, so the match runs to the end of the text.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
        re.DOTALL,
    ),
    re.compile(_WORD_START + r"sk-[A-Za-z0-9_-]{20,}"),
    # ghp personal, ghs installation (what a GitHub App worker holds, ADR-0012), ghu user,
    # gho OAuth and ghr refresh tokens all share one shape.
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)

# The keys every line has, in the order they are written.
_LEADING_KEYS = ("timestamp", "level", "logger", "event")


# ---------- redaction ----------
def redact(text: str) -> str:
    """Replace every secret in the text with "[redacted]".

    The secrets are OpenAI and Anthropic style keys (`sk-`), GitHub tokens (`ghp_`, `ghs_`,
    `ghu_`, `gho_`, `ghr_` and `github_pat_`), AWS access key ids (`AKIA`) and PEM private keys,
    header through END line.
    """
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        # A key that is not a string would stop json.dumps, and a log call must not.
        return {redact(str(key)): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(item) for item in value]
    return value


def _redact_event(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    return {key: _redact_value(value) for key, value in event_dict.items()}


def _lead_with_the_message(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    # So a raw line reads "when, how bad, who, what" before the fields of the call.
    first = {key: event_dict.pop(key) for key in _LEADING_KEYS if key in event_dict}
    return {**first, **event_dict}


def _encode_other(value: object) -> str:
    # What json.dumps cannot encode (a model, a datetime, a set) is written as its repr, and a
    # repr can hold a secret as well as a string can.
    return redact(repr(value))


# ---------- context ----------
def _current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    # Without a tracer provider every span is invalid, and there is no id to give.
    return format(context.trace_id, "032x") if context.is_valid else None


def _add_trace_id(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    # An open span wins over an id bound earlier (see _ActivityContext), so a line written
    # inside a child trace is not pinned to the trace of the activity.
    if (trace_id := _current_trace_id()) is not None:
        event_dict["trace_id"] = trace_id
    return event_dict


def _activity_fields(args: Sequence[Any]) -> dict[str, Any]:
    """The fields to bind for one activity: its type, and the ids in what it was given."""
    fields: dict[str, Any] = {"activity": activity.info().activity_type}
    for arg in args:
        for name in ("task_id", "turn_index"):
            value = getattr(arg, name, None)
            if isinstance(value, str | int) and not isinstance(value, bool):
                fields.setdefault(name, value)
    return fields


class _ActivityContext(temporalio.worker.ActivityInboundInterceptor):
    async def execute_activity(self, input: temporalio.worker.ExecuteActivityInput) -> Any:
        # Bound for the rest of the task, not just this call. The SDK runs each activity in a
        # task of its own, so nothing leaks to the next activity, and the SDK writes "Completing
        # activity as failed" in that task after the activity raised. That line is the one
        # an operator looks for first, and it needs the task id.
        fields = _activity_fields(input.args)
        # This runs inside the RunActivity span of the tracing interceptor, which is closed by
        # the time the SDK writes that warning. The bound id is what the warning carries.
        if (trace_id := _current_trace_id()) is not None:
            fields["trace_id"] = trace_id
        bind_contextvars(**fields)
        return await super().execute_activity(input)


class LogContextInterceptor(temporalio.client.Interceptor, temporalio.worker.Interceptor):
    """Binds `task_id`, `turn_index` and `activity` to every log line of an activity.

    It is a client interceptor as well, so it goes on the client next to the tracing one and the
    worker takes it from there (see `interceptors()`).
    """

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        return _ActivityContext(next)


def interceptors() -> list[LogContextInterceptor]:
    """The interceptor that adds the task context to the logs of every activity.

    Register it where `telemetry.otel.interceptors()` is registered, on the client that the
    worker is built on. Registering it again on the worker would run it twice.
    """
    return [LogContextInterceptor()]


# ---------- setup ----------
def setup_logging(level: int | str = logging.INFO, *, stream: TextIO | None = None) -> None:
    """Write every log line of this process as JSON to `stream` (stderr by default).

    This replaces the handlers of the root logger, as `logging.basicConfig(force=True)` does, so
    a second call, or a `basicConfig` before it, never makes a line appear twice. The worker calls
    it once at startup, before it connects to Temporal.
    """
    shared: list[Processor] = [
        merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_trace_id,
    ]
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,  # skip the work for a call that will be dropped
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        # What a standard library record goes through to look like a structlog event.
        foreign_pre_chain=shared,
        # What every line goes through last. The exception text is formatted before the
        # redaction so the traceback is covered, and the JSON is written after it.
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _lead_with_the_message,
            _redact_event,
            structlog.processors.JSONRenderer(serializer=json.dumps, default=_encode_other),
        ],
    )
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
