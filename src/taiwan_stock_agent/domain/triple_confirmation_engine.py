"""Triple Confirmation Engine — deterministic confidence scoring.

Score breakdown (max 100 pts before risk deductions):

  Pillar 1: Momentum (0–40 pts)
    +20  close > vwap_5d   (price above 5-day volume-weighted average)
    +20  daily_volume > 20-day avg volume * 1.5  (daily volume surge)

  Pillar 2: Chip (0–40 pts)
    +15  net_buyer_count_diff > 0   (more buyer branches than seller branches over 3 days)
    +15  concentration_top15 > 0.35 (top-15 branches hold >35% of total buy vol today)
    +10  no 隔日沖 branch in top-3 buyers  (quality confirmation)

  Pillar 3: Space / POC proxy (0–20 pts)
    +20  close > twenty_day_high * 0.99  (within 1% of or above 20-day high)
         NOTE: real VolumeProfile (intraday tick) deferred to Phase 4

  Risk deductions:
    -25  隔日沖 in top-3 buyers
    -15  momentum divergence (price new high but external buy ratio declining)
         NOTE: external_buy_ratio unavailable from FinMind T+1 daily data;
               divergence deduction is SKIPPED in Phase 1-3

  Final: confidence = max(0, min(100, score))
  Action: confidence >= 70 → LONG | confidence <= 30 → CAUTION | else → WATCH
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from taiwan_stock_agent.domain.models import (
    BrokerWithLabel,
    ChipReport,
    DailyOHLCV,
    ExecutionPlan,
    SignalOutput,
    VolumeProfile,
    Reasoning,
)

logger = logging.getLogger(__name__)

_LONG_THRESHOLD = 70
_CAUTION_THRESHOLD = 30


@dataclass
class _ScoreBreakdown:
    """Intermediate scoring state for transparency and testing."""
    vwap_5d_pts: int = 0
    volume_surge_pts: int = 0
    net_buyer_diff_pts: int = 0
    concentration_pts: int = 0
    no_daytrade_pts: int = 0
    space_pts: int = 0
    daytrade_deduction: int = 0
    divergence_deduction: int = 0
    flags: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        raw = (
            self.vwap_5d_pts
            + self.volume_surge_pts
            + self.net_buyer_diff_pts
            + self.concentration_pts
            + self.no_daytrade_pts
            + self.space_pts
            - self.daytrade_deduction
            - self.divergence_deduction
        )
        return max(0, min(100, raw))


class TripleConfirmationEngine:
    """Compute the Triple Confirmation confidence score.

    Usage::

        engine = TripleConfirmationEngine()
        signal = engine.score(
            ohlcv=today_ohlcv,
            ohlcv_history=last_20_days,
            chip_report=chip_report,
            volume_profile=volume_profile,
        )
    """

    def score(
        self,
        ohlcv: DailyOHLCV,
        ohlcv_history: list[DailyOHLCV],
        chip_report: ChipReport,
        volume_profile: VolumeProfile,
    ) -> SignalOutput:
        """Compute deterministic confidence and return a SignalOutput.

        The reasoning fields are left empty here — the LLM layer (StrategistAgent)
        fills them in from the breakdown and chip report.

        ohlcv_history should include at least 20 sessions (includes ohlcv itself).
        """
        breakdown = self._compute(ohlcv, ohlcv_history, chip_report, volume_profile)
        confidence = breakdown.total
        action = self._map_action(confidence)
        plan = self._make_execution_plan(ohlcv, volume_profile)

        data_quality_flags = list(ohlcv.data_quality_flags)
        data_quality_flags.extend(chip_report.data_quality_flags)
        data_quality_flags.extend(volume_profile.data_quality_flags)

        return SignalOutput(
            ticker=ohlcv.ticker,
            date=ohlcv.trade_date,
            action=action,
            confidence=confidence,
            reasoning=Reasoning(),  # filled by StrategistAgent
            execution_plan=plan,
            halt_flag=False,
            data_quality_flags=data_quality_flags,
        )

    def score_with_breakdown(
        self,
        ohlcv: DailyOHLCV,
        ohlcv_history: list[DailyOHLCV],
        chip_report: ChipReport,
        volume_profile: VolumeProfile,
    ) -> tuple[SignalOutput, _ScoreBreakdown]:
        """Same as score() but also returns the breakdown for LLM prompting."""
        breakdown = self._compute(ohlcv, ohlcv_history, chip_report, volume_profile)
        confidence = breakdown.total
        action = self._map_action(confidence)
        plan = self._make_execution_plan(ohlcv, volume_profile)

        data_quality_flags = list(ohlcv.data_quality_flags)
        data_quality_flags.extend(chip_report.data_quality_flags)
        data_quality_flags.extend(volume_profile.data_quality_flags)

        signal = SignalOutput(
            ticker=ohlcv.ticker,
            date=ohlcv.trade_date,
            action=action,
            confidence=confidence,
            reasoning=Reasoning(),
            execution_plan=plan,
            halt_flag=False,
            data_quality_flags=data_quality_flags,
        )
        return signal, breakdown

    # ------------------------------------------------------------------
    # Private computation
    # ------------------------------------------------------------------

    def _compute(
        self,
        ohlcv: DailyOHLCV,
        ohlcv_history: list[DailyOHLCV],
        chip_report: ChipReport,
        volume_profile: VolumeProfile,
    ) -> _ScoreBreakdown:
        bd = _ScoreBreakdown()

        # --- Pillar 1: Momentum ---
        vwap_5d = self._vwap_5d(ohlcv_history)
        if vwap_5d is not None and ohlcv.close > vwap_5d:
            bd.vwap_5d_pts = 20

        vol_20ma = self._volume_20ma(ohlcv_history)
        if vol_20ma is not None and ohlcv.volume > vol_20ma * 1.5:
            bd.volume_surge_pts = 20

        # --- Pillar 2: Chip ---
        if chip_report.net_buyer_count_diff > 0:
            bd.net_buyer_diff_pts = 15

        # Guard: skip concentration check if too few active branches (thinly traded)
        if chip_report.active_branch_count >= 10:
            if chip_report.concentration_top15 > 0.35:
                bd.concentration_pts = 15
        else:
            bd.flags.append(
                f"THIN_MARKET: only {chip_report.active_branch_count} active branches "
                "— concentration check skipped"
            )

        top3 = chip_report.top_buyers[:3]
        daytrade_in_top3 = any(b.label == "隔日沖" for b in top3)

        if not daytrade_in_top3:
            bd.no_daytrade_pts = 10
        else:
            bd.daytrade_deduction = 25
            top3_names = [b.branch_name for b in top3 if b.label == "隔日沖"]
            bd.flags.append(f"隔日沖_TOP3: {', '.join(top3_names)}")
            chip_report.risk_flags.append("隔日沖_TOP3")

        # --- Pillar 3: Space proxy ---
        if ohlcv.close > volume_profile.twenty_day_high * 0.99:
            bd.space_pts = 20

        # --- Risk deductions ---
        # Momentum divergence: skipped in Phase 1-3 (external_buy_ratio unavailable)
        # Phase 4: compare external_buy_ratio today vs 3-day-ago to detect divergence

        logger.debug(
            "score breakdown for %s: vwap=%d vol=%d chip_diff=%d conc=%d "
            "no_dt=%d space=%d dt_ded=%d flags=%s → total=%d",
            ohlcv.ticker,
            bd.vwap_5d_pts, bd.volume_surge_pts, bd.net_buyer_diff_pts,
            bd.concentration_pts, bd.no_daytrade_pts, bd.space_pts,
            bd.daytrade_deduction, bd.flags, bd.total,
        )
        return bd

    @staticmethod
    def _vwap_5d(history: list[DailyOHLCV]) -> float | None:
        """5-day volume-weighted average close.

        vwap_5d = Σ(close_i × volume_i) / Σ(volume_i) for last 5 sessions.
        Uses at most the 5 most recent sessions from history (history sorted ascending).
        Returns None if fewer than 5 sessions available.
        """
        recent = sorted(history, key=lambda x: x.trade_date)[-5:]
        if len(recent) < 5:
            return None
        total_vol = sum(d.volume for d in recent)
        if total_vol == 0:
            return None
        return sum(d.close * d.volume for d in recent) / total_vol

    @staticmethod
    def _volume_20ma(history: list[DailyOHLCV]) -> float | None:
        """20-session simple moving average of daily volume.

        Returns None if fewer than 20 sessions available.
        """
        recent = sorted(history, key=lambda x: x.trade_date)[-20:]
        if len(recent) < 20:
            return None
        return sum(d.volume for d in recent) / len(recent)

    @staticmethod
    def _map_action(confidence: int) -> str:
        if confidence >= _LONG_THRESHOLD:
            return "LONG"
        if confidence <= _CAUTION_THRESHOLD:
            return "CAUTION"
        return "WATCH"

    @staticmethod
    def _make_execution_plan(
        ohlcv: DailyOHLCV, volume_profile: VolumeProfile
    ) -> ExecutionPlan:
        """Compute deterministic entry/stop/target.

        entry_bid_limit = close * 0.995  (lower bound, limit order)
        entry_max_chase = close * 1.005  (upper bound, max acceptable chase)
        stop_loss       = T+0 closing price  (order-entry reference, NOT intraday VWAP)
        target          = poc_proxy * 1.05   (5% above 20-day high proxy)

        Stop-loss note: this is the daily close price at signal time. It does NOT
        account for gap-open scenarios. Traders must manually verify at market open.
        """
        close = ohlcv.close
        return ExecutionPlan(
            entry_bid_limit=round(close * 0.995, 2),
            entry_max_chase=round(close * 1.005, 2),
            stop_loss=round(close, 2),
            target=round(volume_profile.poc_proxy * 1.05, 2),
        )
