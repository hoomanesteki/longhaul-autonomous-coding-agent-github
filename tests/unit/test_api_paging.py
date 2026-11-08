"""Cursors and pages of text for the console API (LH-801).

A cursor is opaque to the console: it hands back what the last page gave it. The pages of text
are the lines of a tool output, counted the way `read_output` counts them.
"""

from __future__ import annotations

import pytest

from longhaul.api import paging

pytestmark = [pytest.mark.unit, pytest.mark.phase8]


def test_a_cursor_returns_the_parts_it_was_made_from() -> None:
    cursor = paging.encode_cursor(["2026-11-03T09:00:00+00:00", 1, "toolu_01"])

    assert paging.decode_cursor(cursor, 3) == ["2026-11-03T09:00:00+00:00", 1, "toolu_01"]


def test_a_cursor_is_safe_in_a_query_string() -> None:
    cursor = paging.encode_cursor(["a/b+c=d", "é" * 40])

    assert all(ch.isalnum() or ch in "-_" for ch in cursor)
    assert paging.decode_cursor(cursor, 2) == ["a/b+c=d", "é" * 40]


@pytest.mark.parametrize(
    "bad",
    ["", "not a cursor", "bm90LWpzb24", paging.encode_cursor(["only one"]), "W10"],
)
def test_a_cursor_that_is_not_ours_is_refused(bad: str) -> None:
    with pytest.raises(paging.CursorError):
        paging.decode_cursor(bad, 3)


def test_a_cursor_with_parts_of_the_wrong_type_is_refused() -> None:
    cursor = paging.encode_cursor([None, {"a": 1}])  # type: ignore[list-item]

    with pytest.raises(paging.CursorError):
        paging.decode_cursor(cursor, 2)


def text(count: int) -> str:
    return "\n".join(f"line {n}" for n in range(1, count + 1)) + "\n"


def test_a_page_of_the_whole_text() -> None:
    page = paging.slice_lines(text(3), 1, None)

    assert (page.start_line, page.end_line, page.total_lines) == (1, 3, 3)
    assert page.text == "line 1\nline 2\nline 3"
    assert page.next_start is None


def test_a_page_of_a_range_counts_the_end_line() -> None:
    page = paging.slice_lines(text(10), 4, 6)

    assert page.text == "line 4\nline 5\nline 6"
    assert (page.start_line, page.end_line, page.total_lines) == (4, 6, 10)
    assert page.next_start is None


def test_an_end_past_the_text_stops_at_the_last_line() -> None:
    page = paging.slice_lines(text(5), 4, 99)

    assert page.text == "line 4\nline 5"
    assert page.end_line == 5


def test_a_long_text_is_cut_at_the_line_limit_and_says_where_to_go_on() -> None:
    page = paging.slice_lines(text(paging.MAX_LINES + 50), 1, None)

    assert page.end_line == paging.MAX_LINES
    assert page.next_start == paging.MAX_LINES + 1
    assert page.total_lines == paging.MAX_LINES + 50


def test_a_long_text_is_cut_at_the_size_limit_on_a_line() -> None:
    wide = "\n".join(["x" * 1000] * 400)

    page = paging.slice_lines(wide, 1, None)

    assert len(page.text) <= paging.MAX_CHARS
    assert page.end_line == len(page.text.split("\n"))
    assert page.next_start == page.end_line + 1


def test_one_line_over_the_size_limit_is_cut_inside_the_line() -> None:
    page = paging.slice_lines("y" * (paging.MAX_CHARS * 2) + "\nnext", 1, None)

    assert len(page.text) == paging.MAX_CHARS
    assert (page.start_line, page.end_line) == (1, 1)
    assert page.next_start == 2


def test_an_empty_text_is_a_page_with_no_lines() -> None:
    page = paging.slice_lines("", 1, None)

    assert (page.text, page.start_line, page.end_line, page.total_lines) == ("", 1, 0, 0)
    assert page.next_start is None


def test_a_start_past_the_end_is_refused() -> None:
    with pytest.raises(paging.RangeError):
        paging.slice_lines(text(3), 4, None)
    with pytest.raises(paging.RangeError):
        paging.slice_lines("", 2, None)


def test_an_end_before_the_start_is_refused() -> None:
    with pytest.raises(paging.RangeError):
        paging.slice_lines(text(3), 3, 2)
