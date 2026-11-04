"""The repo map: the modules of a repo and the symbols in them, for the first prompt (LH-602).

An agent that does not know a repo spends its first turns listing folders and opening files to
find where things are. A map of the modules, with the classes and functions in each, answers
that before the first turn. It is built in the style of aider's repo map: the modules that
matter most come first, each with its signatures, and the list stops when the token budget is
used.

Two steps, with a cache between them.

1. The structure. `run_script` copies repo_map_script.py into the sandbox as text and runs it
   there. The script parses every Python file and prints the modules, their classes, functions
   and signatures, and the import graph as JSON. The repo's code never runs on the worker (plan
   section 4.2), and the JSON that comes back is checked against `RepoStructure` before it is
   used, because the sandbox holds untrusted code and the worker trusts nothing it prints.
2. The map. `render_repo_map` ranks the modules and writes as many as fit in the budget. The
   rank is the number of modules that import a module (a module many others rely on is one the
   agent will meet) plus a boost for every word of the issue that appears in the module's path
   or in the names of its classes and functions (a module the issue is about should come
   first, however few modules import it).

The cache holds step 1 and not step 2. The structure depends only on the commit, but the rank
depends on the issue text, and so does the text that fits, so two tasks on one commit want two
different maps from one structure. `RepoMapCache` keeps the structure of the default branch as
an approved memory item (kind `repo_map`, source `default_branch`, `source_ref` the base sha,
ADR-0010) through the memory store, and parses again only when the sha is one it has not seen.
The sha is also stored in the item's content. The store keeps one row for each distinct text,
so a commit that changed no Python file would otherwise find the row of an older sha and never
hit the cache under its own. `script_runs` counts the times the script ran, which is how a test
tells a cache hit from a rebuild.

The cache is an extra. A store that is down, a row that cannot be read and a failure to write one
are logged, and the map is built from a fresh run of the script. Only a script that fails raises
RepoMapError.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections import Counter
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError

from longhaul.context.tokens import estimate_tokens
from longhaul.contracts import MemoryItem, Sandbox

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "STRUCTURE_VERSION",
    "ClassInfo",
    "FunctionInfo",
    "ModuleInfo",
    "RepoMapCache",
    "RepoMapError",
    "RepoStructure",
    "SkippedFile",
    "build_repo_map",
    "rank_modules",
    "render_repo_map",
    "run_script",
]

KIND = "repo_map"
# The version of the JSON that the script prints and the cache stores. A row written by another
# version is read as a miss, and the script runs again.
STRUCTURE_VERSION = 1
DEFAULT_MAX_TOKENS = 3000
# The script stops itself after 40 seconds (repo_map_script.DEADLINE_S). The activity that calls
# it has 15 minutes, of which the setup and the command check may use most, so this stays short.
SCRIPT_TIMEOUT_S = 60
SCRIPT_SOURCE = Path(__file__).with_name("repo_map_script.py").read_text(encoding="utf-8")

# `python3` and not the image's own path: the fake sandboxes of the tests run on the host. -I
# keeps the repo's folder and the PYTHON* variables out of the interpreter, so a `json.py` in the
# repo cannot stand in for the real one, and -S keeps site-packages and its .pth files out.
SCRIPT_COMMAND = f"python3 -I -S -c {shlex.quote(SCRIPT_SOURCE)}"
STDERR_CHARS = 400

# How much a word of the issue is worth in a module's rank. A word in the path means the module
# is named for the thing the issue talks about. A word in a class or function name means it
# holds code that does. Both outweigh the import count of a typical module (a few, and rarely
# over twenty), and the path counts for more than a symbol because a name is chosen once for a
# whole file.
PATH_WORD_BOOST = 8.0
SYMBOL_WORD_BOOST = 3.0
MIN_WORD_CHARS = 3
# Plain English function words only. Verbs such as add, get and set are left in: they are also
# method names, and a match on one is a real signal.
STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "not", "are", "was", "but", "its",
        "when", "which", "should", "would", "does", "has", "have", "can", "will", "all", "any",
        "one", "into", "also", "please", "than", "then", "they", "them", "there", "what",
        "where", "why", "how", "our", "out", "you", "your",
    }
)  # fmt: skip

# Decorators that change how a method is called, and so how an agent has to write the call.
SHOWN_DECORATORS = frozenset({"property", "classmethod", "staticmethod", "cached_property"})
SHOWN_SKIPPED = 5
NAMES_SHOWN = 12


class RepoMapError(RuntimeError):
    """The script did not produce a structure."""


# ---------- the structure ----------
class FunctionInfo(BaseModel):
    """A function or a method. `signature` is the parameters and return type, as written."""

    name: str
    signature: str = "()"
    doc: str = ""
    decorators: list[str] = []
    is_async: bool = False


class ClassInfo(BaseModel):
    name: str
    bases: list[str] = []
    doc: str = ""
    methods: list[FunctionInfo] = []


class ModuleInfo(BaseModel):
    """One parsed file. `imports` holds the paths of the files of the repo that it imports."""

    path: str
    module: str = ""
    doc: str = ""
    classes: list[ClassInfo] = []
    functions: list[FunctionInfo] = []
    imports: list[str] = []


class SkippedFile(BaseModel):
    path: str
    reason: str


class RepoStructure(BaseModel):
    """What the script prints, plus `sha`, the commit it was read at (set by the caller)."""

    version: int
    sha: str = ""
    modules: list[ModuleInfo] = []
    skipped: list[SkippedFile] = []
    omitted: int = 0  # files left out because the repo was too big for the script's limits


async def run_script(sandbox: Sandbox) -> RepoStructure:
    """Run the script on the workspace of the sandbox and return what it found.

    Files that did not parse are logged here, once, when they are read. A cached structure is
    not logged again.
    """
    result = await sandbox.exec(SCRIPT_COMMAND, timeout_s=SCRIPT_TIMEOUT_S)
    if result.timed_out:
        raise RepoMapError(f"the repo map script did not finish in {SCRIPT_TIMEOUT_S} s")
    if result.exit_code != 0:
        tail = result.stderr.strip()[-STDERR_CHARS:]
        raise RepoMapError(f"the repo map script exited with {result.exit_code}: {tail}")
    try:
        structure = RepoStructure.model_validate_json(result.stdout)
    except ValidationError as exc:
        raise RepoMapError(f"the repo map script printed something else than a map: {exc}") from exc
    if structure.version != STRUCTURE_VERSION:
        raise RepoMapError(
            f"the repo map script is version {structure.version}, the reader {STRUCTURE_VERSION}"
        )
    _log_skipped(structure)
    return structure


def _log_skipped(structure: RepoStructure) -> None:
    for skipped in structure.skipped[:SHOWN_SKIPPED]:
        log.warning("repo map: skipped %s (%s)", skipped.path, skipped.reason)
    if len(structure.skipped) > SHOWN_SKIPPED:
        log.warning("repo map: and %d more files skipped", len(structure.skipped) - SHOWN_SKIPPED)


# ---------- the rank ----------
def words_of(text: str) -> set[str]:
    """The lower-case words of a text. CamelCase and snake_case names are split into theirs."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    found = re.findall(r"[a-z0-9]+", spaced.lower())
    return {w for w in found if len(w) >= MIN_WORD_CHARS and w not in STOPWORDS}


