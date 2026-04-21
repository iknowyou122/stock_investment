<!-- /autoplan restore point: /Users/07683.howard.huang/.gstack/projects/iknowyou122-stock_investment/main-autoplan-restore-20260421-154710.md -->
# Phase 4.23: Coil Tracking + Rolling Backtest Optimization

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Track every stock flagged by the AccumulationEngine (蓄積雷達) daily, check whether it achieved a breakout within a rolling 10-day window, report win-rate by grade (COIL_PRIME/MATURE/EARLY), and surface which engine parameters are underperforming to close the optimization loop.

**Architecture:** One new script (`scripts/coil_monitor.py`) + local SQLite database (`db/coil_track.db`) + new `make coil-monitor` target. SQLite gives atomic writes + ad-hoc SQL queries, at marginal extra effort vs JSON. No Postgres migration required (separate from main DB).

---

## Background & Motivation

The AccumulationEngine produces daily `coil_*.csv` snapshots. We have historical replay (`coil_backtest.py`) and factor analysis (`coil_factor_report.py`), but no live tracking:

- **No daily feedback loop.** We never know if yesterday's COIL_PRIME picks actually broke out.
- **Optimization is blind.** `optimize_coil.py` runs grid search on historical data, but we have no live win-rate signal to validate against.
- **Grade calibration unknown.** Is COIL_PRIME actually better than COIL_EARLY? By how much? Over what horizon?

The existing `accuracy_monitor.py` solves the exact same problem for `scan_*.csv` (Triple Confirmation signals). This plan replicates that pattern with improvements.

---

## Section 1: Data Layer

### 1A: What coil_*.csv already captures

Each row has:
- `scan_date`, `analysis_date`, `ticker`, `name`, `market`
- `grade`: COIL_PRIME / COIL_MATURE / COIL_EARLY
- `score`, `bb_pct`, `vol_ratio`, `consol_range_pct`
- `inst_consec_days`, `weeks_consolidating`
- `vs_60d_high_pct`: distance from 60d high as percentage (negative = below, e.g. -10.12)
- `score_breakdown` (JSON), `flags`

### 1B: CoilSignalRecord schema (JSON cache)

```python
@dataclass
class CoilSignalRecord:
    sig_id: str               # f"{ticker}_{analysis_date}_{grade}" — includes grade to prevent collision
    ticker: str
    name: str
    market: str
    signal_date: str          # analysis_date from CSV
    grade: str                # COIL_PRIME / COIL_MATURE / COIL_EARLY
    score: int
    bb_pct: float
    vol_ratio: float
    weeks_consolidating: int
    vs_60d_high_pct: float
    entry_close: float        # close on signal_date (from CSV — the scan close price)
    resistance: float         # 60d high pinned at signal time (DERIVED at ingest, not re-fetched)
    outcome: str              # PENDING / BREAKOUT / STALLED / EXPIRED
    days_to_breakout: int | None
    max_gain_pct: float | None
    max_adverse_excursion_pct: float | None  # MAE: worst drawdown before breakout or expiry
    checked_thru: str | None  # last date we checked this signal
    breakout_date: str | None
```

**sig_id** uses `{ticker}_{analysis_date}_{grade}` to handle the edge case where the same ticker
appears in the same CSV with different grades (COIL_EARLY promoted to COIL_MATURE on same day).

**resistance derivation at ingest (CRITICAL — never re-derive from OHLCV later):**

```python
# vs_60d_high_pct = (close / 60d_high - 1) × 100, e.g. -10.12 means close is 10.12% below 60d high
# Therefore: 60d_high = close / (1 + vs_60d_high_pct / 100)
resistance = entry_close / (1 + vs_60d_high_pct / 100)
# Example: entry_close=89.0, vs_60d_high_pct=-10.12 → resistance = 89.0 / 0.8988 ≈ 99.02
```

This avoids any re-fetch and ensures resistance is stable across all future outcome checks for this signal.

### 1C: Storage choice — SQLite (user-approved)

**`db/coil_track.db`** — a standalone SQLite database separate from the main Postgres DB.

