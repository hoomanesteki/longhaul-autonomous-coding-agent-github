"""What the worker process is made of (LH-208).

Nothing here starts Temporal or Docker. `build_parts` is given a fake sandbox manager, because
building the real one connects to the Docker daemon, and the database and blob clients it makes
connect only when they are first used. tests/integration/test_worker.py runs `serve` against a
Temporal test server, and tests/e2e/test_durable_run.py starts the real process.
"""

import asyncio
import os
import signal
import socket
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity

from longhaul.blobs.s3 import S3BlobStore
from longhaul.contracts import Message, Role, TextBlock, ToolSpec
from longhaul.cost.ledger import SqlLedger
from longhaul.cost.pricing import PricingError
from longhaul.durable import acts, worker
from longhaul.github.client import GitHubClient
from longhaul.github.remote import GitHubRemote
from longhaul.github.side_effects import SqlLedger as SqlEffectLedger
from longhaul.memory.embedder import HashEmbedder, LocalEmbedder
from longhaul.memory.store import PgMemoryStore
from longhaul.providers.replay import ReplayProvider
from longhaul.providers.scripted import ScriptedProvider
from longhaul.roles import coder, planner
from longhaul.settings import AppConfig, EnvSettings, Memory, load_config
from longhaul.telemetry import metrics

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = load_config(ROOT / "config" / "longhaul.example.yaml")
# The example config names the local embedder, which needs the memory extra that CI does not
# install. The tests that are not about memory use the hash embedder, which needs nothing.
CONFIG = EXAMPLE.model_copy(update={"memory": Memory(enabled=True, embedder="hash")})
CALC_FIX = ROOT / "tests" / "fixtures" / "scripts" / "calc_fix.yaml"


def make_settings(tmp_path: Path, **fields: Any) -> EnvSettings:
    values: dict[str, Any] = {
        "database_url": "postgresql://nobody:nopass@127.0.0.1:1/never",
        "blob_endpoint": "http://127.0.0.1:1",
        "blob_bucket": "nothing",
        "blob_access_key": "key",
        "blob_secret_key": "secret",
        "ckpt_dir": tmp_path / "ckpt",
        **fields,
    }
    return EnvSettings(_env_file=None, **values)


class FakeManager:
    """Stands in for DockerSandboxManager, which connects to the daemon when it is built."""


def names_of(parts: worker.WorkerParts) -> list[str]:
    return sorted(activity._Definition.must_from_callable(fn).name or "" for fn in parts.activities)


# ---------- the provider ----------
def test_a_scripted_worker_with_a_script_plays_it(tmp_path: Path) -> None:
    provider = worker.build_provider(make_settings(tmp_path, provider="scripted", script=CALC_FIX))
    assert isinstance(provider, ScriptedProvider)


def test_a_scripted_worker_without_a_script_has_no_provider_of_its_own(tmp_path: Path) -> None:
    # Every task then brings its script, which is what `longhaul run --script` does.
    assert worker.build_provider(make_settings(tmp_path, provider="scripted")) is None


async def test_a_replay_worker_plays_the_recordings_folder(tmp_path: Path) -> None:
    folder = tmp_path / "recordings"
    recorder = ReplayProvider(folder, record_with=ScriptedProvider(CALC_FIX))
    await recorder.complete(
        model="m",
        system="Role: coder",
        messages=[Message(role="user", content=[TextBlock(text="go")])],
        tools=[ToolSpec(name="list_dir", description="List.", input_schema={"type": "object"})],
        max_tokens=100,
    )

    provider = worker.build_provider(
        make_settings(tmp_path, provider="replay", recordings_dir=folder)
    )

    assert isinstance(provider, ReplayProvider)
    assert provider.name == "replay"


def test_a_replay_worker_without_a_recordings_folder_stops_before_it_starts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="LONGHAUL_RECORDINGS_DIR"):
        worker.build_provider(make_settings(tmp_path, provider="replay"))


def test_a_recording_worker_wraps_its_provider(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        provider="scripted",
        script=CALC_FIX,
        record=True,
        recordings_dir=tmp_path / "recordings",
    )
    provider = worker.build_provider(settings)
    assert isinstance(provider, ReplayProvider)
    assert provider.name == "scripted"


