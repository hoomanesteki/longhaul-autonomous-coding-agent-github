"""The context modes (LH-406): what each one turns on, and how the config and a run name one.

The activities and the workflow are tested where they live (test_activities_llm_context.py,
test_activities_context.py, test_task_workflow.py, test_cli_durable.py). This file covers the
mode itself and the setting that carries it.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from longhaul.context.modes import ContextMode
from longhaul.db.records import COMPACTION_EVENT
from longhaul.durable import acts
from longhaul.durable.workflows import task
from longhaul.settings import Context, load_config

pytestmark = [pytest.mark.unit, pytest.mark.phase4]

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "longhaul.example.yaml"
VALUES: dict[str, Any] = {
    "window_tokens": 200_000,
    "compact_at": 0.6,
    "hard_limit": 0.85,
    "keep_recent_turns": 6,
    "elide_tool_outputs_after_turns": 4,
    "tool_output_max_tokens": 4_000,
}


@pytest.mark.parametrize(
    ("mode", "elides", "compacts", "slides"),
    [
        (ContextMode.FULL, True, True, False),
        (ContextMode.NO_COMPACTION, True, False, False),
        (ContextMode.NONE, False, False, False),
        (ContextMode.SLIDING, False, False, True),
    ],
)
def test_each_mode_says_which_tiers_it_runs(
    mode: ContextMode, elides: bool, compacts: bool, slides: bool
) -> None:
    assert mode.elides is elides
    assert mode.compacts is compacts
    assert mode.slides is slides


def test_the_modes_are_named_as_the_plan_names_them() -> None:
    # `sliding` is the window of the naive baseline (plan section 8.3, LH-903).
    assert [mode.value for mode in ContextMode] == ["full", "no_compaction", "none", "sliding"]


def test_a_mode_is_its_name_for_a_command_line_and_a_yaml_file() -> None:
    assert ContextMode("no_compaction") is ContextMode.NO_COMPACTION
    assert ContextMode.NONE == "none"


def test_the_config_runs_both_tiers_unless_it_says_otherwise() -> None:
    # Every config written before the mode existed keeps the behavior it had.
    assert Context(**VALUES).mode is ContextMode.FULL


@pytest.mark.parametrize("mode", list(ContextMode))
def test_the_config_takes_a_mode_by_name(tmp_path: Path, mode: ContextMode) -> None:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["context"]["mode"] = mode.value
    path = tmp_path / "longhaul.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    assert load_config(path).context.mode is mode


@pytest.mark.parametrize("name", ["partial", "FULL", "", "off", 1])
def test_a_mode_that_does_not_exist_is_refused_and_the_error_names_the_setting(
    tmp_path: Path, name: object
) -> None:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["context"]["mode"] = name
    path = tmp_path / "longhaul.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ValidationError) as caught:
        load_config(path)

    assert [error["loc"] for error in caught.value.errors()] == [("context", "mode")]
    assert "no_compaction" in str(caught.value)


def test_the_example_config_names_its_mode() -> None:
    # The example is the documentation of the setting, so it lists the key with its default.
    assert yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["context"]["mode"] == "full"
    assert load_config(EXAMPLE).context.mode is ContextMode.FULL


def test_a_run_carries_its_mode_in_the_memo_under_a_key_of_its_own() -> None:
    assert acts.CONTEXT_MODE_MEMO_KEY == "context_mode"
    keys = {acts.CONTEXT_MODE_MEMO_KEY, acts.SCRIPT_MEMO_KEY, acts.TUNING_MEMO_KEY}
    assert len(keys) == 3


def test_the_recorder_and_the_workflow_name_the_compaction_event_alike() -> None:
    # The workflow cannot import the recorder (it reaches SQLAlchemy), so each side has the name.
    assert COMPACTION_EVENT == task.COMPACTION_EVENT
