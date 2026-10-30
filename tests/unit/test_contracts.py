import ast
import inspect
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter

from longhaul import contracts
from longhaul.contracts import (
    BlobRef,
    Message,
    Phase,
    ProviderError,
    ProviderErrorKind,
    StuckReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

CONTRACTS_PATH = Path(contracts.__file__)
ALLOWED_IMPORTS = {"__future__", "datetime", "enum", "typing", "pydantic"}

_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
_BLOB = BlobRef(key="tasks/t/a.json", sha256="ab" * 32, size=12)
_SETTINGS = contracts.RepoSettings(repo="o/r")
_SPEC = contracts.TaskSpec(
    task_id="lh-o-r-1",
    repo="o/r",
    issue_number=1,
    title="t",
    body="b",
    settings=_SETTINGS,
)
_STEP = contracts.PlanStep(id="s1", description="d", done_when="tests pass")
_PLAN = contracts.Plan(summary="s", steps=[_STEP], acceptance_checks=["pytest -q"])
_NOTES = contracts.ProgressNotes(
    goal="g",
    plan_status=[_STEP],
    files_touched=["a.py"],
    decisions=["d"],
    failing_tests=[],
    open_hypotheses=[],
    tried_and_failed=[],
    covers_turns_up_to=3,
)
_USAGE = contracts.Usage(input_tokens=1, output_tokens=2)
_OUTCOME = contracts.Outcome(kind=contracts.OutcomeKind.PR_OPENED, pr_url="u", summary="s")

SAMPLES: dict[str, BaseModel] = {
    "BlobRef": _BLOB,
    "ToolSpec": contracts.ToolSpec(name="n", description="d", input_schema={"type": "object"}),
    "ToolCall": contracts.ToolCall(id="c1", name="n", input={"a": 1}),
    "ToolResult": contracts.ToolResult(
        call_id="c1", ok=True, output="o", output_ref=_BLOB, exit_code=0, pre_sha="a", post_sha="b"
    ),
    "TextBlock": TextBlock(text="hi"),
    "ToolUseBlock": ToolUseBlock(id="c1", name="n", input={"a": 1}),
    "ToolResultBlock": ToolResultBlock(tool_use_id="c1", content="out", is_error=True),
    "Message": Message(role="user", content=[TextBlock(text="hi")]),
    "Usage": _USAGE,
    "LLMResponse": contracts.LLMResponse(
        content=[TextBlock(text="hi")],
        tool_calls=[],
        stop_reason="end_turn",
        usage=_USAGE,
        model="m",
        latency_ms=5,
    ),
    "Budgets": contracts.Budgets(),
    "RepoSettings": _SETTINGS,
    "TaskSpec": _SPEC,
    "PlanStep": _STEP,
    "Plan": _PLAN,
    "VerifierVerdict": contracts.VerifierVerdict(
        passed=True, tests_run="pytest", tests_summary="1 passed"
    ),
    "ProgressNotes": _NOTES,
    "StuckSignal": contracts.StuckSignal(reason=StuckReason.NO_PROGRESS, evidence=["e"]),
    "Outcome": _OUTCOME,
    "TaskStatus": contracts.TaskStatus(
        task_id="t",
        phase=Phase.CODING,
        turn_index=1,
        cost_usd=0.5,
        last_checkpoint=None,
        plan_version=None,
        outcome=_OUTCOME,
    ),
    "CarryState": contracts.CarryState(
        spec=_SPEC,
        phase=Phase.PLANNING,
        transcript_ref=_BLOB,
        notes=_NOTES,
        plan=_PLAN,
        started_at=_NOW,
        hints=["h"],
    ),
    "ExecResult": contracts.ExecResult(
        exit_code=0, stdout="o", stderr="", timed_out=False, duration_ms=3
    ),
    "SandboxLimits": contracts.SandboxLimits(),
    "ToolContext": contracts.ToolContext(task_id="t", turn_index=0, blob_prefix="p"),
    "MemoryItem": contracts.MemoryItem(
        id="m1", repo="o/r", kind="lesson", content="c", status="candidate", source="task"
    ),
}


def _all_models() -> list[type[BaseModel]]:
    return [
        obj
        for _, obj in inspect.getmembers(contracts, inspect.isclass)
        if issubclass(obj, BaseModel) and obj.__module__ == contracts.__name__
    ]


def test_every_model_has_a_sample() -> None:
    assert {m.__name__ for m in _all_models()} == set(SAMPLES)


def test_roundtrip_all_models() -> None:
    for model in _all_models():
        sample = SAMPLES[model.__name__]
        assert isinstance(sample, model)
        assert model.model_validate_json(sample.model_dump_json()) == sample


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


def test_defaults_are_not_shared() -> None:
    a = contracts.RepoSettings(repo="a/b")
    b = contracts.RepoSettings(repo="c/d")
    assert a.budgets is not b.budgets
    p1 = contracts.Plan(summary="s", steps=[], acceptance_checks=[])
    p2 = contracts.Plan(summary="s", steps=[], acceptance_checks=[])
    assert p1.risks is not p2.risks


def test_contracts_version() -> None:
    assert contracts.contracts_version == "1"
