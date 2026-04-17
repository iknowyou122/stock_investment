"""Accumulation signal historical backtest.

Replays AccumulationEngine over historical date ranges to compute per-grade
win rates. Designed with no lookahead bias: the engine only sees data up to
the signal date T; the outcome window is T+1 to T+10 trading sessions.

Usage:
    python scripts/coil_backtest.py --date-from 2025-10-01 --date-to 2026-02-28
    python scripts/coil_backtest.py --date-from 2025-10-01 --date-to 2026-02-28 --tickers 2330 2454
    python scripts/coil_backtest.py --date-from 2025-10-01 --date-to 2026-02-28 --save-results
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
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

from taiwan_stock_agent.domain.accumulation_engine import AccumulationEngine
from taiwan_stock_agent.domain.models import DailyOHLCV
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient

_console = Console()
_lock = Lock()

BACKTEST_CSV_FIELDS = [
    "signal_date", "ticker", "market", "grade", "score",
    "entry_close", "twenty_day_high", "success", "days_to_event",
    "final_return", "score_breakdown",
]


# ---------------------------------------------------------------------------
# Ticker-universe helpers
# ---------------------------------------------------------------------------

def _get_tickers_from_sectors(sectors: list[int] | None = None) -> list[str]:
    """Load ticker universe from batch_plan helpers. Local import to avoid circular dep."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
    from batch_plan import (  # type: ignore[import]
        _build_industry_map,
        _build_market_map,
        _build_sector_rows,
        _DEFAULT_SECTOR_NAMES,
    )
    industry_map = _build_industry_map()
    if not industry_map:
        return []

    if sectors:
        rows = _build_sector_rows(industry_map)
        idx_map = {i: name for i, name, _ in rows}
        chosen = {idx_map[n] for n in sectors if n in idx_map}
        if not chosen:
            chosen = _DEFAULT_SECTOR_NAMES
    else:
        chosen = _DEFAULT_SECTOR_NAMES

    return sorted(t for t, ind in industry_map.items() if ind in chosen)


