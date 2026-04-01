PYTHONPATH := src
export PYTHONPATH
PYTHON := .venv/bin/python

.PHONY: run scan api test test-unit test-integration install install-gemini install-openai

# ── 分析股票 ─────────────────────────────────────────────────────────────────
# 用法: make run DATE=2026-03-27 TICKERS="2330 2317 2454"
DATE ?= $(shell date +%Y-%m-%d)

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
	$(PYTHON) -m taiwan_stock_agent --date $(DATE) --tickers $(TICKERS) --skip-freshness-check

# ── 批次掃描 ─────────────────────────────────────────────────────────────────
# 用法: make scan
#       make scan DATE=2026-03-27
scan:
	$(PYTHON) scripts/batch_scan.py --date $(DATE)

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
