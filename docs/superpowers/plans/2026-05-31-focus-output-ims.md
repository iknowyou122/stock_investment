# Focus Output + IMS Ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 500+-stock terminal dump with a focused two-tier daily shortlist: CONVICTION top 10 (sorted by Institutional Momentum Score) + WATCHLIST top 20 (sorted by confidence), defaulting to min_confidence=58.

**Architecture:** Three changes work together: (1) `_scan_one` carries 8 additional chip breakdown fields so IMS can be computed without re-reading the signal; (2) `_compute_ims()` turns those fields into a single smart-money composite; (3) `_print_focus_list()` replaces the terminal industry view with a two-tier ranked table. HTML output and DB recording are unchanged.

**Tech Stack:** Python 3.10+, Rich (already imported), batch_plan.py (single-file change + Makefile tweak)

---

## File Map

| File | Change |
|------|--------|
| `scripts/batch_plan.py` | Add IMS fields to `_scan_one` and `_scan_early_accum_batch`; add `_compute_ims()`, `_ims_bar()`; add `_print_focus_list()`; modify `run_batch()` to call focus view; add `--by-industry` arg |
| `Makefile` | Raise `MIN_CONF ?= 40` → `58` |

---

### Task 1: Add IMS breakdown fields to `_scan_one`

**Files:**
- Modify: `scripts/batch_plan.py:797-838` (`_scan_one` return dict)

- [ ] **Step 1: Write a test for the new fields existing**

Add to `tests/test_batch_plan_ims.py`:
```python
"""Tests for IMS fields in _scan_one result dict and _compute_ims()."""
from unittest.mock import MagicMock, patch
import pytest


def _make_result(**kwargs) -> dict:
    """Minimal valid result dict for IMS tests."""
    base = {
        "ticker": "2330",
        "action": "LONG",
        "confidence": 70.0,
        "halt": False,
        "error": None,
        "flags": [],
        "entry_bid": 850.0,
        "stop_loss": 810.0,
        "target": 980.0,
        "signal_type": "蓄積",
        # IMS fields
        "stealth_accum_composite_pts": 0.0,
        "inst_synergy_pts": 0.0,
        "foreign_trend_pts": 0.0,
        "vol_asymmetry_pts": 0.0,
        "chip_cleanliness_pts": 0.0,
        "large_2w_trend_pts": 0.0,
        "inst_accel_3d_pts": 0.0,
        "obv_stealth_pts": 0.0,
    }
    base.update(kwargs)
    return base


def test_ims_fields_present_in_result():
    """IMS fields must all be present in result dict (even if 0)."""
    from scripts.batch_plan import _compute_ims  # import after adding

    r = _make_result()
    # Must not raise KeyError
    score = _compute_ims(r)
    assert score == 0.0


def test_ims_weights_computed_correctly():
    from scripts.batch_plan import _compute_ims

    r = _make_result(
        stealth_accum_composite_pts=10.0,   # ×2.5 = 25.0
        inst_synergy_pts=5.0,               # ×2.0 = 10.0
        foreign_trend_pts=4.0,              # ×1.5 = 6.0
        large_2w_trend_pts=3.0,             # ×1.5 = 4.5
        chip_cleanliness_pts=7.0,           # ×1.0 = 7.0
        inst_accel_3d_pts=2.0,              # ×1.0 = 2.0
        vol_asymmetry_pts=4.0,              # ×1.0 = 4.0
        obv_stealth_pts=3.0,               # ×1.0 = 3.0
    )
    expected = 25.0 + 10.0 + 6.0 + 4.5 + 7.0 + 2.0 + 4.0 + 3.0  # 61.5
    assert _compute_ims(r) == pytest.approx(expected)


def test_ims_early_accum_bonus():
    """Early accumulation signal types get +5 bonus."""
    from scripts.batch_plan import _compute_ims

    for sig in ("法人建倉", "籌碼轉移", "VCP", "旗形"):
        r = _make_result(signal_type=sig)
        assert _compute_ims(r) == pytest.approx(5.0), f"Expected +5 bonus for {sig}"


def test_ims_no_bonus_for_regular_signals():
    from scripts.batch_plan import _compute_ims

    for sig in ("蓄積", "蓄積★", "趨勢延伸", "回調", "爆量"):
        r = _make_result(signal_type=sig)
        assert _compute_ims(r) == pytest.approx(0.0), f"No bonus expected for {sig}"


def test_ims_missing_fields_default_zero():
    """_compute_ims must not crash when fields are missing (legacy results)."""
    from scripts.batch_plan import _compute_ims

    r = {"ticker": "2330", "signal_type": "蓄積"}
    assert _compute_ims(r) == 0.0
```

