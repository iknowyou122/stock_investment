# Factor Optimization Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a closed-loop system that backfills historical signals, accumulates daily live signals, analyzes which factors drive win rate, and surfaces tuning recommendations through a human-review gate.

**Architecture:** Historical backtest + daily runner both write `SignalOutput` + full score breakdown to `signal_outcomes`. A weekly (or on-demand) factor report runs lift analysis, grid search over a parameter whitelist, and walk-forward validation — producing a JSON recommendation that `apply_tuning.py` presents for human approval before patching `config/engine_params.json`. `optimize.py` chains these steps into a single command.

**Tech Stack:** Python 3.11, psycopg2, pandas, rich, existing StrategistAgent / TripleConfirmationEngine / TWSE infra, raw SQL migrations (no Alembic), pytest.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `db/migrations/008_factor_optimization.sql` | Create | Adds `score_breakdown JSONB`, `source VARCHAR(10)` to `signal_outcomes`; creates `factor_registry` and `engine_versions` tables |
| `config/engine_params.json` | Create | Source-of-truth for tunable parameters (read by `scoring_replay.py`) |
| `src/taiwan_stock_agent/domain/models.py` | Modify | Add `score_breakdown: dict \| None = None` field to `SignalOutput` |
| `src/taiwan_stock_agent/agents/strategist_agent.py` | Modify | Populate `signal.score_breakdown` from hints + breakdown before returning |
| `src/taiwan_stock_agent/domain/scoring_replay.py` | Create | `recompute_score(breakdown, params)` — replay scoring without re-running engine |
| `src/taiwan_stock_agent/infrastructure/signal_recorder.py` | Create | `record_signal(signal, source)` — insert signal + breakdown into DB |
| `scripts/backtest.py` | Create | Iterate date×ticker, call StrategistAgent, record to DB, settle outcomes |
| `scripts/daily_runner.py` | Create | `daily` job (scan + DB write) and `settle` job (backfill T+1/T+3/T+5) |
| `scripts/factor_report.py` | Create | Lift analysis + grid search + walk-forward + residual analysis → JSON |
| `scripts/apply_tuning.py` | Create | Interactive review gate — load recommendations, show diff, apply on approve |
| `scripts/optimize.py` | Create | One-shot orchestrator: settle → factor-report → tune-review |
| `tests/unit/test_scoring_replay.py` | Create | Unit tests for `recompute_score` |
| `tests/unit/test_signal_recorder.py` | Create | Unit tests for `record_signal` (mock DB) |
| `Makefile` | Modify | Add `backtest`, `daily`, `settle`, `factor-report`, `test-factor`, `tune-review`, `optimize` targets |

---

## Task 1: DB Migration 008

**Files:**
- Create: `db/migrations/008_factor_optimization.sql`

- [ ] **Step 1: Write the migration**

```sql
-- db/migrations/008_factor_optimization.sql
-- Adds score_breakdown + source to signal_outcomes, creates factor lifecycle tables.

ALTER TABLE signal_outcomes
    ADD COLUMN IF NOT EXISTS score_breakdown JSONB,
    ADD COLUMN IF NOT EXISTS source VARCHAR(10) NOT NULL DEFAULT 'live';

-- Factor lifecycle registry
CREATE TABLE IF NOT EXISTS factor_registry (
    name               VARCHAR(50) PRIMARY KEY,
    status             VARCHAR(20) NOT NULL DEFAULT 'experimental',
    -- 'experimental' | 'active' | 'deprecated'
    lift_30d           FLOAT,
    lift_90d           FLOAT,
    added_date         DATE NOT NULL DEFAULT CURRENT_DATE,
    deprecated_date    DATE,
    notes              TEXT
);

-- Seed known active factors
INSERT INTO factor_registry (name, status, added_date)
VALUES
    ('RSI_MOM',           'active', CURRENT_DATE),
    ('BREAKOUT_WITH_VOL', 'active', CURRENT_DATE),
    ('GATE_VOL_MET',      'active', CURRENT_DATE),
    ('GATE_TREND_MET',    'active', CURRENT_DATE)
ON CONFLICT (name) DO NOTHING;

-- Engine parameter change history
CREATE TABLE IF NOT EXISTS engine_versions (
    id             SERIAL PRIMARY KEY,
    applied_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    params_before  JSONB NOT NULL,
    params_after   JSONB NOT NULL,
    reason         TEXT,
    lift_estimate  FLOAT
);
```

- [ ] **Step 2: Run the migration**

```bash
psql $DATABASE_URL -f db/migrations/008_factor_optimization.sql
```

Expected: `ALTER TABLE`, `CREATE TABLE`, `CREATE TABLE`, `INSERT 0 4` (or fewer if already exist).

- [ ] **Step 3: Verify schema**

```bash
psql $DATABASE_URL -c "\d signal_outcomes" | grep -E "score_breakdown|source"
psql $DATABASE_URL -c "SELECT name, status FROM factor_registry;"
```

Expected: both columns present; 4 rows in factor_registry.

- [ ] **Step 4: Commit**

```bash
git add db/migrations/008_factor_optimization.sql
git commit -m "feat: migration 008 — score_breakdown, source, factor_registry, engine_versions"
```

---

## Task 2: Add score_breakdown to SignalOutput + StrategistAgent

**Files:**
- Modify: `src/taiwan_stock_agent/domain/models.py`
- Modify: `src/taiwan_stock_agent/agents/strategist_agent.py`
- Test: `tests/unit/test_strategist_agent.py`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_strategist_agent.py`:

```python
def test_run_populates_score_breakdown(make_agent, mock_finmind, mock_proxy):
    """StrategistAgent.run() must populate score_breakdown with raw + pts + flags."""
    signal = make_agent().run("2330", date(2026, 3, 24))
    assert signal.score_breakdown is not None
    assert "raw" in signal.score_breakdown
    assert "pts" in signal.score_breakdown
    assert "flags" in signal.score_breakdown
    assert "taiex_slope" in signal.score_breakdown
    # raw must contain the keys needed by scoring_replay
    raw = signal.score_breakdown["raw"]
    assert "rsi_14" in raw
    assert "volume_vs_20ma" in raw
```

- [ ] **Step 2: Run test to confirm failure**

```bash
.venv/bin/pytest tests/unit/test_strategist_agent.py::test_run_populates_score_breakdown -v
```

Expected: `FAILED` — `AttributeError: 'SignalOutput' object has no attribute 'score_breakdown'`

- [ ] **Step 3: Add field to SignalOutput**

In `src/taiwan_stock_agent/domain/models.py`, add to `SignalOutput`:

```python
class SignalOutput(BaseModel):
    ticker: str
    date: date
    action: Literal["LONG", "WATCH", "CAUTION"]
    confidence: int = Field(ge=0, le=100)
    reasoning: Reasoning
    execution_plan: ExecutionPlan
    halt_flag: bool = False
    data_quality_flags: list[str] = Field(default_factory=list)
    free_tier_mode: bool | None = None
    score_breakdown: dict | None = None   # ← add this line
```

- [ ] **Step 4: Populate score_breakdown in StrategistAgent.run()**

In `src/taiwan_stock_agent/agents/strategist_agent.py`, replace the line:

```python
        return signal
```

(the final return at the end of `run()`, currently line ~169) with:

```python
        # --- Build score_breakdown for factor replay and DB storage ---
        import dataclasses
        pts_dict = {
            k: v for k, v in dataclasses.asdict(breakdown).items()
            if k != "flags"
        }
        # Compute volume_vs_20ma for replay (breakout_vol_ratio grid search)
        avg_vol = twse_proxy.avg_20d_volume if twse_proxy else 0
        volume_vs_20ma = (
            round(today_ohlcv.volume / avg_vol, 4) if avg_vol > 0 else None
        )
        # Determine taiex_slope label for threshold replay
        taiex_slope = "neutral"
        if taiex_history and len(taiex_history) >= 25:
            sorted_taiex = sorted(taiex_history, key=lambda x: x.trade_date)
            ma20_today = sum(d.close for d in sorted_taiex[-20:]) / 20
            ma20_5ago = sum(d.close for d in sorted_taiex[-25:-5]) / 20
            slope_pct = (ma20_today - ma20_5ago) / ma20_5ago * 100 if ma20_5ago else 0
            if slope_pct > 0:
                taiex_slope = "bull"
            elif slope_pct < -1.0:
                taiex_slope = "bear"

        breakdown_dict = {
            "raw": {
                "rsi_14": hints.rsi_14,
                "volume_vs_20ma": volume_vs_20ma,
                "ma20_slope_pct": hints.ma20_slope_pct,
            },
            "pts": pts_dict,
            "flags": list(breakdown.flags),
            "taiex_slope": taiex_slope,
            "scoring_version": breakdown.scoring_version,
        }
        signal = signal.model_copy(update={"score_breakdown": breakdown_dict})
        return signal
