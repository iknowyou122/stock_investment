"""Unit tests for PullbackDetector."""
from __future__ import annotations
from datetime import date, timedelta
from taiwan_stock_agent.domain.models import DailyOHLCV
from taiwan_stock_agent.domain.pullback_detector import PullbackDetector


def _bars(closes: list[float], vols: list[int] | None = None) -> list[DailyOHLCV]:
    base = date(2025, 1, 2)
    return [
        DailyOHLCV(
            ticker="TEST",
            trade_date=base + timedelta(days=i),
            open=c - 0.5,
            high=c + 1.0,
            low=c - 1.0,
            close=c,
            volume=vols[i] if vols else 1_000_000,
        )
        for i, c in enumerate(closes)
    ]


def _pullback_history(vol_pullback: int = 1_000_000) -> list[DailyOHLCV]:
    """75 bars: 60 gradual uptrend → 10 surge to upper BB → 5 pullback to MA20."""
    closes: list[float] = []
    c = 80.0
    for _ in range(60):
        c += 0.3
        closes.append(round(c, 2))
    for _ in range(10):
        c += 2.0
        closes.append(round(c, 2))
    for _ in range(5):
        c -= 2.8
        closes.append(round(c, 2))

    vols = [1_000_000] * 70 + [vol_pullback] * 5
    return _bars(closes, vols)


def test_scores_valid_pullback():
    result = PullbackDetector().score(_pullback_history())
    assert result is not None
    assert result["score"] > 0
    assert "PULLBACK_MA20" in result["flags"]


def test_returns_none_if_too_short():
    assert PullbackDetector().score(_bars([80.0] * 40)) is None


def test_returns_none_if_no_ma_alignment():
    """Downtrend — MA5 < MA20 < MA60."""
    closes = [100.0 - i * 0.5 for i in range(80)]
    assert PullbackDetector().score(_bars(closes)) is None


def test_returns_none_if_price_far_above_ma20():
    """Pure uptrend, never pulls back to MA20."""
    closes = [80.0 + i * 0.5 for i in range(80)]
    assert PullbackDetector().score(_bars(closes)) is None


def test_returns_none_if_no_upper_bb_touch():
    """Tiny oscillation — never reaches upper BB in last 10 days."""
    closes = []
    c = 80.0
    for i in range(80):
        c += 0.05 if i % 2 == 0 else -0.05
        closes.append(round(c, 2))
    assert PullbackDetector().score(_bars(closes)) is None


def test_vol_contraction_flag():
    """Low volume during pullback → VOL_CONTRACTION flag."""
    result = PullbackDetector().score(_pullback_history(vol_pullback=200_000))
    assert result is not None
    assert any("VOL_CONTRACTION" in f for f in result["flags"])


def test_score_capped_at_100():
    result = PullbackDetector().score(_pullback_history())
    assert result is not None
    assert result["score"] <= 100
