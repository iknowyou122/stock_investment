"""Unit tests for StrategistAgent.

All tests use MagicMock for FinMindClient and an in-memory label repo.
No real network calls, no real DB.

Coverage:
  - Full pipeline returns LONG when all pillars pass
  - Halt on empty OHLCV (NO_OHLCV_DATA)
  - Halt when analysis_date not in OHLCV response
  - No LLM call when anthropic_api_key is absent
  - INSUFFICIENT_HISTORY data quality flag propagated
  - _build_volume_profile static method: POC = max(high) of last-20 entries
  - _build_volume_profile PARTIAL_PROFILE flag when < 20 sessions
  - _build_volume_profile NO_HISTORY when empty list
  - _halt_signal structure (confidence == 0, halt_flag, action, data_quality_flags)
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from taiwan_stock_agent.agents.strategist_agent import StrategistAgent
from taiwan_stock_agent.domain.models import BrokerLabel, DailyOHLCV, Reasoning, TWSEChipProxy
from taiwan_stock_agent.domain.triple_confirmation_engine import _AnalysisHints


# ---------------------------------------------------------------------------
# In-memory repo (mirrors conftest._InMemoryRepo — kept local so tests are
# self-contained and not coupled to fixture scope)
# ---------------------------------------------------------------------------

class _InMemoryRepo:
    def __init__(self):
        self._store: dict[str, BrokerLabel] = {}

    def get(self, code: str) -> BrokerLabel | None:
        return self._store.get(code)

    def upsert(self, label: BrokerLabel) -> None:
        self._store[label.branch_code] = label

    def list_all(self) -> list[BrokerLabel]:
        return list(self._store.values())


# ---------------------------------------------------------------------------
# DataFrame factories
# ---------------------------------------------------------------------------

_ANALYSIS_DATE = date(2025, 2, 5)
_BASE_DATE = date(2025, 1, 1)


def _make_mock_ohlcv_df(
    n: int = 25,
    base_date: date = _BASE_DATE,
    analysis_date: date = _ANALYSIS_DATE,
) -> pd.DataFrame:
    """Generate n rows of ascending OHLCV with the last row set to analysis_date.

    The last row (analysis_date) has volume 3x the 20-day average to ensure the
    volume-surge pillar fires. History rows use uniform 10,000 volume so the
    20-day MA is stable and the multiplier is predictable.
    """
    rows = []
    for i in range(n):
        d = base_date + timedelta(days=i)
        close = 100.0 + i * 0.5
        rows.append(
            {
                "trade_date": d,
                "ticker": "9999",
                "open": close - 1,
                "high": close + 1,
                "low": close - 2,
                "close": close,
                "volume": 10_000,  # uniform baseline so 20-day MA = 10,000
            }
        )
    # Last row: analysis_date with volume surge (3x avg) and close at 20d high
    last_close = 100.0 + (n - 1) * 0.5
    rows[-1].update(
        {
            "trade_date": analysis_date,
            "close": last_close,
            "high": last_close + 1,
            "volume": 30_000,  # 3x > 1.5x threshold
        }
    )
    return pd.DataFrame(rows)


def _make_mock_broker_df(analysis_date: date = _ANALYSIS_DATE) -> pd.DataFrame:
    """Three days of broker trades with net-positive buyer diff, no 隔日沖 in top-3."""
    rows = []
    for i in range(3):
        d = analysis_date - timedelta(days=i)
        for j, code in enumerate(["A001", "B002", "C003"]):
            rows.append(
                {
                    "trade_date": d,
                    "ticker": "9999",
                    "branch_code": code,
                    "branch_name": f"Branch{code}",
                    "buy_volume": (3 - j) * 10_000,
                    "sell_volume": 1_000,
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Helper: build agent with mock finmind
# ---------------------------------------------------------------------------

def _make_agent(
    ohlcv_df: pd.DataFrame | None = None,
    broker_df: pd.DataFrame | None = None,
    anthropic_api_key: str = "",
) -> tuple[StrategistAgent, MagicMock]:
    """Return (agent, mock_finmind) pair pre-configured with the given DataFrames."""
    if ohlcv_df is None:
        ohlcv_df = _make_mock_ohlcv_df()
    if broker_df is None:
        broker_df = _make_mock_broker_df()

    mock_finmind = MagicMock()
    mock_finmind.fetch_ohlcv.return_value = ohlcv_df
    mock_finmind.fetch_broker_trades.return_value = broker_df

    repo = _InMemoryRepo()
    agent = StrategistAgent(mock_finmind, repo, anthropic_api_key=anthropic_api_key)
    return agent, mock_finmind


# ===========================================================================
# TestStrategistPipeline
# ===========================================================================

class TestStrategistPipeline:
    def test_returns_long_signal_when_all_pillars_pass(self):
        """25 days ascending OHLCV + net-positive broker diff → LONG with confidence >= 70."""
        agent, _ = _make_agent()
        signal = agent.run("9999", _ANALYSIS_DATE)

        assert signal.halt_flag is False
        assert signal.action == "LONG"
        assert signal.confidence >= 70

    def test_returns_caution_when_empty_ohlcv(self):
        """Empty OHLCV DataFrame triggers halt with NO_OHLCV_DATA flag."""
        agent, _ = _make_agent(ohlcv_df=pd.DataFrame())
        signal = agent.run("9999", _ANALYSIS_DATE)

        assert signal.halt_flag is True
        assert signal.action == "CAUTION"
        assert "NO_OHLCV_DATA" in signal.data_quality_flags

    def test_returns_caution_when_date_not_in_ohlcv(self):
        """OHLCV that doesn't include analysis_date triggers halt."""
        # Build a df whose last row is NOT the analysis_date
        df = _make_mock_ohlcv_df(n=25, analysis_date=date(2025, 2, 4))  # date is 2025-02-04
        # Overwrite so none of the rows are on _ANALYSIS_DATE (2025-02-05)
        df["trade_date"] = [_BASE_DATE + timedelta(days=i) for i in range(len(df))]
        agent, _ = _make_agent(ohlcv_df=df)
        signal = agent.run("9999", _ANALYSIS_DATE)

        assert signal.halt_flag is True
        assert "DATE_NOT_IN_OHLCV" in signal.data_quality_flags

    def test_no_llm_when_api_key_absent(self):
        """With no API key, reasoning.momentum should be empty (LLM not called)."""
        agent, _ = _make_agent(anthropic_api_key="")
        signal = agent.run("9999", _ANALYSIS_DATE)

        # The signal should still be structurally valid
        assert signal.halt_flag is False
        # Reasoning fields must be empty because LLM was skipped
        assert signal.reasoning.momentum == ""

    def test_data_quality_flag_propagated_to_signal(self):
        """Only 5 OHLCV rows → INSUFFICIENT_HISTORY flag present in signal."""
        df = _make_mock_ohlcv_df(n=5, base_date=_ANALYSIS_DATE - timedelta(days=4))
        # Make the last row have analysis_date
        df.loc[df.index[-1], "trade_date"] = _ANALYSIS_DATE
        agent, _ = _make_agent(ohlcv_df=df)
        signal = agent.run("9999", _ANALYSIS_DATE)

        assert any("INSUFFICIENT_HISTORY" in f for f in signal.data_quality_flags)