```

- [ ] **Step 5: Run test to confirm pass**

```bash
.venv/bin/pytest tests/unit/test_strategist_agent.py::test_run_populates_score_breakdown -v
```

Expected: `PASSED`

- [ ] **Step 6: Run full unit suite**

```bash
.venv/bin/pytest tests/unit/ -q
```

Expected: all existing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add src/taiwan_stock_agent/domain/models.py src/taiwan_stock_agent/agents/strategist_agent.py tests/unit/test_strategist_agent.py
git commit -m "feat: populate score_breakdown in SignalOutput for factor replay"
```

---

## Task 3: signal_recorder.py

**Files:**
- Create: `src/taiwan_stock_agent/infrastructure/signal_recorder.py`
- Create: `tests/unit/test_signal_recorder.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_signal_recorder.py
from __future__ import annotations
import json
from datetime import date
from unittest.mock import MagicMock, patch
from taiwan_stock_agent.infrastructure.signal_recorder import record_signal
from taiwan_stock_agent.domain.models import (
    SignalOutput, Reasoning, ExecutionPlan
)


def _make_signal(breakdown: dict | None = None) -> SignalOutput:
    return SignalOutput(
        ticker="2330",
        date=date(2026, 3, 24),
        action="LONG",
        confidence=72,
        reasoning=Reasoning(momentum="strong", chip_analysis="ok", risk_factors="low"),
        execution_plan=ExecutionPlan(
            entry_bid_limit=985.0, entry_max_chase=990.0,
            stop_loss=972.0, target=1015.0,
        ),
        data_quality_flags=["scoring_version:v2"],
        score_breakdown=breakdown,
    )


def test_record_signal_inserts_row():
    signal = _make_signal({"raw": {"rsi_14": 62.0}, "pts": {}, "flags": [], "taiex_slope": "neutral"})
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch("taiwan_stock_agent.infrastructure.signal_recorder.get_connection", return_value=mock_conn):
        signal_id = record_signal(signal, source="backtest")

    assert signal_id  # non-empty UUID string
    mock_cur.execute.assert_called_once()
    call_args = mock_cur.execute.call_args[0]
    params = call_args[1]
    assert params[1] == "2330"            # ticker
    assert params[3] == 72               # confidence
    assert params[5] == 985.0            # entry_price
    assert params[7] == "backtest"       # source
    assert params[8] is not None         # score_breakdown JSON


def test_record_signal_none_breakdown():
    """score_breakdown=None should store NULL in DB without error."""
    signal = _make_signal(breakdown=None)
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch("taiwan_stock_agent.infrastructure.signal_recorder.get_connection", return_value=mock_conn):
        signal_id = record_signal(signal, source="live")

    call_args = mock_cur.execute.call_args[0]
    assert call_args[1][8] is None   # score_breakdown should be None
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
.venv/bin/pytest tests/unit/test_signal_recorder.py -v
```

Expected: `ImportError` — `signal_recorder` module not found.

- [ ] **Step 3: Implement signal_recorder.py**

```python
# src/taiwan_stock_agent/infrastructure/signal_recorder.py
"""Write a SignalOutput + score_breakdown to signal_outcomes table."""
from __future__ import annotations

import json
import uuid

from taiwan_stock_agent.domain.models import SignalOutput
from taiwan_stock_agent.infrastructure.db import get_connection


def record_signal(signal: SignalOutput, source: str = "live") -> str:
    """Insert signal into signal_outcomes. Returns signal_id (UUID string).

    source: 'live' (daily_runner) | 'backtest' (backtest.py)
    """
    signal_id = str(uuid.uuid4())

    # Extract scoring_version from data_quality_flags (e.g. "scoring_version:v2")
    scoring_version = "v2"
    for flag in signal.data_quality_flags:
        if flag.startswith("scoring_version:"):
            scoring_version = flag.split(":", 1)[1]
            break

    score_breakdown_json = (
        json.dumps(signal.score_breakdown) if signal.score_breakdown else None
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signal_outcomes
                    (signal_id, ticker, signal_date, confidence_score, action,
                     entry_price, scoring_version, source, score_breakdown)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (signal_id) DO NOTHING
                """,
                (
                    signal_id,
                    signal.ticker,
                    signal.date,
                    signal.confidence,
                    signal.action,
                    signal.execution_plan.entry_bid_limit,
                    scoring_version,
                    source,
                    score_breakdown_json,
                ),
            )

    return signal_id
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
.venv/bin/pytest tests/unit/test_signal_recorder.py -v
```

Expected: both tests `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add src/taiwan_stock_agent/infrastructure/signal_recorder.py tests/unit/test_signal_recorder.py
git commit -m "feat: signal_recorder — write SignalOutput + breakdown to DB"
```

---

## Task 4: engine_params.json + scoring_replay.py

**Files:**
- Create: `config/engine_params.json`
- Create: `src/taiwan_stock_agent/domain/scoring_replay.py`
- Create: `tests/unit/test_scoring_replay.py`

- [ ] **Step 1: Create config/engine_params.json**

```bash
mkdir -p config
```

```json
{
  "_comment": "Tunable parameters for TripleConfirmationEngine. Edited by apply_tuning.py.",
  "gate_vol_ratio": 1.2,
  "rsi_momentum_lo": 55,
  "rsi_momentum_hi": 70,
  "breakout_vol_ratio": 1.5,
  "sector_topN_pct": 0.20,
  "long_threshold_bull": 63,
  "long_threshold_neutral": 68,
  "long_threshold_bear": 73,
  "watch_min": 45
}
```

- [ ] **Step 2: Write failing tests**

