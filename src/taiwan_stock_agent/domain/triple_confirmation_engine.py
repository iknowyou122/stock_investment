"""Triple Confirmation Engine — deterministic confidence scoring.

Score breakdown (max 100 pts before risk deductions):

  Pillar 1: Momentum (0–50 pts)
    +20  close > vwap_5d   (price above 5-day volume-weighted average)
    +20  daily_volume > 20-day avg volume × 1.5  (daily volume surge)
    +5   (close - low) / (high - low) > 0.7  (K線收盤強弱比: close near top of bar)
    +5   consecutive_up_days >= 3  (連漲天數: price closed higher for 3+ sessions)

  Pillar 2: Chip (0–40 pts paid | 0–40 pts free-tier)
    Paid (chip_data_available=True, FinMind plan):
      +15  net_buyer_count_diff > 0   (more buyer branches than seller branches)
      +15  concentration_top15 > 0.35 (top-15 branches hold >35% of total buy vol)
      +10  no 隔日沖 branch in top-3 buyers  (quality confirmation)
    Free-tier (chip_data_available=False, TWSE opendata fallback):
      +15  foreign_net_buy > 0        (外資買賣超 > 0)
      +10  trust_net_buy > 0          (投信買賣超 > 0)
      +5   dealer_net_buy > 0         (自營商買賣超 > 0)
      +10  margin_balance_change ≤ 0  (融資餘額未增加)
      +5   all three institutions > 0  (三大法人同向: bonus when 外資+投信+自營商 all net buy)
      +5   foreign_consecutive_buy_days >= 3  (外資連買天數)
    NOTE: paid and free-tier are mutually exclusive. Paid takes precedence.

  Pillar 3: Space / POC proxy (0–35 pts)
    +20  close > twenty_day_high × 0.99  (within 1% of or above 20-day high)
    +5   MA5 > MA10 > MA20  (均線多頭排列: bullish MA alignment; requires ≥ 20 sessions)
    +5   MA20 slope > 0  (20-day MA is rising; computed as 5-day diff)
         NOTE: requires ≥ 24 sessions; skipped with flag if insufficient history
    +5   stock 5d return > TAIEX 5d return × 1.2  (RS vs 大盤: relative strength)

  Risk deductions:
    -25  隔日沖 in top-3 buyers
    -15  momentum divergence (Phase 4, skipped in Phase 1-3)
    -10  融券餘額暴增 AND 券資比 > 15% (short balance spike + high short/margin ratio)

  Thresholds:
    free_tier_mode=False (default): LONG ≥ 70  CAUTION ≤ 30
    free_tier_mode=True:            LONG ≥ 55  CAUTION ≤ 30
    LONG guard: in free_tier_mode, LONG is blocked when chip_pts == 0
                (chip_pts=0 means TWSE API unavailable — no chip confirmation)

  Final: confidence = max(0, min(100, score))

Extensibility guide:
  Adding a new SCORING factor:
    1. Add `new_factor_pts: int = 0` to _ScoreBreakdown
    2. Add it to `total` property sum
    3. Add `_new_factor_score(self, ...) -> tuple[int, str | None]` method
    4. Call in _compute(), assign bd.new_factor_pts = pts
    5. Add tests

  Adding a new LLM HINT (non-scoring):
    1. Add `new_hint: type | None = None` to _AnalysisHints
    2. Compute in _compute_hints()
    3. Reference in StrategistAgent._format_hints_for_prompt()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pandas as pd

from taiwan_stock_agent.domain.models import (
    BrokerWithLabel,
    ChipReport,
    DailyOHLCV,
    ExecutionPlan,
    SignalOutput,
    TWSEChipProxy,
    VolumeProfile,
    Reasoning,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# --- Thresholds ---
_LONG_THRESHOLD = 70           # default (paid tier)
_LONG_THRESHOLD_FREE = 60      # free_tier_mode=True (raised from 55 after Tier A expansion)
_CAUTION_THRESHOLD = 30

# --- Pillar max scores (for documentation and future rebalancing) ---
_PILLAR1_MAX = 55              # 20 (VWAP) + 20 (vol surge) + 5 (close strength) + 5 (consec up) + 5 (vol trend)
_PILLAR2_PAID_MAX = 45         # 40 base + 5 FII presence
_PILLAR2_FREE_MAX = 63         # 50 base + 5 (trust consec) + 3 (dealer consec) + 5 (margin util) − 5 (SBL deduction worst case)
_PILLAR3_MAX = 45              # 20 (20d high) + 10 (60d high) + 5 (MA align) + 5 (MA20 slope) + 5 (RS vs TAIEX)

# --- Known FII branch codes (hardcoded; stable, ~1-2 changes per year) ---
# Used in _apply_paid_chip() for FII presence detection.
# Do NOT import this from broker_label_classifier to avoid coupling.
_KNOWN_FII_BRANCH_CODES: dict[str, str] = {
    "1480": "摩根大通",
    "1560": "美林",
    "9200": "瑞銀",
    "1770": "花旗",
    "2030": "高盛",
    "1710": "法國巴黎",
    "8150": "德意志",
    "1790": "麥格理",
}

# --- MA20 slope ---
_MA20_SLOPE_MIN_SESSIONS = 25  # 20 (MA window) + 5 (diff lookback) so iloc[-6] is valid
_MA20_SLOPE_DIFF_DAYS = 5      # compare MA20 today vs 5 sessions ago


@dataclass
class _AnalysisHints:
    """Non-scoring contextual hints for LLM reasoning.

    These fields are NEVER included in _ScoreBreakdown.total.
    To add a new hint: add an Optional field here and compute it in _compute_hints().

    Kept separate from _ScoreBreakdown by design — mixing scoring and hints
    would allow hints to silently inflate the score.
    """
    # Momentum hints
    rsi_14: float | None = None          # RSI(14); >70 overbought, <30 oversold
    macd_line: float | None = None       # MACD line value
    macd_signal: float | None = None     # MACD signal line value
    macd_cross: str | None = None        # "golden" | "dead" | None
    ma20_slope_pct: float | None = None  # % change in MA20 over _MA20_SLOPE_DIFF_DAYS sessions
    ma20_streak: int | None = None       # consecutive sessions close > MA20 (positive) or < MA20 (negative)
    # Space hints
    gap_down_pct: float | None = None    # (open - prev_close) / prev_close; negative = gap down
    high52w_pct: float | None = None     # (close - 52w_high) / 52w_high; negative = below 52w high
    # Chip hints (Tier A expansion)
    daytrade_ratio: float | None = None    # 當沖占比; from TWSEChipProxy (non-scoring)
    short_cover_days: float | None = None  # derived: short_balance / avg_daily_volume


@dataclass
class _ScoreBreakdown:
    """Intermediate scoring state for transparency and testing.

    Extensibility: add new factor pts fields here and in `total` sum.
    Fields ending in _pts are scoring; `flags` is metadata.
    `_AnalysisHints` is a SEPARATE dataclass — never add hint fields here.
    """
    # Pillar 1: Momentum (max _PILLAR1_MAX = 55)
    vwap_5d_pts: int = 0
    volume_surge_pts: int = 0
    close_strength_pts: int = 0  # (close-low)/(high-low) > 0.7 → +5 (K線收盤強弱比)
    consec_up_pts: int = 0       # consecutive_up_days >= 3 → +5 (連漲天數)
    volume_trend_pts: int = 0    # 3 consecutive increasing vol sessions → +5 (量能遞增)

    # Pillar 2: Chip — paid FinMind (max _PILLAR2_PAID_MAX = 45)
    # Active when chip_data_available=True; gated by Phase 1 spike validation
    net_buyer_diff_pts: int = 0
    concentration_pts: int = 0
    no_daytrade_pts: int = 0
    paid_fii_presence_pts: int = 0  # known FII in top_buyers → +5

    # Pillar 2: Chip — free-tier TWSE proxies (max _PILLAR2_FREE_MAX = 63)
    # Active when chip_data_available=False; mutually exclusive with paid chip
    twse_foreign_pts: int = 0       # 外資買賣超 > 0  → +15
    twse_trust_pts: int = 0         # 投信買賣超 > 0  → +10
    twse_dealer_pts: int = 0        # 自營商買賣超 > 0 → +5
    twse_margin_pts: int = 0        # 融資餘額 change ≤ 0 → +10
    twse_all_inst_pts: int = 0      # 三大法人同向 (all three > 0) → +5
    twse_foreign_consec_pts: int = 0  # 外資連買天數 >= 3 → +5
    # Tier A expansion: new free-tier chip factors
    twse_trust_consec_pts: int = 0    # 投信連買天數 >= 3 → +5
    twse_dealer_consec_pts: int = 0   # 自營商連買天數 >= 3 → +3
    twse_margin_util_pts: int = 0     # util < 20% → +5; util > 80% → -5 (can be negative)
    twse_sbl_deduction: int = 0       # sbl_ratio > 10% → 5 (subtracted from total)

    # Pillar 3: Space (max _PILLAR3_MAX = 45)
    space_pts: int = 0              # close > twenty_day_high × 0.99 → +20
    sixty_day_high_pts: int = 0     # close > sixty_day_high × 0.99 → +10 (季線突破)
    ma_alignment_pts: int = 0       # MA5 > MA10 > MA20 → +5 (均線多頭排列)
    ma20_slope_pts: int = 0         # MA20 rising (slope > 0) → +5
    rs_pts: int = 0                 # stock 5d return > TAIEX 5d return × 1.2 → +5

    # Risk deductions
    daytrade_deduction: int = 0
    divergence_deduction: int = 0   # Phase 4+
    short_spike_deduction: int = 0  # 融券餘額暴增 AND 券資比 > 15% → -10

    flags: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Clamped score sum. Explicit enumeration is intentional — see extensibility guide."""
        raw = (
            self.vwap_5d_pts
            + self.volume_surge_pts
            + self.close_strength_pts
            + self.consec_up_pts
            + self.volume_trend_pts
            + self.net_buyer_diff_pts
            + self.concentration_pts
            + self.no_daytrade_pts
            + self.paid_fii_presence_pts
            + self.twse_foreign_pts
            + self.twse_trust_pts
            + self.twse_dealer_pts
            + self.twse_margin_pts
            + self.twse_all_inst_pts
            + self.twse_foreign_consec_pts
            + self.twse_trust_consec_pts
            + self.twse_dealer_consec_pts
            + self.twse_margin_util_pts    # can be negative
            - self.twse_sbl_deduction      # subtract (positive value stored)
            + self.space_pts
            + self.sixty_day_high_pts
            + self.ma_alignment_pts
            + self.ma20_slope_pts
            + self.rs_pts
            - self.daytrade_deduction
            - self.divergence_deduction
            - self.short_spike_deduction
        )
        return max(0, min(100, raw))

    @property
    def chip_pts(self) -> int:
        """Total chip pillar points (paid or free-tier, whichever is active)."""
        return (
            self.net_buyer_diff_pts
            + self.concentration_pts
            + self.no_daytrade_pts
            + self.paid_fii_presence_pts
            + self.twse_foreign_pts
            + self.twse_trust_pts
            + self.twse_dealer_pts
            + self.twse_margin_pts
            + self.twse_all_inst_pts
            + self.twse_foreign_consec_pts
            + self.twse_trust_consec_pts
            + self.twse_dealer_consec_pts
            + self.twse_margin_util_pts    # can be negative
            - self.twse_sbl_deduction
        )


