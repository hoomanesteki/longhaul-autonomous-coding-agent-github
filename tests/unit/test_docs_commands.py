"""The commands the docs show still exist and still parse.

A reader copies commands from the README, the runbook, the architecture page and CONTRIBUTING.md.
These tests read every shell block and every inline code span of those pages and check each
command against what it names: a `longhaul` command against the Typer app, `make <target>`
against the Makefile, a script against the file and its flags, a pytest path and marker against
the tests, and every LONGHAUL_* and LH_* variable against the settings and .env.example. A
renamed flag, target or variable then fails CI instead of the reader.
"""

import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import typer
import typer.main
from typer.testing import CliRunner

from longhaul.cli import app
from longhaul.github.commands import CommandKind
from longhaul.settings import EnvSettings

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
DOCS = ("README.md", "docs/runbook.md", "docs/architecture.md", "CONTRIBUTING.md")
SHELL_LANGS = {"bash", "sh", "shell", "console"}

# Variables that a script or the console reads from the process environment, so they are in
# neither the settings nor .env.example. Each one must still be read by the file named here.
READ_ELSEWHERE = {
    "LH_DEMO_PORT": "scripts/demo_stack.py",
    "LONGHAUL_API_URL": "console/vite.config.ts",
}

# A fence may be indented, as it is inside a list item. The closing fence has the same indent.
_FENCE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<lang>\S*)[^\n]*\n(?P<body>.*?)"
    r"^(?P=indent)(?P=fence)\s*$",
    re.M | re.S,
)
_SPAN = re.compile(r"`([^`\n]+)`")
_ASSIGNMENT = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=")
_ENV_NAME = re.compile(r"\b(?:LONGHAUL|LH)_[A-Z0-9_*]+")
# DIR, FILE, TASK_ID, OWNER/REPO, <id>, <the same key Keystone has>, `...`
_PLACEHOLDER = re.compile(r"[A-Z][A-Z0-9_]*(?:/[A-Z][A-Z0-9_]*)?|<[^>]*>|\.\.\.")
# An inline span that is a command. `longhaul <longhaul@users.noreply.github.com>` is an author,
# and `longhaul.contracts` a module, so a command word must follow `longhaul`.
_INLINE_COMMAND = re.compile(
    r"(?:uv run )?(?:(?:longhaul|make) [a-z]|python )|(?:\./)?scripts/\S+\."
)
_SEPARATORS = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")


@dataclass(frozen=True)
class Shown:
    """One command as a page shows it."""

    doc: str
    line: int
    words: tuple[str, ...]
    inline: bool

    def __str__(self) -> str:
        kind = "inline" if self.inline else "block"
        return f"{self.doc}:{self.line} ({kind}): {' '.join(self.words)}"


# ---------- reading the pages ----------


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def shell_blocks(text: str) -> Iterator[tuple[int, str]]:
    for match in _FENCE.finditer(text):
        if match["lang"] in SHELL_LANGS:
            indent = len(match["indent"])
            body = "\n".join(line[indent:] for line in match["body"].split("\n"))
            yield line_of(text, match.start("body")), body


def inline_spans(text: str) -> Iterator[tuple[int, str]]:
    # A fence is blanked out, not removed, so the line numbers of the spans stay right.
    prose = _FENCE.sub(lambda m: re.sub(r"[^\n]", " ", m[0]), text)
    for match in _SPAN.finditer(prose):
        yield line_of(prose, match.start()), match[1].strip()


def logical_lines(block: str) -> Iterator[tuple[int, str]]:
    """The lines of a shell block with backslash continuations joined, and their offsets."""
    start, pending = 0, ""
    for index, line in enumerate(block.splitlines()):
        if not pending:
            start = index
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        yield start, pending + line
        pending = ""
    if pending:
        yield start, pending


def split_commands(line: str) -> list[list[str]]:
    """The simple commands of one shell line, as words. Comments and $(...) are dropped."""
    line = re.sub(r"\$\([^)]*\)", "X", line)
    try:
        words = shlex.split(line, comments=True)
    except ValueError:
        return []
    commands: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if _SEPARATORS.fullmatch(word):
            commands.append(current)
            current = []
        else:
            current.append(word)
    commands.append(current)
    return [command for command in commands if command]


