"""Integration test fixtures: real PostgreSQL via pytest-postgresql.

These fixtures are scoped to the integration/ directory so that unit tests
can run without libpq installed.
"""
from __future__ import annotations

from pathlib import Path

import psycopg2
import pytest

# pytest-postgresql provides the `postgresql` fixture via this plugin
pytest_plugins = ["pytest_postgresql.plugin"]


@pytest.fixture(scope="session")
def db_migrations_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "db" / "migrations"


@pytest.fixture(scope="function")
def pg_conn(postgresql):
    """Yield a psycopg2 connection to the test PostgreSQL instance.

    Applies all migrations from db/migrations/ before yielding.
    Rolls back after each test for isolation.
    """
    conn = psycopg2.connect(
        host=postgresql.info.host,
        port=postgresql.info.port,
        user=postgresql.info.user,
        dbname=postgresql.info.dbname,
    )
    migrations_dir = Path(__file__).resolve().parents[2] / "db" / "migrations"
    _apply_migrations(conn, migrations_dir)
    yield conn
    conn.rollback()
    conn.close()


def _apply_migrations(conn, migrations_dir: Path) -> None:
    sql_files = sorted(migrations_dir.glob("*.sql"))
    with conn.cursor() as cur:
        for sql_file in sql_files:
            cur.execute(sql_file.read_text(encoding="utf-8"))
    conn.commit()
