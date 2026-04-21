# Pre-Breakout Signal Engine Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the signal system from finding confirmed-breakout stocks into finding stocks 1-3 days before breakout, with industry-grouped output and a bot dashboard sentiment panel.

**Architecture:** 7 sequential tasks across 4 subsystems: (1) Triple Confirmation Engine Gate + Pillar rewrites, (2) AccumulationEngine G1 fix, (3) batch_plan.py industry-grouped display, (4) new SentimentClient + MarketSentiment domain + bot widget. Action labels LONG/WATCH/CAUTION are kept in the engine and DB — only the display layer relabels LONG as "準備突破".

**Tech Stack:** Python, pandas, existing TWSE/TPEx public APIs, Yahoo Finance RSS (public), Rich terminal UI

**Spec:** `docs/superpowers/specs/2026-04-20-pre-breakout-signal-redesign.md`

---

## File Structure

| File | Action | Summary |
|---|---|---|
| `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` | Modify | Gate rewrite, Pillar 1 new factors, Pillar 3 full rewrite, threshold update |
| `tests/unit/test_triple_confirmation_engine_v2.py` | Modify | New gate + pillar tests; old breakout tests removed |
| `src/taiwan_stock_agent/domain/accumulation_engine.py` | Modify | G1: close within MA20 ± 8% AND MA20 slope flat/rising |
| `tests/unit/test_accumulation_engine.py` | Modify | New G1 test cases |
| `scripts/batch_plan.py` | Modify | `_print_by_industry()` replaces `_print_table()` for terminal display; CSV unchanged |
| `src/taiwan_stock_agent/infrastructure/sentiment_client.py` | Create | `BreadthData`, `fetch_breadth()`, `fetch_news_headlines()` |
| `src/taiwan_stock_agent/domain/market_sentiment.py` | Create | `MarketSentiment`, `compute_sentiment()` |
| `tests/unit/test_market_sentiment.py` | Create | Sentiment label logic tests |
| `scripts/bot.py` | Modify | Add sentiment to `_MARKET_CACHE`, `_refresh_market_loop()`, `_render_status_panel()` |

---

## Key Codebase Context

**`triple_confirmation_engine.py`** (1,949 lines):
- `_gate_check()` at line 458: currently 2-of-5 conditions → rewrite to 4 hard conditions
- `_compute()` at line 551: calls `_gate_check()`, then computes pillars
- `_ScoreBreakdown` dataclass at line 197: add new fields, remove breakout fields
- `_build_signal()` at line 1473: maps score → LONG/WATCH/CAUTION (unchanged label)
- `_map_action()` at line 1454: uses `_LONG_THRESHOLD_*` constants (update values)
- `_calculate_bb()` at line 1860: returns `(upper, lower, width_raw, width_pct)` — `width_raw` is `(upper-lower)/mid` as decimal (0.10 = 10%)
- `volume_profile.twenty_day_high`: max HIGH of last 20 bars (already available)

**`test_triple_confirmation_engine_v2.py`** (107 tests):
- Tests check LONG at score ≥ 68 (neutral), ≥ 63 (uptrend), ≥ 73 (downtrend)
- Gate tests check the 2-of-5 combination logic — all will need updating

**`accumulation_engine.py`** (line 36-47): G1 is `MA20 > MA60` — replace with new condition.

**`batch_plan.py`** `_print_table()` at line 661: replace with `_print_by_industry()`. `_apply_sector_ranks()` at line 317 already computes per-industry counts. `industry_map` is already passed into `run_batch()`.

**`_MARKET_CACHE`** in `bot.py` at line 103: dict with keys `global/sectors/watchlist/updated_at`. Add `"sentiment"` key.

---

## Task 1: Engine Gate Rewrite

**Files:**
- Modify: `src/taiwan_stock_agent/domain/triple_confirmation_engine.py`
- Modify: `tests/unit/test_triple_confirmation_engine_v2.py`

The current `_gate_check()` (2-of-5) is replaced with 4 hard conditions: G1 (price zone), G2 (BB width), G3 (liquidity), G4 (regime). Note: liquidity is ALREADY a separate pre-gate in `_compute()` at line 562 (the turnover check). Keep that. Add BB width as G2 inside `_gate_check()`.

- [ ] **Step 1: Write failing tests for new gate behavior**

```python
# In tests/unit/test_triple_confirmation_engine_v2.py
# Add a new test class or replace the gate combination tests

def _make_ohlcv_near_high(
    close: float = 97.0,      # 97% of 20d_high=100 → in zone
    volume: int = 5_000,      # low volume (dryup)
) -> DailyOHLCV:
    return DailyOHLCV(ticker="TEST", trade_date=date(2025, 2, 1),
                      open=96.0, high=97.5, low=95.5, close=close, volume=volume)

def _make_history_flat_bb(n: int = 30) -> list[DailyOHLCV]:
    """30 days of flat close → tight BB."""
    result = []
    d = date(2025, 1, 2)
    for i in range(n):
        result.append(DailyOHLCV(
            ticker="TEST", trade_date=d + timedelta(days=i),
            open=100.0, high=100.5, low=99.5, close=100.0,
            volume=5_000,
        ))
    return result

class TestNewGate:
    def test_gate_passes_price_in_zone_with_tight_bb(self):
        """Stock at 97% of 20d_high with tight BB → should pass gate."""
        eng = TripleConfirmationEngine()
        eng._market = "TSE"
        hist = _make_history_flat_bb(30)
        ohlcv = _make_ohlcv_near_high(close=97.0)
        # twenty_day_high derived from history highs = 100.5
        vp = VolumeProfile(poc_proxy=97.0, twenty_day_high=100.5, sixty_day_high=105.0)
        chip = _make_zero_chip()
        # Use score() and check it's not CAUTION with NO_SETUP
        signal = eng.score(ohlcv, hist, chip, vp)
        assert "NO_SETUP" not in signal.data_quality_flags

    def test_gate_fails_price_already_broke_out(self):
        """Stock at 100% of 20d_high → already broke out, gate fails."""
        eng = TripleConfirmationEngine()
        eng._market = "TSE"
        hist = _make_history_flat_bb(30)
        ohlcv = _make_ohlcv_near_high(close=100.5)  # above 20d_high × 0.99
        vp = VolumeProfile(poc_proxy=100.5, twenty_day_high=100.5, sixty_day_high=105.0)
        chip = _make_zero_chip()
        signal = eng.score(ohlcv, hist, chip, vp)
        assert "NO_SETUP" in signal.data_quality_flags

    def test_gate_fails_price_too_far_below(self):
        """Stock at 80% of 20d_high → too far, gate fails."""
        eng = TripleConfirmationEngine()
        eng._market = "TSE"
        hist = _make_history_flat_bb(30)
        ohlcv = _make_ohlcv_near_high(close=80.0)
        vp = VolumeProfile(poc_proxy=80.0, twenty_day_high=100.5, sixty_day_high=105.0)
        chip = _make_zero_chip()
        signal = eng.score(ohlcv, hist, chip, vp)
        assert "NO_SETUP" in signal.data_quality_flags

    def test_gate_fails_bb_too_wide(self):
        """Stock with BB width > 15% → gate fails."""
        eng = TripleConfirmationEngine()
        eng._market = "TSE"
        # Wide BB: volatile history
        hist = []
        d = date(2025, 1, 2)
        for i in range(30):
            c = 100.0 + (i % 2) * 20  # alternates 100/120 → very wide BB
            hist.append(DailyOHLCV(ticker="TEST", trade_date=d + timedelta(days=i),
                                   open=c-1, high=c+2, low=c-2, close=c, volume=5_000))
        ohlcv = _make_ohlcv_near_high(close=109.0)
        vp = VolumeProfile(poc_proxy=109.0, twenty_day_high=122.0, sixty_day_high=125.0)
        chip = _make_zero_chip()
        signal = eng.score(ohlcv, hist, chip, vp)
        assert "NO_SETUP" in signal.data_quality_flags
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/07683.howard.huang/Documents/code/stock_investment
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestNewGate -v
```

