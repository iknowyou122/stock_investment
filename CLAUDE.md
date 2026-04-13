## Project Context — Read First

Before doing any work in this repo, read these files in order:

1. `docs/design/signal-engine-design.md` — full technical spec: architecture decisions,
   Triple Confirmation formula, FinMind data constraints, broker label classifier logic,
   phase gates, and backtest success criteria. This is the source of truth for WHY
   the code is structured the way it is.
2. `docs/design/ceo-plan.md` — product vision, scope decisions, what was accepted vs
   deferred, and the 12-month ideal state. Read before proposing scope changes.
3. `DESIGN.md` — UI/visual design system for the Phase 3b landing page.
   Read before touching any frontend code.

If you skip these and make architectural decisions that contradict the design doc,
you will create drift that is expensive to fix.

## Phase Gates

| Phase | Status | Gate condition |
|-------|--------|----------------|
| Pre-spike | ✅ Done | `data_alignment_check.py` + `spike_validate.py` written |
| Phase 1 | ✅ Done | Broker label classifier + batch classifier + outcome recorder built |
| Phase 2 | ✅ Done | Triple Confirmation Engine ✅ · ScoutAgent ✅ · Round 2 deepening ✅ · Signal track record ✅ · Sector heat map ✅ |
| Phase 3a | ✅ Done | StrategistAgent CLI + multi-LLM support (Gemini/Claude/OpenAI) + TWSE free-tier proxy |
| Phase 3b | ✅ Done | FastAPI + auth + rate limiting ✅ · Real DB routes ✅ · /track-record ✅ · signal_outcomes table ✅ · /register endpoint ✅ |
| Phase 4 | ✅ Done | `POST /v1/signals/{signal_id}/outcome` ✅ · `BayesianLabelUpdater` ✅ · migrations 004-006 ✅ · pro-tier payment stub ✅ · 253 tests passing ✅ |
| Phase 4.5 | ✅ Done | Makefile local dev ✅ · DB integration fix (json.dumps metadata) ✅ · Gemini 2.5 Flash wired end-to-end ✅ |
| Phase 4.6 | ✅ Done | v2 Triple Confirmation Engine ✅ · Gate layer (2-of-4) ✅ · 3-Pillar + Risk Adjust ✅ · TAIEX regime gate (63/68/73) ✅ · migration 007 (scoring_version) ✅ · 185 unit tests passing ✅ |
| Phase 4.7 | ✅ Done | `make scan` 路徑修正 ✅ · T86 週末跳過（suppress spurious WARNINGs）✅ · 動態 watchlist（728 檔，上市+上櫃 半導體/光電/電子，每日 cache）✅ |
| Phase 4.8 | ✅ Done | 互動式產業選單（數字代號選擇）✅ · 全市場 industry_map cache（ticker→industry）✅ · 日期自動判斷（17:00 切換前一/當日交易日）✅ · T86 rate-limit retry with backoff ✅ |
| Phase 4.9 | ✅ Done | Gate 層可觀測性（GATE_PASS/FAIL/SKIP/MET flags）✅ · Gate VOL 門檻 1.3→1.2 ✅ · RS 日期交集對齊 ✅ · Flag 中文翻譯（_translate_flag）✅ · 輸出條列式換行 ✅ · T86 rate-limit 改 try/except ValueError + retry ✅ · 批次掃描互動式 LLM 選單 ✅ · 兩階段 LLM（Phase 1 全量 deterministic → Phase 2 top N with LLM）✅ |
| Phase 4.10 | ✅ Done | avg_20d_volume bug 修正（一直回傳 0 → 注入真實 20 日均量）✅ · TPEx T86 fallback（上櫃股票三大法人資料）✅ · RSI(14) 計分（55–70 → +4 pts）✅ · 突破確認量能（breakout_volume_pts +3 pts）✅ · 產業相對排名（同產業 top 20% → +5 pts post-processing）✅ · 信號持續加分（前日 CSV 得分 ≥50 → +5 pts）✅ · VolumeProfile POC proxy 改為最大量日收盤價（非 20 日最高價）✅ · `scripts/build_broker_labels.py`（付費 FinMind 用）✅ · `scripts/analyze_outcomes.py`（win-rate 分析）✅ · `make build-labels` / `make analyze` 目標 ✅ |
| Phase 4.11 | ✅ Done | Factor Optimization Loop ✅ · DB migration 008（`score_breakdown JSONB`, `source`, `factor_registry`, `engine_versions` 表）✅ · `signal_recorder.py`（寫入 DB）✅ · `scoring_replay.py`（無需重跑引擎的 Grid Search）✅ · `config/engine_params.json`（可調參數白名單）✅ · `scripts/backtest.py` + `make backtest`（歷史回測）✅ · `scripts/daily_runner.py` + `make daily` / `make settle`（每日掃描+結算）✅ · `scripts/factor_report.py` + `make factor-report`（Lift 分析 + Walk-forward Grid Search + 殘差分析）✅ · `scripts/apply_tuning.py` + `make tune-review`（互動式 Review Gate）✅ · `scripts/test_factor.py` + `make test-factor`（實驗因子 Sandbox）✅ · `scripts/optimize.py` + `make optimize`（一鍵優化迴路）✅ · 213 unit tests passing ✅ |
| Phase 4.12 | ✅ Done | Rich UI for `batch_scan`（progress bar、Panel 掃描頭、ROUNDED 產業表、彩色 confidence）✅ · `label_repo` + `industry_map` 傳入 `run_batch`/`_run_phase`（sector rank + persistence 後處理移入 `run_batch`）✅ · `rsi_momentum_pts` +4（RSI 14, 55–70 健康動能）✅ · `breakout_volume_pts` +3（突破 + 量能 >1.5× 均量確認）✅ · Pillar 1 上限 35→39、Pillar 3 上限 35→38 ✅ · TPEx T86 fallback（上櫃三大法人）✅ · `FinMindClient.fetch_ohlcv` yfinance fallback（FinMind 402 時自動切 `.TW`/`.TWO`）✅ · `scripts/build_broker_labels.py` + `scripts/analyze_outcomes.py` ✅ |
| Phase 4.13 | ✅ Done | `make backtest` 效能優化 ✅ · Margin/SBL/DayTrade 日期級記憶體 cache（每日各 1 次 HTTP → 服務所有 ticker）✅ · TAIEX history 同日期共用（StrategistAgent `_taiex_cache`）✅ · default delay 0.5s→0.1s ✅ · Rich 進度條 + ETA（backtest 主迴路）✅ · 全 CLI 互動式 Rich UI（backtest/daily_runner/analyze/optimize）✅ · `requirements.txt` 加入 rich + yfinance ✅ · 197 unit tests passing ✅ |
| Phase 4.14 | ✅ Done | `make scan` 共用客戶端優化（shared FinMindClient + ChipProxyFetcher，日期級快取跨 worker 共享）✅ · `make precheck` 盤前/盤中確認（TWSE MIS 即時報價 → 確認 entry±3%、量能、大盤）✅ · 197 unit tests passing ✅ |
| Phase 4.15 | ✅ Done | T-2 策略驗證（`entry_delay_analysis.py` D+2 勝率 55.6% > D+0 38.5%）✅ · 軌跡感知持續加分（RISING +7 / STABLE +5 / DECLINING +0，讀近 3 天 CSV）✅ · `EMERGING_SETUP` flag（WATCH + MA排列 + 法人買 + 未突破）✅ · `make precheck` 蓄積中監控表 ✅ · MIS API `z=-` fallback（bid→hl_mid→open）✅ · Settlement 批次優化（executemany）✅ · 跨機器 DB 備份還原（`make db-dump/restore`）✅ · 208 unit tests passing ✅ |
| Phase 4.16 | ✅ Done | `make review`（盤後 T+1 復盤）✅ · `make daily`（scan + review 一鍵）✅ · `make show`（上下鍵互動選日期查歷史結果）✅ · migration 009（stop_loss/intraday_high/low/entry_success/ab_candidate_score + ab_competitions 表）✅ · BATCH SCAN RESULTS 加 Upside% 欄位 + 標題日期 ✅ · CSV 改覆寫模式（防重複 ticker）✅ · Target < Entry 雙層修正（poc_proxy 排除恐慌拋售日 + floor = close×1.05）✅ · `FinMindClient.fetch_ohlcv` 預設改 `adjusted=False`（防除權還原價污染快取）✅ · `--sectors` 非互動跳過選單顯示（make daily 可背景執行）✅ · `questionary` 加入 requirements.txt ✅ · 224 unit tests passing ✅ |

