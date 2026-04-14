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
    volume-surge pillar fires. Baseline volume is set high enough that
    turnover (close × volume) clears the v2.2a liquidity gate (TSE 20M NT$).
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
                "volume": 300_000,  # turnover ≈ 30M NT$ > 20M liquidity gate
            }
        )
    # Last row: analysis_date with volume surge (3x avg) and close at 20d high
    last_close = 100.0 + (n - 1) * 0.5
    rows[-1].update(
        {
            "trade_date": analysis_date,
            "close": last_close,
            "high": last_close + 1,
            "volume": 900_000,  # 3x > 1.5x threshold
        }
    )
    return pd.DataFrame(rows)


def _make_mock_broker_df(analysis_date: date = _ANALYSIS_DATE) -> pd.DataFrame:
    """Three days of broker trades with net-positive buyer diff, no 隔日沖 in top-3.

    Uses 20 distinct branch codes so that active_branch_count >= 10 (avoids THIN_MARKET
    cap) and concentration_top15 is well above 35% (triggering +10 concentration pts).
    """
    rows = []
    # 20 branches with varied buy volumes — top-15 by volume will form concentration
    branch_codes = [f"B{i:03d}" for i in range(20)]
    for i in range(3):
        d = analysis_date - timedelta(days=i)
        for j, code in enumerate(branch_codes):
            buy_vol = max(1_000, (20 - j) * 2_000)  # top branches dominate
            rows.append(
                {
                    "trade_date": d,
                    "ticker": "9999",
                    "branch_code": code,
                    "branch_name": f"Branch{code}",
                    "buy_volume": buy_vol,
                    "sell_volume": 500,
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
        """25 days ascending OHLCV + net-positive broker diff → positive signal (LONG or WATCH).

        v2 note: the test data lacks TAIEX (neutral regime, threshold 68) and no
        institutional proxy data, so a WATCH is a valid positive outcome.  The test
        validates the happy-path pipeline runs end-to-end and returns a non-CAUTION
        signal with meaningful confidence.
        """
        agent, _ = _make_agent()
        signal = agent.run("9999", _ANALYSIS_DATE)

        assert signal.halt_flag is False
        assert signal.action in ("LONG", "WATCH")
        assert signal.confidence >= 45

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
    def test_build_volume_profile_poc_is_max_volume_close(self):
        """poc_proxy = close of highest-volume day in last 20 sessions (not the 20d high)."""
        history = _make_ohlcv_list(25, base_high=100.0)
        # Make one entry in the last-20 window have the highest volume
        peak_idx = 10  # within last 20 (window is indices 5..24)
        peak = history[peak_idx]
        history[peak_idx] = DailyOHLCV(
            ticker=peak.ticker,
            trade_date=peak.trade_date,
            open=peak.open,
            high=peak.high,
            low=peak.low,
            close=peak.close,
            volume=999_999,  # highest volume
        )

        expected_high = max(e.high for e in history[-20:])
        expected_poc = history[peak_idx].close  # highest-volume day's close
        period_end = history[-1].trade_date

        vp = StrategistAgent._build_volume_profile("9999", period_end, history)

        assert vp.poc_proxy == expected_poc
        assert vp.twenty_day_high == expected_high
        assert vp.twenty_day_sessions == 20
        assert vp.data_quality_flags == []

    def test_build_volume_profile_poc_window_is_last_20_not_all(self):
        """poc_proxy uses only the last 20 sessions, not the full history."""
        history = _make_ohlcv_list(25, base_high=100.0)
        # Make the highest-volume day be in the FIRST 5 entries (outside the 20-day window)
        history[2] = DailyOHLCV(
            ticker=history[2].ticker,
            trade_date=history[2].trade_date,
            open=history[2].open,
            high=history[2].high,
            low=history[2].low,
            close=history[2].close,
            volume=999_999,
        )
        period_end = history[-1].trade_date

        vp = StrategistAgent._build_volume_profile("9999", period_end, history)

        # poc_proxy should NOT use the high-volume entry at index 2 (outside last 20)
        assert vp.poc_proxy != history[2].close

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


# ===========================================================================
# TestApplyInstitutionalProxy
# ===========================================================================

def _make_proxy(flags: list[str], foreign_net_buy: int = 0) -> TWSEChipProxy:
    return TWSEChipProxy(
        ticker="9999",
        trade_date=_ANALYSIS_DATE,
        foreign_net_buy=foreign_net_buy,
        is_available=False,
        data_quality_flags=flags,
    )


def _make_ohlcv_seq(
    n: int,
    start_close: float,
    end_close: float,
    base_date: date = date(2025, 1, 20),
) -> list[DailyOHLCV]:
    """n rows linearly interpolated from start_close to end_close."""
    result = []
    for i in range(n):
        frac = i / (n - 1) if n > 1 else 0
        c = start_close + (end_close - start_close) * frac
        result.append(
            DailyOHLCV(
                ticker="9999",
                trade_date=base_date + timedelta(days=i),
                open=c - 1,
                high=c + 1,
                low=c - 2,
                close=c,
                volume=10_000,
            )
        )
    return result


class TestApplyInstitutionalProxy:
    def test_no_change_when_t86_succeeded(self):
        """Proxy unchanged when no T86 error flags present."""
        proxy = _make_proxy(flags=["OTHER_FLAG"])
        stock = _make_ohlcv_seq(10, 100, 110)
        taiex = _make_ohlcv_seq(10, 18000, 18000)

        result = StrategistAgent._apply_institutional_proxy(proxy, stock, taiex)

        assert result.foreign_net_buy == 0
        assert result.data_quality_flags == ["OTHER_FLAG"]

    def test_no_change_when_taiex_history_none(self):
        """T86 failed but no TAIEX data → proxy unchanged."""
        proxy = _make_proxy(flags=["TWSE_T86_ERROR:timeout"])

        stock = _make_ohlcv_seq(10, 100, 103)
        result = StrategistAgent._apply_institutional_proxy(proxy, stock, None)

        assert result.foreign_net_buy == 0

    def test_no_change_when_insufficient_history(self):
        """Only 3 sessions of stock or TAIEX → proxy unchanged (need >= 5)."""
        proxy = _make_proxy(flags=["TWSE_T86_NO_DATA:empty"])
        stock = _make_ohlcv_seq(3, 100, 103)
        taiex = _make_ohlcv_seq(3, 18000, 18054)

        result = StrategistAgent._apply_institutional_proxy(proxy, stock, taiex)

        assert result.foreign_net_buy == 0

    def test_proxy_flag_added_even_below_threshold(self):
        """RS computed but < 3% → no buy signal injected, but flag still added."""
        proxy = _make_proxy(flags=["TWSE_T86_ERROR:blocked"])
        # Stock +1%, TAIEX +0.5% → RS = +0.5%, below 3% threshold
        stock = _make_ohlcv_seq(5, 100, 101.0)
        taiex = _make_ohlcv_seq(5, 18000, 18090)

        result = StrategistAgent._apply_institutional_proxy(proxy, stock, taiex)

        assert result.foreign_net_buy == 0
        assert any("TWSE_T86_PROXY:RS=" in f for f in result.data_quality_flags)

    def test_injects_dealer_only_when_rs_between_1p5_and_3pct(self):
        """RS = +2% (≥1.5% but <3%) → dealer_net_buy=1 only (+5 pts)."""
        proxy = _make_proxy(flags=["TWSE_T86_ERROR:blocked"])
        # Stock +2.5%, TAIEX flat → RS = +2.5%
        stock = _make_ohlcv_seq(5, 100, 102.5)
        taiex = _make_ohlcv_seq(5, 18000, 18000)

        result = StrategistAgent._apply_institutional_proxy(proxy, stock, taiex)

        assert result.dealer_net_buy == 1
        assert result.foreign_net_buy == 0
        assert result.trust_net_buy == 0

    def test_injects_foreign_buy_when_rs_above_3pct(self):
        """RS = +5% (≥3% but <6%) → foreign_net_buy=1, dealer_net_buy=1."""
        proxy = _make_proxy(flags=["TWSE_T86_ERROR:blocked"])
        stock = _make_ohlcv_seq(5, 100, 105.0)
        taiex = _make_ohlcv_seq(5, 18000, 18000)

        result = StrategistAgent._apply_institutional_proxy(proxy, stock, taiex)

        assert result.foreign_net_buy == 1
        assert result.dealer_net_buy == 1   # also set at ≥1.5% tier
        assert result.trust_net_buy == 0    # RS < 6%, trust not set
        assert any("TWSE_T86_PROXY:RS=+5.0%" in f for f in result.data_quality_flags)

    def test_injects_trust_buy_when_rs_above_6pct(self):
        """RS = +8% (≥6% but <9%) → foreign + trust + dealer set."""
        proxy = _make_proxy(flags=["TWSE_T86_ERROR:blocked"])
        stock = _make_ohlcv_seq(5, 100, 108.0)
        taiex = _make_ohlcv_seq(5, 18000, 18000)

        result = StrategistAgent._apply_institutional_proxy(proxy, stock, taiex)

        assert result.foreign_net_buy == 1
        assert result.trust_net_buy == 1
        assert result.dealer_net_buy == 1

    def test_all_three_set_when_rs_above_9pct(self):
        """RS = +10% (≥9%) → all three institutions set → 三大法人同向 bonus fires."""
        proxy = _make_proxy(flags=["TWSE_T86_ERROR:blocked"])
        stock = _make_ohlcv_seq(5, 100, 110.0)
        taiex = _make_ohlcv_seq(5, 18000, 18000)

        result = StrategistAgent._apply_institutional_proxy(proxy, stock, taiex)

        assert result.foreign_net_buy == 1
        assert result.trust_net_buy == 1
        assert result.dealer_net_buy == 1

    def test_uses_twse_t86_no_data_flag_variant(self):
        """TWSE_T86_NO_DATA flag (not just TWSE_T86_ERROR) also triggers proxy."""
        proxy = _make_proxy(flags=["TWSE_T86_NO_DATA:empty_response"])
        stock = _make_ohlcv_seq(5, 100, 105.0)
        taiex = _make_ohlcv_seq(5, 18000, 18000)

        result = StrategistAgent._apply_institutional_proxy(proxy, stock, taiex)

        assert result.foreign_net_buy == 1

    def test_original_proxy_data_preserved(self):
        """Model fields NOT related to T86 (e.g. margin_balance_change) are preserved."""
        proxy = TWSEChipProxy(
            ticker="9999",
            trade_date=_ANALYSIS_DATE,
            is_available=False,
            margin_balance_change=-5000,
            data_quality_flags=["TWSE_T86_ERROR:blocked"],
        )
        stock = _make_ohlcv_seq(5, 100, 105.0)
        taiex = _make_ohlcv_seq(5, 18000, 18000)

        result = StrategistAgent._apply_institutional_proxy(proxy, stock, taiex)

        assert result.margin_balance_change == -5000
        assert result.foreign_net_buy == 1


# ===========================================================================
# TestScoreBreakdown
# ===========================================================================

def test_run_populates_score_breakdown():
    """StrategistAgent.run() must populate score_breakdown with raw + pts + flags."""
    agent, _ = _make_agent()
    signal = agent.run("9999", _ANALYSIS_DATE)
    assert signal.score_breakdown is not None
    assert "raw" in signal.score_breakdown
    assert "pts" in signal.score_breakdown
    assert "flags" in signal.score_breakdown
    assert "taiex_slope" in signal.score_breakdown
    # raw must contain the keys needed by scoring_replay
    raw = signal.score_breakdown["raw"]
    assert "rsi_14" in raw
    assert "volume_vs_20ma" in raw
