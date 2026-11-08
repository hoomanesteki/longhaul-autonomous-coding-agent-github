"""The size of a hint from a person (LH-506).

A hint is kept in the state of the task for as long as the task lives, and every `llm_turn` gets
all of them as input. Nothing else bounds them: the poller cuts the text of a `/longhaul hint`
comment to 2,000 characters, but a hint can also come from the console or from a client that
signals the workflow directly. A few big ones would push the input of `llm_turn` over the payload
guard (ADR-0005) and fail the task, so each hint is cut when its signal arrives.

The module is pure, so the workflow can import it. The cut is the one of github/intake.clip_text,
which cannot be imported there because intake pulls in SQLAlchemy: bytes, on a character boundary,
and a note that says the text was cut.
"""

from __future__ import annotations

__all__ = ["CUT_NOTE", "MAX_AUTHOR_CHARS", "MAX_HINT_BYTES", "clip_author", "clip_hint"]

# 2,000 bytes is about 350 words: more than a person types to unblock a task, and small enough
# that a hundred hints still fit in the payload of one `llm_turn`.
MAX_HINT_BYTES = 2_000
CUT_NOTE = "\n\n[hint cut here: it was longer than 2000 bytes]"
# A GitHub login is at most 39 characters. The console sends its own name.
MAX_AUTHOR_CHARS = 64


def clip_hint(text: str) -> str:
    """At most MAX_HINT_BYTES bytes of `text`, cut on a character boundary, then CUT_NOTE."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= MAX_HINT_BYTES:
        return text
    # "ignore" drops the half of a character that the cut may have left at the end.
    return raw[:MAX_HINT_BYTES].decode("utf-8", "ignore") + CUT_NOTE


def clip_author(name: str) -> str:
    return name[:MAX_AUTHOR_CHARS]