Schema:
```sql
CREATE TABLE IF NOT EXISTS coil_signals (
    sig_id TEXT PRIMARY KEY,     -- ticker_analysis_date_grade
    ticker TEXT NOT NULL,
    name TEXT,
    market TEXT,
    signal_date TEXT NOT NULL,
    grade TEXT NOT NULL,
    score INTEGER,
    bb_pct REAL,
    vol_ratio REAL,
    weeks_consolidating INTEGER,
    vs_60d_high_pct REAL,
    entry_close REAL NOT NULL,
    resistance REAL NOT NULL,    -- pinned at ingest
    outcome TEXT DEFAULT 'PENDING',
    days_to_breakout INTEGER,
    max_gain_pct REAL,
    max_adverse_excursion_pct REAL,
    checked_thru TEXT,
    breakout_date TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_outcome ON coil_signals (outcome);
CREATE INDEX IF NOT EXISTS idx_grade   ON coil_signals (grade);
CREATE INDEX IF NOT EXISTS idx_signal_date ON coil_signals (signal_date);
```

SQLite write pattern (atomic by default via WAL mode):
```python
import sqlite3
from contextlib import contextmanager

DB_PATH = Path("db/coil_track.db")

@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")  # concurrent reads + single writer
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

No `os.replace()` needed — SQLite WAL mode provides crash-safe atomic writes.

### 1D: Breakout criterion

```python
BREAKOUT_THRESHOLD = 1.01  # close > resistance × 1.01
TRACKING_WINDOW = 10       # trading days to watch
EXPIRY_WINDOW = 15         # days after which PENDING → EXPIRED

def _evaluate_coil_outcome(
    entry_close: float,
    resistance: float,    # pinned at ingest time — do NOT re-compute
    future_bars: list[dict],
) -> tuple[str, int | None, float | None, float | None]:
    """Returns (outcome, days_to_breakout, max_gain_pct, mae_pct)."""
    if not future_bars:
        return "PENDING", None, None, None
    threshold = resistance * BREAKOUT_THRESHOLD
    min_close = entry_close
    max_close = entry_close
    for i, bar in enumerate(future_bars, start=1):
        min_close = min(min_close, bar["low"])   # track drawdown via bar low
        max_close = max(max_close, bar["close"])
        if bar["close"] >= threshold:
            mae = (min_close / entry_close - 1) * 100
            gain = (max_close / entry_close - 1) * 100
            return "BREAKOUT", i, gain, mae
    mae = (min_close / entry_close - 1) * 100
    gain = (max_close / entry_close - 1) * 100
    if len(future_bars) >= EXPIRY_WINDOW:
        return "EXPIRED", None, gain, mae
    return "STALLED", None, gain, mae
```

Note `mae_pct` uses bar `low` (not close) to capture the worst intraday price the trader would have seen.

### 1E: Win-rate display guard

Suppress win-rate stats when sample count is below threshold:

```python
MIN_SAMPLE_FOR_WINRATE = 20  # per grade

def _format_winrate(n: int, wins: int) -> str:
    if n < MIN_SAMPLE_FOR_WINRATE:
        return f"[dim]N={n} (不足 {MIN_SAMPLE_FOR_WINRATE} 筆)[/dim]"
    pct = wins / n * 100
    return f"{pct:.0f}%  (N={n})"
```

---

## Section 2: coil_monitor.py

### CLI interface

```
python scripts/coil_monitor.py                        # Dashboard: last 30 days
python scripts/coil_monitor.py --date 2026-04-15     # Stats for specific date
python scripts/coil_monitor.py --grade COIL_PRIME    # Filter by grade
python scripts/coil_monitor.py --top 10              # Top performers
python scripts/coil_monitor.py --export report.csv   # Export to CSV
python scripts/coil_monitor.py --refresh             # Force re-check all PENDING
make coil-monitor
```

### Dashboard output

```
╔══════════════════════════════════════════════════════════════════╗
║         蓄積雷達追蹤看板   2026-04-21                           ║
╠════════════════╦═══════╦═══════════╦═══════════╦════════════════╣
║ 等級           ║ 樣本數 ║ 突破率    ║ 平均天數  ║ 平均 MAE      ║
╠════════════════╬═══════╬═══════════╬═══════════╬════════════════╣
║ COIL_PRIME     ║  24   ║ 67%       ║ 4.2 天    ║ -1.8%         ║
║ COIL_MATURE    ║  41   ║ 51%       ║ 6.8 天    ║ -2.4%         ║
║ COIL_EARLY     ║  8    ║ N=8 (不足) ║ —        ║ —             ║
╠════════════════╬═══════╬═══════════╬═══════════╬════════════════╣
║ ALL            ║  73   ║ 56%       ║ 5.8 天    ║ -2.1%         ║
╚════════════════╩═══════╩═══════════╩═══════════╩════════════════╝

