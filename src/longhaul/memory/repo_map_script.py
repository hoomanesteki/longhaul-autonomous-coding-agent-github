"""Read the Python files of a repo and print their structure as JSON (LH-602).

This file is not imported by the worker. repo_map.py sends its text into the sandbox and runs it
there with `python3 -I -S -c`, because it parses repo code and nothing from the repo may run on
the worker (plan section 4.2). Parsing with `ast` does not execute the code, but the parser has
had crashes of its own, and the sandbox is where a crash costs nothing. For the same reason the
script uses the standard library only, so it needs nothing installed in the image, and `-I -S`
keep the repo's own `json.py` or `ast.py`, and the `.pth` files of a virtualenv, out of it.

What it does:

1. Walk the folder it is given (the current folder by default) for `*.py` files. Hidden folders,
   virtualenvs, caches, build output and symlinks are left out.
2. Parse each file under 200 KB and keep its docstring, its top-level classes and functions, the
   methods of the classes, and the signature and first docstring line of each. A file that does
   not parse is not in `modules`. It is in `skipped`, with the reason.
3. Resolve the imports of each file to the other files of the repo, which gives the import graph.
   `imports` lists the paths a module imports, once each. Ranking is left to the caller, which
   knows the issue text.

The output is one line of JSON: `version`, `modules`, `skipped` and `omitted`, and `out_of_time`
when the deadline stopped the scan. The limits below keep the output under the 1 MB that a
sandbox command may print. repo_map.RepoStructure reads the same shape.

When the modules together are over the output limit, detail goes before modules do. Every module
starts as its path, its name and its imports (the "bare" form). The modules that the most others
import then get their symbol names (the "names" form), and after that the same modules get the
full text, signatures and docstrings, while there is room. The script cannot know the issue, and
the import count is the part of the ranking that it can compute. So a late path in a big repo
still has its place in the map, and the caller's ranking by the issue's words still sees it.
Only a repo whose bare forms alone are over the limit loses modules, and it loses the ones that
the fewest others import. `omitted` counts those, and the files that the deadline left unread.

Module names. A file's importable name comes from the `__init__.py` files above it, so
`src/inventory/store.py` is `inventory.store`: the folder `src` has no `__init__.py`, which makes
it the root. The path with its slashes turned into dots, and that path without a leading `src`
or `lib`, are also taken as names, so a namespace package (no `__init__.py`) still resolves.
If two files claim one name, the first in path order keeps it.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import time
from collections import Counter
from typing import Any

VERSION = 1
# A bigger file is generated code or data, and parsing it costs more than the map gains.
MAX_FILE_BYTES = 200_000
# How much of one file reaches the output.
MAX_DEFINITIONS = 60  # top-level classes and functions
MAX_METHODS = 40  # per class
MAX_SIGNATURE_CHARS = 200
MAX_DOC_CHARS = 120
MAX_BASE_CHARS = 60
MAX_DECORATOR_CHARS = 40
MAX_NAMES = 200  # symbol names kept in the names form
# The output as a whole. The sandbox keeps 1 MB of what a command prints and drops the middle.
MAX_OUTPUT_BYTES = 700_000
MAX_SKIPPED = 200
# The script is the first thing to run against the repo, so it stops itself before the caller's
# own time limit and prints what it has.
DEADLINE_S = 40.0

# Folders that never hold the project's own source. Hidden ones (.git, .venv, .tox) are skipped
# by their dot, and a folder with a pyvenv.cfg is a virtualenv whatever it is called.
SKIP_DIRS = frozenset({"__pycache__", "node_modules", "site-packages", "build", "dist"})
EGG_INFO = ".egg-info"
SOURCE_ROOTS = ("src", "lib")


def first_line(text: str | None, limit: int = MAX_DOC_CHARS) -> str:
    """The first line of a docstring, cut to `limit` characters."""
    if not text:
        return ""
    line = text.strip().split("\n", 1)[0].strip()
    return line if len(line) <= limit else line[: limit - 3] + "..."


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def present(fields: dict[str, Any]) -> dict[str, Any]:
    """The fields that say something. An empty docstring or list is the reader's default, and
    leaving it out keeps a repo of thousands of functions well inside the output limit."""
    return {name: value for name, value in fields.items() if value}


def clean(value: Any) -> Any:
    """The value with every string made valid UTF-8.

    A docstring can hold a lone surrogate (written as "\\udc80" in the source), and os.walk
    decodes a file name of other bytes with surrogateescape. JSON can carry them as escapes, but
    the reader's JSON parser refuses them, and one odd file would then cost the map of the whole
    repo. They become "?".
    """
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    return value


# ---------- step 1: the files ----------
def find_python_files(root: str) -> tuple[list[str], set[str]]:
    """The relative paths of the `*.py` files under root, sorted, and every folder with an __init__.

    Paths use forward slashes. The folders are relative to root too, and the top folder is "".
    """
    files: list[str] = []
    packages: set[str] = set()
    for folder, dirs, names in os.walk(root, followlinks=False):
        # Edit the list in place: that is how os.walk is told not to enter a folder.
        dirs[:] = sorted(d for d in dirs if not skipped_dir(os.path.join(folder, d), d))
        relative = os.path.relpath(folder, root)
        prefix = "" if relative == "." else relative.replace(os.sep, "/")
        for name in sorted(names):
            full = os.path.join(folder, name)
            if not name.endswith(".py") or os.path.islink(full) or not os.path.isfile(full):
                continue
            files.append(f"{prefix}/{name}" if prefix else name)
            if name == "__init__.py":
                packages.add(prefix)
    return sorted(files), packages


def skipped_dir(full: str, name: str) -> bool:
    if name.startswith(".") or name in SKIP_DIRS or name.endswith(EGG_INFO):
        return True
    if os.path.islink(full):
        return True  # a link can lead out of the repo, or back into it and loop
    return os.path.exists(os.path.join(full, "pyvenv.cfg"))


# ---------- step 2: one file ----------
def signature_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    text = f"({ast.unparse(node.args)})"
    if node.returns is not None:
        text += f" -> {ast.unparse(node.returns)}"
    return clip(text, MAX_SIGNATURE_CHARS)


def function_entry(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    return present(
        {
            "name": node.name,
            "signature": signature_of(node),
            "doc": first_line(ast.get_docstring(node)),
            "decorators": [clip(ast.unparse(d), MAX_DECORATOR_CHARS) for d in node.decorator_list],
            "is_async": isinstance(node, ast.AsyncFunctionDef),
        }
    )


def class_entry(node: ast.ClassDef) -> dict[str, Any]:
    methods = [
        function_entry(child)
        for child in node.body
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    return present(
        {
            "name": node.name,
            "bases": [clip(ast.unparse(b), MAX_BASE_CHARS) for b in node.bases],
            "doc": first_line(ast.get_docstring(node)),
            "methods": methods[:MAX_METHODS],
        }
    )


def imports_of(tree: ast.AST) -> list[tuple[int, str, list[str]]]:
    """Every import in the file, wherever it sits, as (level, module, names).

    A plain `import a.b` is (0, "a.b", []). `from . import x` is (1, "", ["x"]). Imports inside
    functions and under `if TYPE_CHECKING` count: the module depends on them either way.
    """
    found: list[tuple[int, str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(0, alias.name, []) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.level, node.module or "", [a.name for a in node.names]))
    return found


class FileTooBigError(Exception):
    """The file is over MAX_FILE_BYTES."""


def read_module(path: str, root: str) -> tuple[dict[str, Any], list[tuple[int, str, list[str]]]]:
    """Parse one file into its entry (without name or imports) and its raw imports.

    Raises FileTooBigError, SyntaxError, ValueError, RecursionError, MemoryError or OSError,
    which the caller turns into a reason for skipping the file.
    """
    full = os.path.join(root, *path.split("/"))
    if os.path.getsize(full) > MAX_FILE_BYTES:
        raise FileTooBigError
    with open(full, "rb") as handle:
        tree = ast.parse(handle.read(), filename=path)
    # The cap is shared by classes and functions, and classes come first because a class
    # says more about a module than a helper function does.
    classes = [class_entry(n) for n in tree.body if isinstance(n, ast.ClassDef)]
    classes = classes[:MAX_DEFINITIONS]
    functions = [
        function_entry(n)
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    entry = present(
        {
            "doc": first_line(ast.get_docstring(tree)),
            "classes": classes,
            "functions": functions[: MAX_DEFINITIONS - len(classes)],
        }
    )
    return entry, imports_of(tree)


def reason_for(exc: BaseException) -> str:
    """Why a file is not in the map, in words for the agent."""
    if isinstance(exc, FileTooBigError):
        return f"larger than {MAX_FILE_BYTES // 1000} KB"
    if isinstance(exc, SyntaxError):
        return f"syntax error at line {exc.lineno}" if exc.lineno else "syntax error"
    if isinstance(exc, RecursionError):
        return "nested too deeply to parse"
    if isinstance(exc, MemoryError):
        return "too large to parse"
    if isinstance(exc, OSError):
        return "could not be read"
    return "could not be parsed"


# ---------- step 3: names and the graph ----------
def module_names(path: str, packages: set[str]) -> tuple[str, bool, list[str]]:
    """The importable name of a file, whether it is a package, and every name that reaches it."""
    folder, _, filename = path.rpartition("/")
    stem = filename[: -len(".py")]
    is_package = stem == "__init__"
    parts: list[str] = [] if is_package else [stem]
    # Climb while the folder is a package: that is where the import system starts too.
    here = folder
    while here in packages and here != "":
        head, _, tail = here.rpartition("/")
        parts.insert(0, tail)
        here = head
    primary = ".".join(parts)
    by_path = ".".join(path[: -len(".py")].split("/"))
    if is_package:
        by_path = by_path.removesuffix(".__init__")
    names = [primary, by_path]
    first, _, rest = by_path.partition(".")
    if first in SOURCE_ROOTS and rest:
        names.append(rest)
    return primary, is_package, [n for n in dict.fromkeys(names) if n]


def resolve_relative(level: int, module: str, package: str) -> str | None:
    """The absolute name of `from <level dots><module> import ...` inside `package`."""
    if level == 0:
        return module
    parts = package.split(".") if package else []
    up = level - 1
    if up > len(parts):
        return None  # more dots than packages: the import cannot work
    base = parts[: len(parts) - up]
    if module:
        base.append(module)
    return ".".join(base)


def longest_prefix(index: dict[str, str], dotted: str) -> str | None:
    """The path of `a.b.c`, else of `a.b`, else of `a`: importing a name imports its parents."""
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        found = index.get(".".join(parts[:end]))
        if found is not None:
            return found
    return None


def resolve_imports(
    raw: list[tuple[int, str, list[str]]], package: str, index: dict[str, str], own: str
) -> list[str]:
    """The paths of the repo files that these imports lead to, sorted, without `own`."""
    targets: set[str] = set()
    for level, module, names in raw:
        base = resolve_relative(level, module, package)
        if base is None:
            continue
        if not names:  # import a.b.c
            found = longest_prefix(index, base)
            if found is not None:
                targets.add(found)
            continue
        # from base import name: `name` is a submodule when the repo has one, else a thing in base.
        for name in names:
            submodule = f"{base}.{name}" if base else name
            found = index.get(submodule) or (longest_prefix(index, base) if base else None)
            if found is not None:
                targets.add(found)
    targets.discard(own)
    return sorted(targets)


# ---------- fitting the output ----------
def size_of(value: Any) -> int:
    """The bytes that `value` takes in the output, with the comma after it in a list.

    The output is ASCII, because json.dumps escapes everything else, so characters are bytes.
    """
    return len(json.dumps(value, separators=(",", ":"))) + 1


def names_of(entry: dict[str, Any]) -> list[str]:
    """The public names of a module's functions, classes and methods, in the order the map shows."""
    names = [f["name"] for f in entry.get("functions", [])]
    for cls in entry.get("classes", []):
        names.append(cls["name"])
        names += [m["name"] for m in cls.get("methods", [])]
    # The map leaves private names out, so they would only take room here.
    return [n for n in dict.fromkeys(names) if not n.startswith("_")][:MAX_NAMES]


