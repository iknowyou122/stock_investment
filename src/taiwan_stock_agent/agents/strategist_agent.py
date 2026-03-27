"""StrategistAgent: aggregate OHLCV + ChipReport + VolumeProfile → SignalOutput.

This agent:
1. Fetches data for a ticker/date via FinMindClient
2. Runs TripleConfirmationEngine (deterministic)
3. Calls Claude API to generate natural language reasoning fields
4. Returns final SignalOutput
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta

from taiwan_stock_agent.domain.broker_label_classifier import BrokerLabelRepository
from taiwan_stock_agent.domain.models import (
    DailyOHLCV,
    Reasoning,
    SignalOutput,
    VolumeProfile,
)
from taiwan_stock_agent.domain.triple_confirmation_engine import (
    TripleConfirmationEngine,
    _ScoreBreakdown,
    _AnalysisHints,
)
from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher
from taiwan_stock_agent.agents.chip_detective_agent import ChipDetectiveAgent
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient

logger = logging.getLogger(__name__)

# Minimum OHLCV history required for Pillar 1+3 calculations
_MIN_HISTORY_SESSIONS = 20


class StrategistAgent:
    """Orchestrates data fetch → Triple Confirmation → LLM reasoning → SignalOutput.

    Usage::

        agent = StrategistAgent(finmind_client, label_repo)
        signal = agent.run(ticker="2330", date=date(2026, 3, 24))
    """

    def __init__(
        self,
        finmind: FinMindClient,
        label_repo: BrokerLabelRepository,
        anthropic_api_key: str | None = None,
        chip_proxy_fetcher: ChipProxyFetcher | None = None,
    ) -> None:
        self._finmind = finmind
        self._chip_detective = ChipDetectiveAgent(label_repo)
        self._chip_proxy_fetcher = chip_proxy_fetcher
        self._anthropic_api_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        # Engine is created per-run with the appropriate free_tier_mode
        # (kept as instance attr for backward compat but overridden in run())
        self._engine = TripleConfirmationEngine()

    def run(self, ticker: str, analysis_date: date) -> SignalOutput:
        """Run the full pipeline for one ticker on analysis_date.

        analysis_date is the T+1 settlement date (the date whose broker flows are
        being analyzed). This should be a day where FinMind data is available.
        """
        # --- Fetch OHLCV (last 25 sessions for 20-day calculations + buffer) ---
        ohlcv_start = analysis_date - timedelta(days=95)
        ohlcv_df = self._finmind.fetch_ohlcv(ticker, ohlcv_start, analysis_date)

        if ohlcv_df.empty:
            logger.warning("No OHLCV data for %s on %s", ticker, analysis_date)
            return self._halt_signal(ticker, analysis_date, "NO_OHLCV_DATA")

        history = self._df_to_ohlcv_list(ohlcv_df, ticker)
        today_rows = [h for h in history if h.trade_date == analysis_date]
        if not today_rows:
            logger.warning("analysis_date %s not in OHLCV for %s", analysis_date, ticker)
            return self._halt_signal(ticker, analysis_date, "DATE_NOT_IN_OHLCV")

        today_ohlcv = today_rows[0]

        if len(history) < _MIN_HISTORY_SESSIONS:
            today_ohlcv.data_quality_flags.append(
                f"INSUFFICIENT_HISTORY: {len(history)} sessions (need {_MIN_HISTORY_SESSIONS})"
            )

        # --- Fetch broker trades (last 5 trading days for 3-day net_buyer_count_diff) ---
        broker_start = analysis_date - timedelta(days=10)
        broker_df = self._finmind.fetch_broker_trades(ticker, broker_start, analysis_date)

        # --- Chip Detective ---
        chip_report = self._chip_detective.analyze(
            ticker=ticker,
            report_date=analysis_date,
            broker_trades_df=broker_df,
        )

        # --- TWSE chip proxy (free-tier fallback when FinMind paid unavailable) ---
        twse_proxy = None
        free_tier_mode = False
        if self._chip_proxy_fetcher is not None:
            twse_proxy = self._chip_proxy_fetcher.fetch(ticker, analysis_date)
            # Use free_tier_mode when paid chip data is unavailable
            # (chip_data_available is signalled by non-empty broker_df)
            free_tier_mode = broker_df.empty

        engine = TripleConfirmationEngine(free_tier_mode=free_tier_mode)

        # --- TAIEX history for RS vs 大盤 (Factor 6) ---
        taiex_df = self._finmind.fetch_taiex_history(analysis_date, lookback_days=35)
        taiex_history: list[DailyOHLCV] | None = None
        if not taiex_df.empty:
            taiex_history = self._df_to_ohlcv_list(taiex_df, "TAIEX")

        # --- Volume Profile proxy ---
        volume_profile = self._build_volume_profile(ticker, analysis_date, history)

        # --- Triple Confirmation (deterministic) ---
        signal, breakdown, hints = engine.score_full(
            ohlcv=today_ohlcv,
            ohlcv_history=history,
            chip_report=chip_report,
            volume_profile=volume_profile,
            twse_proxy=twse_proxy,
            taiex_history=taiex_history,
        )

        # --- LLM reasoning (Phase 3) ---
        if self._anthropic_api_key:
            reasoning = self._generate_reasoning(signal, chip_report, breakdown, hints)
            signal = signal.model_copy(update={"reasoning": reasoning})
        else:
            logger.info(
                "ANTHROPIC_API_KEY not set — skipping LLM reasoning for %s", ticker
            )

        return signal

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _df_to_ohlcv_list(df, ticker: str) -> list[DailyOHLCV]:
        result = []
        for _, row in df.iterrows():
            result.append(
                DailyOHLCV(
                    ticker=ticker,
                    trade_date=row["trade_date"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                )
            )
        return sorted(result, key=lambda x: x.trade_date)

    @staticmethod
    def _build_volume_profile(
        ticker: str, period_end: date, history: list[DailyOHLCV]
    ) -> VolumeProfile:
        """Build Phase 1-3 VolumeProfile proxy from 20-day OHLCV.

        poc_proxy = 20-day high (real POC requires intraday tick data, Phase 4+).
        target_price = poc_proxy * 1.05 (5% above 20-day high).
        """
        sorted_hist = sorted(history, key=lambda x: x.trade_date)
        recent_20 = sorted_hist[-20:]
        recent_60 = sorted_hist[-60:]
        if not recent_20:
            return VolumeProfile(
                ticker=ticker,
                period_end=period_end,
                poc_proxy=0.0,
                twenty_day_high=0.0,
                twenty_day_sessions=0,
                sixty_day_high=0.0,
                sixty_day_sessions=0,
                data_quality_flags=["NO_HISTORY"],
            )

        twenty_day_high = max(d.high for d in recent_20)
        sixty_day_high = max(d.high for d in recent_60) if recent_60 else 0.0
        flags = []
        if len(recent_20) < 20:
            flags.append(f"PARTIAL_PROFILE: only {len(recent_20)} sessions")

        return VolumeProfile(
            ticker=ticker,
            period_end=period_end,
            poc_proxy=twenty_day_high,
            twenty_day_high=twenty_day_high,
            twenty_day_sessions=len(recent_20),
            sixty_day_high=sixty_day_high,
            sixty_day_sessions=len(recent_60),
            data_quality_flags=flags,
        )

    def _generate_reasoning(
        self,
        signal: SignalOutput,
        chip_report,
        breakdown: _ScoreBreakdown,
        hints: _AnalysisHints | None = None,
    ) -> Reasoning:
        """Call Claude API to generate natural language reasoning fields."""
        try:
            import anthropic
        except ImportError:
            logger.warning("anthropic package not installed — skipping LLM reasoning")
            return Reasoning()

        client = anthropic.Anthropic(api_key=self._anthropic_api_key)

        hints_section = self._format_hints_for_prompt(hints) if hints else ""

        prompt = f"""你是一位台股交易分析師。根據以下量化數據，用繁體中文撰寫簡短的分析摘要。

