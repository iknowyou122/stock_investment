# Surge Signal Tracking & LLM-Assisted Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a feedback loop that records daily surge scan signals to SQLite, settles T+1/T+3/T+5 price outcomes automatically, runs lift analysis per factor, and calls an LLM to recommend weight changes — all reviewable via `make surge-tune`.

**Architecture:** SQLite DB (`data/surge_signals.db`) stores every signal with full score_breakdown; `report.py` gains a surge settlement step that fetches close prices via yfinance; `surge_factor_report.py` computes per-factor lift tables and calls `create_llm_provider().complete()` with a structured prompt to propose `config/surge_params.json` edits; `make surge-tune` shows the diff and lets the user accept/reject. `SurgeRadar` already reads all weights from `config/surge_params.json` so no scoring code changes are needed.

**Tech Stack:** Python stdlib `sqlite3`, `yfinance`, existing `create_llm_provider` from `src/taiwan_stock_agent/domain/llm_provider.py`, `rich` for display, existing `config/surge_params.json` schema.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `data/surge_signals.db` | Created at runtime | SQLite DB for signal + outcome storage |
| `scripts/surge_db.py` | **Create** | All SQLite read/write helpers (init schema, insert signal, settle outcomes, query for analysis) |
| `scripts/surge_scan.py` | **Modify** (end of `run_surge_scan`) | Call `surge_db.insert_signals()` after scan completes |
| `scripts/report.py` | **Modify** (add step after T+1 settle) | Call `surge_db.settle_pending()` to fill T+1/T+3/T+5 returns |
| `scripts/surge_factor_report.py` | **Create** | Load settled signals, compute lift, call LLM, propose weight changes |
| `Makefile` | **Modify** | Add `surge-factor` and `surge-tune` targets |
| `tests/unit/test_surge_db.py` | **Create** | Unit tests for all surge_db helpers |

---

## Task 1: Create `scripts/surge_db.py` — SQLite helpers

**Files:**
- Create: `scripts/surge_db.py`
- Create: `tests/unit/test_surge_db.py`

- [ ] **Step 1: Write failing tests**

  Create `tests/unit/test_surge_db.py`:
  ```python
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


  def _signal(ticker="2330", signal_date=None, score=75, grade="SURGE_ALPHA"):
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
          "close_price": 250.0,
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
  ```

- [ ] **Step 2: Run tests to verify they fail**
  ```bash
  .venv/bin/pytest tests/unit/test_surge_db.py -v 2>&1 | head -20
  ```
  Expected: `ModuleNotFoundError: No module named 'surge_db'`

