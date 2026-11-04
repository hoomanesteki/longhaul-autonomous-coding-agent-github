"""The repo map: ranking, rendering within a token budget, the script runner and the cache (LH-602).

The ranking and the rendering are pure, so they run on structures built by hand. The runner is
tested against a sandbox that answers with canned output, and once against FakeSandbox, which
runs the real script on a temp folder. The cache is tested with a memory store kept in a dict.
tests/integration/test_repo_map.py runs the whole path in a container and on Postgres.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from longhaul.context.tokens import estimate_tokens
from longhaul.contracts import ExecResult, MemoryItem
from longhaul.memory import repo_map
from longhaul.memory.repo_map import (
    ClassInfo,
    FunctionInfo,
    ModuleInfo,
    RepoMapCache,
    RepoMapError,
    RepoStructure,
    SkippedFile,
    build_repo_map,
    rank_modules,
    render_repo_map,
    run_script,
    words_of,
)
from longhaul.sandbox.local_manager import FakeSandbox

pytestmark = [pytest.mark.unit, pytest.mark.phase6]

SHA = "a" * 40
OTHER_SHA = "b" * 40
REPO = "acme/inventory"


def fn(name: str, signature: str = "()", doc: str = "", **fields: Any) -> FunctionInfo:
    return FunctionInfo(name=name, signature=signature, doc=doc, **fields)


def mod(path: str, *, imports: list[str] | None = None, **fields: Any) -> ModuleInfo:
    return ModuleInfo(path=path, imports=imports or [], **fields)


def structure(*modules: ModuleInfo, **fields: Any) -> RepoStructure:
    return RepoStructure(version=repo_map.STRUCTURE_VERSION, modules=list(modules), **fields)


def listed(text: str) -> list[str]:
    """The paths of a map in its order: the lines after the header that are not indented."""
    lines = text.splitlines()[1:]
    return [line.split()[0] for line in lines if not line.startswith((" ", "Not "))]


def paths_of(modules: list[ModuleInfo]) -> list[str]:
    return [m.path for m in modules]


# ---------- words ----------
def test_words_are_lower_case_and_split_camel_and_snake_case() -> None:
    assert words_of("InventoryError raised by remove_item() in HTTPServer2") == {
        "inventory",
        "error",
        "raised",
        "remove",
        "item",
        "httpserver2",
    }


def test_short_words_and_plain_function_words_are_dropped() -> None:
    assert words_of("It is the id of a SKU, and it should not fail") == {"sku", "fail"}


# ---------- the rank ----------
def test_modules_are_ranked_by_how_many_others_import_them() -> None:
    found = structure(
        mod("c.py", imports=["a.py", "b.py"]),
        mod("d.py", imports=["a.py"]),
        mod("b.py", imports=["a.py"]),
        mod("a.py"),
    )

    # a is imported three times, b once, and c and d not at all.
    assert paths_of(rank_modules(found, "")) == ["a.py", "b.py", "c.py", "d.py"]


def test_equal_scores_keep_path_order() -> None:
    found = structure(mod("z.py"), mod("m.py"), mod("a.py"))

    assert paths_of(rank_modules(found, "")) == ["a.py", "m.py", "z.py"]


def test_an_import_counts_once_and_only_when_it_leads_to_a_module_of_the_map() -> None:
    found = structure(
        mod("a.py", imports=["b.py", "b.py", "a.py", "gone.py"]),
        mod("b.py"),
        mod("c.py", imports=["a.py"]),
    )

    # b is imported once by a, a once by c, and a's import of itself and of a missing file
    # count for nothing.
    assert paths_of(rank_modules(found, "")) == ["a.py", "b.py", "c.py"]


def test_a_word_of_the_issue_in_a_path_outranks_a_busy_module() -> None:
    busy = mod("core.py")
    importers = [mod(f"user{i}.py", imports=["core.py"]) for i in range(6)]
    found = structure(busy, *importers, mod("billing/invoice.py"))

    ranked = paths_of(rank_modules(found, "The invoice total is wrong"))

    assert ranked[0] == "billing/invoice.py"
    assert ranked[1] == "core.py"


def test_a_word_of_the_issue_in_a_symbol_name_counts_less_than_one_in_a_path() -> None:
    in_path = mod("refund.py")
    in_symbol = mod("util.py", functions=[fn("refund_order")])
    nothing = mod("other.py")
    found = structure(nothing, in_symbol, in_path)

    assert paths_of(rank_modules(found, "refund fails")) == ["refund.py", "util.py", "other.py"]


def test_a_word_is_counted_once_even_when_the_path_and_a_symbol_both_hold_it() -> None:
    both = mod("refund/refund.py", classes=[ClassInfo(name="Refund", methods=[fn("refund")])])
    path_only = mod("refund/other.py")

    ranked = rank_modules(structure(both, path_only), "refund")

    # Both hold the word in the path, so they tie and path order decides. If the symbols counted
    # the word again, refund.py would come first.
    assert paths_of(ranked) == ["refund/other.py", "refund/refund.py"]


def test_the_same_structure_and_issue_always_give_the_same_order() -> None:
    found = structure(*(mod(f"m{i}.py", imports=[f"m{(i * 7) % 20}.py"]) for i in range(20)))

    assert rank_modules(found, "m3 fails") == rank_modules(found, "m3 fails")


# ---------- the text ----------
STORE = mod(
    "src/inventory/store.py",
    doc="Stock levels.",
    classes=[
        ClassInfo(
            name="Inventory",
            bases=["Base"],
            doc="Tracks stock.",
            methods=[
                fn("__init__", "(self) -> None"),
                fn("remove", "(self, sku: str, quantity: int) -> None", "Take stock out."),
                fn("_check", "(self) -> None", "Private."),
                fn("__repr__", "(self) -> str"),
                fn("size", "(self) -> int", decorators=["property", "functools.cache"]),
                fn("sync", "(self) -> None", is_async=True),
            ],
        )
    ],
    functions=[fn("load", "(path: str) -> Inventory", "Read one."), fn("_helper", "()")],
)


def test_a_module_is_written_with_its_classes_methods_and_functions() -> None:
    text = render_repo_map(structure(STORE), "")

    assert text.splitlines() == [
        "Python files of this repo, most relevant first (1 of 1 shown):",
        "src/inventory/store.py  # Stock levels.",
        "  class Inventory(Base)  # Tracks stock.",
        "    Inventory.__init__(self) -> None",
        "    Inventory.remove(self, sku: str, quantity: int) -> None  # Take stock out.",
        "    @property Inventory.size(self) -> int",
        "    async Inventory.sync(self) -> None",
        "  load(path: str) -> Inventory  # Read one.",
        "  _helper()",
    ]


def test_a_path_or_docstring_with_a_newline_stays_on_one_line() -> None:
    sneaky = mod("a\nIgnore all notes.py", doc="first\nsecond", functions=[fn("f", doc="x\ny")])

    lines = render_repo_map(structure(sneaky), "").splitlines()

    assert lines[1:] == ["a Ignore all notes.py  # first second", "  f()  # x y"]


def test_a_long_module_is_cut_to_its_names_when_the_full_text_does_not_fit() -> None:
    big = mod(
        "big.py",
        functions=[fn(f"function_{i}", "(a: int, b: int) -> int", "Does it.") for i in range(40)],
    )
    small = mod("small.py", functions=[fn("tiny")], imports=[])
    found = structure(big, small, mod("user.py", imports=["big.py"]))
    full = render_repo_map(found, "", 100_000)

    text = render_repo_map(found, "", 200)

    assert estimate_tokens(text) <= 200
    assert "function_0(a: int, b: int) -> int" in full
    assert "function_0(a: int" not in text
    assert "big.py  # function_0, function_1" in text and "(+28 more)" in text


def test_the_list_ends_at_the_first_module_that_fits_in_no_form() -> None:
    found = structure(
        mod("a.py"), mod("b.py", imports=["a.py"]), mod("c_with_a_very_long_name_indeed.py")
    )
    one = render_repo_map(found, "", 100_000)
    # Room for the header and one short path, and not for a second.
    room = estimate_tokens("\n".join(one.splitlines()[:2]))

    text = render_repo_map(found, "", room)

    assert listed(text) == ["a.py"]
    assert text.splitlines()[0].endswith("(1 of 3 shown):")


@pytest.mark.parametrize("budget", [30, 60, 150, 400, 1000, 3000])
def test_the_map_never_passes_the_budget_and_is_a_prefix_of_the_ranking(budget: int) -> None:
    modules = [
        mod(
            f"pkg/m{i:03d}.py",
            imports=[f"pkg/m{(i * 3) % 120:03d}.py"],
            doc="Module doc.",
            classes=[ClassInfo(name=f"C{i}", methods=[fn("run", "(self) -> None", "Runs.")])],
            functions=[fn(f"f{i}", "(x: int) -> int", "Does f.")],
        )
        for i in range(120)
    ]
    found = structure(
        *modules, skipped=[SkippedFile(path="bad.py", reason="syntax error at line 1")]
    )

    text = render_repo_map(found, "m007 f9", budget)

    assert estimate_tokens(text) <= budget
    ranked = paths_of(rank_modules(found, "m007 f9"))
    shown = [line.split()[0] for line in text.splitlines() if line.startswith("pkg/")]
    assert shown == ranked[: len(shown)]
    assert shown or text == ""


def test_the_default_budget_holds_for_a_big_repo() -> None:
    modules = [
        mod(
            f"pkg/sub{i % 10}/m{i}.py",
            classes=[
                ClassInfo(
                    name=f"Class{i}",
                    methods=[fn(f"m{j}", "(self, a: int) -> int", "Does it.") for j in range(8)],
                )
            ],
        )
        for i in range(1500)
    ]

    text = render_repo_map(structure(*modules), "m42")

    assert 2500 < estimate_tokens(text) <= 3000
    assert "of 1500 shown" in text.splitlines()[0]


def test_nothing_is_written_when_nothing_fits_or_there_is_nothing_to_write() -> None:
    assert render_repo_map(structure(mod("a.py")), "", 5) == ""
    assert render_repo_map(structure(), "") == ""


def test_a_budget_below_one_token_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        render_repo_map(structure(mod("a.py")), "", 0)


def test_files_that_did_not_parse_are_named_after_the_modules() -> None:
    found = structure(
        mod("a.py"),
        skipped=[
            SkippedFile(path="bad.py", reason="syntax error at line 3"),
            SkippedFile(path="big.py", reason="larger than 200 KB"),
        ],
        omitted=0,
    )

    assert render_repo_map(found, "").splitlines()[1:] == [
        "a.py",
        "Not parsed: bad.py (syntax error at line 3)",
        "Not parsed: big.py (larger than 200 KB)",
    ]


def test_many_files_that_did_not_parse_are_summed_up() -> None:
    skipped = [SkippedFile(path=f"bad{i}.py", reason="syntax error") for i in range(8)]

    lines = render_repo_map(structure(mod("a.py"), skipped=skipped), "").splitlines()

    assert lines[-2:] == ["Not parsed: bad4.py (syntax error)", "Not parsed: 3 more"]


def test_files_left_out_for_size_are_counted() -> None:
    text = render_repo_map(structure(mod("a.py"), omitted=250), "")

    assert text.splitlines()[-1] == "Not read, the repo is too big for the map: 250 more files"


def test_notes_give_way_to_modules_when_the_budget_is_small() -> None:
    skipped = [SkippedFile(path=f"bad{i}.py", reason="syntax error at line 1") for i in range(5)]
    found = structure(mod("a.py"), skipped=skipped)

    text = render_repo_map(found, "", 25)

    assert text and "Not parsed" not in text
    assert estimate_tokens(text) <= 25


# ---------- running the script ----------
@dataclass
class Canned:
    """A sandbox that answers every command with one ExecResult and notes what it was asked."""

    result: ExecResult
    calls: list[tuple[str, int]] = field(default_factory=list)
    id: str = "canned"
    workdir: str = "/workspace"

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        self.calls.append((cmd, timeout_s))
        return self.result

    async def read_file(self, path: str) -> bytes:
        raise NotImplementedError

    async def write_file(self, path: str, data: bytes) -> None:
        raise NotImplementedError

    async def destroy(self) -> None: ...


def answer(stdout: str = "", *, code: int = 0, stderr: str = "", timed_out: bool = False) -> Canned:
    return Canned(
        ExecResult(exit_code=code, stdout=stdout, stderr=stderr, timed_out=timed_out, duration_ms=1)
    )


async def test_the_script_is_sent_as_text_to_an_isolated_interpreter() -> None:
    box = answer(json.dumps({"version": 1, "modules": [], "skipped": []}))

    await run_script(box)

    [(command, timeout_s)] = box.calls
    assert command.startswith("python3 -I -S -c ")
    assert "def scan(" in command
    assert timeout_s == repo_map.SCRIPT_TIMEOUT_S


async def test_a_script_that_times_out_is_an_error() -> None:
    with pytest.raises(RepoMapError, match="did not finish"):
        await run_script(answer(code=137, timed_out=True))


async def test_a_script_that_fails_is_an_error_with_the_end_of_its_stderr() -> None:
    noisy = "x" * 1000 + "Traceback: it broke"

    with pytest.raises(RepoMapError, match="exited with 2: x+Traceback: it broke") as caught:
        await run_script(answer(code=2, stderr=noisy))

    assert len(str(caught.value)) < 500


@pytest.mark.parametrize(
    "stdout", ["", "not json", "[]", '{"modules": []}', '{"version": 1, "modules": "no"}']
)
async def test_output_that_is_not_a_map_is_an_error(stdout: str) -> None:
    with pytest.raises(RepoMapError, match="something else than a map"):
        await run_script(answer(stdout))


async def test_a_script_of_another_version_is_an_error() -> None:
    with pytest.raises(RepoMapError, match="version 99"):
        await run_script(answer('{"version": 99}'))


async def test_files_that_were_skipped_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    skipped = [{"path": f"bad{i}.py", "reason": "syntax error at line 1"} for i in range(7)]
    box = answer(json.dumps({"version": 1, "skipped": skipped}))
    caplog.set_level(logging.WARNING, logger="longhaul.memory.repo_map")

    await run_script(box)

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert messages[0] == "repo map: skipped bad0.py (syntax error at line 1)"
    assert len(messages) == 6 and messages[-1] == "repo map: and 2 more files skipped"


async def test_the_real_script_runs_on_a_workspace_and_its_output_is_read() -> None:
    box = FakeSandbox()
    try:
        await box.write_file("pkg/__init__.py", b'"""Pkg."""\nfrom pkg.core import Engine\n')
        await box.write_file(
            "pkg/core.py", b"class Engine:\n    def go(self, speed: int) -> bool: ...\n"
        )
        await box.write_file("pkg/bad.py", b"def (:\n")

        found = await run_script(box)
        text = await build_repo_map(box, "the engine does not go")
    finally:
        await box.destroy()

    assert [m.path for m in found.modules] == ["pkg/__init__.py", "pkg/core.py"]
    assert found.modules[0].imports == ["pkg/core.py"]
    assert [(s.path, s.reason) for s in found.skipped] == [("pkg/bad.py", "syntax error at line 1")]
    assert listed(text)[0] == "pkg/core.py"
    assert "Engine.go(self, speed: int) -> bool" in text


# ---------- the cache ----------
@dataclass
class Row:
    content: str


class Memory:
    """The part of PgMemoryStore that the cache uses, in a dict keyed like the real lookup."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], Row] = {}
        self.added: list[tuple[MemoryItem, str]] = []
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None

    async def get_verified(self, repo: str, kind: str, source_ref: str) -> Row | None:
        if self.read_error is not None:
            raise self.read_error
        return self.rows.get((repo, kind, source_ref))

    async def add_verified(self, item: MemoryItem, *, source_ref: str) -> str:
        if self.write_error is not None:
            raise self.write_error
        self.added.append((item, source_ref))
        self.rows[(item.repo, item.kind, source_ref)] = Row(item.content)
        return f"item-{len(self.added)}"


