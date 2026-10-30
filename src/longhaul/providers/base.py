"""Provider protocol re-exports and the factory chosen by LONGHAUL_PROVIDER."""

from pathlib import Path

from longhaul.contracts import LLMProvider, ProviderError, ProviderErrorKind
from longhaul.settings import EnvSettings

__all__ = ["LLMProvider", "ProviderError", "ProviderErrorKind", "get_provider"]

_PENDING = {
    "anthropic": "LH-104",
    "openai": "LH-105",
    "replay": "LH-209",
}


def get_provider(settings: EnvSettings, *, script_path: Path | None = None) -> LLMProvider:
    if settings.provider == "scripted":
        if script_path is None:
            raise ValueError("the scripted provider needs script_path")
        from longhaul.providers.scripted import ScriptedProvider

        return ScriptedProvider(script_path)
    story = _PENDING[settings.provider]
    raise NotImplementedError(f"provider {settings.provider!r} is not implemented yet ({story})")
