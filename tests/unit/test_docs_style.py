import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]
STYLE_DOC = ROOT / "docs" / "style.md"

# Banned phrases from the voice guide (plan section 5.10). "em dashes" and "emoji"
# are checked separately: dashes by test_no_long_dashes, emoji by character.
BANNED_PHRASES = [
    "robust",
    "seamless",
    "seamlessly",
    "leverage",
    "delve",
    "comprehensive",
    "cutting-edge",
    "state-of-the-art",
    "game-changer",
    "unlock",
    "empower",
    "elevate",
    "harness the power",
    "supercharge",
    "in today's fast-paced",
    "it's worth noting",
    "furthermore",
    "moreover",
    "plethora",
    "a testament to",
    "navigate the complexities",
    "best-in-class",
]

LONG_DASHES = ("—", "–")


def _checked_files() -> list[Path]:
    files = [ROOT / "README.md"]
    for path in sorted((ROOT / "docs").rglob("*.md")):
        if path.relative_to(ROOT / "docs").parts[0] == "plan":
            continue
        files.append(path)
    return files


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_no_banned_phrases() -> None:
    patterns = {
        phrase: re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)
        for phrase in BANNED_PHRASES
    }
    hits: list[str] = []
    for path in _checked_files():
        if path == STYLE_DOC:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for phrase, pattern in patterns.items():
                if pattern.search(line):
                    hits.append(f"{_rel(path)}:{lineno}: {phrase}")
    assert not hits, "banned phrases found:\n" + "\n".join(hits)


def test_no_long_dashes() -> None:
    hits: list[str] = []
    for path in _checked_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(dash in line for dash in LONG_DASHES):
                hits.append(f"{_rel(path)}:{lineno}")
    assert not hits, "em or en dashes found:\n" + "\n".join(hits)
