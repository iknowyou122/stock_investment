# 分點情報 — Taiwan Stock Broker Label API

台股券商分點行為辨識系統。整合 FinMind 分點明細與 OHLCV 數據，透過 D+2 反轉率將券商分點自動分類為「隔日沖」、「波段贏家」、「地緣券商」、「代操官股」，並輸出 Triple Confirmation 信號（LONG / WATCH / CAUTION）。Phase 3b 提供 FastAPI 供開發者程式化使用，以及 landing page 說明定價方案。

> **Last updated:** 2026-03-25
> **Current focus:** Phase 3b (FastAPI + Landing page) ✅ 完成，等待 spike_validate.py 執行以解鎖 Phase 1

---

## 目前進度快照

| Phase | 狀態 | 說明 |
|-------|------|------|
| **Pre-spike — 基礎建設** | ✅ 完成 | FinMindClient、BrokerLabelClassifier、ScoutAgent、71 個單元測試全通過 |
| **Phase 3b — FastAPI** | ✅ 完成 | `/v1/broker-label/{code}`、`/v1/signal/{ticker}`，mock 資料 |
| **Phase 3b — Landing page** | ✅ 完成 | `frontend/index.html`，暗色主題，6 個 section |
| **spike_validate.py** | ⏳ 卡關 | 需要 FinMind 付費方案（TaiwanStockBrokerTradingStatement）|
| **Integration tests** | ⏳ 卡關 | 需要 `brew install postgresql@17` |
| **Phase 1** | 🔒 未開始 | Gate：spike 確認隔日沖 reversal_rate > 60% at D+2 |
| **Phase 2** | 🔒 未開始 | Gate：Phase 1 分點標籤 DB 建立並回測 |
| **Phase 3** | 🔒 未開始 | Gate：Triple Confirmation 驗證 30 檔標的 |

### 已知限制（FinMind 免費方案）

| Dataset | 免費方案 | 說明 |
|---------|---------|------|
| `TaiwanStockPrice` | ✅ 可用 | 未除權息調整的 OHLCV |
| `TaiwanStockPriceAdj` | ❌ 需付費 | 自動 fallback 到 `TaiwanStockPrice` |
| `TaiwanStockBrokerTradingStatement` | ❌ 需付費 | 分點明細資料，spike 的核心依賴 |

---

## 系統架構

```
Infrastructure  ──  FinMindClient (fetch + Parquet cache + retry)
     │              finmind_client.py
     │
Domain          ──  BrokerLabelClassifier   (D+2 reversal rate → 隔日沖 label)
     │              TripleConfirmationEngine (Momentum + Chip + Space pillars)
     │              models.py               (Pydantic schemas)
     │
Agentic         ──  ScoutAgent              (全市場異常掃描：量能、突破、族群)
     │              ChipDetectiveAgent      (籌碼真偽驗證)
     │              StrategistAgent         (最終決策 + Claude 自然語言輸出)
     │
Presentation    ──  __main__.py             (每日 CLI)
                    api/main.py             (Phase 3b FastAPI)
                    frontend/index.html     (Phase 3b Landing page)
```

**關鍵設計決策：**
- `BrokerLabelClassifier` 使用 **D+2（OHLCV 索引 idx+2，非日曆 +2 天）** 計算反轉率，避免非交易日誤判。
- `FinMindClient` 在 `TaiwanStockPriceAdj` 返回 400（付費限制）時，自動 fallback 到 `TaiwanStockPrice`。
- 所有 OHLCV fetch 有 Parquet 檔案快取（keyed by dataset + ticker + date range），開發時避免重複打 API。

詳細設計見 [`docs/design/signal-engine-design.md`](docs/design/signal-engine-design.md)。

---

## 快速上手

### Prerequisites

