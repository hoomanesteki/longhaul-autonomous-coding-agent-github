import ast
import copy
import inspect
import json
import pickle
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic.fields import FieldInfo

from longhaul import contracts
from longhaul.contracts import (
    BlobRef,
    CallOptions,
    LLMProvider,
    LLMResponse,
    Message,
    Phase,
    ProviderError,
    ProviderErrorKind,
    StopDetails,
    StuckReason,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

CONTRACTS_PATH = Path(contracts.__file__)
ARCHITECTURE = Path(__file__).resolve().parents[2] / "docs" / "architecture.md"
ALLOWED_IMPORTS = {"__future__", "datetime", "enum", "typing", "pydantic"}

# Every field below is set to something other than its default. A field that is dropped
# on the way out comes back as its default, and a sample at the default would not notice.
_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
_BLOB = BlobRef(key="tasks/t/a.json", sha256="ab" * 32, size=12)
_BUDGETS = contracts.Budgets(task_usd=7.5, task_turns=99, task_wall_minutes=60, daily_repo_usd=12.5)
_SETTINGS = contracts.RepoSettings(
    repo="o/r",
    plan_approval_required=True,
    setup_command="pip install -e .",
    test_command="pytest -q",
    budgets=_BUDGETS,
)
_SPEC = contracts.TaskSpec(
    task_id="lh-o-r-1",
    repo="o/r",
    issue_number=1,
    title="t",
    body="b",
    comments=["maintainer comment"],
    base_branch="develop",
    base_sha="c0ffee",
    settings=_SETTINGS,
)
_STEP = contracts.PlanStep(
    id="s1", description="d", files=["a.py"], done_when="tests pass", status="doing"
)
_PLAN = contracts.Plan(
    summary="s", steps=[_STEP], acceptance_checks=["pytest -q"], risks=["flaky test"]
)
_NOTES = contracts.ProgressNotes(
    goal="g",
    plan_status=[_STEP],
    files_touched=["a.py"],
    decisions=["d"],
    failing_tests=["tests/test_a.py::test_x"],
    open_hypotheses=["h"],
    tried_and_failed=["f"],
    covers_turns_up_to=3,
)
_USAGE = Usage(input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_write_tokens=4)
_OUTCOME = contracts.Outcome(kind=contracts.OutcomeKind.PR_OPENED, pr_url="u", summary="s")
_TEXT = TextBlock(text="hi")
_USE = ToolUseBlock(id="c1", name="n", input={"a": 1})
_RESULT = ToolResultBlock(tool_use_id="c1", content="out", is_error=True)
_STOP_DETAILS = StopDetails(category="cyber", explanation="blocked by policy")
_OPTIONS = CallOptions(
    effort="high",
    force_tool="record_progress_notes",
    output_schema={"type": "object", "additionalProperties": False},
)

SAMPLES: dict[str, BaseModel] = {
    "BlobRef": _BLOB,
    "ToolSpec": ToolSpec(name="n", description="d", input_schema={"type": "object"}, strict=True),
    "ToolCall": contracts.ToolCall(id="c1", name="n", input={"a": 1}),
    "ToolResult": contracts.ToolResult(
        call_id="c1",
        ok=True,
        output="o",
        output_ref=_BLOB,
        exit_code=1,
        duration_ms=17,
        pre_sha="a",
        post_sha="b",
    ),
    "TextBlock": _TEXT,
    "ToolUseBlock": _USE,
    "ToolResultBlock": _RESULT,
    "Message": Message(role="user", content=[_TEXT]),
    "Usage": _USAGE,
    "StopDetails": _STOP_DETAILS,
    "CallOptions": _OPTIONS,
    "LLMResponse": LLMResponse(
        content=[TextBlock(text="I cannot help with that.")],
        tool_calls=[],
        stop_reason="refusal",
        raw_stop_reason="refusal",
        stop_details=_STOP_DETAILS,
        usage=_USAGE,
        model="m",
        latency_ms=5,
        request_id="req_011",
        dropped_thinking_blocks=2,
    ),
    "Budgets": _BUDGETS,
    "RepoSettings": _SETTINGS,
    "TaskSpec": _SPEC,
    "PlanStep": _STEP,
    "Plan": _PLAN,
    "VerifierVerdict": contracts.VerifierVerdict(
        passed=True,
        tests_run="pytest",
        tests_summary="1 passed",
        issues=["missing docstring"],
        send_back_step="s1",
    ),
    "ProgressNotes": _NOTES,
    "StuckSignal": contracts.StuckSignal(reason=StuckReason.NO_PROGRESS, evidence=["e"]),
    "Outcome": _OUTCOME,
    "TaskStatus": contracts.TaskStatus(
        task_id="t",
        phase=Phase.PAUSED,
        turn_index=1,
        cost_usd=0.5,
        last_checkpoint="abc123",
        plan_version=2,
        paused_reason="waiting for a hint",
        outcome=_OUTCOME,
    ),
    "CarryState": contracts.CarryState(
        spec=_SPEC,
        phase=Phase.PLANNING,
        turn_index=7,
        transcript_ref=_BLOB,
        notes=_NOTES,
        plan=_PLAN,
        plan_version=2,
        plan_approved=True,
        head_sha="abc123",
        sandbox_id="sandbox-1",
        verify_rounds=1,
        cost_usd=1.25,
        extra_budget_usd=2.5,
        extra_turns=3,
        started_at=_NOW,
        hints=["h"],
        stuck_count=2,
    ),
    "ExecResult": contracts.ExecResult(
        exit_code=0, stdout="o", stderr="", timed_out=False, duration_ms=3
    ),
    "SandboxLimits": contracts.SandboxLimits(
        cpus=1.5, memory_mb=2048, pids=256, disk_note="capped at 2 GB"
    ),
    "ToolContext": contracts.ToolContext(
        task_id="t", turn_index=0, blob_prefix="p", output_max_tokens=123
    ),
    "MemoryItem": contracts.MemoryItem(
        id="m1", repo="o/r", kind="lesson", content="c", status="candidate", source="task"
    ),
}

STOP_REASONS = [
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "refusal",
    "pause_turn",
    "model_context_window_exceeded",
    "other",
]
EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
BLOCKS = [_TEXT, _USE, _RESULT]
# Ways to leave fields out of a dump. A blob helper or a payload converter may use any of
# them to keep history small.
DUMP_MODES: list[dict[str, bool]] = [
    {},
    {"exclude_unset": True},
    {"exclude_defaults": True},
    {"exclude_none": True},
    {"exclude_unset": True, "exclude_defaults": True, "exclude_none": True},
]


def _all_models() -> list[type[BaseModel]]:
    # A name with a leading underscore is a base class for the models, not a model.
    return [
        obj
        for name, obj in inspect.getmembers(contracts, inspect.isclass)
        if issubclass(obj, BaseModel)
        and obj.__module__ == contracts.__name__
        and not name.startswith("_")
    ]


def _models_inside(value: object) -> Iterator[BaseModel]:
    """The model itself and every model nested in its fields, lists or dicts."""
    if isinstance(value, BaseModel):
        yield value
        for name in type(value).model_fields:
            yield from _models_inside(getattr(value, name))
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _models_inside(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _models_inside(item)


def _is_tag(info: FieldInfo) -> bool:
    """A discriminator tag allows one value only, so no sample can set it to anything else."""
    return get_origin(info.annotation) is Literal and len(get_args(info.annotation)) == 1


def _response(**changes: Any) -> LLMResponse:
    fields: dict[str, Any] = {
        "content": [],
        "tool_calls": [],
        "stop_reason": "end_turn",
        "usage": Usage(),
        "model": "m",
        "latency_ms": 1,
    }
    return LLMResponse(**{**fields, **changes})


def _carry(**changes: Any) -> contracts.CarryState:
    fields: dict[str, Any] = {"spec": _SPEC, "phase": Phase.QUEUED, "started_at": _NOW}
    return contracts.CarryState(**{**fields, **changes})


def test_every_model_has_a_sample() -> None:
    assert {m.__name__ for m in _all_models()} == set(SAMPLES)


def test_samples_set_every_field_to_a_non_default_value() -> None:
    at_default: list[str] = []
    for name, sample in SAMPLES.items():
        for model in _models_inside(sample):
            for field, info in type(model).model_fields.items():
                if info.is_required() or _is_tag(info):
                    continue
                if getattr(model, field) == info.get_default(call_default_factory=True):
                    at_default.append(f"{type(model).__name__}.{field} in the {name} sample")
    assert not at_default, "fields still at their default:\n" + "\n".join(at_default)


def test_roundtrip_all_models() -> None:
    for model in _all_models():
        sample = SAMPLES[model.__name__]
        assert isinstance(sample, model)
        back = model.model_validate_json(sample.model_dump_json())
        assert back == sample
        assert back.model_dump() == sample.model_dump()


def test_contracts_imports_are_pure() -> None:
    tree = ast.parse(CONTRACTS_PATH.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module is not None
            found.add(node.module.split(".")[0])
    assert found <= ALLOWED_IMPORTS


def test_message_blocks_discriminated() -> None:
    msg = Message(
        role="assistant",
        content=[
            TextBlock(text="x"),
            ToolUseBlock(id="c1", name="n", input={"k": "v"}),
            ToolResultBlock(tool_use_id="c1", content="r"),
        ],
    )
    back = Message.model_validate_json(msg.model_dump_json())
    assert [type(b) for b in back.content] == [TextBlock, ToolUseBlock, ToolResultBlock]
    raw: dict[str, Any] = {"role": "user", "content": [{"type": "text", "text": "a"}]}
    assert isinstance(Message.model_validate(raw).content[0], TextBlock)
    with pytest.raises(ValueError):
        TypeAdapter(Message).validate_python({"role": "user", "content": [{"type": "nope"}]})


# ---------- block tags survive every dump mode ----------
@pytest.mark.parametrize("mode", DUMP_MODES)
@pytest.mark.parametrize("block", BLOCKS, ids=lambda b: b.type)
def test_block_type_tag_is_in_every_dump(
    block: TextBlock | ToolUseBlock | ToolResultBlock, mode: dict[str, bool]
) -> None:
    assert block.model_dump(**mode)["type"] == block.type
    assert json.loads(block.model_dump_json(**mode))["type"] == block.type


@pytest.mark.parametrize("mode", DUMP_MODES)
def test_messages_decode_after_a_space_saving_dump(mode: dict[str, bool]) -> None:
    msg = Message(role="assistant", content=list(BLOCKS))
    assert Message.model_validate_json(msg.model_dump_json(**mode)) == msg
    transcript = TypeAdapter(list[Message])
    messages = [msg, Message(role="user", content=[_TEXT])]
    assert transcript.validate_json(transcript.dump_json(messages, **mode)) == messages


@pytest.mark.parametrize("mode", DUMP_MODES)
def test_responses_decode_after_a_space_saving_dump(mode: dict[str, bool]) -> None:
    resp = _response(
        content=[_TEXT, _USE],
        tool_calls=[contracts.ToolCall(id="c1", name="n", input={"a": 1})],
        stop_reason="tool_use",
    )
    assert LLMResponse.model_validate_json(resp.model_dump_json(**mode)) == resp


def test_temporal_converter_keeps_tags_and_timezone_when_it_skips_unset_fields() -> None:
    # The pydantic payload converter can be set to skip unset fields to keep history small.
    # Before version 2 that dropped every block tag, and a transcript could not be read back.
    from temporalio.contrib.pydantic import PydanticPayloadConverter, ToJsonOptions

    converter = PydanticPayloadConverter(ToJsonOptions(exclude_unset=True))
    carry = _carry(started_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=-7))))
    transcript = [Message(role="assistant", content=list(BLOCKS))]
    resp = _response(
        content=[_TEXT, _USE],
        tool_calls=[contracts.ToolCall(id="c1", name="n", input={"a": 1})],
        stop_reason="tool_use",
    )

    for value, hint in (
        (carry, contracts.CarryState),
        (transcript, list[Message]),
        (resp, LLMResponse),
    ):
        [payload] = converter.to_payloads([value])
        assert converter.from_payloads([payload], [hint]) == [value]


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_block_json_schemas_still_describe_their_fields(mode: Any) -> None:
    # A client generated from the API schema reads these. FastAPI builds response models in
    # serialization mode, where a plain serializer function would leave "an object, any keys".
    defs = Message.model_json_schema(mode=mode)["$defs"]

    assert set(defs["TextBlock"]["properties"]) == {"type", "text"}
    assert set(defs["ToolUseBlock"]["properties"]) == {"type", "id", "name", "input"}
    assert set(defs["ToolResultBlock"]["properties"]) == {
        "type",
        "tool_use_id",
        "content",
        "is_error",
    }
    assert defs["TextBlock"]["properties"]["type"]["const"] == "text"
    assert defs["TextBlock"]["required"] == ["text"]


