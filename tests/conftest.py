"""pytest fixtures: real PostgreSQL via pytest-postgresql, mock FinMind client."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock

import pandas as pd
import psycopg2
import pytest

# ---------------------------------------------------------------------------
# PostgreSQL fixtures (real DB — no mocks per project testing policy)
# ---------------------------------------------------------------------------

# pytest-postgresql fixture — creates a fresh test DB per session
pytest_plugins = ["pytest_postgresql.plugin"]


@pytest.fixture(scope="session")
def db_migrations_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "db" / "migrations"


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
    migrations_dir = Path(__file__).resolve().parents[1] / "db" / "migrations"
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


# ---------------------------------------------------------------------------
# FinMind client fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_finmind():
    """Mock FinMindClient that returns canned DataFrames without hitting the API."""
    client = MagicMock()
    client.halt_flag = False
    return client


@pytest.fixture
def sample_ohlcv_df() -> pd.DataFrame:
    """25 days of fake OHLCV for ticker 9999."""
    rows = []
    base_close = 100.0
    base_date = date(2025, 1, 2)

    from datetime import timedelta

    d = base_date
    for i in range(25):
        close = base_close + i * 0.5  # slowly rising
        rows.append(
            {
                "trade_date": d,
                "ticker": "9999",
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 10_000 + i * 500,
            }
        )
        # Skip weekends naively
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)

    return pd.DataFrame(rows)


@pytest.fixture
def sample_broker_df() -> pd.DataFrame:
    """3 days of fake broker trade data for ticker 9999."""
    rows = []
    dates = [date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30)]

    for i, d in enumerate(dates):
        # Branch A: large buyer (no 隔日沖 label)
        rows.append(
            {
                "trade_date": d,
                "ticker": "9999",
                "branch_code": "A001",
                "branch_name": "元大-板橋",
                "buy_volume": 50_000 + i * 1000,
                "sell_volume": 5_000,
            }
        )
        # Branch B: medium buyer
        rows.append(
            {
                "trade_date": d,
                "ticker": "9999",
                "branch_code": "B002",
                "branch_name": "富邦-台北",
                "buy_volume": 30_000,
                "sell_volume": 8_000,
            }
        )
        # Branch C: small buyer
        rows.append(
            {
                "trade_date": d,
                "ticker": "9999",
                "branch_code": "C003",
                "branch_name": "國泰-信義",
                "buy_volume": 10_000,
                "sell_volume": 20_000,
            }
        )
        # Branch D: daytrade branch
        rows.append(
            {
                "trade_date": d,
                "ticker": "9999",
                "branch_code": "D004",
                "branch_name": "凱基-台北",
                "buy_volume": 8_000,
                "sell_volume": 5_000,
            }
        )

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Domain object fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_ohlcv(sample_ohlcv_df):
    """Return DailyOHLCV for the last row of sample_ohlcv_df."""
    from taiwan_stock_agent.domain.models import DailyOHLCV

    last = sample_ohlcv_df.iloc[-1]
    return DailyOHLCV(
        ticker=last["ticker"],
        trade_date=last["trade_date"],
        open=last["open"],
        high=last["high"],
        low=last["low"],
        close=last["close"],
        volume=last["volume"],
    )


@pytest.fixture
def sample_ohlcv_history(sample_ohlcv_df):
    """Return list[DailyOHLCV] from sample_ohlcv_df."""
    from taiwan_stock_agent.domain.models import DailyOHLCV

    return [
        DailyOHLCV(
            ticker=row["ticker"],
            trade_date=row["trade_date"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
        )
        for _, row in sample_ohlcv_df.iterrows()
    ]


@pytest.fixture
def in_memory_label_repo():
    """BrokerLabelRepository backed by a plain dict (no DB needed for unit tests)."""
    from taiwan_stock_agent.domain.models import BrokerLabel

    class _InMemoryRepo:
        def __init__(self):
            self._store: dict[str, BrokerLabel] = {}

        def get(self, branch_code: str) -> BrokerLabel | None:
            return self._store.get(branch_code)

        def upsert(self, label: BrokerLabel) -> None:
            self._store[label.branch_code] = label

        def list_all(self) -> list[BrokerLabel]:
            return list(self._store.values())

    return _InMemoryRepo()
