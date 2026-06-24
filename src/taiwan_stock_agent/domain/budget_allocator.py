"""BudgetAllocator — NT$3,000,000 actual capital allocation.

Background
----------
Previously the system gave abstract "% allocation" advice (e.g. "invest 65%")
which the user could not act on directly — they had to translate it into
shares + NT$ themselves, including lot-size math for TWSE (1 lot = 1000
shares) and odd-lot calculation for high-price stocks.

This module accepts the refined picks (output of RefinedPickFilter) plus
the existing open holdings (from HoldingsRepository) and produces a
`PortfolioAllocation` containing concrete `PositionPlan` entries with
share counts, lot/odd-lot breakdown, NT$ amounts, stop/take prices, and
held vs new-buy distinction.

Key rules:
  * Total budget = NT$3,000,000 (configurable)
  * Max simultaneous positions = 12 (concentration cap)
  * Cash reserve >= 15% (NT$450K) as crash buffer
  * Per-position size by tier: S=NT$250K / A=NT$180K / B=NT$120K
  * Held positions emit "is_held=True" PositionPlan (informational, no buy)
  * Once capacity or budget is exhausted, remaining picks go to caller as
    `skipped_picks` (i.e. the watchlist).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Mapping, Optional, Sequence

from .refined_picks import RefinedPick


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PositionPlan:
    """One actionable position: ticker + share count + NT$ amount + tier."""

    ticker: str
    name: str
    sector: str
    tier: str                    # S / A / B
    target_twd: int              # NT$ allocated (planned, before lot rounding)
    actual_twd: int              # NT$ actually consumed (shares × entry_price)
    shares: int                  # total shares (lots*1000 + odd_shares)
    lots: int                    # integer lots (1 lot = 1000 shares)
    odd_shares: int              # remainder shares (零股)
    entry_price: float
    stop_loss: float             # entry × 0.93 (-7%)
    take_profit: float           # entry × 1.15 (+15%)
    is_held: bool                # True = already owned, don't buy more
    rationale: str = ""

    @property
    def has_odd_lots(self) -> bool:
        return self.odd_shares > 0


@dataclass(frozen=True)
class PortfolioAllocation:
    """Complete allocation snapshot for a given day."""

    today: date
    positions: tuple[PositionPlan, ...]    # all positions (held + new) sorted by tier+score
    skipped_picks: tuple[RefinedPick, ...] # refined picks that didn't fit (watchlist)
    budget_twd: int                        # total budget input
    held_value_twd: int                    # NT$ tied up in existing holdings (estimate)
    new_buys_twd: int                      # NT$ for fresh entries today
    cash_reserve_twd: int                  # NT$ remaining as cash
    max_positions: int                     # cap used
    n_positions: int                       # actual positions emitted

    @property
    def held_positions(self) -> tuple[PositionPlan, ...]:
        return tuple(p for p in self.positions if p.is_held)

    @property
    def new_buy_positions(self) -> tuple[PositionPlan, ...]:
        return tuple(p for p in self.positions if not p.is_held)

    @property
    def total_invested_twd(self) -> int:
        return self.held_value_twd + self.new_buys_twd

    @property
    def cash_pct(self) -> float:
        if self.budget_twd <= 0:
            return 0.0
        return round(self.cash_reserve_twd / self.budget_twd * 100, 1)


# ── Allocator ───────────────────────────────────────────────────────────────


class BudgetAllocator:
    """Translate refined picks into concrete share + NT$ position plans."""

    TOTAL_BUDGET = 3_000_000
    MAX_POSITIONS = 12
    MIN_TWD_PER_POSITION = 100_000      # skip if can't afford NT$100K
    CASH_RESERVE_PCT = 0.15             # reserve 15% (NT$450K) as buffer
    TIER_TWD = {"S": 250_000, "A": 180_000, "B": 120_000}
    LOT_SIZE = 1000

    # Tier banding by composite_score: top 3 → S, next 5 → A, rest → B
    TIER_BANDS = (("S", 3), ("A", 5))    # remaining → "B"

    STOP_LOSS_PCT = 0.07
    TAKE_PROFIT_PCT = 0.15

    def allocate(
        self,
        refined_picks: Sequence[RefinedPick],
        held_positions: Sequence,
        *,
        today: date | None = None,
        budget: int = TOTAL_BUDGET,
        max_positions: int | None = None,
    ) -> PortfolioAllocation:
        """Produce a PortfolioAllocation.

        Parameters
        ----------
        refined_picks : output of RefinedPickFilter.refine(), already sorted
                        by composite_score desc.
        held_positions : Sequence of Holding objects from HoldingsRepository.
                         Used to compute held_value_twd and dedupe vs new buys.
        today : analysis date (defaults to date.today()).
        budget : override default NT$3M.
        max_positions : override default MAX_POSITIONS.
        """
        today = today or date.today()
        max_positions = max_positions or self.MAX_POSITIONS

        held_by_ticker = {h.ticker: h for h in held_positions}
        n_held = len(held_by_ticker)

        # Phase 4.50.6 — held value is bounded by TIER_TWD per position so
        # 12 持倉 cannot exceed NT$3M even if historical suggested_pct sums
        # >100% (legacy data wrote %-of-portfolio numbers that weren't
        # budget-aware). Cap each held to TIER_TWD[tier], whichever is
        # smaller compared to its historical %×budget.
        def _held_twd(h) -> int:
            tier_cap = self.TIER_TWD.get((h.tier or "B").upper(), self.TIER_TWD["B"])
            legacy = int(float(h.suggested_pct) / 100.0 * budget)
            return min(legacy, tier_cap) if legacy > 0 else tier_cap

        per_held_twd = {h.ticker: _held_twd(h) for h in held_positions}
        held_value_twd = sum(per_held_twd.values())

        # Available budget for NEW buys = budget × (1 - cash_reserve) - held_value
        cash_floor = int(budget * self.CASH_RESERVE_PCT)
        available_for_new = max(0, budget - cash_floor - held_value_twd)
        capacity = max(0, max_positions - n_held)

        positions: list[PositionPlan] = []
        skipped: list[RefinedPick] = []

        # 1) Emit held positions first (informational, no buy).
        # target = per_held_twd[ticker] (already TIER_TWD-capped above)
        held_picks_emitted: set[str] = set()
        for h in held_positions:
            tier_letter = (h.tier or "B").upper()
            entry_price = float(h.entry_price)
            target_twd = per_held_twd.get(h.ticker, 0)
            shares = int(target_twd / entry_price) if entry_price > 0 else 0
            lots, odd = divmod(shares, self.LOT_SIZE)
            actual = int(shares * entry_price)
            positions.append(PositionPlan(
                ticker=h.ticker,
                name=getattr(h, "name", h.ticker),
                sector=getattr(h, "industry", ""),
                tier=tier_letter,
                target_twd=target_twd,
                actual_twd=actual,
                shares=shares,
                lots=lots,
                odd_shares=odd,
                entry_price=entry_price,
                stop_loss=float(h.stop_loss),
                take_profit=float(h.take_profit),
                is_held=True,
                rationale=f"持倉中 (進場 {h.entry_date})",
            ))
            held_picks_emitted.add(h.ticker)

        # 2) Tier-band the new picks (those not already held)
        new_picks = [p for p in refined_picks if p.ticker not in held_picks_emitted]
        tiered = self._assign_tiers(new_picks)

        # 3) Emit new buys until capacity / budget exhausted
        new_buys_twd = 0
        for tier, pick in tiered:
            if capacity <= 0:
                skipped.append(pick)
                continue
            if pick.entry_price <= 0:
                skipped.append(pick)
                continue
            target = self.TIER_TWD.get(tier, self.TIER_TWD["B"])
            target = min(target, available_for_new)
            if target < self.MIN_TWD_PER_POSITION:
                skipped.append(pick)
                continue

            shares, lots, odd = self.calc_shares(target, pick.entry_price)
            if shares <= 0:
                skipped.append(pick)
                continue
            actual = int(shares * pick.entry_price)
            stop = round(pick.entry_price * (1 - self.STOP_LOSS_PCT), 2)
            tp = round(pick.entry_price * (1 + self.TAKE_PROFIT_PCT), 2)

            positions.append(PositionPlan(
                ticker=pick.ticker,
                name=pick.name,
                sector=pick.sector,
                tier=tier,
                target_twd=target,
                actual_twd=actual,
                shares=shares,
                lots=lots,
                odd_shares=odd,
                entry_price=pick.entry_price,
                stop_loss=stop,
                take_profit=tp,
                is_held=False,
                rationale=self._build_rationale(pick, tier),
            ))
            new_buys_twd += actual
            available_for_new -= actual
            capacity -= 1

        cash_reserve = max(0, budget - held_value_twd - new_buys_twd)

        return PortfolioAllocation(
            today=today,
            positions=tuple(positions),
            skipped_picks=tuple(skipped),
            budget_twd=budget,
            held_value_twd=held_value_twd,
            new_buys_twd=new_buys_twd,
            cash_reserve_twd=cash_reserve,
            max_positions=max_positions,
            n_positions=len(positions),
        )

    # --- helpers ----------------------------------------------------------

    @classmethod
    def calc_shares(cls, target_twd: int, entry_price: float) -> tuple[int, int, int]:
        """Return (total_shares, lots, odd_shares). Handles high-price odd lots.

        Strategy: floor(target_twd / entry_price), then split into 1000-share
        lots + remainder odd shares. For very high-price stocks (e.g. 信驊
        19000+), we may end up with 0 lots and only odd shares — that's the
        right answer since 1 lot would be NT$19M.
        """
        if entry_price <= 0 or target_twd <= 0:
            return (0, 0, 0)
        shares = int(target_twd / entry_price)
        lots, odd = divmod(shares, cls.LOT_SIZE)
        return (shares, lots, odd)

    @classmethod
    def _assign_tiers(
        cls, picks: Sequence[RefinedPick]
    ) -> list[tuple[str, RefinedPick]]:
        """Top 3 → S, next 5 → A, rest → B."""
        out: list[tuple[str, RefinedPick]] = []
        for i, pick in enumerate(picks):
            if i < cls.TIER_BANDS[0][1]:                  # 0..2 → S
                out.append(("S", pick))
            elif i < cls.TIER_BANDS[0][1] + cls.TIER_BANDS[1][1]:  # 3..7 → A
                out.append(("A", pick))
            else:                                          # 8+ → B
                out.append(("B", pick))
        return out

    @classmethod
    def _lots_for_pct(cls, pct: float, budget: int, entry_price: float) -> int:
        """Reverse-engineer shares from historical "%-of-portfolio" allocation.

        Used only to display held positions in NT$ terms when the original
        record stored a percentage. Not perfect — actual shares depend on
        when the user bought — but accurate enough for the dashboard.
        """
        target = int(pct / 100.0 * budget)
        if entry_price <= 0:
            return 0
        return int(target / entry_price)

    @staticmethod
    def _build_rationale(pick: RefinedPick, tier: str) -> str:
        parts = [f"{tier} 級", f"信心 {pick.confidence:.0f}"]
        if pick.industry_state in ("HOT", "EMERGING"):
            parts.append(f"產業 {pick.industry_state}")
        if pick.has_bb_compression:
            parts.append(f"BB {pick.bb_pct:.0f}p 壓縮")
        if pick.concept_tailwind:
            parts.append("熱門題材")
        return " ｜ ".join(parts)
