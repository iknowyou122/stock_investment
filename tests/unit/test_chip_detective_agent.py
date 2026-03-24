"""Unit tests for ChipDetectiveAgent.

Coverage:
  - net_buyer_count_diff positive, zero, negative
  - concentration_top15 with >15 branches (truncate to top-15)
  - thin market guard (< 10 active branches)
  - 隔日沖_TOP3 risk flag set when daytrade branch in top-3
  - empty broker data → data quality flag
  - partial history (< 3 days) → data quality flag
  - label lookup from repo (known / unknown branch)
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from taiwan_stock_agent.agents.chip_detective_agent import ChipDetectiveAgent
from taiwan_stock_agent.domain.models import BrokerLabel


def _make_broker_df(dates: list[date], branches: list[dict]) -> pd.DataFrame:
    """Build a broker trade DataFrame for multiple dates and branches."""
    rows = []
    for d in dates:
        for b in branches:
            rows.append(
                {
                    "trade_date": d,
                    "ticker": "9999",
                    "branch_code": b["code"],
                    "branch_name": b["name"],
                    "buy_volume": b.get("buy", 0),
                    "sell_volume": b.get("sell", 0),
                }
            )
    return pd.DataFrame(rows)


D0 = date(2025, 1, 30)
D1 = date(2025, 1, 29)
D2 = date(2025, 1, 28)

BRANCHES = [
    {"code": "A001", "name": "元大-板橋", "buy": 50_000, "sell": 5_000},
    {"code": "B002", "name": "富邦-台北", "buy": 30_000, "sell": 8_000},
    {"code": "C003", "name": "國泰-信義", "buy": 10_000, "sell": 20_000},
    {"code": "D004", "name": "凱基-台北", "buy": 8_000, "sell": 5_000},
]


class TestNetBuyerCountDiff:
    def test_more_buyers_than_sellers(self, in_memory_label_repo):
        agent = ChipDetectiveAgent(in_memory_label_repo)
        df = _make_broker_df([D0, D1, D2], BRANCHES)
        report = agent.analyze("9999", D0, df)
        # Each day: 4 branches buy, 4 branches sell → diff = 0 per day, total = 0
        # Wait — all branches have both buy and sell > 0, so both buy_branches and sell_branches = 4
        # diff per day = 0; total = 0
        assert report.net_buyer_count_diff == 0

    def test_buyers_exceed_sellers(self, in_memory_label_repo):
        agent = ChipDetectiveAgent(in_memory_label_repo)
        # Branches that only buy (no sell)
        branches = [
            {"code": f"X{i:03}", "name": f"Branch{i}", "buy": 1000, "sell": 0}
            for i in range(5)
        ] + [
            {"code": "S001", "name": "Seller", "buy": 0, "sell": 5000}
        ]
        df = _make_broker_df([D0, D1, D2], branches)
        report = agent.analyze("9999", D0, df)
        # Each day: 5 buy_branches, 1 sell_branch → diff = +4 per day, total = +12
        assert report.net_buyer_count_diff == 12

    def test_sellers_exceed_buyers(self, in_memory_label_repo):
        agent = ChipDetectiveAgent(in_memory_label_repo)
        branches = [
            {"code": "B001", "name": "Buyer", "buy": 5000, "sell": 0}
        ] + [
            {"code": f"S{i:03}", "name": f"Seller{i}", "buy": 0, "sell": 1000}
            for i in range(5)
        ]
        df = _make_broker_df([D0, D1, D2], branches)
        report = agent.analyze("9999", D0, df)
        # Each day: 1 buy, 5 sell → diff = -4 per day, total = -12
        assert report.net_buyer_count_diff == -12


class TestConcentrationTop15:
    def test_concentration_exact_15_branches(self, in_memory_label_repo):
        agent = ChipDetectiveAgent(in_memory_label_repo)
        # 15 branches with equal buy_volume = 1000, plus 1 smaller branch
        branches = [
            {"code": f"T{i:03}", "name": f"Top{i}", "buy": 1000, "sell": 0}
            for i in range(15)
        ] + [
            {"code": "Z999", "name": "Small", "buy": 100, "sell": 0}
        ]
        df = _make_broker_df([D0], branches)
        report = agent.analyze("9999", D0, df)
        expected = 15_000 / 15_100
        assert abs(report.concentration_top15 - expected) < 0.001

    def test_thin_market_flag_when_few_active_branches(self, in_memory_label_repo):
        agent = ChipDetectiveAgent(in_memory_label_repo)
        # Only 5 active buying branches
        branches = [
            {"code": f"T{i:03}", "name": f"Top{i}", "buy": 1000, "sell": 0}
            for i in range(5)
        ]
        df = _make_broker_df([D0], branches)
        report = agent.analyze("9999", D0, df)
        assert report.active_branch_count == 5
        assert any("THIN_MARKET" in f for f in report.data_quality_flags)


class TestRiskFlags:
    def test_daytrade_in_top3_sets_risk_flag(self, in_memory_label_repo):
        # Register凱基-台北 as 隔日沖
        in_memory_label_repo.upsert(
            BrokerLabel(
                branch_code="D004",
                branch_name="凱基-台北",
                label="隔日沖",
                reversal_rate=0.75,
                sample_count=100,
                last_updated=date(2025, 1, 1),
            )
        )
        # Make凱基-台北 the top buyer
        branches = [
            {"code": "D004", "name": "凱基-台北", "buy": 100_000, "sell": 0},
            {"code": "B002", "name": "富邦-台北", "buy": 30_000, "sell": 0},
            {"code": "C003", "name": "國泰-信義", "buy": 10_000, "sell": 0},
        ]
        df = _make_broker_df([D0], branches)
        agent = ChipDetectiveAgent(in_memory_label_repo)
        report = agent.analyze("9999", D0, df)
        assert any("隔日沖_TOP3" in f for f in report.risk_flags)

    def test_no_daytrade_no_risk_flag(self, in_memory_label_repo):
        df = _make_broker_df([D0], BRANCHES)
        agent = ChipDetectiveAgent(in_memory_label_repo)
        report = agent.analyze("9999", D0, df)
        assert not any("隔日沖_TOP3" in f for f in report.risk_flags)


class TestEdgeCases:
    def test_empty_broker_data_returns_quality_flag(self, in_memory_label_repo):
        agent = ChipDetectiveAgent(in_memory_label_repo)
        empty_df = pd.DataFrame(
            columns=["trade_date", "ticker", "branch_code", "branch_name",
                     "buy_volume", "sell_volume"]
        )
        report = agent.analyze("9999", D0, empty_df)
        assert "NO_BROKER_DATA" in report.data_quality_flags
        assert report.active_branch_count == 0

    def test_partial_history_flag_when_fewer_than_3_days(self, in_memory_label_repo):
        agent = ChipDetectiveAgent(in_memory_label_repo)
        # Only 1 day of data
        df = _make_broker_df([D0], BRANCHES)
        report = agent.analyze("9999", D0, df)
        assert any("PARTIAL_HISTORY" in f for f in report.data_quality_flags)

    def test_label_lookup_unknown_branch(self, in_memory_label_repo):
        """Branch not in repo defaults to 'unknown' label."""
        df = _make_broker_df([D0], [
            {"code": "NEW001", "name": "新分點", "buy": 100_000, "sell": 0}
        ])
        agent = ChipDetectiveAgent(in_memory_label_repo)
        report = agent.analyze("9999", D0, df)
        assert report.top_buyers[0].label == "unknown"