def bare_form(entry: dict[str, Any]) -> dict[str, Any]:
    """The path, the module name and the imports of a module, which is what the graph needs."""
    return {k: v for k, v in entry.items() if k in ("path", "module", "imports")}


def names_form(entry: dict[str, Any]) -> dict[str, Any]:
    """The bare form with the names of the module's symbols, but no signatures or docstrings."""
    return present({**bare_form(entry), "names": names_of(entry)})


def fit_modules(
    entries: list[tuple[str, list[str], dict[str, Any]]], budget: int
) -> tuple[list[dict[str, Any]], int]:
    """The module entries to print, in path order, and how many modules had to be dropped.

    Each entry is (path, imports, full entry). The forms are tried from the cheapest, in the
    order of how many modules import the module and then of the path, so that detail is spent
    where the caller's ranking is most likely to look first.
    """
    forms = [(entry, names_form(entry), bare_form(entry)) for _, _, entry in entries]
    sizes = [[size_of(form) for form in group] for group in forms]
    known = {path for path, _, _ in entries}
    in_degree: Counter[str] = Counter()
    for _, imports, _ in entries:
        in_degree.update(target for target in imports if target in known)
    order = sorted(range(len(entries)), key=lambda i: (-in_degree[entries[i][0]], entries[i][0]))

    form_of: dict[int, int] = {}  # index into forms: 0 full, 1 names, 2 bare
    used = 0
    for i in order:
        if used + sizes[i][2] > budget:
            break  # the rest import the fewest others
        form_of[i] = 2
        used += sizes[i][2]
    for target in (1, 0):
        for i in order:
            current = form_of.get(i)
            if current is None or current <= target:
                continue
            extra = sizes[i][target] - sizes[i][current]
            if used + extra <= budget:
                form_of[i] = target
                used += extra
    kept = [forms[i][form_of[i]] for i in range(len(entries)) if i in form_of]
    return kept, len(entries) - len(kept)


