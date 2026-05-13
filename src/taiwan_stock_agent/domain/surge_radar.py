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
        # 爆量分級：5x+ 是主力啟動訊號（前提是 G3 已確保收盤在上半段）
        # 3-5x 為理想爆量（最佳 T+2 勝率帶），2-3x 為有效爆量，1.5-2x 為輕度放量
        if ratio >= 5.0:
            return f.get("vol_ratio_surge", 8), [f"VOL_SURGE:{ratio:.2f}x"]
        if ratio >= 3.0:
            return f.get("vol_ratio_ideal", 10), [f"VOL_IDEAL:{ratio:.2f}x"]
        if ratio >= 2.0:
            return f.get("vol_ratio_solid", 8), [f"VOL_SOLID:{ratio:.2f}x"]
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
        """Reward institutional buying — day 1 is the highest-value signal (起漲點)."""
        if proxy is None or not proxy.is_available:
            return 0, []
        f = self._params.get("factors", {})
        days = max(proxy.foreign_consecutive_buy_days, proxy.trust_consecutive_buy_days)
        if days == 1:
            return f.get("inst_buy_fresh_1d", 8), [f"INST_FRESH:{days}D"]
        if days == 2:
            return f.get("inst_buy_fresh_2d", 7), [f"INST_FRESH:{days}D"]
        if days >= 3:
            return f.get("inst_buy_fresh_3d", 6), [f"INST_FRESH:{days}D"]
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
        # Taiwan daily data has no intraday tick split, so total volume proxies up-volume.
        # On strong up-days selling pressure is low, so total ≈ up-volume (O'Neil proxy).
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
        # RSI > 70 on surge day = momentum confirmation, not overbought warning
        if rsi > 70:
            return f.get("rsi_breakout", 3), [f"RSI_BREAKOUT:{rsi}"]
        if rsi >= 55:
            return f.get("rsi_healthy", 5), [f"RSI_HEALTHY:{rsi}"]
        return 0, [f"RSI_WEAK:{rsi}"]

    def _score_bb_squeeze_breakout(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV]
    ) -> tuple[int, list[str]]:
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

        if recent_min <= p25 and current_width >= recent_min * 1.5:
            return f.get("bb_squeeze_strong", 8), [f"BB_SQUEEZE_BREAK:{label}"]
        if recent_min <= p40 and current_width >= recent_min * 1.3:
            return f.get("bb_squeeze_mild", 4), [f"BB_SQUEEZE_EXPAND:{label}"]
        return 0, [f"BB_WIDE:{current_width:.3f}"]

    def _score_margin_not_hot(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[int, list[str]]:
        """Margin utilization tiers: <15% cool (+4), 15-20% warm (+2), >20% hot (0)."""
        if proxy is None or not proxy.is_available:
            return 0, []
        util = proxy.margin_utilization_rate
        if util is None:
            return 0, []
        f = self._params.get("factors", {})
        if util < 0.15:
            return f.get("margin_not_hot", 4), [f"MARGIN_COOL:{util*100:.1f}%"]
        if util < 0.20:
            return f.get("margin_warm", 2), [f"MARGIN_WARM:{util*100:.1f}%"]
        return 0, [f"MARGIN_HOT:{util*100:.1f}%"]

    def _score_inst_synergy(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[int, list[str]]:
        """土洋合作 + 法人買超佔比。

        外資+投信同日雙買（土洋合作）: 籌碼最強訊號。
        法人買超佔比 (inst_buy_pct) = (外資+投信淨買) / 今日成交量。
        """
        if proxy is None or not proxy.is_available:
            return 0, []
        f = self._params.get("factors", {})
        pts = 0
        flags: list[str] = []

        if proxy.foreign_and_trust_both_buy:
            pts += f.get("inst_synergy_both", 5)
            flags.append("INST_SYNERGY")

        pct = proxy.inst_buy_pct
        if pct is not None and pct > 0:
            if pct >= 0.15:
                pts += f.get("inst_pct_high", 6)
                flags.append(f"INST_PCT_HIGH:{pct*100:.1f}%")
            elif pct >= 0.10:
                pts += f.get("inst_pct_mid", 4)
                flags.append(f"INST_PCT_MID:{pct*100:.1f}%")
            elif pct >= 0.05:
                pts += f.get("inst_pct_low", 2)
                flags.append(f"INST_PCT_LOW:{pct*100:.1f}%")

        return pts, flags

    def _score_margin_declining(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[int, list[str]]:
        """融資餘額今日下降 — 浮額持續被清洗，籌碼沉澱訊號。"""
        if proxy is None or not proxy.is_available:
            return 0, []
        if proxy.margin_balance_change < 0:
            f = self._params.get("factors", {})
            return f.get("margin_declining", 3), ["MARGIN_DECLINING"]
        return 0, []

    def _score_ownership_concentration(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[int, list[str]]:
        """集保大戶進、散戶退 — 週級籌碼集中度，雙向評分。

        加分：大戶增持 / 散戶退出
        扣分：散戶週增（主力尚未進場或正在出貨）
        聯合懲罰：融資高 + 散戶增 = 散戶用槓桿追高，最危險組合
        """
        if proxy is None or not proxy.is_available:
            return 0, []
        f = self._params.get("factors", {})
        pts = 0
        flags: list[str] = []

        large = proxy.large_holder_chg_pct
        retail = proxy.retail_holder_chg_pct

        # ── 加分 ──────────────────────────────────────────────────────────────
        if large is not None and large > 0:
            pts += f.get("chip_large_holder_up", 5)
            flags.append(f"CHIP_LARGE_UP:{large:+.2f}%")
        if retail is not None and retail < 0:
            pts += f.get("chip_retail_exit", 3)
            flags.append(f"CHIP_RETAIL_OUT:{retail:+.2f}%")

        # ── 扣分：散戶流入 ────────────────────────────────────────────────────
        if retail is not None and retail > 0:
            if retail > 0.5:
                pts += f.get("chip_retail_surge_penalty", -5)
                flags.append(f"CHIP_RETAIL_SURGE:{retail:+.2f}%")
            else:
                pts += f.get("chip_retail_in_penalty", -3)
                flags.append(f"CHIP_RETAIL_IN:{retail:+.2f}%")

        # ── 聯合懲罰：融資過熱 + 散戶增 ──────────────────────────────────────
        margin_util = proxy.margin_utilization_rate
        if (retail is not None and retail > 0
                and margin_util is not None and margin_util > 0.20):
            pts += f.get("retail_leverage_trap_penalty", -5)
            flags.append(f"RETAIL_LEVERAGE_TRAP:{margin_util*100:.1f}%")

        return pts, flags

    def _score_daytrade_penalty(
        self, proxy: TWSEChipProxy | None
    ) -> tuple[int, list[str]]:
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
    ) -> tuple[int, list[str]]:
        """Quality confirmer: close walking MA5 after surge indicates sustained demand."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        all_bars = sorted_h + [ohlcv]
        closes = pd.Series([d.close for d in all_bars])
        if len(closes) < 5:
            return 0, []
        ma5 = closes.rolling(5).mean()
        window = min(n, len(closes))
        close_win = closes.iloc[-window:]
        ma5_win = ma5.iloc[-window:]
        valid = ma5_win.notna()
        if valid.sum() == 0:
            return 0, []
        ratio = float((close_win[valid] >= ma5_win[valid]).mean())
        if ratio >= 0.8:
            return 2, ["MA5_WALK"]
        # Only penalise if surge-day close is itself below MA5 (downtrend still active).
        # Stocks recovering from a crash base will have a low historical ratio but their
        # surge-day close may already be above MA5 — don't penalise that breakout.
        current_ma5 = ma5.iloc[-1]
        if ratio < 0.5 and pd.notna(current_ma5) and ohlcv.close < current_ma5:
            return -1, ["MA5_BREAK"]
        return 0, []

    def _score_bb_upper_walk(
        self,
        history: list[DailyOHLCV],
        surge_day: int,
        n: int = 5,
        tolerance: float = 0.03,
    ) -> tuple[int, list[str]]:
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
                return -3, ["BB_UPPER_EXHAUSTION"]
            return 0, ["MOMENTUM_WALK"]
        return 0, []

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

    def _score_market_heat(self, ctx: dict | None) -> tuple[int, list[str]]:
        """Bonus from overnight market heat snapshot (industry 5d trend + concepts + intl).

        ctx keys (all optional):
            ind_5d_rank_pct  float  — industry 5d momentum percentile (0–100)
            accelerating     bool   — industry 1d > 5d/5 by > 0.5%
            hot_concepts     list   — concept keys with rank_pct >= 70
            intl_tailwind    int    — sum of overseas tailwind scores for this ticker
        """
        if not ctx:
            return 0, []
        pts, flags = 0, []
        f = self._params.get("factors", {})

        ind_rank = ctx.get("ind_5d_rank_pct", 0) or 0
        if ind_rank >= 80:
            pts += f.get("heat_ind_hot", 3)
            flags.append(f"IND_HEAT_HOT:{ind_rank:.0f}")
        elif ind_rank >= 60:
            pts += f.get("heat_ind_warm", 2)
            flags.append(f"IND_HEAT_WARM:{ind_rank:.0f}")

        if ctx.get("accelerating"):
            pts += f.get("heat_ind_accel", 2)
            flags.append("IND_ACCEL")

        hot_concepts = ctx.get("hot_concepts") or []
        if hot_concepts:
            pts += f.get("heat_concept", 3)
            flags.append(f"CONCEPT_HOT:{hot_concepts[0]}")

        intl = ctx.get("intl_tailwind", 0) or 0
        if intl > 0:
            bonus = min(intl, f.get("heat_intl_max", 2))
            pts += bonus
            flags.append(f"INTL_TAIL:+{intl}")

        return pts, flags

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
        breakdown: dict[str, int] = {}
        raw = 0

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
            ("ownership_concentration", self._score_ownership_concentration(proxy)),
            ("daytrade_penalty", self._score_daytrade_penalty(proxy)),
            ("bb_squeeze", self._score_bb_squeeze_breakout(ohlcv, history)),
            ("ma5_walk", self._score_ma5_walk(ohlcv, history)),
            ("bb_upper_walk", self._score_bb_upper_walk(history, consec)),
            ("market_heat", self._score_market_heat(heat_context)),
        ]

        for name, (pts, flags) in factors:
            breakdown[name] = pts
            raw += pts
            all_flags.extend(flags)

        raw_max = self._params.get("raw_max_pts", 87)
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
