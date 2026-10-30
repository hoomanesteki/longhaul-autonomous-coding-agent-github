"""Print the pytest marker expression for phases 0..N."""

import sys

USAGE = "usage: markers.py N (N is a phase number from 0 to 9)"


def marker_expression(n: int) -> str:
    if not 0 <= n <= 9:
        raise ValueError(f"phase must be between 0 and 9, got {n}")
    return " or ".join(f"phase{i}" for i in range(n + 1))


def main(argv: list[str]) -> int:
    # A bad call must leave stdout empty and exit 2. The Makefile captures stdout and
    # refuses to run pytest without an expression.
    if len(argv) != 1:
        print(f"{USAGE}: expected one argument, got {len(argv)}", file=sys.stderr)
        return 2
    try:
        expression = marker_expression(int(argv[0]))
    except ValueError as exc:
        print(f"{USAGE}: {exc}", file=sys.stderr)
        return 2
    print(expression)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
