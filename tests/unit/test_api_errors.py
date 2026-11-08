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


@pytest.mark.parametrize(
    ("status", "name"),
    [
        (RPCStatusCode.PERMISSION_DENIED, "PERMISSION_DENIED"),
        (RPCStatusCode.RESOURCE_EXHAUSTED, "RESOURCE_EXHAUSTED"),
        (RPCStatusCode.INVALID_ARGUMENT, "INVALID_ARGUMENT"),
    ],
)
def test_a_server_that_refuses_is_502_with_the_status(status: RPCStatusCode, name: str) -> None:
    answer = temporal_failure(rpc(status))

    assert answer.status_code == 502
    assert name in answer.detail
    assert "not reachable" not in answer.detail


def test_not_found_keeps_its_two_answers() -> None:
    assert (
        temporal_failure(
            rpc(RPCStatusCode.NOT_FOUND, "workflow execution already completed")
        ).status_code
        == 409
    )
    assert temporal_failure(rpc(RPCStatusCode.NOT_FOUND, "workflow not found")).status_code == 404