def test_blocks_keep_their_tag_first_and_still_drop_other_defaults() -> None:
    result = ToolResultBlock(tool_use_id="c1", content="r")
    assert list(result.model_dump()) == ["type", "tool_use_id", "content", "is_error"]
    # The tag is the exception. Other defaults are still left out, which is the point of the flag.
    assert result.model_dump(exclude_defaults=True) == {
        "type": "tool_result",
        "tool_use_id": "c1",
        "content": "r",
    }
    failed = ToolResultBlock(tool_use_id="c1", content="r", is_error=True)
    assert failed.model_dump(exclude_defaults=True)["is_error"] is True


# ---------- stop reasons and response metadata ----------
@pytest.mark.parametrize("reason", STOP_REASONS)
def test_every_stop_reason_is_accepted(reason: str) -> None:
    assert _response(stop_reason=reason).stop_reason == reason


def test_unknown_stop_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _response(stop_reason="max_tokens_exceeded")


def test_stop_reason_alias_lists_the_same_values() -> None:
    assert list(get_args(contracts.StopReason)) == STOP_REASONS


def test_new_response_fields_are_optional() -> None:
    resp = _response()
    assert resp.raw_stop_reason is None
    assert resp.stop_details is None
    assert resp.request_id is None
    assert resp.dropped_thinking_blocks == 0