def _symbol_names(module: ModuleInfo) -> list[str]:
    names = [f.name for f in module.functions]
    for cls in module.classes:
        names.append(cls.name)
        names += [m.name for m in cls.methods]
    return names


def _in_degrees(modules: list[ModuleInfo]) -> Counter[str]:
    """For each path, how many other modules import it."""
    known = {m.path for m in modules}
    degree: Counter[str] = Counter()
    for module in modules:
        # A set: three imports of one module from one file are one dependency.
        degree.update(t for t in set(module.imports) if t in known and t != module.path)
    return degree


def _score(module: ModuleInfo, in_degree: int, issue_words: set[str]) -> float:
    in_path = issue_words & words_of(module.path)
    in_symbols = (issue_words & words_of(" ".join(_symbol_names(module)))) - in_path
    return in_degree + PATH_WORD_BOOST * len(in_path) + SYMBOL_WORD_BOOST * len(in_symbols)


def rank_modules(structure: RepoStructure, issue_text: str) -> list[ModuleInfo]:
    """The modules, most relevant first: import in-degree plus the boost for the issue's words.

    Equal scores keep path order, so the same structure and issue give the same map.
    """
    degree = _in_degrees(structure.modules)
    words = words_of(issue_text)
    scored = [(_score(m, degree[m.path], words), m) for m in structure.modules]
    return [m for _, m in sorted(scored, key=lambda pair: (-pair[0], pair[1].path))]


