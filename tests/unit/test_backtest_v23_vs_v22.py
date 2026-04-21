"""Unit tests for backtest_v23_vs_v22.py metric calculations.

Tests metric helpers in isolation — no API calls, no file I/O.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from backtest_v23_vs_v22 import (
    _confidence_to_tier,
    _resolve_sector_names,
    check_outcome,
    compute_confidence_distribution,
    compute_engine_metrics,
    load_historical_signals,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bar(ticker: str, trade_date: date, close: float, volume: int = 1000) -> object:
    """Return a minimal DailyOHLCV-like object."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from taiwan_stock_agent.domain.models import DailyOHLCV
    return DailyOHLCV(
        ticker=ticker,
        trade_date=trade_date,
        open=close,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=volume,
    )


def _make_bars(closes: list[float], start_date: date = date(2026, 1, 1)) -> list:
    """Build a list of DailyOHLCV bars from a list of closes."""
    return [
        _make_bar("TEST", start_date + timedelta(days=i), c)
        for i, c in enumerate(closes)
    ]


def _make_record(
    v22_confidence: int = 60,
    v22_action: str = "LONG",
    v23_confidence: int = 65,
    v23_action: str = "LONG",
    v23_gate_pass: bool = True,
    win: bool = True,
    max_return_pct: float = 0.05,
    days_to_breakout: int = 5,
    final_return_pct: float = 0.04,
    ticker: str = "TEST",
    signal_date: date = date(2026, 1, 10),
    industry: str = "半導體",
) -> dict:
    return {
        "signal_date": signal_date,
        "ticker": ticker,
        "industry": industry,
        "v22_confidence": v22_confidence,
        "v22_action": v22_action,
        "v23_confidence": v23_confidence,
        "v23_action": v23_action,
        "v23_gate_pass": v23_gate_pass,
        "win": win,
        "max_return_pct": max_return_pct,
        "days_to_breakout": days_to_breakout,
        "final_return_pct": final_return_pct,
        "entry_close": 100.0,
        "twenty_day_high": 108.0,
        "market": "TSE",
    }


# ---------------------------------------------------------------------------
# check_outcome tests
# ---------------------------------------------------------------------------

class TestCheckOutcome:
    def test_win_when_price_breaks_above_high(self):
        """Breakout above 20d high within window → win=True."""
        entry_close = 100.0
        twenty_day_high = 105.0
        # entry_delay=2: entry at bar[1] (close=102), outcome starts from bar[2]
        future_bars = _make_bars([101.0, 102.0, 103.0, 106.0, 104.0])
        result = check_outcome(entry_close, twenty_day_high, future_bars, entry_delay=2)
        assert result["win"] is True
        assert result["days_to_breakout"] == 2  # bar index 3 (0-based) = day 2 in outcome window

    def test_no_win_when_price_stays_below_high(self):
        """Price stays below threshold → win=False."""
        entry_close = 100.0
        twenty_day_high = 115.0  # far above
        future_bars = _make_bars([101.0, 102.0, 103.0, 104.0, 105.0, 106.0])
        result = check_outcome(entry_close, twenty_day_high, future_bars, entry_delay=1)
        assert result["win"] is False
        assert result["days_to_breakout"] == 0

    def test_empty_future_bars(self):
        """No future data → no win."""
        result = check_outcome(100.0, 105.0, [], entry_delay=2)
        assert result["win"] is False
        assert result["max_return_pct"] == 0.0
        assert result["final_return_pct"] == 0.0

    def test_max_return_calculated_correctly(self):
        """max_return_pct = max close in outcome window / entry_price - 1."""
        entry_close = 100.0
        twenty_day_high = 120.0  # never hit
        # entry_delay=1: entry at bar[0] (close=102), outcome = bars[1:]
        future_bars = _make_bars([102.0, 108.0, 110.0, 107.0])
        result = check_outcome(entry_close, twenty_day_high, future_bars, entry_delay=1)
        assert result["win"] is False
        # entry_price = bar[0].close = 102, max_close = 110
        expected_max = (110.0 - 102.0) / 102.0
        assert abs(result["max_return_pct"] - expected_max) < 1e-6

    def test_final_return_is_last_bar(self):
        """final_return_pct = last bar / entry_price - 1."""
        entry_close = 100.0
        twenty_day_high = 120.0
        future_bars = _make_bars([101.0, 108.0, 104.0])
        result = check_outcome(entry_close, twenty_day_high, future_bars, entry_delay=1)
        # entry = bar[0].close = 101, final = bar[-1].close = 104
        expected_final = (104.0 - 101.0) / 101.0
        assert abs(result["final_return_pct"] - expected_final) < 1e-6

    def test_entry_delay_zero_uses_first_bar(self):
        """entry_delay=0: entry at close=entry_close, outcome starts from bar[0]."""
        entry_close = 100.0
        twenty_day_high = 105.0
        future_bars = _make_bars([106.0, 104.0])
        result = check_outcome(entry_close, twenty_day_high, future_bars, entry_delay=0)
        assert result["win"] is True
        assert result["days_to_breakout"] == 1

    def test_breakout_threshold_99_percent(self):
        """Breakout needs close >= twenty_day_high * 0.99, not exactly equal."""
        entry_close = 100.0
        twenty_day_high = 110.0
        # 109.0 / 110.0 = 99.09% > 99%
        future_bars = _make_bars([101.0, 109.0])
        result = check_outcome(entry_close, twenty_day_high, future_bars,
                               entry_delay=1, breakout_threshold=0.99)
        assert result["win"] is True

    def test_entry_delay_exceeds_available_bars(self):
        """entry_delay larger than available bars → no win."""
        entry_close = 100.0
        twenty_day_high = 105.0
        future_bars = _make_bars([106.0])  # only 1 bar, delay=2
        result = check_outcome(entry_close, twenty_day_high, future_bars, entry_delay=2)
        assert result["win"] is False


