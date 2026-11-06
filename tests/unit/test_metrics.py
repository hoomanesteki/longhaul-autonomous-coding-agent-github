"""telemetry/metrics.py: the Prometheus metrics of the worker (LH-704).

Each test makes its own Metrics, which has a registry of its own, so a count never leaks from
one test to the next. tests/integration/test_metrics.py reads the same numbers from /metrics
after a run with the real activities.
"""

import dataclasses
import socket
import urllib.error
import urllib.request
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput

from longhaul.contracts import Role, Usage
from longhaul.durable import acts
from longhaul.telemetry import metrics

pytestmark = [pytest.mark.unit, pytest.mark.phase7]

REPO = "acme/calc"

# The metric names and kinds of plan story LH-704, as Prometheus writes them in /metrics.
EXPOSED = {
    "longhaul_tasks_total": "counter",
    "longhaul_active_tasks": "gauge",
    "longhaul_turns_total": "counter",
    "longhaul_llm_cost_usd_total": "counter",
    "longhaul_llm_errors_total": "counter",
    "longhaul_tool_duration_seconds": "histogram",
    "longhaul_compactions_total": "counter",
    "longhaul_stuck_total": "counter",
    "longhaul_sandbox_recreated_total": "counter",
    "longhaul_activity_retries_total": "counter",
    "longhaul_cache_ratio": "histogram",
}


def value(m: metrics.Metrics, name: str, **labels: str) -> float:
    """One sample of the registry, or 0.0 when there is none (nothing was counted yet)."""
    return m.registry.get_sample_value(name, labels) or 0.0


@pytest.fixture
def m() -> metrics.Metrics:
    return metrics.Metrics()


