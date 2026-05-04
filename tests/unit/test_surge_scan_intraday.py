"""Tests for intraday surge scan helpers."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from surge_scan import (  # noqa: E402
    _build_intraday_bar,
    _precompute_today_snapshot,
    _scan_one_surge,
)
from taiwan_stock_agent.domain.models import DailyOHLCV  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _quote(
    price: float = 100.0,
    volume: int = 5000,
    high: float = 103.0,
    low: float = 98.0,
    open_p: float = 99.0,
    prev: float = 95.0,
) -> dict:
    return {
        "price": price,
        "volume": volume,
        "high": high,
        "low": low,
        "open": open_p,
        "yesterday_close": prev,
        "price_source": "last",
        "timestamp": "10:30:00",
        "name": "測試股",
    }


def _make_history(n: int = 25, ticker: str = "2330") -> list[DailyOHLCV]:
    return [
        DailyOHLCV(
            ticker=ticker,
            trade_date=date(2026, 1, 1) + timedelta(days=i),
            open=100.0,
            high=103.0,
            low=98.0,
            close=100.0 + i * 0.1,
            volume=500_000,
        )
        for i in range(n)
    ]


def _history_df(history: list[DailyOHLCV]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "trade_date": b.trade_date,
            "ticker": b.ticker,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in history
    ])


# ---------------------------------------------------------------------------
# _build_intraday_bar
# ---------------------------------------------------------------------------

class TestBuildIntradayBar:
    def test_volume_projected_to_shares(self):
        today = date(2026, 5, 5)
        bar = _build_intraday_bar("2330", _quote(volume=3000), today, time_ratio=0.5)
        assert bar is not None
        assert bar.volume == 6_000_000  # 3000 * 1000 / 0.5

    def test_ohlc_fields_populated(self):
        today = date(2026, 5, 5)
        bar = _build_intraday_bar(
            "2330", _quote(price=100.0, open_p=99.0, high=103.0, low=98.0), today, time_ratio=1.0
        )
        assert bar is not None
        assert bar.close == 100.0
        assert bar.open == 99.0
        assert bar.high == 103.0
        assert bar.low == 98.0
        assert bar.trade_date == today
        assert bar.ticker == "2330"

    def test_returns_none_when_price_missing(self):
        today = date(2026, 5, 5)
        q = _quote()
        q["price"] = None
        assert _build_intraday_bar("2330", q, today, time_ratio=0.5) is None

    def test_returns_none_when_time_ratio_zero(self):
        today = date(2026, 5, 5)
        assert _build_intraday_bar("2330", _quote(), today, time_ratio=0.0) is None

    def test_open_falls_back_to_price_when_none(self):
        today = date(2026, 5, 5)
        q = _quote(price=100.0)
        q["open"] = None
        bar = _build_intraday_bar("2330", q, today, time_ratio=1.0)
        assert bar is not None
        assert bar.open == 100.0


# ---------------------------------------------------------------------------
# _scan_one_surge — chip_date uses yesterday in intraday mode
# ---------------------------------------------------------------------------

class TestScanOneSurgeIntraday:
    def test_intraday_bar_chip_date_is_yesterday(self):
        today = date(2026, 5, 5)
        yesterday = today - timedelta(days=1)
        prior = _make_history(25)

        intraday_bar = DailyOHLCV(
            ticker="2330",
            trade_date=today,
            open=102.0,
            high=110.0,
            low=101.0,
            close=108.0,
            volume=2_000_000,
        )

        mock_finmind = MagicMock()
        mock_finmind.fetch_ohlcv.return_value = _history_df(prior)

        mock_chip = MagicMock()
        mock_chip.fetch.return_value = None

        _scan_one_surge(
            ticker="2330",
            analysis_date=today,
            finmind=mock_finmind,
            chip_fetcher=mock_chip,
            market="TSE",
            taiex_history=prior,
            industry_rank_pct=80.0,
            intraday_bar=intraday_bar,
        )

        assert mock_chip.fetch.called, "chip_fetcher.fetch should have been called"
        call_date = mock_chip.fetch.call_args[0][1]
        assert call_date == yesterday, f"Expected chip fetch for {yesterday}, got {call_date}"


# ---------------------------------------------------------------------------
# _precompute_today_snapshot — uses MIS vol/price when intraday_quotes provided
# ---------------------------------------------------------------------------

class TestPrecomputeSnapshotIntraday:
    def test_uses_mis_price_when_quotes_provided(self):
        today = date(2026, 5, 5)
        prior = _make_history(25)

        mock_finmind = MagicMock()
        mock_finmind.fetch_ohlcv.return_value = _history_df(prior)

        intraday_quotes = {
            "2330": {
                "price": 110.0,
                "volume": 2000,       # 張
                "yesterday_close": 100.0,
                "high": 112.0,
                "low": 108.0,
                "open": 101.0,
            }
        }

        snapshot = _precompute_today_snapshot(
            tickers=["2330"],
            analysis_date=today,
            finmind=mock_finmind,
            workers=1,
            intraday_quotes=intraday_quotes,
            time_ratio=0.5,
        )

        assert "2330" in snapshot
        # vol_ratio = (2000 * 1000 / 0.5) / 500_000 = 4_000_000 / 500_000 = 8.0
        assert abs(snapshot["2330"]["vol_ratio"] - 8.0) < 0.1
        # day_chg_pct = (110 / 100 - 1) * 100 = 10.0
        assert abs(snapshot["2330"]["day_chg_pct"] - 10.0) < 0.1
