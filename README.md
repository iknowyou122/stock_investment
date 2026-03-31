# 分點情報 — Taiwan Stock Signal Engine

台股籌碼面信號系統。每日收盤後分析指定股票，輸出 **LONG / WATCH / CAUTION** 三級信號，幫助判斷主力是否在進場。

> **Last updated:** 2026-03-31
> **Current focus:** Phase 5（Stripe 付款整合 + 社群信譽評分）
> **Tests:** 253 passed

---

## 這個工具做什麼

每天收盤後，對一支股票做三件事：

1. **動能評分（Pillar 1）** — 量價結構：VWAP、量能突破、K線強弱、連漲天數
2. **籌碼評分（Pillar 2）** — 主力行為：外資/投信連買、融資變化、分點集中度（付費）
3. **空間評分（Pillar 3）** — 突破位置：20/60 日高點、均線多頭排列、相對強度

三柱加總，超過門檻輸出信號。

---

## 如何解讀輸出

```
🔴  2330 | CAUTION | Confidence: 30/100 | 2026-03-26
  執行: 進場 1830.8-1849.2 | 停損 1840.0 | 目標 2110.5
```

| 欄位 | 說明 |
|------|------|
| **信號** | `LONG` 三柱齊發；`WATCH` 動能+空間到位但籌碼不足；`CAUTION` 條件不足，不宜進場 |
| **Confidence** | 三柱總分（0–100），不是勝率，是信號強度 |
| **進場區間** | bid_limit（下限）到 max_chase（上限），超過上限不追價 |
| **停損** | 基於 ATR 計算，跌破即出場 |
| **目標** | 2:1 風報比 |
| **data_quality_flags** | 資料缺口說明（見下方） |

### 常見 flags

| Flag | 意思 | 影響 |
|------|------|------|
| `NO_BROKER_DATA` | FinMind 分點資料需付費，未取得 | 切換免費模式評分，Pillar 2 用 TWSE 代替 |
| `TWSE_T86_ERROR` | TWSE T86 端點被 WAF 擋（2026-03 起） | 自動切換 OHLCV RS proxy（見下方） |
| `TWSE_T86_NO_DATA` | 當日無 T86 外資資料（假日/停牌） | Pillar 2 部分因子為 0 |
| `TWSE_T86_PROXY:RS=+X.X%` | T86 被擋，以 RS vs 大盤估算外資方向 | RS ≥ 3% 自動注入外資買超訊號 |
| `free_tier_mode: true` | 使用 TWSE opendata 替代分點資料 | 門檻自動調整（LONG ≥ 60，WATCH ≥ 35） |

---

## 免費 vs 付費模式

系統自動偵測資料來源，無需手動切換。

| | 免費模式 | 付費模式 |
|--|---------|---------|
| **資料** | TWSE openapi MI_MARGN（融資）+ OHLCV RS proxy（T86 被 WAF 擋，2026-03 起） | + FinMind 分點明細 |
| **Pillar 2 滿分** | 63 pts | 45 pts |
| **LONG 門檻** | ≥ 60 | ≥ 70 |
| **優勢** | 免費，T86 當日可用 | 主力分點行為，更精確 |
| **缺點** | 無法辨識隔日沖分點，主力買賣無法分辨 | FinMind 需付費訂閱 |

> 免費模式下信號仍然有效，但假訊號率較高。建議搭配自己的盤感過濾。

---

## 快速上手

### 前置需求

