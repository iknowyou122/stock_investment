"""Signal outcome repository — records SignalOutput rows and later fills price outcomes.

Table: signal_outcomes (see db/migrations/002_signal_outcomes.sql)
Uses raw psycopg2 via get_connection() — no SQLAlchemy.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from taiwan_stock_agent.domain.models import SignalOutput
from taiwan_stock_agent.infrastructure.db import get_connection

logger = logging.getLogger(__name__)

# Mapping from outcome day label to price column and outcome column
_OUTCOME_COLUMNS = {
    "price_1d": "outcome_1d",
    "price_3d": "outcome_3d",
    "price_5d": "outcome_5d",
}


class SignalOutcomeRepository:
    """CRUD interface for the signal_outcomes table.

    All methods use the module-level get_connection() context manager so
    they share the global psycopg2 connection pool.
    """

    def record(
        self,
        signal: SignalOutput,
        branch_codes: list[str] | None = None,
        scoring_version: str = "v1",
    ) -> str:
        """Insert a new signal row.

        Returns the UUID string of the newly created signal_id.
        entry_price is taken from signal.execution_plan.entry_bid_limit.

        Parameters
        ----------
        signal:
            The SignalOutput to persist.
        branch_codes:
            Optional list of broker branch codes associated with this signal.
            Stored in the branch_codes TEXT[] column (migration 004).
            Defaults to an empty array when None.
        scoring_version:
            The scoring formula version that produced this signal (e.g. "v1", "v2").
            Stored in the scoring_version column (migration 007).
            Defaults to "v1" for backward compatibility.
        """
        entry_price = signal.execution_plan.entry_bid_limit
        codes = branch_codes if branch_codes is not None else []
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO signal_outcomes
                        (ticker, signal_date, confidence_score, action, entry_price,
                         halt_flag, branch_codes, scoring_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING signal_id
                    """,
                    (
                        signal.ticker,
                        signal.date,
                        signal.confidence,
                        signal.action,
                        entry_price,
                        signal.halt_flag,
                        codes,
                        scoring_version,
                    ),
                )
                row = cur.fetchone()
        return str(row[0])

    def fetch_unsettled(self, days_back: int = 7) -> list[dict]:
        """Return rows where price_5d IS NULL and halt_flag IS FALSE.

        Only rows created within the last days_back calendar days are returned.
        Columns returned: signal_id, ticker, signal_date, entry_price, created_at.
        """
        cutoff = datetime.utcnow() - timedelta(days=days_back)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT signal_id, ticker, signal_date, entry_price, created_at
                    FROM signal_outcomes
                    WHERE price_5d IS NULL
                      AND halt_flag = FALSE
                      AND created_at >= %s
                    ORDER BY created_at ASC
                    """,
                    (cutoff,),
                )
                rows = cur.fetchall()
        return [
            {
                "signal_id": str(r[0]),
                "ticker": r[1],
                "signal_date": r[2],
                "entry_price": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]

    def fill_price(self, signal_id: str, column: str, price: float) -> None:
        """Write a price column for a signal row.

        Also computes and writes the corresponding outcome_Xd column as
        (price - entry_price) / entry_price when entry_price != 0.

        column must be one of: price_1d, price_3d, price_5d.
        """
        if column not in _OUTCOME_COLUMNS:
            raise ValueError(
                f"Invalid column '{column}'. Must be one of {list(_OUTCOME_COLUMNS)}"
            )
        outcome_col = _OUTCOME_COLUMNS[column]

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Fetch entry_price so we can compute the outcome
                cur.execute(
                    "SELECT entry_price FROM signal_outcomes WHERE signal_id = %s",
                    (signal_id,),
                )
                row = cur.fetchone()
                if row is None:
                    logger.warning(
                        "fill_price: signal_id %s not found — skipping", signal_id
                    )
                    return
                entry_price: float = row[0]

                outcome: float | None = None
                if entry_price != 0:
                    outcome = (price - entry_price) / entry_price

                cur.execute(
                    f"""
                    UPDATE signal_outcomes
                    SET {column} = %s,
                        {outcome_col} = %s
                    WHERE signal_id = %s
                    """,
                    (price, outcome, signal_id),
                )

    def win_rate_stats(self, days: int = 30, scoring_version: str | None = None) -> dict:
        """Compute win-rate statistics over the last N calendar days.

        Returns::

            {
                "total": int,
                "long_count": int,
                "win_rate_1d": float | None,
                "win_rate_3d": float | None,
                "win_rate_5d": float | None,
                "by_confidence_tier": {
                    "high": {"count": int, "win_rate_1d": ..., ...},
                    "mid":  {...},
                    "low":  {...},
                },
            }

        Tiers: high >= 70, mid 50–69, low < 50.
        Win = outcome_Xd > 0. Only rows where outcome_Xd IS NOT NULL are counted.

        Parameters
        ----------
        days:
            Number of calendar days to look back.
        scoring_version:
            When provided, restrict stats to rows with this scoring_version value.
            Pass "v1" or "v2" to avoid mixing formula generations in win-rate stats.
            None (default) includes all rows for backward compatibility.
        """
        cutoff = datetime.utcnow() - timedelta(days=days)

        with get_connection() as conn:
            with conn.cursor() as cur:
                if scoring_version is not None:
                    cur.execute(
                        """
                        SELECT
                            confidence_score,
                            action,
                            outcome_1d,
                            outcome_3d,
                            outcome_5d
                        FROM signal_outcomes
                        WHERE created_at >= %s
                          AND halt_flag = FALSE
                          AND scoring_version = %s
                        """,
                        (cutoff, scoring_version),
                    )
                else:
                    cur.execute(
                        """
                        SELECT
                            confidence_score,
                            action,
                            outcome_1d,
                            outcome_3d,
                            outcome_5d
                        FROM signal_outcomes
                        WHERE created_at >= %s
                          AND halt_flag = FALSE
                        """,
                        (cutoff,),
                    )
                rows = cur.fetchall()

        if not rows:
            return {
                "total": 0,
                "long_count": 0,
                "win_rate_1d": None,
                "win_rate_3d": None,
                "win_rate_5d": None,
                "by_confidence_tier": {
                    "high": _empty_tier_stats(),
                    "mid": _empty_tier_stats(),
                    "low": _empty_tier_stats(),
                },
            }

        total = len(rows)
        long_count = sum(1 for r in rows if r[1] == "LONG")

        win_rate_1d = _compute_win_rate([r[2] for r in rows])
        win_rate_3d = _compute_win_rate([r[3] for r in rows])
        win_rate_5d = _compute_win_rate([r[4] for r in rows])

        # Partition by confidence tier
        high_rows = [r for r in rows if r[0] >= 70]
        mid_rows  = [r for r in rows if 50 <= r[0] < 70]
        low_rows  = [r for r in rows if r[0] < 50]

        return {
            "total": total,
            "long_count": long_count,
            "win_rate_1d": win_rate_1d,
            "win_rate_3d": win_rate_3d,
            "win_rate_5d": win_rate_5d,
            "by_confidence_tier": {
                "high": _tier_stats(high_rows),
                "mid": _tier_stats(mid_rows),
                "low": _tier_stats(low_rows),
            },
        }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _compute_win_rate(outcomes: list) -> float | None:
    """Win rate over non-None outcome values. Returns None if no settled data."""
    settled = [o for o in outcomes if o is not None]
    if not settled:
        return None
    return sum(1 for o in settled if o > 0) / len(settled)


def _tier_stats(rows: list) -> dict:
    """Build stats dict for a confidence tier."""
    return {
        "count": len(rows),
        "win_rate_1d": _compute_win_rate([r[2] for r in rows]),
        "win_rate_3d": _compute_win_rate([r[3] for r in rows]),
        "win_rate_5d": _compute_win_rate([r[4] for r in rows]),
    }


def _empty_tier_stats() -> dict:
    return {"count": 0, "win_rate_1d": None, "win_rate_3d": None, "win_rate_5d": None}