def scrape(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return str(response.read().decode())


# ---------- what is exposed ----------
def test_every_metric_of_the_story_is_exposed_with_its_kind(m: metrics.Metrics) -> None:
    text = m.render()
    for name, kind in EXPOSED.items():
        assert f"# TYPE {name} {kind}" in text, name


def test_a_new_metrics_object_exposes_no_counts_it_did_not_make(m: metrics.Metrics) -> None:
    assert value(m, "longhaul_active_tasks") == 0
    assert value(m, "longhaul_sandbox_recreated_total") == 0
    assert value(m, "longhaul_compactions_total") == 0
    # The labels that come from a fixed set start at zero, so a rate() has a first sample.
    assert m.registry.get_sample_value("longhaul_llm_errors_total", {"kind": "overloaded"}) == 0.0
    assert m.registry.get_sample_value("longhaul_stuck_total", {"reason": "no_progress"}) == 0.0
    assert m.registry.get_sample_value("longhaul_turns_total", {"role": "coder"}) == 0.0


def test_the_labels_are_the_ones_of_the_story(m: metrics.Metrics) -> None:
    m.finished("run-1", "lh-a-1", REPO, "pr_opened")
    m.add_cost("claude-sonnet-5-5", REPO, 0.5)
    m.count_turn(Role.CODER, Usage(input_tokens=10))
    m.count_llm_error("overloaded")
    m.observe_tool("read_file", 0.2)
    m.count_retry("llm_turn")
    m.count_event("stuck", {"reason": "edit_churn"})
    text = m.render()
    assert 'longhaul_tasks_total{outcome="pr_opened",repo="acme/calc"} 1.0' in text
    assert 'longhaul_llm_cost_usd_total{model="claude-sonnet-5-5",repo="acme/calc"} 0.5' in text
    assert 'longhaul_turns_total{role="coder"} 1.0' in text
    assert 'longhaul_llm_errors_total{kind="overloaded"} 1.0' in text
    assert 'longhaul_tool_duration_seconds_count{tool="read_file"} 1.0' in text
    assert 'longhaul_activity_retries_total{activity="llm_turn"} 1.0' in text
    assert 'longhaul_stuck_total{reason="edit_churn"} 1.0' in text


# ---------- model calls ----------
def test_a_turn_counts_for_its_role_and_adds_its_cache_ratio(m: metrics.Metrics) -> None:
    # 300 of the 400 prompt tokens came from the cache.
    m.count_turn(Role.CODER, Usage(input_tokens=100, cache_read_tokens=300))
    m.count_turn(Role.VERIFIER, Usage(input_tokens=50))

    assert value(m, "longhaul_turns_total", role="coder") == 1
    assert value(m, "longhaul_turns_total", role="verifier") == 1
    assert value(m, "longhaul_cache_ratio_count") == 2
    assert value(m, "longhaul_cache_ratio_sum") == pytest.approx(0.75)
    # The bucket edges include 0, so a call that used no cache is told from a low ratio.
    assert value(m, "longhaul_cache_ratio_bucket", le="0.0") == 1
    assert value(m, "longhaul_cache_ratio_bucket", le="0.8") == 2


def test_cost_adds_up_per_model_and_repo(m: metrics.Metrics) -> None:
    m.add_cost("claude-sonnet-5-5", REPO, 0.25)
    m.add_cost("claude-sonnet-5-5", REPO, 0.5)
    m.add_cost("claude-haiku-5-5", REPO, 0.01)
    m.add_cost("claude-sonnet-5-5", "acme/other", 1.0)

    assert value(m, "longhaul_llm_cost_usd_total", model="claude-sonnet-5-5", repo=REPO) == 0.75
    assert value(m, "longhaul_llm_cost_usd_total", model="claude-haiku-5-5", repo=REPO) == 0.01
    assert (
        value(m, "longhaul_llm_cost_usd_total", model="claude-sonnet-5-5", repo="acme/other") == 1
    )


def test_a_call_that_cost_nothing_adds_nothing(m: metrics.Metrics) -> None:
    m.add_cost("claude-sonnet-5-5", REPO, 0.0)
    assert value(m, "longhaul_llm_cost_usd_total", model="claude-sonnet-5-5", repo=REPO) == 0


def test_an_error_kind_that_is_not_known_is_counted_as_other(m: metrics.Metrics) -> None:
    # A label value from outside would make a new series for each text. The kinds are a fixed set.
    m.count_llm_error("rate_limit")
    m.count_llm_error("a surprise that carries a request id 8f3a")
    assert value(m, "longhaul_llm_errors_total", kind="rate_limit") == 1
    assert value(m, "longhaul_llm_errors_total", kind="other") == 1


# ---------- tools, sandboxes, retries ----------
def test_a_tool_run_goes_in_the_histogram_of_its_tool(m: metrics.Metrics) -> None:
    m.observe_tool("run_tests", 1.5)
    m.observe_tool("run_tests", 0.5)
    m.observe_tool("read_file", 0.01)

    assert value(m, "longhaul_tool_duration_seconds_count", tool="run_tests") == 2
    assert value(m, "longhaul_tool_duration_seconds_sum", tool="run_tests") == pytest.approx(2.0)
    assert value(m, "longhaul_tool_duration_seconds_count", tool="read_file") == 1
    assert value(m, "longhaul_tool_duration_seconds_bucket", tool="run_tests", le="1.0") == 1
    assert value(m, "longhaul_tool_duration_seconds_bucket", tool="run_tests", le="2.5") == 2


def test_a_recreated_sandbox_is_counted(m: metrics.Metrics) -> None:
    m.count_sandbox_recreated()
    m.count_sandbox_recreated()
    assert value(m, "longhaul_sandbox_recreated_total") == 2


# ---------- tasks ----------
def test_a_task_is_active_from_its_start_to_its_end(m: metrics.Metrics) -> None:
    m.task_started("lh-a-1")
    m.task_started("lh-b-2")
    assert value(m, "longhaul_active_tasks") == 2

    m.finished("run-1", "lh-a-1", REPO, "pr_opened")
    assert value(m, "longhaul_active_tasks") == 1
    m.finished("run-2", "lh-b-2", REPO, "error")
    assert value(m, "longhaul_active_tasks") == 0


def test_starting_a_task_twice_counts_it_once(m: metrics.Metrics) -> None:
    # prepare_workspace is retried, and ensure_sandbox runs again after every hand-over.
    m.task_started("lh-a-1")
    m.task_started("lh-a-1")
    assert value(m, "longhaul_active_tasks") == 1


def test_a_task_that_ends_on_a_worker_that_never_saw_it_start_does_not_go_below_zero(
    m: metrics.Metrics,
) -> None:
    # The worker restarted while the task ran: its counters began again at zero.
    m.finished("run-1", "lh-a-1", REPO, "pr_opened")
    assert value(m, "longhaul_active_tasks") == 0
    assert value(m, "longhaul_tasks_total", outcome="pr_opened", repo=REPO) == 1


def test_the_same_run_ending_twice_is_counted_once(m: metrics.Metrics) -> None:
    # A workflow task that is retried runs the local activity again for the same run.
    assert m.finished("run-1", "lh-a-1", REPO, "pr_opened") is True
    assert m.finished("run-1", "lh-a-1", REPO, "pr_opened") is False
    assert value(m, "longhaul_tasks_total", outcome="pr_opened", repo=REPO) == 1


def test_two_runs_of_the_same_task_id_are_two_tasks(m: metrics.Metrics) -> None:
    # A finished task can be started again under the same workflow id. Each run ends once.
    m.finished("run-1", "lh-a-1", REPO, "error")
    m.finished("run-2", "lh-a-1", REPO, "pr_opened")
    assert value(m, "longhaul_tasks_total", outcome="error", repo=REPO) == 1
    assert value(m, "longhaul_tasks_total", outcome="pr_opened", repo=REPO) == 1


def test_the_memory_of_finished_runs_is_bounded(m: metrics.Metrics) -> None:
    limit = metrics.REMEMBERED_RUNS
    for n in range(limit + 10):
        m.finished(f"run-{n}", f"lh-a-{n}", REPO, "pr_opened")
    # The oldest runs are forgotten. The newest ones are still known.
    assert m.finished(f"run-{limit + 9}", "lh-a-x", REPO, "pr_opened") is False
    assert m.finished("run-0", "lh-a-x", REPO, "pr_opened") is True


def test_a_local_folder_is_one_repo_label(m: metrics.Metrics) -> None:
    # `longhaul run` names a task's repo local:<path>. A label per path would grow without end.
    m.finished("run-1", "lh-local-1", "local:/home/a/calc", "pr_opened")
    m.finished("run-2", "lh-local-2", "local:/home/b/other", "pr_opened")
    m.add_cost("claude-sonnet-5-5", "local:/home/a/calc", 0.1)
    assert value(m, "longhaul_tasks_total", outcome="pr_opened", repo="local") == 2
    assert value(m, "longhaul_llm_cost_usd_total", model="claude-sonnet-5-5", repo="local") == 0.1


def test_an_outcome_that_is_not_known_is_counted_as_unknown(m: metrics.Metrics) -> None:
    m.finished("run-1", "lh-a-1", REPO, "something new")
    assert value(m, "longhaul_tasks_total", outcome="unknown", repo=REPO) == 1


# ---------- events ----------
def test_a_compaction_event_is_counted(m: metrics.Metrics) -> None:
    m.count_event("compaction", {"tokens_before": 120_000, "tokens_after": 30_000})
    assert value(m, "longhaul_compactions_total") == 1


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"reason": "no_progress"}, "no_progress"),
        ({"reason": "REPEATED_FAILURE"}, "repeated_failure"),
        ({"reason": "edit_churn", "evidence": ["a.py:3"]}, "edit_churn"),
        ({"reason": "tool_errors"}, "tool_errors"),
        ({"reason": "the model looked sad"}, "other"),
        ({}, "other"),
    ],
)
def test_a_stuck_event_is_counted_by_reason(
    m: metrics.Metrics, payload: dict[str, Any], reason: str
) -> None:
    m.count_event("stuck", payload)
    assert value(m, "longhaul_stuck_total", reason=reason) == 1


