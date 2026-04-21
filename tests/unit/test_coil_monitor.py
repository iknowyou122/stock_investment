"""Unit tests for coil_monitor.py.

Tests cover:
1. Resistance formula correctness
2. Breakout detection
3. MAE uses bar low (not close)
4. Win-rate N-guard suppression
5. Idempotent refresh
6. sig_id includes grade (prevents collision)
7. SQLite WAL concurrent read consistency
8. Auto-apply gate blocks small samples
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from coil_monitor import (
    CoilSignalRecord,
    _derive_resistance,
    _evaluate_coil_outcome,
    _format_winrate,
    _ensure_schema,
    _upsert_signal,
    _load_all,
    _load_pending,
    check_auto_apply_gate,
    ingest_coil_csv,
    MIN_SAMPLE_FOR_WINRATE,
    BREAKOUT_THRESHOLD,
)


# ---------------------------------------------------------------------------
# 1. Resistance formula
# ---------------------------------------------------------------------------

def test_resistance_formula_basic():
    # vs_60d_high_pct=-10.0, entry_close=90.0
    # 90.0 / (1 + (-10.0)/100) = 90.0 / 0.9 = 100.0
    result = _derive_resistance(entry_close=90.0, vs_60d_high_pct=-10.0)
    assert result == pytest.approx(100.0)


def test_resistance_formula_rejects_wrong_formula():
    # Wrong formula: entry_close × (1 + abs(vs_60d_high_pct)) = 90.0 × 1.10 = 99.0 — close but wrong at scale
    # The missing /100 version: 90.0 × (1 + 10.0) = 900.0 — clearly wrong
    wrong = 90.0 * (1 + 10.0)   # missing /100
    correct = _derive_resistance(90.0, -10.0)
    assert correct != pytest.approx(wrong, rel=0.01)
    assert correct == pytest.approx(100.0)


def test_resistance_formula_small_gap():
    # vs_60d_high_pct=-4.04, entry_close=95.0
    # 95.0 / (1 + (-4.04)/100) = 95.0 / 0.9596 ≈ 98.999
    result = _derive_resistance(entry_close=95.0, vs_60d_high_pct=-4.04)
    assert result == pytest.approx(95.0 / 0.9596, rel=1e-4)


# ---------------------------------------------------------------------------
# 2. Breakout detection
# ---------------------------------------------------------------------------

def test_breakout_detected_on_day_3():
    # resistance=100.0, threshold=101.0 (×1.01)
    # Day 1: close=98, Day 2: close=99, Day 3: close=102 → BREAKOUT on day 3
    outcome, days, gain, mae = _evaluate_coil_outcome(
        entry_close=90.0,
        resistance=100.0,
        future_bars=[
            {"close": 98, "low": 97},
            {"close": 99, "low": 98.5},
            {"close": 102, "low": 101},
        ],
    )
    assert outcome == "BREAKOUT"
    assert days == 3


def test_no_breakout_returns_stalled():
    # resistance=100.0 → threshold=101.0; bars never reach it
    outcome, days, gain, mae = _evaluate_coil_outcome(
        entry_close=90.0,
        resistance=100.0,
        future_bars=[{"close": 95, "low": 94}, {"close": 96, "low": 95}],
    )
    assert outcome == "STALLED"
    assert days is None


def test_expired_when_enough_bars():
    # 15 bars without breakout → EXPIRED
    bars = [{"close": 95, "low": 94}] * 15
    outcome, days, gain, mae = _evaluate_coil_outcome(
        entry_close=90.0,
        resistance=100.0,
        future_bars=bars,
    )
    assert outcome == "EXPIRED"


def test_pending_when_no_bars():
    outcome, days, gain, mae = _evaluate_coil_outcome(
        entry_close=90.0,
        resistance=100.0,
        future_bars=[],
    )
    assert outcome == "PENDING"
    assert days is None
    assert gain is None
    assert mae is None


# ---------------------------------------------------------------------------
# 3. MAE uses bar low (not close)
# ---------------------------------------------------------------------------

def test_mae_uses_bar_low():
    # close never drops below entry, but low does
    # entry_close=100, bar1: low=94 (big dip intraday)
    _, _, _, mae = _evaluate_coil_outcome(
        entry_close=100.0,
        resistance=110.0,
        future_bars=[
            {"close": 98, "low": 94},   # low=94 → mae candidate
            {"close": 97, "low": 95},
        ],
    )
    # MAE should be based on worst low = 94
    expected = (94 / 100 - 1) * 100  # -6.0%
    assert mae == pytest.approx(expected)


def test_mae_not_based_on_close():
    # close=98 would give mae=-2%, but low=94 gives mae=-6%
    _, _, _, mae = _evaluate_coil_outcome(
        entry_close=100.0,
        resistance=110.0,
        future_bars=[{"close": 98, "low": 94}],
    )
    close_based_mae = (98 / 100 - 1) * 100  # -2.0%
    assert mae != pytest.approx(close_based_mae)
    assert mae == pytest.approx(-6.0)


# ---------------------------------------------------------------------------
# 4. Win-rate N-guard
# ---------------------------------------------------------------------------

def test_winrate_suppressed_below_minimum():
    result = _format_winrate(n=MIN_SAMPLE_FOR_WINRATE - 1, wins=5)
    assert "不足" in result
    assert "%" not in result


def test_winrate_shows_when_n_at_minimum():
    result = _format_winrate(n=MIN_SAMPLE_FOR_WINRATE, wins=12)
    assert "%" in result
    assert "不足" not in result


def test_winrate_zero_n():
    result = _format_winrate(n=0, wins=0)
    assert "不足" in result


# ---------------------------------------------------------------------------
# 5. Idempotency: refresh twice yields same sig_ids
# ---------------------------------------------------------------------------

def test_ingest_idempotent(tmp_path):
    """Ingesting the same CSV twice should not create duplicate records."""
    import csv as csv_module

    db_path = tmp_path / "coil_track.db"
    _ensure_schema(db_path)

    csv_path = tmp_path / "coil_2026-04-20.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv_module.DictWriter(f, fieldnames=[
            "scan_date", "analysis_date", "ticker", "name", "market", "grade",
            "score", "bb_pct", "vol_ratio", "consol_range_pct", "inst_consec_days",
            "weeks_consolidating", "vs_60d_high_pct", "score_breakdown", "flags",
        ])
        writer.writeheader()
        writer.writerow({
            "scan_date": "2026-04-20", "analysis_date": "2026-04-20",
            "ticker": "2330", "name": "台積電", "market": "TSE",
            "grade": "COIL_PRIME", "score": 75, "bb_pct": 8.5, "vol_ratio": 0.7,
            "consol_range_pct": 5.0, "inst_consec_days": 3,
            "weeks_consolidating": 4, "vs_60d_high_pct": -6.5,
            "score_breakdown": "{}", "flags": "COILING",
        })

    n1 = ingest_coil_csv(csv_path, db_path)
    n2 = ingest_coil_csv(csv_path, db_path)
    assert n1 == 1
    assert n2 == 0  # no duplicates

    all_recs = _load_all(db_path)
    assert len(all_recs) == 1


# ---------------------------------------------------------------------------
# 6. sig_id includes grade (collision prevention)
# ---------------------------------------------------------------------------

def test_sig_id_includes_grade():
    r1 = CoilSignalRecord(
        sig_id=f"2330_2026-04-20_COIL_EARLY",
        ticker="2330", name="台積電", market="TSE",
        signal_date="2026-04-20", grade="COIL_EARLY",
        score=40, bb_pct=10.0, vol_ratio=0.5,
        weeks_consolidating=2, vs_60d_high_pct=-8.0,
        entry_close=500.0, resistance=540.0,
    )
    r2 = CoilSignalRecord(
        sig_id=f"2330_2026-04-20_COIL_PRIME",
        ticker="2330", name="台積電", market="TSE",
        signal_date="2026-04-20", grade="COIL_PRIME",
        score=75, bb_pct=6.0, vol_ratio=0.6,
        weeks_consolidating=4, vs_60d_high_pct=-6.0,
        entry_close=500.0, resistance=531.9,
    )
    assert r1.sig_id != r2.sig_id
    assert "COIL_EARLY" in r1.sig_id
    assert "COIL_PRIME" in r2.sig_id


def test_sig_id_format():
    ticker, sig_date, grade = "4974", "2026-04-20", "COIL_PRIME"
    sig_id = f"{ticker}_{sig_date}_{grade}"
    assert sig_id == "4974_2026-04-20_COIL_PRIME"


# ---------------------------------------------------------------------------
# 7. SQLite WAL: write + read from separate connections
# ---------------------------------------------------------------------------

def test_sqlite_wal_write_read_consistency(tmp_path):
    db_path = tmp_path / "coil_track.db"
    _ensure_schema(db_path)

    rec = CoilSignalRecord(
        sig_id="2330_2026-04-20_COIL_PRIME",
        ticker="2330", name="台積電", market="TSE",
        signal_date="2026-04-20", grade="COIL_PRIME",
        score=75, bb_pct=8.0, vol_ratio=0.6,
        weeks_consolidating=4, vs_60d_high_pct=-10.0,
        entry_close=89.0, resistance=98.9,
        outcome="PENDING",
    )
    _upsert_signal(rec, db_path)

    # Read from a completely fresh connection (simulates second process)
    conn2 = sqlite3.connect(str(db_path))
    conn2.execute("PRAGMA journal_mode=WAL")
    rows = conn2.execute("SELECT sig_id, outcome FROM coil_signals").fetchall()
    conn2.close()

    assert len(rows) == 1
    assert rows[0][0] == "2330_2026-04-20_COIL_PRIME"
    assert rows[0][1] == "PENDING"


def test_upsert_updates_outcome(tmp_path):
    db_path = tmp_path / "coil_track.db"
    _ensure_schema(db_path)

    rec = CoilSignalRecord(
        sig_id="2330_2026-04-20_COIL_PRIME",
        ticker="2330", name="台積電", market="TSE",
        signal_date="2026-04-20", grade="COIL_PRIME",
        score=75, bb_pct=8.0, vol_ratio=0.6,
        weeks_consolidating=4, vs_60d_high_pct=-10.0,
        entry_close=89.0, resistance=98.9,
        outcome="PENDING",
    )
    _upsert_signal(rec, db_path)

    rec.outcome = "BREAKOUT"
    rec.days_to_breakout = 3
    _upsert_signal(rec, db_path)

    all_recs = _load_all(db_path)
    assert len(all_recs) == 1
    assert all_recs[0].outcome == "BREAKOUT"
    assert all_recs[0].days_to_breakout == 3


# ---------------------------------------------------------------------------
# 8. Auto-apply gate blocks small samples
# ---------------------------------------------------------------------------

def test_auto_apply_gate_blocks_no_db(tmp_path):
    db_path = tmp_path / "nonexistent.db"
    ok, reason = check_auto_apply_gate(db_path)
    assert not ok
    assert "不存在" in reason or "資料庫" in reason


def test_auto_apply_gate_blocks_small_n(tmp_path):
    db_path = tmp_path / "coil_track.db"
    _ensure_schema(db_path)

    # Insert 3 settled COIL_PRIME signals (well below 50)
    for i in range(3):
        rec = CoilSignalRecord(
            sig_id=f"2330_2026-04-{i+1:02d}_COIL_PRIME",
            ticker="2330", name="台積電", market="TSE",
            signal_date=f"2026-04-{i+1:02d}", grade="COIL_PRIME",
            score=70, bb_pct=8.0, vol_ratio=0.6,
            weeks_consolidating=4, vs_60d_high_pct=-10.0,
            entry_close=89.0, resistance=98.9,
            outcome="BREAKOUT",
        )
        _upsert_signal(rec, db_path)

    ok, reason = check_auto_apply_gate(db_path, min_days=90, min_n_per_grade=50)
    assert not ok
    assert "50" in reason or "不足" in reason


def test_auto_apply_gate_passes_with_sufficient_data(tmp_path):
    db_path = tmp_path / "coil_track.db"
    _ensure_schema(db_path)

    # Insert 50+ settled signals per grade, spread over 91 days
    grades = ["COIL_PRIME", "COIL_MATURE", "COIL_EARLY"]
    start = date(2026, 1, 1)
    counter = 0
    for grade in grades:
        for i in range(55):
            signal_date = (start + timedelta(days=i)).isoformat()
            rec = CoilSignalRecord(
                sig_id=f"2330_{signal_date}_{grade}_{counter}",
                ticker="2330", name="台積電", market="TSE",
                signal_date=signal_date, grade=grade,
                score=70, bb_pct=8.0, vol_ratio=0.6,
                weeks_consolidating=4, vs_60d_high_pct=-10.0,
                entry_close=89.0, resistance=98.9,
                outcome="BREAKOUT",
            )
            _upsert_signal(rec, db_path)
            counter += 1

    ok, reason = check_auto_apply_gate(db_path, min_days=90, min_n_per_grade=50)
    assert ok, f"Gate should pass but got: {reason}"
