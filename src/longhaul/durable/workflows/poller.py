"""PollerWorkflow: one poll of GitHub for newly labeled issues (LH-307).

A Temporal Schedule starts it every `poller.interval_seconds` (durable/schedule.py, ADR-0008).
It does one thing, which is to run the activity poll_once and return what the activity counted.
Everything that has a reason to change is in the activity: the repos, the label, the database
and the calls to GitHub and Temporal. This module stays as small as it is so that nothing in it
can break determinism or need a Temporal patch (failure mode F13) when the poller grows.

It imports no I/O library (invariant I1, checked by tests/unit/test_invariants.py), reads no
clock and draws no random number. The overlap policy of the Schedule is SKIP, so at most one run
of this workflow is open at a time and two polls never race over the same issue.
"""

from __future__ import annotations

from temporalio import workflow

# Acts are pure, and passing them through the sandbox keeps one class object for PollResult.
with workflow.unsafe.imports_passed_through():
    from longhaul.durable import acts


@workflow.defn(name="PollerWorkflow")
class PollerWorkflow:
    @workflow.run
    async def run(self) -> acts.PollResult:
        result: acts.PollResult = await workflow.execute_activity(
            acts.POLL_ONCE.name,
            result_type=acts.POLL_ONCE.result_type,
            **acts.options(acts.POLL_ONCE),
        )
        return result
