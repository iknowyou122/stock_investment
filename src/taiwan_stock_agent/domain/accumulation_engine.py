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

        # G2: not yet broken out (max of last 10 closes < 60d high × 1.03)
        sixty_day_high = max(closes[-60:])
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
