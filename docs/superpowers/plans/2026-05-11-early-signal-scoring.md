# Early Signal Scoring Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three structural scoring biases that caused 信昌電 (6173) to receive WATCH 47 on its optimal entry day instead of LONG, by tiering sector rank bonuses, compensating first-day near-high setups, and lowering the LONG threshold for confirmed pre-breakout coils.

**Architecture:** Three independent patches applied in order: (1) `_apply_sector_ranks()` in `batch_plan.py` gains tiered bonuses; (2) new `_apply_near_high_first_day()` in `batch_plan.py` compensates first-appearance stocks in the 92-99% zone; (3) `_map_action()` in `triple_confirmation_engine.py` reduces the LONG threshold by 5 when `proximity_pts == 12`.

**Tech Stack:** Python 3.11, pytest, `scripts/batch_plan.py`, `src/taiwan_stock_agent/domain/triple_confirmation_engine.py`

---

## File Map

| File | Change |
|------|--------|
| `scripts/batch_plan.py` | Fix `_apply_sector_ranks()` tiering; add `proximity_pts` to `_run_ticker()` result; add `_apply_near_high_first_day()`; wire into `run_batch()` |
| `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` | Modify `_map_action()` to reduce threshold when `proximity_pts == 12` |
| `tests/unit/test_persistence_bonus.py` | Fix broken `batch_scan` import → `batch_plan`; add Fix 1 + Fix 2 tests |
| `tests/unit/test_triple_confirmation_engine_v2.py` | Add Fix 3 tests for proximity-adjusted threshold |

---

## Task 0: Fix broken test import

The file `tests/unit/test_persistence_bonus.py` imports from `batch_scan` which no longer exists (renamed to `batch_plan`). Fix this before adding new tests so we can run the file.

**Files:**
- Modify: `tests/unit/test_persistence_bonus.py:13`

- [ ] **Step 1: Run tests to confirm the break**

```bash
.venv/bin/pytest tests/unit/test_persistence_bonus.py -q 2>&1 | head -10
```
Expected: `ModuleNotFoundError: No module named 'batch_scan'`

- [ ] **Step 2: Fix the import**

In `tests/unit/test_persistence_bonus.py`, replace line 13:
```python
# Before
from batch_scan import _load_recent_csvs, _apply_persistence_bonus
```
```python
# After
from batch_plan import _load_recent_csvs, _apply_persistence_bonus
```

- [ ] **Step 3: Run tests to confirm they pass**

