"""Serve the built console from the API (LH-802).

`npm run build` in `console/` writes `dist/`. When `LONGHAUL_CONSOLE_DIR` names that folder, the
API answers for the whole site: `/api`, `/healthz` and `/metrics` are the API, a path that is a
file of the build is that file, and every other path gets `index.html`, because the console's
router reads the address and a reload of `/tasks/abc` has to load the page. One process and one
origin means the browser needs no CORS and the console needs no base URL.

The catch-all is added after every API route, so a route always wins. Unknown paths under `/api`
stay 404 for every method: a typo in an API call must not come back as a page of HTML that the
client then fails to parse as JSON, or as a 405.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from starlette.routing import Match

__all__ = ["mount_console"]

# The build gives every file under assets/ a name with a hash of its content, so a copy kept by
# the browser can never be stale. index.html has a fixed name and names those files.
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CATCH_ALLS = {"/api", "/api/{rest:path}", "/{path:path}"}
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

    @app.api_route("/api", methods=ALL_METHODS, include_in_schema=False)
    @app.api_route("/api/{rest:path}", methods=ALL_METHODS, include_in_schema=False)
    async def unknown_api(request: Request) -> Response:
        # A real address that was called with the wrong method is still a 405. Anything else
        # under /api is a typo: 404, whatever the method.
        for route in request.app.router.routes:
            if getattr(route, "path", None) in CATCH_ALLS:
                continue
            match, _ = route.matches(request.scope)
            if match == Match.PARTIAL:
                raise HTTPException(status_code=405, detail="Method Not Allowed")
        raise HTTPException(status_code=404, detail="Not Found")

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def console_file(path: str) -> FileResponse:
        file = served_file(root, path)
        if file is not None and file != index:
            immutable = file.is_relative_to(root / "assets")
            return FileResponse(
                file, headers={"Cache-Control": IMMUTABLE if immutable else REVALIDATE}
            )
        return FileResponse(index, headers={"Cache-Control": REVALIDATE})


def served_file(root: Path, path: str) -> Path | None:
    """The file of the build that `path` names, or None when it names none."""
    try:
        candidate = (root / path).resolve()
        # resolve() follows `..` and links, so a path that leaves the folder fails this check.
        if candidate.is_file() and candidate.is_relative_to(root):
            return candidate
    except (ValueError, OSError):
        # A NUL byte in the address, or a name the file system refuses: not a file of the build.
        pass
    return None
