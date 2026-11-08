"""Serve the built console from the API (LH-802).

`npm run build` in `console/` writes `dist/`. When `LONGHAUL_CONSOLE_DIR` names that folder, the
API answers for the whole site: `/api`, `/healthz` and `/metrics` are the API, a path that is a
file of the build is that file, and every other path gets `index.html`, because the console's
router reads the address and a reload of `/tasks/abc` has to load the page. One process and one
origin means the browser needs no CORS and the console needs no base URL.

The catch-all is added after every API route, so a route always wins. Unknown paths under `/api`
stay 404: a typo in an API call must not come back as a page of HTML that the client then fails
to parse as JSON.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

__all__ = ["mount_console"]

# The build gives every file under assets/ a name with a hash of its content, so a copy kept by
# the browser can never be stale. index.html has a fixed name and names those files.
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"


def mount_console(app: FastAPI, directory: Path) -> None:
    root = directory.resolve()
    index = root / "index.html"
    if not index.is_file():
        raise ValueError(
            f"{root} has no index.html. Run `npm run build` in console/, "
            "or point LONGHAUL_CONSOLE_DIR at the built folder."
        )

    @app.get("/{path:path}", include_in_schema=False)
    async def console_file(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = (root / path).resolve()
        # resolve() follows `..` and links, so a path that leaves the folder fails this check.
        if candidate.is_file() and candidate.is_relative_to(root) and candidate != index:
            immutable = candidate.is_relative_to(root / "assets")
            return FileResponse(
                candidate, headers={"Cache-Control": IMMUTABLE if immutable else REVALIDATE}
            )
        return FileResponse(index, headers={"Cache-Control": REVALIDATE})
