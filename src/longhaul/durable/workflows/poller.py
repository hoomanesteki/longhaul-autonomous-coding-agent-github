"""PollerWorkflow: one poll of GitHub for newly labeled issues, then one sweep of old repos.

A Temporal Schedule starts it every `poller.interval_seconds` (durable/schedule.py, ADR-0008).
It runs the activity poll_once and returns what the activity counted. Everything that has a
reason to change is in the activities: the repos, the label, the database and the calls to
GitHub and Temporal. This module stays as small as it is so that nothing in it can break
determinism or need a Temporal patch (failure mode F13) when the poller grows.

The sweep (LH-507) is the janitor. After the poll, `clean_repos` removes the bare repos of tasks
that ended more than seven days ago (durable/activities/janitor.py). The schedule is its clock, so
it needs no timer of its own. It runs also when the poll failed, because a poller whose GitHub
token has expired should still free the disk. A sweep that fails is logged and does not fail the
poll: the next tick sweeps again, and a sweep must never hide the result of the poll.

The sweep is a new command in a workflow that is open for a few minutes, so it is behind
`workflow.patched(SWEEP_PATCH)`. A run that began on a worker without the sweep replays as it ran.
Drop the patch with `workflow.deprecate_patch` once no such run is open, which is after one
POLLER_RUN_TIMEOUT.

It imports no I/O library (invariant I1, checked by tests/unit/test_invariants.py), reads no
clock and draws no random number. The overlap policy of the Schedule is SKIP, so at most one run
of this workflow is open at a time and two polls never race over the same issue.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.exceptions import FailureError

# Acts are pure, and passing them through the sandbox keeps one class object for PollResult.
with workflow.unsafe.imports_passed_through():
    from longhaul.durable import acts

# The patch marker of the sweep after the poll (LH-507, failure mode F13).
SWEEP_PATCH = "lh-507-sweep-repos"


@workflow.defn(name="PollerWorkflow")
class PollerWorkflow:
    @workflow.run
    async def run(self) -> acts.PollResult:
        try:
            result: acts.PollResult = await workflow.execute_activity(
                acts.POLL_ONCE.name,
                result_type=acts.POLL_ONCE.result_type,
                **acts.options(acts.POLL_ONCE),
            )
        finally:
            if workflow.patched(SWEEP_PATCH):
                await self._sweep()
        return result

    async def _sweep(self) -> None:
        try:
            await workflow.execute_activity(
                acts.CLEAN_REPOS.name,
                result_type=acts.CLEAN_REPOS.result_type,
                **acts.options(acts.CLEAN_REPOS),
            )
        except FailureError as err:
            workflow.logger.warning("the sweep of old repos failed: %s", err.cause or err)
