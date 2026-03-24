"""ChipDetectiveAgent: broker label lookup, concentration scoring, net_buyer_count_diff.

Input:  ticker, date (T+1 settlement), broker_label_db: BrokerLabelRepository,
        raw broker trade rows for last 3 days
Output: ChipReport
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from taiwan_stock_agent.domain.broker_label_classifier import BrokerLabelRepository
from taiwan_stock_agent.domain.models import BrokerWithLabel, ChipReport

logger = logging.getLogger(__name__)

# Minimum active branches to treat concentration as meaningful (see Reviewer Concern #2)
MIN_ACTIVE_BRANCHES_FOR_CONCENTRATION = 10


class ChipDetectiveAgent:
    """Derive chip health metrics from T+1 broker trade data.

    Typical usage::

        agent = ChipDetectiveAgent(label_repo)
        chip_report = agent.analyze(ticker="2330", report_date=date(2026, 3, 24), broker_trades_df=df)

    broker_trades_df must contain rows for at least last 3 trading days for this ticker.
    Expected columns: trade_date (date), ticker (str), branch_code (str),
                      branch_name (str), buy_volume (int), sell_volume (int)
    """

    def __init__(self, label_repo: BrokerLabelRepository) -> None:
        self._repo = label_repo

    def analyze(
        self,
        ticker: str,
        report_date: date,
        broker_trades_df: pd.DataFrame,
    ) -> ChipReport:
        """Compute ChipReport for ticker on report_date.

        report_date is the T+1 settlement date (the date whose broker flows we analyze).
        broker_trades_df should contain rows for report_date and the 2 prior trading days.
        """
        df = broker_trades_df[broker_trades_df["ticker"] == ticker].copy()
        if df.empty:
            logger.warning("No broker trade data for %s on %s", ticker, report_date)
            return ChipReport(
                ticker=ticker,
                report_date=report_date,
                top_buyers=[],
                concentration_top15=0.0,
                net_buyer_count_diff=0,
                risk_flags=[],
                active_branch_count=0,
                data_quality_flags=["NO_BROKER_DATA"],
            )

        # --- Today's data (report_date) ---
        today_df = df[df["trade_date"] == report_date]

        top_buyers = self._compute_top_buyers(today_df, n=15)
        concentration = self._compute_concentration_top15(today_df)
        active_branch_count = int((today_df["buy_volume"] > 0).sum())

        # --- Net buyer count diff (last 3 days including today) ---
        dates_sorted = sorted(df["trade_date"].unique())
        last_3 = dates_sorted[-3:]
        net_buyer_count_diff = self._compute_net_buyer_count_diff(
            df[df["trade_date"].isin(last_3)]
        )

        # --- Risk flags ---
        risk_flags: list[str] = []
        top3_labels = [b.label for b in top_buyers[:3]]
        if "隔日沖" in top3_labels:
            names = [
                b.branch_name for b in top_buyers[:3] if b.label == "隔日沖"
            ]
            risk_flags.append(f"隔日沖_TOP3: {', '.join(names)}")

        # Data quality flags
        data_quality_flags: list[str] = []
        if active_branch_count < MIN_ACTIVE_BRANCHES_FOR_CONCENTRATION:
            data_quality_flags.append(
                f"THIN_MARKET: {active_branch_count} active branches "
                "(concentration_top15 less reliable)"
            )
        if len(last_3) < 3:
            data_quality_flags.append(
                f"PARTIAL_HISTORY: only {len(last_3)} days available for net_buyer_count_diff"
            )

        return ChipReport(
            ticker=ticker,
            report_date=report_date,
            top_buyers=top_buyers,
            concentration_top15=concentration,
            net_buyer_count_diff=net_buyer_count_diff,
            risk_flags=risk_flags,
            active_branch_count=active_branch_count,
            data_quality_flags=data_quality_flags,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compute_top_buyers(
        self, today_df: pd.DataFrame, n: int = 15
    ) -> list[BrokerWithLabel]:
        """Return top-n buyer branches for today, annotated with their label."""
        if today_df.empty:
            return []

        ranked = (
            today_df[today_df["buy_volume"] > 0]
            .sort_values("buy_volume", ascending=False)
            .head(n)
        )

        result: list[BrokerWithLabel] = []
        for _, row in ranked.iterrows():
            code = str(row["branch_code"])
            name = str(row.get("branch_name", code))
            stored = self._repo.get(code)

            result.append(
                BrokerWithLabel(
                    branch_code=code,
                    branch_name=name,
                    label=stored.label if stored else "unknown",
                    reversal_rate=stored.reversal_rate if stored else 0.0,
                    buy_volume=int(row["buy_volume"]),
                    sell_volume=int(row.get("sell_volume", 0)),
                )
            )
        return result

    @staticmethod
    def _compute_concentration_top15(today_df: pd.DataFrame) -> float:
        """top-15 buy vol / total buy vol for today.

        Returns 0.0 if total buy volume is zero.
        """
        if today_df.empty:
            return 0.0
        total_buy = today_df["buy_volume"].sum()
        if total_buy == 0:
            return 0.0
        top15_buy = (
            today_df.nlargest(15, "buy_volume")["buy_volume"].sum()
        )
        return float(top15_buy / total_buy)

    @staticmethod
    def _compute_net_buyer_count_diff(last3_df: pd.DataFrame) -> int:
        """Sum over last 3 trading days of (distinct buying branches - distinct selling branches).

        SQL equivalent (from design doc):
          WITH daily AS (
            SELECT trade_date,
                   COUNT(DISTINCT CASE WHEN buy_volume > 0 THEN branch_code END) AS buy_branches,
                   COUNT(DISTINCT CASE WHEN sell_volume > 0 THEN branch_code END) AS sell_branches
            FROM broker_trades
            WHERE ticker = ... AND trade_date IN (last 3 dates)
            GROUP BY trade_date
          )
          SELECT SUM(buy_branches - sell_branches) FROM daily;

        A POSITIVE value means more buyer branches than seller branches (chips concentrating).
        """
        if last3_df.empty:
            return 0

        total = 0
        for _, day_df in last3_df.groupby("trade_date"):
            buy_branches = int((day_df["buy_volume"] > 0).sum())
            sell_branches = int((day_df["sell_volume"] > 0).sum())
            total += buy_branches - sell_branches
        return total
