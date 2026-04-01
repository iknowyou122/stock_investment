<!-- /autoplan restore point: /Users/07683.howard.huang/.gstack/projects/iknowyou122-stock_investment/main-autoplan-restore-20260401-102459.md -->
# 優化因子計劃書 — 分析因子完整說明書 v2

> 適用版本：Tier A 優化版 v2
> 對象：台股自主操盤手 / 因子系統開發者
> 核心目標：降低假突破、避免重複計分、提高不同市場環境下的穩定性

---

# 一、系統總覽

v2 維持三柱架構，但從 v1 的「多個小因子直接加總」改成：

1. **Gate（必要條件）**
2. **Score（三柱評分）**
3. **Risk Adjust（風險修正）**

三柱如下：

- **Pillar 1 動能（Momentum）**
  - 看今天這根上漲 / 突破，是否真的有主動買盤與延續性
- **Pillar 2 籌碼（Chip / Flow）**
  - 看這波上漲背後，是法人 / 主力建倉，還是散戶 / 隔日沖推升
- **Pillar 3 空間（Structure / Room）**
  - 看現在的位置是否值得追，是剛起漲，還是已接近壓力 / 過熱

---

# 二、Gate 層（必要條件）

## 用途
先確認這檔股票是否具備進入正式評分的資格。
避免把沒有 setup 的股票硬拿去做 LONG / WATCH 排名。

## 判斷條件
以下 4 項中，**至少滿足 2 項**，才可進入正式評分：

- **收盤 > 5日平均 VWAP**
- **今日成交量 > 20日均量 × 1.3**
- **收盤接近 20 日高點（收盤 ≥ 20日高點 × 0.99）**
- **近 5 日報酬 > 大盤近 5 日報酬**

## 解讀
- 滿足 2 項以上：代表這檔股票至少有基本轉強或突破 setup，可進入評分
- 未滿足 2 項：列為 `NO_SETUP`
- `NO_SETUP` 不代表不能漲，只代表目前不是系統偏好的進場型態

---

# 三、Pillar 1 — 動能（Momentum, max 35）

動能柱重點不是單純上漲，而是看：

- 有沒有量
- 有沒有價
- 收盤是否強
- 趨勢是否有延續

---

## 1. 價量突破品質（max 15）

### 判斷條件
由三部分組成：

#### 1-1. 量能強度（max 8）
- 今日量 / 20日均量 < 1.2 → **0 分**
- 1.2 ~ 1.8 → **+4 分**
- > 1.8 → **+8 分**

#### 1-2. 價格方向（max 3）
- 今日收盤 ≥ 昨日收盤 → **+3 分**
- 否則 → **0 分**

#### 1-3. 收盤位置（max 4）
計算：
`(Close - Low) / (High - Low)`

- ≥ 0.7 → **+4 分**
- 0.5 ~ 0.7 → **+2 分**
- < 0.5 → **0 分**

### 解讀
- 這是 v2 最核心的動能因子
- 量大、收紅、收在高檔，代表當天買盤主導且尾盤承接強
- 若只有放量但收在低位，通常代表拉高出貨或多空分歧，不算高品質突破

---

## 2. VWAP 優勢（max 10）

### 判斷條件
- Close > 5日平均 VWAP → **+6 分**
- Close > 當日 VWAP 且 > 5日平均 VWAP → **+10 分**
- 否則 → **0 分**

### 解讀
- 站上 5 日平均 VWAP：代表收盤高於近幾日市場平均成本
- 同時站上當日 VWAP：代表今天盤中買盤承接也強
- 這比單純「今天收紅」更有成本意義

---

## 3. 短期趨勢延續（max 5）

### 判斷條件
- 近 3 日連漲 → **+3 分**
- 近 5 日中有 4 日收紅 / 收高 → **+5 分**

### 解讀
- 這是延續性確認，不是主要觸發因子
- 連漲代表買方不是只強一天，而是持續幾天都有優勢
- 但這個因子不能過度看重，避免單純追逐短線連漲股

---

## 4. 量能遞增結構（max 5）

### 判斷條件
- T-3 < T-2 < T-1 → **+3 分**
- 若今日量再大於 T-1 → **+5 分**

### 解讀
- 量能逐步放大，通常代表市場參與度提升
- 若前三天量已遞增，今天再放大量，更像主力「預熱後發動」
- 若只有今天突然爆量，而前幾天都很冷，訊號品質會較差

---

# 四、Pillar 2 — 籌碼（Chip / Flow, max 40）

籌碼柱分成：

- **付費版：分點資料**
- **免費版：TWSE 法人 + 融資融券資料**

核心邏輯是看：

- 買超強不強
- 是誰在買
- 是否持續
- 散戶有沒有追價
- 是否有機構空方對賭

---

# 五、Pillar 2A — 付費版（分點資料）

---

## 1. 買盤廣度（max 10）

### 判斷條件
最近 3 日累積：
`淨買超分點數 - 淨賣超分點數`

- ≤ 0 → **0 分**
- 1 ~ 10 → **+5 分**
- > 10 → **+10 分**

### 解讀
- 廣度反映市場買方參與程度
- 若只有少數分點買、很多分點賣，代表買盤不夠廣
- 廣度越高，代表不是只有單一資金硬拉

---

## 2. 集中度品質（max 10）

### 判斷條件
`Top15 買超分點買入量 / 全市場總買入量`

- < 25% → **0 分**
- 25% ~ 35% → **+5 分**
- > 35% → **+10 分**

補充限制：
- 若活躍買入分點 < 10 家，則本因子**最高只給 +5 分**

### 解讀
- 集中度高，代表買盤集中於少數有力分點
- 但若成交太冷清，集中度很容易失真，所以要限制低流動性股票的得分
- 最佳狀況是：有廣度、也有集中，代表既有市場參與，也有主力主導

---

## 3. 主力持續性（max 8）

### 判斷條件
以前 5 大買超分點為主，觀察與前一日、近三日的重疊程度：

- 與前一交易日重疊 0 家 → **0 分**
- 重疊 1 家 → **+3 分**
- 重疊 ≥ 2 家 → **+5 分**

若再符合：
- 與前 3 日平均重疊 ≥ 2 家 → **再 +3 分**

### 解讀
- 真正建倉的主力不會只買一天
- 若主要買超分點連續出現，代表資金在持續布局，不是單日脈衝
- 這是 v2 很重要的新因子

---

## 4. 隔日沖過濾（max 7）

### 判斷條件
- Top3 淨買超分點全部**非隔日沖** → **+7 分**
- 任一命中隔日沖 → **0 分**，且另觸發風險扣分

### 解讀
- 這是非常重要的品質過濾
- 若主買分點是隔日沖類型，今天的強勢很可能只是明天賣壓的前奏
- 實戰上，這比很多技術指標更重要

---

## 5. 外資級分點加分（max 5）

