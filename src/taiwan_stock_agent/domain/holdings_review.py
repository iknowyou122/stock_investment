"""Daily holdings review — quantify what worked, surface risks, suggest tuning.

Phase 4.50.5
============
After Phase 4.49 (持倉延續) + Phase 4.50 (NT$3M 預算化), we have a real
trail of decisions in `simulated_holdings`: entries, exits, close reasons,
realised P&L. But there's no automation that reads it back and asks
"did the strategy work yesterday?". This module fills that gap.

Inputs (pure functions, no IO):
  - open_holdings: positions still OPEN (read via HoldingsRepository)
  - closed_in_period: positions CLOSED in the lookback window
  - prices_today: ticker → current price (for unrealised P&L)
  - taiex_return_pct: market benchmark for alpha calculation
  - budget_twd: NT$3M (or user override) for converting % to NT$

Outputs:
  - `HoldingsReview` dataclass with all stats + risk warnings + tier breakdown
  - LLM narrative (via `generate_review_narrative` helper) when an LLMProvider
    is supplied; otherwise a deterministic rule-based summary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TriggerCount:
    """Distribution of close reasons in the lookback window."""

    stop_loss: int = 0
    take_profit: int = 0
    time_stop: int = 0
    tier_drop: int = 0
    manual: int = 0

    @property
    def total(self) -> int:
        return self.stop_loss + self.take_profit + self.time_stop + self.tier_drop + self.manual

    @property
    def tier_drop_rate(self) -> float:
        """Fraction of closes that were TIER_DROP. > 0.4 suggests
        entries are happening too aggressively before thesis confirmation."""
        if self.total == 0:
            return 0.0
        return self.tier_drop / self.total


@dataclass(frozen=True)
class TierStats:
    """Win-rate + average return breakdown for a single tier (S/A/B)."""

    tier: str
    n_total: int                   # OPEN + CLOSED in window
    n_open: int
    n_closed_win: int              # realised_pct > 0.5
    n_closed_loss: int             # realised_pct < -0.5
    n_closed_flat: int             # -0.5 .. 0.5
    avg_realised_pct: Optional[float]   # avg over closed positions
    avg_unrealised_pct: Optional[float] # avg over open positions

    @property
    def n_closed(self) -> int:
        return self.n_closed_win + self.n_closed_loss + self.n_closed_flat

    @property
    def closed_win_rate(self) -> Optional[float]:
        wl = self.n_closed_win + self.n_closed_loss
        if wl == 0:
            return None
        return round(self.n_closed_win / wl * 100, 1)


@dataclass(frozen=True)
class RiskWarning:
    """A single risk callout for the operator's attention today."""

    ticker: str
    name: str
    severity: str       # "high" | "medium" | "low"
    category: str       # "near_stop" | "near_time_stop" | "near_take" | "concentration"
    message: str
    current_price: float
    distance_pct: float = 0.0   # how close to trigger (-1.5% means 1.5% from stop)


@dataclass(frozen=True)
class HoldingsReview:
    """Full review snapshot for the day."""

    today: date
    lookback_days: int
    budget_twd: int

    n_open: int
    n_closed_in_period: int
    portfolio_unrealised_pct: float    # weighted by suggested_pct
    portfolio_unrealised_twd: int       # NT$ unrealised across open positions
    portfolio_realised_twd: int         # NT$ realised P&L from closed-in-period
    closed_win_rate: Optional[float]    # over closed-in-period
    avg_realised_pct: Optional[float]

    triggers: TriggerCount
    tier_stats: tuple[TierStats, ...]
    risk_warnings: tuple[RiskWarning, ...]

    taiex_return_pct: Optional[float]
    alpha_pct: Optional[float]

    # bookkeeping
    open_pnl_by_ticker: Mapping[str, float] = field(default_factory=dict)
    closed_pnl_by_ticker: Mapping[str, float] = field(default_factory=dict)


# ── Builder ─────────────────────────────────────────────────────────────────


# Thresholds for risk warnings
_NEAR_STOP_PCT = 0.025       # within 2.5% of stop loss → high severity
_NEAR_TAKE_PCT = 0.025       # within 2.5% of take profit → low severity (good news)
_TIME_STOP_DAYS_WARNING = 8  # 2 days before TIME_STOP triggers (10 days)
_TIME_STOP_MIN_PROFIT = 0.05 # if still below entry × 1.05 at day 8 → warn