股票代碼: {signal.ticker}
分析日期: {signal.date}
信心分數: {signal.confidence}/100
行動建議: {signal.action}

=== 評分細節 ===
動能指標:
  - VWAP 5日均: {'+20' if breakdown.vwap_5d_pts else '0'} 分
  - 量能突破: {'+20' if breakdown.volume_surge_pts else '0'} 分

籌碼指標:
  - 買賣家數差: {'+15' if breakdown.net_buyer_diff_pts else '0'} 分 (net_buyer_count_diff={chip_report.net_buyer_count_diff})
  - 集中度 Top15: {'+15' if breakdown.concentration_pts else '0'} 分 ({chip_report.concentration_top15:.1%})
  - 無隔日沖在前三: {'+10' if breakdown.no_daytrade_pts else '0'} 分
  - 外資買賣超 (TWSE): {'+15' if breakdown.twse_foreign_pts else '0'} 分
  - 融資餘額變化 (TWSE): {'+10' if breakdown.twse_margin_pts else '0'} 分

空間指標:
  - 接近/突破20日高點: {'+20' if breakdown.space_pts else '0'} 分
  - MA20趨勢向上: {'+5' if breakdown.ma20_slope_pts else '0'} 分

風險扣分:
  - 隔日沖扣分: {'-' + str(breakdown.daytrade_deduction) if breakdown.daytrade_deduction else '無'}

