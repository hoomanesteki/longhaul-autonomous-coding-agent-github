"""Pick the 40 SWE-bench Verified instances that every headline run uses (plan section 8.2).

    uv run python -m eval.swebench.select_subset

It loads the dataset, sorts the instances into the difficulty classes that the dataset carries,
and takes 15 easy, 20 medium and 5 hard ones with seed 42. The ids go to eval/swebench/subset.txt,
one per line, in sorted order. That file is committed and never changes after the first headline
run: a different subset would make two runs incomparable. The command therefore refuses to
replace it unless `--force` is given, and `--check` compares it with a fresh draw instead.

Run it as a module from the repo root. As a script it would put this folder first on sys.path,
where `swebench/` would hide the swebench package.

The draw does not depend on the order of the rows. Each class is sorted by instance id before the
sample is taken, so a dataset that comes back in another order gives the same 40 ids.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

# The dataset that the harness of swebench 5.x reads: its rows carry the image name and the
# eval script of each instance. princeton-nlp/SWE-bench_Verified holds the same 500 ids and
# difficulty labels, but has neither column.
DATASET = "SWE-bench/SWE-bench_Verified"
SEED = 42
EASY = "<15 min fix"
MEDIUM = "15 min - 1 hour"
HARD = "1-4 hours"
# (difficulty label, how many to take). The label ">4 hours" is not used: three instances only.
STRATA: tuple[tuple[str, int], ...] = ((EASY, 15), (MEDIUM, 20), (HARD, 5))
SUBSET_FILE = Path(__file__).with_name("subset.txt")

_INSTANCE_ID = re.compile(r"[A-Za-z0-9][\w.-]*__[\w.-]+-\d+")


def sample_subset(
    rows: Iterable[Mapping[str, Any]],
    *,
    seed: int = SEED,
    strata: Sequence[tuple[str, int]] = STRATA,
) -> list[str]:
    """The sampled instance ids in sorted order. Rows need `instance_id` and `difficulty`.

    Raises ValueError for a row without those fields, and for a class that has fewer instances
    than the sample asks for.
    """
    by_difficulty: dict[str, list[str]] = {}
    for row in rows:
        try:
            by_difficulty.setdefault(str(row["difficulty"]), []).append(str(row["instance_id"]))
        except KeyError as exc:
            raise ValueError(f"a row of the dataset has no {exc.args[0]} field") from exc
    rng = random.Random(seed)
    picked: list[str] = []
    for label, count in strata:
        pool = sorted(by_difficulty.get(label, []))
        if len(pool) < count:
            raise ValueError(
                f"the class {label!r} has {len(pool)} instances, fewer than the {count} to sample"
            )
        picked += rng.sample(pool, count)
    return sorted(picked)


def read_subset(path: Path = SUBSET_FILE) -> list[str]:
    """The ids in the file. A line that is not an instance id is an error."""
    ids = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for instance_id in ids:
        if not _INSTANCE_ID.fullmatch(instance_id):
            raise ValueError(f"{path}: {instance_id!r} is not an instance id")
    return ids


def write_subset(ids: Sequence[str], path: Path = SUBSET_FILE, *, force: bool = False) -> None:
    if path.exists() and not force:
        raise FileExistsError(
            f"{path} exists and is frozen after the first headline run; use --force to replace it"
        )
    path.write_text("".join(f"{instance_id}\n" for instance_id in ids), encoding="utf-8")


def load_rows(dataset: str = DATASET) -> list[dict[str, str]]:
    """`instance_id` and `difficulty` of every instance. Needs the eval extra and the network."""
    from datasets import load_dataset  # lazy: CI and the unit tests do not install it

    rows = load_dataset(dataset, split="test")
    return [{"instance_id": r["instance_id"], "difficulty": r["difficulty"]} for r in rows]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draw the SWE-bench Verified subset.")
    parser.add_argument("--dataset", default=DATASET)
    parser.add_argument("--out", type=Path, default=SUBSET_FILE)
    parser.add_argument("--check", action="store_true", help="compare the file with a fresh draw")
    parser.add_argument("--force", action="store_true", help="replace the frozen file")
    args = parser.parse_args(argv)
    ids = sample_subset(load_rows(args.dataset))
    if args.check:
        if read_subset(args.out) != ids:
            print(f"{args.out} differs from a fresh draw with seed {SEED}", file=sys.stderr)
            return 1
        print(f"{args.out} matches a fresh draw: {len(ids)} ids, seed {SEED}")
        return 0
    try:
        write_subset(ids, args.out, force=args.force)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {len(ids)} ids to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
