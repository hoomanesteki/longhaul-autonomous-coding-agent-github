"""Slash commands in issue comments (LH-308).

parse_command is pure, so every case here is a plain string. The rule is that a comment is a
command only when its first line is one. That keeps a command that someone quotes, pastes or
explains from steering a task.
"""

import pytest

from longhaul.github.commands import (
    MAX_TEXT_CHARS,
    Command,
    CommandKind,
    may_steer,
    parse_command,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase3]


def test_parse_all() -> None:
    assert parse_command("/longhaul approve") == Command(CommandKind.APPROVE)
    assert parse_command("/longhaul reject use a loop, not recursion") == Command(
        CommandKind.REJECT, text="use a loop, not recursion"
    )
    assert parse_command("/longhaul hint the bug is in add()") == Command(
        CommandKind.HINT, text="the bug is in add()"
    )
    assert parse_command("/longhaul budget +5") == Command(CommandKind.BUDGET, usd=5.0)
    assert parse_command("/longhaul budget +2.50 turns +20") == Command(
        CommandKind.BUDGET, usd=2.5, turns=20
    )
    assert parse_command("/longhaul cancel") == Command(CommandKind.CANCEL)
    assert parse_command("/longhaul status") == Command(CommandKind.STATUS)
    assert parse_command("/longhaul retry") == Command(CommandKind.RETRY)


@pytest.mark.parametrize(
    "body",
    [
        "/LONGHAUL APPROVE",
        "/LongHaul Approve",
        "  /longhaul approve",
        "\n\n/longhaul approve",
        "/longhaul   approve   ",
        "/longhaul\tapprove",
        "/longhaul approve\r\nthanks for the quick work",
        "/longhaul approve\nthe plan looks fine to me",
    ],
)
def test_the_form_of_a_command_is_forgiving(body: str) -> None:
    assert parse_command(body) == Command(CommandKind.APPROVE)


def test_only_the_first_line_counts() -> None:
    assert parse_command("/longhaul hint look at add()\nand at sub()") == Command(
        CommandKind.HINT, text="look at add()"
    )
    assert parse_command("Thanks!\n/longhaul approve") is None
    assert parse_command("/longhaul approve\n/longhaul cancel") == Command(CommandKind.APPROVE)


def test_cancel_takes_an_optional_reason() -> None:
    assert parse_command("/longhaul cancel wrong issue") == Command(
        CommandKind.CANCEL, text="wrong issue"
    )


def test_the_text_of_a_command_keeps_its_case_and_inner_spacing() -> None:
    parsed = parse_command("/longhaul HINT Use  `Decimal`, not float")

    assert parsed == Command(CommandKind.HINT, text="Use  `Decimal`, not float")


def test_a_text_is_cut_to_the_limit() -> None:
    parsed = parse_command("/longhaul hint " + "x" * (MAX_TEXT_CHARS + 500))

    assert parsed is not None
    assert parsed.text == "x" * MAX_TEXT_CHARS


@pytest.mark.parametrize(
    ("body", "usd", "turns"),
    [
        ("/longhaul budget +5", 5.0, 0),
        ("/longhaul budget +$5", 5.0, 0),
        ("/longhaul budget +0.25", 0.25, 0),
        ("/longhaul budget +10 turns +30", 10.0, 30),
        ("/longhaul budget +10 TURNS +30", 10.0, 30),
        ("/longhaul budget +3.5 turns +1", 3.5, 1),
    ],
)
def test_a_budget_is_dollars_and_optionally_turns(body: str, usd: float, turns: int) -> None:
    assert parse_command(body) == Command(CommandKind.BUDGET, usd=usd, turns=turns)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "\n\n",
        "looks good to me",
        "/longhaul",
        "/longhaul   ",
        "/longhaul fly",
        "/longhaul approved",
        "/longhaulx approve",
        "/long-haul approve",
        "longhaul approve",
        "please /longhaul approve",
        "@longhaul approve",
        "/longhaul approve now",
        "/longhaul approve.",
        "/longhaul status please",
        "/longhaul retry later",
        "/longhaul reject",
        "/longhaul reject   ",
        "/longhaul hint",
        "/longhaul budget",
        "/longhaul budget 5",
        "/longhaul budget -5",
        "/longhaul budget +0",
        "/longhaul budget +abc",
        "/longhaul budget +5 turns",
        "/longhaul budget +5 turns 20",
        "/longhaul budget +5 turns +0",
        "/longhaul budget +5 turns +2.5",
        "/longhaul budget +5 extra",
        "/longhaul budget +1e3",
        "/longhaul budget +inf",
    ],
)
def test_junk_is_not_a_command(body: str) -> None:
    assert parse_command(body) is None


@pytest.mark.parametrize(
    "body",
    [
        "```\n/longhaul cancel\n```",
        "```text\n/longhaul cancel\n```",
        "~~~\n/longhaul cancel\n~~~",
        "````\n/longhaul cancel\n````",
        "```\n/longhaul cancel",
        "Run this to stop it:\n```\n/longhaul cancel\n```",
        "```\n/longhaul cancel\n```\n/longhaul approve",
        "`/longhaul cancel`",
        "``/longhaul cancel``",
        "    /longhaul cancel",
        "\t/longhaul cancel",
        "> /longhaul cancel",
        ">/longhaul cancel",
        "- /longhaul cancel",
        "* /longhaul cancel",
        "1. /longhaul cancel",
        "**/longhaul cancel**",
    ],
)
def test_ignores_code_blocks(body: str) -> None:
    assert parse_command(body) is None


def test_inline_code_inside_the_text_of_a_command_is_kept() -> None:
    assert parse_command("/longhaul hint call `add()` first") == Command(
        CommandKind.HINT, text="call `add()` first"
    )


def test_a_command_after_a_closed_fence_is_not_the_first_line() -> None:
    # The rule is "the first line", not "the first line outside a code block". A comment that
    # opens with a pasted log is a log with a note, not an order.
    assert parse_command("```\ntraceback\n```\n/longhaul approve") is None


@pytest.mark.parametrize("kind", list(CommandKind))
def test_every_kind_has_a_name_that_a_person_types(kind: CommandKind) -> None:
    assert kind.value == kind.name.lower()
    assert kind.value.isalpha()


@pytest.mark.parametrize(
    ("answer", "may"),
    [
        ({"permission": "admin", "role_name": "admin"}, True),
        ({"permission": "write", "role_name": "maintain"}, True),
        ({"permission": "write", "role_name": "write"}, True),
        ({"permission": "write"}, True),
        ({"role_name": "maintain"}, True),
        ({"permission": "Admin"}, True),
        ({"permission": "read", "role_name": "triage"}, False),
        ({"permission": "read", "role_name": "read"}, False),
        ({"permission": "none"}, False),
        ({"permission": "read", "role_name": "custom-role-on-write"}, False),
        ({}, False),
        ({"permission": None, "role_name": None}, False),
        ({"permission": ["admin"], "role_name": 3}, False),
    ],
)
def test_only_admin_maintain_and_write_may_steer(answer: dict[str, object], may: bool) -> None:
    assert may_steer(answer) is may
