PYTHONPATH := src
export PYTHONPATH

.PHONY: run scan api test test-unit test-integration

# ── 分析股票 ─────────────────────────────────────────────────────────────────
# 用法: make run DATE=2026-03-27
#       make run DATE=2026-03-27 TICKERS="2330 2317 2454"
DATE    ?= $(shell date +%Y-%m-%d)
TICKERS ?= 2330 2317 2454 2382 3008

run:
	python3 -m taiwan_stock_agent --date $(DATE) --tickers $(TICKERS) --skip-freshness-check

# ── 批次掃描 ─────────────────────────────────────────────────────────────────
# 用法: make scan
#       make scan DATE=2026-03-27
scan:
	python3 scripts/batch_scan.py --date $(DATE)

# ── API server ───────────────────────────────────────────────────────────────
api:
	uvicorn taiwan_stock_agent.api.main:app --reload --port 8000

# ── 測試 ─────────────────────────────────────────────────────────────────────
test:
	python3 -m pytest tests/ -q

test-unit:
	python3 -m pytest tests/unit/ -q

test-integration:
	python3 -m pytest tests/integration/ -q
