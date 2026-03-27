# 分點情報 — Taiwan Stock Broker Label API

台股券商分點行為辨識系統。整合 FinMind 分點明細與 OHLCV 數據，透過 D+2 反轉率將券商分點自動分類為「隔日沖」、「波段贏家」、「地緣券商」、「代操官股」，並輸出 Triple Confirmation 信號（LONG / WATCH / CAUTION）。

> **Last updated:** 2026-03-27
> **Current focus:** Phase 4（Collective label curation — 等待用戶基礎）

---

## 目前進度

| Phase | 狀態 | 說明 |
|-------|------|------|
| Pre-spike | ✅ 完成 | 初始架構、資料對齊驗證工具 |
| Phase 1 | ✅ 完成 | Broker label classifier + batch classifier + outcome recorder |
| Phase 2 | ✅ 完成 | Triple Confirmation Engine + ScoutAgent + Sector heat map + Signal track record |
| Phase 3a | ✅ 完成 | StrategistAgent CLI + LLM reasoning + TWSE free-tier proxy |
| Phase 3b | ✅ 完成 | FastAPI + 真實 DB 路由 + /track-record + /register |
| Phase 4 | ⏳ 未開始 | Collective label curation + Bayesian reversal_rate 更新 |

### 已知資料限制

| 資料源 | 狀態 | 說明 |
|--------|------|------|
| FinMind `TaiwanStockPrice` (OHLCV) | ✅ 免費可用 | 未除權息調整；Pillar 1+3 評分來源 |
| FinMind `TaiwanStockPriceAdj` | ❌ 需付費 | 自動 fallback 到 TaiwanStockPrice |
| FinMind `TaiwanStockBrokerTradingStatement` | ❌ 需付費 | 分點明細，Pillar 2 付費版來源 |
| TWSE T86 / MI_MARGN (外資、融資) | ⚠️ IP 限制 | Pillar 2 免費版來源；部分 IP 被封 |

---

## 系統架構

```
Infrastructure  ──  FinMindClient   (fetch + Parquet cache + retry)
     │              ChipProxyFetcher (TWSE opendata free-tier proxy)
     │              db.py            (PostgreSQL connection pool)
     │
Domain          ──  BrokerLabelClassifier    (D+2 reversal rate → 隔日沖 label)
     │              TripleConfirmationEngine  (三柱確認評分引擎)
     │              SignalOutcomeRepository   (信號結果追蹤)
     │              models.py                (Pydantic schemas)
     │
Agentic         ──  ScoutAgent       (全市場異常掃描：量能、突破、板塊)
     │              ChipDetectiveAgent (籌碼真偽驗證)
     │              StrategistAgent    (最終決策 + Claude 自然語言輸出)
     │
Presentation    ──  __main__.py      (每日 CLI)
                    api/main.py      (Phase 3b FastAPI)
                    frontend/index.html (Landing page)
```

---

## 分析因子（Triple Confirmation Engine）

### Pillar 1 — 動能 Momentum｜最高 55 pts

| 因子 | 觸發條件 | 分數 | 資料 |
|------|----------|------|------|
| VWAP 5日均 | 收盤 > 5日成交量加權均價 | +20 | OHLCV |
| 量能突破 | 今日量 > 20日均量 × 1.5，且收盤不收黑 | +20 | OHLCV |
| K線收盤強弱比 | (收–低)/(高–低) > 0.7（收在日內上 30%） | +5 | OHLCV |
| 連漲天數 | 連續收高 ≥ 3 日（含今日） | +5 | OHLCV |
| 量能遞增趨勢 | 前 3 個交易日成交量連續遞增 | +5 | OHLCV |

### Pillar 2 — 籌碼 Chip｜最高 40 pts（付費）/ 50 pts（免費）

**付費版（FinMind 分點資料）**

