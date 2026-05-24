# Phase 4.36 — FinMind 付費整合設計文件

**日期**: 2026-05-24  
**目標**: 將 FinMind 付費方案的 35 個新功能整合進突破掃描系統（make plan + make surge）  
**Goals**: A. 減少假信號 B. 提早發現起漲點 C. 確認主力身份

---

## 整體架構

```
┌─────────────────────────────────────────┐
│  Layer 0: Hard Gates（硬性過濾）         │
│  處置股 / 暫停交易 / 漲停板標記           │
│  大盤融資維持率 / 景氣燈號                │
└───────────────┬─────────────────────────┘
                ↓ 通過才繼續
┌─────────────────────────────────────────┐
│  Layer 1: Data Enrichment（資料注入）    │
│  PaidDataEnricher — 拉所有付費資料      │
│  填入 TWSEChipProxy 新欄位               │
│  + WeeklyOHLCV + FuturesContext          │
└───────────────┬─────────────────────────┘
                ↓
┌─────────────────────────────────────────┐
│  Layer 2: Scoring（計分引擎）            │
│  Pillar 2A 解鎖（分點資料）              │
│  Pillar 2B 新因子（八大行庫/鉅額/CB）    │
│  Pillar 1 週K確認加分                    │
└───────────────┬─────────────────────────┘
                ↓
┌─────────────────────────────────────────┐
│  Layer 3: Post-processing（後處理）      │
│  市值分層調整 / 結算週降信心             │
│  供應鏈概念 basket 自動更新              │
└─────────────────────────────────────────┘
```

### API Call 預算（6,000/小時上限）

| 資料 | 每次掃描 calls | 頻率 |
|------|---------------|------|
| 處置股清單 | 1 | 每日 |
| 暫停交易清單 | 1 | 每日 |
| 漲跌停價清單 | 1 | 每日 |
| 當沖限制清單 | 1 | 每日 |
| 分點資料（728 檔） | ~730 | 每日 |
| 八大行庫（全市場彙整） | 1 | 每日 |
| 鉅額交易（全市場彙整） | 1 | 每日 |
| 期貨大額交易人 | 1 | 每日 |
| 期貨夜盤三法人 | 1 | 每日 |
| 景氣燈號 | 1 | 每月 |
| TDCC 集保（週資料） | ~728 | 每週 |
| 週K（728 檔） | ~728 | 每週 |
| **每日合計** | **~737** | — |

每次掃描遠低於 6,000 上限，安全。

### Graceful Degradation 原則

- 所有新因子失敗時靜默回 0，不中斷掃描
- Gate 類失敗時 log WARNING 但不阻斷（避免 API 問題癱瘓全掃描）
- 每個新資料來源都有 `is_available` flag，LLM 提示詞依此調整說明

---

## Phase 4.36A — 硬 Gate + 分點 Pillar 2A + TDCC

**目標**: 假信號減少，信心分上限解鎖

### Gate 0 擴充（`triple_confirmation_engine.py` + `surge_scan.py`）

```
Gate 0a: 處置股 Gate
  FinMind dataset: TaiwanStockDisposal
  規則: disposition_end_date >= today → action=SKIP，不進計分
  Cache: 每日 1 call 拿全市場清單

Gate 0b: 暫停交易 Gate
  FinMind dataset: TaiwanStockTradingHalt
  規則: 在清單內 → SKIP

Gate 0c: 漲停標記（不排除，標記）
  FinMind dataset: TaiwanDailyPriceLimit
  規則: close == limit_up_price → flag="LIMIT_UP_CLOSE"
  Surge 結果顯示「⚠️ 封板，隔日開盤確認再進」

Gate 0d: 當沖限制標記
  FinMind dataset: TaiwanNoShortSelling
  規則: 在清單 → flag="DAYTRADE_RESTRICTED"
        Surge volume_ratio 門檻上調 20%（排除當沖放大的假量）
```