# ---------------------------------------------------------------------------
# compute_engine_metrics tests
# ---------------------------------------------------------------------------

class TestComputeEngineMetrics:
    def test_empty_records(self):
        """Empty input → n=0, all metrics None."""
        result = compute_engine_metrics([], "v22")
        assert result["n"] == 0
        assert result["win_rate"] is None
        assert result["false_rate"] is None

    def test_v22_counts_long_and_watch(self):
        """v2.2 active = LONG or WATCH signals."""
        records = [
            _make_record(v22_action="LONG"),
            _make_record(v22_action="WATCH"),
            _make_record(v22_action="CAUTION"),  # excluded
        ]
        result = compute_engine_metrics(records, "v22")
        assert result["n"] == 2

    def test_v23_counts_gate_passed(self):
        """v2.3 active = gate_pass=True signals."""
        records = [
            _make_record(v23_gate_pass=True),
            _make_record(v23_gate_pass=True),
            _make_record(v23_gate_pass=False),  # excluded
        ]
        result = compute_engine_metrics(records, "v23")
        assert result["n"] == 2

    def test_win_rate_calculation(self):
        """win_rate = wins / total active."""
        records = [
            _make_record(v22_action="LONG", win=True),
            _make_record(v22_action="LONG", win=True),
            _make_record(v22_action="LONG", win=False),
            _make_record(v22_action="WATCH", win=False),
        ]
        result = compute_engine_metrics(records, "v22")
        assert result["n"] == 4
        assert abs(result["win_rate"] - 0.5) < 1e-9
        assert abs(result["false_rate"] - 0.5) < 1e-9

    def test_avg_upside_positive(self):
        """avg_upside = mean of max_return_pct across active signals."""
        records = [
            _make_record(v22_action="LONG", max_return_pct=0.10),
            _make_record(v22_action="LONG", max_return_pct=0.06),
        ]
        result = compute_engine_metrics(records, "v22")
        assert abs(result["avg_upside"] - 0.08) < 1e-9

    def test_avg_days_to_breakout_only_winners(self):
        """avg_days_to_breakout computed from winning trades only."""
        records = [
            _make_record(v22_action="LONG", win=True, days_to_breakout=3),
            _make_record(v22_action="LONG", win=True, days_to_breakout=7),
            _make_record(v22_action="LONG", win=False, days_to_breakout=0),
        ]
        result = compute_engine_metrics(records, "v22")
        assert abs(result["avg_days_to_breakout"] - 5.0) < 1e-9

    def test_avg_confidence(self):
        """avg_confidence = mean confidence of active signals."""
        records = [
            _make_record(v23_confidence=60, v23_gate_pass=True),
            _make_record(v23_confidence=80, v23_gate_pass=True),
        ]
        result = compute_engine_metrics(records, "v23")
        assert abs(result["avg_confidence"] - 70.0) < 1e-9

    def test_pct_long_and_watch(self):
        """pct_long and pct_watch sum to ≤ 1."""
        records = [
            _make_record(v22_action="LONG"),
            _make_record(v22_action="LONG"),
            _make_record(v22_action="WATCH"),
        ]
        result = compute_engine_metrics(records, "v22")
        assert abs(result["pct_long"] - 2 / 3) < 1e-9
        assert abs(result["pct_watch"] - 1 / 3) < 1e-9
        assert result["pct_long"] + result["pct_watch"] <= 1.0 + 1e-9

    def test_win_rate_perfect(self):
        records = [_make_record(v22_action="LONG", win=True) for _ in range(5)]
        result = compute_engine_metrics(records, "v22")
        assert result["win_rate"] == 1.0
        assert result["false_rate"] == 0.0

    def test_win_rate_zero(self):
        records = [_make_record(v22_action="LONG", win=False) for _ in range(3)]
        result = compute_engine_metrics(records, "v22")
        assert result["win_rate"] == 0.0
        assert result["false_rate"] == 1.0


