"""Unit tests for BudgetAllocator — NT$3M actual capital allocation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Sequence

import pytest

from taiwan_stock_agent.domain.budget_allocator import (
    BudgetAllocator,
    PortfolioAllocation,
    PositionPlan,
)
from taiwan_stock_agent.domain.refined_picks import RefinedPick


# ── Fixtures ────────────────────────────────────────────────────────────────


def _pick(
    ticker: str = "2330",
    name: str = "台積電",
    confidence: float = 100.0,
    entry: float = 100.0,
    composite: float = 100.0,
    sector: str = "半導體業",
    industry_state: str = "HOT",
    bb_pct: float | None = None,
    concept_tailwind: bool = False,
    is_held: bool = False,
) -> RefinedPick:
    return RefinedPick(
        ticker=ticker,
        name=name,
        sector=sector,
        confidence=confidence,
        entry_price=entry,
        bb_pct=bb_pct,
        industry_state=industry_state,
        concept_keys=(),
        concept_tailwind=concept_tailwind,
        is_held=is_held,
        composite_score=composite,
        raw_result={},
    )


@dataclass
class _FakeHolding:
    """Stand-in for HoldingsRepository.Holding."""

    ticker: str
    entry_date: date = field(default_factory=lambda: date(2026, 6, 16))
    entry_price: float = 100.0
    suggested_pct: float = 15.0
    tier: str = "A"
    stop_loss: float = 93.0
    take_profit: float = 115.0
    industry: str = "半導體業"


# ── calc_shares math ────────────────────────────────────────────────────────


class TestCalcShares:
    def test_clean_lot_math(self) -> None:
        # NT$200K @ 100 → 2000 shares = 2 lots, 0 odd
        shares, lots, odd = BudgetAllocator.calc_shares(200_000, 100.0)
        assert shares == 2000
        assert lots == 2
        assert odd == 0

    def test_high_price_odd_lot_only(self) -> None:
        # NT$200K @ 18000 (信驊-like) → 11 shares = 0 lots, 11 odd
        shares, lots, odd = BudgetAllocator.calc_shares(200_000, 18000.0)
        assert shares == 11
        assert lots == 0
        assert odd == 11

    def test_mixed_lot_plus_odd(self) -> None:
        # NT$200K @ 50 → 4000 shares = 4 lots, 0 odd
        shares, lots, odd = BudgetAllocator.calc_shares(200_000, 50.0)
        assert shares == 4000
        assert lots == 4
        assert odd == 0

    def test_mixed_lot_with_remainder(self) -> None:
        # NT$199_900 @ 65 → 3075 shares = 3 lots + 75 odd
        shares, lots, odd = BudgetAllocator.calc_shares(199_900, 65.0)
        assert shares == 3075
        assert lots == 3
        assert odd == 75

    def test_zero_price_returns_zero(self) -> None:
        assert BudgetAllocator.calc_shares(200_000, 0.0) == (0, 0, 0)

    def test_zero_budget_returns_zero(self) -> None:
        assert BudgetAllocator.calc_shares(0, 100.0) == (0, 0, 0)


# ── Tier assignment ────────────────────────────────────────────────────────


class TestTierBanding:
    def test_top_3_get_s_tier(self) -> None:
        picks = [_pick(ticker=f"T{i}", composite=100.0 - i) for i in range(10)]
        tiered = BudgetAllocator._assign_tiers(picks)
        assert [t for t, _ in tiered[:3]] == ["S", "S", "S"]

    def test_next_5_get_a_tier(self) -> None:
        picks = [_pick(ticker=f"T{i}", composite=100.0 - i) for i in range(10)]
        tiered = BudgetAllocator._assign_tiers(picks)
        assert [t for t, _ in tiered[3:8]] == ["A"] * 5

    def test_rest_get_b_tier(self) -> None:
        picks = [_pick(ticker=f"T{i}", composite=100.0 - i) for i in range(10)]
        tiered = BudgetAllocator._assign_tiers(picks)
        assert [t for t, _ in tiered[8:]] == ["B", "B"]


# ── Full allocation flow ────────────────────────────────────────────────────


class TestAllocation:
    def test_empty_picks_empty_held_returns_empty_positions(self) -> None:
        alloc = BudgetAllocator().allocate(refined_picks=[], held_positions=[])
        assert alloc.positions == ()
        assert alloc.cash_reserve_twd == 3_000_000
        assert alloc.cash_pct == 100.0

    def test_single_s_tier_pick_allocates_250k(self) -> None:
        picks = [_pick(ticker="2330", entry=100.0)]
        alloc = BudgetAllocator().allocate(picks, held_positions=[])
        assert len(alloc.positions) == 1
        pos = alloc.positions[0]
        assert pos.is_held is False
        assert pos.tier == "S"
        assert pos.target_twd == 250_000
        # 250K / 100 = 2500 shares = 2 lots + 500 odd
        assert pos.shares == 2500
        assert pos.lots == 2
        assert pos.odd_shares == 500
        assert pos.actual_twd == 250_000

    def test_held_position_emitted_as_held(self) -> None:
        held = [_FakeHolding(ticker="2330", entry_price=100.0, suggested_pct=15.0, tier="A")]
        alloc = BudgetAllocator().allocate(refined_picks=[], held_positions=held)
        assert len(alloc.positions) == 1
        assert alloc.positions[0].is_held is True
        assert alloc.positions[0].ticker == "2330"
        assert alloc.held_value_twd == 450_000  # 15% × 3M

    def test_held_ticker_not_re_bought(self) -> None:
        held = [_FakeHolding(ticker="2330", suggested_pct=15.0)]
        picks = [_pick(ticker="2330"), _pick(ticker="2454")]
        alloc = BudgetAllocator().allocate(picks, held)
        # 2330 only emits once (as held), 2454 emits as new buy
        tickers = [p.ticker for p in alloc.positions]
        assert tickers.count("2330") == 1
        assert tickers.count("2454") == 1
        held_tickers = {p.ticker for p in alloc.held_positions}
        new_buy_tickers = {p.ticker for p in alloc.new_buy_positions}
        assert "2330" in held_tickers
        assert "2454" in new_buy_tickers

    def test_max_positions_enforced(self) -> None:
        # 13 picks, only 12 should fit (MAX_POSITIONS=12)
        picks = [_pick(ticker=f"{i:04d}", entry=100.0) for i in range(13)]
        alloc = BudgetAllocator().allocate(picks, held_positions=[])
        assert len(alloc.new_buy_positions) == 12
        assert len(alloc.skipped_picks) == 1

    def test_held_eats_into_capacity(self) -> None:
        # 11 held + 5 picks → only 1 slot left, 4 picks skipped
        held = [_FakeHolding(ticker=f"H{i:03d}", suggested_pct=1.0) for i in range(11)]
        picks = [_pick(ticker=f"N{i:03d}") for i in range(5)]
        alloc = BudgetAllocator().allocate(picks, held)
        assert len(alloc.new_buy_positions) == 1
        assert len(alloc.skipped_picks) == 4

    def test_min_position_floor_skips_when_budget_too_low(self) -> None:
        # Force tiny budget so nothing meets MIN_TWD_PER_POSITION
        picks = [_pick(ticker="2330")]
        alloc = BudgetAllocator().allocate(picks, held_positions=[], budget=50_000)
        # 50K * 0.85 = 42.5K available, less than 100K min
        assert len(alloc.new_buy_positions) == 0
        assert len(alloc.skipped_picks) == 1

    def test_cash_reserve_at_least_15pct(self) -> None:
        # Many picks but budget capped → cash >= 15%
        picks = [_pick(ticker=f"{i:04d}", entry=100.0) for i in range(12)]
        alloc = BudgetAllocator().allocate(picks, held_positions=[])
        assert alloc.cash_pct >= 14.9   # allow tiny floating drift

    def test_high_price_stock_uses_odd_lots(self) -> None:
        # 信驊-like NT$19000 stock — B tier gets NT$120K target = 6 shares
        picks = [
            _pick(ticker=f"{i:04d}", entry=100.0, composite=200 - i)
            for i in range(8)  # 8 picks → top 3 S, next 5 A, 信驊 in B
        ]
        picks.append(_pick(ticker="5274", entry=19000.0, composite=10.0))
        alloc = BudgetAllocator().allocate(picks, held_positions=[])
        xinwei = next(p for p in alloc.positions if p.ticker == "5274")
        assert xinwei.tier == "B"
        # 120K / 19000 = 6 shares, all odd
        assert xinwei.shares == 6
        assert xinwei.lots == 0
        assert xinwei.odd_shares == 6
        assert xinwei.actual_twd == 114_000

    def test_skipped_zero_entry_price(self) -> None:
        picks = [_pick(ticker="2330", entry=0.0)]
        alloc = BudgetAllocator().allocate(picks, held_positions=[])
        assert len(alloc.new_buy_positions) == 0
        assert len(alloc.skipped_picks) == 1

    def test_stop_loss_and_take_profit_set(self) -> None:
        picks = [_pick(ticker="2330", entry=100.0)]
        alloc = BudgetAllocator().allocate(picks, held_positions=[])
        pos = alloc.positions[0]
        assert pos.stop_loss == 93.0
        assert pos.take_profit == 115.0


# ── PortfolioAllocation properties ─────────────────────────────────────────


class TestPortfolioProperties:
    def test_total_invested_is_held_plus_new(self) -> None:
        held = [_FakeHolding(ticker="2330", suggested_pct=10.0)]   # 300K held
        picks = [_pick(ticker="2454", entry=100.0)]                # 250K new
        alloc = BudgetAllocator().allocate(picks, held)
        assert alloc.total_invested_twd == alloc.held_value_twd + alloc.new_buys_twd

    def test_position_count_field(self) -> None:
        picks = [_pick(ticker=f"{i:04d}") for i in range(5)]
        alloc = BudgetAllocator().allocate(picks, held_positions=[])
        assert alloc.n_positions == 5

    def test_held_and_new_buy_split(self) -> None:
        held = [_FakeHolding(ticker="HELD1"), _FakeHolding(ticker="HELD2")]
        picks = [_pick(ticker="NEW1"), _pick(ticker="NEW2")]
        alloc = BudgetAllocator().allocate(picks, held)
        assert len(alloc.held_positions) == 2
        assert len(alloc.new_buy_positions) == 2

    def test_rationale_includes_tier_and_confidence(self) -> None:
        picks = [_pick(ticker="2330", confidence=95.0, industry_state="HOT")]
        alloc = BudgetAllocator().allocate(picks, held_positions=[])
        pos = alloc.positions[0]
        assert "S 級" in pos.rationale
        assert "信心" in pos.rationale
        assert "HOT" in pos.rationale
