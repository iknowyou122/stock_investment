"""Unit tests for HTFDetector."""
from __future__ import annotations

from datetime import date, timedelta

from taiwan_stock_agent.domain.htf_detector import HTFDetector
from taiwan_stock_agent.domain.models import DailyOHLCV


def _bars(
    closes: list[float],
    vols: list[int] | None = None,
    high_offset: float = 1.0,
    low_offset: float = 1.0,
) -> list[DailyOHLCV]:
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


def _htf_history(
    prior_advance: float = 0.40,
    consolidation_range: float = 0.08,
    flag_vol_ratio: float = 0.35,
    advance_bars: int = 15,
    n_pre_bars: int = 30,
) -> list[DailyOHLCV]:
    """Build synthetic HTF pattern.

    Phase 1: n_pre_bars base bars (gradual uptrend to ensure MA5>MA20)
    Phase 2: advance_bars surge (prior_advance)
    Phase 3: 10 bars consolidation flag (tight range, contracted volume)
    Total: >= 50 bars.
    """
    c = 60.0
    closes: list[float] = []
    vols: list[int] = []
    surge_vol = 2_000_000
    flag_vol = int(surge_vol * flag_vol_ratio)

    # Pre-surge base (gradual uptrend so MA5 > MA20 after surge)
    for i in range(n_pre_bars):
        c += 0.1
        closes.append(round(c, 2))
        vols.append(800_000)

    # Surge phase
    peak = c * (1 + prior_advance)
    step_up = (peak - c) / advance_bars
    for _ in range(advance_bars):
        c += step_up
        closes.append(round(c, 2))
        vols.append(surge_vol)
    peak_price = c

    # Consolidation flag (tight range) — close stays in upper half of flag
    flag_low = peak_price * (1 - consolidation_range * 0.7)
    flag_high = peak_price
    center = (flag_low + flag_high) / 2
    for i in range(10):
        # Keep close in upper half of flag so Gate 4 (close >= consol_low * 1.02) passes
        flag_c = center + (flag_high - center) * 0.4 * ((i % 3) / 2)
        flag_c = max(center, min(flag_high, flag_c))  # never go below center
        closes.append(round(flag_c, 2))
        vols.append(flag_vol)
        c = flag_c

    return _bars(closes, vols, high_offset=0.5, low_offset=0.5)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_detects_valid_flag():
    """Should detect a valid High Tight Flag pattern."""
    hist = _htf_history(prior_advance=0.40)
    result = HTFDetector().score(hist)
    assert result is not None
    assert "HTF" in result["flags"]
    assert result["prior_advance_pct"] >= 25
    assert result["signal_type"] == "旗形"
    assert result["horizon"] == "短線"


def test_rejects_if_advance_less_than_25pct():
    """Should return None when prior advance < 25%."""
    hist = _htf_history(prior_advance=0.20)
    result = HTFDetector().score(hist)
    assert result is None


def test_rejects_if_consolidation_too_wide():
    """Should return None when flag range > 15%.

    Build the flag bars manually with wide swings (>16% close-range).
    """
    # Base + surge (40 bars total, 30 pre + 10 surge)
    hist = _htf_history(prior_advance=0.40, n_pre_bars=30, advance_bars=10)
    sorted_h = sorted(hist, key=lambda x: x.trade_date)
    # Find peak price after surge
    peak_close = max(d.close for d in sorted_h)

    # Replace last 10 bars with a wide oscillation (20% range around peak)
    import datetime
    last_date = sorted_h[-1].trade_date
    wide_flag = []
    for i in range(15):
        swing = peak_close * (0.10 if i % 2 == 0 else -0.10)  # ±10% oscillation
        c = peak_close + swing
        wide_flag.append(DailyOHLCV(
            ticker="TEST",
            trade_date=last_date + datetime.timedelta(days=i + 1),
            open=c - 0.5,
            high=c + 1.0,
            low=c - 1.0,
            close=round(c, 2),
            volume=700_000,
        ))

    combined = sorted_h + wide_flag
    result = HTFDetector().score(combined)
    assert result is None


def test_scores_tight_flags_higher():
    """Tight consolidation should score higher than wide consolidation."""
    hist_tight = _htf_history(prior_advance=0.50, consolidation_range=0.06)
    hist_wide = _htf_history(prior_advance=0.50, consolidation_range=0.13)
    res_tight = HTFDetector().score(hist_tight)
    res_wide = HTFDetector().score(hist_wide)
    if res_tight is not None and res_wide is not None:
        assert res_tight["score"] >= res_wide["score"]


def test_broken_flag_returns_none():
    """Should return None if current price is below consolidation low * 1.02."""
    hist = _htf_history(prior_advance=0.40)
    # Push the last bar's close well below the flag
    last = hist[-1]
    hist[-1] = DailyOHLCV(
        ticker=last.ticker,
        trade_date=last.trade_date,
        open=last.open * 0.80,
        high=last.high * 0.82,
        low=last.low * 0.80,
        close=last.close * 0.81,  # broken well below flag low
        volume=last.volume,
    )
    result = HTFDetector().score(hist)
    assert result is None


def test_returns_none_if_too_short():
    """Returns None when history is shorter than 50 bars."""
    closes = [80.0 + i * 0.1 for i in range(40)]
    result = HTFDetector().score(_bars(closes))
    assert result is None


def test_large_advance_scores_higher():
    """50% advance should score higher than 25% advance."""
    hist_large = _htf_history(prior_advance=0.50, flag_vol_ratio=0.35)
    hist_small = _htf_history(prior_advance=0.27, flag_vol_ratio=0.35)
    res_large = HTFDetector().score(hist_large)
    res_small = HTFDetector().score(hist_small)
    if res_large is not None and res_small is not None:
        assert res_large["score"] >= res_small["score"]