def test_a_scripted_worker_without_a_script_cannot_record(tmp_path: Path) -> None:
    # The scripts come with the tasks and reach the model through a provider that the worker does
    # not hold, so there would be nothing to record. Saying so beats a folder that stays empty.
    settings = make_settings(
        tmp_path, provider="scripted", record=True, recordings_dir=tmp_path / "recordings"
    )
    with pytest.raises(ValueError, match="LONGHAUL_SCRIPT"):
        worker.build_provider(settings)


def test_a_real_provider_without_its_key_stops_the_worker_before_it_starts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        worker.build_provider(make_settings(tmp_path, provider="anthropic"))


# ---------- the activities ----------
def test_the_worker_registers_one_activity_for_every_name_the_workflow_calls(
    tmp_path: Path,
) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    assert names_of(parts) == sorted(act.name for act in acts.ALL)


# ---------- the poller (LH-307) ----------
TOKEN = "ghp_unit_test_token_0123456789abcdef"


def poller_of(parts: worker.WorkerParts) -> Any:
    (poll,) = [fn for fn in parts.activities if getattr(fn, "__name__", "") == "poll_once"]
    return poll.__self__


def test_a_worker_without_a_github_token_still_registers_poll_once(tmp_path: Path) -> None:
    # A worker for local runs needs no token. A poll on it fails with the name of the setting.
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    assert parts.github is None
    assert poller_of(parts)._github is None


def test_the_token_of_the_settings_goes_to_the_poller(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, github_token=TOKEN)
    parts = worker.build_parts(settings, CONFIG, FakeManager())  # type: ignore[arg-type]
    assert isinstance(parts.github, GitHubClient)
    assert poller_of(parts)._github is parts.github


def test_tasks_start_on_the_queue_of_the_worker(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, task_queue="lh-queue-of-this-worker")
    parts = worker.build_parts(settings, CONFIG, FakeManager())  # type: ignore[arg-type]
    assert poller_of(parts)._task_queue == "lh-queue-of-this-worker"


def test_a_token_that_cannot_be_sent_stops_the_worker_before_it_starts(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, github_token="not a token")
    with pytest.raises(ValueError, match="GitHub token"):
        worker.build_parts(settings, CONFIG, FakeManager())  # type: ignore[arg-type]


async def test_closing_the_parts_closes_the_github_client(tmp_path: Path) -> None:
    parts = worker.build_parts(
        make_settings(tmp_path, github_token=TOKEN),
        CONFIG,
        FakeManager(),  # type: ignore[arg-type]
    )
    assert parts.github is not None
    await parts.close()
    assert parts.github._http.is_closed


def test_model_calls_go_on_the_cost_ledger(tmp_path: Path) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    (llm,) = [fn.__self__ for fn in parts.activities if getattr(fn, "__name__", "") == "llm_turn"]
    assert isinstance(llm._ledger, SqlLedger)


def test_the_model_turns_and_the_compactor_follow_the_context_settings(tmp_path: Path) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    (llm,) = [fn.__self__ for fn in parts.activities if getattr(fn, "__name__", "") == "llm_turn"]
    (compactor,) = [
        fn.__self__ for fn in parts.activities if getattr(fn, "__name__", "") == "compact"
    ]
    assert llm._context == CONFIG.context
    assert compactor._context == CONFIG.context
    # Compactor calls are priced and ledgered like any other call (I6).
    assert isinstance(compactor._ledger, SqlLedger)
    # Both estimate with one meter per task, so the tokens in a compaction event and the
    # projections of the model turns come from the same ratio.
    assert compactor._meters is llm._meters


def test_a_model_without_a_price_stops_the_worker_before_it_starts(tmp_path: Path) -> None:
    config = CONFIG.model_copy(
        update={"models": CONFIG.models.model_copy(update={"coder": "claude-opus-9"})}
    )
    settings = make_settings(tmp_path, provider="anthropic", ANTHROPIC_API_KEY="test-key")
    with pytest.raises(PricingError, match="claude-opus-9"):
        worker.build_parts(settings, config, FakeManager())  # type: ignore[arg-type]