### 分點資料 → Pillar 2A 解鎖

```
FinMind dataset: TaiwanStockBrokerTradingStatement
現況: 程式邏輯完整，全輸出 0（無資料）
修改: ChipProxyFetcher.fetch() 呼叫付費 API

解鎖因子（現在全是 0 分）:
  breadth_pts           0 / +5 / +10   波段贏家買超分點數
  concentration_pts     0 / +5 / +10   前3名分點集中度
  continuity_pts        0 / +3 / +5 / +8  波段贏家連續買 N 天
  daytrade_filter_pts   0 / +7         前3名無隔日沖分點
  foreign_broker_pts    0 / +3 / +5    外資指定分行辨識
  合計最高 +40 分，現在是 0
```

### TDCC 集保完整啟用

```
FinMind dataset: TaiwanStockShareholding（週資料）
啟用因子（現在全是 0 分）:
  ownership_concentration_pts   -10 / 0 / +8   400張+大戶週增減
  super_large_pts               -4 / 0 / +8    千張+大戶持股比例+人數
  holder_count_declining_pts     0 / +3 / +5   股東人數連週下降
  chip_concentration_accel_pts   0 / +3 / +6   大戶持股加速集中
  large_2w_trend_pts            -3 / 0 / +5    400張+大戶兩週趨勢
```

### 預期成效

- Pillar 2A：0 → 最高 +40 分
- TDCC 因子：0 → 最高 +30 分（含負分保護）
- 處置股 / 暫停股完全從結果中排除
- 封板股加警示標記

---

## Phase 4.36B — 期貨升級 + Macro Gate

**目標**: 開盤前方向確認，Macro 環境感知

### 期貨大額交易人（Factor E 穩定化）

```
現況: scraping TAIFEX 網頁（格式常改、易斷）
改為: FinMind TaiwanFuturesInstitutionalInvestors（API 穩定）

影響: Factor E「台指期外資淨多單 Gate」可靠性大幅提升
  外資期貨淨空 → SurgeRadar -5 pts + LONG 降 WATCH（現有邏輯，資料更穩）
```

### 期貨夜盤三大法人（全新訊號）

```
FinMind dataset: TaiwanFuturesInstitutionalInvestorsNight
更新時機: 每日盤前（美股收盤後）

新增欄位到 FuturesContext:
  night_foreign_net_long: int   外資夜盤期貨淨多單口數

整合邏輯:
  > +500 口 → 開盤看多確認 +3 pts + flag="NIGHT_SESSION_BULL"
  < -500 口 → 開盤看空 -5 pts + flag="NIGHT_SESSION_BEAR"
  -500 ~ +500 → 中性，不調整

適用場景: make surge-live / make precheck 盤前決策
```

### 大盤融資維持率（市場壓力 Gate）

```
FinMind dataset: TaiwanMarginMaintenanceRatio（大盤整體）
更新頻率: 每日 1 call

Gate 規則:
  維持率 < 130% → 全部 LONG 信號強制降為 WATCH + flag="MARKET_MARGIN_STRESS"
  維持率 < 120% → 全部 LONG/WATCH 降為 CAUTION（市場斷頭危機）
  維持率 ≥ 130% → 不調整

邏輯: 全市場快出現斷頭賣壓時，個股再好也不追突破
Cache: 每日盤後更新，盤中不重拉
```

### 景氣對策信號（月度 Macro Gate）

```
FinMind dataset: TaiwanBusinessCycleSignal（每月 1 筆）
信號對照:

  藍燈（景氣衰退）  → LONG 門檻 +5 pts，confidence cap -10
  黃藍（偏弱）      → confidence cap -5
  綠燈（正常）      → 不調整（維持現狀）
  黃紅（偏熱）      → 動能股加分 +3
  紅燈（過熱）      → 動能股加分 +3，但過熱扣分保留

Cache: 每月更新 1 次，月中不重複拉取
```

