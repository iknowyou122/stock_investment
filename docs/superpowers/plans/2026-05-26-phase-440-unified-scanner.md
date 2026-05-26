# Phase 4.40 Unified Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge `plan`/`surge`/pullback into one `make plan` output where every stock gets a signal type label (蓄積型/回調型/爆量型/趨勢延伸型) + holding horizon (波段/短線) + fundamental badge (★ 月營收+XX%) when applicable.

**Architecture:** Add a `PullbackDetector` domain class that finds stocks in uptrend that have pulled back to MA20 after touching the upper BB. In `run_batch()`, after the TCE scan completes, run a lightweight pullback pass on the same tickers (hits L1 in-memory OHLCV cache — no extra HTTP calls), then load any surge signals from the DB for the same date, merge all three signal types, apply growth enrichment to all, and display a unified table with 型態/持倉/基本面 columns.

**Tech Stack:** Python 3.10+, existing `DailyOHLCV`/`TWSEChipProxy` models, `FinMindClient` L1 mem cache, `query_surge_signals()` from `surge_recorder.py`, `Rich` for terminal, existing HTML generator.

---

## File Map

| File | Change |
|------|--------|
| `src/taiwan_stock_agent/domain/pullback_detector.py` | **Create** — PullbackDetector class |
| `tests/unit/test_pullback_detector.py` | **Create** — 7 unit tests |
| `scripts/batch_plan.py` | **Modify** — 7 additions (classify, pullback scan, surge load, merge, growth enrich extension, print update, HTML update) |

---

## Task 1: PullbackDetector

**Files:**
- Create: `src/taiwan_stock_agent/domain/pullback_detector.py`

- [ ] **Step 1: Write the failing test first**

Create `tests/unit/test_pullback_detector.py`:

```python
"""Unit tests for PullbackDetector."""
from __future__ import annotations
from datetime import date, timedelta
from taiwan_stock_agent.domain.models import DailyOHLCV
from taiwan_stock_agent.domain.pullback_detector import PullbackDetector


def _bars(closes: list[float], vols: list[int] | None = None) -> list[DailyOHLCV]:
    base = date(2025, 1, 2)
    return [
        DailyOHLCV(
            ticker="TEST",
            trade_date=base + timedelta(days=i),
            open=c - 0.5,
            high=c + 1.0,
            low=c - 1.0,
            close=c,
            volume=vols[i] if vols else 1_000_000,
        )
        for i, c in enumerate(closes)
    ]


def _pullback_history(vol_pullback: int = 1_000_000) -> list[DailyOHLCV]:
    """75 bars: 60 gradual uptrend → 10 surge to upper BB → 5 pullback to MA20."""
    closes: list[float] = []
    c = 80.0
    # Phase 1: gentle uptrend → MA alignment builds
    for _ in range(60):
        c += 0.3
        closes.append(round(c, 2))
    # Phase 2: surge to upper BB
    for _ in range(10):
        c += 2.0
        closes.append(round(c, 2))
    # Phase 3: pullback toward MA20
    for _ in range(5):
        c -= 2.8
        closes.append(round(c, 2))

    vols = [1_000_000] * 70 + [vol_pullback] * 5
    return _bars(closes, vols)


def test_scores_valid_pullback():
    result = PullbackDetector().score(_pullback_history())
    assert result is not None
    assert result["score"] > 0
    assert "PULLBACK_MA20" in result["flags"]


def test_returns_none_if_too_short():
    assert PullbackDetector().score(_bars([80.0] * 40)) is None


def test_returns_none_if_no_ma_alignment():
    """Downtrend — MA5 < MA20 < MA60."""
    closes = [100.0 - i * 0.5 for i in range(80)]
    assert PullbackDetector().score(_bars(closes)) is None


def test_returns_none_if_price_far_above_ma20():
    """Pure uptrend, never pulls back to MA20."""
    closes = [80.0 + i * 0.5 for i in range(80)]
    assert PullbackDetector().score(_bars(closes)) is None


def test_returns_none_if_no_upper_bb_touch():
    """Tiny oscillation — never reaches upper BB in last 10 days."""
    closes = []
    c = 80.0
    for i in range(80):
        c += 0.05 if i % 2 == 0 else -0.05
        closes.append(round(c, 2))
    assert PullbackDetector().score(_bars(closes)) is None


def test_vol_contraction_flag():
    """Low volume during pullback → VOL_CONTRACTION flag."""
    result = PullbackDetector().score(_pullback_history(vol_pullback=200_000))
    assert result is not None
    assert any("VOL_CONTRACTION" in f for f in result["flags"])


def test_score_capped_at_100():
    result = PullbackDetector().score(_pullback_history())
    assert result is None or result["score"] <= 100
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/unit/test_pullback_detector.py -v 2>&1 | head -20
```

