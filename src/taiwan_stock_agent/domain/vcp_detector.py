"""VCPDetector — successive pullback contractions with volume dry-up.

Algorithm:
  1. Find local peaks (rolling high) in history[-80:]
  2. For each peak, find the subsequent trough (lowest point before next peak)
  3. Compute pullback_pct = (peak - trough) / peak for each contraction
  4. Check: pullback[i+1] < pullback[i] * 0.85 (each contraction smaller)
  5. Check: volume at each trough < volume at prior trough * 0.85
  6. Current state: in the latest contraction, near trough (within 8%)

Gates:
  Gate 1: at least 2 valid contractions identified
  Gate 2: latest pullback_pct < 15% (tight contraction)
  Gate 3: current close within 8% of latest trough LOW (still in base)
  Gate 4: MA5 > MA60 (longer uptrend intact)

Score factors:
  Contraction count       0–25 pts  (2=15, 3+=25)
  Current BB compression  0–20 pts  (BB width percentile rank vs 60D)
  Volume dry-up           0–20 pts  (trough vol ratio < 0.4x = 20)
  Pullback depth tightness 0–15 pts (latest pullback < 8% = 15, <12% = 8)
  MA alignment            0–15 pts

MIN_SCORE = 40
"""
from __future__ import annotations

from statistics import mean, stdev

from taiwan_stock_agent.domain.models import DailyOHLCV


def _local_peaks_troughs(
    sorted_h: list[DailyOHLCV],
    window: int = 5,
) -> tuple[list[tuple[int, float, float]], list[tuple[int, float, float]]]:
    """Return (peaks, troughs) as (index, close, avg_volume) tuples.

    A bar is a local peak if its high is the highest in its ±window neighborhood.
    A bar is a local trough if its low is the lowest in its ±window neighborhood.
    """
    n = len(sorted_h)
    peaks: list[tuple[int, float, float]] = []
    troughs: list[tuple[int, float, float]] = []

    for i in range(window, n - window):
        hi = sorted_h[i].high
        lo = sorted_h[i].low
        neighborhood_highs = [sorted_h[j].high for j in range(i - window, i + window + 1)]
        neighborhood_lows = [sorted_h[j].low for j in range(i - window, i + window + 1)]

        # local volume: avg of the bar ±2 neighbors
        vol_window = sorted_h[max(0, i - 2):i + 3]
        avg_vol = mean(v.volume for v in vol_window)

        if hi == max(neighborhood_highs):
            peaks.append((i, hi, avg_vol))
        if lo == min(neighborhood_lows):
            troughs.append((i, lo, avg_vol))

    return peaks, troughs


def _find_contractions(
    sorted_h: list[DailyOHLCV],
) -> list[dict]:
    """Find VCP contractions: alternating peak → trough sequences.

    Returns list of dicts with:
      peak_idx, peak_price, trough_idx, trough_price, pullback_pct, trough_avg_vol
    """
    peaks, troughs = _local_peaks_troughs(sorted_h, window=5)
    contractions: list[dict] = []

    for pi, (peak_idx, peak_price, _) in enumerate(peaks):
        # Find the trough that comes after this peak and before the next peak
        next_peak_idx = peaks[pi + 1][0] if pi + 1 < len(peaks) else len(sorted_h)
        candidate_troughs = [
            t for t in troughs
            if t[0] > peak_idx and t[0] < next_peak_idx
        ]
        if not candidate_troughs:
            continue

        # Take the deepest trough (lowest low) after this peak
        trough_idx, trough_price, trough_avg_vol = min(candidate_troughs, key=lambda t: t[1])
        pullback_pct = (peak_price - trough_price) / peak_price if peak_price > 0 else 0.0

        contractions.append({
            "peak_idx": peak_idx,
            "peak_price": peak_price,
            "trough_idx": trough_idx,
            "trough_price": trough_price,
            "pullback_pct": pullback_pct,
            "trough_avg_vol": trough_avg_vol,
        })

    return contractions