# ---------------------------------------------------------------------------
# compute_confidence_distribution tests
# ---------------------------------------------------------------------------

class TestComputeConfidenceDistribution:
    def test_buckets_correct(self):
        records = [
            _make_record(v23_confidence=35),   # 0-39
            _make_record(v23_confidence=45),   # 40-49
            _make_record(v23_confidence=55),   # 50-59
            _make_record(v23_confidence=65),   # 60-69
            _make_record(v23_confidence=75),   # 70-79
            _make_record(v23_confidence=85),   # 80+
            _make_record(v23_confidence=90),   # 80+
        ]
        dist = compute_confidence_distribution(records, "v23")
        assert dist["0-39"] == 1
        assert dist["40-49"] == 1
        assert dist["50-59"] == 1
        assert dist["60-69"] == 1
        assert dist["70-79"] == 1
        assert dist["80+"] == 2

    def test_empty_records(self):
        dist = compute_confidence_distribution([], "v22")
        assert all(v == 0 for v in dist.values())

    def test_boundary_values(self):
        """40 → 40-49 bucket; 80 → 80+ bucket."""
        records = [
            _make_record(v22_confidence=40),
            _make_record(v22_confidence=80),
        ]
        dist = compute_confidence_distribution(records, "v22")
        assert dist["40-49"] == 1
        assert dist["80+"] == 1

    def test_all_in_one_bucket(self):
        records = [_make_record(v23_confidence=70) for _ in range(5)]
        dist = compute_confidence_distribution(records, "v23")
        assert dist["70-79"] == 5
        assert dist["0-39"] == 0
        assert sum(dist.values()) == 5


# ---------------------------------------------------------------------------
# Integration-style test for load_historical_signals (mocked CSV)
# ---------------------------------------------------------------------------