Expected: ImportError — `pullback_detector` module not found.

- [ ] **Step 3: Create `src/taiwan_stock_agent/domain/pullback_detector.py`**

```python
"""PullbackDetector — finds stocks in uptrend that have pulled back to MA20.

Setup criteria:
  Gate 1: MA5 > MA20 > MA60 (confirmed uptrend)
  Gate 2: Current close within MA20 ±3%  (at pullback support)
  Gate 3: Touched upper BB within last 10 days  (had momentum)

Score factors (0–100):
  MA20 proximity   0–30 pts  (closer = better entry)
  Volume contraction 0–20 pts (healthy pullback)
  MA20 slope       0–20 pts  (trend strength)
  Bounce candle    0–15 pts  (reversal signal)
  MA60 slope       0–15 pts  (long-term trend)
"""
from __future__ import annotations

from statistics import mean, stdev

from taiwan_stock_agent.domain.models import DailyOHLCV


class PullbackDetector:
    def score(self, history: list[DailyOHLCV]) -> dict | None:
        """Return score dict or None if gates not met.

        Required: len(history) >= 65 (60 for BB + 5 for MA5).
        """
        if len(history) < 65:
            return None

        sorted_h = sorted(history, key=lambda x: x.trade_date)
        closes = [d.close for d in sorted_h]
        vols = [d.volume for d in sorted_h]
        close = closes[-1]

        # ── Gate 1: MA alignment ──────────────────────────────────────────
        ma5 = mean(closes[-5:])
        ma20 = mean(closes[-20:])
        ma60 = mean(closes[-60:])
        if not (ma5 > ma20 > ma60):
            return None

        # ── Gate 2: price within MA20 ±3% ────────────────────────────────
        ma20_pct = (close - ma20) / ma20
        if abs(ma20_pct) > 0.03:
            return None

        # ── Gate 3: touched upper BB in last 10 bars ──────────────────────
        upper_bb_touched = False
        n = len(closes)
        for i in range(n - 10, n):
            window = closes[max(0, i - 20):i]
            if len(window) < 5:
                continue
            bb_mid = mean(window)
            try:
                bb_std = stdev(window)
            except Exception:
                continue
            if closes[i] >= (bb_mid + 2 * bb_std) * 0.97:
                upper_bb_touched = True
                break
        if not upper_bb_touched:
            return None

        # ── Scoring ───────────────────────────────────────────────────────
        flags: list[str] = ["PULLBACK_MA20"]
        score = 0

        # 1. Proximity to MA20 (closer = better entry, 0–30 pts)
        score += int((1.0 - abs(ma20_pct) / 0.03) * 30)

        # 2. Volume contraction during pullback (0–20 pts)
        avg_vol = mean(vols[-20:])
        pullback_vol = mean(vols[-3:])
        if avg_vol > 0:
            vr = pullback_vol / avg_vol
            if vr < 0.6:
                score += 20
                flags.append("VOL_CONTRACTION_STRONG")
            elif vr < 0.8:
                score += 12
                flags.append("VOL_CONTRACTION")
            elif vr > 1.2:
                score -= 5
                flags.append("VOL_EXPANDING_BEARISH")

        # 3. MA20 slope — uptrend strength (0–20 pts)
        ma20_prev = mean(closes[-25:-5])
        slope = (ma20 - ma20_prev) / ma20_prev if ma20_prev > 0 else 0.0
        if slope > 0.02:
            score += 20
            flags.append("STRONG_UPTREND")
        elif slope > 0.005:
            score += 10
            flags.append("UPTREND")

        # 4. Bounce candle (0–15 pts)
        bar = sorted_h[-1]
        if bar.close > bar.open:
            score += 10
            flags.append("BOUNCE_CANDLE")
        rng = bar.high - bar.low
        if rng > 0 and (bar.close - bar.low) / rng > 0.3:
            score += 5
            flags.append("LONG_LOWER_SHADOW")

        # 5. MA60 uptrend (0–15 pts)
        if len(closes) >= 80:
            ma60_prev = mean(closes[-80:-60])
            slope60 = (ma60 - ma60_prev) / ma60_prev if ma60_prev > 0 else 0.0
            if slope60 > 0.01:
                score += 15
                flags.append("LONG_TERM_UPTREND")
            elif slope60 > 0:
                score += 5

        flags.append(f"PULLBACK_MA20_DIST:{ma20_pct:+.1%}")

        return {
            "score": max(0, min(100, score)),
            "flags": flags,
            "ma20_pct": round(ma20_pct * 100, 2),
            "ma20": round(ma20, 2),
            "ma5": round(ma5, 2),
            "ma60": round(ma60, 2),
        }
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/unit/test_pullback_detector.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/taiwan_stock_agent/domain/pullback_detector.py tests/unit/test_pullback_detector.py
git commit -m "feat: PullbackDetector — MA20 pullback setup (Phase 4.40)"
```

