"""HTFDetector — High Tight Flag pattern.

Pattern: Stock surged 25%+ in the prior 20–40 bars, now consolidating
tightly (<15% range, volume contracted). Buy at the breakout point.

Gates:
  Gate 1: prior advance >= 25% (from 20–40 bars ago low to recent high)
  Gate 2: consolidation range <= 15% of consolidation high (last 5–15 bars)
  Gate 3: consolidation avg volume <= 0.65x prior surge avg volume
  Gate 4: current close >= consolidation_low * 1.02 (not broken down)
  Gate 5: MA5 > MA20 (still in uptrend context)

Score factors:
  Prior advance size      0–20 pts  (25%=10, 35%=15, 50%+=20)
  Consolidation tightness 0–20 pts  (<8%=20, <12%=12)
  Volume contraction      0–20 pts  (<0.4x=20, <0.65x=12)
  Position in flag        0–15 pts  (near flag top = more pts)
  MA alignment            0–15 pts
  Prior advance speed     0–10 pts  (25% in <15 bars = 10, else 5)

MIN_SCORE = 40
"""
from __future__ import annotations

from statistics import mean

from taiwan_stock_agent.domain.models import DailyOHLCV


class HTFDetector:
    MIN_SCORE = 40
    # Consolidation window: look at last N bars for the flag
    _FLAG_WINDOW_MIN = 5
    _FLAG_WINDOW_MAX = 15

    def score(self, history: list[DailyOHLCV]) -> dict | None:
        """Return score dict or None if gates not met.

        Required: len(history) >= 50 (40 bars for prior advance + 10 for MA).
        """
        if len(history) < 50:
            return None

        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        vols = [d.volume for d in sorted_h]
        close = closes[-1]

        # ── Gate 5: MA5 > MA20 ───────────────────────────────────────────
        if len(closes) < 20:
            return None
        ma5 = mean(closes[-5:])
        ma20 = mean(closes[-20:])
        if ma5 <= ma20:
            return None

        # ── Gate 1 & 6: prior advance >= 25% from 20–40 bars ago ─────────
        # Find the prior surge: look for the largest advance in bars [-40:-5]
        # The "surge" ends at the recent high, and starts at a prior low
        if len(sorted_h) < 45:
            return None

        surge_window = sorted_h[-40:-5]  # 35 bars of potential surge history
        surge_closes = [d.close for d in surge_window]
        surge_vols = [d.volume for d in surge_window]

        # Find best advance: lowest low → highest high within the surge window
        best_advance = 0.0
        best_low_idx = 0
        best_high_idx = 0
        for i, bar_lo in enumerate(surge_window):
            for j in range(i + 2, len(surge_window)):
                adv = (surge_window[j].high - bar_lo.low) / bar_lo.low if bar_lo.low > 0 else 0
                if adv > best_advance:
                    best_advance = adv
                    best_low_idx = i
                    best_high_idx = j

        if best_advance < 0.25:
            return None

        prior_advance_pct = best_advance
        advance_bars = best_high_idx - best_low_idx

        # Surge average volume (bars from low to high)
        surge_bar_vols = surge_vols[best_low_idx:best_high_idx + 1]
        surge_avg_vol = mean(surge_bar_vols) if surge_bar_vols else 0

        # ── Gate 2: consolidation range <= 15% ───────────────────────────
        # Check multiple flag window sizes and pick the best fit
        flag_found = False
        flag_window_size = self._FLAG_WINDOW_MIN
        consol_high = 0.0
        consol_low = 0.0
        consol_range_pct = 1.0
        consol_avg_vol = 0.0

        for window_size in range(self._FLAG_WINDOW_MIN, self._FLAG_WINDOW_MAX + 1):
            if len(sorted_h) < window_size + 5:
                break
            flag_bars = sorted_h[-window_size:]
            # Use closes for range pct to avoid exaggeration from intrabar wicks
            flag_closes = [d.close for d in flag_bars]
            close_high = max(flag_closes)
            close_low = min(flag_closes)
            rng = (close_high - close_low) / close_high if close_high > 0 else 1.0
            avg_v = mean(d.volume for d in flag_bars)

            # Gate 3 check inline: only accept windows with contracted volume
            if surge_avg_vol > 0:
                vr = avg_v / surge_avg_vol
            else:
                vr = 1.0

            if rng <= 0.15 and vr <= 0.65:
                flag_found = True
                flag_window_size = window_size
                consol_high = close_high
                consol_low = close_low
                consol_range_pct = rng
                consol_avg_vol = avg_v
                # Prefer the widest valid window (more confidence in consolidation)

        if not flag_found:
            return None

        # ── Gate 3: volume contraction (already enforced in loop above) ──
        if surge_avg_vol <= 0:
            return None
        vol_ratio = consol_avg_vol / surge_avg_vol

        # ── Gate 4: not broken down — close must be at or above consolidation low ──
        # Since consol_low is the minimum close in the flag, being equal is acceptable
        if consol_low <= 0 or close < consol_low * 0.99:
            return None

        # ── Scoring — Phase 4.46 continuous ──────────────────────────────
        flags: list[str] = ["HTF"]
        score = 0.0

        # 1. Prior advance size — 0.25→10, 0.35→15, 0.50+→20 linear
        if prior_advance_pct >= 0.50:
            score += 20.0
        elif prior_advance_pct >= 0.35:
            score += round(15.0 + (prior_advance_pct - 0.35) / 0.15 * 5.0, 2)
        else:
            score += round(10.0 + (prior_advance_pct - 0.25) / 0.10 * 5.0, 2)
        flags.append(f"PRIOR_ADV:{prior_advance_pct:.0%}")

        # 2. Consolidation tightness — 0.15→5, 0.12→12, 0.08→20 linear
        if consol_range_pct < 0.08:
            # 0.04→20 floor, 0.08→12
            if consol_range_pct <= 0.04:
                score += 20.0
            else:
                score += round(12.0 + (0.08 - consol_range_pct) / 0.04 * 8.0, 2)
            flags.append(f"FLAG_TIGHT:{consol_range_pct:.1%}")
        elif consol_range_pct < 0.12:
            score += round(5.0 + (0.12 - consol_range_pct) / 0.04 * 7.0, 2)
            flags.append(f"FLAG_MOD:{consol_range_pct:.1%}")
        else:
            # 0.15→0, 0.12→5 linear
            score += round((0.15 - consol_range_pct) / 0.03 * 5.0, 2)
            flags.append(f"FLAG_WIDE:{consol_range_pct:.1%}")

        # 3. Volume contraction — 0.65→0, 0.40→12, 0.20→20 linear
        if vol_ratio < 0.40:
            if vol_ratio <= 0.20:
                score += 20.0
            else:
                score += round(12.0 + (0.40 - vol_ratio) / 0.20 * 8.0, 2)
            flags.append(f"VOL_CONTRACT:{vol_ratio:.2f}x")
        elif vol_ratio < 0.65:
            score += round((0.65 - vol_ratio) / 0.25 * 12.0, 2)
            flags.append(f"VOL_CONTRACT:{vol_ratio:.2f}x")

        # 4. Position in flag — continuous linear on position_in_flag
        if consol_high > consol_low:
            position_in_flag = (close - consol_low) / (consol_high - consol_low)
            if position_in_flag >= 0.50:
                # 0.50→8, 0.75→15 (cap at 1.0)
                if position_in_flag >= 0.75:
                    pts = round(15.0 + min((position_in_flag - 0.75) / 0.25 * 0.0, 0.0), 2)
                    pts = 15.0
                    score += pts
                    flags.append(f"FLAG_TOP:{position_in_flag:.0%}")
                else:
                    score += round(8.0 + (position_in_flag - 0.50) / 0.25 * 7.0, 2)
                    flags.append(f"FLAG_MID:{position_in_flag:.0%}")

        # 5. MA alignment — partial credit (7.5/pair)
        if len(closes) >= 60:
            ma60 = mean(closes[-60:])
            pair_pts = 0.0
            if ma5 > ma20:
                pair_pts += 7.5
            if ma20 > ma60:
                pair_pts += 7.5
            if pair_pts >= 15.0:
                flags.append("MA_ALIGNED")
            elif pair_pts > 0:
                flags.append("MA_PARTIAL")
            score += pair_pts
        else:
            score += 8.0
            flags.append("MA_PARTIAL")

        # 6. Prior advance speed — 15→5, 10→10 linear (faster = more pts)
        if advance_bars <= 10:
            score += 10.0
            flags.append(f"FAST_SURGE:{advance_bars}b")
        elif advance_bars <= 15:
            score += round(5.0 + (15 - advance_bars) / 5.0 * 5.0, 2)
            flags.append(f"FAST_SURGE:{advance_bars}b")
        else:
            score += 5.0
            flags.append(f"SLOW_SURGE:{advance_bars}b")

        score = round(score, 2)
        if score < self.MIN_SCORE:
            return None

        return {
            "score": round(max(0.0, score), 2),
            "flags": flags,
            "prior_advance_pct": round(prior_advance_pct * 100, 1),
            "consolidation_range_pct": round(consol_range_pct * 100, 1),
            "vol_ratio": round(vol_ratio, 2),
            "flag_window": flag_window_size,
            "signal_type": "旗形",
            "horizon": "短線",
        }