Expected: FAIL with "assert NO_SETUP not in ..." or similar.

- [ ] **Step 3: Rewrite `_gate_check()` in `triple_confirmation_engine.py`**

Replace the entire `_gate_check()` method (lines 458–545). New signature (same external interface, different logic):

```python
def _gate_check(
    self,
    ohlcv: DailyOHLCV,
    ohlcv_history: list[DailyOHLCV],
    volume_profile: VolumeProfile,
    twse_proxy: TWSEChipProxy | None = None,
) -> tuple[bool, int, int, list[str]]:
    """4 hard-gate conditions for pre-breakout setup detection.
    
    Returns (passes, conditions_available=4, conditions_met, detail_flags).
    All 4 conditions must pass; any failure returns (False, ...).
    """
    detail_flags: list[str] = []
    
    # G4: Market regime (blocking override — check first)
    regime = self._compute_taiex_regime(getattr(self, "_taiex_history", []))
    if regime == "downtrend":
        detail_flags.append("GATE_FAIL:G4_DOWNTREND")
        return False, 4, 0, detail_flags
    detail_flags.append("GATE_PASS:G4_REGIME")

    # G1: Price in accumulation zone: 20d_high × 0.85 ≤ close < 20d_high × 0.99
    twenty_high = volume_profile.twenty_day_high
    if twenty_high <= 0:
        detail_flags.append("GATE_SKIP:G1_NO_HIGH_DATA")
        return False, 4, 0, detail_flags
    ratio = ohlcv.close / twenty_high
    if ratio >= 0.99:
        detail_flags.append(f"GATE_FAIL:G1_ALREADY_BROKE_OUT:{ratio:.3f}")
        return False, 4, 1, detail_flags
    if ratio < 0.85:
        detail_flags.append(f"GATE_FAIL:G1_TOO_FAR_BELOW:{ratio:.3f}")
        return False, 4, 1, detail_flags
    detail_flags.append(f"GATE_PASS:G1_ZONE:{ratio:.3f}")

    # G2: Bollinger Band width ≤ 15% (compression required)
    sorted_hist = sorted(ohlcv_history, key=lambda x: x.trade_date)
    bb_upper, bb_lower, bb_width_raw, _ = self._calculate_bb(sorted_hist)
    if bb_width_raw is None:
        detail_flags.append("GATE_SKIP:G2_INSUFFICIENT_BB_DATA")
        return False, 4, 1, detail_flags
    if bb_width_raw > 0.15:  # 0.15 = 15%
        detail_flags.append(f"GATE_FAIL:G2_BB_WIDE:{bb_width_raw*100:.1f}%")
        return False, 4, 2, detail_flags
    detail_flags.append(f"GATE_PASS:G2_BB:{bb_width_raw*100:.1f}%")

    # G3: Liquidity (already checked in _compute() via turnover_20ma, but replicate flag)
    detail_flags.append("GATE_PASS:G3_LIQUIDITY")

    detail_flags.append("GATE_AVAILABLE:4")
    detail_flags.append("GATE_MET:4")
    return True, 4, 4, detail_flags
```

Also update the constants at line ~119:

```python
_LONG_THRESHOLD_NEUTRAL = 65    # was 55
_LONG_THRESHOLD_UPTREND = 60    # was 50
_LONG_THRESHOLD_DOWNTREND = 70  # was 60
_WATCH_MIN = 45                  # was 40
```

- [ ] **Step 4: Run new gate tests to verify they pass**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestNewGate -v
```

Expected: all 4 new gate tests PASS.

- [ ] **Step 5: Check which old gate tests now fail and skip/delete them**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py -v 2>&1 | grep FAIL
```

Old tests that test the 2-of-5 combination logic (e.g., `test_gate_conditions_*`) will fail — add `@pytest.mark.skip(reason="v2 gate replaced by 4-hard-gate in pre-breakout redesign")` to those tests.

- [ ] **Step 6: Run all unit tests**

```bash
.venv/bin/pytest tests/unit/ -q
```

Expected: no unexpected failures (only the skipped old gate tests).

- [ ] **Step 7: Commit**

```bash
git add src/taiwan_stock_agent/domain/triple_confirmation_engine.py tests/unit/test_triple_confirmation_engine_v2.py
git commit -m "feat: rewrite engine gate to 4-hard-conditions for pre-breakout detection"
```

---

## Task 2: Pillar 1 New Factors (volume_dryup + volume_climax)

**Files:**
- Modify: `src/taiwan_stock_agent/domain/triple_confirmation_engine.py`
- Modify: `tests/unit/test_triple_confirmation_engine_v2.py`

Add `volume_dryup_pts` (max 8) and `volume_climax_pts` (max 4) to `_ScoreBreakdown`. Keep `volume_ratio_pts` field but zero it out in `_compute()` (backward compat for DB). Expand RSI healthy range to 40–65.

- [ ] **Step 1: Write failing tests**

