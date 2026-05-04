"""Unit tests for surge_db helpers."""
import json
import sys
import os
from datetime import date, timedelta
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../scripts"))


def _make_db(tmp_path):
    """Return an initialised surge_db pointing at a temp SQLite file."""
    import surge_db
    db_path = tmp_path / "test_surge.db"
    surge_db.init_db(str(db_path))
    return surge_db, str(db_path)


def _signal(ticker="2330", signal_date=None, score=75, grade="SURGE_ALPHA", close_price=250.0):
    return {
        "ticker": ticker,
        "signal_date": str(signal_date or date(2026, 5, 1)),
        "grade": grade,
        "score": score,
        "vol_ratio": 3.5,
        "day_chg_pct": 6.2,
        "gap_pct": 4.1,
        "close_strength": 0.95,
        "rsi": 67.0,
        "inst_consec_days": 2,
        "industry_rank_pct": 88.0,
        "close_price": close_price,
        "market": "TSE",
        "industry": "半導體業",
        "score_breakdown": json.dumps({"vol_ratio_ideal": 10, "pocket_pivot": 12}),
    }


class TestInitDb:
    def test_creates_table(self, tmp_path):
        sdb, path = _make_db(tmp_path)
        import sqlite3
        con = sqlite3.connect(path)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "surge_signals" in tables
        con.close()


class TestInsertSignals:
    def test_inserts_one(self, tmp_path):
        sdb, path = _make_db(tmp_path)
        sdb.insert_signals([_signal()], db_path=path)
        import sqlite3
        con = sqlite3.connect(path)
        count = con.execute("SELECT COUNT(*) FROM surge_signals").fetchone()[0]
        assert count == 1
        con.close()

    def test_upsert_does_not_duplicate(self, tmp_path):
        sdb, path = _make_db(tmp_path)
        sdb.insert_signals([_signal()], db_path=path)
        sdb.insert_signals([_signal()], db_path=path)
        import sqlite3
        con = sqlite3.connect(path)
        count = con.execute("SELECT COUNT(*) FROM surge_signals").fetchone()[0]
        assert count == 1
        con.close()

    def test_inserts_multiple(self, tmp_path):
        sdb, path = _make_db(tmp_path)
        sdb.insert_signals([_signal("2330"), _signal("2454")], db_path=path)
        import sqlite3
        con = sqlite3.connect(path)
        count = con.execute("SELECT COUNT(*) FROM surge_signals").fetchone()[0]
        assert count == 2
        con.close()


class TestSettlePending:
    def test_settle_writes_t1_return(self, tmp_path):
        sdb, path = _make_db(tmp_path)
        # Insert signal 3 days ago so T+1 is in the past
        sig = _signal(signal_date=date.today() - timedelta(days=3), close_price=100.0)
        sdb.insert_signals([sig], db_path=path)

        # Patch yfinance to return a known price
        import unittest.mock as mock
        import pandas as pd
        fake_hist = pd.DataFrame(
            {"Close": [105.0]},
            index=pd.DatetimeIndex([date.today() - timedelta(days=2)])
        )
        with mock.patch("surge_db._fetch_close", return_value=105.0):
            sdb.settle_pending(db_path=path)

        import sqlite3
        con = sqlite3.connect(path)
        row = con.execute("SELECT t1_return_pct FROM surge_signals").fetchone()
        assert row is not None
        assert abs(row[0] - 5.0) < 0.01   # (105/100 - 1) * 100
        con.close()

    def test_unsettled_skipped_if_too_recent(self, tmp_path):
        sdb, path = _make_db(tmp_path)
        sig = _signal(signal_date=date.today(), close_price=100.0)
        sdb.insert_signals([sig], db_path=path)
        sdb.settle_pending(db_path=path)
        import sqlite3
        con = sqlite3.connect(path)
        row = con.execute("SELECT t1_return_pct FROM surge_signals").fetchone()
        assert row[0] is None   # too recent, not settled
        con.close()


class TestQueryForAnalysis:
    def test_returns_settled_signals(self, tmp_path):
        sdb, path = _make_db(tmp_path)
        sig = _signal(close_price=100.0)
        sdb.insert_signals([sig], db_path=path)
        # Manually write a settled t1_return
        import sqlite3
        con = sqlite3.connect(path)
        con.execute("UPDATE surge_signals SET t1_return_pct=3.5 WHERE ticker='2330'")
        con.commit(); con.close()
        rows = sdb.query_settled(db_path=path, min_settled=1)
        assert len(rows) == 1
        assert rows[0]["ticker"] == "2330"
        assert abs(rows[0]["t1_return_pct"] - 3.5) < 0.01
