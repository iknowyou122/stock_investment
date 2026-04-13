"""Write a SignalOutput + score_breakdown to signal_outcomes table."""
from __future__ import annotations

import json
import uuid

from taiwan_stock_agent.domain.models import SignalOutput
from taiwan_stock_agent.infrastructure.db import get_connection


def record_signal(signal: SignalOutput, source: str = "live") -> str:
    """Insert signal into signal_outcomes. Returns signal_id (UUID string).

    Args:
        signal: SignalOutput with all required fields
        source: 'live' (daily_runner) | 'backtest' (backtest.py)

    Returns:
        signal_id as UUID string
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
                     entry_price, stop_loss, scoring_version, source, score_breakdown)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (signal_id) DO NOTHING
                """,
                (
                    signal_id,
                    signal.ticker,
                    signal.date,
                    signal.confidence,
                    signal.action,
                    signal.execution_plan.entry_bid_limit,
                    signal.execution_plan.stop_loss,
                    scoring_version,
                    source,
                    score_breakdown_json,
                ),
            )

    return signal_id