```python
class TestVolumeDryup:
    def _make_dryup_history(self, base_vol: int = 10_000, recent_vol: int = 5_000, n: int = 30) -> list[DailyOHLCV]:
        """30-day history with recent 5 days at low volume."""
        result = []
        d = date(2025, 1, 2)
        for i in range(n):
            vol = recent_vol if i >= n - 5 else base_vol
            c = 100.0
            result.append(DailyOHLCV(ticker="TEST", trade_date=d + timedelta(days=i),
                                     open=c, high=c+0.3, low=c-0.3, close=c, volume=vol))
        return result

    def test_strong_dryup_gives_8pts(self):
        """Last 5d avg < 60% of 20d avg → 8 pts."""
        eng = TripleConfirmationEngine()
        # 20d avg = mix of 10000 and 5000; last 5d = 5000
        # 5000 / ~9250 ≈ 54% < 60% → should give 8 pts
        hist = self._make_dryup_history(base_vol=10_000, recent_vol=5_000, n=30)
        pts = eng._volume_dryup_score(hist)
        assert pts == 8

    def test_moderate_dryup_gives_4pts(self):
        """Last 5d avg 60–80% of 20d avg → 4 pts."""
        hist = self._make_dryup_history(base_vol=10_000, recent_vol=7_000, n=30)
        pts = TripleConfirmationEngine()._volume_dryup_score(hist)
        assert pts == 4

    def test_no_dryup_gives_0pts(self):
        hist = self._make_dryup_history(base_vol=10_000, recent_vol=9_500, n=30)
        pts = TripleConfirmationEngine()._volume_dryup_score(hist)
        assert pts == 0

    def test_volume_climax_both_conditions_gives_4pts(self):
        """At least one day > 2× avg AND current 5d < 80% avg → 4 pts."""
        hist = []
        d = date(2025, 1, 2)
        for i in range(30):
            # Day 15: spike to 25_000 (2.5× avg of 10_000)
            vol = 25_000 if i == 15 else 10_000
            # Last 5 days: 7_500 (75% of avg)
            if i >= 25:
                vol = 7_500
            c = 100.0
            hist.append(DailyOHLCV(ticker="TEST", trade_date=d + timedelta(days=i),
                                   open=c, high=c+0.3, low=c-0.3, close=c, volume=vol))
        pts = TripleConfirmationEngine()._volume_climax_score(hist)
        assert pts == 4

    def test_volume_climax_only_dryup_gives_0pts(self):
        """Dryup without prior climax day → 0 pts."""
        hist = self._make_dryup_history(base_vol=10_000, recent_vol=7_000, n=30)
        pts = TripleConfirmationEngine()._volume_climax_score(hist)
        assert pts == 0
```

- [ ] **Step 2: Run tests to confirm fail**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestVolumeDryup -v
```

Expected: FAIL (methods don't exist yet).

- [ ] **Step 3: Add new fields to `_ScoreBreakdown` and implement scoring methods**

In `_ScoreBreakdown` (line ~207), in the Pillar 1 section, add after `dmi_initiation_pts`:

```python
volume_dryup_pts: int = 0      # 0/4/8 — last 5d avg vs 20d avg (lower = better)
volume_climax_pts: int = 0     # 0/4 — prior spike day + current dryup
```

Update `total` property to add `+ self.volume_dryup_pts + self.volume_climax_pts`.

Update `momentum_pts` property to add `+ self.volume_dryup_pts + self.volume_climax_pts`.

Add static methods to `TripleConfirmationEngine`:

```python
@staticmethod
def _volume_dryup_score(history: list[DailyOHLCV]) -> int:
    """Reward volume drying up — sign of accumulation without selling pressure.
    
    Compares last 5-day average volume to 20-day average volume.
    """
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    if len(sorted_h) < 20:
        return 0
    vols = [d.volume for d in sorted_h]
    avg_20d = sum(vols[-20:]) / 20
    if avg_20d <= 0:
        return 0
    avg_5d = sum(vols[-5:]) / 5
    ratio = avg_5d / avg_20d
    if ratio < 0.60:
        return 8
    if ratio < 0.80:
        return 4
    return 0

@staticmethod
def _volume_climax_score(history: list[DailyOHLCV]) -> int:
    """Validate dryup is real: requires prior high-volume climax day + current dryup.
    
    Pattern: distribution/accumulation climax → quiet consolidation.
    Without prior climax, dryup alone may just mean low-interest stock.
    """
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    if len(sorted_h) < 20:
        return 0
    vols = [d.volume for d in sorted_h]
    avg_20d = sum(vols[-20:]) / 20
    if avg_20d <= 0:
        return 0
    # Check for at least one spike day in last 20 bars (> 2× avg)
    has_prior_climax = any(v > avg_20d * 2.0 for v in vols[-20:-5])
    # Check current dryup (last 5d < 80% of avg)
    avg_5d = sum(vols[-5:]) / 5
    has_current_dryup = (avg_5d / avg_20d) < 0.80
    return 4 if (has_prior_climax and has_current_dryup) else 0
```

In `_compute()`, in the Pillar 1 section after `bd.rsi_momentum_pts = ...`, add:

```python
bd.volume_dryup_pts = self._volume_dryup_score(ohlcv_history)
bd.volume_climax_pts = self._volume_climax_score(ohlcv_history)
```

Also remove `bd.volume_ratio_pts = ...` call (zero it out by not calling `_volume_ratio_score` anymore; keep the field for backward compat).

Update RSI range: in `_rsi_momentum_score()`, change `55 <= rsi <= 70` to `40 <= rsi <= 65`.

- [ ] **Step 4: Run new tests**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestVolumeDryup -v
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/pytest tests/unit/ -q
```

- [ ] **Step 6: Commit**

```bash
git add src/taiwan_stock_agent/domain/triple_confirmation_engine.py tests/unit/test_triple_confirmation_engine_v2.py
git commit -m "feat: add volume_dryup_pts and volume_climax_pts to Pillar 1, expand RSI range 40-65"
```

---

## Task 3: Pillar 3 Full Rewrite (Compression Factors)

**Files:**
- Modify: `src/taiwan_stock_agent/domain/triple_confirmation_engine.py`
- Modify: `tests/unit/test_triple_confirmation_engine_v2.py`

Remove 5 breakout factors. Add 6 compression factors. Add `_atr_20()` helper for consolidation detection.

- [ ] **Step 1: Write failing tests for new Pillar 3 factors**