| 因子 | 觸發條件 | 分數 |
|------|----------|------|
| 淨買超家數差 | 3 日累計買方分點數 > 賣方分點數 | +15 |
| 籌碼集中度 Top15 | 前 15 分點買量 / 總買量 > 35%（活躍分點 ≥ 10） | +15 |
| 前三無隔日沖 | Top3 買超分點中無隔日沖標籤 | +10 |
| ⚠ 風險扣分 | Top3 有隔日沖分點 | **-25** |

**免費版（TWSE opendata proxy）**

| 因子 | 觸發條件 | 分數 |
|------|----------|------|
| 外資買賣超 | foreign_net_buy > 0 | +15 |
| 投信買賣超 | trust_net_buy > 0 | +10 |
| 自營商買賣超 | dealer_net_buy > 0 | +5 |
| 融資餘額變化 | margin_balance_change ≤ 0（融資未增加） | +10 |
| 三大法人同向 | 外資 + 投信 + 自營商全部淨買 | +5 |
| 外資連買天數 | 連續外資淨買 ≥ 3 日 | +5 |
| ⚠ 風險扣分 | 融券暴增 AND 券資比 > 15% | **-10** |

### Pillar 3 — 空間 Space｜最高 45 pts

| 因子 | 觸發條件 | 分數 | 資料 |
|------|----------|------|------|
| 20 日高點突破 | 收盤 ≥ 20 日高 × 0.99 | +20 | OHLCV |
| 60 日高點突破 | 收盤 ≥ 60 日高 × 0.99（需 ≥ 40 日資料） | +10 | OHLCV（95 日視窗）|
| 均線多頭排列 | MA5 > MA10 > MA20 | +5 | OHLCV |
| MA20 斜率向上 | MA20 最近 5 日斜率為正 | +5 | OHLCV |
| RS vs 大盤 | 個股 5 日報酬 > 加權指數 5 日報酬 × 1.2 | +5 | OHLCV + TAIEX |

### 輔助指標（不計分，LLM reasoning 用）

| 指標 | 說明 |
|------|------|
| RSI(14) | 超買(>70) / 超賣(<30) 標記 |
| MACD | 線值 / 訊號線 / 交叉方向 |
| MA20 連續站上天數 | 幾日持續收盤在 MA20 以上 |
| 跳空缺口 | 今日開盤較前收跌逾 1% |
| 距近期高點 % | 收盤距 OHLCV 視窗高點百分比 |

### 觸發門檻

| 模式 | LONG | WATCH | CAUTION |
|------|------|-------|---------|
| 付費模式（有分點資料） | ≥ 70 | 40–69 | ≤ 30 |
| 免費模式（TWSE proxy） | ≥ 55 | 35–54 | ≤ 30 |
| 特殊規則 | 免費模式且 chip_pts = 0 → 強制 CAUTION | | |

---

## 快速上手

### 前置需求

