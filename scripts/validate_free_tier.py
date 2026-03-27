"""Free-tier pipeline validation: StrategistAgent without paid broker data.

Runs the full pipeline using only:
  - FinMind free-tier OHLCV data (no broker branch data needed)
  - TWSE opendata chip proxies: 外資買賣超 (T86) + 融資餘額 (MI_MARGN)

Expected output per ticker:
  - free_tier_mode = True (because broker_df will be empty / unavailable)
  - Chip pillar uses TWSE proxies (0–25 pts) instead of paid broker score (0–40 pts)
  - LONG threshold drops 70 → 55 in free_tier_mode

Usage:
    python scripts/validate_free_tier.py
    python scripts/validate_free_tier.py --tickers 2330 2454 --date 2026-03-21
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# --- macOS Homebrew Python SSL fix ---
# Homebrew Python sometimes ships without the system CA bundle properly wired.
# Apply certifi bundle so TWSE HTTPS calls succeed without setting SSL_CERT_FILE manually.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass  # certifi not installed — network calls may fail on macOS Homebrew Python

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv

load_dotenv()

from taiwan_stock_agent.agents.strategist_agent import StrategistAgent
from taiwan_stock_agent.domain.broker_label_classifier import BrokerLabelRepository  # noqa: F401
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Default tickers: high-liquidity names with reliable TWSE opendata coverage
DEFAULT_TICKERS = ["2330", "2454", "6669"]

# Default analysis date: 5 days ago (gives FinMind data time to settle)
def _default_date() -> date:
    today = date.today()
    candidate = today - timedelta(days=5)
    # Walk back past weekends to a weekday
    while candidate.weekday() >= 5:  # 5=Sat, 6=Sun
        candidate -= timedelta(days=1)
    return candidate


class _InMemoryLabelRepo:
    """Empty repo — no historical broker labels available in free-tier mode.

    Satisfies the BrokerLabelRepository Protocol structurally.
    """

    def get(self, branch_code: str):  # noqa: ARG002
        return None

    def upsert(self, label) -> None:  # noqa: ARG002
        pass

    def list_all(self) -> list:
        return []


def _print_result(ticker: str, result) -> None:
    """Pretty-print a SignalOutput to stdout."""
    print(f"\n{'=' * 60}")
    print(f"  Ticker : {ticker}")
    print(f"  Date   : {result.date}")
    print(f"  Action : {result.action}")
    print(f"  Confidence : {result.confidence}")
    print(f"  free_tier_mode : {result.free_tier_mode}")
    print(f"  halt_flag : {result.halt_flag}")

    if result.data_quality_flags:
        print(f"  Data quality flags:")
        for flag in result.data_quality_flags:
            print(f"    - {flag}")

    if result.reasoning:
        print(f"  Reasoning:")
        print(f"    momentum     : {result.reasoning.momentum or '(empty — no API key)'}")
        print(f"    chip_analysis: {result.reasoning.chip_analysis or '(empty)'}")
        print(f"    risk_factors : {result.reasoning.risk_factors or '(empty)'}")

    if result.execution_plan:
        ep = result.execution_plan
        print(f"  Execution plan:")
        print(f"    entry_bid  : {ep.entry_bid_limit}")
        print(f"    entry_max  : {ep.entry_max_chase}")
        print(f"    stop_loss  : {ep.stop_loss}")
        print(f"    target     : {ep.target}")

    print(f"{'=' * 60}")


def run_validation(tickers: list[str], analysis_date: date) -> None:
    print(f"\nFree-tier pipeline validation")
    print(f"Tickers      : {tickers}")
    print(f"Analysis date: {analysis_date}")
    print(f"Mode         : free_tier_mode (no paid broker data)")

    finmind = FinMindClient()
    chip_proxy_fetcher = ChipProxyFetcher()
    label_repo = _InMemoryLabelRepo()

    agent = StrategistAgent(
        finmind,
        label_repo,
        chip_proxy_fetcher=chip_proxy_fetcher,
        # No anthropic_api_key → LLM reasoning will be skipped (empty fields)
    )

    results = {}
    for ticker in tickers:
        logger.info("Running pipeline for %s on %s ...", ticker, analysis_date)
        try:
            signal = agent.run(ticker, analysis_date)
            results[ticker] = signal
        except Exception as exc:
            logger.error("Pipeline failed for %s: %s", ticker, exc, exc_info=True)
            results[ticker] = None
            continue
        try:
            _print_result(ticker, signal)
        except Exception as exc:
            logger.warning("Display error for %s (signal is valid): %s", ticker, exc)

    # Summary
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    passed = 0
    for ticker, signal in results.items():
        if signal is None:
            status = "ERROR"
        elif signal.halt_flag:
            status = f"HALT ({', '.join(signal.data_quality_flags[:2])})"
        else:
            status = f"{signal.action} (confidence={signal.confidence}, free_tier={signal.free_tier_mode})"
            passed += 1
        print(f"  {ticker:8s}: {status}")

    print(f"\nCompleted: {passed}/{len(tickers)} tickers produced a signal (no halt).")

    # Validate free_tier_mode is set on non-halt signals
    non_halt = [t for t, s in results.items() if s and not s.halt_flag]
    if non_halt:
        free_tier_ok = all(results[t].free_tier_mode for t in non_halt)
        print(f"free_tier_mode=True on all non-halt signals: {'✓' if free_tier_ok else '✗ MISMATCH'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate free-tier StrategistAgent pipeline")
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=DEFAULT_TICKERS,
        help="Ticker symbols to test (default: 2330 2454 6669)",
    )
    parser.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=_default_date(),
        help="Analysis date YYYY-MM-DD (default: most recent Friday)",
    )
    args = parser.parse_args()
    run_validation(args.tickers, args.date)


if __name__ == "__main__":
    main()
