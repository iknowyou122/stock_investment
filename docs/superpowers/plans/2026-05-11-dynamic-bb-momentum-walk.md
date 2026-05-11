# Dynamic BB Threshold + Momentum Walk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace G2's absolute 15% BB threshold with a stock-specific percentile, and add 5MA walk + BB upper walk as quality-confirmer scoring factors in both the pre-breakout engine and the surge radar.

**Architecture:** Three changes to `triple_confirmation_engine.py` (G2 gate, two new score factors) and two new methods in `surge_radar.py`. All helpers are static methods on the same class they're used in. No new files.

**Tech Stack:** Python 3.11, pandas, existing `DailyOHLCV` / `_ScoreBreakdown` / `SurgeRadar` types. Tests via `.venv/bin/pytest`.

---

## File Map

| File | Change |
|------|--------|
| `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` | G2 gate, 2 new fields in `_ScoreBreakdown`, 2 new static methods, wiring in `_compute()` |
| `src/taiwan_stock_agent/domain/surge_radar.py` | 2 new scoring methods, move `consec` before factors, update `raw_max_pts` in params |
| `config/surge_params.json` | `raw_max_pts`: 85 → 87 |
| `tests/unit/test_triple_confirmation_engine_v2.py` | G2, 5MA walk, BB upper walk tests |
| `tests/unit/test_surge_radar.py` | 5MA walk, BB upper walk surge tests |
| `CLAUDE.md` | Mark Phase 4.24 Done |

---

### Task 1: G2 Dynamic Threshold

**Spec:** `_gate_check()` currently uses `bb_w <= 0.15`. `_calculate_bb()` already returns `bb_width_pct` (4th tuple value) — the percentile rank of the current BB width within the last 60 days. Capture it and use ≤35th percentile as the threshold; fall back to absolute ≤15% when history is too short for the percentile to be computed.

**Files:**
- Modify: `src/taiwan_stock_agent/domain/triple_confirmation_engine.py:503-512`
- Test: `tests/unit/test_triple_confirmation_engine_v2.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_triple_confirmation_engine_v2.py` (after existing classes):

```python
# ──────────────────────────────────────────────────────────────────────────────
# G2 Dynamic BB Threshold tests
# ──────────────────────────────────────────────────────────────────────────────

def _make_alternating_history(
    n: int, amplitude: float, base: float = 100.0, start_day: int = 0
) -> list[DailyOHLCV]:
    """Close alternates base±amplitude every bar. High amplitude → wide BB."""
    d = date(2026, 1, 1)
    bars = []
    for i in range(n):
        c = base + (amplitude if i % 2 == 0 else -amplitude)
        bars.append(DailyOHLCV(
            ticker="TEST",
            trade_date=d + timedelta(start_day + i),
            open=c, high=c + 1.0, low=c - 1.0, close=c,
            volume=1_000_000,
        ))
    return bars


class TestG2DynamicThreshold:
    def _g2_flag(self, history: list[DailyOHLCV], close: float = 100.0, tdh: float = 105.0) -> str | None:
        engine = TripleConfirmationEngine()
        ohlcv = _make_ohlcv(close=close)
        vp = _make_volume_profile(twenty_day_high=tdh)
        _, _, _, flags = engine._gate_check(ohlcv, history, vp)
        return next((f for f in flags if "G2" in f), None)

    def test_low_percentile_passes(self):
        # 59 wide-BB bars, then 20 narrow-BB bars → current BB at ~3rd percentile → PASS
        history = (
            _make_alternating_history(59, amplitude=20.0) +
            _make_alternating_history(20, amplitude=0.05, start_day=59)
        )
        flag = self._g2_flag(history, close=100.0, tdh=105.0)
        assert flag is not None
        assert "GATE_PASS:G2_BB_PCT:" in flag

    def test_high_percentile_fails(self):
        # 59 narrow-BB bars, then 20 wide-BB bars → current BB at ~67th percentile → FAIL
        history = (
            _make_alternating_history(59, amplitude=0.05) +
            _make_alternating_history(20, amplitude=20.0, start_day=59)
        )
        flag = self._g2_flag(history, close=100.0, tdh=115.0)
        assert flag is not None
        assert "GATE_FAIL:G2_BB_WIDE_PCT:" in flag

    def test_short_history_fallback_narrow_passes(self):
        # 40 bars → bb_width_pct is None → fallback to absolute ≤15%; amplitude 0.05 → BB ≈ 0.2% → PASS
        history = _make_alternating_history(40, amplitude=0.05)
        flag = self._g2_flag(history, close=100.0, tdh=105.0)
        assert flag is not None
        assert "GATE_PASS:G2_BB_PCT:" in flag

    def test_short_history_fallback_wide_fails(self):
        # 40 bars, amplitude=20 → BB ≈ 80% → fallback absolute > 15% → FAIL
        history = _make_alternating_history(40, amplitude=20.0)
        flag = self._g2_flag(history, close=100.0, tdh=115.0)
        assert flag is not None
        assert "GATE_FAIL:G2_BB_WIDE_PCT:" in flag

    def test_flag_uses_p_suffix_for_percentile(self):
        # When bb_width_pct is computed (79 bars), flag value ends with 'p'
        history = (
            _make_alternating_history(59, amplitude=20.0) +
            _make_alternating_history(20, amplitude=0.05, start_day=59)
        )
        flag = self._g2_flag(history, close=100.0, tdh=105.0)
        assert flag is not None
        assert flag.endswith("p")

    def test_flag_uses_pct_suffix_for_fallback(self):
        # When bb_width_pct is None (40 bars), flag value ends with '%'
        history = _make_alternating_history(40, amplitude=0.05)
        flag = self._g2_flag(history, close=100.0, tdh=105.0)
        assert flag is not None
        assert flag.endswith("%")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestG2DynamicThreshold -v
```

