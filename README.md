# 分點情報 — Taiwan Stock Signal Engine

台股籌碼面信號系統。每日收盤後分析指定股票，輸出 **LONG / WATCH / CAUTION** 三級信號，幫助判斷主力是否在進場。

> **Last updated:** 2026-04-09
> **Current focus:** Phase 4.15 完成（T-2 策略驗證 + 軌跡感知加分 + EMERGING_SETUP）
> **Tests:** 208 unit passed（214 integration skipped，需 DB）
> **Scan watchlist:** 全市場上市+上櫃（每日自動更新），互動式選擇產業 + LLM 兩階段優化

---

## 這個工具做什麼

每天收盤後，對一支股票做三件事：

1. **Gate 層** — 先確認有沒有可交易的 setup（2-of-4 條件）
2. **三柱評分** — 動能（max 35）+ 籌碼（max 40）+ 空間（max 35）
3. **風險修正** — 隔日沖、過熱、長上影、借券等扣分，最多 -51

三層通過後，根據 TAIEX 趨勢動態調整門檻，輸出 LONG / WATCH / CAUTION 信號。

---

## 如何解讀輸出

```
⚠️  6257 | CAUTION | Confidence: 44/100 | 2026-04-01
  動能: 量能比率尚可，但收盤強度與趨勢延續偏弱。
  籌碼: 外資強度低，融資結構分數偏低。
  風險: RSI 76.2 超買，有過熱乖離扣分。
  執行: 進場 155.22-156.78 | 停損 156.0 | 目標 170.1
  ⚠ 數據品質: NO_BROKER_DATA, scoring_version:v2
```

| 欄位 | 說明 |
|------|------|
| **信號** | `LONG` 三柱均衡且超過門檻；`WATCH` 部分到位；`CAUTION` 條件不足或風險過高 |
| **Confidence** | 三柱總分（0–100），不是勝率，是信號強度 |
| **進場區間** | bid_limit（下限）到 max_chase（上限），超過上限不追價 |
| **停損** | T+0 收盤價 |
| **目標** | 60 日高點 × 1.05 |
| **data_quality_flags** | 資料缺口說明（見下方） |

### 常見 flags

| Flag | 意思 | 影響 |
|------|------|------|
| `NO_SETUP` | Gate 層未通過（2-of-4 條件不足） | 強制 CAUTION，confidence=0 |
| `GATE_PASS:VOL` / `GATE_FAIL:VOL` | 量能門檻通過/未過 | 可觀測性 |
| `GATE_MET:N` | N/4 門檻通過（N≥2 才評分） | 可觀測性 |
| `EMERGING_SETUP` | WATCH + MA排列 + 法人買超 + 未突破 | precheck 蓄積監控 |
| `PERSIST_RISING:50,55,60` | 連 3 天遞增訊號 | +7 分加成 |
| `PERSIST_STABLE:55` | 前日持續訊號 | +5 分加成 |
| `SECTOR_RANK:1/15` | 產業排名前 20% | +5 分加成 |
| `DOJI_OR_HALT` | 當日 high==low（漲停/停牌） | close_strength 設為 0 |
| `OHLCV_PROXY:yfinance` | FinMind 限額，改用 yfinance | 資料來源切換 |
| `scoring_version:v2` | 使用 v2 引擎評分 | 正常，代表新版架構 |

---

## 免費 vs 付費模式

系統自動偵測資料來源，無需手動切換。

| | 免費模式 | 付費模式 |
|--|---------|---------|
| **資料** | TWSE openapi（法人/融資/借券）+ OHLCV | + FinMind 分點明細 |
| **Pillar 2** | 外資/投信/自營比率、融資結構、借券壓力 | 分點集中度、主力持續性、隔日沖過濾 |
| **Pillar 2 max** | 40 pts | 40 pts |
| **LONG 門檻** | 63 / 68 / 73（依 TAIEX 趨勢） | 同左 |
| **缺點** | 無法辨識隔日沖分點 | FinMind 需付費訂閱 |

> 免費模式下信號仍有效，但假訊號率較高。建議搭配自己的盤感過濾。

---

## 快速上手

### 前置需求

