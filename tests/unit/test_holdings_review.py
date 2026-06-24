"""Unit tests for daily holdings review."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import pytest

from taiwan_stock_agent.domain.holdings_review import (
    HoldingsReview,
    RiskWarning,
    TierStats,
    TriggerCount,
    build_review,
    generate_review_narrative,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@dataclass
class _FakeHolding:
    """Stand-in for the Holding namedtuple from holdings_repository."""

    ticker: str
    entry_price: float = 100.0
    entry_date: date = field(default_factory=lambda: date(2026, 6, 16))
    suggested_pct: float = 10.0
    tier: str = "A"
    stop_loss: float = 93.0
    take_profit: float = 115.0
    status: str = "OPEN"
    close_date: Optional[date] = None
    close_price: Optional[float] = None
    close_reason: Optional[str] = None
    realised_pct: Optional[float] = None


# ── Trigger counting ───────────────────────────────────────────────────────


class TestTriggerCount:
    def test_all_zero_when_no_closed(self) -> None:
        review = build_review(
            today=date(2026, 6, 24), open_holdings=[], closed_in_period=[],
            prices_today={},
        )
        assert review.triggers.total == 0

    def test_counts_each_reason(self) -> None:
        closed = [
            _FakeHolding(ticker="A", status="CLOSED", close_reason="STOP_LOSS",
                         realised_pct=-5.5),
            _FakeHolding(ticker="B", status="CLOSED", close_reason="STOP_LOSS",
                         realised_pct=-7.0),
            _FakeHolding(ticker="C", status="CLOSED", close_reason="TAKE_PROFIT",
                         realised_pct=15.0),
            _FakeHolding(ticker="D", status="CLOSED", close_reason="TIER_DROP",
                         realised_pct=-1.0),
        ]
        review = build_review(
            today=date(2026, 6, 24), open_holdings=[],
            closed_in_period=closed, prices_today={},
        )
        assert review.triggers.stop_loss == 2
        assert review.triggers.take_profit == 1
        assert review.triggers.tier_drop == 1
        assert review.triggers.time_stop == 0
        assert review.triggers.total == 4

    def test_tier_drop_rate_threshold(self) -> None:
        # 5 of 10 closes are TIER_DROP → 50% — should signal trouble
        closed = [
            _FakeHolding(ticker=f"T{i}", status="CLOSED",
                         close_reason="TIER_DROP" if i < 5 else "TAKE_PROFIT",
                         realised_pct=-2.0 if i < 5 else 8.0)
            for i in range(10)
        ]
        review = build_review(
            today=date(2026, 6, 24), open_holdings=[],
            closed_in_period=closed, prices_today={},
        )
        assert review.triggers.tier_drop_rate == 0.5

    def test_unknown_reason_counted_as_manual(self) -> None:
        closed = [_FakeHolding(ticker="A", status="CLOSED",
                                close_reason="WEIRD_REASON",
                                realised_pct=0.0)]
        review = build_review(
            today=date(2026, 6, 24), open_holdings=[],
            closed_in_period=closed, prices_today={},
        )
        assert review.triggers.manual == 1


# ── Portfolio P&L weighted ─────────────────────────────────────────────────


class TestPortfolioPnL:
    def test_weighted_unrealised_pct(self) -> None:
        open_h = [
            _FakeHolding(ticker="A", entry_price=100.0, suggested_pct=10.0),  # 20%
            _FakeHolding(ticker="B", entry_price=100.0, suggested_pct=10.0),  # 10%
        ]
        prices = {"A": 110.0, "B": 105.0}  # +10%, +5%
        review = build_review(
            today=date(2026, 6, 24), open_holdings=open_h,
            closed_in_period=[], prices_today=prices,
        )
        # Equal weights → (10 + 5) / 2 = 7.5
        assert review.portfolio_unrealised_pct == 7.5

    def test_weighted_uses_suggested_pct(self) -> None:
        open_h = [
            _FakeHolding(ticker="A", entry_price=100.0, suggested_pct=15.0),  # +10% × 15
            _FakeHolding(ticker="B", entry_price=100.0, suggested_pct=5.0),   # -2% ×  5
        ]
        prices = {"A": 110.0, "B": 98.0}
        review = build_review(
            today=date(2026, 6, 24), open_holdings=open_h,
            closed_in_period=[], prices_today=prices,
        )
        # (10*15 + -2*5) / 20 = (150 - 10) / 20 = 7.0
        assert review.portfolio_unrealised_pct == 7.0

    def test_unrealised_twd_calculated(self) -> None:
        # 15% of 3M = NT$450K position at entry=100. price=110 → +10% = NT$45K gain
        open_h = [_FakeHolding(ticker="A", entry_price=100.0, suggested_pct=15.0)]
        prices = {"A": 110.0}
        review = build_review(
            today=date(2026, 6, 24), open_holdings=open_h,
            closed_in_period=[], prices_today=prices,
            budget_twd=3_000_000,
        )
        # shares = 450000 / 100 = 4500; gain = 10 * 4500 = 45000
        assert review.portfolio_unrealised_twd == 45_000

    def test_missing_price_skipped(self) -> None:
        open_h = [
            _FakeHolding(ticker="A", entry_price=100.0),
            _FakeHolding(ticker="B", entry_price=100.0),
        ]
        prices = {"A": 110.0}  # B has no price (weekend / data gap)
        review = build_review(
            today=date(2026, 6, 24), open_holdings=open_h,
            closed_in_period=[], prices_today=prices,
        )
        # Only A contributes
        assert "A" in review.open_pnl_by_ticker
        assert "B" not in review.open_pnl_by_ticker
        assert review.portfolio_unrealised_pct == 10.0


# ── Win rate ────────────────────────────────────────────────────────────────


class TestWinRate:
    def test_no_closed_returns_none(self) -> None:
        review = build_review(
            today=date(2026, 6, 24), open_holdings=[],
            closed_in_period=[], prices_today={},
        )
        assert review.closed_win_rate is None

    def test_all_winners_100pct(self) -> None:
        closed = [
            _FakeHolding(ticker=f"T{i}", status="CLOSED",
                         close_reason="TAKE_PROFIT", realised_pct=10.0)
            for i in range(3)
        ]
        review = build_review(
            today=date(2026, 6, 24), open_holdings=[],
            closed_in_period=closed, prices_today={},
        )
        assert review.closed_win_rate == 100.0

    def test_flat_excluded_from_win_rate(self) -> None:
        closed = [
            _FakeHolding(ticker="A", status="CLOSED", close_reason="MANUAL",
                         realised_pct=5.0),     # win
            _FakeHolding(ticker="B", status="CLOSED", close_reason="MANUAL",
                         realised_pct=-5.0),    # loss
            _FakeHolding(ticker="C", status="CLOSED", close_reason="MANUAL",
                         realised_pct=0.1),     # flat (excluded)
        ]
        review = build_review(
            today=date(2026, 6, 24), open_holdings=[],
            closed_in_period=closed, prices_today={},
        )
        assert review.closed_win_rate == 50.0


# ── Tier breakdown ────────────────────────────────────────────────────────


class TestTierBreakdown:
    def test_separates_by_tier(self) -> None:
        open_h = [
            _FakeHolding(ticker="S1", tier="S"),
            _FakeHolding(ticker="A1", tier="A"),
            _FakeHolding(ticker="A2", tier="A"),
            _FakeHolding(ticker="B1", tier="B"),
        ]
        review = build_review(
            today=date(2026, 6, 24), open_holdings=open_h,
            closed_in_period=[], prices_today={
                "S1": 100, "A1": 100, "A2": 100, "B1": 100,
            },
        )
        s_stat = next(t for t in review.tier_stats if t.tier == "S")
        a_stat = next(t for t in review.tier_stats if t.tier == "A")
        b_stat = next(t for t in review.tier_stats if t.tier == "B")
        assert s_stat.n_open == 1
        assert a_stat.n_open == 2
        assert b_stat.n_open == 1

    def test_tier_win_rate_split(self) -> None:
        closed = [
            _FakeHolding(ticker="A1", tier="A", status="CLOSED",
                         realised_pct=10.0, close_reason="TAKE_PROFIT"),
            _FakeHolding(ticker="A2", tier="A", status="CLOSED",
                         realised_pct=8.0, close_reason="MANUAL"),
            _FakeHolding(ticker="B1", tier="B", status="CLOSED",
                         realised_pct=-5.0, close_reason="STOP_LOSS"),
            _FakeHolding(ticker="B2", tier="B", status="CLOSED",
                         realised_pct=2.0, close_reason="MANUAL"),
        ]
        review = build_review(
            today=date(2026, 6, 24), open_holdings=[],
            closed_in_period=closed, prices_today={},
        )
        a = next(t for t in review.tier_stats if t.tier == "A")
        b = next(t for t in review.tier_stats if t.tier == "B")
        assert a.closed_win_rate == 100.0
        assert b.closed_win_rate == 50.0
        assert a.avg_realised_pct == 9.0
        assert b.avg_realised_pct == -1.5


# ── Risk warnings ─────────────────────────────────────────────────────────


class TestRiskWarnings:
    def test_near_stop_loss_high_severity(self) -> None:
        # entry 100, stop 93, current 94 → 1.08% above stop → high severity
        open_h = [_FakeHolding(ticker="A", entry_price=100.0, stop_loss=93.0)]
        review = build_review(
            today=date(2026, 6, 24), open_holdings=open_h,
            closed_in_period=[], prices_today={"A": 94.0},
            name_map={"A": "AlphaCo"},
        )
        assert len(review.risk_warnings) == 1
        w = review.risk_warnings[0]
        assert w.severity == "high"
        assert w.category == "near_stop"
        assert w.name == "AlphaCo"
        assert "距停損" in w.message

    def test_far_from_stop_no_warning(self) -> None:
        # current 110, stop 93 → 18% above stop, safe
        open_h = [_FakeHolding(ticker="A", entry_price=100.0, stop_loss=93.0)]
        review = build_review(
            today=date(2026, 6, 24), open_holdings=open_h,
            closed_in_period=[], prices_today={"A": 110.0},
        )
        assert not any(w.category == "near_stop" for w in review.risk_warnings)

    def test_near_time_stop_warning(self) -> None:
        # Held 9 days, still below entry × 1.05
        open_h = [_FakeHolding(
            ticker="A", entry_price=100.0, entry_date=date(2026, 6, 15),
            stop_loss=93.0,
        )]
        review = build_review(
            today=date(2026, 6, 24),   # 9 days later
            open_holdings=open_h, closed_in_period=[],
            prices_today={"A": 102.0},  # < 105 threshold
        )
        time_warns = [w for w in review.risk_warnings if w.category == "near_time_stop"]
        assert len(time_warns) == 1
        assert time_warns[0].severity == "medium"

    def test_time_stop_not_triggered_when_profitable(self) -> None:
        open_h = [_FakeHolding(
            ticker="A", entry_price=100.0, entry_date=date(2026, 6, 15),
            stop_loss=93.0,
        )]
        review = build_review(
            today=date(2026, 6, 24),
            open_holdings=open_h, closed_in_period=[],
            prices_today={"A": 108.0},  # > 105 threshold → safe
        )
        assert not any(w.category == "near_time_stop" for w in review.risk_warnings)

    def test_near_take_profit_low_severity(self) -> None:
        # entry 100, take_profit 115, current 114 → 0.88% below TP
        open_h = [_FakeHolding(ticker="A", entry_price=100.0,
                               take_profit=115.0, stop_loss=93.0)]
        review = build_review(
            today=date(2026, 6, 24), open_holdings=open_h,
            closed_in_period=[], prices_today={"A": 114.0},
        )
        tp_warns = [w for w in review.risk_warnings if w.category == "near_take"]
        assert len(tp_warns) == 1
        assert tp_warns[0].severity == "low"


# ── Alpha calculation ─────────────────────────────────────────────────────


class TestAlpha:
    def test_alpha_positive(self) -> None:
        open_h = [_FakeHolding(ticker="A", entry_price=100.0)]
        review = build_review(
            today=date(2026, 6, 24), open_holdings=open_h,
            closed_in_period=[], prices_today={"A": 105.0},
            taiex_return_pct=2.0,
        )
        # portfolio +5%, TAIEX +2% → alpha +3%
        assert review.alpha_pct == 3.0

    def test_alpha_none_when_no_taiex(self) -> None:
        open_h = [_FakeHolding(ticker="A", entry_price=100.0)]
        review = build_review(
            today=date(2026, 6, 24), open_holdings=open_h,
            closed_in_period=[], prices_today={"A": 105.0},
            taiex_return_pct=None,
        )
        assert review.alpha_pct is None


# ── Narrative (rule-based fallback) ──────────────────────────────────────


class TestRuleBasedNarrative:
    def test_no_positions_says_so(self) -> None:
        review = build_review(
            today=date(2026, 6, 24), open_holdings=[],
            closed_in_period=[], prices_today={},
        )
        text = generate_review_narrative(review)
        assert "無持倉" in text

    def test_winning_portfolio_narrative(self) -> None:
        open_h = [_FakeHolding(ticker="A", entry_price=100.0, suggested_pct=15.0)]
        review = build_review(
            today=date(2026, 6, 24), open_holdings=open_h,
            closed_in_period=[], prices_today={"A": 110.0},
            taiex_return_pct=5.0,
        )
        text = generate_review_narrative(review)
        assert "持倉 1 支" in text
        assert "+10.00%" in text or "賺 +10" in text or "+10" in text

    def test_high_tier_drop_rate_warning_included(self) -> None:
        # 6 of 7 closes are TIER_DROP → 86%
        closed = [
            _FakeHolding(ticker=f"T{i}", status="CLOSED",
                         close_reason="TIER_DROP", realised_pct=-1.0)
            for i in range(6)
        ]
        closed.append(_FakeHolding(ticker="OK", status="CLOSED",
                                    close_reason="TAKE_PROFIT", realised_pct=10.0))
        review = build_review(
            today=date(2026, 6, 24), open_holdings=[],
            closed_in_period=closed, prices_today={},
        )
        text = generate_review_narrative(review)
        assert "TIER_DROP" in text
        assert "MIN_CONFIDENCE" in text

    def test_llm_used_when_provided(self) -> None:
        review = build_review(
            today=date(2026, 6, 24), open_holdings=[],
            closed_in_period=[], prices_today={},
        )

        class _FakeLLM:
            def complete(self, prompt: str, max_tokens: int = 500) -> str:
                return "LLM 自訂回應內容超過 20 字元，確保被採用"

        text = generate_review_narrative(review, llm=_FakeLLM())
        assert "LLM 自訂回應內容" in text

    def test_llm_failure_falls_back_to_rule_based(self) -> None:
        review = build_review(
            today=date(2026, 6, 24), open_holdings=[],
            closed_in_period=[], prices_today={},
        )

        class _BrokenLLM:
            def complete(self, prompt: str, max_tokens: int = 500) -> str:
                raise RuntimeError("API down")

        text = generate_review_narrative(review, llm=_BrokenLLM())
        # Falls back to rule-based "無持倉"
        assert "無持倉" in text

    def test_llm_empty_response_falls_back(self) -> None:
        review = build_review(
            today=date(2026, 6, 24), open_holdings=[],
            closed_in_period=[], prices_today={},
        )

        class _EmptyLLM:
            def complete(self, prompt: str, max_tokens: int = 500) -> str:
                return "ok"  # too short

        text = generate_review_narrative(review, llm=_EmptyLLM())
        assert "無持倉" in text
