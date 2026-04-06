PYTHONPATH := src
export PYTHONPATH
PYTHON := .venv/bin/python
_TODAY := $(shell date +%Y-%m-%d)

.PHONY: run scan api test test-unit test-integration install install-gemini install-openai build-labels analyze backtest daily settle factor-report

# ── 分析股票 ─────────────────────────────────────────────────────────────────
# 用法: make run DATE=2026-03-27 TICKERS="2330 2317 2454"
#       make run TICKERS="2330" LLM=gemini
DATE ?= $(shell date +%Y-%m-%d)
LLM  ?=

# ── 安裝依賴 ─────────────────────────────────────────────────────────────────
install:
	$(PYTHON) -m pip install -e "."

install-gemini:
	$(PYTHON) -m pip install -e ".[llm-gemini]"

install-openai:
	$(PYTHON) -m pip install -e ".[llm-openai]"

# ── 分析股票 ─────────────────────────────────────────────────────────────────
# 用法: make run DATE=2026-03-27 TICKERS="2330 2317 2454"
run:
ifndef TICKERS
	$(error 請指定 TICKERS，例如: make run TICKERS="2330 2317")
endif
ifeq ($(DATE),$(_TODAY))
	LLM_PROVIDER=$(LLM) $(PYTHON) -m taiwan_stock_agent --tickers $(TICKERS) --skip-freshness-check
else
	LLM_PROVIDER=$(LLM) $(PYTHON) -m taiwan_stock_agent --date $(DATE) --tickers $(TICKERS) --skip-freshness-check
endif

# ── 批次掃描 ─────────────────────────────────────────────────────────────────
# 用法: make scan
#       make scan DATE=2026-03-27
#       make scan LLM=gemini
#       make scan SECTORS="1 4" LLM=gemini
SECTORS ?=

scan:
ifeq ($(DATE),$(_TODAY))
	$(PYTHON) scripts/batch_scan.py $(if $(LLM),--llm $(LLM)) $(if $(SECTORS),--sectors $(SECTORS))
else
	$(PYTHON) scripts/batch_scan.py --date $(DATE) $(if $(LLM),--llm $(LLM)) $(if $(SECTORS),--sectors $(SECTORS))
endif

# ── API server ───────────────────────────────────────────────────────────────
api:
	$(PYTHON) -m uvicorn taiwan_stock_agent.api.main:app --reload --port 8000

# ── 測試 ─────────────────────────────────────────────────────────────────────
test:
	.venv/bin/pytest tests/ -q

test-unit:
	.venv/bin/pytest tests/unit/ -q

test-integration:
	.venv/bin/pytest tests/integration/ -q

# ── Broker Label DB 建置 ─────────────────────────────────────────────────────
# 第一次執行需要約 5-10 分鐘（FinMind API 抓取 + 分類）
# 之後重跑會從 Parquet cache 讀取，速度很快
# 用法: make build-labels
#       make build-labels WORKERS=5
#       make build-labels DRY_RUN=1     # 只顯示統計，不寫 DB
BUILD_WORKERS ?= 3
DRY_RUN ?=

build-labels:
	$(PYTHON) scripts/build_broker_labels.py \
		--workers $(BUILD_WORKERS) \
		$(if $(DRY_RUN),--dry-run)

# ── 訊號結果回測分析 ─────────────────────────────────────────────────────────
# 用法: make analyze
#       make analyze DAYS=60
#       make analyze SCORING_VERSION=v2
DAYS ?= 90
SCORING_VERSION ?=

analyze:
	$(PYTHON) scripts/analyze_outcomes.py \
		--days $(DAYS) \
		$(if $(SCORING_VERSION),--scoring-version $(SCORING_VERSION))

# ── 歷史回測 ──────────────────────────────────────────────────────────────────
# 用法: make backtest DATE_FROM=2025-10-01 DATE_TO=2026-03-31
#       make backtest DATE_FROM=2026-01-15 DATE_TO=2026-01-15 BACKTEST_TICKERS="2330 2317"
DATE_FROM ?= $(shell date -v-180d +%Y-%m-%d 2>/dev/null || date -d '180 days ago' +%Y-%m-%d)
DATE_TO   ?= $(_TODAY)
BACKTEST_TICKERS ?=

backtest:
	$(PYTHON) scripts/backtest.py \
		--date-from $(DATE_FROM) \
		--date-to $(DATE_TO) \
		$(if $(BACKTEST_TICKERS),--tickers $(BACKTEST_TICKERS))

# ── 每日真實訊號 ──────────────────────────────────────────────────────────────
daily:
ifeq ($(DATE),$(_TODAY))
	$(PYTHON) scripts/daily_runner.py daily
else
	$(PYTHON) scripts/daily_runner.py daily --date $(DATE)
endif

settle:
ifeq ($(DATE),$(_TODAY))
	$(PYTHON) scripts/daily_runner.py settle
else
	$(PYTHON) scripts/daily_runner.py settle --date $(DATE)
endif

# ── 因子分析 ──────────────────────────────────────────────────────────────────
FACTOR_DAYS ?= 180

factor-report:
	$(PYTHON) scripts/factor_report.py --days $(FACTOR_DAYS)