- Python 3.10+
- FinMind API token（免費注冊：[finmindtrade.com](https://finmindtrade.com/)）
- PostgreSQL 14+（Phase 1+ 及 API 需要；本機 demo 可略過）
- Anthropic API key（選填，只在 LLM reasoning 模式需要）

### 1. 安裝依賴

```bash
pip install -e ".[dev]"

# Phase 3b FastAPI 額外依賴
pip install -r requirements-api.txt
```

### 2. 設定環境變數

```bash
cp .env.example .env
```

`.env` 最小設定：

```
FINMIND_API_KEY=<你的 FinMind JWT token>
# DATABASE_URL=postgresql://user:pass@localhost:5432/taiwan_stock  # 選填
# ANTHROPIC_API_KEY=sk-ant-...  # 選填
```

### 3. 跑單元測試

```bash
PYTHONPATH=src python3 -m pytest tests/unit/ -v
# 預期：159 passed
```

### 4. 每日信號分析

```bash
# 標準模式（需 FinMind 付費 + TWSE 未被封）
PYTHONPATH=src python3 -m taiwan_stock_agent --date 2026-03-24 --tickers 2330 2454

# 跳過資料時效檢查（歷史回測）
PYTHONPATH=src python3 -m taiwan_stock_agent --date 2024-03-04 --tickers 2330 --skip-freshness-check

# Demo 模式（注入合成籌碼資料，驗證完整 pipeline）
PYTHONPATH=src python3 -m taiwan_stock_agent --date 2024-03-04 --tickers 2330 2454 2317 --skip-freshness-check --no-llm --demo
```

### 5. 批量掃描

```bash
python3 scripts/batch_scan.py --date 2024-03-04 --tickers 2330 2454 2317 2382 3008
python3 scripts/batch_scan.py --date 2024-03-04 --top 5 --save-csv
```

### 6. 啟動 Phase 3b API

```bash
python3 -m uvicorn src.taiwan_stock_agent.api.main:app --reload --port 8000
```

```bash
curl http://localhost:8000/health
curl http://localhost:8000/v1/broker-label/9600
curl http://localhost:8000/v1/signal/2330
curl http://localhost:8000/v1/track-record?days=30
```

API 文件：`http://localhost:8000/docs`

### 7. 每日信號結果回填（cron）

```bash
python3 scripts/settle_outcomes.py
```

建議 cron schedule（Taiwan time UTC+8）：

```cron
# 每日信號：20:30 CST
30 12 * * 1-5  cd /path/to/stock_investment && python3 -m taiwan_stock_agent --date $(date +\%F)

# 結果回填：21:00 CST
0  13 * * 1-5  cd /path/to/stock_investment && python3 scripts/settle_outcomes.py
```

---

## 環境變數

| 變數 | 必填 | 說明 |
|------|------|------|
| `FINMIND_API_KEY` | ✅ | FinMind JWT token（同 `FINMIND_TOKEN`）|
| `DATABASE_URL` | Phase 1+ | `postgresql://user:pass@localhost:5432/taiwan_stock` |
| `ANTHROPIC_API_KEY` | 選填 | Claude API，`--no-llm` 時不需要 |
| `API_KEY` | 選填 | Phase 3b FastAPI master key，不設則 dev 模式跳過驗證 |

---

## 重要文件

| 文件 | 用途 |
|------|------|
| `CLAUDE.md` | AI 協作者必讀：架構決策、Phase Gates |
| `docs/design/signal-engine-design.md` | 完整技術規格：Triple Confirmation 公式、分點分類邏輯 |
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
│   │   └── signal_outcome_repo.py  # Signal 結果追蹤
│   ├── domain/
│   │   ├── triple_confirmation_engine.py  # 三柱確認評分核心
│   │   ├── broker_label_classifier.py     # 隔日沖分類核心
│   │   └── models.py                      # Pydantic schemas
│   ├── agents/
│   │   ├── scout_agent.py          # 全市場異常掃描 + 板塊熱力圖
│   │   ├── chip_detective_agent.py # 籌碼偵探
│   │   └── strategist_agent.py     # 決策主控 + LLM reasoning
│   └── api/
│       ├── main.py                 # Phase 3b FastAPI app
│       ├── schemas.py              # API request/response models
│       └── auth.py                 # API key 驗證（DB + master key）
├── frontend/
│   └── index.html                  # Phase 3b Landing page
├── scripts/
│   ├── batch_scan.py               # 批量掃描
│   ├── settle_outcomes.py          # 每日結果回填 cron
│   ├── fetch_watchlist.py          # 觀察名單管理
│   ├── validate_free_tier.py       # TWSE free-tier 驗證
│   └── data_alignment_check.py     # 資料日期對齊驗證
├── tests/
│   ├── unit/                       # 159 tests，無需 DB/網路
│   └── integration/                # 需要 PostgreSQL
├── db/migrations/                  # SQL migrations（001–003）
└── docs/design/                    # 設計文件
```