```python
class TestPillar3Compression:
    """Tests for new compression-focused Pillar 3."""

    def _flat_hist(self, n: int = 40, close: float = 97.0) -> list[DailyOHLCV]:
        result = []
        d = date(2025, 1, 2)
        for i in range(n):
            result.append(DailyOHLCV(ticker="T", trade_date=d + timedelta(days=i),
                          open=close, high=close+0.3, low=close-0.3, close=close, volume=5000))
        return result

    def test_proximity_pts_max_when_near_high(self):
        """close/20d_high in 92-99% → 12 pts."""
        eng = TripleConfirmationEngine()
        # close=97, 20d_high=100 → ratio=0.97 → in 92-99% zone
        pts = eng._proximity_score(close=97.0, twenty_day_high=100.0)
        assert pts == 12

    def test_proximity_pts_mid_when_88_92(self):
        """close/20d_high in 88-92% → 6 pts."""
        pts = TripleConfirmationEngine()._proximity_score(close=90.0, twenty_day_high=100.0)
        assert pts == 6

    def test_proximity_pts_zero_below_85(self):
        pts = TripleConfirmationEngine()._proximity_score(close=80.0, twenty_day_high=100.0)
        assert pts == 0

    def test_bb_compression_max_when_very_tight(self):
        """BB width < 8% → 10 pts."""
        eng = TripleConfirmationEngine()
        hist = self._flat_hist(40)  # flat → very tight BB
        pts = eng._bb_compression_score(hist)
        assert pts == 10

    def test_inside_bar_streak_counts_consecutive(self):
        """3 consecutive inside bars → 3 pts."""
        eng = TripleConfirmationEngine()
        result = []
        d = date(2025, 1, 2)
        for i in range(30):
            if i >= 27:  # last 3 bars are inside bars (narrowing range)
                h, l = 100.0 + (29 - i) * 0.1, 100.0 - (29 - i) * 0.1
            else:
                h, l = 101.0, 99.0
            result.append(DailyOHLCV(ticker="T", trade_date=d + timedelta(days=i),
                           open=100.0, high=h, low=l, close=100.0, volume=5000))
        pts = eng._inside_bar_streak_score(result)
        assert pts == 3

    def test_prior_advance_gives_5pts_for_large_advance(self):
        """Prior advance ≥ 20% before consolidation → 5 pts."""
        eng = TripleConfirmationEngine()
        # Build 130-bar history: first 60 bars rise 25%, then consolidate flat
        result = []
        d = date(2025, 1, 2)
        for i in range(130):
            if i < 60:
                c = 80.0 + i * 0.25 / 60 * 80  # 80 → 100 (25% rise)
            else:
                c = 100.0  # flat consolidation
            result.append(DailyOHLCV(ticker="T", trade_date=d + timedelta(days=i),
                           open=c, high=c+0.3, low=c-0.3, close=c, volume=5000))
        pts = eng._prior_advance_score(result)
        assert pts == 5
```

- [ ] **Step 2: Run tests to confirm fail**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestPillar3Compression -v
```

Expected: FAIL (methods don't exist).

- [ ] **Step 3: Remove breakout fields from `_ScoreBreakdown` and add compression fields**

In `_ScoreBreakdown` Pillar 3 section, REMOVE:
```python
breakout_20d_pts: int = 0
breakout_60d_pts: int = 0
breakout_quality_pts: int = 0
breakout_volume_pts: int = 0
upside_space_pts: int = 0
bb_squeeze_breakout_pts: int = 0  # keep as 0, zero it in _compute
```

ADD (keep `ma_alignment_pts`, `ma20_slope_pts`, `relative_strength_pts`):
```python
proximity_pts: int = 0           # 0/6/12 — close distance to 20d_high
bb_compression_pts: int = 0      # 0/5/10 — BB width tightness
ma_convergence_pts: int = 0      # 0/4/8 — MA5/MA10/MA20 convergence
consolidation_weeks_pts: int = 0 # 0/3/6 — consecutive days in compression zone
inside_bar_streak_pts: int = 0   # 0–5 — narrowing bar count
prior_advance_pts: int = 0       # 0/2/5 — prior advance before consolidation
```

Update `total` property: replace removed fields with new ones.
Update `structure_pts` property similarly.

- [ ] **Step 4: Add a static `_atr_20()` helper**

```python
@staticmethod
def _atr_20(history: list[DailyOHLCV]) -> float | None:
    """Simple 20-bar ATR using true range (no Wilder smoothing, for simplicity).
    Returns the average true range of the last 20 bars.
    """
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    if len(sorted_h) < 21:
        return None
    trs = []
    for i in range(len(sorted_h) - 20, len(sorted_h)):
        bar = sorted_h[i]
        prev_close = sorted_h[i - 1].close
        tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None
```

- [ ] **Step 5: Implement new scoring methods**

```python
@staticmethod
def _proximity_score(close: float, twenty_day_high: float) -> int:
    """Reward stocks just below 20d resistance. Max 12 pts."""
    if twenty_day_high <= 0:
        return 0
    ratio = close / twenty_day_high
    if 0.92 <= ratio < 0.99:
        return 12
    if 0.88 <= ratio < 0.92:
        return 6
    return 0  # below 0.88 or at/above 0.99 (already broke)

@staticmethod
def _bb_compression_score(history: list[DailyOHLCV]) -> int:
    """Reward tight BB bands. bb_width_raw = (upper-lower)/mid. Max 10 pts.
    BB threshold hierarchy: gate ≤15% (0.15), consolidation <12% (0.12), max points <8% (0.08).
    """
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    _, _, bb_width_raw, _ = TripleConfirmationEngine._calculate_bb(sorted_h)
    if bb_width_raw is None:
        return 0
    if bb_width_raw < 0.08:
        return 10
    if bb_width_raw < 0.12:
        return 5
    return 0  # 12-15% = 0 pts (gate was ≤15%, but scoring rewards tighter)

@staticmethod
def _ma_convergence_score(history: list[DailyOHLCV]) -> int:
    """MA5/MA10/MA20 converging. Max 8 pts."""
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    closes = [d.close for d in sorted_h]
    if len(closes) < 20:
        return 0
    ma5  = sum(closes[-5:])  / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    if ma20 == 0:
        return 0
    spread = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / ma20
    if spread < 0.02:
        return 8
    if spread < 0.05:
        return 4
    return 0

