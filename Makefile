PYTHONPATH := src
export PYTHONPATH
PYTHON := .venv/bin/python
_TODAY := $(shell date +%Y-%m-%d)

.PHONY: scan precheck settle backtest factor-report optimize test setup migrate api install review daily show

DATE ?= $(shell date +%Y-%m-%d)
LLM  ?=

# ── 安裝依賴 ─────────────────────────────────────────────────────────────────
install:
	$(PYTHON) -m pip install -e ".[llm-gemini,llm-openai]"

# ── 每日掃描（主流程）────────────────────────────────────────────────────────
# 掃描 + 存 CSV（precheck 用）+ 寫 DB（factor-report 用）
# 用法: make scan
#       make scan LLM=gemini LLM_TOP=5
#       make scan SECTORS="1 4"
SECTORS ?=
LLM_TOP ?=

scan:
ifeq ($(DATE),$(_TODAY))
	$(PYTHON) scripts/batch_scan.py --save-csv --save-db $(if $(LLM),--llm $(LLM)) $(if $(LLM_TOP),--llm-top $(LLM_TOP)) $(if $(SECTORS),--sectors $(SECTORS))
else
	$(PYTHON) scripts/batch_scan.py --save-csv --save-db --date $(DATE) $(if $(LLM),--llm $(LLM)) $(if $(LLM_TOP),--llm-top $(LLM_TOP)) $(if $(SECTORS),--sectors $(SECTORS))
endif

# ── 盤中確認 ─────────────────────────────────────────────────────────────────
# 讀取昨日 CSV → 即時報價 → 哪些還能進場
# 用法: make precheck
#       make precheck TOP=10 MIN_CONF=50
TOP     ?= 20
MIN_CONF ?= 40
CSV     ?=

precheck:
	$(PYTHON) scripts/precheck.py \
		--top $(TOP) \
		--min-confidence $(MIN_CONF) \
		$(if $(CSV),--csv $(CSV))

# ── 歷史掃描結果查詢 ──────────────────────────────────────────────────────────
# 用法: make show              # 互動式選擇日期
#       make show SHOW_DATE=2026-04-10
#       make show SHOW_DATE=2026-04-10 TOP=20
SHOW_DATE ?=
show:
	$(PYTHON) scripts/batch_scan.py --show "$(SHOW_DATE)" --top $(TOP) --min-confidence $(MIN_CONF)

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
#       make backtest DATE_FROM=2025-10-01 DATE_TO=2026-03-31 LLM=none
#       make backtest ENTRY_DELAY=1    # T-1 佈局驗證
DATE_FROM      ?=
DATE_TO        ?=
BACKTEST_TICKERS ?=
ENTRY_DELAY    ?=

backtest:
	$(PYTHON) scripts/backtest.py \
		$(if $(DATE_FROM),--date-from $(DATE_FROM)) \
		$(if $(DATE_TO),--date-to $(DATE_TO)) \
		$(if $(LLM),--llm $(LLM)) \
		$(if $(BACKTEST_TICKERS),--tickers $(BACKTEST_TICKERS)) \
		$(if $(SECTORS),--sectors $(SECTORS)) \
		$(if $(ENTRY_DELAY),--entry-delay $(ENTRY_DELAY))

# ── 因子分析 ─────────────────────────────────────────────────────────────────
FACTOR_DAYS ?=

factor-report:
	$(PYTHON) scripts/factor_report.py $(if $(FACTOR_DAYS),--days $(FACTOR_DAYS))

# ── 一鍵優化迴路 ─────────────────────────────────────────────────────────────
# 用法: make optimize
#       make optimize AUTO_APPROVE=1   # 全自動
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

# ── 盤後復盤 ─────────────────────────────────────────────────────────────────
# T+1 結算、勝率、A/B 參數競賽
# 用法: make review
#       make review DATE=2026-04-09
review:
ifeq ($(DATE),$(_TODAY))
	$(PYTHON) scripts/review.py
else
	$(PYTHON) scripts/review.py --date $(DATE)
endif

# ── 完整每日流程 ─────────────────────────────────────────────────────────────
# 掃描 + 復盤（一鍵執行）
# 用法: make daily
#       make daily LLM=gemini LLM_TOP=5
daily:
	$(MAKE) scan
	$(MAKE) review

# ── 資料庫備份與還原 ─────────────────────────────────────────────────────────
# 從 DATABASE_URL 解析連線資訊
_DB_URL  := $(shell grep DATABASE_URL .env 2>/dev/null | cut -d= -f2-)
_DB_NAME := $(shell echo $(_DB_URL) | sed 's|.*\/||')

DUMP_FILE ?= backup_$(shell date +%Y%m%d).dump

# 完整備份（schema + 所有資料）
db-dump:
	@echo "備份資料庫 $(_DB_NAME) → $(DUMP_FILE)"
	pg_dump -Fc "$(_DB_URL)" > "$(DUMP_FILE)"
	@echo "完成：$(DUMP_FILE) ($(shell du -sh $(DUMP_FILE) | cut -f1))"

# 還原（需要目標 DB 已存在且空白）
db-restore:
	@test -n "$(FILE)" || (echo "用法: make db-restore FILE=backup_20260409.dump" && exit 1)
	@echo "還原 $(FILE) → $(_DB_NAME)"
	pg_restore -d "$(_DB_URL)" --no-owner --no-privileges "$(FILE)"
	@echo "完成"

# 只備份最有價值的分析資料（signal_outcomes）
db-dump-signals:
	@echo "備份 signal_outcomes → signals_$(shell date +%Y%m%d).dump"
	pg_dump -Fc -t signal_outcomes "$(_DB_URL)" > "signals_$(shell date +%Y%m%d).dump"
	@echo "完成"

# 新機器一鍵初始化（clone 之後跑這個）
db-init:
	@echo "1. 建立資料庫 $(_DB_NAME)..."
	createdb "$(_DB_NAME)" 2>/dev/null || echo "  (資料庫已存在，略過)"
	@echo "2. 執行 migrations..."
	$(MAKE) migrate
	@echo "3. 完成。如有備份檔請執行: make db-restore FILE=your_backup.dump"

.PHONY: db-dump db-restore db-dump-signals db-init
