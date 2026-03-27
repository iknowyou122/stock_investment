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
        required=True,
        help="Analysis date (T+1 settlement date), format YYYY-MM-DD",
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

    if args.no_llm:
        os.environ.pop("ANTHROPIC_API_KEY", None)

    agent = StrategistAgent(
        finmind=finmind,
        label_repo=label_repo,
        chip_proxy_fetcher=ChipProxyFetcher(),
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
        print(f"  ⚠ 數據品質: {', '.join(signal.data_quality_flags)}")


if __name__ == "__main__":
    main()