- [ ] **Step 3: Create `scripts/surge_db.py`**

  ```python
  """SQLite persistence for surge signal tracking and outcome settlement.

  DB path: data/surge_signals.db (created automatically on first use).
  All public functions accept an optional `db_path` kwarg for testing.
  """
  from __future__ import annotations

  import json
  import sqlite3
  from datetime import date, timedelta
  from pathlib import Path

  _DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "surge_signals.db"

  _SCHEMA = """
  CREATE TABLE IF NOT EXISTS surge_signals (
      id                 INTEGER PRIMARY KEY AUTOINCREMENT,
      signal_date        TEXT    NOT NULL,
      ticker             TEXT    NOT NULL,
      grade              TEXT    NOT NULL,
      score              INTEGER NOT NULL,
      vol_ratio          REAL,
      day_chg_pct        REAL,
      gap_pct            REAL,
      close_strength     REAL,
      rsi                REAL,
      inst_consec_days   INTEGER,
      industry_rank_pct  REAL,
      close_price        REAL,
      market             TEXT,
      industry           TEXT,
      score_breakdown    TEXT,
      t1_return_pct      REAL,
      t3_return_pct      REAL,
      t5_return_pct      REAL,
      settled_at         TEXT,
      UNIQUE(signal_date, ticker)
  );
  """


  def init_db(db_path: str | None = None) -> None:
      path = db_path or str(_DEFAULT_DB)
      Path(path).parent.mkdir(parents=True, exist_ok=True)
      with sqlite3.connect(path) as con:
          con.executescript(_SCHEMA)


  def insert_signals(signals: list[dict], db_path: str | None = None) -> int:
      """Insert or ignore (upsert) a list of surge signal dicts. Returns inserted count."""
      if not signals:
          return 0
      path = db_path or str(_DEFAULT_DB)
      init_db(path)
      rows = [
          (
              s["signal_date"], s["ticker"], s["grade"], s["score"],
              s.get("vol_ratio"), s.get("day_chg_pct"), s.get("gap_pct"),
              s.get("close_strength"), s.get("rsi"), s.get("inst_consec_days"),
              s.get("industry_rank_pct"), s.get("close_price"),
              s.get("market"), s.get("industry"),
              s.get("score_breakdown") if isinstance(s.get("score_breakdown"), str)
              else json.dumps(s.get("score_breakdown") or {}),
          )
          for s in signals
      ]
      sql = """
          INSERT OR IGNORE INTO surge_signals
          (signal_date, ticker, grade, score, vol_ratio, day_chg_pct, gap_pct,
           close_strength, rsi, inst_consec_days, industry_rank_pct, close_price,
           market, industry, score_breakdown)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      """
      with sqlite3.connect(path) as con:
          cur = con.executemany(sql, rows)
          return cur.rowcount


  def _fetch_close(ticker: str, target_date: date, market: str) -> float | None:
      """Fetch closing price for ticker on target_date via yfinance."""
      import yfinance as yf
      import pandas as pd
      suffix = ".TW" if market == "TSE" else ".TWO"
      start = target_date - timedelta(days=1)
      end = target_date + timedelta(days=2)
      try:
          hist = yf.download(
              f"{ticker}{suffix}", start=str(start), end=str(end),
              interval="1d", progress=False, auto_adjust=True,
              multi_level_index=False,
          )
          if hist.empty:
              return None
          # Find the row whose index date <= target_date, take the last one
          hist.index = pd.to_datetime(hist.index).date
          rows = hist[hist.index <= target_date]
          if rows.empty:
              return None
          val = float(rows["Close"].iloc[-1])
          return round(val, 2) if not pd.isna(val) else None
      except Exception:
          return None


  def settle_pending(db_path: str | None = None) -> int:
      """Settle T+1/T+3/T+5 returns for signals old enough to have outcomes.

      A signal is 'old enough' when today >= signal_date + N trading days.
      Uses a simple calendar-day approximation: T+1=1d, T+3=3d, T+5=5d.
      Returns number of rows updated.
      """
      path = db_path or str(_DEFAULT_DB)
      init_db(path)
      today = date.today()
      updated = 0

      with sqlite3.connect(path) as con:
          con.row_factory = sqlite3.Row
          pending = con.execute("""
              SELECT id, ticker, signal_date, close_price, market
              FROM surge_signals
              WHERE close_price IS NOT NULL
                AND (t1_return_pct IS NULL OR t3_return_pct IS NULL OR t5_return_pct IS NULL)
          """).fetchall()

      for row in pending:
          sig_date = date.fromisoformat(row["signal_date"])
          market = row["market"] or "TSE"
          close = row["close_price"]
          updates: dict[str, float | None] = {}

          for n, col in [(1, "t1_return_pct"), (3, "t3_return_pct"), (5, "t5_return_pct")]:
              target = sig_date + timedelta(days=n)
              # Skip weekends
              while target.weekday() >= 5:
                  target += timedelta(days=1)
              if today < target:
                  continue  # Not yet available
              price = _fetch_close(row["ticker"], target, market)
              if price and close:
                  updates[col] = round((price / close - 1) * 100, 3)

          if updates:
              set_clause = ", ".join(f"{k}=?" for k in updates)
              vals = list(updates.values()) + [str(today), row["id"]]
              with sqlite3.connect(path) as con:
                  con.execute(
                      f"UPDATE surge_signals SET {set_clause}, settled_at=? WHERE id=?",
                      vals,
                  )
              updated += 1

      return updated


  def query_settled(
      db_path: str | None = None,
      min_settled: int = 30,
      lookback_days: int = 90,
  ) -> list[dict]:
      """Return settled signals for factor analysis.

      Only returns signals with t1_return_pct populated.
      Raises ValueError if fewer than min_settled rows found.
      """
      path = db_path or str(_DEFAULT_DB)
      init_db(path)
      cutoff = str(date.today() - timedelta(days=lookback_days))
      with sqlite3.connect(path) as con:
          con.row_factory = sqlite3.Row
          rows = con.execute("""
              SELECT * FROM surge_signals
              WHERE t1_return_pct IS NOT NULL
                AND signal_date >= ?
              ORDER BY signal_date DESC
          """, (cutoff,)).fetchall()
      result = [dict(r) for r in rows]
      if len(result) < min_settled:
          raise ValueError(
              f"只有 {len(result)} 筆已結算信號（需要 ≥{min_settled} 筆才能分析）"
          )
      return result
  ```

- [ ] **Step 4: Run tests**
  ```bash
  .venv/bin/pytest tests/unit/test_surge_db.py -v
  ```
  Expected: all 8 tests PASS

- [ ] **Step 5: Commit**
  ```bash
  git add scripts/surge_db.py tests/unit/test_surge_db.py
  git commit -m "feat: add surge_db SQLite helpers for signal tracking and settlement"
  ```

---

## Task 2: Write surge signals to DB after each scan

**Files:**
- Modify: `scripts/surge_scan.py` — `run_surge_scan` function (around line 684–795)

