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

def test_rollback_restores_previous(tmp_path):
    params_file = tmp_path / "engine_params.json"
    history_file = tmp_path / "param_history.json"
    params_file.write_text(json.dumps({**_SAMPLE_PARAMS, "rsi_momentum_hi": 58}))
    history_file.write_text(json.dumps([{"timestamp": "2026-01-01", "changes": [{"param": "rsi_momentum_hi", "from": 55, "to": 58}]}]))
    reverted = rollback_params(params_path=params_file, history_path=history_file)
    assert reverted is not None
    updated = json.loads(params_file.read_text())
    assert updated["rsi_momentum_hi"] == 55