### 判斷條件
- 任一知名外資分點出現在買超名單 → **+3 分**
- 若同時位於前 3 大買超，且集中度強 → **+5 分**

### 解讀
- 外資分點出現，代表有機構級資金參與
- 若只是名單中出現一次，加分有限
- 若進入主要買盤核心，則籌碼品質明顯升級

---

# 六、Pillar 2B — 免費版（TWSE）

---

## 1. 外資買超強度（max 12）

### 判斷條件
計算：
`外資買超股數 / 20日平均成交股數`

- ≤ 0 → **0 分**
- 0% ~ 3% → **+4 分**
- 3% ~ 8% → **+8 分**
- > 8% → **+12 分**

### 解讀
- 不只看外資有沒有買，而是看買的力道夠不夠
- 小買和大買的意義完全不同
- 外資買超強度高，代表國際資金明顯流入

---

## 2. 投信買超強度（max 8）

### 判斷條件
計算：
`投信買超股數 / 20日平均成交股數`

- ≤ 0 → **0 分**
- 0% ~ 3% → **+3 分**
- 3% ~ 8% → **+6 分**
- > 8% → **+8 分**

### 解讀
- 投信較偏中期資金
- 若投信買超強度高，通常比單日自營商買超更有持續性
- 若外資與投信同步偏強，籌碼品質更佳

---

## 3. 自營商買超強度（max 4）

### 判斷條件
計算：
`自營商買超股數 / 20日平均成交股數`

- ≤ 0 → **0 分**
- 0% ~ 3% → **+2 分**
- > 3% → **+4 分**

### 解讀
- 自營商偏短線
- 此因子主要用於補強，不應作為核心籌碼依據
- 單獨自營買超不能解讀為高品質主升段

---

## 4. 法人持續性（max 8）

### 判斷條件
- 外資連買 ≥ 3 日 → **+4 分**
- 投信連買 ≥ 3 日 → **+3 分**
- 自營連買 ≥ 3 日 → **+1 分**

### 解讀
- 單日買超可能只是短期調節或技術性需求
- 連買才代表持續布局意圖
- 外資、投信若同步連買，籌碼面會顯著升級

---

## 5. 三大法人共識（max 4）

### 判斷條件
- 三大法人都淨買
- 且其中至少兩者買超強度達中等以上
→ **+4 分**

否則：
- **0 分**

### 解讀
- 三大法人同向，是強共識信號
- 但若只是三方都小買，不代表真的強，所以 v2 要求至少兩者買超強度夠

---

## 6. 融資結構（max 8，可負分）

### 判斷條件
依「股價方向 × 融資變化」分類：

- **股價漲 + 融資減 / 持平** → **+8 分**
- **股價漲 + 融資小增** → **+3 分**
- **股價漲 + 融資大增** → **-4 分**
- **股價跌 + 融資大減** → **+2 分**
- **股價跌 + 融資不減** → **-3 分**

### 解讀
- 最理想情境是：股價上漲，但融資沒增加，代表不是散戶追價推升
- 若股價漲、融資也暴增，表示散戶槓桿追高，籌碼轉差
- 若股價跌、融資大減，有時反而代表浮額被洗掉

---

## 7. 融資使用率（+4 / 0 / -4）

### 判斷條件
- < 20% → **+4 分**
- 20% ~ 80% → **0 分**
- > 80% → **-4 分**

### 解讀
- 使用率低：代表融資籌碼不擁擠，散戶參與度低，較健康
- 使用率高：代表市場槓桿已偏飽和，一旦轉弱可能加速下跌

---

## 8. 借券賣出壓力（0 / -4 / -8）

### 判斷條件
`借券賣出占比 = 借券賣出股數 / 當日總成交股數`

- < 5% → **0 分**
- 5% ~ 10% → **-4 分**
- > 10% → **-8 分**

### 解讀
- 借券賣出通常代表機構空方或避險部位
- 占比越高，代表市場上越有人對賭這段漲勢
- 若借券壓力高，又剛好遇到突破失敗，風險更大

---

# 七、Pillar 3 — 空間（Structure / Room, max 35）

空間柱重點不是單純看趨勢偏多，
而是看：

- 有沒有真正突破
- 趨勢是否健康
- 是否跑贏大盤
- 上方還有沒有空間

---

## 1. 突破結構（max 15）

### 1-1. 20日高點突破（max 8）
#### 判斷條件
- 收盤 ≥ 20日高點 × 0.99 → **+8 分**
- 否則 → **0 分**

### 1-2. 60日高點突破（max 5）
#### 判斷條件
- 收盤 ≥ 60日高點 × 0.99 → **+5 分**
- 否則 → **0 分**

### 1-3. 突破站穩品質（max 2）
#### 判斷條件
- 今日收盤高於突破位，且未明顯跌回 → **+2 分**
- 否則 → **0 分**

---

## 2. 趨勢健康（max 10）

### 2-1. MA 多頭排列（max 5）
- MA5 > MA10 > MA20 → **+5 分**

### 2-2. MA20 斜率（max 5）
- 今日 MA20 > 5 日前 MA20 → **+5 分**

---

## 3. 相對強弱（max 5）

- 未跑贏大盤 → **0 分**
- 跑贏 0% ~ 20% → **+3 分**
- 跑贏 > 20% → **+5 分**

---

## 4. 上方空間（max 5）

壓力區（120日高點 / 52週高點 / 大量成交套牢區）：
- > 8% → **+5 分**
- 3% ~ 8% → **+2 分**
- < 3% → **0 分**

---

# 八、風險扣分（Risk Adjust）

## 1. 隔日沖在 Top3（-25）
- 付費版分點資料中，Top3 任一標記為隔日沖 → **-25 分**

## 2. 長上影放量（-8）
- 今日量 > 20日均量 × 1.5 且收盤強弱比 < 0.4 → **-8 分**

## 3. 過熱乖離（-5 / -10）
- 收盤距 MA20 > 10% → **-5 分**
- 收盤距 MA60 > 20% → **再 -5 分**

## 4. 當沖過熱（-5）
- 當沖占比 > 35% 且收盤未站穩突破位 → **-5 分**

## 5. 借券放空 + 突破失敗（-8）
- 借券賣出占比 > 10% 且收盤未有效站上 20日高點 → **-8 分**

## 6. 融資追價過熱（-5）
- 股價漲幅明顯 + 融資單日增幅過大 + 融資使用率偏高 → **-5 分**

---

# 九、LLM 輔助指標（不計分）

- RSI(14), MACD, MA20 連續站上天數, 距 52 週高點%, 跳空缺口比例
- 當沖占比, 融券回補天數, ATR/Close, 布林通道寬度, 近 10 日波動率

## 輸出描述欄位
- `breakout_quality`: 乾淨 / 勉強 / 假突破風險
- `chip_quality`: 法人主導 / 主力集中 / 散戶跟風 / 資料不足
- `heat_level`: 低 / 中 / 高
- `setup_type`: 初升段 / 延續段 / 高檔追價

