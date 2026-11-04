"""telemetry/otel.py: the tracer provider, the Temporal interceptor and the GenAI spans.

An InMemorySpanExporter takes the place of the collector. tests/integration/test_tracing.py
runs the same spans inside a workflow.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from opentelemetry.trace import Status, StatusCode
from temporalio.contrib.opentelemetry import TracingInterceptor

from longhaul.contracts import (
    LLMResponse,
    ProviderError,
    ProviderErrorKind,
    Role,
    TextBlock,
    ToolResult,
    Usage,
)
from longhaul.settings import EnvSettings, Telemetry
from longhaul.telemetry import otel

pytestmark = [pytest.mark.unit, pytest.mark.phase7]

SECRET = "PROMPT-TEXT-5c1e"


@pytest.fixture(autouse=True)
def no_leftover_state() -> Iterator[None]:
    """setup_tracing keeps its provider for the process, so each test starts and ends without."""
    otel.configure(None)
    yield
    otel.configure(None)


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    with otel.configured(provider):
        yield memory
    provider.shutdown()


def settings_with(monkeypatch: pytest.MonkeyPatch, endpoint: str | None) -> EnvSettings:
    """Settings with this endpoint and no other source: a .env in the folder must not leak in."""
    if endpoint is None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    else:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    return EnvSettings(_env_file=None)


def reply(text: str = "done", usage: Usage | None = None, **kwargs: Any) -> LLMResponse:
    return LLMResponse(
        content=[TextBlock(text=text)],
        tool_calls=[],
        stop_reason="end_turn",
        raw_stop_reason="end_turn",
        usage=usage or Usage(input_tokens=100, output_tokens=20),
        model="served-model",
        latency_ms=5,
        **kwargs,
    )


def only(exporter: InMemorySpanExporter) -> ReadableSpan:
    (span,) = exporter.get_finished_spans()
    return span


def chat(**kwargs: Any) -> Any:
    arguments: dict[str, Any] = {
        "model": "asked-model",
        "provider": "anthropic",
        "task_id": "lh-acme-calc-1",
        "turn_index": 3,
        "role": Role.CODER,
    }
    return otel.chat_span(**{**arguments, **kwargs})


# ---------- setup_tracing ----------
def test_no_endpoint_means_no_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert otel.setup_tracing(settings_with(monkeypatch, None)) is None
    assert otel.interceptors() == []


def test_the_provider_names_the_service_and_exports_to_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    class FakeExporter:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(kwargs)

        def export(self, spans: Any) -> Any:
            raise AssertionError("nothing should be exported in this test")

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 0) -> bool:
            return True

    monkeypatch.setattr(otel, "OTLPSpanExporter", FakeExporter)

    provider = otel.setup_tracing(settings_with(monkeypatch, "http://localhost:14317"))

    assert provider is not None
    assert provider.resource.attributes["service.name"] == "longhaul-worker"
    assert seen == [{"endpoint": "http://localhost:14317"}]
    provider.shutdown()


def test_an_endpoint_without_a_scheme_is_plain_grpc(monkeypatch: pytest.MonkeyPatch) -> None:
    # The grpc exporter would read "localhost:14317" as scheme "localhost" and then connect
    # over TLS. The compose collector speaks plain gRPC.
    seen: list[dict[str, Any]] = []

    class FakeExporter:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(kwargs)

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(otel, "OTLPSpanExporter", FakeExporter)

    provider = otel.setup_tracing(settings_with(monkeypatch, "localhost:14317"))

    assert seen == [{"endpoint": "http://localhost:14317"}]
    assert provider is not None
    provider.shutdown()


def test_setup_tracing_takes_the_content_choice_from_the_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeExporter:
        def __init__(self, **kwargs: Any) -> None:
            return None

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(otel, "OTLPSpanExporter", FakeExporter)
    settings = settings_with(monkeypatch, "http://localhost:14317")

    provider = otel.setup_tracing(settings)
    assert provider is not None and otel.capturing_content() is False
    provider.shutdown()

    provider = otel.setup_tracing(settings, Telemetry(capture_content=True))
    assert provider is not None and otel.capturing_content() is True
    provider.shutdown()


def test_interceptors_trace_with_the_configured_provider(
    exporter: InMemorySpanExporter,
) -> None:
    (interceptor,) = otel.interceptors()

    assert isinstance(interceptor, TracingInterceptor)
    with interceptor.tracer.start_as_current_span("probe"):
        pass
    assert only(exporter).name == "probe"


def test_interceptor_spans_keep_the_error_type_and_drop_the_text(
    exporter: InMemorySpanExporter,
) -> None:
    # Temporal's interceptor sets "Type: message" as the description of a failed span, and the
    # SDK records the exception with its message and stacktrace.
    (interceptor,) = otel.interceptors()

    with pytest.raises(RuntimeError), interceptor.tracer.start_as_current_span("probe") as span:
        span.set_status(Status(StatusCode.ERROR, f"RuntimeError: {SECRET}"))
        raise RuntimeError(f"boom {SECRET}")

    done = only(exporter)
    assert done.status.status_code is StatusCode.ERROR
    assert done.status.description is None
    assert dict(done.attributes or {})["error.type"] == "RuntimeError"
    assert done.events == ()


def test_a_span_started_by_hand_drops_exception_text_too(exporter: InMemorySpanExporter) -> None:
    # The workflow completion span is started this way, and the interceptor then records the
    # exception on it.
    (interceptor,) = otel.interceptors()

    span = interceptor.tracer.start_span("probe", attributes={"kept": 1})
    span.set_attributes({"also": "kept"})
    span.add_event("note")
    span.update_name("renamed")
    assert span.is_recording()
    assert span.get_span_context().is_valid
    span.record_exception(ValueError(f"bad {SECRET}"), {"exception.message": SECRET})
    span.end()

    done = only(exporter)
    assert done.name == "renamed"
    assert dict(done.attributes or {}) == {"kept": 1, "also": "kept", "error.type": "ValueError"}
    assert [e.name for e in done.events] == ["note"]


def test_interceptor_spans_keep_the_exception_text_when_capture_is_on() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    with otel.configured(provider, capture_content=True):
        (interceptor,) = otel.interceptors()
        with pytest.raises(RuntimeError), interceptor.tracer.start_as_current_span("probe"):
            raise RuntimeError(f"boom {SECRET}")

    (event,) = only(memory).events
    assert SECRET in str((event.attributes or {})["exception.message"])
    provider.shutdown()


def test_a_span_opened_in_detached_starts_a_new_trace(exporter: InMemorySpanExporter) -> None:
    tracer = otel.tracer()
    with (
        tracer.start_as_current_span("caller"),
        otel.detached(),
        tracer.start_as_current_span("task"),
    ):
        pass

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["task"].parent is None
    assert spans["task"].context.trace_id != spans["caller"].context.trace_id


def test_a_span_opened_after_detached_is_a_child_again(exporter: InMemorySpanExporter) -> None:
    tracer = otel.tracer()
    with tracer.start_as_current_span("caller"):
        with otel.detached():
            pass
        with tracer.start_as_current_span("after"):
            pass

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["after"].parent is not None
    assert spans["after"].parent.span_id == spans["caller"].context.span_id


# ---------- the chat span ----------
def test_a_chat_span_carries_the_genai_attributes(exporter: InMemorySpanExporter) -> None:
    usage = Usage(input_tokens=100, output_tokens=20, cache_read_tokens=300, cache_write_tokens=50)
    with chat() as span:
        span.record(reply(usage=usage), cost_usd=0.25)

    done = only(exporter)
    assert done.name == "chat asked-model"
    assert dict(done.attributes or {}) == {
        gen_ai.GEN_AI_OPERATION_NAME: "chat",
        gen_ai.GEN_AI_PROVIDER_NAME: "anthropic",
        gen_ai.GEN_AI_REQUEST_MODEL: "asked-model",
        gen_ai.GEN_AI_RESPONSE_MODEL: "served-model",
        # The conventions count cached tokens in the input total.
        gen_ai.GEN_AI_USAGE_INPUT_TOKENS: 450,
        gen_ai.GEN_AI_USAGE_OUTPUT_TOKENS: 20,
        gen_ai.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS: 300,
        gen_ai.GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS: 50,
        gen_ai.GEN_AI_RESPONSE_FINISH_REASONS: ("end_turn",),
        otel.TASK_ID: "lh-acme-calc-1",
        otel.TURN_INDEX: 3,
        otel.ROLE: "coder",
        otel.COST_USD: 0.25,
        # cache_read / (input + cache_read), as plan LH-405 defines it
        otel.CACHE_RATIO: 0.75,
    }
    assert done.status.status_code is StatusCode.UNSET


def test_the_finish_reason_is_the_providers_own_word(exporter: InMemorySpanExporter) -> None:
    response = reply().model_copy(update={"stop_reason": "tool_use", "raw_stop_reason": "tool_use"})
    with chat() as span:
        span.record(response)

    assert dict(only(exporter).attributes or {})[gen_ai.GEN_AI_RESPONSE_FINISH_REASONS] == (
        "tool_use",
    )


def test_the_cache_ratio_of_a_call_with_no_input_is_zero(exporter: InMemorySpanExporter) -> None:
    with chat() as span:
        span.record(reply(usage=Usage()))

    assert dict(only(exporter).attributes or {})[otel.CACHE_RATIO] == 0.0


def test_a_failed_call_is_an_error_span_that_keeps_the_text_out(
    exporter: InMemorySpanExporter,
) -> None:
    error = ProviderError(
        ProviderErrorKind.OVERLOADED, f"echo of {SECRET}", usage=Usage(input_tokens=7)
    )
    with pytest.raises(ProviderError), chat():
        raise error

    done = only(exporter)
    assert done.status.status_code is StatusCode.ERROR
    assert dict(done.attributes or {})["error.type"] == "overloaded"
    # The billed usage is on the span, and the message is on neither the status nor an event.
    assert dict(done.attributes or {})[gen_ai.GEN_AI_USAGE_INPUT_TOKENS] == 7
    assert done.events == ()
    assert SECRET not in (done.status.description or "")


def test_another_exception_is_recorded_by_its_class_only(exporter: InMemorySpanExporter) -> None:
    with pytest.raises(RuntimeError), chat():
        raise RuntimeError(f"boom {SECRET}")

    done = only(exporter)
    assert done.status.status_code is StatusCode.ERROR
    assert dict(done.attributes or {})["error.type"] == "RuntimeError"
    assert done.events == ()
    assert SECRET not in (done.status.description or "")


def test_a_chat_span_has_no_content_by_default(exporter: InMemorySpanExporter) -> None:
    with chat(system=SECRET) as span:
        span.record(reply(text=SECRET))

    done = only(exporter)
    assert SECRET not in repr(dict(done.attributes or {}))
    assert done.events == ()


def test_content_goes_in_when_capture_is_on() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    with (
        otel.configured(provider, capture_content=True),
        chat(system="the system prompt") as span,
    ):
        span.record(reply(text=SECRET))

    attrs = dict(only(memory).attributes or {})
    assert attrs[gen_ai.GEN_AI_SYSTEM_INSTRUCTIONS] == "the system prompt"
    assert SECRET in str(attrs[gen_ai.GEN_AI_OUTPUT_MESSAGES])
    provider.shutdown()


def test_captured_content_is_cut_to_a_limit() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    long_prompt = "x" * (otel.CONTENT_LIMIT_CHARS + 500)
    with otel.configured(provider, capture_content=True), chat(system=long_prompt):
        pass

    captured = str(dict(only(memory).attributes or {})[gen_ai.GEN_AI_SYSTEM_INSTRUCTIONS])
    assert captured.startswith("x" * 100)
    assert len(captured) < otel.CONTENT_LIMIT_CHARS + 100
    assert "truncated" in captured
    provider.shutdown()


# ---------- the tool span ----------
def tool(**kwargs: Any) -> Any:
    arguments: dict[str, Any] = {
        "name": "read_file",
        "call_id": "toolu_abc_0",
        "task_id": "lh-acme-calc-1",
        "turn_index": 3,
        "arguments": {"path": SECRET},
    }
    return otel.tool_span(**{**arguments, **kwargs})


def test_a_tool_span_carries_the_genai_attributes(exporter: InMemorySpanExporter) -> None:
    with tool() as span:
        span.record(ToolResult(call_id="toolu_abc_0", ok=True, output=SECRET))

    done = only(exporter)
    assert done.name == "execute_tool read_file"
    assert dict(done.attributes or {}) == {
        gen_ai.GEN_AI_OPERATION_NAME: "execute_tool",
        gen_ai.GEN_AI_TOOL_NAME: "read_file",
        gen_ai.GEN_AI_TOOL_CALL_ID: "toolu_abc_0",
        gen_ai.GEN_AI_TOOL_TYPE: "function",
        otel.TASK_ID: "lh-acme-calc-1",
        otel.TURN_INDEX: 3,
    }
    assert done.status.status_code is StatusCode.UNSET


def test_a_tool_that_reports_failure_is_an_error_span(exporter: InMemorySpanExporter) -> None:
    with tool() as span:
        span.record(ToolResult(call_id="toolu_abc_0", ok=False, output=SECRET, exit_code=1))

    done = only(exporter)
    assert done.status.status_code is StatusCode.ERROR
    assert dict(done.attributes or {})["error.type"] == "tool_error"
    assert SECRET not in (done.status.description or "")


def test_a_tool_that_raises_is_an_error_span_with_no_text(exporter: InMemorySpanExporter) -> None:
    with pytest.raises(OSError), tool():
        raise OSError(f"cannot read {SECRET}")

    done = only(exporter)
    assert done.status.status_code is StatusCode.ERROR
    assert dict(done.attributes or {})["error.type"] == "OSError"
    assert done.events == ()


def test_a_tool_span_has_no_content_by_default(exporter: InMemorySpanExporter) -> None:
    with tool() as span:
        span.record(ToolResult(call_id="toolu_abc_0", ok=True, output=SECRET))

    done = only(exporter)
    assert SECRET not in repr(dict(done.attributes or {}))
    assert done.events == ()


def test_tool_content_goes_in_when_capture_is_on() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    with otel.configured(provider, capture_content=True), tool() as span:
        span.record(ToolResult(call_id="toolu_abc_0", ok=True, output="file text"))

    attrs = dict(only(memory).attributes or {})
    assert SECRET in str(attrs[gen_ai.GEN_AI_TOOL_CALL_ARGUMENTS])
    assert attrs[gen_ai.GEN_AI_TOOL_CALL_RESULT] == "file text"
    provider.shutdown()


# ---------- without a provider ----------
def test_spans_cost_nothing_when_tracing_is_off() -> None:
    with chat() as span:
        span.record(reply())
    with tool() as tool_span:
        tool_span.record(ToolResult(call_id="toolu_abc_0", ok=True, output="x"))
