PYTHONPATH := src
export PYTHONPATH
PYTHON := .venv/bin/python
_TODAY := $(shell date +%Y-%m-%d)

.PHONY: run scan api test test-unit test-integration install install-gemini install-openai

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
scan:
ifeq ($(DATE),$(_TODAY))
	$(PYTHON) scripts/batch_scan.py $(if $(LLM),--llm $(LLM))
else
	$(PYTHON) scripts/batch_scan.py --date $(DATE) $(if $(LLM),--llm $(LLM))
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