---

# 十、信號門檻

## LONG
- 總分 ≥ 68
- Momentum ≥ 15, Chip ≥ 12, Structure ≥ 12

## WATCH
- 總分 45 ~ 67，或總分高但某一柱偏弱

## CAUTION
- 總分 < 45，或命中重大風險條件

## NO_SETUP
- Gate 層未通過

---

# 十一、v2 核心精神

1. 不是看「有沒有漲」，而是看「怎麼漲」
2. 不是看「有人買」，而是看「誰在買、買多強、買多久」
3. 不是看「突破了沒」，而是看「突破後還有沒有空間」
4. 分數高不等於必漲 — 代表機會品質，不是預言

---

# /autoplan Review — Phase 1: CEO Review

## PRE-REVIEW SYSTEM AUDIT

**Branch:** main | **Commit:** 02f65d9 | **Mode:** SELECTIVE EXPANSION

**Recently touched files (30 days):**
- `strategist_agent.py` (7x), `llm_provider.py` (5x), `triple_confirmation_engine.py` (3x), `models.py` (4x)

**TODOs that intersect this plan:**
- `Outcome-Driven Factor Validation` — TODOS.md P2: needs signal_outcomes ≥30 entries. Relevant: both CEO voices flag that v2 adds factors without validating v1 factor alpha.
- `net_buyer_count_diff volume weighting` — TODOS.md P2: v2 replaces branch-count with ratio-based scoring, partially addresses this.
- `concentration_top15 thinly-traded edge case` — TODOS.md P3: v2 adds the `< 10 active branches → max +5` cap, which closes this TODO.

---

## Step 0A: Premise Challenge

| # | Premise (stated or implied) | Assessment | Risk |
|---|----------------------------|------------|------|
| P1 | Better formula → better trading outcomes | **ASSUMED, NOT STATED.** No IC validation, no walk-forward test planned. | Critical |
| P2 | Broker branch (分點) data cleanly predicts next-day continuation | **ASSUMED.** Taiwan chip data has custodian consolidation noise. No data quality check in plan. | High |
| P3 | 隔日沖 labels are current and stable | **ASSUMED.** Broker branch behavior shifts quarterly (mergers, strategy changes). Stale labels = -25 misfire. | High |
| P4 | Chip pillar weighting (40pts vs 35pts for momentum/structure) is empirically correct | **ASSUMED.** No statistical basis cited for this pillar balance. | Medium |
| P5 | Gate layer (2-of-4) improves precision without losing recall on valid setups | **ASSUMED.** No analysis of how many v1 LONG signals would be filtered by Gate. | Medium |
| P6 | VWAP 5-day average is available from T+1 daily OHLCV | **VALID.** Daily close × volume is available, 5d VWAP is computable. | None |
| P7 | Upper-space resistance (Pillar 3.4) can be approximated by 120d/52w high | **PRAGMATIC.** Better than nothing; real POC requires intraday data (Phase 4). | Low |

**Premise gate — user must confirm before Phase 1 proceeds to implementation:**
The critical premise is P1. Both external voices (Codex + Claude subagent) independently flagged that v2 is optimizing a score without validating that the score predicts alpha. This doesn't mean v2 is wrong — it means v2 needs a validation gate before or during implementation.

---

## Step 0B: Existing Code Leverage Map

| v2 Sub-problem | Existing code | What changes |
|----------------|---------------|--------------|
| Gate layer | None (new) | Add `_gate_check()` → returns `bool + list[str]` of satisfied conditions |
| Pillar 1 restructure | `_ScoreBreakdown.volume_surge_pts`, `close_strength_pts`, `consec_up_pts`, `volume_trend_pts` | Threshold changes + `volume_surge_pts` becomes 3-tier (0/4/8) + `close_strength_pts` becomes 3-tier (0/2/4) |
| VWAP 5-day advantage (new tier) | `vwap_5d_pts` exists (binary) | Extend to 2-tier: 5d avg only (+6) vs. daily+5d (+10) — requires intraday VWAP. **Blocker: daily-only data means no intraday VWAP.** |
| 買盤廣度 (new paid) | `net_buyer_diff_pts` (branch-count) | New field `buyer_breadth_pts`; replace or complement existing factor |
| 主力持續性 (new paid) | None | New field `smart_money_persist_pts`; requires top-5 buyers overlap query across 3 days |
| 外資買超強度 (ratio, free-tier) | `twse_foreign_pts` (binary) | Change to 4-tier ratio: `foreign_net_buy / 20d_avg_volume` |
| 融資結構 bidirectional (free-tier) | `twse_margin_pts` (binary: ≤0 → +10) | Extend `TWSEChipProxy` + `twse_margin_pts` to support negative values |
| 上方空間 (new, Pillar 3) | `VolumeProfile.sixty_day_high` | Add `one_twenty_day_high` + `fiftytwo_week_high` to `VolumeProfile` model |
| 長上影扣分 (new risk) | None | New `upper_shadow_deduction` field in `_ScoreBreakdown` |
| 過熱乖離扣分 (new risk) | None | New `overheat_deduction` field in `_ScoreBreakdown` |
| 當沖過熱扣分 (new risk) | `daytrade_ratio` in `_AnalysisHints` | Promote to scoring: new `daytrade_overheat_deduction` |
| 融資追價扣分 (new risk) | Partial: `short_spike_deduction` exists | New `margin_chase_deduction` field |
| Pillar min guards for LONG | None | New logic in `_build_signal()`: check momentum_pts ≥ 15, chip_pts ≥ 12, structure_pts ≥ 12 |
| LLM output fields | `Reasoning` model (momentum, chip_analysis, risk_factors) | Add `breakout_quality`, `chip_quality`, `heat_level`, `setup_type` to `SignalOutput` or `Reasoning` |

---

## Step 0C: CURRENT → THIS PLAN → 12-MONTH IDEAL

```
CURRENT (v1 Phase 4.5)              THIS PLAN (v2)                    12-MONTH IDEAL (v3+)
────────────────────────────────    ────────────────────────────────    ────────────────────────────────
Flat factor summation               Gate layer (2-of-4 prerequisite)   Gate + IC-validated factor weights
Binary factors (yes/no)             Continuous/ratio factors            Walk-forward calibrated thresholds
No quality filter at entry          Pillar min guards for LONG          Market-regime adaptive thresholds
Paid tier max: 125+ pts             Paid/free max: ~110 pts each        Single unified scoring model
Risk deduction: daytrade -25 only   6 risk deductions                  Bayesian weight updates from outcomes
LLM: 3 reasoning fields             LLM: +4 qualitative labels         LLM: structured explanation + signal history
No regime awareness                 No regime awareness                 TAIEX trend gate as LONG modifier
```

**Dream state delta:** v2 closes ~60% of the gap to 12-month ideal. Missing: IC validation, regime conditioning, Bayesian weight learning.