class Workspace:
    """A FakeSandbox that holds the files of one commit."""

    def __init__(self) -> None:
        self.box = FakeSandbox()

    async def put(self, files: dict[str, str]) -> FakeSandbox:
        for name, text in files.items():
            await self.box.write_file(name, text.encode())
        return self.box


@pytest.fixture
async def workspace() -> AsyncIterator[Workspace]:
    held = Workspace()
    yield held
    await held.box.destroy()


FILES = {
    "src/shop/__init__.py": '"""Shop."""\n',
    "src/shop/cart.py": "class Cart:\n    def add(self, sku: str) -> None: ...\n",
    "tests/test_cart.py": "def test_add() -> None: ...\n",
}


async def test_the_second_map_of_a_sha_is_built_from_memory_and_not_from_the_script(
    workspace: Workspace,
) -> None:
    box = await workspace.put(FILES)
    memory = Memory()
    cache = RepoMapCache(memory)

    first = await cache.build(box, REPO, SHA, "the cart is wrong")
    second = await cache.build(box, REPO, SHA, "test_add fails")

    assert cache.script_runs == 1
    # The same structure, ranked for two issues: the source file first, then the test first.
    assert listed(first)[0] == "src/shop/cart.py"
    assert listed(second)[0] == "tests/test_cart.py"
    assert len(memory.added) == 1


