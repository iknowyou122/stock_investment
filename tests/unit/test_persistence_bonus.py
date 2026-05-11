"""Unit tests for trajectory-aware persistence bonus."""
from __future__ import annotations

import csv
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from batch_plan import _load_recent_csvs, _apply_persistence_bonus


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


# ──────────────────────────────────────────────────────────────────────────────
# Sector rank tiering tests (Fix 1)
# ──────────────────────────────────────────────────────────────────────────────
from batch_plan import _apply_sector_ranks


def _make_sector_results(tickers_confs: list[tuple[str, int]]) -> list[dict]:
    return [
        {"ticker": t, "confidence": c, "halt": False, "error": None, "flags": []}
        for t, c in tickers_confs
    ]


class TestSectorRanksTiered:
    def test_top_5pct_gets_10(self):
        """With 20 stocks, rank 1 is top 5% → +10."""
        results = _make_sector_results([(str(i), 50 - i) for i in range(20)])
        industry_map = {str(i): "半導體" for i in range(20)}
        _apply_sector_ranks(results, industry_map)
        top = next(r for r in results if r["ticker"] == "0")
        assert top["confidence"] == 60  # 50 + 10

    def test_top_10pct_gets_7(self):
        """With 20 stocks, rank 2 is top 10% (not top 5%) → +7."""
        results = _make_sector_results([(str(i), 50 - i) for i in range(20)])
        industry_map = {str(i): "半導體" for i in range(20)}
        _apply_sector_ranks(results, industry_map)
        second = next(r for r in results if r["ticker"] == "1")
        assert second["confidence"] == 49 + 7

    def test_top_20pct_gets_5(self):
        """With 20 stocks, rank 4 is top 20% but not top 10% → +5."""
        results = _make_sector_results([(str(i), 50 - i) for i in range(20)])
        industry_map = {str(i): "半導體" for i in range(20)}
        _apply_sector_ranks(results, industry_map)
        fourth = next(r for r in results if r["ticker"] == "3")
        assert fourth["confidence"] == 47 + 5

    def test_rank_21pct_gets_no_bonus(self):
        """With 20 stocks, rank 5 is just outside top 20% → no bonus."""
        results = _make_sector_results([(str(i), 50 - i) for i in range(20)])
        industry_map = {str(i): "半導體" for i in range(20)}
        _apply_sector_ranks(results, industry_map)
        fifth = next(r for r in results if r["ticker"] == "4")
        assert fifth["confidence"] == 46  # unchanged

    def test_sector_rank_flag_added(self):
        """SECTOR_RANK:N/M flag must appear on boosted stocks."""
        results = _make_sector_results([(str(i), 50 - i) for i in range(10)])
        industry_map = {str(i): "光電" for i in range(10)}
        _apply_sector_ranks(results, industry_map)
        top = next(r for r in results if r["ticker"] == "0")
        assert any("SECTOR_RANK:" in f for f in top["flags"])

    def test_returns_count_of_boosted(self):
        """Return value equals number of stocks that received a bonus."""
        results = _make_sector_results([(str(i), 50 - i) for i in range(20)])
        industry_map = {str(i): "半導體" for i in range(20)}
        n = _apply_sector_ranks(results, industry_map)
        assert n == 4  # top 20% of 20 = 4

    def test_fewer_than_3_stocks_no_bonus(self):
        """Sector with < 3 valid stocks gets no bonus (unchanged)."""
        results = _make_sector_results([("A", 70), ("B", 60)])
        industry_map = {"A": "小產業", "B": "小產業"}
        n = _apply_sector_ranks(results, industry_map)
        assert n == 0
        assert results[0]["confidence"] == 70
        assert results[1]["confidence"] == 60


# ──────────────────────────────────────────────────────────────────────────────
# Near-high first-day bonus tests (Fix 2)
# ──────────────────────────────────────────────────────────────────────────────
from batch_plan import _apply_near_high_first_day


class TestNearHighFirstDay:
    def test_first_day_proximity12_gets_4(self, tmp_path):
        """Stock appearing for the first time with proximity_pts=12 gets +4."""
        results = [
            {"ticker": "6173", "confidence": 47, "halt": False, "error": None,
             "flags": [], "proximity_pts": 12},
        ]
        n = _apply_near_high_first_day(results, date(2026, 4, 13), tmp_path)
        assert n == 1
        assert results[0]["confidence"] == 51  # 47 + 4
        assert "NEAR_HIGH_COIL" in results[0]["flags"]

    def test_repeat_ticker_no_bonus(self, tmp_path):
        """Stock that appeared yesterday does NOT get the first-day bonus."""
        # Use Wed 2026-04-15 as analysis_date; yesterday is Tue 2026-04-14 (weekday).
        _write_csv(tmp_path, date(2026, 4, 14), [{"ticker": "6173", "confidence": 44}])
        results = [
            {"ticker": "6173", "confidence": 47, "halt": False, "error": None,
             "flags": [], "proximity_pts": 12},
        ]
        n = _apply_near_high_first_day(results, date(2026, 4, 15), tmp_path)
        assert n == 0
        assert results[0]["confidence"] == 47  # unchanged

    def test_low_proximity_no_bonus(self, tmp_path):
        """proximity_pts < 12 (not in 92-99% zone) → no bonus."""
        results = [
            {"ticker": "2330", "confidence": 50, "halt": False, "error": None,
             "flags": [], "proximity_pts": 6},
        ]
        n = _apply_near_high_first_day(results, date(2026, 4, 13), tmp_path)
        assert n == 0
        assert results[0]["confidence"] == 50

    def test_halted_no_bonus(self, tmp_path):
        """Halted stocks are skipped."""
        results = [
            {"ticker": "6173", "confidence": 47, "halt": True, "error": None,
             "flags": [], "proximity_pts": 12},
        ]
        n = _apply_near_high_first_day(results, date(2026, 4, 13), tmp_path)
        assert n == 0

    def test_capped_at_100(self, tmp_path):
        """Confidence cannot exceed 100."""
        results = [
            {"ticker": "6173", "confidence": 98, "halt": False, "error": None,
             "flags": [], "proximity_pts": 12},
        ]
        _apply_near_high_first_day(results, date(2026, 4, 13), tmp_path)
        assert results[0]["confidence"] == 100

    def test_no_proximity_key_no_bonus(self, tmp_path):
        """Result dict missing proximity_pts key → no bonus (graceful fallback)."""
        results = [
            {"ticker": "6173", "confidence": 47, "halt": False, "error": None, "flags": []},
        ]
        n = _apply_near_high_first_day(results, date(2026, 4, 13), tmp_path)
        assert n == 0