def test_response_with_old_fields_only_still_validates() -> None:
    # Transcripts and blobs written under contracts 1 do not have the new keys.
    old = _response().model_dump(
        exclude={"raw_stop_reason", "stop_details", "request_id", "dropped_thinking_blocks"}
    )
    assert LLMResponse.model_validate(old) == _response()


def test_dropped_thinking_blocks_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        _response(dropped_thinking_blocks=-1)


def test_stop_details_fields_are_each_optional() -> None:
    assert StopDetails() == StopDetails(category=None, explanation=None)
    partial = StopDetails(explanation="declined")
    assert StopDetails.model_validate_json(partial.model_dump_json()) == partial
    assert partial.category is None


def test_refusal_response_round_trips_with_its_details() -> None:
    resp = _response(stop_reason="refusal", stop_details=_STOP_DETAILS, raw_stop_reason="refusal")
    back = LLMResponse.model_validate_json(resp.model_dump_json())
    assert back.stop_details == StopDetails(category="cyber", explanation="blocked by policy")


# ---------- tools and call options ----------
def test_tool_spec_is_not_strict_unless_asked() -> None:
    spec = ToolSpec(name="n", description="d", input_schema={"type": "object"})
    assert spec.strict is False
    strict = ToolSpec(name="n", description="d", input_schema={"type": "object"}, strict=True)
    assert json.loads(strict.model_dump_json())["strict"] is True
    assert ToolSpec.model_validate_json(strict.model_dump_json()).strict is True


