"""The three configs of the evaluation on the real stack (LH-903).

Run `make up` and `make sandbox-image` first, as for test_eval_runner.py, whose stack this file
reuses: a worker in this process on a task queue of its own, a scratch database that is migrated
and dropped, and a clean-up that removes only what the test made.

`full` and `naive` run on the durable stack with the scripts of tests/fixtures/scripts/eval. The
naive run differs in one thing the worker can see, the context mode, which travels with the task
in the workflow memo. `single_shot` has no workflow: the runner builds the repo map in a sandbox,
asks the scripted provider once (tests/fixtures/scripts/eval_single_shot), applies the diff and
grades the patch. Every config writes its result files with the hash of its config file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text
from tests.integration import test_eval_runner as eval_stack
from tests.integration.test_eval_runner import EvalStack

from eval import config_files, results, run
from longhaul.context.modes import ContextMode
from longhaul.db.session import make_engine
from longhaul.durable.client import connect, get_task
from longhaul.settings import config_path, load_config

pytestmark = [pytest.mark.integration, pytest.mark.phase9]

# The stack of test_eval_runner.py. Pytest finds a fixture by the name it has in the module.
database = eval_stack.database
stack = eval_stack.stack

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "tests" / "fixtures" / "scripts"
CONFIGS = ROOT / "eval" / "configs"
# The scripts that play each config: the loop is played by the same scripts for full and naive.
SCRIPT_OF = {
    "full": SCRIPTS / "eval",
    "naive": SCRIPTS / "eval",
    "single_shot": SCRIPTS / "eval_single_shot",
}


def run_options(stack: EvalStack, config: str, *extra: str) -> run.RunOptions:
    """The options of one config's run, named after the config so three runs never collide."""
    return run.parse_args(
        [
            "--set",
            "custom",
            "--config",
            config,
            "--run-id",
            f"{stack.run_id}-{config}",
            "--results-dir",
            str(stack.results_dir),
            "--script",
            str(SCRIPT_OF[config]),
            "--limit",
            "2",
            "--concurrency",
            "2",
            "--run-set",
            f"set-{stack.run_id}",
            *extra,
        ]
    )


def folder_of(stack: EvalStack, config: str) -> Path:
    return stack.results_dir / f"{stack.run_id}-{config}"


async def ledger_rows(stack: EvalStack, workflow_id: str) -> list[tuple[int, int, int, int]]:
    """(turn_index, attempt, input_tokens, output_tokens) of the ledger rows of one task."""
    url = stack.settings.database_url
    assert url is not None
    engine = make_engine(url.get_secret_value())
    try:
        async with engine.connect() as conn:
            found = await conn.execute(
                text(
                    "SELECT turn_index, attempt, input_tokens, output_tokens FROM cost_ledger "
                    "WHERE task_id = :id ORDER BY id"
                ),
                {"id": workflow_id},
            )
            return [(int(a), int(b), int(c), int(d)) for a, b, c, d in found.all()]
    finally:
        await engine.dispose()


async def test_three_configs_dry_run(stack: EvalStack) -> None:
    """Each config runs the first two custom tasks with scripted answers, and fixes both."""
    for config in ("full", "naive", "single_shot"):
        options = run_options(stack, config)
        assert options.provider == "scripted"
        summary = await run.run_eval(options, stack.settings)

        found = results.read_results(folder_of(stack, config))
        assert [r.task_id for r in found] == ["calc-01", "calc-02"], config
        assert (summary.attempted, summary.resolved) == (2, 2), config
        assert summary.config == config
        for result in found:
            assert result.config == config
            assert (result.provider, result.scripted) == ("scripted", True)
            assert result.outcome == "pr_opened", (config, result.outcome_summary)
            assert result.resolved is True, (config, result.grading)
            assert result.cost_usd == 0.0
            assert result.tokens.input > 0
            assert result.tokens.output > 0
            assert result.model == load_config(config_path(stack.settings)).models.coder
            assert (folder_of(stack, config) / f"{result.task_id}.patch").read_text().strip()

    # The loops took five turns each. The baseline asked the model once.
    for config, turns in (("full", 5), ("naive", 5), ("single_shot", 1)):
        assert {r.turns for r in results.read_results(folder_of(stack, config))} == {turns}

    # What each config switched, as the result files say it.
    by_config = {
        config: results.read_results(folder_of(stack, config))[0]
        for config in ("full", "naive", "single_shot")
    }
    assert (by_config["full"].context_mode, by_config["full"].memory) == ("full", "on")
    assert (by_config["naive"].context_mode, by_config["naive"].memory) == ("sliding", "off")
    assert (by_config["single_shot"].context_mode, by_config["single_shot"].memory) == (None, "off")


