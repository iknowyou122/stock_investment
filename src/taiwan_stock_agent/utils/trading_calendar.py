from datetime import date

# TWSE official market holidays (國定假日 + 補假).
# Excludes weekends (handled separately). Update annually.
# Source: https://www.twse.com.tw/zh/trading/holidaySchedule.html
_TW_HOLIDAYS: frozenset[date] = frozenset([
    # ── 2025 ────────────────────────────────────────────────────
    date(2025, 1, 1),   # 元旦
    date(2025, 1, 27),  # 農曆除夕補假
    date(2025, 1, 28),  # 農曆除夕
    date(2025, 1, 29),  # 春節初一
    date(2025, 1, 30),  # 春節初二
    date(2025, 1, 31),  # 春節初三
    date(2025, 2, 28),  # 和平紀念日
    date(2025, 4, 3),   # 兒童節補假
    date(2025, 4, 4),   # 兒童節/清明節
    date(2025, 5, 1),   # 勞動節
    date(2025, 5, 30),  # 端午節補假（5/31 週六）
    date(2025, 10, 10), # 國慶日
    # ── 2026 ────────────────────────────────────────────────────
    date(2026, 1, 1),   # 元旦
    date(2026, 1, 2),   # 元旦補假（1/1 週四，補休 1/2 週五）
    date(2026, 2, 16),  # 農曆除夕
    date(2026, 2, 17),  # 春節初一
    date(2026, 2, 18),  # 春節初二
    date(2026, 2, 19),  # 春節初三
    date(2026, 2, 20),  # 春節初四
    date(2026, 2, 27),  # 和平紀念日補假（2/28 週六）
    date(2026, 4, 3),   # 兒童節/清明節補假（4/4 週六）
    date(2026, 5, 1),   # 勞動節
    date(2026, 6, 19),  # 端午節
    date(2026, 9, 25),  # 中秋節
    date(2026, 10, 9),  # 國慶補假（10/10 週六）
])


def is_trading_day(d: date) -> bool:
    """Return True if d is a TWSE trading day (weekday and not a public holiday)."""
    return d.weekday() < 5 and d not in _TW_HOLIDAYS