- Python 3.10+
- FinMind API token（[finmindtrade.com](https://finmindtrade.com/) 免費註冊）
- PostgreSQL 14+（API server 需要；CLI demo 可略過）
- LLM API key（選填，自然語言解說用）：Gemini / Claude / OpenAI 三選一

### 1. 安裝

```bash
pip install -e ".[dev]"

# 若要跑 API server
pip install -r requirements-api.txt
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
make test-unit
# 預期：249 passed
```

---

## 每日使用流程

### 基本用法 — 分析特定股票

```bash
# 今日信號（收盤後跑）
make run DATE=$(date +%F) TICKERS="2330 2454 2317"

# 歷史回測（跳過時效檢查，--skip-freshness-check 已內建）
make run DATE=2024-03-04 TICKERS="2330"

# 不要 LLM，只看分數
PYTHONPATH=src python3 -m taiwan_stock_agent --date $(date +%F) --tickers 2330 --no-llm
```

### 批量掃描 — 找出當日強勢股

```bash
# 掃描（DATE 預設今天）
make scan DATE=$(date +%F)

# 掃描並存 CSV
PYTHONPATH=src python3 scripts/batch_scan.py --date $(date +%F) --tickers 2330 2454 2317 --save-csv
```

### API Server

```bash
make api
# http://localhost:8000/docs
```

### Demo 模式 — 不需 API key，注入合成資料驗證 pipeline

```bash
PYTHONPATH=src python3 -m taiwan_stock_agent \
  --date 2024-03-04 --tickers 2330 2454 2317 \
  --skip-freshness-check --no-llm --demo
```

### 每日自動化（cron）

```bash
crontab -e
```

```cron
# 每日信號：20:30 CST（台灣收盤後）
30 12 * * 1-5  cd /path/to/stock_investment && make run DATE=$(date +\%F) TICKERS="2330 2454 2317" >> logs/signal.log 2>&1
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

## 分析因子（Triple Confirmation Engine）

詳細說明（為什麼 / 量測什麼 / 怎麼用）見 [`docs/design/factor-guide.md`](docs/design/factor-guide.md)。

### Pillar 1 — 動能 Momentum｜最高 55 pts

| 因子 | 觸發條件 | 分數 |
|------|----------|------|
| VWAP 5日均 | 收盤 > 5日成交量加權均價 | +20 |
| 量能突破 | 今日量 > 20日均量 × 1.5，且收盤不收黑 | +20 |
| K線收盤強弱比 | (收–低)/(高–低) > 0.7 | +5 |
| 連漲天數 | 連續收高 ≥ 3 日 | +5 |
| 量能遞增趨勢 | 前 3 日成交量連續遞增 | +5 |

### Pillar 2 — 籌碼 Chip｜付費 45 pts / 免費 63 pts

**付費版（FinMind 分點資料）**

| 因子 | 觸發條件 | 分數 |
|------|----------|------|
| 淨買超家數差 | 3 日累計買方分點數 > 賣方 | +15 |
| 籌碼集中度 Top15 | 前 15 分點買量 / 總買量 > 35% | +15 |
| 前三無隔日沖 | Top3 買超分點無隔日沖標籤 | +10 |
| 外資分點偵測 | Top 買超含已知外資分點代碼 | +5 |
| ⚠ 風險扣分 | Top3 有隔日沖分點 | **-25** |

**免費版（TWSE opendata）**

| 因子 | 觸發條件 | 分數 |
|------|----------|------|
| 外資買賣超 | foreign_net_buy > 0 | +15 |
| 投信買賣超 | trust_net_buy > 0 | +10 |
| 自營商買賣超 | dealer_net_buy > 0 | +5 |
| 融資餘額變化 | margin_balance_change ≤ 0 | +10 |
| 三大法人同向 | 外資 + 投信 + 自營商全部淨買 | +5 |
| 外資連買天數 | 連續外資淨買 ≥ 3 日 | +5 |
| 投信連買天數 | 連續投信淨買 ≥ 3 日 | +5 |
| 自營商連買天數 | 連續自營商淨買 ≥ 3 日 | +3 |
| 融資使用率 | 使用率 < 20%（籌碼未被散戶鎖死）| +5 / −5 |
| ⚠ 風險扣分 | 融券暴增 AND 券資比 > 15% | **-10** |

### Pillar 3 — 空間 Space｜最高 45 pts

| 因子 | 觸發條件 | 分數 |
|------|----------|------|
| 20 日高點突破 | 收盤 ≥ 20 日高 × 0.99 | +20 |
| 60 日高點突破 | 收盤 ≥ 60 日高 × 0.99 | +10 |
| 均線多頭排列 | MA5 > MA10 > MA20 | +5 |
| MA20 斜率向上 | MA20 最近 5 日斜率為正 | +5 |
| RS vs 大盤 | 個股 5 日報酬 > TAIEX 5 日報酬 × 1.2 | +5 |

### 信號門檻

| 模式 | LONG | WATCH | CAUTION |
|------|------|-------|---------|
| 付費（有分點資料） | ≥ 70 | 40–69 | < 40 |
| 免費（TWSE proxy） | ≥ 60 | 35–59 | < 35 |
| 特殊規則 | 免費且 chip_pts = 0 → 強制 CAUTION | | |

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
| Phase 4.5 | ✅ 完成 | Makefile 本地開發環境 + 253 tests passing + DB integration 修復 |
| Phase 5 | ⏳ 規劃中 | Stripe 真實付款整合 + 社群信譽評分 + 台灣 Pay |

---

## 系統架構

```
Infrastructure  ──  FinMindClient      (fetch + Parquet cache + retry)
     │              ChipProxyFetcher   (TWSE opendata free-tier proxy)
     │              db.py              (PostgreSQL connection pool)
     │
Domain          ──  BrokerLabelClassifier    (D+2 reversal rate → 隔日沖 label)
     │              TripleConfirmationEngine  (三柱確認評分引擎)
     │              SignalOutcomeRepository   (信號結果追蹤)
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
| `CLAUDE.md` | AI 協作者必讀：架構決策、Phase Gates |
| `docs/design/factor-guide.md` | 所有因子詳解：為什麼、量測什麼、怎麼用 |
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
│   │   └── signal_outcome_repo.py  # Signal 結果追蹤（含 branch_codes）
│   ├── domain/
│   │   ├── triple_confirmation_engine.py  # 三柱確認評分核心
│   │   ├── broker_label_classifier.py     # 隔日沖分類核心
│   │   ├── bayesian_label_updater.py      # Phase 4：Beta-Bernoulli 社群勝率更新
│   │   └── models.py                      # Pydantic schemas
│   ├── agents/
│   │   ├── scout_agent.py          # 全市場異常掃描 + 板塊熱力圖
│   │   ├── chip_detective_agent.py # 籌碼偵探
│   │   └── strategist_agent.py     # 決策主控 + LLM reasoning + OHLCV RS proxy
│   └── api/
│       ├── main.py                 # FastAPI app（含 Phase 4 /outcome 端點）
│       ├── schemas.py              # API request/response models
│       └── auth.py                 # API key 驗證（DB + master key）
├── frontend/
│   └── index.html                  # Phase 3b Landing page
├── scripts/
│   ├── batch_scan.py               # 批量掃描
│   ├── settle_outcomes.py          # 每日結果回填 cron
│   ├── fetch_watchlist.py          # 觀察名單管理
│   ├── validate_free_tier.py       # TWSE free-tier 驗證
│   ├── validate_sbl_endpoint.py    # TWT93U SBL 端點可用性驗證
│   ├── validate_margin_utilization.py  # MI_MARGN 融資限額驗證
│   └── data_alignment_check.py     # 資料日期對齊驗證
├── tests/
│   ├── unit/                       # 179 tests，無需 DB/網路
│   └── integration/                # 需要 PostgreSQL
├── db/migrations/                  # SQL migrations（001–006）
└── docs/design/                    # 設計文件
```
