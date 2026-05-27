"""SurgeRadar — aggressive detection of stocks igniting a fresh move (Day 0 / Day 1).

Complements TripleConfirmationEngine (mature pre-breakout signals). Target: catch
the first 1-2 bars of a volume surge with multi-factor confirmation, avoiding
late-cycle exhaustion plays.

Design philosophy:
    - Gates filter noise (not-fresh / low-quality / bearish tape)
    - Factors reward confluence (vol + chip + pattern + industry)
    - Grades: SURGE_ALPHA (high conviction), SURGE_BETA (actionable), SURGE_GAMMA (watch)
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from taiwan_stock_agent.domain.models import DailyOHLCV, TWSEChipProxy

_PARAMS_PATH = Path(__file__).resolve().parents[3] / "config" / "surge_params.json"


class SurgeRadar:
    def __init__(self, market: str = "TSE"):
        self._market = market
        self._params = self._load_params()

    @staticmethod
    def _load_params() -> dict:
        try:
            return json.loads(_PARAMS_PATH.read_text())
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _vol_20ma(history: list[DailyOHLCV]) -> float:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        vols = [d.volume for d in sorted_h[-20:]]
        return sum(vols) / len(vols) if vols else 0.0

    @staticmethod
    def _vol_5ma(history: list[DailyOHLCV]) -> float:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        vols = [d.volume for d in sorted_h[-5:]]
        return sum(vols) / len(vols) if vols else 0.0

    @staticmethod
    def _consecutive_surge_days(
        ohlcv: DailyOHLCV, history: list[DailyOHLCV], threshold_mult: float = 1.5
    ) -> int:
        """Count consecutive bars (today back) with vol >= threshold_mult * 20MA."""
        vol_20ma = SurgeRadar._vol_20ma(history)
        if vol_20ma <= 0:
            return 0
        threshold = vol_20ma * threshold_mult
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        all_bars = sorted_h + [ohlcv]
        count = 0
        for bar in reversed(all_bars):
            if bar.volume >= threshold:
                count += 1
            else:
                break
        return count

    @staticmethod
    def _rsi(history: list[DailyOHLCV], period: int = 14) -> float | None:
        if len(history) < period + 1:
            return None
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        gains: list[float] = []
        losses: list[float] = []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            if diff > 0:
                gains.append(diff)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(-diff)
        if len(gains) < period:
            return None
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 1)

    # ------------------------------------------------------------------
    # Gate layer
    # ------------------------------------------------------------------

    def _gate_check(
        self,
        ohlcv: DailyOHLCV,
        history: list[DailyOHLCV],
        taiex_regime: str,
        turnover_20ma: float,
    ) -> tuple[bool, list[str]]:
        """5 hard gates; returns (passed, flags)."""
        flags: list[str] = []
        gates = self._params.get("gates", {})

        if len(history) < 20:
            return False, ["SURGE_SKIP:INSUFFICIENT_HISTORY"]

        # G1: Fresh ignition — consecutive vol-surge days <= max
        max_days = gates.get("fresh_ignition_max_days", 2)
        consec = self._consecutive_surge_days(ohlcv, history)
        if consec == 0:
            return False, ["SURGE_FAIL:G1_NO_VOL_SURGE"]
        if consec > max_days:
            return False, [f"SURGE_FAIL:G1_STALE_DAY{consec}"]

        # G2: Volume — today >= 1.5x 20MA AND >= 2x 5MA
        vol_20ma = self._vol_20ma(history)
        vol_5ma = self._vol_5ma(history)
        min_ratio_20 = gates.get("vol_ratio_min", 1.5)
        min_ratio_5 = gates.get("vol_ratio_5ma_min", 2.0)
        if vol_20ma <= 0 or ohlcv.volume < vol_20ma * min_ratio_20:
            ratio = ohlcv.volume / vol_20ma if vol_20ma > 0 else 0
            return False, [f"SURGE_FAIL:G2_VOL_LOW:{ratio:.2f}x_20MA"]
        if vol_5ma > 0 and ohlcv.volume < vol_5ma * min_ratio_5:
            ratio5 = ohlcv.volume / vol_5ma
            return False, [f"SURGE_FAIL:G2_VOL_NOT_BURST:{ratio5:.2f}x_5MA"]

        # G3: K-bar strength — close in upper half of day range
        bar_range = ohlcv.high - ohlcv.low
        if bar_range <= 0:
            return False, ["SURGE_FAIL:G3_DOJI_OR_HALT"]
        close_strength = (ohlcv.close - ohlcv.low) / bar_range
        min_strength = gates.get("close_strength_min", 0.5)
        if close_strength < min_strength:
            return False, [f"SURGE_FAIL:G3_WEAK_CLOSE:{close_strength:.2f}"]

        # G4: Liquidity (two sub-conditions)
        tse_t = gates.get("min_turnover_tse", 20_000_000)
        tpex_t = gates.get("min_turnover_tpex", 8_000_000)
        threshold = tse_t if self._market == "TSE" else tpex_t
        if turnover_20ma < threshold:
            return False, [f"SURGE_FAIL:G4_LOW_TURNOVER:{turnover_20ma/1e6:.1f}M"]

        min_lots = gates.get("min_avg_daily_lots", 500)
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        vols = [d.volume for d in sorted_h[-20:]]
        avg_lots = sum(vols) / len(vols) / 1000 if vols else 0
        if avg_lots < min_lots:
            return False, [f"SURGE_FAIL:G4_LOW_LOTS:{avg_lots:.0f}張"]

        # G5: TAIEX regime not bearish
        if taiex_regime == "downtrend":
            return False, ["SURGE_FAIL:G5_TAIEX_DOWNTREND"]

        flags.append("SURGE_GATE_PASS")
        flags.append(f"SURGE_DAY{consec}")
        return True, flags

    # ------------------------------------------------------------------
    # Factors (max 85 raw pts)
    # ------------------------------------------------------------------

    def _score_vol_ratio(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> tuple[float, list[str]]:
        """Phase 4.45: log curve, peak at 3-4x (IDEAL zone).
        1.5x→6 / 2x→8 / 3x→10 / 5x+→fades to 8 (exhaustion-aware)
        """
        vol_20ma = self._vol_20ma(history)
        if vol_20ma <= 0:
            return 0.0, []
        ratio = ohlcv.volume / vol_20ma
        f = self._params.get("factors", {})
        # tier peaks: tunable via params
        peak_ideal = float(f.get("vol_ratio_ideal", 10))
        peak_surge = float(f.get("vol_ratio_surge", 8))

        if ratio < 1.5:
            # 1.0→0, 1.5→6 linear
            if ratio < 1.0:
                return 0.0, [f"VOL_LOW:{ratio:.2f}x"]
            mild = float(f.get("vol_ratio_mild", 6))
            pts = round(mild * (ratio - 1.0) / 0.5, 2)
            return pts, [f"VOL_LOW:{ratio:.2f}x"]
        if ratio < 3.0:
            # 1.5→6, 3.0→10 linear
            mild = float(f.get("vol_ratio_mild", 6))
            pts = round(mild + (ratio - 1.5) / 1.5 * (peak_ideal - mild), 2)
            label = "VOL_SOLID" if ratio >= 2.0 else "VOL_MILD"
            return pts, [f"{label}:{ratio:.2f}x"]
        if ratio <= 5.0:
            # 3.0→10 peak, 5.0→8 fade
            pts = round(peak_ideal - (ratio - 3.0) / 2.0 * (peak_ideal - peak_surge), 2)
            return pts, [f"VOL_IDEAL:{ratio:.2f}x"]
        # >5x: continued fade by 1pt per +1x past 5x, floor at 5.0
        pts = round(max(5.0, peak_surge - (ratio - 5.0) * 1.0), 2)
        return pts, [f"VOL_SURGE:{ratio:.2f}x"]

    def _score_close_strength(self, ohlcv: DailyOHLCV) -> tuple[float, list[str]]:
        """Phase 4.45: continuous (ratio-0.3)*11.4, clamp [2, 8]."""
        bar_range = ohlcv.high - ohlcv.low
        if bar_range <= 0:
            return 0.0, []
        ratio = (ohlcv.close - ohlcv.low) / bar_range
        f = self._params.get("factors", {})
        soft = float(f.get("close_soft", 2))
        strong = float(f.get("close_strong", 8))
        # 0.3→soft (2), 1.0→strong (8); linear, clamp
        pts = soft + (ratio - 0.3) / 0.7 * (strong - soft)
        pts = max(soft, min(strong, pts))
        pts = round(pts, 2)
        if ratio >= 0.8:
            return pts, [f"CLOSE_STRONG:{ratio:.2f}"]
        if ratio >= 0.6:
            return pts, [f"CLOSE_HEALTHY:{ratio:.2f}"]
        return pts, [f"CLOSE_SOFT:{ratio:.2f}"]

    def _score_inst_buy_fresh(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[float, list[str]]:
        """Phase 4.45 continuous: net_buy / avg_vol intensity + fresh-day bonus.
        Day 1: 1x intensity → 8, Day 2: scaled to 7, Day 3+: 6.
        """
        if proxy is None or not proxy.is_available:
            return 0.0, []
        f = self._params.get("factors", {})
        days = max(proxy.foreign_consecutive_buy_days, proxy.trust_consecutive_buy_days)
        if days < 1:
            return 0.0, []
        # Day base (peak): 1→8, 2→7, 3+→6
        base_d1 = float(f.get("inst_buy_fresh_1d", 8))
        base_d2 = float(f.get("inst_buy_fresh_2d", 7))
        base_d3 = float(f.get("inst_buy_fresh_3d", 6))
        if days == 1:
            base = base_d1
        elif days == 2:
            base = base_d2
        else:
            base = base_d3

        # Intensity multiplier: net_buy / avg_vol; scale 0→0.5, 0.05→1.0 (cap)
        avg = float(proxy.avg_20d_volume) or 0.0
        net = max(proxy.foreign_net_buy, proxy.trust_net_buy, 0)
        if avg <= 0 or net <= 0:
            # No volume/net-buy data — return base unmodified (cannot compute intensity)
            return round(base, 2), [f"INST_FRESH:{days}D"]
        intensity = min(1.0, max(0.5, 0.5 + (net / avg) / 0.05 * 0.5))
        return round(base * intensity, 2), [f"INST_FRESH:{days}D"]

    def _score_industry_strength(
        self, industry_rank_pct: float | None
    ) -> tuple[float, list[str]]:
        """Phase 4.45 continuous: linear on industry heat percentile.
        60→5, 80→10 (linear up), <60→linear toward 0.
        """
        if industry_rank_pct is None:
            return 0.0, []
        f = self._params.get("factors", {})
        hot = float(f.get("industry_top_20pct", 10))
        warm = float(f.get("industry_top_40pct", 5))
        if industry_rank_pct >= 80:
            return hot, [f"IND_HOT:{industry_rank_pct:.0f}"]
        if industry_rank_pct >= 60:
            # 60→warm, 80→hot linear
            pts = round(warm + (industry_rank_pct - 60) / 20 * (hot - warm), 2)
            return pts, [f"IND_WARM:{industry_rank_pct:.0f}"]
        if industry_rank_pct >= 40:
            # 40→0, 60→warm linear
            pts = round((industry_rank_pct - 40) / 20 * warm, 2)
            return pts, [f"IND_COLD:{industry_rank_pct:.0f}"]
        return 0.0, [f"IND_COLD:{industry_rank_pct:.0f}"]

    def _score_pocket_pivot(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> tuple[float, list[str]]:
        """Phase 4.45: gates kept, score scales with vol over max_down_vol ratio."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_h) < 11:
            return 0.0, []
        last10 = sorted_h[-10:]
        down_vols = [
            b.volume for i, b in enumerate(last10)
            if i > 0 and b.close < last10[i - 1].close
        ]
        if not down_vols:
            return 0.0, []
        max_down_vol = max(down_vols)

        prev_close = sorted_h[-1].close
        is_up_day = ohlcv.close > prev_close
        bar_range = ohlcv.high - ohlcv.low
        close_pos = (ohlcv.close - ohlcv.low) / bar_range if bar_range > 0 else 0
        ma10 = sum(b.close for b in sorted_h[-10:]) / 10

        if (
            is_up_day
            and ohlcv.volume > max_down_vol
            and close_pos >= 0.5
            and ohlcv.close >= ma10
        ):
            f = self._params.get("factors", {})
            peak = float(f.get("pocket_pivot", 12))
            # Continuous on vol multiple over max_down_vol: 1.0→peak*0.7, 2.0+→peak
            if max_down_vol > 0:
                mult = ohlcv.volume / max_down_vol
                if mult >= 2.0:
                    pts = peak
                else:
                    pts = round(peak * 0.7 + (mult - 1.0) / 1.0 * peak * 0.3, 2)
            else:
                pts = peak
            return pts, ["POCKET_PIVOT"]
        return 0.0, []

    def _score_breakaway_gap(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> tuple[float, list[str]]:
        """Phase 4.45 continuous on gap_pct magnitude."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if not sorted_h:
            return 0.0, []
        prev_close = sorted_h[-1].close
        if prev_close <= 0:
            return 0.0, []
        gap_pct = (ohlcv.open / prev_close - 1) * 100
        f = self._params.get("factors", {})
        full = float(f.get("breakaway_gap_full", 8))
        partial = float(f.get("breakaway_gap_partial", 4))
        if gap_pct >= 1.0 and ohlcv.low > prev_close and ohlcv.close > ohlcv.open:
            # 1.0%→full, 3.0%+→ peak (full + 2 bonus capped)
            if gap_pct >= 3.0:
                return full, [f"GAP_FULL:{gap_pct:.1f}%"]
            pts = round(full * (0.7 + (gap_pct - 1.0) / 2.0 * 0.3), 2)
            return pts, [f"GAP_FULL:{gap_pct:.1f}%"]
        if gap_pct >= 0.5 and ohlcv.close > ohlcv.open:
            # 0.5→partial*0.7, 1.0→partial
            pts = round(partial * (0.7 + (gap_pct - 0.5) / 0.5 * 0.3), 2)
            return pts, [f"GAP_PARTIAL:{gap_pct:.1f}%"]
        return 0.0, []

    def _score_relative_strength(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV], taiex_history: list[DailyOHLCV]
    ) -> tuple[float, list[str]]:
        """Phase 4.45 continuous on diff magnitude.
        0.5%→4, 2.0%→8, >3%→8 cap.
        """
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        sorted_t = sorted(taiex_history, key=lambda x: x.trade_date)
        if not sorted_h or len(sorted_t) < 2:
            return 0.0, []
        stock_prev = sorted_h[-1].close
        if stock_prev <= 0:
            return 0.0, []
        stock_chg = (ohlcv.close / stock_prev - 1) * 100

        taiex_prev, taiex_today = sorted_t[-2].close, sorted_t[-1].close
        if taiex_prev <= 0:
            return 0.0, []
        taiex_chg = (taiex_today / taiex_prev - 1) * 100

        diff = stock_chg - taiex_chg
        if diff < 0.5:
            return 0.0, [f"RS:{diff:+.1f}%"]
        f = self._params.get("factors", {})
        peak = float(f.get("relative_strength", 8))
        # 0.5%→peak*0.5, 2.0%+→peak
        if diff >= 2.0:
            pts = peak
        else:
            pts = round(peak * 0.5 + (diff - 0.5) / 1.5 * peak * 0.5, 2)
        return pts, [f"RS:+{diff:.1f}%"]

    def _score_breakout_20d(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> tuple[float, list[str]]:
        """Phase 4.45: continuous on breakout magnitude.
        Just-above (0%)→base 6, +5%→peak 10.
        """
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_h) < 20:
            return 0.0, []
        prior_20d_high = max(b.high for b in sorted_h[-20:])
        if prior_20d_high <= 0 or ohlcv.close <= prior_20d_high:
            return 0.0, []
        f = self._params.get("factors", {})
        peak = float(f.get("breakout_20d", 10))
        # 0%→6, 5%+→peak (10)
        excess = (ohlcv.close - prior_20d_high) / prior_20d_high
        pts = round(min(peak, 6.0 + excess / 0.05 * (peak - 6.0)), 2)
        return pts, [f"BREAKOUT_20D:{ohlcv.close:.2f}>{prior_20d_high:.2f}"]

    def _score_rsi_healthy(
        self, history: list[DailyOHLCV]
    ) -> tuple[float, list[str]]:
        """Phase 4.45 continuous triangular: peak at 60-70 → 5, RSI>70 → 3 (breakout), <55 → linear taper."""
        rsi = self._rsi(history)
        if rsi is None:
            return 0.0, []
        f = self._params.get("factors", {})
        healthy = float(f.get("rsi_healthy", 5))
        breakout = float(f.get("rsi_breakout", 3))
        if rsi > 70:
            # 70+ is breakout territory; only fade if extremely overbought (>95)
            if rsi >= 95:
                pts = max(1.0, breakout - 1.0)
            else:
                pts = breakout
            return pts, [f"RSI_BREAKOUT:{rsi}"]
        if rsi >= 55:
            # 55→healthy floor 4, 65-70→peak 5
            if rsi >= 65:
                pts = healthy
            else:
                pts = round(4.0 + (rsi - 55) / 10.0 * (healthy - 4.0), 2)
            return pts, [f"RSI_HEALTHY:{rsi}"]
        if rsi >= 40:
            # 40→0, 55→4 (linear taper)
            pts = round((rsi - 40) / 15.0 * 4.0, 2)
            return pts, [f"RSI_WEAK:{rsi}"]
        return 0.0, [f"RSI_WEAK:{rsi}"]

    def _score_bb_squeeze_breakout(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> tuple[float, list[str]]:
        """Reward breakouts from prior Bollinger Band compression.

        On the surge day BBs are already expanding, so we look back to check
        whether they were recently squeezed — indicating stored energy release.

        bb_width = 4σ / mean  (fractional band width, period=20)
        squeeze  = recent-10-day minimum width vs 50-day percentile distribution
        """
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        period = 20
        if len(sorted_h) < period + 10:
            return 0, []

        closes = [b.close for b in sorted_h]

        # Compute BB width for every bar in history (excluding today)
        bb_widths: list[float] = []
        for i in range(period - 1, len(closes)):
            window = closes[i - period + 1: i + 1]
            mean = sum(window) / period
            if mean <= 0:
                continue
            std = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
            bb_widths.append(std * 4 / mean)

        if len(bb_widths) < 10:
            return 0, []

        # Recent squeeze: minimum BB width in the last 10 bars
        recent_min = min(bb_widths[-10:])

        # Historical context: 50-day percentile thresholds
        hist = bb_widths[-50:] if len(bb_widths) >= 50 else bb_widths
        hist_sorted = sorted(hist)
        p25 = hist_sorted[max(0, len(hist_sorted) // 4 - 1)]
        p40 = hist_sorted[max(0, int(len(hist_sorted) * 0.4) - 1)]

        # Today's BB width (history + today)
        all_closes = closes + [ohlcv.close]
        w = all_closes[-period:]
        mean_t = sum(w) / period
        if mean_t <= 0:
            return 0, []
        std_t = (sum((x - mean_t) ** 2 for x in w) / period) ** 0.5
        current_width = std_t * 4 / mean_t

        f = self._params.get("factors", {})
        label = f"BB:{recent_min:.3f}→{current_width:.3f}"
        strong = float(f.get("bb_squeeze_strong", 8))
        mild = float(f.get("bb_squeeze_mild", 4))

        # Phase 4.45 continuous on expansion ratio
        if recent_min > 0:
            expansion = current_width / recent_min
            if recent_min <= p25 and expansion >= 1.5:
                # 1.5→mild+2, 2.5→strong, 3.0+→strong cap
                if expansion >= 2.5:
                    pts = strong
                else:
                    pts = round((mild + 2.0) + (expansion - 1.5) / 1.0 * (strong - mild - 2.0), 2)
                return pts, [f"BB_SQUEEZE_BREAK:{label}"]
            if recent_min <= p40 and expansion >= 1.3:
                # 1.3→mild*0.5, 1.5→mild
                if expansion >= 1.5:
                    pts = mild
                else:
                    pts = round(mild * 0.5 + (expansion - 1.3) / 0.2 * mild * 0.5, 2)
                return pts, [f"BB_SQUEEZE_EXPAND:{label}"]
        return 0.0, [f"BB_WIDE:{current_width:.3f}"]

    def _score_margin_not_hot(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[float, list[str]]:
        """Phase 4.45 continuous: 0→4, 0.15→3, 0.20→2, 0.25→0 (linear taper)."""
        if proxy is None or not proxy.is_available:
            return 0.0, []
        util = proxy.margin_utilization_rate
        if util is None:
            return 0.0, []
        f = self._params.get("factors", {})
        cool = float(f.get("margin_not_hot", 4))
        warm = float(f.get("margin_warm", 2))
        if util >= 0.25:
            return 0.0, [f"MARGIN_HOT:{util*100:.1f}%"]
        if util < 0.15:
            # 0→cool peak, 0.15→3 (linear taper to ~75%)
            pts = round(cool - util / 0.15 * 1.0, 2)
            return max(warm + 1.0, pts), [f"MARGIN_COOL:{util*100:.1f}%"]
        if util < 0.20:
            # 0.15→3, 0.20→warm
            pts = round(3.0 - (util - 0.15) / 0.05 * (3.0 - warm), 2)
            return pts, [f"MARGIN_WARM:{util*100:.1f}%"]
        # 0.20-0.25: linear to 0
        pts = round(warm - (util - 0.20) / 0.05 * warm, 2)
        return max(0.0, pts), [f"MARGIN_WARM:{util*100:.1f}%"]

    def _score_inst_synergy(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[float, list[str]]:
        """Phase 4.45 continuous: synergy base + pct linear scaling."""
        if proxy is None or not proxy.is_available:
            return 0.0, []
        f = self._params.get("factors", {})
        pts = 0.0
        flags: list[str] = []

        if proxy.foreign_and_trust_both_buy:
            pts += float(f.get("inst_synergy_both", 5))
            flags.append("INST_SYNERGY")

        pct = proxy.inst_buy_pct
        if pct is not None and pct > 0:
            high = float(f.get("inst_pct_high", 6))
            # 0%→0, 5%→2, 10%→4, 15%+→6 linear
            if pct >= 0.15:
                pts += high
                flags.append(f"INST_PCT_HIGH:{pct*100:.1f}%")
            elif pct >= 0.05:
                # 0.05→2, 0.15→6 (linear with high as ceiling)
                pct_pts = 2.0 + (pct - 0.05) / 0.10 * (high - 2.0)
                pts += round(pct_pts, 2)
                if pct >= 0.10:
                    flags.append(f"INST_PCT_MID:{pct*100:.1f}%")
                else:
                    flags.append(f"INST_PCT_LOW:{pct*100:.1f}%")
            else:
                pts += round(pct / 0.05 * 2.0, 2)

        return round(pts, 2), flags

    def _score_margin_declining(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[float, list[str]]:
        """融資餘額今日下降 — 浮額持續被清洗，籌碼沉澱訊號。"""
        if proxy is None or not proxy.is_available:
            return 0, []
        if proxy.margin_balance_change < 0:
            f = self._params.get("factors", {})
            return f.get("margin_declining", 3), ["MARGIN_DECLINING"]
        return 0, []

    def _score_inst_cumulative_flow(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[float, list[str]]:
        """20日累計法人淨買超 — 靜默蓄積型態偵測。

        連買天數遇到任一賣超日就歸零，無法捕捉「買多賣少、整體持續增持」型機構。
        20日累計指標能偵測這類靜默累積（如 2026-04 元太蓄積型態）。

        Factor layers:
          1. cumul_ratio = (外資+投信 20日累計淨買) / 日均量
             >= 0.5x → CUMUL_FLOW_HOT  +8
             >= 0.2x → CUMUL_FLOW_WARM +4
          2. inst_flow_accel >= 1.5 + 累計為正 → FLOW_ACCEL +3 (爆量日加速確認)
          3. inst_buy_days_ratio >= 55% + 累計正 + 未減速 → QUIET_ACCUM +6
        """
        """Phase 4.45 continuous: log curve on cumul intensity, scaled on accel + ratio."""
        if proxy is None or not proxy.is_available:
            return 0.0, []

        f = self._params.get("factors", {})
        pts = 0.0
        flags: list[str] = []

        cumul_net = proxy.cumul_foreign_20d + proxy.cumul_trust_20d
        avg_vol = proxy.avg_20d_volume

        # 1. Cumulative intensity — log map
        if avg_vol > 0 and cumul_net > 0:
            cumul_ratio = cumul_net / avg_vol
            peak_hot = float(f.get("cumul_flow_hot", 8))
            warm = float(f.get("cumul_flow_warm", 4))
            if cumul_ratio >= 1.0:
                pts += peak_hot
            elif cumul_ratio >= 0.2:
                # 0.2→warm, 0.5→6, 1.0→peak_hot (log scaled)
                import math as _m
                pts += round(warm + _m.log(cumul_ratio / 0.2) / _m.log(5.0) * (peak_hot - warm), 2)
            else:
                pts += round(cumul_ratio / 0.2 * warm, 2)
            if cumul_ratio >= 0.5:
                flags.append(f"CUMUL_FLOW_HOT:{cumul_ratio:.1f}x")
            elif cumul_ratio >= 0.2:
                flags.append(f"CUMUL_FLOW_WARM:{cumul_ratio:.1f}x")

        # 2. Acceleration on surge day — continuous on accel magnitude
        if proxy.inst_flow_accel >= 1.5 and cumul_net > 0:
            peak_accel = float(f.get("flow_accel_bonus", 3))
            # 1.5→2.0, 3.0+→peak
            if proxy.inst_flow_accel >= 3.0:
                pts += peak_accel
            else:
                pts += round(2.0 + (proxy.inst_flow_accel - 1.5) / 1.5 * (peak_accel - 2.0), 2)
            flags.append(f"FLOW_ACCEL:{proxy.inst_flow_accel:.1f}x")

        # 3. Quiet accumulation — continuous on inst_buy_days_ratio
        if (proxy.inst_buy_days_ratio >= 0.55
                and cumul_net > 0
                and proxy.inst_flow_accel >= 0.8):
            peak_quiet = float(f.get("quiet_accum", 6))
            # 0.55→4, 0.80+→peak
            if proxy.inst_buy_days_ratio >= 0.80:
                pts += peak_quiet
            else:
                pts += round(4.0 + (proxy.inst_buy_days_ratio - 0.55) / 0.25 * (peak_quiet - 4.0), 2)
            flags.append(f"QUIET_ACCUM:{proxy.inst_buy_days_ratio:.0%}")

        return round(pts, 2), flags

    def _score_ownership_concentration(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[float, list[str]]:
        """集保大戶進、散戶退 — 週級籌碼集中度，雙向評分。

        加分：大戶增持 / 散戶退出
        扣分：散戶週增（主力尚未進場或正在出貨）
        聯合懲罰：融資高 + 散戶增 = 散戶用槓桿追高，最危險組合
        """
        """Phase 4.45 continuous: linear on holdings_chg magnitudes."""
        if proxy is None or not proxy.is_available:
            return 0.0, []
        f = self._params.get("factors", {})
        pts = 0.0
        flags: list[str] = []

        large = proxy.large_holder_chg_pct
        retail = proxy.retail_holder_chg_pct

        # ── 加分 ──────────────────────────────────────────────────────────────
        if large is not None and large > 0:
            peak_large = float(f.get("chip_large_holder_up", 5))
            # 0→0, 0.5%→peak (linear)
            if large >= 0.5:
                pts += peak_large
            else:
                pts += round(large / 0.5 * peak_large, 2)
            flags.append(f"CHIP_LARGE_UP:{large:+.2f}%")
        if retail is not None and retail < 0:
            peak_exit = float(f.get("chip_retail_exit", 3))
            abs_r = -retail
            if abs_r >= 0.5:
                pts += peak_exit
            else:
                pts += round(abs_r / 0.5 * peak_exit, 2)
            flags.append(f"CHIP_RETAIL_OUT:{retail:+.2f}%")

        # ── 扣分：散戶流入 ────────────────────────────────────────────────────
        if retail is not None and retail > 0:
            penalty_surge = float(f.get("chip_retail_surge_penalty", -5))
            penalty_in = float(f.get("chip_retail_in_penalty", -3))
            if retail > 0.5:
                # 0.5→penalty_in, 1.0+→penalty_surge
                if retail >= 1.0:
                    pts += penalty_surge
                else:
                    pts += round(penalty_in + (retail - 0.5) / 0.5 * (penalty_surge - penalty_in), 2)
                flags.append(f"CHIP_RETAIL_SURGE:{retail:+.2f}%")
            else:
                # 0→0, 0.5→penalty_in linear
                pts += round(retail / 0.5 * penalty_in, 2)
                flags.append(f"CHIP_RETAIL_IN:{retail:+.2f}%")

        # ── 聯合懲罰：融資過熱 + 散戶增 ──────────────────────────────────────
        margin_util = proxy.margin_utilization_rate
        if (retail is not None and retail > 0
                and margin_util is not None and margin_util > 0.20):
            pts += float(f.get("retail_leverage_trap_penalty", -5))
            flags.append(f"RETAIL_LEVERAGE_TRAP:{margin_util*100:.1f}%")

        return round(pts, 2), flags

    def _score_daytrade_penalty(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[float, list[str]]:
        """當沖比例高 = 籌碼不穩、散戶頻繁進出，扣分。"""
        if proxy is None or not proxy.is_available:
            return 0, []
        ratio = proxy.daytrade_ratio
        if ratio is None:
            return 0, []
        f = self._params.get("factors", {})
        if ratio > 0.50:
            return f.get("daytrade_extreme_penalty", -5), [f"DAYTRADE_EXTREME:{ratio*100:.0f}%"]
        if ratio > 0.30:
            return f.get("daytrade_high_penalty", -3), [f"DAYTRADE_HIGH:{ratio*100:.0f}%"]
        return 0, []

    def _score_ma5_walk(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV], n: int = 10
    ) -> tuple[float, list[str]]:
        """Phase 4.45 continuous: 0.5→0, 0.8→1.0, 1.0→2.0; <0.5 + below MA5 → -1."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        all_bars = sorted_h + [ohlcv]
        closes = pd.Series([d.close for d in all_bars])
        if len(closes) < 5:
            return 0.0, []
        ma5 = closes.rolling(5).mean()
        window = min(n, len(closes))
        close_win = closes.iloc[-window:]
        ma5_win = ma5.iloc[-window:]
        valid = ma5_win.notna()
        if valid.sum() == 0:
            return 0.0, []
        ratio = float((close_win[valid] >= ma5_win[valid]).mean())
        if ratio >= 0.5:
            # 0.5→0, 1.0→2.0 (linear)
            pts = round((ratio - 0.5) / 0.5 * 2.0, 2)
            if pts > 0:
                return pts, ["MA5_WALK"]
            return 0.0, []
        current_ma5 = ma5.iloc[-1]
        if pd.notna(current_ma5) and ohlcv.close < current_ma5:
            return -1.0, ["MA5_BREAK"]
        return 0.0, []

    def _score_bb_upper_walk(
        self,
        history: list[DailyOHLCV],
        surge_day: int,
        n: int = 5,
        tolerance: float = 0.03,
    ) -> tuple[float, list[str]]:
        """BB upper walk: MOMENTUM_WALK tag on surge_day<=2; exhaustion deduction on day>=3."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = pd.Series([d.close for d in sorted_h])
        if len(closes) < 20:
            return 0, []
        ma = closes.rolling(20).mean()
        std = closes.rolling(20).std(ddof=0)
        bb_upper = ma + 2 * std
        if len(bb_upper.dropna()) < n:
            return 0, []
        window_upper = bb_upper.iloc[-n:]
        window_close = closes.iloc[-n:]
        near_upper = int((window_close >= window_upper * (1 - tolerance)).sum())
        bb_upper_rising = float(bb_upper.iloc[-1]) > float(bb_upper.iloc[-n])
        if near_upper >= 3 and bb_upper_rising:
            if surge_day >= 3:
                # Phase 4.45: scale exhaustion by surge_day depth: day3→-2, day5+→-3
                penalty = max(-3.0, -2.0 - (surge_day - 3) * 0.5)
                return round(penalty, 2), ["BB_UPPER_EXHAUSTION"]
            return 0.0, ["MOMENTUM_WALK"]
        return 0.0, []

    # ------------------------------------------------------------------
    # Grade + aggregate
    # ------------------------------------------------------------------

    def _grade(self, score: float) -> str | None:
        t = self._params.get("grade_thresholds", {})
        if score >= t.get("SURGE_ALPHA", 55):
            return "SURGE_ALPHA"
        if score >= t.get("SURGE_BETA", 40):
            return "SURGE_BETA"
        if score >= t.get("SURGE_GAMMA", 28):
            return "SURGE_GAMMA"
        return None

    def _score_foreign_trend(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[float, list[str]]:
        """Phase 4.45 continuous: 1.0→0, 1.3→2.0, 2.0+→4.0 linear."""
        if proxy is None or not proxy.is_available:
            return 0.0, []
        accel = proxy.foreign_trend_accel
        if accel <= 1.0:
            return 0.0, []
        if accel >= 2.0:
            return 4.0, [f"FOREIGN_ACCEL:{accel:.1f}x"]
        if accel >= 1.3:
            pts = round(2.0 + (accel - 1.3) / 0.7 * 2.0, 2)
            return pts, [f"FOREIGN_ACCEL_MILD:{accel:.1f}x"]
        # 1.0→0, 1.3→2.0
        pts = round((accel - 1.0) / 0.3 * 2.0, 2)
        return pts, []

    def _score_short_cover(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[float, list[str]]:
        """Phase 4.45 continuous: 0.10→2.0, 0.20+→4.0 linear."""
        if proxy is None or not proxy.is_available:
            return 0.0, []
        rate = proxy.short_cover_rate
        if rate <= 0.10:
            return 0.0, []
        if rate >= 0.20:
            return 4.0, [f"SHORT_CAPITULATION:{rate:.0%}"]
        pts = round(2.0 + (rate - 0.10) / 0.10 * 2.0, 2)
        return pts, [f"SHORT_COVER:{rate:.0%}"]

    def _score_large_2w_trend(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[float, list[str]]:
        """Phase 4.45 continuous: 0→0, 0.5→3.0, 1.5+→5.0 linear."""
        if proxy is None or not proxy.is_available:
            return 0.0, []
        trend = proxy.large_holder_2w_trend
        if trend is None or trend <= 0:
            return 0.0, []
        if trend >= 1.5:
            return 5.0, [f"HOLDER_2W_UPTREND:{trend:+.2f}%"]
        if trend > 0.5:
            pts = round(3.0 + (trend - 0.5) / 1.0 * 2.0, 2)
            return pts, [f"HOLDER_2W_UP:{trend:+.2f}%"]
        # 0→0, 0.5→3.0 linear
        pts = round(trend / 0.5 * 3.0, 2)
        return pts, []

    def _score_inst_accel_short(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[float, list[str]]:
        """Phase 4.45 continuous: 1.0→0, 1.3→2.0, 2.0+→4.0 linear."""
        if proxy is None or not proxy.is_available:
            return 0.0, []
        accel = proxy.inst_accel_3d_10d
        if accel <= 1.0:
            return 0.0, []
        if accel >= 2.0:
            return 4.0, [f"INST_SURGE:{accel:.1f}x"]
        if accel >= 1.3:
            pts = round(2.0 + (accel - 1.3) / 0.7 * 2.0, 2)
            return pts, [f"INST_ACCEL_SHORT:{accel:.1f}x"]
        pts = round((accel - 1.0) / 0.3 * 2.0, 2)
        return pts, []

    def _score_taifex_context(
        self, heat_context: dict | None
    ) -> tuple[float, list[str]]:
        """Factor E: 台指期外資淨多單 + 大盤融資維持率壓力懲罰."""
        if not heat_context:
            return 0, []
        pts, flags = 0, []
        if heat_context.get("taifex_bearish") or heat_context.get("futures_bearish"):
            pts -= 5
            flags.append("TAIFEX_FUTURES_BEARISH")
        margin_rate = heat_context.get("margin_maintenance_rate")
        if margin_rate is not None:
            if margin_rate < 120.0:
                pts -= 15
                flags.append("MARKET_MARGIN_CRISIS")
            elif margin_rate < 130.0:
                pts -= 7
                flags.append("MARKET_MARGIN_STRESS")
        return pts, flags

    def _score_market_heat(self, ctx: dict | None) -> tuple[float, list[str]]:
        """Phase 4.45 continuous: linear on industry rank + log on concept count + capped intl."""
        if not ctx:
            return 0.0, []
        pts = 0.0
        flags: list[str] = []
        f = self._params.get("factors", {})

        ind_rank = ctx.get("ind_5d_rank_pct", 0) or 0
        hot = float(f.get("heat_ind_hot", 3))
        warm = float(f.get("heat_ind_warm", 2))
        if ind_rank >= 80:
            pts += hot
            flags.append(f"IND_HEAT_HOT:{ind_rank:.0f}")
        elif ind_rank >= 60:
            # 60→warm, 80→hot linear
            pts += round(warm + (ind_rank - 60) / 20.0 * (hot - warm), 2)
            flags.append(f"IND_HEAT_WARM:{ind_rank:.0f}")
        elif ind_rank >= 40:
            # 40→0, 60→warm linear
            pts += round((ind_rank - 40) / 20.0 * warm, 2)

        if ctx.get("accelerating"):
            pts += float(f.get("heat_ind_accel", 2))
            flags.append("IND_ACCEL")

        hot_concepts = ctx.get("hot_concepts") or []
        if hot_concepts:
            base = float(f.get("heat_concept", 8))
            multi = float(f.get("heat_concept_multi", 5))
            # 1 concept → base, 2 → base+multi, 3+ → base+multi+small bonus (capped)
            extra = multi if len(hot_concepts) >= 2 else 0.0
            if len(hot_concepts) >= 3:
                extra = min(multi + 2.0, multi + (len(hot_concepts) - 2) * 1.0)
            pts += base + extra
            label = f"{hot_concepts[0]}" + (f"+{len(hot_concepts)-1}more" if len(hot_concepts) > 1 else "")
            flags.append(f"CONCEPT_HOT:{label}")

        intl = ctx.get("intl_tailwind", 0) or 0
        if intl > 0:
            bonus = min(float(intl), float(f.get("heat_intl_max", 2)))
            pts += bonus
            flags.append(f"INTL_TAIL:+{intl}")

        return round(pts, 2), flags

    def score_full(
        self,
        ohlcv: DailyOHLCV,
        history: list[DailyOHLCV],
        proxy: TWSEChipProxy | None,
        taiex_regime: str,
        taiex_history: list[DailyOHLCV],
        turnover_20ma: float,
        industry_rank_pct: float | None = None,
        heat_context: dict | None = None,
    ) -> dict | None:
        """Returns dict if passes gates AND grade >= SURGE_GAMMA, else None."""
        passed, gate_flags = self._gate_check(ohlcv, history, taiex_regime, turnover_20ma)
        if not passed:
            return None

        all_flags: list[str] = gate_flags[:]
        breakdown: dict[str, float] = {}
        raw = 0.0

        consec = self._consecutive_surge_days(ohlcv, history)

        # pocket_pivot and breakout_20d frequently co-fire (both require price above recent
        # highs with strong volume). Combined max = 22/95 pts. Intentional: double confirmation
        # raises conviction. Adjust individual weights in surge_params.json if over-rewarding.
        factors = [
            ("vol_ratio", self._score_vol_ratio(ohlcv, history)),
            ("close_strength", self._score_close_strength(ohlcv)),
            ("inst_buy_fresh", self._score_inst_buy_fresh(proxy)),
            ("industry_strength", self._score_industry_strength(industry_rank_pct)),
            ("pocket_pivot", self._score_pocket_pivot(ohlcv, history)),
            ("breakaway_gap", self._score_breakaway_gap(ohlcv, history)),
            ("relative_strength", self._score_relative_strength(ohlcv, history, taiex_history)),
            ("breakout_20d", self._score_breakout_20d(ohlcv, history)),
            ("rsi_healthy", self._score_rsi_healthy(history)),
            ("margin_not_hot", self._score_margin_not_hot(proxy)),
            ("inst_synergy", self._score_inst_synergy(proxy)),
            ("margin_declining", self._score_margin_declining(proxy)),
            ("inst_cumulative_flow", self._score_inst_cumulative_flow(proxy)),
            ("ownership_concentration", self._score_ownership_concentration(proxy)),
            ("daytrade_penalty", self._score_daytrade_penalty(proxy)),
            ("bb_squeeze", self._score_bb_squeeze_breakout(ohlcv, history)),
            ("ma5_walk", self._score_ma5_walk(ohlcv, history)),
            ("bb_upper_walk", self._score_bb_upper_walk(history, consec)),
            ("market_heat", self._score_market_heat(heat_context)),
            ("foreign_trend", self._score_foreign_trend(proxy)),
            ("short_cover", self._score_short_cover(proxy)),
            ("large_2w_trend", self._score_large_2w_trend(proxy)),
            ("inst_accel_short", self._score_inst_accel_short(proxy)),
            ("taifex_context", self._score_taifex_context(heat_context)),
        ]

        for name, (pts, flags) in factors:
            breakdown[name] = pts
            raw += pts
            all_flags.extend(flags)

        raw_max = self._params.get("raw_max_pts", 87)
        # Phase 4.45: keep continuous score (no min(100, ...)) for finer differentiation
        score = round(raw / raw_max * 100, 2)
        grade = self._grade(score)

        if grade is None:
            return None

        vol_20ma = self._vol_20ma(history)
        vol_ratio = round(ohlcv.volume / vol_20ma, 2) if vol_20ma > 0 else 0.0
        bar_range = ohlcv.high - ohlcv.low
        close_pos = round((ohlcv.close - ohlcv.low) / bar_range, 2) if bar_range > 0 else 0.0
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        prev_close = sorted_h[-1].close if sorted_h else ohlcv.close
        day_chg_pct = round((ohlcv.close / prev_close - 1) * 100, 2) if prev_close > 0 else 0.0
        gap_pct = round((ohlcv.open / prev_close - 1) * 100, 2) if prev_close > 0 else 0.0

        return {
            "grade": grade,
            "score": score,
            "raw_pts": raw,
            "flags": all_flags,
            "score_breakdown": breakdown,
            "vol_ratio": vol_ratio,
            "close_strength": close_pos,
            "day_chg_pct": day_chg_pct,
            "gap_pct": gap_pct,
            "surge_day": consec,
            "industry_rank_pct": industry_rank_pct,
            "rsi": self._rsi(history),
            "close_price": float(ohlcv.close),
            "inst_consec_days": max(
                proxy.foreign_consecutive_buy_days, proxy.trust_consecutive_buy_days
            ) if proxy and proxy.is_available else 0,
        }
