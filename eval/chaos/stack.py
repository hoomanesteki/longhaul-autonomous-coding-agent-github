"""One chaos run on the private services, delivering to a world of its own (LH-904).

`ChaosStack` is the harness `Stack` with four changes. It talks to the Temporal of the private
compose project. Its task is the issue of a custom task, in a FakeGitHub that the worker is told
about through its settings. The model is a replay of a recording (or a recording in progress).
And it knows the three kills that the harness leaves out: the sandbox (`docker kill`), the
Temporal server and Postgres (a restart of the container).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from tests.chaos.harness import WORKER, Stack, Worker, service_settings

from eval import custom_tasks
from eval.chaos import audit, services
from eval.chaos.world import World
from longhaul.contracts import TaskSpec
from longhaul.cost.pricing import Pricing

# Every four turns the run hands over to a new one (Continue-As-New), so the kills also fall in
# the hand-over: the bundle, the event and the first activity of the next run.
CONTINUE_AS_NEW_EVERY = 4
PRICING_FILE = custom_tasks.ROOT / "config" / "pricing.yaml"
# The harness uses 5 s, which a worker keeps on a quiet machine. A suite that runs for an hour
# meets a loaded one, where an event loop that is blocked for 5 s makes a live activity look dead
# and the retry runs beside it. The resume latency after a killed worker is about this long.
HEARTBEAT_TIMEOUT_S = 20.0


def replay_env(recordings: Path) -> dict[str, str]:
    return {"LONGHAUL_PROVIDER": "replay", "LONGHAUL_RECORDINGS_DIR": str(recordings)}


def recording_env(recordings: Path, provider: str, script: Path | None) -> dict[str, str]:
    """Settings of a worker that saves every model call it makes into `recordings`."""
    env = {
        "LONGHAUL_PROVIDER": provider,
        "LONGHAUL_RECORD": "1",
        "LONGHAUL_RECORDINGS_DIR": str(recordings),
    }
    if script is not None:
        env["LONGHAUL_SCRIPT"] = str(script.resolve())
    return env


class ChaosStack(Stack):
    def __init__(
        self,
        root: Path,
        database: str,
        *,
        private: services.PrivateServices,
        world: World,
        provider_env: dict[str, str],
    ) -> None:
        super().__init__(
            root,
            database,
            worker_env={**world.env(), **provider_env},
            script=None,  # the worker's provider answers, so the task brings no script
            continue_as_new_every=CONTINUE_AS_NEW_EVERY,
            heartbeat_timeout_s=HEARTBEAT_TIMEOUT_S,
        )
        self.private = private
        self.world = world

    @property
    def settings(self) -> dict[str, str]:
        """The shared MinIO of `make up`, and the Temporal of the private stack."""
        return {**service_settings(), "temporal": self.private.temporal}

    def _new_task(self) -> TaskSpec:
        return self.world.spec(f"lh-chaos-{self.world.task.id}-{uuid.uuid4().hex[:8]}")

    def strike(self, kind: str, worker: Worker, task_id: str) -> bool:
        if kind == WORKER:
            return super().strike(kind, worker, task_id)
        if kind == "sandbox":
            return services.kill_sandbox(task_id)
        if kind in services.SERVICES:
            return self.private.restart(kind)
        raise ValueError(f"cannot strike {kind!r}")

    def ledger_totals(self, task_id: str) -> tuple[int, float]:
        """How many model calls the task has on the cost ledger, and what they would cost.

        The cost is priced from the tokens of each call at the published rates, because the
        `usd` column of a replayed run is zero (see audit.ledger_cost).
        """
        found = self._query(
            "SELECT model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens "
            "FROM cost_ledger WHERE task_id = %s",
            task_id,
        )
        rows = [audit.LedgerRow(*row) for row in found]  # type: ignore[arg-type]
        return len(rows), audit.ledger_cost(rows, Pricing.load(PRICING_FILE))
