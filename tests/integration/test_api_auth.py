"""The console API behind a bearer token (LH-807).

Every test builds the app on a scratch database, as test_api.py does, and calls it through httpx's
ASGI transport. The checks are the ones an operator relies on: no token and a wrong token both
get 401 on every `/api` route, `/healthz` stays open, `/metrics` is open only on a server bound to
this machine, and a token that keeps sending actions is slowed down after 30 in a minute.

The rate limit reads a clock that the test owns, so a minute passes without waiting for it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from tests.integration import test_run_report as report_tests

from longhaul.api.app import create_app
from longhaul.api.deps import ApiContext
from longhaul.api.limits import ActionLimiter
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.memory.embedder import HashEmbedder
from longhaul.memory.store import PgMemoryStore

pytestmark = [pytest.mark.integration, pytest.mark.phase8]

database = report_tests.database
sessions = report_tests.sessions

Sessions = async_sessionmaker[AsyncSession]

# Built from pieces so that no secret-looking literal sits in the source.
TOKEN = "admin-" + "k3Y9" * 8
OTHER_TOKEN = "admin-" + "z7Q2" * 8
ACTIONS_PER_MINUTE = 30


class Clock:
    """A clock that moves only when the test says so."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@asynccontextmanager
async def serving(
    sessions: Sessions,
    *,
    token: str | None = TOKEN,
    metrics_public: bool = False,
    clock: Clock | None = None,
) -> AsyncIterator[httpx2.AsyncClient]:
    async def connect() -> Client:
        raise ConnectionError("cannot reach Temporal at localhost:1")

    context = ApiContext(
        sessions=sessions,
        blobs=MemoryBlobStore(),
        memory=PgMemoryStore(sessions, HashEmbedder()),
        temporal=connect,
        admin_token=token,
        metrics_public=metrics_public,
        limiter=ActionLimiter(clock=clock or Clock()),
    )
    transport = httpx2.ASGITransport(app=create_app(context))
    async with httpx2.AsyncClient(transport=transport, base_url="http://api") as client:
        yield client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def api_routes() -> list[tuple[str, str]]:
    """Every method and path under /api that the app documents, with ids filled in."""
    schema = create_app(with_console=False).openapi()
    routes = []
    for path, methods in schema["paths"].items():
        if not path.startswith("/api"):
            continue
        for method in methods:
            routes.append((method.upper(), path.replace("{", "x-").replace("}", "")))
    return sorted(routes)


def test_the_route_list_covers_reads_and_actions() -> None:
    # A guard for the parametrized test below: if the filter above matched nothing, every
    # parameter would pass by not existing.
    routes = api_routes()

    assert len(routes) >= 13
    assert {method for method, _ in routes} == {"GET", "POST"}


@pytest.mark.parametrize(("method", "path"), api_routes())
async def test_requires_token(sessions: Sessions, method: str, path: str) -> None:
    async with serving(sessions) as client:
        missing = await client.request(method, path)
        wrong = await client.request(method, path, headers=bearer(OTHER_TOKEN))
        # A scheme other than Bearer, and an empty token, are no token.
        basic = await client.request(method, path, headers={"Authorization": f"Basic {TOKEN}"})
        empty = await client.request(method, path, headers={"Authorization": "Bearer "})

    for response in (missing, wrong, basic, empty):
        assert response.status_code == 401, (method, path, response.text)
        assert response.json() == {"detail": "a valid bearer token is required"}
        assert response.headers["www-authenticate"] == "Bearer"


async def test_requires_token_before_it_reads_the_request(sessions: Sessions) -> None:
    # A bad body or a bad query must not be answered with 422 to a caller with no token, or the
    # reply would tell a stranger what the API accepts.
    async with serving(sessions) as client:
        body = await client.post("/api/tasks/x/hint", json={"nonsense": True})
        query = await client.get("/api/tasks", params={"limit": "many"})

    assert body.status_code == 401
    assert query.status_code == 401


async def test_a_token_that_is_not_ascii_is_refused(sessions: Sessions) -> None:
    # compare_digest raises on a str with a non-ASCII character, which would be a 500.
    async with serving(sessions) as client:
        response = await client.get(
            "/api/repos", headers=[(b"authorization", "Bearer café".encode())]
        )

    assert response.status_code == 401


async def test_the_right_token_is_let_through(sessions: Sessions) -> None:
    async with serving(sessions) as client:
        repos = await client.get("/api/repos", headers=bearer(TOKEN))
        # The scheme name is case-insensitive in the HTTP spec.
        lower = await client.get("/api/repos", headers={"Authorization": f"bearer {TOKEN}"})
        missing_item = await client.post("/api/memory/nothing/approve", headers=bearer(TOKEN))

    assert repos.status_code == 200
    assert lower.status_code == 200
    assert missing_item.status_code == 404


async def test_a_server_with_no_token_set_refuses_everyone(sessions: Sessions) -> None:
    async with serving(sessions, token=None) as client:
        anything = await client.get("/api/repos", headers=bearer(TOKEN))
        nothing = await client.get("/api/repos", headers=bearer("None"))
        blank = await client.get("/api/repos", headers=bearer(" "))

    assert (anything.status_code, nothing.status_code, blank.status_code) == (401, 401, 401)


async def test_healthz_is_public(sessions: Sessions) -> None:
    async with serving(sessions) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200


async def test_metrics_need_the_token_unless_the_server_is_local(sessions: Sessions) -> None:
    async with serving(sessions, metrics_public=False) as client:
        closed = await client.get("/metrics")
        wrong = await client.get("/metrics", headers=bearer(OTHER_TOKEN))
        with_token = await client.get("/metrics", headers=bearer(TOKEN))
    async with serving(sessions, metrics_public=True) as client:
        open_ = await client.get("/metrics")

    assert (closed.status_code, wrong.status_code) == (401, 401)
    assert with_token.status_code == 200
    assert open_.status_code == 200
    # Local metrics are open, but the routes beside them are not.
    async with serving(sessions, metrics_public=True) as client:
        assert (await client.get("/api/repos")).status_code == 401


async def an_action(client: httpx2.AsyncClient, token: str = TOKEN) -> httpx2.Response:
    # An item that does not exist answers 404 after the checks, which is all these tests need.
    return await client.post(f"/api/memory/{uuid.uuid4()}/approve", headers=bearer(token))


async def test_rate_limit_actions(sessions: Sessions) -> None:
    clock = Clock()
    async with serving(sessions, clock=clock) as client:
        first = [await an_action(client) for _ in range(ACTIONS_PER_MINUTE)]
        over = await an_action(client)
        # Reads are not actions, and a limited caller can still look at the console.
        read = await client.get("/api/repos", headers=bearer(TOKEN))
        clock.now += 59
        still = await an_action(client)
        clock.now += 2
        later = await an_action(client)

    assert {response.status_code for response in first} == {404}
    assert over.status_code == 429
    assert over.headers["retry-after"] == "60"
    assert over.json() == {"detail": "too many actions, try again in 60 seconds"}
    assert read.status_code == 200
    assert still.status_code == 429
    assert still.headers["retry-after"] == "1"
    assert later.status_code == 404


async def test_a_refused_request_does_not_use_up_the_limit(sessions: Sessions) -> None:
    async with serving(sessions) as client:
        for _ in range(ACTIONS_PER_MINUTE + 5):
            assert (await an_action(client, OTHER_TOKEN)).status_code == 401
        right = await an_action(client)

    assert right.status_code == 404