---

## Step 0C-bis: Implementation Alternatives

| Approach | Effort | Risk | Completeness |
|----------|--------|------|-------------|
| **A: Full v2 as specified** (all factors, Gate, risk deductions) | CC: ~4h / Human: 3d | Medium — complex, many new fields | 7/10 — implements all factors, but no validation gate |
| **B: Evidence-first v2** — run IC analysis on v1 factors first, then implement only validated new factors | CC: ~6h / Human: 1w | Low | 9/10 — defensible, slower to ship |
| **C: Incremental v2** — add only 主力持續性, ratio-based 外資買超, 上方空間, and 長上影扣分 (4 highest-signal new factors), skip rest | CC: ~2h / Human: 1.5d | Low | 6/10 — faster, leaves uncertainty on other factors |

**Auto-decision (P1 + P3):** Recommend Approach B (Evidence-first). However, user may have valid reasons to move fast. Marked as **TASTE DECISION #1**.

---

## Step 0E: Temporal Interrogation

**Hour 1:** Plan is approved. Start reading `triple_confirmation_engine.py` to understand current state.  
**Hour 3:** Identify that `TWSEChipProxy` needs 2 new fields (`margin_increase_large`, `margin_increase_small`). Models updated.  
**Hour 6:** Gate layer implemented + unit tested. Pillar 1 restructure done.  
**Day 2:** Paid Pillar 2A new factors (買盤廣度, 主力持續性) require new broker_trades queries. Data access blocked by FinMind T+1 window.  
**Day 3:** Free-tier Pillar 2B ratio factors need TWSEChipProxy to return `avg_20d_volume` — not currently computed. New fetcher logic needed.  
**Day 4:** 上方空間 factor requires 120-day OHLCV window (vs. current 60-day). VolumeProfile model + fetcher updated.  
**Day 5:** 253 existing tests. Many will break because thresholds changed and new fields added. Test update sprint.  
**Week 2:** Done if IC validation skipped. If not: +1 week for IC analysis sprint.

---

## CODEX SAYS (CEO — strategy challenge)

> 1. **Optimizing a score, not a tradeable edge** — no explicit objective (1d? 5d? risk-adjusted return?), no walk-forward validation, hand-picked thresholds. V2 risks endless knob-twiddling.
> 2. **Execution reality missing** — Taiwan limit-up/gap behavior, liquidity impact, T+1 data timing. Score can be "right" but unbuyable.
> 3. **Regime risk** — fundamentally a momentum/breakout engine; fails in chop/bear without regime-conditioned thresholds.
> 4. **Double-counting and brittleness** — many factors are variants of "strong close + volume + above VWAP," then risk rules penalize same patterns again. Discrete buckets create cliff effects.
> 5. **Thin competitive moat** — factors are replicable. LLM descriptions aren't defensible. Need transparent live performance.

---

## CLAUDE SUBAGENT (CEO — strategic independence)

> 1. **Alpha validation missing (critical)** — plan assumes better formula → better returns. IC test required first: for each v1 factor, compute correlation vs T+5 returns. Cut |IC| < 0.03 factors.
> 2. **3 unstated premises could be wrong** — 分點 data predicts continuation, 隔日沖 labels current, chip pillar weighting correct.
> 3. **6-month regret scenario (critical)** — v2 ships, win-rate unchanged, team realizes formula was tuned to intuition not outcomes.
> 4. **ML-based scoring dismissed without analysis (high)** — XGBoost on same feature set self-weights factors. Not evaluated.
> 5. **Free vs. paid tier split (high)** — scores from 2A and 2B aren't comparable. Free-tier signal at 72 vs paid-tier at 68 — which is more reliable? User trust breaks if not addressed.
> 6. **No regime conditioning (medium)** — same thresholds in bull/chop/bear produces radically different expected outcomes.

---

## CEO DUAL VOICES — CONSENSUS TABLE

```
CEO DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                           Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Premises valid?                   NO      NO      CONFIRMED: not validated
  2. Right problem to solve?           Partial NO      CONFIRMED: alpha validation needed
  3. Scope calibration correct?        Partial NO      DISAGREE: Claude says add IC gate; Codex says system is over-complex
  4. Alternatives sufficiently explored?NO     NO      CONFIRMED: ML scoring, regime conditioning not evaluated
  5. Competitive/market risks covered? Medium  Thin    CONFIRMED: moat analysis needed
  6. 6-month trajectory sound?         RISKY   RISKY   CONFIRMED: without validation gate, trajectory risky
═══════════════════════════════════════════════════════════════
CONFIRMED = both agree. DISAGREE = models differ.
```

---

## CEO Review Sections 1–10

**Section 1 — Problem Statement:** Well-defined. Existing v1 has binary factors that miss quality gradation. The Gate layer addresses a real problem (no-setup stocks getting scored). The Pillar 3 space factor is a genuine gap. Examined and no critical issues in the problem framing itself.

**Section 2 — Error & Rescue Registry**

| Error | Trigger | Caught by | User sees | Tested? |
|-------|---------|-----------|-----------|---------|
| Gate passes stock with 0 chip data | TWSE API down, all 4 gate conditions false | Gate returns False, NO_SETUP | Action: NO_SETUP | Must add |
| Division by zero in 外資買超強度 | avg_20d_volume = 0 (new listing, suspended) | Guard: if avg_vol == 0 → 0 pts | No crash, 0 pts | Must add |
| 主力持續性 with <3 days of history | New listing, fresh start | Guard: if history < 3 → 0 pts | No crash, 0 pts | Must add |
| VolumeProfile missing 120d high | Not enough OHLCV history | Fallback to 60d high or 0 pts | Flag in data_quality_flags | Must add |
| 融資結構 "large increase" threshold undefined | "大增" is ambiguous | Define: > 5% daily increase = large | Consistent behavior | Must define |

**Section 3 — Scope Assessment:** Scope is appropriate for a signal engine at Phase 4.5. Not an ocean — this is a lake. 6 new scoring factors + 6 new risk deductions + Gate layer + 4 LLM output labels. All in-scope changes touch files already modified in the last 30 days.

**Section 4 — Alternatives (already covered in 0C-bis above)**

**Section 5 — Data Flow:**
```
T+1 Data Available →
  finmind_client: OHLCV (20d/60d/120d window) + broker_trades (3d rolling)
  twse_client: T86 法人 (daily) + MI_MARGNS (融資) + TWT93U (借券)
     ↓
  Gate Check (2-of-4) → NO_SETUP if fails
     ↓
  TripleConfirmationEngine._compute()
    Pillar1: volume_surge (3-tier), close_strength (3-tier), VWAP (2-tier), trend, vol_trend
    Pillar2A: breadth, concentration (with <10 cap), persistence, daytrade_filter, FII
    Pillar2B: foreign_ratio, trust_ratio, dealer_ratio, persistence_days, 三大共識, 融資結構, 融資率, SBL
    Pillar3: 20d/60d break, stand quality, MA align, MA slope, RS, 上方空間
    RiskAdj: daytrade_deduction, upper_shadow, overheat, daytrade_hot, SBL_break, margin_chase
     ↓
  _build_signal(): LONG/WATCH/CAUTION/NO_SETUP with pillar min guards
     ↓
  StrategistAgent: LLM fills reasoning + breakout_quality/chip_quality/heat_level/setup_type
```