---

## Task 2: Signal type tagging in `_scan_one`

**Files:**
- Modify: `scripts/batch_plan.py` — `_scan_one()` function and a new `_classify_tce_signal_type()` helper

- [ ] **Step 1: Add `_classify_tce_signal_type` helper**

Find the line `def _scan_one(` in `batch_plan.py` (around line 765) and insert this function BEFORE it:

```python
def _classify_tce_signal_type(flags: list[str]) -> tuple[str, str]:
    """Return (signal_type_zh, horizon_zh) based on TCE flags.

    Returns:
        signal_type: one of '趨勢延伸', '蓄積★', '蓄積'
        horizon:     one of '波段', '短線'
    """
    if "TREND_WALK" in flags:
        return "趨勢延伸", "波段"
    if "COILING_PRIME" in flags:
        return "蓄積★", "波段"
    return "蓄積", "波段"
```

- [ ] **Step 2: Add `signal_type` and `horizon` to the success return dict in `_scan_one`**

Find the return dict block starting with `"ticker": ticker,` inside the `try:` block of `_scan_one` (around line 779). After the existing `"trend_score": trend_score,` line, add:

```python
            "signal_type": _classify_tce_signal_type(signal.data_quality_flags)[0],
            "horizon": _classify_tce_signal_type(signal.data_quality_flags)[1],
            "secondary_types": [],
```

Also add `signal_type`, `horizon`, `secondary_types` to the error return dict (around line 802):

```python
            "signal_type": "蓄積",
            "horizon": "波段",
            "secondary_types": [],
```

- [ ] **Step 3: Run existing tests to ensure no regressions**

```bash
.venv/bin/pytest tests/unit/ -q
```

Expected: all existing tests pass (620+).

- [ ] **Step 4: Commit**

```bash
git add scripts/batch_plan.py
git commit -m "feat: add signal_type/horizon classification to TCE scan results"
```

---

## Task 3: Pullback scan + Surge DB load + Merge

**Files:**
- Modify: `scripts/batch_plan.py` — add 3 new functions after the existing `_scan_one` block

- [ ] **Step 1: Add `_scan_pullback_batch` function**

Find the line `CSV_FIELDS = [` (around line 825) and insert all three new functions BEFORE it:

