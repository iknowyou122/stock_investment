"""DB-backed OHLCV daily price repository.

All consumers (plan scan, surge scan, backtest, charts) read from here.
Sources (finmind / yfinance) write here so data is unified regardless of origin.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_COLS = ["trade_date", "ticker", "open", "high", "low", "close", "volume"]


class OHLCVRepository:
    """Read/write daily OHLCV prices to PostgreSQL ohlcv_daily table.

    Designed to be used as an L2 cache behind FinMindClient's in-memory L1.
    When DATABASE_URL is not set, all methods are no-ops (returns empty / 0).
    """

    def __init__(self) -> None:
        self._available: bool | None = None  # None = not yet tested

    def _check_available(self) -> bool:
        if self._available is not None:
            return self._available
        if not os.environ.get("DATABASE_URL"):
            self._available = False
            return False
        try:
            from taiwan_stock_agent.infrastructure.db import get_connection
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM ohlcv_daily LIMIT 1")
            self._available = True
        except Exception as e:
            logger.debug("ohlcv_daily not available: %s", e)
            self._available = False
        return self._available

    def get(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """Return rows from DB for ticker in [start, end]. Empty DF if unavailable."""
        if not self._check_available():
            return pd.DataFrame(columns=_COLS)
        try:
            from taiwan_stock_agent.infrastructure.db import get_connection
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT trade_date, ticker, open, high, low, close, volume
                        FROM ohlcv_daily
                        WHERE ticker = %s AND trade_date BETWEEN %s AND %s
                        ORDER BY trade_date
                        """,
                        (ticker, start, end),
                    )
                    rows = cur.fetchall()
            if not rows:
                return pd.DataFrame(columns=_COLS)
            df = pd.DataFrame(rows, columns=_COLS)
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            for col in ["open", "high", "low", "close"]:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").astype("Int64")
            return df
        except Exception as e:
            logger.warning("ohlcv_repository.get failed: %s", e)
            return pd.DataFrame(columns=_COLS)

    def max_date(self, ticker: str) -> date | None:
        """Return the latest trade_date stored for ticker, or None."""
        if not self._check_available():
            return None
        try:
            from taiwan_stock_agent.infrastructure.db import get_connection
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT MAX(trade_date) FROM ohlcv_daily WHERE ticker = %s",
                        (ticker,),
                    )
                    row = cur.fetchone()
            return row[0] if row and row[0] else None
        except Exception as e:
            logger.warning("ohlcv_repository.max_date failed: %s", e)
            return None

    def upsert(self, df: pd.DataFrame, source: str = "finmind") -> int:
        """Write rows to DB. Skips rows with NaN close. Returns rows written."""
        if not self._check_available() or df.empty:
            return 0
        clean = df.dropna(subset=["close"]).copy()
        if clean.empty:
            return 0
        try:
            from taiwan_stock_agent.infrastructure.db import get_connection
            records = [
                (
                    str(row["ticker"]),
                    row["trade_date"],
                    float(row["open"]) if pd.notna(row.get("open")) else None,
                    float(row["high"]) if pd.notna(row.get("high")) else None,
                    float(row["low"]) if pd.notna(row.get("low")) else None,
                    float(row["close"]),
                    int(row["volume"]) if pd.notna(row.get("volume")) else None,
                    source,
                )
                for _, row in clean.iterrows()
            ]
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO ohlcv_daily
                            (ticker, trade_date, open, high, low, close, volume, source)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (ticker, trade_date) DO UPDATE
                            SET open=EXCLUDED.open, high=EXCLUDED.high,
                                low=EXCLUDED.low, close=EXCLUDED.close,
                                volume=EXCLUDED.volume, source=EXCLUDED.source,
                                fetched_at=NOW()
                        """,
                        records,
                    )
            logger.debug("ohlcv_repository: upserted %d rows for %s", len(records), source)
            return len(records)
        except Exception as e:
            logger.warning("ohlcv_repository.upsert failed: %s", e)
            return 0