- [ ] **Step 1: Find the DB write location**

  In `scripts/surge_scan.py`, find the block after `_print_surge_table(results, scan_date, name_map)` (around line 684). This is where we add DB writes.

- [ ] **Step 2: Add import at top of surge_scan.py**

  In `scripts/surge_scan.py`, find the imports section. Add after the existing script imports:
  ```python
  import sys as _sys
  _sys.path.insert(0, str(Path(__file__).parent))
  try:
      import surge_db as _surge_db
      _HAS_SURGE_DB = True
  except ImportError:
      _HAS_SURGE_DB = False
  ```

  Actually, since `scripts/` is already on the path via the `_sys.path.insert` already present for `trade.py`, just add at the top-level imports:
  ```python
  try:
      from surge_db import insert_signals as _surge_db_insert
      _HAS_SURGE_DB = True
  except ImportError:
      _HAS_SURGE_DB = False
  ```

- [ ] **Step 3: Add DB insert after `_print_surge_table`**

  Find in `run_surge_scan`:
  ```python
      _print_surge_table(results, scan_date, name_map)
  ```

  After that line, add:
  ```python
      # Persist signals to SQLite for outcome tracking
      if _HAS_SURGE_DB and results:
          _db_rows = []
          for r in results:
              ticker = r.get("ticker", "")
              _db_rows.append({
                  "signal_date": scan_date,
                  "ticker": ticker,
                  "grade": r.get("grade", ""),
                  "score": r.get("score", 0),
                  "vol_ratio": r.get("vol_ratio"),
                  "day_chg_pct": r.get("day_chg_pct"),
                  "gap_pct": r.get("gap_pct"),
                  "close_strength": r.get("close_strength"),
                  "rsi": r.get("rsi"),
                  "inst_consec_days": r.get("inst_consec_days", 0),
                  "industry_rank_pct": r.get("industry_rank_pct"),
                  "close_price": r.get("close_price"),
                  "market": r.get("market", "TSE"),
                  "industry": (industry_map or {}).get(ticker, ""),
                  "score_breakdown": json.dumps(r.get("score_breakdown") or {}),
              })
          inserted = _surge_db_insert(_db_rows)
          _console.print(f"  [dim]📋 surge_signals DB: {inserted} 筆新增[/dim]")
  ```

  Note: `close_price` needs to come from the result dict. Check that `score_full` in `SurgeRadar` includes `close_price`. Looking at the return dict in `src/taiwan_stock_agent/domain/surge_radar.py:414`, it does NOT include `close_price`. We need to add it.

- [ ] **Step 4: Add `close_price` to SurgeRadar.score_full return**

  In `src/taiwan_stock_agent/domain/surge_radar.py`, find the `return {` block at line 414:
  ```python
          return {
              "grade": grade,
              "score": score,
              ...
              "inst_consec_days": max(...) if proxy and proxy.is_available else 0,
          }
  ```

  Add `"close_price": float(ohlcv.close),` before the closing `}`:
  ```python
          return {
              "grade": grade,
              "score": score,
              "raw_pts": raw,
              "flags": all_flags,
              "score_breakdown": breakdown,
              "vol_ratio": vol_ratio,
              "close_strength": close_pos,
              "day_chg_pct": day_chg_pct,
              "gap_pct": gap_pct,
              "surge_day": consec,
              "industry_rank_pct": industry_rank_pct,
              "rsi": self._rsi(history),
              "close_price": float(ohlcv.close),
              "inst_consec_days": max(
                  proxy.foreign_consecutive_buy_days, proxy.trust_consecutive_buy_days
              ) if proxy and proxy.is_available else 0,
          }
  ```

- [ ] **Step 5: Verify syntax**
  ```bash
  .venv/bin/python -c "import ast; ast.parse(open('scripts/surge_scan.py').read()); print('surge_scan OK')"
  .venv/bin/python -c "import ast; ast.parse(open('src/taiwan_stock_agent/domain/surge_radar.py').read()); print('surge_radar OK')"
  ```
  Expected: both print `OK`

- [ ] **Step 6: Smoke test — verify DB write from CSV**
  ```bash
  .venv/bin/python - <<'EOF'
  import sys, os, json
  sys.path.insert(0, "scripts")
  import surge_db
  surge_db.insert_signals([{
      "signal_date": "2026-05-04", "ticker": "2330", "grade": "SURGE_ALPHA",
      "score": 75, "vol_ratio": 3.5, "day_chg_pct": 6.2, "gap_pct": 4.1,
      "close_strength": 0.95, "rsi": 67.0, "inst_consec_days": 2,
      "industry_rank_pct": 88.0, "close_price": 250.0,
      "market": "TSE", "industry": "半導體業",
      "score_breakdown": json.dumps({"vol_ratio_ideal": 10}),
  }])
  import sqlite3
  con = sqlite3.connect("data/surge_signals.db")
  print(con.execute("SELECT signal_date, ticker, grade, score FROM surge_signals").fetchall())
  EOF
  ```
  Expected: `[('2026-05-04', '2330', 'SURGE_ALPHA', 75)]`

