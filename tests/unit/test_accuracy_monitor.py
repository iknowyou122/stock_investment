"""Unit tests for accuracy_monitor.py.

Tests cover:
- Win-rate calculation
- Rolling metric computation
- Cache loading / saving
- Stratification by industry, market, confidence tier
- CSV export logic
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

# Allow importing from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from accuracy_monitor import (
    CacheStore,
    SignalRecord,
    compute_rolling_win_rate,
    compute_win_rate,
    stratify_by_field,
    load_scan_signals,
    _confidence_to_tier,
    _is_pending,
    _PENDING_CUTOFF_DAYS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_record(
    ticker: str = "2330",
    signal_date: str = "2026-04-01",
    confidence: int = 70,
    action: str = "LONG",
    market: str = "TSE",
    industry: str = "半導體",
    actual_breakout: bool = True,
    days_to_breakout: int = 3,
    max_price: float = 600.0,
    entry_price: float = 580.0,
    twenty_day_high: float = 595.0,
    upside_pct: float = 2.0,
    pending: bool = False,
) -> SignalRecord:
    return SignalRecord(
        signal_id=f"{ticker}_{signal_date}",
        ticker=ticker,
        signal_date=date.fromisoformat(signal_date),
        confidence=confidence,
        action=action,
        market=market,
        industry=industry,
        entry_price=entry_price,
        twenty_day_high=twenty_day_high,
        actual_breakout=actual_breakout,
        days_to_breakout=days_to_breakout,
        max_price=max_price,
        upside_pct=upside_pct,
        pending=pending,
    )


# ---------------------------------------------------------------------------
# compute_win_rate
# ---------------------------------------------------------------------------

class TestComputeWinRate:
    def test_all_wins(self):
        records = [_make_record(actual_breakout=True) for _ in range(5)]
        wr, n = compute_win_rate(records)
        assert wr == 1.0
        assert n == 5

    def test_no_wins(self):
        records = [_make_record(actual_breakout=False) for _ in range(4)]
        wr, n = compute_win_rate(records)
        assert wr == 0.0
        assert n == 4

    def test_mixed(self):
        records = (
            [_make_record(actual_breakout=True)] * 3
            + [_make_record(actual_breakout=False)] * 1
        )
        wr, n = compute_win_rate(records)
        assert wr == pytest.approx(0.75)
        assert n == 4

    def test_empty(self):
        wr, n = compute_win_rate([])
        assert wr is None
        assert n == 0

    def test_pending_excluded(self):
        records = [
            _make_record(actual_breakout=True),
            _make_record(actual_breakout=False, pending=True),  # should be excluded
        ]
        wr, n = compute_win_rate(records)
        assert wr == 1.0
        assert n == 1


# ---------------------------------------------------------------------------
# compute_rolling_win_rate
# ---------------------------------------------------------------------------

class TestComputeRollingWinRate:
    def _make_sequence(self, wins: int, total: int) -> list[SignalRecord]:
        """Produce `total` records with exactly `wins` wins, sorted by signal_date."""
        records = []
        base = date(2026, 1, 1)
        from datetime import timedelta

        for i in range(total):
            d = (base + timedelta(days=i)).isoformat()
            records.append(_make_record(
                ticker=str(i),
                signal_date=d,
                actual_breakout=(i < wins),
            ))
        return records

    def test_rolling_20_last_n(self):
        records = self._make_sequence(wins=15, total=30)
        # Last 20 records: indices 10..29 → wins among those: 10..14 = 5 wins
        roll = compute_rolling_win_rate(records, window=20)
        assert roll is not None
        assert roll == pytest.approx(5 / 20)

    def test_rolling_returns_none_when_not_enough(self):
        records = self._make_sequence(wins=5, total=10)
        roll = compute_rolling_win_rate(records, window=20)
        assert roll is None

    def test_rolling_exact_window(self):
        records = self._make_sequence(wins=10, total=20)
        roll = compute_rolling_win_rate(records, window=20)
        assert roll == pytest.approx(10 / 20)


# ---------------------------------------------------------------------------
# stratify_by_field
# ---------------------------------------------------------------------------

class TestStratifyByField:
    def _make_industry_set(self) -> list[SignalRecord]:
        industries = ["半導體", "半導體", "光電", "電子"]
        bools = [True, False, True, True]
        records = []
        for i, (ind, brk) in enumerate(zip(industries, bools)):
            records.append(_make_record(
                ticker=str(i), industry=ind, actual_breakout=brk
            ))
        return records

    def test_stratify_industry(self):
        records = self._make_industry_set()
        result = stratify_by_field(records, field="industry")
        assert "半導體" in result
        wr, n = result["半導體"]
        assert n == 2
        assert wr == pytest.approx(0.5)

    def test_stratify_market(self):
        records = [
            _make_record(ticker="A", market="TSE", actual_breakout=True),
            _make_record(ticker="B", market="TSE", actual_breakout=False),
            _make_record(ticker="C", market="TPEx", actual_breakout=True),
        ]
        result = stratify_by_field(records, field="market")
        tse_wr, tse_n = result["TSE"]
        assert tse_n == 2
        assert tse_wr == pytest.approx(0.5)
        tpex_wr, tpex_n = result["TPEx"]
        assert tpex_n == 1
        assert tpex_wr == pytest.approx(1.0)

    def test_stratify_empty(self):
        result = stratify_by_field([], field="industry")
        assert result == {}


# ---------------------------------------------------------------------------
# _confidence_to_tier
# ---------------------------------------------------------------------------

class TestConfidenceTier:
    def test_tiers(self):
        assert _confidence_to_tier(30) == "0-39"
        assert _confidence_to_tier(39) == "0-39"
        assert _confidence_to_tier(40) == "40-49"
        assert _confidence_to_tier(49) == "40-49"
        assert _confidence_to_tier(50) == "50-59"
        assert _confidence_to_tier(60) == "60-69"
        assert _confidence_to_tier(70) == "70-79"
        assert _confidence_to_tier(80) == "80+"
        assert _confidence_to_tier(99) == "80+"


# ---------------------------------------------------------------------------
# CacheStore
# ---------------------------------------------------------------------------

class TestCacheStore:
    def test_round_trip(self, tmp_path):
        cache_path = tmp_path / "cache.json"
        store = CacheStore(cache_path)

        rec = _make_record()
        store.upsert(rec)
        store.save()

        # Reload
        store2 = CacheStore(cache_path)
        loaded = store2.get(rec.signal_id)
        assert loaded is not None
        assert loaded.ticker == rec.ticker
        assert loaded.signal_date == rec.signal_date
        assert loaded.actual_breakout == rec.actual_breakout
        assert loaded.confidence == rec.confidence

    def test_save_includes_last_updated(self, tmp_path):
        cache_path = tmp_path / "cache.json"
        store = CacheStore(cache_path)
        store.upsert(_make_record())
        store.save()

        with open(cache_path) as f:
            raw = json.load(f)
        assert "last_updated" in raw
        # Verify ISO-parseable
        datetime.fromisoformat(raw["last_updated"])

    def test_get_missing_returns_none(self, tmp_path):
        cache_path = tmp_path / "does_not_exist.json"
        store = CacheStore(cache_path)
        assert store.get("nonexistent") is None

    def test_load_missing_file(self, tmp_path):
        cache_path = tmp_path / "no_file.json"
        store = CacheStore(cache_path)
        assert len(store.all()) == 0

    def test_upsert_overwrites(self, tmp_path):
        cache_path = tmp_path / "cache.json"
        store = CacheStore(cache_path)

        rec = _make_record()
        store.upsert(rec)

        # Upsert an updated version
        updated = _make_record(days_to_breakout=7, actual_breakout=False)
        store.upsert(updated)

        loaded = store.get(rec.signal_id)
        assert loaded is not None
        assert loaded.days_to_breakout == 7
        assert loaded.actual_breakout is False

    def test_pending_record_marked(self, tmp_path):
        cache_path = tmp_path / "cache.json"
        store = CacheStore(cache_path)
        rec = _make_record(pending=True, actual_breakout=False)
        store.upsert(rec)
        store.save()

        store2 = CacheStore(cache_path)
        loaded = store2.get(rec.signal_id)
        assert loaded.pending is True

    def test_all_returns_all_records(self, tmp_path):
        cache_path = tmp_path / "cache.json"
        store = CacheStore(cache_path)

        for i in range(5):
            store.upsert(_make_record(ticker=str(i), signal_date=f"2026-04-0{i+1}"))

        assert len(store.all()) == 5


# ---------------------------------------------------------------------------
# load_scan_signals
# ---------------------------------------------------------------------------

class TestLoadScanSignals:
    def _write_scan_csv(self, path: Path, rows: list[dict]) -> None:
        fieldnames = [
            "scan_date", "analysis_date", "ticker", "action", "confidence",
            "trend_score", "free_tier", "halt", "entry_bid", "stop_loss",
            "target", "momentum", "chip_analysis", "risk_factors", "data_quality_flags",
        ]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def test_load_basic(self, tmp_path):
        scan_dir = tmp_path / "scans"
        scan_dir.mkdir()
        csv_path = scan_dir / "scan_2026-04-10.csv"
        self._write_scan_csv(csv_path, [
            {
                "scan_date": "2026-04-11",
                "analysis_date": "2026-04-10",
                "ticker": "2330",
                "action": "LONG",
                "confidence": "75",
                "entry_bid": "585.0",
            },
        ])
        signals = load_scan_signals(scan_dir=scan_dir)
        assert len(signals) == 1
        assert signals[0]["ticker"] == "2330"
        assert signals[0]["confidence"] == 75

    def test_skip_caution(self, tmp_path):
        scan_dir = tmp_path / "scans"
        scan_dir.mkdir()
        csv_path = scan_dir / "scan_2026-04-10.csv"
        self._write_scan_csv(csv_path, [
            {
                "scan_date": "2026-04-11",
                "analysis_date": "2026-04-10",
                "ticker": "2330",
                "action": "CAUTION",
                "confidence": "50",
                "entry_bid": "585.0",
            },
        ])
        signals = load_scan_signals(scan_dir=scan_dir)
        assert len(signals) == 0

    def test_deduplicates_by_ticker_date(self, tmp_path):
        scan_dir = tmp_path / "scans"
        scan_dir.mkdir()
        csv_path = scan_dir / "scan_2026-04-10.csv"
        self._write_scan_csv(csv_path, [
            {
                "scan_date": "2026-04-11",
                "analysis_date": "2026-04-10",
                "ticker": "2330",
                "action": "LONG",
                "confidence": "75",
                "entry_bid": "585.0",
            },
            {
                "scan_date": "2026-04-11",
                "analysis_date": "2026-04-10",
                "ticker": "2330",
                "action": "LONG",
                "confidence": "60",
                "entry_bid": "585.0",
            },
        ])
        signals = load_scan_signals(scan_dir=scan_dir)
        assert len(signals) == 1
        # Keeps higher confidence
        assert signals[0]["confidence"] == 75

    def test_filter_by_date_range(self, tmp_path):
        scan_dir = tmp_path / "scans"
        scan_dir.mkdir()
        for d in ["2026-04-05", "2026-04-10", "2026-04-15"]:
            p = scan_dir / f"scan_{d}.csv"
            self._write_scan_csv(p, [{
                "scan_date": d,
                "analysis_date": d,
                "ticker": "2330",
                "action": "LONG",
                "confidence": "70",
                "entry_bid": "580.0",
            }])

        signals = load_scan_signals(
            scan_dir=scan_dir,
            date_from=date(2026, 4, 8),
            date_to=date(2026, 4, 12),
        )
        assert len(signals) == 1
        assert signals[0]["signal_date"] == date(2026, 4, 10)

    def test_no_scan_dir(self, tmp_path):
        missing_dir = tmp_path / "nonexistent"
        signals = load_scan_signals(scan_dir=missing_dir)
        assert signals == []


# ---------------------------------------------------------------------------
# _is_pending boundary tests
# ---------------------------------------------------------------------------

class TestIsPending:
    """
    _PENDING_CUTOFF_DAYS = 10.
    Signal is pending when (today - signal_date).days < 10.
    """

    def test_same_day_is_pending(self, monkeypatch):
        """T+0: signal fired today — still within window."""
        today = date(2026, 4, 21)
        monkeypatch.setattr(
            "accuracy_monitor.date",
            type("_MockDate", (), {"today": staticmethod(lambda: today)}),
        )
        assert _is_pending(today) is True

    def test_t5_is_pending(self, monkeypatch):
        """T+5: 5 days ago — still inside the 10-day window."""
        today = date(2026, 4, 21)
        monkeypatch.setattr(
            "accuracy_monitor.date",
            type("_MockDate", (), {"today": staticmethod(lambda: today)}),
        )
        signal_date = today - timedelta(days=5)
        assert _is_pending(signal_date) is True

    def test_t9_is_pending(self, monkeypatch):
        """T+9: one day before cutoff — still pending."""
        today = date(2026, 4, 21)
        monkeypatch.setattr(
            "accuracy_monitor.date",
            type("_MockDate", (), {"today": staticmethod(lambda: today)}),
        )
        signal_date = today - timedelta(days=_PENDING_CUTOFF_DAYS - 1)
        assert _is_pending(signal_date) is True

    def test_t10_is_settled(self, monkeypatch):
        """T+10: exactly at cutoff — window has closed, no longer pending."""
        today = date(2026, 4, 21)
        monkeypatch.setattr(
            "accuracy_monitor.date",
            type("_MockDate", (), {"today": staticmethod(lambda: today)}),
        )
        signal_date = today - timedelta(days=_PENDING_CUTOFF_DAYS)
        assert _is_pending(signal_date) is False

    def test_t11_is_settled(self, monkeypatch):
        """T+11: one day past cutoff — settled."""
        today = date(2026, 4, 21)
        monkeypatch.setattr(
            "accuracy_monitor.date",
            type("_MockDate", (), {"today": staticmethod(lambda: today)}),
        )
        signal_date = today - timedelta(days=_PENDING_CUTOFF_DAYS + 1)
        assert _is_pending(signal_date) is False
