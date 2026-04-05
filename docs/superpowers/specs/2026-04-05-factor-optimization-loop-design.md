---
title: Factor Optimization Loop
date: 2026-04-05
status: approved
---

# Factor Optimization Loop — 設計文件

## 目標

建立一套持續優化訊號引擎的回饋迴路，讓引擎能透過歷史資料和每日真實訊號自動找出哪些因子有效、哪些應被廢棄，以及建議新因子方向，最終提升勝率。

---

## 整體架構

系統由四條 Pipeline 組成，串接成完整的優化迴路：

```
Pipeline 1: 歷史回測        → 建立基礎訊號資料庫
Pipeline 2: 每日真實累積    → 持續補入新訊號 + 結算報酬
Pipeline 3: 因子分析優化    → Walk-forward + Grid search + 殘差分析
Pipeline 4: Review Gate     → 人工確認 → 套用參數 / 因子變更
```

---

## Pipeline 1 — 歷史回測 `scripts/backtest.py`

### 原理
把引擎的「今天」設成過去某個交易日，從 TWSE 抓該日資料，跑出評分，再對比 T+1/T+3/T+5 真實收盤價算報酬，寫入 `signal_outcomes`（`source='backtest'`）。

### 執行方式
```bash
make backtest DATE_FROM=2025-01-01 DATE_TO=2026-03-31  # 建立基礎資料
make backtest DATE_FROM=2026-01-15 DATE_TO=2026-01-15  # 補跑單一日期
```

### 限制
- TWSE 歷史 OHLCV 最多約 2 年，上限約 2024 年初
- 回測資料完成後即可立刻進行 Pipeline 3 優化，無需等待真實訊號累積

### 關鍵設計：`score_breakdown` 欄位
每筆訊號儲存所有因子的**原始數值**（非只有得分），供 Grid Search 重算用：

```json
{
  "rsi": 62.3,
  "breakout_vol_ratio": 1.7,
  "foreign_net_shares": 45000,
  "gate_vol_ratio": 1.35,
  "ma20_slope": 0.004,
  "margin_change_pct": -0.02,
  "rsi_momentum_pts": 4,
  "breakout_volume_pts": 3,
  "foreign_pts": 12,
  "institution_strength_pts": 8
}
```

---

## Pipeline 2 — 每日真實訊號 `scripts/daily_runner.py`

### Job A — 每日掃描（17:30 後）
```bash
make daily   # 等同 make scan + 結果存入 signal_outcomes (source='live')
```

### Job B — 每日結算（隔日 15:40）
```bash
make settle DATE=2026-04-04   # 補填 T+1/T+3/T+5 收盤價
```

### Cron 排程（本機，之後可移雲端）
```
30 17 * * 1-5   make daily
40 15 * * 1-5   make settle
```

---

## Pipeline 3 — 因子分析優化 `scripts/factor_report.py`

可隨時觸發，有多少資料跑多少：
```bash
make factor-report        # 使用所有可用資料
make factor-report FORCE=1
```

### Step 1 — 現有因子效益分析（Lift）

對每個 flag：
- 計算「有此 flag 的訊號勝率」vs「無此 flag 的訊號勝率」
- `lift = 有 flag 勝率 − 無 flag 勝率`
- `lift > +5%` → 有效因子 ✅
- `lift < -3%` 連續 4 週 → 廢棄候選 ⚠

### Step 2 — Grid Search + Walk-forward 驗證

**不重跑引擎**，直接從 DB 載入 `score_breakdown` 重算分數：

```
對每組候選參數：
    recompute_score(breakdown, params)   ← 純加法，< 1ms/筆
    eval win_rate at params["long_threshold"]

Walk-forward 驗證：
    滑動視窗（train 6 個月, test 1 個月）
    所有視窗 test lift 皆為正 → 進入建議清單
```

**參數白名單（可自動調整）：**

| 參數 | 目前值 | 搜尋範圍 |
|------|--------|----------|
| Gate VOL 倍數 | 1.2× | 1.0–1.5，step 0.05 |
| RSI momentum 下界 | 55 | 45–60，step 1 |
| RSI momentum 上界 | 70 | 65–80，step 1 |
| Breakout vol 倍數 | 1.5× | 1.2–2.0，step 0.1 |
| Sector top-N 門檻 | 20% | 10–30%，step 5% |
| LONG 信心門檻（平盤）| 68 | 60–75，step 1 |

引擎核心邏輯（Pillar 計算公式、Gate 結構）不在白名單，須人工修改。