def _get_market_map() -> dict[str, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
    try:
        from batch_plan import _build_market_map  # type: ignore[import]
        return _build_market_map()
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Trading calendar
# ---------------------------------------------------------------------------

def _get_trading_dates(
    date_from: date,
    date_to: date,
    finmind: FinMindClient,
) -> list[date]:
    """Return sorted list of real trading dates in [date_from, date_to].

    Uses TAIEX history to obtain the actual Taiwan exchange calendar.
    Falls back to weekday filtering if TAIEX fetch fails.
    """
    lookback = (date_to - date_from).days + 10
    try:
        df = finmind.fetch_taiex_history(date_to, lookback_days=lookback)
        if df is not None and not df.empty:
            dates = [
                d for d in df["trade_date"].tolist()
                if date_from <= d <= date_to
            ]
            return sorted(set(dates))
    except Exception:
        pass

    # Fallback: weekdays only (approximate)
    result = []
    cur = date_from
    while cur <= date_to:
        if cur.weekday() < 5:
            result.append(cur)
        cur += timedelta(days=1)
    return result


# ---------------------------------------------------------------------------
# Per-ticker per-date signal scan (no lookahead)
# ---------------------------------------------------------------------------

def _df_to_ohlcv(df, ticker: str) -> list[DailyOHLCV]:
    bars: list[DailyOHLCV] = []
    for _, row in df.iterrows():
        bars.append(DailyOHLCV(
            ticker=ticker,
            trade_date=row["trade_date"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
        ))
    return sorted(bars, key=lambda x: x.trade_date)


def _scan_ticker_on_date(
    ticker: str,
    signal_date: date,
    finmind: FinMindClient,
    chip_fetcher,
    market: str,
    taiex_history: list[DailyOHLCV],
) -> dict | None:
    """Run AccumulationEngine on ticker using data up to signal_date only.

    Returns signal dict or None if no grade produced.
    """
    try:
        start = signal_date - timedelta(days=490)
        history_df = finmind.fetch_ohlcv(ticker, start_date=start, end_date=signal_date)
        if history_df is None or history_df.empty:
            return None

        history = _df_to_ohlcv(history_df, ticker)
        if len(history) < 60:
            return None

        proxy = chip_fetcher.fetch(ticker, signal_date)

        # Taiex regime — same logic as coil_scan._scan_one_coil
        taiex_closes = [b.close for b in sorted(taiex_history, key=lambda x: x.trade_date)]
        taiex_regime = "neutral"
        if len(taiex_closes) >= 63:
            ma20 = sum(taiex_closes[-20:]) / 20
            ma60 = sum(taiex_closes[-60:]) / 60
            if ma20 < ma60 * 0.98:
                taiex_regime = "downtrend"

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

        # 20-day high from bars at signal_date (used for success criterion A)
        twenty_day_high = (
            max(b.high for b in sorted_h[-20:]) if len(sorted_h) >= 20
            else sorted_h[-1].high
        )

        return {
            "ticker": ticker,
            "signal_date": signal_date,
            "market": market,
            "grade": result["grade"],
            "score": result["score"],
            "entry_close": sorted_h[-1].close,
            "twenty_day_high": twenty_day_high,
            "score_breakdown": result.get("score_breakdown", {}),
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Future bars fetch
# ---------------------------------------------------------------------------

def _fetch_future_bars(
    ticker: str,
    signal_date: date,
    finmind: FinMindClient,
) -> list[DailyOHLCV]:
    """Fetch up to 10 trading bars after signal_date (T+1 to T+15 calendar days)."""
    start = signal_date + timedelta(days=1)
    end = signal_date + timedelta(days=15)
    try:
        df = finmind.fetch_ohlcv(ticker, start_date=start, end_date=end)
        if df is None or df.empty:
            return []
        bars = _df_to_ohlcv(df, ticker)
        # Only bars strictly after signal_date, max 10
        bars = [b for b in bars if b.trade_date > signal_date]
        return sorted(bars, key=lambda x: x.trade_date)[:10]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Success criterion
# ---------------------------------------------------------------------------

def _check_success(
    entry_close: float,
    entry_20d_high: float,
    future_bars: list[DailyOHLCV],
) -> tuple[bool, int, float]:
    """Evaluate success criteria for an accumulation signal.

    Criterion A: any bar's close breaks above entry_20d_high.
    Criterion B: T+10 close is >= 5% above entry_close.

    Returns (success, days_to_event, final_return_pct).
      days_to_event = first day criterion met (1-based), or len(future_bars) if none.
      final_return_pct = close of last observed bar / entry_close - 1.
    """
    if not future_bars:
        return False, 0, 0.0

    final_return = future_bars[-1].close / entry_close - 1 if entry_close > 0 else 0.0

    # Criterion A: breakout above 20d high
    for i, bar in enumerate(future_bars, start=1):
        if bar.close >= entry_20d_high:
            return True, i, final_return

    # Criterion B: T+10 close >= +5%
    if len(future_bars) >= 10 and final_return >= 0.05:
        return True, len(future_bars), final_return

    return False, len(future_bars), final_return


# ---------------------------------------------------------------------------
# Main backtest loop
# ---------------------------------------------------------------------------

def run_backtest(
    date_from: date,
    date_to: date,
    tickers: list[str],
    workers: int = 4,
    save_results: bool = False,
    market_map: dict[str, str] | None = None,
    verbose: bool = False,
) -> None:
    from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher

    if market_map is None:
        market_map = {}

    finmind = FinMindClient()
    chip_fetcher = ChipProxyFetcher()

    # Trading calendar
    _console.print(f"[dim]取得交易日曆 {date_from} → {date_to}...[/dim]")
    trading_dates = _get_trading_dates(date_from, date_to, finmind)
    _console.print(f"  交易日共 [bold]{len(trading_dates)}[/bold] 天，掃描 [bold]{len(tickers)}[/bold] 支個股")

    if len(trading_dates) > 60:
        _console.print(
            "[yellow]警告：日期範圍超過 60 交易日，掃描時間可能很長。"
            "建議使用 --tickers 縮小範圍。[/yellow]"
        )

    # Pre-fetch TAIEX history — shared by all tickers, covers full range + lookback
    _console.print("[dim]預載 TAIEX 歷史...[/dim]")
    taiex_lookback = (date_to - date_from).days + 200
    try:
        taiex_df = finmind.fetch_taiex_history(date_to, lookback_days=taiex_lookback)
        taiex_history_full: list[DailyOHLCV] = []
        if taiex_df is not None and not taiex_df.empty:
            for _, row in taiex_df.iterrows():
                taiex_history_full.append(DailyOHLCV(
                    ticker="TAIEX",
                    trade_date=row["trade_date"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row.get("volume", 0)),
                ))
        taiex_history_full = sorted(taiex_history_full, key=lambda x: x.trade_date)
    except Exception:
        taiex_history_full = []

    def _taiex_up_to(d: date) -> list[DailyOHLCV]:
        return [b for b in taiex_history_full if b.trade_date <= d]

    # Phase 1: scan all ticker×date combos for signals
    total_tasks = len(tickers) * len(trading_dates)
    signals: list[dict] = []

    _console.print(f"\n[bold cyan]Phase 1:[/bold cyan] 掃描信號 ({total_tasks} 組合)...\n")

    def _worker(ticker: str, signal_date: date) -> dict | None:
        time.sleep(0.15)  # rate limiting between fetches
        market = market_map.get(ticker, "TSE")
        taiex_slice = _taiex_up_to(signal_date)
        return _scan_ticker_on_date(
            ticker, signal_date, finmind, chip_fetcher, market, taiex_slice
        )

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), TimeElapsedColumn(),
        console=_console, transient=True,
    ) as progress:
        task = progress.add_task("掃描信號...", total=total_tasks)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_worker, ticker, signal_date): (ticker, signal_date)
                for ticker in tickers
                for signal_date in trading_dates
            }
            for future in as_completed(futures):
                progress.advance(task)
                try:
                    result = future.result()
                    if result is not None:
                        with _lock:
                            signals.append(result)
                except Exception:
                    pass

    _console.print(f"  信號數量：[bold]{len(signals)}[/bold]")

    if not signals:
        _console.print("[yellow]未產生任何信號，結束。[/yellow]")
        return

    # Phase 2: evaluate outcomes for each signal
    _console.print(f"\n[bold cyan]Phase 2:[/bold cyan] 評估結果 ({len(signals)} 信號)...\n")
    results: list[dict] = []

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), TimeElapsedColumn(),
        console=_console, transient=True,
    ) as progress:
        task = progress.add_task("評估結果...", total=len(signals))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            def _eval_signal(sig: dict) -> dict:
                time.sleep(0.1)
                future_bars = _fetch_future_bars(sig["ticker"], sig["signal_date"], finmind)
                success, days, ret = _check_success(
                    sig["entry_close"], sig["twenty_day_high"], future_bars
                )
                return {**sig, "success": success, "days_to_event": days, "final_return": ret}

            eval_futures = {executor.submit(_eval_signal, sig): sig for sig in signals}
            for future in as_completed(eval_futures):
                progress.advance(task)
                try:
                    r = future.result()
                    with _lock:
                        results.append(r)
                except Exception:
                    pass

    # ---------------------------------------------------------------------------
    # Random baseline: grade=None signals (score below COIL_EARLY threshold)
    # These are stocks that passed gate G1–G4 but scored too low for a grade.
    # We don't track these during scan (score_full returns None), so we compute
    # a synthetic baseline: overall market return in the same period.
    # ---------------------------------------------------------------------------
    # Compute per-grade summary
    grade_groups: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        grade_groups[r["grade"]].append(r)

    grade_order = ["COIL_PRIME", "COIL_MATURE", "COIL_EARLY"]

    _console.rule("[bold magenta]蓄積信號歷史回測結果[/bold magenta]")
    _console.print(f"  期間：{date_from} → {date_to}  |  總信號：{len(results)}\n")

    # Summary table
    tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta", border_style="dim")
    tbl.add_column("等級", width=14)
    tbl.add_column("N 信號", justify="right", width=8)
    tbl.add_column("勝率", justify="right", width=8)
    tbl.add_column("平均報酬", justify="right", width=10)
    tbl.add_column("平均天數", justify="right", width=8)
    tbl.add_column("中位報酬", justify="right", width=10)

    grade_colors = {
        "COIL_PRIME": "bold magenta",
        "COIL_MATURE": "bold cyan",
        "COIL_EARLY": "yellow",
    }

    for grade in grade_order:
        rows = grade_groups.get(grade, [])
        n = len(rows)
        if n == 0:
            tbl.add_row(grade, "0", "--", "--", "--", "--")
            continue

        wins = [r for r in rows if r["success"]]
        win_rate = len(wins) / n * 100
        returns = [r["final_return"] * 100 for r in rows]
        avg_ret = sum(returns) / n
        sorted_ret = sorted(returns)
        median_ret = sorted_ret[n // 2]
        days_list = [r["days_to_event"] for r in rows if r["days_to_event"] > 0]
        avg_days = sum(days_list) / len(days_list) if days_list else 0

        style = grade_colors.get(grade, "white")
        win_str = f"{win_rate:.1f}%"
        ret_str = f"{avg_ret:+.2f}%"
        med_str = f"{median_ret:+.2f}%"
        days_str = f"{avg_days:.1f}"

        tbl.add_row(
            f"[{style}]{grade}[/{style}]",
            str(n),
            f"[{'green' if win_rate >= 50 else 'red'}]{win_str}[/{'green' if win_rate >= 50 else 'red'}]",
            f"[{'green' if avg_ret >= 0 else 'red'}]{ret_str}[/{'green' if avg_ret >= 0 else 'red'}]",
            days_str,
            f"[{'green' if median_ret >= 0 else 'red'}]{med_str}[/{'green' if median_ret >= 0 else 'red'}]",
        )

    _console.print(tbl)

    # Verbose: top 10 signals by score
    if verbose:
        _console.rule("[bold]Top 10 信號（按分數排序）[/bold]")
        top = sorted(results, key=lambda x: x["score"], reverse=True)[:10]
        top_tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold", border_style="dim")
        top_tbl.add_column("日期", width=12)
        top_tbl.add_column("代號", width=8)
        top_tbl.add_column("等級", width=14)
        top_tbl.add_column("分數", justify="right", width=6)
        top_tbl.add_column("進場價", justify="right", width=8)
        top_tbl.add_column("成功", width=6)
        top_tbl.add_column("報酬", justify="right", width=8)

        for r in top:
            style = grade_colors.get(r["grade"], "white")
            success_str = "[green]✓[/green]" if r["success"] else "[red]✗[/red]"
            ret_pct = r["final_return"] * 100
            top_tbl.add_row(
                str(r["signal_date"]),
                r["ticker"],
                f"[{style}]{r['grade']}[/{style}]",
                str(r["score"]),
                f"{r['entry_close']:.2f}",
                success_str,
                f"[{'green' if ret_pct >= 0 else 'red'}]{ret_pct:+.2f}%[/{'green' if ret_pct >= 0 else 'red'}]",
            )
        _console.print(top_tbl)

    # Save CSV
    if save_results:
        out_dir = Path(__file__).resolve().parents[1] / "data" / "backtest"
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = out_dir / f"coil_backtest_{date.today().isoformat()}.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=BACKTEST_CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for r in sorted(results, key=lambda x: (x["signal_date"], x["ticker"])):
                writer.writerow({
                    "signal_date": r["signal_date"].isoformat(),
                    "ticker": r["ticker"],
                    "market": r["market"],
                    "grade": r["grade"],
                    "score": r["score"],
                    "entry_close": r["entry_close"],
                    "twenty_day_high": r["twenty_day_high"],
                    "success": r["success"],
                    "days_to_event": r["days_to_event"],
                    "final_return": round(r["final_return"], 6),
                    "score_breakdown": json.dumps(r.get("score_breakdown", {})),
                })
        _console.print(f"\n  [green]結果已儲存：[/green]{csv_path}  ({len(results)} 筆)")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="蓄積信號歷史回測 — 計算 COIL_PRIME/MATURE/EARLY 勝率"
    )
    parser.add_argument(
        "--date-from",
        default=(date.today() - timedelta(days=90)).isoformat(),
        help="回測起始日 YYYY-MM-DD（預設：90天前）",
    )
    parser.add_argument(
        "--date-to",
        default=(date.today() - timedelta(days=30)).isoformat(),
        help="回測結束日 YYYY-MM-DD（預設：30天前，保留T+10緩衝）",
    )
    parser.add_argument("--tickers", nargs="+", help="指定個股代號（若省略則從 batch_plan 載入）")
    parser.add_argument("--sectors", nargs="+", type=int, help="產業代號（若省略則使用預設產業）")
    parser.add_argument("--workers", type=int, default=4, help="並行 worker 數（預設 4）")
    parser.add_argument("--save-results", action="store_true", help="儲存原始信號 CSV")
    parser.add_argument("--verbose", action="store_true", help="顯示 Top 10 信號明細")
    args = parser.parse_args()

    date_from = date.fromisoformat(args.date_from)
    date_to = date.fromisoformat(args.date_to)

    if date_from >= date_to:
        _console.print("[red]錯誤：date-from 必須早於 date-to[/red]")
        sys.exit(1)

    if (date.today() - date_to).days < 10:
        _console.print(
            "[yellow]警告：date-to 距今不足 10 天，T+10 結果窗口可能不完整。[/yellow]"
        )

    if args.tickers:
        tickers = args.tickers
        market_map = _get_market_map()
    else:
        _console.print("[dim]載入個股清單...[/dim]")
        tickers = _get_tickers_from_sectors(args.sectors)
        market_map = _get_market_map()
        if not tickers:
            _console.print("[red]找不到個股清單，請使用 --tickers 指定。[/red]")
            sys.exit(1)
        _console.print(f"  載入 [bold]{len(tickers)}[/bold] 支個股")

    run_backtest(
        date_from=date_from,
        date_to=date_to,
        tickers=tickers,
        workers=args.workers,
        save_results=args.save_results,
        market_map=market_map,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