def build_review(
    *,
    today: date,
    open_holdings: Sequence,             # list of Holding objects
    closed_in_period: Sequence,          # list of Holding objects (status=CLOSED)
    prices_today: Mapping[str, float],
    name_map: Optional[Mapping[str, str]] = None,
    taiex_return_pct: Optional[float] = None,
    lookback_days: int = 7,
    budget_twd: int = 3_000_000,
) -> HoldingsReview:
    """Pure function: build the day's HoldingsReview from raw inputs.

    Both `open_holdings` and `closed_in_period` should be Holding objects
    as returned by HoldingsRepository.list_open() / list_recent_closed().
    """
    name_map = name_map or {}

    # ── Open positions: compute unrealised P&L ──────────────────────────
    open_pnl: dict[str, float] = {}
    weighted_pnl_sum = 0.0
    weighted_pnl_weight = 0.0
    unrealised_twd_total = 0
    for h in open_holdings:
        price = float(prices_today.get(h.ticker, 0.0) or 0.0)
        entry = float(h.entry_price)
        if entry <= 0 or price <= 0:
            continue
        pct = (price - entry) / entry * 100
        open_pnl[h.ticker] = round(pct, 2)
        weight = float(h.suggested_pct)
        weighted_pnl_sum += pct * weight
        weighted_pnl_weight += weight
        # NT$ unrealised = (price - entry) × shares; we approximate shares
        # via budget_twd × weight / entry_price (same logic as BudgetAllocator)
        shares = int(budget_twd * weight / 100.0 / entry)
        unrealised_twd_total += int((price - entry) * shares)

    portfolio_unrealised_pct = (
        round(weighted_pnl_sum / weighted_pnl_weight, 2)
        if weighted_pnl_weight > 0 else 0.0
    )

    # ── Closed positions: trigger distribution + realised P&L ────────────
    triggers = _count_triggers(closed_in_period)
    closed_pnl: dict[str, float] = {}
    realised_twd_total = 0
    win_count = loss_count = 0
    sum_realised = 0.0
    sum_realised_n = 0
    for h in closed_in_period:
        rp = h.realised_pct
        if rp is None:
            continue
        rp_f = float(rp)
        closed_pnl[h.ticker] = round(rp_f, 2)
        sum_realised += rp_f
        sum_realised_n += 1
        if rp_f > 0.5:
            win_count += 1
        elif rp_f < -0.5:
            loss_count += 1
        # NT$ realised
        weight = float(h.suggested_pct)
        shares = int(budget_twd * weight / 100.0 / float(h.entry_price)) if h.entry_price else 0
        if h.close_price and h.entry_price:
            realised_twd_total += int(
                (float(h.close_price) - float(h.entry_price)) * shares
            )

    avg_realised_pct = round(sum_realised / sum_realised_n, 2) if sum_realised_n > 0 else None
    closed_win_rate = (
        round(win_count / (win_count + loss_count) * 100, 1)
        if (win_count + loss_count) > 0 else None
    )

    # ── Tier breakdown ──────────────────────────────────────────────────
    tier_stats = _compute_tier_stats(open_holdings, closed_in_period, open_pnl, closed_pnl)

    # ── Risk warnings ───────────────────────────────────────────────────
    warnings = _build_risk_warnings(
        open_holdings, prices_today, today, name_map,
    )

    # ── Alpha vs market ─────────────────────────────────────────────────
    alpha = None
    if taiex_return_pct is not None and weighted_pnl_weight > 0:
        # Compare portfolio's unrealised (this period) vs TAIEX
        alpha = round(portfolio_unrealised_pct - taiex_return_pct, 2)

    return HoldingsReview(
        today=today,
        lookback_days=lookback_days,
        budget_twd=budget_twd,
        n_open=len(open_holdings),
        n_closed_in_period=len(closed_in_period),
        portfolio_unrealised_pct=portfolio_unrealised_pct,
        portfolio_unrealised_twd=unrealised_twd_total,
        portfolio_realised_twd=realised_twd_total,
        closed_win_rate=closed_win_rate,
        avg_realised_pct=avg_realised_pct,
        triggers=triggers,
        tier_stats=tuple(tier_stats),
        risk_warnings=tuple(warnings),
        taiex_return_pct=taiex_return_pct,
        alpha_pct=alpha,
        open_pnl_by_ticker=open_pnl,
        closed_pnl_by_ticker=closed_pnl,
    )


def _count_triggers(closed: Sequence) -> TriggerCount:
    """Tally close reasons across closed positions."""
    counts = {"STOP_LOSS": 0, "TAKE_PROFIT": 0, "TIME_STOP": 0, "TIER_DROP": 0, "MANUAL": 0}
    for h in closed:
        reason = (h.close_reason or "MANUAL").upper()
        if reason in counts:
            counts[reason] += 1
        else:
            counts["MANUAL"] += 1
    return TriggerCount(
        stop_loss=counts["STOP_LOSS"],
        take_profit=counts["TAKE_PROFIT"],
        time_stop=counts["TIME_STOP"],
        tier_drop=counts["TIER_DROP"],
        manual=counts["MANUAL"],
    )


