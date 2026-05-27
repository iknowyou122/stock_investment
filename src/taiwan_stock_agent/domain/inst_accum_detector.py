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

        # ── Scoring ───────────────────────────────────────────────────────
        flags: list[str] = ["INST_ACCUM"]
        score = 0

        # 1. Consecutive buy days (0–25 pts)
        if consec_days >= 8:
            score += 25
            flags.append(f"INST_CONSEC:{consec_days}d")
        elif consec_days >= 5:
            score += 18
            flags.append(f"INST_CONSEC:{consec_days}d")
        else:
            score += 10
            flags.append(f"INST_CONSEC:{consec_days}d")

        # 2. Distance from 60D high — more space = more room to run (0–20 pts)
        if distance_pct >= 0.30:
            score += 20
            flags.append(f"DEEP_BASE:{distance_pct:.0%}")
        elif distance_pct >= 0.20:
            score += 10
            flags.append(f"MID_BASE:{distance_pct:.0%}")
        else:
            # 15–20% below
            score += 5
            flags.append(f"SHALLOW_BASE:{distance_pct:.0%}")

        # 3. Volume dry-up — quiet accumulation (0–15 pts)
        avg_vol_20 = mean(vols[-20:]) if vols else 0
        avg_vol_5 = mean(vols[-5:]) if len(vols) >= 5 else avg_vol_20
        if avg_vol_20 > 0:
            vol_ratio = avg_vol_5 / avg_vol_20
            if vol_ratio < 0.50:
                score += 15
                flags.append("VOL_DRYUP_STRONG")
            elif vol_ratio < 0.70:
                score += 8
                flags.append("VOL_DRYUP")

        # 4. Cumulative net buying (0–15 pts)
        if proxy is not None:
            cumul = proxy.cumul_foreign_20d + proxy.cumul_trust_20d
            if cumul > 0:
                # Normalize: compare to avg_vol_20 (shares); strong = >5% of 20d avg
                if avg_vol_20 > 0 and cumul > avg_vol_20 * 0.05:
                    score += 15
                    flags.append("CUMUL_FLOW_STRONG")
                else:
                    score += 10
                    flags.append("CUMUL_FLOW_POS")

        # 5. Chip cleanliness — large holders increasing (0–10 pts)
        if proxy is not None and proxy.large_holder_chg_pct is not None:
            if proxy.large_holder_chg_pct > 0:
                score += 10
                flags.append("LARGE_HOLDER_ACCUM")
            elif proxy.large_holder_chg_pct < 0:
                score -= 5
                flags.append("LARGE_HOLDER_EXIT")

        # 6. MA alignment (0–10 pts)
        if len(closes) >= 60:
            ma5 = mean(closes[-5:])
            ma20 = mean(closes[-20:])
            ma60 = mean(closes[-60:])
            if ma5 > ma20 > ma60:
                score += 10
                flags.append("MA_ALIGNED")
            elif ma20 > ma60:
                score += 5
                flags.append("MA_PARTIAL_ALIGN")

        if score < self.MIN_SCORE:
            return None

        return {
            "score": max(0, score),
            "flags": flags,
            "sixty_day_high": round(sixty_day_high, 2),
            "distance_pct": round(distance_pct * 100, 1),
            "consec_days": consec_days,
            "signal_type": "法人建倉",
            "horizon": "波段",
        }