- [ ] **Step 2: Run the test to confirm it fails (function not yet defined)**

```bash
cd /Users/howardhuang/Documents/git/stock_investment
python -m pytest tests/test_batch_plan_ims.py -v 2>&1 | head -30
```
Expected: `ImportError` or `ModuleNotFoundError` for `_compute_ims`.

- [ ] **Step 3: Add `_compute_ims` and `_ims_bar` functions to `batch_plan.py`**

Find the block after `_print_score_health` (around line 455) and insert before `_apply_catalyst_filter`:

```python
# ── Institutional Momentum Score ────────────────────────────────────────────

_IMS_EARLY_TYPES = frozenset(["法人建倉", "籌碼轉移", "VCP", "旗形"])


def _compute_ims(r: dict) -> float:
    """Institutional Momentum Score — weighted composite of smart-money accumulation signals.

    Higher score = institutional players are quietly building a position.
    Early-accumulation signal types (InstAccum, ChipTransfer, VCP, HTF) get a +5 bonus
    because they are the primary target of the focus output.
    """
    early_bonus = 5.0 if r.get("signal_type") in _IMS_EARLY_TYPES else 0.0
    return (
        r.get("stealth_accum_composite_pts", 0.0) * 2.5
        + r.get("inst_synergy_pts", 0.0) * 2.0
        + r.get("foreign_trend_pts", 0.0) * 1.5
        + r.get("large_2w_trend_pts", 0.0) * 1.5
        + r.get("chip_cleanliness_pts", 0.0) * 1.0
        + r.get("inst_accel_3d_pts", 0.0) * 1.0
        + r.get("vol_asymmetry_pts", 0.0) * 1.0
        + r.get("obv_stealth_pts", 0.0) * 1.0
        + early_bonus
    )


def _ims_bar(ims: float) -> str:
    """Visual IMS bar scaled 0-60 → 0-10 blocks."""
    filled = max(0, min(10, round(ims / 6.0)))
    bar = "▮" * filled + "▯" * (10 - filled)
    if ims >= 30:
        color = "bright_magenta"
    elif ims >= 15:
        color = "magenta"
    else:
        color = "dim"
    return f"[{color}]{bar}[/{color}] [dim]{ims:.0f}[/dim]"
```

- [ ] **Step 4: Add the IMS breakdown fields to `_scan_one` result dict**

In `_scan_one`, locate the return dict (around line 800). After the existing lines:
```python
"institution_continuity_pts": breakdown_pts.get("institution_continuity_pts", 0),
"proximity_pts": breakdown_pts.get("proximity_pts", 0),
```
Add:
```python
"stealth_accum_composite_pts": breakdown_pts.get("stealth_accum_composite_pts", 0.0),
"inst_synergy_pts": breakdown_pts.get("inst_synergy_pts", 0.0),
"foreign_trend_pts": breakdown_pts.get("foreign_trend_pts", 0.0),
"vol_asymmetry_pts": breakdown_pts.get("vol_asymmetry_pts", 0.0),
"chip_cleanliness_pts": breakdown_pts.get("chip_cleanliness_pts", 0.0),
"large_2w_trend_pts": breakdown_pts.get("large_2w_trend_pts", 0.0),
"inst_accel_3d_pts": breakdown_pts.get("inst_accel_3d_pts", 0.0),
"obv_stealth_pts": breakdown_pts.get("obv_stealth_pts", 0.0),
```

- [ ] **Step 5: Run the test to confirm it passes**

