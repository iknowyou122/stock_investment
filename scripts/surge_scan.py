"""SurgeRadar scanner — aggressive fresh-ignition detection.

Usage:
    python scripts/surge_scan.py                          # 互動式產業選擇
    python scripts/surge_scan.py --sectors 1 4
    python scripts/surge_scan.py --tickers 2330 2454
    python scripts/surge_scan.py --save-csv
    python scripts/surge_scan.py --date 2026-04-21
    python scripts/surge_scan.py --notify                 # Telegram
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from threading import Lock

from rich import box
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv

load_dotenv()

from taiwan_stock_agent.domain.models import DailyOHLCV
from taiwan_stock_agent.domain.surge_radar import SurgeRadar
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient

_console = Console()
_lock = Lock()

SURGE_CSV_FIELDS = [
    "scan_date", "analysis_date", "ticker", "name", "market", "industry",
    "grade", "score", "vol_ratio", "close_strength", "day_chg_pct",
    "gap_pct", "surge_day", "industry_rank_pct", "rsi", "inst_consec_days",
    "score_breakdown", "flags",
]

GRADE_COLOR = {
    "SURGE_ALPHA": "bold red",
    "SURGE_BETA": "bold yellow",
    "SURGE_GAMMA": "cyan",
}


def _load_history(
    ticker: str, analysis_date: date, finmind: FinMindClient
) -> list[DailyOHLCV] | None:
    """Fetch ~250 days history; return None if insufficient."""
    try:
        start = analysis_date - timedelta(days=380)
        df = finmind.fetch_ohlcv(ticker, start_date=start, end_date=analysis_date)
        if df is None or df.empty:
            return None
        history: list[DailyOHLCV] = []
        for _, row in df.iterrows():
            history.append(
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
        history.sort(key=lambda x: x.trade_date)
        if len(history) < 25:
            return None
        return history
    except Exception:
        return None


def _compute_industry_strength(
    per_ticker_today: dict[str, dict],
    industry_map: dict[str, str],
) -> dict[str, float]:
    """Aggregate per-industry strength score and convert to percentile rank per industry.

    Industry strength = mean(vol_ratio * max(day_chg_pct, 0)) across that industry's
    tickers today. Only counts up-days to avoid noise from declining stocks.

    Returns: {industry_name: percentile_rank (0-100)}
    """
    by_industry: dict[str, list[float]] = {}
    for ticker, payload in per_ticker_today.items():
        industry = industry_map.get(ticker)
        if not industry:
            continue
        vr = payload.get("vol_ratio", 0) or 0
        chg = payload.get("day_chg_pct", 0) or 0
        strength = vr * max(chg, 0)
        by_industry.setdefault(industry, []).append(strength)

    industry_score: dict[str, float] = {
        ind: sum(scores) / len(scores) for ind, scores in by_industry.items() if scores
    }
    if not industry_score:
        return {}

    sorted_inds = sorted(industry_score.items(), key=lambda kv: kv[1])
    n = len(sorted_inds)
    ranks: dict[str, float] = {}
    for rank, (ind, _) in enumerate(sorted_inds):
        # rank 0 is weakest → 0%. Last is strongest → 100%.
        ranks[ind] = round(rank / max(n - 1, 1) * 100, 1)
    return ranks


def _scan_one_surge(
    ticker: str,
    analysis_date: date,
    finmind: FinMindClient,
    chip_fetcher,
    market: str,
    taiex_history: list[DailyOHLCV],
    industry_rank_pct: float | None,
) -> dict | None:
    """Full surge scoring for a single ticker."""
    try:
        history = _load_history(ticker, analysis_date, finmind)
        if history is None:
            return None
        ohlcv = history[-1]
        prior_history = history[:-1]
        if len(prior_history) < 20:
            return None

        proxy = chip_fetcher.fetch(ticker, analysis_date)

        # TAIEX regime
        taiex_closes = [b.close for b in sorted(taiex_history, key=lambda x: x.trade_date)]
        taiex_regime = "neutral"
        if len(taiex_closes) >= 63:
            ma20 = sum(taiex_closes[-20:]) / 20
            ma60 = sum(taiex_closes[-60:]) / 60
            if ma20 < ma60 * 0.98:
                taiex_regime = "downtrend"

        turnover_20ma = (
            sum(b.close * b.volume for b in prior_history[-20:]) / 20
            if len(prior_history) >= 20 else 0
        )

        eng = SurgeRadar(market=market)
        result = eng.score_full(
            ohlcv=ohlcv,
            history=prior_history,
            proxy=proxy,
            taiex_regime=taiex_regime,
            taiex_history=taiex_history,
            turnover_20ma=turnover_20ma,
            industry_rank_pct=industry_rank_pct,
        )
        if result is None:
            return None

        result["ticker"] = ticker
        result["market"] = market
        result["analysis_date"] = analysis_date.isoformat()
        return result
    except Exception:
        return None


def _precompute_today_snapshot(
    tickers: list[str],
    analysis_date: date,
    finmind: FinMindClient,
    workers: int = 8,
) -> dict[str, dict]:
    """Pass 1: fetch today's bar + 20d avg vol for every ticker (for industry ranking).

    Returns: {ticker: {"vol_ratio": float, "day_chg_pct": float}}
    """
    snapshot: dict[str, dict] = {}

    def _one(ticker: str) -> tuple[str, dict] | None:
        history = _load_history(ticker, analysis_date, finmind)
        if history is None or len(history) < 21:
            return None
        today = history[-1]
        prior = history[:-1]
        vols = [b.volume for b in prior[-20:]]
        vol_20ma = sum(vols) / len(vols) if vols else 0
        vol_ratio = today.volume / vol_20ma if vol_20ma > 0 else 0
        prev_close = prior[-1].close if prior else 0
        day_chg_pct = (today.close / prev_close - 1) * 100 if prev_close > 0 else 0
        return ticker, {"vol_ratio": vol_ratio, "day_chg_pct": day_chg_pct}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"Pass 1 產業強度預掃 {len(tickers)} 檔...", total=len(tickers))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_one, t): t for t in tickers}
            for future in as_completed(futures):
                progress.advance(task)
                try:
                    result = future.result()
                    if result:
                        with _lock:
                            snapshot[result[0]] = result[1]
                except Exception:
                    pass
    return snapshot


def _print_surge_table(results: list[dict], scan_date: str, name_map: dict[str, str]) -> None:
    _console.rule(f"[bold red]噴發雷達 {scan_date}[/bold red]")
    if not results:
        _console.print("  [dim]無符合條件的噴發標的[/dim]")
        return
    tbl = Table(
        box=box.ROUNDED, show_header=True, header_style="bold red", border_style="dim"
    )
    tbl.add_column("Rank", justify="right", width=4)
    tbl.add_column("代號", width=8)
    tbl.add_column("名稱", width=12)
    tbl.add_column("等級", width=12)
    tbl.add_column("分數", justify="right", width=6)
    tbl.add_column("量比", justify="right", width=6)
    tbl.add_column("漲幅%", justify="right", width=7)
    tbl.add_column("收位", justify="right", width=6)
    tbl.add_column("跳空%", justify="right", width=6)
    tbl.add_column("爆量日", justify="right", width=6)
    tbl.add_column("產業排名", justify="right", width=8)
    tbl.add_column("法人連買", justify="right", width=8)
    tbl.add_column("RSI", justify="right", width=5)

    sorted_r = sorted(results, key=lambda x: x.get("score", 0), reverse=True)
    for i, r in enumerate(sorted_r, 1):
        grade = r.get("grade", "")
        style = GRADE_COLOR.get(grade, "white")
        ticker = r.get("ticker", "")
        name = name_map.get(ticker, ticker)[:8]
        ind_pct = r.get("industry_rank_pct")
        ind_str = f"{ind_pct:.0f}%" if ind_pct is not None else "--"
        rsi = r.get("rsi")
        rsi_str = f"{rsi:.0f}" if rsi is not None else "--"
        tbl.add_row(
            str(i),
            f"[{style}]{ticker}[/{style}]",
            name,
            f"[{style}]{grade}[/{style}]",
            str(r.get("score", 0)),
            f"{r.get('vol_ratio', 0):.2f}",
            f"{r.get('day_chg_pct', 0):+.2f}",
            f"{r.get('close_strength', 0):.2f}",
            f"{r.get('gap_pct', 0):+.1f}",
            str(r.get("surge_day", 0)),
            ind_str,
            str(r.get("inst_consec_days", 0)),
            rsi_str,
        )
    _console.print(tbl)


def _save_surge_csv(
    results: list[dict],
    scan_date: str,
    analysis_date: date,
    csv_path: Path,
    name_map: dict[str, str],
    industry_map: dict[str, str],
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SURGE_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in sorted(results, key=lambda x: x.get("score", 0), reverse=True):
            ticker = r.get("ticker", "")
            writer.writerow({
                "scan_date": scan_date,
                "analysis_date": analysis_date.isoformat(),
                "ticker": ticker,
                "name": name_map.get(ticker, ticker),
                "market": r.get("market", ""),
                "industry": industry_map.get(ticker, ""),
                "grade": r.get("grade", ""),
                "score": r.get("score", 0),
                "vol_ratio": r.get("vol_ratio", ""),
                "close_strength": r.get("close_strength", ""),
                "day_chg_pct": r.get("day_chg_pct", ""),
                "gap_pct": r.get("gap_pct", ""),
                "surge_day": r.get("surge_day", ""),
                "industry_rank_pct": r.get("industry_rank_pct", ""),
                "rsi": r.get("rsi", ""),
                "inst_consec_days": r.get("inst_consec_days", 0),
                "score_breakdown": json.dumps(r.get("score_breakdown", {})),
                "flags": "|".join(r.get("flags", [])),
            })
    _console.print(f"\n  [green]Surge CSV 已儲存:[/green] {csv_path}  ({len(results)} 筆)")


def _notify_surge_telegram(csv_path: Path, scan_date: str) -> None:
    import urllib.request
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        rows = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("grade", "") in ("SURGE_ALPHA", "SURGE_BETA"):
                    rows.append(row)
        if not rows:
            return

        lines = [f"噴發雷達 {scan_date}\n"]
        grade_text = {"SURGE_ALPHA": "ALPHA", "SURGE_BETA": "BETA"}
        for row in rows[:12]:
            grade = row.get("grade", "")
            lines.append(
                f"*{row.get('ticker', '')}* {row.get('name', '')}  "
                f"`{row.get('score', '--')}分` ({grade_text.get(grade, grade)})"
            )
            lines.append(
                f"   量比:{row.get('vol_ratio', '--')}x  "
                f"漲:{row.get('day_chg_pct', '--')}%  "
                f"收位:{row.get('close_strength', '--')}  "
                f"產業:{row.get('industry_rank_pct', '--')}%\n"
            )
        text = "\n".join(lines)
        payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        _console.print(f"  [dim red]TG surge notify error: {exc}[/dim red]")


def run_surge_scan(
    tickers: list[str],
    analysis_date: date,
    workers: int = 8,
    market_map: dict[str, str] | None = None,
    name_map: dict[str, str] | None = None,
    industry_map: dict[str, str] | None = None,
    csv_path: Path | None = None,
    notify: bool = False,
) -> list[dict]:
    from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher

    market_map = market_map or {}
    name_map = name_map or {}
    industry_map = industry_map or {}

    finmind = FinMindClient()
    chip_fetcher = ChipProxyFetcher()

    # Shared TAIEX history
    try:
        taiex_df = finmind.fetch_taiex_history(analysis_date, lookback_days=130)
        taiex_history: list[DailyOHLCV] = []
        if taiex_df is not None and not taiex_df.empty:
            for _, row in taiex_df.iterrows():
                taiex_history.append(
                    DailyOHLCV(
                        ticker="TAIEX",
                        trade_date=row["trade_date"],
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(row.get("volume", 0)),
                    )
                )
            taiex_history.sort(key=lambda x: x.trade_date)
    except Exception:
        taiex_history = []

    # Pass 1: precompute today's snapshot for industry ranking
    snapshot = _precompute_today_snapshot(tickers, analysis_date, finmind, workers)
    industry_ranks = _compute_industry_strength(snapshot, industry_map)

    # Pass 2: full surge scoring
    results: list[dict] = []
    scan_date = date.today().isoformat()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task(
            f"Pass 2 噴發掃描 {len(tickers)} 檔...", total=len(tickers)
        )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for ticker in tickers:
                ind = industry_map.get(ticker)
                ind_rank = industry_ranks.get(ind) if ind else None
                futures[
                    executor.submit(
                        _scan_one_surge,
                        ticker,
                        analysis_date,
                        finmind,
                        chip_fetcher,
                        market_map.get(ticker, "TSE"),
                        taiex_history,
                        ind_rank,
                    )
                ] = ticker
            for future in as_completed(futures):
                progress.advance(task)
                try:
                    result = future.result()
                    if result:
                        with _lock:
                            results.append(result)
                except Exception:
                    pass

    _print_surge_table(results, scan_date, name_map)

    if csv_path and results:
        _save_surge_csv(results, scan_date, analysis_date, csv_path, name_map, industry_map)
        if notify:
            _notify_surge_telegram(csv_path, scan_date)

    return results


def main() -> None:
    from batch_plan import (
        _DEFAULT_SECTOR_NAMES,
        _build_industry_map,
        _build_market_map,
        _build_name_map,
        _build_sector_rows,
        _default_date,
        _sector_menu,
        _select_sectors,
    )

    parser = argparse.ArgumentParser(description="噴發雷達掃描（短線爆量捕捉）")
    parser.add_argument("--tickers", nargs="+", help="指定個股代號")
    parser.add_argument("--sectors", nargs="+", type=int, help="產業代號")
    parser.add_argument("--date", default=None, help="分析日期 YYYY-MM-DD")
    parser.add_argument("--save-csv", action="store_true", default=True, help="儲存 CSV（預設開啟）")
    parser.add_argument("--no-save", action="store_true", help="不儲存 CSV")
    parser.add_argument("--notify", action="store_true", help="推播 Telegram")
    parser.add_argument("--only-notify", action="store_true", help="僅推播現有 CSV")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    analysis_date = date.fromisoformat(args.date) if args.date else _default_date()

    scan_dir = Path(__file__).resolve().parents[1] / "data" / "scans"
    csv_path = scan_dir / f"surge_{analysis_date.isoformat()}.csv"

    if args.only_notify:
        if csv_path.exists():
            _notify_surge_telegram(csv_path, analysis_date.isoformat())
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
            chosen = {idx_map[n] for n in args.sectors if n in idx_map}
            if not chosen:
                _console.print("  [yellow]指定代號無效，使用預設產業[/yellow]")
                chosen = _DEFAULT_SECTOR_NAMES
        else:
            rows = _sector_menu(industry_map)
            chosen = _select_sectors(rows, _DEFAULT_SECTOR_NAMES)

        tickers = sorted(t for t, ind in industry_map.items() if ind in chosen)

    save_csv = args.save_csv and not args.no_save
    final_csv_path = csv_path if save_csv else None

    run_surge_scan(
        tickers=tickers,
        analysis_date=analysis_date,
        workers=args.workers,
        market_map=market_map,
        name_map=name_map,
        industry_map=industry_map,
        csv_path=final_csv_path,
        notify=args.notify,
    )


if __name__ == "__main__":
    main()
