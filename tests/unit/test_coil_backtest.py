"""Unit tests for coil_backtest.py success criteria."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

# Make the scripts directory importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from taiwan_stock_agent.domain.models import DailyOHLCV


def _make_bar(d: date, close: float, high: float | None = None) -> DailyOHLCV:
    if high is None:
        high = close + 1.0
    return DailyOHLCV(
        ticker="TEST",
        trade_date=d,
        open=close - 0.5,
        high=high,
        low=close - 1.0,
        close=close,
        volume=10_000,
    )


def _make_bars(closes: list[float], start: date | None = None) -> list[DailyOHLCV]:
    if start is None:
        start = date(2025, 10, 1)
    return [_make_bar(start + timedelta(days=i), c) for i, c in enumerate(closes)]


# Import the function under test
from coil_backtest import _check_success  # type: ignore[import]


class TestCheckSuccessCriterionA:
    """Criterion A: close breaks above entry 20-day high."""

    def test_breaks_above_20d_high_on_day_3(self):
        entry_close = 100.0
        entry_20d_high = 105.0
        # Days: 100, 102, 106 (breaks on day 3)
        bars = _make_bars([100.0, 102.0, 106.0])
        success, days, ret = _check_success(entry_close, entry_20d_high, bars)
        assert success is True
        assert days == 3
        assert ret == pytest.approx(0.06, abs=1e-6)

    def test_breaks_above_20d_high_on_day_1(self):
        entry_close = 100.0
        entry_20d_high = 103.0
        bars = _make_bars([104.0, 103.0, 102.0])
        success, days, ret = _check_success(entry_close, entry_20d_high, bars)
        assert success is True
        assert days == 1

    def test_exactly_at_20d_high_counts_as_break(self):
        entry_close = 100.0
        entry_20d_high = 108.0
        bars = _make_bars([105.0, 106.0, 108.0])  # exactly hits on day 3
        success, days, ret = _check_success(entry_close, entry_20d_high, bars)
        assert success is True
        assert days == 3


class TestCheckSuccessCriterionB:
    """Criterion B: T+10 close is >= 5% above entry close."""

    def test_plus_6pct_at_day_10(self):
        entry_close = 100.0
        entry_20d_high = 200.0  # very high — criterion A won't fire
        # 10 bars, final close = 106
        closes = [101.0, 101.5, 102.0, 102.5, 103.0, 103.5, 104.0, 104.5, 105.0, 106.0]
        bars = _make_bars(closes)
        success, days, ret = _check_success(entry_close, entry_20d_high, bars)
        assert success is True
        assert days == 10
        assert ret == pytest.approx(0.06, abs=1e-6)

    def test_exactly_5pct_at_day_10_is_success(self):
        entry_close = 100.0
        entry_20d_high = 200.0
        closes = [100.5] * 9 + [105.0]
        bars = _make_bars(closes)
        success, days, ret = _check_success(entry_close, entry_20d_high, bars)
        assert success is True
        assert ret == pytest.approx(0.05, abs=1e-6)

    def test_criterion_b_requires_exactly_10_bars(self):
        """If fewer than 10 bars exist, criterion B should not fire even if return >= 5%."""
        entry_close = 100.0
        entry_20d_high = 200.0
        closes = [100.0, 106.0]  # only 2 bars, +6% but no 20d high break
        bars = _make_bars(closes)
        success, days, ret = _check_success(entry_close, entry_20d_high, bars)
        # Fewer than 10 bars → criterion B cannot fire
        assert success is False


class TestCheckSuccessFailure:
    """Below 20d high AND < 5% return → failure."""

    def test_plus_2pct_below_20d_high(self):
        entry_close = 100.0
        entry_20d_high = 115.0
        closes = [101.0, 101.5, 102.0] + [101.0] * 7  # 10 bars, +2% at end
        bars = _make_bars(closes)
        success, days, ret = _check_success(entry_close, entry_20d_high, bars)
        assert success is False
        assert ret == pytest.approx(0.01, abs=1e-4)

    def test_negative_return_failure(self):
        entry_close = 100.0
        entry_20d_high = 115.0
        closes = [99.0] * 10
        bars = _make_bars(closes)
        success, days, ret = _check_success(entry_close, entry_20d_high, bars)
        assert success is False
        assert ret < 0

    def test_empty_future_bars_failure(self):
        success, days, ret = _check_success(100.0, 110.0, [])
        assert success is False
        assert days == 0
        assert ret == 0.0

    def test_exactly_below_5pct_with_10_bars(self):
        entry_close = 100.0
        entry_20d_high = 200.0
        # T+10 close = 104.99, just under 5%
        closes = [100.0] * 9 + [104.99]
        bars = _make_bars(closes)
        success, days, ret = _check_success(entry_close, entry_20d_high, bars)
        assert success is False
        assert ret == pytest.approx(0.0499, abs=1e-4)


class TestCheckSuccessDaysToEvent:
    """days_to_event is correctly set to the number of bars observed."""

    def test_days_to_event_matches_len_bars_on_failure(self):
        entry_close = 100.0
        entry_20d_high = 200.0
        closes = [101.0, 102.0, 103.0]  # only 3 bars, failure
        bars = _make_bars(closes)
        success, days, ret = _check_success(entry_close, entry_20d_high, bars)
        assert success is False
        assert days == 3

    def test_days_to_event_is_1_on_immediate_breakout(self):
        entry_close = 100.0
        entry_20d_high = 103.0
        bars = _make_bars([104.0])
        success, days, ret = _check_success(entry_close, entry_20d_high, bars)
        assert success is True
        assert days == 1
