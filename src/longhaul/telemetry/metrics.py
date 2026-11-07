"""Prometheus metrics for the worker: the numbers an operator watches, and the endpoint for them.

Eleven metrics, named in plan story LH-704 and drawn on docker/grafana/dashboards/longhaul.json:

| metric | what it counts | updated by |
|---|---|---|
| longhaul_tasks_total{outcome,repo} | tasks that ended | record_event, for the outcome event |
| longhaul_active_tasks | tasks this worker is running | prepare_workspace, ensure_sandbox |
| longhaul_turns_total{role} | model calls that returned a reply | llm_turn |
| longhaul_llm_cost_usd_total{model,repo} | dollars the cost ledger priced | llm_turn |
| longhaul_llm_errors_total{kind} | model calls that failed | llm_turn |
| longhaul_tool_duration_seconds{tool} | tool run time (histogram) | tool_exec |
| longhaul_compactions_total | compactions | record_event (LH-404) |
| longhaul_stuck_total{reason} | stuck detections | record_event (LH-505) |
| longhaul_sandbox_recreated_total | sandboxes rebuilt (failure mode F3) | ensure_sandbox |
| longhaul_activity_retries_total{activity} | activity attempts after the first | RetryCounter |
| longhaul_cache_ratio | share of a prompt read from the cache (histogram) | llm_turn |

Only activities update them, never workflow code (CONTRIBUTING.md rule 4): a workflow runs again
on every replay, and a counter that a replay bumps would count the same turn twice. The workflow
reaches the metrics through the activities it already calls, and through one `record_event` call
at the end of a run (acts.OUTCOME_EVENT).

Labels stay low-cardinality, because every label value is a series that Prometheus keeps. The
values come from fixed sets (roles, outcomes, provider error kinds, stuck reasons, tool names of
the registry, activity names) or from the allowlisted repos and the configured models. A value
from outside those sets, such as a tool name the model made up or a folder passed to
`longhaul run`, is mapped to one catch-all label.

Counts are per process. A worker that restarts begins again at zero, which `rate()` and
`increase()` handle. The gauge is rebuilt from the tasks the process has seen, so a task that was
running before a restart is counted again when its next run calls ensure_sandbox, and one that
ends on a worker that never saw it start does not take the gauge below zero.

Temporal can run an activity twice. Two kinds of count are guarded against it:

* the end of a task is counted once per workflow run id. A workflow task that fails after its
  local activity ran is run again, and so is the activity. (A replay does not run it: the result
  is in the history.) Another worker would count it again, since the memory of finished runs is
  per process, and that case is rare enough to leave;
* an event is counted after the recorder wrote it, so the metric follows the events table.

The registry is the object's own and not the global one, so a test can make a fresh set and read
it without anything leaking in from other tests. The process uses the one in `current()`.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from wsgiref.simple_server import WSGIServer

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server
from prometheus_client.exposition import generate_latest
from temporalio import activity
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput, Interceptor

from longhaul.contracts import OutcomeKind, ProviderErrorKind, Role, Usage
from longhaul.github.intake import LOCAL_PREFIX
from longhaul.telemetry.otel import cache_ratio

# The repo label of every `local:<path>` task. `longhaul run` takes any folder, and one series
# for each of them would grow without end.
LOCAL_REPO = "local"
# What a label value outside its fixed set becomes.
OTHER = "other"
UNKNOWN = "unknown"

# The stuck rules of plan story LH-505, as the `reason` of a stuck event.
STUCK_REASONS = ("no_progress", "repeated_failure", "edit_churn", "tool_errors")
# The event types of plan section 3.5 that move a counter. The outcome event is separate: it
# carries a run id and the repo, and the activity hands those to `finished`.
COMPACTION_EVENT = "compaction"
STUCK_EVENT = "stuck"

# How many ended runs a process remembers, to count a run once. A run id is a short string, so
# this is under a megabyte. A run is only seen twice within seconds of its end.
REMEMBERED_RUNS = 10_000

# Tool runs last from a few milliseconds (a file read) to the longest command timeout (600 s).
TOOL_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
# The ratio is between 0 and 1. The 0.0 edge tells a call that missed the cache entirely from
# one that read a little of it.
RATIO_BUCKETS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)

_ERROR_KINDS = frozenset(kind.value for kind in ProviderErrorKind)
_OUTCOMES = frozenset(kind.value for kind in OutcomeKind)


def repo_label(repo: str) -> str:
    return LOCAL_REPO if repo.startswith(LOCAL_PREFIX) else repo


class Metrics:
    """The eleven metrics on one registry, and the calls that update them."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()
        r = self.registry
        self._tasks = Counter(
            "longhaul_tasks_total",
            "Tasks that ended, by outcome and repo.",
            ["outcome", "repo"],
            registry=r,
        )
        self._active = Gauge(
            "longhaul_active_tasks", "Tasks that this worker is running.", registry=r
        )
        self._turns = Counter(
            "longhaul_turns_total",
            "Model calls that returned a reply, by role.",
            ["role"],
            registry=r,
        )
        self._cost = Counter(
            "longhaul_llm_cost_usd_total",
            "Dollars the cost ledger priced for model calls, by model and repo.",
            ["model", "repo"],
            registry=r,
        )
        self._errors = Counter(
            "longhaul_llm_errors_total",
            "Model calls that failed, by error kind.",
            ["kind"],
            registry=r,
        )
        self._tool_seconds = Histogram(
            "longhaul_tool_duration_seconds",
            "Time a tool took to run, by tool.",
            ["tool"],
            buckets=TOOL_BUCKETS,
            registry=r,
        )
        self._compactions = Counter(
            "longhaul_compactions_total", "Transcript compactions.", registry=r
        )
        self._stuck = Counter(
            "longhaul_stuck_total",
            "Times a task was found stuck, by reason.",
            ["reason"],
            registry=r,
        )
        self._recreated = Counter(
            "longhaul_sandbox_recreated_total", "Sandboxes that had to be rebuilt.", registry=r
        )
        self._retries = Counter(
            "longhaul_activity_retries_total",
            "Activity attempts after the first, by activity.",
            ["activity"],
            registry=r,
        )
        self._cache_ratio = Histogram(
            "longhaul_cache_ratio",
            "Share of a model call's prompt that was read from the cache.",
            buckets=RATIO_BUCKETS,
            registry=r,
        )
        # Series for the labels that come from a fixed set start at zero. A rate() or an
        # increase() over a series that appears only with its first event misses that event.
        for role in Role:
            self._turns.labels(role=role.value)
        for kind in (*sorted(_ERROR_KINDS), OTHER):
            self._errors.labels(kind=kind)
        for reason in (*STUCK_REASONS, OTHER):
            self._stuck.labels(reason=reason)

        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._ended: OrderedDict[str, None] = OrderedDict()

    def render(self) -> str:
        """The metrics in the text format that /metrics serves."""
        return generate_latest(self.registry).decode("utf-8")

    # ---------- tasks ----------
    def task_started(self, task_id: str) -> None:
        """Note that this worker runs the task. Doing it again for the same task changes nothing."""
        with self._lock:
            self._running.add(task_id)
            self._active.set(len(self._running))

    def finished(self, run_id: str, task_id: str, repo: str, outcome: str) -> bool:
        """Count the end of a task, once per workflow run. False when this run was counted before.

        The task leaves the active ones in any case, so a repeat cannot leave it there.
        """
        with self._lock:
            self._running.discard(task_id)
            self._active.set(len(self._running))
            if run_id in self._ended:
                return False
            self._ended[run_id] = None
            if len(self._ended) > REMEMBERED_RUNS:
                self._ended.popitem(last=False)
        label = outcome if outcome in _OUTCOMES else UNKNOWN
        self._tasks.labels(outcome=label, repo=repo_label(repo)).inc()
        return True

    # ---------- model calls ----------
    def count_turn(self, role: Role, usage: Usage) -> None:
        """A model call returned a reply: one more turn, and a sample of its cache ratio."""
        self._turns.labels(role=role.value).inc()
        self._cache_ratio.observe(cache_ratio(usage))

    def add_cost(self, model: str, repo: str, usd: float) -> None:
        self._cost.labels(model=model, repo=repo_label(repo)).inc(usd)

    def count_llm_error(self, kind: str) -> None:
        self._errors.labels(kind=kind if kind in _ERROR_KINDS else OTHER).inc()

    # ---------- tools, sandboxes, retries ----------
    def observe_tool(self, tool: str, seconds: float) -> None:
        self._tool_seconds.labels(tool=tool).observe(seconds)

    def count_sandbox_recreated(self) -> None:
        self._recreated.inc()

    def count_retry(self, activity_name: str) -> None:
        self._retries.labels(activity=activity_name).inc()

    # ---------- events ----------
    def count_event(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """Count an event of the log, if it is one that a metric follows."""
        if event_type == COMPACTION_EVENT:
            self._compactions.inc()
        elif event_type == STUCK_EVENT:
            reason = str(payload.get("reason", "")).lower()
            self._stuck.labels(reason=reason if reason in STUCK_REASONS else OTHER).inc()


# ---------- the metrics of this process ----------
@dataclass
class _State:
    metrics: Metrics = field(default_factory=Metrics)


_state = _State()


def current() -> Metrics:
    """The metrics that the activities of this process update and the endpoint serves."""
    return _state.metrics


@contextmanager
def configured(metrics: Metrics) -> Iterator[Metrics]:
    """Use these metrics inside the block and put the previous ones back after it. For tests."""
    before = _state.metrics
    _state.metrics = metrics
    try:
        yield metrics
    finally:
        _state.metrics = before


# ---------- retries ----------
class RetryCounter(Interceptor):
    """A worker interceptor that counts the attempts of an activity after its first.

    The attempt number is Temporal's own, so the count is right for an activity of any kind,
    also a local one, without each activity having to check. Pass it as `interceptors=` to the
    Worker. The metrics are looked up when an attempt starts, unless a set was given.
    """

    def __init__(self, target: Metrics | None = None) -> None:
        self._target = target

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _CountsRetries(next, self._target)


class _CountsRetries(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, target: Metrics | None) -> None:
        super().__init__(next)
        self._target = target

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        if info.attempt > 1:
            (self._target or current()).count_retry(info.activity_type)
        return await super().execute_activity(input)


# ---------- the endpoint ----------
@dataclass
class MetricsServer:
    """The HTTP server of /metrics, running in a thread of its own."""

    server: WSGIServer
    thread: threading.Thread
    _stopped: bool = False

    @property
    def port(self) -> int:
        """The port it listens on. It is the one asked for, or the one chosen for port 0."""
        return int(self.server.server_port)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def serve(port: int, host: str, registry: CollectorRegistry | None = None) -> MetricsServer:
    """Start serving /metrics on host:port, and return the server so the caller can stop it.

    Port 0 lets the system choose, which tests use to avoid a port that is taken. A port that is
    taken stops the worker with the setting to change: two workers on one machine need two ports.
    """
    chosen = registry if registry is not None else current().registry
    try:
        server, thread = start_http_server(port, addr=host, registry=chosen)
    except OSError as exc:
        raise ValueError(
            f"cannot serve metrics on {host}:{port}: {exc.strerror or exc}. "
            "Set LONGHAUL_METRICS_PORT to a free port"
        ) from exc
    return MetricsServer(server, thread)