class TripleConfirmationEngine:
    """Compute the Triple Confirmation confidence score.

    Args:
        free_tier_mode: When True, uses LONG threshold of 55 instead of 70,
            and labels output with free_tier_mode=True. Also enables LONG guard
            (LONG blocked when chip_pts == 0).

    Usage::

        engine = TripleConfirmationEngine()
        signal = engine.score(
            ohlcv=today_ohlcv,
            ohlcv_history=last_20_days,
            chip_report=chip_report,
            volume_profile=volume_profile,
        )

        # With free-tier TWSE proxy:
        engine = TripleConfirmationEngine(free_tier_mode=True)
        signal = engine.score(..., twse_proxy=proxy)
    """

    def __init__(self, free_tier_mode: bool = False) -> None:
        self._free_tier_mode = free_tier_mode
        self._long_threshold = _LONG_THRESHOLD_FREE if free_tier_mode else _LONG_THRESHOLD
        self._taiex_history: list[DailyOHLCV] = []  # injected for RS vs 大盤 (Factor 6)

    def score(
        self,
        ohlcv: DailyOHLCV,
        ohlcv_history: list[DailyOHLCV],
        chip_report: ChipReport,
        volume_profile: VolumeProfile,
        twse_proxy: TWSEChipProxy | None = None,
        taiex_history: list[DailyOHLCV] | None = None,
    ) -> SignalOutput:
        """Compute deterministic confidence and return a SignalOutput.

        The reasoning fields are left empty here — the LLM layer (StrategistAgent)
        fills them in from the breakdown and chip report.

        ohlcv_history should include at least 24 sessions (for MA20 slope).
        taiex_history: TAIEX index OHLCV for RS vs 大盤 scoring (Factor 6, optional).
        """
        self._taiex_history = taiex_history or []
        breakdown = self._compute(ohlcv, ohlcv_history, chip_report, volume_profile, twse_proxy)
        return self._build_signal(ohlcv, breakdown, volume_profile, chip_report)

    def score_with_breakdown(
        self,
        ohlcv: DailyOHLCV,
        ohlcv_history: list[DailyOHLCV],
        chip_report: ChipReport,
        volume_profile: VolumeProfile,
        twse_proxy: TWSEChipProxy | None = None,
        taiex_history: list[DailyOHLCV] | None = None,
    ) -> tuple[SignalOutput, _ScoreBreakdown]:
        """Same as score() but also returns the breakdown for LLM prompting."""
        self._taiex_history = taiex_history or []
        breakdown = self._compute(ohlcv, ohlcv_history, chip_report, volume_profile, twse_proxy)
        return self._build_signal(ohlcv, breakdown, volume_profile, chip_report), breakdown

    def score_full(
        self,
        ohlcv: DailyOHLCV,
        ohlcv_history: list[DailyOHLCV],
        chip_report: ChipReport,
        volume_profile: VolumeProfile,
        twse_proxy: TWSEChipProxy | None = None,
        taiex_history: list[DailyOHLCV] | None = None,
    ) -> tuple[SignalOutput, _ScoreBreakdown, _AnalysisHints]:
        """Score + breakdown + analysis hints. Use this from StrategistAgent."""
        self._taiex_history = taiex_history or []
        breakdown = self._compute(ohlcv, ohlcv_history, chip_report, volume_profile, twse_proxy)
        hints = self._compute_hints(ohlcv, ohlcv_history, twse_proxy=twse_proxy)
        signal = self._build_signal(ohlcv, breakdown, volume_profile, chip_report)
        return signal, breakdown, hints

    # ------------------------------------------------------------------
    # Private computation
    # ------------------------------------------------------------------

    def _compute(
        self,
        ohlcv: DailyOHLCV,
        ohlcv_history: list[DailyOHLCV],
        chip_report: ChipReport,
        volume_profile: VolumeProfile,
        twse_proxy: TWSEChipProxy | None,
    ) -> _ScoreBreakdown:
        bd = _ScoreBreakdown()

        # --- Pillar 1: Momentum ---
        vwap_pts, vwap_flag = self._vwap_score(ohlcv, ohlcv_history)
        bd.vwap_5d_pts = vwap_pts
        if vwap_flag:
            bd.flags.append(vwap_flag)

        vol_pts, vol_flag = self._volume_surge_score(ohlcv, ohlcv_history)
        bd.volume_surge_pts = vol_pts
        if vol_flag:
            bd.flags.append(vol_flag)

        cs_pts, cs_flag = self._close_strength_score(ohlcv)
        bd.close_strength_pts = cs_pts
        if cs_flag:
            bd.flags.append(cs_flag)

        cu_pts = self._consec_up_score(ohlcv, ohlcv_history)
        bd.consec_up_pts = cu_pts

        vt_pts = self._volume_trend_score(ohlcv_history)
        bd.volume_trend_pts = vt_pts

        # --- Pillar 2: Chip (paid vs free-tier, mutually exclusive) ---
        if chip_report.net_buyer_count_diff != 0 or chip_report.active_branch_count > 0:
            # Paid chip data available — use FinMind factors
            self._apply_paid_chip(bd, chip_report)
        elif twse_proxy is not None and twse_proxy.is_available:
            # Free-tier fallback — TWSE opendata proxies
            self._apply_free_chip(bd, twse_proxy)
        else:
            bd.flags.append("NO_CHIP_DATA")

        # --- Pillar 3: Space ---
        space_pts, space_flag = self._space_score(ohlcv, volume_profile)
        bd.space_pts = space_pts
        if space_flag:
            bd.flags.append(space_flag)

        s60_pts, s60_flag = self._sixty_day_high_score(ohlcv, volume_profile)
        bd.sixty_day_high_pts = s60_pts
        if s60_flag:
            bd.flags.append(s60_flag)

        ma_align_pts, ma_align_flag = self._ma_alignment_score(ohlcv_history)
        bd.ma_alignment_pts = ma_align_pts
        if ma_align_flag:
            bd.flags.append(ma_align_flag)

        slope_pts, slope_flag = self._ma20_slope_score(ohlcv_history)
        bd.ma20_slope_pts = slope_pts
        if slope_flag:
            bd.flags.append(slope_flag)

        # RS vs 大盤 requires taiex_history to be injected; stored on instance if provided
        rs_pts = 0
        if hasattr(self, "_taiex_history") and self._taiex_history:
            rs_pts, rs_flag = self._rs_score(ohlcv, ohlcv_history, self._taiex_history)
            if rs_flag:
                bd.flags.append(rs_flag)
        bd.rs_pts = rs_pts

        logger.debug(
            "score breakdown for %s: p1=%d+%d+%d+%d+%d "
            "p2_paid=%d+%d+%d+%d "
            "p2_free=%d+%d+%d+%d+%d+%d+%d+%d+%d-%d "
            "p3=%d+%d+%d+%d+%d deduct=%d+%d flags=%s → total=%d",
            ohlcv.ticker,
            bd.vwap_5d_pts, bd.volume_surge_pts, bd.close_strength_pts,
            bd.consec_up_pts, bd.volume_trend_pts,
            bd.net_buyer_diff_pts, bd.concentration_pts, bd.no_daytrade_pts,
            bd.paid_fii_presence_pts,
            bd.twse_foreign_pts, bd.twse_trust_pts, bd.twse_dealer_pts, bd.twse_margin_pts,
            bd.twse_all_inst_pts, bd.twse_foreign_consec_pts,
            bd.twse_trust_consec_pts, bd.twse_dealer_consec_pts,
            bd.twse_margin_util_pts, bd.twse_sbl_deduction,
            bd.space_pts, bd.sixty_day_high_pts, bd.ma_alignment_pts, bd.ma20_slope_pts, bd.rs_pts,
            bd.daytrade_deduction, bd.short_spike_deduction, bd.flags, bd.total,
        )
        return bd

    def _apply_paid_chip(self, bd: _ScoreBreakdown, chip_report: ChipReport) -> None:
        """Apply FinMind paid chip scoring to breakdown (in-place)."""
        if chip_report.net_buyer_count_diff > 0:
            bd.net_buyer_diff_pts = 15

        if chip_report.active_branch_count >= 10:
            if chip_report.concentration_top15 > 0.35:
                bd.concentration_pts = 15
        else:
            bd.flags.append(
                f"THIN_MARKET: only {chip_report.active_branch_count} active branches "
                "— concentration check skipped"
            )

        top3 = chip_report.top_buyers[:3]
        daytrade_in_top3 = any(b.label == "隔日沖" for b in top3)
        if not daytrade_in_top3:
            bd.no_daytrade_pts = 10
        else:
            bd.daytrade_deduction = 25
            top3_names = [b.branch_name for b in top3 if b.label == "隔日沖"]
            bd.flags.append(f"隔日沖_TOP3: {', '.join(top3_names)}")
            chip_report.risk_flags.append("隔日沖_TOP3")

        # Known FII branch code detection → +5 (Tier A, A5)
        top_buyers = chip_report.top_buyers
        if any(b.branch_code in _KNOWN_FII_BRANCH_CODES for b in top_buyers):
            bd.paid_fii_presence_pts = 5
            fii_names = [
                _KNOWN_FII_BRANCH_CODES[b.branch_code]
                for b in top_buyers
                if b.branch_code in _KNOWN_FII_BRANCH_CODES
            ]
            bd.flags.append(f"FII_PRESENT: {', '.join(fii_names)}")

    def _apply_free_chip(self, bd: _ScoreBreakdown, proxy: TWSEChipProxy) -> None:
        """Apply TWSE free-tier chip scoring to breakdown (in-place)."""
        if proxy.foreign_net_buy > 0:
            bd.twse_foreign_pts = 15
        if proxy.trust_net_buy > 0:
            bd.twse_trust_pts = 10
        if proxy.dealer_net_buy > 0:
            bd.twse_dealer_pts = 5
        if proxy.margin_balance_change <= 0:
            bd.twse_margin_pts = 10
        # 三大法人同向: bonus when all three are net buyers
        if proxy.foreign_net_buy > 0 and proxy.trust_net_buy > 0 and proxy.dealer_net_buy > 0:
            bd.twse_all_inst_pts = 5
        # 外資連買天數 >= 3
        if proxy.foreign_consecutive_buy_days >= 3:
            bd.twse_foreign_consec_pts = 5

        # --- Tier A expansion factors ---
        # 投信連買天數 >= 3 → +5
        if proxy.trust_consecutive_buy_days >= 3:
            bd.twse_trust_consec_pts = 5
        # 自營商連買天數 >= 3 → +3
        if proxy.dealer_consecutive_buy_days >= 3:
            bd.twse_dealer_consec_pts = 3
        # 融資使用率
        if proxy.margin_utilization_rate is not None:
            if proxy.margin_utilization_rate < 0.20:
                bd.twse_margin_util_pts = 5   # healthy: lots of room to buy
            elif proxy.margin_utilization_rate > 0.80:
                bd.twse_margin_util_pts = -5  # crowded: nearly maxed out
                bd.flags.append(f"MARGIN_HIGH_UTIL: {proxy.margin_utilization_rate:.1%}")
        # 借券賣出占比 > 10% → deduction
        if proxy.sbl_available and proxy.sbl_ratio > 0.10:
            bd.twse_sbl_deduction = 5
            bd.flags.append(f"SBL_HEAVY: {proxy.sbl_ratio:.1%}")

        # 融券餘額暴增 + 券資比 > 15% → risk deduction
        if proxy.short_balance_increased and proxy.short_margin_ratio > 0.15:
            bd.short_spike_deduction = 10
            bd.flags.append(
                f"SHORT_SPIKE: ratio={proxy.short_margin_ratio:.2%}"
            )
        for flag in proxy.data_quality_flags:
            bd.flags.append(f"TWSE:{flag}")

    def _close_strength_score(self, ohlcv: DailyOHLCV) -> tuple[int, str | None]:
        """K線收盤強弱比: (close - low) / (high - low) > 0.7 → +5 pts.

        Measures where the close sits within today's range. > 0.7 means close
        is in the top 30% of the day's range (strong close).
        Skipped if high == low (no price movement, e.g. halted stock).
        """
        bar_range = ohlcv.high - ohlcv.low
        if bar_range <= 0:
            return 0, "CLOSE_STRENGTH_SKIP:ZERO_RANGE"
        ratio = (ohlcv.close - ohlcv.low) / bar_range
        return (5, None) if ratio > 0.7 else (0, None)

    def _consec_up_score(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> int:
        """連漲天數: +5 pts if close has risen for 3+ consecutive sessions (incl. today).

        Counts consecutive sessions where close > prev_close going backward
        from today (inclusive). Requires at least 3 sessions in history.
        Returns 0 if insufficient history.
        """
        # Build sorted sequence: history + today's ohlcv
        all_bars = sorted(history, key=lambda x: x.trade_date) + [ohlcv]
        if len(all_bars) < 3:
            return 0
        count = 0
        for i in range(len(all_bars) - 1, 0, -1):
            if all_bars[i].close > all_bars[i - 1].close:
                count += 1
            else:
                break
        return 5 if count >= 3 else 0

    def _ma_alignment_score(
        self, history: list[DailyOHLCV]
    ) -> tuple[int, str | None]:
        """均線多頭排列: MA5 > MA10 > MA20 → +5 pts.

        Requires at least 20 sessions. Skipped with flag if insufficient.
        """
        recent = sorted(history, key=lambda x: x.trade_date)
        if len(recent) < 20:
            return 0, "INSUFFICIENT_HISTORY_MA_ALIGNMENT"
        closes = pd.Series([d.close for d in recent])
        ma5 = closes.rolling(5).mean().iloc[-1]
        ma10 = closes.rolling(10).mean().iloc[-1]
        ma20 = closes.rolling(20).mean().iloc[-1]
        if pd.isna(ma5) or pd.isna(ma10) or pd.isna(ma20):
            return 0, "MA_ALIGNMENT_NAN"
        return (5, None) if ma5 > ma10 > ma20 else (0, None)

    def _rs_score(
        self,
        ohlcv: DailyOHLCV,
        history: list[DailyOHLCV],
        taiex_history: list[DailyOHLCV],
    ) -> tuple[int, str | None]:
        """RS vs 大盤: +5 pts if stock 5d return > TAIEX 5d return × 1.2.

        Computes 5-day price return for the stock and for TAIEX.
        Requires at least 5 sessions in both histories.
        Stock return: (today.close - history[-5].close) / history[-5].close
        """
        stock_bars = sorted(history, key=lambda x: x.trade_date)
        taiex_bars = sorted(taiex_history, key=lambda x: x.trade_date)

        # Need at least 5 sessions back (index 4 from end = 5 sessions ago)
        if len(stock_bars) < 5 or len(taiex_bars) < 5:
            return 0, "INSUFFICIENT_HISTORY_RS"

        stock_base = stock_bars[-5].close
        taiex_base = taiex_bars[-5].close
        if stock_base <= 0 or taiex_base <= 0:
            return 0, "RS_SCORE_ZERO_BASE"

        # Find TAIEX close closest to today's date
        taiex_close = taiex_bars[-1].close

        stock_ret = (ohlcv.close - stock_base) / stock_base
        taiex_ret = (taiex_close - taiex_base) / taiex_base

        return (5, None) if stock_ret > taiex_ret * 1.2 else (0, None)

    def _vwap_score(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> tuple[int, str | None]:
        vwap_5d = self._vwap_5d(history)
        if vwap_5d is None:
            return 0, "INSUFFICIENT_HISTORY_VWAP5D"
        return (20, None) if ohlcv.close > vwap_5d else (0, None)

    def _volume_surge_score(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> tuple[int, str | None]:
        vol_20ma = self._volume_20ma(history)
        if vol_20ma is None:
            return 0, "INSUFFICIENT_HISTORY_VOL20MA"
        if ohlcv.volume <= vol_20ma * 1.5:
            return 0, None
        # Volume surge confirmed — check direction: distribution day does NOT get bonus
        # Use the last history entry whose trade_date < ohlcv.trade_date (prev day)
        prev_day = [d for d in history if d.trade_date < ohlcv.trade_date]
        if prev_day:
            prev_close = max(prev_day, key=lambda x: x.trade_date).close
            if ohlcv.close < prev_close:
                return 0, "VOLUME_DISTRIBUTION"
        return 20, None

    def _volume_trend_score(self, history: list[DailyOHLCV]) -> int:
        """Volume accumulation trend: +5 if prior 3 sessions have strictly increasing volume.

        Checks sessions[-4], [-3], [-2] (excludes today) to avoid double-counting
        with _volume_surge_score which measures today's level.
        Requires >= 4 sessions in history.
        """
        sorted_history = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_history) < 4:
            return 0
        v1, v2, v3 = (
            sorted_history[-4].volume,
            sorted_history[-3].volume,
            sorted_history[-2].volume,
        )
        return 5 if v1 < v2 < v3 else 0

    def _space_score(
        self, ohlcv: DailyOHLCV, volume_profile: VolumeProfile
    ) -> tuple[int, str | None]:
        if ohlcv.close > volume_profile.twenty_day_high * 0.99:
            return 20, None
        return 0, None

    def _sixty_day_high_score(
        self, ohlcv: DailyOHLCV, volume_profile: VolumeProfile
    ) -> tuple[int, str | None]:
        """60-day high breakout: +10 if close within 1% of or above 60d high.

        Fires independently of the 20d high check — genuine breakouts can score both (+30 total).
        Returns INSUFFICIENT_HISTORY_60D_HIGH flag when fewer than 40 sessions available.
        """
        if volume_profile.sixty_day_sessions < 40:
            return 0, "INSUFFICIENT_HISTORY_60D_HIGH"
        if volume_profile.sixty_day_high <= 0:
            return 0, None
        if ohlcv.close > volume_profile.sixty_day_high * 0.99:
            return 10, None
        return 0, None

    def _ma20_slope_score(self, history: list[DailyOHLCV]) -> tuple[int, str | None]:
        """MA20 slope: +5 pts if MA20 is rising (slope > 0).

        Slope = (MA20[-1] - MA20[-5]) / MA20[-5]
        Requires at least _MA20_SLOPE_MIN_SESSIONS = 24 sessions.
        """
        slope = self._ma20_slope(history)
        if slope is None:
            return 0, "INSUFFICIENT_HISTORY_MA20_SLOPE"
        return (5, None) if slope > 0 else (0, None)

    def _build_signal(
        self,
        ohlcv: DailyOHLCV,
        breakdown: _ScoreBreakdown,
        volume_profile: VolumeProfile,
        chip_report: ChipReport,
    ) -> SignalOutput:
        confidence = breakdown.total
        action = self._map_action(confidence, breakdown.chip_pts)
        plan = self._make_execution_plan(ohlcv, volume_profile)

        data_quality_flags = list(ohlcv.data_quality_flags)
        data_quality_flags.extend(chip_report.data_quality_flags)
        data_quality_flags.extend(volume_profile.data_quality_flags)

        return SignalOutput(
            ticker=ohlcv.ticker,
            date=ohlcv.trade_date,
            action=action,
            confidence=confidence,
            reasoning=Reasoning(),
            execution_plan=plan,
            halt_flag=False,
            data_quality_flags=data_quality_flags,
            free_tier_mode=True if self._free_tier_mode else None,
        )

    def _compute_hints(
        self,
        ohlcv: DailyOHLCV,
        history: list[DailyOHLCV],
        twse_proxy: TWSEChipProxy | None = None,
    ) -> _AnalysisHints:
        """Compute non-scoring contextual hints for LLM reasoning.

        All computations here are informational only — never affect scoring.
        twse_proxy: optional; used to derive daytrade_ratio and short_cover_days.
        """
        hints = _AnalysisHints()
        sorted_history = sorted(history, key=lambda x: x.trade_date)
        closes = pd.Series([d.close for d in sorted_history])

        if len(closes) >= 14:
            hints.rsi_14 = self._rsi(closes, 14)

        if len(closes) >= 26:
            macd_line, signal_line = self._macd(closes)
            hints.macd_line = macd_line
            hints.macd_signal = signal_line
            if macd_line is not None and signal_line is not None:
                # Simple cross detection: current vs previous bar
                if len(closes) >= 27:
                    prev_closes = closes.iloc[:-1]
                    prev_macd, prev_signal = self._macd(prev_closes)
                    if prev_macd is not None and prev_signal is not None:
                        if prev_macd <= prev_signal and macd_line > signal_line:
                            hints.macd_cross = "golden"
                        elif prev_macd >= prev_signal and macd_line < signal_line:
                            hints.macd_cross = "dead"

        if len(closes) >= _MA20_SLOPE_MIN_SESSIONS:
            slope = self._ma20_slope(history)
            if slope is not None:
                hints.ma20_slope_pct = round(slope * 100, 3)

            # MA20 streak: consecutive sessions above/below MA20
            ma20 = closes.rolling(20).mean()
            streak = 0
            for i in range(len(closes) - 1, -1, -1):
                if pd.isna(ma20.iloc[i]):
                    break
                if closes.iloc[i] > ma20.iloc[i]:
                    if streak >= 0:
                        streak += 1
                    else:
                        break
                else:
                    if streak <= 0:
                        streak -= 1
                    else:
                        break
            hints.ma20_streak = streak

        # Gap down hint
        if len(sorted_history) >= 2:
            prev_close = sorted_history[-2].close
            if prev_close > 0:
                gap = (ohlcv.open - prev_close) / prev_close
                hints.gap_down_pct = round(gap * 100, 3)

        # high52w_pct: proximity to period high (uses available window, aspirationally 52w)
        all_highs = [d.high for d in sorted_history]
        if all_highs:
            period_high = max(all_highs)
            if period_high > 0:
                hints.high52w_pct = round((ohlcv.close - period_high) / period_high * 100, 2)

        # Chip hints from TWSE proxy (Tier A expansion)
        if twse_proxy is not None and twse_proxy.is_available:
            hints.daytrade_ratio = twse_proxy.daytrade_ratio

            # Derive short_cover_days = short_balance_proxy / avg_daily_volume
            # We use short_margin_ratio × margin_balance as a rough short balance proxy
            # since the actual short balance isn't directly stored in TWSEChipProxy.
            # More accurate: short_balance = short_margin_ratio × margin_balance
            # avg_daily_volume from last 20 sessions of ohlcv_history
            avg_vol = self._volume_20ma(history)
            if (
                avg_vol is not None
                and avg_vol > 0
                and twse_proxy.short_margin_ratio > 0
            ):
                # margin_balance_change is the diff, not the level — use it as a rough proxy
                # when the margin balance level isn't available separately.
                # short_cover_days requires actual short balance; if short_margin_ratio > 0
                # and we have OHLCV volume, compute an approximate value.
                # We cannot derive the exact short balance without the raw margin level,
                # so we store None if the data is insufficient.
                if twse_proxy.short_cover_days is not None:
                    hints.short_cover_days = round(twse_proxy.short_cover_days, 1)

        return hints

    def _map_action(self, confidence: int, chip_pts: int = 0) -> str:
        """Map confidence score to action label.

        LONG guard (free_tier_mode only): even if score >= threshold,
        LONG is blocked when chip_pts == 0 (no chip data available).
        A score above threshold without chip confirmation is a false signal.
        """
        if confidence >= self._long_threshold:
            if self._free_tier_mode and chip_pts == 0:
                logger.debug(
                    "LONG guard triggered: score=%d >= threshold=%d but chip_pts=0 "
                    "(TWSE unavailable) → downgraded to WATCH",
                    confidence, self._long_threshold,
                )
                return "WATCH"
            return "LONG"
        if confidence <= _CAUTION_THRESHOLD:
            return "CAUTION"
        return "WATCH"

    # ------------------------------------------------------------------
    # Static computation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _vwap_5d(history: list[DailyOHLCV]) -> float | None:
        """5-day volume-weighted average close.

        vwap_5d = Σ(close_i × volume_i) / Σ(volume_i) for last 5 sessions.
        Returns None if fewer than 5 sessions available.
        """
        recent = sorted(history, key=lambda x: x.trade_date)[-5:]
        if len(recent) < 5:
            return None
        total_vol = sum(d.volume for d in recent)
        if total_vol == 0:
            return None
        return sum(d.close * d.volume for d in recent) / total_vol

    @staticmethod
    def _volume_20ma(history: list[DailyOHLCV]) -> float | None:
        """20-session simple moving average of daily volume.

        Returns None if fewer than 20 sessions available.
        """
        recent = sorted(history, key=lambda x: x.trade_date)[-20:]
        if len(recent) < 20:
            return None
        return sum(d.volume for d in recent) / len(recent)

    @staticmethod
    def _ma20_slope(history: list[DailyOHLCV]) -> float | None:
        """MA20 slope as percentage change over _MA20_SLOPE_DIFF_DAYS sessions.

        slope = (MA20_today - MA20_N_days_ago) / MA20_N_days_ago

        Returns None if fewer than _MA20_SLOPE_MIN_SESSIONS sessions available.
        Positive = rising, Negative = falling.
        """
        recent = sorted(history, key=lambda x: x.trade_date)
        if len(recent) < _MA20_SLOPE_MIN_SESSIONS:
            return None

        closes = pd.Series([d.close for d in recent])
        ma20 = closes.rolling(20).mean()

        ma20_today = ma20.iloc[-1]
        ma20_prev = ma20.iloc[-1 - _MA20_SLOPE_DIFF_DAYS]

        if pd.isna(ma20_today) or pd.isna(ma20_prev) or ma20_prev == 0:
            return None

        return (ma20_today - ma20_prev) / ma20_prev

    @staticmethod
    def _rsi(closes: pd.Series, period: int) -> float | None:
        """RSI(period) for the most recent bar."""
        if len(closes) < period + 1:
            return None
        delta = closes.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi = 100 - (100 / (1 + rs))
        val = rsi.iloc[-1]
        return None if pd.isna(val) else round(float(val), 2)

    @staticmethod
    def _macd(
        closes: pd.Series,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> tuple[float | None, float | None]:
        """MACD line and signal line for most recent bar."""
        if len(closes) < slow + signal:
            return None, None
        ema_fast = closes.ewm(span=fast, adjust=False).mean()
        ema_slow = closes.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        ml = macd_line.iloc[-1]
        sl = signal_line.iloc[-1]
        if pd.isna(ml) or pd.isna(sl):
            return None, None
        return round(float(ml), 4), round(float(sl), 4)

    @staticmethod
    def _make_execution_plan(
        ohlcv: DailyOHLCV, volume_profile: VolumeProfile
    ) -> ExecutionPlan:
        """Compute deterministic entry/stop/target.

        entry_bid_limit = close × 0.995  (lower bound, limit order)
        entry_max_chase = close × 1.005  (upper bound, max acceptable chase)
        stop_loss       = T+0 closing price  (not intraday VWAP — requires tick data)
        target          = poc_proxy × 1.05  (5% above 20-day high proxy)
        """
        close = ohlcv.close
        return ExecutionPlan(
            entry_bid_limit=round(close * 0.995, 2),
            entry_max_chase=round(close * 1.005, 2),
            stop_loss=round(close, 2),
            target=round(volume_profile.poc_proxy * 1.05, 2),
        )