def _consolidation_weeks_score(self, history: list[DailyOHLCV]) -> int:
    """Count consecutive days of compression (BB<12% AND range<1.5×ATR), / 5 = weeks. Max 6 pts.
    BB threshold at 12% is the mid-tier between gate (15%) and max compression reward (8%).
    """
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    if len(sorted_h) < 21:
        return 0
    atr = self._atr_20(sorted_h)
    if atr is None or atr <= 0:
        return 0
    # Walk backwards from most recent bar
    count = 0
    for i in range(len(sorted_h) - 1, max(len(sorted_h) - 61, 20), -1):
        window = sorted_h[max(0, i - 19): i + 1]
        _, _, bb_w, _ = self._calculate_bb(window)
        if bb_w is None or bb_w >= 0.12:
            break
        bar = sorted_h[i]
        prev_close = sorted_h[i - 1].close
        tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        if tr >= atr * 1.5:
            break
        count += 1
    weeks = count / 5
    if weeks >= 4:
        return 6
    if weeks >= 2:
        return 3
    return 0

@staticmethod
def _inside_bar_streak_score(history: list[DailyOHLCV]) -> int:
    """Count consecutive inside bars (high ≤ prev_high AND low ≥ prev_low). Max 5 pts."""
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    if len(sorted_h) < 2:
        return 0
    streak = 0
    for i in range(len(sorted_h) - 1, 0, -1):
        bar = sorted_h[i]
        prev = sorted_h[i - 1]
        if bar.high <= prev.high and bar.low >= prev.low:
            streak += 1
        else:
            break
    return min(streak, 5)

@staticmethod
def _prior_advance_score(history: list[DailyOHLCV]) -> int:
    """Prior advance ≥ 20% in 60 bars before current consolidation. Max 5 pts.
    
    Lookback: find max close in bars [-120:-60] vs close at bar -120.
    This validates the stock had an uptrend before the current base.
    """
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    if len(sorted_h) < 120:
        return 0
    prior_window = sorted_h[-120:-60]
    base_close = prior_window[0].close
    if base_close <= 0:
        return 0
    peak_close = max(d.close for d in prior_window)
    advance = (peak_close - base_close) / base_close
    if advance >= 0.20:
        return 5
    if advance >= 0.10:
        return 2
    return 0
```

- [ ] **Step 6: Wire new factors in `_compute()` — replace Pillar 3 section**

Remove calls to `_breakout_20d_score`, `_breakout_60d_score`, etc.
Add:
```python
# --- Pillar 3: Compression Structure ---
bd.proximity_pts = self._proximity_score(ohlcv.close, volume_profile.twenty_day_high)
bd.bb_compression_pts = self._bb_compression_score(ohlcv_history)
bd.ma_convergence_pts = self._ma_convergence_score(ohlcv_history)
bd.consolidation_weeks_pts = self._consolidation_weeks_score(ohlcv_history)
bd.inside_bar_streak_pts = self._inside_bar_streak_score(ohlcv_history)
bd.prior_advance_pts = self._prior_advance_score(ohlcv_history)
# Keep ma_alignment_pts, ma20_slope_pts, relative_strength_pts
```

- [ ] **Step 7: Run new Pillar 3 tests**

```bash
.venv/bin/pytest tests/unit/test_triple_confirmation_engine_v2.py::TestPillar3Compression -v
```

Expected: all tests PASS.

- [ ] **Step 8: Run full suite**

```bash
.venv/bin/pytest tests/unit/ -q
```

- [ ] **Step 9: Commit**

```bash
git add src/taiwan_stock_agent/domain/triple_confirmation_engine.py tests/unit/test_triple_confirmation_engine_v2.py
git commit -m "feat: rewrite Pillar 3 to compression factors (proximity, BB, MA convergence, consolidation)"
```

---

## Task 4: AccumulationEngine G1 Fix

**Files:**
- Modify: `src/taiwan_stock_agent/domain/accumulation_engine.py`
- Modify: `tests/unit/test_accumulation_engine.py`

Replace `G1: MA20 > MA60` with `G1: close within MA20 ± 8% AND MA20 slope flat/rising`.

- [ ] **Step 1: Write failing tests**

Check existing test file first:
```bash
cat tests/unit/test_accumulation_engine.py | head -50
```

Add new G1 tests:
```python
def test_g1_passes_when_close_near_ma20_with_flat_slope():
    """close within MA20 ± 8% AND slope flat → G1 passes."""
    # Build history: 60 bars flat at 100.0 → MA20 ≈ MA60 ≈ 100, slope = 0
    hist = _make_flat_history(n=65, close=100.0)
    eng = AccumulationEngine(market="TSE")
    passes, flags = eng._gate_check(hist, "neutral", 25_000_000)
    # With flat MA and close near MA20, G1 should pass
    assert "ACCUM_FAIL:G1_MA20_LE_MA60" not in flags
    assert "ACCUM_FAIL:G1_CLOSE_FAR_FROM_MA20" not in flags

def test_g1_fails_when_close_too_far_from_ma20():
    """close > MA20 × 1.08 (more than 8% above MA20) → G1 fails."""
    hist = _make_flat_history(n=65, close=100.0)
    # Override last bar with close far above MA20
    hist[-1] = DailyOHLCV(ticker="TEST", trade_date=hist[-1].trade_date,
                           open=110.0, high=111.0, low=109.0, close=112.0, volume=20_000)
    eng = AccumulationEngine(market="TSE")
    passes, flags = eng._gate_check(hist, "neutral", 25_000_000)
    assert "ACCUM_FAIL:G1_CLOSE_FAR_FROM_MA20" in flags

def test_g1_fails_when_ma20_slope_falling():
    """MA20 slope < -1% in 5 days → G1 fails."""
    # Declining history: MA20 is falling
    hist = []
    d = date(2025, 1, 2)
    for i in range(65):
        c = 110.0 - i * 0.2  # declining 0.2/day → MA20 clearly falling
        hist.append(DailyOHLCV(ticker="TEST", trade_date=d + timedelta(days=i),
                               open=c, high=c+0.3, low=c-0.3, close=c, volume=20_000))
    eng = AccumulationEngine(market="TSE")
    passes, flags = eng._gate_check(hist, "neutral", 25_000_000)
    assert "ACCUM_FAIL:G1_MA20_SLOPE_DOWN" in flags
```

- [ ] **Step 2: Run tests to confirm fail**

```bash
.venv/bin/pytest tests/unit/test_accumulation_engine.py -v -k "g1"
```

- [ ] **Step 3: Rewrite G1 in `accumulation_engine.py` `_gate_check()`**

Replace lines 36-46 (the G1 block):

```python
# G1: close within MA20 ± 8% AND MA20 slope flat or rising
if len(closes) < 60:
    return False, ["ACCUM_SKIP:INSUFFICIENT_HISTORY"]
