"""PullbackDetector — finds stocks in uptrend that have pulled back to MA20.

Setup criteria:
  Gate 1: MA5 > MA20 > MA60 (confirmed uptrend)
  Gate 2: Current close within MA20 ±5%  (at pullback support zone)
  Gate 3: Touched upper BB within last 20 days  (had momentum)

Score factors (0–100+):
  MA20 proximity      0–30 pts  (tiered: ±1.5% / ±3% / ±5%)
  Volume contraction  0–20 pts  (healthy shrink during pullback)
  MA20 slope          0–20 pts  (trend strength)
  Bounce candle       0–20 pts  (reversal signal + volume confirm)
  MA60 slope          0–15 pts  (long-term trend)
  RSI reset           0–15 pts  (momentum cool-down, not trend break)
  Prior advance       0–10 pts  (earned enough profit to defend)
  Pullback duration   0–10 pts  (sweet spot: 3–7 days)
"""
from __future__ import annotations

from statistics import mean, stdev

from taiwan_stock_agent.domain.models import DailyOHLCV


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[-period - 1 + i] - closes[-period - 2 + i]
        (gains if d > 0 else losses).append(abs(d))
    avg_gain = mean(gains) if gains else 0.0
    avg_loss = mean(losses) if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


class PullbackDetector:
    # Gate 2 threshold widened: ±3% → ±5%
    _MA20_GATE = 0.05
    # Gate 3 lookback widened: 10 → 20 bars
    _BB_LOOKBACK = 20
    # Minimum score to emit a signal
    MIN_SCORE = 40

    def score(self, history: list[DailyOHLCV]) -> dict | None:
        """Return score dict or None if gates not met.

        Required: len(history) >= 65 (60 for BB + 5 for MA5).
        """
        if len(history) < 65:
            return None

        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        highs = [d.high for d in sorted_h]
        vols = [d.volume for d in sorted_h]
        close = closes[-1]

        # ── Gate 1: MA alignment ──────────────────────────────────────────
        ma5 = mean(closes[-5:])
        ma20 = mean(closes[-20:])
        ma60 = mean(closes[-60:])
        if not (ma5 > ma20 > ma60):
            return None

        # ── Gate 2: price within MA20 ±5% ────────────────────────────────
        ma20_pct = (close - ma20) / ma20
        if abs(ma20_pct) > self._MA20_GATE:
            return None

        # ── Gate 3: touched upper BB within last _BB_LOOKBACK bars ───────
        n = len(closes)
        upper_bb_touched = False
        for i in range(n - self._BB_LOOKBACK, n):
            window = closes[max(0, i - 19):i + 1]
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

        # 1. MA20 proximity — tiered (0–30 pts)
        abs_pct = abs(ma20_pct)
        if abs_pct <= 0.015:
            score += 30
            flags.append("MA20_TIGHT")
        elif abs_pct <= 0.03:
            score += 20
        else:
            score += 10   # ±3–5% zone

        # 2. Volume contraction during pullback (0–20 pts)
        avg_vol = mean(vols[-20:])
        pullback_vol = mean(vols[-3:])
        if avg_vol > 0:
            vr = pullback_vol / avg_vol
            if vr < 0.5:
                score += 20
                flags.append("VOL_CONTRACTION_STRONG")
            elif vr < 0.7:
                score += 12
                flags.append("VOL_CONTRACTION")
            elif vr > 1.3:
                score -= 5
                flags.append("VOL_EXPANDING_BEARISH")

        # 3. MA20 slope — uptrend strength (0–20 pts)
        ma20_prev = mean(closes[-40:-20])
        slope = (ma20 - ma20_prev) / ma20_prev if ma20_prev > 0 else 0.0
        if slope > 0.02:
            score += 20
            flags.append("STRONG_UPTREND")
        elif slope > 0.005:
            score += 10
            flags.append("UPTREND")

        # 4. Bounce candle + volume confirm (0–20 pts)
        bar = sorted_h[-1]
        is_green = bar.close > bar.open
        rng = bar.high - bar.low
        close_strength = (bar.close - bar.low) / rng if rng > 0 else 0.0

        if is_green:
            score += 8
            flags.append("BOUNCE_CANDLE")
        if close_strength > 0.6:
            score += 7
            flags.append("STRONG_CLOSE")
        elif close_strength > 0.3:
            score += 3
            flags.append("LONG_LOWER_SHADOW")

        # Volume expanding on bounce day → confirms buyers stepping in
        if len(vols) >= 2 and vols[-1] > vols[-2] * 1.15 and is_green:
            score += 5
            flags.append("VOL_BOUNCE")

        # 5. MA60 uptrend (0–15 pts)
        if len(closes) >= 80:
            ma60_prev = mean(closes[-80:-60])
            slope60 = (ma60 - ma60_prev) / ma60_prev if ma60_prev > 0 else 0.0
            if slope60 > 0.01:
                score += 15
                flags.append("LONG_TERM_UPTREND")
            elif slope60 > 0:
                score += 5

        # 6. RSI reset — momentum cool-down without trend break (0–15 pts)
        rsi_now = _rsi(closes, 14)
        # Look for prior RSI peak in last 30 bars
        rsi_peak = max(
            _rsi(closes[:-(30 - i)], 14) if len(closes) > 30 - i + 15 else 50.0
            for i in range(30)
        ) if len(closes) >= 45 else rsi_now

        if rsi_now < 40:
            score -= 10
            flags.append("RSI_OVERSOLD")
        elif 42 <= rsi_now <= 58 and rsi_peak >= 60:
            score += 15
            flags.append(f"RSI_RESET:{rsi_now:.0f}")
        elif 58 < rsi_now <= 68:
            score += 8
            flags.append(f"RSI_HEALTHY:{rsi_now:.0f}")
        elif rsi_now > 68:
            # Still hot — may not have pulled back enough
            score -= 5
            flags.append("RSI_HOT")

        # 7. Prior advance — did the stock earn profit to defend? (0–10 pts)
        if len(closes) >= 30:
            low_30 = min(closes[-30:])
            high_30 = max(closes[-30:])
            advance_pct = (high_30 - low_30) / low_30 if low_30 > 0 else 0.0
            if advance_pct >= 0.25:
                score += 10
                flags.append(f"PRIOR_ADVANCE:{advance_pct:.0%}")
            elif advance_pct >= 0.12:
                score += 5
                flags.append(f"PRIOR_ADVANCE:{advance_pct:.0%}")

        # 8. Pullback duration — sweet spot is 3–7 trading days (0–10 pts)
        pullback_days = 0
        for i in range(1, min(15, len(sorted_h))):
            bar_i = sorted_h[-(i + 1)]
            # Walk back while price is below the local high
            if sorted_h[-i].close < sorted_h[-i - 1].close or sorted_h[-i].close < ma20:
                pullback_days += 1
            else:
                break
        if 3 <= pullback_days <= 7:
            score += 10
            flags.append(f"PULLBACK_DAYS:{pullback_days}")
        elif pullback_days == 2:
            score += 4
            flags.append(f"PULLBACK_DAYS:{pullback_days}")
        elif pullback_days >= 10:
            score -= 8
            flags.append(f"PULLBACK_LONG:{pullback_days}")

        flags.append(f"PULLBACK_MA20_DIST:{ma20_pct:+.1%}")

        return {
            "score": max(0, score),   # no upper cap — consistent with TCE
            "flags": flags,
            "ma20_pct": round(ma20_pct * 100, 2),
            "ma20": round(ma20, 2),
            "ma5": round(ma5, 2),
            "ma60": round(ma60, 2),
            "rsi": round(rsi_now, 1),
            "pullback_days": pullback_days,
        }