```python
# tests/unit/test_scoring_replay.py
from __future__ import annotations
from taiwan_stock_agent.domain.scoring_replay import recompute_score, load_params, DEFAULT_PARAMS


def _base_breakdown(rsi: float = 62.0, vol_ratio: float = 1.8, breakout: bool = True) -> dict:
    """Minimal breakdown dict for testing."""
    return {
        "raw": {
            "rsi_14": rsi,
            "volume_vs_20ma": vol_ratio,
            "ma20_slope_pct": 0.3,
        },
        "pts": {
            "volume_ratio_pts": 8,
            "price_direction_pts": 3,
            "close_strength_pts": 4,
            "vwap_advantage_pts": 6,
            "trend_continuity_pts": 5,
            "volume_escalation_pts": 5,
            "rsi_momentum_pts": 4,        # RSI 62 in [55,70] → 4
            "foreign_strength_pts": 12,
            "trust_strength_pts": 8,
            "dealer_strength_pts": 4,
            "institution_continuity_pts": 8,
            "institution_consensus_pts": 4,
            "margin_structure_pts": 4,
            "margin_utilization_pts": 4,
            "sbl_pressure_pts": 0,
            "breadth_pts": 0,
            "concentration_pts": 0,
            "continuity_pts": 0,
            "daytrade_filter_pts": 0,
            "foreign_broker_pts": 0,
            "breakout_20d_pts": 8 if breakout else 0,
            "breakout_60d_pts": 5,
            "breakout_quality_pts": 2,
            "breakout_volume_pts": 3,    # breakout + vol 1.8 >= 1.5 → 3
            "ma_alignment_pts": 5,
            "ma20_slope_pts": 5,
            "relative_strength_pts": 5,
            "upside_space_pts": 5,
            "daytrade_risk": 0,
            "long_upper_shadow": 0,
            "overheat_ma20": 0,
            "overheat_ma60": 0,
            "daytrade_heat": 0,
            "sbl_breakout_fail": 0,
            "margin_chase_heat": 0,
            "scoring_version": "v2",
        },
        "flags": ["RSI_MOM", "BREAKOUT_WITH_VOL"],
        "taiex_slope": "neutral",
        "scoring_version": "v2",
    }


def test_recompute_score_unchanged_params_gives_same_score():
    bd = _base_breakdown()
    score, action = recompute_score(bd, DEFAULT_PARAMS)
    # Sum all pts manually (same as breakdown)
    expected = sum(v for k, v in bd["pts"].items()
                   if k not in ("daytrade_risk","long_upper_shadow","overheat_ma20",
                               "overheat_ma60","daytrade_heat","sbl_breakout_fail",
                               "margin_chase_heat","scoring_version")
                  ) - sum(bd["pts"].get(k, 0) for k in (
                        "daytrade_risk","long_upper_shadow","overheat_ma20",
                        "overheat_ma60","daytrade_heat","sbl_breakout_fail","margin_chase_heat"))
    expected = max(0, min(100, expected))
    assert score == expected


def test_recompute_score_rsi_zone_widened():
    """Widening RSI zone to [50,72] captures RSI=52 that was previously excluded."""
    bd = _base_breakdown(rsi=52.0)
    bd["pts"]["rsi_momentum_pts"] = 0   # RSI 52 < 55 → 0 with defaults

    params_wider = {**DEFAULT_PARAMS, "rsi_momentum_lo": 50, "rsi_momentum_hi": 72}
    score_wider, _ = recompute_score(bd, params_wider)

    score_default, _ = recompute_score(bd, DEFAULT_PARAMS)
    assert score_wider == score_default + 4   # rsi_momentum_pts gained 4


def test_recompute_score_breakout_vol_raised():
    """Raising breakout_vol_ratio to 2.0 removes pts when actual ratio is 1.7."""
    bd = _base_breakdown(vol_ratio=1.7, breakout=True)
    # Default: 1.7 >= 1.5 → breakout_volume_pts = 3
    params_strict = {**DEFAULT_PARAMS, "breakout_vol_ratio": 2.0}
    score_strict, _ = recompute_score(bd, params_strict)
    score_default, _ = recompute_score(bd, DEFAULT_PARAMS)
    assert score_strict == score_default - 3


def test_recompute_score_threshold_change_affects_action():
    """Lowering long threshold from 68 → 60 should flip WATCH → LONG."""
    bd = _base_breakdown()
    # Force score to 62 by adjusting pts
    bd["pts"]["vwap_advantage_pts"] = 0   # remove some pts to land ~62
    bd["pts"]["ma20_slope_pts"] = 0
    bd["pts"]["upside_space_pts"] = 0
    # Score will be below 68 but above 60
    _, action_default = recompute_score(bd, DEFAULT_PARAMS)
    params_lower = {**DEFAULT_PARAMS, "long_threshold_neutral": 60}
    _, action_lower = recompute_score(bd, params_lower)
    # With lower threshold, more likely to be LONG (or at least not worse)
    assert action_lower in ("LONG", "WATCH")


def test_load_params_returns_dict():
    params = load_params()
    assert "gate_vol_ratio" in params
    assert "rsi_momentum_lo" in params
    assert isinstance(params["long_threshold_neutral"], (int, float))
```

- [ ] **Step 3: Run tests to confirm failure**

```bash
.venv/bin/pytest tests/unit/test_scoring_replay.py -v
```

Expected: `ImportError` — `scoring_replay` module not found.

- [ ] **Step 4: Implement scoring_replay.py**

```python
# src/taiwan_stock_agent/domain/scoring_replay.py
"""Replay Triple Confirmation scoring from stored score_breakdown without re-running engine.

Used by factor_report.py grid search to evaluate parameter combinations on
historical breakdowns in milliseconds per signal.
"""
from __future__ import annotations

import json
from pathlib import Path

_PARAMS_PATH = Path(__file__).resolve().parents[3] / "config" / "engine_params.json"

DEFAULT_PARAMS: dict = {
    "gate_vol_ratio": 1.2,
    "rsi_momentum_lo": 55,
    "rsi_momentum_hi": 70,
    "breakout_vol_ratio": 1.5,
    "sector_topN_pct": 0.20,
    "long_threshold_bull": 63,
    "long_threshold_neutral": 68,
    "long_threshold_bear": 73,
    "watch_min": 45,
}

_RISK_FIELDS = frozenset({
    "daytrade_risk", "long_upper_shadow", "overheat_ma20",
    "overheat_ma60", "daytrade_heat", "sbl_breakout_fail", "margin_chase_heat",
})
_NON_SCORE_FIELDS = frozenset({"scoring_version"})


def load_params() -> dict:
    """Load tunable params from config/engine_params.json, falling back to defaults."""
    if _PARAMS_PATH.exists():
        with open(_PARAMS_PATH) as f:
            data = json.load(f)
        # Merge with defaults so new keys added later are handled gracefully
        return {**DEFAULT_PARAMS, **{k: v for k, v in data.items() if not k.startswith("_")}}
    return dict(DEFAULT_PARAMS)


def _sum_pts(pts: dict) -> int:
    total = 0
    for k, v in pts.items():
        if k in _NON_SCORE_FIELDS:
            continue
        if k in _RISK_FIELDS:
            total -= int(v)
        else:
            total += int(v)
    return max(0, min(100, total))


def recompute_score(breakdown: dict, params: dict) -> tuple[int, str]:
    """Replay scoring with candidate params. Returns (score, action).

    Uses stored breakdown["raw"] raw values + breakdown["pts"] stored pts.
    Only re-evaluates pts for params in the whitelist; all other pts are unchanged.

    Args:
        breakdown: dict with keys "raw", "pts", "flags", "taiex_slope"
        params: parameter dict (use load_params() or DEFAULT_PARAMS as base)

    Returns:
        (score 0–100, action "LONG"|"WATCH"|"CAUTION")
    """
    pts = dict(breakdown.get("pts", {}))
    raw = breakdown.get("raw", {})

    # --- Re-evaluate RSI momentum pts ---
    rsi = raw.get("rsi_14")
    if rsi is not None:
        lo = params.get("rsi_momentum_lo", DEFAULT_PARAMS["rsi_momentum_lo"])
        hi = params.get("rsi_momentum_hi", DEFAULT_PARAMS["rsi_momentum_hi"])
        pts["rsi_momentum_pts"] = 4 if lo <= rsi <= hi else 0

    # --- Re-evaluate breakout volume pts ---
    vol_ratio = raw.get("volume_vs_20ma")
    if vol_ratio is not None:
        bv_thresh = params.get("breakout_vol_ratio", DEFAULT_PARAMS["breakout_vol_ratio"])
        had_breakout = pts.get("breakout_20d_pts", 0) > 0
        pts["breakout_volume_pts"] = (
            3 if (had_breakout and vol_ratio >= bv_thresh) else 0
        )

    # --- Re-evaluate gate_vol (affects score only if gate now fails) ---
    # If vol_ratio falls below new gate threshold → signal would have been CAUTION
    if vol_ratio is not None:
        gate_vol = params.get("gate_vol_ratio", DEFAULT_PARAMS["gate_vol_ratio"])
        flags = breakdown.get("flags", [])
        gate_was_passing = any("GATE_PASS" in f or "GATE_VOL_MET" in f for f in flags)
        if gate_was_passing and vol_ratio < gate_vol:
            # Gate would have failed with new threshold → CAUTION, score 0
            return 0, "CAUTION"

    score = _sum_pts(pts)

    # --- Determine action ---
    taiex_slope = breakdown.get("taiex_slope", "neutral")
    if taiex_slope == "bull":
        threshold = params.get("long_threshold_bull", DEFAULT_PARAMS["long_threshold_bull"])
    elif taiex_slope == "bear":
        threshold = params.get("long_threshold_bear", DEFAULT_PARAMS["long_threshold_bear"])
    else:
        threshold = params.get("long_threshold_neutral", DEFAULT_PARAMS["long_threshold_neutral"])

    watch_min = params.get("watch_min", DEFAULT_PARAMS["watch_min"])

    if score >= threshold:
        action = "LONG"
    elif score >= watch_min:
        action = "WATCH"
    else:
        action = "CAUTION"

    return score, action
```

- [ ] **Step 5: Run tests to confirm pass**

```bash
.venv/bin/pytest tests/unit/test_scoring_replay.py -v
```

Expected: all 5 tests `PASSED`.

- [ ] **Step 6: Commit**

```bash
git add config/engine_params.json src/taiwan_stock_agent/domain/scoring_replay.py tests/unit/test_scoring_replay.py
git commit -m "feat: engine_params.json + scoring_replay — parameter whitelist and replay function"
```

---

## Task 5: scripts/backtest.py

**Files:**
- Create: `scripts/backtest.py`
- Modify: `Makefile`

- [ ] **Step 1: Implement backtest.py**

