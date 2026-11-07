"""Read a workflow history the way invariant I2 cares about: by the size of its payloads."""

from __future__ import annotations

from google.protobuf.message import Message as ProtoMessage
from temporalio.client import WorkflowHistory


def payload_sizes(history: WorkflowHistory) -> list[int]:
    """The size of every payload in the history: inputs, results, failure details and so on."""
    sizes: list[int] = []

    def walk(message: ProtoMessage) -> None:
        if message.DESCRIPTOR.full_name == "temporal.api.common.v1.Payload":
            sizes.append(len(message.data))  # type: ignore[attr-defined]
            return
        for field_, value in message.ListFields():
            if field_.message_type is None:
                continue  # a number, a string or an enum
            if isinstance(value, ProtoMessage):
                walk(value)
            elif field_.message_type.GetOptions().map_entry:
                items = [v for v in value.values() if isinstance(v, ProtoMessage)]
                for item in items:
                    walk(item)
            else:
                for item in value:  # a repeated field
                    walk(item)

    for event in history.events:
        walk(event)
    return sizes