PENDING: 18 signals awaiting outcome
最近突破: 2454 聯發科 (COIL_PRIME, +4.2%, MAE -0.8%, Day 3)
最近失敗: 3711 日月光 (COIL_MATURE, -1.8%, MAE -4.2%, Expired Day 10)
```

### Factor correlation section

For STALLED/EXPIRED signals, compute average score_breakdown per factor vs BREAKOUT signals:
- Which factors score lower in failures?
- Print top-3 differentiating factors with lift delta
- Feed this directly into `optimize_coil.py` recommendations

---

## Section 3: Integration with coil_scan.py

Add `--track` flag. When `coil_scan.py --save-csv --track` runs:
1. Load `config/coil_outcomes_cache.json` (create if absent)
2. For each new signal in today's CSV: ingest into cache (status=PENDING), derive `resistance` at ingest time
3. Check yesterday's PENDING signals that now have D+1 data → update outcomes
4. Save atomically via `os.replace()`

**Makefile update required:** Change the existing `coil` target to always pass `--track`:

```makefile
coil:
    $(PYTHON) scripts/coil_scan.py --save-csv --track --notify \
        $(if $(SECTORS),--sectors $(SECTORS)) \
        $(if $(TICKERS),--tickers $(TICKERS)) \
        $(if $(DATE),--date $(DATE))
```

This ensures every operator running `make coil` populates the cache automatically.

**Single writer rule:** `coil_scan.py --track` and `coil_monitor.py --refresh` both write to the same SQLite DB. SQLite WAL mode handles concurrent reads safely. For write serialization, use `PRAGMA busy_timeout=5000` so the second writer waits up to 5 s rather than failing immediately.

---

## Section 4: Rolling Optimization Loop

### New make target: `make coil-loop`

```makefile
coil-loop:
    $(PYTHON) scripts/coil_scan.py --save-csv --track --notify    # Scan + ingest
    $(PYTHON) scripts/coil_monitor.py --refresh                   # Update outcomes
    $(PYTHON) scripts/coil_factor_report.py --live-db db/coil_track.db
    $(PYTHON) scripts/optimize_coil.py --live-db db/coil_track.db  # live SQLite outcomes as ground truth
