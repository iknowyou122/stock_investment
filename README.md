# 分點情報 — Taiwan Stock Signal Engine

台股籌碼面信號系統。每日收盤後分析指定股票，輸出 **LONG / READY / WATCH / CAUTION** 四級信號，幫助判斷主力是否在進場。

> **Last updated:** 2026-04-16
> **Current focus:** Phase 4.19 完成（Bot 即時看板 Watchlist Prices + 市場資料 30s 刷新）
> **Tests:** 224 unit passed
> **Scan watchlist:** 全市場上市+上櫃（每日自動更新），互動式選擇產業 + LLM 兩階段優化

---

## Telegram Bot 指令

系統整合了 Telegram Bot，讓你可以透過手機遠端執行與接收通知。指令命名與 `Makefile` 保持對齊：

| 指令 | 對應 Makefile | 功能說明 |
| :--- | :--- | :--- |
| `/plan` | `make plan` | **擬定計畫**：全市場掃描，更新今日潛力名單並推播。 |
| `/trade` | `make trade` | **執行交易**：盤中即時檢查名單中的標的是否達進場條件。 |
| `/report` | `make report` | **產出報告**：結算昨日獲利與勝率，並產出盤後總結。 |
| `/optimize` | `make optimize` | **參數優化**：啟動 AI Agent 分析因子表現並提出優化建議。 |

### 其他常用指令
- `/top`: 查看今日名單（不重新掃描）。
- `/status`: 檢查系統狀態、名單檔數、LLM 設定。
- `/approve`: 套用待確認的 AI 優化建議。
- `/rollback`: 還原上一版參數或捨棄建議。
- `/help`: 顯示完整指令清單。

---

## 執行步驟

| 階段 | 時間 | 指令 | 目的 |
| :--- | :--- | :--- | :--- |
| **1. 擬定計畫** | 每日收盤後 (17:00+) | `make plan` | 全市場掃描，找出明天的潛力標的（儲存至 CSV）。 |
| **2. 執行交易** | **隔日盤中** (09:00-13:30) | `make trade` | 連線即時報價，確認買點、量能與大盤。 |
| **3. 產出報告** | 隔日收盤後 | `make report` | 結算昨日訊號的獲利，檢討勝率與 A/B 參數。 |
| **4. 一鍵流程** | 每日收盤後 | `make flow` | 同時執行今日 `plan` 與昨日 `report`。 |

---

## 核心指令與運作邏輯

### 1. 擬定計畫 `make plan`（盤後選股）
**背後工作：**
- **全市場掃描**：自動抓取今日收盤後的量價、法人買賣超、融資券等數據。
- **流動性過濾**：執行 v2.2a Gate，排除成交額不足（上市<20M, 上櫃<8M）的冷門股。
- **三大柱評分**：針對每檔股票進行動能、籌碼、空間的維度運算。
- **型態識別**：區分出「強勢突破 (LONG)」與「蓄勢待發 (READY/蓄積)」標的。
- **產出計畫**：將建議進場價 (Entry)、停損、目標價存入當日 CSV 與資料庫。

### 2. 執行交易 `make trade`（盤中即時）
**背後工作：**
- **即時連線**：連線 TWSE MIS API 獲取盤中最新成交價與委買委賣。
- **進場確認**：自動檢查現價是否仍處於建議進場價的 **±3% 區間**，漲太高會建議 `SKIP`。
- **量能推算**：根據當前時間比例，推算今日預估量，確保人氣有跟上。
- **大盤守護**：監控加權指數，若跌幅過大會發出 `WARN` 警示以降低曝險。

### 3. 產出報告 `make report`（獲利結算）
**背後工作：**
- **自動結算**：抓取今日收盤價，對比昨日訊號，判斷是否有跌入買點 (O/X) 且收盤獲利。
- **勝率追蹤**：更新過去 14 天的滾動勝率，檢視因子是否在當前盤勢失效。
- **參數進化**：若勝率低於 50%，自動啟動 **A/B 參數競賽**，微調門檻以尋找更優組合。

### 4. 一鍵流程 `make flow`（完整功課）
**背後工作：**
- **先結算，後規劃**：先執行 `make report` 檢討昨日表現，再執行 `make plan` 擬定明天計畫。
- **數據閉環**：確保每日的分析資料與獲利紀錄都能精確對接，達成系統的自我迭代。

---

## 如何解讀輸出

