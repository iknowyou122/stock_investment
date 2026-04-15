# Telegram Bot + AI Optimize Agent — 設計文件

**日期：** 2026-04-15  
**狀態：** 待實作  
**範圍：** 自動化推播 + 盤中監控 + AI 週期優化

---

## 背景與問題

現有系統（`make plan` / `make trade` / `make report`）信號引擎邏輯已完整，但：

1. 指令複雜，使用者從未實際跟著系統操作
2. 需手動執行，無自動推播
3. 無盤中滾動式監控
4. 優化迴路（`make optimize`）需手動觸發

核心目標：讓系統真正「跑起來」，不需記得任何指令，結果自動推到 Telegram。

---

## 設計範圍

**包含：**
- `scripts/bot.py`：主 daemon，APScheduler + Telegram Bot
- `scripts/optimize_agent.py`：AI 優化 agent（Claude/Gemini/OpenAI 可選）
- `scripts/bot_setup.py`：一鍵安裝腳本
- `Makefile` 新增 `bot-setup` / `bot` targets

**不包含（不改動）：**
- `batch_scan.py`、`precheck.py`、`factor_report.py`、`optimize.py`
- `engine_params.json`、資料庫 schema、現有 API

---

## 架構

```
make bot-setup（首次）→ 寫入 .env（TG token + Chat ID）

make bot（每日啟動）
  → 互動選擇 LLM（Claude / Gemini / OpenAI，預設 Claude）
  → 啟動 scripts/bot.py daemon
       ├─ APScheduler（排程，見下表）
       └─ Telegram long-poll（指令處理）

scripts/optimize_agent.py（由 bot.py 呼叫）
  settle → factor_report → Claude API → 寫 engine_params.json → Telegram
```

---

## 排程

| 時間 | 動作 | 推播條件 |
|------|------|---------|
| 平日 09:05 | 全市場掃描，建立今日名單 | 固定推播開盤名單 |
| 平日 09:05–13:25，每 10 分鐘 | precheck 今日名單 | 有達進場條件才推 |
| 平日 10:05、11:05、12:05、13:05 | 全市場重掃，更新排名 | 有名單異動才推 |
| 平日 17:00 | 盤後報告 | 固定推播 |
| 週二、週五 18:00 | optimize_agent | 固定推播優化報告 |

---

## Telegram 訊息格式

### ① 09:05 開盤名單

```
📋 開盤名單 2026-04-15 09:05

🟢 6933 信驊     conf:59  入場 320–328  目標 358  停損 312
   ★ COILING_PRIME · 外資+投信
🟢 3704 合一     conf:59  入場 85–87   目標 95   停損 82
   ★ 突破20日高 · 法人買
⚡ 2402 中光電   conf:57  蓄積★★  建議 T-1/T-2 佈局

共 8 檔入監控，蓄積待噴發 3 檔
```

### ② 盤中進場訊號（有達標才推）

```
🔔 進場訊號 10:32
6933 信驊  現價 323 ✅ 入場區間
量能 1.4× 均量 | 大盤中性
建議：分批進場，停損 312
```

### ③ 名單異動（有變化才推）

```
📊 名單更新 11:05
✨ 新進：4949 XXX  conf:56
⬇️ 移出：2474 XXX  conf 降至 38
```

### ④ 17:00 盤後報告

```
📈 盤後報告 2026-04-15

━━ 今日命中率 ━━
昨日名單 8 檔 → 3 檔達進場條件 (37.5%)
✅ 6933 信驊   進場 322 → 收 335 (+4.0%)
✅ 3704 合一   進場 86  → 收 89  (+3.5%)
⏳ 2402 中光電  未達進場條件

━━ 隔日建倉名單 ━━
🟢 XXXX 公司A  conf:62  入場 xxx  目標 xxx  停損 xxx
🟢 XXXX 公司B  conf:59  ...

━━ 蓄積待噴發（T-1/T-2 佈局）━━
⚡ XXXX  COILING_PRIME · 連3日縮量 · 建議入場 xxx–xxx
⚡ XXXX  EMERGING_SETUP · 法人悄悄買 · 目標突破 20 日高
```

### ⑤ 週二/週五 18:00 優化報告

```
🤖 優化報告 2026-04-15

📊 近期勝率：52.3%（↑3.8%）
🔧 已套用 2 項調整：
  · 外資買超門檻 300→500 張（lift +1.2%）
  · RSI 健康區間下限 55→58

💬 LLM 判斷：
外資大單過濾雜訊效果明顯，RSI 提高後假訊號減少。
COILING_PRIME 近期勝率最高(61%)，建議提高權重（下次評估）。

⚙️ 引擎版本：v2.2 → v2.3
```

---

## Telegram 互動指令

