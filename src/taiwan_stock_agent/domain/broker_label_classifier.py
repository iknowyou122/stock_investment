"""BrokerLabelClassifier: behavioral fingerprinting of 分點 (broker branches).

Classification rules (from design doc):
  - label = '隔日沖' if reversal_rate > 0.60 AND sample_count >= 50
  - label = 'unknown' until sample_count reaches 50

The reversal_rate is defined as:
  P(D+2 stock close < D+0 close | branch is in top-3 buyers on D+0)

  Note: we use D+2 (not D+1) because FinMind 分點 data is published T+1 night
  (Day D flows visible on Day D+1 evening). Earliest tradeable execution is D+2 open.

Other labels (波段贏家, 地緣券商, 代操官股) are manually curated or require
additional feature engineering beyond reversal_rate — see TODOS.md for Phase 2.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Protocol

import pandas as pd

from taiwan_stock_agent.domain.models import BrokerLabel

logger = logging.getLogger(__name__)

# Classification thresholds
DAYTRADE_REVERSAL_THRESHOLD = 0.60
MIN_SAMPLE_COUNT = 50


class BrokerLabelRepository(Protocol):
    """Interface for broker label storage. Implementations: PostgreSQL, in-memory dict."""

    def get(self, branch_code: str) -> BrokerLabel | None:
        """Return the label for a branch, or None if not found."""
        ...

    def upsert(self, label: BrokerLabel) -> None:
        """Insert or update a broker label record."""
        ...

    def list_all(self) -> list[BrokerLabel]:
        """Return all stored broker labels."""
        ...


class PostgresBrokerLabelRepository:
    """PostgreSQL-backed BrokerLabelRepository.

    Expects the broker_labels table from db/migrations/001_broker_labels.sql.
    """

    def __init__(self, conn_factory) -> None:
        self._conn_factory = conn_factory

    def get(self, branch_code: str) -> BrokerLabel | None:
        from taiwan_stock_agent.infrastructure.db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT branch_code, branch_name, label, reversal_rate,
                           sample_count, last_updated, metadata
                    FROM broker_labels
                    WHERE branch_code = %s
                    """,
                    (branch_code,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return BrokerLabel(
            branch_code=row[0],
            branch_name=row[1],
            label=row[2],
            reversal_rate=row[3],
            sample_count=row[4],
            last_updated=row[5],
            metadata=row[6] or {},
        )

    def upsert(self, label: BrokerLabel) -> None:
        from taiwan_stock_agent.infrastructure.db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO broker_labels
                        (branch_code, branch_name, label, reversal_rate,
                         sample_count, last_updated, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (branch_code) DO UPDATE SET
                        branch_name   = EXCLUDED.branch_name,
                        label         = EXCLUDED.label,
                        reversal_rate = EXCLUDED.reversal_rate,
                        sample_count  = EXCLUDED.sample_count,
                        last_updated  = EXCLUDED.last_updated,
                        metadata      = EXCLUDED.metadata
                    """,
                    (
                        label.branch_code,
                        label.branch_name,
                        label.label,
                        label.reversal_rate,
                        label.sample_count,
                        label.last_updated,
                        label.metadata,
                    ),
                )

    def list_all(self) -> list[BrokerLabel]:
        from taiwan_stock_agent.infrastructure.db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT branch_code, branch_name, label, reversal_rate,
                           sample_count, last_updated, metadata
                    FROM broker_labels
                    """
                )
                rows = cur.fetchall()
        return [
            BrokerLabel(
                branch_code=r[0],
                branch_name=r[1],
                label=r[2],
                reversal_rate=r[3],
                sample_count=r[4],
                last_updated=r[5],
                metadata=r[6] or {},
            )
            for r in rows
        ]


class BrokerLabelClassifier:
    """Compute and assign behavioral labels to broker branches from historical trade data.

    Usage (batch classification from raw FinMind data)::

        classifier = BrokerLabelClassifier(repo)
        classifier.fit(broker_trades_df, ohlcv_df)
        # broker_labels table is now populated

    broker_trades_df columns: trade_date, ticker, branch_code, branch_name,
                               buy_volume, sell_volume
    ohlcv_df columns:          trade_date, ticker, open, high, low, close, volume
    """

    def __init__(self, repo: BrokerLabelRepository) -> None:
        self.repo = repo

    def fit(
        self,
        broker_trades: pd.DataFrame,
        ohlcv: pd.DataFrame,
        as_of: date | None = None,
    ) -> dict[str, BrokerLabel]:
        """Classify all branches with sufficient samples.

        Returns a dict mapping branch_code → BrokerLabel (also persists to repo).
        """
        as_of = as_of or date.today()

        # Identify top-3 buyers per (ticker, trade_date)
        top3 = self._compute_top3_buyers(broker_trades)

        # Compute reversal_rate per branch at D+2 execution horizon
        rates = self._compute_reversal_rates(top3, ohlcv)

        labels: dict[str, BrokerLabel] = {}
        for _, row in rates.iterrows():
            branch_code = row["branch_code"]
            branch_name = row.get("branch_name", branch_code)
            n = int(row["sample_count"])
            rate = float(row["reversal_rate"])

            if n < MIN_SAMPLE_COUNT:
                label_str = "unknown"
            elif rate > DAYTRADE_REVERSAL_THRESHOLD:
                label_str = "隔日沖"
            else:
                label_str = "unknown"
                # 波段贏家 / 地緣券商 / 代操官股 classification deferred to Phase 2
                # (requires additional feature engineering beyond reversal_rate alone)

            bl = BrokerLabel(
                branch_code=branch_code,
                branch_name=branch_name,
                label=label_str,
                reversal_rate=rate,
                sample_count=n,
                last_updated=as_of,
            )
            labels[branch_code] = bl
            self.repo.upsert(bl)

        logger.info(
            "BrokerLabelClassifier.fit: classified %d branches "
            "(%d 隔日沖, %d unknown)",
            len(labels),
            sum(1 for b in labels.values() if b.label == "隔日沖"),
            sum(1 for b in labels.values() if b.label == "unknown"),
        )
        return labels

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_top3_buyers(broker_trades: pd.DataFrame) -> pd.DataFrame:
        """Return rows where branch is in top-3 by buy_volume per (ticker, trade_date)."""
        bt = broker_trades.copy()
        bt["rank"] = bt.groupby(["ticker", "trade_date"])["buy_volume"].rank(
            method="first", ascending=False
        )
        return bt[bt["rank"] <= 3].copy()

    @staticmethod
    def _compute_reversal_rates(
        top3: pd.DataFrame,
        ohlcv: pd.DataFrame,
    ) -> pd.DataFrame:
        """Compute reversal rate at D+2 execution horizon per branch.

        Reversal = stock close on D+2 < stock close on D+0.

        D+2 logic:
          - D+0: trade_date in top3 row (the day the branch was top-3 buyer)
          - D+1: FinMind publishes the data (not tradeable)
          - D+2: earliest execution open; we check D+2 close as outcome

        This directly addresses the data latency constraint in the design doc.
        """
        # Build a lookup: (ticker, date) → close
        close_map = (
            ohlcv.set_index(["ticker", "trade_date"])["close"].to_dict()
        )

        # Build a mapping: (ticker, trade_date) → D+2 trade_date
        # We need to find the 2nd next trading date after each date.
        # Approach: sort unique dates per ticker, then shift by 2.
        trading_dates_by_ticker: dict[str, list] = {}
        for ticker, grp in ohlcv.groupby("ticker"):
            trading_dates_by_ticker[ticker] = sorted(grp["trade_date"].unique())

        def get_d2_date(ticker: str, d0: date) -> date | None:
            dates = trading_dates_by_ticker.get(ticker, [])
            try:
                idx = dates.index(d0)
            except ValueError:
                return None
            if idx + 2 < len(dates):
                return dates[idx + 2]
            return None

        rows = []
        for _, row in top3.iterrows():
            ticker = row["ticker"]
            d0 = row["trade_date"]
            close_d0 = close_map.get((ticker, d0))
            d2 = get_d2_date(ticker, d0)
            close_d2 = close_map.get((ticker, d2)) if d2 else None

            if close_d0 is None or close_d2 is None:
                continue

            reversal = int(close_d2 < close_d0)
            rows.append(
                {
                    "branch_code": row["branch_code"],
                    "branch_name": row.get("branch_name", row["branch_code"]),
                    "reversal": reversal,
                }
            )

        if not rows:
            return pd.DataFrame(
                columns=["branch_code", "branch_name", "reversal_rate", "sample_count"]
            )

        df = pd.DataFrame(rows)
        result = (
            df.groupby(["branch_code", "branch_name"])["reversal"]
            .agg(reversal_rate="mean", sample_count="count")
            .reset_index()
        )
        return result
