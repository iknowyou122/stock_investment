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
