"""Triple Confirmation Engine v2 — deterministic confidence scoring.

Score breakdown (max 100 pts before risk deductions):

  Gate (2-of-4 conditions required to enter scoring):
    Cond 1: close > 5d_avg_vwap
    Cond 2: volume > 20d_avg_volume × 1.3
    Cond 3: close >= twenty_day_high × 0.99   (only when twenty_day_high > 0)
    Cond 4: 5d_stock_return > 5d_taiex_return  (only when taiex data available)
    Fail: action=CAUTION, confidence=0, data_quality_flags=["NO_SETUP"]

  Pillar 1: Momentum (max 39 pts)
    volume_ratio_pts:     0/4/5/8 — vol/20d_avg: <1.2→0, 1.2-2.0→4, 2.0-3.0→8, ≥3.0→5+VOL_EXHAUSTION_RISK
    price_direction_pts:  0/3     — close >= prev_close → +3
    close_strength_pts:  -2/0/2/4 — (close-low)/(high-low): ≥0.8→4, 0.6-0.8→2, 0.4-0.6→0, <0.4→-2+CLOSE_WEAK_OUT_PATTERN
                                    guard: high==low → 0, flag DOJI_OR_HALT
    vwap_advantage_pts:   0/6     — close > 5d_avg_vwap → +6 (intraday VWAP unavailable on T+1)
    trend_continuity_pts: 0/3/5   — 3 consec up → 3; 4-of-5 up → 5
    volume_escalation_pts:0/3/5   — T-3<T-2<T-1 → 3; + today>T-1 → 5
    rsi_momentum_pts:     0/4     — RSI(14) 55–70 → +4 (healthy momentum, not overbought)
    dmi_initiation_pts:   0/2/4/6 — DMI +DI>-DI + ADX≥20; fresh cross/ADX rising → +6

  Pillar 2A: Chip paid (max 40 pts)
    breadth_pts:          0/5/10  — net_buyer_diff ≤0 → 0, 1–10 → 5, >10 → 10
    concentration_pts:    0/5/10  — conc<25% → 0, 25–35% → 5, >35% → 10
                                    cap: active_branch_count < 10 → max 5
    continuity_pts:       0/3/5/8 — top5 overlap with yesterday; +3 for 3d avg ≥2
    daytrade_filter_pts:  0/7     — no 隔日沖 in top3 → +7
    foreign_broker_pts:   0/3/5   — any FII in top_buyers → 3; FII in top3 + high conc → 5

  Pillar 2B: Chip free (max 40 pts)
    foreign_strength_pts:     0/4/8/12  — foreign_net_buy/avg_20d_vol ratio tiers
    trust_strength_pts:       0/3/6/8   — trust_net_buy/avg_20d_vol ratio tiers
    dealer_strength_pts:      0/2/4     — dealer_net_buy/avg_20d_vol ratio tiers
    institution_continuity_pts: 0–8     — foreign≥3d→4, trust≥3d→3, dealer≥3d→1
    institution_consensus_pts:  0/4     — all three net buy + ≥2 at medium+ strength → +4
    margin_structure_pts:    -4 to +8   — price×margin direction matrix
    margin_utilization_pts:  -4/0/+4    — <20% → +4, >80% → -4
    sbl_pressure_pts:         0/-4/-8   — sbl_ratio 5–10% → -4, >10% → -8

  Pillar 3: Structure/Space (max 38 pts)
    breakout_20d_pts:     0/8    — close ≥ twenty_day_high × 0.99 (only when > 0) → +8
    breakout_60d_pts:     0/5    — close ≥ sixty_day_high × 0.99 → +5 (≥40 sessions)
    breakout_quality_pts: 0/2    — breakout + close_strength ≥ 0.7 → +2
    breakout_volume_pts:  0/3    — breakout_20d + volume > 20d_avg × 1.5 → +3 (confirms breakout)
    ma_alignment_pts:     0/5    — MA5 > MA10 > MA20 → +5 (≥20 sessions)
    ma20_slope_pts:       0/5    — MA20 rising vs 5d ago → +5 (≥25 sessions)
    relative_strength_pts:0/3/5  — stock 5d return vs TAIEX; 0–20% outperform → 3, >20% → 5
    upside_space_pts:     0/2/5  — distance to 120d/52w high: 3–8% → 2, >8% → 5
    bb_squeeze_breakout_pts: 0/2/3/5 — BB squeeze: setup 2, breakout 3, +vol confirm 5

  Risk deductions:
    daytrade_risk:        0/-25  — 隔日沖 in top3
    long_upper_shadow:    0/-8   — vol > 1.5×avg AND close_strength < 0.4 (組合懲罰，疊加 close_strength -2)
    vol_consecutive_surge:0/-5   — vol > 1.5×avg 連續 ≥3 日（框架第3根爆量不追）flag VOL_DAY{N}_NO_CHASE
    overheat_ma20:        0/-5   — close > MA20 × 1.10
    overheat_ma60:        0/-5   — close > MA60 × 1.20
    daytrade_heat:        0/-5   — daytrade_ratio > 35% AND close not above 20d high
    sbl_breakout_fail:    0/-8   — sbl_ratio > 10% AND close < twenty_day_high × 0.99
    margin_chase_heat:    0/-5   — price up + 融資大增 + margin_utilization > 60%
    adx_exhaustion:       0/-6   — ADX > 55
    dmi_divergence:       0/-4   — +DI↓ −DI↑ + price up

  Thresholds (regime-adjusted):
    Uptrend   (TAIEX MA20 today > 5d ago):  LONG ≥ 63
    Neutral   (default):                    LONG ≥ 68
    Downtrend (TAIEX MA20 < 5d ago by >1%): LONG ≥ 73
    WATCH: score ≥ 45
    CAUTION: score < 45

  Final: confidence = max(0, min(100, score))
  scoring_version: "v2"

Extensibility guide:
  Adding a new SCORING factor:
    1. Add `new_factor_pts: int = 0` to _ScoreBreakdown
    2. Add it to `total` property sum (explicit enumeration)
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PILLAR1_MAX = 35
_PILLAR2_PAID_MAX = 40
_PILLAR2_FREE_MAX = 40
_PILLAR3_MAX = 35

_LONG_THRESHOLD_NEUTRAL = 65    # was 55
_LONG_THRESHOLD_UPTREND = 60    # was 50
_LONG_THRESHOLD_DOWNTREND = 70  # was 60
_WATCH_MIN = 45                  # was 40
_CAUTION_THRESHOLD = 44         # derived from _WATCH_MIN - 1

# MA20 slope computation parameters
_MA20_SLOPE_MIN_SESSIONS = 25   # 20 (MA window) + 5 (diff lookback) so iloc[-6] is valid
_MA20_SLOPE_DIFF_DAYS = 5       # compare MA20 today vs 5 sessions ago

# v2.2a Liquidity Gate — daily turnover thresholds (NT$).
# Amount-based instead of share-count so high-priced stocks aren't over-filtered
# and low-priced stocks aren't under-filtered.
_LIQUIDITY_THRESHOLDS: dict[str, float] = {
    "TSE":  40_000_000.0,   # NT$ 40M/day — filters thin stocks, still reachable by mid-cap
    "TPEx": 15_000_000.0,   # NT$ 15M/day — TPEx boards are thinner, but still need tradable depth
}
_DEFAULT_MARKET = "TSE"

# Known FII branch codes (hardcoded; stable, ~1-2 changes per year)
# Do NOT import from broker_label_classifier to avoid coupling.
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


# ---------------------------------------------------------------------------
# Analysis hints (non-scoring, for LLM reasoning only)
# ---------------------------------------------------------------------------

@dataclass
class _AnalysisHints:
    """Non-scoring contextual hints for LLM reasoning.

    These fields are NEVER included in _ScoreBreakdown.total.
    To add a new hint: add an Optional field here and compute it in _compute_hints().

    Kept separate from _ScoreBreakdown by design — mixing scoring and hints
    would allow hints to silently inflate the score.
    """
    # Momentum hints
    rsi_14: float | None = None
    macd_line: float | None = None
    macd_signal: float | None = None
    macd_cross: str | None = None
    ma20_slope_pct: float | None = None
    ma20_streak: int | None = None
    # Space hints
    gap_down_pct: float | None = None
    high52w_pct: float | None = None
    # Chip hints
    daytrade_ratio: float | None = None
    short_cover_days: float | None = None
    # v2 qualitative labels for LLM output
    breakout_quality: str | None = None   # "乾淨" | "勉強" | "假突破風險"
    chip_quality: str | None = None       # "法人主導" | "主力集中" | "散戶跟風" | "資料不足"
    heat_level: str | None = None         # "低" | "中" | "高"
    setup_type: str | None = None         # "初升段" | "延續段" | "高檔追價"
    # DMI / BB hints
    adx: float | None = None
    plus_di: float | None = None
    minus_di: float | None = None
    bb_upper: float | None = None
    bb_lower: float | None = None
    bb_width_percentile: float | None = None


# ---------------------------------------------------------------------------
# Score breakdown
# ---------------------------------------------------------------------------

@dataclass
class _ScoreBreakdown:
    """Intermediate scoring state for transparency and testing.

    Extensibility: add new factor pts fields here and in `total` sum.
    Fields ending in _pts are scoring; `flags` is metadata.
    `_AnalysisHints` is a SEPARATE dataclass — never add hint fields here.
    """
    scoring_version: str = "v2"

    # --- Pillar 1: Momentum (max _PILLAR1_MAX = 39) ---
    volume_ratio_pts: int = 0         # 0/4/8
    price_direction_pts: int = 0      # 0/3
    close_strength_pts: int = 0       # 0/2/4
    vwap_advantage_pts: int = 0       # 0/6
    trend_continuity_pts: int = 0     # 0/3/5
    volume_escalation_pts: int = 0    # 0/3/5
    rsi_momentum_pts: int = 0         # 0/4 — RSI(14) 40–65
    dmi_initiation_pts: int = 0       # 0/2/4/6 — DMI: fresh cross/rising ADX → 6
    volume_dryup_pts: int = 0         # 0/4/8 — last 5d avg vs 20d avg (lower = better)
    volume_climax_pts: int = 0        # 0/4 — prior spike day + current dryup

    # --- Pillar 2A: Chip paid (max _PILLAR2_PAID_MAX = 40) ---
    breadth_pts: int = 0              # 0/5/10
    concentration_pts: int = 0        # 0/5/10
    continuity_pts: int = 0           # 0/3/5/8
    daytrade_filter_pts: int = 0      # 0/7
    foreign_broker_pts: int = 0       # 0/3/5

    # --- Pillar 2B: Chip free (max _PILLAR2_FREE_MAX = 40) ---
    foreign_strength_pts: int = 0         # 0/4/8/12
    trust_strength_pts: int = 0           # 0/3/6/8
    dealer_strength_pts: int = 0          # 0/2/4
    institution_continuity_pts: int = 0   # 0–8
    institution_consensus_pts: int = 0    # 0/4
    margin_structure_pts: int = 0         # -4 to +8
    margin_utilization_pts: int = 0       # -4/0/+4
    sbl_pressure_pts: int = 0             # 0/-4/-8

    # --- Pillar 3: Structure/Space (max _PILLAR3_MAX = 40) ---
    proximity_pts: int = 0            # 0/6/12 — close distance to 20d_high
    bb_compression_pts: int = 0       # 0/5/10 — BB width tightness
    ma_convergence_pts: int = 0       # 0/4/8 — MA5/MA10/MA20 convergence
    consolidation_weeks_pts: int = 0  # 0/3/6 — consecutive days in compression zone
    inside_bar_streak_pts: int = 0    # 0–5 — narrowing bar count
    prior_advance_pts: int = 0        # 0/2/5 — prior advance before consolidation
    ma_alignment_pts: int = 0         # 0/5
    ma20_slope_pts: int = 0           # 0/5
    relative_strength_pts: int = 0    # 0/3/5
    bb_squeeze_breakout_pts: int = 0  # 0/2/3/5 — BB_SQUEEZE_SETUP/BREAKOUT

    # --- Pillar 4: Accumulation Detection (max 13) ---
    emerging_setup_pts: int = 0       # 0/10
    pullback_setup_pts: int = 0       # 0/8
    bb_squeeze_coiling_pts: int = 0   # 0/3

    # --- Risk deductions (stored as non-negative values; subtracted in total) ---
    daytrade_risk: int = 0            # 0 or 25
    long_upper_shadow: int = 0        # 0 or 8
    overheat_ma20: int = 0            # 0 or 5
    overheat_ma60: int = 0            # 0 or 5
    daytrade_heat: int = 0            # 0 or 5
    sbl_breakout_fail: int = 0        # 0 or 8
    margin_chase_heat: int = 0        # 0 or 5
    adx_exhaustion_deduction: int = 0   # 0 or 6 — ADX > 55
    dmi_divergence_deduction: int = 0   # 0 or 4 — +DI falling while -DI rising
    vol_consecutive_surge: int = 0      # 0 or 5 — 3+ consecutive vol surge days (框架第3根不追)

    flags: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Clamped score sum. Explicit enumeration is intentional — see extensibility guide."""
        raw = (
            # Pillar 1
            self.volume_ratio_pts
            + self.price_direction_pts
            + self.close_strength_pts
            + self.vwap_advantage_pts
            + self.trend_continuity_pts
            + self.volume_escalation_pts
            + self.rsi_momentum_pts
            + self.dmi_initiation_pts
            + self.volume_dryup_pts
            + self.volume_climax_pts
            # Pillar 2A paid
            + self.breadth_pts
            + self.concentration_pts
            + self.continuity_pts
            + self.daytrade_filter_pts
            + self.foreign_broker_pts
            # Pillar 2B free
            + self.foreign_strength_pts
            + self.trust_strength_pts
            + self.dealer_strength_pts
            + self.institution_continuity_pts
            + self.institution_consensus_pts
            + self.margin_structure_pts      # can be negative
            + self.margin_utilization_pts    # can be negative
            + self.sbl_pressure_pts          # can be negative (0/-4/-8)
            # Pillar 3
            + self.proximity_pts
            + self.bb_compression_pts
            + self.ma_convergence_pts
            + self.consolidation_weeks_pts
            + self.inside_bar_streak_pts
            + self.prior_advance_pts
            + self.ma_alignment_pts
            + self.ma20_slope_pts
            + self.relative_strength_pts
            + self.bb_squeeze_breakout_pts
            # Pillar 4
            + self.emerging_setup_pts
            + self.pullback_setup_pts
            + self.bb_squeeze_coiling_pts
            # Risk deductions
            - self.daytrade_risk
            - self.long_upper_shadow
            - self.overheat_ma20
            - self.overheat_ma60
            - self.daytrade_heat
            - self.sbl_breakout_fail
            - self.margin_chase_heat
            - self.adx_exhaustion_deduction
            - self.dmi_divergence_deduction
            - self.vol_consecutive_surge
        )
        return max(0, min(100, raw))

    @property
    def chip_pts(self) -> int:
        """Total chip pillar points from whichever path was used (paid or free)."""
        return (
            # Paid
            self.breadth_pts
            + self.concentration_pts
            + self.continuity_pts
            + self.daytrade_filter_pts
            + self.foreign_broker_pts
            # Free
            + self.foreign_strength_pts
            + self.trust_strength_pts
            + self.dealer_strength_pts
            + self.institution_continuity_pts
            + self.institution_consensus_pts
            + self.margin_structure_pts
            + self.margin_utilization_pts
            + self.sbl_pressure_pts
        )

    @property
    def momentum_pts(self) -> int:
        """Total Pillar 1 points."""
        return (
            self.volume_ratio_pts
            + self.price_direction_pts
            + self.close_strength_pts
            + self.vwap_advantage_pts
            + self.trend_continuity_pts
            + self.volume_escalation_pts
            + self.rsi_momentum_pts
            + self.dmi_initiation_pts
            + self.volume_dryup_pts
            + self.volume_climax_pts
        )

    @property
    def structure_pts(self) -> int:
        """Total Pillar 3 points."""
        return (
            self.proximity_pts
            + self.bb_compression_pts
            + self.ma_convergence_pts
            + self.consolidation_weeks_pts
            + self.inside_bar_streak_pts
            + self.prior_advance_pts
            + self.ma_alignment_pts
            + self.ma20_slope_pts
            + self.relative_strength_pts
            + self.bb_squeeze_breakout_pts
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class TripleConfirmationEngine:
    """Compute the Triple Confirmation v2 confidence score.

    Args:
        free_tier_mode: Unused in v2 (threshold regime-adjusted by TAIEX MA20).
            Kept for backward compatibility with callers.

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
        self._taiex_history: list[DailyOHLCV] = []
        self._market: str = _DEFAULT_MARKET

    # ------------------------------------------------------------------
    # Public API — signatures unchanged from v1
    # ------------------------------------------------------------------

    def score(
        self,
        ohlcv: DailyOHLCV,
        ohlcv_history: list[DailyOHLCV],
        chip_report: ChipReport,
        volume_profile: VolumeProfile,
        twse_proxy: TWSEChipProxy | None = None,
        taiex_history: list[DailyOHLCV] | None = None,
        market: str = _DEFAULT_MARKET,
    ) -> SignalOutput:
        """Compute deterministic v2 confidence and return a SignalOutput."""
        self._taiex_history = taiex_history or []
        self._market = market if market in _LIQUIDITY_THRESHOLDS else _DEFAULT_MARKET
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
        market: str = _DEFAULT_MARKET,
    ) -> tuple[SignalOutput, _ScoreBreakdown]:
        """Same as score() but also returns the breakdown for LLM prompting."""
        self._taiex_history = taiex_history or []
        self._market = market if market in _LIQUIDITY_THRESHOLDS else _DEFAULT_MARKET
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
        market: str = _DEFAULT_MARKET,
    ) -> tuple[SignalOutput, _ScoreBreakdown, _AnalysisHints]:
        """Score + breakdown + analysis hints. Use this from StrategistAgent."""
        self._taiex_history = taiex_history or []
        self._market = market if market in _LIQUIDITY_THRESHOLDS else _DEFAULT_MARKET
        breakdown = self._compute(ohlcv, ohlcv_history, chip_report, volume_profile, twse_proxy)
        hints = self._compute_hints(ohlcv, ohlcv_history, twse_proxy=twse_proxy)
        signal = self._build_signal(ohlcv, breakdown, volume_profile, chip_report)
        return signal, breakdown, hints

    # ------------------------------------------------------------------
    # Gate layer
    # ------------------------------------------------------------------

    def _gate_check(
        self,
        ohlcv: DailyOHLCV,
        ohlcv_history: list[DailyOHLCV],
        volume_profile: VolumeProfile,
        twse_proxy: TWSEChipProxy | None = None,
    ) -> tuple[bool, int, int, list[str]]:
        """Evaluate the 4 hard gate conditions for pre-breakout detection.

        Returns (passes, conditions_available, conditions_met, detail_flags).
        Conditions:
        G1: Price Zone (85% <= close / 20d_high < 99%)
        G2: BB Compression (BB width <= 15%)
        G3: Liquidity (Turnover 20MA > Threshold)
        G4: Market Regime (TAIEX not downtrend)
        """
        required = 4  # G1–G4 all mandatory; G5 added when 60d data is present
        conditions_met = 0
        detail_flags: list[str] = []

        # G1: Price Zone
        if volume_profile.twenty_day_high > 0:
            ratio = ohlcv.close / volume_profile.twenty_day_high
            if ratio >= 0.99:
                detail_flags.append(f"GATE_FAIL:G1_ALREADY_BROKE_OUT:{ratio*100:.1f}%")
            elif ratio < 0.85:
                detail_flags.append(f"GATE_FAIL:G1_TOO_FAR_BELOW:{ratio*100:.1f}%")
            else:
                conditions_met += 1
                detail_flags.append(f"GATE_PASS:G1_ZONE:{ratio*100:.1f}%")
        else:
            detail_flags.append("GATE_SKIP:G1_NO_HIGH")

        # G2: BB Compression
        _, _, bb_w, _ = self._calculate_bb(ohlcv_history)
        if bb_w is not None:
            if bb_w <= 0.15:
                conditions_met += 1
                detail_flags.append(f"GATE_PASS:G2_BB:{bb_w*100:.1f}%")
            else:
                detail_flags.append(f"GATE_FAIL:G2_BB_WIDE:{bb_w*100:.1f}%")
        else:
            detail_flags.append("GATE_SKIP:G2_NO_BB")

        # G3: Liquidity
        turnover_20ma = self._turnover_20ma(ohlcv_history)
        l_threshold = _LIQUIDITY_THRESHOLDS.get(self._market, _LIQUIDITY_THRESHOLDS[_DEFAULT_MARKET])
        if turnover_20ma is not None:
            if turnover_20ma >= l_threshold:
                conditions_met += 1
                detail_flags.append(f"GATE_PASS:G3_LIQ:{turnover_20ma/1e6:.1f}M")
            else:
                detail_flags.append(f"GATE_FAIL:G3_LOW_LIQ:{turnover_20ma/1e6:.1f}M")
        else:
            detail_flags.append("GATE_SKIP:G3_NO_DATA")

        # G4: Market Regime
        regime = self._compute_taiex_regime(getattr(self, "_taiex_history", []))
        if regime != "downtrend":
            conditions_met += 1
            detail_flags.append(f"GATE_PASS:G4_REGIME:{regime}")
        else:
            detail_flags.append("GATE_FAIL:G4_REGIME:DOWNTREND")

        # G5: No significant overhead (requires 60d data).
        # Compares the consolidation ceiling (20d high) to the 60d high.
        # If 20d_high is far below 60d_high, sellers from 2 months ago sit overhead.
        if volume_profile.sixty_day_sessions >= 40 and volume_profile.sixty_day_high > 0:
            required += 1
            ratio_60d = volume_profile.twenty_day_high / volume_profile.sixty_day_high
            if ratio_60d >= 0.85:
                conditions_met += 1
                detail_flags.append(f"GATE_PASS:G5_NO_OVERHEAD:{ratio_60d*100:.1f}%")
            else:
                detail_flags.append(f"GATE_FAIL:G5_OVERHEAD:{ratio_60d*100:.1f}%")
        else:
            detail_flags.append("GATE_SKIP:G5_NO_60D_DATA")

        passes = conditions_met == required
        return passes, required, conditions_met, detail_flags

    # ------------------------------------------------------------------
    # Core computation
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

        # --- v2.3 Gate: Pre-breakout Hard Filters ---
        gate_passes, _, _, gate_detail_flags = self._gate_check(
            ohlcv, ohlcv_history, volume_profile, twse_proxy
        )
        bd.flags.extend(gate_detail_flags)

        if not gate_passes:
            bd.flags.append("NO_SETUP")
            return bd

        # --- v2.2b COILING Detector (now auxiliary to new gate) ---
        regime_for_coiling = self._compute_taiex_regime(getattr(self, "_taiex_history", []))
        coiling_score, coiling_flags = self._coiling_detect(
            ohlcv, ohlcv_history, volume_profile, twse_proxy, regime_for_coiling
        )
        bd.flags.extend(coiling_flags)
        if coiling_score >= 4:
            bd.flags.append("COILING_PRIME")
        elif coiling_score >= 3:
            bd.flags.append("COILING")

        # --- Pillar 1: Momentum ---
        vol_pts, vol_flag = self._volume_ratio_score(ohlcv, ohlcv_history)
        bd.volume_ratio_pts = vol_pts
        if vol_flag:
            bd.flags.append(vol_flag)
        bd.price_direction_pts = self._price_direction_score(ohlcv, ohlcv_history)

        cs_pts, cs_flag = self._close_strength_score(ohlcv)
        bd.close_strength_pts = cs_pts
        if cs_flag:
            bd.flags.append(cs_flag)

        vwap_pts, vwap_flag = self._vwap_advantage_score(ohlcv, ohlcv_history)
        bd.vwap_advantage_pts = vwap_pts
        if vwap_flag:
            bd.flags.append(vwap_flag)

        bd.trend_continuity_pts = self._trend_continuity_score(ohlcv, ohlcv_history)
        bd.volume_escalation_pts = self._volume_escalation_score(ohlcv, ohlcv_history)
        bd.rsi_momentum_pts = self._rsi_momentum_score(ohlcv_history)
        bd.volume_dryup_pts = self._volume_dryup_score(ohlcv_history)
        bd.volume_climax_pts = self._volume_climax_score(ohlcv_history)

        # Pre-compute DMI once — shared by initiation score + risk deductions
        sorted_hist = sorted(ohlcv_history, key=lambda x: x.trade_date)
        dmi_now = self._calculate_dmi(sorted_hist)
        dmi_5d_ago = (
            self._calculate_dmi(sorted_hist[:-5])
            if len(sorted_hist) >= 34
            else (None, None, None)
        )

        dmi_pts, dmi_flag = self._dmi_initiation_score_cached(dmi_now, dmi_5d_ago)
        bd.dmi_initiation_pts = dmi_pts
        if dmi_flag:
            bd.flags.append(dmi_flag)

        # --- Pillar 2: Chip (paid vs free-tier, mutually exclusive) ---
        if chip_report.net_buyer_count_diff != 0 or chip_report.active_branch_count > 0:
            # Paid chip data available — use FinMind factors
            self._apply_paid_chip(bd, chip_report)
        elif twse_proxy is not None and twse_proxy.is_available:
            # Free-tier fallback — TWSE opendata proxies
            self._apply_free_chip(bd, twse_proxy)
        else:
            bd.flags.append("NO_CHIP_DATA")

        # --- Pillar 3: Compression Structure ---
        bd.proximity_pts = self._proximity_score(ohlcv.close, volume_profile.twenty_day_high)
        bd.bb_compression_pts = self._bb_compression_score(ohlcv_history)
        bd.ma_convergence_pts = self._ma_convergence_score(ohlcv_history)
        bd.consolidation_weeks_pts = self._consolidation_weeks_score(ohlcv_history)
        bd.inside_bar_streak_pts = self._inside_bar_streak_score(ohlcv_history)
        bd.prior_advance_pts = self._prior_advance_score(ohlcv_history)

        bb_sq_pts, bb_sq_flag = self._bb_squeeze_breakout_score(ohlcv, ohlcv_history)
        bd.bb_squeeze_breakout_pts = bb_sq_pts
        if bb_sq_flag:
            bd.flags.append(bb_sq_flag)

        ma_align_pts, ma_align_flag = self._ma_alignment_score(ohlcv_history)
        bd.ma_alignment_pts = ma_align_pts
        if ma_align_flag:
            bd.flags.append(ma_align_flag)

        slope_pts, slope_flag = self._ma20_slope_score(ohlcv_history)
        bd.ma20_slope_pts = slope_pts
        if slope_flag:
            bd.flags.append(slope_flag)

        taiex = getattr(self, "_taiex_history", [])
        if taiex:
            rs_pts, rs_flag = self._relative_strength_score(ohlcv, ohlcv_history, taiex)
            bd.relative_strength_pts = rs_pts
            if rs_flag:
                bd.flags.append(rs_flag)

        # --- Pillar 4: Accumulation Detection ---
        self._accumulation_score(bd, ohlcv, ohlcv_history, volume_profile, twse_proxy)

        # --- Risk deductions ---
        self._apply_risk_deductions(
            bd, ohlcv, ohlcv_history, volume_profile, twse_proxy,
            dmi_now=dmi_now, dmi_5d_ago=dmi_5d_ago,
        )

        logger.debug(
            "v2 score breakdown for %s: "
            "p1=%d+%d+%d+%d+%d+%d+%d+%d+%d "
            "p2_paid=%d+%d+%d+%d+%d "
            "p2_free=%d+%d+%d+%d+%d+%d+%d+%d "
            "p3=%d+%d+%d+%d+%d+%d+%d+%d+%d "
            "p4=%d+%d+%d "
            "risk=-%d-%d-%d-%d-%d-%d-%d-%d-%d "
            "flags=%s → total=%d",
            ohlcv.ticker,
            bd.price_direction_pts, bd.close_strength_pts,
            bd.vwap_advantage_pts, bd.trend_continuity_pts, bd.volume_escalation_pts,
            bd.rsi_momentum_pts, bd.dmi_initiation_pts, bd.volume_dryup_pts,
            bd.volume_climax_pts,
            bd.breadth_pts, bd.concentration_pts, bd.continuity_pts,
            bd.daytrade_filter_pts, bd.foreign_broker_pts,
            bd.foreign_strength_pts, bd.trust_strength_pts, bd.dealer_strength_pts,
            bd.institution_continuity_pts, bd.institution_consensus_pts,
            bd.margin_structure_pts, bd.margin_utilization_pts, bd.sbl_pressure_pts,
            bd.proximity_pts, bd.bb_compression_pts, bd.ma_convergence_pts,
            bd.consolidation_weeks_pts, bd.inside_bar_streak_pts, bd.prior_advance_pts,
            bd.ma_alignment_pts, bd.ma20_slope_pts, bd.relative_strength_pts,
            bd.emerging_setup_pts, bd.pullback_setup_pts, bd.bb_squeeze_coiling_pts,
            bd.daytrade_risk, bd.long_upper_shadow, bd.overheat_ma20, bd.overheat_ma60,
            bd.daytrade_heat, bd.sbl_breakout_fail, bd.margin_chase_heat,
            bd.adx_exhaustion_deduction, bd.dmi_divergence_deduction,
            bd.flags, bd.total,
        )
        return bd

    # ------------------------------------------------------------------
    # Pillar 1: Momentum scoring methods
    # ------------------------------------------------------------------

    def _volume_ratio_score(self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]) -> tuple[int, str | None]:
        """Volume ratio vs 20d avg.
        <1.2 → 0, 1.2-2.0 → 4, 2.0-3.0 → 8 (最佳爆量區間),
        ≥3.0 → 5 + VOL_EXHAUSTION_RISK (極端量：警戒噴出型).
        """
        vol_20ma = self._volume_20ma(history)
        if vol_20ma is None or vol_20ma == 0:
            return 0, None
        ratio = ohlcv.volume / vol_20ma
        if ratio >= 3.0:
            return 5, "VOL_EXHAUSTION_RISK"
        if ratio >= 2.0:
            return 8, None
        if ratio >= 1.2:
            return 4, None
        return 0, None

    def _price_direction_score(self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]) -> int:
        """Price direction: close >= prev_close → +3."""
        prev_day = [d for d in history if d.trade_date < ohlcv.trade_date]
        if not prev_day:
            return 0
        prev_close = max(prev_day, key=lambda x: x.trade_date).close
        return 3 if ohlcv.close >= prev_close else 0

    def _close_strength_score(self, ohlcv: DailyOHLCV) -> tuple[int, str | None]:
        """K線收盤強弱比: (close-low)/(high-low).
        ≥0.8 → +4 (買盤全日主導), 0.6-0.8 → +2 (健康收盤), 0.4-0.6 → 0 (觀察),
        <0.4 → -2 (出貨型：開高走低).
        Guard: high==low → 0, flag DOJI_OR_HALT.
        """
        bar_range = ohlcv.high - ohlcv.low
        if bar_range <= 0:
            return 0, "DOJI_OR_HALT"
        ratio = (ohlcv.close - ohlcv.low) / bar_range
        if ratio >= 0.8:
            return 4, None
        if ratio >= 0.6:
            return 2, None
        if ratio >= 0.4:
            return 0, None
        return -2, "CLOSE_WEAK_OUT_PATTERN"

    def _close_strength_ratio(self, ohlcv: DailyOHLCV) -> float | None:
        """Return (close-low)/(high-low) or None when high==low."""
        bar_range = ohlcv.high - ohlcv.low
        if bar_range <= 0:
            return None
        return (ohlcv.close - ohlcv.low) / bar_range

    @staticmethod
    def _volume_dryup_score(history: list[DailyOHLCV]) -> int:
        """Reward volume drying up. Max 8 pts."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_h) < 20:
            return 0
        vols = [d.volume for d in sorted_h]
        avg_20d = sum(vols[-20:]) / 20
        if avg_20d <= 0:
            return 0
        avg_5d = sum(vols[-5:]) / 5
        ratio = avg_5d / avg_20d
        if ratio < 0.60:
            return 8
        if ratio < 0.80:
            return 4
        return 0

    @staticmethod
    def _volume_climax_score(history: list[DailyOHLCV]) -> int:
        """Prior spike day + current dryup. Max 4 pts."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_h) < 20:
            return 0
        vols = [d.volume for d in sorted_h]
        avg_20d = sum(vols[-20:]) / 20
        if avg_20d <= 0:
            return 0
        has_prior_climax = any(v > avg_20d * 2.0 for v in vols[-20:-5])
        avg_5d = sum(vols[-5:]) / 5
        has_current_dryup = (avg_5d / avg_20d) < 0.80
        return 4 if (has_prior_climax and has_current_dryup) else 0

    def _vwap_advantage_score(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> tuple[int, str | None]:
        """VWAP advantage: close > 5d_avg_vwap → +6.
        Intraday VWAP unavailable on T+1 daily data so only 5d tier is used.
        """
        vwap_5d = self._vwap_5d(history)
        if vwap_5d is None:
            return 0, "INSUFFICIENT_HISTORY_VWAP5D"
        return (6, None) if ohlcv.close > vwap_5d else (0, None)

    def _trend_continuity_score(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> int:
        """Trend continuity: 3 consec up → +3; 4-of-last-5 bars up → +5 (takes precedence)."""
        all_bars = sorted(history, key=lambda x: x.trade_date) + [ohlcv]
        if len(all_bars) < 3:
            return 0

        # Count consecutive up days from the end
        consec = 0
        for i in range(len(all_bars) - 1, 0, -1):
            if all_bars[i].close > all_bars[i - 1].close:
                consec += 1
            else:
                break

        if len(all_bars) >= 5:
            # Count up bars in last 5 (excluding today as that's in all_bars[-1])
            last5 = all_bars[-5:]
            up_count = sum(
                1 for i in range(1, len(last5))
                if last5[i].close > last5[i - 1].close
            )
            if up_count >= 4:
                return 5

        if consec >= 3:
            return 3
        return 0

    def _rsi_momentum_score(self, history: list[DailyOHLCV]) -> int:
        """RSI(14) momentum zone: 40 ≤ RSI ≤ 65 → +4.

        Rationale: this range indicates healthy recovery momentum — stock has been
        forming a base or rebounding, but has not yet entered overbought territory.
        """
        recent = sorted(history, key=lambda x: x.trade_date)
        if len(recent) < 16:
            return 0
        closes = pd.Series([d.close for d in recent])
        rsi = self._rsi(closes, period=14)
        if rsi is None:
            return 0
        return 4 if 40.0 <= rsi <= 65.0 else 0

    def _volume_escalation_score(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> int:
        """Volume escalation: T-3 < T-2 < T-1 → +3; + today > T-1 → +5."""
        sorted_history = sorted(history, key=lambda x: x.trade_date)
        # Need at least 4 sessions before today (T-3, T-2, T-1, and today context)
        prev_days = [d for d in sorted_history if d.trade_date < ohlcv.trade_date]
        if len(prev_days) < 3:
            return 0
        t1 = prev_days[-1].volume  # yesterday
        t2 = prev_days[-2].volume  # 2 days ago
        t3 = prev_days[-3].volume  # 3 days ago
        if t3 < t2 < t1:
            if ohlcv.volume > t1:
                return 5
            return 3
        return 0

    # ------------------------------------------------------------------
    # Pillar 2A: Paid chip scoring
    # ------------------------------------------------------------------

    def _apply_paid_chip(self, bd: _ScoreBreakdown, chip_report: ChipReport) -> None:
        """Apply FinMind paid chip scoring to breakdown (in-place)."""
        # 1. Breadth: net_buyer_count_diff tiers
        diff = chip_report.net_buyer_count_diff
        if diff > 10:
            bd.breadth_pts = 10
        elif diff >= 1:
            bd.breadth_pts = 5
        else:
            bd.breadth_pts = 0

        # 2. Concentration quality (with thin-market cap)
        if chip_report.active_branch_count >= 10:
            conc = chip_report.concentration_top15
            if conc > 0.35:
                bd.concentration_pts = 10
            elif conc >= 0.25:
                bd.concentration_pts = 5
            else:
                bd.concentration_pts = 0
        elif chip_report.active_branch_count > 0:
            # Thin market: cap at +5 if concentration is strong
            if chip_report.concentration_top15 > 0.35:
                bd.concentration_pts = 5
            bd.flags.append(
                f"THIN_MARKET: only {chip_report.active_branch_count} active branches "
                "— concentration capped at 5"
            )
        else:
            bd.flags.append("THIN_MARKET: no active branches")

        # 3. Continuity: top-5 buyer overlap with prior days
        bd.continuity_pts = self._compute_continuity_pts(chip_report)

        # 4. 隔日沖 filter
        top3 = chip_report.top_buyers[:3]
        daytrade_in_top3 = any(b.label == "隔日沖" for b in top3)
        if not daytrade_in_top3:
            bd.daytrade_filter_pts = 7
        else:
            bd.daytrade_risk = 25
            top3_names = [b.branch_name for b in top3 if b.label == "隔日沖"]
            bd.flags.append(f"隔日沖_TOP3: {', '.join(top3_names)}")
            chip_report.risk_flags.append("隔日沖_TOP3")

        # 5. Known FII branch detection
        top_buyers = chip_report.top_buyers
        fii_in_top3 = any(
            b.branch_code in _KNOWN_FII_BRANCH_CODES for b in top3
        )
        fii_any = any(b.branch_code in _KNOWN_FII_BRANCH_CODES for b in top_buyers)
        if fii_any:
            fii_names = [
                _KNOWN_FII_BRANCH_CODES[b.branch_code]
                for b in top_buyers
                if b.branch_code in _KNOWN_FII_BRANCH_CODES
            ]
            bd.flags.append(f"FII_PRESENT: {', '.join(fii_names)}")
            if fii_in_top3 and chip_report.concentration_top15 > 0.35:
                bd.foreign_broker_pts = 5
            else:
                bd.foreign_broker_pts = 3

    def _compute_continuity_pts(self, chip_report: ChipReport) -> int:
        """Main force continuity: top-5 buyer overlap with previous days.

        Uses chip_report.historical_top5_buyers (index 0 = yesterday, etc.)
        Returns 0/3/5/8.
        """
        if not chip_report.historical_top5_buyers:
            return 0

        today_codes = {b.branch_code for b in chip_report.top_buyers[:5]}

        # Yesterday overlap
        yesterday_top5 = chip_report.historical_top5_buyers[0]
        yesterday_codes = {b.branch_code for b in yesterday_top5[:5]}
        yesterday_overlap = len(today_codes & yesterday_codes)

        if yesterday_overlap == 0:
            base = 0
        elif yesterday_overlap == 1:
            base = 3
        else:  # >= 2
            base = 5

        # 3-day average overlap bonus
        if len(chip_report.historical_top5_buyers) >= 3:
            overlaps = []
            for day_list in chip_report.historical_top5_buyers[:3]:
                prior_codes = {b.branch_code for b in day_list[:5]}
                overlaps.append(len(today_codes & prior_codes))
            avg_overlap = sum(overlaps) / len(overlaps)
            if avg_overlap >= 2.0:
                base = min(8, base + 3)

        return base

    # ------------------------------------------------------------------
    # Pillar 2B: Free-tier chip scoring
    # ------------------------------------------------------------------

    def _apply_free_chip(self, bd: _ScoreBreakdown, proxy: TWSEChipProxy) -> None:
        """Apply TWSE free-tier chip scoring to breakdown (in-place)."""
        avg_vol = proxy.avg_20d_volume

        # 1. Foreign buy strength (ratio-based)
        bd.foreign_strength_pts = self._institution_strength_pts(
            proxy.foreign_net_buy, avg_vol, tiers=(0.0, 0.03, 0.08), points=(0, 4, 8, 12)
        )

        # 2. Trust buy strength
        bd.trust_strength_pts = self._institution_strength_pts(
            proxy.trust_net_buy, avg_vol, tiers=(0.0, 0.03, 0.08), points=(0, 3, 6, 8)
        )

        # 3. Dealer buy strength
        bd.dealer_strength_pts = self._institution_strength_pts(
            proxy.dealer_net_buy, avg_vol, tiers=(0.0, 0.03), points=(0, 2, 4)
        )

        # 4. Institution continuity
        consec_pts = 0
        if proxy.foreign_consecutive_buy_days >= 3:
            consec_pts += 4
        if proxy.trust_consecutive_buy_days >= 3:
            consec_pts += 3
        if proxy.dealer_consecutive_buy_days >= 3:
            consec_pts += 1
        bd.institution_continuity_pts = consec_pts

        # 5. Three-institution consensus
        # All three net buy, and at least two at medium+ strength
        foreign_medium = bd.foreign_strength_pts >= 4
        trust_medium = bd.trust_strength_pts >= 3
        dealer_medium = bd.dealer_strength_pts >= 2
        all_net_buy = (
            proxy.foreign_net_buy > 0
            and proxy.trust_net_buy > 0
            and proxy.dealer_net_buy > 0
        )
        medium_count = sum([foreign_medium, trust_medium, dealer_medium])
        if all_net_buy and medium_count >= 2:
            bd.institution_consensus_pts = 4

        # 6. Margin structure (price direction × margin change)
        bd.margin_structure_pts = self._margin_structure_pts(proxy)

        # 7. Margin utilization
        if proxy.margin_utilization_rate is not None:
            if proxy.margin_utilization_rate < 0.20:
                bd.margin_utilization_pts = 4
            elif proxy.margin_utilization_rate > 0.80:
                bd.margin_utilization_pts = -4
                bd.flags.append(f"MARGIN_HIGH_UTIL: {proxy.margin_utilization_rate:.1%}")

        # 8. SBL pressure
        if proxy.sbl_available:
            if proxy.sbl_ratio > 0.10:
                bd.sbl_pressure_pts = -8
                bd.flags.append(f"SBL_HEAVY: {proxy.sbl_ratio:.1%}")
            elif proxy.sbl_ratio > 0.05:
                bd.sbl_pressure_pts = -4
                bd.flags.append(f"SBL_MODERATE: {proxy.sbl_ratio:.1%}")

        for flag in proxy.data_quality_flags:
            bd.flags.append(f"TWSE:{flag}")

    @staticmethod
    def _institution_strength_pts(
        net_buy: int,
        avg_20d_vol: int,
        tiers: tuple,
        points: tuple,
    ) -> int:
        """Compute ratio-based institution strength points.

        tiers: (lower_bound_1, lower_bound_2, ...) — ratios above which to award each tier
        points: (pts_at_zero_or_below, pts_tier1, pts_tier2, ...)
        """
        if net_buy <= 0:
            return 0
        if avg_20d_vol <= 0:
            # No volume reference — binary: bought → lowest positive tier
            return points[1] if len(points) > 1 else 0
        ratio = net_buy / avg_20d_vol
        # Walk tiers from highest to lowest
        for i in range(len(tiers) - 1, -1, -1):
            if ratio > tiers[i]:
                return points[i + 1]
        return points[0]

    def _margin_structure_pts(self, proxy: TWSEChipProxy) -> int:
        """融資結構 scoring: price direction × margin change.

        Uses margin_balance_change sign as margin direction proxy.
        '大增' = >5% single-day increase; we approximate from margin_balance_change sign
        and the proxy field `short_balance_increased` (reused semantically).

        v2 definition:
        - 股價漲 + 融資減/持平 → +8
        - 股價漲 + 融資小增 → +3
        - 股價漲 + 融資大增 → -4
        - 股價跌 + 融資大減 → +2
        - 股價跌 + 融資不減 → -3
        """
        price_up = proxy.foreign_net_buy >= 0  # fallback: use proxy attribute
        # We don't have prev_close in TWSEChipProxy directly; use margin_balance_change
        # sign-only approach with magnitude classification:
        # large = abs(change) > 5% of balance approximated by short_balance_increased flag
        # small = change > 0 but not large
        margin_up = proxy.margin_balance_change > 0
        margin_down = proxy.margin_balance_change < 0
        # margin_large_change: we reuse short_balance_increased as the "large" signal
        # (caller is responsible for populating this correctly)
        margin_large = proxy.short_balance_increased

        # Determine "stock price direction" from proxy: if foreign is net buy → up, else down
        # This is an approximation — callers should ensure margin_balance_change reflects
        # today's margin change and short_balance_increased reflects a large margin increase
        if margin_up:
            if margin_large:
                return -4  # 融資大增
            return 3       # 融資小增
        elif margin_down:
            if margin_large:
                # short_balance_increased here reused as "large decrease" signal
                # (caller sets to True for large magnitude regardless of direction)
                return 2   # 融資大減 (washout — positive)
            return 8       # 融資減/持平 → best case
        else:
            # margin_balance_change == 0 → 持平
            return 8

    # ------------------------------------------------------------------
    # Pillar 3: Compression Structure scoring methods
    # ------------------------------------------------------------------

    @staticmethod
    def _atr_20(history: list[DailyOHLCV]) -> float | None:
        """Simple 20-bar ATR using true range (no Wilder smoothing)."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_h) < 21:
            return None
        trs = []
        for i in range(len(sorted_h) - 20, len(sorted_h)):
            bar = sorted_h[i]
            prev_close = sorted_h[i - 1].close
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
            trs.append(tr)
        return sum(trs) / len(trs) if trs else None

    @staticmethod
    def _proximity_score(close: float, twenty_day_high: float) -> int:
        """Reward stocks just below 20d resistance. Max 12 pts."""
        if twenty_day_high <= 0:
            return 0
        ratio = close / twenty_day_high
        if 0.92 <= ratio < 0.99:
            return 12
        if 0.88 <= ratio < 0.92:
            return 6
        return 0

    @staticmethod
    def _bb_compression_score(history: list[DailyOHLCV]) -> int:
        """Reward tight BB bands. Max 10 pts."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        _, _, bb_width_raw, _ = TripleConfirmationEngine._calculate_bb(sorted_h)
        if bb_width_raw is None:
            return 0
        if bb_width_raw < 0.08:
            return 10
        if bb_width_raw < 0.12:
            return 5
        return 0

    @staticmethod
    def _ma_convergence_score(history: list[DailyOHLCV]) -> int:
        """MA5/MA10/MA20 converging. Max 8 pts."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        if len(closes) < 20:
            return 0
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        if ma20 == 0:
            return 0
        spread = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / ma20
        if spread < 0.02:
            return 8
        if spread < 0.05:
            return 4
        return 0

    def _consolidation_weeks_score(self, history: list[DailyOHLCV]) -> int:
        """Count consecutive days of compression (BB<12% AND range<1.5xATR). Max 6 pts."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_h) < 21:
            return 0
        atr = self._atr_20(sorted_h)
        if atr is None or atr <= 0:
            return 0
        count = 0
        for i in range(len(sorted_h) - 1, max(len(sorted_h) - 61, 20), -1):
            window = sorted_h[max(0, i - 19) : i + 1]
            _, _, bb_w, _ = self._calculate_bb(window)
            if bb_w is None or bb_w >= 0.12:
                break
            bar = sorted_h[i]
            prev_close = sorted_h[i - 1].close
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
            if tr >= atr * 1.5:
                break
            count += 1
        weeks = count / 5
        if weeks >= 4:
            return 6
        if weeks >= 2:
            return 3
        return 0

    @staticmethod
    def _inside_bar_streak_score(history: list[DailyOHLCV]) -> int:
        """Count consecutive inside bars. Max 5 pts."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_h) < 2:
            return 0
        streak = 0
        for i in range(len(sorted_h) - 1, 0, -1):
            bar = sorted_h[i]
            prev = sorted_h[i - 1]
            if bar.high <= prev.high and bar.low >= prev.low:
                streak += 1
            else:
                break
        return min(streak, 5)

    @staticmethod
    def _prior_advance_score(history: list[DailyOHLCV]) -> int:
        """Prior advance >= 20% in 60 bars before current consolidation. Max 5 pts."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_h) < 120:
            return 0
        prior_window = sorted_h[-120:-60]
        base_close = prior_window[0].close
        if base_close <= 0:
            return 0
        peak_close = max(d.close for d in prior_window)
        advance = (peak_close - base_close) / base_close
        if advance >= 0.20:
            return 5
        if advance >= 0.10:
            return 2
        return 0

    def _ma_alignment_score(
        self, history: list[DailyOHLCV]
    ) -> tuple[int, str | None]:
        """均線多頭排列: MA5 > MA10 > MA20 → +5 pts (≥20 sessions required)."""
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

    def _ma20_slope_score(self, history: list[DailyOHLCV]) -> tuple[int, str | None]:
        """MA20 slope: +5 pts if MA20 is rising vs 5 sessions ago."""
        slope = self._ma20_slope(history)
        if slope is None:
            return 0, "INSUFFICIENT_HISTORY_MA20_SLOPE"
        return (5, None) if slope > 0 else (0, None)

    def _relative_strength_score(
        self,
        ohlcv: DailyOHLCV,
        history: list[DailyOHLCV],
        taiex_history: list[DailyOHLCV],
    ) -> tuple[int, str | None]:
        """RS vs 大盤: 0–20% outperform → +3, >20% → +5."""
        stock_bars = sorted(history, key=lambda x: x.trade_date)
        taiex_bars = sorted(taiex_history, key=lambda x: x.trade_date)
        if len(stock_bars) < 5 or len(taiex_bars) < 5:
            return 0, "INSUFFICIENT_HISTORY_RS"
        stock_base = stock_bars[-5].close
        taiex_base = taiex_bars[-5].close
        if stock_base <= 0 or taiex_base <= 0:
            return 0, "RS_SCORE_ZERO_BASE"
        stock_ret = (ohlcv.close - stock_base) / stock_base
        taiex_ret = (taiex_bars[-1].close - taiex_base) / taiex_base
        outperform = stock_ret - taiex_ret
        if outperform > 0.20:
            return 5, None
        if outperform > 0:
            return 3, None
        return 0, None

    def _dmi_initiation_score(
        self, history: list[DailyOHLCV]
    ) -> tuple[int, str | None]:
        """Legacy entry point — computes DMI from scratch."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        dmi_now = self._calculate_dmi(sorted_h)
        dmi_5d = (
            self._calculate_dmi(sorted_h[:-5])
            if len(sorted_h) >= 34
            else (None, None, None)
        )
        return self._dmi_initiation_score_cached(dmi_now, dmi_5d)

    @staticmethod
    def _dmi_initiation_score_cached(
        dmi_now: tuple[float | None, float | None, float | None],
        dmi_5d_ago: tuple[float | None, float | None, float | None],
    ) -> tuple[int, str | None]:
        """Score DMI trend initiation from pre-computed DMI values.

        Scoring:
          +DI <= -DI OR ADX < 20       → 0
          ADX >= 20 + fresh DI cross    → 6 (DMI_FRESH_CROSS)
          ADX >= 20 + ADX rising        → 6 (DMI_TREND_INIT)
          ADX 20-55 + stale cross       → 4 (DMI_TREND_CONT)
          ADX > 55 (near exhaustion)    → 2 (DMI_TREND_CONT)
        """
        plus_di, minus_di, adx = dmi_now
        if plus_di is None or minus_di is None or adx is None:
            return 0, None
        if plus_di <= minus_di:
            return 0, None
        if adx < 20:
            return 0, None

        # ADX > 55: trend likely near exhaustion (also gets -6 risk deduction)
        if adx > 55:
            return 2, "DMI_TREND_CONT"

        plus_di_5d, minus_di_5d, adx_5d = dmi_5d_ago

        # Fresh crossover: 5 days ago +DI was NOT above -DI → cross within 5d
        if (
            plus_di_5d is not None
            and minus_di_5d is not None
            and plus_di_5d <= minus_di_5d
        ):
            return 6, "DMI_FRESH_CROSS"

        # ADX rising = trend strengthening
        if adx_5d is not None and adx > adx_5d:
            return 6, "DMI_TREND_INIT"

        return 4, "DMI_TREND_CONT"

    def _bb_squeeze_breakout_score(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> tuple[int, str | None]:
        bb_upper, bb_lower, bb_width, bb_width_pct = self._calculate_bb(history)
        if bb_upper is None or bb_width_pct is None:
            return 0, None
        if bb_width_pct >= 20:
            return 0, None
        if ohlcv.close <= bb_upper:
            return 2, "BB_SQUEEZE_SETUP"
        vol_20ma = self._volume_20ma(history)
        if vol_20ma is not None and vol_20ma > 0 and ohlcv.volume > vol_20ma * 1.5:
            return 5, "BB_SQUEEZE_BREAKOUT"
        return 3, "BB_SQUEEZE_BREAKOUT"

    def _accumulation_score(
        self,
        bd: _ScoreBreakdown,
        ohlcv: DailyOHLCV,
        history: list[DailyOHLCV],
        volume_profile: VolumeProfile,
        twse_proxy: TWSEChipProxy | None,
    ) -> None:
        """Compute Pillar 4: Accumulation Detection scoring (in-place)."""
        # --- 4a. EMERGING_SETUP (+10 pts) ---
        # MA aligned + MA20 rising + institutional buy + NOT yet broken out
        ma_aligned = bd.ma_alignment_pts > 0
        ma20_rising = bd.ma20_slope_pts > 0
        has_inst_buy = False
        if twse_proxy is not None and twse_proxy.is_available:
            has_inst_buy = (twse_proxy.foreign_net_buy > 0 or twse_proxy.trust_net_buy > 0)

        no_breakout_yet = (ohlcv.close < volume_profile.twenty_day_high * 0.99)

        if ma_aligned and ma20_rising and has_inst_buy and no_breakout_yet:
            bd.emerging_setup_pts = 10
            bd.flags.append("EMERGING_SETUP")

        # --- 4b. PULLBACK_SETUP (+8 pts) ---
        # Had breakout in last 20d + near MA20 + MA20 rising + volume contraction
        # Mutually exclusive with EMERGING_SETUP in practice (one requires breakout, one forbids)
        if bd.emerging_setup_pts == 0:
            # recent = sorted(history, key=lambda x: x.trade_date)
            # if len(recent) >= 20: # handled by slope and alignment requirements
            if ma20_rising:
                # Approximate MA20 from history
                closes = pd.Series([d.close for d in history])
                if len(closes) >= 20:
                    ma20 = closes.rolling(20).mean().iloc[-1]
                    if not pd.isna(ma20) and ma20 > 0:
                        near_ma20 = (ma20 * 0.97 <= ohlcv.close <= ma20 * 1.03)

                        # Volume contraction: last 3 days volume < 20d avg * 0.8
                        vol_20ma = self._volume_20ma(history)
                        if vol_20ma is not None and vol_20ma > 0:
                            # Average volume of yesterday, day before, and today
                            recent_h = sorted(history, key=lambda x: x.trade_date)
                            last3_vol_avg = (recent_h[-1].volume + recent_h[-2].volume + ohlcv.volume) / 3
                            vol_contracted = (last3_vol_avg < vol_20ma * 0.8)

                            if near_ma20 and vol_contracted:
                                # Verify if we had a breakout recently
                                # If twenty_day_high is significantly above current price,
                                # it implies we pulled back from a recent high.
                                if volume_profile.twenty_day_high > ohlcv.close * 1.02:
                                    bd.pullback_setup_pts = 8
                                    bd.flags.append("PULLBACK_SETUP")

        # --- 4c. BB_SQUEEZE_COILING bonus (+3 pts) ---
        # BB Squeeze + extreme volume contraction
        if "BB_SQUEEZE_SETUP" in bd.flags:
            vol_20ma = self._volume_20ma(history)
            if vol_20ma is not None and vol_20ma > 0:
                recent_h = sorted(history, key=lambda x: x.trade_date)
                last3_vol_avg = (recent_h[-1].volume + recent_h[-2].volume + ohlcv.volume) / 3
                if last3_vol_avg < vol_20ma * 0.7:
                    bd.bb_squeeze_coiling_pts = 3
                    bd.flags.append("BB_SQUEEZE_COILING")

    # ------------------------------------------------------------------
    # Risk deductions
    # ------------------------------------------------------------------

    def _vol_consecutive_surge_count(self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]) -> int:
        """Count consecutive bars (including today) with vol > 1.5× 20d avg."""
        vol_20ma = self._volume_20ma(history)
        if not vol_20ma:
            return 0
        threshold = vol_20ma * 1.5
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        all_bars = sorted_h + [ohlcv]
        count = 0
        for bar in reversed(all_bars):
            if bar.volume >= threshold:
                count += 1
            else:
                break
        return count

    def _apply_risk_deductions(
        self,
        bd: _ScoreBreakdown,
        ohlcv: DailyOHLCV,
        history: list[DailyOHLCV],
        volume_profile: VolumeProfile,
        twse_proxy: TWSEChipProxy | None,
        *,
        dmi_now: tuple[float | None, float | None, float | None] | None = None,
        dmi_5d_ago: tuple[float | None, float | None, float | None] | None = None,
    ) -> None:
        """Compute and apply all risk deductions to breakdown (in-place)."""
        vol_20ma = self._volume_20ma(history)
        cs_ratio = self._close_strength_ratio(ohlcv)

        # 1. 長上影放量: vol > 1.5×avg AND close_strength < 0.4
        if vol_20ma is not None and vol_20ma > 0:
            if ohlcv.volume > vol_20ma * 1.5:
                if cs_ratio is not None and cs_ratio < 0.4:
                    bd.long_upper_shadow = 8
                    bd.flags.append("LONG_UPPER_SHADOW")

        # 2. 過熱乖離 (v2 historical: data shows these are positive trend signals, removing deductions)
        # recent = sorted(history, key=lambda x: x.trade_date)
        # if len(recent) >= 20:
        #     closes = pd.Series([d.close for d in recent])
        #     ma20 = closes.rolling(20).mean().iloc[-1]
        #     if not pd.isna(ma20) and ma20 > 0:
        #         if ohlcv.close > ma20 * 1.10:
        #             bd.overheat_ma20 = 0  # removed -5
        #     if len(recent) >= 60:
        #         ma60 = closes.rolling(60).mean().iloc[-1]
        #         if not pd.isna(ma60) and ma60 > 0:
        #             if ohlcv.close > ma60 * 1.20:
        #                 bd.overheat_ma60 = 0  # removed -5

        # 3. 當沖過熱: daytrade_ratio > 35% AND not above 20d high
        if twse_proxy is not None and twse_proxy.daytrade_ratio is not None:
            above_20d = (
                volume_profile.twenty_day_high > 0
                and ohlcv.close >= volume_profile.twenty_day_high * 0.99
            )
            if twse_proxy.daytrade_ratio > 0.35 and not above_20d:
                bd.daytrade_heat = 5
                bd.flags.append(f"DAYTRADE_HEAT: {twse_proxy.daytrade_ratio:.1%}")

        # 4. 借券放空 + 突破失敗
        if twse_proxy is not None and twse_proxy.sbl_available and twse_proxy.sbl_ratio > 0.10:
            above_20d = (
                volume_profile.twenty_day_high > 0
                and ohlcv.close >= volume_profile.twenty_day_high * 0.99
            )
            if not above_20d:
                bd.sbl_breakout_fail = 8
                bd.flags.append("SBL_BREAKOUT_FAIL")

        # 5. 融資追價過熱: price up + 融資大增 + margin_util > 60%
        if twse_proxy is not None:
            if (
                twse_proxy.margin_balance_change > 0
                and twse_proxy.short_balance_increased
                and twse_proxy.margin_utilization_rate is not None
                and twse_proxy.margin_utilization_rate > 0.60
            ):
                bd.margin_chase_heat = 5
                bd.flags.append("MARGIN_CHASE_HEAT")

        # 6. ADX 過熱耗竭: ADX > 55 (trend likely exhausted)
        # Use cached DMI if provided, otherwise compute from scratch
        if dmi_now is not None:
            plus_di, minus_di, adx = dmi_now
        else:
            sorted_hist = sorted(history, key=lambda x: x.trade_date)
            plus_di, minus_di, adx = self._calculate_dmi(sorted_hist)
        if adx is not None and adx > 55:
            bd.adx_exhaustion_deduction = 6
            bd.flags.append(f"ADX_EXHAUSTION:{adx:.1f}")

        # 7. DMI 背離: +DI falling while -DI rising (momentum weakening)
        if plus_di is not None and minus_di is not None:
            if dmi_5d_ago is not None:
                plus_di_5d, minus_di_5d, _ = dmi_5d_ago
            else:
                sorted_hist = sorted(history, key=lambda x: x.trade_date)
                plus_di_5d, minus_di_5d, _ = (
                    self._calculate_dmi(sorted_hist[:-5])
                    if len(sorted_hist) >= 34
                    else (None, None, None)
                )
            sorted_for_prev = sorted(history, key=lambda x: x.trade_date)
            if (
                plus_di_5d is not None
                and minus_di_5d is not None
                and plus_di < plus_di_5d       # +DI declining
                and minus_di > minus_di_5d     # -DI rising
                and len(sorted_for_prev) >= 2
                and ohlcv.close >= sorted_for_prev[-2].close  # but price still up
            ):
                bd.dmi_divergence_deduction = 4
                bd.flags.append("DMI_DIVERGENCE")

        # 8. 連續爆量 ≥3 日：框架第3根不追（量 > 1.5× 20d avg 連續天數含今日）
        consec = self._vol_consecutive_surge_count(ohlcv, history)
        if consec >= 3:
            bd.vol_consecutive_surge = 5
            bd.flags.append(f"VOL_DAY{consec}_NO_CHASE")

    # ------------------------------------------------------------------
    # Signal building
    # ------------------------------------------------------------------

    def _compute_taiex_regime(self, taiex_history: list[DailyOHLCV]) -> str:
        """Return 'uptrend', 'downtrend', or 'neutral' based on TAIEX MA20.

        uptrend:   TAIEX MA20 today > TAIEX MA20 5 sessions ago
        downtrend: TAIEX MA20 today < TAIEX MA20 5 sessions ago by >1%
        neutral:   otherwise
        """
        slope = self._ma20_slope(taiex_history)
        if slope is None:
            return "neutral"
        if slope > 0:
            return "uptrend"
        if slope < -0.01:
            return "downtrend"
        return "neutral"

    def _map_action(
        self, confidence: int, bd: _ScoreBreakdown | None = None, chip_pts: int = 0
    ) -> str:
        """Map confidence score to action label using regime-adjusted thresholds."""
        taiex = getattr(self, "_taiex_history", [])
        regime = self._compute_taiex_regime(taiex)
        if regime == "uptrend":
            long_threshold = _LONG_THRESHOLD_UPTREND
        elif regime == "downtrend":
            long_threshold = _LONG_THRESHOLD_DOWNTREND
        else:
            long_threshold = _LONG_THRESHOLD_NEUTRAL

        if confidence >= long_threshold:
            return "LONG"
        if confidence >= _WATCH_MIN:
            return "WATCH"
        return "CAUTION"

    def _build_signal(
        self,
        ohlcv: DailyOHLCV,
        breakdown: _ScoreBreakdown,
        volume_profile: VolumeProfile,
        chip_report: ChipReport,
    ) -> SignalOutput:
        # Gate failure: return CAUTION with NO_SETUP flag and confidence=0
        if "NO_SETUP" in breakdown.flags:
            plan = self._make_execution_plan(ohlcv, volume_profile)
            data_quality_flags = list(ohlcv.data_quality_flags)
            data_quality_flags.extend(chip_report.data_quality_flags)
            data_quality_flags.extend(volume_profile.data_quality_flags)
            # Propagate gate detail flags (GATE_PASS/FAIL/SKIP, INSUFFICIENT_GATE_DATA, GATE_MET)
            for f in breakdown.flags:
                if any(f.startswith(p) for p in (
                    "GATE_PASS:", "GATE_FAIL:", "GATE_SKIP:",
                    "INSUFFICIENT_GATE_DATA:", "GATE_MET:", "GATE_AVAILABLE:",
                    "LOW_LIQUIDITY:",
                )):
                    data_quality_flags.append(f)
                if "GATE_FAIL:G3_LOW_LIQ" in f:
                    data_quality_flags.append(f"LOW_LIQUIDITY:{self._market}")
            # v2.2b: COILING flag on gate-failed stocks is still meaningful for watchlist surfacing
            for tag in ("COILING_PRIME", "COILING"):
                if tag in breakdown.flags and tag not in data_quality_flags:
                    data_quality_flags.append(tag)
            data_quality_flags.append("NO_SETUP")
            # Top-level summary when data was insufficient
            if any(f.startswith("INSUFFICIENT_GATE_DATA:") for f in breakdown.flags):
                data_quality_flags.append("INSUFFICIENT_GATE_DATA")
            data_quality_flags.append("scoring_version:v2")
            return SignalOutput(
                ticker=ohlcv.ticker,
                date=ohlcv.trade_date,
                action="CAUTION",
                confidence=0,
                reasoning=Reasoning(),
                execution_plan=plan,
                halt_flag=False,
                data_quality_flags=data_quality_flags,
                free_tier_mode=True if self._free_tier_mode else None,
            )

        confidence = breakdown.total
        action = self._map_action(confidence, breakdown, breakdown.chip_pts)
        plan = self._make_execution_plan(ohlcv, volume_profile)

        data_quality_flags = list(ohlcv.data_quality_flags)
        data_quality_flags.extend(chip_report.data_quality_flags)
        data_quality_flags.extend(volume_profile.data_quality_flags)
        data_quality_flags.append("scoring_version:v2")

        # v2.2b: propagate COILING / COILING_PRIME flags (set in _compute)
        for tag in ("COILING_PRIME", "COILING"):
            if tag in breakdown.flags and tag not in data_quality_flags:
                data_quality_flags.append(tag)

        # EMERGING_SETUP: WATCH stocks with pre-breakout characteristics
        # MA aligned + MA20 slope up + institutional buying + in accumulation zone
        if action == "WATCH":
            has_ma_setup = (breakdown.ma_alignment_pts > 0 and breakdown.ma20_slope_pts > 0)
            has_institutional = (
                breakdown.foreign_strength_pts > 0
                or breakdown.trust_strength_pts > 0
                or breakdown.institution_continuity_pts >= 4
            )
            in_accumulation_zone = (breakdown.proximity_pts > 0)
            if has_ma_setup and has_institutional and in_accumulation_zone:
                data_quality_flags.append("EMERGING_SETUP")

        # Propagate specific gate failures for visibility in tests and UI
        for f in breakdown.flags:
            if "GATE_FAIL:G1_ALREADY_BROKE_OUT" in f:
                data_quality_flags.append("COILING_FAIL:G5_ALREADY_BROKE")
            if "GATE_FAIL:G4_REGIME:DOWNTREND" in f:
                data_quality_flags.append("COILING_FAIL:G3_TAIEX_DOWNTREND")
            if "GATE_FAIL:G3_LOW_LIQ" in f:
                data_quality_flags.append(f"LOW_LIQUIDITY:{self._market}")

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

    # ------------------------------------------------------------------
    # Hints (non-scoring, for LLM reasoning)
    # ------------------------------------------------------------------

    def _compute_hints(
        self,
        ohlcv: DailyOHLCV,
        history: list[DailyOHLCV],
        twse_proxy: TWSEChipProxy | None = None,
    ) -> _AnalysisHints:
        """Compute non-scoring contextual hints for LLM reasoning."""
        hints = _AnalysisHints()
        sorted_history = sorted(history, key=lambda x: x.trade_date)
        closes = pd.Series([d.close for d in sorted_history])

        if len(closes) >= 14:
            hints.rsi_14 = self._rsi(closes, 14)

        if len(closes) >= 26:
            macd_line, signal_line = self._macd(closes)
            hints.macd_line = macd_line
            hints.macd_signal = signal_line
            if macd_line is not None and signal_line is not None and len(closes) >= 27:
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

        plus_di, minus_di, adx = self._calculate_dmi(sorted_history)
        hints.adx = adx
        hints.plus_di = plus_di
        hints.minus_di = minus_di

        bb_upper, bb_lower, _, bb_width_pct = self._calculate_bb(sorted_history)
        hints.bb_upper = bb_upper
        hints.bb_lower = bb_lower
        hints.bb_width_percentile = bb_width_pct

        if len(sorted_history) >= 2:
            prev_close = sorted_history[-2].close
            if prev_close > 0:
                gap = (ohlcv.open - prev_close) / prev_close
                hints.gap_down_pct = round(gap * 100, 3)

        all_highs = [d.high for d in sorted_history]
        if all_highs:
            period_high = max(all_highs)
            if period_high > 0:
                hints.high52w_pct = round((ohlcv.close - period_high) / period_high * 100, 2)

        if twse_proxy is not None and twse_proxy.is_available:
            hints.daytrade_ratio = twse_proxy.daytrade_ratio
            if twse_proxy.short_cover_days is not None:
                hints.short_cover_days = round(twse_proxy.short_cover_days, 1)

        return hints

    # ------------------------------------------------------------------
    # Static computation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _vwap_5d(history: list[DailyOHLCV]) -> float | None:
        """5-day volume-weighted average close.

        Returns None if fewer than 5 sessions or total volume is zero.
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
    def _turnover_20ma(history: list[DailyOHLCV]) -> float | None:
        """20-session simple moving average of daily turnover (NT$).

        turnover_i = close_i × volume_i. Used by v2.2a liquidity gate so
        the threshold auto-adapts to high/low priced stocks.
        """
        recent = sorted(history, key=lambda x: x.trade_date)[-20:]
        if len(recent) < 20:
            return None
        return sum(d.close * d.volume for d in recent) / len(recent)

    def _coiling_detect(
        self,
        ohlcv: DailyOHLCV,
        history: list[DailyOHLCV],
        volume_profile: VolumeProfile,
        twse_proxy: TWSEChipProxy | None,
        regime: str,
    ) -> tuple[int, list[str]]:
        """v2.2b COILING detector — Gate (6 mandatory) + Quality Score (5 K-of-N).

        Returns (score, flags). Score ∈ [0, 5]; 0 means Gate failed.
        Flags document which condition fired (for debugging / LLM hints).
        """
        sorted_hist = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_hist) < 60:
            return 0, ["COILING_SKIP:INSUFFICIENT_HISTORY"]

        closes = [d.close for d in sorted_hist]
        highs = [d.high for d in sorted_hist]
        lows = [d.low for d in sorted_hist]
        volumes = [d.volume for d in sorted_hist]

        # --- Gate G2: MA20 > MA60 and MA20 slope ≥ 0 ---
        ma20_today = sum(closes[-20:]) / 20
        ma60_today = sum(closes[-60:]) / 60
        if ma20_today <= ma60_today:
            return 0, ["COILING_FAIL:G2_MA20_LE_MA60"]
        if len(closes) >= 25:
            ma20_5d_ago = sum(closes[-25:-5]) / 20
            if ma20_today < ma20_5d_ago:
                return 0, ["COILING_FAIL:G2_MA20_SLOPE_DOWN"]

        # --- Gate G3: TAIEX regime != downtrend ---
        if regime == "downtrend":
            return 0, ["COILING_FAIL:G3_TAIEX_DOWNTREND"]

        # --- Gate G4: pivot range over last 5 sessions < 5% ---
        last5_highs = highs[-4:] + [ohlcv.high]
        last5_lows = lows[-4:] + [ohlcv.low]
        pivot_low = min(last5_lows)
        pivot_high = max(last5_highs)
        if pivot_low <= 0:
            return 0, ["COILING_FAIL:G4_NO_RANGE"]
        pivot_range = (pivot_high - pivot_low) / pivot_low
        if pivot_range >= 0.05:
            return 0, [f"COILING_FAIL:G4_RANGE_{pivot_range*100:.1f}PCT"]

        # --- Gate G5: no breakout in last 5 sessions (close < 20d_high) ---
        twenty_day_high = volume_profile.twenty_day_high
        last5_closes = closes[-4:] + [ohlcv.close]
        if twenty_day_high > 0 and max(last5_closes) >= twenty_day_high:
            return 0, ["COILING_FAIL:G5_ALREADY_BROKE"]

        # --- Gate G6: close ≥ max(close[-10:]) × 0.97 (sit on platform top) ---
        last10_closes = closes[-9:] + [ohlcv.close]
        platform_top = max(last10_closes)
        if ohlcv.close < platform_top * 0.97:
            return 0, ["COILING_FAIL:G6_BELOW_PLATFORM"]

        # Gate passed — compute Quality Score
        flags: list[str] = ["COILING_GATE_PASS"]
        score = 0

        # --- Q1: Bollinger squeeze — bb_width_percentile < 20 ---
        _, _, _, bb_width_pct = self._calculate_bb(sorted_hist)
        if bb_width_pct is not None and bb_width_pct < 20.0:
            score += 1
            flags.append(f"COILING_Q1_SQUEEZE:{bb_width_pct:.0f}")

        # --- Q2: volume dry-up — 5d avg vol < 20d avg vol × 0.9 ---
        if len(volumes) >= 20:
            vol_20ma = sum(volumes[-20:]) / 20
            vol_5ma = sum(volumes[-5:]) / 5
            if vol_20ma > 0 and vol_5ma < vol_20ma * 0.9:
                score += 1
                flags.append(f"COILING_Q2_DRYUP:{vol_5ma/vol_20ma:.2f}")

        # --- Q3: institutional continuous buying (3+ consecutive days) ---
        if twse_proxy is not None and twse_proxy.is_available:
            if (
                twse_proxy.foreign_consecutive_buy_days >= 3
                or twse_proxy.trust_consecutive_buy_days >= 3
            ):
                score += 1
                flags.append("COILING_Q3_CHIP_CONTINUOUS")

        # --- Q4: prior constructive advance — close / min(close[-60:]) ≥ 1.15 ---
        min60 = min(closes[-60:])
        if min60 > 0 and ohlcv.close / min60 >= 1.15:
            score += 1
            flags.append(f"COILING_Q4_PRIOR_RUN:{(ohlcv.close/min60-1)*100:.0f}PCT")

        # --- Q5: close strength — last 5 sessions avg (close-low)/(high-low) > 0.5 ---
        recent5 = sorted_hist[-4:] + [ohlcv]
        strengths: list[float] = []
        for d in recent5:
            rng = d.high - d.low
            if rng > 0:
                strengths.append((d.close - d.low) / rng)
        if strengths and sum(strengths) / len(strengths) > 0.5:
            score += 1
            flags.append("COILING_Q5_CLOSE_STRONG")

        flags.append(f"COILING_SCORE:{score}")
        return score, flags

    @staticmethod
    def _ma20_slope(history: list[DailyOHLCV]) -> float | None:
        """MA20 slope as percentage change over _MA20_SLOPE_DIFF_DAYS sessions.

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
    def _calculate_dmi(
        history: list[DailyOHLCV],
        period: int = 14,
    ) -> tuple[float | None, float | None, float | None]:
        if len(history) < period * 2 + 1:
            return None, None, None

        sorted_h = sorted(history, key=lambda x: x.trade_date)
        highs = [d.high for d in sorted_h]
        lows = [d.low for d in sorted_h]
        closes = [d.close for d in sorted_h]

        tr_list, pdm_list, ndm_list = [], [], []
        for i in range(1, len(sorted_h)):
            high, low, prev_close = highs[i], lows[i], closes[i - 1]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            pdm_list.append(up_move if up_move > down_move and up_move > 0 else 0.0)
            ndm_list.append(down_move if down_move > up_move and down_move > 0 else 0.0)
            tr_list.append(float(tr))

        def _wilder_smooth(values: list[float], p: int) -> list[float]:
            if len(values) < p:
                return []
            result = [sum(values[:p])]
            for v in values[p:]:
                result.append(result[-1] - result[-1] / p + v)
            return result

        atr = _wilder_smooth(tr_list, period)
        pdi_raw = _wilder_smooth(pdm_list, period)
        ndi_raw = _wilder_smooth(ndm_list, period)

        if not atr or len(atr) != len(pdi_raw) or len(atr) != len(ndi_raw):
            return None, None, None

        plus_di_series = [100 * p / a if a > 0 else 0.0 for p, a in zip(pdi_raw, atr)]
        minus_di_series = [100 * n / a if a > 0 else 0.0 for n, a in zip(ndi_raw, atr)]

        dx_series = []
        for p, n in zip(plus_di_series, minus_di_series):
            denom = p + n
            dx_series.append(100 * abs(p - n) / denom if denom > 0 else 0.0)

        if len(dx_series) < period:
            return None, None, None
        adx_val = sum(dx_series[:period]) / period
        for dx in dx_series[period:]:
            adx_val = adx_val - adx_val / period + dx / period

        return (
            round(plus_di_series[-1], 2),
            round(minus_di_series[-1], 2),
            round(adx_val, 2),
        )

    @staticmethod
    def _calculate_bb(
        history: list[DailyOHLCV],
        period: int = 20,
        num_std: float = 2.0,
        percentile_window: int = 60,
    ) -> tuple[float | None, float | None, float | None, float | None]:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = pd.Series([d.close for d in sorted_h])

        if len(closes) < period:
            return None, None, None, None

        ma = closes.rolling(period).mean()
        std = closes.rolling(period).std(ddof=0)
        upper = ma + num_std * std
        lower = ma - num_std * std
        width = (upper - lower) / ma.replace(0, float("nan"))

        bb_upper = upper.iloc[-1]
        bb_lower = lower.iloc[-1]
        bb_width_now = width.iloc[-1]

        if pd.isna(bb_upper) or pd.isna(bb_lower) or pd.isna(bb_width_now):
            return None, None, None, None

        bb_width_pct: float | None = None
        width_vals = width.dropna()
        if len(width_vals) >= percentile_window:
            recent_widths = width_vals.iloc[-percentile_window:]
            rank = (recent_widths < bb_width_now).sum()
            bb_width_pct = round(float(rank) / len(recent_widths) * 100, 1)

        return round(float(bb_upper), 4), round(float(bb_lower), 4), round(float(bb_width_now), 6), bb_width_pct

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
        stop_loss       = T+0 closing price
        target          = max(poc_proxy × 1.05, close × 1.05)
                          Guarantees target > entry. poc_proxy may be lower than
                          current close when the highest-volume day was a panic
                          selloff, so we floor at close × 1.05.
        """
        close = ohlcv.close
        raw_target = round(volume_profile.poc_proxy * 1.05, 2)
        target = max(raw_target, round(close * 1.05, 2))
        return ExecutionPlan(
            entry_bid_limit=round(close * 0.995, 2),
            entry_max_chase=round(close * 1.005, 2),
            stop_loss=close,
            target=target,
        )
