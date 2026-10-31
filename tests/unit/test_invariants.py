"""Invariant tests. Each test names the invariant it pins."""

import ast
import pickle
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from longhaul.contracts import BlobRef
from longhaul.durable.payload_guard import (
    DEFAULT_LIMIT,
    PayloadTooLargeError,
    assert_small,
    json_size,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase2]


class Note(BaseModel):
    text: str


def _string_of_json_size(size: int) -> str:
    """A string whose JSON form is exactly `size` bytes: the text plus two quotes."""
    return "x" * (size - 2)


def test_payload_size_guard() -> None:
    # I2: nothing larger than 256 KB crosses an activity boundary.
    with pytest.raises(PayloadTooLargeError) as caught:
        assert_small({"transcript": "x" * 300_000})
    assert caught.value.limit == 256_000
    assert caught.value.size > 300_000
    assert "blob store" in str(caught.value)

    assert_small({"transcript": "x" * 1_000})


def test_payload_size_guard_measures_pydantic_models() -> None:
    assert_small(Note(text="short"))
    with pytest.raises(PayloadTooLargeError):
        assert_small(Note(text="x" * 300_000))


def test_payload_size_guard_measures_lists_of_models() -> None:
    # A transcript is a list of messages, which is how the largest payloads look.
    assert_small([Note(text="x" * 100)] * 10)
    with pytest.raises(PayloadTooLargeError):
        assert_small([Note(text="x" * 100_000)] * 3)


def test_payload_size_guard_limit_is_inclusive() -> None:
    at_limit = _string_of_json_size(DEFAULT_LIMIT)
    assert json_size(at_limit) == DEFAULT_LIMIT
    assert_small(at_limit)

    with pytest.raises(PayloadTooLargeError) as caught:
        assert_small(at_limit + "x")
    assert caught.value.size == DEFAULT_LIMIT + 1


def test_payload_size_guard_takes_its_own_limit() -> None:
    assert_small("x" * 100, limit=1_000)
    with pytest.raises(PayloadTooLargeError) as caught:
        assert_small("x" * 100, limit=50)
    assert caught.value.limit == 50
    assert caught.value.size == 102


def test_payload_size_guard_counts_utf8_bytes_not_characters() -> None:
    # Three bytes per character: 100 characters are 300 bytes plus the quotes.
    assert json_size("€" * 100) == 302


def test_payload_too_large_error_survives_pickling() -> None:
    # Exceptions cross process boundaries by pickle, which replays only the message
    # unless the class says how to rebuild itself.
    error = pickle.loads(pickle.dumps(PayloadTooLargeError(300_002, 256_000)))
    assert isinstance(error, PayloadTooLargeError)
    assert (error.size, error.limit) == (300_002, 256_000)
    assert str(error) == str(PayloadTooLargeError(300_002, 256_000))


def test_a_blob_ref_is_tiny_next_to_the_limit() -> None:
    # The point of ADR-0005: a 5 MB payload crosses the boundary as this.
    ref = BlobRef(key="tasks/lh-o-r-42/turns/0007/response.json", sha256="0" * 64, size=5_000_000)
    assert json_size(ref) < 200


# ---------- I1: workflow modules import no I/O libraries ----------
#
# Workflow code runs inside Temporal's sandbox, and the sandbox does not reliably block I/O
# libraries (ADR-0004, appendix section 1). Some of them import cleanly there, and a module
# loaded under workflow.unsafe.imports_passed_through() skips the sandbox's check altogether.
# So a scan of the source is the guard. The plan names httpx; the lock file has httpx2 and
# httpcore2 in its place, so the list names all three.

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
WORKFLOWS = SRC / "longhaul" / "durable" / "workflows"
IO_MODULES = frozenset(
    {
        "httpx",
        "httpx2",
        "httpcore2",
        "docker",
        "sqlalchemy",
        "boto3",
        "anthropic",
        "openai",
        "subprocess",
    }
)
# Calls that import a module by name. Only a string literal can be compared with IO_MODULES.
DYNAMIC_IMPORTS = frozenset({"__import__", "importlib.import_module", "import_module"})


def _dotted(node: ast.expr) -> str:
    """The dotted name of a name or attribute chain, and "" for any other expression."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def imported_names(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """(line, dotted name) for each absolute import in the tree, wherever it sits.

    That covers imports inside functions, try blocks, `if TYPE_CHECKING` and
    imports_passed_through() blocks, and calls like __import__("docker") with a literal.
    Relative imports are skipped here: they name a module of the package itself, which
    import_closure follows.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module
        elif isinstance(node, ast.Call) and node.args and _dotted(node.func) in DYNAMIC_IMPORTS:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                yield node.lineno, first.value