ma20 = sum(closes[-20:]) / 20
if ma20 <= 0:
    return False, ["ACCUM_FAIL:G1_MA20_ZERO"]
# Sub-condition 1: close within ± 8% of MA20
proximity = abs(closes[-1] - ma20) / ma20
if proximity > 0.08:
    direction = "ABOVE" if closes[-1] > ma20 else "BELOW"
    return False, [f"ACCUM_FAIL:G1_CLOSE_FAR_FROM_MA20:{direction}:{proximity*100:.1f}%"]
# Sub-condition 2: MA20 slope flat or rising (≥ -1% over 5 days)
if len(closes) >= 25:
    ma20_5d_ago = sum(closes[-25:-5]) / 20
    if ma20_5d_ago > 0 and (ma20 - ma20_5d_ago) / ma20_5d_ago < -0.01:
        return False, ["ACCUM_FAIL:G1_MA20_SLOPE_DOWN"]
```

- [ ] **Step 4: Run G1 tests**

```bash
.venv/bin/pytest tests/unit/test_accumulation_engine.py -v
```

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/pytest tests/unit/ -q
```

- [ ] **Step 6: Commit**

```bash
git add src/taiwan_stock_agent/domain/accumulation_engine.py tests/unit/test_accumulation_engine.py
git commit -m "fix: relax AccumulationEngine G1 to close within MA20 ±8% with slope check"
```

---

## Task 5: Industry-Grouped Output in batch_plan.py

**Files:**
- Modify: `scripts/batch_plan.py`

Add `_print_by_industry()` function. Keep `_print_table()` intact (other callers may use it). Call `_print_by_industry()` from the main scan output path. CSV output is unchanged.

- [ ] **Step 1: Read relevant section of batch_plan.py**

```bash
sed -n '661,800p' scripts/batch_plan.py
```

- [ ] **Step 2: Add `_print_by_industry()` after `_print_table()`**

```python
def _print_by_industry(
    results: list[dict],
    top: int,
    min_confidence: int,
    scan_date: str = "",
    name_map: dict[str, str] | None = None,
    industry_map: dict[str, str] | None = None,
) -> None:
    """Print scan results grouped by industry strength, sorted high→low.
    
    Each industry section shows: industry name, strength %, qualifying stocks.
    Industries with no qualifying stocks show header only.
    Weak industries (strength < -1%) are shown last with ▼ marker.
    """
    from collections import defaultdict

    valid = [r for r in results if not r["halt"] and r["error"] is None
             and r["confidence"] >= min_confidence]

    if not valid:
        _console.print("[dim]  (無符合條件標的)[/dim]")
        return

    ind_map = industry_map or {}
    name_m  = name_map or {}

    # Compute industry strength: median change_pct of all results per industry
    # If no change_pct available, use 0.0
    industry_change: dict[str, list[float]] = defaultdict(list)
    for r in results:  # use ALL results for industry strength, not just valid
        ind = ind_map.get(r["ticker"], "其他")
        chg = r.get("change_pct", 0.0) or 0.0
        industry_change[ind].append(chg)

    def _median(vals: list[float]) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    industry_strength: dict[str, float] = {
        ind: _median(chgs) for ind, chgs in industry_change.items()
    }

    # Group valid stocks by industry
    by_industry: dict[str, list[dict]] = defaultdict(list)
    for r in valid:
        ind = ind_map.get(r["ticker"], "其他")
        by_industry[ind].append(r)

    # Sort stocks within each industry by confidence desc
    for ind in by_industry:
        by_industry[ind].sort(key=lambda r: r["confidence"], reverse=True)

    # Sort industries: strong first, weak last
    all_industries = sorted(
        industry_strength.keys(),
        key=lambda ind: industry_strength[ind],
        reverse=True,
    )

    title = f"掃描結果  {scan_date}  【產業強度排序】" if scan_date else "掃描結果  【產業強度排序】"
    _console.print(f"\n[bold white]{title}[/bold white]")

    for ind in all_industries:
        strength = industry_strength.get(ind, 0.0)
        stocks = by_industry.get(ind, [])
        ready_n = sum(1 for s in stocks if s["action"] == "LONG")

        strength_icon = "▲" if strength >= 0 else "▼"
        strength_color = "green" if strength >= 0 else "red"
        ind_header = (
            f"[dim]──[/dim] [bold]{ind}[/bold]  "
            f"[{strength_color}]{strength_icon}{abs(strength):.1f}%[/{strength_color}]"
        )
        if stocks:
            ind_header += f"  [dim]({ready_n} 準備突破 / {len(stocks)} 整理中)[/dim]"
        else:
            ind_header += "  [dim]─ 無符合個股 ─[/dim]"

        _console.print(ind_header)

        for s in stocks:
            ticker = s["ticker"]
            name   = (name_m.get(ticker) or "")[:5]
            action = s["action"]
            conf   = s["confidence"]
            # vs 20d high: derive from flags or entry_bid/close ratio
            bb_pct = ""  # TODO: expose bb_width from score_breakdown in future
            twenty_high_pct = ""
            flags = s.get("data_quality_flags", "") or ""

            action_label = "🚀 準備突破" if action == "LONG" else "🔍 整理中"
            action_clr   = "cyan" if action == "LONG" else "yellow"

            conf_bar = _conf_bar(conf)
            _console.print(
                f"  [dim]{ticker}[/dim]  [{action_clr}]{action_label}[/{action_clr}]"
                f"  {conf_bar}  [dim]{name}[/dim]"
            )

    if top and len(valid) > top:
        _console.print(f"\n[dim]  (顯示前 {top} 檔，共 {len(valid)} 檔符合條件)[/dim]")
```

- [ ] **Step 3: Update the main output call in `run_batch()` or the display section**

Find where `_print_table()` is called from `run_batch()` (around line 936–960). Add a call to `_print_by_industry()` if `industry_map` is available:

```python
# Existing: _print_table(results, top, min_confidence, ...)
# Add after:
if industry_map:
    _print_by_industry(results, top, min_confidence, scan_date=scan_date,
                       name_map=name_map, industry_map=industry_map)
else:
    _print_table(results, top, min_confidence, scan_date=scan_date, name_map=name_map)
```

- [ ] **Step 4: Test manually with a sample run**

```bash
make show  # use existing scan CSV to verify output format
```

Expected: grouped output showing industry sections, no Python errors.