async def test_what_is_kept_is_the_structure_as_an_approved_item_of_the_default_branch(
    workspace: Workspace,
) -> None:
    memory = Memory()

    await RepoMapCache(memory).build(await workspace.put(FILES), REPO, SHA, "")

    [(item, source_ref)] = memory.added
    assert (item.repo, item.kind, item.status, item.source) == (
        REPO,
        "repo_map",
        "approved",
        "default_branch",
    )
    assert source_ref == SHA
    kept = json.loads(item.content)
    assert (kept["version"], kept["sha"]) == (repo_map.STRUCTURE_VERSION, SHA)
    assert [m["path"] for m in kept["modules"]] == [
        "src/shop/__init__.py",
        "src/shop/cart.py",
        "tests/test_cart.py",
    ]


async def test_a_new_sha_or_another_repo_is_parsed_again(workspace: Workspace) -> None:
    box = await workspace.put(FILES)
    cache = RepoMapCache(Memory())

    await cache.build(box, REPO, SHA, "")
    await cache.build(box, REPO, OTHER_SHA, "")
    await cache.build(box, "acme/other", SHA, "")

    assert cache.script_runs == 3


async def test_a_cache_in_another_process_reads_what_the_first_one_kept(
    workspace: Workspace,
) -> None:
    box = await workspace.put(FILES)
    memory = Memory()
    await RepoMapCache(memory).build(box, REPO, SHA, "")
    later = RepoMapCache(memory)

    await later.build(box, REPO, SHA, "")

    assert later.script_runs == 0


