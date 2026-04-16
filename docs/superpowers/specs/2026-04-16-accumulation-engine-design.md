# Accumulation Engine — Design Spec

**Date:** 2026-04-16  
**Status:** Approved  
**Author:** Howard Huang

---

## Problem Statement

The current Triple Confirmation Engine is a momentum engine: its gate requires volume surge (>1.2×), close near high, and other post-breakout signals. Stocks in the accumulation phase (蓄積) fail this gate by definition and are buried in the CAUTION/confidence=0 pile. The COILING detector (Phase 4.17) correctly identifies flags but never surfaces them — a comment in the code even notes "for watchlist surfacing" that was never implemented.

The engine's primary purpose should be finding stocks *about to* break out. Momentum continuation is a secondary concern. The current architecture inverts this priority.

---

## Solution: Two Parallel Tracks

`make plan` runs two independent engines and outputs two named lists:

1. **Momentum Track** (existing) — `scan_YYYY-MM-DD.csv` — LONG/WATCH/CAUTION
2. **Accumulation Track** (new) — `coil_YYYY-MM-DD.csv` — COIL_PRIME/COIL_MATURE/COIL_EARLY

Both tracks share the same ticker universe, OHLCV data, and institutional data fetched once (shared client, no duplicate API calls).

---

## Part 1: AccumulationEngine Detection Logic

### File
`src/taiwan_stock_agent/domain/accumulation_engine.py`

### New Indicator Implementations Required

Three technical indicators do not exist in the current codebase and must be implemented as static methods on `AccumulationEngine` (or in a shared `src/taiwan_stock_agent/domain/_indicators.py` module):

| Method | Signature | Notes |
|--------|-----------|-------|
| `_obv(history)` | `list[DailyOHLCV] → float \| None` | On-Balance Volume; return 5-day linear slope |
| `_atr(history, period=14)` | `list[DailyOHLCV] → float \| None` | True Range average over `period` days |
| `_atr_percentile(history, period=14, window=252)` | `list[DailyOHLCV] → float \| None` | ATR value as percentile of rolling `window`-day ATR history |
| `_kd_d(history, k_period=9, d_smooth=3)` | `list[DailyOHLCV] → float \| None` | Stochastic %D (slow); return `None` if insufficient history |

The existing `_calculate_bb`, `_rsi`, `_calculate_dmi` static methods on `TripleConfirmationEngine` are **not** to be imported into `AccumulationEngine` — duplicate if needed to maintain clean separation.

### Minimum History Requirement

Two factors (BB bandwidth percentile, ATR percentile) require a 252-trading-day window (~14 calendar months). The engine must:
1. Check `len(history) >= 252` before computing percentile-based factors.
2. If history is shorter: score those two factors as 0 pts (no partial credit), and append flag `ACCUM_SHORT_HISTORY:{n}` to the result flags.
3. The gate layer requires only 60 days of history (same as existing COILING detector).

### Gate Layer (4 mandatory conditions — all must pass)

| Gate | Condition | Rationale |
|------|-----------|-----------|
| G1 Trend floor | MA20 > MA60; MA20 today ≥ MA20 5 sessions ago | Mid-term uptrend; same computation as `_coiling_detect` G2 (lines 1700–1707 of triple_confirmation_engine.py) |
| G2 Not yet broken out | max(close[-10:]) < 60-day high × 1.03 | Exclude stocks already in breakout. Intentionally replaces `_coiling_detect` G5 (20-day high) with a wider 60-day window, and drops the 5-session pivot-range gate (G4) and platform-top gate (G6) from the existing detector — those conditions are too strict for early accumulation. |
| G3 Market regime | TAIEX regime ≠ downtrend | Same as existing COILING G3 |
| G4 Liquidity | 20-day avg turnover ≥ TSE 20M / TPEx 8M TWD | Same as existing liquidity gate |

### Scoring Factors (0–100 points, 15 factors across 3 dimensions)

#### Dimension A — Compression Pattern (型態壓縮)

| Factor | Max pts | Trigger |
|--------|---------|---------|
| BB bandwidth compression | 20 | BB width percentile vs 252-day history: <15% → 20pts; <30% → 10pts |
| Volume dry-up | 15 | 5-day avg vol vs 20-day avg vol: <70% → 15pts; <85% → 8pts |
| Price consolidation range | 15 | 20-day high/low spread: <5% → 15pts; <8% → 8pts |
| ATR contraction | 10 | 14-day ATR percentile vs 252-day history: <20% → 10pts; <35% → 5pts |
| Inside bar count | 5 | Count of inside bars in last 5 sessions: ≥3 → 5pts; ≥1 → 2pts |

