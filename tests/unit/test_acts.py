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

from longhaul.context import compact
from longhaul.contracts import (
    BlobRef,
    ProgressNotes,
    ProviderErrorKind,
    RepoSettings,
    Role,
    StopDetails,
    TaskSpec,
    ToolResult,
    Usage,
)
from longhaul.durable import acts
from longhaul.durable.payload_guard import DEFAULT_LIMIT, PayloadTooLargeError, assert_small
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
        "ensure_sandbox",
        "llm_turn",
        "poll_once",
        "prepare_workspace",
        "record_event",
        "tool_exec",
    ]
    assert len(set(names)) == len(names)


@pytest.mark.parametrize(
    ("act", "arg_type", "result_type"),
    [
        (acts.PREPARE_WORKSPACE, TaskSpec, acts.PrepareWorkspaceOutput),
        (acts.ENSURE_SANDBOX, acts.EnsureSandboxInput, str),
        (acts.LLM_TURN, acts.LlmTurnInput, acts.LlmTurnOutput),
        (acts.TOOL_EXEC, acts.ToolExecInput, ToolResult),
        (acts.APPEND_RESULTS, acts.AppendResultsInput, BlobRef),
        (acts.CLEANUP, acts.CleanupInput, NoneType),
        (acts.BUNDLE_CHECKPOINT, acts.BundleCheckpointInput, BlobRef),
        (acts.RECORD_EVENT, acts.RecordEventInput, NoneType),
        (acts.POLL_ONCE, NoneType, acts.PollResult),
        (acts.COMPACT, acts.CompactInput, acts.CompactOutput),
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