class VCPDetector:
    MIN_SCORE = 40

    def score(self, history: list[DailyOHLCV]) -> dict | None:
        """Return score dict or None if gates not met.

        Required: len(history) >= 80.
        """
        if len(history) < 80:
            return None

        sorted_h = sorted(history, key=lambda x: x.trade_date)
        # Use last 80 bars for VCP detection
        working = sorted_h[-80:]
        closes = [d.close for d in sorted_h]
        close = closes[-1]

        # ── Gate 4: MA5 > MA60 (uptrend intact) ──────────────────────────
        ma5 = mean(closes[-5:])
        ma60 = mean(closes[-60:])
        if ma5 <= ma60:
            return None

        # ── Find contractions ─────────────────────────────────────────────
        contractions = _find_contractions(working)

        # ── Gate 1: at least 2 valid contractions ────────────────────────
        if len(contractions) < 2:
            return None

        # Verify each contraction is smaller than the prior
        valid_contractions: list[dict] = [contractions[0]]
        for c in contractions[1:]:
            if c["pullback_pct"] < valid_contractions[-1]["pullback_pct"] * 0.85:
                valid_contractions.append(c)
            else:
                # Reset — start a new sequence from here
                valid_contractions = [c]

        if len(valid_contractions) < 2:
            return None

        latest = valid_contractions[-1]

        # ── Gate 2: latest pullback_pct < 15% ────────────────────────────
        if latest["pullback_pct"] >= 0.15:
            return None

        # ── Gate 3: current close within 8% of latest trough ─────────────
        trough_price = latest["trough_price"]
        if trough_price <= 0:
            return None
        dist_from_trough = (close - trough_price) / trough_price
        if dist_from_trough > 0.08:
            return None

        # ── Scoring ───────────────────────────────────────────────────────
        flags: list[str] = ["VCP"]
        score = 0

        # 1. Contraction count (0–25 pts)
        n_contractions = len(valid_contractions)
        if n_contractions >= 3:
            score += 25
            flags.append(f"VCP_{n_contractions}C")
        else:
            score += 15
            flags.append("VCP_2C")

        # 2. BB compression (0–20 pts)
        if len(closes) >= 20:
            try:
                recent_std = stdev(closes[-20:])
                recent_mid = mean(closes[-20:])
                bb_width = (4 * recent_std) / recent_mid if recent_mid > 0 else 1.0

                # Compare to 60D history of BB widths
                bb_widths_hist: list[float] = []
                for i in range(20, min(60, len(closes))):
                    w = closes[-(i + 1):-1] if i < len(closes) - 1 else closes[-i:]
                    if len(w) >= 10:
                        s = stdev(w[-10:])
                        m = mean(w[-10:])
                        if m > 0:
                            bb_widths_hist.append(4 * s / m)
                if bb_widths_hist:
                    rank_pct = sum(1 for w in bb_widths_hist if w < bb_width) / len(bb_widths_hist)
                    if rank_pct <= 0.15:
                        score += 20
                        flags.append("BB_VERY_TIGHT")
                    elif rank_pct <= 0.30:
                        score += 12
                        flags.append("BB_TIGHT")
                    elif rank_pct <= 0.50:
                        score += 6
            except Exception:
                pass

        # 3. Volume dry-up at troughs (0–20 pts)
        if len(valid_contractions) >= 2:
            prior = valid_contractions[-2]
            trough_vol_ratio = (
                latest["trough_avg_vol"] / prior["trough_avg_vol"]
                if prior["trough_avg_vol"] > 0
                else 1.0
            )
            if trough_vol_ratio < 0.40:
                score += 20
                flags.append(f"TROUGH_VOL_DRYUP:{trough_vol_ratio:.2f}x")
            elif trough_vol_ratio < 0.65:
                score += 12
                flags.append(f"TROUGH_VOL_LOW:{trough_vol_ratio:.2f}x")
            elif trough_vol_ratio < 0.85:
                score += 6
                flags.append(f"TROUGH_VOL_DECL:{trough_vol_ratio:.2f}x")

        # 4. Pullback depth tightness (0–15 pts)
        pullback_pct = latest["pullback_pct"]
        if pullback_pct < 0.08:
            score += 15
            flags.append(f"PULLBACK_TIGHT:{pullback_pct:.1%}")
        elif pullback_pct < 0.12:
            score += 8
            flags.append(f"PULLBACK_MOD:{pullback_pct:.1%}")

        # 5. MA alignment (0–15 pts)
        if len(closes) >= 60:
            ma20 = mean(closes[-20:])
            if ma5 > ma20 > ma60:
                score += 15
                flags.append("MA_ALIGNED")
            elif ma20 > ma60:
                score += 8
                flags.append("MA_PARTIAL")

        if score < self.MIN_SCORE:
            return None

        return {
            "score": max(0, score),
            "flags": flags,
            "contractions": n_contractions,
            "latest_pullback_pct": round(latest["pullback_pct"] * 100, 1),
            "dist_from_trough_pct": round(dist_from_trough * 100, 1),
            "signal_type": "VCP",
            "horizon": "波段",
        }
