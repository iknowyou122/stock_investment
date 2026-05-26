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
| Phase 4.17 | ✅ Done | v2.2a 流動性金額門檻（TSE 2000萬 / TPEx 800萬 日均成交金額，自動適應高低價股）✅ · v2.2b COILING 蓄積偵測器（Gate 6硬條件 + Quality Score 5 K-of-N；flag=COILING/COILING_PRIME）✅ · BB/DMI 因子整合（dmi_initiation_pts / bb_squeeze_breakout_pts / adx_exhaustion / dmi_divergence）✅ · `make scan` 市場別自動識別（TSE/TPEx，market_map cache）✅ · StrategistAgent.run() 接收 market 參數 ✅ · batch_scan 蓄勢標的 inline 標記 ✅ · precheck 蓄積監控表顯示 COILING 強度（蓄積★/蓄積/雛形）✅ · 240 unit tests passing ✅ |
| Phase 4.18 | ✅ Done | **Telegram Bot 指令命名對齊**：`/scan`→`/plan`, `/precheck`→`/trade`, `/postmarket`→`/report` ✅ · 同步更新 `README.md`, `CLAUDE.md` 與 `/help` 說明 ✅ |
| Phase 4.19 | ✅ Done | **Bot 即時看板升級**：Watchlist Prices 區塊（現價/漲跌%/信心/vs進場）✅ · 市場資料刷新 60s→30s ✅ · `_fetch_watchlist_prices_sync` MIS API 批次查詢（TSE/TPEx 自動辨識）✅ · 盤後 fallback 前收價（is_live 旗標）✅ · Market Monitor + Global Markets subtitle 加 "Last update" 標示 ✅ |
| Phase 4.20 | ❌ Removed (2026-04-22) | **蓄積雷達（AccumulationEngine）已移除**：standalone coil 系統（`accumulation_engine.py`, `coil_scan.py`, `coil_backtest.py`, `coil_factor_report.py`, `optimize_coil.py`, `coil_monitor.py`）全部刪除，因為無法預測蓄積期長短，追蹤勝率意義有限。Pillar 4 嵌入式 COILING 偵測器保留於 `triple_confirmation_engine.py` 作為壓縮型態評分因子。|
| Phase 4.16 | ✅ Done | `make review`（盤後 T+1 復盤）✅ · `make daily`（scan + review 一鍵）✅ · `make show`（上下鍵互動選日期查歷史結果）✅ · migration 009（stop_loss/intraday_high/low/entry_success/ab_candidate_score + ab_competitions 表）✅ · BATCH SCAN RESULTS 加 Upside% 欄位 + 標題日期 ✅ · CSV 改覆寫模式（防重複 ticker）✅ · Target < Entry 雙層修正（poc_proxy 排除恐慌拋售日 + floor = close×1.05）✅ · `FinMindClient.fetch_ohlcv` 預設改 `adjusted=False`（防除權還原價污染快取）✅ · `--sectors` 非互動跳過選單顯示（make daily 可背景執行）✅ · `questionary` 加入 requirements.txt ✅ · 224 unit tests passing ✅ |
| Phase 4.21 | ✅ Done | **預突破信號引擎重新設計**：Gate 改為 4 硬條件（85–99% 區間 + BB≤15% + 流動性 + 大盤非下跌）✅ · Pillar 3 完全重寫（壓縮質量因子：proximity/bb_compression/ma_convergence/consolidation_weeks/inside_bar/prior_advance）✅ · 市場情感系統（BreadthData+MarketSentiment+compute_sentiment，標籤多頭熱絡/中性震盪/偏空謹慎）✅ · sentiment_client.py（TWSE breadth + Yahoo RSS）✅ · batch_plan.py 產業分組輸出（industry_strength計算、按產業強度排序）✅ · bot.py sentiment widget（_fetch_sentiment_sync + 市場輿情面板）✅ · test_market_sentiment.py（6/6測試通過）✅ · test_triple_confirmation_engine_v2_fix.py Pillar3 測試 ✅ |
| Phase 4.22 | ✅ Done | **Early Signal Scoring Fixes**：Sector rank 分級加分（top5%+10/top10%+7/top20%+5）✅ · Near-high 首日補償（NEAR_HIGH_COIL +4，proximity_pts=12 首次出現）✅ · Uptrend/Neutral proximity_pts=12 降 LONG 門檻 5 pts ✅ · `test_persistence_bonus.py` import 修正 ✅ · ticker 次要排序確保可重現 ✅ · 新增 20 個單元測試 ✅ |
| Phase 4.23 | ❌ Removed (2026-04-22) | **蓄積信號追蹤系統已移除**：`coil_monitor.py`, `db/coil_track.db`, 5 份 coil_*.csv 快取全部刪除。`batch_plan.py` Pass 2 整合移除。`bot.py` coil panel 移除。`make flow` 改為 plan + report。 |
| Phase 4.24 | ✅ Done | **Dynamic BB Threshold + Momentum Walk**：G2 門檻改為 60 日分位數 ≤35p（fallback 絕對 ≤15%）✅ · `ma5_walk_pts` Pillar 1 因子（≥80% 收在 MA5 上 → +2，MA5_WALK flag）✅ · `bb_upper_walk_pts` Pillar 3 因子（proximity=12 + 收盤貼 BB 上軌 3/5 天 + 上揚 → +3，BB_UPPER_COIL flag）✅ · SurgeRadar `_score_ma5_walk`（+2/−1，MA5_WALK/MA5_BREAK）✅ · `_score_bb_upper_walk`（MOMENTUM_WALK tag / BB_UPPER_EXHAUSTION −3）✅ · `raw_max_pts` 85→87 ✅ · 28 新單元測試 ✅ |
| Phase 4.25 | ✅ Done | **Market Heat Engine（四層架構）**：Layer 1 `market_heat.py`（28 大產業 1d/5d/20d 動量 + 廣度 + 領頭羊 + 輪動）✅ · Layer 2 `concept_heat.py` + `config/concepts.json`（10 個策展 basket：AI GPU、CPO、HBM、機器人、重電、車電…）✅ · Layer 3 `international_signals.py`（NVDA/SOX/TSM ADR/AVGO/ANET 隔夜訊號 → 台股產業+概念順風映射）✅ · LLM 整合 `theme_analyzer.py`（OpenAI/Claude/Gemini 多模型，每日市場主軸 + 主流題材 + 輪動方向）✅ · `tight_base_backtest.py` v1 baseline（D+1 命中率 2.2%）✅ · `tight_base_v2_backtest.py` 熱度感知版（bonus≥5 命中率 5.1%，bonus≥6 命中率 6.2%，D+1~3 命中率 14.8% = 2.8x lift）✅ · `make heat-scan` / `make pre-surge` / `make tight-base-bt` / `make tight-base-v2` ✅ · `docs/design/market-heat-engine.md` ✅ |
| Phase 4.26 | ✅ Done | **Surge 評分優化 + Pre-Surge 整合移除**：Vol ratio 重新分級（5x+ VOL_SURGE +8 / 3-5x VOL_IDEAL +10 / 2-3x VOL_SOLID +8，移除舊懲罰邏輯）✅ · 法人買超權重翻轉（第1天 4→8 / 第3天+ 10→6，強化起漲點偵測）✅ · RSI>70 從 0 改為 +3（RSI_BREAKOUT，起漲點動能確認）✅ · `raw_max_pts` 95→90 ✅ · SURGE_GAMMA 門檻拉至 40（等同移除，低品質訊號不輸出）✅ · `pre_surge.py` 刪除，新增 `update_market_heat.py`（僅做熱度快照，每日 17:05 由 `_job_surge_postmarket` 自動觸發）✅ · Bot 移除 pre_surge 面板 / `/presurge` 指令 / 17:15 排程，Surge 面板佔滿右欄 ✅ · `_DEFAULT_SECTOR_NAMES` 移除生技醫療業，加入玻璃陶瓷（AI 供應鏈聚焦）✅ · `make heat-update` ✅ |
| Phase 4.27 | ✅ Done | **Surge 訊號追蹤 → D+1 確認 → T+2 進場提醒**：`scripts/surge_tracker.py`（`save_watch` / `check_d1` / `format_d1_alert`）✅ · D+0 盤後存 ALPHA 訊號至 `data/surge_tracking/watch_YYYY-MM-DD.json` ✅ · D+1 驗條件（close_d1 ≥ close_d0×0.97 + 非強力黑 K），通過則印出 T+2 進場候選表 ✅ · `surge_scan.py` CSV 加入 `close_price` 欄位 ✅ · tracker 整合進 `surge_scan.py` main()，`make surge` 一鍵完成掃描→存 watch→驗 D+1→印候選表（`NOTIFY=1` 時同步推播 Telegram）✅ · `_job_surge_postmarket` 亦整合 tracker（scan → save_watch → check_d1 → heat-update）✅ · 盤中模式（`--intraday`）不觸發 tracker ✅ |
| Phase 4.28 | ✅ Done | **籌碼集中三因子**：`_score_inst_synergy`（土洋合作 +5 / 法人買超佔比 ≥15% +6 / ≥10% +4 / ≥5% +2）✅ · `_score_margin_declining`（融資今日下降 +3，MARGIN_DECLINING flag）✅ · `_score_ownership_concentration`（集保大戶400張+週增 +5 / 散戶100張-週減 +3，TDCC via FinMind API，無 API Key 時靜默略過）✅ · `ChipProxyFetcher.fetch()` 新增 `today_volume` 參數（算法人佔比）✅ · `TWSEChipProxy` 新增 `inst_buy_pct` / `foreign_and_trust_both_buy` / `large_holder_chg_pct` / `retail_holder_chg_pct` 欄位 ✅ · `raw_max_pts` 90→110 ✅ · 477 unit tests passing ✅ |
| Phase 4.29 | ✅ Done | **Surge 掃描品質修正**：G3 `close_strength_min` 0.5→0.4（捕捉 cs≈0.43 強勢股如元太 5/6）✅ · 產業鎖定（移除互動式選單，`make surge` 固定使用 `_DEFAULT_SECTOR_NAMES`，避免漏掃光電業等）✅ · `concepts.json` v1.1（清除 17 個錯誤/重複 ticker，移除永豐實/榮剛/為升等非 AI 供應鏈股票）✅ · Makefile `surge` 日期 bug 修正（`DATE` 全域預設值蓋掉 `_default_date()` → 改用 `SURGE_DATE` 空白變數，五點前正確取前一交易日）✅ |
| Phase 4.33 | ✅ Done | **五大籌碼面因子（ABCDE）**：A `foreign_trend_accel` 外資W1/W2趨勢加速比（`_fetch_institution_consecutive_days` 10-tuple）✅ · B `short_cover_rate` 融券回補率空頭投降（`_fetch_short_cover_rate`，MI_MARGN 6-tuple）✅ · C `large_holder_2w_trend` 400張+大戶兩週持股趨勢（TDCC 5-tuple，取兩週前快照差值）✅ · D `inst_accel_3d_10d` 法人近3日/近10日加速比（同 10-tuple 計算）✅ · E 台指期外資淨多單 Gate（`fetch_taifex_context`，LONG→WATCH 降級 + SurgeRadar −5pts `TAIFEX_FUTURES_BEARISH`）✅ · `TWSEChipProxy` 新增 4 欄位 + `_ScoreBreakdown` 新增 4 分項 ✅ · `surge_params.json` `raw_max_pts` 110→127 ✅ · 524 unit tests passing ✅ |
| Phase 4.34 | ✅ Done | **產業輪動雷達**：`config/rotation_map.json`（44節點：34 TWSE產業+10概念basket；63有向邊，含 `lag_weeks`/`conf` 權重）✅ · `scripts/rotation_tracker.py`（讀近10個熱度快照，計算 HOT/COOLING/EMERGING/COLD 狀態，依邊評分輸出 `data/market_heat/rotation_signal.json`）✅ · `update_market_heat.py` 自動觸發 rotation tracker ✅ · `batch_plan.py` HTML 頭部新增「📡 輪動候選」列（hover tooltip 顯示觸發來源+預期週期）和「🔻 降溫中」列 ✅ · `make rotation` 目標 ✅ · fix: `batch_plan._fetch_plan_chart` + `surge_scan._fetch_chart_candles` 補上 yfinance "possibly delisted" 警告抑制 ✅ · 524 unit tests passing ✅ |
| Phase 4.35 | ✅ Done | **CSV → DB 全面遷移**：移除所有 scan CSV / surge CSV 輸出路徑 ✅ · 新建 `surge_signals` 表（爆量掃描結果，UNIQUE on analysis_date+ticker）+ `surge_watch` 表（D+1 追蹤，替代 JSON 檔案）✅ · `db/migrations/010_surge_to_db.sql`（建表 + source_valid 約束加入 'surge'）✅ · `src/taiwan_stock_agent/infrastructure/surge_recorder.py`（`record_surge_signals` / `save_surge_watch` / `confirm_surge_watch` / `load_surge_watch` / `query_surge_signals`）✅ · `batch_plan.py`：`_load_recent_csvs` → `_load_recent_db`（查 signal_outcomes，軌跡加分改從 DB 讀）✅ · `--show` 模式改查 signal_outcomes DB ✅ · `surge_scan.py`：移除 CSV 輸出，改用 `surge_recorder.record_surge_signals` ✅ · `surge_tracker.py` 完整改寫：`save_watch` / `check_d1` 皆查/寫 DB，移除 JSON 檔依賴 ✅ · `bot.py`：移除 CSV glob，改用 `_query_plan_signals_db` / `_query_surge_signals_db` ✅ · `accuracy_monitor.py`：`load_scan_signals` 改查 signal_outcomes DB ✅ · `plan_surge_label.py`：`_load_surge_tickers` 改用 `query_surge_signals` ✅ · 548 unit tests passing ✅ |
| Phase 4.36A | ✅ Done | **Gate 0 硬性過濾 + PaidDataFetcher + TDCC/融資維持率 Macro Gate**：`PaidDataFetcher`（5個市場層級付費 FinMind 資料集，session 級快取）✅ · `TWSEChipProxy` 新增 4 個 Gate 0 bool 欄位（is_disposal/is_trading_halt/is_limit_up/is_daytrade_restricted）✅ · TCE Gate 0（處置股/暫停 → SKIP；漲停/當沖限制 → flag）✅ · SurgeRadar Gate 0（處置/暫停 → 跳過；當沖限制 → vol 門檻 ×1.2）✅ · 大盤融資維持率 Macro Gate（< 130 → LONG→WATCH，< 120 → CAUTION）✅ · 實際驗證 5 個 FinMind dataset 名稱（原文件全錯）✅ · TDCC 修正（dataset: TaiwanStockHoldingSharesPer，field: HoldingSharesLevel，more-than 前綴處理）✅ · Pillar 2A（TaiwanStockBrokerTradingStatement 在現有方案不可用）已確認 ✅ · 578 unit tests passing ✅ |
| Phase 4.36B | ✅ Done | **Plan HTML 升級 + 分數去上限 + 早期佈局門檻校準**：HTML 結果頁新增互動式 Filter Bar（操作方向 pill、信心分數滑桿、產業下拉選單，即時篩選）✅ · `min_confidence` 參數正確傳入 `_generate_plan_html`（修正先前 filter 無效問題）✅ · TCE `total` property 移除 `min(100, ...)` 上限（分數現可突破 100，反映真實優質程度）✅ · `batch_plan.py` 所有後處理加分（PERSIST_RISING/NEAR_HIGH_COIL/SECTOR_RANK）同步移除上限 ✅ · `_print_score_health()` 分數分布健康檢查（P25/P50/P75/P95 + 頂部四分位寬度警告）加入 `make plan` 與 `make surge` 輸出 ✅ · `_key_signals_html` + `_rule_rec` 完整改寫：解析 flag 中的實際數值（GATE_PASS:G1_ZONE:98.6%、DUAL_FLOW_STRONG:F+860K/T+342K、CUMUL_FLOW_HOT:2.7x 等），HTML 固定顯示「依據」標籤列，LLM 分析同樣呈現數字根據 ✅ · `rotation_tracker.py` Python 3.9 `@dataclass` bug 修正（`sys.modules` 注冊後再 `exec_module`）✅ · **流動性門檻 TSE 40M→15M / TPEx 15M→8M**（建倉策略無需高流動性，提早捕捉小型股佈局機會）✅ · **`_is_inst_momentum` 連買天數 5→3 天**（法人連買 3 天已是足夠的早期佈局信號）✅ · 595 unit tests passing ✅ |
| Phase 4.37 | ✅ Done | **評分品質門（Cross-Pillar + 反向扣分 + 追高懲罰）**：跨柱最低要求（P1<12｜P2<10｜P3<12 → 最高 WATCH，flag CROSS_PILLAR_WEAK）✅ · 追高懲罰因子 `recent_advance_deduction`（近20日最低收盤漲幅 >40% → -10 HIGH_BASE_RISK / >25% → -5 MOD_BASE_RISK）✅ · OBV 出貨訊號（normalized slope < -0.05 → -3，flag OBV_DIST，原範圍 0–5 擴為 -3/0/2/3/5）✅ · 量能不對稱負向（avg_up/avg_down < 0.5 → -4 / <0.7 → -2，flag VOL_ASYM_WEAK，原範圍 0–4 擴為 -4/-2/0/2/4）✅ · 分點資料修正（`fetch_broker_trades` 改為逐日迭代，解決 TaiwanStockTradingDailyReport 400 error，Pillar 2A 恢復正常得分）✅ · 606 unit tests passing ✅ |
| Phase 4.38 | ✅ Done | **統一 OHLCV DB（L2 快取層）**：新建 `ohlcv_daily` PostgreSQL 表（migration 011）✅ · `OHLCVRepository`（`get`/`upsert`/`max_date`，DB 不可用時自動降級為 no-op）✅ · `FinMindClient` DB-first pattern（L1 mem → L2 DB → Parquet → API，源頭無論 FinMind/yfinance 皆寫回同一表）✅ · `_used_yfinance` flag 修正 source 偵測（原 `df.get()` 對 DataFrame 無效）✅ · `batch_plan.py` + `surge_scan.py` 注入 `OHLCVRepository`（所有掃描共享 DB 快取）✅ · 7 個 `OHLCVRepository` 單元測試（無 DB 環境 no-op 路徑覆蓋）✅ · 613 unit tests passing ✅ |
| Phase 4.39 | ✅ Done | **TREND_WALK bypass track**：`_is_trend_walk()`（G2 唯一失敗 + MA5>MA20>MA60 + proximity ≥90%）✅ · TREND_WALK 繞過 BB 壓縮門檻，捕捉沿 BB 上軌走強的趨勢股 ✅ · RSI 刻意不納入條件（趨勢股自然帶高 RSI 65–80，Pillar 1 已計分，雙重篩選會阻擋目標設置）✅ · `score_full` 修正：TREND_WALK 加入 bypass-track 傳播列表（`data_quality_flags`）✅ · 7 個 TestTrendWalkTrack 單元測試（含整合測試）✅ · 620 unit tests passing ✅ |