def block_commands(doc: str) -> Iterator[Shown]:
    text = (ROOT / doc).read_text(encoding="utf-8")
    for first, block in shell_blocks(text):
        for offset, line in logical_lines(block):
            # shlex keeps `&&` glued to a word when there is no space, so space it first.
            spaced = re.sub(r"(&&|\|\||;|\|)", r" \1 ", line)
            for words in split_commands(spaced):
                yield Shown(doc, first + offset, tuple(words), inline=False)


def inline_commands(doc: str) -> Iterator[Shown]:
    text = (ROOT / doc).read_text(encoding="utf-8")
    for line, span in inline_spans(text):
        if not _INLINE_COMMAND.match(span):
            continue
        # A bare `|` in a span is a synopsis (`memory approve|reject`), not a pipe.
        for words in split_commands(re.sub(r"(&&|;)", r" \1 ", span)):
            yield Shown(doc, line, tuple(words), inline=True)


@cache
def all_commands() -> tuple[Shown, ...]:
    return tuple(c for doc in DOCS for c in (*block_commands(doc), *inline_commands(doc)))


def without_assignments(words: tuple[str, ...]) -> tuple[str, ...]:
    """The command after any leading `NAME=value` words and an `export`."""
    rest = list(words)
    if rest[:1] == ["export"]:
        return ()
    while rest and _ASSIGNMENT.match(rest[0]):
        rest.pop(0)
    return tuple(rest)


def commands_of(head: str) -> list[Shown]:
    """The shown commands that run `head`, with `uv run` and assignments taken off."""
    found = []
    for shown in all_commands():
        words = without_assignments(shown.words)
        if words[:2] == ("uv", "run"):
            words = words[2:]
        if words[:1] == (head,):
            found.append(Shown(shown.doc, shown.line, words, shown.inline))
    return found


# ---------- the Typer app ----------
#
# Typer builds its commands on a click of its own, so these helpers read them by their
# attributes (commands, params, opts, make_context) and not by class.


def root_command() -> Any:
    command = typer.main.get_command(app)
    assert hasattr(command, "commands")
    return command


def resolve(words: list[str]) -> tuple[Any, list[str], list[str]]:
    """The command that `longhaul <words>` names, the names on the way to it, and its arguments."""
    command = root_command()
    path: list[str] = []
    while hasattr(command, "commands") and words and not words[0].startswith("-"):
        sub = command.commands.get(words[0])
        if sub is None:
            raise LookupError(f"no command {' '.join(['longhaul', *path, words[0]])!r}")
        path.append(words.pop(0))
        command = sub
    return command, path, words


def sample_value(param: Any) -> str:
    """A value that `param` accepts, to stand in for a placeholder such as DIR or TASK_ID."""
    annotation = param.type_descriptor.annotation
    info = param.type_descriptor.parameter_info
    if annotation is Path:
        dir_only = getattr(info, "dir_okay", True) and not getattr(info, "file_okay", True)
        return str(ROOT / "docs") if dir_only else str(ROOT / "README.md")
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return str(next(iter(annotation)).value)
    if annotation in (int, float):
        return "1"
    return "placeholder"


def fill_placeholders(command: Any, args: list[str]) -> list[str]:
    options = {name: p for p in command.params for name in getattr(p, "opts", [])}
    positionals = [p for p in command.params if p.param_type_name == "argument"]
    filled: list[str] = []
    for index, arg in enumerate(args):
        before = args[index - 1] if index else ""
        takes_value = before in options and not options[before].is_flag
        if not _PLACEHOLDER.fullmatch(arg):
            filled.append(arg)
            if not arg.startswith("-") and not takes_value:
                positionals = positionals[1:]
            continue
        param = options[before] if takes_value else (positionals.pop(0) if positionals else None)
        filled.append(sample_value(param) if param is not None else arg)
    return filled


def synopsis_variants(words: tuple[str, ...]) -> list[list[str]]:
    """`memory approve|reject [--by NAME]` read as the commands it stands for."""
    plain = [word.strip("[]") for word in words if word.strip("[]")]
    variants: list[list[str]] = [[]]
    for word in plain:
        choices = word.split("|") if re.fullmatch(r"[a-z-]+(\|[a-z-]+)+", word) else [word]
        variants = [[*variant, choice] for variant in variants for choice in choices]
    return variants


