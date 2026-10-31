"""The sandbox settings every worker of TaskWorkflow uses (the second guard of invariant I1)."""

import pytest
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from longhaul.durable.workflow_sandbox import BLOCKED_MODULES, restrictions, workflow_runner

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

# The libraries named by I1 (plan section 4.3) that a workflow could import. subprocess is
# left out: it is standard library, the sandbox already refuses to call it, and blocking
# its import would break modules that only mention it.
I1_LIBRARIES = {
    "httpx",
    "httpx2",
    "httpcore2",
    "docker",
    "sqlalchemy",
    "boto3",
    "anthropic",
    "openai",
}


def test_every_library_named_by_i1_is_blocked() -> None:
    assert I1_LIBRARIES <= BLOCKED_MODULES


def test_the_libraries_under_the_named_ones_are_blocked_too() -> None:
    # boto3 sits on botocore, and httpx on httpcore. Blocking only the front door would leave
    # a way in through the other.
    assert {"botocore", "httpcore"} <= BLOCKED_MODULES


def test_the_sandbox_refuses_access_to_each_blocked_module() -> None:
    assert restrictions().invalid_modules.access == BLOCKED_MODULES


def test_the_other_restrictions_stay_at_the_sdk_defaults() -> None:
    # The default member list is what stops datetime.now(), random and open() in workflow code.
    default = SandboxRestrictions.default
    assert restrictions().invalid_module_members == default.invalid_module_members
    assert restrictions().passthrough_modules == default.passthrough_modules


def test_the_runner_is_a_sandboxed_runner() -> None:
    assert isinstance(workflow_runner(), SandboxedWorkflowRunner)
