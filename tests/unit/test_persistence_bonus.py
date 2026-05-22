"""Unit tests for trajectory-aware persistence bonus (DB-backed)."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from batch_plan import _apply_persistence_bonus, _apply_near_high_first_day


_DATA_DIR = Path("/tmp")  # ignored by DB-backed functions


def _make_result(ticker: str, confidence: int, **kwargs) -> dict:
    return {
        "ticker": ticker,
        "confidence": confidence,
        "halt": False,
        "error": None,
        "flags": [],
        **kwargs,
    }


# ──────────────────────────────────────────────────────────────────────────────
# TestPersistenceBonus — patches _load_recent_db
# ──────────────────────────────────────────────────────────────────────────────

class TestPersistenceBonus:
    def test_rising_trajectory_gets_7(self):
        recent = [
            {"2330": 50},
            {"2330": 55},
            {"2330": 60},
        ]
        with patch("batch_plan._load_recent_db", return_value=recent):
            results = [_make_result("2330", 55)]
            n = _apply_persistence_bonus(results, date(2026, 4, 10), _DATA_DIR)
        assert n == 1
        assert results[0]["confidence"] == 55 + 7
        assert any("PERSIST_RISING" in f for f in results[0]["flags"])

    def test_stable_gets_5(self):
        recent = [{"2330": 55}]
        with patch("batch_plan._load_recent_db", return_value=recent):
            results = [_make_result("2330", 60)]
            n = _apply_persistence_bonus(results, date(2026, 4, 10), _DATA_DIR)
        assert n == 1
        assert results[0]["confidence"] == 60 + 5
        assert any("PERSIST_STABLE" in f for f in results[0]["flags"])

    def test_declining_gets_0(self):
        recent = [{"2330": 70}, {"2330": 55}]
        with patch("batch_plan._load_recent_db", return_value=recent):
            results = [_make_result("2330", 50)]
            n = _apply_persistence_bonus(results, date(2026, 4, 10), _DATA_DIR)
        assert n == 0
        assert results[0]["confidence"] == 50

    def test_below_min_conf_skipped(self):
        # yesterday score 45 < min_prev_conf 50
        recent = [{"2330": 45}]
        with patch("batch_plan._load_recent_db", return_value=recent):
            results = [_make_result("2330", 60)]
            n = _apply_persistence_bonus(results, date(2026, 4, 10), _DATA_DIR)
        assert n == 0

    def test_halted_stock_skipped(self):
        recent = [{"2330": 60}]
        with patch("batch_plan._load_recent_db", return_value=recent):
            results = [_make_result("2330", 55)]
            results[0]["halt"] = True
            n = _apply_persistence_bonus(results, date(2026, 4, 10), _DATA_DIR)
        assert n == 0

    def test_no_recent_returns_0(self):
        with patch("batch_plan._load_recent_db", return_value=[]):
            results = [_make_result("2330", 55)]
            n = _apply_persistence_bonus(results, date(2026, 4, 10), _DATA_DIR)
        assert n == 0

    def test_not_rising_if_flat(self):
        recent = [{"2330": 55}, {"2330": 55}, {"2330": 55}]
        with patch("batch_plan._load_recent_db", return_value=recent):
            results = [_make_result("2330", 55)]
            n = _apply_persistence_bonus(results, date(2026, 4, 10), _DATA_DIR)
        assert n == 1
        assert results[0]["confidence"] == 55 + 5
        assert any("PERSIST_STABLE" in f for f in results[0]["flags"])

    def test_capped_at_100(self):
        recent = [{"2330": 90}, {"2330": 91}, {"2330": 92}]
        with patch("batch_plan._load_recent_db", return_value=recent):
            results = [_make_result("2330", 97)]
            _apply_persistence_bonus(results, date(2026, 4, 10), _DATA_DIR)
        assert results[0]["confidence"] == 100


# ──────────────────────────────────────────────────────────────────────────────
# TestNearHighFirstDay — patches _load_recent_db
# ──────────────────────────────────────────────────────────────────────────────

class TestNearHighFirstDay:
    def test_first_day_proximity12_gets_4(self):
        with patch("batch_plan._load_recent_db", return_value=[]):
            results = [_make_result("6173", 47, proximity_pts=12)]
            n = _apply_near_high_first_day(results, date(2026, 4, 13), _DATA_DIR)
        assert n == 1
        assert results[0]["confidence"] == 51
        assert "NEAR_HIGH_COIL" in results[0]["flags"]

    def test_repeat_ticker_no_bonus(self):
        # Stock appeared yesterday → no first-day bonus
        recent = [{"6173": 44}]
        with patch("batch_plan._load_recent_db", return_value=recent):
            results = [_make_result("6173", 47, proximity_pts=12)]
            n = _apply_near_high_first_day(results, date(2026, 4, 15), _DATA_DIR)
        assert n == 0
        assert results[0]["confidence"] == 47

    def test_low_proximity_no_bonus(self):
        with patch("batch_plan._load_recent_db", return_value=[]):
            results = [_make_result("2330", 50, proximity_pts=6)]
            n = _apply_near_high_first_day(results, date(2026, 4, 13), _DATA_DIR)
        assert n == 0
        assert results[0]["confidence"] == 50

    def test_halted_no_bonus(self):
        with patch("batch_plan._load_recent_db", return_value=[]):
            results = [_make_result("6173", 47, proximity_pts=12)]
            results[0]["halt"] = True
            n = _apply_near_high_first_day(results, date(2026, 4, 13), _DATA_DIR)
        assert n == 0

    def test_capped_at_100(self):
        with patch("batch_plan._load_recent_db", return_value=[]):
            results = [_make_result("6173", 98, proximity_pts=12)]
            _apply_near_high_first_day(results, date(2026, 4, 13), _DATA_DIR)
        assert results[0]["confidence"] == 100

    def test_no_proximity_key_no_bonus(self):
        with patch("batch_plan._load_recent_db", return_value=[]):
            results = [_make_result("6173", 47)]
            n = _apply_near_high_first_day(results, date(2026, 4, 13), _DATA_DIR)
        assert n == 0


# ──────────────────────────────────────────────────────────────────────────────
# Sector rank tiering tests (unchanged — no DB dependency)
# ──────────────────────────────────────────────────────────────────────────────
from batch_plan import _apply_sector_ranks


def _make_sector_results(tickers_confs: list[tuple[str, int]]) -> list[dict]:
    return [
        {"ticker": t, "confidence": c, "halt": False, "error": None, "flags": []}
        for t, c in tickers_confs
    ]


class TestSectorRanksTiered:
    def test_top_5pct_gets_10(self):
        results = _make_sector_results([(str(i), 50 - i) for i in range(20)])
        industry_map = {str(i): "半導體" for i in range(20)}
        _apply_sector_ranks(results, industry_map)
        top = next(r for r in results if r["ticker"] == "0")
        assert top["confidence"] == 60

    def test_top_10pct_gets_7(self):
        results = _make_sector_results([(str(i), 50 - i) for i in range(20)])
        industry_map = {str(i): "半導體" for i in range(20)}
        _apply_sector_ranks(results, industry_map)
        second = next(r for r in results if r["ticker"] == "1")
        assert second["confidence"] == 49 + 7

    def test_top_20pct_gets_5(self):
        results = _make_sector_results([(str(i), 50 - i) for i in range(20)])
        industry_map = {str(i): "半導體" for i in range(20)}
        _apply_sector_ranks(results, industry_map)
        fourth = next(r for r in results if r["ticker"] == "3")
        assert fourth["confidence"] == 47 + 5

    def test_rank_21pct_gets_no_bonus(self):
        results = _make_sector_results([(str(i), 50 - i) for i in range(20)])
        industry_map = {str(i): "半導體" for i in range(20)}
        _apply_sector_ranks(results, industry_map)
        fifth = next(r for r in results if r["ticker"] == "4")
        assert fifth["confidence"] == 46

    def test_sector_rank_flag_added(self):
        results = _make_sector_results([(str(i), 50 - i) for i in range(10)])
        industry_map = {str(i): "光電" for i in range(10)}
        _apply_sector_ranks(results, industry_map)
        top = next(r for r in results if r["ticker"] == "0")
        assert any("SECTOR_RANK:" in f for f in top["flags"])

    def test_returns_count_of_boosted(self):
        results = _make_sector_results([(str(i), 50 - i) for i in range(20)])
        industry_map = {str(i): "半導體" for i in range(20)}
        n = _apply_sector_ranks(results, industry_map)
        assert n == 4

    def test_fewer_than_3_stocks_no_bonus(self):
        results = _make_sector_results([("A", 70), ("B", 60)])
        industry_map = {"A": "小產業", "B": "小產業"}
        n = _apply_sector_ranks(results, industry_map)
        assert n == 0
        assert results[0]["confidence"] == 70
        assert results[1]["confidence"] == 60