# ---------- the text ----------
def _line(text: str) -> str:
    # Paths, docstrings and defaults come from the repo, and one of them may hold a newline.
    # Everything is one line, so that a name cannot start a line of the map of its own.
    return " ".join(text.split())


def _note(doc: str) -> str:
    return f"  # {_line(doc)}" if doc.strip() else ""


def _function_line(fn: FunctionInfo, indent: str, owner: str = "") -> str:
    shown = [d for d in fn.decorators if d in SHOWN_DECORATORS]
    marks = "".join(f"@{d} " for d in shown) + ("async " if fn.is_async else "")
    name = f"{owner}.{fn.name}" if owner else fn.name
    return indent + _line(f"{marks}{name}{fn.signature}")


def _class_lines(cls: ClassInfo) -> list[str]:
    bases = f"({_line(', '.join(cls.bases))})" if cls.bases else ""
    lines = [f"  class {_line(cls.name)}{bases}{_note(cls.doc)}"]
    for method in cls.methods:
        # Private methods are noise in a map. __init__ stays: it says how to build the class.
        if method.name.startswith("_") and method.name != "__init__":
            continue
        lines.append(_function_line(method, "    ", cls.name) + _note(method.doc))
    return lines


def _full(module: ModuleInfo) -> list[str]:
    lines = [_line(module.path) + _note(module.doc)]
    for cls in module.classes:
        lines += _class_lines(cls)
    lines += [_function_line(f, "  ") + _note(f.doc) for f in module.functions]
    return lines


def _names(module: ModuleInfo) -> list[str]:
    names = [_line(n) for n in dict.fromkeys(_symbol_names(module)) if not n.startswith("_")]
    if not names:
        return [_line(module.path)]
    more = f" (+{len(names) - NAMES_SHOWN} more)" if len(names) > NAMES_SHOWN else ""
    return [f"{_line(module.path)}  # {', '.join(names[:NAMES_SHOWN])}{more}"]


def _tiers(module: ModuleInfo) -> list[list[str]]:
    """The ways to write a module, from the fullest to the shortest, without repeats."""
    options = [_full(module), _names(module), [_line(module.path)]]
    return [block for i, block in enumerate(options) if block not in options[:i]]


def _header(shown: int, total: int) -> str:
    return f"Python files of this repo, most relevant first ({shown} of {total} shown):"


def _notes(structure: RepoStructure) -> list[str]:
    lines = [f"Not parsed: {_line(s.path)} ({_line(s.reason)})" for s in structure.skipped]
    if len(lines) > SHOWN_SKIPPED:
        lines = [
            *lines[:SHOWN_SKIPPED],
            f"Not parsed: {len(structure.skipped) - SHOWN_SKIPPED} more",
        ]
    if structure.omitted:
        lines.append(f"Not read, the repo is too big for the map: {structure.omitted} more files")
    return lines


def _first_that_fits(
    options: list[list[str]], before: list[str], after: list[str], max_tokens: int
) -> list[str] | None:
    for block in options:
        if estimate_tokens("\n".join([*before, *block, *after])) <= max_tokens:
            return block
    return None