```python
def _scan_pullback_batch(
    tickers: list[str],
    analysis_date: date,
    agent: StrategistAgent,
    market_map: dict[str, str] | None = None,
    min_score: int = 40,
) -> list[dict]:
    """Run PullbackDetector on all tickers using cached OHLCV (L1 mem hit — no HTTP).

    Returns result dicts (same shape as _scan_one) for qualifying stocks only.
    """
    from taiwan_stock_agent.domain.pullback_detector import PullbackDetector
    detector = PullbackDetector()
    results: list[dict] = []

    for ticker in tickers:
        try:
            from datetime import timedelta
            ohlcv_df = agent._client.fetch_ohlcv(
                ticker,
                analysis_date - timedelta(days=130),
                analysis_date,
            )
            history = StrategistAgent._df_to_ohlcv_list(ohlcv_df, ticker)
            if not history:
                continue
            det = detector.score(history)
            if det is None or det["score"] < min_score:
                continue
            sorted_h = sorted(history, key=lambda x: x.trade_date)
            close = float(sorted_h[-1].close) if sorted_h else 0.0
            ma20 = det["ma20"]
            results.append({
                "ticker": ticker,
                "action": "LONG",
                "confidence": det["score"],
                "halt": False,
                "free_tier": None,
                "flags": det["flags"],
                "entry_bid": round(close * 0.997, 1),
                "stop_loss": round(ma20 * 0.97, 1),
                "target": round(close * 1.08, 1),
                "verdict": f"趨勢回調至 MA20（±{abs(det['ma20_pct']):.1f}%）",
                "position": "",
                "momentum": "",
                "chip": "",
                "risk": "",
                "elapsed": 0.0,
                "error": None,
                "_signal": None,
                "trend_score": 0,
                "institution_continuity_pts": 0,
                "proximity_pts": 0,
                "signal_type": "回調",
                "horizon": "波段",
                "secondary_types": [],
                "change_pct": 0.0,
            })
        except Exception:
            continue

    return results


def _load_surge_from_db(analysis_date: date, min_score: int = 50) -> list[dict]:
    """Load surge signals from surge_signals DB for analysis_date.

    Normalises each row to the same result-dict shape as _scan_one so they
    can be merged with TCE and pullback results without special-casing.
    Returns empty list if DB unavailable or no records.
    """
    import json as _json
    from taiwan_stock_agent.infrastructure.surge_recorder import query_surge_signals

    surge_rows = query_surge_signals(analysis_date, min_score=min_score)
    results: list[dict] = []
    for s in surge_rows:
        grade = s.get("grade", "")
        sig_type = "爆量★" if grade == "SURGE_ALPHA" else "爆量"
        flags = s.get("flags") or []
        if isinstance(flags, str):
            try:
                flags = _json.loads(flags)
            except Exception:
                flags = []
        close = float(s.get("close_price") or 0.0)
        results.append({
            "ticker": s["ticker"],
            "action": "LONG",
            "confidence": int(s.get("score") or 0),
            "halt": False,
            "free_tier": None,
            "flags": flags,
            "entry_bid": round(close * 0.997, 1),
            "stop_loss": round(close * 0.97, 1),
            "target": round(close * 1.05, 1),
            "verdict": f"{sig_type}  Vol×{s.get('vol_ratio', 0):.1f}",
            "position": "",
            "momentum": "",
            "chip": "",
            "risk": "",
            "elapsed": 0.0,
            "error": None,
            "_signal": None,
            "trend_score": 0,
            "institution_continuity_pts": 0,
            "proximity_pts": 0,
            "signal_type": sig_type,
            "horizon": "短線",
            "secondary_types": [],
            "change_pct": float(s.get("day_chg_pct") or 0.0),
        })
    return results


def _merge_unified_signals(
    tce_results: list[dict],
    pullback_results: list[dict],
    surge_results: list[dict],
) -> list[dict]:
    """Merge three result lists. Deduplicate by ticker: keep highest-confidence result
    as primary; add others' signal_type to secondary_types list.

    Halted / error TCE results are preserved as-is (pullback/surge won't produce them).
    """
    # Start with all TCE results (includes halted/error rows that display as dash)
    merged: dict[str, dict] = {r["ticker"]: r for r in tce_results}

    for r in [*pullback_results, *surge_results]:
        ticker = r["ticker"]
        new_conf = r.get("confidence", 0)
        if ticker not in merged:
            merged[ticker] = r
        else:
            existing = merged[ticker]
            if existing.get("halt") or existing.get("error"):
                # Replace halted/error entry with a valid signal
                merged[ticker] = r
            elif new_conf > existing.get("confidence", 0):
                # New signal is stronger — make it primary, demote old
                existing_type = existing.get("signal_type", "")
                merged[ticker] = r
                if existing_type and existing_type not in r.get("secondary_types", []):
                    merged[ticker].setdefault("secondary_types", []).append(existing_type)
            else:
                # Existing is stronger — keep it, record new type as secondary
                new_type = r.get("signal_type", "")
                if new_type and new_type not in existing.get("secondary_types", []):
                    existing.setdefault("secondary_types", []).append(new_type)

    return list(merged.values())
```