#### Dimension B — Technical Confirmation (技術確認)

| Factor | Max pts | Trigger |
|--------|---------|---------|
| MA convergence | 10 | Max gap among MA5/MA10/MA20 as % of price: <2% → 10pts; <4% → 5pts |
| OBV trend | 8 | OBV 5-day slope > 0 while price flat (accumulation signal) → 8pts |
| KD low-range consolidation | 7 | KD-D < 30 and flat for ≥3 sessions → 7pts |
| Close above BB midline | 5 | Close > 20-day MA (BB midline) → 5pts (bulls in control) |

#### Dimension C — Chip Behavior (籌碼行為)

| Factor | Max pts | Trigger |
|--------|---------|---------|
| Institutional consecutive buy days | 20 | Foreign or trust: ≥5 days → 20pts; ≥3 days → 12pts; ≥1 day → 5pts. Uses `TWSEChipProxy.foreign_consecutive_buy_days` / `trust_consecutive_buy_days` — already pre-computed by `ChipProxyFetcher`, no additional API call needed. |
| Institutional net buy trend (10d) | 10 | Use `foreign_consecutive_buy_days >= 3` as proxy (data available without additional API calls). Full 10-day cumulative net buy requires a `ChipProxyFetcher.fetch_history(ticker, n_days=10)` extension — **deferred to Phase 4.21**. For Phase 4.20, award 10pts if `foreign_consecutive_buy_days >= 3`, 5pts if >= 1. |
| Up-day vs down-day volume structure | 8 | Avg volume on up-days > avg volume on down-days (last 10 sessions) → 8pts |
| Market-relative strength | 7 | On days TAIEX fell, stock fell less than half of TAIEX's decline (last 5 sessions) → 7pts |
| Price proximity to resistance | 5 | Close within 95–100% of 60-day high → 5pts (coiling just below ceiling) |
| Prior constructive advance | 5 | Close / min(close[-60:]) ≥ 1.15 → 5pts (cost basis support) |

**Total: 150 raw points, normalized to 0–100.**

### Signal Grades

| Grade | Score | Meaning |
|-------|-------|---------|
| `COIL_PRIME` | ≥ 70 | High-confidence accumulation: extreme BB squeeze + institutional buying |
| `COIL_MATURE` | 50–69 | Mature setup: most conditions in place |
| `COIL_EARLY` | 35–49 | Early formation: worth monitoring |
| No output | < 35 | Does not qualify |

---

## Part 2: Integration Architecture

### make plan Flow

```
make plan
  │
  ├─ Fetch ticker universe (shared, once)
  ├─ Fetch OHLCV + institutional data (shared client, date-level cache)
  │
  ├─ Pass 1: Existing momentum engine (unchanged)
  │    └─ scan_YYYY-MM-DD.csv
  │
  └─ Pass 2: New AccumulationEngine
       └─ coil_YYYY-MM-DD.csv
```

### coil CSV Schema

`coil_YYYY-MM-DD.csv` columns:

| Field | Type | Description |
|-------|------|-------------|
| `scan_date` | str (YYYY-MM-DD) | Date the scan was run |
| `analysis_date` | str (YYYY-MM-DD) | Trading date analyzed |
| `ticker` | str | Stock ticker |
| `name` | str | Stock name (from name_map cache) |
| `market` | str | TSE or TPEx |
| `grade` | str | COIL_PRIME / COIL_MATURE / COIL_EARLY |
| `score` | int | Normalized 0–100 score |
| `bb_pct` | float | BB width percentile (0–100); lower = more compressed |
| `vol_ratio` | float | 5-day avg vol / 20-day avg vol; lower = more dry-up |
| `consol_range_pct` | float | 20-day high/low spread as % of close |
| `inst_consec_days` | int | Max of foreign/trust consecutive buy days |
| `weeks_consolidating` | int | Approx sessions in consolidation range / 5 |
| `vs_60d_high_pct` | float | (close / 60-day high - 1) × 100; negative = below resistance |
| `score_breakdown` | JSON str | Dict of all 15 factor sub-scores for backtest replay |
| `flags` | str (pipe-separated) | Gate pass/fail flags and quality flags |

