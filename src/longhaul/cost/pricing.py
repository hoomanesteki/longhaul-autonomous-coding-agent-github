"""What a model call costs, from the pricing file.

`config/pricing.yaml` lists, for each model, the dollars per million tokens of input, output,
cache writes and cache reads. The numbers live in that file and nowhere in code (plan section
3.8), so a price change is an edit to one file and the date in its comment.

`Pricing.cost` is `(in*pi + out*po + cw*pcw + cr*pcr) / 1e6`. The four counts do not overlap
(ADR-0003, section 5), so they are added with no subtraction. The arithmetic is Decimal because
a price such as 0.125 has no exact float, and a ledger has to add up.

A model can carry a `long_prompt` block: a size in tokens and a second set of rates. A call whose
prompt is above that size is billed at the second set, output tokens included. The prompt of a
call is its input, cache read and cache write tokens added together, because cached tokens are
part of the prompt all the same.

Scripted and replay runs cost nothing and may name any model, so they get `Pricing.free()`. For
every other provider the worker calls `load_pricing` when it starts. It reads the file and stops
when a model in the config has no price. Without that check each call to the model would be
billed by the provider and then fail to be priced, which is money that nobody recorded.
"""

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from longhaul.contracts import Usage
from longhaul.settings import AppConfig

_PER_MILLION = Decimal(1_000_000)

# The providers whose calls cost nothing: the scripted provider plays a file and the replay
# provider plays a recording. A unit test keeps the names equal to those in the settings.
FREE_PROVIDERS = frozenset({"scripted", "replay"})


class PricingError(ValueError):
    """The pricing file cannot be used, or a model has no price."""


class _Strict(BaseModel):
    # A misspelled rate would otherwise be dropped, and the model would be billed without it.
    model_config = ConfigDict(extra="forbid", frozen=True)


class Rates(_Strict):
    """What a million tokens cost, in dollars."""

    input_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)
    cache_write_per_mtok: Decimal = Field(ge=0)
    cache_read_per_mtok: Decimal = Field(ge=0)

    def cost(self, usage: Usage) -> Decimal:
        total = (
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
            + usage.cache_write_tokens * self.cache_write_per_mtok
            + usage.cache_read_tokens * self.cache_read_per_mtok
        )
        return total / _PER_MILLION


class LongPromptRates(Rates):
    """The rates of a call whose prompt is longer than `above_tokens` tokens."""

    above_tokens: int = Field(gt=0)


class ModelPrice(Rates):
    """The rates of one model, and the rates for a long prompt when it has them."""

    long_prompt: LongPromptRates | None = None

    def rates_for(self, usage: Usage) -> Rates:
        prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        if self.long_prompt is not None and prompt > self.long_prompt.above_tokens:
            return self.long_prompt
        return self


_NO_COST = ModelPrice(
    input_per_mtok=Decimal(0),
    output_per_mtok=Decimal(0),
    cache_write_per_mtok=Decimal(0),
    cache_read_per_mtok=Decimal(0),
)


class _PricingFile(_Strict):
    models: dict[str, ModelPrice]


class Pricing:
    """The prices of the models a run may call."""

    def __init__(self, models: Mapping[str, ModelPrice], *, free: bool = False) -> None:
        self._models = dict(models)
        self._free = free

    @classmethod
    def free(cls) -> Self:
        """Prices for a run whose calls cost nothing, whatever model it names."""
        return cls({}, free=True)

    @classmethod
    def load(cls, path: Path | str) -> Self:
        """Read the pricing file. A file that cannot be used raises a PricingError naming it."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise PricingError(f"{path} cannot be read: {exc}") from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise PricingError(f"{path} is not valid YAML: {exc}") from exc
        try:
            parsed = _PricingFile.model_validate(data)
        except ValidationError as exc:
            raise PricingError(f"{path}: {_problems(exc)}") from exc
        return cls(parsed.models)

    def has_price(self, model: str) -> bool:
        return self._free or model in self._models

    def price(self, model: str) -> ModelPrice:
        if self._free:
            return _NO_COST
        try:
            return self._models[model]
        except KeyError:
            raise PricingError(f"no price for model {model!r}") from None

    def cost(self, usage: Usage, model: str) -> Decimal:
        """The dollars one call cost, with every digit. Rounding is the ledger's job."""
        counts = (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
        )
        if min(counts) < 0:
            raise PricingError(f"a call cannot use a negative number of tokens: {usage}")
        return self.price(model).rates_for(usage).cost(usage)


def _problems(exc: ValidationError) -> str:
    """Each problem of a file with the key it is under, so the line to fix is easy to find."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'the file'}: {error['msg']}"
        for error in exc.errors()
    )


def check_prices(config: AppConfig, pricing: Pricing) -> None:
    """Stop when a model that the config names has no price.

    The worker calls this before it takes a task. A model without a price cannot be costed, and
    the fix is a line in the pricing file, which is easier to make before work has started.
    """
    configured = set(config.models.model_dump().values())
    missing = sorted(model for model in configured if not pricing.has_price(model))
    if missing:
        raise PricingError(
            f"no price for {', '.join(missing)} in {config.pricing_file}; add each model "
            "to that file, or change the model in the config"
        )


def load_pricing(provider: str, config: AppConfig) -> Pricing:
    """The prices a worker with this provider runs on, checked against the config.

    A provider that costs nothing needs no file, so a scripted run works with any model id.
    Every other provider reads `config.pricing_file` and must have a price for every model in
    the config.
    """
    if provider in FREE_PROVIDERS:
        return Pricing.free()
    pricing = Pricing.load(config.pricing_file)
    check_prices(config, pricing)
    return pricing