| 欄位 | 說明 |
|------|------|
| **信號** | `LONG` 三柱均衡；`READY` 蓄積中；`WATCH` 部分到位；`CAUTION` 風險過高 |
| **Confidence** | 三柱總分（0–100），信號強度 |
| **進場區間 (Entry)** | `close × 0.995` 為建議掛單價，±3% 為執行區間 |
| **停損 (Stop)** | T+0 收盤價參考 |
| **目標 (Target)** | `max(poc_proxy × 1.05, close × 1.05)` |

### 常見 flags

| Flag | 意思 | 影響 |
|------|------|------|
| `LOW_LIQUIDITY` | 均量低於 1000 張或成交額不足 | 強制 CAUTION，confidence=0 |
| `COILING_PRIME` | **特選蓄積**：極致壓縮 + 法人連買 | 顯示為「蓄積★」 |
| `COILING` | **蓄積中**：符合 VCP 型態與帶寬壓縮 | precheck 重點監控 |
| `PERSIST_RISING` | 連 3 天遞增訊號 | +7 分加成 |

---

## Telegram Bot 即時看板

`make bot` 啟動後會在終端機顯示四格 Rich 看板，**每 30 秒自動刷新**：

| 格子 | 內容 |
|------|------|
| **Bot Status** | Watchlist 名單數、上次掃描時間、Alert 狀態、Last Cmd、以及 **Watchlist Prices 即時股價表** |
| **Market Monitor** | TAIEX 即時指數 + 28 個 TWSE 產業指數熱力圖 |
| **Global Markets** | 外匯、美股四大指數、VIX、商品、比特幣 |
| **Activity Log** | 滾動 12 行執行紀錄 |

**Watchlist Prices 欄位說明：**

| 欄位 | 說明 |
|------|------|
| 代號 | 青色 = LONG，黃色 = WATCH |
| 現價 | 盤中彩色顯示；盤後 fallback 前收價（暗灰色） |
| 漲跌% | ▲紅漲 ▼綠跌（台灣慣例）；盤後顯示 `--` |
| 信心 | 引擎評分 0–100 |
| vs進場 | 現價相對 entry_bid 的漲跌幅；正=紅、負=綠 |

---

## 每日使用流程

### 標準每日工作流

```
收盤後 (14:00+)     →  make flow            一鍵完成：擬定明日計畫 + 結算昨日報告
  ├─ make plan        掃描全市場 + 存 CSV + 寫 DB
  └─ make report      T+1 結算 + 14日滾動勝率 + A/B 競賽
隔日盤中 (09-13:30) →  make trade           T+1 即時確認（昨日計畫 + 即時報價）
                    →  make precheck-t2     T+2 進場確認（勝率最高窗口）
週末               →  make settle          補填長期 T+3/5 結算價
```

**T+2 進場策略：**
根據回測，D+2（訊號後 2 天）進場勝率 **55.6%** 遠高於 D+0 的 **38.5%**。
建議：`make plan` 產出計畫後，隔兩天再用 `make precheck-t2` 確認進場。

---

### 詳細用法

#### 1. 擬定計畫 (Plan)
```bash
make plan
make plan SECTORS="1 4"        # 掃描指定產業
make plan LLM=gemini LLM_TOP=5 # 前 5 名啟動 AI 分析
```

#### 2. 執行交易 (Trade)
```bash
make trade
make trade TOP=10 MIN_CONF=50 # 只看高信心標的
```

#### 3. 產出報告 (Report)
```bash
make report
make report DATE=2026-04-13 # 結算特定日期
```

#### 4. 查詢歷史
```bash
make show                           # 上下鍵互動選日期
make show SHOW_DATE=2026-04-08      # 直接指定
```

---

## 系統維護

- `make test`: 執行單元測試。
- `make migrate`: 更新資料庫結構。
- `make db-dump`: 備份資料庫。
- `make backtest`: 歷史數據回測。
- `make factor-report`: 因子力道分析。
- `make optimize`: 超參數自動優化。

---

## 目錄結構

```
stock_investment/
├── src/taiwan_stock_agent/
│   ├── infrastructure/
│   │   ├── finmind_client.py       # FinMind API (cache/retry/fallback)
│   │   ├── twse_client.py          # TWSE opendata free-tier proxy
│   ├── domain/
│   │   ├── triple_confirmation_engine.py  # v2 引擎 (Liquidity/Coiling/Scoring)
│   ├── agents/
│   │   └── strategist_agent.py     # 決策主控 + LLM reasoning
├── scripts/
│   ├── batch_plan.py               # make plan 核心腳本
│   ├── trade.py                    # make trade 核心腳本
│   ├── report.py                   # make report 核心腳本
│   ├── bot.py                      # Telegram bot daemon + Rich 即時看板
│   └── daily_runner.py             # make flow / settle 核心腳本
└── docs/design/                    # 因子規格與產品計畫
```