前三大買超券商:
{self._format_top3(chip_report.top_buyers[:3])}

風險標記: {', '.join(chip_report.risk_flags) if chip_report.risk_flags else '無'}

{hints_section}

=== 執行計畫 ===
進場區間: {signal.execution_plan.entry_bid_limit} - {signal.execution_plan.entry_max_chase}
停損參考: {signal.execution_plan.stop_loss} (T+0 收盤價，非盤中即時)
目標價: {signal.execution_plan.target}

請分別用1-2句話填寫以下欄位:
1. momentum (動能分析): 描述量價狀況
2. chip_analysis (籌碼分析): 描述籌碼集中度與主力行為
3. risk_factors (風險因素): 列出主要風險

回傳 JSON 格式，欄位: momentum, chip_analysis, risk_factors"""

        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIStatusError as e:
            logger.warning("Anthropic API error (skipping LLM reasoning): %s", e)
            return Reasoning()
        except anthropic.APIConnectionError as e:
            logger.warning("Anthropic connection error (skipping LLM reasoning): %s", e)
            return Reasoning()
        raw = message.content[0].text.strip()

        # Extract JSON from response
        try:
            # Try to find JSON block
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()
            data = json.loads(raw)
            return Reasoning(
                momentum=data.get("momentum", ""),
                chip_analysis=data.get("chip_analysis", ""),
                risk_factors=data.get("risk_factors", ""),
            )
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.warning("Failed to parse LLM reasoning JSON: %s", e)
            return Reasoning(momentum=raw, chip_analysis="", risk_factors="")

    @staticmethod
    def _format_hints_for_prompt(hints: _AnalysisHints | None) -> str:
        """Format analysis hints for LLM prompt context.

        Extensibility: when adding a new hint field to _AnalysisHints,
        add a corresponding line here.
        """
        if hints is None:
            return ""
        lines = ["=== 技術輔助指標 (僅供參考，不計入評分) ==="]
        if hints.rsi_14 is not None:
            status = "超買" if hints.rsi_14 > 70 else ("超賣" if hints.rsi_14 < 30 else "中性")
            lines.append(f"RSI(14): {hints.rsi_14:.1f} ({status})")
        if hints.macd_line is not None and hints.macd_signal is not None:
            cross = f" [{hints.macd_cross}交叉]" if hints.macd_cross else ""
            lines.append(f"MACD: 線={hints.macd_line:.4f} 訊號={hints.macd_signal:.4f}{cross}")
        if hints.ma20_slope_pct is not None:
            direction = "上升" if hints.ma20_slope_pct > 0 else "下降"
            lines.append(f"MA20趨勢: {direction} ({hints.ma20_slope_pct:+.2f}%/5日)")
        if hints.ma20_streak is not None and hints.ma20_streak != 0:
            direction = "站上" if hints.ma20_streak > 0 else "跌破"
            lines.append(f"MA20連續{direction}MA20: {abs(hints.ma20_streak)}日")
        if hints.gap_down_pct is not None and hints.gap_down_pct < -1.0:
            lines.append(f"跳空: {hints.gap_down_pct:+.2f}%")
        if hints.high52w_pct is not None:
            lines.append(f"距近期高點: {hints.high52w_pct:+.2f}%")
        return "\n".join(lines) if len(lines) > 1 else ""

    @staticmethod
    def _format_top3(top3) -> str:
        if not top3:
            return "  (無資料)"
        lines = []
        for b in top3:
            lines.append(
                f"  {b.branch_name} [{b.label}] 買{b.buy_volume:,} 賣{b.sell_volume:,} "
                f"(reversal={b.reversal_rate:.0%})"
            )
        return "\n".join(lines)

    @staticmethod
    def _halt_signal(ticker: str, analysis_date: date, reason: str) -> SignalOutput:
        from taiwan_stock_agent.domain.models import ExecutionPlan

        return SignalOutput(
            ticker=ticker,
            date=analysis_date,
            action="CAUTION",
            confidence=0,
            reasoning=Reasoning(risk_factors=f"Data unavailable: {reason}"),
            execution_plan=ExecutionPlan(
                entry_bid_limit=0.0,
                entry_max_chase=0.0,
                stop_loss=0.0,
                target=0.0,
            ),
            halt_flag=True,
            data_quality_flags=[reason],
        )
