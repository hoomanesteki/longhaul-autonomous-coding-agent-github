"""The activity contract of TaskWorkflow: names, payload types and the section 3.7 policies.

acts.py is the one module the workflow and the activity implementations both import, so
these tests pin what each side may rely on.
"""

from datetime import timedelta
from pathlib import Path
from types import NoneType
from typing import Any, get_args

import pytest
from pydantic import BaseModel, ValidationError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.workflow import ActivityCancellationType

from longhaul.checkpoints.squash import NothingToDeliverError
from longhaul.context import compact
from longhaul.context.modes import ContextMode
from longhaul.contracts import (
    BlobRef,
    Phase,
    Plan,
    PlanStep,
    ProgressNotes,
    ProviderErrorKind,
    RepoSettings,
    Role,
    StopDetails,
    TaskSpec,
    TaskStatus,
    ToolResult,
    Usage,
)
from longhaul.durable import acts
from longhaul.durable.payload_guard import DEFAULT_LIMIT, PayloadTooLargeError, assert_small
from longhaul.github import side_effects
from longhaul.github.client import GitHubFatal, GitHubRetryable
from longhaul.github.inputs import CommentInput, LabelInput, PullRequestInput
from longhaul.roles.stuck import CallSignal
from longhaul.settings import load_config
from longhaul.tools.control import FINISH

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
REF = BlobRef(key="tasks/lh-x/turns/0001/transcript.json", sha256="0" * 64, size=10)
SHA = "a" * 40
NOTES = ProgressNotes(
    goal="fix add()",
    plan_status=[],
    files_touched=["src/calc/__init__.py"],
    decisions=[],
    failing_tests=[],
    open_hypotheses=[],
    tried_and_failed=["casting to int"],
    covers_turns_up_to=6,
)
SPEC = TaskSpec(
    task_id="lh-x",
    repo="acme/calc",
    issue_number=42,
    title="add() subtracts",
    body="add(2, 3) returns -1",
    settings=RepoSettings(repo="acme/calc"),
)


def test_activity_names_are_the_ones_in_section_3_7() -> None:
    names = [act.name for act in acts.ALL]
    assert sorted(names) == [
        "append_results",
        "bundle_checkpoint",
        "cleanup",
        "compact",
        "decide_plan",
        "ensure_sandbox",
        "llm_turn",
        "load_memory",
        "poll_once",
        "prepare_workspace",
        "record_event",
        "record_step",
        "store_plan",
        "tool_exec",
    ]
    assert len(set(names)) == len(names)


@pytest.mark.parametrize(
    ("act", "arg_type", "result_type"),
    [
        (acts.PREPARE_WORKSPACE, TaskSpec, acts.PrepareWorkspaceOutput),
        (acts.ENSURE_SANDBOX, acts.EnsureSandboxInput, str),
        (acts.LLM_TURN, acts.LlmTurnInput, acts.LlmTurnOutput),
        (acts.TOOL_EXEC, acts.ToolExecInput, acts.ToolExecResult),
        (acts.APPEND_RESULTS, acts.AppendResultsInput, BlobRef),
        (acts.CLEANUP, acts.CleanupInput, NoneType),
        (acts.BUNDLE_CHECKPOINT, acts.BundleCheckpointInput, BlobRef),
        (acts.RECORD_EVENT, acts.RecordEventInput, NoneType),
        (acts.POLL_ONCE, NoneType, acts.PollResult),
        (acts.COMPACT, acts.CompactInput, acts.CompactOutput),
        (acts.STORE_PLAN, acts.StorePlanInput, NoneType),
        (acts.DECIDE_PLAN, acts.DecidePlanInput, NoneType),
        (acts.LOAD_MEMORY, acts.LoadMemoryInput, BlobRef),
        (acts.RECORD_STEP, acts.RecordStepInput, NoneType),
    ],
    ids=lambda value: getattr(value, "name", None) or getattr(value, "__name__", ""),
)
def test_each_activity_names_its_payload_types(
    act: acts.Act, arg_type: type, result_type: type
) -> None:
    assert act.arg_type is arg_type
    assert act.result_type is result_type


@pytest.mark.parametrize(
    ("act", "start_to_close_s", "heartbeat_s", "attempts"),
    [
        pytest.param(acts.PREPARE_WORKSPACE, 15 * 60, 60, 3, id="prepare_workspace"),
        # Section 3.7 gives ensure_sandbox 10 minutes and 5 attempts. The heartbeat is added: it
        # builds a container and runs the setup command, so without one a cancel would have to
        # wait for the activity to end, and a killed worker would be noticed after 10 minutes.
        pytest.param(acts.ENSURE_SANDBOX, 10 * 60, 60, 5, id="ensure_sandbox"),
        pytest.param(acts.LLM_TURN, 10 * 60, 30, 8, id="llm_turn"),
        pytest.param(acts.TOOL_EXEC, 600 + 60, 15, 3, id="tool_exec"),
        # These two are not in the table. They are small blob and cleanup calls, sized like
        # load_memory.
        pytest.param(acts.APPEND_RESULTS, 2 * 60, None, 3, id="append_results"),
        pytest.param(acts.CLEANUP, 2 * 60, None, 3, id="cleanup"),
        # Section 3.7 gives bundle_checkpoint 5 minutes and 3 attempts. The heartbeat is added
        # for the reason ensure_sandbox has one: it uploads a whole history, and a cancel has to
        # reach it.
        pytest.param(acts.BUNDLE_CHECKPOINT, 5 * 60, 60, 3, id="bundle_checkpoint"),
        # A local activity: 10 seconds, and no heartbeat to set.
        pytest.param(acts.RECORD_EVENT, 10, None, 3, id="record_event"),
        # Not in the table either. A poll reads a page or two of issues for each repo and starts
        # the tasks that are new, so three minutes is far more than it needs. The heartbeat is
        # what ends a poll on a killed worker: the schedule skips a tick while a poll is open.
        pytest.param(acts.POLL_ONCE, 3 * 60, 30, 3, id="poll_once"),
        # Section 3.7 gives compact 10 minutes and 5 attempts. The heartbeat is added for the
        # reason llm_turn has one: it is a model call, and a cancel has to reach it.
        pytest.param(acts.COMPACT, 10 * 60, 30, 5, id="compact"),
        # Not in the table. A plan is one row, so it is sized like append_results.
        pytest.param(acts.STORE_PLAN, 2 * 60, None, 3, id="store_plan"),
        # Not in the table. A decision updates one row, so it is sized like store_plan (LH-502).
        pytest.param(acts.DECIDE_PLAN, 2 * 60, None, 3, id="decide_plan"),
        # Section 3.7: 2 minutes and 3 attempts. One search of the store and one blob write.
        pytest.param(acts.LOAD_MEMORY, 2 * 60, None, 3, id="load_memory"),
        # Not in the table. A step update is one event and at most one checkpoint row.
        pytest.param(acts.RECORD_STEP, 2 * 60, None, 3, id="record_step"),
    ],
)
def test_timeouts_and_attempts_match_section_3_7(
    act: acts.Act, start_to_close_s: int, heartbeat_s: int | None, attempts: int
) -> None:
    assert act.start_to_close == timedelta(seconds=start_to_close_s)
    assert act.heartbeat == (None if heartbeat_s is None else timedelta(seconds=heartbeat_s))
    assert act.retry.maximum_attempts == attempts


