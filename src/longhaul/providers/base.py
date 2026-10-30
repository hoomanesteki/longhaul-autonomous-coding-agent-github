"""Provider protocol re-exports and the factory chosen by LONGHAUL_PROVIDER."""

from pathlib import Path

from longhaul.contracts import LLMProvider, ProviderError, ProviderErrorKind
from longhaul.settings import EnvSettings

__all__ = ["LLMProvider", "ProviderError", "ProviderErrorKind", "get_provider"]

_PENDING = {
    "anthropic": "LH-104",
    "replay": "LH-209",
}


def get_provider(settings: EnvSettings, *, script_path: Path | None = None) -> LLMProvider:
    if settings.provider == "scripted":
        if script_path is None:
            raise ValueError("the scripted provider needs script_path")
        from longhaul.providers.scripted import ScriptedProvider

        return ScriptedProvider(script_path)
    if settings.provider == "openai":
        if settings.openai_api_key is None:
            raise ValueError("provider 'openai' needs OPENAI_API_KEY in the environment")
        from longhaul.providers.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            settings.openai_api_key.get_secret_value(), base_url=settings.openai_base_url
        )
    story = _PENDING[settings.provider]
    raise NotImplementedError(f"provider {settings.provider!r} is not implemented yet ({story})")
