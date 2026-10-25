"""Print the pytest marker expression for phases 0..N."""

import sys


def marker_expression(n: int) -> str:
    if not 0 <= n <= 9:
        raise ValueError(f"phase must be between 0 and 9, got {n}")
    return " or ".join(f"phase{i}" for i in range(n + 1))


def main(argv: list[str]) -> int:
    try:
        print(marker_expression(int(argv[0])))
    except (IndexError, ValueError) as exc:
        print(f"usage: markers.py N (0-9): {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
