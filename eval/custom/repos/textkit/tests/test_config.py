from pathlib import Path

from textkit.config import load_defaults


def test_load_defaults_reads_a_toml_file(tmp_path: Path) -> None:
    path = tmp_path / "textkit.toml"
    path.write_text('width = 60\nsuffix = "..."\n', encoding="utf-8")
    assert load_defaults(path) == {"width": 60, "suffix": "..."}


def test_load_defaults_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_defaults(tmp_path / "missing.toml") == {}
