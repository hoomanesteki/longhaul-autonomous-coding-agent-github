"""The data converter that every client and worker uses: pydantic's, with a size guard (I2).

Invariant I2 says that no payload serializes above 256 KB (ADR-0005). `assert_small` checks that
inside an activity, which is too late for an input: the workflow has scheduled it by then, and
Temporal has written it into the history, where every replay reads it again. This guard runs
where a payload is made, so an oversized one never leaves the process.

Why a payload converter and not a PayloadCodec. The converter is the one hook that every encode
goes through, whether it happens on a client or in a worker: the input of a workflow, a signal,
a query, an update, the input and the result of an activity, the arguments of a Continue-As-New,
the result of a workflow, a memo, and the details of a failure. A codec sees the same bytes, but
the worker runs it over the finished completion of a workflow task, outside the workflow code.
An error there fails the workflow task (the SDK logs "Failed encoding completion" and the server
retries the task), and the workflow can never end on it. The converter raises inside the call
that builds the payload, so the error reaches the workflow code at the line that tried to
schedule the activity (see acts.FAIL_THE_WORKFLOW for what happens next).

Only encoding is checked. A payload that comes back from the server is decoded as it is, so a
history written before the guard, or by another client, can still be replayed and read.

The SDK builds one converter per client and gives the worker the one of its client, and the
workflow sandbox gets an instance made by the same factory. Connecting with this data converter
(durable.client.connect) is therefore all a process has to do.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from temporalio.api.common.v1 import Payload
from temporalio.contrib.pydantic import PydanticPayloadConverter
from temporalio.converter import DataConverter

from longhaul.durable.payload_guard import assert_size


class GuardedPayloadConverter(PydanticPayloadConverter):
    """The pydantic payload converter, refusing any single payload over the limit.

    `to_payload` calls `to_payloads`, so overriding the plural covers both.
    """

    def to_payloads(self, values: Sequence[Any]) -> list[Payload]:
        payloads = super().to_payloads(values)
        # The cap is per payload, as in ADR-0005. An activity with two arguments sends two.
        for payload in payloads:
            assert_size(len(payload.data))
        return payloads


# The SDK needs a class with a constructor that takes no arguments, to make one converter for
# each serialization context.
guarded_data_converter = DataConverter(payload_converter_class=GuardedPayloadConverter)