```bash
cd /Users/howardhuang/Documents/git/stock_investment
python -m pytest tests/test_batch_plan_ims.py -v
```
Expected: All 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_batch_plan_ims.py scripts/batch_plan.py
git commit -m "feat: add IMS breakdown fields to _scan_one + _compute_ims/_ims_bar helpers"
```

---

### Task 2: Add IMS zero-fields to `_scan_early_accum_batch` result dict

Early accum results need the same IMS keys (as 0.0) so `_compute_ims` can read them cleanly and the early-type bonus fires.

**Files:**
- Modify: `scripts/batch_plan.py` — `_scan_early_accum_batch` result dict (around line 1200)

- [ ] **Step 1: Locate the result dict in `_scan_early_accum_batch`**

Find the block that ends with:
```python
"institution_continuity_pts": 0,
"proximity_pts": 0,
"signal_type": best["signal_type"],
```

- [ ] **Step 2: Add IMS fields with 0.0 defaults**

After `"proximity_pts": 0,` add:
```python
"stealth_accum_composite_pts": 0.0,
"inst_synergy_pts": 0.0,
"foreign_trend_pts": 0.0,
"vol_asymmetry_pts": 0.0,
"chip_cleanliness_pts": 0.0,
"large_2w_trend_pts": 0.0,
"inst_accel_3d_pts": 0.0,
"obv_stealth_pts": 0.0,
```

- [ ] **Step 3: Raise `min_score` in `_scan_early_accum_batch` from 35 → 45**

Find:
```python
def _scan_early_accum_batch(
    tickers: list[str],
    analysis_date: date,
    agent: "StrategistAgent",
    market_map: dict[str, str] | None = None,
    min_score: int = 35,
) -> list[dict]:
```
Change `min_score: int = 35` → `min_score: int = 45`.

- [ ] **Step 4: Run full test suite to confirm no regression**

```bash
cd /Users/howardhuang/Documents/git/stock_investment
python -m pytest tests/ -x -q 2>&1 | tail -10
```
Expected: same pass count as before (671+), 0 new failures.

- [ ] **Step 5: Commit**

```bash
git add scripts/batch_plan.py
git commit -m "feat: add IMS zero-fields to early accum results; raise min_score 35→45"
```

---

### Task 3: Add `_print_focus_list` function

**Files:**
- Modify: `scripts/batch_plan.py` — insert after `_print_by_industry` (around line 1600)

- [ ] **Step 1: Add `_print_focus_list` function**

Insert the function right after `_print_by_industry` ends (before `def _run_phase`):

```python
def _print_focus_list(
    results: list[dict],
    top_conviction: int,
    top_watchlist: int,
    min_confidence: float,
    scan_date: str = "",
    name_map: dict[str, str] | None = None,
) -> None:
    """Two-tier focused output: CONVICTION (IMS-ranked top 10) + WATCHLIST (conf-ranked top 20).

    CONVICTION = highest Institutional Momentum Score — surfaces quiet accumulation before breakout.
    WATCHLIST  = remaining valid results sorted by overall confidence score.
    """
    valid = [
        r for r in results
        if not r.get("halt") and r.get("error") is None
        and r.get("confidence", 0) >= min_confidence
        and "NO_CATALYST" not in (r.get("flags") or [])
    ]
    if not valid:
        _console.print(Panel(
            f"[dim]無符合條件的標的 (min_confidence={min_confidence})[/dim]",
            border_style="yellow",
        ))
        return

    # Compute IMS for all candidates
    for r in valid:
        r["_ims"] = _compute_ims(r)

    # CONVICTION: top N by IMS (ties broken by confidence)
    conviction_pool = sorted(valid, key=lambda r: (r["_ims"], r["confidence"]), reverse=True)
    conviction = conviction_pool[:top_conviction]
    conviction_tickers = {r["ticker"] for r in conviction}

    # WATCHLIST: remaining sorted by confidence
    watchlist_pool = [r for r in valid if r["ticker"] not in conviction_tickers]
    watchlist_pool.sort(key=lambda r: r["confidence"], reverse=True)
    watchlist = watchlist_pool[:top_watchlist]

    name_m = name_map or {}

    def _row(r: dict) -> tuple:
        ticker = r["ticker"]
        short = name_m.get(ticker, "")
        ticker_cell = f"{ticker}\n[dim]{short}[/dim]" if short else ticker
        sig_cell, horizon_cell, fund_cell = _make_signal_cells(r)
        action_str = r["action"] + ("*" if r.get("free_tier") else "")
        action_cell = Text.from_markup(
            f"[{_action_style(r['action'])}]{action_str}[/{_action_style(r['action'])}]"
        )
        entry = r.get("entry_bid", 0.0)
        stop  = r.get("stop_loss", 0.0)
        tgt   = r.get("target", 0.0)
        up    = (tgt / entry - 1) * 100 if entry > 0 else 0.0
        return (
            ticker_cell, sig_cell, horizon_cell, action_cell,
            _conf_bar(r["confidence"]),
            _ims_bar(r["_ims"]),
            f"{entry:.1f}", f"{stop:.1f}", f"{tgt:.1f}", f"{up:+.1f}%",
            fund_cell,
        )

    _COLS = [
        ("Rank",       "center", "dim",   5),
        ("Ticker",     "left",   "bold white", 11),
        ("型態",       "left",   "white", 10),
        ("持倉",       "left",   "white",  7),
        ("Action",     "left",   "white", 10),
        ("Confidence", "left",   "white", 18),
        ("IMS 動能",   "left",   "white", 18),
        ("Entry",      "right",  "cyan",   9),
        ("Stop",       "right",  "red",    9),
        ("Target",     "right",  "green",  9),
        ("Upside",     "right",  "yellow", 7),
        ("基本面",     "left",   "white", 14),
    ]

    date_str = f"  {scan_date}" if scan_date else ""

    # ── CONVICTION section ─────────────────────────────────────────────────
    if conviction:
        _console.print(
            f"\n[bold bright_magenta]▶ CONVICTION{date_str}  "
            f"法人動能最強 {len(conviction)} 檔[/bold bright_magenta]"
            f"  [dim]IMS 由高→低排序[/dim]"
        )
        ct = Table(
            box=box.ROUNDED, show_header=True,
            header_style="bold white on dark_blue",
            border_style="magenta", show_lines=True,
        )
        for name, justify, _, width in _COLS:
            ct.add_column(name, justify=justify, width=width)
        for i, r in enumerate(conviction, 1):
            ct.add_row(str(i), *_row(r))
        _console.print(ct)

    # ── WATCHLIST section ──────────────────────────────────────────────────
    if watchlist:
        _console.print(
            f"\n[bold cyan]▶ WATCHLIST{date_str}  "
            f"信心 {min_confidence:.0f}+ 觀察 {len(watchlist)} 檔[/bold cyan]"
            f"  [dim]信心分排序[/dim]"
        )
        wt = Table(
            box=box.SIMPLE, show_header=True,
            header_style="bold dim", show_lines=False, padding=(0, 1),
        )
        for name, justify, _, width in _COLS:
            wt.add_column(name, justify=justify, width=width)
        for i, r in enumerate(watchlist, 1):
            wt.add_row(str(i), *_row(r))
        _console.print(wt)

    total_shown = len(conviction) + len(watchlist)
    _console.print(
        f"\n[dim]  顯示 {total_shown} 檔  "
        f"（CONVICTION {len(conviction)} + WATCHLIST {len(watchlist)}）"
        f"  全部通過門檻: {len(valid)} 檔[/dim]"
    )
