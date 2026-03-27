"""ScoutAgent: Phase 2 market anomaly scanner.

Scans a watchlist for three anomaly patterns on a given trading date:
  1. VOLUME_SURGE    — daily volume > 20-day average × 2.0
  2. PRICE_BREAKOUT  — close > 20-day high × 0.99 (within 1% of or above 20-day high)
  3. SECTOR_CORRELATION — >= 3 tickers in the watchlist all trigger BOTH
                          VOLUME_SURGE and PRICE_BREAKOUT on the same day

Design rationale: feeds StrategistAgent with candidates already filtered for
anomalies, reducing the daily scan from O(market) to O(anomalies).  Rather than
running the full Triple Confirmation pipeline for the entire market universe, the
caller passes a focused watchlist; ScoutAgent surfaces only the tickers worth the
deeper analysis cost.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from taiwan_stock_agent.domain.models import AnomalySignal
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient

logger = logging.getLogger(__name__)

# Thresholds (matching design doc Phase 2 spec)
_VOLUME_SURGE_MULTIPLIER = 2.0   # today_volume > 20d_avg × 2.0
_BREAKOUT_PROXIMITY = 0.99       # close > twenty_day_high × 0.99
_SECTOR_CORRELATION_MIN = 3      # minimum tickers for sector correlation trigger
_MIN_SESSIONS = 5                # skip ticker if OHLCV has fewer than this
_LOOKBACK_CALENDAR_DAYS = 35     # fetch window: enough to cover 20+ trading sessions


class ScoutAgent:
    """Phase 2 anomaly scanner over a watchlist.

    Usage::

        scout = ScoutAgent(finmind_client)
        signals = scout.scan(watchlist=["2330", "2317", "2454"], scan_date=date(2026, 3, 24))
        # signals sorted by magnitude descending; one ticker may appear multiple times
        # (once per trigger_type)
    """

    def __init__(self, finmind: FinMindClient) -> None:
        self._finmind = finmind

    def scan(self, watchlist: list[str], scan_date: date) -> list[AnomalySignal]:
        """Scan all tickers in watchlist for anomalies on scan_date.

        For each ticker:
          1. Fetch 25 sessions of OHLCV ending on scan_date.
          2. Check VOLUME_SURGE: today_volume > 20d_avg_volume × 2.0
          3. Check PRICE_BREAKOUT: today_close > 20d_high × 0.99
          4. After scanning all tickers, check SECTOR_CORRELATION:
             if >= 3 tickers both volume_surge AND price_breakout on the same day,
             each such ticker gets an additional SECTOR_CORRELATION AnomalySignal.

        Tickers with OHLCV < 5 sessions are skipped with a warning (no crash).
        Results are returned sorted by magnitude descending.
        A ticker may appear multiple times for different trigger_types.
        """
        start = scan_date - timedelta(days=_LOOKBACK_CALENDAR_DAYS)

        signals: list[AnomalySignal] = []
        dual_signal_tickers: list[str] = []  # both volume_surge AND price_breakout

        for ticker in watchlist:
            try:
                ticker_signals, is_dual = self._scan_ticker(ticker, scan_date, start)
            except Exception as exc:
                logger.warning(
                    "ScoutAgent: unexpected error scanning %s — skipping. (%s: %s)",
                    ticker, type(exc).__name__, exc,
                )
                continue

            signals.extend(ticker_signals)
            if is_dual:
                dual_signal_tickers.append(ticker)

        # --- Sector correlation check ---
        if len(dual_signal_tickers) >= _SECTOR_CORRELATION_MIN:
            for ticker in dual_signal_tickers:
                signals.append(
                    AnomalySignal(
                        ticker=ticker,
                        trade_date=scan_date,
                        trigger_type="SECTOR_CORRELATION",
                        magnitude=float(len(dual_signal_tickers)),
                        description=(
                            f"Sector correlation: {len(dual_signal_tickers)} tickers "
                            f"in watchlist all triggered VOLUME_SURGE + PRICE_BREAKOUT "
                            f"on {scan_date}."
                        ),
                    )
                )

        signals.sort(key=lambda s: s.magnitude, reverse=True)
        return signals

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _scan_ticker(
        self,
        ticker: str,
        scan_date: date,
        start: date,
    ) -> tuple[list[AnomalySignal], bool]:
        """Return (list_of_signals, is_dual_signal) for one ticker.

        is_dual_signal is True when the ticker triggered both VOLUME_SURGE
        and PRICE_BREAKOUT — used by the caller to count sector correlation.
        """
        ohlcv_df = self._finmind.fetch_ohlcv(ticker, start, scan_date)

        if ohlcv_df is None or ohlcv_df.empty:
            logger.warning("ScoutAgent: no OHLCV data for %s — skipping.", ticker)
            return [], False

        # Filter to rows on or before scan_date and sort ascending
        df = ohlcv_df[ohlcv_df["trade_date"] <= scan_date].copy()
        df = df.sort_values("trade_date").reset_index(drop=True)

        if len(df) < _MIN_SESSIONS:
            logger.warning(
                "ScoutAgent: only %d sessions for %s (need >= %d) — skipping.",
                len(df), ticker, _MIN_SESSIONS,
            )
            return [], False

        today_rows = df[df["trade_date"] == scan_date]
        if today_rows.empty:
            logger.warning(
                "ScoutAgent: scan_date %s not present in OHLCV for %s — skipping.",
                scan_date, ticker,
            )
            return [], False

        today = today_rows.iloc[-1]
        today_volume = int(today["volume"])
        today_close = float(today["close"])

        # Lookback window: last 20 sessions EXCLUDING today for the baseline
        history = df[df["trade_date"] < scan_date].tail(20)
        if history.empty:
            logger.warning(
                "ScoutAgent: no history rows before %s for %s — skipping.",
                scan_date, ticker,
            )
            return [], False

        avg_volume = float(history["volume"].mean())
        twenty_day_high = float(history["high"].max())

        signals: list[AnomalySignal] = []
        has_volume_surge = False
        has_breakout = False

        # --- VOLUME_SURGE ---
        if avg_volume > 0:
            volume_ratio = today_volume / avg_volume
            if volume_ratio > _VOLUME_SURGE_MULTIPLIER:
                has_volume_surge = True
                signals.append(
                    AnomalySignal(
                        ticker=ticker,
                        trade_date=scan_date,
                        trigger_type="VOLUME_SURGE",
                        magnitude=round(volume_ratio, 4),
                        description=(
                            f"{ticker}: volume {today_volume:,} is {volume_ratio:.2f}x "
                            f"the 20-day average {avg_volume:,.0f} on {scan_date}."
                        ),
                    )
                )

        # --- PRICE_BREAKOUT ---
        if twenty_day_high > 0:
            price_pct = (today_close - twenty_day_high) / twenty_day_high
            if today_close > twenty_day_high * _BREAKOUT_PROXIMITY:
                has_breakout = True
                signals.append(
                    AnomalySignal(
                        ticker=ticker,
                        trade_date=scan_date,
                        trigger_type="PRICE_BREAKOUT",
                        magnitude=round(price_pct, 6),
                        description=(
                            f"{ticker}: close {today_close} is {price_pct:+.2%} "
                            f"vs 20-day high {twenty_day_high} on {scan_date}."
                        ),
                    )
                )

        is_dual = has_volume_surge and has_breakout
        return signals, is_dual
