from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from taiwan_stock_agent.domain.models import DailyOHLCV

_PARAMS_PATH = Path(__file__).resolve().parents[3] / "config" / "accumulation_params.json"


class AccumulationEngine:

    def __init__(self, market: str = "TSE"):
        self._market = market
        self._params = self._load_params()

    @staticmethod
    def _load_params() -> dict:
        try:
            return json.loads(_PARAMS_PATH.read_text())
        except Exception:
            return {}

    def _gate_check(
        self,
        history: list[DailyOHLCV],
        taiex_regime: str,
        turnover_20ma: float,
    ) -> tuple[bool, list[str]]:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        flags: list[str] = []

        # G1: MA20 > MA60 and MA20 slope >= 0
        if len(closes) < 60:
            return False, ["ACCUM_SKIP:INSUFFICIENT_HISTORY"]
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        if ma20 <= ma60:
            return False, ["ACCUM_FAIL:G1_MA20_LE_MA60"]
        if len(closes) >= 25:
            ma20_5d_ago = sum(closes[-25:-5]) / 20
            if ma20 < ma20_5d_ago:
                return False, ["ACCUM_FAIL:G1_MA20_SLOPE_DOWN"]

        # G2: not yet broken out (max of last 10 closes < 60d resistance × 1.03)
        # Use bar.high for resistance level — close can exceed prior closes while highs define resistance
        sixty_day_high = max(bar.high for bar in sorted_h[-60:])
        last10_closes = closes[-10:]
        if max(last10_closes) >= sixty_day_high * 1.03:
            return False, ["ACCUM_FAIL:G2_ALREADY_BROKE"]

        # G3: market regime
        if taiex_regime == "downtrend":
            return False, ["G3_TAIEX_DOWNTREND"]

        # G4: liquidity
        tse_threshold = 20_000_000
        tpex_threshold = 8_000_000
        threshold = tse_threshold if self._market == "TSE" else tpex_threshold
        if turnover_20ma < threshold:
            return False, [f"G4_LOW_LIQUIDITY:{turnover_20ma/1e6:.1f}M<{threshold/1e6:.0f}M"]

        flags.append("ACCUM_GATE_PASS")
        return True, flags


    @staticmethod
    def _obv_slope(history: list[DailyOHLCV]) -> float | None:
        """5-day linear slope of OBV. Returns None if < 6 bars."""
        if len(history) < 6:
            return None
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        obv = 0.0
        obvs = []
        prev_close = sorted_h[0].close
        for bar in sorted_h[1:]:
            if bar.close > prev_close:
                obv += bar.volume
            elif bar.close < prev_close:
                obv -= bar.volume
            obvs.append(obv)
            prev_close = bar.close
        series = pd.Series(obvs[-5:])
        x = pd.Series(range(5), dtype=float)
        denom = (5 * (x**2).sum() - x.sum()**2)
        if denom == 0:
            return None
        slope = (5 * (x * series).sum() - x.sum() * series.sum()) / denom
        return float(slope)

    @staticmethod
    def _atr(history: list[DailyOHLCV], period: int = 14) -> float | None:
        """Average True Range over `period` bars. Returns None if insufficient history.

        Note: uses SMA of True Range (not Wilder smoothing).
        """
        if len(history) < period + 1:
            return None
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        trs = []
        for i in range(1, len(sorted_h)):
            prev_close = sorted_h[i - 1].close
            bar = sorted_h[i]
            tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
            trs.append(tr)
        return float(sum(trs[-period:]) / period)

    @staticmethod
    def _atr_percentile(history: list[DailyOHLCV], period: int = 14, window: int = 252) -> float | None:
        """
        Percentile rank of current ATR within the trailing `window` ATR values.
        Returns None if len(history) < period + window.

        Note: uses SMA of True Range (not Wilder smoothing).
        """
        if len(history) < period + window:
            return None
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        trs = []
        for i in range(1, len(sorted_h)):
            prev_close = sorted_h[i - 1].close
            bar = sorted_h[i]
            tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
            trs.append(tr)
        atrs = [sum(trs[i:i + period]) / period for i in range(len(trs) - period + 1)]
        recent_atr = atrs[-1]
        window_atrs = atrs[-window:]
        rank = sum(1 for v in window_atrs if v < recent_atr)
        return round(float(rank) / len(window_atrs) * 100, 1)

    @staticmethod
    def _kd_d(history: list[DailyOHLCV], k_period: int = 9, d_smooth: int = 3,
              lookback: int = 5) -> list[float] | None:
        """
        Returns last `lookback` Stochastic %D values, or None if insufficient history.
        Minimum bars required: k_period + d_smooth + lookback.

        Applies one SMA smoothing to raw %K, equivalent to Fast Stochastic %D convention.
        """
        min_needed = k_period + d_smooth + lookback
        if len(history) < min_needed:
            return None
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        k_vals = []
        for i in range(k_period - 1, len(sorted_h)):
            window = sorted_h[i - k_period + 1: i + 1]
            low_k = min(b.low for b in window)
            high_k = max(b.high for b in window)
            rng = high_k - low_k
            k = ((sorted_h[i].close - low_k) / rng * 100) if rng > 0 else 50.0
            k_vals.append(k)
        d_vals = []
        for i in range(d_smooth - 1, len(k_vals)):
            d_vals.append(sum(k_vals[i - d_smooth + 1: i + 1]) / d_smooth)
        return [round(v, 2) for v in d_vals[-lookback:]] if len(d_vals) >= lookback else None

    # ------------------------------------------------------------------
    # Dimension A — Compression Pattern
    # ------------------------------------------------------------------

    def _score_bb_compression(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = pd.Series([d.close for d in sorted_h])
        if len(closes) < 20:
            return 0, []
        period, num_std, window = 20, 2.0, 252
        ma = closes.rolling(period).mean()
        std = closes.rolling(period).std(ddof=0)
        width = ((ma + num_std * std) - (ma - num_std * std)) / ma.replace(0, float("nan"))
        width_vals = width.dropna()
        if len(width_vals) < window:
            return 0, ["ACCUM_SHORT_HISTORY:" + str(len(width_vals))]
        recent = width_vals.iloc[-window:]
        current = width_vals.iloc[-1]
        pct = float((recent < current).sum()) / len(recent) * 100
        if pct < 15:
            return 20, [f"ACCUM_BB_PCT:{pct:.0f}"]
        if pct < 30:
            return 10, [f"ACCUM_BB_PCT:{pct:.0f}"]
        return 0, [f"ACCUM_BB_PCT:{pct:.0f}"]

    def _score_volume_dryup(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        vols = [d.volume for d in sorted_h]
        if len(vols) < 20:
            return 0, []
        avg20 = sum(vols[-20:]) / 20
        avg5 = sum(vols[-5:]) / 5
        if avg20 <= 0:
            return 0, []
        ratio = avg5 / avg20
        if ratio < 0.70:
            return 15, [f"ACCUM_VOL_DRYUP:{ratio:.2f}"]
        if ratio < 0.85:
            return 8, [f"ACCUM_VOL_DRYUP:{ratio:.2f}"]
        return 0, [f"ACCUM_VOL_RATIO:{ratio:.2f}"]

    def _score_consolidation_range(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_h) < 20:
            return 0, []
        last20 = sorted_h[-20:]
        high20 = max(d.high for d in last20)
        low20 = min(d.low for d in last20)
        if low20 <= 0:
            return 0, []
        spread = (high20 - low20) / low20
        if spread < 0.05:
            return 15, [f"ACCUM_RANGE:{spread*100:.1f}PCT"]
        if spread < 0.08:
            return 8, [f"ACCUM_RANGE:{spread*100:.1f}PCT"]
        return 0, [f"ACCUM_RANGE:{spread*100:.1f}PCT"]

    def _score_atr_contraction(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
        pct = self._atr_percentile(history)
        if pct is None:
            return 0, [f"ACCUM_SHORT_HISTORY:{len(history)}"]
        if pct < 20:
            return 10, [f"ACCUM_ATR_PCT:{pct:.0f}"]
        if pct < 35:
            return 5, [f"ACCUM_ATR_PCT:{pct:.0f}"]
        return 0, [f"ACCUM_ATR_PCT:{pct:.0f}"]

    def _score_inside_bars(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_h) < 6:
            return 0, []
        last6 = sorted_h[-6:]
        count = 0
        for i in range(1, len(last6)):
            cur, prev = last6[i], last6[i - 1]
            if cur.high <= prev.high and cur.low >= prev.low:
                count += 1
        if count >= 3:
            return 5, [f"ACCUM_INSIDE_BARS:{count}"]
        if count >= 1:
            return 2, [f"ACCUM_INSIDE_BARS:{count}"]
        return 0, []

    # ------------------------------------------------------------------
    # Dimension B — Technical Confirmation
    # ------------------------------------------------------------------

    def _score_ma_convergence(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        if len(closes) < 20:
            return 0, []
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        ref = closes[-1]
        if ref <= 0:
            return 0, []
        gap = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / ref * 100
        if gap < 2.0:
            return 10, [f"ACCUM_MA_GAP:{gap:.1f}PCT"]
        if gap < 4.0:
            return 5, [f"ACCUM_MA_GAP:{gap:.1f}PCT"]
        return 0, [f"ACCUM_MA_GAP:{gap:.1f}PCT"]

    def _score_obv_trend(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
        if len(history) < 10:
            return 0, []
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        price_return = abs(closes[-1] / closes[-6] - 1) if closes[-6] > 0 else 1.0
        if price_return >= 0.02:
            return 0, []
        slope = self._obv_slope(sorted_h)
        if slope is not None and slope > 0:
            return 8, ["ACCUM_OBV_RISING"]
        return 0, []

    def _score_kd_low_flat(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
        vals = self._kd_d(history)
        if vals is None or len(vals) < 3:
            return 0, []
        latest_d = vals[-1]
        flatness = max(vals[-3:]) - min(vals[-3:])
        if latest_d < 30 and flatness < 5.0:
            return 7, [f"ACCUM_KD_D:{latest_d:.1f}"]
        return 0, []

    def _score_close_above_midline(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        if len(closes) < 20:
            return 0, []
        ma20 = sum(closes[-20:]) / 20
        if closes[-1] > ma20:
            return 5, ["ACCUM_ABOVE_MIDLINE"]
        return 0, []

    # ------------------------------------------------------------------
    # Dimension C — Chip Behavior
    # ------------------------------------------------------------------

    def _score_institutional_consec(self, proxy: "TWSEChipProxy | None") -> tuple[int, list[str]]:
        if proxy is None or not proxy.is_available:
            return 0, []
        days = max(proxy.foreign_consecutive_buy_days, proxy.trust_consecutive_buy_days)
        if days >= 5:
            return 20, [f"ACCUM_INST_CONSEC:{days}D"]
        if days >= 3:
            return 12, [f"ACCUM_INST_CONSEC:{days}D"]
        if days >= 1:
            return 5, [f"ACCUM_INST_CONSEC:{days}D"]
        return 0, []

    def _score_institutional_net_trend(self, proxy: "TWSEChipProxy | None") -> tuple[int, list[str]]:
        # Phase 4.20 proxy: consecutive days as proxy. Full impl deferred to Phase 4.21.
        if proxy is None or not proxy.is_available:
            return 0, []
        if proxy.foreign_consecutive_buy_days >= 3:
            return 10, ["ACCUM_NET_TREND_PROXY:CONSEC3"]
        if proxy.foreign_consecutive_buy_days >= 1:
            return 5, ["ACCUM_NET_TREND_PROXY:CONSEC1"]
        return 0, []

    def _score_updown_volume(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        last10 = sorted_h[-10:] if len(sorted_h) >= 10 else sorted_h
        if len(last10) < 4:
            return 0, []
        up_vols = [b.volume for i, b in enumerate(last10[1:], 1) if b.close > last10[i - 1].close]
        dn_vols = [b.volume for i, b in enumerate(last10[1:], 1) if b.close < last10[i - 1].close]
        if not up_vols or not dn_vols:
            return 0, []
        if sum(up_vols) / len(up_vols) > sum(dn_vols) / len(dn_vols):
            return 8, ["ACCUM_UP_VOL_DOMINATES"]
        return 0, []

    def _score_market_relative_strength(
        self, history: list[DailyOHLCV], taiex_history: list[DailyOHLCV]
    ) -> tuple[int, list[str]]:
        if not taiex_history or len(history) < 5 or len(taiex_history) < 5:
            return 0, []
        sorted_s = sorted(history, key=lambda x: x.trade_date)[-5:]
        sorted_t = sorted(taiex_history, key=lambda x: x.trade_date)[-5:]
        protected = 0
        for i in range(1, min(len(sorted_s), len(sorted_t))):
            taiex_chg = (sorted_t[i].close / sorted_t[i - 1].close - 1) if sorted_t[i - 1].close > 0 else 0
            stock_chg = (sorted_s[i].close / sorted_s[i - 1].close - 1) if sorted_s[i - 1].close > 0 else 0
            if taiex_chg < 0 and stock_chg > taiex_chg / 2:
                protected += 1
        if protected >= 2:
            return 7, [f"ACCUM_MKT_PROTECT:{protected}"]
        return 0, []

    def _score_proximity_to_resistance(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        if len(closes) < 60:
            return 0, []
        # Use bar.high for resistance — consistent with gate layer G2 which also uses bar.high
        high60 = max(bar.high for bar in sorted_h[-60:])
        ratio = closes[-1] / high60 if high60 > 0 else 0
        if 0.95 <= ratio < 1.03:
            return 5, [f"ACCUM_VS_HIGH:{(ratio - 1) * 100:.1f}PCT"]
        return 0, [f"ACCUM_VS_HIGH:{(ratio - 1) * 100:.1f}PCT"]

    def _score_prior_advance(self, history: list[DailyOHLCV]) -> tuple[int, list[str]]:
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        if len(closes) < 60:
            return 0, []
        min60 = min(closes[-60:])
        if min60 > 0 and closes[-1] / min60 >= 1.15:
            return 5, [f"ACCUM_PRIOR_ADVANCE:{(closes[-1] / min60 - 1) * 100:.0f}PCT"]
        return 0, []

    # ------------------------------------------------------------------
    # Aggregator
    # ------------------------------------------------------------------

    def _grade(self, score: int) -> str | None:
        thresholds = self._params.get("grade_thresholds", {
            "COIL_PRIME": 70, "COIL_MATURE": 50, "COIL_EARLY": 35
        })
        if score >= thresholds["COIL_PRIME"]:
            return "COIL_PRIME"
        if score >= thresholds["COIL_MATURE"]:
            return "COIL_MATURE"
        if score >= thresholds["COIL_EARLY"]:
            return "COIL_EARLY"
        return None

    def score_full(
        self,
        history: list[DailyOHLCV],
        proxy,
        taiex_regime: str,
        taiex_history: list[DailyOHLCV],
        turnover_20ma: float,
    ) -> "dict | None":
        """Returns grade dict or None if gate fails or score below COIL_EARLY threshold."""
        passed, gate_flags = self._gate_check(history, taiex_regime, turnover_20ma)
        if not passed:
            return None

        breakdown: dict[str, int] = {}
        all_flags: list[str] = gate_flags[:]
        raw = 0

        factors = [
            ("bb_compression", self._score_bb_compression(history)),
            ("volume_dryup", self._score_volume_dryup(history)),
            ("consolidation_range", self._score_consolidation_range(history)),
            ("atr_contraction", self._score_atr_contraction(history)),
            ("inside_bars", self._score_inside_bars(history)),
            ("ma_convergence", self._score_ma_convergence(history)),
            ("obv_trend", self._score_obv_trend(history)),
            ("kd_low_flat", self._score_kd_low_flat(history)),
            ("close_above_midline", self._score_close_above_midline(history)),
            ("inst_consec", self._score_institutional_consec(proxy)),
            ("inst_net_trend", self._score_institutional_net_trend(proxy)),
            ("updown_volume", self._score_updown_volume(history)),
            ("market_strength", self._score_market_relative_strength(history, taiex_history)),
            ("proximity_resistance", self._score_proximity_to_resistance(history)),
            ("prior_advance", self._score_prior_advance(history)),
        ]

        for name, (pts, flags) in factors:
            breakdown[name] = pts
            raw += pts
            all_flags.extend(flags)

        raw_max = self._params.get("raw_max_pts", 150)
        score = min(100, round(raw / raw_max * 100))
        grade = self._grade(score)

        if grade is None:
            return None

        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        high60 = max(closes[-60:]) if len(closes) >= 60 else closes[-1]
        vols = [d.volume for d in sorted_h]
        avg20v = sum(vols[-20:]) / 20 if len(vols) >= 20 else 0
        avg5v = sum(vols[-5:]) / 5 if len(vols) >= 5 else 0
        vol_ratio = avg5v / avg20v if avg20v > 0 else 1.0

        return {
            "grade": grade,
            "score": score,
            "raw_pts": raw,
            "flags": all_flags,
            "score_breakdown": breakdown,
            "bb_pct": next((float(f.split(":")[1]) for f in all_flags if f.startswith("ACCUM_BB_PCT:")), None),
            "vol_ratio": round(vol_ratio, 2),
            "inst_consec_days": max(proxy.foreign_consecutive_buy_days, proxy.trust_consecutive_buy_days) if proxy and proxy.is_available else 0,
            "vs_60d_high_pct": round((closes[-1] / high60 - 1) * 100, 2) if high60 > 0 else 0.0,
            "consol_range_pct": next(
                (float(f.split(":")[1].replace("PCT", "")) for f in all_flags if f.startswith("ACCUM_RANGE:")),
                None
            ),
        }
