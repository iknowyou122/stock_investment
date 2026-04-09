PYTHONPATH := src
export PYTHONPATH
PYTHON := .venv/bin/python
_TODAY := $(shell date +%Y-%m-%d)

.PHONY: scan precheck settle backtest factor-report optimize test setup migrate api install

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
