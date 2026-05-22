"""Write surge scan results and D+1 watch entries to DB."""
from __future__ import annotations

import json
import logging
from datetime import date

from taiwan_stock_agent.infrastructure.db import get_connection

logger = logging.getLogger(__name__)


def record_surge_signals(results: list[dict], analysis_date: date, scan_date: date) -> int:
    """Upsert surge scan results into surge_signals table.

    Returns count of rows written.
    """
    if not results:
        return 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                rows = []
                for r in results:
                    rows.append((
                        analysis_date,
                        scan_date,
                        r.get("ticker", ""),
                        r.get("name", ""),
                        r.get("market", ""),
                        r.get("industry", ""),
                        r.get("grade", ""),
                        r.get("score"),
                        r.get("vol_ratio"),
                        r.get("close_strength"),
                        r.get("day_chg_pct"),
                        r.get("gap_pct"),
                        r.get("surge_day"),
                        r.get("industry_rank_pct"),
                        r.get("rsi"),
                        r.get("inst_consec_days"),
                        r.get("close_price"),
                        json.dumps(r.get("score_breakdown") or {}),
                        "|".join(r.get("flags") or []),
                    ))
                cur.executemany(
                    """
                    INSERT INTO surge_signals
                        (analysis_date, scan_date, ticker, name, market, industry,
                         grade, score, vol_ratio, close_strength, day_chg_pct,
                         gap_pct, surge_day, industry_rank_pct, rsi, inst_consec_days,
                         close_price, score_breakdown, flags)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (analysis_date, ticker) DO UPDATE SET
                        grade           = EXCLUDED.grade,
                        score           = EXCLUDED.score,
                        vol_ratio       = EXCLUDED.vol_ratio,
                        close_strength  = EXCLUDED.close_strength,
                        day_chg_pct     = EXCLUDED.day_chg_pct,
                        close_price     = EXCLUDED.close_price,
                        score_breakdown = EXCLUDED.score_breakdown,
                        flags           = EXCLUDED.flags
                    """,
                    rows,
                )
            conn.commit()
        return len(rows)
    except Exception as e:
        logger.warning("record_surge_signals failed: %s", e)
        return 0


def save_surge_watch(signals: list[dict], scan_date: date) -> int:
    """Insert ALPHA surge signals into surge_watch for D+1 tracking.

    Returns count of rows written.
    """
    if not signals:
        return 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                rows = [(
                    scan_date,
                    s["ticker"],
                    s.get("name", ""),
                    s.get("market", "TSE"),
                    s.get("industry", ""),
                    s.get("score"),
                    s.get("close_price"),
                    s.get("vol_ratio"),
                    s.get("close_strength"),
                    s.get("day_chg_pct"),
                    s.get("flags", ""),
                ) for s in signals]
                cur.executemany(
                    """
                    INSERT INTO surge_watch
                        (scan_date, ticker, name, market, industry,
                         score, close_price, vol_ratio, close_strength, day_chg_pct, flags)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (scan_date, ticker) DO NOTHING
                    """,
                    rows,
                )
            conn.commit()
        return len(rows)
    except Exception as e:
        logger.warning("save_surge_watch failed: %s", e)
        return 0


def confirm_surge_watch(scan_date: date, ticker: str, close_d1: float, d1_chg_pct: float) -> None:
    """Mark a surge_watch entry as D+1 confirmed."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE surge_watch
                    SET d1_confirmed = TRUE, close_d1 = %s, d1_chg_pct = %s
                    WHERE scan_date = %s AND ticker = %s
                    """,
                    (close_d1, d1_chg_pct, scan_date, ticker),
                )
            conn.commit()
    except Exception as e:
        logger.warning("confirm_surge_watch %s %s: %s", scan_date, ticker, e)


def load_surge_watch(scan_date: date) -> list[dict]:
    """Load ALPHA signals tracked for scan_date from DB."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ticker, name, market, industry, score,
                           close_price, vol_ratio, close_strength, day_chg_pct, flags
                    FROM surge_watch
                    WHERE scan_date = %s
                    ORDER BY score DESC
                    """,
                    (scan_date,),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.warning("load_surge_watch %s: %s", scan_date, e)
        return []


def query_surge_signals(
    analysis_date: date,
    grades: set[str] | None = None,
    min_score: int = 0,
) -> list[dict]:
    """Query surge_signals for a given analysis_date."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ticker, name, market, industry, grade, score,
                           vol_ratio, close_strength, day_chg_pct, gap_pct,
                           close_price, flags
                    FROM surge_signals
                    WHERE analysis_date = %s
                      AND score >= %s
                    ORDER BY score DESC
                    """,
                    (analysis_date, min_score),
                )
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                if grades:
                    rows = [r for r in rows if r.get("grade") in grades]
                return rows
    except Exception as e:
        logger.warning("query_surge_signals %s: %s", analysis_date, e)
        return []