```

- [ ] **Step 2: Run import check (no syntax error)**

```bash
cd /Users/howardhuang/Documents/git/stock_investment
python -c "from scripts.batch_plan import _print_focus_list, _compute_ims, _ims_bar; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/batch_plan.py
git commit -m "feat: add _print_focus_list two-tier output (CONVICTION+WATCHLIST)"
```

---

### Task 4: Wire `_print_focus_list` into `run_batch` + add `--by-industry` flag

**Files:**
- Modify: `scripts/batch_plan.py` — `run_batch()` signature and call site; argparse section

- [ ] **Step 1: Add `by_industry: bool = False` parameter to `run_batch`**

Find:
```python
def run_batch(
    tickers: list[str],
    analysis_date: date,
    top: int,
    min_confidence: int,
    workers: int,
    llm_provider=None,
    llm_top: int | None = None,
    label_repo=None,
    industry_map: dict[str, str] | None = None,
    save_db: bool = True,
    name_map: dict[str, str] | None = None,
    market_map: dict[str, str] | None = None,
    sort_by: str = "trend",
) -> None:
```
Change to:
```python
def run_batch(
    tickers: list[str],
    analysis_date: date,
    top: int,
    min_confidence: int,
    workers: int,
    llm_provider=None,
    llm_top: int | None = None,
    label_repo=None,
    industry_map: dict[str, str] | None = None,
    save_db: bool = True,
    name_map: dict[str, str] | None = None,
    market_map: dict[str, str] | None = None,
    sort_by: str = "trend",
    by_industry: bool = False,
) -> None:
```

- [ ] **Step 2: Replace the terminal output block in `run_batch`**

Find the block at the end of `run_batch` (after the rotation bonus code) that looks like:
```python
    if industry_map:
        _print_by_industry(
            results,
            top,
            min_confidence,
            scan_date=str(analysis_date),
            name_map=name_map,
            industry_map=industry_map,
        )
    else:
        _print_table(
            results,
            top,
            min_confidence,
            scan_date=str(analysis_date),
            name_map=name_map,
            sort_by=sort_by,
        )
```
Replace with:
```python
    if by_industry and industry_map:
        _print_by_industry(
            results,
            top,
            min_confidence,
            scan_date=str(analysis_date),
            name_map=name_map,
            industry_map=industry_map,
        )
    elif by_industry:
        _print_table(
            results,
            top,
            min_confidence,
            scan_date=str(analysis_date),
            name_map=name_map,
            sort_by=sort_by,
        )
    else:
        _print_focus_list(
            results,
            top_conviction=10,
            top_watchlist=20,
            min_confidence=min_confidence,
            scan_date=str(analysis_date),
            name_map=name_map,
        )
