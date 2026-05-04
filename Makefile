PYTHONPATH := src
export PYTHONPATH
PYTHON := .venv/bin/python
_TODAY := $(shell date +%Y-%m-%d)

.PHONY: plan trade trade-t2 report settle backtest backtest-compare factor-report optimize test setup migrate api install flow show bot-setup bot monitor surge surge-live

DATE ?= $(shell date +%Y-%m-%d)
LLM  ?=

# ── 安裝依賴 ─────────────────────────────────────────────────────────────────
install:
	$(PYTHON) -m pip install -e ".[llm-gemini,llm-openai]"

# ── 盤後擬定計畫 (Plan) ──────────────────────────────────────────────────────
# 掃描 + 存 CSV（trade 用）+ 寫 DB（factor-report 用）
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

# ── 盤中執行交易 (Trade) ──────────────────────────────────────────────────────
# 讀取昨日 CSV → 即時報價 → 哪些還能進場
# 用法: make trade
#       make trade TOP=10 MIN_CONF=50
TOP     ?= 20
MIN_CONF ?= 40
CSV     ?=

trade:
	$(PYTHON) scripts/trade.py \
		--top $(TOP) \
		--min-confidence $(MIN_CONF) \
		$(if $(CSV),--csv $(CSV))

# T+2 進場確認：自動載入 2 個交易日前的 CSV（D+2 勝率 55.6% > D+0 38.5%）
# 用法: make trade-t2
trade-t2:
	$(PYTHON) scripts/trade.py \
		--t2 \
		--top $(TOP) \
		--min-confidence $(MIN_CONF)

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
surge:
	$(PYTHON) scripts/surge_scan.py --save-csv $(if $(NOTIFY),--notify) $(if $(SECTORS),--sectors $(SECTORS)) $(if $(TICKERS),--tickers $(TICKERS)) $(if $(DATE),--date $(DATE))

surge-live:
	$(PYTHON) scripts/surge_scan.py --intraday $(if $(NOTIFY),--notify) $(if $(SECTORS),--sectors $(SECTORS)) $(if $(TICKERS),--tickers $(TICKERS))

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