- [ ] **Step 5: Commit**

```bash
git add scripts/batch_plan.py
git commit -m "feat: add industry-grouped output sorted by industry strength"
```

---

## Task 6: Market Sentiment Client + Domain Module

**Files:**
- Create: `src/taiwan_stock_agent/infrastructure/sentiment_client.py`
- Create: `src/taiwan_stock_agent/domain/market_sentiment.py`
- Create: `tests/unit/test_market_sentiment.py`

Fetch TWSE breadth data + Yahoo Finance Taiwan RSS. Compute sentiment label. No external auth needed.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_market_sentiment.py
from taiwan_stock_agent.domain.market_sentiment import (
    compute_sentiment, MarketSentiment, BreadthData
)

class TestComputeSentiment:
    def _breadth(self, ad_ratio: float, vol_ratio: float) -> BreadthData:
        return BreadthData(ad_ratio=ad_ratio, volume_ratio=vol_ratio)

    def test_green_label_when_all_positive(self):
        """ad_ratio > 2.0 AND rsi 50-70 AND vol_ratio > 1.0 → 多頭熱絡."""
        s = compute_sentiment(self._breadth(2.5, 1.2), [], taiex_rsi=60.0)
        assert s.label == "多頭熱絡"
        assert s.emoji == "🟢"

    def test_red_label_when_ad_ratio_low(self):
        """ad_ratio < 0.8 → 偏空謹慎."""
        s = compute_sentiment(self._breadth(0.5, 0.9), [], taiex_rsi=45.0)
        assert s.label == "偏空謹慎"
        assert s.emoji == "🔴"

    def test_red_label_when_rsi_low(self):
        """rsi < 40 → 偏空謹慎."""
        s = compute_sentiment(self._breadth(1.5, 1.0), [], taiex_rsi=35.0)
        assert s.label == "偏空謹慎"

    def test_yellow_label_by_default(self):
        """Normal conditions → 中性震盪."""
        s = compute_sentiment(self._breadth(1.2, 0.95), [], taiex_rsi=52.0)
        assert s.label == "中性震盪"
        assert s.emoji == "🟡"

    def test_bearish_keyword_in_headlines_creates_alert(self):
        """Headline with bearish keyword → alert string in s.alerts."""
        headlines = ["台股今日暴跌 外資大量出走", "Fed升息預期升溫"]
        s = compute_sentiment(self._breadth(1.5, 1.0), headlines, taiex_rsi=52.0)
        assert len(s.alerts) > 0
        assert any("暴跌" in a or "升息" in a for a in s.alerts)

    def test_hot_keyword_in_headlines_extracted(self):
        """Headline with hot keyword → keyword in s.hot_keywords."""
        headlines = ["AI伺服器需求大爆發 CoWoS訂單暢旺"]
        s = compute_sentiment(self._breadth(1.5, 1.0), headlines, taiex_rsi=60.0)
        assert "AI" in s.hot_keywords or "CoWoS" in s.hot_keywords
```

- [ ] **Step 2: Run tests to confirm fail**

```bash
.venv/bin/pytest tests/unit/test_market_sentiment.py -v
```

- [ ] **Step 3: Create `market_sentiment.py`**

```python
# src/taiwan_stock_agent/domain/market_sentiment.py
from __future__ import annotations
from dataclasses import dataclass, field

_BEARISH_WORDS = ["升息", "制裁", "暴跌", "崩盤", "停牌", "調查", "虧損", "下修", "暴跌", "賣壓"]
_HOT_KEYWORDS  = ["AI", "CoWoS", "HBM", "電動車", "記憶體", "機器人", "散熱", "伺服器", "半導體"]


@dataclass
class BreadthData:
    ad_ratio: float      # advance / decline count ratio
    volume_ratio: float  # today volume / 20d avg volume


@dataclass
class MarketSentiment:
    label: str
    emoji: str
    ad_ratio: float
    taiex_rsi: float
    volume_ratio: float
    alerts: list[str] = field(default_factory=list)
    hot_keywords: list[str] = field(default_factory=list)


def compute_sentiment(
    breadth: BreadthData,
    headlines: list[str],
    taiex_rsi: float,
) -> MarketSentiment:
    """Compute market sentiment from quantitative breadth + news headlines."""
    # Label logic
    is_red = (
        breadth.ad_ratio < 0.8
        or taiex_rsi < 40
    )
    is_green = (
        breadth.ad_ratio > 2.0
        and 50 <= taiex_rsi <= 70
        and breadth.volume_ratio > 1.0
    )
    if is_red:
        label, emoji = "偏空謹慎", "🔴"
    elif is_green:
        label, emoji = "多頭熱絡", "🟢"
    else:
        label, emoji = "中性震盪", "🟡"

    # Scan headlines for keywords
    all_text = " ".join(headlines)
    alerts = [w for w in _BEARISH_WORDS if w in all_text]
    hot_keywords = [w for w in _HOT_KEYWORDS if w in all_text]

    return MarketSentiment(
        label=label,
        emoji=emoji,
        ad_ratio=breadth.ad_ratio,
        taiex_rsi=taiex_rsi,
        volume_ratio=breadth.volume_ratio,
        alerts=alerts,
        hot_keywords=hot_keywords,
    )
```

- [ ] **Step 4: Create `sentiment_client.py`**

```python
# src/taiwan_stock_agent/infrastructure/sentiment_client.py
"""Fetches market breadth from TWSE and news headlines from Yahoo Finance RSS."""
from __future__ import annotations
import logging
import urllib.request
import urllib.error
from xml.etree import ElementTree

from taiwan_stock_agent.domain.market_sentiment import BreadthData

logger = logging.getLogger(__name__)

_YAHOO_RSS_URL = "https://tw.stock.yahoo.com/rss"
_TWSE_BREADTH_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.asp?ex_ch=tse_t00.tw&json=1&delay=0"