def test_llm_turn_backs_off_from_two_to_120_seconds() -> None:
    retry = acts.LLM_TURN.retry
    assert retry.initial_interval == timedelta(seconds=2)
    assert retry.maximum_interval == timedelta(seconds=120)
    assert retry.backoff_coefficient == 2.0


def test_llm_turn_does_not_retry_what_a_retry_cannot_fix() -> None:
    never = set(acts.LLM_TURN.retry.non_retryable_error_types or [])
    assert {
        ProviderErrorKind.INVALID_REQUEST.value,
        ProviderErrorKind.AUTH.value,
        ProviderErrorKind.CONTEXT_TOO_LONG.value,
    } <= never
    # The retryable kinds stay out of the list.
    assert never.isdisjoint(
        {
            ProviderErrorKind.RATE_LIMIT.value,
            ProviderErrorKind.OVERLOADED.value,
            ProviderErrorKind.SERVER.value,
            ProviderErrorKind.TIMEOUT.value,
        }
    )


@pytest.mark.parametrize("act", acts.ALL, ids=lambda act: act.name)
def test_no_activity_retries_an_oversized_payload(act: acts.Act) -> None:
    # Retrying cannot make a payload smaller (ADR-0005), and the guard raises this type.
    assert PayloadTooLargeError.__name__ in (act.retry.non_retryable_error_types or [])


def test_tool_exec_timeout_follows_the_configured_command_timeout() -> None:
    # The workflow cannot read the config file, so the number is copied into acts.py.
    config = load_config(ROOT / "config" / "longhaul.example.yaml")
    assert config.sandbox.command_timeout_s == acts.TOOL_TIMEOUT_S


def test_the_hand_over_interval_follows_the_configured_one() -> None:
    # Same reason as the tool timeout: the workflow cannot read the config file.
    config = load_config(ROOT / "config" / "longhaul.example.yaml")
    assert config.loop.continue_as_new_every_turns == acts.CONTINUE_AS_NEW_EVERY_TURNS


def test_only_record_event_is_local() -> None:
    assert [act.name for act in acts.ALL if act.local] == ["record_event"]


def test_finish_is_the_name_of_the_finish_tool() -> None:
    assert acts.FINISH_TOOL == FINISH


# ---------- options and tuning ----------
def test_options_carry_the_policy_of_the_activity() -> None:
    options = acts.options(acts.LLM_TURN)
    assert options["start_to_close_timeout"] == timedelta(minutes=10)
    assert options["heartbeat_timeout"] == timedelta(seconds=30)
    assert options["retry_policy"] is acts.LLM_TURN.retry
    # The clean-up that follows a cancel must not overlap the activity it cancelled.
    assert options["cancellation_type"] is ActivityCancellationType.WAIT_CANCELLATION_COMPLETED


def test_tuning_shortens_the_heartbeat_timeout_of_every_activity_that_has_one() -> None:
    tuning = acts.Tuning(heartbeat_timeout_s=2)
    for act in acts.ALL:
        expected = None if act.heartbeat is None else timedelta(seconds=2)
        assert acts.options(act, tuning)["heartbeat_timeout"] == expected


def test_tuning_never_lengthens_a_timeout() -> None:
    options = acts.options(acts.TOOL_EXEC, acts.Tuning(heartbeat_timeout_s=3600))
    assert options["heartbeat_timeout"] == timedelta(seconds=15)


def test_tuning_leaves_every_other_setting_alone() -> None:
    for act in acts.ALL:
        tuned = dict(acts.options(act, acts.Tuning(heartbeat_timeout_s=2)))
        plain = dict(acts.options(act))
        tuned.pop("heartbeat_timeout")
        plain.pop("heartbeat_timeout")
        assert tuned == plain


def test_an_empty_tuning_changes_nothing() -> None:
    for act in acts.ALL:
        assert acts.options(act, acts.Tuning()) == acts.options(act, None)


def test_a_local_activity_gets_its_timeout_and_retries_but_no_heartbeat() -> None:
    options = acts.local_options(acts.RECORD_EVENT)
    assert options["start_to_close_timeout"] == timedelta(seconds=10)
    assert options["retry_policy"] is acts.RECORD_EVENT.retry
    assert "heartbeat_timeout" not in options
    assert options["cancellation_type"] is ActivityCancellationType.WAIT_CANCELLATION_COMPLETED


@pytest.mark.parametrize("seconds", [0, -1])
def test_tuning_rejects_a_timeout_that_cannot_work(seconds: float) -> None:
    with pytest.raises(ValidationError):
        acts.Tuning(heartbeat_timeout_s=seconds)


@pytest.mark.parametrize("turns", [0, -3])
def test_tuning_rejects_a_hand_over_interval_that_cannot_work(turns: int) -> None:
    with pytest.raises(ValidationError):
        acts.Tuning(continue_as_new_every_turns=turns)


def test_the_hand_over_interval_is_the_configured_one_unless_tuned() -> None:
    assert acts.continue_as_new_every() == acts.CONTINUE_AS_NEW_EVERY_TURNS
    assert acts.continue_as_new_every(acts.Tuning()) == acts.CONTINUE_AS_NEW_EVERY_TURNS
    assert acts.continue_as_new_every(acts.Tuning(heartbeat_timeout_s=2)) == 40
    assert acts.continue_as_new_every(acts.Tuning(continue_as_new_every_turns=3)) == 3


@pytest.mark.parametrize(
    ("turn_index", "every", "expected"),
    [(0, 3, False), (1, 3, False), (2, 3, False), (3, 3, True), (4, 3, False), (6, 3, True)],
)
def test_a_run_hands_over_after_every_nth_turn(turn_index: int, every: int, expected: bool) -> None:
    tuning = acts.Tuning(continue_as_new_every_turns=every)
    assert acts.hand_off_due(turn_index, tuning) is expected


