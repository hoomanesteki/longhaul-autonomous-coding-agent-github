"""Terminate the Temporal workflow of a demo task. Used by scripts/demo.sh as a last resort.

The address and namespace come from the settings object, which reads .env as the worker and
`longhaul run` do. The script runs from the repository root, where .env lives.
"""

from __future__ import annotations

import asyncio
import sys

from temporalio.client import Client

from longhaul.settings import get_settings


async def main(workflow_id: str) -> None:
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await client.get_workflow_handle(workflow_id).terminate(reason="demo interrupted")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
