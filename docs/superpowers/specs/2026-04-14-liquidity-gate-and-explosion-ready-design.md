# 流動性門檻與蓄勢偵測設計文件 (v2.2)

> 設計日期：2026-04-14
> 目標：排除冷門股，並在股價「先蹲後跳」的蹲伏階段預先標記出即將噴出的潛力股。

## 0. 設計原則

1. **先蹲後跳 (VCP)**：真正的爆發前兆是「基底已成 + 波動壓縮 + 籌碼悄悄進場」，不是「已經貼近前高且量能放大」— 後者是破底或突破當下，不是蓄勢。
2. **Gate 與 Score 分離**：Gate 是硬性否決條件（base 是否成立），Score 是品質分數（蓄勢強度）。避免 5-of-5 AND 導致零命中。
3. **不新增 action label**：`READY` 擴充會污染回測統計與下游 schema。改用 `data_quality_flags` 標記 `COILING` / `COILING_PRIME`，action 維持 `WATCH`。
4. **分兩階段交付**：v2.2a（Liquidity Gate，純排除）先上線；v2.2b（COILING Detector）再接上。

---

## 1. v2.2a — Liquidity Gate（流動性硬門檻）

### 1.1 計算
- `vol_20ma` = 過去 20 個交易日成交量平均（股）。
- `turnover_20ma` = `vol_20ma × close`（20 日平均成交金額，新台幣）。
- 採 **金額門檻** 而非張數門檻，自動適應高低價股（避免高價股被 1000 張誤殺、低價股被 100 張漏過）。

### 1.2 分級門檻（日均成交金額）
| 市場 | 門檻 | 等效參考 |
|------|------|----------|
| TSE 上市 | `turnover_20ma ≥ NT$ 20,000,000`（2000 萬）| 50 元股 ≈ 400 張；200 元股 ≈ 100 張 |
| TPEx 上櫃 | `turnover_20ma ≥ NT$ 8,000,000`（800 萬）| 上櫃流動性普遍較薄，門檻設為主板 40% |

### 1.3 為何用金額不用張數
- 高價股（如 600 元）1000 張 = 6 億元/日，門檻過嚴；100 張 = 6000 萬/日，已非常充足。
- 低價股（如 15 元）1000 張 = 1500 萬/日，看似足夠但實際買賣價差大；2000 萬金額門檻會要求約 1300 張，更貼近真實流動性。
- 金額門檻等同於「散戶下單 10–50 萬，市場不會被你推動」的經驗值。

### 1.3 實作位置
`TripleConfirmationEngine._gate_check`，於 Gate 四條件檢查前執行。

### 1.4 未達門檻處理
- Gate 直接判定不通過。
- `action = CAUTION`
- `confidence = 0`
- `data_quality_flags += ["LOW_LIQUIDITY"]`
- 跳過後續所有評分（節省運算）。

---

## 2. v2.2b — COILING Detector（蓄勢偵測器）

當標的通過 Liquidity Gate 但 `action` 尚未達 `LONG` 時，執行 COILING 偵測。採 **Gate + Score** 兩層架構。

### 2.1 COILING Gate（6 項硬條件，全部必須成立）

| # | 條件 | 公式 | 目的 |
|---|------|------|------|
| G1 | 流動性 | `turnover_20ma ≥ 門檻` | 已由 v2.2a 保證 |
| G2 | 中期多頭結構 | `MA20 > MA60` 且 `MA20` 近 5 日斜率 `≥ 0` | 確認處於上升通道，不是下跌反彈 |
| G3 | 大盤狀態 | TAIEX regime `!= downtrend` | 熊市中 VCP 假訊號暴增 |
| G4 | 緊繃盤整 | 近 5 日 `(max_high - min_low) / min_low < 5%` | 量化「蹲」的緊實度 |
| G5 | 尚未突破 | 近 5 日 `max(close) < twenty_day_high` | 排除突破當下與突破後回測 |
| G6 | 價格靠近基底高點 | `close ≥ max(close[-10:]) × 0.97` | 蹲在自己的平台頂部，不是平台底部 |

