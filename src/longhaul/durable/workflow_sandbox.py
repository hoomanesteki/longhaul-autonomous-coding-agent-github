"""Temporal sandbox settings for any worker that runs TaskWorkflow.

This is the second guard of invariant I1. The first is the source scan in
tests/unit/test_invariants.py. The sandbox alone is not enough (ADR-0004, appendix section 1):
docker, sqlalchemy, boto3, openai and httpx2 all failed to import inside it, but only because
they touch restricted standard library members while they load. A library that does not would
import cleanly.

With these settings the same imports fail at once, with a message that names the library
("Cannot access docker from inside a workflow"), and a workflow module that imports one
cannot be loaded by a worker at all.

A module imported under workflow.unsafe.imports_passed_through() skips this check. The
source scan covers those, which is why it follows first party imports.
"""

from __future__ import annotations

import dataclasses

from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxMatcher,
    SandboxRestrictions,
)

# The libraries named by I1 that a workflow could import, and the ones they sit on. subprocess
# is not here: it is standard library, the sandbox already refuses to call it, and blocking
# its import would break modules that only mention it.
BLOCKED_MODULES = frozenset(
    {
        "httpx",
        "httpx2",
        "httpcore",
        "httpcore2",
        "docker",
        "sqlalchemy",
        "boto3",
        "botocore",
        "anthropic",
        "openai",
    }
)


def restrictions() -> SandboxRestrictions:
    """The SDK defaults, with access to the I/O libraries refused."""
    return dataclasses.replace(
        SandboxRestrictions.default,
        invalid_modules=SandboxMatcher(access=set(BLOCKED_MODULES)),
    )


def workflow_runner() -> SandboxedWorkflowRunner:
    """The `workflow_runner` argument for a Worker or Replayer that runs TaskWorkflow."""
    return SandboxedWorkflowRunner(restrictions=restrictions())