**免費 vs 付費因子說明：**

| 因子 | 免費可用 | 需付費 FinMind | 說明 |
|------|----------|----------------|------|
| Pillar 1 動能（RSI、突破、均線）| ✅ | — | TWSE/TPEx OHLCV 政府公開資料 |
| Pillar 2B 三大法人（外資+投信+自營）| ✅ | — | TWSE T86 + TPEx T86 政府端點 |
| Pillar 2A 分點籌碼（隔日沖/波段贏家）| ✗ | ✅ | FinMind `TaiwanStockBrokerTradingStatement` |
| Pillar 3 結構（支撐/壓力/融資融券）| ✅（部分）| — | MI_MARGN 政府資料；SBL 目前降級為 0 |
| 產業排名後處理加分 | ✅ | — | 本機 industry_map cache |
| 信號持續加分（軌跡感知）| ✅ | — | 近 3 天 signal_outcomes DB；RISING +7 / STABLE +5 / DECLINING +0 |
| EMERGING_SETUP 蓄積偵測 | ✅ | — | WATCH + MA排列 + 法人買 + 未突破 20 日高 |

**Phase 5 (next):**
- Real Stripe webhook handling (requires production Stripe account + deployment)
- Community reputation scoring and spam/bot filtering
- 台灣Pay integration

