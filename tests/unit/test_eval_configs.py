"""The config files of the evaluation (LH-903): what each one says and how it is hashed.

`eval/configs/<name>.yaml` defines a config of plan section 8.3. The runner stores the hash of
the file in every result, so a number can be traced to the exact definition that produced it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from eval import config_files, results
from longhaul.context.modes import ContextMode

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ROOT / "eval" / "configs"


def write(tmp_path: Path, body: str, name: str = "naive") -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


NAIVE = """\
name: naive
description: A test copy.
runner: durable
planner: false
verifier: false
memory: false
stuck_detector: false
context_mode: sliding
"""


# ---------- the three files ----------
def test_there_is_one_file_for_each_config_the_runner_accepts() -> None:
    names = sorted(p.stem for p in FOLDER.glob("*.yaml"))
    assert names == sorted(results.CONFIG_NAMES) == ["full", "naive", "single_shot"]


@pytest.mark.parametrize("name", results.CONFIG_NAMES)
def test_each_file_loads_and_names_itself(name: str) -> None:
    loaded = config_files.load(name)
    assert loaded.config.name == name
    assert loaded.config.description


def test_the_full_config_is_the_product() -> None:
    config = config_files.load("full").config
    assert config.runner == "durable"
    assert (config.planner, config.verifier, config.memory, config.stuck_detector) == (
        True,
        True,
        True,
        True,
    )
    assert config.context_mode is ContextMode.FULL


def test_the_naive_config_is_the_same_stack_with_everything_extra_off() -> None:
    config = config_files.load("naive").config
    assert config.runner == "durable"
    assert (config.planner, config.verifier, config.memory, config.stuck_detector) == (
        False,
        False,
        False,
        False,
    )
    # The sliding window stands where the product has its two tiers (plan section 8.3).
    assert config.context_mode is ContextMode.SLIDING


def test_the_single_shot_config_runs_no_loop_and_has_no_switches() -> None:
    config = config_files.load("single_shot").config
    assert config.runner == "single_shot"
    assert (config.planner, config.verifier, config.memory, config.stuck_detector) == (
        None,
        None,
        None,
        None,
    )
    assert config.context_mode is None
    assert config.top_files == 5


def test_a_config_names_its_switches_for_the_result_files() -> None:
    assert config_files.switch(True) == "on"
    assert config_files.switch(False) == "off"
    assert config_files.switch(None) == "off"  # single shot uses no memory


# ---------- the hash ----------
@pytest.mark.parametrize("name", results.CONFIG_NAMES)
def test_the_hash_is_the_sha256_of_the_file(name: str) -> None:
    expected = hashlib.sha256((FOLDER / f"{name}.yaml").read_bytes()).hexdigest()
    assert config_files.load(name).sha256 == expected


def test_the_three_files_have_three_different_hashes() -> None:
    hashes = {config_files.load(name).sha256 for name in results.CONFIG_NAMES}
    assert len(hashes) == 3


def test_a_change_to_the_file_changes_the_hash(tmp_path: Path) -> None:
    before = config_files.load_file(write(tmp_path, NAIVE)).sha256
    after = config_files.load_file(write(tmp_path, NAIVE + "# one more comment\n")).sha256
    assert before != after


def test_the_hash_does_not_depend_on_what_the_yaml_means(tmp_path: Path) -> None:
    # A comment changes no setting and still changes the hash: the hash names the file.
    plain = config_files.load_file(write(tmp_path, NAIVE))
    commented = config_files.load_file(write(tmp_path, "# why\n" + NAIVE))
    assert plain.config == commented.config
    assert plain.sha256 != commented.sha256


# ---------- mistakes ----------
def test_a_config_that_does_not_exist_lists_the_ones_that_do() -> None:
    with pytest.raises(ValueError, match=r"no config 'fast'.*full.*naive.*single_shot"):
        config_files.load("fast")


def test_a_file_that_names_another_config_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, NAIVE.replace("name: naive", "name: full"))
    with pytest.raises(ValueError, match="naive.yaml.*name"):
        config_files.load_file(path)


def test_a_key_the_runner_does_not_know_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, NAIVE + "temperature: 0.2\n")
    with pytest.raises(ValueError, match="temperature"):
        config_files.load_file(path)


def test_a_missing_switch_is_refused_and_not_read_as_off(tmp_path: Path) -> None:
    path = write(tmp_path, NAIVE.replace("memory: false\n", ""))
    with pytest.raises(ValueError, match="memory"):
        config_files.load_file(path)


def test_a_single_shot_config_with_a_context_mode_is_refused(tmp_path: Path) -> None:
    body = """\
name: single_shot
description: A test copy.
runner: single_shot
planner: null
verifier: null
memory: null
stuck_detector: null
context_mode: full
top_files: 5
"""
    with pytest.raises(ValueError, match="context_mode"):
        config_files.load_file(write(tmp_path, body, "single_shot"))


def test_a_file_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="naive.yaml"):
        config_files.load_file(write(tmp_path, "- just\n- a list\n"))
