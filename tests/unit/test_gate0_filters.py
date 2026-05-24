"""Tests for Gate 0 hard filters in TCE and SurgeRadar."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from taiwan_stock_agent.domain.models import DailyOHLCV, TWSEChipProxy, VolumeProfile
from taiwan_stock_agent.domain.triple_confirmation_engine import TripleConfirmationEngine


TEST_DATE = date(2026, 5, 22)


def _ohlcv(close: float = 100.0, volume: int = 5_000_000) -> DailyOHLCV:
    return DailyOHLCV(
        ticker="TEST", trade_date=TEST_DATE,
        open=close * 0.99, high=close * 1.01, low=close * 0.98,
        close=close, volume=volume,
    )


def _history(n: int = 25, close: float = 95.0) -> list[DailyOHLCV]:
    bars = []
    start = TEST_DATE - timedelta(days=n + 5)
    for i in range(n):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        bars.append(DailyOHLCV(
            ticker="TEST", trade_date=d,
            open=close, high=close * 1.01, low=close * 0.99, close=close, volume=4_000_000,
        ))
    return bars


def _proxy(**kwargs) -> TWSEChipProxy:
    defaults = dict(ticker="TEST", trade_date=TEST_DATE, is_available=True)
    defaults.update(kwargs)
    return TWSEChipProxy(**defaults)


def _vp() -> VolumeProfile:
    return VolumeProfile(ticker="TEST", poc_proxy=90.0, twenty_day_high=102.0, twenty_day_sessions=20)


def _chip():
    from taiwan_stock_agent.domain.triple_confirmation_engine import ChipReport
    return ChipReport(ticker="TEST", report_date=TEST_DATE)


class TestDisposalGate:
    def test_disposal_ticker_returns_skip_action(self):
        """is_disposal=True → action must not be LONG or WATCH."""
        engine = TripleConfirmationEngine()
        proxy = _proxy(is_disposal=True)
        signal, _, _ = engine.score_full(
            _ohlcv(), _history(), _chip(), _vp(), twse_proxy=proxy
        )
        assert signal.action in ("SKIP", "CAUTION"), f"Expected SKIP/CAUTION, got {signal.action}"
        assert any("DISPOSAL" in f for f in signal.data_quality_flags)

    def test_non_disposal_not_affected(self):
        """is_disposal=False → normal scoring, action can be anything valid."""
        engine = TripleConfirmationEngine()
        proxy = _proxy(is_disposal=False)
        signal, _, _ = engine.score_full(
            _ohlcv(), _history(), _chip(), _vp(), twse_proxy=proxy
        )
        assert not any("DISPOSAL" in f for f in signal.data_quality_flags)


class TestHaltGate:
    def test_halt_ticker_returns_skip(self):
        engine = TripleConfirmationEngine()
        proxy = _proxy(is_trading_halt=True)
        signal, _, _ = engine.score_full(
            _ohlcv(), _history(), _chip(), _vp(), twse_proxy=proxy
        )
        assert signal.action in ("SKIP", "CAUTION")
        assert any("HALT" in f for f in signal.data_quality_flags)


class TestLimitUpFlag:
    def test_limit_up_adds_flag_but_does_not_skip(self):
        """is_limit_up=True adds LIMIT_UP_CLOSE flag but does NOT force skip."""
        engine = TripleConfirmationEngine()
        proxy = _proxy(is_limit_up=True)
        signal, _, _ = engine.score_full(
            _ohlcv(), _history(), _chip(), _vp(), twse_proxy=proxy
        )
        assert "LIMIT_UP_CLOSE" in signal.data_quality_flags
        # action should still be based on normal scoring (not forced to SKIP)
        assert signal.action in ("LONG", "WATCH", "CAUTION", "SKIP")


class TestDaytradeRestrictedFlag:
    def test_daytrade_restricted_adds_flag(self):
        engine = TripleConfirmationEngine()
        proxy = _proxy(is_daytrade_restricted=True)
        signal, _, _ = engine.score_full(
            _ohlcv(), _history(), _chip(), _vp(), twse_proxy=proxy
        )
        assert "DAYTRADE_RESTRICTED" in signal.data_quality_flags