```bash
.venv/bin/pytest tests/unit/test_persistence_bonus.py -q
```
Expected: all existing tests pass (no errors).

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_persistence_bonus.py
git commit -m "fix: update test_persistence_bonus import batch_scan→batch_plan"
```

---

## Task 1: Fix 1 — Sector rank tiered bonuses (TDD)

**Files:**
- Modify: `scripts/batch_plan.py:320-348` (`_apply_sector_ranks`)
- Modify: `tests/unit/test_persistence_bonus.py` (add new tests at end of file)

### Step 1a — Write failing tests

- [ ] **Step 1: Add tier tests to test_persistence_bonus.py**

Append to `tests/unit/test_persistence_bonus.py`:

```python
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
        # rank 1 (highest confidence = 50) should get +10
        top = next(r for r in results if r["ticker"] == "0")
        assert top["confidence"] == 60  # 50 + 10

    def test_top_10pct_gets_7(self):
        """With 20 stocks, rank 2 is top 10% (not top 5%) → +7."""
        results = _make_sector_results([(str(i), 50 - i) for i in range(20)])
        industry_map = {str(i): "半導體" for i in range(20)}
        _apply_sector_ranks(results, industry_map)
        # rank 2 (confidence = 49) → top 10%, not top 5% → +7
        second = next(r for r in results if r["ticker"] == "1")
        assert second["confidence"] == 49 + 7

    def test_top_20pct_gets_5(self):
        """With 20 stocks, rank 4 is top 20% but not top 10% → +5."""
        results = _make_sector_results([(str(i), 50 - i) for i in range(20)])
        industry_map = {str(i): "半導體" for i in range(20)}
        _apply_sector_ranks(results, industry_map)
        # rank 4 (confidence = 47) → top 20% only → +5
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/pytest tests/unit/test_persistence_bonus.py::TestSectorRanksTiered -q
```
Expected: all 7 tests FAIL (current code gives +5 flat, no flag).

### Step 1b — Implement

- [ ] **Step 3: Replace `_apply_sector_ranks` in `scripts/batch_plan.py`**

Replace lines 320–348 (the entire `_apply_sector_ranks` function):

```python
def _apply_sector_ranks(results: list[dict], industry_map: dict[str, str]) -> int:
    """Boost stocks by sector rank tier (top 5%→+10, top 10%→+7, top 20%→+5).

    Only applied when a sector has ≥ 3 valid (non-halt) results.
    Adds SECTOR_RANK:N/M flag to boosted stocks.
    Returns count of stocks boosted.
    """
    from collections import defaultdict

    sector_valid: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        if r["halt"] or r["error"] is not None:
            continue
        sector = industry_map.get(r["ticker"], "")
        if sector:
            sector_valid[sector].append(r)

    boosted = 0
    for sector, rs in sector_valid.items():
        if len(rs) < 3:
            continue
        sorted_rs = sorted(rs, key=lambda r: r["confidence"], reverse=True)
        total = len(sorted_rs)
        top_5pct  = max(1, total // 20)
        top_10pct = max(1, total // 10)
        top_20pct = max(1, total // 5)
        for rank, r in enumerate(sorted_rs[:top_20pct], 1):
            if rank <= top_5pct:
                bonus = 10
            elif rank <= top_10pct:
                bonus = 7
            else:
                bonus = 5
            r["confidence"] = min(100, r["confidence"] + bonus)
            r["flags"] = list(r.get("flags") or []) + [f"SECTOR_RANK:{rank}/{total}"]
            boosted += 1

    return boosted
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
.venv/bin/pytest tests/unit/test_persistence_bonus.py::TestSectorRanksTiered -q
```
Expected: all 7 tests PASS.

- [ ] **Step 5: Update the log message in `run_batch()` (line ~1098)**

Find this line in `scripts/batch_plan.py`:
```python
            _console.print(f"  [dim]↑ 產業相對排名加分: {n_sector} 檔 (+5 pts each)[/dim]")
```
Replace with:
```python
            _console.print(f"  [dim]↑ 產業相對排名加分: {n_sector} 檔 (+5/+7/+10 tier)[/dim]")
```

- [ ] **Step 6: Run full unit tests to confirm no regressions**

```bash
.venv/bin/pytest tests/unit/ -q --ignore=tests/unit/test_api_outcome_endpoint.py
```
Expected: same pass/skip counts as baseline (415 passed), with Task 0 and Fix 1 tests now also passing.

- [ ] **Step 7: Commit**

```bash
git add scripts/batch_plan.py tests/unit/test_persistence_bonus.py
git commit -m "feat: tier sector rank bonuses (top5%+10 / top10%+7 / top20%+5) + re-enable SECTOR_RANK flag"
```

---

## Task 2: Fix 2 — Near-high coil first-day bonus (TDD)

**Files:**
- Modify: `scripts/batch_plan.py` — `_run_ticker()` result dict + new `_apply_near_high_first_day()` + `run_batch()` wiring
- Modify: `tests/unit/test_persistence_bonus.py` — add new test class

### Step 2a — Add proximity_pts to result dict

- [ ] **Step 1: Add `proximity_pts` to `_run_ticker()` result**

In `scripts/batch_plan.py`, find the `_run_ticker()` function's success return dict (around line 596):

```python
        return {
            "ticker": ticker,
            "action": signal.action,
            "confidence": signal.confidence,
            "halt": signal.halt_flag,
            "free_tier": signal.free_tier_mode,
            "flags": signal.data_quality_flags,
            "entry_bid": signal.execution_plan.entry_bid_limit,
            "stop_loss": signal.execution_plan.stop_loss,
            "target": signal.execution_plan.target,
            "momentum": signal.reasoning.momentum if signal.reasoning else "",
            "chip": signal.reasoning.chip_analysis if signal.reasoning else "",
            "risk": signal.reasoning.risk_factors if signal.reasoning else "",
            "elapsed": elapsed,
            "error": None,
            "_signal": signal,
            "trend_score": trend_score,
            "institution_continuity_pts": breakdown_pts.get("institution_continuity_pts", 0),
        }
```

Add `"proximity_pts"` after `"institution_continuity_pts"`:

```python
            "institution_continuity_pts": breakdown_pts.get("institution_continuity_pts", 0),
            "proximity_pts": breakdown_pts.get("proximity_pts", 0),
```

Also add `"proximity_pts": 0` to the ERROR fallback dict (a few lines below, around line 600):
```python
        return {
            "ticker": ticker,
            "action": "ERROR",
            "confidence": -1,
            "halt": True,
            "free_tier": None,
            "flags": [],
            "entry_bid": 0.0,
            "stop_loss": 0.0,
            "target": 0.0,
            "momentum": "",
            "chip": "",
            "risk": "",
            "elapsed": time.time() - t0,
            "error": str(e),
            "_signal": None,
            "trend_score": 0,
            "institution_continuity_pts": 0,
        }
```
Add after `"institution_continuity_pts": 0,`:
```python
            "proximity_pts": 0,
```

### Step 2b — Write failing tests

- [ ] **Step 2: Add near-high tests to test_persistence_bonus.py**

Append to `tests/unit/test_persistence_bonus.py` (after the `TestSectorRanksTiered` class):

```python
# ──────────────────────────────────────────────────────────────────────────────
# Near-high first-day bonus tests (Fix 2)
# ──────────────────────────────────────────────────────────────────────────────
from batch_plan import _apply_near_high_first_day


class TestNearHighFirstDay:
    def test_first_day_proximity12_gets_4(self, tmp_path):
        """Stock appearing for the first time with proximity_pts=12 gets +4."""
        # No CSV files → ticker is a first-day appearance
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
        _write_csv(tmp_path, date(2026, 4, 12), [{"ticker": "6173", "confidence": 44}])
        results = [
            {"ticker": "6173", "confidence": 47, "halt": False, "error": None,
             "flags": [], "proximity_pts": 12},
        ]
        n = _apply_near_high_first_day(results, date(2026, 4, 13), tmp_path)
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
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
.venv/bin/pytest tests/unit/test_persistence_bonus.py::TestNearHighFirstDay -q
```
Expected: `ImportError` or `AttributeError` — `_apply_near_high_first_day` doesn't exist yet.

### Step 2c — Implement

- [ ] **Step 4: Add `_apply_near_high_first_day` to `scripts/batch_plan.py`**

Insert this function immediately after `_apply_persistence_bonus` (around line 499, before `_make_label_repo`):

```python
def _apply_near_high_first_day(
    results: list[dict],
    analysis_date: date,
    data_dir: Path,
) -> int:
    """Give +4 pts to stocks in the 92-99% zone (proximity_pts=12) on their first scan day.

    Compensates for the missing day-1 persist bonus on strong pre-breakout setups.
    Only activates when the ticker was absent from yesterday's CSV.
    Called after _apply_persistence_bonus so there is no double-count.
    Returns count of stocks boosted.
    """
    recent = _load_recent_csvs(analysis_date, data_dir, lookback=1, min_conf=40)
    yesterday_tickers: set[str] = set(recent[0].keys()) if recent else set()

    boosted = 0
    for r in results:
        if r.get("halt") or r.get("error") is not None:
            continue
        if r["ticker"] in yesterday_tickers:
            continue
        if r.get("proximity_pts", 0) == 12:
            r["confidence"] = min(100, r["confidence"] + 4)
            r["flags"] = list(r.get("flags") or []) + ["NEAR_HIGH_COIL"]
            boosted += 1

    return boosted
```

- [ ] **Step 5: Wire `_apply_near_high_first_day` into `run_batch()`**

In `scripts/batch_plan.py`, find the post-processing block in `run_batch()` (around line 1104):

```python
    n_persist = _apply_persistence_bonus(results, analysis_date, scan_data_dir)
    if n_persist:
        _console.print(f"  [dim]↑ 持續訊號加分: {n_persist} 檔 (RISING +7 / STABLE +5)[/dim]")
```

Add after it:

```python
    n_near_high = _apply_near_high_first_day(results, analysis_date, scan_data_dir)
    if n_near_high:
        _console.print(f"  [dim]↑ 近高蓄積首日補償: {n_near_high} 檔 (NEAR_HIGH_COIL +4)[/dim]")
```

- [ ] **Step 6: Run tests to confirm they pass**

```bash
.venv/bin/pytest tests/unit/test_persistence_bonus.py::TestNearHighFirstDay -q
```
Expected: all 6 tests PASS.

- [ ] **Step 7: Run full unit tests**

```bash
.venv/bin/pytest tests/unit/ -q --ignore=tests/unit/test_api_outcome_endpoint.py
```
Expected: same baseline pass count + new tests pass.

- [ ] **Step 8: Commit**

```bash
git add scripts/batch_plan.py tests/unit/test_persistence_bonus.py
git commit -m "feat: add near-high coil first-day bonus (+4 pts, NEAR_HIGH_COIL flag) for pre-breakout setups"
```

---

## Task 3: Fix 3 — Proximity-adjusted LONG threshold (TDD)

**Files:**
- Modify: `src/taiwan_stock_agent/domain/triple_confirmation_engine.py:1554-1571` (`_map_action`)
- Modify: `tests/unit/test_triple_confirmation_engine_v2.py` — add new test class

### Step 3a — Write failing tests

- [ ] **Step 1: Add threshold tests to test_triple_confirmation_engine_v2.py**

Find the end of the existing `TestMapAction` class (around line 955). Add a new test class after it:

```python
class TestMapActionProximityThreshold:
    """Verify that proximity_pts=12 lowers the LONG threshold by 5."""

    def _make_bd_with_proximity(self, proximity: int) -> _ScoreBreakdown:
        bd = _ScoreBreakdown()
        bd.proximity_pts = proximity
        return bd

    def _make_taiex_history(self, rising: bool, n: int = 30):
        from datetime import date, timedelta
        bars = []
        base = date(2026, 1, 2)
        for i in range(n):
            close = 20000.0 + (i * 10 if rising else -i * 10)
            d = base + timedelta(days=i)
            bars.append(DailyOHLCV(
                ticker="^TWII", trade_date=d,
                open=close, high=close + 50, low=close - 50, close=close, volume=1_000_000,
            ))
        return bars

    def test_uptrend_proximity12_threshold_55(self):
        """Uptrend + proximity_pts=12 → LONG threshold drops 60→55."""
        eng = TripleConfirmationEngine()
        eng._taiex_history = self._make_taiex_history(rising=True, n=30)
        bd = self._make_bd_with_proximity(12)
        # Score 57: without fix → WATCH (threshold 60); with fix → LONG (threshold 55)
        assert eng._map_action(57, bd=bd) == "LONG"
        assert eng._map_action(54, bd=bd) == "WATCH"  # still below 55

    def test_uptrend_no_proximity_still_60(self):
        """Uptrend without proximity=12 → threshold unchanged at 60."""
        eng = TripleConfirmationEngine()
        eng._taiex_history = self._make_taiex_history(rising=True, n=30)
        bd = self._make_bd_with_proximity(6)  # mid zone, not max
        assert eng._map_action(59, bd=bd) == "WATCH"   # 59 < 60
        assert eng._map_action(60, bd=bd) == "LONG"

    def test_neutral_proximity12_threshold_60(self):
        """Neutral regime + proximity_pts=12 → LONG threshold drops 65→60."""
        eng = TripleConfirmationEngine()
        # No taiex history → neutral
        bd = self._make_bd_with_proximity(12)
        assert eng._map_action(62, bd=bd) == "LONG"   # 62 >= 60
        assert eng._map_action(59, bd=bd) == "WATCH"  # 59 < 60

    def test_neutral_no_proximity_still_65(self):
        """Neutral without proximity=12 → threshold unchanged at 65."""
        eng = TripleConfirmationEngine()
        bd = self._make_bd_with_proximity(0)
        assert eng._map_action(64, bd=bd) == "WATCH"
        assert eng._map_action(65, bd=bd) == "LONG"

    def test_downtrend_proximity12_unchanged_70(self):
        """Downtrend + proximity_pts=12 → threshold stays 70 (cautious)."""
        eng = TripleConfirmationEngine()
        eng._taiex_history = self._make_taiex_history(rising=False, n=30)
        bd = self._make_bd_with_proximity(12)
        assert eng._map_action(69, bd=bd) == "WATCH"
        assert eng._map_action(70, bd=bd) == "LONG"

    def test_no_bd_backward_compatible(self):
        """_map_action(score) without bd arg still uses standard thresholds."""
        eng = TripleConfirmationEngine()
        eng._taiex_history = self._make_taiex_history(rising=True, n=30)
        assert eng._map_action(62) == "LONG"   # uptrend threshold 60
        assert eng._map_action(59) == "WATCH"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestMapActionProximityThreshold -q
```
Expected: `test_uptrend_proximity12_threshold_55` and `test_neutral_proximity12_threshold_60` FAIL (current code ignores proximity).

### Step 3b — Implement

- [ ] **Step 3: Modify `_map_action` in `triple_confirmation_engine.py`**

Find `_map_action` (around line 1554) and replace the body:

```python
    def _map_action(
        self, confidence: int, bd: _ScoreBreakdown | None = None, chip_pts: int = 0
    ) -> str:
        """Map confidence score to action label using regime-adjusted thresholds.

        When proximity_pts == 12 (stock in 92-99% zone), reduce the LONG threshold
        by 5 for uptrend and neutral regimes. Downtrend keeps the conservative 70.
        """
        taiex = getattr(self, "_taiex_history", [])
        regime = self._compute_taiex_regime(taiex)
        if regime == "uptrend":
            long_threshold = _LONG_THRESHOLD_UPTREND
        elif regime == "downtrend":
            long_threshold = _LONG_THRESHOLD_DOWNTREND
        else:
            long_threshold = _LONG_THRESHOLD_NEUTRAL

        if bd is not None and bd.proximity_pts == 12 and regime != "downtrend":
            long_threshold = max(long_threshold - 5, _WATCH_MIN + 1)

        if confidence >= long_threshold:
            return "LONG"
        if confidence >= _WATCH_MIN:
            return "WATCH"
        return "CAUTION"
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestMapActionProximityThreshold -q
```
Expected: all 6 tests PASS.

- [ ] **Step 5: Run full unit tests to confirm no regressions**

```bash
.venv/bin/pytest tests/unit/ -q --ignore=tests/unit/test_api_outcome_endpoint.py
```
Expected: same baseline pass count + all new tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/taiwan_stock_agent/domain/triple_confirmation_engine.py \
        tests/unit/test_triple_confirmation_engine_v2.py
git commit -m "feat: lower LONG threshold by 5 when proximity_pts=12 (uptrend/neutral regimes)"
```

---

## Task 4: Update CLAUDE.md phase gate

- [ ] **Step 1: Update CLAUDE.md Phase 4.22 entry**

In `CLAUDE.md`, find the Phase 4.19 entry (last Done phase) and add below it:

```markdown
| Phase 4.22 | ✅ Done | **Early Signal Scoring Fixes**：Sector rank 分級加分（top5%+10/top10%+7/top20%+5）✅ · Near-high 首日補償（NEAR_HIGH_COIL +4）✅ · Uptrend/Neutral proximity_pts=12 降門檻 5 pts ✅ · `test_persistence_bonus.py` import 修正 ✅ |
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "chore: mark Phase 4.22 early signal scoring fixes as done"
```

---

## Final Verification

- [ ] **Run complete test suite**

```bash
.venv/bin/pytest tests/unit/ -q --ignore=tests/unit/test_api_outcome_endpoint.py
```
Expected: baseline 415 + ~20 new tests, 0 new failures.

- [ ] **Confirm all 3 fixes visible in a dry run**

```bash
# Verify sector tiering in batch output:
python -c "
import sys; sys.path.insert(0, 'scripts')
from batch_plan import _apply_sector_ranks
results = [{'ticker': str(i), 'confidence': 60-i, 'halt': False, 'error': None, 'flags': []} for i in range(20)]
industry_map = {str(i): 'test' for i in range(20)}
_apply_sector_ranks(results, industry_map)
for r in results[:5]:
    print(r['ticker'], r['confidence'], r['flags'])
"
```
Expected: tickers 0,1,2,3 boosted with +10/+7/+5/+5 and SECTOR_RANK flags.
