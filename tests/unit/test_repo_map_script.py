"""The script that reads a repo's Python files in the sandbox (LH-602).

The script is a plain module, so most of these tests call its functions on a temp folder.
Two of them run it the way the sandbox does, as text given to `python3 -I -S -c`, because that
is how its two promises are kept: it needs nothing but the standard library, and a file of the
repo cannot stand in for one of the modules it imports.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from longhaul.memory import repo_map_script as script
from longhaul.memory.repo_map import SCRIPT_SOURCE, RepoStructure, render_repo_map

pytestmark = [pytest.mark.unit, pytest.mark.phase6]


def write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root


def modules(found: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["path"]: m for m in found["modules"]}


# ---------- what the script is ----------
def test_the_script_uses_the_standard_library_only() -> None:
    tree = ast.parse(Path(script.__file__).read_text())
    imported = {
        (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else a.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for a in (node.names if isinstance(node, ast.Import) else [node])
        if not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
    }
    assert imported <= set(sys.stdlib_module_names), imported


def test_running_it_as_text_prints_one_line_of_json_that_the_reader_accepts(
    tmp_path: Path,
) -> None:
    write(tmp_path, {"pkg/__init__.py": '"""Pkg."""\n', "pkg/a.py": "def f(x: int) -> int: ...\n"})
    done = subprocess.run(
        [sys.executable, "-I", "-S", "-c", SCRIPT_SOURCE],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert done.stdout.count("\n") == 1
    structure = RepoStructure.model_validate_json(done.stdout)
    assert [m.path for m in structure.modules] == ["pkg/__init__.py", "pkg/a.py"]
    assert structure.modules[1].functions[0].signature == "(x: int) -> int"


def test_a_file_of_the_repo_cannot_take_the_place_of_a_module_the_script_imports(
    tmp_path: Path,
) -> None:
    # `python -c` would put the current folder first on sys.path, and the repo is untrusted.
    write(tmp_path, {name: "raise SystemExit('the repo ran')\n" for name in ("json.py", "ast.py")})
    done = subprocess.run(
        [sys.executable, "-I", "-S", "-c", SCRIPT_SOURCE],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )

    assert done.returncode == 0, done.stderr
    assert [m["path"] for m in json.loads(done.stdout)["modules"]] == ["ast.py", "json.py"]


# ---------- what it collects ----------
def test_classes_functions_signatures_and_the_first_line_of_each_docstring(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "shop.py": '''"""Prices.