def test_call_options_default_to_nothing_set() -> None:
    options = CallOptions()
    assert (options.effort, options.force_tool, options.output_schema) == (None, None, None)


def test_call_options_effort_levels_match_the_alias() -> None:
    assert list(get_args(contracts.EffortLevel)) == EFFORT_LEVELS


@pytest.mark.parametrize("effort", EFFORT_LEVELS)
def test_call_options_accept_every_effort_level(effort: Any) -> None:
    assert CallOptions(effort=effort).effort == effort


@pytest.mark.parametrize("effort", ["", "none", "ultra", "MEDIUM", 3])
def test_call_options_reject_an_unknown_effort(effort: Any) -> None:
    with pytest.raises(ValidationError):
        CallOptions(effort=effort)


def test_call_options_reject_a_blank_forced_tool() -> None:
    with pytest.raises(ValidationError):
        CallOptions(force_tool="")


def test_call_options_reject_a_misspelled_option() -> None:
    # A silently ignored effort setting would change cost and quality with no sign of it.
    with pytest.raises(ValidationError, match="effrot"):
        CallOptions(**{"effrot": "low"})


def test_call_options_round_trip_with_every_option_set() -> None:
    back = CallOptions.model_validate_json(_OPTIONS.model_dump_json())
    assert back == _OPTIONS
    assert back.output_schema == {"type": "object", "additionalProperties": False}


