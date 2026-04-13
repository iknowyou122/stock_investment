"""StrategistAgent: aggregate OHLCV + ChipReport + VolumeProfile → SignalOutput.

This agent:
1. Fetches data for a ticker/date via FinMindClient
2. Runs TripleConfirmationEngine (deterministic)
3. Calls Claude API to generate natural language reasoning fields
4. Returns final SignalOutput
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from datetime import date, timedelta

from taiwan_stock_agent.domain.broker_label_classifier import BrokerLabelRepository
from taiwan_stock_agent.domain.llm_provider import LLMProvider, AnthropicProvider, create_llm_provider
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

# Sentinel — pass as llm_provider to explicitly disable LLM (vs. None which triggers auto-detect)
_LLM_DISABLED = object()


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
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self._finmind = finmind
        self._chip_detective = ChipDetectiveAgent(label_repo)
        self._chip_proxy_fetcher = chip_proxy_fetcher

        # LLM provider resolution order:
        #   1. explicit llm_provider=_LLM_DISABLED  → disabled (no LLM calls)
        #   2. explicit llm_provider=<instance>     → use that provider
        #   3. anthropic_api_key= argument (backward compat)
        #   4. auto-detect from env (LLM_PROVIDER / ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY)
        if llm_provider is _LLM_DISABLED:
            self._llm_provider: LLMProvider | None = None
        elif llm_provider is not None:
            self._llm_provider = llm_provider
        elif anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", ""):
            key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
            self._llm_provider = AnthropicProvider(key)
        else:
            self._llm_provider = create_llm_provider()

        # Engine is created per-run with the appropriate free_tier_mode
        # (kept as instance attr for backward compat but overridden in run())
        self._engine = TripleConfirmationEngine()
        # TAIEX history cache: one fetch per date serves all tickers on that date.
        # {analysis_date: list[DailyOHLCV] | None}
        self._taiex_cache: dict[date, list[DailyOHLCV] | None] = {}

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

        # --- TAIEX history for RS vs 大盤 (Factor 6) — cached per date ---
        if analysis_date in self._taiex_cache:
            taiex_history = self._taiex_cache[analysis_date]
        else:
            taiex_df = self._finmind.fetch_taiex_history(analysis_date, lookback_days=35)
            taiex_history: list[DailyOHLCV] | None = None
            if not taiex_df.empty:
                taiex_history = self._df_to_ohlcv_list(taiex_df, "TAIEX")
            self._taiex_cache[analysis_date] = taiex_history

        # --- Enrich TWSEChipProxy with avg_20d_volume from OHLCV ---
        # avg_20d_volume is required for ratio-based institution strength scoring
        # (foreign/trust/dealer net_buy / avg_vol → tiered 0/4/8/12 pts).
        # Without it, scoring falls back to binary mode (bought = 4 pts, regardless of size).
        if twse_proxy is not None and history:
            recent_20_vols = sorted(history, key=lambda x: x.trade_date)[-20:]
            if recent_20_vols:
                avg_vol = int(sum(d.volume for d in recent_20_vols) / len(recent_20_vols))
                twse_proxy = twse_proxy.model_copy(update={"avg_20d_volume": avg_vol})

        # --- OHLCV institutional proxy (Option B: fills Factor 3 when T86 is blocked) ---
        if twse_proxy is not None:
            twse_proxy = self._apply_institutional_proxy(twse_proxy, history, taiex_history)

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
        if self._llm_provider is not None:
            reasoning = self._generate_reasoning(signal, chip_report, breakdown, hints)
            signal = signal.model_copy(update={"reasoning": reasoning})
        else:
            logger.info(
                "No LLM provider configured — skipping reasoning for %s "
                "(set ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY or LLM_PROVIDER)",
                ticker,
            )

        # --- Build score_breakdown for factor replay and DB storage ---
        pts_dict = {
            k: v for k, v in dataclasses.asdict(breakdown).items()
            if k != "flags"
        }
        # Compute volume_vs_20ma for replay (breakout_vol_ratio grid search)
        avg_vol = twse_proxy.avg_20d_volume if twse_proxy else 0
        volume_vs_20ma = (
            round(today_ohlcv.volume / avg_vol, 4) if avg_vol > 0 else None
        )
        # Determine taiex_slope label for threshold replay
        taiex_slope = engine._compute_taiex_regime(taiex_history) if taiex_history else "neutral"

        breakdown_dict = {
            "raw": {
                "rsi_14": hints.rsi_14,
                "volume_vs_20ma": volume_vs_20ma,
                "ma20_slope_pct": hints.ma20_slope_pct,
            },
            "pts": pts_dict,
            "flags": list(breakdown.flags),
            "taiex_slope": taiex_slope,
            "scoring_version": breakdown.scoring_version,
        }
        signal = signal.model_copy(update={"score_breakdown": breakdown_dict})
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

        poc_proxy = highest-volume day's close in last 20 sessions.
        This is a better approximation of where most volume traded (real POC concept)
        compared to using the 20-day high. Real POC requires intraday tick data (Phase 4+).
        target_price = poc_proxy * 1.05.
        """
        sorted_hist = sorted(history, key=lambda x: x.trade_date)
        recent_20 = sorted_hist[-20:]
        recent_60 = sorted_hist[-60:]
        recent_120 = sorted_hist[-120:]
        recent_252 = sorted_hist[-252:]

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
        one_twenty_day_high = max(d.high for d in recent_120) if recent_120 else 0.0
        fiftytwo_week_high = max(d.high for d in recent_252) if recent_252 else 0.0

        # POC proxy: close price of the highest-volume day in last 20 sessions.
        # Rationale: the day with the most volume is where the most conviction existed;
        # that price level tends to act as support/resistance.
        # Panic filter: exclude days where close dropped >5% vs open (large red candles);
        # those days have high volume but artificially depressed closes that would set
        # poc_proxy far below current price and produce target < entry.
        # Falls back to twenty_day_high if no valid candidate or all volumes are 0.
        valid_days = [d for d in recent_20 if d.open <= 0 or (d.close / d.open - 1) > -0.05]
        candidates = valid_days if valid_days else recent_20
        max_vol_day = max(candidates, key=lambda d: d.volume)
        poc_proxy = max_vol_day.close if max_vol_day.volume > 0 else twenty_day_high

        flags = []
        if len(recent_20) < 20:
            flags.append(f"PARTIAL_PROFILE: only {len(recent_20)} sessions")

        return VolumeProfile(
            ticker=ticker,
            period_end=period_end,
            poc_proxy=poc_proxy,
            twenty_day_high=twenty_day_high,
            twenty_day_sessions=len(recent_20),
            sixty_day_high=sixty_day_high,
            sixty_day_sessions=len(recent_60),
            one_twenty_day_high=one_twenty_day_high,
            one_twenty_day_sessions=len(recent_120),
            fiftytwo_week_high=fiftytwo_week_high,
            fiftytwo_week_sessions=len(recent_252),
            data_quality_flags=flags,
        )

    @staticmethod
    def _apply_institutional_proxy(
        proxy: "TWSEChipProxy",
        stock_history: list[DailyOHLCV],
        taiex_history: list[DailyOHLCV] | None,
    ) -> "TWSEChipProxy":
        """Fill in Factor 3 (外資買賣超) proxy via OHLCV RS when T86 is blocked.

        When TWSE T86 returns a security challenge (TWSE_T86_ERROR / TWSE_T86_NO_DATA),
        estimate institutional direction from the stock's 5-day relative strength vs TAIEX:
          - RS ≥ 3%: inject foreign_net_buy = 1  (+15 pts in engine)
          - RS ≥ 6%: also inject trust_net_buy = 1
        Flags: adds TWSE_T86_PROXY:RS=+X.X% to data_quality_flags.

        If T86 succeeded, or insufficient history exists, returns proxy unchanged.
        """
        t86_failed = any(
            "TWSE_T86_ERROR" in f or "TWSE_T86_NO_DATA" in f
            for f in proxy.data_quality_flags
        )
        if not t86_failed:
            return proxy

        if taiex_history is None or len(taiex_history) < 5 or len(stock_history) < 5:
            return proxy

        stock_5d = sorted(stock_history, key=lambda x: x.trade_date)[-5:]
        taiex_5d = sorted(taiex_history, key=lambda x: x.trade_date)[-5:]

        if stock_5d[0].close <= 0 or taiex_5d[0].close <= 0:
            return proxy

        stock_return = (stock_5d[-1].close - stock_5d[0].close) / stock_5d[0].close
        taiex_return = (taiex_5d[-1].close - taiex_5d[0].close) / taiex_5d[0].close
        rs = stock_return - taiex_return

        flags = proxy.data_quality_flags + [f"TWSE_T86_PROXY:RS={rs:+.1%}"]

        # Stepped scoring — each tier adds one more institution signal:
        #   RS ≥ 9%: foreign + trust + dealer → +35 pts (incl. 三大法人同向 bonus)
        #   RS ≥ 6%: foreign + trust          → +25 pts
        #   RS ≥ 3%: foreign only             → +15 pts
        #   RS ≥ 1.5%: dealer only            → +5 pts  (weak outperformance)
        #   RS < 1.5%: no signal injected
        updates: dict = {"data_quality_flags": flags}
        if rs >= 0.015:
            updates["dealer_net_buy"] = 1
        if rs >= 0.03:
            updates["foreign_net_buy"] = 1
        if rs >= 0.06:
            updates["trust_net_buy"] = 1
        if rs >= 0.09:
            # dealer already set at 0.015; trust + foreign already set — all three present
            pass  # 三大法人同向 bonus fires automatically in engine

        if len(updates) > 1:  # something was injected beyond just flags
            return proxy.model_copy(update=updates)

        return proxy.model_copy(update={"data_quality_flags": flags})

    def _generate_reasoning(
        self,
        signal: SignalOutput,
        chip_report,
        breakdown: _ScoreBreakdown,
        hints: _AnalysisHints | None = None,
    ) -> Reasoning:
        """Call configured LLM provider to generate natural language reasoning fields."""
        if self._llm_provider is None:
            return Reasoning()

        hints_section = self._format_hints_for_prompt(hints) if hints else ""

        prompt = f"""你是一位台股交易分析師。根據以下量化數據，用繁體中文撰寫簡短的分析摘要。

股票代碼: {signal.ticker}
分析日期: {signal.date}
信心分數: {signal.confidence}/100
行動建議: {signal.action}

=== 評分細節 (v2) ===
動能指標 (Pillar 1):
  - 量能比率: {breakdown.volume_ratio_pts} 分
  - VWAP 5日優勢: {breakdown.vwap_advantage_pts} 分
  - 收盤方向: {breakdown.price_direction_pts} 分
  - 收盤強度: {breakdown.close_strength_pts} 分
  - 趨勢延續: {breakdown.trend_continuity_pts} 分
  - 量能遞增: {breakdown.volume_escalation_pts} 分
  - RSI動能區間: {breakdown.rsi_momentum_pts} 分

籌碼指標 (Pillar 2):
  - 付費版 - 買盤廣度: {breakdown.breadth_pts} 分
  - 付費版 - 集中度: {breakdown.concentration_pts} 分
  - 付費版 - 隔日沖過濾: {breakdown.daytrade_filter_pts} 分 (net_buyer_count_diff={chip_report.net_buyer_count_diff})
  - 免費版 - 外資強度: {breakdown.foreign_strength_pts} 分
  - 免費版 - 融資結構: {breakdown.margin_structure_pts} 分
  - 免費版 - 借券壓力: {breakdown.sbl_pressure_pts} 分

空間指標 (Pillar 3):
  - 突破20日高點: {breakdown.breakout_20d_pts} 分
  - 突破60日高點: {breakdown.breakout_60d_pts} 分
  - 突破量能確認: {breakdown.breakout_volume_pts} 分
  - MA多頭排列: {breakdown.ma_alignment_pts} 分
  - MA20斜率: {breakdown.ma20_slope_pts} 分
  - 相對強弱: {breakdown.relative_strength_pts} 分

風險扣分:
  - 隔日沖: {'-' + str(breakdown.daytrade_risk) if breakdown.daytrade_risk else '無'}
  - 長上影: {'-' + str(breakdown.long_upper_shadow) if breakdown.long_upper_shadow else '無'}
  - 過熱乖離: {'-' + str(breakdown.overheat_ma20 + breakdown.overheat_ma60) if (breakdown.overheat_ma20 or breakdown.overheat_ma60) else '無'}
  - 融資追價: {'-' + str(breakdown.margin_chase_heat) if breakdown.margin_chase_heat else '無'}

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
            raw = self._llm_provider.complete(prompt, max_tokens=2000)
        except RuntimeError as e:
            logger.warning(
                "LLM reasoning failed [%s] (skipping): %s",
                getattr(self._llm_provider, "name", "unknown"),
                e,
            )
            return Reasoning()

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
