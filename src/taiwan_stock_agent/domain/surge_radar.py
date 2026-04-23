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
    ) -> tuple[int, list[str]]:
        vol_20ma = self._vol_20ma(history)
        if vol_20ma <= 0:
            return 0, []
        ratio = ohlcv.volume / vol_20ma
        f = self._params.get("factors", {})
        if ratio >= 3.0:
            return f.get("vol_ratio_extreme_warn", 5), [f"VOL_EXTREME:{ratio:.2f}x"]
        if ratio >= 2.0:
            return f.get("vol_ratio_ideal", 10), [f"VOL_IDEAL:{ratio:.2f}x"]
        if ratio >= 1.5:
            return f.get("vol_ratio_mild", 6), [f"VOL_MILD:{ratio:.2f}x"]
        return 0, [f"VOL_LOW:{ratio:.2f}x"]

    def _score_close_strength(self, ohlcv: DailyOHLCV) -> tuple[int, list[str]]:
        bar_range = ohlcv.high - ohlcv.low
        if bar_range <= 0:
            return 0, []
        ratio = (ohlcv.close - ohlcv.low) / bar_range
        f = self._params.get("factors", {})
        if ratio >= 0.8:
            return f.get("close_strong", 8), [f"CLOSE_STRONG:{ratio:.2f}"]
        if ratio >= 0.6:
            return f.get("close_healthy", 5), [f"CLOSE_HEALTHY:{ratio:.2f}"]
        return f.get("close_soft", 2), [f"CLOSE_SOFT:{ratio:.2f}"]

    def _score_inst_buy_fresh(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[int, list[str]]:
        """Reward 1-3 day consecutive inst buying (fresh ignition, not late-stage)."""
        if proxy is None or not proxy.is_available:
            return 0, []
        f = self._params.get("factors", {})
        days = max(proxy.foreign_consecutive_buy_days, proxy.trust_consecutive_buy_days)
        if days >= 3:
            return f.get("inst_buy_fresh_3d", 10), [f"INST_FRESH:{days}D"]
        if days == 2:
            return f.get("inst_buy_fresh_2d", 7), [f"INST_FRESH:{days}D"]
        if days == 1:
            return f.get("inst_buy_fresh_1d", 4), [f"INST_FRESH:{days}D"]
        return 0, []

    def _score_industry_strength(
        self, industry_rank_pct: float | None
    ) -> tuple[int, list[str]]:
        """Reward stocks in industries trading hot today.

        industry_rank_pct: percentile rank of stock's industry in today's
        industry heat (0 = weakest, 100 = strongest).
        """
        if industry_rank_pct is None:
            return 0, []
        f = self._params.get("factors", {})
        if industry_rank_pct >= 80:
            return f.get("industry_top_20pct", 10), [f"IND_HOT:{industry_rank_pct:.0f}"]
        if industry_rank_pct >= 60:
            return f.get("industry_top_40pct", 5), [f"IND_WARM:{industry_rank_pct:.0f}"]
        return 0, [f"IND_COLD:{industry_rank_pct:.0f}"]

    def _score_pocket_pivot(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> tuple[int, list[str]]:
        """Pocket pivot: today's up-volume > max down-volume in last 10 days,
        close in upper half, price above MA10."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_h) < 11:
            return 0, []
        last10 = sorted_h[-10:]
        down_vols = [
            b.volume for i, b in enumerate(last10)
            if i > 0 and b.close < last10[i - 1].close
        ]
        if not down_vols:
            return 0, []
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
            return f.get("pocket_pivot", 12), ["POCKET_PIVOT"]
        return 0, []

    def _score_breakaway_gap(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> tuple[int, list[str]]:
        """Gap-up with follow-through: open > prev_close*1.01, low > prev_close,
        close > open (gap held)."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if not sorted_h:
            return 0, []
        prev_close = sorted_h[-1].close
        if prev_close <= 0:
            return 0, []
        gap_pct = (ohlcv.open / prev_close - 1) * 100
        f = self._params.get("factors", {})
        if gap_pct >= 1.0 and ohlcv.low > prev_close and ohlcv.close > ohlcv.open:
            return f.get("breakaway_gap_full", 8), [f"GAP_FULL:{gap_pct:.1f}%"]
        if gap_pct >= 0.5 and ohlcv.close > ohlcv.open:
            return f.get("breakaway_gap_partial", 4), [f"GAP_PARTIAL:{gap_pct:.1f}%"]
        return 0, []

    def _score_relative_strength(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV], taiex_history: list[DailyOHLCV]
    ) -> tuple[int, list[str]]:
        """Stock's today-return > TAIEX today-return by >= 0.5%."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        sorted_t = sorted(taiex_history, key=lambda x: x.trade_date)
        if not sorted_h or len(sorted_t) < 2:
            return 0, []
        stock_prev = sorted_h[-1].close
        if stock_prev <= 0:
            return 0, []
        stock_chg = (ohlcv.close / stock_prev - 1) * 100

        taiex_prev, taiex_today = sorted_t[-2].close, sorted_t[-1].close
        if taiex_prev <= 0:
            return 0, []
        taiex_chg = (taiex_today / taiex_prev - 1) * 100

        diff = stock_chg - taiex_chg
        if diff >= 0.5:
            f = self._params.get("factors", {})
            return f.get("relative_strength", 8), [f"RS:+{diff:.1f}%"]
        return 0, [f"RS:{diff:+.1f}%"]

    def _score_breakout_20d(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> tuple[int, list[str]]:
        """Close breaks above max(high) of last 20 bars (excluding today)."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        if len(sorted_h) < 20:
            return 0, []
        prior_20d_high = max(b.high for b in sorted_h[-20:])
        if ohlcv.close > prior_20d_high:
            f = self._params.get("factors", {})
            return f.get("breakout_20d", 10), [f"BREAKOUT_20D:{ohlcv.close:.2f}>{prior_20d_high:.2f}"]
        return 0, []

    def _score_rsi_healthy(
        self, history: list[DailyOHLCV]
    ) -> tuple[int, list[str]]:
        rsi = self._rsi(history)
        if rsi is None:
            return 0, []
        f = self._params.get("factors", {})
        if 55 <= rsi <= 70:
            return f.get("rsi_healthy", 5), [f"RSI_HEALTHY:{rsi}"]
        if rsi > 70:
            return 0, [f"RSI_HOT:{rsi}"]
        return 0, [f"RSI_WEAK:{rsi}"]

    def _score_margin_not_hot(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[int, list[str]]:
        """Margin utilization not overheated (< 15%) — crowd not piled in yet."""
        if proxy is None or not proxy.is_available:
            return 0, []
        util = proxy.margin_utilization_rate
        if util is None:
            return 0, []
        f = self._params.get("factors", {})
        if util < 0.15:
            return f.get("margin_not_hot", 4), [f"MARGIN_COOL:{util*100:.1f}%"]
        return 0, [f"MARGIN_HOT:{util*100:.1f}%"]

    # ------------------------------------------------------------------
    # Grade + aggregate
    # ------------------------------------------------------------------

    def _grade(self, score: int) -> str | None:
        t = self._params.get("grade_thresholds", {})
        if score >= t.get("SURGE_ALPHA", 55):
            return "SURGE_ALPHA"
        if score >= t.get("SURGE_BETA", 40):
            return "SURGE_BETA"
        if score >= t.get("SURGE_GAMMA", 28):
            return "SURGE_GAMMA"
        return None

    def score_full(
        self,
        ohlcv: DailyOHLCV,
        history: list[DailyOHLCV],
        proxy: TWSEChipProxy | None,
        taiex_regime: str,
        taiex_history: list[DailyOHLCV],
        turnover_20ma: float,
        industry_rank_pct: float | None = None,
    ) -> dict | None:
        """Returns dict if passes gates AND grade >= SURGE_GAMMA, else None."""
        passed, gate_flags = self._gate_check(ohlcv, history, taiex_regime, turnover_20ma)
        if not passed:
            return None

        all_flags: list[str] = gate_flags[:]
        breakdown: dict[str, int] = {}
        raw = 0

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
        ]

        for name, (pts, flags) in factors:
            breakdown[name] = pts
            raw += pts
            all_flags.extend(flags)

        raw_max = self._params.get("raw_max_pts", 85)
        score = min(100, round(raw / raw_max * 100))
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
        consec = self._consecutive_surge_days(ohlcv, history)

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
            "inst_consec_days": max(
                proxy.foreign_consecutive_buy_days, proxy.trust_consecutive_buy_days
            ) if proxy and proxy.is_available else 0,
        }