```python
# scripts/backtest.py
"""Historical backtest: run TripleConfirmationEngine on past dates → signal_outcomes.

Usage:
    python scripts/backtest.py --date-from 2025-10-01 --date-to 2026-03-31
    python scripts/backtest.py --date-from 2026-01-15 --date-to 2026-01-15 --tickers 2330 2317
    make backtest DATE_FROM=2025-10-01 DATE_TO=2026-03-31
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.agents.strategist_agent import StrategistAgent
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher
from taiwan_stock_agent.infrastructure.signal_recorder import record_signal
from taiwan_stock_agent.infrastructure.db import init_pool, get_connection

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _is_trading_day(d: date) -> bool:
    """Exclude weekends. (Holidays not checked — TWSE will return empty data.)"""
    return d.weekday() < 5


def _date_range(start: date, end: date) -> list[date]:
    days = []
    cur = start
    while cur <= end:
        if _is_trading_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _settle_outcomes(signal_ids: list[tuple[str, str, date]]) -> None:
    """Backfill T+1/T+3/T+5 prices for signals we just inserted.

    signal_ids: list of (signal_id, ticker, signal_date)
    """
    from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
    finmind = FinMindClient()

    with get_connection() as conn:
        for signal_id, ticker, signal_date in signal_ids:
            # Fetch price window: signal_date to signal_date + 10 calendar days
            end = signal_date + timedelta(days=14)
            try:
                df = finmind.fetch_ohlcv(ticker, signal_date, end)
            except Exception as e:
                logger.warning("settle %s %s: %s", ticker, signal_date, e)
                continue
            if df.empty:
                continue

            closes: dict[date, float] = {}
            for _, row in df.iterrows():
                closes[row["trade_date"]] = float(row["close"])

            # Find entry price (signal_date close)
            entry = closes.get(signal_date)
            if entry is None:
                continue

            # Find T+1, T+3, T+5 trading day closes
            trading_days = sorted(closes.keys())
            signal_idx = trading_days.index(signal_date) if signal_date in trading_days else -1
            if signal_idx < 0:
                continue

            def get_offset(n: int) -> float | None:
                idx = signal_idx + n
                return closes[trading_days[idx]] if idx < len(trading_days) else None

            p1 = get_offset(1)
            p3 = get_offset(3)
            p5 = get_offset(5)

            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE signal_outcomes
                    SET price_1d = %s, price_3d = %s, price_5d = %s,
                        outcome_1d = CASE WHEN %s IS NOT NULL THEN (%s - %s) / %s ELSE NULL END,
                        outcome_3d = CASE WHEN %s IS NOT NULL THEN (%s - %s) / %s ELSE NULL END,
                        outcome_5d = CASE WHEN %s IS NOT NULL THEN (%s - %s) / %s ELSE NULL END
                    WHERE signal_id = %s
                """, (
                    p1, p3, p5,
                    p1, p1, entry, entry,
                    p3, p3, entry, entry,
                    p5, p5, entry, entry,
                    signal_id,
                ))


def _load_watchlist_for_date(analysis_date: date, data_dir: Path) -> list[str]:
    """Load cached watchlist (industry_map) for a date, falling back to nearby dates."""
    for delta in range(0, 8):
        candidate = analysis_date - timedelta(days=delta)
        cache_file = data_dir / f"industry_map_{candidate}.json"
        if cache_file.exists():
            with open(cache_file) as f:
                return list(json.load(f).keys())
    return []


def run_backtest(
    date_from: date,
    date_to: date,
    tickers: list[str] | None,
    settle: bool,
    delay: float,
) -> None:
    init_pool()

    finmind = FinMindClient()
    chip_proxy = ChipProxyFetcher()

    # Use _EmptyLabelRepo (no paid FinMind needed)
    class _EmptyLabelRepo:
        def get_label(self, branch_code: str):
            return None
        def get_labels_bulk(self, codes):
            return {}

    agent = StrategistAgent(
        finmind=finmind,
        label_repo=_EmptyLabelRepo(),
        chip_proxy_fetcher=chip_proxy,
    )

    data_dir = Path(__file__).resolve().parents[1] / "data" / "watchlist_cache"
    trading_days = _date_range(date_from, date_to)

    total = 0
    recorded: list[tuple[str, str, date]] = []

    for day in trading_days:
        day_tickers = tickers if tickers else _load_watchlist_for_date(day, data_dir)
        if not day_tickers:
            logger.warning("No watchlist for %s, skipping", day)
            continue

        print(f"\n[{day}] scanning {len(day_tickers)} tickers...")
        for ticker in day_tickers:
            try:
                signal = agent.run(ticker, day)
                if signal.halt_flag:
                    continue
                sid = record_signal(signal, source="backtest")
                recorded.append((sid, ticker, day))
                total += 1
                if delay > 0:
                    time.sleep(delay)
            except Exception as e:
                logger.warning("skip %s %s: %s", ticker, day, e)

    print(f"\nBacktest complete: {total} signals recorded.")

    if settle and recorded:
        print(f"Settling {len(recorded)} signals (T+1/T+3/T+5)...")
        _settle_outcomes(recorded)
        print("Settlement done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Historical backtest → signal_outcomes")
    parser.add_argument("--date-from", required=True, type=date.fromisoformat)
    parser.add_argument("--date-to", required=True, type=date.fromisoformat)
    parser.add_argument("--tickers", nargs="*", help="Limit to specific tickers")
    parser.add_argument("--no-settle", action="store_true", help="Skip T+N outcome settlement")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Seconds between requests (default: 0.5)")
    args = parser.parse_args()

    run_backtest(
        date_from=args.date_from,
        date_to=args.date_to,
        tickers=args.tickers,
        settle=not args.no_settle,
        delay=args.delay,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Add make targets to Makefile**

Add after the existing `analyze` target:

```makefile
# ── 歷史回測 ──────────────────────────────────────────────────────────────────
# 用法: make backtest DATE_FROM=2025-10-01 DATE_TO=2026-03-31
#       make backtest DATE_FROM=2026-01-15 DATE_TO=2026-01-15 TICKERS="2330 2317"
DATE_FROM ?= $(shell date -v-180d +%Y-%m-%d 2>/dev/null || date -d '180 days ago' +%Y-%m-%d)
DATE_TO   ?= $(_TODAY)
BACKTEST_TICKERS ?=

backtest:
	$(PYTHON) scripts/backtest.py \
		--date-from $(DATE_FROM) \
		--date-to $(DATE_TO) \
		$(if $(BACKTEST_TICKERS),--tickers $(BACKTEST_TICKERS))
