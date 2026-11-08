"""The probe of the chaos suite's watchdog against a real Temporal server (LH-904).

It reads from the Temporal of `make up` and writes nothing. The unit tests cover the logic of the
watchdog; this one checks that a healthy server counts as answering.
"""

import pytest
from tests.chaos.harness import service_settings

from eval.chaos import watchdog

pytestmark = [pytest.mark.integration, pytest.mark.phase9]


def test_a_running_temporal_server_answers_the_probe() -> None:
    assert watchdog.temporal_answers(service_settings()["temporal"]) is True