def io_imports(source: str) -> list[str]:
    """One "line N: name" entry for each import of an I/O library or one of its submodules."""
    return sorted(
        f"line {line}: {name}"
        for line, name in imported_names(ast.parse(source))
        # Compare the top-level package, so docker.errors is caught and dockerfile_parse is not.
        if name.split(".")[0] in IO_MODULES
    )


def module_name(path: Path, src: Path) -> str:
    parts = list(path.relative_to(src).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def module_file(name: str, src: Path) -> Path | None:
    base = src.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _prefixes(name: str) -> list[str]:
    """Importing a.b.c loads a, then a.b, then a.b.c, so each of them counts."""
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def _import_targets(node: ast.AST, package: str) -> list[str]:
    """Dotted names an import statement can load, relative imports resolved against package.

    In `from a import b`, b may be a module or an attribute of a. Both are listed, and
    import_closure keeps the ones that are files.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level == 0:
        base = node.module or ""
    else:
        # One dot is the package itself, two dots its parent, and so on.
        parts = package.split(".")
        keep = len(parts) - (node.level - 1)
        if keep < 1:
            return []
        base = ".".join([*parts[:keep], *([node.module] if node.module else [])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def import_closure(roots: Iterable[str], src: Path) -> dict[str, Path]:
    """Every module under src that importing the roots loads, as {module name: file}.

    It follows imports of modules that exist under src (the package's own code) and also
    counts the parent packages, whose __init__ runs first. Third-party modules are not
    followed: the lock file decides what they import, and the scans above name the ones
    that matter.
    """
    found: dict[str, Path] = {}
    todo = list(roots)
    while todo:
        for name in _prefixes(todo.pop()):
            file = module_file(name, src)
            if file is None or name in found:
                continue
            found[name] = file
            package = name if file.name == "__init__.py" else name.rpartition(".")[0]
            for node in ast.walk(ast.parse(file.read_text())):
                todo.extend(_import_targets(node, package))
    return found


def closure_violations(roots: Iterable[str], src: Path) -> dict[str, list[str]]:
    """{module: its banned imports} for every module in the closure that has any."""
    found = {
        name: io_imports(file.read_text()) for name, file in import_closure(roots, src).items()
    }
    return {name: hits for name, hits in sorted(found.items()) if hits}


def workflow_modules(src: Path = SRC) -> list[str]:
    folder = src / "longhaul" / "durable" / "workflows"
    return [module_name(path, src) for path in sorted(folder.glob("*.py"))]


def write_tree(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def test_workflows_have_no_io_imports() -> None:
    # I1: workflow code does no I/O, so it imports no library that does.
    files = sorted(WORKFLOWS.glob("*.py"))
    assert files, f"no workflow modules under {WORKFLOWS}; did the directory move?"
    found = {path.name: io_imports(path.read_text()) for path in files}
    assert {name: hits for name, hits in found.items() if hits} == {}


def test_workflow_import_closure_has_no_io_imports() -> None:
    # I1, one level down. A workflow imports longhaul.contracts and longhaul.durable.acts,
    # and anything those import runs in the sandbox as well. A module imported under
    # imports_passed_through() is not checked by the sandbox, so this scan is the only guard.
    roots = workflow_modules()
    closure = import_closure(roots, SRC)
    assert {"longhaul", "longhaul.durable", "longhaul.durable.workflows"} <= set(closure)
    assert closure_violations(roots, SRC) == {}


def test_the_closure_walk_reaches_what_the_task_workflow_imports() -> None:
    # Without this, a walk that found nothing would pass the scan above for the wrong reason.
    closure = import_closure(workflow_modules(), SRC)
    assert {
        "longhaul.durable.workflows.task",
        "longhaul.contracts",
        "longhaul.durable.acts",
        "longhaul.durable.payload_guard",
    } <= set(closure)


@pytest.mark.parametrize(
    "source",
    [
        "import docker",
        "import docker.errors",
        "import docker as d",
        "from docker import errors",
        "from docker.types import Mount",
        "from docker import *",
        "import os, subprocess",
        "from subprocess import run",
        "import httpx",
        "import httpx2",
        "from httpx2 import AsyncClient",
        "import httpcore2.sync",
        "import sqlalchemy.ext.asyncio",
        "import boto3",
        "from boto3.session import Session",
        "import anthropic",
        "from openai import AsyncOpenAI",
        "def f():\n    import docker",
        "class A:\n    def f(self):\n        from sqlalchemy import text",
        "if TYPE_CHECKING:\n    import docker",
        "with workflow.unsafe.imports_passed_through():\n    from boto3 import client",
        "try:\n    import openai\nexcept ImportError:\n    openai = None",
        "__import__('docker')",
        "importlib.import_module('boto3.session')",
        "import_module('subprocess')",
    ],
)
def test_io_imports_finds_every_form(source: str) -> None:
    assert io_imports(source)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "import asyncio\nfrom datetime import timedelta",
        "from longhaul.contracts import CarryState",
        "from temporalio import workflow",
        # Other packages that start with the same letters.
        "import dockerfile_parse",
        "import openai_compat",
        # A module of the package itself that happens to share a name.
        "from . import docker",
        "from .docker import Thing",
        "from .. import subprocess",
        "import my.docker",
        "from my.docker import thing",
        # A name is not an import, and a computed module name cannot be compared.
        "docker = 1\nprint(docker)",
        "importlib.import_module(name)",
        "importlib.import_module('asyncio')",
    ],
)
def test_io_imports_ignores_everything_else(source: str) -> None:
    assert io_imports(source) == []


def test_io_imports_reports_the_line_and_the_module() -> None:
    source = "import os\n\nfrom docker.types import Mount\nimport subprocess\n"
    assert io_imports(source) == ["line 3: docker.types", "line 4: subprocess"]


def test_import_closure_follows_the_packages_own_imports(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/flow.py": "from pkg.helpers import util\nimport temporalio\n",
            "pkg/helpers/__init__.py": "",
            "pkg/helpers/util.py": "def f():\n    from pkg import deep\n",
            "pkg/deep.py": "import docker\n",
            "pkg/unused.py": "import boto3\n",
        },
    )
    assert set(import_closure(["pkg.flow"], tmp_path)) == {
        "pkg",
        "pkg.flow",
        "pkg.helpers",
        "pkg.helpers.util",
        "pkg.deep",
    }
    assert closure_violations(["pkg.flow"], tmp_path) == {"pkg.deep": ["line 1: docker"]}


def test_import_closure_resolves_relative_imports(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a/__init__.py": "",
            "pkg/a/flow.py": "from . import near\nfrom .near2 import x\nfrom ..up import y\n",
            "pkg/a/near.py": "import openai\n",
            "pkg/a/near2.py": "import subprocess\n",
            "pkg/up.py": "from pkg.a import far\n",
            "pkg/a/far.py": "import httpx2\n",
        },
    )
    assert closure_violations(["pkg.a.flow"], tmp_path) == {
        "pkg.a.far": ["line 1: httpx2"],
        "pkg.a.near": ["line 1: openai"],
        "pkg.a.near2": ["line 1: subprocess"],
    }


def test_import_closure_counts_the_init_of_each_parent_package(tmp_path: Path) -> None:
    # Importing pkg.helpers.util runs pkg/helpers/__init__.py first.
    write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/flow.py": "import pkg.helpers.util\n",
            "pkg/helpers/__init__.py": "from boto3 import client\n",
            "pkg/helpers/util.py": "",
        },
    )
    assert closure_violations(["pkg.flow"], tmp_path) == {"pkg.helpers": ["line 1: boto3"]}


def test_import_closure_ends_on_import_cycles_and_ignores_unknown_names(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from pkg import b\nfrom pkg import not_a_module\nimport numpy\n",
            "pkg/b.py": "from pkg import a\n",
        },
    )
    assert set(import_closure(["pkg.a"], tmp_path)) == {"pkg", "pkg.a", "pkg.b"}


def test_workflow_modules_lists_the_package_and_its_workflows(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "longhaul/__init__.py": "",
            "longhaul/durable/__init__.py": "",
            "longhaul/durable/workflows/__init__.py": "",
            "longhaul/durable/workflows/task.py": "",
            "longhaul/durable/activities/llm.py": "import anthropic\n",
        },
    )
    assert workflow_modules(tmp_path) == [
        "longhaul.durable.workflows",
        "longhaul.durable.workflows.task",
    ]


# ---------- I4: every GitHub write goes through github/side_effects.py (LH-304) ----------
#
# The GitHub client keeps its write methods private and says that only side_effects.py may
# call them. That module adds the idempotency key and the search for an earlier attempt. A
# write from anywhere else would be posted twice when a retry follows a reply that never
# arrived, so a scan of the source is the guard. The scan also looks at the client's own file:
# nothing in it may call a write method either.

phase3 = pytest.mark.phase3

GITHUB_WRITE_METHODS = frozenset({"_post", "_patch", "_delete"})
# getattr(obj, "_post") reaches a method without naming it as an attribute.
ATTRIBUTE_BUILTINS = frozenset({"getattr", "hasattr", "setattr", "delattr"})
SIDE_EFFECTS = SRC / "longhaul" / "github" / "side_effects.py"
GITHUB_CLIENT = SRC / "longhaul" / "github" / "client.py"


def write_method_uses(source: str) -> list[str]:
    """One "line N: name" entry for each use of a write method of the GitHub client.

    A use is the attribute wherever it appears: called, handed on as a callback
    (functools.partial(client._post, path)), assigned, or reached by getattr with the name as
    a string. A definition (`def _post`) is not a use. A name that is computed cannot be
    compared, like in the scan for I1.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in GITHUB_WRITE_METHODS:
            found.append(f"line {node.lineno}: {node.attr}")
        elif isinstance(node, ast.Call) and _dotted(node.func) in ATTRIBUTE_BUILTINS:
            named = node.args[1] if len(node.args) > 1 else None
            if isinstance(named, ast.Constant) and named.value in GITHUB_WRITE_METHODS:
                found.append(f"line {node.lineno}: {named.value}")
    return sorted(found)


def writes_outside_side_effects(src: Path = SRC) -> dict[str, list[str]]:
    """{file: its uses of the write methods} for every module under src but side_effects.py."""
    allowed = src / "longhaul" / "github" / "side_effects.py"
    found = {
        path.relative_to(src).as_posix(): write_method_uses(path.read_text())
        for path in sorted((src / "longhaul").rglob("*.py"))
        if path != allowed
    }
    return {name: hits for name, hits in found.items() if hits}


@phase3
def test_github_writes_only_in_side_effects() -> None:
    # I4: only side_effects.py calls _post, _patch or _delete of the GitHub client.
    assert writes_outside_side_effects(SRC) == {}


@phase3
def test_the_write_scan_sees_the_methods_and_their_one_caller() -> None:
    # Without this, a client whose write methods were renamed, or a side_effects.py that no
    # longer used them, would pass the scan above for the wrong reason.
    defined = {
        node.name
        for node in ast.walk(ast.parse(GITHUB_CLIENT.read_text()))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    assert defined >= GITHUB_WRITE_METHODS
    used = {entry.partition(": ")[2] for entry in write_method_uses(SIDE_EFFECTS.read_text())}
    assert {"_post", "_delete"} <= used


@phase3
@pytest.mark.parametrize(
    "source",
    [
        "await client._post('/x', {})",
        "await self.client._delete(path)",
        "await gh._patch(path, body)",
        "send = client._post",
        "functools.partial(client._post, path)",
        "GitHubClient._post(client, path, body)",
        "client._delete = fake",
        "getattr(client, '_post')",
        "getattr(client, '_delete', None)",
        "hasattr(client, '_patch')",
        "setattr(client, '_post', fake)",
        "def f():\n    return client._post('/x', {})",
        "class A:\n    async def g(self):\n        await self._delete('/x')",
        "try:\n    pass\nexcept Exception:\n    client._patch('/x', {})",
    ],
)
def test_write_method_uses_finds_every_form(source: str) -> None:
    assert write_method_uses(source)


@phase3
@pytest.mark.parametrize(
    "source",
    [
        "",
        # Other names, including ones that start with a write method's name.
        "client.post('/x')",
        "client._get('/x')",
        "client._posted",
        "client._post_comment()",
        "client._deleted = True",
        "x._delete_all()",
        # A definition is not a use, and neither is a plain name.
        "def _post(self): ...",
        "async def _delete(self, path): ...",
        "class C:\n    _patch = None",
        "_post(x)",
        # A string is a use only as the name that getattr is asked for, and a computed name
        # cannot be compared.
        "getattr(client, name)",
        "getattr(client, '_get')",
        "getattr(client)",
        "print('_post')",
        "{'_post': 1}",
    ],
)
def test_write_method_uses_ignores_everything_else(source: str) -> None:
    assert write_method_uses(source) == []


@phase3
def test_write_method_uses_reports_the_line_and_the_name() -> None:
    source = "x = 1\nawait c._post('/a', {})\n\nfor n in ns:\n    await c._delete(n)\n"
    assert write_method_uses(source) == ["line 2: _post", "line 5: _delete"]


@phase3
def test_the_write_scan_flags_other_modules_and_lets_side_effects_through(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "longhaul/__init__.py": "",
            "longhaul/github/__init__.py": "",
            "longhaul/github/side_effects.py": "async def f(c):\n    await c._post('/x', {})\n",
            "longhaul/github/client.py": "class C:\n    async def _post(self, p, b): ...\n",
            "longhaul/durable/activities/github.py": "async def g(c):\n    await c._delete('/x')\n",
            "longhaul/github/intake.py": "def h(c):\n    return getattr(c, '_patch')\n",
            "longhaul/other.py": "x = 1\n",
        },
    )
    assert writes_outside_side_effects(tmp_path) == {
        "longhaul/durable/activities/github.py": ["line 2: _delete"],
        "longhaul/github/intake.py": ["line 2: _patch"],
    }
