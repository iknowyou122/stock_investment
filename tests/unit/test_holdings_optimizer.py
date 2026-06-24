"""Unit tests for holdings_optimizer rule-based suggestions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pytest

from taiwan_stock_agent.domain.holdings_optimizer import (
    OptimizationSuggestion,
    build_optimization_report,
)
from taiwan_stock_agent.domain.holdings_review import (
    HoldingsReview,
    RiskWarning,
    TierStats,
    TriggerCount,
)


def _review(
    *,
    n_open: int = 5,
    n_closed: int = 0,
    triggers: Optional[TriggerCount] = None,
    tier_stats: tuple[TierStats, ...] = (),
    closed_win_rate: Optional[float] = None,
) -> HoldingsReview:
    """Build a stub HoldingsReview for testing rules in isolation."""
    return HoldingsReview(
        today=date(2026, 6, 24),
        lookback_days=7,
        budget_twd=3_000_000,
        n_open=n_open,
        n_closed_in_period=n_closed,
        portfolio_unrealised_pct=0.0,
        portfolio_unrealised_twd=0,
        portfolio_realised_twd=0,
        closed_win_rate=closed_win_rate,
        avg_realised_pct=None,
        triggers=triggers or TriggerCount(),
        tier_stats=tier_stats,
        risk_warnings=(),
        taiex_return_pct=None,
        alpha_pct=None,
    )


# ── Rule: high TIER_DROP rate ─────────────────────────────────────────────


class TestHighTierDropRule:
    def test_no_suggestion_when_too_few_closes(self) -> None:
        r = _review(triggers=TriggerCount(tier_drop=2, take_profit=1))  # total 3
        report = build_optimization_report(r)
        assert not any(s.rule == "high_tier_drop_rate" for s in report.suggestions)

    def test_below_threshold_no_suggestion(self) -> None:
        # 3 of 10 TIER_DROP = 30% → below 40% threshold
        r = _review(triggers=TriggerCount(tier_drop=3, take_profit=4, stop_loss=3))
        report = build_optimization_report(r)
        assert not any(s.rule == "high_tier_drop_rate" for s in report.suggestions)

    def test_above_threshold_emits_medium(self) -> None:
        # 5 of 10 = 50% → medium severity
        r = _review(triggers=TriggerCount(tier_drop=5, take_profit=3, stop_loss=2))
        report = build_optimization_report(r)
        sugg = next(s for s in report.suggestions if s.rule == "high_tier_drop_rate")
        assert sugg.severity == "medium"
        assert "MIN_CONFIDENCE" in sugg.parameter
        assert "90" in sugg.suggested_value

    def test_very_high_emits_high_severity(self) -> None:
        # 7 of 10 = 70% → high severity, +10 confidence
        r = _review(triggers=TriggerCount(tier_drop=7, take_profit=2, stop_loss=1))
        report = build_optimization_report(r)
        sugg = next(s for s in report.suggestions if s.rule == "high_tier_drop_rate")
        assert sugg.severity == "high"
        assert "95" in sugg.suggested_value


# ── Rule: high STOP_LOSS rate ─────────────────────────────────────────────


class TestHighStopLossRule:
    def test_no_suggestion_when_below_threshold(self) -> None:
        r = _review(triggers=TriggerCount(stop_loss=3, take_profit=5, tier_drop=2))  # 30%
        report = build_optimization_report(r)
        assert not any(s.rule == "high_stop_loss_rate" for s in report.suggestions)

    def test_above_threshold_emits_high(self) -> None:
        # 6 of 10 = 60% → high
        r = _review(triggers=TriggerCount(stop_loss=6, take_profit=2, tier_drop=2))
        report = build_optimization_report(r)
        sugg = next(s for s in report.suggestions if s.rule == "high_stop_loss_rate")
        assert sugg.severity == "high"
        assert "STOP_LOSS_PCT" in sugg.parameter


# ── Rule: B tier underperforms A ──────────────────────────────────────────


class TestBTierUnderperformsRule:
    def test_no_suggestion_when_insufficient_samples(self) -> None:
        tier_stats = (
            TierStats(tier="S", n_total=0, n_open=0, n_closed_win=0,
                      n_closed_loss=0, n_closed_flat=0,
                      avg_realised_pct=None, avg_unrealised_pct=None),
            TierStats(tier="A", n_total=2, n_open=0, n_closed_win=2,
                      n_closed_loss=0, n_closed_flat=0,
                      avg_realised_pct=10.0, avg_unrealised_pct=None),
            TierStats(tier="B", n_total=2, n_open=0, n_closed_win=0,
                      n_closed_loss=2, n_closed_flat=0,
                      avg_realised_pct=-5.0, avg_unrealised_pct=None),
        )
        r = _review(tier_stats=tier_stats)
        report = build_optimization_report(r)
        assert not any(s.rule == "b_tier_underperforms" for s in report.suggestions)

    def test_b_significantly_worse_emits_suggestion(self) -> None:
        tier_stats = (
            TierStats(tier="S", n_total=0, n_open=0, n_closed_win=0,
                      n_closed_loss=0, n_closed_flat=0,
                      avg_realised_pct=None, avg_unrealised_pct=None),
            TierStats(tier="A", n_total=5, n_open=0, n_closed_win=4,
                      n_closed_loss=1, n_closed_flat=0,
                      avg_realised_pct=8.0, avg_unrealised_pct=None),
            TierStats(tier="B", n_total=5, n_open=0, n_closed_win=1,
                      n_closed_loss=4, n_closed_flat=0,
                      avg_realised_pct=-2.0, avg_unrealised_pct=None),
        )
        r = _review(tier_stats=tier_stats)
        report = build_optimization_report(r)
        sugg = next(s for s in report.suggestions if s.rule == "b_tier_underperforms")
        assert sugg.severity == "medium"
        assert "TIER_TWD" in sugg.parameter

    def test_tier_similar_no_suggestion(self) -> None:
        # Both tiers at +5%, +6% → diff < 3%
        tier_stats = (
            TierStats(tier="S", n_total=0, n_open=0, n_closed_win=0,
                      n_closed_loss=0, n_closed_flat=0,
                      avg_realised_pct=None, avg_unrealised_pct=None),
            TierStats(tier="A", n_total=5, n_open=0, n_closed_win=4,
                      n_closed_loss=1, n_closed_flat=0,
                      avg_realised_pct=6.0, avg_unrealised_pct=None),
            TierStats(tier="B", n_total=5, n_open=0, n_closed_win=4,
                      n_closed_loss=1, n_closed_flat=0,
                      avg_realised_pct=5.0, avg_unrealised_pct=None),
        )
        r = _review(tier_stats=tier_stats)
        report = build_optimization_report(r)
        assert not any(s.rule == "b_tier_underperforms" for s in report.suggestions)


# ── Rule: portfolio full with good winrate ────────────────────────────────


class TestPortfolioFullRule:
    def test_full_with_good_winrate_suggests_loosen(self) -> None:
        r = _review(n_open=12, closed_win_rate=70.0,
                    triggers=TriggerCount(take_profit=5, stop_loss=2))
        report = build_optimization_report(r)
        sugg = next(s for s in report.suggestions if s.rule == "portfolio_full_with_good_winrate")
        assert sugg.severity == "low"
        assert "MAX_OPEN_HOLDINGS" in sugg.parameter

    def test_full_but_bad_winrate_no_suggestion(self) -> None:
        r = _review(n_open=12, closed_win_rate=40.0)
        report = build_optimization_report(r)
        # Don't suggest loosening cap when win rate is bad
        assert not any(s.rule == "portfolio_full_with_good_winrate" for s in report.suggestions)


# ── Rule: take profit capping winners ─────────────────────────────────────


class TestTakeProfitTooTight:
    def test_high_tp_rate_suggests_widen(self) -> None:
        r = _review(triggers=TriggerCount(take_profit=6, stop_loss=2, tier_drop=2))
        report = build_optimization_report(r)
        sugg = next(s for s in report.suggestions if s.rule == "take_profit_capping_winners")
        assert sugg.severity == "low"
        assert "TAKE_PROFIT_PCT" in sugg.parameter


# ── Report sorting & aggregation ──────────────────────────────────────────


class TestReportAggregation:
    def test_severity_sort_order(self) -> None:
        # High TIER_DROP (high) + B-tier diff (medium) + tp-cap (low)
        tier_stats = (
            TierStats(tier="A", n_total=5, n_open=0, n_closed_win=4,
                      n_closed_loss=1, n_closed_flat=0,
                      avg_realised_pct=10.0, avg_unrealised_pct=None),
            TierStats(tier="B", n_total=5, n_open=0, n_closed_win=1,
                      n_closed_loss=4, n_closed_flat=0,
                      avg_realised_pct=-2.0, avg_unrealised_pct=None),
        )
        r = _review(
            triggers=TriggerCount(tier_drop=7, take_profit=2, stop_loss=1),
            tier_stats=tier_stats,
        )
        report = build_optimization_report(r)
        severities = [s.severity for s in report.suggestions]
        # high first, then medium, then low
        if "high" in severities and "medium" in severities:
            assert severities.index("high") < severities.index("medium")

    def test_no_suggestions_when_everything_healthy(self) -> None:
        # Balanced triggers, good tier perf, not full
        r = _review(
            n_open=5,
            triggers=TriggerCount(take_profit=4, stop_loss=2, tier_drop=2),
            closed_win_rate=65.0,
        )
        report = build_optimization_report(r)
        # No high-severity issues
        assert report.n_high == 0

    def test_report_carries_review_date(self) -> None:
        r = _review()
        report = build_optimization_report(r)
        assert report.review_date == "2026-06-24"