Do not implement Phase N+1 without the Phase N gate condition being met.

## Makefile Commands

All available `make` targets — do NOT cite commands not in this list.

| Command | Script | Engine | Purpose |
|---------|--------|--------|---------|
| `make analyze TICKER=2330` | `analyze.py` | TCE | 單股深度分析 + 買賣建議 + 因子解釋 |
| `make plan` | `batch_plan.py` | TCE | **佈局/建倉掃描**（2–12 週持倉策略，提早識別蓄積型態，結果存 signal_outcomes DB）|
| `make settle` | `daily_runner.py settle` | — | 週末結算（更新信號勝率）|
| `make backtest` | `backtest.py` | TCE | 歷史回測 |
| `make backtest-compare` | `backtest_v23_vs_v22.py` | TCE | v2.3 vs v2.2 比較 |
| `make factor-report` | `factor_report.py` | TCE | 因子 Lift 分析 + Walk-forward |
| `make optimize` | `optimize.py` | TCE | 一鍵因子優化迴路 |
| `make label` | `plan_surge_label.py` | — | 標記 plan 信號結果 |
| `make auto-tune` | `auto_tune.py` | — | 自動調參 |
| `make test` | pytest | — | 執行所有單元測試 |
| `make report` | `report.py` | — | 盤後復盤（非 TCE）|
| `make growth` | `growth_scan.py` | — | 月營收成長股掃描（MOPS，YoY ≥20%）|
| `make flow` | plan + surge + report | Both | 全流程一鍵（growth[月] → plan → surge → report）|
| `make bot` | `bot.py` | — | 啟動 Telegram Bot |
| `make surge` | `surge_scan.py` | SurgeRadar | **短線爆量掃描**（當日/次日快進快出，非 TCE，結果存 surge_signals DB）|
| `make surge-live` | `surge_scan.py --intraday` | SurgeRadar | 盤中即時爆量掃描 |
| `make surge-factor` | `surge_factor_report.py` | SurgeRadar | Surge 因子 Lift 報告 |
| `make surge-tune` | `surge_factor_report.py --llm --apply` | SurgeRadar | Surge 因子 LLM 調參 |
| `make surge-backtest` | `surge_backtest.py` | SurgeRadar | Surge 歷史回測 |
| `make heat-scan` | `heat_scan.py` | — | 市場熱度掃描（產業+概念）|
| `make heat-update` | `update_market_heat.py` | — | 每日熱度快照更新 |
| `make rotation` | `rotation_tracker.py` | — | 板塊輪動追蹤 |
| `make tight-base-bt` | `tight_base_backtest.py` | — | 緊縮底部回測 v1 |
| `make tight-base-v2` | `tight_base_v2_backtest.py` | — | 緊縮底部回測 v2（熱度感知）|
| `make chip-loading-bt` | `chip_loading_backtest.py` | — | 籌碼吸收回測 |
| `make monitor` | `accuracy_monitor.py` | — | 信號準確率監控 |
| `make db-dump` | pg_dump | — | 備份完整 DB |
| `make db-restore` | pg_restore | — | 還原 DB |
| `make db-dump-signals` | pg_dump | — | 僅備份 signals 表 |
| `make db-init` | migrations | — | 初始化/執行 migration |
| `make migrate` | alembic | — | 執行 DB migration |
| `make api` | uvicorn | — | 啟動 FastAPI server |
| `make setup` | pip install | — | 安裝依賴 |

> `make daily` / `make show` **不存在**。每日工作流程用 `make flow`（plan + surge + report）。

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
