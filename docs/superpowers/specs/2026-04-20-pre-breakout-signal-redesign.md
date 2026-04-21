# Pre-Breakout Signal Engine Redesign

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Transform the signal system from finding confirmed-breakout stocks (already surged 2-3 days) into finding stocks 1-3 days before breakout, with industry-grouped output and a market sentiment panel in the bot dashboard.

**Architecture:** Three parallel changes — (1) rebuild Triple Confirmation Engine Gate + Pillar 3 to reward compression instead of breakout, (2) restructure batch_plan.py output into industry-sorted groups, (3) add a new sentiment layer (quantitative breadth + Yahoo RSS news) displayed in the bot dashboard.

**Tech Stack:** Python, existing TWSE/TPEx public APIs, Yahoo Finance RSS (public), Rich terminal UI

---

## Background & Motivation

The current Triple Confirmation Engine requires `close ≥ 20d_high × 0.99` as a Gate condition and awards heavy points in Pillar 3 for confirmed breakouts (`breakout_20d_pts +8`, `breakout_60d_pts +5`). This means stocks only appear as LONG after they have already broken out — often 2-3 days into a run.

The AccumulationEngine (蓄積雷達) was meant to catch pre-breakout setups but Gate G1 (`MA20 > MA60`) is too strict — true VCP/coiling setups often have MA20 < MA60 at the start of consolidation.

**Target state:** Both engines find stocks in the 1-3 day window before breakout — tight BB, volume drying up, price hovering just below resistance.

---

## Section 1: Triple Confirmation Engine Rebuild

### Gate Conditions (full rewrite)

Replace the current 2-of-4 gate with 4 hard conditions, all must pass:

| # | Condition | Value |
|---|---|---|
| G1 | Price in accumulation zone | `20d_high × 0.85 ≤ close < 20d_high × 0.99` |
| G2 | Bollinger Band compression | BB width ≤ 15% |
| G3 | Liquidity | daily turnover 20d MA ≥ 20M (TSE) / 8M (TPEx) |
| G4 | Market regime | TAIEX regime ≠ "downtrend" — **blocking override**: if downtrend, SKIP regardless of G1-G3 |

Stocks already above `20d_high × 0.99` → SKIP (already broke out, not our target).
Stocks below `20d_high × 0.85` → SKIP (too far from resistance, not imminent).

BB width formula: `(BB_upper - BB_lower) / BB_mid × 100%`
- BB_upper/lower: 20-day Bollinger Bands (2 standard deviations)
- BB_mid: 20-day simple moving average

### Pillar 1: Momentum (keep, minor adjustments)

- **Remove** `volume_ratio_pts` (no longer rewarding high volume)
- **Add** `volume_dryup_pts`: last 5d avg volume vs 20d avg volume
  - < 60% → +8 pts (strong dryup = accumulation sign)
  - 60–80% → +4 pts
  - ≥ 80% → 0 pts
- **Add** `volume_climax_pts`: validates the dryup is real (not just low-activity stock). Requires at least one day in the last 20 days with volume > 2× 20d_avg, AND current 5d avg < 80% of 20d_avg.
  - Both conditions met → +4 pts
  - Only current dryup (no prior climax) → +0 pts (dryup alone is weaker evidence)
- **Keep** `rsi_momentum_pts` but expand healthy range to RSI 40–65 (consolidating stocks sit lower)
- **Keep** `ma_alignment_pts`, `dmi_initiation_pts`

### Pillar 2: Chip (keep largely intact)

Institutional quiet accumulation is a key confirmation signal:
- Keep `foreign_strength_pts`, `trust_strength_pts`, `dealer_strength_pts`
- Keep `institution_continuity_pts` (consecutive days of net buying)
- Keep `margin_structure_pts` (low margin utilization = better)

### Pillar 3: Structure — FULL REWRITE

Remove all breakout-confirmation factors. Replace with compression-quality factors:

| Factor | Max pts | Logic |
|---|---|---|
| `proximity_pts` | 12 | `close/20d_high` in 92–99% = 12, 88–92% = 6, 85–88% = 0 |
| `bb_compression_pts` | 10 | BB width < 8% = 10, 8–12% = 5, 12–15% = 0 |
| `ma_convergence_pts` | 8 | MA5/MA10/MA20 all within 2% of each other = 8, within 5% = 4 |
| `consolidation_weeks_pts` | 6 | Consolidation defined as consecutive trading days where: BB width < 12% AND daily range (high-low) < 1.5× ATR(20). Count consecutive such days starting from most recent, divide by 5 to get weeks. ≥ 4 weeks = 6, 2–4 weeks = 3, < 2 weeks = 0 |
| `inside_bar_streak_pts` | 5 | Count of consecutive bars (from most recent) where high ≤ prev_high AND low ≥ prev_low. 1 bar = 1pt, 2 = 2pt, 3 = 3pt, 4 = 4pt, ≥5 = 5pt |
| `prior_advance_pts` | 5 | Max close in the 60 trading days before current consolidation window vs close 60 days before that. Advance ≥ 20% = 5, 10–20% = 2, < 10% = 0 |

