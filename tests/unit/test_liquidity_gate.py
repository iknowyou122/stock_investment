"""Regression test for the liquidity bypass bug found via 1817 凱撒衛.

Background
==========
TCE Gate-3 rejects tickers with 20-day average dollar volume below NT$ 8M
(see triple_confirmation_engine.py:147). The bug was that
`_scan_pullback_batch` and `_scan_early_accum_batch` did NOT enforce the
same floor — a low-liquidity ticker like 1817 (avg ~3.1M NT$) was halted
by TCE (conf=0) but then re-promoted by PullbackDetector with conf=98+
and an action=LONG label, which `_merge_unified_signals` then overwrote
the TCE result with. This made it look like a valid "buy" signal in the
Tier panel even though TCE had explicitly disqualified it.

This regression test pins the fix:
  * _passes_liquidity_floor returns True only when 20-day avg dollar
    volume >= NT$ 8,000,000.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("batch_plan", _SCRIPTS / "batch_plan.py")
batch_plan = importlib.util.module_from_spec(spec)
# Avoid heavy module-level side-effects during import (the script wires up
# logging + dotenv when imported as __main__, but as a regular module these
# stay safe).
sys.modules["batch_plan"] = batch_plan
spec.loader.exec_module(batch_plan)


@dataclass
class _Bar:
    trade_date: date
    close: float
    volume: float


def _bars(close: float, volume: float, n: int = 20) -> list[_Bar]:
    start = date(2026, 5, 1)
    return [_Bar(start + timedelta(days=i), close, volume) for i in range(n)]


class TestLiquidityFloor:
    def test_low_liquidity_ticker_rejected(self) -> None:
        # 1817-like: close ~39, volume ~78k → ~3.1M/day → below 8M floor
        history = _bars(close=39.5, volume=78_226, n=25)
        assert batch_plan._passes_liquidity_floor(history) is False

    def test_high_liquidity_ticker_passes(self) -> None:
        # 2330-like: close ~2400, volume ~30M → ~72B/day → far above 8M
        history = _bars(close=2400.0, volume=30_000_000, n=25)
        assert batch_plan._passes_liquidity_floor(history) is True

    def test_exactly_at_floor_passes(self) -> None:
        # 8M / 25 close = 320,000 volume → exactly hits the 8M floor
        history = _bars(close=25.0, volume=320_000, n=25)
        assert batch_plan._passes_liquidity_floor(history) is True

    def test_just_below_floor_rejected(self) -> None:
        history = _bars(close=25.0, volume=300_000, n=25)
        assert batch_plan._passes_liquidity_floor(history) is False

    def test_history_shorter_than_20_bars_rejected(self) -> None:
        # Even a "rich" ticker is rejected if we cannot compute a stable
        # 20-day window — better to skip than risk noisy signals.
        history = _bars(close=2400.0, volume=30_000_000, n=15)
        assert batch_plan._passes_liquidity_floor(history) is False

    def test_uses_last_20_days_only(self) -> None:
        # Earlier 10 bars are dirt-poor, last 20 bars are rich.
        # Should pass because the floor uses only the last 20.
        start = date(2026, 4, 1)
        poor = [_Bar(start + timedelta(days=i), 39.5, 78_000) for i in range(10)]
        rich = [_Bar(start + timedelta(days=i + 10), 2400.0, 30_000_000) for i in range(20)]
        history = poor + rich
        assert batch_plan._passes_liquidity_floor(history) is True

    def test_floor_constant_matches_tce_g3(self) -> None:
        # If anyone ever loosens this constant, that change should be visible
        # in this test so the reviewer can decide whether to also loosen TCE G3.
        assert batch_plan._LIQUIDITY_FLOOR_TWD == 8_000_000.0