任一不成立 → 不是 COILING，結束偵測。

### 2.2 COILING Quality Score（5 項加分項，K-of-N）

| # | 條件 | 公式 | 權重 | 意涵 |
|---|------|------|------|------|
| Q1 | 波動壓縮 | `bb_width_percentile < 20`（過去 60 日最窄 20%）| +1 | Bollinger squeeze |
| Q2 | 量能乾涸 | 近 5 日均量 `<` 近 20 日均量 `× 0.9` | +1 | 真正的蹲是量縮，不是量暖 |
| Q3 | 籌碼連續進場 | 近 5 日法人淨買天數 `≥ 3`，或累計淨買額 `/ (vol_20ma × close) > 2%` | +1 | 單日淨買是雜訊，連續性才是訊號 |
| Q4 | 前期建設性漲幅 | `close / min(close[-60:]) ≥ 1.15` | +1 | VCP 前提：至少已有 ~15% 建設性上漲 |
| Q5 | 收盤強度 | 近 5 日平均 `(close - low) / (high - low) > 0.5` | +1 | 每天都收在當日高點附近 |

### 2.3 觸發規則

| 條件 | Flag | 說明 |
|------|------|------|
| Gate 通過且 `Score ≥ 3` | `COILING` | 一般蓄勢 |
| Gate 通過且 `Score ≥ 4` | `COILING_PRIME` | 高品質蓄勢（優先關注）|

- `action` **維持 WATCH**，不新增 label。
- `data_quality_flags` 加入對應 flag。
- `StrategistAgent` 的 LLM prompt 加入 `setup_type: "蓄勢待發 (COILING)"` 或 `"高品質蓄勢 (COILING_PRIME)"`。

---

## 3. 與舊設計的差異

| 項目 | 舊 v2.2 | 新 v2.2 |
|------|---------|---------|
| 流動性門檻 | 單一 1000 張 | 金額門檻 TSE 2000 萬 / TPEx 800 萬（自動適應高低價股）|
| 結構 | 5 項 AND | Gate（6 硬條件）+ Score（5 K-of-N）|
| 「蹲」的定義 | 量能 `>` 20 日均量（量暖）| 量能 `<` 20 日均量 × 0.9（量縮）|
| 基底判定 | 僅檢查 BB width | 加 G4 緊繃盤整 + G5 未突破 + G6 靠近平台頂 |
| 前期漲幅 | 未檢查 | Q4 要求 ≥ 15% 建設性上漲 |
| 籌碼 | 單日淨買 | 5 日連續性或累計比重 |
| 觸發標籤 | 新增 `action=READY` | 沿用 `action=WATCH` + flag |
| 回測相容性 | 需改 label 統計 | 零影響 |

## 4. 實作順序

1. **v2.2a**：`TripleConfirmationEngine._gate_check` 加入分級流動性檢查 + `LOW_LIQUIDITY` flag。單元測試覆蓋 TSE/TPEx 分支。
2. **v2.2b**：
   - 新增 `_coiling_detect(df, hints, chip_flow) -> (passed: bool, score: int)`。
   - 在 `run()` 末段、action 計算之後執行：若非 LONG 且 COILING 通過，注入 flag。
   - 單元測試：Gate 每條件的否決案例 + Score 各組合 + 邊界（score=2/3/4）。
3. **回測驗證**：`make backtest` 跑歷史資料，統計 `COILING` / `COILING_PRIME` 在 D+5 / D+10 的勝率與 Lift，確認優於純 WATCH baseline 再上線。

## 5. 影響評估

- **精確度**：Gate 硬條件會大幅減少假訊號；Score K-of-N 保留調參空間。
- **命中率**：預期 COILING_PRIME 每日 ≤ 10 檔，COILING ≤ 30 檔（需回測實測）。
- **下游相容**：action/label schema 不變，CSV、DB、/track-record 皆無需調整。
- **可觀測性**：`data_quality_flags` 已在 batch_scan Rich UI 顯示；新 flag 自動出現，不需 UI 改動。