**Display column mapping** (terminal table → CSV field):
- `BB壓縮` → `bb_pct` (display as percentile tier: 極致/收窄/正常)
- `法人連買` → `inst_consec_days`
- `橫盤週` → `weeks_consolidating`
- `vs前高` → `vs_60d_high_pct`

### Terminal Output

Two tables displayed sequentially after scan completes:

1. Existing `BATCH SCAN RESULTS` table (unchanged)
2. New `蓄積雷達` table with columns: Rank / Ticker / 名稱 / 等級 / 分數 / BB壓縮 / 法人連買 / 橫盤週 / vs前高

**Display scope**: COIL_EARLY and above shown in terminal (for exploration). Only COIL_MATURE and above pushed to Telegram (for actionable signals). This asymmetry is intentional. Sorted by score descending.

### Telegram Notifications

- **Message 1** (existing): 隔日建倉名單 — momentum results, unchanged
- **Message 2** (new): 蓄積雷達觀察清單 — only COIL_MATURE and above, sent after Message 1

### make bot Dashboard

New "蓄積雷達" section added to Bot Status panel (bottom of existing Watchlist Prices block). Shows top 5 from latest `coil_*.csv`. Refreshes every 30 seconds alongside existing data.

### New Files

| File | Purpose |
|------|---------|
| `src/taiwan_stock_agent/domain/accumulation_engine.py` | New engine class |
| `scripts/coil_scan.py` | Standalone accumulation scan script |
| `data/scans/coil_YYYY-MM-DD.csv` | Accumulation scan output |
| `config/accumulation_params.json` | Tunable parameters (factor weights, grade thresholds, gate conditions) |

### Modified Files

| File | Change |
|------|--------|
| `scripts/batch_plan.py` | Add Pass 2 call + render accumulation table |
| `scripts/bot.py` | Add 蓄積雷達 block to status panel |
| `Makefile` | Add `coil`, `coil-backtest`, `coil-factor-report` targets |

---

## Part 3: Backtest & Optimization

### Success Definition

A COIL signal at day T is **successful** if, within T+1 to T+10 trading days:
- **(A)** Close breaks above the 20-day high recorded at T, **OR**
- **(B)** T+10 close is ≥ 5% above T close

Either condition alone counts as success.

### Backtest (`make coil-backtest`)

- Script: `scripts/coil_backtest.py`
- Replays AccumulationEngine over historical OHLCV + institutional data
- Reports per-grade win rate, avg return, avg days-to-breakout
- Benchmarks against random baseline (same holding period, same universe)

### Factor Lift Analysis (`make coil-factor-report`)

- For each of the 15 factors: compute win rate with factor present vs absent
- Output factor importance ranking
- Flag factors with Lift < 1.05 as potential noise (recommend weight reduction)

### Parameter Optimization

- Grid search over factor score ceilings and grade thresholds
- Walk-forward validation (train on older data, test on recent 60 days)
- Results written to `config/accumulation_params.json`
- `make tune-review` interactive gate before applying (same pattern as existing engine)
- `make optimize-coil` runs accumulation-only optimization (new target in `scripts/optimize_coil.py`, mirrors `scripts/optimize.py` pattern)
- `make optimize` is **not modified** — momentum and accumulation optimization remain separate commands to avoid scope creep

### New Makefile Targets

| Target | Function |
|--------|----------|
| `make coil` | Run accumulation scan only (skip momentum engine) |
| `make coil-backtest` | Historical backtest of accumulation signals |
| `make coil-factor-report` | Factor lift analysis + weight recommendations |
| `make optimize-coil` | Grid search + walk-forward optimization for accumulation params |

---

## Out of Scope

- Paid FinMind broker-level chip data (分點籌碼) — accumulation engine uses free-tier data only
- Intraday accumulation detection — daily bars only
- UI changes to the web API / Phase 3b endpoints

---

## Success Criteria

- `make plan` outputs both CSVs without increasing total runtime by more than 30%
- Accumulation backtest shows COIL_PRIME win rate ≥ 50% on 60-day walk-forward
- COIL_PRIME/COIL_MATURE stocks are visually distinct from momentum list in terminal and Telegram
- `make coil-factor-report` identifies at least 2 factors to drop or reweight