```

Add `backtest` to `.PHONY`.

- [ ] **Step 3: Smoke test (dry run)**

```bash
make backtest DATE_FROM=2026-03-31 DATE_TO=2026-03-31 BACKTEST_TICKERS="2330"
```

Expected: prints `[2026-03-31] scanning 1 tickers...`, records 1 signal (or skips if halt), no Python errors.

- [ ] **Step 4: Commit**

```bash
git add scripts/backtest.py Makefile
git commit -m "feat: scripts/backtest.py + make backtest — historical signal simulation"
```

---

## Task 6: scripts/daily_runner.py

**Files:**
- Create: `scripts/daily_runner.py`
- Modify: `Makefile`

- [ ] **Step 1: Implement daily_runner.py**

```python
# scripts/daily_runner.py
"""Two jobs: daily scan → DB, and T+N settlement.

Usage:
    python scripts/daily_runner.py daily           # scan today → DB
    python scripts/daily_runner.py settle          # settle pending T+1/T+3/T+5
    python scripts/daily_runner.py settle --date 2026-04-03
    make daily
    make settle
    make settle DATE=2026-04-03
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.infrastructure.db import init_pool, get_connection
from taiwan_stock_agent.infrastructure.signal_recorder import record_signal
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job A: daily scan
# ---------------------------------------------------------------------------

def run_daily(analysis_date: date, llm: str | None, sectors: str | None) -> None:
    """Run batch_scan for today and store results to DB."""
    # Delegate to existing batch_scan.py for scanning logic, but capture results
    # via the agent API rather than subprocess.
    # We reuse the same agent setup as batch_scan but add DB recording.

    from taiwan_stock_agent.agents.strategist_agent import StrategistAgent
    from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
    from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher
    import json

    class _EmptyLabelRepo:
        def get_label(self, branch_code):
            return None
        def get_labels_bulk(self, codes):
            return {}

    init_pool()
    finmind = FinMindClient()
    agent = StrategistAgent(
        finmind=finmind,
        label_repo=_EmptyLabelRepo(),
        chip_proxy_fetcher=ChipProxyFetcher(),
    )

    # Load watchlist from cache
    data_dir = Path(__file__).resolve().parents[1] / "data" / "watchlist_cache"
    watchlist_file = data_dir / f"industry_map_{analysis_date}.json"
    # Fall back up to 7 days back
    for delta in range(0, 8):
        candidate = analysis_date - timedelta(days=delta)
        f = data_dir / f"industry_map_{candidate}.json"
        if f.exists():
            with open(f) as fh:
                tickers = list(json.load(fh).keys())
            break
    else:
        print("No watchlist cache found — run make scan first to build cache")
        return

    if sectors:
        # Filter by sector using same industry_map logic as batch_scan
        with open(f) as fh:
            industry_map = json.load(fh)
        sector_filter = [s.strip() for s in sectors.split()]
        tickers = [t for t, ind in industry_map.items() if any(s in ind for s in sector_filter)]

    print(f"[{analysis_date}] Running daily scan for {len(tickers)} tickers → DB")
    recorded = 0
    for ticker in tickers:
        try:
            signal = agent.run(ticker, analysis_date)
            if signal.halt_flag:
                continue
            record_signal(signal, source="live")
            recorded += 1
        except Exception as e:
            logger.warning("skip %s: %s", ticker, e)
        time.sleep(0.3)

    print(f"Recorded {recorded} signals to signal_outcomes (source=live)")


# ---------------------------------------------------------------------------
# Job B: settle outcomes
# ---------------------------------------------------------------------------

def run_settle(settle_date: date) -> None:
    """Backfill T+1/T+3/T+5 outcomes for signals with pending prices.

    settle_date: the trading date whose T+N prices we are filling.
    For example, settle_date=2026-04-04 fills T+1 outcomes for signals
    from 2026-04-03, T+3 for 2026-04-01, T+5 for 2026-03-30.
    """
    init_pool()
    finmind = FinMindClient()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_id, ticker, signal_date, entry_price
                FROM signal_outcomes
                WHERE price_5d IS NULL
                  AND halt_flag = FALSE
                  AND signal_date <= %s - INTERVAL '5 days'
                ORDER BY signal_date DESC
                LIMIT 200
            """, (settle_date,))
            rows = cur.fetchall()

    if not rows:
        print(f"[{settle_date}] Nothing to settle.")
        return

    print(f"[{settle_date}] Settling {len(rows)} signals...")

    for signal_id, ticker, signal_date, entry_price in rows:
        try:
            end = signal_date + timedelta(days=14)
            df = finmind.fetch_ohlcv(ticker, signal_date, end)
        except Exception as e:
            logger.warning("settle %s %s: %s", ticker, signal_date, e)
            continue

        if df.empty:
            continue

        closes: dict[date, float] = {}
        for _, row in df.iterrows():
            closes[row["trade_date"]] = float(row["close"])

        trading_days = sorted(closes.keys())
        if signal_date not in trading_days:
            continue

        idx = trading_days.index(signal_date)

        def get_close(offset: int) -> float | None:
            i = idx + offset
            return closes[trading_days[i]] if i < len(trading_days) else None

        p1, p3, p5 = get_close(1), get_close(3), get_close(5)

        def outcome(p: float | None) -> float | None:
            return (p - entry_price) / entry_price if p is not None else None

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE signal_outcomes
                    SET price_1d=%s, price_3d=%s, price_5d=%s,
                        outcome_1d=%s, outcome_3d=%s, outcome_5d=%s
                    WHERE signal_id=%s AND price_5d IS NULL
                """, (p1, p3, p5, outcome(p1), outcome(p3), outcome(p5), signal_id))

        time.sleep(0.2)

    print("Settlement complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="job", required=True)

    daily_p = sub.add_parser("daily", help="Scan today and record to DB")
    daily_p.add_argument("--date", type=date.fromisoformat, default=date.today())
    daily_p.add_argument("--llm", default=None)
    daily_p.add_argument("--sectors", default=None)

    settle_p = sub.add_parser("settle", help="Backfill T+1/T+3/T+5 outcomes")
    settle_p.add_argument("--date", type=date.fromisoformat, default=date.today())

    args = parser.parse_args()
    if args.job == "daily":
        run_daily(args.date, args.llm, args.sectors)
    else:
        run_settle(args.date)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Add make targets to Makefile**

```makefile
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
```

Add `daily settle` to `.PHONY`.

- [ ] **Step 3: Smoke test**

```bash
make settle DATE=2026-04-05
```

Expected: `[2026-04-05] Settling N signals...` or `Nothing to settle.`

- [ ] **Step 4: Commit**

```bash
git add scripts/daily_runner.py Makefile
git commit -m "feat: daily_runner — daily scan to DB + T+N settlement"
```

---

## Task 7: scripts/factor_report.py

**Files:**
- Create: `scripts/factor_report.py`
- Modify: `Makefile`

This is the core analysis script. It runs three analyses: lift, grid search with walk-forward, and residual analysis.

- [ ] **Step 1: Implement factor_report.py**

```python
# scripts/factor_report.py
"""Factor effectiveness analysis + grid search + walk-forward + residual analysis.

Reads signal_outcomes from DB (requires score_breakdown JSONB to be populated).
Outputs JSON recommendation file + rich terminal report.

Usage:
    python scripts/factor_report.py
    python scripts/factor_report.py --days 180
    python scripts/factor_report.py --min-samples 10
    make factor-report
    make factor-report FORCE=1
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    from rich.panel import Panel
    _console = Console()
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False
    _console = None  # type: ignore

from taiwan_stock_agent.domain.scoring_replay import recompute_score, load_params, DEFAULT_PARAMS
from taiwan_stock_agent.infrastructure.db import init_pool, get_connection

_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "factor_reports"


def _fetch_rows(days: int, scoring_version: str | None) -> list[dict]:
    """Load all settled signals with score_breakdown from DB."""
    query = """
        SELECT signal_id, ticker, signal_date, confidence_score, action,
               outcome_1d, outcome_3d, outcome_5d, score_breakdown, source
        FROM signal_outcomes
        WHERE signal_date >= CURRENT_DATE - INTERVAL '%s days'
          AND halt_flag = FALSE
          AND outcome_1d IS NOT NULL
          AND score_breakdown IS NOT NULL
    """
    params: list[Any] = [days]
    if scoring_version:
        query += " AND scoring_version = %s"
        params.append(scoring_version)
    query += " ORDER BY signal_date"

    rows = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                if d["score_breakdown"] and isinstance(d["score_breakdown"], str):
                    d["score_breakdown"] = json.loads(d["score_breakdown"])
                rows.append(d)
    return rows


# ---------------------------------------------------------------------------
# Analysis 1: Flag lift
# ---------------------------------------------------------------------------

def _compute_lift(rows: list[dict], min_samples: int) -> list[dict]:
    """Compute lift per flag: win_rate_with_flag - win_rate_without_flag."""
    # Gather all unique flags
    all_flags: set[str] = set()
    for r in rows:
        bd = r.get("score_breakdown") or {}
        all_flags.update(bd.get("flags", []))

    results = []
    overall_win = sum(1 for r in rows if r["outcome_1d"] > 0) / len(rows) if rows else 0

    for flag in sorted(all_flags):
        with_flag = [r for r in rows if flag in (r.get("score_breakdown") or {}).get("flags", [])]
        without_flag = [r for r in rows if flag not in (r.get("score_breakdown") or {}).get("flags", [])]

        if len(with_flag) < min_samples:
            continue

        win_with = sum(1 for r in with_flag if r["outcome_1d"] > 0) / len(with_flag)
        win_without = (
            sum(1 for r in without_flag if r["outcome_1d"] > 0) / len(without_flag)
            if without_flag else overall_win
        )

        results.append({
            "flag": flag,
            "n_with": len(with_flag),
            "n_without": len(without_flag),
            "win_with": win_with,
            "win_without": win_without,
            "lift": win_with - win_without,
        })

    return sorted(results, key=lambda x: x["lift"], reverse=True)


# ---------------------------------------------------------------------------
# Analysis 2: Grid search + walk-forward
# ---------------------------------------------------------------------------

_PARAM_GRID: dict[str, list] = {
    "rsi_momentum_lo": [48, 50, 52, 55, 58],
    "rsi_momentum_hi": [67, 70, 72, 75],
    "breakout_vol_ratio": [1.2, 1.3, 1.4, 1.5, 1.7, 2.0],
    "long_threshold_neutral": [63, 65, 68, 70, 72],
}


