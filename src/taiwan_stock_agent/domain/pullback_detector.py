"""PullbackDetector — finds stocks in uptrend that have pulled back to MA20.

Setup criteria:
  Gate 1: MA5 > MA20 > MA60 (confirmed uptrend)
  Gate 2: Current close within MA20 ±3%  (at pullback support)
  Gate 3: Touched upper BB within last 10 days  (had momentum)

Score factors (0–100):
  MA20 proximity   0–30 pts  (closer = better entry)
  Volume contraction 0–20 pts (healthy pullback)
  MA20 slope       0–20 pts  (trend strength)
  Bounce candle    0–15 pts  (reversal signal)
  MA60 slope       0–15 pts  (long-term trend)
"""
from __future__ import annotations

from statistics import mean, stdev

from taiwan_stock_agent.domain.models import DailyOHLCV


class PullbackDetector:
    def score(self, history: list[DailyOHLCV]) -> dict | None:
        """Return score dict or None if gates not met.

        Required: len(history) >= 65 (60 for BB + 5 for MA5).
        """
        if len(history) < 65:
            return None

        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        vols = [d.volume for d in sorted_h]
        close = closes[-1]

        # ── Gate 1: MA alignment ──────────────────────────────────────────
        ma5 = mean(closes[-5:])
        ma20 = mean(closes[-20:])
        ma60 = mean(closes[-60:])
        if not (ma5 > ma20 > ma60):
            return None

        # ── Gate 2: price within MA20 ±3% ────────────────────────────────
        ma20_pct = (close - ma20) / ma20
        if abs(ma20_pct) > 0.03:
            return None

        # ── Gate 3: touched upper BB in last 10 bars ──────────────────────
        upper_bb_touched = False
        n = len(closes)
        for i in range(n - 10, n):
            window = closes[max(0, i - 20):i]
            if len(window) < 5:
                continue
            bb_mid = mean(window)
            try:
                bb_std = stdev(window)
            except Exception:
                continue
            if closes[i] >= (bb_mid + 2 * bb_std) * 0.97:
                upper_bb_touched = True
                break
        if not upper_bb_touched:
            return None

        # ── Scoring ───────────────────────────────────────────────────────
        flags: list[str] = ["PULLBACK_MA20"]
        score = 0

        # 1. Proximity to MA20 (closer = better entry, 0–30 pts)
        score += int((1.0 - abs(ma20_pct) / 0.03) * 30)

        # 2. Volume contraction during pullback (0–20 pts)
        avg_vol = mean(vols[-20:])
        pullback_vol = mean(vols[-3:])
        if avg_vol > 0:
            vr = pullback_vol / avg_vol
            if vr < 0.6:
                score += 20
                flags.append("VOL_CONTRACTION_STRONG")
            elif vr < 0.8:
                score += 12
                flags.append("VOL_CONTRACTION")
            elif vr > 1.2:
                score -= 5
                flags.append("VOL_EXPANDING_BEARISH")

        # 3. MA20 slope — uptrend strength (0–20 pts)
        ma20_prev = mean(closes[-25:-5])
        slope = (ma20 - ma20_prev) / ma20_prev if ma20_prev > 0 else 0.0
        if slope > 0.02:
            score += 20
            flags.append("STRONG_UPTREND")
        elif slope > 0.005:
            score += 10
            flags.append("UPTREND")

        # 4. Bounce candle (0–15 pts)
        bar = sorted_h[-1]
        if bar.close > bar.open:
            score += 10
            flags.append("BOUNCE_CANDLE")
        rng = bar.high - bar.low
        if rng > 0 and (bar.close - bar.low) / rng > 0.3:
            score += 5
            flags.append("LONG_LOWER_SHADOW")

        # 5. MA60 uptrend (0–15 pts)
        if len(closes) >= 80:
            ma60_prev = mean(closes[-80:-60])
            slope60 = (ma60 - ma60_prev) / ma60_prev if ma60_prev > 0 else 0.0
            if slope60 > 0.01:
                score += 15
                flags.append("LONG_TERM_UPTREND")
            elif slope60 > 0:
                score += 5

        flags.append(f"PULLBACK_MA20_DIST:{ma20_pct:+.1%}")

        return {
            "score": max(0, min(100, score)),
            "flags": flags,
            "ma20_pct": round(ma20_pct * 100, 2),
            "ma20": round(ma20, 2),
            "ma5": round(ma5, 2),
            "ma60": round(ma60, 2),
        }
