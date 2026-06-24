"""Rule-based parameter tuning suggestions based on holdings review.

Phase 4.50.5
============
Reads a `HoldingsReview` snapshot and emits actionable suggestions like
"raise MIN_CONFIDENCE from 85 → 90 because TIER_DROP rate is 50%".

Critical: this module NEVER auto-applies changes. It only produces text
suggestions for the operator to consider. Parameter changes require an
explicit commit so the audit trail stays clean.

Suggestion sources (rule-based):
  - TIER_DROP rate > 40%   → entries too aggressive, raise confidence floor
  - STOP_LOSS rate > 50%   → stop loss too tight or pick quality issue
  - B-tier underperforms A → shrink B allocation or skip B entirely
  - Same-day full portfolio → consider raising MAX_OPEN_HOLDINGS
  - Frequent take-profit early → consider widening take-profit target
  - Holdings persistently flat → tighten time-stop threshold
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .holdings_review import HoldingsReview, TierStats


@dataclass(frozen=True)
class OptimizationSuggestion:
    """A single rule-based tuning recommendation."""

    rule: str               # short rule id, e.g. "high_tier_drop_rate"
    severity: str           # "high" | "medium" | "low"
    parameter: str          # which knob to tune
    current_value: str
    suggested_value: str
    rationale: str          # why this matters (Mandarin)


@dataclass(frozen=True)
class OptimizationReport:
    """All suggestions produced for a given review."""

    review_date: str
    suggestions: tuple[OptimizationSuggestion, ...]

    @property
    def n_high(self) -> int:
        return sum(1 for s in self.suggestions if s.severity == "high")

    @property
    def n_total(self) -> int:
        return len(self.suggestions)


# ── Constants matching current production defaults ─────────────────────────


CURRENT_MIN_CONFIDENCE = 85
CURRENT_MAX_OPEN_HOLDINGS = 12
CURRENT_STOP_LOSS_PCT = 7
CURRENT_TAKE_PROFIT_PCT = 15
CURRENT_TIER_B_TWD = 120_000


# ── Threshold rules (each can be tested independently) ─────────────────────


def _rule_high_tier_drop_rate(review: HoldingsReview) -> OptimizationSuggestion | None:
    """If > 40% of closes are TIER_DROP, entries are happening on weak signals."""
    if review.triggers.total < 5:
        return None       # not enough data
    rate = review.triggers.tier_drop_rate
    if rate <= 0.4:
        return None
    suggested = CURRENT_MIN_CONFIDENCE + 5 if rate < 0.6 else CURRENT_MIN_CONFIDENCE + 10
    return OptimizationSuggestion(
        rule="high_tier_drop_rate",
        severity="high" if rate > 0.6 else "medium",
        parameter="RefinedPickFilter.MIN_CONFIDENCE",
        current_value=str(CURRENT_MIN_CONFIDENCE),
        suggested_value=str(suggested),
        rationale=(
            f"TIER_DROP 占 {rate*100:.0f}% 平倉原因 ({review.triggers.tier_drop} / "
            f"{review.triggers.total})，表示進場後論點崩盤次數過高。提高信心門檻可篩掉"
            f"進場時機未成熟的標的。"
        ),
    )


def _rule_high_stop_loss_rate(review: HoldingsReview) -> OptimizationSuggestion | None:
    """If > 50% of closes are STOP_LOSS, either pick quality or stop is too tight."""
    if review.triggers.total < 5:
        return None
    rate = review.triggers.stop_loss / review.triggers.total
    if rate <= 0.5:
        return None
    return OptimizationSuggestion(
        rule="high_stop_loss_rate",
        severity="high",
        parameter="HoldingsManager.STOP_LOSS_PCT",
        current_value=f"{CURRENT_STOP_LOSS_PCT}%",
        suggested_value=f"{CURRENT_STOP_LOSS_PCT + 2}% (or reassess pick quality)",
        rationale=(
            f"STOP_LOSS 占 {rate*100:.0f}% 平倉原因 ({review.triggers.stop_loss} / "
            f"{review.triggers.total})。可能停損太緊（隨小幅震盪觸發），或進場時"
            f"未真正蓄積完成。先檢視 7 天內被砍標的的進場日 BB% 與量比。"
        ),
    )


def _rule_b_tier_underperforms(review: HoldingsReview) -> OptimizationSuggestion | None:
    """If B tier avg return is materially worse than A, suggest shrinking B."""
    a = next((t for t in review.tier_stats if t.tier == "A"), None)
    b = next((t for t in review.tier_stats if t.tier == "B"), None)
    if a is None or b is None:
        return None
    if a.avg_realised_pct is None or b.avg_realised_pct is None:
        return None
    if a.n_closed < 3 or b.n_closed < 3:
        return None       # need enough samples
    delta = a.avg_realised_pct - b.avg_realised_pct
    if delta < 3.0:
        return None       # tiers performing similarly
    suggested = max(60_000, CURRENT_TIER_B_TWD - 40_000)
    return OptimizationSuggestion(
        rule="b_tier_underperforms",
        severity="medium",
        parameter="BudgetAllocator.TIER_TWD['B']",
        current_value=f"NT${CURRENT_TIER_B_TWD:,}",
        suggested_value=f"NT${suggested:,}",
        rationale=(
            f"A 級平均 {a.avg_realised_pct:+.2f}% 顯著優於 B 級 {b.avg_realised_pct:+.2f}% "
            f"(差距 {delta:.1f}%)。建議縮小 B 級配置以將資金集中在 A 級。"
        ),
    )


def _rule_portfolio_always_full(review: HoldingsReview) -> OptimizationSuggestion | None:
    """If open holdings near cap and there are many watchlist picks (signals waiting)."""
    if review.n_open < CURRENT_MAX_OPEN_HOLDINGS - 1:
        return None
    if review.closed_win_rate is None or review.closed_win_rate < 60:
        return None       # don't loosen unless current strategy is working
    return OptimizationSuggestion(
        rule="portfolio_full_with_good_winrate",
        severity="low",
        parameter="HoldingsManager.MAX_OPEN_HOLDINGS",
        current_value=str(CURRENT_MAX_OPEN_HOLDINGS),
        suggested_value=str(CURRENT_MAX_OPEN_HOLDINGS + 3),
        rationale=(
            f"持倉滿載 ({review.n_open}/{CURRENT_MAX_OPEN_HOLDINGS}) 且勝率 "
            f"{review.closed_win_rate}% > 60%。可考慮放寬持倉上限讓系統發揮，"
            f"但需同步監控集中度警告。"
        ),
    )


def _rule_take_profit_too_tight(review: HoldingsReview) -> OptimizationSuggestion | None:
    """If TAKE_PROFIT triggers ≥ 40% of closes, might be capping winners early."""
    if review.triggers.total < 5:
        return None
    rate = review.triggers.take_profit / review.triggers.total
    if rate <= 0.4:
        return None
    return OptimizationSuggestion(
        rule="take_profit_capping_winners",
        severity="low",
        parameter="HoldingsManager.TAKE_PROFIT_PCT",
        current_value=f"{CURRENT_TAKE_PROFIT_PCT}%",
        suggested_value=f"{CURRENT_TAKE_PROFIT_PCT + 5}%",
        rationale=(
            f"TAKE_PROFIT 占 {rate*100:.0f}% 平倉原因 ({review.triggers.take_profit} / "
            f"{review.triggers.total})。停利線過緊可能在大幅突破前就出場，可考慮放寬。"
        ),
    )


# ── Public entry point ─────────────────────────────────────────────────────


_RULES = (
    _rule_high_tier_drop_rate,
    _rule_high_stop_loss_rate,
    _rule_b_tier_underperforms,
    _rule_portfolio_always_full,
    _rule_take_profit_too_tight,
)


def build_optimization_report(review: HoldingsReview) -> OptimizationReport:
    """Run all rules against a review and return the suggestions found.

    Suggestions are sorted by severity desc (high → medium → low).
    """
    suggestions: list[OptimizationSuggestion] = []
    for rule in _RULES:
        s = rule(review)
        if s is not None:
            suggestions.append(s)

    severity_order = {"high": 0, "medium": 1, "low": 2}
    suggestions.sort(key=lambda s: severity_order.get(s.severity, 99))

    return OptimizationReport(
        review_date=str(review.today),
        suggestions=tuple(suggestions),
    )