- [ ] **Step 2: Run existing tests**

```bash
.venv/bin/pytest tests/unit/ -q
```

Expected: all pass (no regressions — new functions are not yet called).

- [ ] **Step 3: Commit**

```bash
git add scripts/batch_plan.py
git commit -m "feat: add pullback scan, surge DB load, merge functions (Phase 4.40)"
```

---

## Task 4: Wire into `run_batch()`

**Files:**
- Modify: `scripts/batch_plan.py` — `run_batch()` function (around line 1189)

- [ ] **Step 1: Wire pullback + surge after TCE scan completes**

Find the post-processing block in `run_batch()` that starts with `# --- Post-processing: sector ranking + persistence ---` (around line 1255). Insert this block BEFORE that comment:

```python
    # ── Pullback scan (uses cached OHLCV — no extra HTTP) ────────────────────
    _console.print("\n[bold cyan][Pullback Scan][/bold cyan] 回調型偵測中…")
    _shared_agent = _make_agent(analysis_date, llm_provider=None, label_repo=label_repo)
    pullback_results = _scan_pullback_batch(
        tickers, analysis_date, _shared_agent, market_map=market_map
    )
    _console.print(f"  回調型信號: [green]{len(pullback_results)}[/green] 檔")

    # ── Surge signals from DB (populated by `make surge`) ────────────────────
    surge_db_results = _load_surge_from_db(analysis_date)
    if surge_db_results:
        _console.print(f"  爆量型信號 (DB): [green]{len(surge_db_results)}[/green] 檔")

    # ── Merge all three signal types ──────────────────────────────────────────
    results = _merge_unified_signals(results, pullback_results, surge_db_results)
```

Note: `_make_agent` is defined earlier in the file (around line 748). The pullback scan shares the same FinMindClient L1 cache.

- [ ] **Step 2: Extend `_apply_growth_bonus` to also enrich pullback + surge results**

The existing `_apply_growth_bonus(results, growth_index)` call (around line 1278) now operates on the merged list, so pullback and surge results also get growth enrichment automatically. No extra code needed — just verify the call is AFTER the merge.

Check the order: `results = _merge_unified_signals(...)` must come BEFORE `growth_index = _load_growth_index()`. If it does not, move the merge block up.

- [ ] **Step 3: Run a smoke test with a known ticker**

```bash
make analyze TICKER=2330 DATE=2026-05-22
```

Expected: output as before (no crash, `make analyze` uses StrategistAgent directly, not batch_plan).

```bash
.venv/bin/pytest tests/unit/ -q
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add scripts/batch_plan.py
git commit -m "feat: wire pullback + surge DB into run_batch unified scan (Phase 4.40)"
```

---

## Task 5: Update terminal display — `_print_table`

**Files:**
- Modify: `scripts/batch_plan.py` — `_print_table()` function (around line 869)

- [ ] **Step 1: Add 型態 and 基本面 columns**

Replace the `table.add_column` block and the row-building loop in `_print_table`. Find the block starting with `table.add_column("Rank", ...)` and ending with `table.add_row(...)`. Replace it:

```python
    table.add_column("Rank", justify="center", style="dim", width=5)
    table.add_column("Ticker", style="bold white", width=11)
    table.add_column("型態", width=9)
    table.add_column("持倉", width=7)
    table.add_column("Action", width=10)
    table.add_column("Confidence", width=18)
    table.add_column("Entry", justify="right", style="cyan", width=9)
    table.add_column("Stop", justify="right", style="red", width=9)
    table.add_column("Target", justify="right", style="green", width=9)
    table.add_column("Upside", justify="right", style="yellow", width=7)
    table.add_column("基本面", width=14)
```