- Python 3.10+
- FinMind API token（免費注冊：[finmindtrade.com](https://finmindtrade.com/)）
- PostgreSQL 14+（Integration tests 需要；日常開發可略過）
-（可選）Anthropic API key — 只在 LLM reasoning 模式需要

### 1. 安裝依賴

```bash
# 核心依賴（domain + infrastructure）
pip install -e ".[dev]"

# Phase 3b FastAPI 依賴
pip install -r requirements-api.txt
```

### 2. 設定環境變數

```bash
cp .env.example .env
```

編輯 `.env`：

```
FINMIND_API_KEY=<你的 FinMind JWT token>
DATABASE_URL=postgresql://user:pass@localhost:5432/taiwan_stock
# ANTHROPIC_API_KEY=sk-ant-...   # 可選，只在 --llm 模式需要
# API_KEY=<自訂 API key>          # 可選，Phase 3b FastAPI auth，不設則 dev 模式跳過驗證
```

### 3. 驗證資料對齊（OHLCV 模式，免費方案可用）

```bash
PYTHONPATH=src python scripts/data_alignment_check.py --ohlcv-only --ticker 2330 --date 2025-01-15
```

加上 `--ohlcv-only` 可跳過分點資料（免費方案無法取得分點明細）。

### 4. 跑單元測試

```bash
PYTHONPATH=src python -m pytest tests/unit/ -v
# 預期：71 passed
```

### 5. 啟動 Phase 3b API（開發模式）

```bash
python -m uvicorn src.taiwan_stock_agent.api.main:app --reload --port 8000
```

測試 endpoints：

```bash
curl http://localhost:8000/health
curl http://localhost:8000/v1/broker-label/9600
curl http://localhost:8000/v1/signal/2330
```

API 文件：`http://localhost:8000/docs`

### 6. 查看 Landing page

直接在瀏覽器開啟 `frontend/index.html`。

---

## 解鎖 spike_validate.py（需付費 FinMind 方案）

Spike 的目的是驗證核心假設：**隔日沖分點的 D+2 反轉率是否確實 > 60%**。

升級 FinMind 方案後執行：

```bash
# 先跑完整資料對齊確認
PYTHONPATH=src python scripts/data_alignment_check.py --ticker 2330 --date 2025-01-15

# 再跑 spike
PYTHONPATH=src python scripts/spike_validate.py --ticker 2330
```

Gate 條件：reversal_rate > 0.60，sample_count ≥ 50。通過後才開始 Phase 1。

---

## 解鎖 Integration Tests（需要完整 PostgreSQL）

```bash
brew install postgresql@17
brew services start postgresql@17
createdb taiwan_stock

PYTHONPATH=src python -m pytest tests/integration/ -v
```

---

## Phase 1 操作（spike 通過後）

**建立分點標籤 DB（一次性，或有新歷史資料時重跑）**

```bash
python scripts/run_phase1_classification.py --tickers 2330 2317 2454 --lookback-days 365
python scripts/run_phase1_classification.py --dry-run  # 預覽，不寫 DB
```

**每日信號產生（每個交易日 20:00 CST 後執行）**

```bash
python -m taiwan_stock_agent --date 2025-01-31
python -m taiwan_stock_agent --date 2025-01-31 --no-llm    # 不需要 ANTHROPIC_API_KEY
python -m taiwan_stock_agent --date 2025-01-31 --tickers 2330 2317 --output signals.json
```

**記錄實際結果（每日 cron，信號日 D+2 後執行）**

```bash
python scripts/record_signal_outcomes.py --date 2025-02-04
```

建議的 cron schedule（Taiwan time, UTC+8）：

```cron
# 每日信號：20:30 CST = 12:30 UTC
30 12 * * 1-5  cd /path/to/stock_investment && python -m taiwan_stock_agent --date $(date +\%F)

# 結果記錄：21:00 CST = 13:00 UTC
0  13 * * 1-5  cd /path/to/stock_investment && python scripts/record_signal_outcomes.py
```

---

## 重要文件索引

| 文件 | 用途 |
|------|------|
| `CLAUDE.md` | AI 協作者必讀：架構決策、Phase Gates、禁止動作 |
| `docs/design/signal-engine-design.md` | 完整技術規格：Triple Confirmation 公式、分點分類邏輯 |
| `docs/design/ceo-plan.md` | 產品願景、scope 決策、12 個月目標 |
| `DESIGN.md` | Phase 3b UI 設計系統：色票、字型、layout |
| `TODOS.md` | P1/P2/P3 待辦事項，含 FinMind 商業授權待確認 |

---

## 環境變數

| 變數 | 必填 | 說明 |
|------|------|------|
| `FINMIND_API_KEY` | ✅ | FinMind JWT token（同 `FINMIND_TOKEN`）|
| `DATABASE_URL` | ✅（Phase 1+）| `postgresql://user:pass@localhost:5432/taiwan_stock` |
| `ANTHROPIC_API_KEY` | 選填 | Claude API，只在 `--llm` 模式需要 |
| `API_KEY` | 選填 | Phase 3b FastAPI auth key，不設則 dev 模式跳過驗證 |

---

## 目錄結構

```
stock_investment/
├── src/taiwan_stock_agent/
│   ├── infrastructure/
│   │   ├── finmind_client.py   # FinMind API wrapper（cache + retry + fallback）
│   │   └── db.py               # PostgreSQL connection pool
│   ├── domain/
│   │   ├── broker_label_classifier.py  # 隔日沖分類核心
│   │   ├── triple_confirmation.py      # Triple Confirmation Engine
│   │   └── models.py                   # Pydantic schemas
│   ├── agents/
│   │   ├── scout_agent.py      # 全市場異常掃描
│   │   ├── chip_detective.py   # 籌碼偵探
│   │   └── strategist.py       # 決策主控
│   └── api/
│       ├── main.py             # Phase 3b FastAPI app
│       ├── schemas.py          # API request/response models
│       └── auth.py             # API key 驗證
├── frontend/
│   └── index.html              # Phase 3b Landing page
├── scripts/
│   ├── data_alignment_check.py # 資料日期對齊驗證
│   ├── spike_validate.py       # 隔日沖假設驗證（需付費 FinMind）
│   └── run_phase1_classification.py
├── tests/
│   ├── unit/                   # 71 tests，無需 DB/網路
│   └── integration/            # 需要 PostgreSQL
├── db/migrations/              # SQL migrations
├── docs/design/                # 設計文件
├── CLAUDE.md                   # AI 協作指引
├── DESIGN.md                   # UI 設計系統
└── TODOS.md                    # 待辦事項
```