| 指令 | 功能 |
|------|------|
| `/top` | 今日 Top 10 名單 |
| `/status` | 上次掃描時間、名單數量、監控狀態、當前 LLM |
| `/pause` | 暫停盤中推播 |
| `/resume` | 恢復盤中推播 |
| `/params` | 顯示當前 engine_params.json |
| `/optimize` | 手動觸發優化（任何時間可用） |
| `/approve` | 套用低信心 LLM 建議 |
| `/rollback` | 還原上一版參數 |

---

## optimize_agent 決策邏輯

### 執行流程

1. `settle` — 補填近期 T+1/T+3/T+5 結果
2. `factor_report` — 產出 lift 分析 + walk-forward grid search
3. 解析輸出（lift 表、勝率、各因子貢獻度）
4. 呼叫 LLM API（傳入當前參數 + 分析報告）
5. 信心判斷 → auto-apply 或推 Telegram 等確認
6. 推送報告

### LLM Prompt 結構

```
當前參數：[engine_params.json 全文]
近期成果：樣本數 N、整體勝率 X%、各 flag 勝率
因子分析：[factor_report 輸出]

任務：
1. 指出哪些因子表現偏弱（lift < 1.0）
2. 提出具體參數調整（限白名單內）
3. 每個調整說明理由和預期改善
4. 給出整體信心分數（0–100）

輸出格式：JSON
{
  "confidence": 82,
  "changes": [
    {"param": "rsi_min", "from": 55, "to": 58, "reason": "..."}
  ],
  "summary": "本次調整重點..."
}
```

### 安全機制

| 規則 | 說明 |
|------|------|
| 參數白名單 | 只改 `engine_params.json` 中 `tunable: true` 的欄位 |
| 單次變幅上限 | 每個參數每次最多 ±20% |
| 信心門檻 | confidence ≥ 75 → auto-apply；< 75 → 推 Telegram 等 `/approve` |
| 樣本數保護 | 樣本 < 20 個信號時，只報告不調整 |
| Changelog | 每次調整寫入 `config/param_history.json` |
| 回滾 | `/rollback` 指令還原上一版參數 |

---

## 啟動流程

### `make bot-setup`（首次）

```
 股票信號機器人 — 初始設定
────────────────────────────────
[1/3] 安裝依賴套件 (python-telegram-bot, apscheduler)...  ✅
[2/3] Telegram 設定
  Bot Token: （互動輸入）
  Chat ID:   （互動輸入）
  測試訊息發送中... ✅ 收到
[3/3] 寫入 .env  ✅

設定完成，執行 make bot 啟動
```

### `make bot`（每日）

```
 股票信號機器人 v1.0
────────────────────────────────
optimize_agent LLM：
  [1] Claude (claude-sonnet-4-6)   ← 預設
  [2] Gemini (gemini-2.5-flash)
  [3] OpenAI (gpt-4o)
選擇 (Enter = 1)：

啟動中...
  ✅ Telegram Bot 連線
  ✅ 排程載入（下次任務：17:00 盤後報告）
  ✅ 監控中（Ctrl+C 停止）
```

### 盤中 CLI 畫面（Rich）

```
┌─ 股票信號機器人 ──────────────── 11:24:03 ─┐
│ 今日名單：8 檔   已推播：2   大盤：中性     │
├────────────────────────────────────────────┤
│  Ticker  公司      conf  現價   狀態        │
│  6933    信驊       59   323   ✅ 進場區間  │
│  3704    合一       59    87   ⏳ 等待      │
│  2402    中光電     57    42   ⚡ 蓄積      │
├────────────────────────────────────────────┤
│ 下次全市場掃描：12:05  下次 precheck：11:30 │
│ LLM：Claude   optimize：週五 18:00         │
└────────────────────────────────────────────┘
```

---

## 新增檔案清單

| 檔案 | 說明 |
|------|------|
| `scripts/bot.py` | 主 daemon（APScheduler + Telegram Bot） |
| `scripts/optimize_agent.py` | AI 優化 agent |
| `scripts/bot_setup.py` | 一鍵安裝腳本 |
| `config/param_history.json` | 參數變更 changelog |

### Makefile 新增

```makefile
bot-setup:
    $(PYTHON) scripts/bot_setup.py

bot:
    $(PYTHON) scripts/bot.py
```

---

## 依賴套件

```
python-telegram-bot>=21.0
apscheduler>=3.10
```

（其餘依賴：`anthropic`、`google-generativeai`、`openai` 已在 extras 中）

---

## 不實作項目

- macOS launchd 自動啟動（手動 `make bot` 即可）
- Web dashboard
- 多用戶 / 多 chat 支援
- 回測結果推播（已有 `make backtest`）
