"""Provider protocol re-exports, the factory chosen by LONGHAUL_PROVIDER, and the
two transcript conventions that scripted and replay providers read.

Role line. The first line of every system prompt names the role, written with
``ROLE_LINE_FORMAT``. Prompt builders import the constant instead of retyping it.

Turn marker. A compaction (LH-403) rewrites the transcript to a user message
with the notes, followed by the most recent turns. The count of assistant
messages then drops, although the task is still on the same turn. So the first
user message of a compacted transcript carries one line, written with
``format_turn_offset(n)``, where n is the number of assistant turns folded into
the notes so far, summed over all compactions. ``turn_index`` adds n to the
assistant messages still in the transcript, which keeps the turn number the
same before and after a compaction.
"""

import re
from pathlib import Path

from longhaul.contracts import (
    LLMProvider,
    Message,
    ProviderError,
    ProviderErrorKind,
    Role,
    TextBlock,
)
from longhaul.settings import EnvSettings

__all__ = [
    "ROLE_LINE_FORMAT",
    "ROLE_LINE_PREFIX",
    "TURN_OFFSET_FORMAT",
    "LLMProvider",
    "ProviderError",
    "ProviderErrorKind",
    "format_role_line",
    "format_turn_offset",
    "get_provider",
    "parse_role_line",
    "read_turn_offset",
    "turn_index",
]

ROLE_LINE_FORMAT = "Role: {role}"
ROLE_LINE_PREFIX = ROLE_LINE_FORMAT.split("{role}")[0]

TURN_OFFSET_FORMAT = "[longhaul:turn_offset={offset}]"
_OFFSET_HEAD, _OFFSET_TAIL = TURN_OFFSET_FORMAT.split("{offset}")
# The marker must stand alone on its line, so prose that mentions it is not read.
_TURN_OFFSET_LINE = re.compile(
    rf"^[ \t]*{re.escape(_OFFSET_HEAD)}(\d+){re.escape(_OFFSET_TAIL)}[ \t]*$", re.MULTILINE
)

_PENDING = {
    "replay": "LH-209",
}


def format_role_line(role: Role | str) -> str:
    return ROLE_LINE_FORMAT.format(role=role.value if isinstance(role, Role) else role)


def parse_role_line(system: str) -> str | None:
    """The role named on the first line of a system prompt, or None when it has no role line."""
    first = system.split("\n", 1)[0]
    if not first.startswith(ROLE_LINE_PREFIX):
        return None
    return first[len(ROLE_LINE_PREFIX) :].strip() or None


def format_turn_offset(offset: int) -> str:
    if offset < 0:
        raise ValueError(f"turn offset must not be negative, got {offset}")
    return TURN_OFFSET_FORMAT.format(offset=offset)


def read_turn_offset(messages: list[Message]) -> int:
    """The turn offset written into the first user message, or 0 when there is none.

    Only the first message is read. Tool output and later user messages can hold
    any text, and they must not move the turn counter. The last marker wins, so
    one that is quoted earlier in the message cannot hide the real one.
    """
    if not messages or messages[0].role != "user":
        return 0
    found = [
        int(match.group(1))
        for block in messages[0].content
        if isinstance(block, TextBlock)
        for match in _TURN_OFFSET_LINE.finditer(block.text)
    ]
    return found[-1] if found else 0


def turn_index(messages: list[Message]) -> int:
    """Assistant turns completed so far, including turns folded into notes."""
    return read_turn_offset(messages) + sum(1 for m in messages if m.role == "assistant")


def get_provider(settings: EnvSettings, *, script_path: Path | None = None) -> LLMProvider:
    if settings.provider == "scripted":
        path = script_path or settings.script
        if path is None:
            raise ValueError(
                "the scripted provider needs a script: pass script_path or set LONGHAUL_SCRIPT"
            )
        from longhaul.providers.scripted import ScriptedProvider

        return ScriptedProvider(path)
    if settings.provider == "anthropic":
        if settings.anthropic_api_key is None:
            raise ValueError("provider 'anthropic' needs ANTHROPIC_API_KEY in the environment")
        from longhaul.providers.anthropic import AnthropicProvider

        return AnthropicProvider(settings.anthropic_api_key.get_secret_value())
    if settings.provider == "openai":
        if settings.openai_api_key is None:
            raise ValueError("provider 'openai' needs OPENAI_API_KEY in the environment")
        from longhaul.providers.openai_compat import OpenAICompatProvider

        api_key = settings.openai_api_key.get_secret_value()
        # An empty OPENAI_BASE_URL is not a URL. Leaving it out lets the SDK use its default.
        if settings.openai_base_url:
            return OpenAICompatProvider(api_key, base_url=settings.openai_base_url)
        return OpenAICompatProvider(api_key)
    story = _PENDING[settings.provider]
    raise NotImplementedError(f"provider {settings.provider!r} is not implemented yet ({story})")
