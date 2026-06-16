"""HoldingsManager — daily simulation of "what to hold, sell, or buy more of".

This sits between AllocationAdvisor (which produces today's *fresh* tier
recommendations) and HoldingsRepository (which persists open positions).

Each day the pipeline calls:
    HoldingsManager.process_day(today, signals_by_ticker, fresh_plan)

which:
  1. evaluates exit rules on every OPEN holding (stop loss, take profit,
     time stop, tier drop) and CLOSES the ones that triggered
  2. opens new positions for fresh Tier S/A/B picks that we don't already
     hold
  3. classifies today's tier output so the HTML/terminal can render three
     buckets: HOLDING (with P&L) / NEW BUYS / PENDING EXITS

Returned `DailyPortfolio` is purely a value object — the repository handles
the actual DB writes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Mapping, Optional, Sequence

from ..domain.capital_allocator import AllocationPlan, TIER_ORDER, TierRecommendation
from ..infrastructure.holdings_repository import (
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    ExitDecision,
    Holding,
    HoldingsRepository,
    evaluate_exit,
)

logger = logging.getLogger(__name__)


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HoldingSnapshot:
    """An open holding plus today's price / P&L."""

    holding: Holding
    current_price: float
    unrealised_pct: float
    days_held: int
    # Hold/exit decision evaluated by rules
    exit_decision: ExitDecision
    # Fresh tier today (None if not in today's tier output)
    today_tier: Optional[str] = None
    today_confidence: Optional[float] = None
    name: str = ""


@dataclass(frozen=True)
class NewBuy:
    """A fresh Tier S/A/B recommendation that is NOT already held."""

    ticker: str
    name: str
    tier: str
    suggested_pct: float
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit: float
    industry: str
    concept_keys: tuple[str, ...]
    reasoning: str


@dataclass(frozen=True)
class DailyPortfolio:
    today: date
    holdings: tuple[HoldingSnapshot, ...]        # all OPEN at start of day
    pending_exits: tuple[HoldingSnapshot, ...]   # subset that should close today
    new_buys: tuple[NewBuy, ...]                 # fresh tier S/A/B not held
    skipped_recs: tuple[NewBuy, ...]             # tier C-tier-with-pct fresh recs

    @property
    def holding_count(self) -> int:
        return len(self.holdings)

    @property
    def total_invested_pct(self) -> float:
        return round(sum(h.holding.suggested_pct for h in self.holdings), 1)

    @property
    def cash_pct(self) -> float:
        return round(max(0.0, 100.0 - self.total_invested_pct), 1)

    @property
    def portfolio_unrealised_pct(self) -> float:
        """Weighted average P&L across all OPEN holdings."""
        if not self.holdings:
            return 0.0
        weighted = sum(h.unrealised_pct * h.holding.suggested_pct for h in self.holdings)
        total_pct = sum(h.holding.suggested_pct for h in self.holdings)
        return round(weighted / total_pct, 2) if total_pct > 0 else 0.0


# ── Manager ──────────────────────────────────────────────────────────────────


