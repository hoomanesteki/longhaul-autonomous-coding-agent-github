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
from longhaul.contracts import Message, TextBlock, ToolSpec
from longhaul.cost.ledger import SqlLedger
from longhaul.cost.pricing import PricingError
from longhaul.durable import acts, worker
from longhaul.providers.replay import ReplayProvider
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