**Removed factors:** `breakout_20d_pts`, `breakout_60d_pts`, `breakout_quality_pts`, `breakout_volume_pts`, `upside_space_pts`

**BB width threshold progression** (intentional hierarchy): Gate G2 uses ≤15% as a loose entry filter; `consolidation_weeks_pts` uses <12% as a mid-tier criterion; `bb_compression_pts` rewards <8% with maximum points. Each threshold represents increasing quality of compression.

### Output Grade Redefinition

| Grade | Score | Meaning |
|---|---|---|
| `READY` | ≥ 65 | High-quality pre-breakout setup, 1-3 days to potential breakout |
| `WATCH` | 45–64 | Consolidating, not yet at critical point |
| `SKIP` | < 45 | Insufficient compression or too far from resistance |

Regime-adjusted thresholds (applied globally based on TAIEX regime, not per-industry):
- Uptrend (TAIEX MA20 > 5d ago): READY ≥ 60
- Neutral: READY ≥ 65
- Downtrend: READY ≥ 70 (but most stocks will already fail G4 gate)

### Tests

All existing tests in `tests/unit/test_triple_confirmation_engine.py` must be updated to reflect the new Gate and Pillar 3 logic. Key new test cases:
- Stock at 97% of 20d high with BB width 7% → passes Gate, high proximity_pts
- Stock at 80% of 20d high → fails Gate G1
- Stock at 100% of 20d high → fails Gate G1 (already broke out)
- Stock with BB width 20% → fails Gate G2

---

## Section 2: AccumulationEngine Gate G1 Fix

**Current:** `G1: MA20 > MA60` (too strict — many valid VCP setups have MA20 < MA60)

**New G1 (two sub-conditions, both required):**
1. `close within MA20 ± 8%` (stock is consolidating near its 20-day moving average)
2. `MA20[today] ≥ MA20[5d_ago] × 0.99` (MA20 slope is flat or rising — prevents entry on falling MA bounces)

Sub-condition 2 ensures we don't pick up stocks in active downtrends that happen to touch MA20 from below. A stock in downtrend will have MA20 slope clearly negative (> -1%/5 days).

Keep G2, G3, G4 unchanged.

---

## Section 3: Industry-Grouped Output

### Industry Strength Calculation

After scanning all stocks, group by industry (using existing `industry_map` cache):

```python
industry_strength[industry] = median(today_change_pct for all stocks in industry)
```

Sort industries by strength, highest to lowest.

### Output Format

Replace the current flat ranked list with industry-grouped sections:

```
── 半導體  ▲+2.4%  (3 檔準備突破) ─────────────────────
  2330  台積電   READY  85  vs前高 -2.1%  BB 6.2%
  3711  日月光   READY  72  vs前高 -3.8%  BB 8.1%
  2303  聯電     WATCH  58  vs前高 -5.4%  BB 11.3%

── 電子零組件  ▲+1.8%  (1 檔準備突破) ──────────────────
  2317  鴻海     READY  68  vs前高 -4.2%  BB 9.5%

── 生技醫療  ▲+0.9% ─ 無符合個股 ──────────────────────
```

Rules:
- Industries with no qualifying stocks: show header only, no rows
- Industries with strength < -1%: shown last, marked with `▼`
- Within each industry: sort by confidence score descending
- Telegram push: same format, each industry as a separate block

### Changes to batch_plan.py

- After `run_batch()` collects all signals, compute `industry_strength` dict
- Sort signals by `(industry_strength[industry], confidence)` descending
- Group into Rich `Table` panels per industry
- CSV output unchanged (flat list, for backward compatibility)

---

## Section 4: Market Sentiment System

### New Files

**`src/taiwan_stock_agent/infrastructure/sentiment_client.py`**

Fetches two data sources:

1. **Quantitative breadth** (TWSE MIS public API):
   - Advance-decline ratio: `up_stocks / down_stocks`
   - Market volume vs 20d average
   - Pulls from existing TAIEX history for RSI(14)
   - TWSE margin balance (公開資訊觀測站, daily CSV)