class HoldingsManager:
    """Stateful, day-over-day portfolio manager.

    Open/close decisions are rule-based here; LLM intervention is optional
    (`AllocationAdvisor` handles LLM allocation tier).
    """

    def __init__(
        self,
        repository: Optional[HoldingsRepository] = None,
        stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
        take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
    ) -> None:
        self._repo = repository or HoldingsRepository()
        self._stop_loss_pct = stop_loss_pct
        self._take_profit_pct = take_profit_pct

    # --- public API ----------------------------------------------------

    def process_day(
        self,
        *,
        today: date,
        plan: AllocationPlan,
        prices_today: Mapping[str, float],
        confidences_today: Optional[Mapping[str, float]] = None,
        name_map: Optional[Mapping[str, str]] = None,
        industry_map: Optional[Mapping[str, str]] = None,
        concepts_by_ticker: Optional[Mapping[str, Sequence[str]]] = None,
        commit: bool = True,
    ) -> DailyPortfolio:
        """Compute the daily portfolio:

          - For each open holding: evaluate rules, optionally close.
          - For each fresh S/A/B tier rec: if NOT already held, mark as new buy.
        """
        name_map = name_map or {}
        confidences_today = confidences_today or {}
        industry_map = industry_map or {}
        concepts_by_ticker = concepts_by_ticker or {}

        open_holdings = self._repo.list_open()
        held_tickers = {h.ticker for h in open_holdings}

        # Build a quick lookup of today's tier per ticker
        tier_by_ticker: dict[str, tuple[str, float]] = {}
        for tier in TIER_ORDER:
            for rec in plan.tiers.get(tier, []):
                tier_by_ticker[rec.ticker] = (tier, rec.suggested_pct)

        # ── Process open holdings ────────────────────────────────────
        snapshots: list[HoldingSnapshot] = []
        pending_exits: list[HoldingSnapshot] = []
        for h in open_holdings:
            cur_price = float(prices_today.get(h.ticker, 0.0) or 0.0)
            if cur_price <= 0:
                # Skip evaluation if no price (likely weekend / data gap)
                snapshots.append(HoldingSnapshot(
                    holding=h,
                    current_price=0.0,
                    unrealised_pct=0.0,
                    days_held=(today - h.entry_date).days,
                    exit_decision=ExitDecision(False),
                    today_tier=tier_by_ticker.get(h.ticker, (None, None))[0],
                    today_confidence=confidences_today.get(h.ticker),
                    name=name_map.get(h.ticker, h.ticker),
                ))
                continue

            unreal = round((cur_price - h.entry_price) / h.entry_price * 100, 2)
            days_held = (today - h.entry_date).days
            tce = confidences_today.get(h.ticker)
            decision = evaluate_exit(h, cur_price, today, tce)

            snap = HoldingSnapshot(
                holding=h,
                current_price=cur_price,
                unrealised_pct=unreal,
                days_held=days_held,
                exit_decision=decision,
                today_tier=tier_by_ticker.get(h.ticker, (None, None))[0],
                today_confidence=tce,
                name=name_map.get(h.ticker, h.ticker),
            )
            snapshots.append(snap)
            if decision.should_close:
                pending_exits.append(snap)
                if commit and self._repo.available:
                    self._repo.close_position(
                        h.holding_id,
                        close_date=today,
                        close_price=cur_price,
                        close_reason=decision.close_reason or "MANUAL",
                        entry_price=h.entry_price,
                        notes=f" | {decision.rationale}",
                    )

        # ── Identify new buys (fresh tier S/A/B not held) ───────────
        new_buys: list[NewBuy] = []
        skipped: list[NewBuy] = []
        for tier in ("S", "A", "B"):
            for rec in plan.tiers.get(tier, []):
                if rec.ticker in held_tickers:
                    continue
                price = float(prices_today.get(rec.ticker, 0.0) or 0.0)
                if price <= 0:
                    continue
                conf = float(confidences_today.get(rec.ticker, 0.0) or 0.0)
                concepts = tuple(concepts_by_ticker.get(rec.ticker, ()))
                buy = NewBuy(
                    ticker=rec.ticker,
                    name=name_map.get(rec.ticker, rec.ticker),
                    tier=tier,
                    suggested_pct=rec.suggested_pct,
                    confidence=conf,
                    entry_price=price,
                    stop_loss=round(price * (1 - self._stop_loss_pct), 2),
                    take_profit=round(price * (1 + self._take_profit_pct), 2),
                    industry=industry_map.get(rec.ticker, ""),
                    concept_keys=concepts,
                    reasoning=rec.reasoning,
                )
                new_buys.append(buy)
                if commit and self._repo.available:
                    self._repo.open_position(
                        ticker=rec.ticker,
                        entry_date=today,
                        entry_price=price,
                        suggested_pct=rec.suggested_pct,
                        tier=tier,
                        industry=buy.industry,
                        concept_keys=concepts,
                        entry_reason=rec.reasoning,
                        stop_loss_pct=self._stop_loss_pct,
                        take_profit_pct=self._take_profit_pct,
                    )

        # Also surface fresh C-tier with pct > 0 as low-conviction candidates
        for rec in plan.tiers.get("C", []):
            if rec.ticker in held_tickers or rec.suggested_pct <= 0:
                continue
            price = float(prices_today.get(rec.ticker, 0.0) or 0.0)
            if price <= 0:
                continue
            conf = float(confidences_today.get(rec.ticker, 0.0) or 0.0)
            concepts = tuple(concepts_by_ticker.get(rec.ticker, ()))
            skipped.append(NewBuy(
                ticker=rec.ticker,
                name=name_map.get(rec.ticker, rec.ticker),
                tier="C",
                suggested_pct=rec.suggested_pct,
                confidence=conf,
                entry_price=price,
                stop_loss=round(price * (1 - self._stop_loss_pct), 2),
                take_profit=round(price * (1 + self._take_profit_pct), 2),
                industry=industry_map.get(rec.ticker, ""),
                concept_keys=concepts,
                reasoning=rec.reasoning,
            ))

        return DailyPortfolio(
            today=today,
            holdings=tuple(snapshots),
            pending_exits=tuple(pending_exits),
            new_buys=tuple(new_buys),
            skipped_recs=tuple(skipped),
        )
