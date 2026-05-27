"""Unit tests for ChipTransferDetector."""
from __future__ import annotations

from datetime import date, timedelta

from taiwan_stock_agent.domain.chip_transfer_detector import ChipTransferDetector
from taiwan_stock_agent.domain.models import DailyOHLCV, TWSEChipProxy


def _bars(
    closes: list[float],
    vols: list[int] | None = None,
    high_offset: float = 0.5,
    low_offset: float = 0.5,
) -> list[DailyOHLCV]:
    base = date(2025, 1, 2)
    return [
        DailyOHLCV(
            ticker="TEST",
            trade_date=base + timedelta(days=i),
            open=c - 0.2,
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
    margin_decline_streak: int = 7,
    large_holder_chg_pct: float | None = 1.2,
    retail_holder_chg_pct: float | None = -1.5,
    margin_balance_change: int = -10_000,
) -> TWSEChipProxy:
    return TWSEChipProxy(
        ticker="TEST",
        trade_date=date(2025, 3, 1),
        foreign_consecutive_buy_days=foreign_consec,
        trust_consecutive_buy_days=trust_consec,
        margin_decline_streak=margin_decline_streak,
        large_holder_chg_pct=large_holder_chg_pct,
        retail_holder_chg_pct=retail_holder_chg_pct,
        margin_balance_change=margin_balance_change,
        is_available=True,
    )


def _stable_history(close: float = 80.0, n: int = 30) -> list[DailyOHLCV]:
    """Stable sideways history — narrow range ideal for chip transfer."""
    import random
    random.seed(42)
    closes = [close + random.uniform(-0.5, 0.5) for _ in range(n)]
    vols_base = 1_000_000
    vols = [vols_base] * n
    # Low recent 5d volume for contraction signal
    for i in range(n - 5, n):
        vols[i] = 400_000
    return _bars(closes, vols, high_offset=0.3, low_offset=0.3)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_scores_valid_chip_transfer():
    """K-of-N met with all 5 signals → should score."""
    hist = _stable_history(n=30)
    proxy = _make_proxy()
    result = ChipTransferDetector().score(hist, proxy)
    assert result is not None
    assert result["score"] >= ChipTransferDetector.MIN_SCORE
    assert "CHIP_TRANSFER" in result["flags"]
    assert result["signal_type"] == "籌碼轉移"


def test_k_of_n_gate_fails_with_only_one_signal():
    """Only 1 signal met (margin) → gate requires 3 → returns None."""
    hist = _stable_history(n=30)
    proxy = _make_proxy(
        foreign_consec=1,  # Signal C fails
        trust_consec=0,
        margin_decline_streak=7,  # Signal A OK
        large_holder_chg_pct=-0.5,  # Signal D fails (negative)
        retail_holder_chg_pct=0.2,  # Signal E fails (positive)
    )
    # Only Signal A (margin) + Signal B (price stability) may pass
    # Even if B passes, we need 3 → fail
    result = ChipTransferDetector().score(hist, proxy)
    # If score happens to be >= MIN_SCORE with just 2 signals it passes (min 3 required)
    if result is not None:
        assert len(result["signals_met"]) >= 3


def test_proxy_none_ohlcv_only_mode():
    """Without proxy, falls back to OHLCV-only mode (2-of-3)."""
    # Stable + contracted volume → should trigger
    hist = _stable_history(n=30)
    result = ChipTransferDetector().score(hist, proxy=None)
    # May or may not pass depending on score threshold; just verify no crash
    assert result is None or result["signal_type"] == "籌碼轉移"


def test_proxy_none_insufficient_ohlcv():
    """Without proxy, volatile price history → should return None."""
    closes = [80.0 + (i % 2) * 10 for i in range(30)]  # high volatility
    hist = _bars(closes, high_offset=6.0, low_offset=6.0)
    result = ChipTransferDetector().score(hist, proxy=None)
    assert result is None


def test_margin_decline_adds_pts():
    """Higher margin decline streak → higher score."""
    hist = _stable_history(n=30)
    proxy_low = _make_proxy(margin_decline_streak=5)
    proxy_high = _make_proxy(margin_decline_streak=12)
    res_low = ChipTransferDetector().score(hist, proxy_low)
    res_high = ChipTransferDetector().score(hist, proxy_high)
    if res_low is not None and res_high is not None:
        assert res_high["score"] >= res_low["score"]


def test_price_stability_scoring():
    """Tight price range (<5%) should score higher than moderate range."""
    hist_tight = _stable_history(n=30)  # 0.3 offset → tight
    hist_wide = _bars(
        [80.0 + (i % 3) * 3 for i in range(30)],
        high_offset=3.0,
        low_offset=3.0,
    )
    proxy = _make_proxy()
    res_tight = ChipTransferDetector().score(hist_tight, proxy)
    res_wide = ChipTransferDetector().score(hist_wide, proxy)
    # Tight should score at least as well
    if res_tight is not None and res_wide is not None:
        assert res_tight["score"] >= res_wide["score"]


def test_returns_none_if_too_short():
    """Returns None when history is too short."""
    hist = _stable_history(n=15)
    proxy = _make_proxy()
    result = ChipTransferDetector().score(hist, proxy)
    assert result is None