class TestLoadHistoricalSignals:
    def test_filters_by_min_confidence(self, tmp_path):
        """Signals below min_confidence are excluded."""
        csv_content = (
            "scan_date,analysis_date,ticker,action,confidence,trend_score,free_tier,halt,"
            "entry_bid,stop_loss,target,momentum,chip_analysis,risk_factors,data_quality_flags\n"
            "2026-04-01,2026-03-31,2330,LONG,75,30,True,False,500,480,550,,,,\n"
            "2026-04-01,2026-03-31,2317,WATCH,30,20,True,False,200,190,220,,,,\n"
        )
        scan_file = tmp_path / "scan_2026-04-01.csv"
        scan_file.write_text(csv_content, encoding="utf-8")

        with (
            patch("backtest_v23_vs_v22._SCANS_DIR", tmp_path),
            patch("backtest_v23_vs_v22._WATCHLIST_CACHE_DIR", tmp_path),
            patch("backtest_v23_vs_v22._load_industry_map", return_value={}),
        ):
            from backtest_v23_vs_v22 import load_historical_signals
            signals = load_historical_signals(min_confidence=50)

        tickers = [s["ticker"] for s in signals]
        assert "2330" in tickers
        assert "2317" not in tickers

    def test_filters_caution_action(self, tmp_path):
        """CAUTION signals are excluded."""
        csv_content = (
            "scan_date,analysis_date,ticker,action,confidence,trend_score,free_tier,halt,"
            "entry_bid,stop_loss,target,momentum,chip_analysis,risk_factors,data_quality_flags\n"
            "2026-04-01,2026-03-31,2330,CAUTION,70,30,True,False,500,480,550,,,,\n"
        )
        scan_file = tmp_path / "scan_2026-04-01.csv"
        scan_file.write_text(csv_content, encoding="utf-8")

        with (
            patch("backtest_v23_vs_v22._SCANS_DIR", tmp_path),
            patch("backtest_v23_vs_v22._WATCHLIST_CACHE_DIR", tmp_path),
            patch("backtest_v23_vs_v22._load_industry_map", return_value={}),
        ):
            from backtest_v23_vs_v22 import load_historical_signals
            signals = load_historical_signals(min_confidence=0)

        assert len(signals) == 0

    def test_deduplication_keeps_highest_confidence(self, tmp_path):
        """Duplicate (ticker, date) → keep highest confidence."""
        csv_content_a = (
            "scan_date,analysis_date,ticker,action,confidence,trend_score,free_tier,halt,"
            "entry_bid,stop_loss,target,momentum,chip_analysis,risk_factors,data_quality_flags\n"
            "2026-04-01,2026-03-31,2330,LONG,60,30,True,False,500,480,550,,,,\n"
        )
        csv_content_b = (
            "scan_date,analysis_date,ticker,action,confidence,trend_score,free_tier,halt,"
            "entry_bid,stop_loss,target,momentum,chip_analysis,risk_factors,data_quality_flags\n"
            "2026-04-01,2026-03-31,2330,LONG,75,32,True,False,500,480,550,,,,\n"
        )
        # Both files cover the same date, create two separate "date" files
        (tmp_path / "scan_2026-04-01.csv").write_text(csv_content_a, encoding="utf-8")
        # Use a different filename (still parses same analysis_date)
        (tmp_path / "scan_2026-04-02.csv").write_text(csv_content_b, encoding="utf-8")

        with (
            patch("backtest_v23_vs_v22._SCANS_DIR", tmp_path),
            patch("backtest_v23_vs_v22._WATCHLIST_CACHE_DIR", tmp_path),
            patch("backtest_v23_vs_v22._load_industry_map", return_value={}),
        ):
            from backtest_v23_vs_v22 import load_historical_signals
            signals = load_historical_signals(min_confidence=0)

        # (2330, 2026-03-31) appears twice — keep highest confidence
        matching = [s for s in signals if s["ticker"] == "2330"]
        assert len(matching) == 1
        assert matching[0]["v22_confidence"] == 75

    def test_filters_by_date_range(self, tmp_path):
        """Signals outside [date_from, date_to] are excluded."""
        csv_content = (
            "scan_date,analysis_date,ticker,action,confidence,trend_score,free_tier,halt,"
            "entry_bid,stop_loss,target,momentum,chip_analysis,risk_factors,data_quality_flags\n"
            "2026-04-02,2026-04-01,2330,LONG,70,30,True,False,500,480,550,,,,\n"
            "2026-04-10,2026-04-09,2317,LONG,65,28,True,False,200,190,220,,,,\n"
        )
        (tmp_path / "scan_2026-04-02.csv").write_text(csv_content.split("\n")[0] + "\n" + csv_content.split("\n")[1], encoding="utf-8")
        (tmp_path / "scan_2026-04-10.csv").write_text(csv_content.split("\n")[0] + "\n" + csv_content.split("\n")[2], encoding="utf-8")

        with (
            patch("backtest_v23_vs_v22._SCANS_DIR", tmp_path),
            patch("backtest_v23_vs_v22._WATCHLIST_CACHE_DIR", tmp_path),
            patch("backtest_v23_vs_v22._load_industry_map", return_value={}),
        ):
            signals = load_historical_signals(
                date_from=date(2026, 4, 1),
                date_to=date(2026, 4, 5),
                min_confidence=0,
            )

        tickers = [s["ticker"] for s in signals]
        assert "2330" in tickers
        assert "2317" not in tickers

    def test_filters_by_sector(self, tmp_path):
        """Only tickers in the selected industry are returned."""
        csv_content = (
            "scan_date,analysis_date,ticker,action,confidence,trend_score,free_tier,halt,"
            "entry_bid,stop_loss,target,momentum,chip_analysis,risk_factors,data_quality_flags\n"
            "2026-04-01,2026-03-31,2330,LONG,70,30,True,False,500,480,550,,,,\n"
            "2026-04-01,2026-03-31,2317,LONG,65,28,True,False,200,190,220,,,,\n"
        )
        scan_file = tmp_path / "scan_2026-04-01.csv"
        scan_file.write_text(csv_content, encoding="utf-8")

        # 2330 → 半導體, 2317 → 電子通路
        industry_map = {"2330": "半導體", "2317": "電子通路"}

        with (
            patch("backtest_v23_vs_v22._SCANS_DIR", tmp_path),
            patch("backtest_v23_vs_v22._WATCHLIST_CACHE_DIR", tmp_path),
            patch("backtest_v23_vs_v22._load_industry_map", return_value=industry_map),
        ):
            # 半導體 is the 1st industry alphabetically between the two
            signals = load_historical_signals(min_confidence=0, sectors=[1])

        tickers = [s["ticker"] for s in signals]
        assert "2330" in tickers
        assert "2317" not in tickers

    def test_malformed_csv_date_falls_back_to_file_date(self, tmp_path):
        """Rows with an unparseable analysis_date use the file date instead."""
        csv_content = (
            "scan_date,analysis_date,ticker,action,confidence,trend_score,free_tier,halt,"
            "entry_bid,stop_loss,target,momentum,chip_analysis,risk_factors,data_quality_flags\n"
            "2026-04-01,NOT_A_DATE,2330,LONG,70,30,True,False,500,480,550,,,,\n"
        )
        scan_file = tmp_path / "scan_2026-04-01.csv"
        scan_file.write_text(csv_content, encoding="utf-8")

        with (
            patch("backtest_v23_vs_v22._SCANS_DIR", tmp_path),
            patch("backtest_v23_vs_v22._WATCHLIST_CACHE_DIR", tmp_path),
            patch("backtest_v23_vs_v22._load_industry_map", return_value={}),
        ):
            signals = load_historical_signals(min_confidence=0)

        assert len(signals) == 1
        # Falls back to the file date (scan_2026-04-01 → 2026-04-01)
        assert signals[0]["signal_date"] == date(2026, 4, 1)