def parse_problem(shown: Shown) -> str | None:
    """Why the app would refuse the shown `longhaul` command, or None when it parses."""
    for words in synopsis_variants(shown.words[1:]):
        try:
            command, path, args = resolve(list(words))
            known = {name for p in command.params for name in [*p.opts, *p.secondary_opts]}
            unknown = [a for a in args if a.startswith("--") and a.split("=")[0] not in known]
            if unknown:
                return f"{shown}: no option {unknown[0]} for {' '.join(['longhaul', *path])}"
            if shown.inline and not args:
                # Prose names a command (`longhaul status`) without its arguments.
                continue
            # Inline spans are often a synopsis, so a missing argument is not a problem there.
            command.make_context(
                " ".join(["longhaul", *path]),
                fill_placeholders(command, args),
                resilient_parsing=shown.inline,
            )
        except (LookupError, typer.TyperException) as error:
            return f"{shown}: {error}"
    return None


def test_the_extraction_finds_the_commands_of_the_pages() -> None:
    # Guards the guard: without these the checks below would pass on nothing.
    shown = {" ".join(c.words) for c in all_commands()}
    assert "make setup" in shown and "make demo" in shown
    assert "uv run longhaul worker" in shown
    assert "uv run longhaul poller install" in shown
    assert any(c.startswith("uv run longhaul run --path tests/fixtures/repos/calc") for c in shown)
    assert "longhaul cancel TASK_ID [--reason TEXT]" in shown
    assert "longhaul memory approve|reject" in shown
    assert len(commands_of("longhaul")) >= 30
    assert len(commands_of("make")) >= 30


def test_every_page_gives_the_check_some_commands() -> None:
    # A page whose commands the reader cannot see (a list instead of a block, an unread fence)
    # would pass every check below without being checked at all.
    docs = {c.doc for c in all_commands()}
    assert docs == set(DOCS)


def test_a_fence_inside_a_list_item_is_read() -> None:
    # The README shows the worker and the poller inside the steps of "Run on your repo".
    readme = {" ".join(c.words) for c in block_commands("README.md")}
    assert "uv run longhaul worker" in readme
    assert "uv run longhaul poller install" in readme
    text = "1. Start it:\n\n   ```bash\n   make demo\n   ```\n"
    assert list(shell_blocks(text)) == [(4, "make demo\n")]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "make setup && make up && make demo",
            [["make", "setup"], ["make", "up"], ["make", "demo"]],
        ),
        ("make check          # lint, format check", [["make", "check"]]),
        ("export TOKEN=$(openssl rand -hex 32)    # or put it in .env", [["export", "TOKEN=X"]]),
        (
            'uv run pytest -m "chaos and not chaos_lite"',
            [["uv", "run", "pytest", "-m", "chaos and not chaos_lite"]],
        ),
        ("curl -s url | head -c 400", [["curl", "-s", "url"], ["head", "-c", "400"]]),
    ],
)
def test_a_shell_line_splits_into_its_commands(text: str, expected: list[list[str]]) -> None:
    assert split_commands(re.sub(r"(&&|\|\||;|\|)", r" \1 ", text)) == expected


def test_continuation_lines_are_one_command() -> None:
    block = "uv run longhaul run \\\n  --path a \\\n  --issue-file b\nmake up\n"
    assert list(logical_lines(block)) == [
        (0, "uv run longhaul run    --path a    --issue-file b"),
        (3, "make up"),
    ]