More about prices that the map does not need.
"""


class Cart(Base, metaclass=Meta):
    """A cart.

    Holds items.
    """

    def __init__(self, owner: str, *items: int, tax: float = 0.2, **extra: str) -> None:
        pass

    @property
    def total(self) -> float:
        """The sum."""

    async def pay(self, card: "Card") -> bool: ...


def price(sku, /, qty=1):
    """Price of qty units of sku."""


async def refresh() -> None: ...
'''
        },
    )

    [shop] = script.scan(str(tmp_path))["modules"]

    assert shop["doc"] == "Prices."
    [cart] = shop["classes"]
    assert (cart["name"], cart["bases"], cart["doc"]) == ("Cart", ["Base"], "A cart.")
    init, total, pay = cart["methods"]
    assert (
        init["signature"] == "(self, owner: str, *items: int, tax: float=0.2, **extra: str) -> None"
    )
    assert (total["decorators"], total["doc"]) == (["property"], "The sum.")
    assert (pay["name"], pay["is_async"], pay["signature"]) == (
        "pay",
        True,
        "(self, card: 'Card') -> bool",
    )
    price, refresh = shop["functions"]
    assert (price["signature"], price["doc"]) == ("(sku, /, qty=1)", "Price of qty units of sku.")
    assert refresh == {"name": "refresh", "signature": "() -> None", "is_async": True}


def test_what_has_nothing_to_say_is_left_out_of_the_output(tmp_path: Path) -> None:
    write(tmp_path, {"m.py": "def f(): ...\n"})

    [module] = script.scan(str(tmp_path))["modules"]

    assert module == {
        "functions": [{"name": "f", "signature": "()"}],
        "path": "m.py",
        "module": "m",
    }


def test_nested_definitions_are_not_part_of_the_top_level(tmp_path: Path) -> None:
    source = "def outer():\n    def inner(): ...\n\nclass C:\n    class Inner: ...\n    x = 1\n"
    write(tmp_path, {"m.py": source})

    [module] = script.scan(str(tmp_path))["modules"]

    assert [f["name"] for f in module["functions"]] == ["outer"]
    assert [c["name"] for c in module["classes"]] == ["C"]
    assert "methods" not in module["classes"][0]


def test_long_signatures_and_docstrings_are_cut(tmp_path: Path) -> None:
    long_args = ", ".join(f"argument_{i}: int" for i in range(40))
    write(tmp_path, {"m.py": f'def long({long_args}):\n    """{"word " * 60}"""\n'})

    [module] = script.scan(str(tmp_path))["modules"]

    [long] = module["functions"]
    assert len(long["signature"]) == script.MAX_SIGNATURE_CHARS
    assert long["signature"].endswith("...")
    assert len(long["doc"]) == script.MAX_DOC_CHARS
    assert long["doc"].endswith("...")


def test_the_methods_of_a_class_and_the_definitions_of_a_module_are_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script, "MAX_METHODS", 2)
    monkeypatch.setattr(script, "MAX_DEFINITIONS", 4)
    methods = "".join(f"    def m{i}(self): ...\n" for i in range(5))
    classes = "".join(f"class C{i}:\n    pass\n" for i in range(2))
    functions = "".join(f"def f{i}(): ...\n" for i in range(5))
    write(tmp_path, {"m.py": f"class K:\n{methods}\n{classes}{functions}"})

    [module] = script.scan(str(tmp_path))["modules"]

    assert [c["name"] for c in module["classes"]] == ["K", "C0", "C1"]
    assert [m["name"] for m in module["classes"][0]["methods"]] == ["m0", "m1"]
    assert [f["name"] for f in module["functions"]] == ["f0"]  # the classes used the room
    monkeypatch.setattr(script, "MAX_DEFINITIONS", 2)
    [module] = script.scan(str(tmp_path))["modules"]
    assert [c["name"] for c in module["classes"]] == ["K", "C0"]
    assert "functions" not in module


# ---------- module names ----------
def test_the_name_of_a_module_comes_from_the_init_files_above_it(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "src/pkg/__init__.py": "",
            "src/pkg/sub/__init__.py": "",
            "src/pkg/sub/leaf.py": "",
            "src/loose/script.py": "",
            "tests/test_leaf.py": "",
            "setup.py": "",
        },
    )

    names = {path: m["module"] for path, m in modules(script.scan(str(tmp_path))).items()}

    assert names == {
        "src/pkg/__init__.py": "pkg",
        "src/pkg/sub/__init__.py": "pkg.sub",
        "src/pkg/sub/leaf.py": "pkg.sub.leaf",
        "src/loose/script.py": "script",
        "tests/test_leaf.py": "test_leaf",
        "setup.py": "setup",
    }


# ---------- the import graph ----------
def graph(root: Path) -> dict[str, list[str]]:
    return {path: m.get("imports", []) for path, m in modules(script.scan(str(root))).items()}


def test_absolute_imports_lead_to_the_files_of_the_repo(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "",
            "pkg/b.py": "import pkg.a\nimport os, json\nfrom pkg import a as other\n",
            "main.py": "from pkg.b import thing\nimport pkg\nfrom pkg.a import *\n",
        },
    )

    found = graph(tmp_path)

    assert found["pkg/b.py"] == ["pkg/a.py"]
    assert found["main.py"] == ["pkg/__init__.py", "pkg/a.py", "pkg/b.py"]
    assert found["pkg/a.py"] == []


def test_importing_a_name_of_a_package_leads_to_the_package_file(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "src/inventory/__init__.py": "from inventory.store import Inventory\n",
            "src/inventory/store.py": "",
            "tests/test_store.py": "from inventory import Inventory\n",
        },
    )

    found = graph(tmp_path)

    assert found["src/inventory/__init__.py"] == ["src/inventory/store.py"]
    assert found["tests/test_store.py"] == ["src/inventory/__init__.py"]


def test_importing_a_submodule_by_its_parent_leads_to_the_submodule(tmp_path: Path) -> None:
    write(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "", "main.py": "from pkg import a\n"})

    assert graph(tmp_path)["main.py"] == ["pkg/a.py"]


def test_relative_imports_are_resolved_against_the_package_of_the_file(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "pkg/__init__.py": "from . import a\nfrom .sub import leaf\n",
            "pkg/a.py": "from .sub.leaf import x\nfrom .. import nothing\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/leaf.py": "from .. import a\nfrom ..a import y\nfrom ... import toofar\n",
        },
    )

    found = graph(tmp_path)

    assert found["pkg/__init__.py"] == ["pkg/a.py", "pkg/sub/leaf.py"]
    assert found["pkg/a.py"] == ["pkg/sub/leaf.py"]
    assert found["pkg/sub/leaf.py"] == ["pkg/a.py"]


def test_imports_inside_functions_and_type_checking_blocks_count(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "a.py": "",
            "b.py": "",
            "c.py": "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    import a\n"
            "def f():\n    import b\n",
        },
    )

    assert graph(tmp_path)["c.py"] == ["a.py", "b.py"]


def test_a_module_does_not_import_itself_and_each_import_counts_once(tmp_path: Path) -> None:
    write(tmp_path, {"a.py": "import a\nimport b\nimport b\nfrom b import x, y\n", "b.py": ""})

    assert graph(tmp_path)["a.py"] == ["b.py"]


def test_a_namespace_package_resolves_by_its_path(tmp_path: Path) -> None:
    # No __init__.py anywhere: the name is the path, with or without a leading src.
    write(
        tmp_path,
        {
            "src/ns/mod.py": "",
            "tools/run.py": "import src.ns.mod\nimport ns.mod\nimport tools.run\n",
        },
    )

    assert graph(tmp_path)["tools/run.py"] == ["src/ns/mod.py"]


def test_two_files_with_one_name_leave_it_to_the_first_in_path_order(tmp_path: Path) -> None:
    write(tmp_path, {"a/util.py": "", "b/util.py": "", "main.py": "import util\n"})

    assert graph(tmp_path)["main.py"] == ["a/util.py"]


# ---------- what is skipped ----------
def test_a_file_with_a_syntax_error_is_skipped_and_listed_with_its_line(tmp_path: Path) -> None:
    write(tmp_path, {"ok.py": "def f(): ...\n", "bad.py": "x = 1\ndef g(:\n    pass\n"})

    found = script.scan(str(tmp_path))

    assert list(modules(found)) == ["ok.py"]
    assert found["skipped"] == [{"path": "bad.py", "reason": "syntax error at line 2"}]


def test_a_file_over_the_size_limit_is_skipped_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script, "MAX_FILE_BYTES", 50)
    write(tmp_path, {"big.py": "x = 1\n" * 20, "small.py": "x = 1\n"})

    found = script.scan(str(tmp_path))

    assert list(modules(found)) == ["small.py"]
    assert found["skipped"] == [{"path": "big.py", "reason": "larger than 0 KB"}]


def test_the_default_size_limit_is_200_kb(tmp_path: Path) -> None:
    write(
        tmp_path,
        {"ok.py": "x = 1\n" + "# pad\n" * 30_000, "big.py": "x = 1\n" + "# pad\n" * 40_000},
    )

    found = script.scan(str(tmp_path))

    assert list(modules(found)) == ["ok.py"]
    assert found["skipped"] == [{"path": "big.py", "reason": "larger than 200 KB"}]


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"x = 1\x00\n", "syntax error"),
        (b"# coding: utf-8\nx = '\xff'\n", "syntax error at line 2"),
        (b"x = " + b"1+" * 20_000 + b"1\n", "nested too deeply to parse"),
    ],
    ids=["null byte", "not utf-8", "nesting"],
)
def test_a_file_that_cannot_be_parsed_is_skipped_whatever_the_reason(
    tmp_path: Path, data: bytes, reason: str
) -> None:
    (tmp_path / "odd.py").write_bytes(data)
    write(tmp_path, {"ok.py": ""})

    found = script.scan(str(tmp_path))

    assert list(modules(found)) == ["ok.py"]
    assert found["skipped"] == [{"path": "odd.py", "reason": reason}]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (SyntaxError("bad"), "syntax error"),
        (MemoryError(), "too large to parse"),
        (PermissionError(), "could not be read"),
        (ValueError(), "could not be parsed"),
    ],
)
def test_every_kind_of_failure_has_a_reason_in_words(error: BaseException, reason: str) -> None:
    assert script.reason_for(error) == reason


def test_folders_that_do_not_hold_the_projects_source_are_not_walked(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "keep/a.py": "",
            ".git/hooks/pre.py": "",
            ".venv/lib/site.py": "",
            "env/pyvenv.cfg": "home = /usr/bin\n",
            "env/lib/mod.py": "",
            "node_modules/x/y.py": "",
            "pkg/__pycache__/z.py": "",
            "pkg.egg-info/w.py": "",
            "build/lib/b.py": "",
            "dist/d.py": "",
            "lib/python3.12/site-packages/s.py": "",
            "top.py": "",
        },
    )

    assert sorted(modules(script.scan(str(tmp_path)))) == ["keep/a.py", "top.py"]


def test_links_are_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside, {"secret.py": "def leak(): ...\n"})
    repo = write(tmp_path / "repo", {"a.py": ""})
    (repo / "linked_dir").symlink_to(outside, target_is_directory=True)
    (repo / "linked_file.py").symlink_to(outside / "secret.py")
    (repo / "loop").symlink_to(repo, target_is_directory=True)

    assert sorted(modules(script.scan(str(repo)))) == ["a.py"]


# ---------- strings that JSON cannot carry ----------
def test_a_lone_surrogate_in_a_docstring_does_not_make_the_output_unreadable(
    tmp_path: Path,
) -> None:
    # The source holds the escape, and ast.get_docstring returns the character itself.
    write(tmp_path, {"pkg/weird.py": 'def f():\n    "\\udc80 lone surrogate"\n'})

    found = script.scan(str(tmp_path))

    structure = RepoStructure.model_validate_json(json.dumps(found))
    [weird] = structure.modules
    assert weird.functions[0].doc == "? lone surrogate"


def test_a_path_that_is_not_utf8_is_made_valid_before_it_is_printed() -> None:
    # os.walk decodes a file name of other bytes with surrogateescape.
    name = os.fsdecode(b"pkg/caf\xe9.py")
    assert "\udce9" in name

    assert script.clean({"path": name, "imports": [name], "n": 1}) == {
        "path": "pkg/caf?.py",
        "imports": ["pkg/caf?.py"],
        "n": 1,
    }


# ---------- limits ----------
def test_a_scan_that_runs_out_of_time_says_how_many_files_it_left_out(tmp_path: Path) -> None:
    write(tmp_path, {f"m{i}.py": "" for i in range(4)})

    found = script.scan(str(tmp_path), deadline_s=-1)

    assert found["modules"] == [] and found["omitted"] == 4


def test_a_scan_that_runs_out_of_time_says_so_and_one_that_does_not_is_silent(
    tmp_path: Path,
) -> None:
    write(tmp_path, {"m.py": "def f(): ...\n"})

    assert script.scan(str(tmp_path), deadline_s=-1)["out_of_time"] is True
    assert "out_of_time" not in script.scan(str(tmp_path))


DOCUMENTED = "".join(
    f'def {name}(value: int, other: str = "x") -> int:\n    """Does the {name} work."""\n\n'
    for name in ("alpha", "beta", "gamma", "delta")
)


def size_of(entry: dict[str, Any]) -> int:
    return len(json.dumps(entry, separators=(",", ":")))


def test_output_over_the_size_limit_sheds_detail_and_keeps_every_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, {f"m{i:02d}.py": DOCUMENTED for i in range(12)})
    full = script.scan(str(tmp_path))
    assert all("functions" in m for m in full["modules"])
    monkeypatch.setattr(script, "MAX_OUTPUT_BYTES", sum(map(size_of, full["modules"])) // 2)

    found = script.scan(str(tmp_path))

    # Nothing is dropped. The path order stays, and the last module still has its names.
    assert [m["path"] for m in found["modules"]] == [m["path"] for m in full["modules"]]
    assert found["omitted"] == 0
    last = modules(found)["m11.py"]
    assert last["names"] == ["alpha", "beta", "gamma", "delta"]
    assert "functions" not in last and "doc" not in last
    assert sum(map(size_of, found["modules"])) <= script.MAX_OUTPUT_BYTES


def test_detail_goes_first_to_the_modules_that_the_most_others_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {f"m{i:02d}.py": "import zz_core\n" + DOCUMENTED for i in range(6)}
    write(tmp_path, {**files, "zz_core.py": DOCUMENTED})
    full = modules(script.scan(str(tmp_path)))
    # Room for the names of every module and the full text of one, and not of two.
    cap = sum(size_of(script.names_form(m)) for m in full.values())
    cap += size_of(full["zz_core.py"]) - size_of(script.names_form(full["zz_core.py"])) + 20
    monkeypatch.setattr(script, "MAX_OUTPUT_BYTES", cap)

    found = modules(script.scan(str(tmp_path)))

    assert [path for path, m in found.items() if "functions" in m] == ["zz_core.py"]
    assert all("names" in m for path, m in found.items() if path != "zz_core.py")


def test_when_even_the_bare_modules_do_not_fit_the_least_imported_are_dropped_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The last in path order is the one the others import, so it stays when they go.
    write(tmp_path, {**{f"m{i}.py": "import m7\n" for i in range(7)}, "m7.py": ""})
    bare = {"path": "m0.py", "module": "m0", "imports": ["m7.py"]}
    monkeypatch.setattr(script, "MAX_OUTPUT_BYTES", size_of(bare) * 3 + 5)

    found = script.scan(str(tmp_path))

    assert [m["path"] for m in found["modules"]] == ["m0.py", "m1.py", "m7.py"]
    assert all(set(m) <= {"path", "module", "imports"} for m in found["modules"])
    assert found["omitted"] == 5


def test_a_repo_over_the_real_size_limit_still_lists_the_module_the_issue_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "".join(
        f'def function_{i}(first: int, second: str = "x") -> int:\n    """Does thing {i}."""\n\n'
        for i in range(30)
    )
    files = {f"pkg/m{i:03d}.py": body for i in range(300)}
    write(tmp_path, {**files, "zz_sql/functions.py": "def count(expr): ...\n"})
    cap = script.MAX_OUTPUT_BYTES
    with monkeypatch.context() as unlimited:
        unlimited.setattr(script, "MAX_OUTPUT_BYTES", 10**9)
        assert sum(map(size_of, script.scan(str(tmp_path))["modules"])) > cap

    done = subprocess.run(
        [sys.executable, "-I", "-S", "-c", SCRIPT_SOURCE],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )

    assert len(done.stdout) < script.MAX_OUTPUT_BYTES + 50_000
    structure = RepoStructure.model_validate_json(done.stdout)
    assert structure.omitted == 0 and len(structure.modules) == 301
    assert structure.modules[-1].path == "zz_sql/functions.py"
    text = render_repo_map(structure, "func.count in zz_sql/functions.py is wrong")
    assert [line for line in text.splitlines() if line.startswith("zz_sql/")][0].startswith(
        "zz_sql/functions.py"
    )


def test_the_list_of_skipped_files_is_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script, "MAX_SKIPPED", 2)
    write(tmp_path, {f"bad{i}.py": "def (:\n" for i in range(5)})

    assert len(script.scan(str(tmp_path))["skipped"]) == 2


def test_an_empty_folder_gives_an_empty_map(tmp_path: Path) -> None:
    assert script.scan(str(tmp_path)) == {
        "version": script.VERSION,
        "modules": [],
        "skipped": [],
        "omitted": 0,
    }