---

## Phase 4.36C — 新因子

**目標**: 更早偵測機構行為，軋空確認

### 八大行庫買賣

```
FinMind dataset: TaiwanStockEightMajorInstitutions
新增 Pillar 2B 因子: eight_major_banks_pts

計分規則:
  外資 + 八大行庫同日淨買 → 政策+外資雙確認 +5
  八大行庫連買 ≥ 3 日     → +3（政策資金持續進場）
  八大行庫今日淨買（單獨）→ +2
  八大行庫淨賣            → -2（政策撤退警示）

flag: GOVT_BANK_BUY / GOVT_BANK_SELL
```

### 鉅額交易（機構建倉提前偵測）

```
FinMind dataset: TaiwanStockBlockTrade
新增 Pillar 2B 因子: block_trade_pts

計分規則:
  今日有鉅額買進紀錄              → +4
  鉅額買進量 > 流通量 0.5%        → +7（大規模建倉）
  鉅額賣出量 > 鉅額買進量         → -3（機構出貨）

flag: BLOCK_BUY_DETECTED / BLOCK_SELL_WARNING
LLM chip_analysis 說明鉅額交易的建倉意涵
```

### CB 可轉債溢價因子

```
FinMind dataset: TaiwanConvertibleBond + TaiwanCBInstitutionalInvestors
新增 Pillar 2B 因子: cb_factor_pts

計分規則:
  CB 溢價率 > 5%                          → +3（市場看好轉換）
  機構近 5 日 CB 淨買 + 距轉換期 < 90 天  → +4（買盤即將湧現）
  CB 折價 < -5%                           → -2（市場不看好正股）
  無發行 CB                               → 0（不影響）
```

### 借券費率強化軋空偵測

```
FinMind dataset: TaiwanStockShortSelling（借券費率欄位）
強化現有因子: short_squeeze_setup_pts

現在判斷: 券資比高 + 融券餘額下降
升級後新增條件:
  借券費率 > 0.3% + 上述條件成立 → 額外 +3
  （高費率 = 搶著放空，被迫回補成本高，軋空加速）
```

---

## Phase 4.36D — 結構性品質提升

**目標**: 多週期確認，資料準確性，自動化維護

### 週K多週期確認

```
FinMind dataset: TaiwanStockWeekPrice
新增 Pillar 1 因子: weekly_trend_pts

計分規則:
  週K多頭排列（收 > MA5w > MA13w > MA26w）→ +4
  週K縮量整理 ≥ 3 週                       → +3（週線蓄積確認）
  週K Death Cross（MA5w 下穿 MA13w）        → -10（逆勢突破高風險）

flag: WEEKLY_UPTREND / WEEKLY_COMPRESSION / WEEKLY_DEATH_CROSS
```

### 市值分層信心調整

```
FinMind dataset: TaiwanStockMarketValue
Post-processing 調整（計分完成後）:

  市值 > 500 億 → confidence +3（法人關注，流動性佳）
  市值 50–500 億 → 不調整（主戰場）
  市值 < 50 億  → confidence -5，需外資確認才升 LONG

flag: MEGA_CAP_BREAKOUT / SMALL_CAP_RISK
```

### 供應鏈圖譜 → 概念 basket 自動化

```
FinMind dataset: TaiwanStockIndustryChain
用途: 補充 config/concepts.json（不覆蓋手工維護項目）

每日更新流程（update_market_heat.py 觸發）:
  1. 下載最新產業鏈關係
  2. 對比 concepts.json 現有成員
  3. 補入新成員，標記 concept_source: "auto_chain"
  4. 手工維護項目標記 concept_source: "curated"，不被覆蓋

效果: 概念 basket 從 ~200 檔擴到全市場覆蓋
```

### 還原股價修正回測準確性

