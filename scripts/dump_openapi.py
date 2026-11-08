"""Write the OpenAPI schema of the console API to console/openapi.json (`make api-types`).

The console's typed client is generated from this file (LH-802), so it is committed. A unit test
compares it with the app and fails when the two differ. The schema needs no database, blob store
or Temporal: the app builds its stores only when the server starts.

    uv run python scripts/dump_openapi.py            # write the file
    uv run python scripts/dump_openapi.py --check    # exit 1 when the file is out of date
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from longhaul.api.app import render_openapi

TARGET = Path(__file__).resolve().parents[1] / "console" / "openapi.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--check", action="store_true", help="do not write; fail when the file is out of date"
    )
    args = parser.parse_args(argv)
    schema = render_openapi()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != schema:
            print(f"{TARGET} is out of date. Run `make api-types`.", file=sys.stderr)
            return 1
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(schema, encoding="utf-8")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