def test_an_event_that_has_no_metric_changes_nothing(m: metrics.Metrics) -> None:
    before = m.render()
    for kind in ("hint", "continue_as_new", "sandbox_recreated", "paused"):
        m.count_event(kind, {"reason": "no_progress"})
    assert m.render() == before


# ---------- the process-wide metrics ----------
def test_configured_swaps_the_current_metrics_and_puts_the_old_ones_back(
    m: metrics.Metrics,
) -> None:
    before = metrics.current()
    with metrics.configured(m):
        assert metrics.current() is m
    assert metrics.current() is before


def test_the_default_metrics_are_one_object(m: metrics.Metrics) -> None:
    assert metrics.current() is metrics.current()


# ---------- retries ----------
class _Next(ActivityInboundInterceptor):
    def __init__(self) -> None:
        self.ran = 0

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        self.ran += 1
        return "done"


async def run_through_interceptor(m: metrics.Metrics, *, attempt: int, name: str) -> _Next:
    """Run one activity attempt through the interceptor, inside an activity context."""
    chain = _Next()
    inbound = metrics.RetryCounter(m).intercept_activity(chain)
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, attempt=attempt, activity_type=name)
    input_ = ExecuteActivityInput(fn=lambda: None, args=[], executor=None, headers={})
    assert await env.run(inbound.execute_activity, input_) == "done"
    return chain


