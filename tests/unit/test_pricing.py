"""What a model call costs (LH-701): the pricing table, the cost formula and the startup check.

The hand-computed costs use the rates that are checked in at config/pricing.yaml, which
tests/unit/test_settings.py pins. Each one is worked out in a comment, so that a wrong rate or a
wrong formula shows up as a number that anyone can check with a calculator.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from longhaul.contracts import Usage
from longhaul.cost.pricing import (
    FREE_PROVIDERS,
    Pricing,
    PricingError,
    check_prices,
    load_pricing,
)
from longhaul.settings import AppConfig, EnvSettings, load_config

pytestmark = [pytest.mark.unit, pytest.mark.phase7]

ROOT = Path(__file__).resolve().parents[2]
PRICING = ROOT / "config" / "pricing.yaml"
EXAMPLE = ROOT / "config" / "longhaul.example.yaml"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
ROLES = ("planner", "coder", "verifier", "compactor")

# Dollars per million tokens. Sonnet 5.5: input 2.00, output 10.00, cache write 2.50, cache read
# 0.20. Haiku 5.5: 0.10, 0.50, 0.125 and 0.01 up to a 100,000 token prompt, then 0.50, 2.50,
# 0.625 and 0.05. The first two usages are turns of tests/fixtures/usage_samples.json.
SAMPLES = [
    # The first turn of a task writes its prompt to the cache.
    # 336 * 2.00 + 482 * 10.00 + 6504 * 2.50 = 672 + 4820 + 16260 = 21752, so 0.021752
    pytest.param(
        SONNET,
        Usage(input_tokens=336, output_tokens=482, cache_write_tokens=6504),
        Decimal("0.021752"),
        id="sonnet writes the cache",
    ),
    # A later turn reads the same prefix back from the cache.
    # 5457 * 2.00 + 1388 * 10.00 + 6504 * 0.20 = 10914 + 13880 + 1300.8 = 26094.8, so 0.0260948
    pytest.param(
        SONNET,
        Usage(input_tokens=5457, output_tokens=1388, cache_read_tokens=6504),
        Decimal("0.0260948"),
        id="sonnet reads the cache",
    ),
    # A compaction call on Haiku. The prompt is 57,000 tokens, under the long prompt line.
    # 12000 * 0.10 + 2000 * 0.50 + 5000 * 0.125 + 40000 * 0.01 = 1200 + 1000 + 625 + 400 = 3225,
    # so 0.003225
    pytest.param(
        HAIKU,
        Usage(
            input_tokens=12_000,
            output_tokens=2_000,
            cache_read_tokens=40_000,
            cache_write_tokens=5_000,
        ),
        Decimal("0.003225"),
        id="haiku compaction",
    ),
]


def prices() -> Pricing:
    return Pricing.load(PRICING)


def with_models(config: AppConfig, **ids: str) -> AppConfig:
    """The config with some role models replaced."""
    return config.model_copy(update={"models": config.models.model_copy(update=ids)})


# ---------- the cost formula ----------
@pytest.mark.parametrize(("model", "usage", "dollars"), SAMPLES)
def test_cost_math(model: str, usage: Usage, dollars: Decimal) -> None:
    assert prices().cost(usage, model) == dollars


def test_the_cost_keeps_every_digit() -> None:
    # One Haiku cache write token is 0.125 / 1,000,000 dollars. Rounding is the ledger's job.
    assert prices().cost(Usage(cache_write_tokens=1), HAIKU) == Decimal("0.000000125")


def test_a_call_with_no_tokens_costs_nothing() -> None:
    assert prices().cost(Usage(), SONNET) == 0


def test_a_long_prompt_is_billed_at_the_long_prompt_rates() -> None:
    # The prompt is 20,000 + 80,000 + 10,000 = 110,000 tokens, over the 100,000 line. All four
    # rates change, and the output rate with them.
    # 20000 * 0.50 + 3000 * 2.50 + 10000 * 0.625 + 80000 * 0.05 = 10000 + 7500 + 6250 + 4000
    # = 27750, so 0.02775
    usage = Usage(
        input_tokens=20_000,
        output_tokens=3_000,
        cache_read_tokens=80_000,
        cache_write_tokens=10_000,
    )
    assert prices().cost(usage, HAIKU) == Decimal("0.02775")


def test_the_long_prompt_rates_start_above_100000_tokens() -> None:
    at_the_line = Usage(
        input_tokens=60_000,
        output_tokens=1_000,
        cache_read_tokens=30_000,
        cache_write_tokens=10_000,
    )
    one_over = at_the_line.model_copy(update={"input_tokens": 60_001})
    # 100,000 tokens: 60000 * 0.10 + 1000 * 0.50 + 10000 * 0.125 + 30000 * 0.01
    # = 6000 + 500 + 1250 + 300 = 8050, so 0.00805
    assert prices().cost(at_the_line, HAIKU) == Decimal("0.00805")
    # 100,001 tokens: 60001 * 0.50 + 1000 * 2.50 + 10000 * 0.625 + 30000 * 0.05
    # = 30000.5 + 2500 + 6250 + 1500 = 40250.5, so 0.0402505
    assert prices().cost(one_over, HAIKU) == Decimal("0.0402505")


def test_cached_tokens_count_toward_the_prompt_size() -> None:
    # Only 1,000 tokens are uncached, but the prompt is 101,000 tokens long.
    # 1000 * 0.50 + 100000 * 0.05 = 500 + 5000 = 5500, so 0.0055
    usage = Usage(input_tokens=1_000, cache_read_tokens=100_000)
    assert prices().cost(usage, HAIKU) == Decimal("0.0055")


def test_a_model_without_a_long_prompt_block_has_one_price_at_any_size() -> None:
    # 500000 * 2.00 + 1000 * 10.00 = 1000000 + 10000 = 1010000, so 1.01
    usage = Usage(input_tokens=500_000, output_tokens=1_000)
    assert prices().cost(usage, SONNET) == Decimal("1.01")


def test_a_model_without_a_price_is_an_error() -> None:
    with pytest.raises(PricingError, match="claude-opus-9"):
        prices().cost(Usage(input_tokens=1), "claude-opus-9")


def test_negative_token_counts_are_refused() -> None:
    with pytest.raises(PricingError, match="negative"):
        prices().cost(Usage(input_tokens=-1), SONNET)


def test_the_price_of_a_model_can_be_read() -> None:
    price = prices().price(HAIKU)
    assert price.input_per_mtok == Decimal("0.10")
    assert price.long_prompt is not None
    assert (price.long_prompt.above_tokens, price.long_prompt.output_per_mtok) == (
        100_000,
        Decimal("2.5"),
    )
    assert prices().price(SONNET).long_prompt is None


# ---------- scripted and replay runs ----------
def test_a_free_pricing_costs_nothing_for_any_model() -> None:
    free = Pricing.free()
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=5)
    assert free.cost(usage, SONNET) == 0
    assert free.cost(usage, "a model nobody priced") == 0
    assert free.has_price("a model nobody priced")


def test_the_free_providers_are_scripted_and_replay() -> None:
    known = set(get_args(EnvSettings.model_fields["provider"].annotation))
    assert sorted(FREE_PROVIDERS) == ["replay", "scripted"]
    assert known >= FREE_PROVIDERS


# ---------- the pricing file ----------
def test_the_checked_in_file_prices_both_models() -> None:
    loaded = prices()
    assert loaded.has_price(SONNET) and loaded.has_price(HAIKU)
    assert not loaded.has_price("gpt-5")


def write_prices(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "pricing.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


GOOD = {
    "input_per_mtok": 1,
    "output_per_mtok": 5,
    "cache_write_per_mtok": 1.25,
    "cache_read_per_mtok": 0.1,
}


def test_whole_numbers_and_decimals_both_load(tmp_path: Path) -> None:
    loaded = Pricing.load(write_prices(tmp_path, {"models": {"m": GOOD}}))
    # 1,000,000 tokens of each kind: 1 + 5 + 1.25 + 0.1 dollars.
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_write_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )
    assert loaded.cost(usage, "m") == Decimal("7.35")


def test_a_rate_of_zero_is_allowed(tmp_path: Path) -> None:
    # A model on a local server can be priced at nothing, on purpose.
    free_rates = dict.fromkeys(GOOD, 0)
    loaded = Pricing.load(write_prices(tmp_path, {"models": {"local": free_rates}}))
    assert loaded.has_price("local")
    assert loaded.cost(Usage(input_tokens=10, output_tokens=10), "local") == 0


def without(rates: dict[str, Any], name: str) -> dict[str, Any]:
    return {key: value for key, value in rates.items() if key != name}


BAD_FILES = [
    pytest.param(
        {"models": {"m": without(GOOD, "input_per_mtok")}}, "input_per_mtok", id="missing"
    ),
    pytest.param(
        {"models": {"m": {**GOOD, "output_per_mtok": None}}}, "output_per_mtok", id="null"
    ),
    pytest.param(
        {"models": {"m": {**GOOD, "cache_read_per_mtok": -1}}}, "cache_read_per_mtok", id="negative"
    ),
    pytest.param(
        {"models": {"m": {**GOOD, "cache_write_per_mtok": "cheap"}}},
        "cache_write_per_mtok",
        id="text",
    ),
    pytest.param(
        {"models": {"m": {**GOOD, "input_per_mtok": float("inf")}}}, "input_per_mtok", id="infinite"
    ),
    pytest.param({"models": {"m": {**GOOD, "input_per_mtk": 1}}}, "input_per_mtk", id="misspelled"),
    pytest.param(
        {
            "models": {
                "m": {**GOOD, "long_prompt": without(GOOD, "output_per_mtok") | {"above_tokens": 9}}
            }
        },
        "output_per_mtok",
        id="long prompt without a rate",
    ),
    pytest.param(
        {"models": {"m": {**GOOD, "long_prompt": GOOD}}},
        "above_tokens",
        id="long prompt without a line",
    ),
    pytest.param(
        {"models": {"m": {**GOOD, "long_prompt": {**GOOD, "above_tokens": 0}}}},
        "above_tokens",
        id="long prompt line at zero",
    ),
    pytest.param({"models": ["m"]}, "models", id="models is a list"),
    pytest.param({"prices": {"m": GOOD}}, "models", id="no models"),
    pytest.param(
        {"models": {"m": GOOD}, "currency": "usd"}, "currency", id="unknown top level key"
    ),
    pytest.param(["m"], "", id="not a mapping"),
]


@pytest.mark.parametrize(("data", "mentions"), BAD_FILES)
def test_a_file_that_cannot_be_used_is_refused_with_its_path(
    tmp_path: Path, data: Any, mentions: str
) -> None:
    path = write_prices(tmp_path, data)
    with pytest.raises(PricingError) as caught:
        Pricing.load(path)
    assert str(path) in str(caught.value)
    assert mentions in str(caught.value)


def test_a_file_that_is_not_yaml_is_refused_with_its_path(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text("models: [unclosed", encoding="utf-8")
    with pytest.raises(PricingError, match="not valid YAML") as caught:
        Pricing.load(path)
    assert str(path) in str(caught.value)


def test_a_missing_file_is_refused_with_its_path(tmp_path: Path) -> None:
    path = tmp_path / "absent.yaml"
    with pytest.raises(PricingError, match="cannot be read") as caught:
        Pricing.load(path)
    assert str(path) in str(caught.value)


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(PricingError):
        Pricing.load(path)


# ---------- the startup check ----------
def test_missing_price_fails_startup() -> None:
    config = with_models(load_config(EXAMPLE), coder="claude-opus-9")
    with pytest.raises(PricingError) as caught:
        check_prices(config, prices())
    message = str(caught.value)
    assert "claude-opus-9" in message
    assert str(config.pricing_file) in message


def test_the_example_config_is_fully_priced() -> None:
    check_prices(load_config(EXAMPLE), prices())


@pytest.mark.parametrize("role", ROLES)
def test_every_model_the_config_names_needs_a_price(role: str) -> None:
    config = with_models(load_config(EXAMPLE), **{role: "unpriced-model"})
    with pytest.raises(PricingError, match="unpriced-model"):
        check_prices(config, prices())


def test_every_missing_model_is_named_once() -> None:
    config = with_models(
        load_config(EXAMPLE), planner="zeta", coder="alpha", verifier="alpha", compactor=HAIKU
    )
    with pytest.raises(PricingError) as caught:
        check_prices(config, prices())
    message = str(caught.value)
    assert message.count("alpha") == 1
    assert message.index("alpha") < message.index("zeta")


def test_a_free_pricing_passes_for_any_model() -> None:
    config = with_models(load_config(EXAMPLE), coder="anything", compactor="goes")
    check_prices(config, Pricing.free())


@pytest.mark.parametrize("provider", sorted(FREE_PROVIDERS))
def test_scripted_and_replay_runs_cost_nothing_and_need_no_file(provider: str) -> None:
    config = load_config(EXAMPLE).model_copy(update={"pricing_file": Path("/nonexistent/p.yaml")})
    pricing = load_pricing(provider, with_models(config, coder="any-model-at-all"))
    assert pricing.cost(Usage(input_tokens=1_000_000, output_tokens=1_000_000), SONNET) == 0


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_a_paid_provider_reads_the_file_and_checks_every_model(provider: str) -> None:
    config = load_config(EXAMPLE).model_copy(update={"pricing_file": PRICING})
    assert load_pricing(provider, config).has_price(SONNET)

    with pytest.raises(PricingError, match="claude-haiku-9"):
        load_pricing(provider, with_models(config, compactor="claude-haiku-9"))
