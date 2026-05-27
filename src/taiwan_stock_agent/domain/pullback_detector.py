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

        # ── Scoring — Phase 4.46 continuous ──────────────────────────────
        flags: list[str] = ["PULLBACK_MA20"]
        score = 0.0

        # 1. MA20 proximity — continuous on distance from MA20
        # 0%→30, ±1.5%→25, ±3%→20, ±5%→10 linear
        abs_pct = abs(ma20_pct)
        if abs_pct <= 0.015:
            # 0→30, 0.015→25 (closer = better)
            score += round(30.0 - abs_pct / 0.015 * 5.0, 2)
            flags.append("MA20_TIGHT")
        elif abs_pct <= 0.03:
            # 0.015→25, 0.03→20 linear
            score += round(25.0 - (abs_pct - 0.015) / 0.015 * 5.0, 2)
        else:
            # 0.03→20, 0.05→10 linear
            score += round(20.0 - (abs_pct - 0.03) / 0.02 * 10.0, 2)

        # 2. Volume contraction — continuous on ratio
        avg_vol = mean(vols[-20:])
        pullback_vol = mean(vols[-3:])
        if avg_vol > 0:
            vr = pullback_vol / avg_vol
            if vr < 0.5:
                # 0.3→20, 0.5→12 linear
                if vr <= 0.3:
                    score += 20.0
                else:
                    score += round(12.0 + (0.5 - vr) / 0.2 * 8.0, 2)
                flags.append("VOL_CONTRACTION_STRONG")
            elif vr < 0.7:
                score += round((0.7 - vr) / 0.2 * 12.0, 2)
                flags.append("VOL_CONTRACTION")
            elif vr > 1.3:
                # 1.3→-3, 2.0+→-5
                if vr >= 2.0:
                    score -= 5.0
                else:
                    score -= round(3.0 + (vr - 1.3) / 0.7 * 2.0, 2)
                flags.append("VOL_EXPANDING_BEARISH")

        # 3. MA20 slope — continuous on magnitude
        ma20_prev = mean(closes[-40:-20])
        slope = (ma20 - ma20_prev) / ma20_prev if ma20_prev > 0 else 0.0
        if slope > 0.02:
            score += 20.0
            flags.append("STRONG_UPTREND")
        elif slope > 0.005:
            # 0.005→10, 0.02→20 linear
            score += round(10.0 + (slope - 0.005) / 0.015 * 10.0, 2)
            flags.append("UPTREND")
        elif slope > 0:
            # 0→0, 0.005→10 linear
            score += round(slope / 0.005 * 10.0, 2)

        # 4. Bounce candle + volume confirm
        bar = sorted_h[-1]
        is_green = bar.close > bar.open
        rng = bar.high - bar.low
        close_strength = (bar.close - bar.low) / rng if rng > 0 else 0.0

        if is_green:
            score += 8.0
            flags.append("BOUNCE_CANDLE")
        # Close strength — continuous taper
        if close_strength > 0.3:
            # 0.3→3, 0.6→7, 1.0→7 (cap)
            if close_strength >= 0.6:
                score += 7.0
                flags.append("STRONG_CLOSE")
            else:
                score += round(3.0 + (close_strength - 0.3) / 0.3 * 4.0, 2)
                flags.append("LONG_LOWER_SHADOW")

        if len(vols) >= 2 and vols[-1] > vols[-2] * 1.15 and is_green:
            # 1.15x→3, 1.5x+→5 linear
            ratio = vols[-1] / vols[-2]
            if ratio >= 1.5:
                score += 5.0
            else:
                score += round(3.0 + (ratio - 1.15) / 0.35 * 2.0, 2)
            flags.append("VOL_BOUNCE")

        # 5. MA60 uptrend — continuous on slope
        if len(closes) >= 80:
            ma60_prev = mean(closes[-80:-60])
            slope60 = (ma60 - ma60_prev) / ma60_prev if ma60_prev > 0 else 0.0
            if slope60 > 0.01:
                score += 15.0
                flags.append("LONG_TERM_UPTREND")
            elif slope60 > 0:
                # 0→0, 0.01→15 linear (but cap at 5 in old version — keep similar)
                score += round(slope60 / 0.01 * 5.0 + 5.0, 2)

        # 6. RSI reset — continuous on RSI distance from sweet spot
        rsi_now = _rsi(closes, 14)
        rsi_peak = max(
            _rsi(closes[:-(30 - i)], 14) if len(closes) > 30 - i + 15 else 50.0
            for i in range(30)
        ) if len(closes) >= 45 else rsi_now

        if rsi_now < 40:
            # 40→0, 30+→-10 linear
            if rsi_now <= 30:
                score -= 10.0
            else:
                score -= round((40 - rsi_now) / 10.0 * 10.0, 2)
            flags.append("RSI_OVERSOLD")
        elif 42 <= rsi_now <= 58 and rsi_peak >= 60:
            # 42→10, 50→15 peak, 58→10 (tent)
            if 48 <= rsi_now <= 52:
                score += 15.0
            elif rsi_now < 48:
                score += round(10.0 + (rsi_now - 42) / 6.0 * 5.0, 2)
            else:
                score += round(10.0 + (58 - rsi_now) / 6.0 * 5.0, 2)
            flags.append(f"RSI_RESET:{rsi_now:.0f}")
        elif 58 < rsi_now <= 68:
            # 58→8, 68→0 linear
            score += round((68 - rsi_now) / 10.0 * 8.0, 2)
            flags.append(f"RSI_HEALTHY:{rsi_now:.0f}")
        elif rsi_now > 68:
            # 68→0, 80+→-5 linear
            if rsi_now >= 80:
                score -= 5.0
            else:
                score -= round((rsi_now - 68) / 12.0 * 5.0, 2)
            flags.append("RSI_HOT")

        # 7. Prior advance — continuous
        if len(closes) >= 30:
            low_30 = min(closes[-30:])
            high_30 = max(closes[-30:])
            advance_pct = (high_30 - low_30) / low_30 if low_30 > 0 else 0.0
            if advance_pct >= 0.25:
                score += 10.0
                flags.append(f"PRIOR_ADVANCE:{advance_pct:.0%}")
            elif advance_pct >= 0.12:
                # 0.12→5, 0.25→10 linear
                score += round(5.0 + (advance_pct - 0.12) / 0.13 * 5.0, 2)
                flags.append(f"PRIOR_ADVANCE:{advance_pct:.0%}")
            elif advance_pct > 0:
                score += round(advance_pct / 0.12 * 5.0, 2)

        # 8. Pullback duration — continuous tent at sweet spot
        pullback_days = 0
        for i in range(1, min(15, len(sorted_h))):
            if sorted_h[-i].close < sorted_h[-i - 1].close or sorted_h[-i].close < ma20:
                pullback_days += 1
            else:
                break
        if 3 <= pullback_days <= 7:
            # tent: 5 days peak, taper at ends
            if 4 <= pullback_days <= 6:
                score += 10.0
            elif pullback_days == 3:
                score += 8.0
            else:  # 7
                score += 8.0
            flags.append(f"PULLBACK_DAYS:{pullback_days}")
        elif pullback_days == 2:
            score += 4.0
            flags.append(f"PULLBACK_DAYS:{pullback_days}")
        elif pullback_days >= 10:
            # 10→-5, 15+→-8 linear
            if pullback_days >= 15:
                score -= 8.0
            else:
                score -= round(5.0 + (pullback_days - 10) / 5.0 * 3.0, 2)
            flags.append(f"PULLBACK_LONG:{pullback_days}")
        elif pullback_days == 8 or pullback_days == 9:
            score += 5.0
            flags.append(f"PULLBACK_DAYS:{pullback_days}")

        flags.append(f"PULLBACK_MA20_DIST:{ma20_pct:+.1%}")

        score = round(score, 2)
        return {
            "score": round(max(0.0, score), 2),   # no upper cap — consistent with TCE
            "flags": flags,
            "ma20_pct": round(ma20_pct * 100, 2),
            "ma20": round(ma20, 2),
            "ma5": round(ma5, 2),
            "ma60": round(ma60, 2),
            "rsi": round(rsi_now, 1),
            "pullback_days": pullback_days,
        }