**Section 6 — Failure Modes Registry**

| Mode | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Gate filters 40%+ of watchlist, users see no signals | Medium | High | Log NO_SETUP counts; tune if >30% filtered |
| Score inflation: v2 scores systematically lower than v1 (LONG threshold at 68 may cut too many) | High | Medium | Backtest threshold on historical data; consider 65 as starting point |
| 主力持續性 overlap logic O(N²) on 30-ticker scan | Low | Low | Cap at top-5 per day; 30 tickers × 3 days is manageable |
| INTRADAY VWAP unavailable — VWAP 2-tier factor becomes VWAP 1-tier | High (confirmed T+1 only) | Medium | See blocker note in Step 0B: daily VWAP = close (tautology). Simplify to keep 5d VWAP only |
| Free vs. paid score incomparability causes user confusion | Medium | High | Document explicitly: free-tier threshold 55, paid-tier 68; never cross-compare |
| 融資大增 threshold "large" undefined | High | High | Define before shipping: >5% single-day increase = large, >2% = small |

**Section 7 — Security:** No new external endpoints. No user-facing data inputs in this change. Signal scoring is internal computation. Nothing flagged.

**Section 8 — Observability:** New Gate layer should log `NO_SETUP` reason flags. New risk deductions should appear in signal_output `data_quality_flags`. Both are in scope per plan.

**Section 9 — Deployment:** T+1 scoring, no real-time dependency. Deployment is just a code push + migration for any new model fields. No migration needed for signal_outcomes (scores will change, historical record preserved but not comparable).

**Section 10 — Timeline:** Realistic at CC speed. 4-6 hours implementation + test updates.

---

## "NOT in scope" — CEO phase

| Item | Reason |
|------|--------|
| ML-based scoring (XGBoost) | Requires labeled outcome dataset ≥ 100 signals. Available in ~2 months. Defer to v3. |
| Market regime conditioning | Good idea; adds 1 day implementation. TASTE DECISION #2 — defer vs. include. |
| IC validation analysis sprint | Recommended before shipping; TASTE DECISION #1 |
| Walk-forward backtesting framework | Phase 5+ item. Not blocking v2. |
| Intraday VWAP (true daily VWAP) | Phase 4+ (requires tick data feed). Current: daily close = VWAP tautology. |

---

## "What already exists" — CEO phase

| v2 capability | Existing code |
|--------------|---------------|
| 3-tier volume scoring | `volume_surge_pts` (binary currently) |
| Close strength graded | `close_strength_pts` (binary currently: > 0.7 → +5) |
| TWSE foreign/trust/dealer | `twse_foreign_pts`, `twse_trust_pts`, `twse_dealer_pts` |
| 連買天數 fields | `TWSEChipProxy.foreign_consecutive_buy_days`, `trust_consecutive_buy_days`, `dealer_consecutive_buy_days` |
| SBL scoring | `twse_sbl_deduction` (already a deduction) |
| 融資使用率 | `twse_margin_util_pts` (already negative-capable) |
| FII branch detection | `paid_fii_presence_pts` + `_KNOWN_FII_BRANCH_CODES` |
| 60-day high | `sixty_day_high_pts` in `_ScoreBreakdown` + `VolumeProfile.sixty_day_high` |

Most v2 changes are extensions of existing fields, not new infrastructure. Good.

---

## CEO Completion Summary