```

### How the feedback loop works

1. `coil_scan.py --track` → saves `coil_YYYY-MM-DD.csv` + ingests new signals into cache
2. `coil_monitor.py --refresh` → checks all PENDING signals, fetches OHLCV for unchecked days, updates cache
3. `coil_factor_report.py --live-cache ...` → reads LIVE outcomes (not synthetic backtest). Factor lift now uses real breakout data.
4. `optimize_coil.py --live-cache ...` → grid search using live outcomes as ground truth

### Auto-apply safety gate

`optimize_coil.py --auto-apply` is dangerous with small samples. Gate conditions (ALL required):
- Minimum 90 calendar days of live signals in cache
- N ≥ 50 completed outcomes per grade being optimized
- Walk-forward lift > 5% (existing threshold)

If gate conditions not met, print "⚠ 樣本不足 (需90天+50筆/等級)，建議手動審查" and exit without applying.

---

## Section 5: Telegram Integration

`bot.py` already has a coil radar block. Add a "今日蓄積追蹤" summary (refreshed every 30s):

```
蓄積追蹤  (近30天: 73筆)
PRIME  67% ▲ (N=24)  MATURE  51% → (N=41)  EARLY  N=8 不足
昨日新增: 12 檔  ●  待確認: 18 檔  ●  近期 MAE avg: -2.1%
```

---

## Section 6: `coil_factor_report.py` modification

**Required (not optional, user-approved):** Add `--live-db PATH` argument that, when provided:
- Queries `db/coil_track.db` for resolved `CoilSignalRecord` outcomes instead of `coil_backtest.py` synthetic output
- Computes factor lift against real breakout/stall/expired outcomes from SQLite
- Falls back to backtest output when `--live-db` not provided (backward compatible)

Without this, `make coil-loop` step 3 produces identical output to today. This is the wire that closes the loop.

---

## Files to Create / Modify

| File | Action | What changes |
|---|---|---|
| `scripts/coil_monitor.py` | **Create** | Full tracking dashboard, SQLite R/W (WAL), OHLCV outcome check, MAE tracking |
| `db/coil_track.db` | **Create (auto)** | Auto-created on first run by coil_monitor.py (SQLite, gitignored) |
| `scripts/coil_scan.py` | **Modify** | Add `--track` flag; update `make coil` to always pass it |
| `scripts/coil_factor_report.py` | **Modify** | Add `--live-db PATH` argument to query live SQLite outcomes |
| `scripts/optimize_coil.py` | **Modify** | Add `--live-db PATH` + auto-apply safety gate (90d + N≥50) |
| `scripts/bot.py` | **Modify** | Add coil tracking summary to coil radar block |
| `Makefile` | **Modify** | Update `coil` target (add `--track`), add `coil-monitor` + `coil-loop` targets |
| `tests/unit/test_coil_monitor.py` | **Create** | Unit tests (see Section 7) |

---

## Section 7: Test Plan

**Required test cases for `test_coil_monitor.py`:**

```python
# 1. Resistance formula correctness
def test_resistance_formula():
    # vs_60d_high_pct=-10.0, entry_close=90.0
    # Expected: 90.0 / (1 + (-10.0)/100) = 90.0 / 0.9 = 100.0
    assert _derive_resistance(entry_close=90.0, vs_60d_high_pct=-10.0) == pytest.approx(100.0)
    # NOT 90.0 × (1 + 10.0) = 990.0 — old wrong formula

# 2. Breakout detection
def test_breakout_detected_on_day_3():
    # resistance=100.0, threshold=101.0
    # Day 1: close=98, Day 2: close=99, Day 3: close=102
    outcome, days, gain, mae = _evaluate_coil_outcome(
        entry_close=90.0, resistance=100.0,
        future_bars=[{"close": 98, "low": 97}, {"close": 99, "low": 98.5}, {"close": 102, "low": 101}]
    )
    assert outcome == "BREAKOUT"
    assert days == 3

# 3. MAE tracks bar low, not close
def test_mae_uses_bar_low():
    _, _, _, mae = _evaluate_coil_outcome(
        entry_close=100.0, resistance=110.0,
        future_bars=[{"close": 98, "low": 94}, {"close": 97, "low": 95}]
    )
    assert mae == pytest.approx((94 / 100 - 1) * 100)  # -6.0%, not -3.0%

# 4. N-guard suppresses win-rate below minimum
def test_winrate_suppressed_when_n_below_minimum():
    result = _format_winrate(n=8, wins=5)
    assert "不足" in result
    assert "%" not in result

# 5. Idempotency: --refresh twice yields same result
def test_refresh_idempotent(tmp_path):
    # Create a cache, run refresh, run refresh again
    # Assert cache contents identical

# 6. sig_id collision: same ticker + date, different grades → two separate records
def test_sig_id_includes_grade():
    r1 = CoilSignalRecord(ticker="2330", signal_date="2026-04-20", grade="COIL_EARLY", ...)
    r2 = CoilSignalRecord(ticker="2330", signal_date="2026-04-20", grade="COIL_PRIME", ...)
    assert r1.sig_id != r2.sig_id

# 7. SQLite WAL: concurrent read during write sees consistent state
def test_sqlite_wal_concurrent_read(tmp_path):
    # Write a signal with WAL mode, then read from a second connection mid-transaction
    # Assert reader sees committed state (not partial write)