def test_provider_protocol_takes_call_options() -> None:
    for method in (LLMProvider.complete, LLMProvider.count_tokens):
        param = inspect.signature(method).parameters["options"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is None
    # The system prompt stays one string: its cache breakpoint sits at the end.
    assert "system" in inspect.signature(LLMProvider.complete).parameters


# ---------- provider errors ----------
@pytest.mark.parametrize(
    ("kind", "retryable"),
    [
        (ProviderErrorKind.RATE_LIMIT, True),
        (ProviderErrorKind.OVERLOADED, True),
        (ProviderErrorKind.SERVER, True),
        (ProviderErrorKind.TIMEOUT, True),
        (ProviderErrorKind.CONTEXT_TOO_LONG, False),
        (ProviderErrorKind.INVALID_REQUEST, False),
        (ProviderErrorKind.AUTH, False),
    ],
)
def test_provider_error_retryable(kind: ProviderErrorKind, retryable: bool) -> None:
    err = ProviderError(kind, "boom", retry_after_s=2.5)
    assert err.retryable is retryable
    assert (err.kind, err.message, err.retry_after_s) == (kind, "boom", 2.5)
    assert str(err) == "boom"


def test_provider_error_pickles() -> None:
    err = ProviderError(ProviderErrorKind.RATE_LIMIT, "slow down", retry_after_s=3.0)
    clone = pickle.loads(pickle.dumps(err))
    assert (clone.kind, clone.message, clone.retry_after_s) == (err.kind, "slow down", 3.0)


def test_provider_error_has_no_usage_unless_given() -> None:
    err = ProviderError(ProviderErrorKind.AUTH, "no key")
    assert err.usage is None
    assert pickle.loads(pickle.dumps(err)).usage is None


def test_provider_error_keeps_usage_through_pickle_and_copy() -> None:
    # A failure after a billed response must still reach the cost ledger, also when the
    # exception is rebuilt on the other side of an activity boundary.
    usage = Usage(input_tokens=10, output_tokens=2, cache_read_tokens=5, cache_write_tokens=1)
    err = ProviderError(ProviderErrorKind.SERVER, "bad body", retry_after_s=1.5, usage=usage)
    for clone in (pickle.loads(pickle.dumps(err)), copy.copy(err), copy.deepcopy(err)):
        assert isinstance(clone, ProviderError)
        assert (clone.kind, clone.message, clone.retry_after_s) == (err.kind, "bad body", 1.5)
        assert clone.usage == usage


def test_provider_error_keeps_usage_set_after_construction() -> None:
    err = ProviderError(ProviderErrorKind.SERVER, "bad body")
    err.usage = Usage(output_tokens=9)
    assert pickle.loads(pickle.dumps(err)).usage == Usage(output_tokens=9)


# ---------- carry state ----------
def test_started_at_rejects_a_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        _carry(started_at=datetime(2026, 1, 2, 3, 4, 5))


def test_started_at_rejects_a_naive_json_string() -> None:
    payload = json.loads(_carry().model_dump_json())
    payload["started_at"] = "2026-01-02T03:04:05"
    with pytest.raises(ValidationError, match="timezone"):
        contracts.CarryState.model_validate_json(json.dumps(payload))


def test_started_at_is_normalised_to_utc() -> None:
    local = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=-7)))
    state = _carry(started_at=local)
    assert state.started_at == local
    assert state.started_at.tzinfo is UTC
    assert state.started_at.utcoffset() == timedelta(0)


def test_started_at_stays_utc_after_a_json_round_trip() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    state = _carry(started_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=offset))
    back = contracts.CarryState.model_validate_json(state.model_dump_json())
    # pydantic hands back its own tzinfo class unless the model normalises it.
    assert back.started_at.tzinfo is UTC
    assert back.started_at == state.started_at
    # Workflow code subtracts it from workflow.now(), which is aware UTC.
    assert datetime.now(UTC) - back.started_at > timedelta(0)


def test_defaults_are_not_shared() -> None:
    a = contracts.RepoSettings(repo="a/b")
    b = contracts.RepoSettings(repo="c/d")
    assert a.budgets is not b.budgets
    p1 = contracts.Plan(summary="s", steps=[], acceptance_checks=[])
    p2 = contracts.Plan(summary="s", steps=[], acceptance_checks=[])
    assert p1.risks is not p2.risks


# ---------- version and changelog ----------
def test_contracts_version() -> None:
    assert contracts.contracts_version == "2"


def test_changelog_has_a_row_for_the_current_version() -> None:
    rows = re.findall(r"^\|\s*(\d+)\s*\|", ARCHITECTURE.read_text(encoding="utf-8"), re.MULTILINE)
    assert contracts.contracts_version in rows
