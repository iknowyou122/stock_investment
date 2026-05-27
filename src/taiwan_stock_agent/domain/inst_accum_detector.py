"""InstAccumDetector — institutional accumulation at the base.

Setup: Stock 15–40% below its 60D high, institutions quietly buying.
This fires BEFORE the surge, not after.

Gates (all required):
  Gate 1: close is 15–40% below sixty_day_high (space to run, not in freefall)
  Gate 2: institution consecutive buy days >= 3 (foreign OR trust)
  Gate 3: NOT a disposal/halted stock (from proxy.is_disposal if available)

Score factors:
  Consecutive buy days    0–25 pts  (3d=10, 5d=18, 8d+=25)
  Distance from 60D high  0–20 pts  (more space = more pts, -20%=10, -30%=20)
  Volume dry-up           0–15 pts  (recent 5D vol < 0.5x 20D avg = 15, <0.7x=8)
  Cumulative net buying   0–15 pts  (20D cumul net positive = 10, strong = 15)
  Chip cleanliness        0–10 pts  (large holder increasing = 10)
  MA alignment            0–10 pts  (MA5>MA20>MA60 = 10, MA20>MA60 only = 5)

MIN_SCORE = 35
"""
from __future__ import annotations

from statistics import mean

from taiwan_stock_agent.domain.models import DailyOHLCV, TWSEChipProxy


class InstAccumDetector:
    MIN_SCORE = 35

    def score(
        self,
        history: list[DailyOHLCV],
        proxy: TWSEChipProxy | None = None,
    ) -> dict | None:
        """Return score dict or None if gates not met.

        Required: len(history) >= 65 (60 for sixty_day_high + 5 for MA5).
        """
        if len(history) < 65:
            return None

        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        vols = [d.volume for d in sorted_h]
        close = closes[-1]

        # ── Gate 3: skip disposal/halted stocks ───────────────────────────
        if proxy is not None and (proxy.is_disposal or proxy.is_trading_halt):
            return None

        # ── Gate 1: close is 15–40% below sixty_day_high ─────────────────
        sixty_day_high = max(d.high for d in sorted_h[-60:])
        if sixty_day_high <= 0:
            return None
        distance_pct = (sixty_day_high - close) / sixty_day_high
        if distance_pct < 0.15 or distance_pct > 0.40:
            return None

        # ── Gate 2: institution consecutive buy days >= 3 ─────────────────
        if proxy is not None:
            consec_days = max(
                proxy.foreign_consecutive_buy_days,
                proxy.trust_consecutive_buy_days,
            )
        else:
            # Cannot determine consecutive days without proxy
            consec_days = 0

        if consec_days < 3:
            return None

        # ── Scoring (Phase 4.46: continuous) ──────────────────────────────
        flags: list[str] = ["INST_ACCUM"]
        score = 0.0

        # 1. Consecutive buy days — continuous: 3d→10, 5d→18, 8d+→25 linear
        if consec_days >= 8:
            score += 25.0
        elif consec_days >= 5:
            # 5→18, 8→25 linear
            score += round(18.0 + (consec_days - 5) / 3.0 * 7.0, 2)
        else:
            # 3→10, 5→18 linear
            score += round(10.0 + (consec_days - 3) / 2.0 * 8.0, 2)
        flags.append(f"INST_CONSEC:{consec_days}d")

        # 2. Distance from 60D high — continuous: 15%→5, 20%→10, 30%+→20
        if distance_pct >= 0.30:
            score += 20.0
            flags.append(f"DEEP_BASE:{distance_pct:.0%}")
        elif distance_pct >= 0.20:
            # 0.20→10, 0.30→20 linear
            score += round(10.0 + (distance_pct - 0.20) / 0.10 * 10.0, 2)
            flags.append(f"MID_BASE:{distance_pct:.0%}")
        else:
            # 0.15→5, 0.20→10 linear
            score += round(5.0 + (distance_pct - 0.15) / 0.05 * 5.0, 2)
            flags.append(f"SHALLOW_BASE:{distance_pct:.0%}")

        # 3. Volume dry-up — continuous on ratio
        avg_vol_20 = mean(vols[-20:]) if vols else 0
        avg_vol_5 = mean(vols[-5:]) if len(vols) >= 5 else avg_vol_20
        if avg_vol_20 > 0:
            vol_ratio = avg_vol_5 / avg_vol_20
            if vol_ratio < 0.50:
                # 0.30→15, 0.50→8 (drier = more pts)
                if vol_ratio <= 0.30:
                    score += 15.0
                else:
                    score += round(8.0 + (0.50 - vol_ratio) / 0.20 * 7.0, 2)
                flags.append("VOL_DRYUP_STRONG")
            elif vol_ratio < 0.70:
                # 0.50→8, 0.70→0 linear
                score += round((0.70 - vol_ratio) / 0.20 * 8.0, 2)
                flags.append("VOL_DRYUP")

        # 4. Cumulative net buying — continuous on intensity
        if proxy is not None:
            cumul = proxy.cumul_foreign_20d + proxy.cumul_trust_20d
            if cumul > 0:
                if avg_vol_20 > 0:
                    intensity = cumul / avg_vol_20  # ratio over 20d avg vol
                    # 0→0, 0.05→10, 0.20+→15
                    if intensity >= 0.20:
                        score += 15.0
                        flags.append("CUMUL_FLOW_STRONG")
                    elif intensity >= 0.05:
                        score += round(10.0 + (intensity - 0.05) / 0.15 * 5.0, 2)
                        flags.append("CUMUL_FLOW_STRONG")
                    else:
                        score += round(intensity / 0.05 * 10.0, 2)
                        flags.append("CUMUL_FLOW_POS")
                else:
                    score += 10.0
                    flags.append("CUMUL_FLOW_POS")

        # 5. Chip cleanliness — continuous on large_holder_chg_pct magnitude
        if proxy is not None and proxy.large_holder_chg_pct is not None:
            chg = proxy.large_holder_chg_pct
            if chg > 0:
                # 0→0, 0.5%→10, beyond→10 cap
                if chg >= 0.5:
                    score += 10.0
                else:
                    score += round(chg / 0.5 * 10.0, 2)
                flags.append("LARGE_HOLDER_ACCUM")
            elif chg < 0:
                # 0→0, -0.5%→-5 (cap)
                if chg <= -0.5:
                    score -= 5.0
                else:
                    score += round(chg / 0.5 * 5.0, 2)
                flags.append("LARGE_HOLDER_EXIT")

        # 6. MA alignment — partial credit (3.34/pair MA5>MA20, MA20>MA60)
        if len(closes) >= 60:
            ma5 = mean(closes[-5:])
            ma20 = mean(closes[-20:])
            ma60 = mean(closes[-60:])
            pair_pts = 0.0
            if ma5 > ma20:
                pair_pts += 5.0
            if ma20 > ma60:
                pair_pts += 5.0
            if pair_pts >= 10.0:
                flags.append("MA_ALIGNED")
            elif pair_pts > 0:
                flags.append("MA_PARTIAL_ALIGN")
            score += pair_pts

        score = round(score, 2)
        if score < self.MIN_SCORE:
            return None

        return {
            "score": round(max(0.0, score), 2),
            "flags": flags,
            "sixty_day_high": round(sixty_day_high, 2),
            "distance_pct": round(distance_pct * 100, 1),
            "consec_days": consec_days,
            "signal_type": "法人建倉",
            "horizon": "波段",
        }
