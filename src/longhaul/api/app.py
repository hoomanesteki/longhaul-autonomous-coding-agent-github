"""The console API as a FastAPI app (LH-801, ADR-0011).

`create_app(context)` builds the app on the stores it is given, which is how the tests use it.
`create_app()` with no context builds one from the LONGHAUL_* settings when the server starts and
closes it when the server stops, which is how `longhaul api` runs it (uvicorn's factory mode).
The app does nothing at import time and nothing until it starts, so `openapi_schema()` can dump
the schema in CI with no database or Temporal.

Beside the task and memory routes this module has the three that belong to no resource:

* `GET /api/repos`, the repos in `repo_settings` with their task counts;
* `GET /healthz`, which answers 200 when the database does. Temporal is left out on purpose: the
  console works from the database without it, and a probe that failed with Temporal would take
  the whole page down for an outage that only breaks the live status and the actions;
* `GET /metrics`, in the Prometheus text format.

Everything under `/api` needs the bearer token (LH-807), and so does `/metrics` unless the server
listens on this machine. `/healthz` and the console's own files are open: the login page has to
load before there is a token to send, and a probe has none.

The interactive docs (`/docs`, `/redoc` and `/openapi.json`) are off. They need no token, and
`/docs` loads Swagger UI from a CDN on the origin where the console keeps its token, so a page
that the CDN changed could read it. `LONGHAUL_API_DOCS=true` turns them on for local development.
`make api-types` does not need the routes: it builds the schema with `openapi_schema()`.

Operation ids are the names of the route functions, so the generated client (LH-802) has
`listTasks` and `approvePlan` and not `list_tasks_api_tasks_get`.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.routing import APIRoute
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from longhaul.api import metrics, queries, routes_memory, routes_tasks
from longhaul.api.console import mount_console
from longhaul.api.deps import ApiContext, Ctx, authorize, authorize_metrics, open_context
from longhaul.api.errors import PROTECTED
from longhaul.api.schemas import Health, RepoView
from longhaul.settings import EnvSettings, get_settings

__all__ = ["create_app", "openapi_schema", "render_openapi"]

TITLE = "Longhaul console API"
DESCRIPTION = (
    "Tasks, timelines, actions and memory for the Longhaul console. The console reads this API "
    "and nothing else: it never talks to the database or to Temporal."
)


def operation_id(route: APIRoute) -> str:
    return route.name


@asynccontextmanager
async def from_settings(app: FastAPI) -> AsyncIterator[None]:
    """Build the context of a deployment when the server starts, and close it when it stops."""
    async with open_context(EnvSettings()) as context:
        app.state.context = context
        yield


def create_app(
    context: ApiContext | None = None,
    console_dir: Path | None = None,
    *,
    with_console: bool = True,
    docs: bool | None = None,
) -> FastAPI:
    """Build the app. `console_dir` is the built console to serve at `/`.

    A deployment (no context) takes the folder from LONGHAUL_CONSOLE_DIR, and the tests that give
    a context also give the folder or none. `with_console=False` serves no console and reads no
    setting for one, which is what `openapi_schema()` uses: the dump must work on a clone that
    has no build yet. The file routes are left out of the schema in any case.

    `docs` turns the interactive docs on. None means LONGHAUL_API_DOCS for a deployment, and off
    for an app built on a context, which reads no settings.
    """
    show_docs = docs if docs is not None else context is None and get_settings().api_docs
    app = FastAPI(
        title=TITLE,
        description=DESCRIPTION,
        version="1.0.0",
        generate_unique_id_function=operation_id,
        lifespan=from_settings if context is None else None,
        docs_url="/docs" if show_docs else None,
        redoc_url="/redoc" if show_docs else None,
        openapi_url="/openapi.json" if show_docs else None,
    )
    if context is not None:
        app.state.context = context
    app.state.api_metrics = metrics.ApiMetrics()
    app.include_router(routes_tasks.router)
    app.include_router(routes_memory.router)
    add_system_routes(app)
    folder = None
    if with_console:
        folder = console_dir if console_dir is not None else configured_console_dir(context)
    if folder is not None:
        mount_console(app, folder)  # last, so that every route above it wins
    app.middleware("http")(count_requests)
    return app


def configured_console_dir(context: ApiContext | None) -> Path | None:
    """The folder of LONGHAUL_CONSOLE_DIR, for the app a deployment starts.

    An app built on a context is a test's. It reads no settings, so a developer's environment
    cannot change what a test serves.
    """
    if context is not None:
        return None
    configured = get_settings().console_dir
    return Path(configured) if configured else None


async def count_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    started = time.perf_counter()
    status = 500  # an exception that no handler turned into a response
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        template = getattr(route, "path", None)
        request.app.state.api_metrics.observe(
            request.method, template, status, time.perf_counter() - started
        )


def add_system_routes(app: FastAPI) -> None:
    @app.get("/api/repos", dependencies=[Depends(authorize)], responses=PROTECTED)
    async def list_repos(ctx: Ctx) -> list[RepoView]:
        """The repos Longhaul knows, from `repo_settings`, with how many tasks each has."""
        return await queries.list_repos(ctx.sessions)

    @app.get("/healthz")
    async def healthz(ctx: Ctx) -> Health:
        """200 when the database answers."""
        try:
            async with ctx.sessions() as session:
                await session.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError) as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return Health(status="ok")

    @app.get("/metrics", dependencies=[Depends(authorize_metrics)], responses=PROTECTED)
    async def prometheus(request: Request) -> Response:
        """Prometheus text: the worker metrics of this process and the API's own."""
        body = metrics.exposition(request.app.state.api_metrics)
        return Response(body, media_type=CONTENT_TYPE_LATEST)


def openapi_schema() -> dict[str, Any]:
    """The OpenAPI schema of the app. It needs no database, no blob store and no Temporal."""
    return create_app(with_console=False, docs=False).openapi()


def render_openapi() -> str:
    """The text of console/openapi.json: sorted keys, two spaces, a newline at the end.

    The same schema always gives the same bytes, so a diff of the file shows only a real change
    of the API.
    """
    return json.dumps(openapi_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
