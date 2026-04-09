"""Phase A: T-2 策略驗證 — 比較 delay=0/1/2 的進場勝率和報酬。

從 DB 讀取所有 LONG 訊號，用 OHLCV 資料模擬不同 entry_delay 的結果。
不需重跑 backtest，直接用歷史價格做 what-if 分析。

Usage:
    python scripts/entry_delay_analysis.py
    python scripts/entry_delay_analysis.py --days 180
    python scripts/entry_delay_analysis.py --min-confidence 50
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn

_console = Console()


def _fetch_long_signals(days: int, min_confidence: int) -> list[dict]:
    """Fetch all LONG signals from DB."""
    from taiwan_stock_agent.infrastructure.db import init_pool, get_connection
    init_pool()

    query = """
        SELECT signal_id, ticker, signal_date, confidence_score, action, entry_price
        FROM signal_outcomes
        WHERE signal_date >= CURRENT_DATE - (%s * INTERVAL '1 day')
          AND action = 'LONG'
          AND halt_flag = FALSE
          AND confidence_score >= %s
        ORDER BY signal_date, ticker
    """
    rows = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, [days, min_confidence])
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                rows.append(dict(zip(cols, row)))
    return rows


def _fetch_ohlcv_batch(tickers: set[str], date_from: date, date_to: date) -> dict[str, dict[date, float]]:
    """Fetch OHLCV for all tickers, return {ticker: {date: close}}."""
    from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
    fm = FinMindClient()

    result: dict[str, dict[date, float]] = {}
    ticker_list = sorted(tickers)

    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.fields[ticker]}[/cyan]"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task("ohlcv", total=len(ticker_list), ticker="")
        for ticker in ticker_list:
            progress.update(task, ticker=ticker)
            try:
                df = fm.fetch_ohlcv(ticker, date_from, date_to)
                if not df.empty:
                    closes = {}
                    for _, row in df.iterrows():
                        closes[row["trade_date"]] = float(row["close"])
                    result[ticker] = closes
            except Exception:
                pass
            progress.advance(task)

    return result


def _simulate_delay(
    signals: list[dict],
    ohlcv: dict[str, dict[date, float]],
    delay: int,
) -> dict:
    """Simulate entry at signal_date + delay trading days.

    Returns stats: n, win_1d, win_3d, win_5d, avg_ret_1d/3d/5d, avg_entry_improvement
    """
    wins_1d = 0
    wins_3d = 0
    wins_5d = 0
    rets_1d: list[float] = []
    rets_3d: list[float] = []
    rets_5d: list[float] = []
    entry_improvements: list[float] = []  # vs delay=0 entry
    n = 0

    for sig in signals:
        ticker = sig["ticker"]
        closes = ohlcv.get(ticker, {})
        if not closes:
            continue

        trading_days = sorted(closes.keys())
        sig_date = sig["signal_date"]
        if isinstance(sig_date, str):
            sig_date = date.fromisoformat(sig_date)

        if sig_date not in trading_days:
            continue
        sig_idx = trading_days.index(sig_date)

        # Entry at signal_date + delay
        entry_idx = sig_idx + delay
        if entry_idx >= len(trading_days):
            continue

        entry_price = closes[trading_days[entry_idx]]
        if entry_price <= 0:
            continue

        # Also get delay=0 entry for comparison
        d0_price = closes[trading_days[sig_idx]]

        # T+1, T+3, T+5 from entry
        def ret_at(n_days: int) -> float | None:
            idx = entry_idx + n_days
            if idx < len(trading_days):
                return (closes[trading_days[idx]] - entry_price) / entry_price
            return None

        r1 = ret_at(1)
        r3 = ret_at(3)
        r5 = ret_at(5)

        n += 1

        if d0_price > 0:
            entry_improvements.append((d0_price - entry_price) / d0_price)

        if r1 is not None:
            rets_1d.append(r1)
            if r1 > 0:
                wins_1d += 1
        if r3 is not None:
            rets_3d.append(r3)
            if r3 > 0:
                wins_3d += 1
        if r5 is not None:
            rets_5d.append(r5)
            if r5 > 0:
                wins_5d += 1

    return {
        "delay": delay,
        "n": n,
        "n_1d": len(rets_1d), "n_3d": len(rets_3d), "n_5d": len(rets_5d),
        "win_1d": wins_1d / len(rets_1d) if rets_1d else 0,
        "win_3d": wins_3d / len(rets_3d) if rets_3d else 0,
        "win_5d": wins_5d / len(rets_5d) if rets_5d else 0,
        "avg_ret_1d": sum(rets_1d) / len(rets_1d) if rets_1d else 0,
        "avg_ret_3d": sum(rets_3d) / len(rets_3d) if rets_3d else 0,
        "avg_ret_5d": sum(rets_5d) / len(rets_5d) if rets_5d else 0,
        "avg_entry_improvement": sum(entry_improvements) / len(entry_improvements) if entry_improvements else 0,
        "median_entry_improvement": sorted(entry_improvements)[len(entry_improvements) // 2] if entry_improvements else 0,
    }


def _print_comparison(results: list[dict]) -> None:
    """Rich table comparing delay=0/1/2 results."""

    _console.print(Panel(
        "[bold cyan]T-2 策略驗證：Entry Delay 比較分析[/bold cyan]\n"
        "[dim]使用同一批 LONG 訊號，模擬不同進場時點的報酬差異[/dim]",
        border_style="cyan",
        padding=(0, 2),
    ))

    tbl = Table(
        title="Entry Delay 勝率 & 報酬",
        box=box.ROUNDED,
        header_style="bold white on dark_blue",
        show_lines=True,
    )
    tbl.add_column("進場方式", style="bold white", width=20)
    tbl.add_column("樣本數", justify="right", width=8)
    tbl.add_column("T+1 勝率", justify="right", width=10)
    tbl.add_column("T+3 勝率", justify="right", width=10)
    tbl.add_column("T+5 勝率", justify="right", width=10)
    tbl.add_column("T+1 平均報酬", justify="right", width=12)
    tbl.add_column("T+3 平均報酬", justify="right", width=12)
    tbl.add_column("T+5 平均報酬", justify="right", width=12)

    labels = {
        0: "D+0 當日收盤",
        1: "D+1 隔日收盤",
        2: "D+2 兩日後收盤",
    }

    baseline = results[0] if results else None

    for r in results:
        # Color code vs baseline
        def _color(val: float, base_val: float) -> str:
            if val > base_val + 0.005:
                return f"[green]{val:.1%}[/green]"
            elif val < base_val - 0.005:
                return f"[red]{val:.1%}[/red]"
            return f"{val:.1%}"

        def _color_ret(val: float, base_val: float) -> str:
            if val > base_val + 0.0005:
                return f"[green]{val:+.2%}[/green]"
            elif val < base_val - 0.0005:
                return f"[red]{val:+.2%}[/red]"
            return f"{val:+.2%}"

        b = baseline or r
        tbl.add_row(
            labels.get(r["delay"], f"D+{r['delay']}"),
            str(r["n"]),
            _color(r["win_1d"], b["win_1d"]),
            _color(r["win_3d"], b["win_3d"]),
            _color(r["win_5d"], b["win_5d"]),
            _color_ret(r["avg_ret_1d"], b["avg_ret_1d"]),
            _color_ret(r["avg_ret_3d"], b["avg_ret_3d"]),
            _color_ret(r["avg_ret_5d"], b["avg_ret_5d"]),
        )

    _console.print()
    _console.print(tbl)

    # Entry price improvement table
    _console.print()
    tbl2 = Table(
        title="進場價格改善（vs D+0 當日收盤）",
        box=box.ROUNDED,
        header_style="bold white on dark_green",
    )
    tbl2.add_column("進場方式", style="bold white", width=20)
    tbl2.add_column("平均價格改善", justify="right", width=15)
    tbl2.add_column("中位數改善", justify="right", width=15)

    for r in results:
        if r["delay"] == 0:
            tbl2.add_row(labels[0], "[dim]基準[/dim]", "[dim]基準[/dim]")
        else:
            avg_imp = r["avg_entry_improvement"]
            med_imp = r["median_entry_improvement"]
            avg_color = "green" if avg_imp > 0 else "red"
            med_color = "green" if med_imp > 0 else "red"
            tbl2.add_row(
                labels.get(r["delay"], f"D+{r['delay']}"),
                f"[{avg_color}]{avg_imp:+.2%}[/{avg_color}]",
                f"[{med_color}]{med_imp:+.2%}[/{med_color}]",
            )

    _console.print(tbl2)

    # Interpretation
    _console.print()
    _console.print("[bold]怎麼看這張表：[/bold]")
    _console.print("  [green]價格改善 > 0[/green]  → D+N 進場比 D+0 便宜（有利）")
    _console.print("  [green]勝率/報酬提升[/green]  → 延遲進場反而更好（通常是因為買在回檔低點）")
    _console.print("  [red]勝率/報酬下降[/red]    → 延遲進場錯過動能（追漲型訊號不適合延遲）")
    _console.print()
    _console.print("[bold]T-2 策略可行條件：[/bold]")
    _console.print("  D+2 的 T+5 勝率 ≈ D+0 的 T+5 勝率（動能持續 5 天以上）")
    _console.print("  且進場價格有明顯改善（>0.5%）")


def main() -> None:
    parser = argparse.ArgumentParser(description="T-2 策略驗證：Entry Delay 比較分析")
    parser.add_argument("--days", type=int, default=180, help="分析期間（天數，預設 180）")
    parser.add_argument("--min-confidence", type=int, default=50, help="最低信心門檻（預設 50）")
    args = parser.parse_args()

    # 1. Fetch LONG signals from DB
    _console.print(f"[dim]從 DB 載入 LONG 訊號（{args.days} 天內，信心 ≥{args.min_confidence}）...[/dim]")
    signals = _fetch_long_signals(args.days, args.min_confidence)
    if not signals:
        _console.print("[red]DB 中無符合條件的 LONG 訊號。請先執行 make backtest[/red]")
        return

    _console.print(f"[dim]找到 {len(signals)} 筆 LONG 訊號[/dim]")

    # 2. Fetch OHLCV for all tickers
    tickers = {s["ticker"] for s in signals}
    dates = [s["signal_date"] for s in signals]
    min_date = min(dates) - timedelta(days=5)
    max_date = max(dates) + timedelta(days=15)  # need T+5 from delay=2

    _console.print(f"[dim]載入 {len(tickers)} 支股票的 OHLCV ({min_date} ~ {max_date})...[/dim]")
    ohlcv = _fetch_ohlcv_batch(tickers, min_date, max_date)
    _console.print(f"[dim]OHLCV 載入完成：{len(ohlcv)}/{len(tickers)} 支有資料[/dim]")

    # 3. Simulate delay=0, 1, 2
    results = []
    for delay in (0, 1, 2):
        _console.print(f"[dim]模擬 entry_delay={delay}...[/dim]")
        r = _simulate_delay(signals, ohlcv, delay)
        results.append(r)

    # 4. Print comparison
    _console.print()
    _print_comparison(results)


if __name__ == "__main__":
    main()
