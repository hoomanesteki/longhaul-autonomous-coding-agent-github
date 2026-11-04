"""Traces for the worker: the tracer provider, Temporal's interceptor and the GenAI spans.

Three parts, each small enough for the rest of the code to call without knowing OpenTelemetry:

- `setup_tracing(settings, telemetry)` builds a tracer provider that exports over OTLP to the
  collector. It does nothing when no endpoint is set, and then every span below is a no-op.
- `interceptors()` gives the Temporal interceptor that links workflow and activity spans into
  one trace. `durable.client.connect` registers it on the client, and a worker takes the
  interceptors of its client, so one registration covers both. Registering it again on the
  worker would trace every call twice.
- `detached()` runs a block with no current span, so `durable.client.start_task` opens a new
  trace for each task even when a poller activity calls it.
- `chat_span` and `tool_span` wrap a provider call and a tool run inside an activity. Their
  names and attributes follow the OpenTelemetry GenAI conventions, taken from the constants of
  opentelemetry-semantic-conventions where it has them (docs/architecture.md lists them).

Content capture is off by default. When it is off, nothing the model or a tool said goes into an
attribute or an event: not the prompt, not tool arguments, not tool output, and not the text
of an exception, because a provider error can echo the request. Spans keep names, sizes, token
counts and timings, which is what an operator needs to see where time and tokens went. Temporal's
own spans record the message and stacktrace of a failed activity or workflow, so while capture is
off the interceptor gets a tracer that keeps only the error type (`_QuietTracer`).

The provider is kept in module state and not in the global OpenTelemetry slot. The global slot
accepts one provider per process and warns on a second, which a test suite would trigger.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME as RESOURCE_SERVICE_NAME
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace import Link, Span, SpanContext, SpanKind, Status, StatusCode, Tracer
from opentelemetry.util.types import Attributes, AttributeValue
from pydantic_core import to_json
from temporalio.contrib.opentelemetry import TracingInterceptor

from longhaul.contracts import LLMResponse, Message, ProviderError, Role, ToolResult, Usage
from longhaul.settings import EnvSettings, Telemetry

SERVICE_NAME = "longhaul-worker"
TRACER_NAME = "longhaul"

# Attributes that only Longhaul defines. The GenAI ones come from the semantic conventions.
TASK_ID = "longhaul.task_id"
TURN_INDEX = "longhaul.turn_index"
ROLE = "longhaul.role"
COST_USD = "longhaul.cost_usd"
CACHE_RATIO = "longhaul.cache_ratio"

# What `error.type` holds for a tool that ran and reported failure, as opposed to one that raised.
TOOL_ERROR = "tool_error"

# One captured value is cut here, so a long transcript cannot make a span too big to export.
CONTENT_LIMIT_CHARS = 16_000


@dataclass
class _State:
    provider: TracerProvider | None = None
    capture_content: bool = False


_state = _State()


# ---------- setup ----------
def setup_tracing(
    settings: EnvSettings, telemetry: Telemetry | None = None
) -> TracerProvider | None:
    """Start exporting spans to settings.otel_exporter_otlp_endpoint, and return the provider.

    The caller owns the provider and calls shutdown() on it when the worker stops, which sends
    the spans that are still queued. Without an endpoint nothing is set up and None comes back.
    """
    endpoint = settings.otel_exporter_otlp_endpoint
    if not endpoint:
        configure(None)
        return None
    provider = build_provider(endpoint)
    configure(provider, capture_content=telemetry.capture_content if telemetry else False)
    return provider


def build_provider(endpoint: str) -> TracerProvider:
    resource = Resource.create({RESOURCE_SERVICE_NAME: SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=_grpc_endpoint(endpoint))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def _grpc_endpoint(endpoint: str) -> str:
    # The gRPC exporter reads "localhost:4317" as the scheme "localhost" and then asks for TLS.
    # The collector in compose speaks plain gRPC, so a bare host and port means http.
    return endpoint if "://" in endpoint else f"http://{endpoint}"


def configure(provider: TracerProvider | None, *, capture_content: bool = False) -> None:
    """Set the provider that spans and interceptors use. None turns tracing off."""
    _state.provider = provider
    _state.capture_content = capture_content


@contextmanager
def configured(provider: TracerProvider, *, capture_content: bool = False) -> Iterator[None]:
    """Use this provider inside the block and restore the previous setup after it. For tests."""
    before = (_state.provider, _state.capture_content)
    configure(provider, capture_content=capture_content)
    try:
        yield
    finally:
        configure(before[0], capture_content=before[1])


def capturing_content() -> bool:
    return _state.capture_content


def tracer() -> Tracer:
    # Without a provider of ours, the API's default tracer records nothing.
    provider = _state.provider
    return provider.get_tracer(TRACER_NAME) if provider else trace.get_tracer(TRACER_NAME)


def interceptors() -> list[TracingInterceptor]:
    """Temporal's tracing interceptor, or none while tracing is off.

    Pass the result as `interceptors=` to Client.connect. A worker uses its client's interceptors
    too, so the worker needs no list of its own.
    """
    if _state.provider is None:
        return []
    chosen = tracer() if _state.capture_content else _QuietTracer(tracer())
    return [TracingInterceptor(chosen)]


@contextmanager
def detached() -> Iterator[None]:
    """Run the block with no current span, so a span opened inside it starts a new trace.

    A poller activity starts one task per issue. Inside RunActivity:poll_once, every task would
    otherwise join the trace of that poll, and Jaeger would show one trace per poll.
    """
    token = otel_context.attach(otel_context.Context())
    try:
        yield
    finally:
        otel_context.detach(token)


# ---------- Temporal's spans without exception text ----------
class _QuietSpan(Span):
    """A span that keeps the type of an exception and drops its text.

    Temporal's interceptor sets "Type: message" as the status description of a failed span and
    records the exception, message and stacktrace included, as an event. A provider error can
    echo the request, so with capture off the text must not reach the exporter.
    """

    def __init__(self, span: Span) -> None:
        self._span = span

    def end(self, end_time: int | None = None) -> None:
        self._span.end(end_time)

    def get_span_context(self) -> SpanContext:
        return self._span.get_span_context()

    def set_attributes(self, attributes: Mapping[str, AttributeValue]) -> None:
        self._span.set_attributes(attributes)

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        self._span.set_attribute(key, value)

    def add_event(
        self, name: str, attributes: Attributes = None, timestamp: int | None = None
    ) -> None:
        self._span.add_event(name, attributes, timestamp)

    def add_link(self, context: SpanContext, attributes: Attributes = None) -> None:
        self._span.add_link(context, attributes)

    def update_name(self, name: str) -> None:
        self._span.update_name(name)

    def is_recording(self) -> bool:
        return self._span.is_recording()

    def set_status(self, status: Status | StatusCode, description: str | None = None) -> None:
        code = status.status_code if isinstance(status, Status) else status
        self._span.set_status(code)

    def record_exception(
        self,
        exception: BaseException,
        attributes: Attributes = None,
        timestamp: int | None = None,
        escaped: bool = False,
    ) -> None:
        self._span.set_attribute(ERROR_TYPE, type(exception).__name__)


class _QuietTracer(Tracer):
    """A tracer whose spans are `_QuietSpan`, for the interceptor while capture is off."""

    def __init__(self, tracer: Tracer) -> None:
        self._tracer = tracer

    def start_span(
        self,
        name: str,
        context: otel_context.Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> Span:
        span = self._tracer.start_span(
            name,
            context,
            kind,
            attributes,
            links,
            start_time,
            record_exception,
            set_status_on_exception,
        )
        return _QuietSpan(span)

    # The API types this as its own private context manager class, which an ordinary
    # contextmanager does not subclass. The call sites only use it in a with statement.
    @contextmanager
    def start_as_current_span(  # type: ignore[override]
        self,
        name: str,
        context: otel_context.Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        end_on_exit: bool = True,
    ) -> Iterator[Span]:
        # The SDK would record the exception and set the status itself on the way out. Doing
        # both here, through the quiet span, keeps the same behavior without the text.
        with self._tracer.start_as_current_span(
            name,
            context,
            kind,
            attributes,
            links,
            start_time,
            record_exception=False,
            set_status_on_exception=False,
            end_on_exit=end_on_exit,
        ) as span:
            quiet = _QuietSpan(span)
            try:
                yield quiet
            except BaseException as exc:
                if record_exception:
                    quiet.record_exception(exc)
                if set_status_on_exception:
                    quiet.set_status(StatusCode.ERROR)
                raise


# ---------- model calls ----------
class ChatSpan:
    """The open span of one provider call. `record` adds what the reply says."""

    def __init__(self, span: Span) -> None:
        self._span = span

    def record(self, response: LLMResponse, *, cost_usd: float = 0.0) -> None:
        """Set the reply's attributes.

        `cost_usd` is 0.0 until the pricing table arrives (LH-701), as LlmTurnOutput.cost_usd is.
        """
        span = self._span
        span.set_attribute(gen_ai.GEN_AI_RESPONSE_MODEL, response.model)
        span.set_attribute(
            gen_ai.GEN_AI_RESPONSE_FINISH_REASONS,
            [response.raw_stop_reason or response.stop_reason],
        )
        _set_usage(span, response.usage)
        span.set_attribute(COST_USD, cost_usd)
        span.set_attribute(CACHE_RATIO, cache_ratio(response.usage))
        if _state.capture_content:
            span.set_attribute(gen_ai.GEN_AI_OUTPUT_MESSAGES, _cut(_json(response.content)))


@contextmanager
def chat_span(
    *,
    model: str,
    provider: str,
    task_id: str,
    turn_index: int,
    role: Role,
    system: str = "",
    messages: Sequence[Message] = (),
) -> Iterator[ChatSpan]:
    """A span named "chat <model>" around one provider call.

    `system` and `messages` are the request. They are read only when content capture is on.
    """
    attributes: dict[str, AttributeValue] = {
        gen_ai.GEN_AI_OPERATION_NAME: gen_ai.GenAiOperationNameValues.CHAT.value,
        gen_ai.GEN_AI_PROVIDER_NAME: provider,
        gen_ai.GEN_AI_REQUEST_MODEL: model,
        TASK_ID: task_id,
        TURN_INDEX: turn_index,
        ROLE: role.value,
    }
    if _state.capture_content:
        attributes[gen_ai.GEN_AI_SYSTEM_INSTRUCTIONS] = _cut(system)
        attributes[gen_ai.GEN_AI_INPUT_MESSAGES] = _cut(_json(messages))
    with _open(f"chat {model}", SpanKind.CLIENT, attributes) as span:
        yield ChatSpan(span)


def cache_ratio(usage: Usage) -> float:
    """The share of the prompt that came from the cache: cache_read / (input + cache_read)."""
    denominator = usage.input_tokens + usage.cache_read_tokens
    return usage.cache_read_tokens / denominator if denominator else 0.0


def _set_usage(span: Span, usage: Usage) -> None:
    # The conventions count cached tokens in gen_ai.usage.input_tokens and break them out below.
    span.set_attribute(
        gen_ai.GEN_AI_USAGE_INPUT_TOKENS,
        usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
    )
    span.set_attribute(gen_ai.GEN_AI_USAGE_OUTPUT_TOKENS, usage.output_tokens)
    span.set_attribute(gen_ai.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS, usage.cache_read_tokens)
    span.set_attribute(gen_ai.GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS, usage.cache_write_tokens)


# ---------- tool runs ----------
class ToolSpan:
    """The open span of one tool run. `record` adds the result."""

    def __init__(self, span: Span) -> None:
        self._span = span

    def record(self, result: ToolResult) -> None:
        if _state.capture_content:
            self._span.set_attribute(gen_ai.GEN_AI_TOOL_CALL_RESULT, _cut(result.output))
        if not result.ok:
            # The tool ran and said no, as a failing test run does. The model sees the text; the
            # span only says that it failed.
            self._span.set_attribute(ERROR_TYPE, TOOL_ERROR)
            self._span.set_status(Status(StatusCode.ERROR))


@contextmanager
def tool_span(
    *,
    name: str,
    call_id: str,
    task_id: str,
    turn_index: int,
    arguments: Mapping[str, Any] | None = None,
) -> Iterator[ToolSpan]:
    """A span named "execute_tool <name>" around one tool run.

    `arguments` is read only when content capture is on. The role is not an attribute here:
    a tool call does not carry the role of the turn that made it.
    """
    attributes: dict[str, AttributeValue] = {
        gen_ai.GEN_AI_OPERATION_NAME: gen_ai.GenAiOperationNameValues.EXECUTE_TOOL.value,
        gen_ai.GEN_AI_TOOL_NAME: name,
        gen_ai.GEN_AI_TOOL_CALL_ID: call_id,
        gen_ai.GEN_AI_TOOL_TYPE: "function",
        TASK_ID: task_id,
        TURN_INDEX: turn_index,
    }
    if _state.capture_content and arguments is not None:
        attributes[gen_ai.GEN_AI_TOOL_CALL_ARGUMENTS] = _cut(_json(arguments))
    with _open(f"execute_tool {name}", SpanKind.INTERNAL, attributes) as span:
        yield ToolSpan(span)


# ---------- shared ----------
@contextmanager
def _open(name: str, kind: SpanKind, attributes: Mapping[str, AttributeValue]) -> Iterator[Span]:
    """Open a span, make it current, and on an exception mark it failed without any message.

    The SDK's own handling records the exception text as an event and the status description.
    A provider error can echo the request, so only the error type is kept.
    """
    with tracer().start_as_current_span(
        name,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as exc:
            _fail(span, exc)
            raise


def _fail(span: Span, exc: BaseException) -> None:
    if isinstance(exc, ProviderError):
        span.set_attribute(ERROR_TYPE, exc.kind.value)
        if exc.usage is not None:  # billed, then failed
            _set_usage(span, exc.usage)
    else:
        span.set_attribute(ERROR_TYPE, type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR))


def _json(value: Any) -> str:
    return to_json(value).decode("utf-8", errors="replace")


def _cut(text: str) -> str:
    if len(text) <= CONTENT_LIMIT_CHARS:
        return text
    left = len(text) - CONTENT_LIMIT_CHARS
    return f"{text[:CONTENT_LIMIT_CHARS]}\n[truncated, {left} more characters]"