def every_command_path(group: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    for name, command in group.commands.items():
        yield (*path, name)
        if hasattr(command, "commands"):
            yield from every_command_path(command, (*path, name))


@pytest.mark.parametrize("path", sorted(every_command_path(root_command())), ids=" ".join)
def test_every_subcommand_answers_help(path: tuple[str, ...]) -> None:
    result = CliRunner().invoke(app, [*path, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_every_longhaul_command_the_docs_show_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    # The docs give paths relative to the repo root, and the app checks that they exist.
    monkeypatch.chdir(ROOT)
    problems = [p for shown in commands_of("longhaul") if (p := parse_problem(shown))]
    assert problems == []


@pytest.mark.parametrize(
    "words",
    [
        ("longhaul", "rn", "--path", "DIR"),
        ("longhaul", "run", "--paht", "DIR", "--issue-file", "FILE"),
        ("longhaul", "run", "--path", "DIR"),
        ("longhaul", "cancel", "TASK_ID", "--why", "text"),
        ("longhaul", "memory", "forget", "ID"),
        ("longhaul", "api", "--port", "many"),
        ("longhaul", "status", "a", "b"),
    ],
    ids=[
        "unknown-command",
        "unknown-option",
        "missing-option",
        "unknown-cancel-option",
        "unknown-subcommand",
        "bad-int",
        "extra-argument",
    ],
)
def test_the_parse_check_notices_a_command_that_drifted(
    words: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(ROOT)
    assert parse_problem(Shown("README.md", 1, words, inline=False))


def test_the_parse_check_reads_a_synopsis_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    for words in [
        ("longhaul", "cancel", "TASK_ID", "[--reason", "TEXT]"),
        ("longhaul", "memory", "approve|reject"),
        ("longhaul", "status"),
        ("longhaul", "memory"),
    ]:
        assert parse_problem(Shown("README.md", 1, words, inline=True)) is None, words
    unknown = ("longhaul", "poller", "pause", "[--reason", "TEXT]")
    assert parse_problem(Shown("README.md", 1, unknown, inline=True))


# ---------- issue comments ----------


def test_every_issue_command_the_docs_show_is_one_the_poller_knows() -> None:
    verbs = {kind.value for kind in CommandKind}
    problems = []
    for doc in DOCS:
        text = (ROOT / doc).read_text(encoding="utf-8")
        for match in re.finditer(r"/longhaul ([a-z]+)", text):
            if match[1] not in verbs:
                problems.append(f"{doc}:{line_of(text, match.start())}: /longhaul {match[1]}")
    assert problems == []


# ---------- make ----------


@cache
def makefile() -> str:
    return (ROOT / "Makefile").read_text(encoding="utf-8")


def make_targets(text: str) -> set[str]:
    return set(re.findall(r"^([A-Za-z][\w-]*)\s*:(?!=)", text, re.M))


# Make targets that the pages name for the repo a task works on, not for this one.
OTHER_REPOS = {("docs/runbook.md", ("make", "test"))}


def make_problem(shown: Shown, text: str) -> str | None:
    if (shown.doc, shown.words) in OTHER_REPOS:
        return None
    words = shown.words[1:]
    targets = [w for w in words if not w.startswith("-") and "=" not in w]
    variables = [w.split("=", 1)[0] for w in words if _ASSIGNMENT.match(w)]
    if not targets and not shown.inline:
        return f"{shown}: no target"
    missing = [t for t in targets if t not in make_targets(text)]
    unused = [v for v in variables if f"$({v})" not in text]
    if missing or unused:
        return f"{shown}: no target {missing}, unused variable {unused}"
    return None


def test_every_make_target_the_docs_show_exists() -> None:
    problems = [p for shown in commands_of("make") if (p := make_problem(shown, makefile()))]
    assert problems == []


def test_the_make_check_notices_a_missing_target_or_variable() -> None:
    text = "PHASE ?= 0\n\ngate: check\n\techo $(PHASE)\ncheck:\n\ttrue\n"
    assert make_targets(text) == {"gate", "check"}
    assert make_problem(Shown("README.md", 1, ("make", "gate", "PHASE=9"), False), text) is None
    assert make_problem(Shown("README.md", 1, ("make", "test"), False), text)
    assert make_problem(Shown("README.md", 1, ("make", "gate", "STAGE=9"), False), text)


# ---------- scripts and the paths they take ----------


def script_of(words: tuple[str, ...]) -> str | None:
    """The repo path that a command runs: `python eval/run.py`, `./scripts/demo.sh`."""
    if words[:1] == ("python",) and len(words) > 1 and words[1].endswith(".py"):
        return words[1]
    if words and re.match(r"(?:\./)?(?:scripts|eval)/\S+\.(?:sh|py)$", words[0]):
        return words[0].removeprefix("./")
    return None


def script_commands() -> list[tuple[Shown, str]]:
    found = []
    for shown in all_commands():
        words = without_assignments(shown.words)
        if words[:2] == ("uv", "run"):
            words = words[2:]
        script = script_of(words)
        if script is not None:
            found.append((Shown(shown.doc, shown.line, words, shown.inline), script))
    return found


@cache
def help_text(script: str) -> str:
    result = subprocess.run(
        [sys.executable, script, "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert result.returncode == 0, f"{script} --help: {result.stderr}"
    return result.stdout


def test_every_script_the_docs_run_exists_and_takes_the_flags_shown() -> None:
    problems = []
    found = script_commands()
    assert len(found) >= 10
    for shown, script in found:
        path = ROOT / script
        if not path.is_file():
            problems.append(f"{shown}: {script} does not exist")
            continue
        if not script.endswith(".py") and not os.access(path, os.X_OK):
            problems.append(f"{shown}: {script} is not executable")
        flags = [w.split("=", 1)[0] for w in shown.words if w.startswith("--")]
        if flags and script.endswith(".py"):
            text = help_text(script)
            problems += [
                f"{shown}: {script} has no {flag}"
                for flag in flags
                if not re.search(rf"{re.escape(flag)}\b", text)
            ]
    assert problems == []


def test_every_script_the_pages_name_exists() -> None:
    problems = []
    for doc in DOCS:
        text = (ROOT / doc).read_text(encoding="utf-8")
        for line, span in inline_spans(text):
            for name in re.findall(r"(?<![\w./-])(?:\./)?scripts/[\w./-]+\.(?:sh|py)\b", span):
                if not (ROOT / name.removeprefix("./")).is_file():
                    problems.append(f"{doc}:{line}: {name}")
    assert problems == []


# ---------- pytest ----------


def registered_markers() -> set[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {entry.split(":", 1)[0] for entry in config["tool"]["pytest"]["ini_options"]["markers"]}


def pytest_problems(shown: Shown) -> list[str]:
    words = list(shown.words[1:])
    problems = []
    markers = registered_markers()
    while words:
        word = words.pop(0)
        if word in ("-m", "-k") and words:
            expression = words.pop(0)
            names = set(re.findall(r"[A-Za-z_]\w*", expression)) - {"and", "or", "not"}
            if word == "-m":
                problems += [
                    f"{shown}: marker {name} is not registered" for name in names - markers
                ]
            continue
        if not word.startswith("-") and not (ROOT / word.split("::", 1)[0]).exists():
            problems.append(f"{shown}: {word} does not exist")
    return problems


def test_every_pytest_run_the_docs_show_names_real_tests_and_markers() -> None:
    found = commands_of("pytest")
    assert len(found) >= 4
    assert [p for shown in found for p in pytest_problems(shown)] == []


def test_the_pytest_check_notices_a_missing_path_or_marker() -> None:
    gone = Shown("README.md", 1, ("pytest", "tests/unit/test_gone.py"), False)
    assert pytest_problems(gone)
    assert pytest_problems(Shown("README.md", 1, ("pytest", "-m", "chaos and not quick"), False))
    assert (
        pytest_problems(Shown("README.md", 1, ("pytest", "-m", "chaos_lite", "tests/unit"), False))
        == []
    )


# ---------- environment variables ----------


def settings_variables() -> set[str]:
    prefix = str(EnvSettings.model_config.get("env_prefix", ""))
    names = set()
    for name, field in EnvSettings.model_fields.items():
        alias = field.validation_alias
        names.add(alias if isinstance(alias, str) else f"{prefix}{name}".upper())
    return names


def env_example_variables() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M))


def known_variables() -> set[str]:
    return settings_variables() | env_example_variables() | set(READ_ELSEWHERE)


def unknown_variables(text: str, known: set[str]) -> set[str]:
    unknown = set()
    for name in set(_ENV_NAME.findall(text)):
        if "*" in name:
            pattern = re.compile(re.escape(name).replace(r"\*", "[A-Z0-9_]+"))
            if not any(pattern.fullmatch(k) for k in known):
                unknown.add(name)
        elif name not in known:
            unknown.add(name)
    return unknown


@pytest.mark.parametrize("doc", DOCS)
def test_every_variable_the_page_names_is_a_setting_or_in_env_example(doc: str) -> None:
    text = (ROOT / doc).read_text(encoding="utf-8")
    assert unknown_variables(text, known_variables()) == set()


def test_the_variables_read_elsewhere_are_read_there() -> None:
    for name, path in READ_ELSEWHERE.items():
        assert name in (ROOT / path).read_text(encoding="utf-8"), f"{path} no longer reads {name}"
        assert name not in known_variables() - set(READ_ELSEWHERE), f"{name} is a setting now"


def test_every_setting_is_in_env_example() -> None:
    # .env.example is where a reader finds the settings, so a new one goes there too.
    assert settings_variables() - env_example_variables() == set()


def test_the_variable_check_notices_an_unknown_name() -> None:
    known = {"LONGHAUL_DATABASE_URL", "LH_POSTGRES_PORT"}
    assert unknown_variables("set `LONGHAUL_DATABSE_URL`", known) == {"LONGHAUL_DATABSE_URL"}
    assert unknown_variables("the `LH_*_PORT` values", known) == set()
    assert unknown_variables("the `LONGHAUL_BLOB_*` values", known) == {"LONGHAUL_BLOB_*"}