2. **News headlines** (Yahoo Finance Taiwan RSS, public):
   - URL: `https://tw.stock.yahoo.com/rss`
   - Parse titles only (no body scraping)
   - No authentication required

```python
class SentimentClient:
    def fetch_breadth(self, trade_date: str) -> BreadthData  # ad_ratio, volume_ratio
    def fetch_news_headlines(self) -> list[str]  # last 20 titles
```

**`src/taiwan_stock_agent/domain/market_sentiment.py`**

```python
@dataclass
class MarketSentiment:
    label: str          # "多頭熱絡" | "中性震盪" | "偏空謹慎"
    emoji: str          # 🔴 | 🟡 | 🟢
    ad_ratio: float
    taiex_rsi: float
    volume_ratio: float
    alerts: list[str]   # news-based warning strings
    hot_keywords: list[str]  # trending sector keywords

def compute_sentiment(breadth: BreadthData, headlines: list[str], taiex_rsi: float) -> MarketSentiment:
    ...
```

**Label logic:**
- 🔴 `多頭熱絡`: ad_ratio > 2.0 AND RSI 50–70 AND volume_ratio > 1.0
- 🟢 `偏空謹慎`: ad_ratio < 0.8 OR RSI < 40 OR TAIEX < MA20
- 🟡 `中性震盪`: everything else

**Keyword lists:**
- Bearish alerts: `["升息", "制裁", "暴跌", "崩盤", "停牌", "調查", "虧損", "下修"]`
- Hot sector keywords: `["AI", "CoWoS", "HBM", "電動車", "記憶體", "機器人", "散熱"]`

### Bot Dashboard Integration

Add a new sentiment widget to `_render_status_panel()` (bottom of Bot Status panel):

```
市場輿情  🔴 多頭熱絡
漲跌比 2.3 · RSI 61 · 量 1.2×
⚠ 台積電法說上調目標價
```

The sentiment is refreshed every 30 seconds alongside market data (use `_MARKET_CACHE` dict with key `"sentiment"`). Add `_fetch_sentiment()` to the `_refresh_market_loop()` background task.

---

## Files to Create / Modify

| File | Action | What changes |
|---|---|---|
| `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` | **Modify** | Gate rewrite, Pillar 3 full rewrite, grade thresholds |
| `src/taiwan_stock_agent/domain/accumulation_engine.py` | **Modify** | G1 condition: MA20>MA60 → close within MA20 ±8% |
| `src/taiwan_stock_agent/infrastructure/sentiment_client.py` | **Create** | BreadthData fetcher + Yahoo RSS parser |
| `src/taiwan_stock_agent/domain/market_sentiment.py` | **Create** | Sentiment scoring + label computation |
| `scripts/batch_plan.py` | **Modify** | Industry-grouped output, industry_strength sort |
| `scripts/bot.py` | **Modify** | Add sentiment widget to `_render_status_panel()`, add `_fetch_sentiment()` to market loop |
| `tests/unit/test_triple_confirmation_engine.py` | **Modify** | Update all Gate + Pillar 3 tests |
| `tests/unit/test_market_sentiment.py` | **Create** | Sentiment label logic tests |

---

## Out of Scope

- PTT scraping (too fragile, terms of service risk)
- make plan output sentiment header (removed per user request)
- Paid FinMind broker-level data (no change to paid/free tier split)
- Historical backtest of new engine (separate task after implementation)

---

## Implementation Status — COMPLETE ✅

**2026-04-21** All components implemented and tested:

1. ✅ **Triple Confirmation Engine v2.3**: Gate rewritten (4 hard conditions for pre-breakout), Pillar 3 fully rewritten (compression quality factors)
2. ✅ **AccumulationEngine**: G1 condition updated (close within MA20±8% + MA20 slope flat/rising)
3. ✅ **market_sentiment.py**: Created with sentiment label logic (多頭熱絡/中性震盪/偏空謹慎)
4. ✅ **sentiment_client.py**: Created with TWSE breadth fetch + Yahoo RSS headlines
5. ✅ **batch_plan.py**: Industry-grouped output with strength calculation (industry_strength dict, sorted by median change)
6. ✅ **bot.py**: Sentiment widget integrated (_fetch_sentiment_sync + 市場輿情 panel in status display)
7. ✅ **Tests**: 
   - test_market_sentiment.py: 6 tests passing (label logic, keyword extraction)
   - test_triple_confirmation_engine_v2_fix.py: 8 tests passing (Gate conditions, Pillar 3 factors, AccumulationEngine G1)

**Next phase:** Historical backtest of new pre-breakout engine (separate task)