```
FinMind dataset: TaiwanStockPriceAdj
影響範圍: make backtest（不影響即時掃描）

問題: 除權息股票歷史 MA/BB 在除權日有跳空，回測進出場點失真
修正: backtest.py 改用還原價計算指標
注意: 即時掃描維持 adjusted=False，避免即時價格失真
     兩套資料平行存在，路徑不同
```

---

## 新增欄位彙整（TWSEChipProxy）

| 欄位 | 類型 | 來源 | Phase |
|------|------|------|-------|
| `is_disposal` | bool | TaiwanStockDisposal | 4.36A |
| `is_trading_halt` | bool | TaiwanStockTradingHalt | 4.36A |
| `is_limit_up` | bool | TaiwanDailyPriceLimit | 4.36A |
| `is_daytrade_restricted` | bool | TaiwanNoShortSelling | 4.36A |
| `eight_major_banks_net` | int | TaiwanStockEightMajorInstitutions | 4.36C |
| `eight_major_banks_streak` | int | TaiwanStockEightMajorInstitutions | 4.36C |
| `block_buy_volume` | int | TaiwanStockBlockTrade | 4.36C |
| `block_sell_volume` | int | TaiwanStockBlockTrade | 4.36C |
| `cb_premium_pct` | float\|None | TaiwanConvertibleBond | 4.36C |
| `cb_inst_net_5d` | int\|None | TaiwanCBInstitutionalInvestors | 4.36C |
| `short_borrow_rate` | float\|None | TaiwanStockShortSelling | 4.36C |
| `market_cap_bn` | float\|None | TaiwanStockMarketValue | 4.36D |

### 新增欄位（FuturesContext）

| 欄位 | 類型 | 來源 | Phase |
|------|------|------|-------|
| `night_foreign_net_long` | int | TaiwanFuturesInstitutionalInvestorsNight | 4.36B |
| `business_cycle_signal` | str | TaiwanBusinessCycleSignal | 4.36B |

### 新增欄位（OHLCVHistory）

| 欄位 | 類型 | 來源 | Phase |
|------|------|------|-------|
| `weekly_candles` | list[WeeklyOHLCV] | TaiwanStockWeekPrice | 4.36D |

---

## 完整功能對照

| Phase | 功能數 | 新增 Gates | 新因子 | 分數影響 |
|-------|--------|-----------|--------|----------|
| 4.36A | 9 | 4 個新 Gate | Pillar 2A 解鎖 + TDCC 5 因子 | +0 to +70 pts |
| 4.36B | 3 | 景氣燈號 Gate | 夜盤三法人 + Factor E 穩定化 | 方向確認質提升 |
| 4.36C | 4 | — | 八大行庫 + 鉅額 + CB + 借券費率 | +0 to +19 pts |
| 4.36D | 4 | — | 週K + 市值 + 供應鏈 + 還原股價 | +0 to +7 pts |
| **合計** | **20** | **5** | **15 個新因子** | **最高 +96 pts** |

---

## 不在本次範圍

以下資料暫不整合（技術複雜度高，ROI 相對低）：

- 台股分K / 逐筆 / 每5秒指數 → 盤中場景，獨立規劃
- 期貨/選擇權交易明細 → max pain 計算複雜，另立 spike
- 可轉債每日總覽 → CB 因子已透過溢價率覆蓋核心需求
- 台股權證標的對照 → 量能過濾機制複雜，延後
- 美國股價分K → 已有 yfinance fallback 覆蓋
- 借貸款項擔保品餘額 → 與融資維持率重疊，優先用後者

---

## Gate / Phase 實作守則

1. 每個 Phase 獨立上線，不等後面的 Phase
2. 所有新因子失敗靜默回 0，不中斷掃描
3. 每個新 FinMind 資料集加入 `_DATASET_CACHE`，避免重複 call
4. 測試策略：每個新因子至少 3 個 unit test（正常/邊界/無資料）
5. 上線後觀察 2 週回測 lift，確認因子有效再調整權重