- [ ] **Step 7: Commit**
  ```bash
  git add scripts/surge_scan.py src/taiwan_stock_agent/domain/surge_radar.py
  git commit -m "feat: persist surge signals to SQLite after each scan"
  ```

---

## Task 3: Auto-settle T+1/T+3/T+5 in `make report`

**Files:**
- Modify: `scripts/report.py` — `main()` function (find the `[Step 1]` block)

- [ ] **Step 1: Add surge settlement import and call**

  In `scripts/report.py`, find the `main()` function. Find the block:
  ```python
      _console.print("\n[Step 1] T+1 結算昨日 LONG 信號...")
      settled = run_t1_settle(review_date)
  ```

  After the existing `[Step 1]` block completes (find the next `[Step 2]` line), add:
  ```python
      # Surge signal settlement (T+1/T+3/T+5)
      _console.print("\n[Step 1b] Surge 信號結算（T+1/T+3/T+5）...")
      try:
          import sys as _sys
          _sys.path.insert(0, str(Path(__file__).parent))
          from surge_db import settle_pending as _surge_settle
          n_settled = _surge_settle()
          _console.print(f"  結算完成: {n_settled} 筆 surge 信號")
      except Exception as _e:
          _console.print(f"  [dim]surge 結算略過: {_e}[/dim]")
  ```

- [ ] **Step 2: Verify syntax**
  ```bash
  .venv/bin/python -c "import ast; ast.parse(open('scripts/report.py').read()); print('OK')"
  ```
  Expected: `OK`

- [ ] **Step 3: Commit**
  ```bash
  git add scripts/report.py
  git commit -m "feat: auto-settle surge signals T+1/T+3/T+5 in make report"
  ```

---

## Task 4: Create `scripts/surge_factor_report.py` — Lift Analysis + LLM Optimization

**Files:**
- Create: `scripts/surge_factor_report.py`
- Create: `tests/unit/test_surge_factor_report.py`

- [ ] **Step 1: Write failing tests**

  Create `tests/unit/test_surge_factor_report.py`:
  ```python
  """Tests for surge factor lift analysis."""
  import json
  import sys, os
  sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../scripts"))


  def _make_signals(n=40, win_pct=0.6):
      """Generate n synthetic settled signal dicts."""
      import random
      random.seed(42)
      signals = []
      for i in range(n):
          is_win = random.random() < win_pct
          signals.append({
              "ticker": f"{2000+i}",
              "signal_date": "2026-04-01",
              "grade": "SURGE_ALPHA" if i % 3 == 0 else "SURGE_BETA",
              "score": 55 + (i % 30),
              "vol_ratio": 1.5 + (i % 5),
              "gap_pct": (i % 10) * 0.5,
              "rsi": 50 + (i % 25),
              "inst_consec_days": i % 4,
              "industry_rank_pct": (i % 5) * 20.0,
              "score_breakdown": json.dumps({"pocket_pivot": 12 if i % 2 == 0 else 0}),
              "t1_return_pct": 2.5 if is_win else -1.5,
          })
      return signals


  class TestComputeLift:
      def test_returns_dict_per_factor(self):
          from surge_factor_report import compute_lift
          signals = _make_signals(40)
          lift = compute_lift(signals)
          assert "vol_ratio" in lift
          assert "gap_pct" in lift
          assert "pocket_pivot" in lift

      def test_lift_has_required_keys(self):
          from surge_factor_report import compute_lift
          signals = _make_signals(40)
          lift = compute_lift(signals)
          factor = lift["vol_ratio"]
          assert "present_wr" in factor
          assert "absent_wr" in factor
          assert "lift_pp" in factor
          assert "n_present" in factor
          assert "n_absent" in factor

      def test_win_rate_between_0_and_1(self):
          from surge_factor_report import compute_lift
          signals = _make_signals(40)
          lift = compute_lift(signals)
          for f, vals in lift.items():
              assert 0.0 <= vals["present_wr"] <= 1.0, f"{f} present_wr out of range"
              assert 0.0 <= vals["absent_wr"] <= 1.0, f"{f} absent_wr out of range"

      def test_pocket_pivot_from_breakdown(self):
          from surge_factor_report import compute_lift
          signals = _make_signals(40)
          lift = compute_lift(signals)
          # pocket_pivot comes from score_breakdown JSON — should be present
          assert "pocket_pivot" in lift


  class TestBuildGradeSummary:
      def test_grade_summary_keys(self):
          from surge_factor_report import build_grade_summary
          signals = _make_signals(40)
          summary = build_grade_summary(signals)
          assert "SURGE_ALPHA" in summary
          alpha = summary["SURGE_ALPHA"]
          assert "n" in alpha
          assert "t1_wr" in alpha
          assert "t1_avg_ret" in alpha

      def test_win_rate_is_fraction(self):
          from surge_factor_report import build_grade_summary
          signals = _make_signals(40)
          summary = build_grade_summary(signals)
          for grade, vals in summary.items():
              assert 0.0 <= vals["t1_wr"] <= 1.0
  ```

