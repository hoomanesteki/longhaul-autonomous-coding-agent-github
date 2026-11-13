"""How an error from Temporal becomes an answer of the console API (LH-801)."""

from __future__ import annotations

import pytest
from temporalio.service import RPCError, RPCStatusCode

from longhaul.api.errors import temporal_failure

pytestmark = [pytest.mark.unit, pytest.mark.phase8]


def rpc(status: RPCStatusCode, message: str = "x") -> RPCError:
    return RPCError(message, status, b"")


@pytest.mark.parametrize(
    "exc",
    [
        rpc(RPCStatusCode.UNAVAILABLE),
        rpc(RPCStatusCode.DEADLINE_EXCEEDED),
        ConnectionError("refused"),
        TimeoutError(),
    ],
)
def test_a_server_that_does_not_answer_is_503(exc: Exception) -> None:
    answer = temporal_failure(exc)

    assert answer.status_code == 503
    assert answer.detail == "Temporal is not reachable"


# INVALID_ARGUMENT was in this list when every refusal was a 502 (LH-801). It is the person's
# request that Temporal turns down, so it is a 400 now (LH-991) and has its own test below.
@pytest.mark.parametrize(
    ("status", "name"),
    [
        (RPCStatusCode.PERMISSION_DENIED, "PERMISSION_DENIED"),
        (RPCStatusCode.RESOURCE_EXHAUSTED, "RESOURCE_EXHAUSTED"),
        (RPCStatusCode.INTERNAL, "INTERNAL"),
        (RPCStatusCode.UNKNOWN, "UNKNOWN"),
        (RPCStatusCode.ABORTED, "ABORTED"),
        (RPCStatusCode.UNAUTHENTICATED, "UNAUTHENTICATED"),
    ],
)
def test_a_server_that_refuses_is_502_with_the_status(status: RPCStatusCode, name: str) -> None:
    answer = temporal_failure(rpc(status))

    assert answer.status_code == 502
    assert name in answer.detail
    assert "not reachable" not in answer.detail


def test_a_request_that_temporal_finds_invalid_is_400() -> None:
    answer = temporal_failure(rpc(RPCStatusCode.INVALID_ARGUMENT))

    assert answer.status_code == 400
    assert "INVALID_ARGUMENT" in answer.detail


def test_a_request_that_the_state_of_the_task_refuses_is_409() -> None:
    answer = temporal_failure(rpc(RPCStatusCode.FAILED_PRECONDITION))

    assert answer.status_code == 409
    assert "FAILED_PRECONDITION" in answer.detail


@pytest.mark.parametrize("status", list(RPCStatusCode))
def test_the_answer_never_carries_the_message_of_the_server(status: RPCStatusCode) -> None:
    secret = "dial tcp 10.1.2.3:7233: namespace internal-prod rejected"
    if status is RPCStatusCode.NOT_FOUND:
        return  # its two answers are fixed texts, tested above

    answer = temporal_failure(rpc(status, secret))

    assert "10.1.2.3" not in answer.detail and "internal-prod" not in answer.detail
    assert answer.status_code in {400, 409, 502, 503}
    assert len(answer.detail) < 80


def test_not_found_keeps_its_two_answers() -> None:
    assert (
        temporal_failure(
            rpc(RPCStatusCode.NOT_FOUND, "workflow execution already completed")
        ).status_code
        == 409
    )
    assert temporal_failure(rpc(RPCStatusCode.NOT_FOUND, "workflow not found")).status_code == 404