def test_a_planner_turn_limit_the_workflow_cannot_follow_stops_the_worker(tmp_path: Path) -> None:
    # The workflow keeps its own copy of the limit, so a different number in the file would be
    # read, accepted and ignored. Stopping at the start says so while the operator is looking.
    loop = CONFIG.loop.model_copy(update={"planner_max_turns": 8})
    config = CONFIG.model_copy(update={"loop": loop})
    with pytest.raises(ValueError, match=r"loop\.planner_max_turns.*8.*20"):
        worker.build_parts(make_settings(tmp_path), config, FakeManager())  # type: ignore[arg-type]


def test_the_worker_shares_its_blob_store_with_other_processes(tmp_path: Path) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    assert isinstance(parts.blobs, S3BlobStore)


def test_a_missing_blob_setting_stops_the_worker_with_the_names_to_set(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, blob_endpoint=None)
    with pytest.raises(ValueError, match="LONGHAUL_BLOB_ENDPOINT"):
        worker.build_parts(settings, CONFIG, FakeManager())  # type: ignore[arg-type]


def test_a_missing_database_url_stops_the_worker_with_the_name_to_set(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, database_url=None)
    with pytest.raises(ValueError, match="LONGHAUL_DATABASE_URL"):
        worker.build_parts(settings, CONFIG, FakeManager())  # type: ignore[arg-type]


def test_the_checkpoint_folder_comes_from_the_settings(tmp_path: Path) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    assert parts.store.root == tmp_path / "ckpt"


async def test_closing_the_parts_closes_the_database_pool(tmp_path: Path) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    await parts.close()
    await parts.close()  # a second close is harmless


# ---------- signals ----------
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_sigterm_and_sigint_start_a_graceful_stop(sig: signal.Signals) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    worker.install_signal_handlers(stop)
    try:
        # Raising a signal that nothing handles would end the test run, so check first.
        assert signal.getsignal(sig) not in (signal.SIG_DFL, signal.default_int_handler)
        os.kill(os.getpid(), sig)
        async with asyncio.timeout(5):
            await stop.wait()
    finally:
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(name)


def test_the_graceful_timeout_lets_a_running_test_command_finish() -> None:
    # A tool call runs for 10 minutes at most. The worker waits for the short ones and gives up
    # on a long one, which Temporal then retries on the next worker.
    assert worker.GRACEFUL_SHUTDOWN_S >= 30
    assert acts.TOOL_EXEC.start_to_close.total_seconds() > worker.GRACEFUL_SHUTDOWN_S


# ---------- GitHub (LH-305) ----------
def _workspace_of(parts: worker.WorkerParts) -> Any:
    (found,) = {
        fn.__self__ for fn in parts.activities if getattr(fn, "__name__", "") == "prepare_workspace"
    }
    return found


async def test_a_worker_with_a_github_token_can_prepare_github_repos(tmp_path: Path) -> None:
    parts = worker.build_parts(
        make_settings(tmp_path, github_token="github-token-for-tests"),
        CONFIG,
        FakeManager(),  # type: ignore[arg-type]
    )
    try:
        assert isinstance(_workspace_of(parts)._github, GitHubRemote)
    finally:
        await parts.close()
    assert parts.github is not None and parts.github._http.is_closed


def test_a_worker_without_a_github_token_prepares_local_folders_only(tmp_path: Path) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    assert _workspace_of(parts)._github is None
    assert parts.github is None