def _compute_tier_stats(
    open_holdings: Sequence,
    closed: Sequence,
    open_pnl: Mapping[str, float],
    closed_pnl: Mapping[str, float],
) -> list[TierStats]:
    """Build TierStats for each of S, A, B (skips C — observation only)."""
    buckets: dict[str, dict[str, list]] = {
        t: {"open": [], "closed": []} for t in ("S", "A", "B")
    }
    for h in open_holdings:
        tier = (h.tier or "B").upper()
        if tier in buckets:
            buckets[tier]["open"].append(h)
    for h in closed:
        tier = (h.tier or "B").upper()
        if tier in buckets:
            buckets[tier]["closed"].append(h)

    out: list[TierStats] = []
    for tier in ("S", "A", "B"):
        bucket = buckets[tier]
        n_open = len(bucket["open"])
        win = sum(1 for h in bucket["closed"]
                  if h.realised_pct is not None and float(h.realised_pct) > 0.5)
        loss = sum(1 for h in bucket["closed"]
                   if h.realised_pct is not None and float(h.realised_pct) < -0.5)
        flat = len(bucket["closed"]) - win - loss

        # avg pcts
        closed_pcts = [float(h.realised_pct) for h in bucket["closed"]
                       if h.realised_pct is not None]
        avg_realised = round(sum(closed_pcts) / len(closed_pcts), 2) if closed_pcts else None

        open_pcts = [open_pnl[h.ticker] for h in bucket["open"] if h.ticker in open_pnl]
        avg_unrealised = round(sum(open_pcts) / len(open_pcts), 2) if open_pcts else None

        out.append(TierStats(
            tier=tier,
            n_total=n_open + len(bucket["closed"]),
            n_open=n_open,
            n_closed_win=win,
            n_closed_loss=loss,
            n_closed_flat=flat,
            avg_realised_pct=avg_realised,
            avg_unrealised_pct=avg_unrealised,
        ))
    return out


def _build_risk_warnings(
    open_holdings: Sequence,
    prices_today: Mapping[str, float],
    today: date,
    name_map: Mapping[str, str],
) -> list[RiskWarning]:
    """Identify positions that need attention this morning."""
    warnings: list[RiskWarning] = []
    for h in open_holdings:
        price = float(prices_today.get(h.ticker, 0.0) or 0.0)
        if price <= 0 or float(h.entry_price) <= 0:
            continue
        name = name_map.get(h.ticker, h.ticker)
        sl = float(h.stop_loss)
        tp = float(h.take_profit)

        # 1) Near stop loss
        if sl > 0:
            dist = (price - sl) / sl
            if dist < _NEAR_STOP_PCT and dist > 0:
                warnings.append(RiskWarning(
                    ticker=h.ticker, name=name,
                    severity="high",
                    category="near_stop",
                    message=f"距停損 {sl:.1f} 僅 {dist*100:+.1f}% (現價 {price:.1f}) — 隨時可能觸發",
                    current_price=price,
                    distance_pct=round(dist * 100, 1),
                ))
                continue

        # 2) Approaching time stop (day 8+, still below entry × 1.05)
        days_held = (today - h.entry_date).days
        if days_held >= _TIME_STOP_DAYS_WARNING:
            entry = float(h.entry_price)
            threshold = entry * (1 + _TIME_STOP_MIN_PROFIT)
            if price < threshold:
                warnings.append(RiskWarning(
                    ticker=h.ticker, name=name,
                    severity="medium",
                    category="near_time_stop",
                    message=f"持有 {days_held} 天且未漲過 +5% (現 {price:.1f} < {threshold:.1f}) — 接近時間停損",
                    current_price=price,
                    distance_pct=round((price - entry) / entry * 100, 1),
                ))
                continue

        # 3) Near take profit (informational, not warning)
        if tp > 0:
            dist_tp = (tp - price) / price
            if 0 < dist_tp < _NEAR_TAKE_PCT:
                warnings.append(RiskWarning(
                    ticker=h.ticker, name=name,
                    severity="low",
                    category="near_take",
                    message=f"距停利 {tp:.1f} 僅 {dist_tp*100:+.1f}% (現價 {price:.1f}) — 準備獲利了結",
                    current_price=price,
                    distance_pct=round(-dist_tp * 100, 1),
                ))
    return warnings


# ── LLM narrative (best-effort, falls back to rule-based) ──────────────────