@pytest.mark.parametrize(
    "stale",
    [
        "not json",
        '{"version": 1}',  # no sha: it is not the map of this commit
        json.dumps({"version": 1, "sha": OTHER_SHA}),
        json.dumps({"version": 0, "sha": SHA}),
    ],
    ids=["garbage", "no sha", "other sha", "other version"],
)
async def test_a_row_that_does_not_fit_is_a_miss_and_is_replaced(
    workspace: Workspace, stale: str
) -> None:
    box = await workspace.put(FILES)
    memory = Memory()
    memory.rows[(REPO, "repo_map", SHA)] = Row(stale)
    cache = RepoMapCache(memory)

    text = await cache.build(box, REPO, SHA, "")

    assert cache.script_runs == 1
    assert "src/shop/cart.py" in text
    assert json.loads(memory.rows[(REPO, "repo_map", SHA)].content)["sha"] == SHA


async def test_a_store_that_cannot_be_read_is_a_miss(
    workspace: Workspace, caplog: pytest.LogCaptureFixture
) -> None:
    memory = Memory()
    memory.read_error = ConnectionError("database is down")
    cache = RepoMapCache(memory)
    caplog.set_level(logging.WARNING, logger="longhaul.memory.repo_map")

    text = await cache.build(await workspace.put(FILES), REPO, SHA, "")

    assert "src/shop/cart.py" in text and cache.script_runs == 1
    assert "database is down" in caplog.text


