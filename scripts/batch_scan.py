"""Batch scanner — runs StrategistAgent on multiple tickers and ranks by confidence.

Usage:
    python scripts/batch_scan.py
    python scripts/batch_scan.py --date 2026-03-25
    python scripts/batch_scan.py --tickers 2330 2454 2317 --date 2026-03-25
    python scripts/batch_scan.py --min-confidence 40
    python scripts/batch_scan.py --top 10 --date 2026-03-25
    python scripts/batch_scan.py --save-csv              # 存到 data/scans/
    python scripts/batch_scan.py --save-csv --csv-path results.csv
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.agents.strategist_agent import StrategistAgent
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher

logging.basicConfig(
    level=logging.WARNING,  # suppress INFO noise during batch run
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# 台股高流動性標的清單（可依需求擴充）
# -------------------------------------------------------------------
DEFAULT_WATCHLIST = [
    # 半導體
    "2330",  # 台積電
    "2454",  # 聯發科
    "2303",  # 聯電
    "2379",  # 瑞昱
    "3711",  # 日月光投控
    "2408",  # 南亞科
    "2344",  # 華邦電
    # 電子/組裝
    "2317",  # 鴻海
    "2382",  # 廣達
    "2356",  # 英業達
    "2324",  # 仁寶
    "6669",  # 緯穎
    "3231",  # 緯創
    "2357",  # 華碩
    "2353",  # 宏碁
    "2308",  # 台達電
    # 金融
    "2881",  # 富邦金
    "2882",  # 國泰金
    "2884",  # 玉山金
    "2891",  # 中信金
    "2886",  # 兆豐金
    # 傳產/航運
    "2603",  # 長榮
    "2609",  # 陽明
    "1301",  # 台塑
    "1303",  # 南亞
    "2002",  # 中鋼
    # 面板
    "2409",  # 友達
    "3481",  # 群創
]


class _EmptyLabelRepo:
    def get(self, _): return None
    def upsert(self, _): pass
    def list_all(self): return []


def _default_date() -> date:
    candidate = date.today() - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _make_agent() -> StrategistAgent:
    """Create a thread-local agent with its own FinMind + TWSE clients.

    Each worker thread gets independent HTTP sessions and Parquet cache file
    handles, avoiding lock contention and race conditions on shared state.
    """
    return StrategistAgent(
        FinMindClient(),
        _EmptyLabelRepo(),
        chip_proxy_fetcher=ChipProxyFetcher(),
    )


def _scan_one(ticker: str, analysis_date: date) -> dict:
    """Run pipeline for one ticker; return result dict."""
    t0 = time.time()
    try:
        signal = _make_agent().run(ticker, analysis_date)
        elapsed = time.time() - t0
        return {
            "ticker": ticker,
            "action": signal.action,
            "confidence": signal.confidence,
            "halt": signal.halt_flag,
            "free_tier": signal.free_tier_mode,
            "flags": signal.data_quality_flags,
            "entry_bid": signal.execution_plan.entry_bid_limit,
            "stop_loss": signal.execution_plan.stop_loss,
            "target": signal.execution_plan.target,
            "momentum": signal.reasoning.momentum if signal.reasoning else "",
            "chip": signal.reasoning.chip_analysis if signal.reasoning else "",
            "risk": signal.reasoning.risk_factors if signal.reasoning else "",
            "elapsed": elapsed,
            "error": None,
        }
    except Exception as e:
        return {
            "ticker": ticker,
            "action": "ERROR",
            "confidence": -1,
            "halt": True,
            "free_tier": None,
            "flags": [],
            "entry_bid": 0.0,
            "stop_loss": 0.0,
            "target": 0.0,
            "momentum": "",
            "chip": "",
            "risk": "",
            "elapsed": time.time() - t0,
            "error": str(e),
        }


CSV_FIELDS = [
    "scan_date", "analysis_date", "ticker", "action", "confidence",
    "free_tier", "halt", "entry_bid", "stop_loss", "target",
    "momentum", "chip_analysis", "risk_factors", "data_quality_flags",
]


def _save_csv(results: list[dict], analysis_date: date, csv_path: Path) -> None:
    """Append scan results to a CSV file (creates with header if new)."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists()
    scan_date = date.today().isoformat()

    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        for r in results:
            writer.writerow({
                "scan_date": scan_date,
                "analysis_date": analysis_date.isoformat(),
                "ticker": r["ticker"],
                "action": r["action"],
                "confidence": r["confidence"],
                "free_tier": r.get("free_tier", ""),
                "halt": r["halt"],
                "entry_bid": r["entry_bid"],
                "stop_loss": r["stop_loss"],
                "target": r["target"],
                "momentum": r["momentum"],
                "chip_analysis": r["chip"],
                "risk_factors": r["risk"],
                "data_quality_flags": "|".join(r.get("flags") or []),
            })

    print(f"\n  📄 CSV 已儲存: {csv_path}  ({len(results)} 筆)")


