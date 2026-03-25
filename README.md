# Taiwan Stock AI Analysis Agent

A post-market signal engine for Taiwan equities that combines broker branch behavioral fingerprinting (分點分析), momentum, and volume-profile analysis into a Triple Confirmation score. Each evening after FinMind publishes T+1 broker data (~20:00 CST), the agent scores a watchlist of tickers and emits LONG / WATCH / CAUTION signals with deterministic execution plans. An optional Claude API call generates natural-language reasoning in Traditional Chinese.

---

## Architecture

The system is organized into four layers:

```
Infrastructure  ──  FinMindClient (data fetch + cache), db.py (PostgreSQL pool)
     │
Domain          ──  BrokerLabelClassifier, TripleConfirmationEngine, models (Pydantic)
     │
Agentic         ──  ChipDetectiveAgent, StrategistAgent (orchestration + LLM reasoning)
     │
Presentation    ──  CLI (__main__.py), Phase 3b FastAPI landing page (see DESIGN.md)
```

- **Infrastructure**: thin wrappers with retry/backoff, Parquet file cache, and a connection pool. No business logic.
- **Domain**: pure functions and classifiers. `BrokerLabelClassifier` fingerprints broker branches using D+2 reversal rates. `TripleConfirmationEngine` scores Momentum + Chip + Space pillars.
- **Agentic**: `StrategistAgent` orchestrates the full pipeline; optionally calls Claude for reasoning fields.
- **Presentation**: `__main__.py` is the daily CLI; the Phase 3b landing page is gated on Phase 3 validation.

---

## Prerequisites

- Python 3.10+
- PostgreSQL 14+ (local or remote)
- FinMind API token — free tier at [finmindtrade.com](https://finmindtrade.com/)
- (Optional) Anthropic API key — only required for LLM reasoning mode

---

## Setup

**1. Clone the repository**

```bash
git clone <repo-url>
cd stock_investment
```

**2. Install dependencies**

```bash
pip install -e ".[dev]"
# or, if no pyproject.toml extras are defined yet:
pip install -r requirements.txt
```

**3. Configure environment**

```bash
cp .env.example .env
# Edit .env and fill in FINMIND_TOKEN and DATABASE_URL
```

**4. Run database migrations**

```bash
psql $DATABASE_URL \
  -f db/migrations/001_broker_labels.sql \
  -f db/migrations/002_signal_outcomes.sql
```

**5. Verify data alignment**

Checks that FinMind OHLCV and broker trade date ranges are consistent:

```bash
python scripts/data_alignment_check.py --ticker 2330
```

**6. Run spike validation**

Confirms the 隔日沖 reversal rate hypothesis (gate condition: reversal_rate > 60% at D+2):

```bash
python scripts/spike_validate.py --ticker 2330
```

---

## Phase 1 Operations

**Run full broker label classification (once, or after new history becomes available)**

```bash
python scripts/run_phase1_classification.py
# Custom tickers and lookback:
python scripts/run_phase1_classification.py --tickers 2330 2317 2454 --lookback-days 365
# Preview without writing to DB:
python scripts/run_phase1_classification.py --dry-run
```

**Daily signal generation (run after 20:00 CST on TWSE trading days)**

```bash
python -m taiwan_stock_agent --date 2025-01-31
# Skip LLM reasoning (no ANTHROPIC_API_KEY required):
python -m taiwan_stock_agent --date 2025-01-31 --no-llm
# Subset of tickers:
python -m taiwan_stock_agent --date 2025-01-31 --tickers 2330 2317
# Write JSON output to file:
python -m taiwan_stock_agent --date 2025-01-31 --output signals.json
```

**Record actual outcomes (nightly cron, run D+2 after signal date)**

```bash
python scripts/record_signal_outcomes.py
# Specify target date explicitly:
python scripts/record_signal_outcomes.py --date 2025-02-04
# Preview without writing to DB:
python scripts/record_signal_outcomes.py --dry-run
```

A typical cron schedule (Taiwan time, UTC+8):

```cron
# Daily signal generation: 20:30 CST = 12:30 UTC
30 12 * * 1-5  cd /path/to/stock_investment && python -m taiwan_stock_agent --date $(date +\%F)

# Outcome recorder: 21:00 CST = 13:00 UTC
0  13 * * 1-5  cd /path/to/stock_investment && python scripts/record_signal_outcomes.py
```

---

## Testing

**Unit tests** (no database or network required):

```bash
pytest tests/unit/
```

**Integration tests** (requires a running PostgreSQL instance):

```bash
pytest tests/integration/ --postgresql-host localhost
```

---

## Phase Gates

| Phase   | Status         | Gate condition |
|---------|----------------|----------------|
| Pre-spike | Done         | Run `data_alignment_check.py` then `spike_validate.py` |
| Phase 1 | Not started    | Spike must confirm 隔日沖 reversal_rate > 60% at D+2 |
| Phase 2 | Not started    | Phase 1 broker label DB built and backtested |
| Phase 3 | Not started    | Triple Confirmation validated on 30 tickers |
| Phase 3b | Not started   | Landing page + FastAPI (see DESIGN.md) |

Do not implement Phase N+1 without the Phase N gate condition being met.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FINMIND_TOKEN` | Yes | FinMind API token. Get one at finmindtrade.com. |
| `DATABASE_URL` | Yes | PostgreSQL connection string, e.g. `postgresql://user:pass@localhost:5432/taiwan_stock` |
| `ANTHROPIC_API_KEY` | No | Claude API key. Only needed for LLM reasoning (`--no-llm` bypasses this). |
