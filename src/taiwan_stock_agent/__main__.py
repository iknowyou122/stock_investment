"""CLI entry point: python -m taiwan_stock_agent --date YYYY-MM-DD [--tickers 2330 2317 ...]

Run window: after 20:00 Taiwan time (UTC+8) on TWSE trading days.
FinMind T+1 分點 data is typically published by 18:00-20:00 after TSE market close (13:30).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_WATCHLIST = ["2330", "2317", "2454", "2382", "3008"]


def _patch_agent_for_demo(agent: object, analysis_date: "date") -> None:
    """Monkey-patch ChipDetectiveAgent and ChipProxyFetcher with realistic mock data.

    Scenario: 波段贏家 accumulation — strong chip concentration, net buyers rising,
    foreign net buy positive, margin decreasing (healthy).
    OHLCV stays real; only chip sources are replaced.
    """
    from datetime import date as _date
    from taiwan_stock_agent.domain.models import (
        BrokerWithLabel, ChipReport, TWSEChipProxy,
    )

    def _mock_chip_report(ticker: str, report_date: _date, broker_trades_df=None) -> ChipReport:
        return ChipReport(
            ticker=ticker,
            report_date=report_date,
            top_buyers=[
                BrokerWithLabel(branch_code="9A00", branch_name="元大-竹北",
                                label="波段贏家", reversal_rate=0.21,
                                buy_volume=1_250_000, sell_volume=80_000),
                BrokerWithLabel(branch_code="6460", branch_name="國泰-新竹",
                                label="地緣券商", reversal_rate=0.38,
                                buy_volume=620_000, sell_volume=50_000),
                BrokerWithLabel(branch_code="1480", branch_name="摩根大通",
                                label="代操官股", reversal_rate=0.18,
                                buy_volume=480_000, sell_volume=120_000),
            ],
            concentration_top15=0.52,
            net_buyer_count_diff=12,
            risk_flags=[],
            active_branch_count=48,
        )

    def _mock_twse_proxy(ticker: str, trade_date: _date) -> TWSEChipProxy:
        return TWSEChipProxy(
            ticker=ticker,
            trade_date=trade_date,
            foreign_net_buy=8_500_000,
            trust_net_buy=1_200_000,
            dealer_net_buy=-200_000,
            margin_balance_change=-320_000,
            foreign_consecutive_buy_days=3,
            short_balance_increased=False,
            short_margin_ratio=0.04,
            is_available=True,
        )

    # Patch ChipDetectiveAgent.analyze
    agent._chip_detective.analyze = _mock_chip_report  # type: ignore[attr-defined]
    # Patch ChipProxyFetcher.fetch
    agent._chip_proxy_fetcher.fetch = _mock_twse_proxy  # type: ignore[attr-defined]
    logger.info(
        "DEMO MODE: chip data replaced with synthetic 波段贏家 accumulation scenario"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Taiwan Stock AI Agent — post-market Triple Confirmation signal engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m taiwan_stock_agent --date 2026-03-24
  python -m taiwan_stock_agent --date 2026-03-24 --tickers 2330 2317
  python -m taiwan_stock_agent --date 2026-03-24 --no-llm
  python -m taiwan_stock_agent --date 2026-03-24 --output signals.json
        """,
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Analysis date YYYY-MM-DD（預設: 17:00 前用前一交易日，之後用今日）",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=DEFAULT_WATCHLIST,
        help=f"Tickers to analyze (default: {' '.join(DEFAULT_WATCHLIST)})",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM reasoning (Claude API call) — only deterministic scoring",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON output to this file (default: stdout)",
    )
    parser.add_argument(
        "--skip-freshness-check",
        action="store_true",
        help="Skip T+1 data freshness verification (use for historical backfill)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "Demo mode: inject synthetic chip data (波段贏家 accumulation scenario). "
            "OHLCV is real; chip/TWSE data is mock. Use to verify full pipeline."
        ),
    )
    args = parser.parse_args()

    if args.date is None:
        now = datetime.now()
        from datetime import timedelta
        candidate = date.today() if now.hour >= 17 else date.today() - timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        analysis_date = candidate
    else:
        try:
            analysis_date = date.fromisoformat(args.date)
        except ValueError:
            logger.error("Invalid date format: %s (expected YYYY-MM-DD)", args.date)
            sys.exit(1)

    # Lazy imports so --help works without DB/env setup
    import os
    from taiwan_stock_agent.infrastructure.finmind_client import (
        FinMindClient,
        DataNotYetAvailableError,
    )
    from taiwan_stock_agent.domain.models import BrokerLabel
    from taiwan_stock_agent.agents.strategist_agent import StrategistAgent
    from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher
    from taiwan_stock_agent.domain.llm_provider import create_llm_provider

    # Initialize DB pool only if DATABASE_URL is configured
    label_repo: object
    if os.environ.get("DATABASE_URL"):
        from taiwan_stock_agent.infrastructure.db import init_pool
        from taiwan_stock_agent.domain.broker_label_classifier import (
            PostgresBrokerLabelRepository,
        )
        init_pool()
        label_repo = PostgresBrokerLabelRepository(conn_factory=None)
        logger.info("Using PostgreSQL broker label repository")
    else:
        # No DB configured — use empty in-memory repo (broker labels = unknown)
        # Chip scoring still works via TWSE free-tier proxy
        class _EmptyLabelRepo:
            def get(self, branch_code: str) -> BrokerLabel | None:
                return None
            def upsert(self, label: BrokerLabel) -> None:
                pass
            def list_all(self) -> list:
                return []
        label_repo = _EmptyLabelRepo()
        logger.info("DATABASE_URL not set — using empty label repo (broker labels = unknown)")

    finmind = FinMindClient()

    llm_provider = None if args.no_llm else create_llm_provider()
    if llm_provider:
        logger.info("LLM provider: %s (%s)", llm_provider.name, llm_provider._model)

    agent = StrategistAgent(
        finmind=finmind,
        label_repo=label_repo,
        chip_proxy_fetcher=ChipProxyFetcher(),
        llm_provider=llm_provider,
    )

    if args.demo:
        _patch_agent_for_demo(agent, analysis_date)

    # T+1 data freshness check (uses first ticker as canary)
    if not args.skip_freshness_check:
        try:
            finmind.verify_data_freshness(args.tickers[0], analysis_date)
        except DataNotYetAvailableError as e:
            logger.error("%s", e)
            sys.exit(1)

    # Run analysis for each ticker
    signals = []
    for ticker in args.tickers:
        logger.info("Analyzing %s ...", ticker)
        try:
            signal = agent.run(ticker=ticker, analysis_date=analysis_date)
            signals.append(signal.model_dump(mode="json"))
            _print_signal(signal)
        except Exception as e:
            logger.error("Failed to analyze %s: %s", ticker, e)
            signals.append({"ticker": ticker, "error": str(e)})

    # Output
    output_json = json.dumps(signals, ensure_ascii=False, indent=2, default=str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)
        logger.info("Signals written to %s", args.output)
    else:
        print("\n" + "=" * 60)
        print("SIGNAL OUTPUT (JSON)")
        print("=" * 60)
        print(output_json)


