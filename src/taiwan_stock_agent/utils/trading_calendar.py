from datetime import date


def is_trading_day(d: date) -> bool:
    """Return True if d is a Taiwan Stock Exchange trading day (Mon–Fri).

    Note: Does not handle TWSE irregular holidays (typhoon closures etc.).
    Consistent with existing project behaviour in backtest.py and daily_runner.py.
    """
    return d.weekday() < 5