def test_the_configured_interval_applies_without_a_tuning() -> None:
    assert not acts.hand_off_due(39)
    assert acts.hand_off_due(40)
    assert not acts.hand_off_due(41)
    assert acts.hand_off_due(80)


def test_the_server_can_ask_for_a_hand_over_at_any_turn() -> None:
    assert acts.hand_off_due(7, suggested=True)
    assert not acts.hand_off_due(7, suggested=False)


@pytest.mark.parametrize("seconds", [0, -1])
def test_tuning_rejects_a_status_grace_that_cannot_work(seconds: float) -> None:
    with pytest.raises(ValidationError):
        acts.Tuning(status_grace_s=seconds)


def test_a_hand_over_waits_for_a_status_comment_as_long_as_the_tuning_says() -> None:
    assert acts.status_grace() == acts.STATUS_GRACE == timedelta(seconds=30)
    assert acts.status_grace(acts.Tuning(heartbeat_timeout_s=2)) == acts.STATUS_GRACE
    assert acts.status_grace(acts.Tuning(status_grace_s=1.5)) == timedelta(seconds=1.5)


@pytest.mark.parametrize("window", [0, -8_000])
def test_tuning_rejects_a_window_that_cannot_hold_a_request(window: int) -> None:
    with pytest.raises(ValidationError):
        acts.Tuning(window_tokens=window)


def test_a_window_in_the_tuning_changes_no_timeout() -> None:
    for act in acts.ALL:
        assert acts.options(act, acts.Tuning(window_tokens=8_000)) == acts.options(act, None)


def test_the_activities_that_count_tokens_take_the_window_of_a_run() -> None:
    inputs: list[Any] = [
        acts.LlmTurnInput(
            task_id="lh-x",
            turn_index=0,
            role=Role.CODER,
            spec=SPEC,
            transcript_ref=None,
            window_tokens=8_000,
        ),
        acts.ToolExecInput(
            task_id="lh-x",
            turn_index=0,
            call=acts.ToolCallSummary(id="t1", name="list_dir", input={}),
            pre_sha=SHA,
            sandbox_id="sbx-1",
            window_tokens=8_000,
        ),
        acts.AppendResultsInput(
            task_id="lh-x", turn_index=0, transcript_ref=REF, results=[], window_tokens=8_000
        ),
    ]
    assert [i.window_tokens for i in inputs] == [8_000] * 3


def test_a_run_without_a_window_of_its_own_sends_none() -> None:
    turn = acts.LlmTurnInput(
        task_id="lh-x", turn_index=0, role=Role.CODER, spec=SPEC, transcript_ref=None
    )
    append = acts.AppendResultsInput(task_id="lh-x", turn_index=0, transcript_ref=REF, results=[])
    assert (turn.window_tokens, append.window_tokens) == (None, None)


def test_tuning_rejects_unknown_fields() -> None:
    # A misspelled knob would otherwise be dropped and the run would use the long timeouts.
    with pytest.raises(ValidationError):
        acts.Tuning.model_validate({"heartbeat_timeout": 2})


# ---------- payload types ----------
def test_a_tool_call_summary_carries_its_arguments_inline_or_by_ref() -> None:
    inline = acts.ToolCallSummary(id="t1", name="read_file", input={"path": "a.py"})
    no_arguments = acts.ToolCallSummary(id="t2", name="list_dir", input={})
    by_ref = acts.ToolCallSummary(id="t3", name="edit_file", args_ref=REF)
    assert inline.args_ref is None
    assert no_arguments.input == {}
    assert by_ref.input is None


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"input": {"path": "a.py"}, "args_ref": REF}],
    ids=["neither", "both"],
)
def test_a_tool_call_summary_needs_exactly_one_of_input_and_args_ref(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        acts.ToolCallSummary(id="t1", name="read_file", **kwargs)  # type: ignore[arg-type]


def test_finish_summary_reads_the_text_the_model_gave() -> None:
    call = acts.ToolCallSummary(id="t", name="finish", input={"summary": "  fixed add()  "})
    assert acts.finish_summary(call) == "fixed add()"


@pytest.mark.parametrize(
    "given",
    [{}, {"summary": ""}, {"summary": "   "}, {"summary": 3}, None],
    ids=["missing", "empty", "blank", "not text", "by ref"],
)
def test_finish_summary_falls_back_when_the_model_gave_no_text(
    given: dict[str, object] | None,
) -> None:
    call = acts.ToolCallSummary(
        id="t", name="finish", input=given, args_ref=REF if given is None else None
    )
    assert acts.finish_summary(call) == acts.NO_SUMMARY


def test_the_cost_of_a_turn_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        acts.LlmTurnOutput(
            transcript_ref=REF, stop_reason="end_turn", usage=Usage(), cost_usd=-0.01
        )


SAMPLES: list[BaseModel] = [
    acts.PrepareWorkspaceOutput(sandbox_id="sbx-1", base_sha=SHA, head_sha=SHA),
    acts.PrepareWorkspaceOutput(sandbox_id="sbx-1", base_sha=SHA, head_sha=SHA, repo_map_ref=REF),
    acts.EnsureSandboxInput(task_id="lh-x", head_sha=SHA, sandbox_id="sbx-1", spec=SPEC),
    acts.EnsureSandboxInput(task_id="lh-x", head_sha=SHA, sandbox_id=None, spec=SPEC),
    acts.LlmTurnInput(
        task_id="lh-x",
        turn_index=3,
        role=Role.CODER,
        spec=SPEC,
        transcript_ref=REF,
        memory_ref=REF,
        hints=["alice: use pytest -x"],
        high_output_cap=True,
        paused_ref=REF,
    ),
    acts.LlmTurnOutput(
        transcript_ref=REF,
        tool_calls_ref=REF,
        tool_call_summaries=[
            acts.ToolCallSummary(id="t1", name="read_file", input={"path": "a.py"}),
            acts.ToolCallSummary(id="t2", name="edit_file", args_ref=REF),
        ],
        stop_reason="tool_use",
        usage=Usage(input_tokens=10, output_tokens=5),
        cost_usd=0.02,
        input_tokens_total=10,
        response_ref=REF,
    ),
    acts.LlmTurnOutput(
        transcript_ref=REF,
        stop_reason="refusal",
        stop_details=StopDetails(category="cyber", explanation="no"),
        raw_stop_reason="refusal",
        usage=Usage(),
        paused_ref=REF,
    ),
    acts.ToolExecInput(
        task_id="lh-x",
        turn_index=3,
        call=acts.ToolCallSummary(id="t1", name="read_file", input={"path": "a.py"}),
        pre_sha=SHA,
        sandbox_id="sbx-1",
    ),
    acts.AppendResultsInput(
        task_id="lh-x",
        turn_index=3,
        transcript_ref=REF,
        results=[
            acts.ToolResultSummary(call_id="t1", ok=True, output_ref=REF),
            acts.ToolResultSummary(call_id="t2", ok=False, output="finish needs a summary"),
        ],
        user_text="Use tools or call finish.",
    ),
    acts.CleanupInput(task_id="lh-x", sandbox_id=None, reason="cancelled by alice"),
    acts.Tuning(heartbeat_timeout_s=2.5),
    acts.Tuning(continue_as_new_every_turns=3),
    acts.Tuning(window_tokens=8_000),
    acts.BundleCheckpointInput(task_id="lh-x", turn_index=40),
    acts.RecordEventInput(
        task_id="lh-x", type="continue_as_new", payload={"turn_index": 40, "bundle": None}
    ),
    acts.CompactInput(
        task_id="lh-x",
        turn_index=7,
        role=Role.CODER,
        spec=SPEC,
        transcript_ref=REF,
        notes=NOTES,
        forced=True,
        script_ref=REF,
    ),
    acts.CompactInput(
        task_id="lh-x", turn_index=7, role=Role.CODER, spec=SPEC, transcript_ref=REF, notes=None
    ),
    acts.CompactOutput(
        transcript_ref=REF,
        notes=NOTES,
        status="repaired",
        tokens_before=5_000,
        tokens_after=2_000,
        folded_turns=6,
        cost_usd=0.003,
    ),
    acts.LlmTurnOutput(
        transcript_ref=REF,
        stop_reason="tool_use",
        usage=Usage(),
        context_decision="compact",
        request_tokens=4_321,
    ),
]


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda sample: type(sample).__name__)
def test_payloads_survive_the_pydantic_data_converter(sample: BaseModel) -> None:
    # The workflow calls activities by name, so the converter decodes with result_type and
    # the activity decodes with its parameter's type hint. Both read what this wrote.
    converter = pydantic_data_converter.payload_converter
    payloads = converter.to_payloads([sample])
    assert converter.from_payloads(payloads, [type(sample)]) == [sample]