def _print_table(results: list[dict], top: int, min_confidence: int) -> None:
    # Filter out errors and halts
    valid = [r for r in results if not r["halt"] and r["error"] is None]
    halted = [r for r in results if r["halt"] or r["error"] is not None]

    # Sort by confidence desc
    valid.sort(key=lambda r: r["confidence"], reverse=True)

    # Apply filters
    if min_confidence > 0:
        valid = [r for r in valid if r["confidence"] >= min_confidence]

    if top:
        valid = valid[:top]

    print(f"\n{'=' * 72}")
    print(f"  BATCH SCAN RESULTS")
    print(f"{'=' * 72}")
    print(f"  {'Rank':<5} {'Ticker':<8} {'Action':<10} {'Conf':>5} {'Entry':>10} {'Stop':>10} {'Target':>10}")
    print(f"  {'-' * 67}")

    for i, r in enumerate(valid, 1):
        action_display = r["action"]
        if r["free_tier"]:
            action_display += "*"  # mark free-tier results
        print(
            f"  {i:<5} {r['ticker']:<8} {action_display:<10} {r['confidence']:>5} "
            f"{r['entry_bid']:>10.1f} {r['stop_loss']:>10.1f} {r['target']:>10.1f}"
        )
        if r["momentum"]:
            print(f"         動能: {r['momentum']}")
        if r["chip"]:
            print(f"         籌碼: {r['chip']}")
        if r["risk"]:
            print(f"         風險: {r['risk']}")
        if r["momentum"] or r["chip"] or r["risk"]:
            print()

    if not valid:
        print(f"  (無符合條件的標的，min_confidence={min_confidence})")

    print(f"\n  * = free_tier_mode (無分點資料，閾值較低)")

    if halted:
        print(f"\n  略過 {len(halted)} 檔 (HALT/ERROR):", ", ".join(r["ticker"] for r in halted))

    print(f"{'=' * 72}")
    print(f"  掃描完成: {len(results)} 檔，有效訊號 {len(valid)} 檔\n")


def run_batch(
    tickers: list[str],
    analysis_date: date,
    top: int,
    min_confidence: int,
    workers: int,
    csv_path: Path | None = None,
) -> None:
    print(f"\n掃描清單: {len(tickers)} 檔")
    print(f"分析日期: {analysis_date}")
    print(f"並行執行: {workers} 個 worker（每個 worker 獨立 HTTP session）\n")

    results: list[dict] = []

    # Each worker creates its own StrategistAgent+FinMindClient to avoid
    # shared-state race conditions on requests.Session and Parquet cache handles.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_scan_one, ticker, analysis_date): ticker
            for ticker in tickers
        }
        for i, future in enumerate(as_completed(futures), 1):
            ticker = futures[future]
            result = future.result()
            results.append(result)
            status = "HALT" if result["halt"] else f"conf={result['confidence']}"
            print(f"  [{i:>2}/{len(tickers)}] {ticker:<8} {status}")

    _print_table(results, top, min_confidence)

    if csv_path:
        _save_csv(results, analysis_date, csv_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="批量掃描台股，依信心分數排序")
    parser.add_argument("--tickers", nargs="+", help="自訂標的清單（預設: 內建 watchlist）")
    parser.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=_default_date(),
        help="分析日期 YYYY-MM-DD（預設: 最近交易日）",
    )
    parser.add_argument("--top", type=int, default=10, help="顯示前 N 名（預設: 10）")
    parser.add_argument("--min-confidence", type=int, default=0, help="最低信心分數門檻（預設: 0）")
    parser.add_argument("--workers", type=int, default=5, help="並行 worker 數（預設: 5；建議 3-8，受 FinMind rate limit 限制）")
    parser.add_argument("--save-csv", action="store_true", help="儲存結果到 CSV 檔案")
    parser.add_argument(
        "--csv-path",
        type=Path,
        default=None,
        help="CSV 路徑（預設: data/scans/scan_YYYY-MM-DD.csv）",
    )
    args = parser.parse_args()

    csv_path: Path | None = None
    if args.save_csv:
        if args.csv_path:
            csv_path = args.csv_path
        else:
            scan_dir = Path(__file__).resolve().parents[1] / "data" / "scans"
            csv_path = scan_dir / f"scan_{args.date}.csv"

    tickers = args.tickers or DEFAULT_WATCHLIST
    run_batch(tickers, args.date, args.top, args.min_confidence, args.workers, csv_path)


if __name__ == "__main__":
    main()
