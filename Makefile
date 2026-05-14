PYTHONPATH := src
export PYTHONPATH
PYTHON := .venv/bin/python
_TODAY := $(shell date +%Y-%m-%d)

.PHONY: plan report settle backtest backtest-compare factor-report optimize test setup migrate api install flow show bot-setup bot monitor surge surge-live surge-factor surge-tune surge-backtest heat-scan heat-update tight-base-bt tight-base-v2

DATE ?= $(shell date +%Y-%m-%d)
LLM  ?=

# ── 安裝依賴 ─────────────────────────────────────────────────────────────────
install:
	$(PYTHON) -m pip install -e ".[llm-gemini,llm-openai]"

# ── 盤後擬定計畫 (Plan) ──────────────────────────────────────────────────────
# 掃描 + 存 CSV + 寫 DB（factor-report 用）
# 用法: make plan
#       make plan LLM=gemini LLM_TOP=5
#       make plan SECTORS="1 4"
SECTORS ?=
LLM_TOP ?=
SORT    ?= trend

NOTIFY  ?= 1

plan:
ifeq ($(DATE),$(_TODAY))
	$(PYTHON) scripts/batch_plan.py --save-csv --save-db --sort-by $(SORT) $(if $(LLM),--llm $(LLM)) $(if $(LLM_TOP),--llm-top $(LLM_TOP)) $(if $(SECTORS),--sectors $(SECTORS)) $(if $(TICKERS),--tickers $(TICKERS)) $(if $(filter 1,$(NOTIFY)),--notify)
else
	$(PYTHON) scripts/batch_plan.py --save-csv --save-db --date $(DATE) --sort-by $(SORT) $(if $(LLM),--llm $(LLM)) $(if $(LLM_TOP),--llm-top $(LLM_TOP)) $(if $(SECTORS),--sectors $(SECTORS)) $(if $(TICKERS),--tickers $(TICKERS)) $(if $(filter 1,$(NOTIFY)),--notify)
endif

TOP     ?= 20
MIN_CONF ?= 40
CSV     ?=

# ── 歷史掃描結果查詢 ──────────────────────────────────────────────────────────
# 用法: make show              # 互動式選擇日期
#       make show SHOW_DATE=2026-04-10
SHOW_DATE ?=
show:
	$(PYTHON) scripts/batch_plan.py --show "$(SHOW_DATE)" --top $(TOP) --min-confidence $(MIN_CONF)

# ── 週末結算 ─────────────────────────────────────────────────────────────────
# 補填 T+1/T+3/T+5 漲跌幅（每週末跑一次）
settle:
ifeq ($(DATE),$(_TODAY))
	$(PYTHON) scripts/daily_runner.py settle
else
	$(PYTHON) scripts/daily_runner.py settle --date $(DATE)
endif

# ── 歷史回測 ─────────────────────────────────────────────────────────────────
# 用法: make backtest
DATE_FROM      ?=
DATE_TO        ?=
TICKERS        ?=
ENTRY_DELAY    ?=

backtest:
	$(PYTHON) scripts/backtest.py \
		$(if $(DATE_FROM),--date-from $(DATE_FROM)) \
		$(if $(DATE_TO),--date-to $(DATE_TO)) \
		$(if $(LLM),--llm $(LLM)) \
		$(if $(TICKERS),--tickers $(TICKERS)) \
		$(if $(SECTORS),--sectors $(SECTORS)) \
		$(if $(ENTRY_DELAY),--entry-delay $(ENTRY_DELAY))

# ── v2.2 vs v2.3 引擎比對回測 ────────────────────────────────────────────────
# 用法: make backtest-compare
#       make backtest-compare DATE_FROM=2026-01-01 DATE_TO=2026-03-31
#       make backtest-compare MIN_CONF=50 SECTORS="1 4" SAVE_CSV=1
MIN_CONF    ?= 40
SAVE_CSV    ?=

backtest-compare:
	$(PYTHON) scripts/backtest_v23_vs_v22.py \
		$(if $(DATE_FROM),--date-from $(DATE_FROM)) \
		$(if $(DATE_TO),--date-to $(DATE_TO)) \
		$(if $(SECTORS),--sectors $(SECTORS)) \
		--min-confidence $(MIN_CONF) \
		$(if $(filter 1,$(SAVE_CSV)),--save-csv)

