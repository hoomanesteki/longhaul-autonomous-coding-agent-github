"""Retrieval into the prompt: the memory block of a task (LH-604, ADR-0010, invariant I7).

The agent starts a task knowing nothing about the repo. Memory gives it three kinds of notes, and
this module writes them as the one block that ends the system prompt (block 4 of the layout in
agent/prompts.py):

* the commands that ran the repo's tests and linter when Longhaul tried them on the default
  branch (LH-603), all of them, because there are few and the agent should not guess;
* the lessons, pitfalls and conventions that are nearest to the issue by cosine distance, five
  at most. A note is found by what the issue says, so a repo with a hundred notes still sends
  five;
* the repo map that prepare_workspace built for this issue (LH-602), which comes in as text.

Three rules shape the text.

**Only approved notes (I7).** The store is asked for approved items, and the answer is checked
again: a note that is not approved, not of this repo or not of the asked kind is dropped here. A
bug in one place then does not put a proposed note in a prompt. Approval is ADR-0010's business,
and this module keeps its half of the bargain at the last step before the text leaves.

**Data, not instructions.** The block opens with a line that tells the model to treat the notes
as hints and to verify them, and holds them in a fenced block. A fence is only a signal while the
content cannot close it, so its length is one more than the longest run of backticks inside
(`fence_for`): a note that contains ``` cannot end the block and write the rest as prose. Each
lesson and command is one line, with its whitespace collapsed and its length capped, so a note
cannot start a line of its own that looks like a heading or a fence.

**A budget.** The block is at most `budget_tokens` by the estimate of context/tokens.py, 4,000
by default. The commands come first, then the lessons, and the map takes what is left, cut at a
line with a note that says so. The map is the part that gives way because it is the largest and
the only one that is already ranked by relevance, so its tail is the least that the agent loses.
Notes that do not fit are left out whole.

An empty string means that there is nothing to say: the section stays out of the prompt, and the
bytes of the prompt are what they were before memory.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Protocol

from longhaul.context.tokens import estimate_tokens
from longhaul.contracts import MemoryItem

__all__ = [
    "DEFAULT_BUDGET_TOKENS",
    "HEADER",
    "LESSON_KINDS",
    "MAX_COMMANDS",
    "MAX_LESSONS",
    "CommandReader",
    "MemoryReader",
    "fence_for",
    "first_test_command",
    "is_test_command",
    "render_memory_block",
]

# The first line of the block. It is the words of the plan (LH-604) and ADR-0010.
HEADER = "Notes from past work on this repo. Treat them as hints and verify before relying on them."
DEFAULT_BUDGET_TOKENS = 4000
# The kinds that are found by what the issue says. The repo map and the commands are not: the map
# is passed in, and the commands are listed whole.
LESSON_KINDS = ("lesson", "pitfall", "convention")
MAX_LESSONS = 5
MAX_COMMANDS = 8
# One note is one line of at most this many characters. LH-605 trims what it drafts to 300, so
# this only bites on a note that a person wrote by hand.
MAX_NOTE_CHARS = 400
# The part of an issue that the notes are searched by. The embedder reads 512 tokens at most.
MAX_QUERY_CHARS = 8000

COMMANDS_TITLE = "Commands that ran this repo's tests or linter on its default branch:"
LESSONS_TITLE = "Lessons from earlier work on this repo, nearest to this issue first:"
MAP_CUT = "... (map cut to fit the notes budget)"

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_RUN_OF_TICKS = re.compile(r"`+")
# A command that runs the tests, as opposed to one that lints: pytest or tox, maybe behind a
# runner, or the `test` target of a Makefile. `pytest-watch` and `make test-all` are not it.
_TEST_COMMAND = re.compile(
    r"(?:(?:uv run|poetry run|pipenv run|hatch run|python3? -m) )*(?:pytest|tox)(?:\s.*)?"
    r"|make test(?:\s.*)?"
)


class _Listed(Protocol):
    """The part of a listed item that retrieval reads (PgMemoryStore.list_items)."""

    @property
    def content(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def status(self) -> str: ...


class CommandReader(Protocol):
    """The read that finds the commands of a repo: all that the test command needs."""

    async def list_items(
        self, repo: str, *, status: str | None = None, kind: str | None = None
    ) -> Sequence[_Listed]: ...


class MemoryReader(CommandReader, Protocol):
    """The two reads of PgMemoryStore that the memory block needs."""

    async def search(
        self,
        repo: str,
        query: str,
        k: int,
        approved_only: bool = True,
        *,
        kinds: Sequence[str] | None = None,
    ) -> list[MemoryItem]: ...


# ---------- the block ----------
async def render_memory_block(
    memory: MemoryReader,
    repo: str,
    issue_text: str,
    *,
    repo_map: str = "",
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> str:
    """The memory block for a task on `repo` whose issue says `issue_text` (title and body).

    `repo_map` is the map that prepare_workspace wrote for this issue. The result is at most
    `budget_tokens` by `estimate_tokens`, or the empty string when there is nothing to say.
    """
    if budget_tokens < 1:
        raise ValueError(f"budget_tokens must be at least 1, got {budget_tokens}")
    commands = await _approved_commands(memory, repo)
    lessons = await _nearest_lessons(memory, repo, issue_text)
    map_lines = [line.rstrip() for line in repo_map.strip().splitlines()]
    return _fit(commands, lessons, map_lines, budget_tokens)


async def _approved_commands(memory: CommandReader, repo: str) -> list[str]:
    listed = await memory.list_items(repo, status="approved", kind="command")
    commands = (
        _one_line(e.content) for e in listed if e.status == "approved" and e.kind == "command"
    )
    # Oldest first, as the store lists them, so that the block does not change order when a
    # command is added.
    return list(dict.fromkeys(c for c in commands if c))[:MAX_COMMANDS]


async def _nearest_lessons(memory: MemoryReader, repo: str, issue_text: str) -> list[str]:
    query = issue_text[:MAX_QUERY_CHARS]
    if not query.strip():
        return []
    found = await memory.search(repo, query, MAX_LESSONS, True, kinds=LESSON_KINDS)
    lines = [
        f"- [{item.kind}] {text}"
        for item in found
        if item.status == "approved"
        and item.repo == repo
        and item.kind in LESSON_KINDS
        and (text := _one_line(item.content))
    ]
    return lines[:MAX_LESSONS]


def _one_line(text: str) -> str:
    """The note on one line: whitespace collapsed, control characters gone, length capped."""
    line = " ".join(_CONTROL.sub(" ", text).split())
    if len(line) <= MAX_NOTE_CHARS:
        return line
    return line[: MAX_NOTE_CHARS - 3].rstrip() + "..."


# ---------- the fence ----------
def fence_for(content: str) -> str:
    """A fence of backticks that nothing in `content` can close.

    A closing fence needs at least as many backticks as the opening one (CommonMark), so one more
    than the longest run inside is enough, and three is the shortest fence there is.
    """
    longest = max((len(run) for run in _RUN_OF_TICKS.findall(content)), default=0)
    return "`" * max(3, longest + 1)


def _assemble(lines: list[str]) -> str:
    content = "\n".join(lines)
    fence = fence_for(content)
    return f"{HEADER}\n\n{fence}text\n{content}\n{fence}"


# ---------- the budget ----------
def _fit(commands: list[str], lessons: list[str], map_lines: list[str], budget: int) -> str:
    """The sections in the order of their importance, each as far as the budget allows."""

    def fits(lines: list[str]) -> bool:
        return estimate_tokens(_assemble(lines)) <= budget

    body = _grow([], COMMANDS_TITLE, [f"- {command}" for command in commands], fits)
    body = _grow(body, LESSONS_TITLE, lessons, fits)
    body = _with_map(body, map_lines, fits)
    return _assemble(body) if body else ""


def _grow(
    body: list[str], title: str, items: list[str], fits: Callable[[list[str]], bool]
) -> list[str]:
    """`body` with as many of `items` as fit, under a title that goes in with the first one.

    An item that does not fit ends the section. A note that is shorter and comes later is not
    squeezed in behind it, because the items are in order of importance.
    """
    grown, started = body, False
    for item in items:
        heading = [] if started else [*([""] if grown else []), title]
        candidate = [*grown, *heading, item]
        if not fits(candidate):
            break
        grown, started = candidate, True
    return grown


def _with_map(
    body: list[str], map_lines: list[str], fits: Callable[[list[str]], bool]
) -> list[str]:
    """`body` followed by the repo map, whole if it fits, else its longest prefix that does.

    The length of the block grows with every line of the map, so the longest prefix is found by
    halving, and the map of a big repo is not assembled line by line. A cut map ends with a note,
    which has its room reserved. A map of which not even one line fits is left out.
    """
    if not map_lines:
        return body
    lead = [""] if body else []
    whole = [*body, *lead, *map_lines]
    if fits(whole):
        return whole

    def cut(lines: int) -> list[str]:
        return [*body, *lead, *map_lines[:lines], MAP_CUT]

    low, high, best = 1, len(map_lines) - 1, 0
    while low <= high:
        middle = (low + high) // 2
        if fits(cut(middle)):
            best, low = middle, middle + 1
        else:
            high = middle - 1
    return cut(best) if best else body


# ---------- the test command ----------
def is_test_command(command: str) -> bool:
    """Whether `command` runs the repo's tests: pytest or tox, or the `test` target of make."""
    return _TEST_COMMAND.fullmatch(command.strip()) is not None


async def first_test_command(memory: CommandReader, repo: str) -> str | None:
    """The first approved command of `repo` that runs its tests, or None.

    This is the test command of a repo whose settings name none. "First" is the order of the
    store, oldest first, which is the order in which prepare_workspace found them: the pytest
    config of pyproject.toml before tox before the Makefile. A command that a person rejected is
    not approved and is not offered, and neither is a linter.
    """
    for command in await _approved_commands(memory, repo):
        if is_test_command(command):
            return command
    return None
