#!/usr/bin/env python3
"""
chip_loading_backtest.py  —  CHIP_LOADING 偵測器回測

偵測邏輯（等同 TCE._is_chip_loading()）：
  G5 fail  : twenty_day_high / sixty_day_high < 85%   (頭部壓制)
  G4 pass  : TAIEX regime != downtrend
  G1 pass  : close / twenty_day_high in [85%, 99%)
  chip strong:
    - foreign_consec ≥ 3 OR trust_consec ≥ 3
    - cumul_foreign_20d + cumul_trust_20d > 0

每個 signal 記錄觸發日 close，追蹤 D+3 / D+5 / D+10 報酬率。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import requests
import yfinance as yf
import pandas as pd
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

warnings.filterwarnings("ignore")

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))

_CACHE_DIR = _ROOT / "data" / "backtest_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

console = Console()

# ── 工具 ─────────────────────────────────────────────────────────────────────

def _trading_days(start: date, end: date) -> list[date]:
    """Return weekday dates (Mon-Fri) in range."""
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _yf_ticker(ticker: str) -> str:
    return f"{ticker}.TW" if len(ticker) == 4 and ticker.isdigit() else f"{ticker}.TWO"


# ── OHLCV ────────────────────────────────────────────────────────────────────

def _load_ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame | None:
    sym = _yf_ticker(ticker)
    try:
        df = yf.Ticker(sym).history(start=start, end=end, auto_adjust=True)
        if df.empty:
            sym2 = ticker + ".TWO"
            df = yf.Ticker(sym2).history(start=start, end=end, auto_adjust=True)
        if df.empty:
            return None
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    except Exception:
        return None


# ── T86 法人資料 ──────────────────────────────────────────────────────────────

def _t86_cache_path(d: date) -> Path:
    return _CACHE_DIR / f"t86_{d.isoformat()}.json"


def _fetch_t86_tse(d: date) -> dict[str, tuple[int, int]]:
    """Fetch TWSE T86 for all tickers on date d.
    Returns {ticker: (foreign_net, trust_net)} in shares.
    """
    cache = _t86_cache_path(d)
    if cache.exists():
        raw = json.loads(cache.read_text())
        return {k: tuple(v) for k, v in raw.items()}

    url = (
        f"https://www.twse.com.tw/rwd/zh/fund/T86"
        f"?response=json&date={d.strftime('%Y%m%d')}&selectType=ALLBUT0999"
    )
    try:
        resp = requests.get(url, timeout=10, verify=False)
        data = resp.json()
    except Exception:
        return {}

    if data.get("stat") != "OK":
        cache.write_text(json.dumps({}))
        return {}

    result: dict[str, tuple[int, int]] = {}
    for row in data.get("data", []):
        try:
            code = row[0].strip()
            # col 4 = 外資差, col 10 = 投信買, col 11 = 投信賣
            foreign_net = int(row[4].replace(",", "")) if row[4].strip() else 0
            trust_net = (
                int(row[10].replace(",", "")) - int(row[11].replace(",", ""))
                if row[10].strip() and row[11].strip()
                else 0
            )
            result[code] = (foreign_net, trust_net)
        except (IndexError, ValueError):
            continue

    cache.write_text(json.dumps(result))
    time.sleep(0.3)
    return result


# ── 從時序 T86 計算籌碼指標 ───────────────────────────────────────────────────

def _compute_chip_stats(
    ticker: str,
    analysis_date: date,
    t86_by_date: dict[date, dict[str, tuple[int, int]]],
) -> tuple[int, int, int, int] | None:
    """
    Returns (foreign_consec, trust_consec, cumul_foreign_20d, cumul_trust_20d)
    or None if no data.
    """
    dates = sorted(t86_by_date.keys())
    past = [d for d in dates if d <= analysis_date]
    if not past:
        return None

    foreign_series: list[int] = []
    trust_series: list[int] = []
    for d in past[-20:]:
        row = t86_by_date[d].get(ticker)
        if row:
            foreign_series.append(row[0])
            trust_series.append(row[1])
        else:
            foreign_series.append(0)
            trust_series.append(0)

    if not foreign_series:
        return None

    def _consec(series: list[int]) -> int:
        count = 0
        for v in reversed(series):
            if v > 0:
                count += 1
            else:
                break
        return count

    return (
        _consec(foreign_series),
        _consec(trust_series),
        sum(foreign_series),
        sum(trust_series),
    )


# ── 大盤 regime ───────────────────────────────────────────────────────────────

def _taiex_regime(taiex_df: pd.DataFrame, d: date) -> str:
    subset = taiex_df[taiex_df.index <= pd.Timestamp(d)]
    if len(subset) < 30:
        return "neutral"
    close = subset["Close"].iloc[-1]
    ma20 = subset["Close"].iloc[-20:].mean()
    ma60 = subset["Close"].iloc[-60:].mean() if len(subset) >= 60 else ma20
    if close > ma20 and ma20 > ma60:
        return "uptrend"
    if close < ma20 and ma20 < ma60:
        return "downtrend"
    return "neutral"


# ── 核心偵測 ──────────────────────────────────────────────────────────────────

def _chip_loading_fires(
    ticker: str,
    analysis_date: date,
    ohlcv: pd.DataFrame,
    t86_by_date: dict[date, dict[str, tuple[int, int]]],
    taiex_df: pd.DataFrame,
) -> bool:
    rows = ohlcv[ohlcv.index <= pd.Timestamp(analysis_date)]
    if len(rows) < 60:
        return False

    close = rows["Close"].iloc[-1]
    twenty_day_high = rows["High"].iloc[-20:].max()
    sixty_day_high = rows["High"].iloc[-60:].max()

    if twenty_day_high <= 0 or sixty_day_high <= 0:
        return False

    # G1: 85% ≤ close / 20D_high < 99%
    prox = close / twenty_day_high
    if not (0.85 <= prox < 0.99):
        return False

    # G5: 20D_high / 60D_high < 85%
    ratio_60d = twenty_day_high / sixty_day_high
    if ratio_60d >= 0.85:
        return False

    # G4: TAIEX not downtrend
    if _taiex_regime(taiex_df, analysis_date) == "downtrend":
        return False

    # Chip
    stats = _compute_chip_stats(ticker, analysis_date, t86_by_date)
    if stats is None:
        return False
    foreign_consec, trust_consec, cumul_foreign, cumul_trust = stats
    if foreign_consec < 3 and trust_consec < 3:
        return False
    if cumul_foreign + cumul_trust <= 0:
        return False

    return True


# ── Forward return ────────────────────────────────────────────────────────────

def _forward_return(
    ohlcv: pd.DataFrame, signal_date: date, holding_days: int
) -> float | None:
    future = ohlcv[ohlcv.index > pd.Timestamp(signal_date)]
    if len(future) < holding_days:
        return None
    entry = ohlcv[ohlcv.index <= pd.Timestamp(signal_date)]["Close"].iloc[-1]
    exit_ = future["Close"].iloc[holding_days - 1]
    return (exit_ - entry) / entry * 100


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="CHIP_LOADING backtest")
    parser.add_argument("--days", type=int, default=90, help="look-back days (default 90)")
    parser.add_argument("--workers", type=int, default=8, help="yfinance parallel workers")
    args = parser.parse_args()

    end_date = date.today()
    start_date = end_date - timedelta(days=args.days + 90)  # extra for 60D context

    console.print(Panel(
        f"[bold]CHIP_LOADING 回測[/bold]\n"
        f"訊號區間 {end_date - timedelta(days=args.days)} → {end_date}\n"
        f"(OHLCV 下載起始: {start_date})",
        style="cyan",
    ))

    # Load ticker universe
    watchlist_dir = _ROOT / "data" / "watchlist_cache"
    industry_map: dict[str, str] = {}
    for f in watchlist_dir.glob("industry_map_*.json"):
        try:
            industry_map.update(json.loads(f.read_text()))
        except Exception:
            pass
    if not industry_map:
        console.print("[red]找不到 industry_map，請先執行 make scan 建立快取[/red]")
        sys.exit(1)

    tickers = list(industry_map.keys())[:300]  # cap to 300 for speed
    console.print(f"[dim]Universe: {len(tickers)} 檔 (capped 300)[/dim]")

    # Download TAIEX
    console.print("[dim]下載大盤歷史...[/dim]")
    taiex_df = yf.Ticker("^TWII").history(
        start=start_date.isoformat(), end=end_date.isoformat(), auto_adjust=True
    )
    taiex_df.index = pd.to_datetime(taiex_df.index).tz_localize(None).normalize()

    # Download OHLCV in parallel
    ohlcv_map: dict[str, pd.DataFrame] = {}
    with Progress(
        SpinnerColumn(), TextColumn("{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(), console=console,
    ) as progress:
        task = progress.add_task("下載 OHLCV", total=len(tickers))
        for ticker in tickers:
            df = _load_ohlcv(ticker, start_date.isoformat(), end_date.isoformat())
            if df is not None and len(df) >= 60:
                ohlcv_map[ticker] = df
            progress.advance(task)
    console.print(f"[dim]有效 OHLCV: {len(ohlcv_map)} 檔[/dim]")

    # Download T86 by date (one call per date = all tickers)
    signal_days = _trading_days(end_date - timedelta(days=args.days), end_date)
    all_dates_needed = _trading_days(
        end_date - timedelta(days=args.days + 30), end_date
    )

    t86_by_date: dict[date, dict[str, tuple[int, int]]] = {}
    with Progress(
        SpinnerColumn(), TextColumn("{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(), console=console,
    ) as progress:
        task = progress.add_task("下載 T86 法人", total=len(all_dates_needed))
        for d in all_dates_needed:
            t86_by_date[d] = _fetch_t86_tse(d)
            progress.advance(task)

    # Scan for CHIP_LOADING signals
    signals: list[dict] = []
    console.print("[dim]掃描 CHIP_LOADING 訊號...[/dim]")
    for ticker, df in ohlcv_map.items():
        for d in signal_days:
            if pd.Timestamp(d) not in df.index:
                continue
            try:
                fires = _chip_loading_fires(ticker, d, df, t86_by_date, taiex_df)
            except Exception:
                continue
            if fires:
                r3 = _forward_return(df, d, 3)
                r5 = _forward_return(df, d, 5)
                r10 = _forward_return(df, d, 10)
                signals.append({
                    "ticker": ticker,
                    "date": d.isoformat(),
                    "r3": r3,
                    "r5": r5,
                    "r10": r10,
                })

    if not signals:
        console.print("[yellow]無符合 CHIP_LOADING 條件的歷史訊號[/yellow]")
        return

    # Statistics
    def _stats(key: str) -> tuple[int, float, float]:
        vals = [s[key] for s in signals if s[key] is not None]
        if not vals:
            return 0, 0.0, 0.0
        win = sum(1 for v in vals if v > 0)
        return len(vals), win / len(vals) * 100, sum(vals) / len(vals)

    n3, wr3, avg3 = _stats("r3")
    n5, wr5, avg5 = _stats("r5")
    n10, wr10, avg10 = _stats("r10")

    table = Table(title=f"CHIP_LOADING 回測結果  (訊號 {len(signals)} 筆)", box=box.ROUNDED)
    table.add_column("持有天數", style="bold")
    table.add_column("樣本數", justify="right")
    table.add_column("勝率", justify="right")
    table.add_column("平均報酬", justify="right")
    table.add_row("D+3", str(n3), f"{wr3:.1f}%",
                  f"[green]+{avg3:.2f}%[/green]" if avg3 >= 0 else f"[red]{avg3:.2f}%[/red]")
    table.add_row("D+5", str(n5), f"{wr5:.1f}%",
                  f"[green]+{avg5:.2f}%[/green]" if avg5 >= 0 else f"[red]{avg5:.2f}%[/red]")
    table.add_row("D+10", str(n10), f"{wr10:.1f}%",
                  f"[green]+{avg10:.2f}%[/green]" if avg10 >= 0 else f"[red]{avg10:.2f}%[/red]")
    console.print(table)

    # Top signals
    top = sorted(
        [s for s in signals if s["r10"] is not None],
        key=lambda x: x["r10"],
        reverse=True,
    )[:10]
    if top:
        top_table = Table(title="最佳 D+10 訊號 (Top 10)", box=box.SIMPLE)
        top_table.add_column("Ticker")
        top_table.add_column("觸發日")
        top_table.add_column("D+3")
        top_table.add_column("D+5")
        top_table.add_column("D+10")
        for s in top:
            fmt = lambda v: f"+{v:.1f}%" if v and v >= 0 else (f"{v:.1f}%" if v else "-")
            top_table.add_row(s["ticker"], s["date"], fmt(s["r3"]), fmt(s["r5"]), fmt(s["r10"]))
        console.print(top_table)

    console.print(
        Panel(
            f"[bold]結論[/bold]\n"
            f"訊號數: {len(signals)}  ·  "
            f"D+5 勝率: {wr5:.1f}%  ·  平均: {avg5:+.2f}%  ·  "
            f"D+10 勝率: {wr10:.1f}%  ·  平均: {avg10:+.2f}%",
            style="green" if avg5 > 0 else "yellow",
        )
    )


if __name__ == "__main__":
    main()
