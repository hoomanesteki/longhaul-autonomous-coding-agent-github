"""Shaping a long tool output into a head, a tail and a handle (LH-402)."""

import pytest

from longhaul.context.tokens import estimate_tokens
from longhaul.context.truncate import (
    HEAD_LINES,
    TAIL_LINES,
    format_size,
    shape_output,
    split_lines,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase4]

HANDLE = "out_toolu_01"
CAP = 4000


def marker(lines: int, size: str, handle: str = HANDLE) -> str:
    unit = "line" if lines == 1 else "lines"
    return (
        f"[output truncated: {lines} {unit}, {size}. Full text: handle {handle}. "
        "Use read_output to page or filter.]"
    )


def numbered_lines(count: int, width: int = 100) -> str:
    """`count` lines of exactly `width` bytes each, the newline included."""
    return "".join(f"{n:05d} ".ljust(width - 1, "x") + "\n" for n in range(1, count + 1))


def test_shape_large_output() -> None:
    # Ten thousand lines of 100 bytes each: one million bytes, far over the cap.
    full = numbered_lines(10_000)
    assert len(full.encode()) == 1_000_000

    shaped = shape_output(full, CAP, HANDLE)

    assert estimate_tokens(shaped) <= CAP
    rows = shaped.split("\n")
    source = full.split("\n")
    # The head, then the marker, then the tail: 60 + 1 + 60 rows.
    assert (HEAD_LINES, TAIL_LINES) == (60, 60)
    assert len(rows) == 121
    assert rows[:60] == source[:60]
    assert rows[60] == marker(10_000, "976.6 KB")
    assert rows[61:] == source[9_940:10_000]
    # The lines in between are gone from what the model sees.
    assert "05000 " not in shaped


def test_output_under_the_cap_is_returned_as_it_is() -> None:
    text = numbered_lines(100)
    assert estimate_tokens(text) <= CAP
    assert shape_output(text, CAP, HANDLE) is text
    assert shape_output("", CAP, HANDLE) == ""


def test_the_cap_is_the_token_estimate_of_the_whole_text() -> None:
    at_the_cap = "x" * int(CAP * 3.5)
    assert estimate_tokens(at_the_cap) == CAP
    assert shape_output(at_the_cap, CAP, HANDLE) == at_the_cap
    over = shape_output(at_the_cap + "x", CAP, HANDLE)
    assert over.endswith("]")
    assert estimate_tokens(over) <= CAP


def test_lines_that_fit_after_the_cut_are_kept_whole() -> None:
    # 200 lines of 50 bytes are 10,000 bytes, about 2,900 tokens. The cap is 2,000, and the
    # 120 lines that are kept take about 1,700 together with the marker.
    full = numbered_lines(200, width=50)
    shaped = shape_output(full, 2000, HANDLE)
    rows = shaped.split("\n")
    source = full.split("\n")
    assert len(rows) == 121
    assert rows[:60] == source[:60]
    assert rows[60] == marker(200, "9.8 KB")
    assert rows[61:] == source[140:200]
    assert estimate_tokens(shaped) <= 2000


def test_long_lines_are_cut_by_characters_so_the_result_still_fits() -> None:
    # 100 lines of 1,000 characters: no line to leave out, yet far over the cap.
    full = "".join(f"{n:03d}" + "y" * 997 + "\n" for n in range(100))
    shaped = shape_output(full, CAP, HANDLE)
    assert estimate_tokens(shaped) <= CAP
    before, found, after = shaped.partition(marker(100, "97.8 KB"))
    assert found
    # The start of the output is before the marker and its end is after it.
    assert before.startswith("000yyy")
    assert after.endswith("y" * 50)
    assert "099" in after
    assert "050" not in shaped


def test_one_enormous_line_keeps_its_start_and_the_marker() -> None:
    full = "z" * (3 * 1024 * 1024)
    shaped = shape_output(full, CAP, HANDLE)
    assert estimate_tokens(shaped) <= CAP
    assert shaped.startswith("zzzz")
    assert shaped.endswith("\n" + marker(1, "3.0 MB"))


def test_a_cap_too_small_for_the_marker_gives_the_marker_alone() -> None:
    shaped = shape_output(numbered_lines(1_000), 5, HANDLE)
    assert shaped == marker(1_000, "97.7 KB")


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 bytes"),
        (1, "1 byte"),
        (900, "900 bytes"),
        (1023, "1023 bytes"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024 - 1, "1.0 MB"),
        (1024 * 1024, "1.0 MB"),
        (5 * 1024 * 1024 + 512 * 1024, "5.5 MB"),
    ],
)
def test_the_size_is_given_in_the_unit_a_person_reads(size: int, text: str) -> None:
    assert format_size(size) == text


def test_the_size_in_the_marker_is_in_bytes_not_characters() -> None:
    # 1,000 characters of three bytes each. A cap of one token leaves room for the marker only.
    shaped = shape_output("€" * 1000, 1, HANDLE)
    assert shaped == marker(1, "2.9 KB")


def test_lines_are_counted_the_way_read_output_pages_them() -> None:
    # Only "\n" ends a line, a CRLF ending loses its carriage return, and a final newline does
    # not start another line. The marker and read_output have to agree on the count.
    assert split_lines("a\r\nb\r\nc\n") == ["a", "b", "c"]
    crlf = "".join(f"row {n}\r\n" for n in range(1, 301)) * 21
    shaped = shape_output(crlf, 2000, HANDLE)
    rows = shaped.split("\n")
    assert rows[0] == "row 1"
    assert rows[60] == marker(6_300, "53.2 KB")
    assert "\r" not in shaped


def test_the_handle_is_written_into_the_marker_as_given() -> None:
    shaped = shape_output(numbered_lines(1_000), 500, "out_call-7")
    assert "Full text: handle out_call-7." in shaped
