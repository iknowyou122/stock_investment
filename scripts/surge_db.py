"""SQLite persistence for surge signal tracking and outcome settlement."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "surge_signals.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS surge_signals (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date        TEXT    NOT NULL,
    ticker             TEXT    NOT NULL,
    grade              TEXT    NOT NULL,
    score              INTEGER NOT NULL,
    vol_ratio          REAL,
    day_chg_pct        REAL,
    gap_pct            REAL,
    close_strength     REAL,
    rsi                REAL,
    inst_consec_days   INTEGER,
    industry_rank_pct  REAL,
    close_price        REAL,
    market             TEXT,
    industry           TEXT,
    score_breakdown    TEXT,
    t1_return_pct      REAL,
    t3_return_pct      REAL,
    t5_return_pct      REAL,
    settled_at         TEXT,
    UNIQUE(signal_date, ticker)
);
"""


def init_db(db_path: str | None = None) -> None:
    path = db_path or str(_DEFAULT_DB)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.executescript(_SCHEMA)


def insert_signals(signals: list[dict], db_path: str | None = None) -> int:
    """Insert or ignore (upsert) a list of surge signal dicts. Returns inserted count."""
    if not signals:
        return 0
    path = db_path or str(_DEFAULT_DB)
    init_db(path)
    rows = [
        (
            s["signal_date"], s["ticker"], s["grade"], s["score"],
            s.get("vol_ratio"), s.get("day_chg_pct"), s.get("gap_pct"),
            s.get("close_strength"), s.get("rsi"), s.get("inst_consec_days"),
            s.get("industry_rank_pct"), s.get("close_price"),
            s.get("market"), s.get("industry"),
            s.get("score_breakdown") if isinstance(s.get("score_breakdown"), str)
            else json.dumps(s.get("score_breakdown") or {}),
        )
        for s in signals
    ]
    sql = """
        INSERT OR IGNORE INTO surge_signals
        (signal_date, ticker, grade, score, vol_ratio, day_chg_pct, gap_pct,
         close_strength, rsi, inst_consec_days, industry_rank_pct, close_price,
         market, industry, score_breakdown)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    with sqlite3.connect(path) as con:
        cur = con.executemany(sql, rows)
        return cur.rowcount


def _fetch_close(ticker: str, target_date: date, market: str) -> float | None:
    """Fetch closing price for ticker on target_date via yfinance."""
    import yfinance as yf
    import pandas as pd
    suffix = ".TW" if market == "TSE" else ".TWO"
    start = target_date - timedelta(days=1)
    end = target_date + timedelta(days=2)
    try:
        hist = yf.download(
            f"{ticker}{suffix}", start=str(start), end=str(end),
            interval="1d", progress=False, auto_adjust=True,
            multi_level_index=False,
        )
        if hist.empty:
            return None
        hist.index = pd.to_datetime(hist.index).date
        rows = hist[hist.index <= target_date]
        if rows.empty:
            return None
        val = float(rows["Close"].iloc[-1])
        return round(val, 2) if not pd.isna(val) else None
    except Exception:
        return None


def settle_pending(db_path: str | None = None) -> int:
    """Settle T+1/T+3/T+5 returns for signals old enough to have outcomes.

    Uses calendar-day approximation: T+1=1d, T+3=3d, T+5=5d (skipping weekends).
    Returns number of rows updated.
    """
    path = db_path or str(_DEFAULT_DB)
    init_db(path)
    today = date.today()
    with sqlite3.connect(path) as con:
        con.row_factory = sqlite3.Row
        pending = con.execute("""
            SELECT id, ticker, signal_date, close_price, market
            FROM surge_signals
            WHERE close_price IS NOT NULL
              AND (t1_return_pct IS NULL OR t3_return_pct IS NULL OR t5_return_pct IS NULL)
        """).fetchall()

    all_updates: list[tuple] = []
    for row in pending:
        sig_date = date.fromisoformat(row["signal_date"])
        market = row["market"] or "TSE"
        close = row["close_price"]
        updates: dict[str, float | None] = {}

        for n, col in [(1, "t1_return_pct"), (3, "t3_return_pct"), (5, "t5_return_pct")]:
            target = sig_date + timedelta(days=n)
            while target.weekday() >= 5:
                target += timedelta(days=1)
            if today < target:
                continue
            price = _fetch_close(row["ticker"], target, market)
            if price is not None and close:
                updates[col] = round((price / close - 1) * 100, 3)

        if updates:
            t1 = updates.get("t1_return_pct")
            t3 = updates.get("t3_return_pct")
            t5 = updates.get("t5_return_pct")
            all_updates.append((t1, t3, t5, str(today), row["id"]))

    if all_updates:
        with sqlite3.connect(path) as con:
            con.executemany(
                "UPDATE surge_signals SET t1_return_pct=COALESCE(?,t1_return_pct), "
                "t3_return_pct=COALESCE(?,t3_return_pct), "
                "t5_return_pct=COALESCE(?,t5_return_pct), "
                "settled_at=? WHERE id=?",
                all_updates,
            )

    return len(all_updates)


def query_settled(
    db_path: str | None = None,
    min_settled: int = 30,
    lookback_days: int = 90,
) -> list[dict]:
    """Return settled signals for factor analysis.

    Only returns signals with t1_return_pct populated.
    Raises ValueError if fewer than min_settled rows found.
    """
    path = db_path or str(_DEFAULT_DB)
    init_db(path)
    cutoff = str(date.today() - timedelta(days=lookback_days))
    with sqlite3.connect(path) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT * FROM surge_signals
            WHERE t1_return_pct IS NOT NULL
              AND signal_date >= ?
            ORDER BY signal_date DESC
        """, (cutoff,)).fetchall()
    result = [dict(r) for r in rows]
    if len(result) < min_settled:
        raise ValueError(
            f"只有 {len(result)} 筆已結算信號（需要 ≥{min_settled} 筆才能分析）"
        )
    return result