**免費 vs 付費因子說明：**

| 因子 | 免費可用 | 需付費 FinMind | 說明 |
|------|----------|----------------|------|
| Pillar 1 動能（RSI、突破、均線）| ✅ | — | TWSE/TPEx OHLCV 政府公開資料 |
| Pillar 2B 三大法人（外資+投信+自營）| ✅ | — | TWSE T86 + TPEx T86 政府端點 |
| Pillar 2A 分點籌碼（隔日沖/波段贏家）| ✗ | ✅ | FinMind `TaiwanStockBrokerTradingStatement` |
| Pillar 3 結構（支撐/壓力/融資融券）| ✅（部分）| — | MI_MARGN 政府資料；SBL 目前降級為 0 |
| 產業排名後處理加分 | ✅ | — | 本機 industry_map cache |
| 信號持續加分（軌跡感知）| ✅ | — | 近 3 天 CSV；RISING +7 / STABLE +5 / DECLINING +0 |
| EMERGING_SETUP 蓄積偵測 | ✅ | — | WATCH + MA排列 + 法人買 + 未突破 20 日高 |

**Phase 5 (next):**
- Real Stripe webhook handling (requires production Stripe account + deployment)
- Community reputation scoring and spam/bot filtering
- 台灣Pay integration

Do not implement Phase N+1 without the Phase N gate condition being met.

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available gstack skills:
- /office-hours
- /plan-ceo-review
- /plan-eng-review
- /plan-design-review
- /design-consultation
- /review
- /ship
- /land-and-deploy
- /canary
- /benchmark
- /browse
- /qa
- /qa-only
- /design-review
- /setup-browser-cookies
- /setup-deploy
- /retro
- /investigate
- /document-release
- /codex
- /cso
- /careful
- /freeze
- /guard
- /unfreeze
- /gstack-upgrade

If gstack skills aren't working, run `cd .claude/skills/gstack && ./setup` to build the binary and register skills.

## Design System
Always read DESIGN.md before making any visual or UI decisions.
All font choices, colors, spacing, and aesthetic direction are defined there.
Do not deviate without explicit user approval.
In QA mode, flag any code that doesn't match DESIGN.md.