# ===========================================================================
# TestStrategistVolumeProfile
# ===========================================================================

def _make_ohlcv_list(
    n: int,
    base_date: date = date(2025, 1, 1),
    base_high: float = 100.0,
) -> list[DailyOHLCV]:
    """Generate n DailyOHLCV entries with varying highs."""
    result = []
    for i in range(n):
        high = base_high + i * 0.5
        result.append(
            DailyOHLCV(
                ticker="9999",
                trade_date=base_date + timedelta(days=i),
                open=high - 2,
                high=high,
                low=high - 3,
                close=high - 1,
                volume=10_000,
            )
        )
    return result


class TestStrategistVolumeProfile:
    def test_build_volume_profile_uses_20d_high(self):
        """poc_proxy equals max(high) of the last 20 entries (not all 25)."""
        history = _make_ohlcv_list(25, base_high=100.0)
        # The 20-day window is entries [5..24]; max high is at index 24
        expected_high = max(e.high for e in history[-20:])
        # All-time max would include indices 0..4 as well, but we only want last 20
        period_end = history[-1].trade_date

        vp = StrategistAgent._build_volume_profile("9999", period_end, history)

        assert vp.poc_proxy == expected_high
        assert vp.twenty_day_high == expected_high
        assert vp.twenty_day_sessions == 20
        assert vp.data_quality_flags == []

    def test_build_volume_profile_partial_flag_when_fewer_than_20(self):
        """Only 10 entries → PARTIAL_PROFILE flag in data_quality_flags."""
        history = _make_ohlcv_list(10)
        period_end = history[-1].trade_date

        vp = StrategistAgent._build_volume_profile("9999", period_end, history)

        assert any("PARTIAL_PROFILE" in f for f in vp.data_quality_flags)
        assert vp.twenty_day_sessions == 10

    def test_build_volume_profile_no_history(self):
        """Empty history → poc_proxy == 0.0 and NO_HISTORY flag."""
        vp = StrategistAgent._build_volume_profile("9999", date(2025, 1, 31), [])

        assert vp.poc_proxy == 0.0
        assert "NO_HISTORY" in vp.data_quality_flags


