"""Record the model calls of a run, then play them back.

A chaos run kills the worker many times and compares the final diff with a run that was never
killed. That comparison means something only if the model says the same thing every time, so
the chaos runs do not call a model. They play a recording of one run.

A recording is a folder with one JSON file per call, named `<seq>-turn<turn>-<hash>.json`:

    {"turn_index": 3, "request_sha256": "...", "request": {...}, "response": {...}}

`seq` is the order the calls were made in, so the folder reads in order and a second recording
session in the same folder goes on after the first. The request is kept whole, so that a person
can diff two requests when a replay stops matching.

Matching. A call is found by its turn and the hash of its request (`request_hash`): the SHA-256
of the model, the system prompt, the messages, the tools and `max_tokens` as JSON with sorted
keys. Options such as the effort are not part of it, as in the scripted provider. The turn is
the number of assistant turns done, counting turns folded into notes (providers.base.turn_index).

Risk R6. The request of a later turn holds the output of earlier tools, and a tool can print
something that changes from run to run, such as the seconds a test run took. Then the hash does
not match, and the call falls back to the first recording of the same turn, with a warning that
names the hash. A change to a prompt or a tool description does the same for every turn. Re-record
when the warnings are many. A turn that has no recording at all is an error that is not retried,
since asking again cannot make one appear.

Recording. `ReplayProvider(folder, record_with=provider)` calls the provider and saves each
pair before it returns the reply. The first recording of a (turn, hash) pair is kept. A call
that Temporal ran twice sends the same request twice, and the second reply may differ, but the
requests after it were built from the first one. Record without kills. Files are written to a
temporary name and renamed, so a worker that is killed in the middle of a write leaves no file
that a replay would read as damaged. A recording provider goes by the name of the one it wraps,
because that is who made the calls.
"""

import asyncio
import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from longhaul.contracts import (
    CallOptions,
    LLMProvider,
    LLMResponse,
    Message,
    ProviderError,
    ProviderErrorKind,
    ToolSpec,
)
from longhaul.providers.base import turn_index

log = logging.getLogger(__name__)

FILE_NAME = "{seq:04d}-turn{turn:03d}-{digest}.json"
_DIGEST_CHARS = 12
_CHARS_PER_TOKEN = 4


def canonical_request(
    model: str, system: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int
) -> str:
    """The request as the JSON text that is hashed: sorted keys, no spaces."""
    request = {
        "model": model,
        "system": system,
        "messages": [m.model_dump(mode="json") for m in messages],
        "tools": [t.model_dump(mode="json") for t in tools],
        "max_tokens": max_tokens,
    }
    return json.dumps(request, sort_keys=True, separators=(",", ":"))


def request_hash(
    model: str, system: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int
) -> str:
    canonical = canonical_request(model, system, messages, tools, max_tokens)
    return hashlib.sha256(canonical.encode()).hexdigest()


class _Pair(BaseModel):
    turn_index: int = Field(ge=0)
    request_sha256: str = Field(min_length=1)
    request: dict[str, Any]
    response: LLMResponse


def _read_pairs(folder: Path) -> list[_Pair]:
    """Every recording in the folder, in the order the calls were made."""
    pairs: list[_Pair] = []
    for path in sorted(folder.glob("*.json")):
        try:
            pairs.append(_Pair.model_validate_json(path.read_bytes()))
        except (ValidationError, OSError) as exc:
            raise ValueError(f"damaged recording {path}: {exc}") from exc
    return pairs


def _next_seq(folder: Path) -> int:
    taken = [int(p.name.split("-", 1)[0]) for p in folder.glob("*.json") if p.name[:1].isdigit()]
    return max(taken, default=0) + 1


class ReplayProvider:
    """Plays the recordings in `recording_dir`, or with `record_with`, records that provider."""

    def __init__(
        self, recording_dir: Path | str, *, record_with: LLMProvider | None = None
    ) -> None:
        self._dir = Path(recording_dir)
        self._inner = record_with
        self.name = "replay" if record_with is None else record_with.name
        self._lock = threading.Lock()  # a recording is written from a worker thread
        self._seen: set[tuple[int, str]] = set()
        self._seq = 1
        self._exact: dict[tuple[int, str], LLMResponse] = {}
        self._by_turn: dict[int, LLMResponse] = {}
        if record_with is None:
            self._load()
        else:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._seen = {(p.turn_index, p.request_sha256) for p in _read_pairs(self._dir)}
            self._seq = _next_seq(self._dir)

    def _load(self) -> None:
        if not self._dir.is_dir():
            raise ValueError(f"the recordings folder {self._dir} does not exist")
        pairs = _read_pairs(self._dir)
        if not pairs:
            raise ValueError(f"no recordings in {self._dir}")
        for pair in pairs:
            # The first recording of a pair wins, as in record mode.
            self._exact.setdefault((pair.turn_index, pair.request_sha256), pair.response)
            self._by_turn.setdefault(pair.turn_index, pair.response)

    # ----- the protocol -----
    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        cache_system: bool = True,
        options: CallOptions | None = None,
    ) -> LLMResponse:
        canonical = canonical_request(model, system, messages, tools, max_tokens)
        turn = turn_index(messages)
        if self._inner is None:
            return self._play(turn, hashlib.sha256(canonical.encode()).hexdigest())
        reply = await self._inner.complete(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            cache_system=cache_system,
            options=options,
        )
        await asyncio.to_thread(self._save, turn, canonical, reply)
        return reply

    async def count_tokens(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        options: CallOptions | None = None,
    ) -> int:
        if self._inner is not None:
            return await self._inner.count_tokens(
                model=model, system=system, messages=messages, tools=tools, options=options
            )
        # A replay has no model to ask. The size of the request is a steady estimate.
        return max(1, len(canonical_request(model, system, messages, tools, 0)) // _CHARS_PER_TOKEN)

    # ----- replaying -----
    def _play(self, turn: int, digest: str) -> LLMResponse:
        recorded = self._exact.get((turn, digest))
        if recorded is None:
            recorded = self._by_turn.get(turn)
            if recorded is None:
                raise ProviderError(
                    ProviderErrorKind.INVALID_REQUEST,
                    f"no recording for turn {turn} in {self._dir}; it covers turns "
                    f"{sorted(self._by_turn)}",
                )
            log.warning(
                "no recording matches the request %s at turn %d; playing the first recording "
                "of that turn. A prompt or a tool output changed since the recording was made "
                "(risk R6): record again if this keeps happening",
                digest[:_DIGEST_CHARS],
                turn,
            )
        # The time of the original call is not the time of this one, and a copy keeps a caller
        # that edits the reply from changing the recording.
        return recorded.model_copy(deep=True, update={"latency_ms": 0})

    # ----- recording -----
    def _save(self, turn: int, canonical: str, reply: LLMResponse) -> None:
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self._lock:
            if (turn, digest) in self._seen:
                return
            self._seen.add((turn, digest))
            seq, self._seq = self._seq, self._seq + 1
            path = self._dir / FILE_NAME.format(seq=seq, turn=turn, digest=digest[:_DIGEST_CHARS])
            pair = {
                "turn_index": turn,
                "request_sha256": digest,
                "request": json.loads(canonical),
                "response": reply.model_dump(mode="json"),
            }
            partial = path.with_name(path.name + ".part")
            partial.write_text(json.dumps(pair, indent=1) + "\n", encoding="utf-8")
            os.replace(partial, path)  # a reader sees the whole file or none