### Step 3 — 殘差分析（新因子建議）

找出「引擎猜錯」的訊號族群：
- **False positive**（高分 ≥ 65 但 outcome_1d < 0）
- **False negative**（低分 < 50 但 outcome_1d > +3%）

對兩組的 `score_breakdown` 數值做差異分析，輸出文字建議：
```
建議考慮新因子：
  外資連買天數 ≥ 3 日（FP 組平均 1.2 天，FN 組平均 3.8 天）
  量比 > 2.5 且收漲（FN 組命中率 71%）
```

這是**提示**，不自動加入引擎。

---

## Factor Registry — 因子生命週期

### 狀態機
```
experimental → active → deprecated
```

### DB 表：`factor_registry`
```sql
CREATE TABLE factor_registry (
  name          VARCHAR(50) PRIMARY KEY,
  status        VARCHAR(20),   -- experimental | active | deprecated
  lift_30d      FLOAT,
  lift_90d      FLOAT,
  added_date    DATE,
  deprecated_date DATE,
  notes         TEXT
);
```

### Factor Sandbox — 測試新因子
寫一個實驗因子函數放進 `src/taiwan_stock_agent/factors/experimental/`，不需重跑引擎：

```bash
make test-factor FACTOR=consecutive_foreign_3d
```

系統用歷史 `score_breakdown` 重算 → 輸出勝率對比。確認有效後升為 `active`。

---

## Pipeline 4 — Review Gate `scripts/apply_tuning.py`

```bash
make tune-review
```

互動式顯示本週建議，支援：
- `[A]` 全部套用
- `[S]` 逐一確認
- `[X]` 略過本週

套用後：
1. 更新 `config/engine_params.json`（引擎從此處讀取參數）
2. 舊參數 snapshot 存入 `engine_versions` 表
3. 自動 git commit

### DB 表：`engine_versions`
```sql
CREATE TABLE engine_versions (
  id             SERIAL PRIMARY KEY,
  applied_at     TIMESTAMP,
  params_before  JSONB,
  params_after   JSONB,
  reason         TEXT,
  lift_estimate  FLOAT
);
```

---

## Makefile 新目標

| 指令 | 說明 |
|------|------|
| `make backtest` | 歷史回測，建立基礎訊號資料 |
| `make daily` | 每日掃描 + 存入 DB |
| `make settle` | 回填 T+1/T+3/T+5 結算價 |
| `make factor-report` | 因子效益分析 + Grid Search + 殘差分析 |
| `make test-factor FACTOR=<name>` | 測試實驗因子 |
| `make tune-review` | 互動式 review + 套用調參 |
| `make optimize` | **一鍵跑完完整優化迴路**（見下方）|

### `make optimize` — 一鍵優化腳本 `scripts/optimize.py`

依序執行以下步驟，中間任一步驟失敗即停止：

```
Step 1  settle        補填昨日 + 前幾日所有待結算訊號
Step 2  factor-report 跑因子效益分析 + grid search + 殘差分析
Step 3  tune-review   顯示建議清單，等待 approve/skip
```

支援參數：
```bash
make optimize                    # 互動式（tune-review 需要你 approve）
make optimize AUTO_APPROVE=1     # 全自動（適合 cron，lift > 0 的建議全部套用）
make optimize SKIP_SETTLE=1      # 跳過 settle（已手動跑過）
make optimize DRY_RUN=1          # 只看報告，不套用任何變更
```

**AUTO_APPROVE 安全機制：**
- 單一參數調整幅度超過 20% → 強制停止，需人工確認
- 任何建議廢棄因子 → 強制停止，需人工確認（廢棄是不可逆操作）

---

## DB Schema 變更

| 表 | 變更 |
|----|------|
| `signal_outcomes` | 新增 `score_breakdown JSONB`、`source VARCHAR(10)` |
| `factor_registry` | 新增（因子生命週期管理）|
| `engine_versions` | 新增（調參歷史記錄）|

需要新 Alembic migration（008）。

---

## 實作順序建議

1. DB migration 008（新增欄位 + 兩張新表）
2. `scripts/backtest.py` + `make backtest`
3. `scripts/daily_runner.py` + `make daily` + `make settle`
4. `scripts/factor_report.py`（Lift 分析 → Grid Search → 殘差分析）
5. Factor Sandbox（`make test-factor`）
6. `scripts/apply_tuning.py` + `make tune-review`
7. `scripts/optimize.py` + `make optimize`（整合 step 1–6 的一鍵腳本）