async def test_the_naive_run_reaches_the_workflow_as_the_sliding_mode(stack: EvalStack) -> None:
    """The config is not only a label in the result: the task carries the mode to the worker."""
    await run.run_eval(run_options(stack, "naive", "--limit", "1"), stack.settings)

    client = await connect(
        stack.settings.temporal_address, namespace=stack.settings.temporal_namespace
    )
    described = await get_task(client, f"lh-eval-{stack.run_id}-naive-calc-01").describe()
    assert await described.memo_value("context_mode", type_hint=ContextMode) is ContextMode.SLIDING


async def test_the_single_shot_call_is_on_the_ledger(stack: EvalStack) -> None:
    """Invariant I6 holds for the baseline: its one model call has a row under the task id."""
    await run.run_eval(run_options(stack, "single_shot", "--limit", "1"), stack.settings)

    rows = await ledger_rows(stack, f"lh-eval-{stack.run_id}-single_shot-calc-01")
    assert len(rows) == 1
    turn_index, attempt, input_tokens, output_tokens = rows[0]
    assert (turn_index, attempt) == (0, 1)
    assert input_tokens > 0
    assert output_tokens > 0


async def test_config_hash_recorded(stack: EvalStack) -> None:
    """Every result file and the summary carry the sha256 of the config file, and the seed."""
    for config in ("full", "naive", "single_shot"):
        await run.run_eval(
            run_options(stack, config, "--limit", "1", "--seed", "7"), stack.settings
        )

        expected = hashlib.sha256((CONFIGS / f"{config}.yaml").read_bytes()).hexdigest()
        assert config_files.load(config).sha256 == expected
        folder = folder_of(stack, config)

        # The file as it is on disk, not only as the model reads it back.
        raw = json.loads((folder / "calc-01.json").read_text(encoding="utf-8"))
        assert raw["config_hash"] == expected
        assert raw["seed"] == 7
        assert raw["run_set"] == f"set-{stack.run_id}"
        assert raw["allow_mixed_models"] is False
        on_disk = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        assert on_disk["config_hash"] == expected
        assert on_disk["seed"] == 7

    # Three configs, three definitions: no two runs share a hash.
    hashes = {
        results.read_results(folder_of(stack, config))[0].config_hash
        for config in ("full", "naive", "single_shot")
    }
    assert len(hashes) == 3


async def test_mixed_models_in_a_run_set_are_refused_before_a_task_starts(
    stack: EvalStack, tmp_path: Path
) -> None:
    """A second config of a run set must use the model of the first, or say it may not."""
    earlier = run_options(stack, "full", "--limit", "1")
    await run.run_eval(earlier, stack.settings)

    # The worker config that the runner reads names another coder model.
    data = yaml.safe_load(config_path(stack.settings).read_text(encoding="utf-8"))
    data["models"]["coder"] = "claude-haiku-5-5"
    other_path = tmp_path / "other.yaml"
    other_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    mixed = stack.settings.model_copy(update={"config": other_path})

    refused = run_options(stack, "naive", "--limit", "1")
    with pytest.raises(ValueError, match="--allow-mixed-models"):
        await run.run_eval(refused, mixed)
    # Nothing was claimed: the run id can still be used.
    assert not folder_of(stack, "naive").exists()

    allowed = run_options(stack, "naive", "--limit", "1", "--allow-mixed-models")
    summary = await run.run_eval(allowed, mixed)
    assert summary.model == "claude-haiku-5-5"
    assert summary.resolved == 1
    # The files say that the mix was meant, so a later comparison does not take it for a mistake.
    assert summary.allow_mixed_models is True
    (naive,) = results.read_results(folder_of(stack, "naive"))
    assert naive.allow_mixed_models is True