def test_a_workspace_recorded_before_the_repo_map_existed_still_reads() -> None:
    # Workflow histories hold the output of earlier runs, and they must replay.
    old = {"sandbox_id": "sbx-1", "base_sha": SHA, "head_sha": SHA}

    assert acts.PrepareWorkspaceOutput.model_validate(old).repo_map_ref is None


# ---------- LH-204: what the activities added to the payloads ----------
def test_a_model_turn_needs_the_spec_to_build_its_prompt() -> None:
    with pytest.raises(ValidationError):
        acts.LlmTurnInput.model_validate(
            {"task_id": "lh-x", "turn_index": 0, "role": "coder", "transcript_ref": None}
        )


def test_a_model_turn_starts_without_a_raised_cap_or_paused_text() -> None:
    first = acts.LlmTurnInput(
        task_id="lh-x", turn_index=0, role=Role.CODER, spec=SPEC, transcript_ref=None
    )
    assert (first.high_output_cap, first.paused_ref) == (False, None)


def test_a_tool_result_summary_carries_its_text_inline_or_by_ref() -> None:
    inline = acts.ToolResultSummary(call_id="t1", ok=True, output="ok")
    empty = acts.ToolResultSummary(call_id="t2", ok=True, output="")
    by_ref = acts.ToolResultSummary(call_id="t3", ok=True, output_ref=REF)
    assert (inline.output_ref, empty.output, by_ref.output) == (None, "", None)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"output": "x", "output_ref": REF}],
    ids=["neither", "both"],
)
def test_a_tool_result_summary_needs_exactly_one_of_output_and_output_ref(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        acts.ToolResultSummary(call_id="t1", ok=True, **kwargs)  # type: ignore[arg-type]


def test_a_stored_result_is_summarized_by_its_ref() -> None:
    stored = ToolResult(
        call_id="t1", ok=False, output="preview...", output_ref=REF, exit_code=1, post_sha=SHA
    )
    assert acts.summarize_result(stored) == acts.ToolResultSummary(
        call_id="t1", ok=False, output_ref=REF
    )


def test_a_result_without_a_stored_text_is_summarized_by_its_text() -> None:
    # The bad-finish answer is made by the workflow itself, and nothing stores it.
    plain = ToolResult(call_id="t1", ok=False, output="finish needs a summary")
    assert acts.summarize_result(plain) == acts.ToolResultSummary(
        call_id="t1", ok=False, output="finish needs a summary"
    )


def test_a_turn_with_many_large_results_stays_far_below_the_payload_limit() -> None:
    # Fifty calls whose outputs are 15 KB each would be 750 KB inline. By ref they are a few KB.
    results = [
        ToolResult(call_id=f"toolu_{n:03d}", ok=True, output="x" * 15_000, output_ref=REF)
        for n in range(50)
    ]
    payload = acts.AppendResultsInput(
        task_id="lh-x",
        turn_index=3,
        transcript_ref=REF,
        results=[acts.summarize_result(r) for r in results],
    )
    assert_small(payload)
    assert len(payload.model_dump_json()) < DEFAULT_LIMIT // 10


def test_the_inline_limits_leave_room_under_the_payload_limit() -> None:
    # CONTRIBUTING.md rule 5: nothing over 16 KB crosses inline. The total for one reply keeps the
    # llm_turn output, with its per-call overhead, under the 256 KB cap (I2).
    assert acts.INLINE_LIMIT_BYTES == 16_000
    assert acts.INLINE_ARGS_TOTAL_BYTES < DEFAULT_LIMIT // 2
    assert acts.INLINE_ARGS_TOTAL_BYTES >= acts.INLINE_LIMIT_BYTES
    assert acts.OUTPUT_PREVIEW_CHARS < acts.INLINE_LIMIT_BYTES


# ---------- LH-206: sandbox recovery ----------
def test_the_workflow_and_tool_exec_agree_on_the_error_type_of_a_lost_sandbox() -> None:
    # tool_exec raises ApplicationError(type=SANDBOX_GONE), and the workflow reads the same type
    # from the cause of the ActivityError. The name is also in the plan (section 3.7 story).
    assert acts.SANDBOX_GONE == "SandboxGoneError"


def test_a_sandbox_is_recreated_at_most_twice_for_one_tool_call() -> None:
    assert acts.MAX_SANDBOX_RECREATIONS == 2


@pytest.mark.parametrize("missing", ["task_id", "head_sha", "sandbox_id", "spec"])
def test_ensure_sandbox_names_every_field_of_its_input(missing: str) -> None:
    # The spec is there for the setup command. sandbox_id may be None but not absent, so a
    # caller cannot forget it.
    fields = {"task_id": "lh-x", "head_sha": SHA, "sandbox_id": "sbx-1", "spec": SPEC}
    del fields[missing]
    with pytest.raises(ValidationError):
        acts.EnsureSandboxInput.model_validate(fields)


# ---------- LH-208: a script that travels with a task ----------
def test_a_model_turn_starts_without_a_script() -> None:
    turn = acts.LlmTurnInput(
        task_id="lh-x", turn_index=0, role=Role.CODER, spec=SPEC, transcript_ref=None
    )
    assert turn.script_ref is None


def test_a_script_ref_survives_the_pydantic_data_converter() -> None:
    turn = acts.LlmTurnInput(
        task_id="lh-x",
        turn_index=0,
        role=Role.CODER,
        spec=SPEC,
        transcript_ref=None,
        script_ref=REF,
    )
    converter = pydantic_data_converter.payload_converter
    payloads = converter.to_payloads([turn])
    assert converter.from_payloads(payloads, [acts.LlmTurnInput]) == [turn]


def test_the_script_and_the_tuning_have_memo_keys_of_their_own() -> None:
    assert acts.SCRIPT_MEMO_KEY == "script"
    assert acts.SCRIPT_MEMO_KEY != acts.TUNING_MEMO_KEY


# ---------- LH-406: the context mode ----------
def test_a_model_turn_and_a_compaction_name_no_mode_unless_the_run_does() -> None:
    turn = acts.LlmTurnInput(
        task_id="lh-x", turn_index=0, role=Role.CODER, spec=SPEC, transcript_ref=None
    )
    compaction = acts.CompactInput(
        task_id="lh-x", turn_index=0, role=Role.CODER, spec=SPEC, transcript_ref=REF
    )
    assert turn.context_mode is None
    assert compaction.context_mode is None


def test_a_context_mode_survives_the_pydantic_data_converter() -> None:
    turn = acts.LlmTurnInput(
        task_id="lh-x",
        turn_index=0,
        role=Role.CODER,
        spec=SPEC,
        transcript_ref=None,
        context_mode=ContextMode.NO_COMPACTION,
    )
    compaction = acts.CompactInput(
        task_id="lh-x",
        turn_index=0,
        role=Role.CODER,
        spec=SPEC,
        transcript_ref=REF,
        context_mode=ContextMode.NONE,
    )
    converter = pydantic_data_converter.payload_converter
    assert converter.from_payloads(converter.to_payloads([turn]), [acts.LlmTurnInput]) == [turn]
    assert converter.from_payloads(converter.to_payloads([compaction]), [acts.CompactInput]) == [
        compaction
    ]


# ---------- LH-307: the poller ----------
def test_poll_once_backs_off_briefly_because_the_next_tick_is_a_retry_too() -> None:
    retry = acts.POLL_ONCE.retry
    assert retry.initial_interval == timedelta(seconds=5)
    assert retry.maximum_interval == timedelta(seconds=30)
    assert retry.backoff_coefficient == 2.0


def test_poll_once_does_not_retry_what_a_retry_cannot_fix() -> None:
    never = set(acts.POLL_ONCE.retry.non_retryable_error_types or [])
    assert {acts.GITHUB_FATAL, acts.POLLER_CONFIG} <= never
    assert acts.GITHUB_RETRYABLE not in never


def test_the_error_types_poll_once_raises_have_fixed_names() -> None:
    # The activity raises these as ApplicationError types and the policy matches on the names.
    # side_effects.py uses the same two GitHub names.
    assert (acts.GITHUB_FATAL, acts.GITHUB_RETRYABLE) == ("GitHubFatal", "GitHubRetryable")
    assert acts.POLLER_CONFIG == "PollerConfigError"


def test_a_poll_run_may_last_as_long_as_all_attempts_of_poll_once() -> None:
    # If the run timeout were shorter, the server would cut a poll that is still retrying.
    act = acts.POLL_ONCE
    attempts = act.retry.maximum_attempts
    waits = sum(
        min(
            act.retry.initial_interval.total_seconds() * act.retry.backoff_coefficient**n,
            act.retry.maximum_interval.total_seconds(),
        )
        for n in range(attempts - 1)
    )
    worst = attempts * act.start_to_close.total_seconds() + waits
    assert acts.POLLER_RUN_TIMEOUT.total_seconds() >= worst


def test_the_poller_schedule_has_the_id_of_the_plan_and_a_short_catch_up_window() -> None:
    assert acts.POLLER_SCHEDULE_ID == "longhaul-poller"
    assert acts.POLLER_CATCHUP_WINDOW.total_seconds() == 120


def test_a_poll_result_counts_and_is_small() -> None:
    result = acts.PollResult(repos=2, issues=5, started=3, known=2)
    converter = pydantic_data_converter.payload_converter
    payloads = converter.to_payloads([result])
    assert converter.from_payloads(payloads, [acts.PollResult]) == [result]
    assert acts.PollResult() == acts.PollResult(repos=0, issues=0, started=0, known=0)
    assert_small(result)


def test_a_poll_result_cannot_count_below_zero() -> None:
    with pytest.raises(ValidationError):
        acts.PollResult(started=-1)


# ---------- LH-404: the context decision and the compact activity ----------
def test_a_model_turn_says_ok_until_it_says_otherwise() -> None:
    out = acts.LlmTurnOutput(transcript_ref=REF, stop_reason="end_turn", usage=Usage())
    assert out.context_decision == "ok"
    assert out.request_tokens == 0


@pytest.mark.parametrize("decision", ["ok", "compact", "force"])
def test_a_model_turn_may_ask_for_compaction(decision: str) -> None:
    out = acts.LlmTurnOutput(
        transcript_ref=REF, stop_reason="end_turn", usage=Usage(), context_decision=decision
    )
    assert out.context_decision == decision


def test_a_model_turn_cannot_ask_for_anything_else() -> None:
    with pytest.raises(ValidationError):
        acts.LlmTurnOutput(
            transcript_ref=REF, stop_reason="end_turn", usage=Usage(), context_decision="halve"
        )


def test_a_projection_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        acts.LlmTurnOutput(
            transcript_ref=REF, stop_reason="end_turn", usage=Usage(), request_tokens=-1
        )


def test_compact_backs_off_like_llm_turn_and_does_not_retry_what_a_retry_cannot_fix() -> None:
    retry = acts.COMPACT.retry
    assert retry.initial_interval == acts.LLM_TURN.retry.initial_interval
    assert retry.maximum_interval == acts.LLM_TURN.retry.maximum_interval
    never = set(retry.non_retryable_error_types or [])
    assert {
        ProviderErrorKind.INVALID_REQUEST.value,
        ProviderErrorKind.AUTH.value,
        ProviderErrorKind.CONTEXT_TOO_LONG.value,
    } <= never
    assert "PayloadTooLargeError" in never


def test_a_forced_compaction_keeps_the_last_two_turns() -> None:
    # F6: when the window is too full to wait, keep only the most recent 2 turns and the notes.
    assert acts.FORCED_KEEP_RECENT_TURNS == 2


def test_compaction_has_four_outcomes() -> None:
    out = acts.CompactOutput(
        transcript_ref=REF,
        notes=None,
        status="unchanged",
        tokens_before=10,
        tokens_after=10,
        folded_turns=0,
    )
    assert out.cost_usd == 0.0
    with pytest.raises(ValidationError):
        acts.CompactOutput(
            transcript_ref=REF,
            notes=None,
            status="lost",  # type: ignore[arg-type]
            tokens_before=10,
            tokens_after=10,
            folded_turns=0,
        )


def test_the_compaction_outcomes_of_the_activity_include_those_of_compact() -> None:
    # acts.py cannot import compact.py, which reaches the tool registry, so the words are
    # written twice and this keeps them equal.
    assert set(get_args(compact.CompactStatus)) | {"unchanged"} == set(get_args(acts.CompactStatus))


# ---------- LH-306: the GitHub activities ----------
#
# They are registered only on a worker that has a GitHub token, so they are a tuple of their own
# and `ALL` stays what every worker runs.
def test_the_github_activities_are_those_of_section_3_7_and_deliver() -> None:
    names = [act.name for act in acts.GITHUB]
    assert sorted(names) == [
        "add_label",
        "build_pr_branch",
        "create_pr",
        "deliver",
        "post_comment",
        "post_plan",
        "remove_label",
    ]
    assert not {act.name for act in acts.ALL} & set(names)


@pytest.mark.parametrize(
    ("act", "arg_type", "result_type"),
    [
        (acts.POST_COMMENT, CommentInput, str),
        (acts.ADD_LABEL, LabelInput, str),
        (acts.REMOVE_LABEL, LabelInput, str),
        (acts.CREATE_PR, PullRequestInput, str),
        (acts.BUILD_PR_BRANCH, acts.BuildPrBranchInput, acts.PrBranchOutput),
        (acts.DELIVER, acts.DeliverInput, str),
        (acts.POST_PLAN, acts.PostPlanInput, str),
    ],
    ids=lambda value: getattr(value, "name", None) or getattr(value, "__name__", ""),
)
def test_each_github_activity_names_its_payload_types(
    act: acts.Act, arg_type: type, result_type: type
) -> None:
    assert act.arg_type is arg_type
    assert act.result_type is result_type


def test_the_inputs_of_the_writes_are_the_ones_side_effects_uses() -> None:
    # The workflow cannot import side_effects, so the models live in a module both import.
    assert side_effects.CommentInput is CommentInput
    assert side_effects.LabelInput is LabelInput
    assert side_effects.PullRequestInput is PullRequestInput


@pytest.mark.parametrize(
    ("act", "start_to_close_s", "heartbeat_s", "attempts"),
    [
        pytest.param(acts.POST_COMMENT, 2 * 60, None, 10, id="post_comment"),
        pytest.param(acts.ADD_LABEL, 2 * 60, None, 10, id="add_label"),
        pytest.param(acts.REMOVE_LABEL, 2 * 60, None, 10, id="remove_label"),
        pytest.param(acts.CREATE_PR, 2 * 60, None, 10, id="create_pr"),
        pytest.param(acts.BUILD_PR_BRANCH, 5 * 60, None, 3, id="build_pr_branch"),
        # Not in section 3.7. deliver pushes a branch (git's own limit is 10 minutes) and then
        # does what create_pr does, so a killed worker has to be noticed in a minute.
        pytest.param(acts.DELIVER, 10 * 60, 60, 5, id="deliver"),
        # The plan comment is a post_comment whose body the activity renders.
        pytest.param(acts.POST_PLAN, 2 * 60, None, 10, id="post_plan"),
    ],
)
def test_github_timeouts_and_attempts_match_section_3_7(
    act: acts.Act, start_to_close_s: int, heartbeat_s: int | None, attempts: int
) -> None:
    assert act.start_to_close == timedelta(seconds=start_to_close_s)
    assert act.heartbeat == (None if heartbeat_s is None else timedelta(seconds=heartbeat_s))
    assert act.retry.maximum_attempts == attempts


@pytest.mark.parametrize(
    "act", [acts.POST_COMMENT, acts.ADD_LABEL, acts.REMOVE_LABEL, acts.CREATE_PR, acts.POST_PLAN]
)
def test_github_writes_back_off_to_five_minutes(act: acts.Act) -> None:
    assert act.retry.maximum_interval == timedelta(minutes=5)
    assert act.retry.backoff_coefficient == 2.0
    assert act.retry.initial_interval == timedelta(seconds=5)


@pytest.mark.parametrize("act", acts.GITHUB, ids=lambda act: act.name)
def test_no_github_activity_retries_what_a_retry_cannot_fix(act: acts.Act) -> None:
    never = set(act.retry.non_retryable_error_types or [])
    assert {PayloadTooLargeError.__name__, acts.GITHUB_FATAL} <= never
    assert acts.GITHUB_RETRYABLE not in never


def test_the_github_error_types_are_the_ones_side_effects_raises() -> None:
    assert GitHubFatal.__name__ == acts.GITHUB_FATAL
    assert GitHubRetryable.__name__ == acts.GITHUB_RETRYABLE


@pytest.mark.parametrize("act", acts.GITHUB, ids=lambda act: act.name)
def test_github_activities_are_not_local(act: acts.Act) -> None:
    assert not act.local


def test_tuning_shortens_the_heartbeat_of_deliver_only() -> None:
    tuning = acts.Tuning(heartbeat_timeout_s=2)
    for act in acts.GITHUB:
        expected = timedelta(seconds=2) if act is acts.DELIVER else None
        assert acts.options(act, tuning)["heartbeat_timeout"] == expected


GITHUB_SAMPLES: list[BaseModel] = [
    acts.BuildPrBranchInput(
        task_id="lh-x", repo="acme/calc", summary="Fixed it.", base_sha="b" * 40, head_sha=SHA
    ),
    acts.PrBranchOutput(
        branch="longhaul-pr/42",
        head_sha=SHA,
        base_sha="b" * 40,
        base_branch="main",
        title="fix(calc): fix it",
    ),
    acts.DeliverInput(
        task_id="lh-x",
        repo="acme/calc",
        issue_number=42,
        summary="Fixed it.",
        branch="longhaul-pr/42",
        base_branch="main",
        base_sha="b" * 40,
        head_sha=SHA,
        title="fix(calc): fix it",
    ),
    CommentInput(task_id="lh-x", repo="acme/calc", number=42, body="hi", idempotency_key="k:a:1"),
    LabelInput(task_id="lh-x", repo="acme/calc", number=42, label="l", idempotency_key="k:a:1"),
]


@pytest.mark.parametrize("sample", GITHUB_SAMPLES, ids=lambda sample: type(sample).__name__)
def test_github_payloads_survive_the_pydantic_data_converter(sample: BaseModel) -> None:
    converter = pydantic_data_converter.payload_converter
    payloads = converter.to_payloads([sample])
    assert converter.from_payloads(payloads, [type(sample)]) == [sample]


def test_nothing_to_deliver_is_named_after_the_error_class() -> None:
    assert NothingToDeliverError.__name__ == acts.NOTHING_TO_DELIVER


# ---------- LH-501: the planner ----------
def test_the_planner_turn_limit_follows_the_configured_one() -> None:
    # Same reason as the tool timeout: the workflow cannot read the config file.
    config = load_config(ROOT / "config" / "longhaul.example.yaml")
    assert config.loop.planner_max_turns == acts.PLANNER_MAX_TURNS == 20


def test_planning_is_switched_on_by_a_memo_key_of_its_own() -> None:
    assert acts.PLAN_MEMO_KEY == "plan"
    assert len({acts.PLAN_MEMO_KEY, acts.SCRIPT_MEMO_KEY, acts.TUNING_MEMO_KEY}) == 3


def test_a_tool_call_is_not_read_only_unless_the_workflow_says_so() -> None:
    call = acts.ToolCallSummary(id="t1", name="read_file", input={"path": "a.py"})
    default = acts.ToolExecInput(
        task_id="lh-x", turn_index=0, call=call, pre_sha=SHA, sandbox_id="sbx-1"
    )
    planning = default.model_copy(update={"read_only": True})
    assert default.read_only is False
    assert planning.read_only is True


def test_the_plan_activities_are_registered_where_their_inputs_need_a_token() -> None:
    # Storing a plan needs the database only. Posting it needs a GitHub token.
    assert acts.STORE_PLAN in acts.ALL
    assert acts.RECORD_STEP in acts.ALL and acts.RECORD_STEP not in acts.GITHUB
    assert acts.POST_PLAN in acts.GITHUB
    assert acts.STORE_PLAN not in acts.GITHUB and acts.POST_PLAN not in acts.ALL


def test_a_plan_has_a_version_from_one() -> None:
    plan = Plan(summary="s", steps=[], acceptance_checks=["pytest -q"])
    with pytest.raises(ValidationError):
        acts.StorePlanInput(task_id="lh-x", version=0, plan=plan)
    with pytest.raises(ValidationError):
        acts.PostPlanInput(task_id="lh-x", repo="acme/calc", number=42, version=0, plan=plan)


PLAN = Plan(
    summary="add() subtracts",
    steps=[PlanStep(id="s1", description="Fix add()", done_when="the tests pass")],
    acceptance_checks=["pytest -q"],
    risks=["none known"],
)
PLAN_SAMPLES: list[BaseModel] = [
    acts.StorePlanInput(task_id="lh-x", version=1, plan=PLAN),
    acts.PostPlanInput(task_id="lh-x", repo="acme/calc", number=42, version=1, plan=PLAN),
    acts.RecordStepInput(
        task_id="lh-x", step_id="s1", status="done", note="ok", head_sha=SHA, turn_index=4
    ),
    acts.BuildPrBranchInput(
        task_id="lh-x", repo="acme/calc", summary="s", base_sha=SHA, head_sha=SHA, plan=PLAN
    ),
    acts.ToolExecInput(
        task_id="lh-x",
        turn_index=2,
        call=acts.ToolCallSummary(id="t1", name="run_command", input={"command": "ls"}),
        pre_sha=SHA,
        sandbox_id="sbx-1",
        read_only=True,
    ),
    acts.LlmTurnInput(
        task_id="lh-x",
        turn_index=0,
        role=Role.PLANNER,
        spec=SPEC,
        transcript_ref=None,
        plan=PLAN,
    ),
]


@pytest.mark.parametrize("sample", PLAN_SAMPLES, ids=lambda sample: type(sample).__name__)
def test_planner_payloads_survive_the_pydantic_data_converter(sample: BaseModel) -> None:
    converter = pydantic_data_converter.payload_converter
    payloads = converter.to_payloads([sample])
    assert converter.from_payloads(payloads, [type(sample)]) == [sample]
    assert_small(sample)


# ---------- memory in the payloads (LH-604) ----------
@pytest.mark.phase6
def test_a_workspace_output_from_before_memory_still_parses_and_loads_no_memory() -> None:
    # A history that was recorded before LH-604 holds this JSON. The workflow decides whether to
    # call load_memory from it, so what it lacks must read as "no memory" and the old command
    # sequence must not change.
    old = '{"sandbox_id": "sbx-1", "base_sha": "' + SHA + '", "head_sha": "' + SHA + '"}'

    out = acts.PrepareWorkspaceOutput.model_validate_json(old)

    assert (out.memory_enabled, out.test_command, out.repo_map_ref) == (False, None, None)


@pytest.mark.phase6
def test_the_workspace_output_says_whether_the_worker_keeps_memory_and_the_test_command() -> None:
    out = acts.PrepareWorkspaceOutput(
        sandbox_id="sbx-1",
        base_sha=SHA,
        head_sha=SHA,
        repo_map_ref=REF,
        memory_enabled=True,
        test_command="pytest -q",
    )

    back = acts.PrepareWorkspaceOutput.model_validate_json(out.model_dump_json())

    assert back == out
    assert (back.memory_enabled, back.test_command) == (True, "pytest -q")


@pytest.mark.phase6
def test_load_memory_takes_the_spec_and_the_map_of_the_workspace() -> None:
    inp = acts.LoadMemoryInput(spec=SPEC, repo_map_ref=REF)
    assert acts.LoadMemoryInput.model_validate_json(inp.model_dump_json()) == inp
    assert acts.LoadMemoryInput(spec=SPEC).repo_map_ref is None


@pytest.mark.phase6
def test_the_model_turn_and_the_compaction_see_the_same_memory_block() -> None:
    # The compactor counts the tokens of the request it would be part of, system prompt included.
    turn = acts.LlmTurnInput(
        task_id="lh-x", turn_index=0, role=Role.CODER, spec=SPEC, transcript_ref=None
    )
    assert turn.memory_ref is None
    compact = acts.CompactInput(
        task_id="lh-x", turn_index=3, role=Role.CODER, spec=SPEC, transcript_ref=REF
    )
    assert compact.memory_ref is None
    assert acts.CompactInput(**{**compact.model_dump(), "memory_ref": REF}).memory_ref == REF


# ---------- the coder with a plan (LH-503) ----------
@pytest.mark.phase5
def test_a_step_update_names_one_of_the_statuses_of_the_plan() -> None:
    with pytest.raises(ValidationError):
        acts.RecordStepInput(
            task_id="lh-x", step_id="s1", status="finished", head_sha=SHA, turn_index=1
        )


@pytest.mark.phase5
def test_the_delivery_of_a_task_without_a_plan_asks_for_one_commit() -> None:
    inp = acts.BuildPrBranchInput(
        task_id="lh-x", repo="acme/calc", summary="s", base_sha=SHA, head_sha=SHA
    )
    assert inp.plan is None


@pytest.mark.phase5
def test_status_is_a_task_status_that_also_carries_the_plan() -> None:
    plain = TaskStatus(
        task_id="lh-x",
        phase=Phase.CODING,
        turn_index=1,
        cost_usd=0.0,
        last_checkpoint=None,
        plan_version=1,
    )
    planned = acts.PlannedStatus(**plain.model_dump(), plan=PLAN)

    assert isinstance(planned, TaskStatus)
    assert planned.plan == PLAN
    assert acts.PlannedStatus(**plain.model_dump()).plan is None


@pytest.mark.phase5
def test_a_client_reads_the_plan_when_it_asks_for_it_and_the_status_when_it_does_not() -> None:
    converter = pydantic_data_converter.payload_converter
    plain = TaskStatus(
        task_id="lh-x",
        phase=Phase.CODING,
        turn_index=1,
        cost_usd=0.0,
        last_checkpoint=SHA,
        plan_version=1,
    )
    planned = acts.PlannedStatus(**plain.model_dump(), plan=PLAN)
    payloads = converter.to_payloads([planned])

    assert converter.from_payloads(payloads, [acts.PlannedStatus]) == [planned]
    assert converter.from_payloads(payloads, [TaskStatus]) == [plain]


# ---------- what tool_exec says to the stuck detector (LH-505) ----------
def test_a_tool_exec_result_is_a_tool_result_with_one_more_field() -> None:
    result = acts.ToolExecResult(call_id="c1", ok=True, output="o")
    assert isinstance(result, ToolResult)
    assert result.signal is None
    fact = CallSignal(tests_passed=3, edited_regions=[("a.py", 1, 2)])
    back = pydantic_data_converter.payload_converter.from_payload(
        pydantic_data_converter.payload_converter.to_payload(
            result.model_copy(update={"signal": fact})
        ),
        acts.ToolExecResult,
    )
    assert back.signal == fact


def test_a_plain_tool_result_reads_as_one_without_facts() -> None:
    # A history from before the detector, and the stubs of the workflow tests, return a ToolResult.
    plain = ToolResult(call_id="c1", ok=True, output="o")
    converter = pydantic_data_converter.payload_converter
    back = converter.from_payload(converter.to_payload(plain), acts.ToolExecResult)
    assert back.signal is None and back.output == "o"


# ---------- LH-502: the plan gate ----------
@pytest.mark.phase5
def test_the_plan_wait_follows_the_configured_hint_wait() -> None:
    # Same reason as the tool timeout: the workflow cannot read the config file.
    config = load_config(ROOT / "config" / "longhaul.example.yaml")
    assert config.stuck.hint_wait_hours == acts.HINT_WAIT_HOURS == 24


@pytest.mark.phase5
def test_the_plan_wait_is_a_duration_in_hours() -> None:
    assert acts.hint_wait() == timedelta(hours=24)


@pytest.mark.phase5
def test_the_decision_is_registered_where_its_input_needs_only_the_database() -> None:
    # Recording who decided a plan needs the database and no GitHub token.
    assert acts.DECIDE_PLAN in acts.ALL
    assert acts.DECIDE_PLAN not in acts.GITHUB


@pytest.mark.phase5
def test_a_decision_names_a_plan_version_from_one() -> None:
    with pytest.raises(ValidationError):
        acts.DecidePlanInput(task_id="lh-x", version=0, approved=True, by="alice")
    inp = acts.DecidePlanInput(task_id="lh-x", version=2, approved=False, by="alice")
    assert acts.DecidePlanInput.model_validate_json(inp.model_dump_json()) == inp
    assert_small(inp)


@pytest.mark.phase5
def test_a_decision_does_not_retry_an_oversized_payload() -> None:
    assert PayloadTooLargeError.__name__ in (acts.DECIDE_PLAN.retry.non_retryable_error_types or [])


# ---------- LH-504: the verifier ----------
def test_the_verify_round_limit_follows_the_configured_one() -> None:
    # Same reason as the planner's turn limit: the workflow cannot read the config file.
    config = load_config(ROOT / "config" / "longhaul.example.yaml")
    assert config.loop.max_verify_rounds == acts.MAX_VERIFY_ROUNDS == 3


def test_a_review_brief_is_small_and_names_its_outputs_by_ref() -> None:
    ref = BlobRef(
        key="tasks/lh-x/turns/0003/tool-verify1-tests-output.txt", sha256="0" * 64, size=9
    )
    brief = acts.ReviewBrief(
        checks=[
            acts.CheckRun(
                command="pytest -q",
                call_id="verify1-tests",
                passed=False,
                exit_code=1,
                output_ref=ref,
            )
        ],
        diff=acts.CheckRun(command="git_diff", call_id="verify1-diff", passed=True, output_ref=ref),
    )
    converter = pydantic_data_converter.payload_converter
    (payload,) = converter.to_payloads([brief])
    assert converter.from_payloads([payload], [acts.ReviewBrief]) == [brief]
    assert len(payload.data) < 1_000


def test_a_model_turn_has_no_review_unless_the_workflow_gives_one() -> None:
    inp = acts.LlmTurnInput(
        task_id="lh-x", turn_index=0, role=Role.VERIFIER, spec=SPEC, transcript_ref=None
    )
    assert inp.review is None


def test_a_delivery_is_not_verified_unless_the_workflow_says_so() -> None:
    inp = acts.DeliverInput(
        task_id="lh-x",
        repo="acme/calc",
        issue_number=42,
        summary="s",
        branch="longhaul-pr/42",
        base_branch="main",
        base_sha=SHA,
        head_sha=SHA,
        title="fix(calc): fix it",
    )
    assert inp.verified_tests is None
    assert inp.model_copy(update={"verified_tests": "3 passed"}).verified_tests == "3 passed"