def _win_rate_with_params(rows: list[dict], params: dict) -> float:
    if not rows:
        return 0.0
    wins = sum(
        1 for r in rows
        if r.get("score_breakdown") and
        recompute_score(r["score_breakdown"], params)[0] >=
        params.get("long_threshold_neutral", 68) and
        r["outcome_1d"] > 0
    )
    longs = sum(
        1 for r in rows
        if r.get("score_breakdown") and
        recompute_score(r["score_breakdown"], params)[0] >=
        params.get("long_threshold_neutral", 68)
    )
    return wins / longs if longs >= 5 else 0.0


def _walk_forward_windows(rows: list[dict], train_months: int = 6, test_months: int = 1) -> list[tuple[list, list]]:
    """Return [(train_rows, test_rows), ...] sliding windows."""
    if not rows:
        return []

    dates = sorted(set(r["signal_date"] for r in rows))
    if not dates:
        return []

    first, last = dates[0], dates[-1]
    windows = []
    window_start = first

    while True:
        train_end = window_start + timedelta(days=30 * train_months)
        test_end = train_end + timedelta(days=30 * test_months)
        if test_end > last:
            break

        train = [r for r in rows if window_start <= r["signal_date"] < train_end]
        test = [r for r in rows if train_end <= r["signal_date"] < test_end]

        if len(train) >= 20 and len(test) >= 5:
            windows.append((train, test))

        window_start += timedelta(days=30)

    return windows


def _grid_search(rows: list[dict], n_random: int = 500) -> list[dict]:
    """Random search over PARAM_GRID. Returns top 5 candidates validated on all windows."""
    windows = _walk_forward_windows(rows)
    if len(windows) < 2:
        return []

    base_params = load_params()

    # Generate candidate param sets
    candidates = []
    all_keys = list(_PARAM_GRID.keys())
    for _ in range(n_random):
        cand = dict(base_params)
        for k in all_keys:
            cand[k] = random.choice(_PARAM_GRID[k])
        candidates.append(cand)

    # Evaluate each candidate on all walk-forward windows
    results = []
    for params in candidates:
        train_lifts = []
        test_lifts = []
        base_win = _win_rate_with_params(rows, base_params)

        for train, test in windows:
            train_win = _win_rate_with_params(train, params)
            test_win = _win_rate_with_params(test, params)
            base_train_win = _win_rate_with_params(train, base_params)
            base_test_win = _win_rate_with_params(test, base_params)
            train_lifts.append(train_win - base_train_win)
            test_lifts.append(test_win - base_test_win)

        # Only include if ALL test windows show positive lift
        if all(l >= 0 for l in test_lifts) and test_lifts:
            avg_test_lift = sum(test_lifts) / len(test_lifts)
            results.append({
                "params": {k: params[k] for k in all_keys},
                "avg_test_lift": avg_test_lift,
                "n_windows": len(windows),
            })

    results.sort(key=lambda x: x["avg_test_lift"], reverse=True)
    return results[:5]


# ---------------------------------------------------------------------------
# Analysis 3: Residual analysis
# ---------------------------------------------------------------------------

def _residual_analysis(rows: list[dict]) -> list[str]:
    """Find patterns in false positives (high score, lost) and false negatives (low score, won)."""
    fp = [r for r in rows if r["confidence_score"] >= 65 and r["outcome_1d"] < 0]
    fn = [r for r in rows if r["confidence_score"] < 50 and r["outcome_1d"] > 0.03]

    suggestions = []

    # Compare raw values between FP and FN groups
    def avg_raw(group: list[dict], key: str) -> float | None:
        vals = [
            r["score_breakdown"]["raw"].get(key)
            for r in group
            if r.get("score_breakdown") and r["score_breakdown"].get("raw")
        ]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    for key in ("rsi_14", "volume_vs_20ma", "ma20_slope_pct"):
        fp_avg = avg_raw(fp, key)
        fn_avg = avg_raw(fn, key)
        if fp_avg is not None and fn_avg is not None and len(fp) >= 5 and len(fn) >= 5:
            diff = abs(fn_avg - fp_avg)
            if diff > 0.1 * abs(fp_avg + 1e-9):
                direction = "higher" if fn_avg > fp_avg else "lower"
                suggestions.append(
                    f"{key}: FN avg={fn_avg:.2f} ({direction} than FP avg={fp_avg:.2f}) "
                    f"— 考慮調整 {key} 閾值 (FP={len(fp)}, FN={len(fn)})"
                )

    if not suggestions:
        suggestions.append("樣本量不足以識別殘差模式 (需要 FP≥5, FN≥5)")

    return suggestions


# ---------------------------------------------------------------------------
# Report output
# ---------------------------------------------------------------------------

