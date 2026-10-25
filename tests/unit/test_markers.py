import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

_spec = importlib.util.spec_from_file_location(
    "markers", Path(__file__).parents[2] / "scripts" / "markers.py"
)
assert _spec and _spec.loader
markers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(markers)


def test_marker_expression() -> None:
    assert markers.marker_expression(0) == "phase0"
    assert markers.marker_expression(2) == "phase0 or phase1 or phase2"
    assert markers.marker_expression(9).endswith("phase9")
    with pytest.raises(ValueError):
        markers.marker_expression(10)
    with pytest.raises(ValueError):
        markers.marker_expression(-1)
