"""Unit tests for ScoutAgent.

All tests use MagicMock for FinMindClient — no real network calls.

Coverage:
  - VOLUME_SURGE detected when today_volume > 20d_avg × 2.0
  - PRICE_BREAKOUT detected when close >= 20d_high × 0.99
  - No anomaly emitted when volume and price are normal
  - SECTOR_CORRELATION triggered when >= 3 tickers both surge and break out
  - SECTOR_CORRELATION NOT triggered for only 2 tickers
  - Skips ticker gracefully when OHLCV history is insufficient (< 5 rows)
  - Results sorted by magnitude descending
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

from taiwan_stock_agent.agents.scout_agent import ScoutAgent
from taiwan_stock_agent.domain.models import AnomalySignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCAN_DATE = date(2025, 2, 5)


def _make_ohlcv_df(
    ticker: str,
    scan_date: date = _SCAN_DATE,
    today_volume: int = 10_000,
    today_close: float = 100.0,
    history_avg_volume: int = 10_000,
    history_high: float = 110.0,
    n_history: int = 20,
) -> pd.DataFrame:
    """Build a synthetic OHLCV DataFrame.

    The last row is scan_date with the given today_volume and today_close.
    The preceding n_history rows form the lookback baseline with uniform
    history_avg_volume and history_high.
    """
    rows = []
    for i in range(n_history):
        d = scan_date - timedelta(days=n_history - i)
        rows.append(
            {
                "trade_date": d,
                "ticker": ticker,
                "open": history_high - 2,
                "high": history_high,
                "low": history_high - 3,
                "close": history_high - 1,
                "volume": history_avg_volume,
            }
        )
    # Today's row
    rows.append(
        {
            "trade_date": scan_date,
            "ticker": ticker,
            "open": today_close - 1,
            "high": today_close + 0.5,
            "low": today_close - 2,
            "close": today_close,
            "volume": today_volume,
        }
    )
    return pd.DataFrame(rows)


def _make_scout(ohlcv_map: dict[str, pd.DataFrame]) -> ScoutAgent:
    """Return a ScoutAgent whose finmind returns per-ticker DataFrames from ohlcv_map."""
    mock_finmind = MagicMock()
    mock_finmind.fetch_ohlcv.side_effect = lambda ticker, start, end: ohlcv_map.get(
        ticker, pd.DataFrame()
    )
    return ScoutAgent(mock_finmind)


# ---------------------------------------------------------------------------
# TestScoutAgent
# ---------------------------------------------------------------------------

class TestScoutAgent:
    def test_volume_surge_detected(self):
        """Volume 25,000 vs avg 10,000 (2.5x) → VOLUME_SURGE signal."""
        df = _make_ohlcv_df(
            "9999",
            today_volume=25_000,
            history_avg_volume=10_000,
            today_close=105.0,
            history_high=200.0,  # High far above close so no breakout
        )
        scout = _make_scout({"9999": df})
        results = scout.scan(["9999"], _SCAN_DATE)

        trigger_types = [s.trigger_type for s in results]
        assert "VOLUME_SURGE" in trigger_types

        surge = next(s for s in results if s.trigger_type == "VOLUME_SURGE")
        assert surge.ticker == "9999"
        assert surge.trade_date == _SCAN_DATE
        # magnitude should be 2.5
        assert abs(surge.magnitude - 2.5) < 0.01

    def test_price_breakout_detected(self):
        """Close exactly at 20-day high → PRICE_BREAKOUT signal."""
        twenty_day_high = 110.0
        df = _make_ohlcv_df(
            "9999",
            today_volume=10_000,      # Not a surge (same as avg)
            history_avg_volume=10_000,
            today_close=twenty_day_high,
            history_high=twenty_day_high,
        )
        scout = _make_scout({"9999": df})
        results = scout.scan(["9999"], _SCAN_DATE)

        trigger_types = [s.trigger_type for s in results]
        assert "PRICE_BREAKOUT" in trigger_types

    def test_no_anomaly_when_normal(self):
        """Normal volume and price well below 20-day high → no signals."""
        df = _make_ohlcv_df(
            "9999",
            today_volume=10_000,
            history_avg_volume=10_000,
            today_close=100.0,
            history_high=200.0,  # close (100) is far below high (200)
        )
        scout = _make_scout({"9999": df})
        results = scout.scan(["9999"], _SCAN_DATE)

        assert results == []

    def test_sector_correlation_detected_when_3plus_tickers_surge(self):
        """Three tickers each with volume surge + breakout → SECTOR_CORRELATION signals."""
        tickers = ["A001", "B002", "C003"]
        ohlcv_map: dict[str, pd.DataFrame] = {}
        for t in tickers:
            ohlcv_map[t] = _make_ohlcv_df(
                t,
                today_volume=25_000,       # 2.5x surge
                history_avg_volume=10_000,
                today_close=110.0,         # exactly at 20d high
                history_high=110.0,
            )

        scout = _make_scout(ohlcv_map)
        results = scout.scan(tickers, _SCAN_DATE)

        sector_signals = [s for s in results if s.trigger_type == "SECTOR_CORRELATION"]
        assert len(sector_signals) == 3
        sector_tickers = {s.ticker for s in sector_signals}
        assert sector_tickers == set(tickers)

    def test_sector_correlation_not_triggered_for_2_tickers(self):
        """Only 2 tickers with dual signal → no SECTOR_CORRELATION."""
        tickers = ["A001", "B002"]
        ohlcv_map: dict[str, pd.DataFrame] = {}
        for t in tickers:
            ohlcv_map[t] = _make_ohlcv_df(
                t,
                today_volume=25_000,
                history_avg_volume=10_000,
                today_close=110.0,
                history_high=110.0,
            )

        scout = _make_scout(ohlcv_map)
        results = scout.scan(tickers, _SCAN_DATE)

        assert not any(s.trigger_type == "SECTOR_CORRELATION" for s in results)

    def test_skips_ticker_with_insufficient_history(self):
        """Only 2 rows of OHLCV → ticker skipped, no crash, not in results."""
        tiny_df = pd.DataFrame(
            [
                {
                    "trade_date": _SCAN_DATE - timedelta(days=1),
                    "ticker": "9999",
                    "open": 99.0, "high": 101.0, "low": 98.0,
                    "close": 100.0, "volume": 50_000,
                },
                {
                    "trade_date": _SCAN_DATE,
                    "ticker": "9999",
                    "open": 100.0, "high": 102.0, "low": 99.0,
                    "close": 101.0, "volume": 50_000,
                },
            ]
        )
        scout = _make_scout({"9999": tiny_df})
        results = scout.scan(["9999"], _SCAN_DATE)

        assert results == []

    def test_results_sorted_by_magnitude_descending(self):
        """Multiple anomalies across tickers → sorted by magnitude descending."""
        # Ticker A: higher volume ratio (3.0x)
        df_a = _make_ohlcv_df(
            "A001",
            today_volume=30_000,
            history_avg_volume=10_000,
            today_close=100.0,
            history_high=200.0,  # no breakout
        )
        # Ticker B: lower volume ratio (2.1x)
        df_b = _make_ohlcv_df(
            "B002",
            today_volume=21_000,
            history_avg_volume=10_000,
            today_close=100.0,
            history_high=200.0,  # no breakout
        )

        scout = _make_scout({"A001": df_a, "B002": df_b})
        results = scout.scan(["A001", "B002"], _SCAN_DATE)

        assert len(results) >= 2
        magnitudes = [s.magnitude for s in results]
        assert magnitudes == sorted(magnitudes, reverse=True)
