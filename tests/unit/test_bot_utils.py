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