# ---------- putting it together ----------
def scan(root: str = ".", deadline_s: float = DEADLINE_S) -> dict[str, Any]:
    """The structure of the Python files under root, as the dict that main prints."""
    started = time.monotonic()
    paths, packages = find_python_files(root)
    index: dict[str, str] = {}
    packages_of: dict[str, str] = {}
    for path in paths:
        primary, is_package, names = module_names(path, packages)
        packages_of[path] = primary if is_package else primary.rpartition(".")[0]
        for name in names:
            index.setdefault(name, path)

    parsed: list[tuple[str, dict[str, Any], list[tuple[int, str, list[str]]]]] = []
    skipped: list[dict[str, str]] = []
    omitted = 0
    out_of_time = False
    for position, path in enumerate(paths):
        if time.monotonic() - started > deadline_s:
            omitted = len(paths) - position
            out_of_time = True
            break
        try:
            entry, raw = read_module(path, root)
        except (
            SyntaxError,
            ValueError,
            RecursionError,
            MemoryError,
            OSError,
            FileTooBigError,
        ) as e:
            if len(skipped) < MAX_SKIPPED:
                skipped.append({"path": path, "reason": reason_for(e)})
            continue
        parsed.append((path, entry, raw))

    entries: list[tuple[str, list[str], dict[str, Any]]] = []
    for path, entry, raw in parsed:
        imports = resolve_imports(raw, packages_of[path], index, path)
        entry["path"] = path
        entry["module"] = module_names(path, packages)[0]
        if imports:
            entry["imports"] = imports
        entries.append((path, imports, clean(entry)))
    kept, dropped = fit_modules(entries, MAX_OUTPUT_BYTES - size_of(clean(skipped)))
    found: dict[str, Any] = {
        "version": VERSION,
        "modules": kept,
        "skipped": clean(skipped),
        "omitted": omitted + dropped,
    }
    if out_of_time:
        found["out_of_time"] = True
    return found


def main(argv: list[str]) -> int:
    root = argv[1] if len(argv) > 1 else "."
    json.dump(scan(root), sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
