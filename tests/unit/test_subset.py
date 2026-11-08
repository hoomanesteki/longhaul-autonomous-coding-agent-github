"""The SWE-bench Verified subset: the sampling and the frozen file (LH-902).

No network here. The sampling function gets a small fake dataset with the two fields it reads,
and the committed subset.txt is checked for what can be checked without the dataset. Run
`uv run python -m eval.swebench.select_subset --check` to compare it with the real one.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from pathlib import Path

import pytest

from eval.swebench import select_subset
from eval.swebench.select_subset import EASY, HARD, MEDIUM

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

SPLIT = {EASY: 15, MEDIUM: 20, HARD: 5}


def fake_dataset() -> list[dict[str, str]]:
    """Rows with the fields of the dataset that matter, and others that must not matter."""
    sizes = {EASY: 40, MEDIUM: 50, HARD: 12, ">4 hours": 3}
    rows = []
    for difficulty, size in sizes.items():
        for number in range(size):
            rows.append(
                {
                    "instance_id": f"org__repo{len(difficulty)}-{number}",
                    "difficulty": difficulty,
                    "repo": "org/repo",
                    "problem_statement": "text",
                }
            )
    return rows


def difficulty_of(rows: list[dict[str, str]]) -> dict[str, str]:
    return {row["instance_id"]: row["difficulty"] for row in rows}


def test_stratified_counts() -> None:
    rows = fake_dataset()

    picked = select_subset.sample_subset(rows)

    assert len(picked) == 40 == len(set(picked))
    by_difficulty = difficulty_of(rows)
    assert Counter(by_difficulty[i] for i in picked) == SPLIT
    assert select_subset.SEED == 42
    assert dict(select_subset.STRATA) == SPLIT


def test_the_ids_come_out_sorted() -> None:
    picked = select_subset.sample_subset(fake_dataset())

    assert picked == sorted(picked)


def test_the_same_seed_gives_the_same_ids_whatever_the_order_of_the_rows() -> None:
    rows = fake_dataset()
    shuffled = list(rows)
    random.Random(1).shuffle(shuffled)

    assert select_subset.sample_subset(rows) == select_subset.sample_subset(shuffled)


def test_another_seed_gives_other_ids() -> None:
    rows = fake_dataset()

    assert select_subset.sample_subset(rows, seed=42) != select_subset.sample_subset(rows, seed=43)


def test_the_hardest_tasks_are_never_picked() -> None:
    rows = fake_dataset()
    by_difficulty = difficulty_of(rows)

    picked = select_subset.sample_subset(rows)

    assert ">4 hours" not in {by_difficulty[i] for i in picked}


def test_a_stratum_that_is_too_small_is_an_error_that_names_it() -> None:
    rows = [row for row in fake_dataset() if row["difficulty"] != HARD][:100]

    with pytest.raises(ValueError, match="1-4 hours"):
        select_subset.sample_subset(rows)


def test_a_row_without_the_fields_is_an_error() -> None:
    with pytest.raises(ValueError, match="difficulty"):
        select_subset.sample_subset([{"instance_id": "a__b-1"}])


def test_the_file_is_one_id_per_line_and_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "subset.txt"
    ids = ["a__b-1", "a__b-2", "c__d-30"]

    select_subset.write_subset(ids, path)

    assert path.read_text(encoding="utf-8") == "a__b-1\na__b-2\nc__d-30\n"
    assert select_subset.read_subset(path) == ids


def test_the_frozen_file_is_not_replaced_without_force(tmp_path: Path) -> None:
    path = tmp_path / "subset.txt"
    select_subset.write_subset(["a__b-1"], path)

    with pytest.raises(FileExistsError, match="frozen"):
        select_subset.write_subset(["a__b-2"], path)
    assert select_subset.read_subset(path) == ["a__b-1"]

    select_subset.write_subset(["a__b-2"], path, force=True)
    assert select_subset.read_subset(path) == ["a__b-2"]


def test_a_file_with_a_bad_line_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "subset.txt"
    path.write_text("a__b-1\n../../etc/passwd\n", encoding="utf-8")

    with pytest.raises(ValueError, match="passwd"):
        select_subset.read_subset(path)


def test_the_committed_subset_has_forty_distinct_instance_ids() -> None:
    ids = select_subset.read_subset()

    assert len(ids) == 40 == len(set(ids))
    assert ids == sorted(ids)
    assert all(re.fullmatch(r"[\w.-]+__[\w.-]+-\d+", i) for i in ids)