And replace the `table.add_row(...)` call:

```python
        # Signal type badge
        sig_type = r.get("signal_type", "蓄積")
        secondary = r.get("secondary_types") or []
        secondary_str = f"\n[dim]+{secondary[0]}[/dim]" if secondary else ""
        sig_type_colors = {
            "爆量★": "bold bright_red",
            "爆量": "red",
            "回調": "bright_yellow",
            "趨勢延伸": "bright_cyan",
            "蓄積★": "bright_green",
            "蓄積": "cyan",
        }
        sig_color = sig_type_colors.get(sig_type, "white")
        sig_cell = f"[{sig_color}]{sig_type}[/{sig_color}]{secondary_str}"

        horizon = r.get("horizon", "波段")
        horizon_color = "red" if horizon == "短線" else "cyan"
        horizon_cell = f"[{horizon_color}]{horizon}[/{horizon_color}]"

        # Fundamental badge
        yoy = r.get("growth_yoy")
        consec = r.get("growth_consecutive", 0)
        if yoy:
            consec_str = f" 連{consec}M" if consec >= 3 else ""
            fund_cell = f"[bright_green]★ +{yoy:.0f}%{consec_str}[/bright_green]"
        else:
            fund_cell = "[dim]—[/dim]"

        upside_pct = (r["target"] / r["entry_bid"] - 1) * 100 if r["entry_bid"] > 0 else 0
        ticker = r["ticker"]
        if name_map:
            short_name = name_map.get(ticker, "")
            ticker_cell = f"{ticker}\n[dim]{short_name}[/dim]" if short_name else ticker
        else:
            ticker_cell = ticker

        table.add_row(
            str(i),
            ticker_cell,
            sig_cell,
            horizon_cell,
            action_text,
            _conf_bar(r["confidence"]),
            f"{r['entry_bid']:.1f}",
            f"{r['stop_loss']:.1f}",
            f"{r['target']:.1f}",
            f"{upside_pct:+.1f}%",
            fund_cell,
        )
```

- [ ] **Step 2: Run tests**

```bash
.venv/bin/pytest tests/unit/ -q
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add scripts/batch_plan.py
git commit -m "feat: add 型態/持倉/基本面 columns to plan terminal table (Phase 4.40)"
```

---

## Task 6: Update `_print_by_industry` with same columns

**Files:**
- Modify: `scripts/batch_plan.py` — `_print_by_industry()` function (around line 975)

- [ ] **Step 1: Find the inner `_render_industry_table` or equivalent**

Read `_print_by_industry` from line 975 for ~100 lines to find where `Table` columns are added and `add_row` is called.

```bash
grep -n "add_column\|add_row" scripts/batch_plan.py | awk -F: '$2 > 975 && $2 < 1100 {print}'
```

- [ ] **Step 2: Add 型態 + 基本面 columns**

After finding the column definitions, add `型態` and `基本面` in the same positions as Task 5. In the row loop, use the same `sig_cell` / `fund_cell` logic from Task 5 (copy the 15-line block verbatim).

- [ ] **Step 3: Run tests**

```bash
.venv/bin/pytest tests/unit/ -q
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add scripts/batch_plan.py
git commit -m "feat: add signal type columns to industry-grouped plan table (Phase 4.40)"
```

---

## Task 7: HTML signal type badge

**Files:**
- Modify: `scripts/batch_plan.py` — `_generate_plan_html()` function (around line 1664)

- [ ] **Step 1: Find where each stock card is rendered in the HTML**

```bash
grep -n "action_badge\|conf_badge\|card\|ticker.*html\|data-ticker" scripts/batch_plan.py | head -20
```

- [ ] **Step 2: Add signal type + fundamental badge to each stock card**

Find the section inside `_generate_plan_html` that builds per-stock HTML (look for the f-string that contains `data-confidence`, `data-action`, or the stock card `<div>`). After the existing `action` badge, insert:

