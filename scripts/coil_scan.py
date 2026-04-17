"""Accumulation scanner — runs AccumulationEngine on multiple tickers.

Usage:
    python scripts/coil_scan.py                          # 互動式產業選擇
    python scripts/coil_scan.py --sectors 1 4
    python scripts/coil_scan.py --tickers 2330 2454
    python scripts/coil_scan.py --save-csv
    python scripts/coil_scan.py --date 2026-04-13
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from threading import Lock

from rich import box
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, TaskProgressColumn
from rich.table import Table
from rich.text import Text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.domain.accumulation_engine import AccumulationEngine
from taiwan_stock_agent.domain.models import DailyOHLCV
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient

_console = Console()
_lock = Lock()

COIL_CSV_FIELDS = [
    "scan_date", "analysis_date", "ticker", "name", "market", "grade", "score",
    "bb_pct", "vol_ratio", "consol_range_pct", "inst_consec_days",
    "weeks_consolidating", "vs_60d_high_pct", "score_breakdown", "flags",
]

GRADE_COLOR = {
    "COIL_PRIME": "bold magenta",
    "COIL_MATURE": "bold cyan",
    "COIL_EARLY": "yellow",
}


def _weeks_consolidating(history: list) -> int:
    """Count consecutive sessions where close stays within 20d high/low spread, divided by 5."""
    if len(history) < 20:
        return 0
    sorted_h = sorted(history, key=lambda x: x.trade_date)
    last20 = sorted_h[-20:]
    high20 = max(d.high for d in last20)
    low20 = min(d.low for d in last20)
    count = 0
    for bar in reversed(sorted_h):
        if low20 <= bar.close <= high20:
            count += 1
        else:
            break
    return count // 5


def _scan_one_coil(
    ticker: str,
    analysis_date: date,
    finmind: FinMindClient,
    chip_fetcher,
    market: str,
    taiex_history: list,
) -> dict | None:
    try:
        # Fetch 330 trading days (need 266 for ATR percentile + buffer)
        start = analysis_date - timedelta(days=490)
        history_df = finmind.fetch_ohlcv(ticker, start_date=start, end_date=analysis_date)
        if history_df is None or history_df.empty:
            return None
        history: list[DailyOHLCV] = []
        for _, row in history_df.iterrows():
            history.append(DailyOHLCV(
                ticker=ticker,
                trade_date=row["trade_date"],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
            ))
        history = sorted(history, key=lambda x: x.trade_date)
        if len(history) < 60:
            return None
        proxy = chip_fetcher.fetch(ticker, analysis_date)

        # Compute taiex_regime from taiex_history
        taiex_closes = [b.close for b in sorted(taiex_history, key=lambda x: x.trade_date)]
        taiex_regime = "neutral"
        if len(taiex_closes) >= 63:
            ma20 = sum(taiex_closes[-20:]) / 20
            ma60 = sum(taiex_closes[-60:]) / 60
            if ma20 < ma60 * 0.98:
                taiex_regime = "downtrend"

        # Compute avg daily turnover from last 20 days
        sorted_h = sorted(history, key=lambda x: x.trade_date)
        last20 = sorted_h[-20:]
        turnover_20ma = sum(b.close * b.volume for b in last20) / 20 if last20 else 0

        eng = AccumulationEngine(market=market)
        result = eng.score_full(
            history=history,
            proxy=proxy,
            taiex_regime=taiex_regime,
            taiex_history=taiex_history,
            turnover_20ma=turnover_20ma,
        )
        if result is None:
            return None

        result["ticker"] = ticker
        result["market"] = market
        result["analysis_date"] = analysis_date.isoformat()
        result["weeks_consolidating"] = _weeks_consolidating(history)
        return result
    except Exception:
        return None


def _notify_coil_telegram(coil_csv_path: Path, scan_date: str) -> None:
    import urllib.request
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        rows = []
        with open(coil_csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("grade", "") in ("COIL_PRIME", "COIL_MATURE", "COIL_EARLY"):
                    rows.append(row)
        if not rows:
            return

        lines = [f"蓄積雷達觀察清單 {scan_date}\n"]
        grade_text = {
            "COIL_PRIME": "PRIME",
            "COIL_MATURE": "MATURE",
            "COIL_EARLY": "EARLY"
        }
        for row in rows[:12]:
            grade = row.get("grade", "")
            grade_label = grade_text.get(grade, grade)
            ticker = row.get("ticker", "")
            name = row.get("name", "")
            score = row.get("score", "--")
            vs_high = row.get("vs_60d_high_pct", "--")
            consec = row.get("inst_consec_days", "0")
            weeks = row.get("weeks_consolidating", "0")

            # 第一行：代號 名稱 分數 等級
            lines.append(f"*{ticker}* {name}  `{score}分` ({grade_label})")
            # 第二行：縮排數據 
            lines.append(f"   前高:{vs_high}%  法人:{consec}d  橫盤:{weeks}w\n")

        text = "\n".join(lines)
        payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        _console.print(f"  [dim red]TG coil notify error: {exc}[/dim red]")


def _save_coil_csv(
    results: list[dict],
    scan_date: str,
    analysis_date: date,
    csv_path: Path,
    name_map: dict[str, str],
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COIL_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in sorted(results, key=lambda x: x.get("score", 0), reverse=True):
            writer.writerow({
                "scan_date": scan_date,
                "analysis_date": analysis_date.isoformat(),
                "ticker": r.get("ticker", ""),
                "name": name_map.get(r.get("ticker", ""), r.get("ticker", "")),
                "market": r.get("market", ""),
                "grade": r.get("grade", ""),
                "score": r.get("score", 0),
                "bb_pct": r.get("bb_pct", ""),
                "vol_ratio": r.get("vol_ratio", ""),
                "consol_range_pct": r.get("consol_range_pct", ""),
                "inst_consec_days": r.get("inst_consec_days", 0),
                "weeks_consolidating": r.get("weeks_consolidating", 0),
                "vs_60d_high_pct": r.get("vs_60d_high_pct", ""),
                "score_breakdown": json.dumps(r.get("score_breakdown", {})),
                "flags": "|".join(r.get("flags", [])),
            })
    _console.print(f"\n  [green]Coil CSV 已儲存:[/green] {csv_path}  ({len(results)} 筆)")


def _print_coil_table(results: list[dict], scan_date: str, name_map: dict[str, str]) -> None:
    _console.rule(f"[bold magenta]蓄積雷達 {scan_date}[/bold magenta]")
    if not results:
        _console.print("  [dim]無符合條件的蓄積標的[/dim]")
        return
    tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta", border_style="dim")
    tbl.add_column("Rank", justify="right", width=4)
    tbl.add_column("代號", width=8)
    tbl.add_column("名稱", width=12)
    tbl.add_column("等級", width=12)
    tbl.add_column("分數", justify="right", width=6)
    tbl.add_column("BB壓縮", justify="right", width=8)
    tbl.add_column("法人連買", justify="right", width=8)
    tbl.add_column("橫盤週", justify="right", width=7)
    tbl.add_column("vs前高", justify="right", width=8)

    sorted_r = sorted(results, key=lambda x: x.get("score", 0), reverse=True)
    for i, r in enumerate(sorted_r, 1):
        grade = r.get("grade", "")
        style = GRADE_COLOR.get(grade, "white")
        bb_pct = r.get("bb_pct")
        bb_str = f"{bb_pct:.0f}%" if bb_pct is not None else "--"
        vs_high = r.get("vs_60d_high_pct")
        vs_str = f"{vs_high:+.1f}%" if vs_high is not None else "--"
        ticker = r.get("ticker", "")
        name = name_map.get(ticker, ticker)[:8]
        tbl.add_row(
            str(i),
            f"[{style}]{ticker}[/{style}]",
            name,
            f"[{style}]{grade}[/{style}]",
            str(r.get("score", 0)),
            bb_str,
            str(r.get("inst_consec_days", 0)),
            str(r.get("weeks_consolidating", 0)),
            vs_str,
        )
    _console.print(tbl)


def run_coil_scan(
    tickers: list[str],
    analysis_date: date,
    workers: int = 8,
    market_map: dict[str, str] | None = None,
    name_map: dict[str, str] | None = None,
    csv_path: Path | None = None,
    notify: bool = False,
) -> list[dict]:
    from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher

    if market_map is None:
        market_map = {}
    if name_map is None:
        name_map = {}

    finmind = FinMindClient()
    chip_fetcher = ChipProxyFetcher()

    # Fetch TAIEX history once (shared across all tickers)
    try:
        taiex_df = finmind.fetch_taiex_history(analysis_date, lookback_days=130)
        taiex_history: list[DailyOHLCV] = []
        if taiex_df is not None and not taiex_df.empty:
            for _, row in taiex_df.iterrows():
                taiex_history.append(DailyOHLCV(
                    ticker="TAIEX",
                    trade_date=row["trade_date"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row.get("volume", 0)),
                ))
            taiex_history = sorted(taiex_history, key=lambda x: x.trade_date)
    except Exception:
        taiex_history = []

    results: list[dict] = []
    scan_date = date.today().isoformat()

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), TimeElapsedColumn(),
        console=_console, transient=True
    ) as progress:
        task = progress.add_task(f"蓄積掃描 {len(tickers)} 檔...", total=len(tickers))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _scan_one_coil,
                    ticker, analysis_date, finmind, chip_fetcher,
                    market_map.get(ticker, "TSE"), taiex_history
                ): ticker
                for ticker in tickers
            }
            for future in as_completed(futures):
                progress.advance(task)
                try:
                    result = future.result()
                    if result:
                        with _lock:
                            results.append(result)
                except Exception:
                    pass

    _print_coil_table(results, scan_date, name_map)

    if csv_path and results:
        _save_coil_csv(results, scan_date, analysis_date, csv_path, name_map)
        if notify:
            _notify_coil_telegram(csv_path, scan_date)

    return results


def main() -> None:
    # Local import to avoid circular dependency — batch_plan imports coil_scan in Pass 2,
    # so coil_scan must not import batch_plan at module level.
    from batch_plan import (
        _build_industry_map,
        _build_name_map,
        _build_market_map,
        _build_sector_rows,
        _sector_menu,
        _select_sectors,
        _default_date,
        _DEFAULT_SECTOR_NAMES,
    )

    parser = argparse.ArgumentParser(description="蓄積雷達掃描")
    parser.add_argument("--tickers", nargs="+", help="指定個股代號")
    parser.add_argument("--sectors", nargs="+", type=int, help="產業代號")
    parser.add_argument("--date", default=None, help="分析日期 YYYY-MM-DD")
    parser.add_argument("--save-csv", action="store_true", default=True, help="儲存 CSV（預設開啟）")
    parser.add_argument("--no-save", action="store_true", help="不儲存 CSV")
    parser.add_argument("--notify", action="store_true", help="推播 Telegram")
    parser.add_argument("--only-notify", action="store_true", help="僅執行推播而不掃描")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    analysis_date = date.fromisoformat(args.date) if args.date else _default_date()

    # Pre-calculate CSV path
    scan_dir = Path(__file__).resolve().parents[1] / "data" / "scans"
    csv_path = scan_dir / f"coil_{analysis_date.isoformat()}.csv"

    if args.only_notify:
        if csv_path.exists():
            _notify_coil_telegram(csv_path, analysis_date.isoformat())
            _console.print(f"  [green]已針對現有 CSV 執行推播:[/green] {csv_path}")
        else:
            _console.print(f"  [red]找不到 CSV 檔案，無法推播:[/red] {csv_path}")
        return

    industry_map = _build_industry_map()
    name_map = _build_name_map()
    market_map = _build_market_map()

    if args.tickers:
        tickers = args.tickers
    else:
        if not industry_map:
            _console.print("[yellow]找不到 industry_map，無法選擇產業[/yellow]")
            return

        industry_map_rows = _build_sector_rows(industry_map)
        idx_map = {i: name for i, name, _ in industry_map_rows}

        if args.sectors:
            # Non-interactive: resolve numeric codes directly
            chosen = {idx_map[n] for n in args.sectors if n in idx_map}
            if not chosen:
                _console.print("  [yellow]指定代號無效，使用預設產業[/yellow]")
                chosen = _DEFAULT_SECTOR_NAMES
        else:
            rows = _sector_menu(industry_map)
            chosen = _select_sectors(rows, _DEFAULT_SECTOR_NAMES)

        tickers = sorted(t for t, ind in industry_map.items() if ind in chosen)

    save_csv = args.save_csv and not args.no_save
    csv_path = None
    if save_csv:
        scan_dir = Path(__file__).resolve().parents[1] / "data" / "scans"
        csv_path = scan_dir / f"coil_{analysis_date.isoformat()}.csv"

    run_coil_scan(
        tickers=tickers,
        analysis_date=analysis_date,
        workers=args.workers,
        market_map=market_map,
        name_map=name_map,
        csv_path=csv_path,
        notify=args.notify,
    )


if __name__ == "__main__":
    main()
