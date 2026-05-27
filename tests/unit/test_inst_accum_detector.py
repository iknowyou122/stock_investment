"""Unit tests for InstAccumDetector."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from taiwan_stock_agent.domain.inst_accum_detector import InstAccumDetector
from taiwan_stock_agent.domain.models import DailyOHLCV, TWSEChipProxy


def _bars(
    closes: list[float],
    vols: list[int] | None = None,
    high_offset: float = 1.0,
    low_offset: float = 1.0,
) -> list[DailyOHLCV]:
    """Create synthetic DailyOHLCV bars."""
    base = date(2025, 1, 2)
    return [
        DailyOHLCV(
            ticker="TEST",
            trade_date=base + timedelta(days=i),
            open=c - 0.5,
            high=c + high_offset,
            low=c - low_offset,
            close=c,
            volume=vols[i] if vols else 1_000_000,
        )
        for i, c in enumerate(closes)
    ]


def _make_proxy(
    foreign_consec: int = 5,
    trust_consec: int = 0,
    cumul_foreign: int = 500_000,
    cumul_trust: int = 0,
    large_holder_chg_pct: float | None = 0.5,
    retail_holder_chg_pct: float | None = -0.3,
    is_disposal: bool = False,
    is_trading_halt: bool = False,
    margin_decline_streak: int = 0,
) -> TWSEChipProxy:
    return TWSEChipProxy(
        ticker="TEST",
        trade_date=date(2025, 3, 1),
        foreign_consecutive_buy_days=foreign_consec,
        trust_consecutive_buy_days=trust_consec,
        cumul_foreign_20d=cumul_foreign,
        cumul_trust_20d=cumul_trust,
        large_holder_chg_pct=large_holder_chg_pct,
        retail_holder_chg_pct=retail_holder_chg_pct,
        is_disposal=is_disposal,
        is_trading_halt=is_trading_halt,
        margin_decline_streak=margin_decline_streak,
        is_available=True,
    )


def _base_history(
    close_now: float = 70.0,
    sixty_day_high_close: float = 100.0,
    n_bars: int = 70,
) -> list[DailyOHLCV]:
    """Build 70-bar history where stock is ~30% below its 60D high.

    First 10 bars: high-price zone (to set 60D high)
    Remaining bars: gradual decline to current level
    """
    closes: list[float] = []
    # First 10 bars at peak area
    for i in range(10):
        closes.append(round(sixty_day_high_close - i * 0.1, 2))
    # Next bars declining to close_now
    remaining = n_bars - 10
    step = (sixty_day_high_close - close_now) / max(remaining, 1)
    c = sixty_day_high_close
    for _ in range(remaining):
        c -= step
        closes.append(round(c, 2))

    bars = []
    base = date(2025, 1, 2)
    for i, c in enumerate(closes):
        # Set the high of the first 10 bars to sixty_day_high_close + 1
        hi = sixty_day_high_close + 1.0 if i < 10 else c + 1.0
        bars.append(DailyOHLCV(
            ticker="TEST",
            trade_date=base + timedelta(days=i),
            open=c - 0.5,
            high=hi,
            low=c - 1.0,
            close=c,
            volume=1_000_000,
        ))
    return bars


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_scores_valid_pattern():
    """Should score a valid institutional accumulation setup."""
    hist = _base_history(close_now=70.0, sixty_day_high_close=100.0)
    proxy = _make_proxy(foreign_consec=5)
    result = InstAccumDetector().score(hist, proxy)
    assert result is not None
    assert result["score"] >= InstAccumDetector.MIN_SCORE
    assert "INST_ACCUM" in result["flags"]
    assert result["signal_type"] == "法人建倉"
    assert result["horizon"] == "波段"


def test_returns_none_if_not_enough_consec_days():
    """Should return None when consecutive buy days < 3."""
    hist = _base_history(close_now=70.0, sixty_day_high_close=100.0)
    proxy = _make_proxy(foreign_consec=2, trust_consec=1)
    result = InstAccumDetector().score(hist, proxy)
    assert result is None


def test_returns_none_if_price_too_close_to_high():
    """Should return None when price is within 15% of 60D high."""
    hist = _base_history(close_now=90.0, sixty_day_high_close=100.0)
    proxy = _make_proxy(foreign_consec=5)
    result = InstAccumDetector().score(hist, proxy)
    assert result is None


def test_returns_none_if_price_too_far_from_high():
    """Should return None when price is >40% below 60D high (freefall)."""
    hist = _base_history(close_now=55.0, sixty_day_high_close=100.0)
    proxy = _make_proxy(foreign_consec=5)
    result = InstAccumDetector().score(hist, proxy)
    assert result is None


def test_scores_higher_with_more_consec_days():
    """More consecutive buy days should yield a higher score."""
    hist = _base_history(close_now=70.0, sixty_day_high_close=100.0)
    proxy_3d = _make_proxy(foreign_consec=3)
    proxy_8d = _make_proxy(foreign_consec=8)
    res_3 = InstAccumDetector().score(hist, proxy_3d)
    res_8 = InstAccumDetector().score(hist, proxy_8d)
    assert res_3 is not None
    assert res_8 is not None
    assert res_8["score"] > res_3["score"]


def test_volume_dryup_adds_pts():
    """Low recent volume vs 20D avg should add score."""
    hist_norm = _base_history(close_now=70.0)
    hist_dryup = _base_history(close_now=70.0)
    # Inject low-volume last 5 bars into hist_dryup
    for i in range(-5, 0):
        bar = hist_dryup[i]
        hist_dryup[i] = DailyOHLCV(
            ticker=bar.ticker,
            trade_date=bar.trade_date,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=300_000,  # very low
        )

    proxy = _make_proxy(foreign_consec=5)
    res_norm = InstAccumDetector().score(hist_norm, proxy)
    res_dry = InstAccumDetector().score(hist_dryup, proxy)
    assert res_norm is not None
    assert res_dry is not None
    assert res_dry["score"] > res_norm["score"]


def test_proxy_none_returns_none():
    """Without proxy, cannot determine consec days → gate fails → None."""
    hist = _base_history(close_now=70.0, sixty_day_high_close=100.0)
    result = InstAccumDetector().score(hist, proxy=None)
    assert result is None


def test_returns_none_if_disposal():
    """Should skip disposal stocks."""
    hist = _base_history(close_now=70.0, sixty_day_high_close=100.0)
    proxy = _make_proxy(foreign_consec=8, is_disposal=True)
    result = InstAccumDetector().score(hist, proxy)
    assert result is None


def test_returns_none_if_too_short():
    """Should return None when history is too short."""
    hist = _base_history(n_bars=40)
    proxy = _make_proxy(foreign_consec=5)
    result = InstAccumDetector().score(hist, proxy)
    assert result is None