- [ ] **Step 2: Run tests to verify they fail**
  ```bash
  .venv/bin/pytest tests/unit/test_surge_factor_report.py -v 2>&1 | head -10
  ```
  Expected: `ModuleNotFoundError: No module named 'surge_factor_report'`

- [ ] **Step 3: Create `scripts/surge_factor_report.py`**

  ```python
  """Surge factor lift analysis and LLM-assisted weight optimization.

  Usage:
      python scripts/surge_factor_report.py          # show lift table
      python scripts/surge_factor_report.py --llm    # add LLM recommendations
      make surge-factor                              # show lift table
      make surge-tune                               # LLM recommendations + interactive apply
  """
  from __future__ import annotations

  import argparse
  import json
  import os
  import sys
  from datetime import date
  from pathlib import Path

  sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
  sys.path.insert(0, str(Path(__file__).resolve().parent))

  from rich.console import Console
  from rich.table import Table
  from rich import box

  _console = Console()
  _PARAMS_PATH = Path(__file__).resolve().parents[1] / "config" / "surge_params.json"

  # ── Factor definitions ────────────────────────────────────────────────────────
  # Each entry: (factor_name, threshold_fn)
  # threshold_fn(signal_dict) -> bool: True = factor "present" (should help)
  _FACTOR_THRESHOLDS: dict[str, callable] = {
      "vol_ratio_3x":       lambda s: (s.get("vol_ratio") or 0) >= 3.0,
      "vol_ratio_2x":       lambda s: (s.get("vol_ratio") or 0) >= 2.0,
      "gap_3pct":           lambda s: (s.get("gap_pct") or 0) >= 3.0,
      "gap_1pct":           lambda s: (s.get("gap_pct") or 0) >= 1.0,
      "rsi_healthy":        lambda s: 55.0 <= (s.get("rsi") or 0) <= 72.0,
      "inst_buy_1d":        lambda s: (s.get("inst_consec_days") or 0) >= 1,
      "inst_buy_2d":        lambda s: (s.get("inst_consec_days") or 0) >= 2,
      "ind_top_20pct":      lambda s: (s.get("industry_rank_pct") or 0) >= 80.0,
      "ind_top_40pct":      lambda s: (s.get("industry_rank_pct") or 0) >= 60.0,
      "close_strong":       lambda s: (s.get("close_strength") or 0) >= 0.8,
      "score_65plus":       lambda s: (s.get("score") or 0) >= 65,
      "score_55plus":       lambda s: (s.get("score") or 0) >= 55,
      "pocket_pivot":       lambda s: _has_breakdown_flag(s, "pocket_pivot"),
      "breakaway_gap_full": lambda s: _has_breakdown_flag(s, "breakaway_gap_full"),
      "relative_strength":  lambda s: _has_breakdown_flag(s, "relative_strength"),
  }


  def _has_breakdown_flag(signal: dict, key: str) -> bool:
      try:
          bd = signal.get("score_breakdown")
          if isinstance(bd, str):
              bd = json.loads(bd)
          return bool((bd or {}).get(key, 0))
      except Exception:
          return False


  def compute_lift(signals: list[dict]) -> dict[str, dict]:
      """Compute T+1 win-rate lift for each factor threshold.

      A signal is a 'win' when t1_return_pct > 0.
      Returns dict of {factor_name: {present_wr, absent_wr, lift_pp, n_present, n_absent, avg_ret_present, avg_ret_absent}}.
      """
      result = {}
      settled = [s for s in signals if s.get("t1_return_pct") is not None]
      if not settled:
          return result

      for name, fn in _FACTOR_THRESHOLDS.items():
          present = [s for s in settled if fn(s)]
          absent  = [s for s in settled if not fn(s)]

          def _wr(lst):
              if not lst:
                  return 0.0
              return sum(1 for s in lst if (s.get("t1_return_pct") or 0) > 0) / len(lst)

          def _avg(lst):
              if not lst:
                  return 0.0
              return sum(s.get("t1_return_pct") or 0 for s in lst) / len(lst)

          result[name] = {
              "present_wr":      round(_wr(present), 4),
              "absent_wr":       round(_wr(absent), 4),
              "lift_pp":         round((_wr(present) - _wr(absent)) * 100, 1),
              "n_present":       len(present),
              "n_absent":        len(absent),
              "avg_ret_present": round(_avg(present), 2),
              "avg_ret_absent":  round(_avg(absent), 2),
          }
      return result


  def build_grade_summary(signals: list[dict]) -> dict[str, dict]:
      """Win rate and avg return per grade for settled T+1 signals."""
      settled = [s for s in signals if s.get("t1_return_pct") is not None]
      grades = ["SURGE_ALPHA", "SURGE_BETA", "SURGE_GAMMA"]
      result = {}
      for g in grades:
          group = [s for s in settled if s.get("grade") == g]
          if not group:
              continue
          wr = sum(1 for s in group if (s.get("t1_return_pct") or 0) > 0) / len(group)
          avg_ret = sum(s.get("t1_return_pct") or 0 for s in group) / len(group)
          result[g] = {"n": len(group), "t1_wr": round(wr, 4), "t1_avg_ret": round(avg_ret, 2)}
      return result


  def print_lift_table(lift: dict[str, dict]) -> None:
      tbl = Table(title="因子 Lift 分析 (T+1)", box=box.ROUNDED, show_lines=True)
      tbl.add_column("因子", style="cyan")
      tbl.add_column("有 N", justify="right")
      tbl.add_column("有 勝率", justify="right")
      tbl.add_column("無 N", justify="right")
      tbl.add_column("無 勝率", justify="right")
      tbl.add_column("Lift(pp)", justify="right")
      tbl.add_column("有 均報酬%", justify="right")
      for name, v in sorted(lift.items(), key=lambda x: -x[1]["lift_pp"]):
          lift_pp = v["lift_pp"]
          style = "green" if lift_pp >= 10 else ("yellow" if lift_pp >= 5 else "")
          tbl.add_row(
              name,
              str(v["n_present"]),
              f"{v['present_wr']*100:.1f}%",
              str(v["n_absent"]),
              f"{v['absent_wr']*100:.1f}%",
              f"[{style}]{lift_pp:+.1f}[/{style}]" if style else f"{lift_pp:+.1f}",
              f"{v['avg_ret_present']:+.2f}%",
          )
      _console.print(tbl)


  def build_llm_prompt(
      signals: list[dict],
      lift: dict[str, dict],
      grade_summary: dict[str, dict],
      current_params: dict,
  ) -> str:
      n = len(signals)
      factors_json = json.dumps(current_params.get("factors", {}), ensure_ascii=False, indent=2)
      gates_json = json.dumps(current_params.get("grade_thresholds", {}), ensure_ascii=False, indent=2)

      lift_lines = []
      for name, v in sorted(lift.items(), key=lambda x: -x[1]["lift_pp"]):
          lift_lines.append(
              f"  {name}: 有({v['n_present']}筆)勝率{v['present_wr']*100:.1f}% "
              f"vs 無({v['n_absent']}筆){v['absent_wr']*100:.1f}%  "
              f"Lift={v['lift_pp']:+.1f}pp  均報酬{v['avg_ret_present']:+.2f}%"
          )

      grade_lines = []
      for g, v in grade_summary.items():
          grade_lines.append(
              f"  {g}: N={v['n']}, T+1勝率={v['t1_wr']*100:.1f}%, 均報酬={v['t1_avg_ret']:+.2f}%"
          )

      return f"""你是台灣股市量化交易因子優化專家。以下是「噴發雷達」系統的因子表現數據，請分析並建議優化方向。

## 現有因子權重（config/surge_params.json）
### factors（加分點數）：
{factors_json}

### grade_thresholds（等級門檻）：
{gates_json}

## 統計期間：{n} 筆已結算信號（T+1 收盤結算）

## 等級表現：
{chr(10).join(grade_lines)}

## 因子 Lift 分析（T+1 勝率差異）：
{chr(10).join(lift_lines)}

## 分析要求：
1. 找出 Lift > 10pp 的高效因子，這些應該增加點數
2. 找出 Lift < 3pp 的低效因子，考慮減少點數或移除
3. 若 SURGE_GAMMA 勝率 < 45%，建議提高門檻（減少雜訊）
4. 若樣本數 < 10，標記為「樣本不足，暫不調整」
5. 考慮因子組合效應（例如 gap + inst_buy 同時存在）

## 輸出格式（嚴格遵守 JSON）：
{{
  "reasoning": "2-3句整體評估",
  "factor_changes": {{
    "factor_name": {{"current": N, "proposed": M, "reason": "..."}},
    ...
  }},
  "gate_changes": {{
    "SURGE_ALPHA": {{"current": N, "proposed": M, "reason": "..."}},
    ...
  }},
  "new_factors_to_consider": ["描述1", "描述2"],
  "confidence": "high/medium/low",
  "min_signals_needed": N
}}

只輸出 JSON，不要其他文字。"""


  def call_llm_optimization(prompt: str) -> dict | None:
      """Call LLM and parse JSON response. Returns None on failure."""
      try:
          from taiwan_stock_agent.domain.llm_provider import create_llm_provider
          llm = create_llm_provider(None)
          if llm is None:
              _console.print("[yellow]⚠ 找不到 LLM API key，略過 LLM 分析[/yellow]")
              return None
          _console.print(f"  [dim]呼叫 {llm.name} 分析因子…[/dim]")
          raw = llm.complete(prompt, max_tokens=1500)
          # Strip markdown fences if present
          raw = raw.strip()
          if raw.startswith("```"):
              raw = raw.split("```")[1]
              if raw.startswith("json"):
                  raw = raw[4:]
          return json.loads(raw.strip())
      except json.JSONDecodeError as e:
          _console.print(f"[red]LLM 回應無法解析 JSON: {e}[/red]")
          return None
      except Exception as e:
          _console.print(f"[red]LLM 呼叫失敗: {e}[/red]")
          return None


  def print_llm_suggestions(suggestion: dict, current_params: dict) -> None:
      """Pretty-print LLM suggestions as a Rich table."""
      _console.rule("[bold cyan]LLM 優化建議")
      _console.print(f"\n[bold]整體評估：[/bold] {suggestion.get('reasoning', '')}")
      _console.print(f"[dim]信心度：{suggestion.get('confidence', '?')} | "
                     f"建議最少樣本：{suggestion.get('min_signals_needed', '?')} 筆[/dim]\n")

      changes = suggestion.get("factor_changes", {})
      if changes:
          tbl = Table(title="因子權重建議", box=box.SIMPLE_HEAVY)
          tbl.add_column("因子"); tbl.add_column("現在", justify="right")
          tbl.add_column("建議", justify="right"); tbl.add_column("原因")
          for fname, v in changes.items():
              cur = v.get("current", "?"); prop = v.get("proposed", "?")
              diff = "" if cur == prop else (" ▲" if (prop or 0) > (cur or 0) else " ▼")
              style = "green" if diff == " ▲" else ("red" if diff == " ▼" else "")
              tbl.add_row(fname, str(cur), f"[{style}]{prop}{diff}[/{style}]", v.get("reason", ""))
          _console.print(tbl)

      gate_changes = suggestion.get("gate_changes", {})
      if gate_changes:
          tbl2 = Table(title="等級門檻建議", box=box.SIMPLE_HEAVY)
          tbl2.add_column("等級"); tbl2.add_column("現在", justify="right")
          tbl2.add_column("建議", justify="right"); tbl2.add_column("原因")
          for gname, v in gate_changes.items():
              tbl2.add_row(gname, str(v.get("current")), str(v.get("proposed")), v.get("reason", ""))
          _console.print(tbl2)

      new_factors = suggestion.get("new_factors_to_consider", [])
      if new_factors:
          _console.print("\n[bold]建議新增因子：[/bold]")
          for f in new_factors:
              _console.print(f"  • {f}")


  def apply_suggestions_interactive(suggestion: dict, params: dict) -> bool:
      """Interactively apply LLM suggestions to surge_params.json. Returns True if applied."""
      if not sys.stdin.isatty():
          _console.print("[yellow]非互動模式，略過套用[/yellow]")
          return False

      _console.print("\n[bold cyan]是否套用以上建議？[/bold cyan]")
      _console.print("  [y] 全部套用  [n] 略過  [s] 儲存建議至 config/surge_pending.json")
      choice = input("> ").strip().lower()

      if choice == "n":
          return False

      if choice == "s":
          pending_path = _PARAMS_PATH.parent / "surge_pending.json"
          pending_path.write_text(json.dumps(suggestion, ensure_ascii=False, indent=2))
          _console.print(f"[green]建議已儲存至 {pending_path}[/green]")
          return False

      # Apply factor changes
      factor_changes = suggestion.get("factor_changes", {})
      for fname, v in factor_changes.items():
          if fname in params.get("factors", {}):
              params["factors"][fname] = v["proposed"]

      # Apply gate changes
      gate_changes = suggestion.get("gate_changes", {})
      for gname, v in gate_changes.items():
          if gname in params.get("grade_thresholds", {}):
              params["grade_thresholds"][gname] = v["proposed"]

      _PARAMS_PATH.write_text(json.dumps(params, ensure_ascii=False, indent=2))
      _console.print(f"[green]✓ 已套用並儲存至 {_PARAMS_PATH}[/green]")
      return True


  def main() -> None:
      parser = argparse.ArgumentParser(description="Surge factor lift analysis and LLM optimization")
      parser.add_argument("--llm", action="store_true", help="呼叫 LLM 分析並建議權重")
      parser.add_argument("--apply", action="store_true", help="互動式套用 LLM 建議")
      parser.add_argument("--min-signals", type=int, default=30, help="最少已結算信號數（default: 30）")
      parser.add_argument("--lookback", type=int, default=90, help="回溯天數（default: 90）")
      args = parser.parse_args()

      from surge_db import query_settled
      try:
          signals = query_settled(min_settled=args.min_signals, lookback_days=args.lookback)
      except ValueError as e:
          _console.print(f"[yellow]{e}[/yellow]")
          return

      _console.rule(f"[bold]噴發雷達 因子分析 — {len(signals)} 筆已結算信號")

      lift = compute_lift(signals)
      grade_summary = build_grade_summary(signals)
      print_lift_table(lift)

      # Grade summary
      _console.print()
      for g, v in grade_summary.items():
          _console.print(f"  {g}: N={v['n']}, T+1勝率={v['t1_wr']*100:.1f}%, 均報酬={v['t1_avg_ret']:+.2f}%")

      if not (args.llm or args.apply):
          return

      current_params = json.loads(_PARAMS_PATH.read_text())
      prompt = build_llm_prompt(signals, lift, grade_summary, current_params)
      suggestion = call_llm_optimization(prompt)
      if suggestion is None:
          return

      print_llm_suggestions(suggestion, current_params)

      if args.apply:
          apply_suggestions_interactive(suggestion, current_params)


  if __name__ == "__main__":
      main()
  ```

- [ ] **Step 4: Run tests**
  ```bash
  .venv/bin/pytest tests/unit/test_surge_factor_report.py -v
  ```
  Expected: all 6 tests PASS

- [ ] **Step 5: Commit**
  ```bash
  git add scripts/surge_factor_report.py tests/unit/test_surge_factor_report.py
  git commit -m "feat: add surge_factor_report with lift analysis and LLM optimization"
  ```

---

## Task 5: Add Makefile targets and wire into `make report`

**Files:**
- Modify: `Makefile`

- [ ] **Step 1: Add `surge-factor` and `surge-tune` targets**

  In `Makefile`, find the `.PHONY` line and add `surge-factor surge-tune`:
  ```makefile
  .PHONY: plan report settle backtest backtest-compare factor-report optimize test setup migrate api install flow show bot-setup bot monitor surge surge-live surge-factor surge-tune
  ```

  After the `surge-live` block, add:
  ```makefile
  # ── Surge 因子分析 + LLM 優化 ──────────────────────────────────────────────
  # 用法: make surge-factor              # 顯示因子 Lift 表（不呼叫 LLM）
  #       make surge-tune               # LLM 建議 + 互動式套用
  surge-factor:
  	$(PYTHON) scripts/surge_factor_report.py --min-signals $(MIN_SIGNALS)

  surge-tune:
  	$(PYTHON) scripts/surge_factor_report.py --llm --apply --min-signals $(MIN_SIGNALS)

  MIN_SIGNALS ?= 30
  ```

- [ ] **Step 2: Update `make flow` comment block**

  In `Makefile`, find the flow comment:
  ```makefile
  # ── 用法: make flow
  #   1. plan   — 預突破批次掃描
  #   3. report — T+1 結算 + 勝率報告
  ```
  Replace with:
  ```makefile
  # ── 用法: make flow
  #   1. plan         — 預突破批次掃描
  #   2. surge        — 噴發雷達掃描（儲存至 surge_signals.db）
  #   3. report       — T+1 結算（含 surge 信號） + 勝率報告
  ```

- [ ] **Step 3: Verify make targets exist**
  ```bash
  make surge-factor --dry-run 2>&1 | head -3
  make surge-tune --dry-run 2>&1 | head -3
  ```
  Expected: shows the python command without executing

- [ ] **Step 4: Run full test suite**
  ```bash
  make test 2>&1 | tail -5
  ```
  Expected: same or higher pass count as before

- [ ] **Step 5: Commit**
  ```bash
  git add Makefile
  git commit -m "feat: add make surge-factor and make surge-tune targets"
  ```

---

## Verification

**End-to-end test (after accumulating ≥30 settled signals):**

```bash
# Day 1: run surge scan (signals written to DB)
make surge

# Check signals were written
python - <<'EOF'
import sqlite3
con = sqlite3.connect("data/surge_signals.db")
rows = con.execute("SELECT signal_date, COUNT(*) FROM surge_signals GROUP BY signal_date").fetchall()
print(rows)
EOF

# Day 2+: run report (settles T+1)
make report
# Expected console output: "[Step 1b] Surge 信號結算（T+1/T+3/T+5）... 結算完成: N 筆 surge 信號"

# After 30+ settled: factor analysis
make surge-factor
# Expected: Rich table showing Lift pp for each factor

# LLM optimization + interactive review
make surge-tune
# Expected: LLM suggestion table, then prompt to apply/skip/save
```

**Graceful degradation (no DB, no LLM):**
```bash
# surge_db missing → scan still completes, just prints "surge_signals DB: 略過"
# LLM key missing → surge-factor still shows lift table, LLM section skipped with warning
# Fewer than MIN_SIGNALS → "只有 N 筆已結算信號（需要 ≥30 筆）" and exits cleanly
```