def _translate_flag(flag: str) -> str:
    """把內部 flag 代碼轉成中文說明。支援靜態比對與前綴比對。"""
    # --- 靜態 flags ---
    _STATIC: dict[str, str] = {
        # 信號結果
        "NO_SETUP":                         "無進場條件（Gate 層未達 2/4）",
        # 資料缺失
        "NO_BROKER_DATA":                   "無分點資料（FinMind 免費方案限制）",
        "NO_HISTORY":                       "無 K 線歷史資料",
        "NO_CHIP_DATA":                     "無籌碼資料（付費版 + 免費版均失敗）",
        # TWSE API 問題
        "TWSE_T86_SCHEMA_CHANGED":          "T86 欄位結構異動",
        "TWSE_T86_TRUST_PARSE_ERROR":       "T86 投信買賣超數字解析失敗",
        "TWSE_T86_DEALER_PARSE_ERROR":      "T86 自營商買賣超數字解析失敗",
        "TWSE_SBL_SCHEMA_CHANGED":          "借券賣出欄位結構異動",
        "TWSE_SBL_PARSE_ERROR":             "借券賣出數字解析失敗",
        "TWSE_MARGN_ERROR:UnexpectedFormat":"融資融券 API 回應格式異常",
        # K 線計算問題
        "DOJI_OR_HALT":                     "十字星或停牌（最高 = 最低）",
        "INSUFFICIENT_HISTORY_VWAP5D":      "歷史不足（無法計算 5 日 VWAP）",
        "INSUFFICIENT_HISTORY_MA_ALIGNMENT":"歷史不足（無法計算 MA5/10/20 排列）",
        "INSUFFICIENT_HISTORY_MA20_SLOPE":  "歷史不足（無法計算 MA20 斜率）",
        "INSUFFICIENT_HISTORY_60D_HIGH":    "歷史不足（無法計算 60 日高點）",
        "INSUFFICIENT_HISTORY_RS":          "歷史不足（無法計算相對強弱）",
        "TWENTY_DAY_HIGH_ZERO":             "20 日高點為 0（資料不足）",
        "INSUFFICIENT_GATE_DATA":           "資料不足導致 Gate 無法完整評估",
        "MA_ALIGNMENT_NAN":                 "均線計算出現 NaN（資料異常）",
        "RS_SCORE_ZERO_BASE":               "相對強弱基期為 0（無法計算）",
        # 風險旗標
        "LONG_UPPER_SHADOW":                "高量上影線（量大收黑，賣壓警告）",
        "SBL_BREAKOUT_FAIL":                "借券沉重 + 未突破 20 日高（空方佔優）",
        "MARGIN_CHASE_HEAT":                "融資追漲熱（漲價 + 融資增 + 使用率高）",
        "THIN_MARKET: no active branches":  "無活躍分點（市場極冷清）",
    }
    if flag in _STATIC:
        return _STATIC[flag]

    # --- 前綴比對（含動態內容）---
    if flag.startswith("TWSE_T86_RATE_LIMITED:"):
        return f"T86 籌碼資料限流（{flag.split(':', 1)[1]}）"
    if flag.startswith("TWSE_T86_NO_DATA:"):
        return f"T86 無資料（{flag.split(':', 1)[1]}，假日或尚未更新）"
    if flag.startswith("TWSE_T86_TICKER_NOT_FOUND:"):
        return f"T86 查無此股（{flag.split(':', 1)[1]}，當日未交易）"
    if flag.startswith("TWSE_T86_ERROR:"):
        return f"T86 抓取異常（{flag.split(':', 1)[1]}）"
    if flag.startswith("TWSE_SBL_RATE_LIMITED:"):
        return f"借券 API 限流（{flag.split(':', 1)[1]}）"
    if flag.startswith("TWSE_SBL_ERROR:"):
        return f"借券 API 異常（{flag.split(':', 1)[1]}）"
    if flag.startswith("TWSE_MARGN_ERROR:"):
        return f"融資融券 API 異常（{flag.split(':', 1)[1]}）"
    if flag.startswith("TWSE_MARGIN_NO_PREV:"):
        return f"前日融資餘額缺失（{flag.split(':', 1)[1]}）"
    if flag.startswith("TWSE_T86_PROXY:"):
        return f"T86 失敗，改用大盤相對強弱代替（{flag.split(':', 1)[1]}）"
    if flag.startswith("TWSE:"):
        return f"籌碼代理：{flag[5:]}"
    if flag.startswith("GATE_PASS:"):
        _cond_labels = {"VWAP": "收盤 > 5日VWAP", "VOL": "量能 > 20日均量×1.2",
                        "HIGH20": "收盤 ≥ 20日高點×99%", "RS": "5日漲幅 > 大盤"}
        cond = flag.split(":", 1)[1]
        return f"Gate 通過：{_cond_labels.get(cond, cond)}"
    if flag.startswith("GATE_FAIL:"):
        _cond_labels = {"VWAP": "收盤 > 5日VWAP", "VOL": "量能 > 20日均量×1.2",
                        "HIGH20": "收盤 ≥ 20日高點×99%", "RS": "5日漲幅 > 大盤"}
        cond = flag.split(":", 1)[1]
        return f"Gate 未達：{_cond_labels.get(cond, cond)}"
    if flag.startswith("GATE_SKIP:"):
        _cond_labels = {"VWAP": "收盤 > 5日VWAP", "VOL": "量能 > 20日均量×1.2",
                        "HIGH20": "收盤 ≥ 20日高點×99%", "RS": "5日漲幅 > 大盤"}
        cond = flag.split(":", 1)[1]
        return f"Gate 跳過（資料不足）：{_cond_labels.get(cond, cond)}"
    if flag.startswith("GATE_MET:"):
        return f"Gate 達標條件數：{flag.split(':', 1)[1]}/2"
    if flag.startswith("INSUFFICIENT_GATE_DATA:"):
        _labels = {"VWAP": "VWAP 歷史不足（需5根）", "VOL": "成交量均線不足（需20根）",
                   "RS": "RS 資料不足（需5個共同交易日）"}
        cond = flag.split(":", 1)[1]
        return f"Gate 資料不足：{_labels.get(cond, cond)}"
    if flag.startswith("GATE_AVAILABLE:"):
        return f"Gate 可評估條件數：{flag.split(':', 1)[1]}/4"
    if flag.startswith("scoring_version:"):
        return f"評分引擎：{flag.split(':', 1)[1]}"
    if flag.startswith("INSUFFICIENT_HISTORY:"):
        return f"K 線歷史不足（{flag.split(':', 1)[1]}）"
    if flag.startswith("PARTIAL_HISTORY:"):
        return f"歷史資料不完整（{flag.split(':', 1)[1]}）"
    if flag.startswith("PARTIAL_PROFILE:"):
        return f"成交量剖面不完整（{flag.split(':', 1)[1]}）"
    if flag.startswith("THIN_MARKET:"):
        return f"市場冷清（{flag.split('THIN_MARKET:', 1)[1].strip()}）"
    if flag.startswith("MARGIN_HIGH_UTIL:"):
        return f"融資使用率偏高（{flag.split(':', 1)[1]}）"
    if flag.startswith("SBL_HEAVY:"):
        return f"借券沉重（{flag.split(':', 1)[1]}，空方壓力大）"
    if flag.startswith("SBL_MODERATE:"):
        return f"借券中等（{flag.split(':', 1)[1]}，留意空方）"
    if flag.startswith("DAYTRADE_HEAT:"):
        return f"當沖比例偏高（{flag.split(':', 1)[1]}）"
    if flag.startswith("OVERHEAT_MA20:"):
        return f"股價過熱（{flag.split(':', 1)[1]}，高於 MA20 超過 10%）"
    if flag.startswith("OVERHEAT_MA60:"):
        return f"股價過熱（{flag.split(':', 1)[1]}，高於 MA60 超過 20%）"
    if flag.startswith("FII_PRESENT:"):
        return f"外資法人進駐（{flag.split(':', 1)[1]}）"
    if flag.startswith("隔日沖_TOP3:"):
        return f"主買前三含隔日沖券商（{flag.split(':', 1)[1]}）"

    # 未知 flag 原樣顯示
    return flag


