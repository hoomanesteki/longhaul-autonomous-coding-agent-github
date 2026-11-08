"""The API's own metrics, served on /metrics next to the worker's.

The worker's eleven metrics (telemetry.metrics) describe the agent. These two describe the API:
how many requests it answered and how long they took. They sit on a registry of their own so
that a test can build an app and read them without anything from another test, and so that the
page of /metrics is the two registries one after the other, with no name in both.

The route label is the path template (`/api/tasks/{task_id}`) and never the path, so a task id is
not a label value and the number of series stays fixed. A request that matched no route is
counted under `unmatched`.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.exposition import generate_latest

from longhaul.telemetry import metrics

__all__ = ["ApiMetrics", "UNMATCHED", "exposition"]

UNMATCHED = "unmatched"


class ApiMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()
        self._requests = Counter(
            "longhaul_api_requests",
            "Requests the console API answered, by method, route and status.",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self._duration = Histogram(
            "longhaul_api_request_duration_seconds",
            "How long the console API took to answer, by route.",
            ["route"],
            registry=self.registry,
        )

    def observe(self, method: str, route: str | None, status: int, seconds: float) -> None:
        label = route or UNMATCHED
        self._requests.labels(method, label, str(status)).inc()
        self._duration.labels(label).observe(seconds)


def exposition(api: ApiMetrics) -> bytes:
    """The text of /metrics: the worker metrics of this process, then the API's own."""
    return generate_latest(metrics.current().registry) + generate_latest(api.registry)
