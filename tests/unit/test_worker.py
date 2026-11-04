"""What the worker process is made of (LH-208).

Nothing here starts Temporal or Docker. `build_parts` is given a fake sandbox manager, because
building the real one connects to the Docker daemon, and the database and blob clients it makes
connect only when they are first used. tests/integration/test_worker.py runs `serve` against a
Temporal test server, and tests/e2e/test_durable_run.py starts the real process.
"""

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity

from longhaul.blobs.s3 import S3BlobStore
from longhaul.cost.ledger import SqlLedger
from longhaul.cost.pricing import PricingError
from longhaul.durable import acts, worker
from longhaul.github.remote import GitHubRemote
from longhaul.github.side_effects import SqlLedger as SqlEffectLedger
from longhaul.providers.scripted import ScriptedProvider
from longhaul.settings import EnvSettings, load_config

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_config(ROOT / "config" / "longhaul.example.yaml")
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


def test_a_real_provider_without_its_key_stops_the_worker_before_it_starts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        worker.build_provider(make_settings(tmp_path, provider="anthropic"))


# ---------- the activities ----------
def test_the_worker_registers_one_activity_for_every_name_the_workflow_calls(
    tmp_path: Path,
) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    assert names_of(parts) == sorted(act.name for act in acts.ALL)


def test_model_calls_go_on_the_cost_ledger(tmp_path: Path) -> None:
    parts = worker.build_parts(make_settings(tmp_path), CONFIG, FakeManager())  # type: ignore[arg-type]
    (llm,) = [fn.__self__ for fn in parts.activities if getattr(fn, "__name__", "") == "llm_turn"]
    assert isinstance(llm._ledger, SqlLedger)


def test_a_model_without_a_price_stops_the_worker_before_it_starts(tmp_path: Path) -> None:
    config = CONFIG.model_copy(
        update={"models": CONFIG.models.model_copy(update={"coder": "claude-opus-9"})}
    )
    settings = make_settings(tmp_path, provider="anthropic", ANTHROPIC_API_KEY="test-key")
    with pytest.raises(PricingError, match="claude-opus-9"):
        worker.build_parts(settings, config, FakeManager())  # type: ignore[arg-type]


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