- Python 3.10+
- FinMind API token（[finmindtrade.com](https://finmindtrade.com/) 免費註冊）
- PostgreSQL 14+（API server 需要；CLI demo 可略過）
- LLM API key（選填，自然語言解說用）：Gemini / Claude / OpenAI 三選一

### 1. 安裝

```bash
# 建立 venv
python3.11 -m venv .venv

# 基本安裝
make install

# 含 Gemini LLM（推薦）
make install-gemini

# 含 OpenAI LLM
make install-openai
```

### 2. 設定環境變數

```bash
cp .env.example .env
# 編輯 .env，填入 FINMIND_API_KEY
```

最小設定：

```
FINMIND_API_KEY=<你的 FinMind JWT token>
# DATABASE_URL=postgresql://user:pass@localhost:5432/taiwan_stock  # 選填

# LLM 三選一（選填，不設則跳過自然語言解說）
# LLM_PROVIDER=gemini   # gemini | claude | openai
# LLM_MODEL=gemini-2.5-flash
# GEMINI_API_KEY=AIza...
# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
```

### 3. 確認安裝正確

```bash
make test
# 預期：208 passed
```

---

## 搬移到新機器

### 舊機器：匯出資料

```bash
# 完整備份（schema + 所有分析資料，推薦）
make db-dump
# 產出 backup_YYYYMMDD.dump（壓縮格式，通常 10-50 MB）

# 或只備份訊號資料（最重要的部分）
make db-dump-signals
# 產出 signals_YYYYMMDD.dump
```

把以下三個東西帶走：
1. `backup_YYYYMMDD.dump` — 資料庫備份
2. `.env` — API keys（FinMind / LLM / DB）
3. `data/scans/` — 歷史掃描 CSV（選擇性）

### 新機器：初始化

```bash
# 1. 取得代碼
git clone https://github.com/iknowyou122/stock_investment.git
cd stock_investment

# 2. 建 venv + 安裝依賴
python3.11 -m venv .venv
make install

# 3. 複製 .env（含 DATABASE_URL、FINMIND_API_KEY 等）
cp /path/to/old/.env .env

# 4. 建立 DB + 跑 migrations
make db-init

# 5. 還原資料（可選，沒有也能重新跑 backtest）
make db-restore FILE=backup_20260409.dump

# 6. 確認
make test
```

> **DATABASE_URL 格式：** `postgresql://localhost/taiwan_stock`（本機）或 `postgresql://user:pass@host:5432/taiwan_stock`（遠端）
>
> **沒有備份也沒關係：** `make migrate` 建好 schema 後，`make backtest` 重新跑回測即可重建分析資料。

---

## 查看資料庫內容

### psql 命令列

```bash
psql postgresql://localhost/taiwan_stock
```

常用查詢：

```sql
-- 列出所有資料表
\dt

-- 看欄位結構
\d signal_outcomes

-- 最近 10 筆訊號
SELECT ticker, signal_date, action, confidence_score, outcome_1d, outcome_5d
FROM signal_outcomes
ORDER BY signal_date DESC
LIMIT 10;

-- 各 action 數量
SELECT action, COUNT(*) FROM signal_outcomes GROUP BY action;

-- 結算率（有 outcome_5d 的比例）
SELECT
  COUNT(*) FILTER (WHERE outcome_5d IS NOT NULL) AS settled,
  COUNT(*) AS total
FROM signal_outcomes;

-- 近 30 天 LONG 訊號勝率
SELECT
  action,
  COUNT(*) AS n,
  ROUND(AVG(CASE WHEN outcome_5d > 0 THEN 1 ELSE 0 END) * 100, 1) AS win_pct_5d,
  ROUND(AVG(outcome_5d) * 100, 2) AS avg_ret_5d
FROM signal_outcomes
WHERE signal_date >= CURRENT_DATE - 30
  AND outcome_5d IS NOT NULL
  AND halt_flag = FALSE
GROUP BY action
ORDER BY action;

-- 離開
\q
```

### GUI 工具（TablePlus）

免費版夠用。新增連線：
- Type: `PostgreSQL`
- Host: `localhost`，Port: `5432`
- Database: `taiwan_stock`

### 內建分析腳本

```bash
# 訊號勝率分析（按 action / confidence tier / flag 分組）
make analyze

# 因子貢獻度 + Walk-forward Grid Search
make factor-report

# 軌跡分類勝率（RISING / STABLE / FIRST / DECLINING）
.venv/bin/python scripts/trajectory_analysis.py

# T-2 佈局驗證（D+0 / D+1 / D+2 進場勝率比較）
.venv/bin/python scripts/entry_delay_analysis.py
```

---

## 每日使用流程

### 標準三段式工作流

```
收盤後 (14:00+)   →  make scan          掃描 + 存 CSV + 寫 DB
隔日盤中 (09-13:30) →  make precheck      即時報價確認 + 蓄積中監控
週末               →  make settle        補填 T+1/3/5 結算價
```

### Step 1：收盤後掃描

```bash
# 互動式掃描（選產業 + LLM）
make scan

# 非互動（CI / cron 用）
make scan SECTORS="1 5" LLM=gemini LLM_TOP=5

# 指定歷史日期
make scan DATE=2026-03-28
```

掃描輸出兩份：
- `data/scans/scan_YYYY-MM-DD.csv` — watchlist（供 precheck 讀取）
- DB `signal_outcomes` — 供 factor-report / backtest 分析

### Step 2：隔日盤中確認

```bash
make precheck          # 預設：TOP=20, MIN_CONF=40
make precheck TOP=10 MIN_CONF=50
```

輸出三個區塊：
- **可執行**（LONG，現價在 entry ±3%）→ 積極進場
- **注意**（LONG，量能偏低或大盤偏弱）→ 條件性進場
- **🌱 蓄積中**（WATCH + EMERGING_SETUP）→ 掛限價單被動佈局（T-2 策略）

> TWSE MIS 即時報價：盤中若 `z=-`（最新成交無資料），自動 fallback 到委買價 → 高低中點 → 開盤價。

### Step 3：週末結算

```bash
make settle
```

### API Server

```bash
make api
# http://localhost:8000/docs
```

### 每日自動化（cron）

```cron
# 收盤後掃描（非互動模式）
30 14 * * 1-5  cd /path/to/stock_investment && make scan SECTORS="1 5" LLM=none

# 週末結算
0 10 * * 6    cd /path/to/stock_investment && make settle
```

---

## API Server（Phase 3b）

```bash
# 啟動
make api
# 或直接：
uvicorn taiwan_stock_agent.api.main:app --reload --port 8000

# 測試
curl http://localhost:8000/health
curl http://localhost:8000/v1/signal/2330
curl http://localhost:8000/v1/broker-label/9600
curl http://localhost:8000/v1/track-record?days=30
```

互動文件：`http://localhost:8000/docs`

---

## Phase 4 — 社群勝率回報（Collective Label Curation）

用戶可以回報信號結果，驅動 Bayesian 更新分點標籤的 `community_signal_win_rate`。

```bash
# 回報信號結果（需要 API key）
curl -X POST http://localhost:8000/v1/signals/{signal_id}/outcome \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"did_buy": true, "outcome": "win"}'

# 每日 cron：更新所有分點的 community_signal_win_rate
python3 scripts/run_bayesian_update.py
```

`outcome` 可填 `"win"` / `"lose"` / `"break_even"` / `null`（尚未出場）。
每個 API key 每日上限：免費 10 次，Pro 100 次。

---

## 已知資料限制

| 資料源 | 狀態 | 說明 |
|--------|------|------|
| FinMind `TaiwanStockPrice` (OHLCV) | ✅ 免費 | 未除權息調整；Pillar 1+3 評分來源 |
| FinMind `TaiwanStockPriceAdj` | ❌ 需付費 | 自動 fallback 到未調整價格 |
| FinMind `TaiwanStockBrokerTradingStatement` | ❌ 需付費 | 分點明細；Pillar 2 付費版 |
| TWSE T86（外資/投信/自營商買賣超） | ⚠ WAF 封鎖 | 2026-03 起 TWSE 啟用 WAF，自動 fallback 到 OHLCV RS proxy |
| TWSE openapi MI_MARGN（融資/融券） | ✅ 免費 | 融資餘額、融資使用率、券資比 |
| TWSE TWT93U（借券賣出） | ❌ 端點已下架 | 因子 graceful degrade 為 0 |

---

## 分析因子（Triple Confirmation Engine v2）

詳細規格見 [`docs/design/factor-optimization-v2-plan.md`](docs/design/factor-optimization-v2-plan.md)。

### Gate 層（2-of-4 通過才進入評分）

| # | 條件 | 備註 |
|---|------|------|
| 1 | 收盤 > 5日平均 VWAP | |
| 2 | 今日量 > 20日均量 × 1.2 | |
| 3 | 收盤 ≥ 20日高點 × 0.99 | 20日高點為 0 時不計入 |
| 4 | 個股 5日報酬 > TAIEX 5日報酬 | TAIEX 無資料時不計入 |

未通過：`action="CAUTION"`, `confidence=0`, `data_quality_flags=["NO_SETUP"]`

### Pillar 1 — 動能 Momentum｜max 35 pts

| 因子 | 條件 | 分數 |
|------|------|------|
| 量能比率 | 今日量/20日均量：1.2–1.8→4，>1.8→8 | 0/4/8 |
| 價格方向 | 收盤 ≥ 昨收 | +3 |
| K線收盤強弱 | (收–低)/(高–低)：≥0.7→4，0.5–0.7→2 | 0/2/4 |
| VWAP 優勢 | 收盤 > 5日平均 VWAP | +6 |
| 趨勢延續 | 3日連漲→3，近5日有4日收紅→5 | 0/3/5 |
| 量能遞增 | T-3<T-2<T-1→3，今日再大→5 | 0/3/5 |

### Pillar 2A — 籌碼付費｜max 40 pts（FinMind 分點）

| 因子 | 條件 | 分數 |
|------|------|------|
| 買盤廣度 | 3日淨買分點差：1-10→5，>10→10 | 0/5/10 |
| 集中度品質 | Top15買量/總買量（<10家分點上限+5）| 0/5/10 |
| 主力持續性 | Top5分點與前日重疊：1→3，≥2→5；3日均重疊≥2→+3 | 0–8 |
| 隔日沖過濾 | Top3全非隔日沖→7，否則→0（觸發-25扣分）| 0/7 |
| 外資分點 | 已知外資分點出現→3，進入Top3→5 | 0/3/5 |

### Pillar 2B — 籌碼免費｜max 40 pts（TWSE opendata）

| 因子 | 條件 | 分數 |
|------|------|------|
| 外資強度 | 外資買超/20日均量：0-3%→4，3-8%→8，>8%→12 | 0/4/8/12 |
| 投信強度 | 投信買超/20日均量：0-3%→3，3-8%→6，>8%→8 | 0/3/6/8 |
| 自營商強度 | 自營買超/20日均量：0-3%→2，>3%→4 | 0/2/4 |
| 法人持續性 | 外資連買≥3→4，投信→3，自營→1 | 0–8 |
| 三大法人共識 | 三者全買且兩者達中等以上 | +4 |
| 融資結構 | 漲+減→8，漲+小增→3，漲+大增→-4，跌+大減→2，跌+不減→-3 | -4~+8 |
| 融資使用率 | <20%→+4，>80%→-4 | -4/0/+4 |
| 借券賣出壓力 | 占比5-10%→-4，>10%→-8 | 0/-4/-8 |

### Pillar 3 — 空間 Structure｜max 35 pts

| 因子 | 條件 | 分數 |
|------|------|------|
| 20日高點突破 | 收盤 ≥ 20日高 × 0.99 | +8 |
| 60日高點突破 | 收盤 ≥ 60日高 × 0.99 | +5 |
| 突破站穩品質 | 站穩未跌回 | +2 |
| 均線多頭排列 | MA5 > MA10 > MA20 | +5 |
| MA20 斜率 | MA20 向上 | +5 |
| 相對強弱 | 跑贏大盤 0-20%→3，>20%→5 | 0/3/5 |
| 上方空間 | 距壓力：>8%→5，3-8%→2 | 0/2/5 |

### 風險扣分（最多 -51）

| 條件 | 扣分 |
|------|------|
| Top3 買超含隔日沖分點 | -25 |
| 長上影線（量>1.5倍 且 收盤強度<0.4） | -8 |
| 過熱 vs MA20（>MA20×1.10） | -5 |
| 過熱 vs MA60（>MA60×1.20） | -5 |
| 當沖過熱（占比>35% 且 未站穩突破位） | -5 |
| 借券放空+突破失敗 | -8 |
| 融資追價過熱 | -5 |

### 信號門檻（v2，依 TAIEX MA20 斜率）

| TAIEX 趨勢 | LONG | WATCH | CAUTION |
|-----------|------|-------|---------|
| 上升（MA20 向上） | ≥ 63 | 45–62 | < 45 |
| 中性 | ≥ 68 | 45–67 | < 45 |
| 下跌（MA20 向下） | ≥ 73 | 45–72 | < 45 |

**LONG 額外條件：** Momentum ≥ 15, Chip ≥ 12, Structure ≥ 12

---

## 目前進度

| Phase | 狀態 | 說明 |
|-------|------|------|
| Pre-spike | ✅ 完成 | 初始架構、資料對齊驗證工具 |
| Phase 1 | ✅ 完成 | Broker label classifier + batch classifier + outcome recorder |
| Phase 2 | ✅ 完成 | Triple Confirmation Engine + ScoutAgent + Sector heat map |
| Phase 3a | ✅ 完成 | StrategistAgent CLI + 多 LLM 支援（Gemini/Claude/OpenAI）+ TWSE free-tier proxy |
| Phase 3b | ✅ 完成 | FastAPI + 真實 DB 路由 + /track-record + /register |
| Phase 4 | ✅ 完成 | Collective label curation + BayesianLabelUpdater + 社群勝率回報 API + 付費 stub |
| Phase 4.5 | ✅ 完成 | Makefile 本地開發環境 + DB integration 修復 + Gemini 2.5 Flash |
| Phase 4.6 | ✅ 完成 | v2 引擎：Gate 層 + 三柱重構 + 風險修正 + TAIEX regime 門檻 + migration 007 |
| Phase 4.7 | ✅ 完成 | `make scan` 路徑修正 + T86 週末跳過 + 動態 watchlist（728 檔，每日自動更新）|
| Phase 4.8 | ✅ 完成 | 互動式產業選單（`--sectors` 數字代號）+ 全市場 industry_map cache + 日期自動判斷（17:00 切換）+ T86 rate-limit retry |
| Phase 4.9 | ✅ 完成 | Gate 層可觀測性（GATE_PASS/FAIL/SKIP flags）+ Gate VOL 門檻 1.3→1.2 + Flag 中文翻譯 + 兩階段 LLM |
| Phase 4.10 | ✅ 完成 | avg_20d_volume bug 修正 + TPEx T86 fallback + RSI 計分 + 突破量確認 + 產業排名後處理 + 信號持續加分 |
| Phase 4.11 | ✅ 完成 | Factor Optimization Loop：backtest + factor-report + optimize + scoring_replay |
| Phase 4.12 | ✅ 完成 | Rich UI + shared clients + FinMind yfinance fallback |
| Phase 4.13 | ✅ 完成 | backtest 效能優化：Margin/SBL/DayTrade 日期級 cache + OHLCV pre-fetch + Rich 進度條 |
| Phase 4.14 | ✅ 完成 | `make scan` shared client + `make precheck` 即時報價確認（TWSE MIS）|
| Phase 4.15 | ✅ 完成 | T-2 策略驗證（D+2 勝率 55.6% > D+0 38.5%）+ 軌跡感知加分（RISING+7/STABLE+5/DECLINING+0）+ EMERGING_SETUP flag + precheck 蓄積監控 + Settlement 批次優化 |
| Phase 5 | ⏳ 規劃中 | Stripe 真實付款整合 + 社群信譽評分 + 台灣 Pay |

---

## 系統架構

```
Infrastructure  ──  FinMindClient      (fetch + Parquet cache + retry)
     │              ChipProxyFetcher   (TWSE opendata free-tier proxy)
     │              db.py              (PostgreSQL connection pool)
     │
Domain          ──  BrokerLabelClassifier    (D+2 reversal rate → 隔日沖 label)
     │              TripleConfirmationEngine  (v2：Gate + 三柱 + Risk Adjust)
     │              BayesianLabelUpdater      (Beta-Bernoulli 社群勝率更新)
     │              models.py                (Pydantic schemas)
     │
Agentic         ──  ScoutAgent         (全市場異常掃描：量能、突破、板塊)
     │              ChipDetectiveAgent  (籌碼真偽驗證)
     │              StrategistAgent     (最終決策 + LLM 自然語言輸出)
     │              llm_provider.py     (Gemini / Claude / OpenAI 抽象層)
     │
Presentation    ──  __main__.py        (每日 CLI)
                    api/main.py        (Phase 3b FastAPI)
                    frontend/index.html (Landing page)
```

---

## 環境變數

| 變數 | 必填 | 說明 |
|------|------|------|
| `FINMIND_API_KEY` | ✅ | FinMind JWT token（同 `FINMIND_TOKEN`）|
| `DATABASE_URL` | Phase 1+ | `postgresql://user:pass@localhost:5432/taiwan_stock` |
| `LLM_PROVIDER` | 選填 | `gemini` / `claude` / `openai`，不設則自動偵測 |
| `LLM_MODEL` | 選填 | 覆蓋預設模型（gemini 預設 `gemini-2.5-flash`，claude 預設 `claude-sonnet-4-6`）|
| `GEMINI_API_KEY` | 選填 | Google Gemini API key |
| `ANTHROPIC_API_KEY` | 選填 | Anthropic Claude API key |
| `OPENAI_API_KEY` | 選填 | OpenAI API key |
| `API_KEY` | 選填 | Phase 3b FastAPI master key，不設則 dev 模式跳過驗證 |

---

## 重要文件

| 文件 | 用途 |
|------|------|
| `CLAUDE.md` | AI 協作必讀：架構決策、Phase Gates |
| `docs/design/factor-optimization-v2-plan.md` | v2 因子完整規格 + autoplan review |
| `docs/design/signal-engine-design.md` | 技術規格：v2 公式、分點分類邏輯 |
| `docs/design/ceo-plan.md` | 產品願景、scope 決策、12 個月目標 |
| `DESIGN.md` | Phase 3b UI 設計系統：色票、字型、layout |

---

## 目錄結構

```
stock_investment/
├── src/taiwan_stock_agent/
│   ├── infrastructure/
│   │   ├── finmind_client.py       # FinMind API（cache + retry + fallback）
│   │   ├── twse_client.py          # TWSE opendata free-tier chip proxy
│   │   ├── db.py                   # PostgreSQL connection pool
│   │   └── signal_outcome_repo.py  # Signal 結果追蹤（含 branch_codes）
│   ├── domain/
│   │   ├── triple_confirmation_engine.py  # v2 引擎（Gate + 三柱 + Risk Adjust）
│   │   ├── broker_label_classifier.py     # 隔日沖分類核心
│   │   ├── bayesian_label_updater.py      # Beta-Bernoulli 社群勝率更新（按 scoring_version 分版）
│   │   └── models.py                      # Pydantic schemas（含 avg_20d_volume 等 v2 新欄位）
│   ├── agents/
│   │   ├── scout_agent.py          # 全市場異常掃描 + 板塊熱力圖
│   │   ├── chip_detective_agent.py # 籌碼偵探
│   │   └── strategist_agent.py     # 決策主控 + LLM reasoning + OHLCV RS proxy
│   └── api/
│       ├── main.py                 # FastAPI app（含 Phase 4 /outcome 端點）
│       ├── schemas.py              # API request/response models
│       └── auth.py                 # API key 驗證（DB + master key）
├── data/
│   └── watchlist_cache/            # 全市場 ticker→industry map 每日 cache（industry_map_YYYY-MM-DD.json）
├── frontend/
│   └── index.html                  # Phase 3b Landing page
├── scripts/
│   ├── batch_scan.py               # 批量掃描（動態 watchlist，每日 cache，shared clients）
│   ├── precheck.py                 # 盤中即時確認 + EMERGING_SETUP 蓄積監控
│   ├── backtest.py                 # 歷史回測（OHLCV pre-fetch + entry-delay + 批次 settlement）
│   ├── daily_runner.py             # 每日掃描 + 結算 runner
│   ├── factor_report.py            # Lift 分析 + Walk-forward Grid Search + 殘差分析
│   ├── optimize.py                 # 一鍵優化迴路
│   ├── entry_delay_analysis.py     # T-2 策略驗證：D+0/1/2 勝率比較
│   └── trajectory_analysis.py      # RISING/STABLE/DECLINING 軌跡勝率驗證
├── tests/
│   ├── unit/
│   │   ├── test_triple_confirmation_engine_v2.py  # v2 引擎測試
│   │   ├── test_persistence_bonus.py              # 軌跡感知加分測試
│   │   └── ...                     # 208 tests 總計，無需 DB/網路
│   └── integration/                # 需要 PostgreSQL
├── db/migrations/                  # SQL migrations（001–008）
└── docs/design/
    ├── factor-optimization-v2-plan.md  # v2 因子完整規格
    ├── signal-engine-design.md
    └── ceo-plan.md
```