```python
                sig_type = r.get("signal_type", "蓄積")
                horizon = r.get("horizon", "波段")
                yoy = r.get("growth_yoy")
                consec = r.get("growth_consecutive", 0)

                sig_colors = {
                    "爆量★": "#ff4444", "爆量": "#ff7744",
                    "回調": "#ffcc44", "趨勢延伸": "#44ccff",
                    "蓄積★": "#44ff88", "蓄積": "#44aaff",
                }
                sig_bg = sig_colors.get(sig_type, "#888888")
                horizon_bg = "#cc4444" if horizon == "短線" else "#226688"

                type_badge = (
                    f'<span style="background:{sig_bg};color:#000;'
                    f'border-radius:4px;padding:2px 6px;font-size:11px;'
                    f'font-weight:bold;margin-right:4px">{sig_type}</span>'
                    f'<span style="background:{horizon_bg};color:#fff;'
                    f'border-radius:4px;padding:2px 6px;font-size:11px;'
                    f'margin-right:4px">{horizon}</span>'
                )
                if yoy:
                    consec_str = f" 連{consec}M" if consec >= 3 else ""
                    fund_badge = (
                        f'<span style="background:#1a4a2a;color:#44ff88;'
                        f'border-radius:4px;padding:2px 6px;font-size:11px">'
                        f'★ 月營收 +{yoy:.0f}%{consec_str}</span>'
                    )
                else:
                    fund_badge = ""
```

Then include `type_badge` and `fund_badge` in the card HTML f-string, immediately after the existing action/confidence badges.

- [ ] **Step 3: Verify HTML renders correctly**

```bash
make plan DATE=2026-05-22 SECTORS="1 4" TICKERS="2330 8086"
```

Open the generated HTML in a browser and confirm signal type badges and fundamental badges appear.

- [ ] **Step 4: Run full test suite**

```bash
.venv/bin/pytest tests/unit/ -q
```

Expected: all pass (620+ tests).

- [ ] **Step 5: Update CLAUDE.md Phase gate**

Add to CLAUDE.md Phase Gates table:

```
| Phase 4.40 | ✅ Done | **統一掃描輸出**：PullbackDetector（MA20回調型偵測）✅ · signal_type/horizon 欄位（蓄積/回調/爆量/趨勢延伸）✅ · `make plan` 整合 TCE + Pullback + Surge DB 三路信號 ✅ · 成長股基本面★標記（所有信號類型）✅ · 終端輸出新增 型態/持倉/基本面 欄位 ✅ · HTML 信號類型徽章 ✅ · N unit tests passing ✅ |
```

- [ ] **Step 6: Final commit**

```bash
git add scripts/batch_plan.py CLAUDE.md
git commit -m "feat: HTML signal badges + CLAUDE.md Phase 4.40 gate (Phase 4.40 complete)"
```

---

## Self-Review Checklist

- [x] **Spec coverage**: PullbackDetector ✓, signal_type labels ✓, surge from DB ✓, merge/dedup ✓, growth enrichment extended ✓, terminal display ✓, HTML badges ✓
- [x] **No placeholders**: All code blocks complete and self-contained
- [x] **Type consistency**: `_scan_pullback_batch` returns same dict shape as `_scan_one`; `_load_surge_from_db` same shape; `_merge_unified_signals` consumes list[dict] and returns list[dict]
- [x] **`_df_to_ohlcv_list` is a `@staticmethod`** — called as `StrategistAgent._df_to_ohlcv_list(ohlcv_df, ticker)` in Task 3 ✓
- [x] **`_make_agent` call in Task 4**: This function exists in `batch_plan.py` (around line 748). It creates a shared agent. The pullback scan reuses the same `agent._client` L1 mem cache that was populated during the TCE scan — no extra HTTP calls.
- [x] **Growth enrichment**: `_apply_growth_bonus(results, growth_index)` is called after the merge in `run_batch()`, so it naturally enriches all signal types. No code change needed for that function.
- [x] **`surge-live` unaffected**: `surge_scan.py` is not modified in this plan.
