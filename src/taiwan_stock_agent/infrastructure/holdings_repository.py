"""Simulated holdings repository — track open positions across daily runs.

Each row in `simulated_holdings` represents a "what the user should buy /
hold / sell" recommendation that persists day-over-day. Used by
AllocationAdvisor to distinguish "we already entered this yesterday — show
it as a held position" from "this is a brand-new buy signal today".

Close reasons mirror the rule-based exit policy:
  STOP_LOSS   — price hit the -7% stop loss line
  TAKE_PROFIT — price hit the +15% take profit line
  TIME_STOP   — 10 trading days elapsed and price < entry * 1.05
  TIER_DROP   — TCE confidence collapsed (was Tier S/A, now C or rejected)
  MANUAL      — user-initiated close (CLI or HTML action)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Holding:
    holding_id: int
    ticker: str
    entry_date: date
    entry_price: float
    suggested_pct: float
    tier: str
    stop_loss: float
    take_profit: float
    industry: str
    concept_keys: tuple[str, ...]
    entry_reason: str
    status: str  # OPEN | CLOSED | REDUCED
    close_date: Optional[date] = None
    close_price: Optional[float] = None
    close_reason: Optional[str] = None
    realised_pct: Optional[float] = None
    notes: str = ""

    @property
    def is_open(self) -> bool:
        return self.status == "OPEN"


@dataclass(frozen=True)
class ExitDecision:
    """Result of evaluating whether to close a position today."""

    should_close: bool
    close_reason: Optional[str] = None
    rationale: str = ""


# ── Default exit policy ─────────────────────────────────────────────────────


DEFAULT_STOP_LOSS_PCT = 0.08     # -8% (was -7%): 6/25→7/6 data 12/16 期間內
                                  # 觸停損但多數只有 -6~-9%（whipsaw）。放寬 1pt
                                  # 給進場後 2-3 天必然波動的空間
DEFAULT_TAKE_PROFIT_PCT = 0.15   # +15%
DEFAULT_TIME_STOP_DAYS = 10
DEFAULT_TIME_STOP_MIN_PROFIT = 0.05  # if after 10 days price < entry * 1.05 → exit


def evaluate_exit(
    holding: Holding,
    current_price: float,
    today: date,
    tce_confidence_today: Optional[float] = None,
) -> ExitDecision:
    """Pure rule-based exit decision.

    Order matters: stop loss > take profit > time stop > tier drop.
    """
    if holding.entry_price <= 0:
        return ExitDecision(False)

    # Stop loss
    if current_price <= holding.stop_loss:
        return ExitDecision(
            True,
            "STOP_LOSS",
            f"price {current_price:.2f} hit stop_loss {holding.stop_loss:.2f}",
        )

    # Take profit
    if current_price >= holding.take_profit:
        return ExitDecision(
            True,
            "TAKE_PROFIT",
            f"price {current_price:.2f} hit take_profit {holding.take_profit:.2f}",
        )

    # Time stop
    held_days = _trading_days_between(holding.entry_date, today)
    if held_days >= DEFAULT_TIME_STOP_DAYS:
        threshold = holding.entry_price * (1.0 + DEFAULT_TIME_STOP_MIN_PROFIT)
        if current_price < threshold:
            return ExitDecision(
                True,
                "TIME_STOP",
                f"held {held_days} days, price {current_price:.2f} below "
                f"{threshold:.2f} (entry × 1.05)",
            )

    # Tier drop: 6/25 data showed 5/6 平倉原因 = TIER_DROP，但 T+1 高分 picks
    # 勝率仍 66.7%。單日 conf<30 誤傷 whipsaw 標的。改為：
    #   - 收得更嚴：conf < 20（極端崩盤才觸發，不是短暫波動）
    #   - 加冷卻期：持有 ≥5 天才可觸發（避免進場 2-3 天就砍）
    if (
        tce_confidence_today is not None
        and tce_confidence_today < 20
        and held_days >= 5
    ):
        return ExitDecision(
            True,
            "TIER_DROP",
            f"TCE conf collapsed to {tce_confidence_today:.0f} (<20) after "
            f"{held_days} days — thesis fully broken",
        )

    return ExitDecision(False)


def _trading_days_between(start: date, end: date) -> int:
    """Approximation: weekdays only, not adjusted for TWSE holidays."""
    if end <= start:
        return 0
    days = (end - start).days
    weekdays = 0
    cur = start
    while cur < end:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            weekdays += 1
    return weekdays


# ── Repository ──────────────────────────────────────────────────────────────


class HoldingsRepository:
    """Thin DB wrapper for simulated_holdings.

    DB-unavailable mode: when DATABASE_URL is unset or the connection fails,
    every method becomes a no-op so the rest of the pipeline keeps working.
    """

    def __init__(self) -> None:
        self._db_url = os.environ.get("DATABASE_URL", "").strip()

    @property
    def available(self) -> bool:
        return bool(self._db_url)

    def list_open(self) -> list[Holding]:
        if not self.available:
            return []
        try:
            import psycopg2
        except ImportError:
            return []
        try:
            with psycopg2.connect(self._db_url) as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT holding_id, ticker, entry_date, entry_price,
                              suggested_pct, tier, stop_loss, take_profit,
                              industry, concept_keys, entry_reason, status,
                              close_date, close_price, close_reason,
                              realised_pct, notes
                       FROM simulated_holdings
                       WHERE status = 'OPEN'
                       ORDER BY entry_date DESC, ticker"""
                )
                return [self._row_to_holding(r) for r in cur.fetchall()]
        except Exception as exc:  # pragma: no cover
            logger.warning("list_open failed: %s", exc)
            return []

    def get(self, ticker: str) -> Optional[Holding]:
        """Return the latest OPEN holding for ticker, or None."""
        for h in self.list_open():
            if h.ticker == ticker:
                return h
        return None

    def list_recent_closed(self, *, since: date | None = None) -> list[Holding]:
        """Phase 4.50.5 — CLOSED holdings since the given date (default: 30 days).

        Used by holdings_review.py for daily P&L recap and trigger-history
        analysis (STOP_LOSS / TAKE_PROFIT / TIER_DROP / TIME_STOP counts).
        """
        if not self.available:
            return []
        try:
            import psycopg2
        except ImportError:
            return []
        if since is None:
            since = date.today() - timedelta(days=30)
        try:
            with psycopg2.connect(self._db_url) as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT holding_id, ticker, entry_date, entry_price,
                              suggested_pct, tier, stop_loss, take_profit,
                              industry, concept_keys, entry_reason, status,
                              close_date, close_price, close_reason,
                              realised_pct, notes
                       FROM simulated_holdings
                       WHERE status = 'CLOSED' AND close_date >= %s
                       ORDER BY close_date DESC, ticker""",
                    (since,),
                )
                return [self._row_to_holding(r) for r in cur.fetchall()]
        except Exception as exc:  # pragma: no cover
            logger.warning("list_recent_closed failed: %s", exc)
            return []

    def open_position(
        self,
        *,
        ticker: str,
        entry_date: date,
        entry_price: float,
        suggested_pct: float,
        tier: str,
        industry: str = "",
        concept_keys: Sequence[str] = (),
        entry_reason: str = "",
        stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
        take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
    ) -> Optional[int]:
        """Insert a new OPEN position. Returns holding_id or None on failure."""
        if not self.available or entry_price <= 0:
            return None
        try:
            import psycopg2
        except ImportError:
            return None
        sl = round(entry_price * (1 - stop_loss_pct), 2)
        tp = round(entry_price * (1 + take_profit_pct), 2)
        try:
            with psycopg2.connect(self._db_url) as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO simulated_holdings
                       (ticker, entry_date, entry_price, suggested_pct, tier,
                        stop_loss, take_profit, industry, concept_keys,
                        entry_reason)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (ticker, entry_date)
                       DO UPDATE SET suggested_pct = EXCLUDED.suggested_pct,
                                     tier = EXCLUDED.tier,
                                     stop_loss = EXCLUDED.stop_loss,
                                     take_profit = EXCLUDED.take_profit,
                                     entry_reason = EXCLUDED.entry_reason,
                                     updated_at = CURRENT_TIMESTAMP
                       RETURNING holding_id""",
                    (
                        ticker, entry_date, entry_price, suggested_pct, tier,
                        sl, tp, industry, ",".join(concept_keys), entry_reason,
                    ),
                )
                row = cur.fetchone()
                conn.commit()
                return row[0] if row else None
        except Exception as exc:  # pragma: no cover
            logger.warning("open_position failed for %s: %s", ticker, exc)
            return None

    def close_position(
        self,
        holding_id: int,
        *,
        close_date: date,
        close_price: float,
        close_reason: str,
        entry_price: float,
        notes: str = "",
    ) -> bool:
        if not self.available:
            return False
        try:
            import psycopg2
        except ImportError:
            return False
        realised = round((close_price - entry_price) / entry_price * 100, 2) if entry_price > 0 else 0.0
        try:
            with psycopg2.connect(self._db_url) as conn, conn.cursor() as cur:
                cur.execute(
                    """UPDATE simulated_holdings
                       SET status='CLOSED',
                           close_date=%s,
                           close_price=%s,
                           close_reason=%s,
                           realised_pct=%s,
                           notes=COALESCE(notes,'') || %s,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE holding_id=%s AND status='OPEN'""",
                    (close_date, close_price, close_reason, realised, notes, holding_id),
                )
                ok = cur.rowcount > 0
                conn.commit()
                return ok
        except Exception as exc:  # pragma: no cover
            logger.warning("close_position failed for %s: %s", holding_id, exc)
            return False

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_holding(row) -> Holding:
        (
            hid, tk, ed, ep, pct, tier, sl, tp, ind, ck, reason, status,
            cd, cp, cr, rp, notes,
        ) = row
        return Holding(
            holding_id=hid,
            ticker=str(tk),
            entry_date=ed,
            entry_price=float(ep) if ep is not None else 0.0,
            suggested_pct=float(pct) if pct is not None else 0.0,
            tier=str(tier),
            stop_loss=float(sl) if sl is not None else 0.0,
            take_profit=float(tp) if tp is not None else 0.0,
            industry=str(ind or ""),
            concept_keys=tuple((ck or "").split(",")) if ck else (),
            entry_reason=str(reason or ""),
            status=str(status),
            close_date=cd,
            close_price=float(cp) if cp is not None else None,
            close_reason=str(cr) if cr else None,
            realised_pct=float(rp) if rp is not None else None,
            notes=str(notes or ""),
        )
