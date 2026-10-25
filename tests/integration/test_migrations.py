import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

pytestmark = [pytest.mark.integration, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]
DSN = os.environ.get(
    "LONGHAUL_DATABASE_URL", "postgresql://longhaul:longhaul@localhost:5432/longhaul"
)


def _tables() -> set[str]:
    with psycopg.connect(DSN) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    return {r[0] for r in rows}


def test_upgrade_downgrade_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", DSN)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
    wanted = {"repo_settings", "tasks"}

    command.upgrade(cfg, "head")
    assert wanted <= _tables()

    command.upgrade(cfg, "head")
    assert wanted <= _tables()

    command.downgrade(cfg, "base")
    assert not wanted & _tables()

    command.upgrade(cfg, "head")
    assert wanted <= _tables()