# ── 因子分析 ─────────────────────────────────────────────────────────────────
FACTOR_DAYS ?=

factor-report:
	$(PYTHON) scripts/factor_report.py $(if $(FACTOR_DAYS),--days $(FACTOR_DAYS))

# ── 一鍵優化迴路 ─────────────────────────────────────────────────────────────
DAYS         ?=
AUTO_APPROVE ?=
DRY_RUN      ?=
SKIP_SETTLE  ?=

optimize:
	$(PYTHON) scripts/optimize.py \
		$(if $(AUTO_APPROVE),--auto-approve) \
		$(if $(DRY_RUN),--dry-run) \
		$(if $(SKIP_SETTLE),--skip-settle) \
		$(if $(DAYS),--days $(DAYS))

label:
	$(PYTHON) scripts/plan_surge_label.py $(if $(DRY_RUN),--dry-run) $(if $(DAYS),--lookback $(DAYS))

auto-tune:
	$(PYTHON) scripts/auto_tune.py $(if $(DRY_RUN),--dry-run) $(if $(DAYS),--lookback $(DAYS))

# ── 測試 ─────────────────────────────────────────────────────────────────────
test:
	.venv/bin/pytest tests/unit/ -q

# ── 環境初始化 ───────────────────────────────────────────────────────────────
setup:
	$(PYTHON) scripts/setup.py

migrate:
	$(PYTHON) scripts/migrate.py $(if $(DRY_RUN),--dry-run)

# ── API server ───────────────────────────────────────────────────────────────
api:
	$(PYTHON) -m uvicorn taiwan_stock_agent.api.main:app --reload --port 8000

# ── 盤後產出報告 (Report) ──────────────────────────────────────────────────────
# T+1 結算、勝率、A/B 參數競賽
# 用法: make report
#       make report DATE=2026-04-09
report:
ifeq ($(DATE),$(_TODAY))
	$(PYTHON) scripts/report.py
else
	$(PYTHON) scripts/report.py --date $(DATE)
endif

# ── 完整每日流程 (Flow) ────────────────────────────────────────────────────────
# 掃描 + 產出報告（一鍵執行）
# 用法: make flow
# 執行順序：
#   1. plan   — 預突破批次掃描
#   2. surge  — 噴發雷達掃描（短線爆量）
#   3. report — T+1 結算 + 勝率報告
flow:
	$(MAKE) plan
	$(MAKE) surge
	$(MAKE) report

# ── 資料庫備份與還原 ─────────────────────────────────────────────────────────
# 從 DATABASE_URL 解析連線資訊
_DB_URL  := $(shell grep DATABASE_URL .env 2>/dev/null | cut -d= -f2-)
_DB_NAME := $(shell echo $(_DB_URL) | sed 's|.*\/||')

DUMP_FILE ?= backup_$(shell date +%Y%m%d).dump

db-dump:
	@echo "備份資料庫 $(_DB_NAME) → $(DUMP_FILE)"
	pg_dump -Fc "$(_DB_URL)" > "$(DUMP_FILE)"
	@echo "完成：$(DUMP_FILE) ($(shell du -sh $(DUMP_FILE) | cut -f1))"

db-restore:
	@test -n "$(FILE)" || (echo "用法: make db-restore FILE=backup_20260409.dump" && exit 1)
	@echo "還原 $(FILE) → $(_DB_NAME)"
	pg_restore -d "$(_DB_URL)" --no-owner --no-privileges "$(FILE)"
	@echo "完成"

db-dump-signals:
	@echo "備份 signal_outcomes → signals_$(shell date +%Y%m%d).dump"
	pg_dump -Fc -t signal_outcomes "$(_DB_URL)" > "signals_$(shell date +%Y%m%d).dump"
	@echo "完成"

db-init:
	@echo "1. 建立資料庫 $(_DB_NAME)..."
	createdb "$(_DB_NAME)" 2>/dev/null || echo "  (資料庫已存在，略過)"
	@echo "2. 執行 migrations..."
	$(MAKE) migrate
	@echo "3. 完成。如有備份檔請執行: make db-restore FILE=your_backup.dump"