| Dimension | Finding | Auto-decided | Taste? |
|-----------|---------|--------------|--------|
| Problem framing | Sound — Gate + ratio factors address real v1 gaps | Accepted | No |
| Alpha validation | Missing — no IC test planned before v2 ships | Flag + TASTE DECISION | Yes (#1) |
| Premise validity | 3 premises unvalidated (分點 clean, labels current, pillar weights correct) | Flag | No |
| Scope | Right-sized lake; not an ocean | Accepted | No |
| Market regime conditioning | Missing but high value | TASTE DECISION | Yes (#2) |
| Free/paid incomparability | Needs clear documentation + separate thresholds (already in plan) | Accepted | No |
| Implementation approach | Approach A (full v2) vs B (evidence-first) | TASTE DECISION | Yes (#1) |
| Intraday VWAP blocker | Daily VWAP = close tautology. Simplify VWAP 2-tier to VWAP 1-tier for now | Auto-decided: simplify | No |
| 融資大增 threshold | Undefined "large". Must define before shipping. | Auto-decided: define as >5% | No |

**Phase 1 complete.** Codex: 5 concerns. Claude subagent: 6 findings (3 critical/high). Consensus: 5/6 confirmed. 2 taste decisions to surface at gate.

---

## Decision Audit Trail

| # | Phase | Decision | Principle | Rationale | Rejected |
|---|-------|----------|-----------|-----------|----------|
| 1 | CEO | SELECTIVE EXPANSION mode | P1 (completeness) | Plan is well-scoped; surface expansion opportunities individually | N/A |
| 2 | CEO | Intraday VWAP blocker → simplify VWAP 2-tier to 5d-VWAP only | P5 (explicit) | T+1 daily data: daily VWAP = close (tautology). Using daily VWAP "advantage" is misleading | Keep broken dual-VWAP tier |
| 3 | CEO | Define "融資大增" as > 5% single-day increase | P5 (explicit) | Undefined threshold creates inconsistent behavior | Leave undefined |
| 4 | CEO | Market regime conditioning → TASTE DECISION #2 | N/A | Reasonable people differ on include-now vs defer | Auto-add now |
| 5 | CEO | IC validation sprint → TASTE DECISION #1 | N/A | Speed vs. rigor tradeoff; user has context on urgency | Skip entirely |
| 6 | CEO | concentration_top15 TODOS.md P3 → closed by v2 plan's < 10 branch cap | P2 (boil lakes) | v2 adds the cap; TODOS item resolved | Keep open |


---

# /autoplan Review — Phase 3: Eng Review

**Phase 2 (Design):** Skipped — no UI scope detected.

## Step 0: Scope Challenge

Files touched by this plan (blast radius):
- `src/taiwan_stock_agent/domain/models.py` — TWSEChipProxy, VolumeProfile, SignalOutput, ChipReport
- `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` — _ScoreBreakdown, TripleConfirmationEngine
- `src/taiwan_stock_agent/agents/chip_detective_agent.py` — ChipReport construction
- `src/taiwan_stock_agent/agents/strategist_agent.py` — OHLCV window, LLM prompt field names
- `src/taiwan_stock_agent/infrastructure/twse_client.py` — avg_20d_volume field
- `migrations/` — scoring_version column on signal_outcomes
- `tests/unit/test_triple_confirmation_engine.py` — ALL score expectations will change
- `tests/unit/test_strategist_agent.py` — prompt field references will break

This is a large blast radius. All these files are actively maintained. Not a risky delta, but test update burden is real.

---

## Step 0.5: Eng Dual Voices

### CLAUDE SUBAGENT (eng — independent review)

**Finding 1 — _ScoreBreakdown total invariant missing (medium)**
No test asserts `sum(all _pts fields) == total`. Silent omission when adding v2 fields is the main failure mode of this architecture. Must add: `test_score_breakdown_total_covers_all_pts_fields` using `dataclasses.fields()` introspection.

**Finding 2 — Gate TAIEX unavailability ambiguity (high)**  
Gate condition 4 (RS > 大盤) requires `taiex_history`. When TAIEX fetch fails, condition is unavailable not false. The engine cannot distinguish → stocks that would have passed condition 4 may be incorrectly blocked as NO_SETUP. Fix: return `gate_conditions_available: int` from Gate check.

**Finding 3 — Division by zero in close_strength_pts: (Close - Low) / (High - Low) (critical)**  
When `High == Low` (limit-up halt, newly listed, suspended stock with single print), this divides by zero. Guard: `if high == low: return 0, "DOJI_OR_HALT"`. Same latent bug exists in v1.

**Finding 4 — avg_20d_volume missing from TWSEChipProxy (high)**  
v2 ratio-based factors (外資/投信/自營商 strength) require `foreign_net_buy / avg_20d_volume`. The `TWSEChipProxy` model has no `avg_20d_volume` field. Either add it to the model + twse_client fetch, or compute from `ohlcv_history` in the engine (coupling). Missing field = `AttributeError` at runtime.

**Finding 5 — 主力持續性 requires per-day top-5 buyer lists (medium)**  
`ChipReport` today exposes only `top_buyers: list[BrokerWithLabel]` (single day). Overlap with prior 3 days requires `historical_top5_buyers: list[list[BrokerWithLabel]]` — a structural gap. `ChipDetectiveAgent.analyze()` must be extended to build this field. Non-trivial.

**Finding 6 — 上方空間 requires 120d/52w window (medium)**  
`StrategistAgent` fetches `ohlcv_start = analysis_date - timedelta(days=95)`. 120 trading days requires ~170 calendar days. 52 weeks = 250+ trading sessions. `VolumeProfile` needs `one_twenty_day_high: float` + `fiftytwo_week_high: float` fields, and the fetch window must extend to at least 130 calendar days for 120d coverage.

**Finding 7 — scoring_version missing from signal_outcomes (high)**  
v1/v2 `confidence` scores are not on the same scale. `BayesianLabelUpdater` computes reversal rates from historical confidence values. Mixed v1/v2 population distorts the posterior. Fix: DB migration 007 adds `scoring_version VARCHAR(10) DEFAULT 'v1'`. Set `'v2'` on all signals post-deployment. Filter by version in Bayesian queries.

**Finding 8 — free_tier_mode LONG guard semantics broken (medium)**  
Current guard: LONG blocked when `chip_pts == 0`. With v2 bidirectional 融資 (−4) + SBL (−8) + 融資使用率 (−4), `chip_pts` can be −16 with real chip data. `== 0` check semantics break. Change to `chip_pts < 1` → blocks only when no positive chip confirmation exists.

**Finding 9 — StrategistAgent prompt references v1 field names (medium)**  
`_generate_reasoning()` in `strategist_agent.py` references `breakdown.vwap_5d_pts`, `breakdown.net_buyer_diff_pts` etc. v2 renames/restructures some fields. Silent attribute access errors → blank reasoning text. Add contract test: every field referenced in the LLM prompt must exist on `_ScoreBreakdown`.

**Finding 10 — 3am failure: twenty_day_high == 0.0 floods LONG (critical)**  
New listing or partial history → `VolumeProfile.twenty_day_high == 0.0`. Gate condition: `close >= twenty_day_high * 0.99` becomes `close >= 0.0` → always True. Combined with RS condition unavailable (TAIEX down) and VWAP likely true, Gate passes 2/2 available conditions for ALL stocks. Every stock gets scored. Pillar 3 上方空間 distance to 0.0 → 100% → +5 pts always. Engine floods LONG signals.
Fix: Add `PARTIAL_PROFILE` Gate guard: if `twenty_day_high == 0.0`, treat condition as unavailable (not True).

---

### CODEX SAYS (eng — architecture challenge)
[Codex timeout — proceeding single-reviewer mode]

---

## ENG DUAL VOICES — CONSENSUS TABLE

```
ENG DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                           Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Architecture sound?               WARN    N/A    [subagent-only]: _ScoreBreakdown extensible but missing invariant test
  2. Test coverage sufficient?         NO      N/A    [subagent-only]: Gate, bidirectional chip, zero-division all unaddressed
  3. Performance risks addressed?      LOW     N/A    [subagent-only]: No N+1 issues; main risk is lookback window size
  4. Security threats covered?         OK      N/A    No new attack surface
  5. Error paths handled?              NO      N/A    [subagent-only]: 3 critical missing guards (high==low, avg_20d_vol=0, twenty_day_high=0)
  6. Deployment risk manageable?       HIGH    N/A    scoring_version migration is a hard requirement before first v2 signal
═══════════════════════════════════════════════════════════════
```

---

## Section 1: Architecture

```
TripleConfirmationEngine (v2 target architecture)
├── _gate_check(ohlcv, ohlcv_history, taiex_history) → GateResult(passed, conditions_met, conditions_available)
│     ├── condition_1: close > vwap_5d                  # from ohlcv_history (already computed in Pillar 1)
│     ├── condition_2: volume > 20d_avg * 1.3            # from ohlcv_history
│     ├── condition_3: close >= twenty_day_high * 0.99   # from volume_profile (guard: != 0.0)
│     └── condition_4: return_5d > taiex_return_5d       # from taiex_history (skip if unavailable)
├── _compute(ohlcv, ohlcv_history, chip_report, volume_profile, twse_proxy)
│     ├── Pillar1: _volume_quality_score() [3-tier: 0/4/8]
│     │            _price_dir_score()      [binary: 0/3]
│     │            _close_strength_score() [3-tier: 0/2/4, guard high==low]
│     │            _vwap_score()           [5d VWAP only — simplify from 2-tier]
│     │            _trend_score()          [3d/4-of-5 days]
│     │            _vol_trend_score()      [T-3<T-2<T-1 + today]
│     ├── Pillar2A (if chip_data_available):
│     │            _buyer_breadth_score()  [3-tier net_branches: 0/5/10]
│     │            _concentration_score()  [3-tier + <10 branch cap]
│     │            _smart_money_persist_score() [overlap top5 buyers, requires ChipReport.historical_top5_buyers]
│     │            _daytrade_filter_score() [+7 if no daytrade in top3, no longer -25 risk]
│     │            _fii_presence_score()   [+3/+5]
│     ├── Pillar2B (if not chip_data_available):
│     │            _foreign_ratio_score()  [4-tier ratio, guard avg_vol=0]
│     │            _trust_ratio_score()    [4-tier ratio]
│     │            _dealer_ratio_score()   [3-tier ratio]
│     │            _institution_persist_score() [consec days, 3 separate]
│     │            _institution_consensus_score() [3大+2 medium]
│     │            _margin_structure_score() [bidirectional: -4 to +8]
│     │            _margin_util_score()    [+4/0/-4]
│     │            _sbl_score()            [0/-4/-8]
│     └── Pillar3:
│                  _breakout_score()       [20d +8, 60d +5, stability +2, guard twenty_day_high!=0]
│                  _trend_health_score()   [MA align +5, MA slope +5]
│                  _rs_score()             [3-tier: 0/+3/+5]
│                  _upside_space_score()   [3-tier, requires 120d/52w high in VolumeProfile]
├── _apply_risk_deductions(bd, ohlcv, twse_proxy)
│     ├── daytrade_top3 → -25      # kept as risk deduction (in addition to Pillar 2A +7 quality)
│     ├── upper_shadow_volume → -8
│     ├── overheat_ma20 → -5
│     ├── overheat_ma60 → -5
│     ├── daytrade_hot → -5
│     ├── sbl_breakout_fail → -8  # compound: SBL>10% AND !close > 20d_high
│     └── margin_chase → -5
└── _build_signal(ohlcv, bd, volume_profile)
      └── LONG guard: total ≥ 68 AND momentum_pts ≥ 15 AND chip_pts ≥ 12 AND structure_pts ≥ 12
```

**Coupling concern:** `_gate_check` condition_1 (VWAP) and `_vwap_score` in Pillar 1 compute the same value. The Gate and Pillar 1 should share a cached `vwap_5d` rather than recomputing. Add to `_compute` preamble: compute `vwap_5d`, `avg_vol_20d`, `return_5d` once and reuse.

---

## Section 2: Code Quality

**Issues found:**

| Issue | File:region | Severity | Fix |
|-------|-------------|----------|-----|
| `_PILLAR2_FREE_MAX = 63` doc constant will be wrong after v2 ratio redesign | triple_confirmation_engine.py:91 | Low | Update after v2 factors finalized |
| `_LONG_THRESHOLD_FREE = 60` may need adjustment with new max scores | triple_confirmation_engine.py:84 | Medium | Backtest; tentatively keep at 60 for v2 |
| `chip_pts` property sums paid + free fields — with paid factors renamed, this enumerates wrong fields | triple_confirmation_engine.py:222 | High | Rewrite `chip_pts` to conditionally sum by tier |
| `StrategistAgent._format_hints_for_prompt()` references v1 field names | strategist_agent.py:~300 | Medium | Update after v2 field names confirmed |
| `ChipReport.active_branch_count` already exists (models.py:53) | models.py:53 | Good | No change needed — v2's <10 branch cap already uses this |

---

## Section 3: Test Diagram

**New code paths requiring tests:**

| New path | Type | Test file | Status |
|----------|------|-----------|--------|
| Gate: 2-of-4 pass | Unit | test_triple_confirmation_engine.py | Missing |
| Gate: 1-of-4 pass → NO_SETUP | Unit | test_triple_confirmation_engine.py | Missing |
| Gate: TAIEX unavailable + 2 other conditions pass | Unit | test_triple_confirmation_engine.py | Missing |
| Gate: twenty_day_high == 0.0 → condition unavailable not true | Unit | test_triple_confirmation_engine.py | Missing |
| close_strength: High == Low → 0, DOJI_OR_HALT flag | Unit | test_triple_confirmation_engine.py | Missing |
| 外資買超強度: avg_20d_volume == 0 → 0 pts, no exception | Unit | test_triple_confirmation_engine.py | Missing |
| 融資結構: stock up + 融資 大增 → −4 pts | Unit | test_triple_confirmation_engine.py | Missing |
| 融資結構: chip_pts = −16, free_tier LONG guard fires at < 1 | Unit | test_triple_confirmation_engine.py | Missing |
| _ScoreBreakdown.total covers all _pts fields (invariant) | Unit | test_triple_confirmation_engine.py | Missing |
| Pillar min guard: total ≥ 68 but momentum < 15 → WATCH not LONG | Unit | test_triple_confirmation_engine.py | Missing |
| 3am scenario: twenty_day_high = 0 does NOT produce LONG flood | Integration | test_triple_confirmation_engine.py | Missing |
| scoring_version = 'v2' set on new SignalOutput | Unit | test_strategist_agent.py | Missing |
| StrategistAgent prompt references all _ScoreBreakdown fields that exist | Contract | test_strategist_agent.py | Missing |
| 主力持續性: < 3 days history → 0 pts, no exception | Unit | test_triple_confirmation_engine.py | Missing |

**Existing tests that will break on v2:**
- Any test asserting specific score values for Pillar 1 (volume threshold changed from 1.5x to 1.2x/1.8x)
- Any test asserting `no_daytrade_pts = 10` (v2 changes to `daytrade_filter_pts = 7`)
- Any test asserting `net_buyer_diff_pts` (field may be renamed)
- Free-tier LONG guard test asserting `chip_pts == 0` blocks LONG

---

## Section 4: Performance

No N+1 queries introduced. The 主力持続性 overlap check is O(top5 × 3 days) = O(15 comparisons) per ticker. Fine for 30-ticker watchlist. The biggest performance concern is extending the OHLCV lookback window from 95 days to 170+ calendar days — this increases FinMind API data volume by ~80%. Cache the extended window to avoid redundant fetches within a single analysis run.

---

## Failure Modes Registry (Eng)

| Mode | Root cause | Detection | Fix | Priority |
|------|------------|-----------|-----|----------|
| Zero-division in close_strength | High == Low (halt) | DOJI_OR_HALT flag | Guard high==low → 0 pts | Critical |
| LONG flood at 3am | twenty_day_high == 0.0 passes Gate condition 3 | Signal count spike | Guard == 0.0 as unavailable | Critical |
| AttributeError on avg_20d_volume | Field missing from TWSEChipProxy | Runtime crash | Add field to model | High |
| BayesianLabelUpdater cross-version distortion | No scoring_version in signal_outcomes | Silent wrong posterior | Migration 007 | High |
| 主力持続性 no-data for new listings | ChipReport.historical_top5_buyers empty | Score = 0, no crash (with guard) | Guard len < 3 → 0 pts | Medium |
| Test suite breaks after v2 | Score thresholds changed | CI fails | Update test fixtures | Medium |
| StrategistAgent blank reasoning | Stale field name references | Missing text in output | Update prompt field names | Medium |

---

## "NOT in scope" — Eng phase

| Item | Reason |
|------|--------|
| Real-time / intraday VWAP | Phase 4 — requires tick data feed |
| Walk-forward backtest framework | Phase 5 |
| True Volume Profile (POC) | Phase 4 |
| ML-based scoring | Requires ≥100 labeled outcomes; TODOS.md |
| Market regime conditioning | TASTE DECISION #2 |

---

## "What already exists" — Eng phase

| v2 need | Existing field/method |
|---------|----------------------|
| 3-day rolling branch data | FinMind broker_trades with 10-day window already fetched |
| <10 branch cap logic | `active_branch_count` in `ChipReport` already exists |
| 60-day high in VolumeProfile | `sixty_day_high` already in model |
| SBL deduction | `twse_sbl_deduction` already in `_ScoreBreakdown` |
| 融資使用率 scoring | `twse_margin_util_pts` already in `_ScoreBreakdown` (negative-capable) |
| FII branch detection | `_KNOWN_FII_BRANCH_CODES` dict + `paid_fii_presence_pts` |
| Consecutive buy day fields | `trust_consecutive_buy_days`, `dealer_consecutive_buy_days` in TWSEChipProxy |
| Data quality flags | `data_quality_flags: list[str]` already on all domain models |

---

## Eng Completion Summary

| Area | Finding | Auto-decided |
|------|---------|--------------|
| Gate layer | Must add conditions_available to handle TAIEX unavailability | Flagged + fix required |
| close_strength division by zero | Critical bug in both v1 and v2 — fix before v2 ships | Auto-decided: fix in v2 |
| avg_20d_volume in TWSEChipProxy | Blocking: add field to model | Auto-decided: add |
| 主力持续性 historical data | Non-trivial ChipReport extension | Auto-decided: add `historical_top5_buyers` |
| 120d/52w high window | Extend fetch to 170 calendar days | Auto-decided: extend |
| scoring_version migration | Hard requirement before first v2 signal | Auto-decided: migration 007 |
| free_tier LONG guard | Change chip_pts == 0 to chip_pts < 1 | Auto-decided: change |
| _ScoreBreakdown invariant test | Must add before v2 ships | Auto-decided: add |
| pillar_pts properties | Add `momentum_pts`, `structure_pts` properties to _ScoreBreakdown | Auto-decided: add |
| StrategistAgent field names | Update after v2 fields confirmed | Auto-decided: update with v2 |

**Phase 3 complete.** Claude subagent: 10 findings (2 critical, 4 high, 4 medium). Codex: timeout [single-reviewer mode]. Critical gaps: 2 (High==Low division, twenty_day_high flood). High gaps: 4 (avg_20d_volume, scoring_version, Gate TAIEX handling, ChipReport historical_top5_buyers).


---

# Cross-Phase Themes

Three concerns appeared independently in both CEO and Eng review phases.
These are high-confidence signals, not noise.

**Theme 1: Data availability is the load-bearing constraint**
Flagged in Phase 1 (CEO — premise challenge, Section 3) and Phase 3 (Eng — Section 1, architecture, and failure modes registry).
Every v2 Pillar 2B ratio factor (外資/投信/自營商 strength) requires `avg_20d_volume`. Missing from `TWSEChipProxy`. If it ships without that field, free-tier users get 0 pts on three factors and the system silently underscores their signals. Not a maybe — it will happen on every free-tier run until the field is added.

**Theme 2: Scoring version isolation is a data integrity prerequisite**
Flagged in Phase 1 (CEO — Section 2, failure modes: BayesianLabelUpdater corruption) and Phase 3 (Eng — critical gap T18, migration 007).
If v2 signals land in `signal_outcomes` without `scoring_version`, the Bayesian updater will mix v1 and v2 confidence values. The reversal rates it produces will be meaningless — and they feed back into LLM reasoning. This is a silent data corruption path, not a crash.

**Theme 3: Factor quality should be validated before full deployment**
Flagged in Phase 1 (CEO — premise 5: "v2 factors will outperform v1") and Phase 3 (Eng — TASTE DECISION #1: IC validation sprint).
Both phases independently landed on the same question: are we confident enough in the new factor weights to replace v1 without a validation gate? The CEO phase noted the 12-month ideal requires measured IC. The Eng phase flagged it as the top taste decision. This is the most important open question for the user.


---

# TODOS.md Deferred Items

The following items were identified during /autoplan review and should be added to TODOS.md:

## From CEO Review
- **[P2] IC Analysis Sprint** — backtest v1 factor weights, compute IC for each factor against next-day returns, validate v2 weights before full deployment. Trigger: TASTE DECISION #1 outcome.
- **[P2] Market Regime Conditioning** — TAIEX trend gate as LONG threshold modifier (+5 buffer in uptrend, -5 in downtrend). Trigger: TASTE DECISION #2 outcome.

## From Eng Review
- **[P1] `TWSEChipProxy.avg_20d_volume` field** — required for ratio-based 外資/投信/自營商 factors; blocking for free-tier v2.
- **[P1] Migration 007: `signal_outcomes.scoring_version`** — must land before first v2 signal is written; prevents BayesianLabelUpdater data corruption.
- **[P1] `VolumeProfile` 120-day / 52-week high fields** — needed for full Pillar 3 upside space factor; can stub to 0 initially with data_quality_flag.
- **[P2] `ChipReport.historical_top5_buyers`** — needed for 主力持続性 factor; v2 can degrade gracefully without it (score 0) but full scoring requires it.
- **[P2] Gate `gate_conditions_available` return value** — expose TAIEX unavailability to caller.


---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/autoplan` | Scope & strategy | 1 | issues_open (2 unresolved) | Premise 5 (IC validation) + Premise 7 (regime conditioning) not yet validated; TASTE DECISION #2 resolved: regime gate in v2 |
| Eng Review | `/autoplan` | Architecture & tests | 1 | issues_open (4 unresolved, 2 critical) | ZeroDivisionError bug + partial-history Gate bug (critical); avg_20d_volume + scoring_version migration (blocking) |
| CEO Voices | `/autoplan` | Dual-voice strategy | 1 | issues_found | Codex + Claude subagent; 4/6 confirmed, 2 disagree (IC validation, regime gate) → both resolved at gate |
| Eng Voices | `/autoplan` | Dual-voice architecture | 1 | issues_found | Claude subagent (10 findings); Codex timed out [subagent-only]; 4/6 confirmed |
| Design Review | skipped | No UI scope | 0 | — | — |

**VERDICT:** APPROVED with overrides. TASTE DECISION #1: implement v2 first, IC after 20 trading days. TASTE DECISION #2: add TAIEX regime gate to v2 (thresholds 63/68/73). 2 critical bugs + 2 blocking infra gaps must be resolved before first v2 signal is written.