def _print_signal(signal) -> None:
    """Pretty-print a signal to stdout."""
    action_color = {"LONG": "✅", "WATCH": "👁 ", "CAUTION": "⚠️ "}.get(
        signal.action, "❓"
    )
    print(
        f"\n{action_color} {signal.ticker} | {signal.action} | "
        f"Confidence: {signal.confidence}/100 | {signal.date}"
    )
    if signal.reasoning.momentum:
        print(f"  動能: {signal.reasoning.momentum}")
    if signal.reasoning.chip_analysis:
        print(f"  籌碼: {signal.reasoning.chip_analysis}")
    if signal.reasoning.risk_factors:
        print(f"  風險: {signal.reasoning.risk_factors}")
    plan = signal.execution_plan
    print(
        f"  執行: 進場 {plan.entry_bid_limit}-{plan.entry_max_chase} "
        f"| 停損 {plan.stop_loss} | 目標 {plan.target}"
    )
    if signal.data_quality_flags:
        def _is_gate_detail(f: str) -> bool:
            return (f.startswith("GATE_PASS:") or f.startswith("GATE_FAIL:")
                    or f.startswith("GATE_SKIP:") or f.startswith("GATE_MET:")
                    or f.startswith("INSUFFICIENT_GATE_DATA"))

        signal_flags = [f for f in signal.data_quality_flags
                        if f == "NO_SETUP" or _is_gate_detail(f)]
        meta_flags   = [f for f in signal.data_quality_flags
                        if f.startswith("scoring_version") or f.startswith("GATE_AVAILABLE")]
        data_flags   = [f for f in signal.data_quality_flags
                        if f not in signal_flags and f not in meta_flags]

        if signal_flags:
            print("  ⛔ 無進場條件:")
            for f in signal_flags:
                print(f"     · {_translate_flag(f)}")
        if data_flags:
            print("  ⚠ 數據品質:")
            for f in data_flags:
                print(f"     · {_translate_flag(f)}")
        if meta_flags:
            print("  ℹ 引擎:")
            for f in meta_flags:
                print(f"     · {_translate_flag(f)}")


if __name__ == "__main__":
    main()
