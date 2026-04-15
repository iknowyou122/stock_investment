from datetime import date
from taiwan_stock_agent.utils.trading_calendar import is_trading_day

def test_weekday_is_trading_day():
    assert is_trading_day(date(2026, 4, 14)) is True   # Monday

def test_saturday_is_not_trading_day():
    assert is_trading_day(date(2026, 4, 18)) is False  # Saturday

def test_sunday_is_not_trading_day():
    assert is_trading_day(date(2026, 4, 19)) is False  # Sunday

def test_friday_is_trading_day():
    assert is_trading_day(date(2026, 4, 17)) is True   # Friday


import json
from pathlib import Path

def test_engine_params_has_tunable_whitelist():
    params_path = Path(__file__).parents[2] / "config" / "engine_params.json"
    params = json.loads(params_path.read_text())
    assert "tunable_whitelist" in params, "engine_params.json must have tunable_whitelist"
    whitelist = params["tunable_whitelist"]
    assert isinstance(whitelist, list)
    assert len(whitelist) >= 5
    for key in whitelist:
        assert key in params, f"whitelist key '{key}' not in params"
        assert isinstance(params[key], (int, float))


from taiwan_stock_agent.utils.param_safety import validate_changes, apply_changes, rollback_params
import tempfile, os

_SAMPLE_PARAMS = {
    "gate_vol_ratio": 1.2,
    "rsi_momentum_hi": 55,
    "watch_min": 40,
    "tunable_whitelist": ["gate_vol_ratio", "rsi_momentum_hi", "watch_min"],
}

def test_validate_changes_rejects_non_whitelisted():
    changes = [{"param": "rsi_momentum_lo", "from": 30, "to": 25}]
    ok, errors = validate_changes(changes, _SAMPLE_PARAMS)
    assert not ok
    assert any("whitelist" in e for e in errors)

def test_validate_changes_rejects_large_delta():
    changes = [{"param": "rsi_momentum_hi", "from": 55, "to": 70}]
    ok, errors = validate_changes(changes, _SAMPLE_PARAMS)
    assert not ok
    assert any("20%" in e for e in errors)

def test_validate_changes_accepts_small_delta():
    changes = [{"param": "rsi_momentum_hi", "from": 55, "to": 58}]
    ok, errors = validate_changes(changes, _SAMPLE_PARAMS)
    assert ok
    assert errors == []

def test_apply_changes_writes_history(tmp_path):
    params_file = tmp_path / "engine_params.json"
    history_file = tmp_path / "param_history.json"
    params_file.write_text(json.dumps(_SAMPLE_PARAMS))
    history_file.write_text("[]")
    changes = [{"param": "rsi_momentum_hi", "from": 55, "to": 58, "reason": "test"}]
    apply_changes(changes, params_path=params_file, history_path=history_file)
    updated = json.loads(params_file.read_text())
    assert updated["rsi_momentum_hi"] == 58
    history = json.loads(history_file.read_text())
    assert len(history) == 1
    assert history[0]["changes"] == changes

from taiwan_stock_agent.utils.bot_formatters import (
    format_opening_list, format_entry_signal, format_postmarket_report,
)

_SAMPLE_SIGNALS = [
    {"ticker": "6933", "name": "信驊", "action": "WATCH", "confidence": 59,
     "entry_bid": 320.0, "target": 358.0, "stop_loss": 312.0, "flags": "COILING_PRIME"},
    {"ticker": "3704", "name": "合一", "action": "WATCH", "confidence": 57,
     "entry_bid": 85.0, "target": 95.0, "stop_loss": 82.0, "flags": "EMERGING_SETUP"},
]

def test_format_opening_list_contains_ticker():
    msg = format_opening_list(_SAMPLE_SIGNALS, scan_date="2026-04-15")
    assert "6933" in msg and "信驊" in msg

def test_format_opening_list_shows_coiling():
    msg = format_opening_list(_SAMPLE_SIGNALS, scan_date="2026-04-15")
    assert "蓄積" in msg or "COILING" in msg

def test_format_opening_list_empty():
    msg = format_opening_list([], scan_date="2026-04-15")
    assert "0" in msg or "無" in msg

def test_format_entry_signal():
    msg = format_entry_signal("6933", "信驊", price=322.0, entry_low=318.0, entry_high=328.0, stop=312.0)
    assert "6933" in msg and "322" in msg and "✅" in msg

def test_format_postmarket_hit_rate():
    hits = [{"ticker": "6933", "triggered": True, "price": 322.0}]
    msg = format_postmarket_report(_SAMPLE_SIGNALS, hits, _SAMPLE_SIGNALS, "2026-04-15")
    assert "命中率" in msg

def test_rollback_restores_previous(tmp_path):
    params_file = tmp_path / "engine_params.json"
    history_file = tmp_path / "param_history.json"
    params_file.write_text(json.dumps({**_SAMPLE_PARAMS, "rsi_momentum_hi": 58}))
    history_file.write_text(json.dumps([{"timestamp": "2026-01-01", "changes": [{"param": "rsi_momentum_hi", "from": 55, "to": 58}]}]))
    reverted = rollback_params(params_path=params_file, history_path=history_file)
    assert reverted is not None
    updated = json.loads(params_file.read_text())
    assert updated["rsi_momentum_hi"] == 55