# ---------- one GitHub client (LH-305 and LH-307) ----------
async def test_the_poller_and_the_clone_share_one_github_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two clients would leave one of them out of WorkerParts.close(), and its connection pool
    # would stay open when the worker stops.
    built: list[GitHubClient] = []

    class Recording(GitHubClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(worker, "GitHubClient", Recording)
    parts = worker.build_parts(
        make_settings(tmp_path, github_token=TOKEN),
        CONFIG,
        FakeManager(),  # type: ignore[arg-type]
    )
    try:
        assert len(built) == 1
        assert parts.github is built[0]
        assert poller_of(parts)._github is built[0]
        assert _workspace_of(parts)._github._client is built[0]
    finally:
        await parts.close()
    assert all(client._http.is_closed for client in built)


# ---------- GitHub delivery (LH-306) ----------
async def test_a_worker_with_a_github_token_registers_the_github_activities(
    tmp_path: Path,
) -> None:
    parts = worker.build_parts(
        make_settings(tmp_path, github_token="github-token-for-tests"),
        CONFIG,
        FakeManager(),  # type: ignore[arg-type]
    )
    try:
        # Everything the workflow calls, for a task of an issue as well as for a local one.
        assert names_of(parts) == sorted(act.name for act in (*acts.ALL, *acts.GITHUB))
    finally:
        await parts.close()


def test_a_worker_without_a_github_token_registers_none_of_them(tmp_path: Path) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    assert not {act.name for act in acts.GITHUB} & set(names_of(parts))


async def test_the_github_writes_go_through_the_ledger_of_the_worker(tmp_path: Path) -> None:
    parts = worker.build_parts(
        make_settings(tmp_path, github_token="github-token-for-tests"),
        CONFIG,
        FakeManager(),  # type: ignore[arg-type]
    )
    try:
        (writes,) = {
            fn.__self__ for fn in parts.activities if getattr(fn, "__name__", "") == "post_comment"
        }
        (delivery,) = {
            fn.__self__ for fn in parts.activities if getattr(fn, "__name__", "") == "deliver"
        }
        assert isinstance(writes._ledger, SqlEffectLedger)
        assert delivery._ledger is writes._ledger
        assert delivery._author == "longhaul <longhaul@users.noreply.github.com>"
    finally:
        await parts.close()


async def test_the_commit_author_and_the_templates_come_from_the_settings(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    parts = worker.build_parts(
        make_settings(
            tmp_path,
            github_token="github-token-for-tests",
            git_author="Ada <ada@example.com>",
            templates_dir=templates,
        ),
        CONFIG,
        FakeManager(),  # type: ignore[arg-type]
    )
    try:
        (delivery,) = {
            fn.__self__ for fn in parts.activities if getattr(fn, "__name__", "") == "deliver"
        }
        assert delivery._author == "Ada <ada@example.com>"
        assert delivery._templates == templates
    finally:
        await parts.close()


def test_a_commit_author_git_would_misread_stops_the_worker_before_it_starts(
    tmp_path: Path,
) -> None:
    settings = make_settings(
        tmp_path, github_token="github-token-for-tests", git_author="no address here"
    )
    with pytest.raises(ValueError, match="LONGHAUL_GIT_AUTHOR"):
        worker.build_parts(settings, CONFIG, FakeManager())  # type: ignore[arg-type]


# ---------- the metrics endpoint (LH-704) ----------
class FakeParts:
    """What build_parts returns, for a main() that connects to nothing."""

    activities: list[Any] = []

    def __init__(self) -> None:
        self.closed = False
        self.checked = False
        self.check_error: Exception | None = None

    async def check_memory(self) -> None:
        self.checked = True
        if self.check_error is not None:
            raise self.check_error

    async def close(self) -> None:
        self.closed = True


def scrape(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=10) as reply:
        return str(reply.read().decode())


@pytest.fixture
def serving_endpoints(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Any]:
    """main() with its connections replaced. The servers it starts are collected for the test."""
    monkeypatch.chdir(tmp_path)  # a .env in the folder must not change the settings
    monkeypatch.setenv("LONGHAUL_METRICS_PORT", "0")
    monkeypatch.setenv("LONGHAUL_METRICS_HOST", "127.0.0.1")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("LONGHAUL_CONFIG", str(ROOT / "config" / "longhaul.example.yaml"))
    monkeypatch.setattr(worker, "DockerSandboxManager", FakeManager)
    monkeypatch.setattr(worker, "build_parts", lambda *args: FakeParts())
    monkeypatch.setattr(worker, "install_signal_handlers", lambda stop: None)
    servers: list[Any] = []
    real = metrics.serve

    def spy(*args: Any, **kwargs: Any) -> Any:
        servers.append(real(*args, **kwargs))
        return servers[-1]

    monkeypatch.setattr(worker.metrics, "serve", spy)
    return servers


async def test_the_worker_serves_its_metrics_while_it_runs_and_stops_them_after(
    monkeypatch: pytest.MonkeyPatch, serving_endpoints: list[Any]
) -> None:
    seen: list[str] = []

    async def connect(address: str, **kwargs: Any) -> object:
        return object()

    async def serve(client: Any, queue: str, activities: Any, stop: asyncio.Event) -> None:
        seen.append(scrape(serving_endpoints[0].port))

    monkeypatch.setattr(worker, "connect", connect)
    monkeypatch.setattr(worker, "serve", serve)

    await worker.main()

    assert "# TYPE longhaul_active_tasks gauge" in seen[0]
    with pytest.raises(urllib.error.URLError):
        scrape(serving_endpoints[0].port)


async def test_the_endpoint_stops_when_the_worker_fails(
    monkeypatch: pytest.MonkeyPatch, serving_endpoints: list[Any]
) -> None:
    async def connect(address: str, **kwargs: Any) -> object:
        raise ConnectionError("temporal is down")

    monkeypatch.setattr(worker, "connect", connect)

    with pytest.raises(ConnectionError):
        await worker.main()

    with pytest.raises(urllib.error.URLError):
        scrape(serving_endpoints[0].port)


async def test_a_taken_metrics_port_stops_the_worker_before_it_connects(
    monkeypatch: pytest.MonkeyPatch, serving_endpoints: list[Any]
) -> None:
    connected: list[str] = []

    async def connect(address: str, **kwargs: Any) -> object:
        connected.append(address)
        return object()

    monkeypatch.setattr(worker, "connect", connect)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        monkeypatch.setenv("LONGHAUL_METRICS_PORT", str(taken.getsockname()[1]))
        with pytest.raises(ValueError, match="LONGHAUL_METRICS_PORT"):
            await worker.main()
    assert connected == []


# ---------- the planner (LH-501) ----------
def activity_object(parts: worker.WorkerParts, name: str) -> Any:
    (owner,) = {fn.__self__ for fn in parts.activities if getattr(fn, "__name__", "") == name}
    return owner


def test_the_planner_gets_its_own_tools_on_every_worker(tmp_path: Path) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    llm = activity_object(parts, "llm_turn")

    assert [tool.name for tool in llm.tools_for(Role.PLANNER)] == [*planner.TOOLS, "submit_plan"]
    coder = {tool.name for tool in llm.tools_for(Role.CODER)}
    assert {"edit_file", "write_file", "finish"} <= coder
    assert "submit_plan" not in coder


def test_a_worker_stores_plans_with_the_recorder_of_the_rest(tmp_path: Path) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    store = activity_object(parts, "store_plan")
    assert store._records is activity_object(parts, "llm_turn")._recorder


async def test_a_worker_with_a_token_posts_plans_through_the_ledger_of_the_writes(
    tmp_path: Path,
) -> None:
    templates = tmp_path / "templates"
    parts = worker.build_parts(
        make_settings(tmp_path, github_token="github-token-for-tests", templates_dir=templates),
        CONFIG,
        FakeManager(),  # type: ignore[arg-type]
    )
    try:
        posting = activity_object(parts, "post_plan")
        writes = activity_object(parts, "post_comment")
        assert posting._ledger is writes._ledger
        assert posting._client is parts.github
        assert posting._templates == templates
    finally:
        await parts.close()


def test_a_worker_without_a_token_cannot_post_a_plan_but_still_stores_it(tmp_path: Path) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    names = names_of(parts)
    assert "store_plan" in names and "post_plan" not in names


# ---------- LH-308: the poller's ledger, and where GitHub is ----------
async def test_the_poller_posts_its_replies_through_the_ledger_of_the_other_writes(
    tmp_path: Path,
) -> None:
    parts = worker.build_parts(
        make_settings(tmp_path, github_token="github-token-for-tests"),
        CONFIG,
        FakeManager(),  # type: ignore[arg-type]
    )
    try:
        (writes,) = {
            fn.__self__ for fn in parts.activities if getattr(fn, "__name__", "") == "post_comment"
        }
        assert poller_of(parts)._ledger is writes._ledger
    finally:
        await parts.close()


async def test_a_worker_without_a_token_still_gives_the_poller_a_ledger(tmp_path: Path) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    try:
        assert isinstance(poller_of(parts)._ledger, SqlEffectLedger)
    finally:
        await parts.close()


async def test_github_is_github_dot_com_by_default(tmp_path: Path) -> None:
    parts = worker.build_parts(
        make_settings(tmp_path, github_token="github-token-for-tests"),
        CONFIG,
        FakeManager(),  # type: ignore[arg-type]
    )
    try:
        assert parts.github is not None
        assert str(parts.github._http.base_url).rstrip("/") == "https://api.github.com"
        assert _workspace_of(parts)._github.clone_url("acme/calc") == (
            "https://github.com/acme/calc.git"
        )
    finally:
        await parts.close()


async def test_the_api_and_the_clone_address_follow_the_settings(tmp_path: Path) -> None:
    parts = worker.build_parts(
        make_settings(
            tmp_path,
            github_token="github-token-for-tests",
            github_api_url="http://127.0.0.1:8099",
            github_web_url=str(tmp_path / "remotes"),
        ),
        CONFIG,
        FakeManager(),  # type: ignore[arg-type]
    )
    try:
        assert parts.github is not None
        assert str(parts.github._http.base_url).rstrip("/") == "http://127.0.0.1:8099"
        assert _workspace_of(parts)._github.clone_url("acme/calc") == (
            f"{tmp_path / 'remotes'}/acme/calc.git"
        )
    finally:
        await parts.close()


# ---------- memory (LH-604) ----------
@pytest.mark.phase6
async def test_a_memory_table_of_another_embedder_stops_the_worker_before_it_takes_a_port(
    monkeypatch: pytest.MonkeyPatch, serving_endpoints: list[Any]
) -> None:
    parts = FakeParts()
    parts.check_error = ValueError("memory.embedder names another embedder than the table's")
    monkeypatch.setattr(worker, "build_parts", lambda *args: parts)
    connected: list[str] = []

    async def connect(address: str, **kwargs: Any) -> object:
        connected.append(address)
        return object()

    monkeypatch.setattr(worker, "connect", connect)

    with pytest.raises(ValueError, match="another embedder"):
        await worker.main()

    # It stopped before the metrics endpoint and the connection, and still closed what it built.
    assert (parts.checked, parts.closed) == (True, True)
    assert serving_endpoints == []
    assert connected == []


@pytest.mark.phase6
async def test_a_healthy_memory_table_lets_the_worker_start(
    monkeypatch: pytest.MonkeyPatch, serving_endpoints: list[Any]
) -> None:
    parts = FakeParts()
    monkeypatch.setattr(worker, "build_parts", lambda *args: parts)

    async def connect(address: str, **kwargs: Any) -> object:
        return object()

    async def serve(client: Any, queue: str, activities: Any, stop: asyncio.Event) -> None:
        return None

    monkeypatch.setattr(worker, "connect", connect)
    monkeypatch.setattr(worker, "serve", serve)

    await worker.main()

    assert parts.checked is True
    assert len(serving_endpoints) == 1


def loader_of(parts: worker.WorkerParts) -> Any:
    (found,) = [
        fn.__self__ for fn in parts.activities if getattr(fn, "__name__", "") == "load_memory"
    ]
    return found


def with_memory(**fields: Any) -> AppConfig:
    values = {"enabled": True, "embedder": "hash", **fields}
    return CONFIG.model_copy(update={"memory": Memory(**values)})


def pretend_fastembed_is(installed: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the memory extra look installed or missing, whatever the environment has."""
    if installed:
        monkeypatch.setitem(sys.modules, "fastembed", types.ModuleType("fastembed"))
    else:
        monkeypatch.setitem(sys.modules, "fastembed", None)  # an import finds nothing


@pytest.mark.phase6
def test_a_worker_with_memory_shares_one_store_between_the_workspace_and_the_prompt(
    tmp_path: Path,
) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]

    assert isinstance(parts.memory, PgMemoryStore)
    assert isinstance(parts.memory._embedder, HashEmbedder)
    workspace = _workspace_of(parts)
    # The commands and the map are kept in it, and the map needs the blob store as well.
    assert workspace._memory is parts.memory
    assert workspace._blobs is parts.blobs
    assert workspace.repo_maps is not None
    # load_memory reads the same store and writes the block to the same blob store.
    loader = loader_of(parts)
    assert loader._memory is parts.memory
    assert loader._blobs is parts.blobs


@pytest.mark.phase6
def test_a_worker_with_memory_off_builds_no_store_and_keeps_the_activity(tmp_path: Path) -> None:
    parts = worker.build_parts(
        make_settings(tmp_path),
        with_memory(enabled=False),
        FakeManager(),  # type: ignore[arg-type]
    )

    assert parts.memory is None
    workspace = _workspace_of(parts)
    assert workspace._memory is None
    assert workspace.repo_maps is None
    # The workflow names load_memory, so the worker keeps answering it: with an empty block.
    assert names_of(parts) == sorted(act.name for act in acts.ALL)
    assert loader_of(parts)._memory is None


@pytest.mark.phase6
def test_the_local_embedder_without_the_memory_extra_stops_the_worker_at_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pretend_fastembed_is(False, monkeypatch)

    with pytest.raises(ValueError, match=r"uv sync --extra memory"):
        worker.build_parts(
            make_settings(tmp_path),
            with_memory(embedder="local"),
            FakeManager(),  # type: ignore[arg-type]
        )


@pytest.mark.phase6
def test_the_local_embedder_with_the_extra_loads_no_model_until_it_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pretend_fastembed_is(True, monkeypatch)

    parts = worker.build_parts(
        make_settings(tmp_path),
        with_memory(embedder="local"),
        FakeManager(),  # type: ignore[arg-type]
    )

    assert parts.memory is not None
    assert isinstance(parts.memory._embedder, LocalEmbedder)
    assert parts.memory._embedder._model is None


@pytest.mark.phase6
def test_memory_off_needs_no_extra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pretend_fastembed_is(False, monkeypatch)

    parts = worker.build_parts(
        make_settings(tmp_path),
        with_memory(enabled=False, embedder="local"),
        FakeManager(),  # type: ignore[arg-type]
    )

    assert parts.memory is None


@pytest.mark.phase6
async def test_the_start_asks_the_store_whether_its_embedder_wrote_the_table(
    tmp_path: Path,
) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    asked: list[str] = []

    class Store:
        async def check_embedder(self) -> None:
            asked.append("checked")

    parts.memory = Store()  # type: ignore[assignment]
    await parts.check_memory()
    assert asked == ["checked"]

    parts.memory = None
    await parts.check_memory()  # nothing to check
    assert asked == ["checked"]
    await parts.close()


@pytest.mark.phase6
async def test_a_memory_database_that_cannot_be_reached_stops_the_worker_with_a_message(
    tmp_path: Path,
) -> None:
    # make_settings points the database at a port nobody listens on.
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    try:
        with pytest.raises(ValueError, match="memory"):
            await parts.check_memory()
    finally:
        await parts.close()


# ---------- the coder with a plan (LH-503) ----------
@pytest.mark.phase5
def test_a_coder_with_a_plan_gets_the_step_tools_and_one_without_keeps_finish(
    tmp_path: Path,
) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    llm = activity_object(parts, "llm_turn")

    plain = [tool.name for tool in llm.tools_for(Role.CODER)]
    planned = [tool.name for tool in llm.tools_for(Role.CODER, planned=True)]

    assert "finish" in plain
    assert not set(coder.CONTROL_TOOLS) & set(plain)
    assert planned == [*(name for name in plain if name != "finish"), *coder.CONTROL_TOOLS]
    # The flag is for the coder only: the planner has one list.
    assert llm.tools_for(Role.PLANNER, planned=True) == llm.tools_for(Role.PLANNER)


@pytest.mark.phase5
def test_a_worker_records_steps_with_the_recorder_of_the_rest(tmp_path: Path) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    recording = activity_object(parts, "record_step")
    assert recording._records is activity_object(parts, "llm_turn")._recorder
