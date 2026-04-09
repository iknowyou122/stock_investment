"""Unit tests for trajectory-aware persistence bonus."""
from __future__ import annotations

import csv
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from batch_scan import _load_recent_csvs, _apply_persistence_bonus


CSV_FIELDS = [
    "scan_date", "analysis_date", "ticker", "action", "confidence",
    "free_tier", "halt", "entry_bid", "stop_loss", "target",
    "momentum", "chip_analysis", "risk_factors", "data_quality_flags",
]


def _write_csv(data_dir: Path, scan_date: date, rows: list[dict]) -> None:
    csv_path = data_dir / f"scan_{scan_date}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "scan_date": scan_date.isoformat(),
                "analysis_date": scan_date.isoformat(),
                "ticker": r["ticker"],
                "action": "LONG",
                "confidence": r["confidence"],
                "free_tier": "",
                "halt": "False",
                "entry_bid": "100",
                "stop_loss": "95",
                "target": "110",
                "momentum": "",
                "chip_analysis": "",
                "risk_factors": "",
                "data_quality_flags": "",
            })


def _make_result(ticker: str, confidence: int) -> dict:
    return {
        "ticker": ticker,
        "confidence": confidence,
        "halt": False,
        "error": None,
        "flags": [],
    }


class TestLoadRecentCsvs:
    def test_loads_three_days(self, tmp_path):
        # Monday=2026-04-06, Tue=07, Wed=08, Thu=09(analysis)
        for i, d in enumerate([date(2026, 4, 6), date(2026, 4, 7), date(2026, 4, 8)]):
            _write_csv(tmp_path, d, [{"ticker": "2330", "confidence": 50 + i * 5}])

        csvs = _load_recent_csvs(date(2026, 4, 9), tmp_path, lookback=3)
        assert len(csvs) == 3
        # old → new
        assert csvs[0]["2330"] == 50
        assert csvs[1]["2330"] == 55
        assert csvs[2]["2330"] == 60

    def test_skips_weekends(self, tmp_path):
        # Thu=2026-04-02, Fri=03, Mon=06(analysis_date)
        _write_csv(tmp_path, date(2026, 4, 2), [{"ticker": "2330", "confidence": 50}])
        _write_csv(tmp_path, date(2026, 4, 3), [{"ticker": "2330", "confidence": 55}])

        csvs = _load_recent_csvs(date(2026, 4, 6), tmp_path, lookback=3)
        assert len(csvs) == 2
        assert csvs[0]["2330"] == 50
        assert csvs[1]["2330"] == 55

    def test_min_conf_filter(self, tmp_path):
        _write_csv(tmp_path, date(2026, 4, 8), [
            {"ticker": "2330", "confidence": 60},
            {"ticker": "2317", "confidence": 30},  # below min_conf=40
        ])
        csvs = _load_recent_csvs(date(2026, 4, 9), tmp_path, lookback=1, min_conf=40)
        assert "2330" in csvs[0]
        assert "2317" not in csvs[0]


class TestPersistenceBonus:
    def test_rising_trajectory_gets_7(self, tmp_path):
        for i, d in enumerate([date(2026, 4, 7), date(2026, 4, 8), date(2026, 4, 9)]):
            _write_csv(tmp_path, d, [{"ticker": "2330", "confidence": 50 + i * 5}])

        results = [_make_result("2330", 55)]
        n = _apply_persistence_bonus(results, date(2026, 4, 10), tmp_path)
        assert n == 1
        assert results[0]["confidence"] == 55 + 7
        assert any("PERSIST_RISING" in f for f in results[0]["flags"])

    def test_stable_gets_5(self, tmp_path):
        # Only yesterday available, score >= 50
        _write_csv(tmp_path, date(2026, 4, 9), [{"ticker": "2330", "confidence": 55}])

        results = [_make_result("2330", 60)]
        n = _apply_persistence_bonus(results, date(2026, 4, 10), tmp_path)
        assert n == 1
        assert results[0]["confidence"] == 60 + 5
        assert any("PERSIST_STABLE" in f for f in results[0]["flags"])

    def test_declining_gets_0(self, tmp_path):
        # Score dropped > 5 from previous
        _write_csv(tmp_path, date(2026, 4, 8), [{"ticker": "2330", "confidence": 70}])
        _write_csv(tmp_path, date(2026, 4, 9), [{"ticker": "2330", "confidence": 55}])

        results = [_make_result("2330", 50)]
        n = _apply_persistence_bonus(results, date(2026, 4, 10), tmp_path)
        assert n == 0
        assert results[0]["confidence"] == 50  # no bonus

    def test_below_min_conf_skipped(self, tmp_path):
        _write_csv(tmp_path, date(2026, 4, 9), [{"ticker": "2330", "confidence": 45}])

        results = [_make_result("2330", 60)]
        n = _apply_persistence_bonus(results, date(2026, 4, 10), tmp_path)
        assert n == 0  # yesterday score 45 < min_prev_conf 50

    def test_halted_stock_skipped(self, tmp_path):
        _write_csv(tmp_path, date(2026, 4, 9), [{"ticker": "2330", "confidence": 60}])

        results = [_make_result("2330", 55)]
        results[0]["halt"] = True
        n = _apply_persistence_bonus(results, date(2026, 4, 10), tmp_path)
        assert n == 0

    def test_no_csvs_returns_0(self, tmp_path):
        results = [_make_result("2330", 55)]
        n = _apply_persistence_bonus(results, date(2026, 4, 10), tmp_path)
        assert n == 0

    def test_not_rising_if_flat(self, tmp_path):
        # Same score 3 days = not rising (needs strictly increasing)
        for d in [date(2026, 4, 7), date(2026, 4, 8), date(2026, 4, 9)]:
            _write_csv(tmp_path, d, [{"ticker": "2330", "confidence": 55}])

        results = [_make_result("2330", 55)]
        n = _apply_persistence_bonus(results, date(2026, 4, 10), tmp_path)
        assert n == 1
        assert results[0]["confidence"] == 55 + 5  # STABLE, not RISING
        assert any("PERSIST_STABLE" in f for f in results[0]["flags"])

    def test_capped_at_100(self, tmp_path):
        for i, d in enumerate([date(2026, 4, 7), date(2026, 4, 8), date(2026, 4, 9)]):
            _write_csv(tmp_path, d, [{"ticker": "2330", "confidence": 90 + i}])

        results = [_make_result("2330", 97)]
        _apply_persistence_bonus(results, date(2026, 4, 10), tmp_path)
        assert results[0]["confidence"] == 100  # capped