async def test_the_first_attempt_of_an_activity_is_not_a_retry(m: metrics.Metrics) -> None:
    chain = await run_through_interceptor(m, attempt=1, name="llm_turn")
    assert chain.ran == 1
    assert value(m, "longhaul_activity_retries_total", activity="llm_turn") == 0


async def test_every_later_attempt_is_counted_for_its_activity(m: metrics.Metrics) -> None:
    await run_through_interceptor(m, attempt=2, name="llm_turn")
    await run_through_interceptor(m, attempt=3, name="llm_turn")
    await run_through_interceptor(m, attempt=2, name="tool_exec")
    assert value(m, "longhaul_activity_retries_total", activity="llm_turn") == 2
    assert value(m, "longhaul_activity_retries_total", activity="tool_exec") == 1


async def test_the_retry_counter_uses_the_current_metrics_unless_it_is_given_others(
    counters: metrics.Metrics,
) -> None:
    chain = _Next()
    inbound = metrics.RetryCounter().intercept_activity(chain)
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, attempt=2, activity_type=acts.TOOL_EXEC.name)
    input_ = ExecuteActivityInput(fn=lambda: None, args=[], executor=None, headers={})
    await env.run(inbound.execute_activity, input_)
    assert value(counters, "longhaul_activity_retries_total", activity="tool_exec") == 1


# ---------- the HTTP endpoint ----------
def test_the_endpoint_serves_the_metrics_and_stops(m: metrics.Metrics) -> None:
    m.count_sandbox_recreated()
    server = metrics.serve(0, "127.0.0.1", m.registry)
    try:
        text = scrape(f"http://127.0.0.1:{server.port}/metrics")
        assert "longhaul_sandbox_recreated_total 1.0" in text
        assert "# TYPE longhaul_cache_ratio histogram" in text
    finally:
        server.stop()
    with pytest.raises(urllib.error.URLError):
        scrape(f"http://127.0.0.1:{server.port}/metrics")


def test_stopping_the_endpoint_twice_is_harmless(m: metrics.Metrics) -> None:
    server = metrics.serve(0, "127.0.0.1", m.registry)
    server.stop()
    server.stop()


def test_a_port_that_is_taken_stops_the_worker_with_the_setting_to_change(
    m: metrics.Metrics,
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        with pytest.raises(ValueError, match="LONGHAUL_METRICS_PORT"):
            metrics.serve(port, "127.0.0.1", m.registry)
