"""The API serves the built console at `/` (LH-802).

In the compose `full` profile one process answers for both: `/api` and the probes are the API,
and everything else is the console's files, with `index.html` for any address that is a page of
the single-page app. The tests build a small console folder in tmp_path, and drive the app
without a database, because the file routes do not use one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from longhaul.api.app import create_app, openapi_schema
from longhaul.api.deps import ApiContext
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.memory.embedder import HashEmbedder
from longhaul.memory.store import PgMemoryStore
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.unit, pytest.mark.phase8]

INDEX = "<!doctype html><title>Longhaul console</title><div id=root></div>"


async def unreachable() -> Client:
    raise ConnectionError("no Temporal in this test")


@pytest.fixture
def console(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "app-1a2b3c.js").write_text("console.log('hi');", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the console", encoding="utf-8")
    return root


def test_the_root_serves_index_html(console: Path) -> None:
    client = TestClient(create_app(console_dir=console))

    response = client.get("/")

    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["content-type"].startswith("text/html")
    # The page names hashed files. If the browser kept an old copy of it after a new build, it
    # would ask for files that are gone.
    assert response.headers["cache-control"] == "no-cache"


def test_a_built_file_is_served_and_kept_by_the_browser(console: Path) -> None:
    client = TestClient(create_app(console_dir=console))

    response = client.get("/assets/app-1a2b3c.js")

    assert response.status_code == 200
    assert response.text == "console.log('hi');"
    assert "immutable" in response.headers["cache-control"]


@pytest.mark.parametrize("path", ["/tasks", "/tasks/abc-123", "/memory", "/assets/gone.js"])
def test_an_address_that_is_not_a_file_gets_the_page(console: Path, path: str) -> None:
    # The router of the console reads the address, so a reload of /tasks/abc-123 must still load
    # the page and not a 404.
    client = TestClient(create_app(console_dir=console))

    response = client.get(path)

    assert response.status_code == 200
    assert response.text == INDEX


def test_an_unknown_api_address_is_a_404_and_not_the_page(console: Path) -> None:
    client = TestClient(create_app(console_dir=console))

    for path in ("/api/nothing", "/api"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "Longhaul console" not in response.text


def test_the_api_routes_come_before_the_files(console: Path) -> None:
    # /metrics checks the token setting of the context (LH-807), so this app is given one. The
    # stores are never touched: /metrics reads no database.
    sessions = async_sessionmaker[AsyncSession]()
    context = ApiContext(
        sessions=sessions,
        blobs=MemoryBlobStore(),
        memory=PgMemoryStore(sessions, HashEmbedder()),
        temporal=unreachable,
        metrics_public=True,
    )
    client = TestClient(create_app(context, console_dir=console))

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "longhaul_api_requests_total" in response.text or response.text.startswith("#")


@pytest.mark.parametrize(
    "path", ["/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"]
)
def test_a_path_that_leaves_the_folder_does_not_read_outside_it(console: Path, path: str) -> None:
    client = TestClient(create_app(console_dir=console))

    response = client.get(path)

    assert "outside the console" not in response.text


def test_without_a_console_folder_the_root_is_a_404() -> None:
    client = TestClient(create_app())

    assert client.get("/").status_code == 404


def test_a_folder_with_no_index_html_is_refused_at_start(tmp_path: Path) -> None:
    # A typo in LONGHAUL_CONSOLE_DIR or a missing `npm run build` should stop the server, not
    # leave one that answers 404 at the root.
    with pytest.raises(ValueError, match="index.html"):
        create_app(console_dir=tmp_path)


def test_the_folder_comes_from_the_settings(console: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LONGHAUL_CONSOLE_DIR", str(console))
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        assert client.get("/").text == INDEX
    finally:
        get_settings.cache_clear()


def test_the_console_folder_is_off_by_default() -> None:
    assert EnvSettings.model_fields["console_dir"].default == ""


def test_the_schema_is_the_same_with_and_without_the_console(console: Path) -> None:
    # console/openapi.json is generated from the app with no folder. A folder in the
    # environment must not put the file routes into it.
    assert create_app(console_dir=console).openapi() == openapi_schema()


def test_the_schema_dump_ignores_a_console_folder_that_has_no_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `make api-types` runs on a fresh clone, where LONGHAUL_CONSOLE_DIR may name a dist/ that
    # does not exist yet. The dump needs no console and must not read the setting.
    monkeypatch.setenv("LONGHAUL_CONSOLE_DIR", str(tmp_path / "missing"))
    get_settings.cache_clear()
    try:
        assert openapi_schema()["info"]["title"]
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("path", ["/%00", "/a%00b", "/assets/%00"])
def test_a_path_with_a_nul_byte_gets_the_page_and_not_a_500(console: Path, path: str) -> None:
    client = TestClient(create_app(console_dir=console))

    response = client.get(path)

    assert response.status_code == 200
    assert response.text == INDEX


def test_head_on_the_root_is_answered(console: Path) -> None:
    client = TestClient(create_app(console_dir=console))

    assert client.head("/").status_code == 200


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_an_unknown_api_address_is_a_404_for_every_method(console: Path, method: str) -> None:
    # A typo in the URL of an action must not come back as "405 Method Not Allowed".
    client = TestClient(create_app(console_dir=console))

    response = client.request(method, "/api/nope")

    assert response.status_code == 404


def test_a_wrong_method_on_a_real_api_address_is_still_a_405(console: Path) -> None:
    client = TestClient(create_app(console_dir=console))

    assert client.post("/api/repos").status_code == 405
