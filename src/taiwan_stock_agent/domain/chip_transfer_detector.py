"""ChipTransferDetector — retail-to-smart-money chip transfer.

Gates (K-of-N: at least 3 of 5 signals present):
  Signal A: margin_persist_decline (proxy.margin_decline_streak >= 5 OR estimate from proxy)
  Signal B: price stable during decline (20D price range / MA20 < 10%)
  Signal C: institution consecutive buy >= 3 (foreign or trust)
  Signal D: large holder pct increasing (proxy.large_holder_chg_pct > 0)
  Signal E: retail holder pct declining (proxy.retail_holder_chg_pct < 0)

Score factors:
  Margin decline days     0–20 pts
  Price stability         0–20 pts  (tighter range = more pts)
  Institution buy days    0–20 pts
  Large holder increase   0–15 pts
  Retail decrease         0–15 pts
  Volume contraction      0–10 pts

If proxy is None, score from OHLCV only (price stability + volume contraction).
K-of-N threshold lowers to 2-of-3 for OHLCV-only mode.

MIN_SCORE = 40
"""
from __future__ import annotations

from statistics import mean

from taiwan_stock_agent.domain.models import DailyOHLCV, TWSEChipProxy


class ChipTransferDetector:
    MIN_SCORE = 40

    def score(
        self,
        history: list[DailyOHLCV],
        proxy: TWSEChipProxy | None = None,
    ) -> dict | None:
        """Return score dict or None if K-of-N gate not met.

        Required: len(history) >= 25 (20 for range + 5 for MA5).
        """
        if len(history) < 25:
            return None

        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        vols = [d.volume for d in sorted_h]
        close = closes[-1]

        # ── Compute MA20 for price stability check ────────────────────────
        ma20 = mean(closes[-20:])
        if ma20 <= 0:
            return None

        # ── Price range stability (Signal B) ─────────────────────────────
        high_20 = max(d.high for d in sorted_h[-20:])
        low_20 = min(d.low for d in sorted_h[-20:])
        price_range_pct = (high_20 - low_20) / ma20 if ma20 > 0 else 1.0
        signal_b = price_range_pct < 0.10

        # ── Volume contraction ────────────────────────────────────────────
        avg_vol_20 = mean(vols[-20:]) if vols else 0
        avg_vol_5 = mean(vols[-5:]) if len(vols) >= 5 else avg_vol_20
        vol_ratio = (avg_vol_5 / avg_vol_20) if avg_vol_20 > 0 else 1.0

        if proxy is None:
            # OHLCV-only mode: K-of-3 with at most 3 signals (B + vol contraction + MA)
            signals_met = []
            if signal_b:
                signals_met.append("B")
            if vol_ratio < 0.70:
                signals_met.append("VOL")

            # Check if MA5 > MA20 (subtle bullish tilt)
            if len(closes) >= 20:
                ma5 = mean(closes[-5:])
                if ma5 > ma20:
                    signals_met.append("MA")

            if len(signals_met) < 2:
                return None

            # Scoring in OHLCV-only mode — Phase 4.46 continuous
            flags: list[str] = ["CHIP_TRANSFER"]
            score = 0.0

            # Price stability — 0.10→0, 0.05→12, 0.02→20 linear
            if price_range_pct < 0.05:
                if price_range_pct <= 0.02:
                    score += 20.0
                else:
                    score += round(12.0 + (0.05 - price_range_pct) / 0.03 * 8.0, 2)
                flags.append(f"PRICE_STABLE_TIGHT:{price_range_pct:.1%}")
            elif signal_b:
                # 0.10→0, 0.05→12 linear
                score += round((0.10 - price_range_pct) / 0.05 * 12.0, 2)
                flags.append(f"PRICE_STABLE:{price_range_pct:.1%}")

            # Volume contraction — 0.70→0, 0.50→6, 0.30→10 linear
            if vol_ratio < 0.50:
                if vol_ratio <= 0.30:
                    score += 10.0
                else:
                    score += round(6.0 + (0.50 - vol_ratio) / 0.20 * 4.0, 2)
                flags.append("VOL_CONTRACT_STRONG")
            elif vol_ratio < 0.70:
                score += round((0.70 - vol_ratio) / 0.20 * 6.0, 2)
                flags.append("VOL_CONTRACT")

            score = round(score, 2)
            if score < self.MIN_SCORE:
                return None

            return {
                "score": round(max(0.0, score), 2),
                "flags": flags,
                "price_range_pct": round(price_range_pct * 100, 1),
                "vol_ratio": round(vol_ratio, 2),
                "signals_met": signals_met,
                "signal_type": "籌碼轉移",
                "horizon": "波段",
            }

        # ── Full mode with proxy ──────────────────────────────────────────
        signals_met: list[str] = []

        # Signal A: margin decline streak
        margin_streak = proxy.margin_decline_streak
        signal_a = margin_streak >= 5
        if signal_a:
            signals_met.append("A")

        if signal_b:
            signals_met.append("B")

        # Signal C: institution consecutive buy >= 3
        consec_days = max(
            proxy.foreign_consecutive_buy_days,
            proxy.trust_consecutive_buy_days,
        )
        signal_c = consec_days >= 3
        if signal_c:
            signals_met.append("C")

        # Signal D: large holder pct increasing
        signal_d = proxy.large_holder_chg_pct is not None and proxy.large_holder_chg_pct > 0
        if signal_d:
            signals_met.append("D")

        # Signal E: retail holder pct declining
        signal_e = proxy.retail_holder_chg_pct is not None and proxy.retail_holder_chg_pct < 0
        if signal_e:
            signals_met.append("E")

        # K-of-N gate: 3 of 5
        if len(signals_met) < 3:
            return None

        # ── Scoring — Phase 4.46 continuous ──────────────────────────────
        flags = ["CHIP_TRANSFER"]
        score = 0.0

        # 1. Margin decline days — 0→0, 5→12, 10+→20 linear
        if margin_streak >= 10:
            score += 20.0
            flags.append(f"MARGIN_DECLINE:{margin_streak}d")
        elif margin_streak >= 5:
            score += round(12.0 + (margin_streak - 5) / 5.0 * 8.0, 2)
            flags.append(f"MARGIN_DECLINE:{margin_streak}d")
        elif proxy.margin_balance_change < 0:
            score += 5.0
            flags.append("MARGIN_DECLINING_TODAY")

        # 2. Price stability — same continuous as OHLCV-only mode
        if price_range_pct < 0.05:
            if price_range_pct <= 0.02:
                score += 20.0
            else:
                score += round(12.0 + (0.05 - price_range_pct) / 0.03 * 8.0, 2)
            flags.append(f"PRICE_STABLE_TIGHT:{price_range_pct:.1%}")
        elif signal_b:
            score += round((0.10 - price_range_pct) / 0.05 * 12.0, 2)
            flags.append(f"PRICE_STABLE:{price_range_pct:.1%}")

        # 3. Institution buy days — 3→8, 5→14, 8+→20 linear
        if consec_days >= 8:
            score += 20.0
            flags.append(f"INST_CONSEC:{consec_days}d")
        elif consec_days >= 5:
            score += round(14.0 + (consec_days - 5) / 3.0 * 6.0, 2)
            flags.append(f"INST_CONSEC:{consec_days}d")
        elif signal_c:
            score += round(8.0 + (consec_days - 3) / 2.0 * 6.0, 2)
            flags.append(f"INST_CONSEC:{consec_days}d")

        # 4. Large holder increase — 0→0, 1%→8, 2%+→15 linear
        if signal_d:
            chg = proxy.large_holder_chg_pct or 0.0
            if chg > 1.0:
                # 1.0→8 floor, 2.0→15
                if chg >= 2.0:
                    score += 15.0
                else:
                    score += round(8.0 + (chg - 1.0) / 1.0 * 7.0, 2)
                flags.append(f"LARGE_HOLDER_ACCUM:{chg:+.1f}%")
            else:
                # 0→0, 1.0→8 linear
                score += round(chg / 1.0 * 8.0, 2)
                flags.append(f"LARGE_HOLDER_INCR:{chg:+.1f}%")

        # 5. Retail decrease — 0→0, -1%→8, -2%+→15 linear
        if signal_e:
            chg = proxy.retail_holder_chg_pct or 0.0
            if chg < -1.0:
                if chg <= -2.0:
                    score += 15.0
                else:
                    score += round(8.0 + (-chg - 1.0) / 1.0 * 7.0, 2)
                flags.append(f"RETAIL_EXIT:{chg:+.1f}%")
            else:
                score += round((-chg) / 1.0 * 8.0, 2)
                flags.append(f"RETAIL_DECLINE:{chg:+.1f}%")

        # 6. Volume contraction — same continuous as OHLCV-only mode
        if vol_ratio < 0.50:
            if vol_ratio <= 0.30:
                score += 10.0
            else:
                score += round(6.0 + (0.50 - vol_ratio) / 0.20 * 4.0, 2)
            flags.append("VOL_CONTRACT_STRONG")
        elif vol_ratio < 0.70:
            score += round((0.70 - vol_ratio) / 0.20 * 6.0, 2)
            flags.append("VOL_CONTRACT")

        score = round(score, 2)
        if score < self.MIN_SCORE:
            return None

        return {
            "score": round(max(0.0, score), 2),
            "flags": flags,
            "price_range_pct": round(price_range_pct * 100, 1),
            "vol_ratio": round(vol_ratio, 2),
            "margin_streak": margin_streak,
            "consec_days": consec_days,
            "signals_met": signals_met,
            "signal_type": "籌碼轉移",
            "horizon": "波段",
        }
