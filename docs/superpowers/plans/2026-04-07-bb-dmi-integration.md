# BB & DMI 技術指標整合實作計畫 (v2.1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `TripleConfirmationEngine` 中整合布林通道擠壓爆發因子與 DMI 趨勢啟動因子，提升對趨勢發動點的捕捉能力與風險過濾。

**Architecture:**
1.  在 `triple_confirmation_engine.py` 中實作 DMI 與 Bollinger Bands 的數學計算邏輯。
2.  擴展 `_ScoreBreakdown` 與 `_AnalysisHints` 以容納新因子。
3.  在 `_compute` 流程中加入新因子的評分邏輯。
4.  調整 `StrategistAgent` 的資料抓取視窗以確保有足夠的歷史數據進行計算。

**Tech Stack:** Python, Pandas, DataClasses

---

### Task 1: 擴展領域模型與資料結構

**Files:**
- Modify: `src/taiwan_stock_agent/domain/triple_confirmation_engine.py`

- [ ] **Step 1: 更新 `_ScoreBreakdown` 資料結構**
    新增 `dmi_initiation_pts` (Pillar 1), `bb_squeeze_breakout_pts` (Pillar 3), 以及風險扣分欄位 `adx_exhaustion_deduction`, `dmi_divergence_deduction`。更新 `total`, `momentum_pts`, `structure_pts` 屬性以包含這些新因子。

- [ ] **Step 2: 更新 `_AnalysisHints` 資料結構**
    新增 `adx`, `plus_di`, `minus_di`, `bb_upper`, `bb_lower`, `bb_width_percentile` 等欄位，供 LLM 分析使用。

- [ ] **Step 3: 提交變更**
    `git commit -m "domain: extend _ScoreBreakdown and _AnalysisHints for BB/DMI"`

---

### Task 2: 實作技術指標計算邏輯

**Files:**
- Modify: `src/taiwan_stock_agent/domain/triple_confirmation_engine.py`

- [ ] **Step 1: 實作 `_calculate_bb` 方法**
    計算 20MA、上下軌以及帶寬分位數（需 60 日歷史）。

- [ ] **Step 2: 實作 `_calculate_dmi` 方法**
    計算 14 日週期的 +DI, -DI, ADX。

- [ ] **Step 3: 撰寫單元測試驗證指標計算**
    在 `tests/unit/test_triple_confirmation_engine.py` 中加入針對這兩個私有計算方法的測試。

- [ ] **Step 4: 提交變更**
    `git commit -m "engine: implement BB and DMI calculation logic"`

---

### Task 3: 整合評分與風險邏輯

**Files:**
- Modify: `src/taiwan_stock_agent/domain/triple_confirmation_engine.py`

- [ ] **Step 1: 實作 `_dmi_initiation_score` 與 `_bb_squeeze_breakout_score`**
    依照設計文件中的條件（ADX 區間、帶寬分位數、成交量確認等）編寫評分邏輯。

- [ ] **Step 2: 實作風險扣分邏輯**
    實作 `adx_exhaustion_deduction` (ADX > 55) 與 `dmi_divergence_deduction` (背離)。

- [ ] **Step 3: 更新 `_compute` 與 `_compute_hints`**
    將上述計算整合進核心計算流程。

- [ ] **Step 4: 提交變更**
    `git commit -m "engine: integrate BB/DMI scoring and risk deductions"`

---

### Task 4: 調整資料抓取視窗與最終驗證

**Files:**
- Modify: `src/taiwan_stock_agent/agents/strategist_agent.py`
- Test: `tests/unit/test_triple_confirmation_engine.py`

- [ ] **Step 1: 擴大 `StrategistAgent` 的資料抓取視窗**
    將 `ohlcv_start` 從 `analysis_date - timedelta(days=95)` 調整為 `timedelta(days=130)`。

- [ ] **Step 2: 更新現有測試案例**
    更新 `test_triple_confirmation_engine.py` 以反映新的總分結構。

- [ ] **Step 3: 執行完整測試**
    `pytest tests/unit/test_triple_confirmation_engine.py -v`

- [ ] **Step 4: 提交變更並清理**
    `git commit -m "agent: extend data fetch window and verify full v2.1 pipeline"`