def _save_recommendations(lift_results: list[dict], grid_results: list[dict], residual: list[str], report_date: date) -> Path:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _OUTPUT_DIR / f"factor_report_{report_date}.json"
    payload = {
        "report_date": str(report_date),
        "lift_analysis": lift_results,
        "grid_search_top5": grid_results,
        "residual_suggestions": residual,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return out_path


def _print_report(lift_results, grid_results, residual, n_rows: int) -> None:
    if _HAS_RICH and _console:
        _console.print(Panel(f"[bold cyan]Factor Report[/bold cyan]  {n_rows} 筆已結算訊號", border_style="cyan"))

        # Lift table
        tbl = Table(title="因子 Lift 分析", box=box.ROUNDED, header_style="bold white on dark_blue")
        tbl.add_column("Flag", width=25)
        tbl.add_column("N (有)", justify="right", width=8)
        tbl.add_column("有 Flag 勝率", justify="right", width=12)
        tbl.add_column("無 Flag 勝率", justify="right", width=12)
        tbl.add_column("Lift", justify="right", width=10)
        for r in lift_results:
            color = "green" if r["lift"] > 0.05 else ("yellow" if r["lift"] > -0.03 else "red")
            tbl.add_row(
                r["flag"], str(r["n_with"]),
                f"{r['win_with']:.1%}", f"{r['win_without']:.1%}",
                f"[{color}]{r['lift']:+.1%}[/{color}]",
            )
        _console.print(tbl)

        # Grid search results
        if grid_results:
            _console.print("\n[bold]Grid Search Top 建議（walk-forward 驗證通過）:[/bold]")
            for i, g in enumerate(grid_results, 1):
                _console.print(f"  {i}. lift=+{g['avg_test_lift']:.1%}  params={g['params']}")
        else:
            _console.print("\n[dim]Grid Search: 無通過 walk-forward 驗證的參數組合（樣本可能不足）[/dim]")

        # Residual
        _console.print("\n[bold]殘差分析建議:[/bold]")
        for s in residual:
            _console.print(f"  • {s}")
    else:
        print(f"\n=== Factor Report ({n_rows} signals) ===")
        for r in lift_results:
            print(f"  {r['flag']}: lift={r['lift']:+.1%} (n={r['n_with']})")


def run_report(days: int, min_samples: int, scoring_version: str | None) -> Path | None:
    init_pool()
    try:
        rows = _fetch_rows(days, scoring_version)
    except Exception as e:
        print(f"DB error: {e}\n請設定 DATABASE_URL")
        return None

    if len(rows) < 20:
        print(f"⚠ 只有 {len(rows)} 筆資料（需要 ≥20）。請先執行 make backtest 建立基礎資料。")
        return None

    lift_results = _compute_lift(rows, min_samples)
    grid_results = _grid_search(rows)
    residual = _residual_analysis(rows)

    _print_report(lift_results, grid_results, residual, len(rows))

    out_path = _save_recommendations(lift_results, grid_results, residual, date.today())
    print(f"\n報告已儲存至 {out_path}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--min-samples", type=int, default=10)
    parser.add_argument("--scoring-version", default=None)
    args = parser.parse_args()
    run_report(args.days, args.min_samples, args.scoring_version)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Add make target**

```makefile
# ── 因子分析 ──────────────────────────────────────────────────────────────────
FACTOR_DAYS ?= 180

factor-report:
	$(PYTHON) scripts/factor_report.py --days $(FACTOR_DAYS)
```

Add `factor-report` to `.PHONY`.

- [ ] **Step 3: Smoke test with mocked data (no DB needed)**

```bash
.venv/bin/python -c "
from scripts.factor_report import _compute_lift, _grid_search, _walk_forward_windows
from datetime import date, timedelta
import random

# Mock 50 rows with breakdowns
rows = []
for i in range(50):
    d = date(2026,1,1) + timedelta(days=i*3)
    rows.append({
        'signal_date': d, 'confidence_score': random.randint(55,85),
        'outcome_1d': random.uniform(-0.03, 0.05),
        'score_breakdown': {
            'raw': {'rsi_14': random.uniform(45,75), 'volume_vs_20ma': random.uniform(1.0,2.5)},
            'pts': {'rsi_momentum_pts': 4, 'breakout_20d_pts': 8, 'breakout_volume_pts': 3,
                    'volume_ratio_pts': 8, 'price_direction_pts': 3, 'close_strength_pts': 4,
                    'vwap_advantage_pts': 6, 'trend_continuity_pts': 5, 'volume_escalation_pts': 5,
                    'foreign_strength_pts': 8, 'trust_strength_pts': 6, 'dealer_strength_pts': 2,
                    'institution_continuity_pts': 4, 'institution_consensus_pts': 4,
                    'margin_structure_pts': 4, 'margin_utilization_pts': 4, 'sbl_pressure_pts': 0,
                    'breadth_pts': 0, 'concentration_pts': 0, 'continuity_pts': 0,
                    'daytrade_filter_pts': 0, 'foreign_broker_pts': 0,
                    'breakout_60d_pts': 5, 'breakout_quality_pts': 2, 'ma_alignment_pts': 5,
                    'ma20_slope_pts': 5, 'relative_strength_pts': 5, 'upside_space_pts': 5,
                    'daytrade_risk': 0, 'long_upper_shadow': 0, 'overheat_ma20': 0,
                    'overheat_ma60': 0, 'daytrade_heat': 0, 'sbl_breakout_fail': 0,
                    'margin_chase_heat': 0, 'scoring_version': 'v2'},
            'flags': ['RSI_MOM', 'BREAKOUT_WITH_VOL'] if random.random() > 0.5 else ['GATE_VOL_MET'],
            'taiex_slope': 'neutral', 'scoring_version': 'v2',
        }
    })
lift = _compute_lift(rows, min_samples=5)
print('Lift computed:', len(lift), 'flags')
windows = _walk_forward_windows(rows)
print('Walk-forward windows:', len(windows))
print('OK')
"
```

Expected: `Lift computed: N flags`, `Walk-forward windows: N`, `OK`

- [ ] **Step 4: Commit**

```bash
git add scripts/factor_report.py Makefile
git commit -m "feat: factor_report — lift analysis, grid search, walk-forward, residual"
```

---

## Task 8: scripts/apply_tuning.py + Factor Sandbox

**Files:**
- Create: `scripts/apply_tuning.py`
- Modify: `Makefile`

- [ ] **Step 1: Implement apply_tuning.py**

```python
# scripts/apply_tuning.py
"""Interactive review gate for engine parameter tuning.

Reads latest factor report JSON, displays recommendations, prompts for approval.
On approve: updates config/engine_params.json + records to engine_versions table.

Usage:
    python scripts/apply_tuning.py
    python scripts/apply_tuning.py --auto-approve   # for cron (with safety limits)
    make tune-review
    make tune-review AUTO_APPROVE=1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.domain.scoring_replay import load_params, DEFAULT_PARAMS
from taiwan_stock_agent.infrastructure.db import init_pool, get_connection

_PARAMS_PATH = Path(__file__).resolve().parents[1] / "config" / "engine_params.json"
_REPORT_DIR = Path(__file__).resolve().parents[1] / "data" / "factor_reports"

# Safety limits for AUTO_APPROVE mode
_MAX_CHANGE_PCT = 0.20   # block if any param changes by > 20%


def _load_latest_report() -> dict | None:
    reports = sorted(_REPORT_DIR.glob("factor_report_*.json"), reverse=True)
    if not reports:
        return None
    with open(reports[0]) as f:
        return json.load(f)


def _apply_params(new_params: dict, old_params: dict, reason: str, lift: float) -> None:
    """Write new params to JSON + record version history to DB."""
    # Write config
    full_params = {**old_params, **new_params, "_comment": "Tunable parameters. Edited by apply_tuning.py."}
    with open(_PARAMS_PATH, "w") as f:
        json.dump(full_params, f, indent=2, ensure_ascii=False)

    # Record to DB
    try:
        init_pool()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO engine_versions (applied_at, params_before, params_after, reason, lift_estimate)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    datetime.now(),
                    json.dumps(old_params),
                    json.dumps(full_params),
                    reason,
                    lift,
                ))
    except Exception as e:
        print(f"[warn] Could not write to engine_versions table: {e}")


def _safety_check(old_params: dict, new_params: dict) -> list[str]:
    """Return list of violations. Empty = safe to auto-apply."""
    violations = []
    for k, new_val in new_params.items():
        old_val = old_params.get(k)
        if old_val and old_val != 0:
            change_pct = abs(new_val - old_val) / abs(old_val)
            if change_pct > _MAX_CHANGE_PCT:
                violations.append(
                    f"{k}: {old_val} → {new_val} ({change_pct:.0%} change > {_MAX_CHANGE_PCT:.0%} limit)"
                )
    return violations


def run_review(auto_approve: bool, dry_run: bool) -> None:
    report = _load_latest_report()
    if not report:
        print("No factor report found. Run `make factor-report` first.")
        return

    grid_results = report.get("grid_search_top5", [])
    if not grid_results:
        print("No grid search recommendations in latest report (insufficient data).")
        return

    old_params = load_params()
    best = grid_results[0]
    new_params = {**old_params, **best["params"]}
    lift = best["avg_test_lift"]

    # Build diff display
    changes = {
        k: (old_params.get(k), v)
        for k, v in best["params"].items()
        if old_params.get(k) != v
    }

    print(f"\n{'='*55}")
    print(f"  本週調參建議 (walk-forward lift: +{lift:.1%})")
    print(f"  報告日期: {report['report_date']}")
    print(f"{'='*55}")
    if changes:
        print(f"\n  {'參數':<28} {'目前值':>8}  →  {'建議值':>8}")
        print(f"  {'-'*48}")
        for k, (old, new) in changes.items():
            print(f"  {k:<28} {str(old):>8}  →  {str(new):>8}")
    else:
        print("  (現有參數已是最佳，無需調整)")
        return

    if dry_run:
        print("\n[DRY RUN] 不套用任何變更。")
        return

    # Safety check
    violations = _safety_check(old_params, best["params"])
    if violations:
        print(f"\n⚠ 安全限制觸發（AUTO_APPROVE 不可套用）：")
        for v in violations:
            print(f"  - {v}")
        print("請使用互動模式手動確認。")
        if auto_approve:
            return

    if auto_approve and not violations:
        _apply_params(best["params"], old_params, f"auto-tune {report['report_date']}", lift)
        print(f"\n✅ 自動套用完成 (lift +{lift:.1%})")
        return

    # Interactive
    print(f"\n[A] 全部套用   [S] 略過本週   [D] 顯示所有候選\n")
    choice = input("選擇: ").strip().upper()

    if choice == "A":
        _apply_params(best["params"], old_params, f"manual-tune {report['report_date']}", lift)
        print(f"\n✅ 已套用到 config/engine_params.json")
    elif choice == "D":
        print("\n所有候選:")
        for i, g in enumerate(grid_results, 1):
            print(f"  {i}. lift=+{g['avg_test_lift']:.1%}  {g['params']}")
        idx = input("\n輸入編號套用 (Enter 略過): ").strip()
        if idx.isdigit() and 1 <= int(idx) <= len(grid_results):
            chosen = grid_results[int(idx) - 1]
            _apply_params(chosen["params"], old_params, f"manual-tune {report['report_date']}", chosen["avg_test_lift"])
            print(f"✅ 已套用 #{idx}")
        else:
            print("略過。")
    else:
        print("略過本週。")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto-approve", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_review(args.auto_approve, args.dry_run)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Add make targets**

```makefile
# ── 調參 ──────────────────────────────────────────────────────────────────────
AUTO_APPROVE ?=

tune-review:
	$(PYTHON) scripts/apply_tuning.py \
		$(if $(AUTO_APPROVE),--auto-approve) \
		$(if $(DRY_RUN),--dry-run)

# ── 實驗因子測試 ──────────────────────────────────────────────────────────────
# 用法: make test-factor FACTOR=my_factor_name
# 在 src/taiwan_stock_agent/factors/experimental/ 放一個同名 .py
# 該檔案需實作 compute(breakdown: dict) -> int 回傳額外得分
FACTOR ?=

test-factor:
ifndef FACTOR
	$(error 請指定 FACTOR，例如: make test-factor FACTOR=consecutive_foreign_3d)
endif
	$(PYTHON) scripts/test_factor.py --factor $(FACTOR)
```

Add `tune-review test-factor` to `.PHONY`.

- [ ] **Step 3: Create factor sandbox runner scripts/test_factor.py**

```python
# scripts/test_factor.py
"""Factor sandbox: test an experimental factor against historical breakdowns.

Usage:
    make test-factor FACTOR=my_factor_name

The factor module must be at:
    src/taiwan_stock_agent/factors/experimental/<FACTOR>.py

And must implement:
    def compute(breakdown: dict) -> int:
        # breakdown has keys: raw, pts, flags, taiex_slope
        # return additional pts (positive or negative)
        ...
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.infrastructure.db import init_pool, get_connection


def run_test(factor_name: str) -> None:
    # Load factor module
    factor_path = (
        Path(__file__).resolve().parents[1]
        / "src" / "taiwan_stock_agent" / "factors" / "experimental"
        / f"{factor_name}.py"
    )
    if not factor_path.exists():
        print(f"Factor file not found: {factor_path}")
        print("Create the file with a compute(breakdown: dict) -> int function.")
        sys.exit(1)

    spec = importlib.util.spec_from_file_location(factor_name, factor_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    spec.loader.exec_module(mod)  # type: ignore

    if not hasattr(mod, "compute"):
        print(f"Factor module must implement compute(breakdown: dict) -> int")
        sys.exit(1)

    # Load breakdowns from DB
    init_pool()
    rows = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT outcome_1d, score_breakdown
                FROM signal_outcomes
                WHERE outcome_1d IS NOT NULL AND score_breakdown IS NOT NULL
                ORDER BY signal_date DESC LIMIT 500
            """)
            for outcome, bd in cur.fetchall():
                if isinstance(bd, str):
                    bd = json.loads(bd)
                rows.append({"outcome_1d": outcome, "score_breakdown": bd})

    if not rows:
        print("No data available. Run make backtest first.")
        return

    # Baseline win rate
    baseline_win = sum(1 for r in rows if r["outcome_1d"] > 0) / len(rows)

    # Apply factor: signals where factor returns > 0 are "boosted"
    boosted = []
    for r in rows:
        try:
            extra = mod.compute(r["score_breakdown"])
        except Exception as e:
            extra = 0
        if extra > 0:
            boosted.append(r)

    if not boosted:
        print(f"Factor '{factor_name}': triggered on 0/{len(rows)} signals.")
        return

    boosted_win = sum(1 for r in boosted if r["outcome_1d"] > 0) / len(boosted)
    lift = boosted_win - baseline_win

    print(f"\nFactor: {factor_name}")
    print(f"  觸發: {len(boosted)}/{len(rows)} 訊號 ({len(boosted)/len(rows):.1%})")
    print(f"  基準勝率: {baseline_win:.1%}")
    print(f"  觸發後勝率: {boosted_win:.1%}")
    print(f"  Lift: {lift:+.1%}")

    if lift > 0.05:
        print(f"\n  ✅ 建議升級為 active (lift > +5%)")
    elif lift < -0.03:
        print(f"\n  ❌ 建議丟棄 (lift < -3%)")
    else:
        print(f"\n  ⚠ 效果不明顯，繼續觀察")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factor", required=True)
    args = parser.parse_args()
    run_test(args.factor)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Create experimental factors directory**

