PYTHONPATH := src
export PYTHONPATH
PYTHON := .venv/bin/python
_TODAY := $(shell date +%Y-%m-%d)

.PHONY: run scan precheck api test test-unit test-integration install install-gemini install-openai build-labels analyze setup migrate backtest daily settle factor-report tune-review test-factor optimize

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

# ── 盤前/盤中確認 ────────────────────────────────────────────────────────────
# 讀取昨日 scan CSV → 抓即時報價 → 確認哪些還能進場
# 用法: make precheck
#       make precheck TOP=10
#       make precheck CSV=data/scans/scan_2026-04-07.csv
TOP ?= 20
CSV ?=
MIN_CONF ?= 40

precheck:
	$(PYTHON) scripts/precheck.py \
		--top $(TOP) \
		--min-confidence $(MIN_CONF) \
		$(if $(CSV),--csv $(CSV))

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
DAYS ?=
SCORING_VERSION ?=

analyze:
	$(PYTHON) scripts/analyze_outcomes.py \
		$(if $(DAYS),--days $(DAYS)) \
		$(if $(SCORING_VERSION),--scoring-version $(SCORING_VERSION))

# ── 環境初始化（第一次使用）────────────────────────────────────────────────────
# 自動安裝 PostgreSQL、建立 DB、設定 .env、跑所有 migrations
setup:
	$(PYTHON) scripts/setup.py

# ── DB Migrations ────────────────────────────────────────────────────────────
# 用法: make migrate
#       make migrate DRY_RUN=1   # 只列出 pending，不執行
migrate:
	$(PYTHON) scripts/migrate.py $(if $(DRY_RUN),--dry-run)

# ── 歷史回測 ──────────────────────────────────────────────────────────────────
# 優化迴路起點：撈過去歷史資料 → 產生訊號存 DB → settle → analyze → optimize
# 用法: make backtest                        # 全互動
#       make backtest DATE_FROM=2025-10-01 DATE_TO=2026-03-31
#       make backtest SECTORS="5 31" LLM=none
DATE_FROM ?=
DATE_TO   ?=
BACKTEST_TICKERS ?=

backtest:
	$(PYTHON) scripts/backtest.py \
		$(if $(DATE_FROM),--date-from $(DATE_FROM)) \
		$(if $(DATE_TO),--date-to $(DATE_TO)) \
		$(if $(LLM),--llm $(LLM)) \
		$(if $(BACKTEST_TICKERS),--tickers $(BACKTEST_TICKERS)) \
		$(if $(SECTORS),--sectors $(SECTORS))

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
FACTOR_DAYS ?=

factor-report:
	$(PYTHON) scripts/factor_report.py $(if $(FACTOR_DAYS),--days $(FACTOR_DAYS))

# ── 調參 ──────────────────────────────────────────────────────────────────────
AUTO_APPROVE ?=
SKIP_SETTLE ?=

tune-review:
	$(PYTHON) scripts/apply_tuning.py \
		$(if $(AUTO_APPROVE),--auto-approve) \
		$(if $(DRY_RUN),--dry-run)

# ── 一鍵優化迴路 ──────────────────────────────────────────────────────────────
# 用法: make optimize
#       make optimize AUTO_APPROVE=1      # 全自動（cron 用）
#       make optimize DRY_RUN=1           # 只看報告
#       make optimize SKIP_SETTLE=1       # 跳過補填步驟
optimize:
	$(PYTHON) scripts/optimize.py \
		$(if $(AUTO_APPROVE),--auto-approve) \
		$(if $(DRY_RUN),--dry-run) \
		$(if $(SKIP_SETTLE),--skip-settle) \
		$(if $(DAYS),--days $(DAYS))

# ── 實驗因子測試 ──────────────────────────────────────────────────────────────
# 用法: make test-factor FACTOR=my_factor_name
FACTOR ?=

test-factor:
ifndef FACTOR
	$(error 請指定 FACTOR，例如: make test-factor FACTOR=consecutive_foreign_3d)
endif
	$(PYTHON) scripts/test_factor.py --factor $(FACTOR)