# ===========================================================================
# TestStrategistHalt
# ===========================================================================

class TestStrategistHalt:
    def test_halt_signal_has_zero_confidence(self):
        """_halt_signal always returns confidence == 0 and halt_flag == True."""
        signal = StrategistAgent._halt_signal("2330", date(2025, 1, 31), "TEST_REASON")

        assert signal.confidence == 0
        assert signal.halt_flag is True
        assert signal.action == "CAUTION"

    def test_halt_signal_reason_in_data_quality_flags(self):
        """The reason string is recorded in data_quality_flags."""
        signal = StrategistAgent._halt_signal("2330", date(2025, 1, 31), "TEST_REASON")

        assert "TEST_REASON" in signal.data_quality_flags


# ===========================================================================
# TestStrategistChipProxyInjection
# ===========================================================================

class TestStrategistChipProxyInjection:
    def test_strategist_injects_chip_proxy_fetcher(self):
        """ChipProxyFetcher passed to agent → fetch() called with ticker + date."""
        mock_proxy_fetcher = MagicMock()
        mock_proxy_fetcher.fetch.return_value = TWSEChipProxy(
            ticker="9999",
            trade_date=_ANALYSIS_DATE,
            is_available=False,
        )

        mock_finmind = MagicMock()
        mock_finmind.fetch_ohlcv.return_value = _make_mock_ohlcv_df()
        mock_finmind.fetch_broker_trades.return_value = _make_mock_broker_df()

        repo = _InMemoryRepo()
        agent = StrategistAgent(
            mock_finmind, repo, chip_proxy_fetcher=mock_proxy_fetcher
        )
        agent.run("9999", _ANALYSIS_DATE)

        mock_proxy_fetcher.fetch.assert_called_once_with("9999", _ANALYSIS_DATE)

    def test_strategist_free_tier_propagates_to_output(self):
        """chip_proxy_fetcher set + empty broker_df → SignalOutput.free_tier_mode=True."""
        mock_proxy_fetcher = MagicMock()
        mock_proxy_fetcher.fetch.return_value = TWSEChipProxy(
            ticker="9999",
            trade_date=_ANALYSIS_DATE,
            is_available=True,
            foreign_net_buy=200_000,
        )

        mock_finmind = MagicMock()
        mock_finmind.fetch_ohlcv.return_value = _make_mock_ohlcv_df()
        # Empty broker_df with correct columns → free_tier_mode activates
        mock_finmind.fetch_broker_trades.return_value = pd.DataFrame(
            columns=["trade_date", "ticker", "branch_code", "branch_name", "buy_volume", "sell_volume"]
        )

        repo = _InMemoryRepo()
        agent = StrategistAgent(
            mock_finmind, repo, chip_proxy_fetcher=mock_proxy_fetcher
        )
        signal = agent.run("9999", _ANALYSIS_DATE)

        assert signal.free_tier_mode is True

    def test_strategist_hints_passed_to_llm(self):
        """_AnalysisHints from score_full() are forwarded to _generate_reasoning."""
        captured: list = []

        def _capture_hints(signal, chip_report, breakdown, hints=None):
            captured.append(hints)
            return Reasoning()

        with patch.object(StrategistAgent, "_generate_reasoning", side_effect=_capture_hints):
            agent, _ = _make_agent(anthropic_api_key="test-key")
            agent.run("9999", _ANALYSIS_DATE)

        assert len(captured) == 1, "_generate_reasoning should have been called once"
        hints = captured[0]
        assert hints is not None, "hints must not be None — score_full() should always return hints"
        assert isinstance(hints, _AnalysisHints)