.PHONY: db-dump db-restore db-dump-signals db-init

## ── Telegram Bot ──────────────────────────────────────────────────────────
bot-setup:
	$(PYTHON) scripts/bot_setup.py

bot:
	$(PYTHON) scripts/bot.py $(if $(LLM),--llm $(LLM))

# ── 噴發雷達掃描（短線爆量捕捉）─────────────────────────────────────────────
# SURGE_DATE: 不設定時由 Python _default_date() 自動判斷（17:00 前 → 前一交易日；之後 → 今天）
# 用法: make surge                          # 自動日期
#       SURGE_DATE=2026-05-06 make surge    # 指定日期
SURGE_DATE ?=
surge:
	$(PYTHON) scripts/surge_scan.py --save-csv --llm $(if $(NOTIFY),--notify) $(if $(SECTORS),--sectors $(SECTORS)) $(if $(TICKERS),--tickers $(TICKERS)) $(if $(SURGE_DATE),--date $(SURGE_DATE))

surge-live:
	$(PYTHON) scripts/surge_scan.py --intraday $(if $(NOTIFY),--notify) $(if $(SECTORS),--sectors $(SECTORS)) $(if $(TICKERS),--tickers $(TICKERS))

# ── Surge 因子分析 + LLM 優化 ──────────────────────────────────────────────
# 用法: make surge-factor              # 顯示因子 Lift 表（不呼叫 LLM）
#       make surge-tune               # LLM 建議 + 互動式套用
MIN_SIGNALS ?= 30

surge-factor:
	$(PYTHON) scripts/surge_factor_report.py --min-signals $(MIN_SIGNALS)

surge-tune:
	$(PYTHON) scripts/surge_factor_report.py --llm --apply --min-signals $(MIN_SIGNALS)

SURGE_BACKTEST_DAYS ?= 90
SURGE_BACKTEST_OUTPUT ?= data/surge_backtest.csv
surge-backtest:
	$(PYTHON) scripts/surge_backtest.py --days $(SURGE_BACKTEST_DAYS) --output $(SURGE_BACKTEST_OUTPUT)

# ── Market Heat Engine (Phase 4.25) ────────────────────────────────────────
# heat-scan:      28 大產業熱度 + 概念股 basket + 國際隔夜訊號 + LLM 主軸分析
# heat-update:    更新熱度快照供 surge 評分使用（每日 17:05 自動執行）
# tight-base-bt:  TIGHT_BASE 偵測器回測（v1 不含熱度過濾）
# tight-base-v2:  熱度感知 TIGHT_BASE 回測（驗證命中率提升）

heat-scan:
	$(PYTHON) scripts/heat_scan.py

heat-update:
	$(PYTHON) scripts/update_market_heat.py

tight-base-bt:
	$(PYTHON) scripts/tight_base_backtest.py --days 90

tight-base-v2:
	$(PYTHON) scripts/tight_base_v2_backtest.py --days 90

chip-loading-bt:
	$(PYTHON) scripts/chip_loading_backtest.py --days 90

# ── 信號準確度監控 ──────────────────────────────────────────────────────────────
# 載入歷史 scan CSV，驗證突破結果，顯示滾動勝率 Dashboard
# 用法: make monitor
#       make monitor MIN_CONF=50
#       make monitor DATE_FROM=2026-04-01 DATE_TO=2026-04-20
#       make monitor EXPORT=report.csv
#       make monitor NO_FETCH=1      # 只讀快取，不查 API
EXPORT    ?=
NO_FETCH  ?=

monitor:
	$(PYTHON) scripts/accuracy_monitor.py \
		$(if $(DATE),--date $(DATE)) \
		$(if $(MIN_CONF),--min-confidence $(MIN_CONF)) \
		$(if $(DATE_FROM),--date-from $(DATE_FROM)) \
		$(if $(DATE_TO),--date-to $(DATE_TO)) \
		$(if $(EXPORT),--export $(EXPORT)) \
		$(if $(filter 1,$(NO_FETCH)),--no-fetch)