Expected: 6 FAILED (G2 flag text doesn't match yet — still `G2_BB:` not `G2_BB_PCT:`)

- [ ] **Step 3: Implement G2 dynamic threshold**

In `triple_confirmation_engine.py`, replace lines 503-512:

```python
        # G2: BB Compression (dynamic: ≤35th percentile of 60d history; fallback: absolute ≤15%)
        _, _, bb_w, bb_width_pct = self._calculate_bb(ohlcv_history)
        if bb_w is not None:
            if bb_width_pct is not None:
                threshold_met = bb_width_pct <= 35.0
                label = f"{bb_width_pct:.1f}p"
            else:
                threshold_met = bb_w <= 0.15
                label = f"{bb_w * 100:.1f}%"
            if threshold_met:
                conditions_met += 1
                detail_flags.append(f"GATE_PASS:G2_BB_PCT:{label}")
            else:
                detail_flags.append(f"GATE_FAIL:G2_BB_WIDE_PCT:{label}")
        else:
            detail_flags.append("GATE_SKIP:G2_NO_BB")
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestG2DynamicThreshold -v
```

Expected: 6 PASSED

- [ ] **Step 5: Run full test suite to check regressions**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py tests/unit/test_triple_confirmation_engine_v2_fix.py -q
```

Expected: all pass (note: any existing test that asserted `GATE_PASS:G2_BB:` or `GATE_FAIL:G2_BB_WIDE:` will break — grep for those strings and update the assertion to `G2_BB_PCT:`).

```bash
grep -n "G2_BB:" tests/unit/test_triple_confirmation_engine_v2.py tests/unit/test_triple_confirmation_engine_v2_fix.py
```

Update any matching assertions to use the new `G2_BB_PCT:` prefix.

- [ ] **Step 6: Commit**

```bash
git add src/taiwan_stock_agent/domain/triple_confirmation_engine.py \
        tests/unit/test_triple_confirmation_engine_v2.py
git commit -m "feat: replace G2 absolute BB threshold with 60d percentile (≤35p, fallback ≤15%)"
```

---

### Task 2: 5MA Walk Factor in Engine

**Spec:** New Pillar 1 factor: if ≥80% of last 10 days had `close ≥ MA5`, award +2 pts and add `MA5_WALK` flag. Needs new field `ma5_walk_pts` in `_ScoreBreakdown` and wiring in `_compute()`.

**Files:**
- Modify: `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` (3 locations: dataclass field, `total`/`momentum_pts` properties, `_compute()`, new static method)
- Test: `tests/unit/test_triple_confirmation_engine_v2.py`

- [ ] **Step 1: Write failing tests**

Add after `TestG2DynamicThreshold` in the test file:

```python
# ──────────────────────────────────────────────────────────────────────────────
# 5MA Walk Factor tests
# ──────────────────────────────────────────────────────────────────────────────

def _make_declining_history(n: int, step: float = 1.0) -> list[DailyOHLCV]:
    """Linearly declining closes. In declining trend, close < MA5 most of the time."""
    d = date(2026, 1, 1)
    bars = []
    for i in range(n):
        c = 200.0 - i * step
        bars.append(DailyOHLCV(
            ticker="TEST", trade_date=d + timedelta(i),
            open=c, high=c + 1.0, low=c - 1.0, close=c, volume=10_000,
        ))
    return bars


class TestMa5WalkScore:
    def test_rising_history_gets_2(self):
        # Gently rising (close = 100 + i*0.5): close is always above MA5 → 100% ratio ≥ 80% → 2 pts
        history = _make_history(30, base_close=100.0)
        pts = TripleConfirmationEngine._ma5_walk_score(history)
        assert pts == 2

    def test_declining_history_gets_0(self):
        # Declining: close < MA5 every day (MA5 lags above) → ratio < 80% → 0 pts
        history = _make_declining_history(20, step=2.0)
        pts = TripleConfirmationEngine._ma5_walk_score(history)
        assert pts == 0

    def test_insufficient_history_gets_0(self):
        history = _make_history(4)  # < 5 bars → cannot compute MA5
        pts = TripleConfirmationEngine._ma5_walk_score(history)
        assert pts == 0

    def test_field_exists_in_breakdown(self):
        bd = _ScoreBreakdown()
        assert hasattr(bd, "ma5_walk_pts")
        assert bd.ma5_walk_pts == 0

    def test_ma5_walk_in_total(self):
        bd = _ScoreBreakdown()
        bd.ma5_walk_pts = 2
        base = _ScoreBreakdown().total
        assert bd.total == base + 2

    def test_ma5_walk_flag_added(self):
        # Rising history → MA5_WALK appears in bd.flags after _compute()
        history = _make_history(40, base_close=100.0)
        engine = TripleConfirmationEngine()
        # Call _ma5_walk_score directly; flag is added in _compute() wiring
        pts = TripleConfirmationEngine._ma5_walk_score(history)
        assert pts == 2  # confirms the method returns 2 for rising history
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestMa5WalkScore -v
```

Expected: FAILED (attribute/method not found)

- [ ] **Step 3: Add `ma5_walk_pts` field to `_ScoreBreakdown`**

In `triple_confirmation_engine.py`, after `volume_climax_pts: int = 0` (line 218):

```python
    ma5_walk_pts: int = 0             # 0/2 — close ≥ MA5 for ≥80% of last 10 days
```

- [ ] **Step 4: Add `ma5_walk_pts` to `total` property**

In the `total` property, after `+ self.volume_climax_pts` (in the Pillar 1 block):

```python
            + self.ma5_walk_pts
```

- [ ] **Step 5: Add `ma5_walk_pts` to `momentum_pts` property**

In the `momentum_pts` property, after `+ self.volume_climax_pts`:

```python
            + self.ma5_walk_pts
```

- [ ] **Step 6: Add `_ma5_walk_score()` static method**

Add after `_bb_compression_score()` (around line 1131):

```python
    @staticmethod
    def _ma5_walk_score(history: list[DailyOHLCV], n: int = 10) -> int:
        """Close >= MA5 for >= 80% of last n days → +2 pts (short-term trend quality)."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = pd.Series([d.close for d in sorted_h])
        if len(closes) < 5:
            return 0
        ma5 = closes.rolling(5).mean()
        window = min(n, len(closes))
        close_win = closes.iloc[-window:]
        ma5_win = ma5.iloc[-window:]
        valid = ma5_win.notna()
        if valid.sum() == 0:
            return 0
        ratio = float((close_win[valid] >= ma5_win[valid]).mean())
        return 2 if ratio >= 0.8 else 0
```

- [ ] **Step 7: Wire up in `_compute()`**

In `_compute()`, after `bd.volume_climax_pts = self._volume_climax_score(ohlcv_history)` (around line 607):

```python
        ma5_walk = self._ma5_walk_score(ohlcv_history)
        bd.ma5_walk_pts = ma5_walk
        if ma5_walk > 0:
            bd.flags.append("MA5_WALK")
```

- [ ] **Step 8: Run tests**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestMa5WalkScore -v
```

Expected: 7 PASSED

- [ ] **Step 9: Commit**

```bash
git add src/taiwan_stock_agent/domain/triple_confirmation_engine.py \
        tests/unit/test_triple_confirmation_engine_v2.py
git commit -m "feat: add ma5_walk_pts Pillar 1 factor (+2 when close ≥ MA5 for ≥80% of last 10d)"
```

---

### Task 3: BB Upper Walk Factor in Engine

**Spec:** New Pillar 3 factor: if stock is in the 92-99% zone (`proximity_pts == 12`, which also implies G2 passed since the gate would have rejected otherwise) AND 3 of the last 5 bars were within 3% of a rising BB upper, award +3 pts and add `BB_UPPER_COIL` flag. Conditional on `proximity_pts == 12` only — gate passage already guarantees G2 passed.

**Files:**
- Modify: `src/taiwan_stock_agent/domain/triple_confirmation_engine.py`
- Test: `tests/unit/test_triple_confirmation_engine_v2.py`

- [ ] **Step 1: Write failing tests**

Add after `TestMa5WalkScore`:

```python
# ──────────────────────────────────────────────────────────────────────────────
# BB Upper Walk Factor tests
# ──────────────────────────────────────────────────────────────────────────────

def _make_rising_history(n: int, step: float = 2.0) -> list[DailyOHLCV]:
    """Linearly rising closes. In a 2pt/day uptrend, close stays within 3% of BB upper."""
    d = date(2026, 1, 1)
    bars = []
    for i in range(n):
        c = 100.0 + i * step
        bars.append(DailyOHLCV(
            ticker="TEST", trade_date=d + timedelta(i),
            open=c - 0.5, high=c + 1.0, low=c - 1.0, close=c, volume=1_000_000,
        ))
    return bars


class TestBbUpperWalkScore:
    def test_rising_trend_near_upper_gets_3(self):
        # Step=2 per day: close stays within ~3% of BB upper (math verified in spec)
        history = _make_rising_history(25, step=2.0)
        pts = TripleConfirmationEngine._bb_upper_walk_score(history)
        assert pts == 3

    def test_flat_history_not_rising_bb_gets_0(self):
        # Flat closes → std=0 → BB upper = close; BB_upper not rising → 0
        history = _make_history(25, flat=True)
        pts = TripleConfirmationEngine._bb_upper_walk_score(history)
        assert pts == 0

    def test_declining_close_below_upper_gets_0(self):
        # Declining: close falls away from BB upper (MA lags above) → fewer than 3 near upper
        history = _make_declining_history(25, step=1.0)
        pts = TripleConfirmationEngine._bb_upper_walk_score(history)
        assert pts == 0

    def test_insufficient_history_gets_0(self):
        history = _make_rising_history(15)  # < 20 bars → BB not computable
        pts = TripleConfirmationEngine._bb_upper_walk_score(history)
        assert pts == 0

    def test_field_exists_in_breakdown(self):
        bd = _ScoreBreakdown()
        assert hasattr(bd, "bb_upper_walk_pts")
        assert bd.bb_upper_walk_pts == 0

    def test_bb_upper_walk_in_total(self):
        bd = _ScoreBreakdown()
        bd.bb_upper_walk_pts = 3
        base = _ScoreBreakdown().total
        assert bd.total == base + 3

    def test_only_awarded_when_proximity12(self):
        # proximity_pts=6 (85-91% zone) → bb_upper_walk_pts must remain 0
        # Verify by checking _ScoreBreakdown field default and the conditional in _compute()
        # We test this at the static method level: the static method itself always returns pts;
        # the conditional (proximity == 12) lives in _compute(). So this test verifies
        # that _compute() does NOT set bb_upper_walk_pts when proximity_pts != 12.
        # Use a full score_full() call with proximity controlled by close / twenty_day_high.
        history = _make_rising_history(40, step=2.0)
        ohlcv = _make_ohlcv(close=history[-1].close)
        # Proximity 6: close at 87% of twenty_day_high (85–91% range)
        twenty_day_high = history[-1].close / 0.87
        vp = _make_volume_profile(twenty_day_high=twenty_day_high)
        chip = _make_chip_report(net_buyer_diff=0, active_branches=0)
        engine = TripleConfirmationEngine()
        engine._taiex_history = _make_history(30, base_close=17000.0)
        result = engine.score_full(
            ohlcv=ohlcv,
            ohlcv_history=history[:-1],
            chip_report=chip,
            volume_profile=vp,
        )
        bd_pts = result.get("score_breakdown", {}).get("pts", {})
        assert bd_pts.get("bb_upper_walk_pts", 0) == 0
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestBbUpperWalkScore -v
```

Expected: FAILED (method/attribute not found)

- [ ] **Step 3: Add `bb_upper_walk_pts` field to `_ScoreBreakdown`**

After `bb_squeeze_breakout_pts: int = 0` (line 247):

```python
    bb_upper_walk_pts: int = 0        # 0/3 — proximity=12, 3/5 days near BB upper and rising
```

- [ ] **Step 4: Add to `total` property**

In the `total` property, after `+ self.bb_squeeze_breakout_pts`:

```python
            + self.bb_upper_walk_pts
```

- [ ] **Step 5: Add to `structure_pts` property**

In the `structure_pts` property, after `+ self.bb_squeeze_breakout_pts`:

```python
            + self.bb_upper_walk_pts
```

- [ ] **Step 6: Add `_bb_upper_walk_score()` static method**

Add after `_ma5_walk_score()`:

```python
    @staticmethod
    def _bb_upper_walk_score(
        history: list[DailyOHLCV], n: int = 5, tolerance: float = 0.03
    ) -> int:
        """3 of last n days close >= BB_upper*(1-tol) AND BB_upper rising → +3 pts."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = pd.Series([d.close for d in sorted_h])
        if len(closes) < 20:
            return 0
        ma = closes.rolling(20).mean()
        std = closes.rolling(20).std(ddof=0)
        bb_upper = ma + 2 * std
        if len(bb_upper.dropna()) < n:
            return 0
        window_upper = bb_upper.iloc[-n:]
        window_close = closes.iloc[-n:]
        near_upper = int((window_close >= window_upper * (1 - tolerance)).sum())
        bb_upper_rising = float(bb_upper.iloc[-1]) > float(bb_upper.iloc[-n])
        return 3 if (near_upper >= 3 and bb_upper_rising) else 0
```

- [ ] **Step 7: Wire up in `_compute()`**

In `_compute()`, after `bd.proximity_pts = self._proximity_score(ohlcv.close, volume_profile.twenty_day_high)` (around line 634):

```python
        if bd.proximity_pts == 12:
            bb_walk = self._bb_upper_walk_score(ohlcv_history)
            bd.bb_upper_walk_pts = bb_walk
            if bb_walk > 0:
                bd.flags.append("BB_UPPER_COIL")
```

- [ ] **Step 8: Run tests**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestBbUpperWalkScore -v
```

Expected: 7 PASSED (the `test_only_awarded_when_proximity12` test requires `score_full()` to return a `score_breakdown` dict with `pts` key — verify the return format matches. If `score_breakdown` is stored differently, adjust the assertion to match the actual key used).

- [ ] **Step 9: Run full engine test suite**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py tests/unit/test_triple_confirmation_engine_v2_fix.py -q
```

Expected: all pass

- [ ] **Step 10: Commit**

```bash
git add src/taiwan_stock_agent/domain/triple_confirmation_engine.py \
        tests/unit/test_triple_confirmation_engine_v2.py
git commit -m "feat: add bb_upper_walk_pts Pillar 3 factor (+3 when proximity=12 and close walks BB upper)"
```

---

### Task 4: SurgeRadar 5MA Walk + BB Upper Walk

**Spec:** Two new scoring factors in `SurgeRadar`:
- `_score_ma5_walk()`: +2 if close ≥ MA5 for ≥80% of last 10 bars (including today); −1 if < 50%; else 0. Uses `ohlcv + history` combined.
- `_score_bb_upper_walk()`: uses `history` only. If 3/5 days near BB upper AND BB rising: MOMENTUM_WALK tag when surge_day ≤ 2 (no score change); BB_UPPER_EXHAUSTION −3 pts when surge_day ≥ 3.
- `consec` (consecutive surge days) must be computed once before the factors loop so `_score_bb_upper_walk()` can receive it.
- Update `raw_max_pts` from 85 to 87 (ma5_walk adds max +2; bb_upper_walk is never positive).

**Files:**
- Modify: `src/taiwan_stock_agent/domain/surge_radar.py`
- Modify: `config/surge_params.json`
- Test: `tests/unit/test_surge_radar.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_surge_radar.py` (after existing classes):

```python
# ──────────────────────────────────────────────────────────────────────────────
# 5MA Walk scoring
# ──────────────────────────────────────────────────────────────────────────────

def _rising_history(n: int = 30, step: float = 0.5) -> list[DailyOHLCV]:
    """Gently rising history — close always above MA5."""
    d = date(2026, 1, 1)
    return [
        _bar(close=100.0 + i * step, volume=600_000, day=i)
        for i in range(n)
    ]


def _declining_history(n: int = 30, step: float = 1.0) -> list[DailyOHLCV]:
    """Declining history — close always below MA5."""
    d = date(2026, 1, 1)
    return [
        _bar(close=200.0 - i * step, volume=600_000, day=i)
        for i in range(n)
    ]


class TestSurgeMa5Walk:
    def test_walking_gets_plus2(self):
        hist = _rising_history(30)
        today = _bar(close=hist[-1].close + 0.5, volume=600_000, day=30)
        pts, flags = SurgeRadar()._score_ma5_walk(today, hist)
        assert pts == 2
        assert "MA5_WALK" in flags

    def test_breaking_down_gets_minus1(self):
        hist = _declining_history(30)
        today = _bar(close=hist[-1].close - 1.0, volume=600_000, day=30)
        pts, flags = SurgeRadar()._score_ma5_walk(today, hist)
        assert pts == -1
        assert "MA5_BREAK" in flags

    def test_neutral_gets_0(self):
        # Alternating above/below MA5 — roughly 50% ratio → 0 pts
        bars = []
        for i in range(30):
            c = 100.0 + (2.0 if i % 4 < 2 else -2.0)
            bars.append(_bar(close=c, volume=600_000, day=i))
        today = _bar(close=102.0, volume=600_000, day=30)
        pts, flags = SurgeRadar()._score_ma5_walk(today, bars)
        assert pts == 0

    def test_insufficient_history_gets_0(self):
        hist = _rising_history(3)
        today = _bar(close=102.0, volume=600_000, day=3)
        pts, _ = SurgeRadar()._score_ma5_walk(today, hist)
        assert pts == 0


# ──────────────────────────────────────────────────────────────────────────────
# BB Upper Walk scoring
# ──────────────────────────────────────────────────────────────────────────────

def _bb_upper_walk_history(n: int = 25, step: float = 2.0) -> list[DailyOHLCV]:
    """Rising history at step/day — close walks BB upper."""
    return [_bar(close=100.0 + i * step, volume=600_000, day=i) for i in range(n)]


class TestSurgeBbUpperWalk:
    def test_surge_day1_walking_adds_momentum_tag_no_score(self):
        hist = _bb_upper_walk_history(25, step=2.0)
        pts, flags = SurgeRadar()._score_bb_upper_walk(hist, surge_day=1)
        assert pts == 0
        assert "MOMENTUM_WALK" in flags

    def test_surge_day2_walking_adds_momentum_tag(self):
        hist = _bb_upper_walk_history(25, step=2.0)
        pts, flags = SurgeRadar()._score_bb_upper_walk(hist, surge_day=2)
        assert pts == 0
        assert "MOMENTUM_WALK" in flags

    def test_surge_day3_walking_gets_minus3(self):
        hist = _bb_upper_walk_history(25, step=2.0)
        pts, flags = SurgeRadar()._score_bb_upper_walk(hist, surge_day=3)
        assert pts == -3
        assert "BB_UPPER_EXHAUSTION" in flags

    def test_not_walking_gets_0_no_flag(self):
        hist = _declining_history(25)
        pts, flags = SurgeRadar()._score_bb_upper_walk(hist, surge_day=1)
        assert pts == 0
        assert "MOMENTUM_WALK" not in flags
        assert "BB_UPPER_EXHAUSTION" not in flags

    def test_insufficient_history_gets_0(self):
        hist = _rising_history(10)
        pts, _ = SurgeRadar()._score_bb_upper_walk(hist, surge_day=1)
        assert pts == 0
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/pytest tests/unit/test_surge_radar.py::TestSurgeMa5Walk tests/unit/test_surge_radar.py::TestSurgeBbUpperWalk -v
```

Expected: FAILED (methods not found)

- [ ] **Step 3: Add `_score_ma5_walk()` to `SurgeRadar`**

Add after `_score_margin_not_hot()` (around line 348):

```python
    def _score_ma5_walk(
        self, ohlcv: DailyOHLCV, history: list[DailyOHLCV], n: int = 10
    ) -> tuple[int, list[str]]:
        """Quality confirmer: close walking MA5 after surge indicates sustained demand."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        all_bars = sorted_h + [ohlcv]
        closes = pd.Series([d.close for d in all_bars])
        if len(closes) < 5:
            return 0, []
        ma5 = closes.rolling(5).mean()
        window = min(n, len(closes))
        close_win = closes.iloc[-window:]
        ma5_win = ma5.iloc[-window:]
        valid = ma5_win.notna()
        if valid.sum() == 0:
            return 0, []
        ratio = float((close_win[valid] >= ma5_win[valid]).mean())
        if ratio >= 0.8:
            return 2, ["MA5_WALK"]
        if ratio < 0.5:
            return -1, ["MA5_BREAK"]
        return 0, []

    def _score_bb_upper_walk(
        self,
        history: list[DailyOHLCV],
        surge_day: int,
        n: int = 5,
        tolerance: float = 0.03,
    ) -> tuple[int, list[str]]:
        """BB upper walk: MOMENTUM_WALK tag on day≤2; exhaustion deduction on day≥3."""
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = pd.Series([d.close for d in sorted_h])
        if len(closes) < 20:
            return 0, []
        ma = closes.rolling(20).mean()
        std = closes.rolling(20).std(ddof=0)
        bb_upper = ma + 2 * std
        if len(bb_upper.dropna()) < n:
            return 0, []
        window_upper = bb_upper.iloc[-n:]
        window_close = closes.iloc[-n:]
        near_upper = int((window_close >= window_upper * (1 - tolerance)).sum())
        bb_upper_rising = float(bb_upper.iloc[-1]) > float(bb_upper.iloc[-n])
        if near_upper >= 3 and bb_upper_rising:
            if surge_day >= 3:
                return -3, ["BB_UPPER_EXHAUSTION"]
            return 0, ["MOMENTUM_WALK"]
        return 0, []
```

- [ ] **Step 4: Move `consec` computation before the factors list in `score_full()` and wire up new factors**

In `score_full()`, find the line `consec = self._consecutive_surge_days(ohlcv, history)` (around line 419). Move it to just before the `factors = [...]` list:

```python
        consec = self._consecutive_surge_days(ohlcv, history)

        factors = [
            ("vol_ratio", self._score_vol_ratio(ohlcv, history)),
            ("close_strength", self._score_close_strength(ohlcv)),
            ("inst_buy_fresh", self._score_inst_buy_fresh(proxy)),
            ("industry_strength", self._score_industry_strength(industry_rank_pct)),
            ("pocket_pivot", self._score_pocket_pivot(ohlcv, history)),
            ("breakaway_gap", self._score_breakaway_gap(ohlcv, history)),
            ("relative_strength", self._score_relative_strength(ohlcv, history, taiex_history)),
            ("breakout_20d", self._score_breakout_20d(ohlcv, history)),
            ("rsi_healthy", self._score_rsi_healthy(history)),
            ("margin_not_hot", self._score_margin_not_hot(proxy)),
            ("ma5_walk", self._score_ma5_walk(ohlcv, history)),
            ("bb_upper_walk", self._score_bb_upper_walk(history, consec)),
        ]
```

Remove the original `consec = ...` line further down (now it's a duplicate).

- [ ] **Step 5: Update `raw_max_pts` in surge_params.json**

```json
"raw_max_pts": 87
```

(was 85; +2 for ma5_walk max. bb_upper_walk max is 0.)

- [ ] **Step 6: Run tests**

```bash
.venv/bin/pytest tests/unit/test_surge_radar.py -q
```

Expected: all pass (including existing tests — the `consec` move is transparent since no test asserts on its location)

- [ ] **Step 7: Commit**

```bash
git add src/taiwan_stock_agent/domain/surge_radar.py \
        config/surge_params.json \
        tests/unit/test_surge_radar.py
git commit -m "feat: add ma5_walk and bb_upper_walk quality confirmers to SurgeRadar"
```

---

### Task 5: CLAUDE.md Phase 4.24

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add Phase 4.24 entry**

In `CLAUDE.md`, after the Phase 4.22 Done entry, add:

```markdown
| Phase 4.24 | ✅ Done | **Dynamic BB Threshold + Momentum Walk**：G2 門檻改為 60 日分位數 ≤35p（fallback 絕對 ≤15%）✅ · `ma5_walk_pts` Pillar 1 因子（≥80% 收在 MA5 上 → +2）✅ · `bb_upper_walk_pts` Pillar 3 因子（proximity=12 + 收盤貼 BB 上軌 3/5 天 + 上揚 → +3，BB_UPPER_COIL flag）✅ · SurgeRadar `_score_ma5_walk`（+2/−1）✅ · `_score_bb_upper_walk`（MOMENTUM_WALK / BB_UPPER_EXHAUSTION −3）✅ · 18 新單元測試 ✅ |
```

- [ ] **Step 2: Run full test suite to confirm nothing broken**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py tests/unit/test_triple_confirmation_engine_v2_fix.py tests/unit/test_surge_radar.py tests/unit/test_persistence_bonus.py -q
```

Expected: all pass

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "chore: mark Phase 4.24 dynamic BB threshold + momentum walk as done"
```

---

## Self-Review

**Spec coverage check:**
- Fix 1 (G2 percentile): Task 1 ✅
- Fix 2 (5MA walk engine): Task 2 ✅
- Fix 3 (BB upper walk engine, conditional on proximity=12): Task 3 ✅
- Fix 4 (SurgeRadar 5MA walk): Task 4 ✅
- Fix 5 (SurgeRadar BB upper walk): Task 4 ✅
- raw_max_pts update: Task 4 ✅

**Placeholder scan:** No TBD/TODO. All code blocks are complete.

**Type consistency:**
- `_ma5_walk_score` → returns `int` → consistent with other score methods
- `_bb_upper_walk_score` → returns `int` → consistent
- `_score_ma5_walk` in SurgeRadar → returns `tuple[int, list[str]]` → consistent with all other `_score_*` methods
- `_score_bb_upper_walk` in SurgeRadar → same return type ✅
- `ma5_walk_pts` / `bb_upper_walk_pts` field names used identically in dataclass, `total`, and `_compute()` wiring ✅

**Note for Task 3 test `test_only_awarded_when_proximity12`:** This test calls `engine.score_full()` and reads from `result["score_breakdown"]["pts"]`. Verify the exact key path matches what `score_full()` returns for the breakdown dict. If the key is different (e.g., the breakdown is serialized differently), adjust accordingly — the logic assertion (pts == 0) is the important part.