# ---------------------------------------------------------------------------
# _confidence_to_tier tests
# ---------------------------------------------------------------------------

class TestConfidenceToTier:
    def test_below_40(self):
        assert _confidence_to_tier(0) == "0-39"
        assert _confidence_to_tier(39) == "0-39"

    def test_40_to_49(self):
        assert _confidence_to_tier(40) == "40-49"
        assert _confidence_to_tier(49) == "40-49"

    def test_50_to_59(self):
        assert _confidence_to_tier(50) == "50-59"
        assert _confidence_to_tier(59) == "50-59"

    def test_60_to_69(self):
        assert _confidence_to_tier(60) == "60-69"
        assert _confidence_to_tier(69) == "60-69"

    def test_70_to_79(self):
        assert _confidence_to_tier(70) == "70-79"
        assert _confidence_to_tier(79) == "70-79"

    def test_80_plus(self):
        assert _confidence_to_tier(80) == "80+"
        assert _confidence_to_tier(100) == "80+"


# ---------------------------------------------------------------------------
# _resolve_sector_names tests
# ---------------------------------------------------------------------------

class TestResolveSectorNames:
    def test_returns_correct_industry_names(self):
        """1-based indices map to alphabetically-sorted industry names."""
        industry_map = {
            "2330": "半導體",
            "2317": "電子通路",
            "6505": "光電",
        }
        # Sorted: 光電(1), 半導體(2), 電子通路(3)
        result = _resolve_sector_names(industry_map, [1, 2])
        assert result == {"光電", "半導體"}

    def test_out_of_range_index_is_ignored(self):
        """Index beyond the industry count is silently skipped."""
        industry_map = {"2330": "半導體"}
        result = _resolve_sector_names(industry_map, [99])
        assert result == set()

    def test_empty_industry_map(self):
        result = _resolve_sector_names({}, [1])
        assert result == set()

    def test_single_industry(self):
        industry_map = {"2330": "半導體", "2454": "半導體"}
        result = _resolve_sector_names(industry_map, [1])
        assert result == {"半導體"}