async def test_a_store_that_cannot_be_written_still_gives_the_map(
    workspace: Workspace, caplog: pytest.LogCaptureFixture
) -> None:
    memory = Memory()
    memory.write_error = ConnectionError("read-only")
    caplog.set_level(logging.WARNING, logger="longhaul.memory.repo_map")

    text = await RepoMapCache(memory).build(await workspace.put(FILES), REPO, SHA, "")

    assert "src/shop/cart.py" in text
    assert "could not keep the repo map" in caplog.text and "read-only" in caplog.text


async def test_a_script_that_fails_is_counted_and_keeps_nothing() -> None:
    memory = Memory()
    cache = RepoMapCache(memory)

    with pytest.raises(RepoMapError):
        await cache.build(answer(code=1, stderr="boom"), REPO, SHA, "")

    assert cache.script_runs == 1 and memory.added == []


async def test_building_without_a_cache_runs_the_script_every_time(workspace: Workspace) -> None:
    box = await workspace.put(FILES)

    assert await build_repo_map(box, "cart") == await build_repo_map(box, "cart")


def test_the_script_source_is_the_file_next_to_the_reader() -> None:
    path = Path(repo_map.__file__).with_name("repo_map_script.py")

    assert path.read_text(encoding="utf-8") == repo_map.SCRIPT_SOURCE