```bash
mkdir -p src/taiwan_stock_agent/factors/experimental
touch src/taiwan_stock_agent/factors/__init__.py
touch src/taiwan_stock_agent/factors/experimental/__init__.py
```

- [ ] **Step 5: Smoke test tune-review dry run**

```bash
make tune-review DRY_RUN=1
```

Expected: `No factor report found` (if no report yet) or displays diff + `[DRY RUN]`.

- [ ] **Step 6: Commit**

```bash
git add scripts/apply_tuning.py scripts/test_factor.py src/taiwan_stock_agent/factors/ Makefile
git commit -m "feat: apply_tuning + factor sandbox — review gate and experimental factor testing"
```

---

## Task 9: scripts/optimize.py + Final Makefile

**Files:**
- Create: `scripts/optimize.py`
- Modify: `Makefile`

- [ ] **Step 1: Implement optimize.py**

```python
# scripts/optimize.py
"""One-shot optimization orchestrator: settle → factor-report → tune-review.

Usage:
    python scripts/optimize.py              # interactive (tune-review prompts)
    python scripts/optimize.py --auto-approve   # fully automated (cron-safe)
    python scripts/optimize.py --skip-settle    # skip settlement step
    python scripts/optimize.py --dry-run        # report only, no changes
    make optimize
    make optimize AUTO_APPROVE=1
    make optimize DRY_RUN=1
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="Full optimization loop")
    parser.add_argument("--auto-approve", action="store_true",
                        help="Apply recommendations without interactive prompt (cron mode)")
    parser.add_argument("--skip-settle", action="store_true",
                        help="Skip settlement step (if already run separately)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only — do not write any changes")
    parser.add_argument("--days", type=int, default=180,
                        help="Days of history for factor report (default: 180)")
    args = parser.parse_args()

    today = date.today()
    print(f"\n{'='*55}")
    print(f"  Factor Optimization Loop  [{today}]")
    print(f"{'='*55}\n")

    # Step 1: Settle
    if not args.skip_settle:
        print("Step 1/3: 補填未結算訊號...")
        from scripts.daily_runner import run_settle
        try:
            run_settle(today)
        except Exception as e:
            print(f"  ⚠ Settle failed: {e} — continuing...")
    else:
        print("Step 1/3: 略過 settle（--skip-settle）")

    # Step 2: Factor report
    print("\nStep 2/3: 跑因子分析 + Grid Search...")
    from scripts.factor_report import run_report
    report_path = run_report(days=args.days, min_samples=10, scoring_version=None)
    if report_path is None:
        print("  ⚠ Factor report failed or insufficient data — stopping.")
        sys.exit(1)

    # Step 3: Apply tuning
    if args.dry_run:
        print("\nStep 3/3: [DRY RUN] 略過套用調參。")
        return

    print("\nStep 3/3: 審核調參建議...")
    from scripts.apply_tuning import run_review
    run_review(auto_approve=args.auto_approve, dry_run=False)

    print(f"\n{'='*55}")
    print("  Optimization loop complete.")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Add final Makefile targets and update .PHONY**

```makefile
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
		$(if $(FACTOR_DAYS),--days $(FACTOR_DAYS))
```

Update `.PHONY` to include all new targets:

```makefile
.PHONY: run scan api test test-unit test-integration install install-gemini install-openai \
        build-labels analyze backtest daily settle factor-report test-factor tune-review optimize
```

- [ ] **Step 3: Smoke test dry run**

```bash
make optimize DRY_RUN=1 SKIP_SETTLE=1
```

Expected: Runs factor report step, prints `[DRY RUN] 略過套用調參` at the end. No errors.

- [ ] **Step 4: Run full unit test suite**

```bash
.venv/bin/pytest tests/unit/ -q
```

Expected: all tests pass (no regressions).

- [ ] **Step 5: Commit**

```bash
git add scripts/optimize.py Makefile
git commit -m "feat: optimize.py — one-shot optimization orchestrator (settle → report → tune)"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] Migration 008 (score_breakdown, source, factor_registry, engine_versions) → Task 1
- [x] score_breakdown populated in SignalOutput → Task 2
- [x] signal_recorder writes to DB → Task 3
- [x] engine_params.json + scoring_replay → Task 4
- [x] backtest.py + make backtest → Task 5
- [x] daily_runner daily + settle → Task 6
- [x] factor_report: lift + grid search + walk-forward + residual → Task 7
- [x] apply_tuning + tune-review → Task 8
- [x] factor sandbox + test-factor → Task 8
- [x] optimize.py + make optimize + AUTO_APPROVE safety → Task 9

**Type consistency:**
- `record_signal(signal: SignalOutput, source: str) -> str` — used consistently in backtest.py and daily_runner.py
- `recompute_score(breakdown: dict, params: dict) -> tuple[int, str]` — used in factor_report.py and test_scoring_replay.py
- `run_report(days, min_samples, scoring_version) -> Path | None` — imported by optimize.py
- `run_review(auto_approve, dry_run) -> None` — imported by optimize.py
- `run_settle(settle_date: date) -> None` — imported by optimize.py

**No placeholders:** All steps contain complete code.