# 8. auto-apply gate blocks when N < 50
def test_auto_apply_gate_blocks_small_sample():
    with pytest.raises(SystemExit):
        apply_if_safe(cache_path=..., min_n_per_grade=50, min_days=90)
```

---

## Out of Scope

- Postgres migration for coil_tracking (SQLite `db/coil_track.db` is self-contained)
- Real-time intraday coil tracking (daily close sufficient for accumulation patterns)
- Telegram push for individual coil outcomes (summary widget only)
- Integration with Phase 4.22 A/B test framework (separate task)

---

## Acceptance Criteria

1. `make coil-monitor` runs and shows dashboard with MAE column
2. Win-rate suppressed when N < 20 per grade (shows "不足 20 筆" instead)
3. `resistance` derived correctly from `vs_60d_high_pct` at ingest (not re-fetched)
4. SQLite WAL mode enabled (`PRAGMA journal_mode=WAL`); `busy_timeout=5000` for write contention
5. `coil_scan.py --track` ingests today's signals automatically; `make coil` auto-passes `--track`
6. `coil_factor_report.py --live-db` reads live SQLite outcomes when flag provided
7. `optimize_coil.py --auto-apply` blocked unless 90d + N≥50 per grade
8. 8 unit test cases in `test_coil_monitor.py` all passing
9. Bot shows coil tracking summary widget with MAE

---

## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|---------|
| 1 | CEO | Fix `resistance` derivation — use `entry_close / (1 + vs_60d_high_pct/100)` at ingest | Mechanical | P1, P5 | Subagent identified critical bug: formula without /100 gives 11× price | Re-fetch from OHLCV (slow, unnecessary) |
| 2 | CEO | Add `max_adverse_excursion_pct` (MAE) to `CoilSignalRecord` | Mechanical | P1 | Subagent: win-rate without MAE is misleading; trader needs drawdown context | Defer to v2 |
| 3 | CEO | Use `os.replace()` for atomic JSON writes | Mechanical | P1 | Subagent: json.dump() directly → partial-write corruption on crash | Keep non-atomic |
| 4 | CEO | Suppress win-rate when N < 20 per grade | Mechanical | P1 | Subagent: N=8 showing "67%" misleads; 95% CI ≈ ±20% | Always show |
| 5 | CEO | Gate `--auto-apply` behind 90d + N≥50 | Mechanical | P1 | Subagent: overfitting with N=24; grid search result is noise | No gate |
| 6 | CEO→USER | **SQLite** chosen for `db/coil_track.db` | **TASTE→DECIDED** | P3 | User chose SQLite: atomic writes + ad-hoc SQL + future JOIN with signal_outcomes | JSON cache |
| 7 | CEO→USER | **Wire `coil_factor_report.py --live-db` now** | **TASTE→DECIDED** | P1 | User approved: completes full feedback loop in this PR | Defer |
| 8 | Eng | Include grade in `sig_id`: `{ticker}_{analysis_date}_{grade}` | Mechanical | P5 | Eng subagent: same ticker + date can appear twice with different grades | Ticker+date only |
| 9 | Eng | `coil_scan.py --track` = sole writer; `coil_monitor.py` reads only (or uses same CacheStore lock) | Mechanical | P5 | Eng subagent: two concurrent writers to same JSON without locking = race condition |  |
| 10 | Eng | Update `make coil` to always pass `--track` | Mechanical | P1 | Eng subagent: if `--track` is opt-in, most operators will never populate the cache | Optional flag |

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | issues_open | 6 findings: resistance formula (critical), MAE missing, JSON atomicity, N-guard, auto-apply gate, storage choice |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | unavailable | Codex auth expired — run `codex auth login` to re-enable |
| Eng Review | `/plan-eng-review` | Architecture & tests | 1 | issues_open | 5 findings: resistance /100 bug, two writers race, sig_id collision, factor_report not wired, make coil missing --track |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | skipped | No web UI scope |

**VERDICT:** REVIEWED — all mechanical findings auto-decided and incorporated into plan. 2 taste decisions surfaced at approval gate.
