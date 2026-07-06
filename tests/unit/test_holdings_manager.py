"""Unit tests for HoldingsManager + holdings_repository exit rules."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from taiwan_stock_agent.agents.holdings_manager import (
    DailyPortfolio,
    HoldingsManager,
    NewBuy,
    HoldingSnapshot,
)
from taiwan_stock_agent.domain.capital_allocator import (
    AllocationPlan,
    TierRecommendation,
)
from taiwan_stock_agent.infrastructure.holdings_repository import (
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    DEFAULT_TIME_STOP_DAYS,
    ExitDecision,
    Holding,
    HoldingsRepository,
    evaluate_exit,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _holding(
    ticker: str = "2330",
    entry_price: float = 100.0,
    entry_date: date = date(2026, 6, 1),
    tier: str = "A",
    suggested_pct: float = 15.0,
) -> Holding:
    sl = round(entry_price * (1 - DEFAULT_STOP_LOSS_PCT), 2)
    tp = round(entry_price * (1 + DEFAULT_TAKE_PROFIT_PCT), 2)
    return Holding(
        holding_id=1,
        ticker=ticker,
        entry_date=entry_date,
        entry_price=entry_price,
        suggested_pct=suggested_pct,
        tier=tier,
        stop_loss=sl,
        take_profit=tp,
        industry="半導體業",
        concept_keys=(),
        entry_reason="test",
        status="OPEN",
    )


def _empty_plan() -> AllocationPlan:
    return AllocationPlan(
        tiers={t: [] for t in ("S", "A", "B", "C")},
        warnings=(),
        summary="test",
        provider="test",
        snapshot_date="2026-06-16",
    )


# ── evaluate_exit ───────────────────────────────────────────────────────────


class TestExitRules:
    def test_stop_loss_triggered(self) -> None:
        h = _holding(entry_price=100.0)
        # stop_loss = 93.0, price below
        dec = evaluate_exit(h, current_price=92.0, today=date(2026, 6, 5))
        assert dec.should_close
        assert dec.close_reason == "STOP_LOSS"

    def test_take_profit_triggered(self) -> None:
        h = _holding(entry_price=100.0)
        # take_profit = 115.0, price above
        dec = evaluate_exit(h, current_price=116.0, today=date(2026, 6, 5))
        assert dec.should_close
        assert dec.close_reason == "TAKE_PROFIT"

    def test_time_stop_after_10_days_with_low_return(self) -> None:
        h = _holding(entry_price=100.0, entry_date=date(2026, 5, 20))
        # entry 5/20, today 6/5 → 16 cal days → ~12 weekdays
        dec = evaluate_exit(h, current_price=102.0, today=date(2026, 6, 5))
        assert dec.should_close
        assert dec.close_reason == "TIME_STOP"

    def test_no_time_stop_if_already_profitable_enough(self) -> None:
        h = _holding(entry_price=100.0, entry_date=date(2026, 5, 20))
        # price > 105 → no time stop
        dec = evaluate_exit(h, current_price=108.0, today=date(2026, 6, 5))
        assert not dec.should_close

    def test_tier_drop_triggered_when_conf_collapses(self) -> None:
        # Phase 4.50.7: TIER_DROP now requires conf<20 AND held_days>=5
        # (was single-day <30). This test uses 6/10 → 6/18 (~6 weekdays)
        # with conf=15 to trigger.
        h = _holding(entry_price=100.0, entry_date=date(2026, 6, 10))
        dec = evaluate_exit(h, current_price=98.0, today=date(2026, 6, 18),
                            tce_confidence_today=15.0)
        assert dec.should_close
        assert dec.close_reason == "TIER_DROP"

    def test_tier_drop_NOT_triggered_before_cooldown(self) -> None:
        # New guard: don't fire TIER_DROP within first 5 days even if
        # conf collapses (avoids day-2 whipsaw exit).
        h = _holding(entry_price=100.0, entry_date=date(2026, 6, 10))
        dec = evaluate_exit(h, current_price=98.0, today=date(2026, 6, 12),
                            tce_confidence_today=15.0)
        assert not dec.should_close

    def test_tier_drop_NOT_triggered_at_conf_25(self) -> None:
        # Old threshold was <30, new is <20. conf=25 should no longer fire.
        h = _holding(entry_price=100.0, entry_date=date(2026, 6, 10))
        dec = evaluate_exit(h, current_price=98.0, today=date(2026, 6, 20),
                            tce_confidence_today=25.0)
        assert not dec.should_close

    def test_no_exit_when_within_bounds(self) -> None:
        h = _holding(entry_price=100.0, entry_date=date(2026, 6, 10))
        dec = evaluate_exit(h, current_price=102.0, today=date(2026, 6, 12),
                            tce_confidence_today=85.0)
        assert not dec.should_close

    def test_stop_loss_priority_over_take_profit(self) -> None:
        # Both stop loss and take profit shouldn't both trigger; stop wins.
        # Phase 4.50.7: stop_loss default -8% so entry 100 → stop 92.0
        h = _holding(entry_price=100.0)
        dec = evaluate_exit(h, current_price=92.0, today=date(2026, 6, 5))
        assert dec.should_close
        assert dec.close_reason == "STOP_LOSS"


# ── HoldingsManager.process_day ─────────────────────────────────────────────


class _FakeRepo:
    """In-memory HoldingsRepository test double."""

    def __init__(self, initial_open: list[Holding] | None = None) -> None:
        self._open: dict[int, Holding] = {h.holding_id: h for h in (initial_open or [])}
        self._closed: list[tuple] = []
        self._next_id = max(self._open.keys(), default=0) + 1
        self.available = True

    def list_open(self) -> list[Holding]:
        return list(self._open.values())

    def get(self, ticker: str) -> Holding | None:
        for h in self._open.values():
            if h.ticker == ticker:
                return h
        return None

    def open_position(self, *, ticker, entry_date, entry_price, suggested_pct,
                      tier, industry="", concept_keys=(), entry_reason="",
                      stop_loss_pct=DEFAULT_STOP_LOSS_PCT,
                      take_profit_pct=DEFAULT_TAKE_PROFIT_PCT) -> int:
        sl = round(entry_price * (1 - stop_loss_pct), 2)
        tp = round(entry_price * (1 + take_profit_pct), 2)
        hid = self._next_id
        self._next_id += 1
        h = Holding(
            holding_id=hid, ticker=ticker, entry_date=entry_date,
            entry_price=entry_price, suggested_pct=suggested_pct, tier=tier,
            stop_loss=sl, take_profit=tp, industry=industry,
            concept_keys=tuple(concept_keys), entry_reason=entry_reason,
            status="OPEN",
        )
        self._open[hid] = h
        return hid

    def close_position(self, holding_id, *, close_date, close_price, close_reason,
                       entry_price, notes="") -> bool:
        h = self._open.pop(holding_id, None)
        if h is None:
            return False
        realised = round((close_price - entry_price) / entry_price * 100, 2)
        self._closed.append((holding_id, close_reason, realised))
        return True


class TestProcessDay:
    def test_no_open_holdings_no_new_buys_returns_empty_portfolio(self) -> None:
        repo = _FakeRepo()
        mgr = HoldingsManager(repository=repo)  # type: ignore[arg-type]
        plan = _empty_plan()
        dp = mgr.process_day(today=date(2026, 6, 16), plan=plan,
                             prices_today={}, commit=False)
        assert dp.holdings == ()
        assert dp.new_buys == ()
        assert dp.pending_exits == ()
        assert dp.total_invested_pct == 0.0
        assert dp.cash_pct == 100.0

    def test_open_position_held_with_pnl(self) -> None:
        h = _holding(entry_price=100.0, entry_date=date(2026, 6, 10))
        repo = _FakeRepo([h])
        mgr = HoldingsManager(repository=repo)  # type: ignore[arg-type]
        plan = _empty_plan()
        dp = mgr.process_day(
            today=date(2026, 6, 12), plan=plan,
            prices_today={"2330": 104.0},
            confidences_today={"2330": 80.0},
            commit=False,
        )
        assert len(dp.holdings) == 1
        snap = dp.holdings[0]
        assert snap.unrealised_pct == pytest.approx(4.0)
        assert not snap.exit_decision.should_close
        assert dp.portfolio_unrealised_pct == pytest.approx(4.0)

    def test_open_position_triggers_stop_loss(self) -> None:
        h = _holding(entry_price=100.0, entry_date=date(2026, 6, 10))
        repo = _FakeRepo([h])
        mgr = HoldingsManager(repository=repo)  # type: ignore[arg-type]
        plan = _empty_plan()
        dp = mgr.process_day(
            today=date(2026, 6, 12), plan=plan,
            prices_today={"2330": 90.0},   # below stop_loss 93
            commit=True,
        )
        assert len(dp.pending_exits) == 1
        assert dp.pending_exits[0].exit_decision.close_reason == "STOP_LOSS"
        # Position should be closed in repo
        assert repo.list_open() == []
        assert repo._closed[0][1] == "STOP_LOSS"

    def test_new_buy_for_fresh_a_tier_pick(self) -> None:
        repo = _FakeRepo()
        mgr = HoldingsManager(repository=repo)  # type: ignore[arg-type]
        plan = AllocationPlan(
            tiers={
                "S": [],
                "A": [TierRecommendation(ticker="2330", tier="A",
                                          suggested_pct=15.0,
                                          reasoning="strong setup",
                                          rotation_score=80.0)],
                "B": [], "C": [],
            },
            warnings=(), summary="", provider="test", snapshot_date="2026-06-16",
        )
        dp = mgr.process_day(
            today=date(2026, 6, 16), plan=plan,
            prices_today={"2330": 100.0},
            confidences_today={"2330": 85.0},
            name_map={"2330": "台積電"},
            industry_map={"2330": "半導體業"},
            commit=True,
        )
        assert len(dp.new_buys) == 1
        buy = dp.new_buys[0]
        assert buy.ticker == "2330"
        assert buy.tier == "A"
        assert buy.entry_price == 100.0
        assert buy.stop_loss == 92.0  # Phase 4.50.7: -8% stop
        assert buy.take_profit == 115.0
        # Position should be opened in repo
        assert len(repo.list_open()) == 1

    def test_existing_holding_not_re_recommended_as_new_buy(self) -> None:
        h = _holding(ticker="2330", entry_price=100.0, entry_date=date(2026, 6, 10))
        repo = _FakeRepo([h])
        mgr = HoldingsManager(repository=repo)  # type: ignore[arg-type]
        plan = AllocationPlan(
            tiers={
                "S": [],
                "A": [TierRecommendation(ticker="2330", tier="A",
                                          suggested_pct=15.0, reasoning="x",
                                          rotation_score=80.0)],
                "B": [], "C": [],
            },
            warnings=(), summary="", provider="test", snapshot_date="2026-06-16",
        )
        dp = mgr.process_day(
            today=date(2026, 6, 12), plan=plan,
            prices_today={"2330": 105.0},
            commit=False,
        )
        # Held, not re-bought
        assert len(dp.new_buys) == 0
        assert len(dp.holdings) == 1

    def test_invested_pct_and_cash_pct_sum_to_100(self) -> None:
        h1 = _holding(ticker="2330", suggested_pct=20.0)
        h2 = Holding(
            holding_id=2, ticker="2317", entry_date=date(2026, 6, 1),
            entry_price=100.0, suggested_pct=15.0, tier="B",
            stop_loss=92.0, take_profit=115.0, industry="", concept_keys=(),
            entry_reason="", status="OPEN",
        )
        repo = _FakeRepo([h1, h2])
        mgr = HoldingsManager(repository=repo)  # type: ignore[arg-type]
        dp = mgr.process_day(
            today=date(2026, 6, 12), plan=_empty_plan(),
            prices_today={"2330": 100.0, "2317": 100.0},
            commit=False,
        )
        assert dp.total_invested_pct == 35.0
        assert dp.cash_pct == 65.0

    def test_skipped_recs_capture_c_tier_with_positive_pct(self) -> None:
        repo = _FakeRepo()
        mgr = HoldingsManager(repository=repo)  # type: ignore[arg-type]
        plan = AllocationPlan(
            tiers={
                "S": [], "A": [], "B": [],
                "C": [TierRecommendation(ticker="9999", tier="C",
                                          suggested_pct=2.0, reasoning="x",
                                          rotation_score=30.0)],
            },
            warnings=(), summary="", provider="test", snapshot_date="2026-06-16",
        )
        dp = mgr.process_day(
            today=date(2026, 6, 16), plan=plan,
            prices_today={"9999": 50.0},
            commit=False,
        )
        assert len(dp.skipped_recs) == 1
        assert dp.skipped_recs[0].ticker == "9999"

    def test_position_with_no_price_today_kept_open(self) -> None:
        h = _holding(entry_price=100.0, entry_date=date(2026, 6, 10))
        repo = _FakeRepo([h])
        mgr = HoldingsManager(repository=repo)  # type: ignore[arg-type]
        dp = mgr.process_day(
            today=date(2026, 6, 12), plan=_empty_plan(),
            prices_today={},   # no price for 2330
            commit=True,
        )
        # Held with 0 P&L, not closed
        assert len(dp.holdings) == 1
        assert dp.holdings[0].unrealised_pct == 0.0
        assert not dp.holdings[0].exit_decision.should_close
        assert repo.list_open() == [h]


# ── Phase 4.50: capacity cap ────────────────────────────────────────────────


class TestMaxOpenHoldings:
    """MAX_OPEN_HOLDINGS prevents portfolio ballooning past 12 positions."""

    def test_picks_exceeding_capacity_go_to_watchlist(self) -> None:
        # 11 already held + 5 new picks → only 1 slot left, 4 go to watchlist
        held = [
            Holding(
                holding_id=i, ticker=f"H{i:03d}",
                entry_date=date(2026, 6, 16),
                entry_price=100.0, suggested_pct=8.0, tier="B",
                stop_loss=92.0, take_profit=115.0, industry="", concept_keys=(),
                entry_reason="", status="OPEN",
            )
            for i in range(11)
        ]
        repo = _FakeRepo(held)
        mgr = HoldingsManager(repository=repo)  # type: ignore[arg-type]
        plan = AllocationPlan(
            tiers={
                "S": [], "B": [], "C": [],
                "A": [
                    TierRecommendation(ticker=f"NEW{i}", tier="A",
                                       suggested_pct=15.0, reasoning="x",
                                       rotation_score=70.0)
                    for i in range(5)
                ],
            },
            warnings=(), summary="", provider="t", snapshot_date="x",
        )
        prices = {f"NEW{i}": 100.0 for i in range(5)}
        prices.update({f"H{i:03d}": 100.0 for i in range(11)})
        dp = mgr.process_day(
            today=date(2026, 6, 16), plan=plan,
            prices_today=prices, commit=False,
        )
        assert len(dp.new_buys) == 1, "Only 1 slot should remain (11 held + 1 = 12 cap)"
        assert len(dp.watchlist_picks) == 4
        # Watchlist tickers are the leftover NEW1..NEW4
        wl_tickers = {b.ticker for b in dp.watchlist_picks}
        assert len(wl_tickers) == 4

    def test_full_portfolio_all_picks_to_watchlist(self) -> None:
        # 12 already held = at cap, all new picks go to watchlist
        held = [
            Holding(
                holding_id=i, ticker=f"H{i:03d}",
                entry_date=date(2026, 6, 16),
                entry_price=100.0, suggested_pct=8.0, tier="B",
                stop_loss=92.0, take_profit=115.0, industry="", concept_keys=(),
                entry_reason="", status="OPEN",
            )
            for i in range(12)
        ]
        repo = _FakeRepo(held)
        mgr = HoldingsManager(repository=repo)  # type: ignore[arg-type]
        plan = AllocationPlan(
            tiers={
                "S": [], "B": [], "C": [],
                "A": [
                    TierRecommendation(ticker="NEW1", tier="A",
                                       suggested_pct=15.0, reasoning="x",
                                       rotation_score=70.0),
                ],
            },
            warnings=(), summary="", provider="t", snapshot_date="x",
        )
        prices = {"NEW1": 100.0}
        prices.update({f"H{i:03d}": 100.0 for i in range(12)})
        dp = mgr.process_day(
            today=date(2026, 6, 16), plan=plan,
            prices_today=prices, commit=False,
        )
        assert len(dp.new_buys) == 0
        assert len(dp.watchlist_picks) == 1

    def test_watchlist_not_written_to_db(self) -> None:
        # 12 held, 1 new pick → goes to watchlist; repo should not have it
        held = [
            Holding(
                holding_id=i, ticker=f"H{i:03d}",
                entry_date=date(2026, 6, 16),
                entry_price=100.0, suggested_pct=8.0, tier="B",
                stop_loss=92.0, take_profit=115.0, industry="", concept_keys=(),
                entry_reason="", status="OPEN",
            )
            for i in range(12)
        ]
        repo = _FakeRepo(held)
        mgr = HoldingsManager(repository=repo)  # type: ignore[arg-type]
        plan = AllocationPlan(
            tiers={
                "S": [], "B": [], "C": [],
                "A": [TierRecommendation(ticker="NEW1", tier="A",
                                          suggested_pct=15.0, reasoning="x",
                                          rotation_score=70.0)],
            },
            warnings=(), summary="", provider="t", snapshot_date="x",
        )
        prices = {"NEW1": 100.0}
        prices.update({f"H{i:03d}": 100.0 for i in range(12)})
        dp = mgr.process_day(
            today=date(2026, 6, 16), plan=plan,
            prices_today=prices, commit=True,
        )
        # Repo should still only have the 12 original held positions
        assert len(repo.list_open()) == 12
        assert "NEW1" not in {h.ticker for h in repo.list_open()}

    def test_exit_frees_capacity_in_same_run(self) -> None:
        """When today closes a position via STOP_LOSS, freed slot opens for a new buy."""
        # 12 held; one will trigger STOP_LOSS (entry 100, today 80 = -20%)
        held = []
        for i in range(11):
            held.append(Holding(
                holding_id=i, ticker=f"H{i:03d}",
                entry_date=date(2026, 6, 16),
                entry_price=100.0, suggested_pct=8.0, tier="B",
                stop_loss=92.0, take_profit=115.0, industry="", concept_keys=(),
                entry_reason="", status="OPEN",
            ))
        # 12th will exit
        held.append(Holding(
            holding_id=99, ticker="EXIT",
            entry_date=date(2026, 6, 16),
            entry_price=100.0, suggested_pct=8.0, tier="B",
            stop_loss=92.0, take_profit=115.0, industry="", concept_keys=(),
            entry_reason="", status="OPEN",
        ))
        repo = _FakeRepo(held)
        mgr = HoldingsManager(repository=repo)  # type: ignore[arg-type]
        plan = AllocationPlan(
            tiers={
                "S": [], "B": [], "C": [],
                "A": [TierRecommendation(ticker="NEW1", tier="A",
                                          suggested_pct=15.0, reasoning="x",
                                          rotation_score=70.0)],
            },
            warnings=(), summary="", provider="t", snapshot_date="x",
        )
        prices = {"EXIT": 80.0, "NEW1": 100.0}  # EXIT triggers stop loss
        prices.update({f"H{i:03d}": 105.0 for i in range(11)})
        dp = mgr.process_day(
            today=date(2026, 6, 17), plan=plan,
            prices_today=prices, commit=True,
        )
        # EXIT closed, so capacity = 11 remaining → 1 slot free → NEW1 entered
        assert len(dp.pending_exits) == 1
        assert dp.pending_exits[0].holding.ticker == "EXIT"
        assert len(dp.new_buys) == 1
        assert dp.new_buys[0].ticker == "NEW1"
        assert len(dp.watchlist_picks) == 0
