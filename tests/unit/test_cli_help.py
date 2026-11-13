"""The --help of every longhaul command, as it reads on an 80-column terminal.

Typer keeps the line breaks inside the later paragraphs of a docstring, and the docstrings are
wrapped near 100 characters, so on a narrow terminal each of those lines came out as a full line
and a short one. The help of a command is one line per paragraph, and the terminal wraps it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from longhaul import cli

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

WIDTH = 80
# Rich styles help text when it detects CI (GITHUB_ACTIONS) or FORCE_COLOR.
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
STORY_ID = re.compile(r"\bLH-\d+")
PANEL = ("╭", "│", "╰")


def every_command(command: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    yield path
    for name, sub in getattr(command, "commands", {}).items():
        yield from every_command(sub, (*path, name))


ROOT = typer.main.get_command(cli.app)
PATHS = sorted(every_command(ROOT))


def command_at(path: tuple[str, ...]) -> Any:
    command: Any = ROOT
    for name in path:
        command = command.commands[name]
    return command


@pytest.fixture
def render(monkeypatch: pytest.MonkeyPatch) -> Any:
    # TERMINAL_WIDTH is read when Typer is imported and wins over COLUMNS, so pin both.
    monkeypatch.setattr("typer.rich_utils.MAX_WIDTH", WIDTH)

    def run(path: tuple[str, ...]) -> list[str]:
        result = CliRunner().invoke(cli.app, [*path, "--help"], env={"COLUMNS": str(WIDTH)})
        assert result.exit_code == 0, result.output
        return ANSI.sub("", result.output).splitlines()

    return run


def test_the_walk_finds_every_command_and_subcommand() -> None:
    names = {" ".join(path) for path in PATHS}
    assert {"", "run-local", "worker", "api", "run", "memory approve", "poller trigger"} <= names


@pytest.mark.parametrize("path", PATHS, ids=lambda path: " ".join(path) or "longhaul")
def test_the_help_fits_80_columns(render: Any, path: tuple[str, ...]) -> None:
    too_long = [line for line in render(path) if len(line.rstrip()) > WIDTH]
    assert too_long == []


@pytest.mark.parametrize("path", PATHS, ids=lambda path: " ".join(path) or "longhaul")
def test_the_help_names_no_story(render: Any, path: tuple[str, ...]) -> None:
    assert STORY_ID.findall("\n".join(render(path))) == []


@pytest.mark.parametrize("path", PATHS, ids=lambda path: " ".join(path) or "longhaul")
def test_each_paragraph_of_the_help_is_one_line(path: tuple[str, ...]) -> None:
    command = command_at(path)
    texts = [command.help or "", *(getattr(p, "help", None) or "" for p in command.params)]
    broken = [p for text in texts for p in text.split("\n\n") if "\n" in p.strip()]
    assert broken == []


@pytest.mark.parametrize("path", PATHS, ids=lambda path: " ".join(path) or "longhaul")
def test_no_line_of_the_text_stops_short(render: Any, path: tuple[str, ...]) -> None:
    # Outside the panels, a line followed by more of its paragraph is one the terminal wrapped,
    # so the next word could not have fitted on it. A few columns of slack cover the padding.
    lines = [line.rstrip() for line in render(path) if not line.strip().startswith(PANEL)]
    short = [
        (line, after)
        for line, after in zip(lines, lines[1:], strict=False)
        if line.strip() and after.strip() and len(line) + 1 + len(after.split()[0]) < WIDTH - 4
    ]
    assert short == []
