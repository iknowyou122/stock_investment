from __future__ import annotations

from pathlib import Path

import pandas as pd

from taiwan_stock_agent.domain.models import DailyOHLCV

_PARAMS_PATH = Path(__file__).resolve().parents[3] / "config" / "accumulation_params.json"


class AccumulationEngine:

    @staticmethod
    def _obv_slope(history: list[DailyOHLCV]) -> float | None:
        """5-day linear slope of OBV. Returns None if < 5 bars."""
        if len(history) < 5:
            return None
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        obv = 0.0
        obvs = []
        prev_close = sorted_h[0].close
        for bar in sorted_h:
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
        """Average True Range over `period` bars. Returns None if insufficient history."""
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