def fetch_breadth(taiex_rsi: float | None = None) -> BreadthData | None:
    """Fetch advance/decline ratio from TWSE MIS.
    
    Returns BreadthData or None if fetch fails.
    TWSE MIS tse_t00.tw returns market-wide up/down count.
    """
    try:
        req = urllib.request.Request(_TWSE_BREADTH_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json
            data = json.loads(resp.read())
        # Data format: msgArray[0] has fields including "u" (up) and "d" (down)
        msg = data.get("msgArray", [{}])[0]
        up   = float(msg.get("u", 0) or 0)
        down = float(msg.get("d", 0) or 0)
        vol_ratio = 1.0  # placeholder: volume ratio computed elsewhere
        if down == 0:
            return BreadthData(ad_ratio=3.0, volume_ratio=vol_ratio)
        return BreadthData(ad_ratio=up / down, volume_ratio=vol_ratio)
    except Exception as e:
        logger.debug("fetch_breadth error: %s", e)
        return None


def fetch_news_headlines(max_items: int = 20) -> list[str]:
    """Fetch latest headlines from Yahoo Finance Taiwan RSS.
    
    Returns list of headline strings. Empty list on failure.
    """
    try:
        req = urllib.request.Request(_YAHOO_RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            xml_data = resp.read()
        root = ElementTree.fromstring(xml_data)
        titles = []
        for item in root.iter("item"):
            title_el = item.find("title")
            if title_el is not None and title_el.text:
                titles.append(title_el.text.strip())
            if len(titles) >= max_items:
                break
        return titles
    except Exception as e:
        logger.debug("fetch_news_headlines error: %s", e)
        return []
```

- [ ] **Step 5: Run sentiment tests**

```bash
.venv/bin/pytest tests/unit/test_market_sentiment.py -v
```

Expected: all tests PASS (no network needed, tests only use `compute_sentiment`).

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/pytest tests/unit/ -q
```

- [ ] **Step 7: Commit**

```bash
git add src/taiwan_stock_agent/infrastructure/sentiment_client.py \
        src/taiwan_stock_agent/domain/market_sentiment.py \
        tests/unit/test_market_sentiment.py
git commit -m "feat: add SentimentClient (TWSE breadth + Yahoo RSS) and MarketSentiment domain"
```

---

## Task 7: Bot Dashboard Sentiment Widget

**Files:**
- Modify: `scripts/bot.py`

Add `"sentiment"` to `_MARKET_CACHE`. Add `_fetch_sentiment_sync()`. Add it to `_refresh_market_loop()`. Display in `_render_status_panel()`.

- [ ] **Step 1: Add `"sentiment"` key to `_MARKET_CACHE`**

At line 103, add to the dict:
```python
"sentiment": None,  # MarketSentiment | None
```

- [ ] **Step 2: Add `_fetch_sentiment_sync()` function**

After `_fetch_watchlist_prices_sync()`:

```python
def _fetch_sentiment_sync() -> "MarketSentiment | None":
    """Fetch TWSE breadth + Yahoo RSS headlines, return MarketSentiment."""
    try:
        from taiwan_stock_agent.infrastructure.sentiment_client import fetch_breadth, fetch_news_headlines
        from taiwan_stock_agent.domain.market_sentiment import compute_sentiment

        # Compute TAIEX RSI from cached global data
        taiex_data = _MARKET_CACHE.get("global", {}).get("taiex")
        taiex_rsi = 50.0  # default neutral
        # Simple approximation: use cached TAIEX price change direction
        if taiex_data:
            chg = taiex_data.get("change_pct", 0.0) or 0.0
            # Crude RSI proxy from recent change; real RSI needs history
            taiex_rsi = 55.0 if chg > 0.5 else (45.0 if chg < -0.5 else 50.0)

        breadth = fetch_breadth()
        headlines = fetch_news_headlines()
        if breadth is None:
            return None
        return compute_sentiment(breadth, headlines, taiex_rsi)
    except Exception as e:
        logger.debug("sentiment fetch error: %s", e)
        return None
```

- [ ] **Step 3: Add sentiment fetch to `_refresh_market_loop()`**

In `_refresh_market_loop()` (line 294), add sentiment to the `asyncio.gather()` call:

```python
global_data, sector_data, wl_data, sentiment = await asyncio.gather(
    loop.run_in_executor(None, _fetch_global_markets_sync),
    loop.run_in_executor(None, _fetch_tw_sectors_sync),
    loop.run_in_executor(None, _fetch_watchlist_prices_sync, tickers),
    loop.run_in_executor(None, _fetch_sentiment_sync),
)
if sentiment is not None:
    _MARKET_CACHE["sentiment"] = sentiment
```

- [ ] **Step 4: Add sentiment widget to `_render_status_panel()`**

At the end of `_render_status_panel()`, before `return Panel(...)`, add:

```python
# ── Market Sentiment widget ──────────────────────────────────────────────
sentiment = _MARKET_CACHE.get("sentiment")
if sentiment:
    st_clr = {"🟢": "green", "🟡": "yellow", "🔴": "red"}.get(sentiment.emoji, "white")
    t.add_row("", "")
    t.add_row(
        "[dim]市場輿情[/dim]",
        f"[{st_clr}]{sentiment.emoji} {sentiment.label}[/{st_clr}]",
    )
    t.add_row(
        "",
        f"[dim]漲跌比 {sentiment.ad_ratio:.1f} · RSI {sentiment.taiex_rsi:.0f} · 量 {sentiment.volume_ratio:.1f}×[/dim]",
    )
    if sentiment.alerts:
        t.add_row("", f"[yellow]⚠ {sentiment.alerts[0]}[/yellow]")
    if sentiment.hot_keywords:
        kws = " ".join(sentiment.hot_keywords[:3])
        t.add_row("", f"[cyan]🔥 {kws}[/cyan]")
```

- [ ] **Step 5: Test by running the bot**

```bash
make bot
```

Verify sentiment widget appears in Bot Status panel. If `_fetch_sentiment_sync()` returns None (e.g., market closed / TWSE API unavailable), the widget just doesn't appear — no crash.

- [ ] **Step 6: Run syntax check**

```bash
.venv/bin/python -c "import ast; ast.parse(open('scripts/bot.py').read()); print('syntax OK')"
```

- [ ] **Step 7: Commit**

```bash
git add scripts/bot.py
git commit -m "feat: add market sentiment widget to bot dashboard (TWSE breadth + Yahoo RSS)"
```

---

## Final: Run All Tests + Update README

- [ ] **Run all unit tests**

```bash
.venv/bin/pytest tests/unit/ -q
```

Expected: all tests pass (no unexpected failures).

- [ ] **Update README.md Phase table**

Add Phase 4.21 entry:
```
| Phase 4.21 | ✅ Done | Pre-breakout signal engine: Gate rewrite (BB ≤15%, close 85-99% of 20d_high), Pillar 3 compression factors, AccumulationEngine G1 fix, industry-grouped output, market sentiment widget |
```

- [ ] **Update CLAUDE.md Phase Gates table** similarly.

- [ ] **Final commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: update Phase 4.21 gate — pre-breakout engine redesign complete"
```