def generate_review_narrative(
    review: HoldingsReview,
    *,
    llm: Optional[object] = None,
) -> str:
    """Return a 2-3 sentence Mandarin narrative explaining the day's review.

    Uses an LLMProvider (`.complete(prompt, max_tokens=...)`) if available,
    otherwise falls back to a deterministic rule-based summary.
    """
    if llm is not None and hasattr(llm, "complete"):
        try:
            prompt = _build_llm_prompt(review)
            text = llm.complete(prompt, max_tokens=400)
            if text and len(text.strip()) > 20:
                return text.strip()
        except Exception as exc:
            logger.warning("LLM narrative failed, falling back: %s", exc)

    return _rule_based_narrative(review)


def _build_llm_prompt(review: HoldingsReview) -> str:
    tier_summary = " / ".join(
        f"{t.tier}: 開{t.n_open} 收{t.n_closed} "
        f"勝率 {t.closed_win_rate or 'N/A'}% 平均{t.avg_realised_pct or 0:+.1f}%"
        for t in review.tier_stats if t.n_total > 0
    )
    triggers = (
        f"STOP_LOSS×{review.triggers.stop_loss} / TAKE_PROFIT×{review.triggers.take_profit} / "
        f"TIME_STOP×{review.triggers.time_stop} / TIER_DROP×{review.triggers.tier_drop}"
    )
    warn_lines = "\n".join(
        f"- {w.ticker} {w.name}: {w.message}"
        for w in review.risk_warnings[:5]
    ) or "無風險警示"

    return (
        f"你是台股資金管理顧問。以下是模擬持倉組合過去 {review.lookback_days} 日的復盤統計，"
        f"請用 2-3 句繁體中文輸出「今日重點觀察」+「明日該注意什麼」。\n\n"
        f"今日: {review.today}  預算 NT${review.budget_twd:,}\n"
        f"持倉: {review.n_open} 支 (未實現 P&L {review.portfolio_unrealised_pct:+.2f}%, "
        f"NT${review.portfolio_unrealised_twd:+,})\n"
        f"已平倉: {review.n_closed_in_period} 支 (勝率 {review.closed_win_rate or 'N/A'}%, "
        f"平均 {review.avg_realised_pct or 0:+.2f}%, 實現 NT${review.portfolio_realised_twd:+,})\n"
        f"觸發: {triggers}\n"
        f"Tier 表現: {tier_summary}\n"
        f"大盤 TAIEX: {review.taiex_return_pct or 0:+.2f}%   Alpha: {review.alpha_pct or 0:+.2f}%\n"
        f"風險警示:\n{warn_lines}\n\n"
        f"輸出 2-3 句，conversational，不用 markdown 標題。"
    )


def _rule_based_narrative(review: HoldingsReview) -> str:
    """Deterministic fallback when LLM is unavailable."""
    parts: list[str] = []

    # 1) Portfolio status
    if review.n_open == 0:
        parts.append("目前無持倉。")
    else:
        pnl = review.portfolio_unrealised_pct
        pnl_color = "賺" if pnl > 0.5 else ("虧" if pnl < -0.5 else "持平")
        parts.append(
            f"持倉 {review.n_open} 支，未實現 {pnl_color} {pnl:+.2f}% "
            f"(NT${review.portfolio_unrealised_twd:+,})。"
        )

    # 2) Closed period summary
    if review.n_closed_in_period > 0:
        wr = review.closed_win_rate
        avg = review.avg_realised_pct or 0
        parts.append(
            f"過去 {review.lookback_days} 天平倉 {review.n_closed_in_period} 支，"
            f"勝率 {wr}%，平均報酬 {avg:+.2f}%。"
        )
        # Trigger callout
        if review.triggers.tier_drop_rate > 0.4:
            parts.append(
                f"⚠️ TIER_DROP 觸發 {review.triggers.tier_drop} 次 "
                f"({review.triggers.tier_drop_rate*100:.0f}% 比率偏高)，"
                f"進場時機篩選需收緊（建議 MIN_CONFIDENCE 升至 90+）。"
            )

    # 3) Risk warnings
    if review.risk_warnings:
        high_warn = [w for w in review.risk_warnings if w.severity == "high"]
        if high_warn:
            tickers = ", ".join(f"{w.ticker} {w.name}" for w in high_warn[:3])
            parts.append(f"⚠️ 接近停損: {tickers}，明早開盤要注意。")

    # 4) Alpha
    if review.alpha_pct is not None:
        if review.alpha_pct > 0.3:
            parts.append(f"Alpha vs 大盤 {review.alpha_pct:+.2f}% — 表現優於指數。")
        elif review.alpha_pct < -0.3:
            parts.append(f"Alpha vs 大盤 {review.alpha_pct:+.2f}% — 落後指數，檢視配置。")

    return " ".join(parts) if parts else "今日無顯著變化。"