def render_repo_map(
    structure: RepoStructure, issue_text: str, max_tokens: int = DEFAULT_MAX_TOKENS
) -> str:
    """Write the map for one issue: ranked modules until `max_tokens` is used.

    A module goes in with its classes, methods and functions if that fits, or else as a line of
    names, or else as its path alone. The first module that fits in none of the three ends the
    list, so the map is a prefix of the ranking. Files that did not parse follow the modules,
    because a file that is broken may be the one the issue is about. The empty string means that
    nothing fits, or that the repo has no Python module.
    """
    if max_tokens < 1:
        raise ValueError(f"max_tokens must be at least 1, got {max_tokens}")
    ranked = rank_modules(structure, issue_text)
    notes = _notes(structure)
    if estimate_tokens("\n".join(notes)) * 2 > max_tokens:
        notes = []  # a few lines at most, but they must not crowd out the modules
    # The count in the header can only get shorter, so the room it takes now is enough.
    top = _header(len(ranked), len(ranked))
    body: list[str] = []
    shown = 0
    for module in ranked:
        fitted = _first_that_fits(_tiers(module), [top, *body], notes, max_tokens)
        if fitted is None:
            break
        body += fitted
        shown += 1
    if not shown:
        return ""
    return "\n".join([_header(shown, len(ranked)), *body, *notes])


async def build_repo_map(
    sandbox: Sandbox, issue_text: str, max_tokens: int = DEFAULT_MAX_TOKENS
) -> str:
    """The repo map of the sandbox's workspace for one issue, parsed fresh.

    This is the uncached path: it runs the script every time. A caller that builds maps for
    many tasks uses RepoMapCache.
    """
    return render_repo_map(await run_script(sandbox), issue_text, max_tokens)


# ---------- the cache ----------
class CachedItem(Protocol):
    @property
    def content(self) -> str: ...


class StructureMemory(Protocol):
    """The part of PgMemoryStore that the cache uses."""

    async def get_verified(self, repo: str, kind: str, source_ref: str) -> CachedItem | None: ...

    async def add_verified(self, item: MemoryItem, *, source_ref: str) -> str: ...


class RepoMapCache:
    """Builds repo maps and keeps the parsed structure of each default-branch sha.

    One per worker is enough: the shared state is the memory store, and the counter only says
    how many times this object had to run the script.
    """

    def __init__(self, memory: StructureMemory) -> None:
        self._memory = memory
        self.script_runs = 0

    async def build(
        self,
        sandbox: Sandbox,
        repo: str,
        sha: str,
        issue_text: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """The map of `repo` at `sha` for this issue. The workspace must be at `sha`."""
        structure = await self.structure_at(sandbox, repo, sha)
        return render_repo_map(structure, issue_text, max_tokens)

    async def structure_at(self, sandbox: Sandbox, repo: str, sha: str) -> RepoStructure:
        """The structure of the repo at `sha`, from memory if it is there, else from the script."""
        cached = await self._read(repo, sha)
        if cached is not None:
            return cached
        self.script_runs += 1  # counted before the run: a script that fails was run all the same
        structure = (await run_script(sandbox)).model_copy(update={"sha": sha})
        await self._write(repo, sha, structure)
        return structure

    async def _read(self, repo: str, sha: str) -> RepoStructure | None:
        try:
            entry = await self._memory.get_verified(repo, KIND, sha)
            if entry is None:
                return None
            structure = RepoStructure.model_validate_json(entry.content)
        except Exception as exc:  # a store that is down is a miss; a cancel is not an Exception
            log.warning("could not read the cached repo map of %s at %s: %s", repo, sha, exc)
            return None
        if structure.version != STRUCTURE_VERSION or structure.sha != sha:
            log.info("the cached repo map of %s at %s is from another version", repo, sha)
            return None
        return structure

    async def _write(self, repo: str, sha: str, structure: RepoStructure) -> None:
        item = MemoryItem(
            id="",  # the store makes the id
            repo=repo,
            kind="repo_map",
            content=structure.model_dump_json(exclude_defaults=True),
            status="approved",
            source="default_branch",
        )
        try:
            await self._memory.add_verified(item, source_ref=sha)
        except Exception as exc:
            log.warning("could not keep the repo map of %s at %s: %s", repo, sha, exc)