```

- [ ] **Step 3: Add `--by-industry` argparse flag**

In the argparse section (near line 3290), after the `--sort-by` argument, add:
```python
    parser.add_argument(
        "--by-industry",
        action="store_true",
        help="按產業分組顯示所有結果（舊行為）；預設為 CONVICTION+WATCHLIST 焦點清單",
    )
```

- [ ] **Step 4: Pass `by_industry` to `run_batch` in the main call**

Find the `run_batch(...)` call in `main()`. It currently passes many keyword args. Add `by_industry=args.by_industry` to that call.

The call site looks like:
```python
    run_batch(
        tickers=tickers,
        analysis_date=analysis_date,
        top=args.top,
        min_confidence=args.min_confidence,
        workers=args.workers,
        llm_provider=llm_provider,
        llm_top=llm_top,
        label_repo=label_repo,
        industry_map=industry_map,
        save_db=True,
        name_map=name_map,
        market_map=market_map,
        sort_by=args.sort_by,
    )
```
Add `, by_industry=args.by_industry` before the closing `)`.

- [ ] **Step 5: Smoke-test the CLI flag parses**

```bash
cd /Users/howardhuang/Documents/git/stock_investment
python scripts/batch_plan.py --help 2>&1 | grep -E "by-industry|min-confidence|focus"
```
Expected output includes `--by-industry`.

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests/ -x -q 2>&1 | tail -10
```
Expected: same pass count as before, 0 new failures.

- [ ] **Step 7: Commit**

```bash
git add scripts/batch_plan.py
git commit -m "feat: wire _print_focus_list into run_batch; add --by-industry flag"
```

---

### Task 5: Raise default thresholds

**Files:**
- Modify: `Makefile` — `MIN_CONF` default
- Modify: `scripts/batch_plan.py` — argparse `--min-confidence` default

- [ ] **Step 1: Raise `MIN_CONF` in Makefile**

Find line (around line 57):
```
MIN_CONF ?= 40
```
Change to:
```
MIN_CONF ?= 58
```

There is a second `MIN_CONF ?= 40` occurrence around line 96 (used by `backtest-compare`). Change that one too:
```
MIN_CONF    ?= 40
```
→
```
MIN_CONF    ?= 58
```

- [ ] **Step 2: Raise argparse default for `--min-confidence`**

Find in the argparse section:
```python
    parser.add_argument("--min-confidence", type=int, default=50, help="最低信心分數門檻（預設: 50）")
```
Change to:
```python
    parser.add_argument("--min-confidence", type=int, default=58, help="最低信心分數門檻（預設: 58）")
```

- [ ] **Step 3: Also update the `--top` default in argparse to 10 → 30 for `plan` mode**

Find:
```python
    parser.add_argument("--top", type=int, default=10, help="顯示前 N 名（預設: 10）")
```
Change to:
```python
    parser.add_argument("--top", type=int, default=30, help="顯示前 N 名（預設: 30）")
```

Note: `top` is only used by the old `--show` and `--by-industry` paths in the new code. `_print_focus_list` hardcodes `top_conviction=10` and `top_watchlist=20`, so this change only affects legacy/show modes.

- [ ] **Step 4: Confirm Makefile parses correctly**

```bash
cd /Users/howardhuang/Documents/git/stock_investment
make -n plan 2>&1 | head -5
```
Expected: no `--min-confidence` flag is passed (plan target does not inject it; argparse default of 58 applies).

- [ ] **Step 5: Run tests to confirm no regression**

```bash
python -m pytest tests/test_batch_plan_ims.py tests/ -x -q 2>&1 | tail -10
```
Expected: 0 failures.

- [ ] **Step 6: Commit**

```bash
git add Makefile scripts/batch_plan.py
git commit -m "feat: raise min_confidence default 50→58 and MIN_CONF Makefile default 40→58"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** IMS fields in `_scan_one` ✓, `_compute_ims` ✓, `_ims_bar` ✓, `_print_focus_list` ✓, `run_batch` wiring ✓, `--by-industry` flag ✓, threshold raises ✓, early accum min_score ✓
- [x] **No placeholders:** All code blocks are complete
- [x] **Type consistency:** `_compute_ims(r: dict) -> float` used consistently; `_ims_bar(ims: float) -> str` used in `_print_focus_list`
- [x] **IMS fields in early accum results:** Task 2 adds the 8 zero-default fields so `_compute_ims` never KeyErrors
- [x] **`run_batch` call site:** Task 4 step 4 adds `by_industry=args.by_industry` to the actual call
- [x] **Backward compat:** `--by-industry` flag preserves old industry-grouped behavior for users who need it
