"""Unit tests for signal_recorder.

Tests the record_signal() function which writes SignalOutput + score_breakdown to DB.
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

from taiwan_stock_agent.domain.models import (
    ExecutionPlan,
    Reasoning,
    SignalOutput,
)
from taiwan_stock_agent.infrastructure.signal_recorder import record_signal


def _make_signal(breakdown: dict | None = None) -> SignalOutput:
    """Factory for test SignalOutput."""
    return SignalOutput(
        ticker="2330",
        date=date(2026, 3, 24),
        action="LONG",
        confidence=72,
        reasoning=Reasoning(momentum="strong", chip_analysis="ok", risk_factors="low"),
        execution_plan=ExecutionPlan(
            entry_bid_limit=985.0,
            entry_max_chase=990.0,
            stop_loss=972.0,
            target=1015.0,
        ),
        data_quality_flags=["scoring_version:v2"],
        score_breakdown=breakdown,
    )


def test_record_signal_inserts_row():
    """Test that record_signal inserts a row with the correct parameters."""
    signal = _make_signal(
        {
            "raw": {"rsi_14": 62.0},
            "pts": {},
            "flags": [],
            "taiex_slope": "neutral",
        }
    )
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch(
        "taiwan_stock_agent.infrastructure.signal_recorder.get_connection",
        return_value=mock_conn,
    ):
        signal_id = record_signal(signal, source="backtest")

    assert signal_id  # non-empty UUID string
    mock_cur.execute.assert_called_once()
    call_args = mock_cur.execute.call_args[0]
    params = call_args[1]
    assert params[1] == "2330"  # ticker
    assert params[3] == 72  # confidence
    assert params[5] == 985.0  # entry_price (entry_bid_limit)
    assert params[6] == 972.0  # stop_loss
    assert params[8] == "backtest"  # source
    assert params[9] is not None  # score_breakdown JSON


def test_record_signal_none_breakdown():
    """Test that score_breakdown=None stores NULL in DB without error."""
    signal = _make_signal(breakdown=None)
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch(
        "taiwan_stock_agent.infrastructure.signal_recorder.get_connection",
        return_value=mock_conn,
    ):
        signal_id = record_signal(signal, source="live")

    call_args = mock_cur.execute.call_args[0]
    assert call_args[1][9] is None  # score_breakdown should be None


def test_record_signal_extracts_scoring_version():
    """Test that record_signal extracts scoring_version from data_quality_flags."""
    signal = SignalOutput(
        ticker="2454",
        date=date(2026, 3, 24),
        action="WATCH",
        confidence=45,
        reasoning=Reasoning(momentum="neutral", chip_analysis="mixed"),
        execution_plan=ExecutionPlan(
            entry_bid_limit=100.0,
            entry_max_chase=105.0,
            stop_loss=95.0,
            target=115.0,
        ),
        data_quality_flags=["scoring_version:v1", "some_other_flag"],
        score_breakdown=None,
    )
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch(
        "taiwan_stock_agent.infrastructure.signal_recorder.get_connection",
        return_value=mock_conn,
    ):
        signal_id = record_signal(signal, source="live")

    call_args = mock_cur.execute.call_args[0]
    params = call_args[1]
    assert params[7] == "v1"  # scoring_version


def test_record_signal_default_scoring_version():
    """Test that scoring_version defaults to 'v2' if not found in data_quality_flags."""
    signal = SignalOutput(
        ticker="3008",
        date=date(2026, 3, 24),
        action="CAUTION",
        confidence=35,
        reasoning=Reasoning(momentum="weak", chip_analysis="negative"),
        execution_plan=ExecutionPlan(
            entry_bid_limit=50.0,
            entry_max_chase=52.0,
            stop_loss=48.0,
            target=60.0,
        ),
        data_quality_flags=[],  # no scoring_version flag
        score_breakdown=None,
    )
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch(
        "taiwan_stock_agent.infrastructure.signal_recorder.get_connection",
        return_value=mock_conn,
    ):
        signal_id = record_signal(signal, source="live")

    call_args = mock_cur.execute.call_args[0]
    params = call_args[1]
    assert params[7] == "v2"  # scoring_version defaults to v2


def test_record_signal_returns_uuid():
    """Test that record_signal returns a valid UUID string."""
    signal = _make_signal({"raw": {"rsi_14": 50.0}, "pts": {}})
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch(
        "taiwan_stock_agent.infrastructure.signal_recorder.get_connection",
        return_value=mock_conn,
    ):
        signal_id = record_signal(signal, source="live")

    # Check it's a non-empty string with UUID-like format
    assert isinstance(signal_id, str)
    assert len(signal_id) == 36  # standard UUID length with hyphens
    assert signal_id.count("-") == 4  # UUID has 4 hyphens
