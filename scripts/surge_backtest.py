"""3-month historical backtest for SurgeRadar.

Downloads OHLCV via yfinance per-ticker (thread-safe), runs SurgeRadar day-by-day,
settles T+5 and T+10 forward returns, then prints a factor lift report.

Usage:
    python scripts/surge_backtest.py                     # 90-day window, full universe
    python scripts/surge_backtest.py --days 60
    python scripts/surge_backtest.py --top N             # limit universe to N tickers
    python scripts/surge_backtest.py --output results.csv
    make surge-backtest
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import pandas as pd
import yfinance as yf
from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel
from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TextColumn, TimeElapsedColumn,
)

from taiwan_stock_agent.domain.models import DailyOHLCV
from taiwan_stock_agent.domain.surge_radar import SurgeRadar

_console = Console()
_ROOT = Path(__file__).resolve().parents[1]
_INDUSTRY_MAP_DIR = _ROOT / "data" / "watchlist_cache"


# ── Universe ───────────────────────────────────────────────────────────────

def _load_industry_map() -> dict[str, str]:
    files = sorted(_INDUSTRY_MAP_DIR.glob("industry_map_*.json"))
    if not files:
        return {}
    with open(files[-1]) as f:
        return json.load(f)


def _yf_symbol(ticker: str) -> str:
    """4-digit numeric: <4000 → TSE (.TW), ≥4000 → TPEx (.TWO)."""
    try:
        return f"{ticker}.TW" if int(ticker) < 4000 else f"{ticker}.TWO"
    except ValueError:
        return f"{ticker}.TW"


def _market(ticker: str) -> str:
    try:
        return "TSE" if int(ticker) < 4000 else "TPEx"
    except ValueError:
        return "TSE"


# ── Data download ──────────────────────────────────────────────────────────

def _download_one(ticker: str, start: str, end: str) -> tuple[str, list[DailyOHLCV]]:
    sym = _yf_symbol(ticker)
    try:
        df = yf.Ticker(sym).history(
            start=start, end=end, interval="1d",
            auto_adjust=True, actions=False,
        )
        if df.empty:
            return ticker, []
        df.index = pd.to_datetime(df.index).normalize()
        bars: list[DailyOHLCV] = []
        for idx, row in df.iterrows():
            try:
                bars.append(DailyOHLCV(
                    ticker=ticker,
                    trade_date=idx.date(),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row.get("Volume", 0)),
                ))
            except Exception:
                pass
        return ticker, bars
    except Exception:
        return ticker, []


def download_all(
    tickers: list[str], start: str, end: str, workers: int = 30
) -> dict[str, dict[date, DailyOHLCV]]:
    all_bars: dict[str, dict[date, DailyOHLCV]] = {}
    with Progress(
        SpinnerColumn(), BarColumn(), MofNCompleteColumn(),
        TextColumn("{task.description}"), TimeElapsedColumn(), console=_console,
    ) as prog:
        task = prog.add_task("Downloading OHLCV", total=len(tickers))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_download_one, t, start, end): t for t in tickers}
            for fut in as_completed(futs):
                ticker, bars = fut.result()
                prog.advance(task)
                if bars:
                    all_bars[ticker] = {b.trade_date: b for b in bars}
    loaded = len(all_bars)
    _console.print(f"  Loaded {loaded}/{len(tickers)} tickers with data")
    return all_bars


def download_taiex(start: str, end: str) -> list[DailyOHLCV]:
    df = yf.Ticker("^TWII").history(
        start=start, end=end, interval="1d",
        auto_adjust=True, actions=False,
    )
    if df.empty:
        return []
    df.index = pd.to_datetime(df.index).normalize()
    bars: list[DailyOHLCV] = []
    for idx, row in df.iterrows():
        try:
            bars.append(DailyOHLCV(
                ticker="TAIEX",
                trade_date=idx.date(),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=0,
            ))
        except Exception:
            pass
    return bars


# ── Industry rank ──────────────────────────────────────────────────────────

def build_industry_ranks(
    all_bars: dict[str, dict[date, DailyOHLCV]],
    industry_map: dict[str, str],
    trading_days: list[date],
) -> dict[date, dict[str, float]]:
    """Pre-compute industry rank percentile for every (day, ticker)."""
    _console.print("  Computing industry ranks…")
    result: dict[date, dict[str, float]] = {}

    for scan_date in trading_days:
        chg: dict[str, float] = {}
        for ticker, date_bars in all_bars.items():
            dates_sorted = sorted(date_bars)
            if scan_date not in date_bars:
                continue
            idx = dates_sorted.index(scan_date)
            if idx == 0:
                continue
            prev = date_bars[dates_sorted[idx - 1]]
            curr = date_bars[scan_date]
            if prev.close > 0:
                chg[ticker] = (curr.close / prev.close - 1) * 100

        ind_groups: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for t, c in chg.items():
            ind = industry_map.get(t)
            if ind:
                ind_groups[ind].append((t, c))

        day_ranks: dict[str, float] = {}
        for ind, members in ind_groups.items():
            vals = np.array([v for _, v in members])
            for t, v in members:
                day_ranks[t] = round(float(np.mean(vals <= v) * 100), 1)
        result[scan_date] = day_ranks

    return result


# ── Core backtest ──────────────────────────────────────────────────────────

def run_backtest(
    days: int = 90,
    min_score: int = 0,
    top_n: int | None = None,
) -> list[dict]:
    today = date.today()
    # Extra lookback for 60-bar history + 70-bar TAIEX history
    lookback_extra = 120
    start_date = str(today - timedelta(days=days + lookback_extra))
    end_date = str(today + timedelta(days=1))

    industry_map = _load_industry_map()
    if not industry_map:
        _console.print("[red]No industry map found.[/red]")
        return []

    tickers = list(industry_map.keys())
    if top_n:
        tickers = tickers[:top_n]
    _console.print(f"Universe: {len(tickers)} tickers  |  window: {days}d")

    all_bars = download_all(tickers, start_date, end_date, workers=40)
    taiex_bars = download_taiex(start_date, end_date)
    taiex_dict: dict[date, DailyOHLCV] = {b.trade_date: b for b in taiex_bars}
    taiex_dates_sorted = sorted(taiex_dict)
    _console.print(f"  TAIEX: {len(taiex_dates_sorted)} bars")

    window_start = today - timedelta(days=days)
    all_trading_days: list[date] = sorted({
        d for dbs in all_bars.values() for d in dbs
        if window_start <= d < today
    })
    _console.print(f"  Trading days in window: {len(all_trading_days)} ({window_start} to {today - timedelta(days=1)})")

    # Pre-compute industry ranks
    ind_ranks = build_industry_ranks(all_bars, industry_map, all_trading_days)

    eng = SurgeRadar()
    signals: list[dict] = []

    with Progress(
        SpinnerColumn(), BarColumn(), MofNCompleteColumn(),
        TextColumn("{task.description}"), TimeElapsedColumn(), console=_console,
    ) as prog:
        task = prog.add_task("Scanning", total=len(all_trading_days))
        for scan_date in all_trading_days:
            prog.update(task, description=f"Scanning {scan_date}", advance=1)

            # Build TAIEX history (up to 70 bars before this date)
            taiex_idx = [i for i, d in enumerate(taiex_dates_sorted) if d <= scan_date]
            if len(taiex_idx) < 70:
                continue
            taiex_history = [taiex_dict[taiex_dates_sorted[i]] for i in taiex_idx[-70:]]

            # TAIEX regime
            taiex_regime = "neutral"
            if len(taiex_history) >= 60:
                ma20 = sum(b.close for b in taiex_history[-20:]) / 20
                ma60 = sum(b.close for b in taiex_history[-60:]) / 60
                if ma20 < ma60 * 0.98:
                    taiex_regime = "downtrend"

            day_ranks = ind_ranks.get(scan_date, {})

            for ticker, date_bars in all_bars.items():
                if scan_date not in date_bars:
                    continue
                dates_sorted = sorted(date_bars)
                idx = dates_sorted.index(scan_date)
                if idx < 60:
                    continue

                history = [date_bars[dates_sorted[i]] for i in range(idx - 60, idx)]
                today_bar = date_bars[scan_date]

                recent20 = [date_bars[dates_sorted[i]] for i in range(max(0, idx - 20), idx)]
                if not recent20:
                    continue
                turnover_20ma = sum(b.close * b.volume for b in recent20) / len(recent20)
                if turnover_20ma <= 0:
                    continue

                try:
                    result = eng.score_full(
                        ohlcv=today_bar,
                        history=history,
                        proxy=None,
                        taiex_regime=taiex_regime,
                        taiex_history=taiex_history,
                        turnover_20ma=turnover_20ma,
                        industry_rank_pct=day_ranks.get(ticker),
                    )
                except Exception:
                    continue

                if result is None:
                    continue
                if result["score"] < min_score:
                    continue

                signals.append({
                    "signal_date": scan_date,
                    "ticker": ticker,
                    "market": _market(ticker),
                    "industry": industry_map.get(ticker, ""),
                    "grade": result["grade"],
                    "score": result["score"],
                    "vol_ratio": result.get("vol_ratio"),
                    "day_chg_pct": result.get("day_chg_pct"),
                    "gap_pct": result.get("gap_pct"),
                    "close_strength": result.get("close_strength"),
                    "rsi": result.get("rsi"),
                    "close_price": today_bar.close,
                    "score_breakdown": result.get("score_breakdown", {}),
                    "flags": "|".join(result.get("flags", [])),
                })

    _console.print(f"\n[bold]Total signals found: {len(signals)}[/bold]")

    # Settle T+5 and T+10
    _console.print("Settling forward returns…")
    for sig in signals:
        ticker = sig["ticker"]
        sd = sig["signal_date"]
        close = sig["close_price"]
        if not close or ticker not in all_bars:
            sig["t5_return_pct"] = None
            sig["t10_return_pct"] = None
            continue
        date_bars = all_bars[ticker]
        dates_sorted = sorted(date_bars)
        try:
            base_idx = dates_sorted.index(sd)
        except ValueError:
            sig["t5_return_pct"] = None
            sig["t10_return_pct"] = None
            continue
        for n, col in [(5, "t5_return_pct"), (10, "t10_return_pct")]:
            fwd_idx = base_idx + n
            if fwd_idx < len(dates_sorted):
                fwd_price = date_bars[dates_sorted[fwd_idx]].close
                sig[col] = round((fwd_price / close - 1) * 100, 2) if fwd_price and close else None
            else:
                sig[col] = None  # not yet settled

    return signals


# ── Reporting ──────────────────────────────────────────────────────────────

def _wr(lst: list[float]) -> float:
    return round(sum(1 for v in lst if v > 0) / len(lst) * 100, 1) if lst else 0.0


def _avg(lst: list[float]) -> float:
    return round(sum(lst) / len(lst), 2) if lst else 0.0


def _med(lst: list[float]) -> float:
    if not lst:
        return 0.0
    s = sorted(lst)
    n = len(s)
    return round((s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2), 2)


def print_report(signals: list[dict], output_csv: str | None = None) -> None:
    if not signals:
        _console.print("[yellow]No signals to analyze.[/yellow]")
        return

    if output_csv:
        rows = [{k: v for k, v in s.items() if k != "score_breakdown"} for s in signals]
        pd.DataFrame(rows).to_csv(output_csv, index=False)
        _console.print(f"[green]Saved {len(signals)} signals → {output_csv}[/green]")

    s5  = [s for s in signals if s.get("t5_return_pct")  is not None]
    s10 = [s for s in signals if s.get("t10_return_pct") is not None]

    _console.print(Panel(
        f"Total signals: {len(signals)}   T+5 settled: {len(s5)}   T+10 settled: {len(s10)}",
        title="[bold]Surge Backtest Summary[/bold]",
    ))

    # ── Grade table ──
    grade_tbl = Table(
        "Grade", "Count", "T+5 WR%", "T+5 Avg%", "T+5 Med%",
        "T+10 WR%", "T+10 Avg%", box=box.ROUNDED, title="Grade Win Rates",
    )
    for grade in ("SURGE_ALPHA", "SURGE_BETA", "SURGE_GAMMA"):
        g5  = [s["t5_return_pct"]  for s in s5  if s["grade"] == grade]
        g10 = [s["t10_return_pct"] for s in s10 if s["grade"] == grade]
        cnt = sum(1 for s in signals if s["grade"] == grade)
        wr5 = _wr(g5); wr10 = _wr(g10)
        c5 = "green" if wr5 >= 55 else ("red" if wr5 < 45 else "yellow")
        c10 = "green" if wr10 >= 55 else ("red" if wr10 < 45 else "yellow")
        grade_tbl.add_row(
            grade, str(cnt),
            f"[{c5}]{wr5:.1f}%[/{c5}]", f"{_avg(g5):+.2f}%", f"{_med(g5):+.2f}%",
            f"[{c10}]{wr10:.1f}%[/{c10}]", f"{_avg(g10):+.2f}%",
        )
    _console.print(grade_tbl)

    # ── Score bucket table ──
    buckets = [(28, 40), (40, 55), (55, 65), (65, 75), (75, 101)]
    sbkt = Table(
        "Score", "Count", "T+5 WR%", "T+5 Avg%", "T+10 WR%", "T+10 Avg%",
        box=box.ROUNDED, title="Score Bucket Win Rates",
    )
    for lo, hi in buckets:
        g5  = [s["t5_return_pct"]  for s in s5  if lo <= s["score"] < hi]
        g10 = [s["t10_return_pct"] for s in s10 if lo <= s["score"] < hi]
        cnt = sum(1 for s in signals if lo <= s["score"] < hi)
        sbkt.add_row(
            f"{lo}–{hi-1}", str(cnt),
            f"{_wr(g5):.1f}%", f"{_avg(g5):+.2f}%",
            f"{_wr(g10):.1f}%", f"{_avg(g10):+.2f}%",
        )
    _console.print(sbkt)

    if not s5:
        return

    baseline_wr5 = _wr([s["t5_return_pct"] for s in s5])
    baseline_avg5 = _avg([s["t5_return_pct"] for s in s5])

    # ── Factor lift table ──
    def _bd(sig: dict, key: str) -> int:
        bd = sig.get("score_breakdown") or {}
        return int((bd if isinstance(bd, dict) else {}).get(key, 0))

    factor_checks: dict[str, callable] = {
        "vol_ratio_2-3x":    lambda s: 2.0 <= (s.get("vol_ratio") or 0) < 3.0,
        "vol_ratio_>3x":     lambda s: (s.get("vol_ratio") or 0) >= 3.0,
        "close_strong≥0.8":  lambda s: (s.get("close_strength") or 0) >= 0.8,
        "gap≥3pct":          lambda s: (s.get("gap_pct") or 0) >= 3.0,
        "rsi_healthy":       lambda s: 55 <= (s.get("rsi") or 0) <= 72,
        "pocket_pivot":      lambda s: _bd(s, "pocket_pivot") > 0,
        "breakout_20d":      lambda s: _bd(s, "breakout_20d") > 0,
        "breakaway_gap":     lambda s: _bd(s, "breakaway_gap") > 0,
        "rel_strength":      lambda s: _bd(s, "relative_strength") > 0,
        "bb_squeeze_break":  lambda s: "BB_SQUEEZE_BREAK" in (s.get("flags") or ""),
        "bb_squeeze_expand": lambda s: "BB_SQUEEZE_EXPAND" in (s.get("flags") or ""),
        "bb_squeeze_any":    lambda s: "BB_SQUEEZE" in (s.get("flags") or ""),
        "day_chg>5pct":      lambda s: (s.get("day_chg_pct") or 0) >= 5.0,
    }

    lift_rows: list[tuple] = []
    for name, fn in factor_checks.items():
        present = [s for s in s5 if fn(s)]
        absent  = [s for s in s5 if not fn(s)]
        if len(present) < 5:
            continue
        wr_p  = _wr([s["t5_return_pct"] for s in present])
        avg_p = _avg([s["t5_return_pct"] for s in present])
        wr_a  = _wr([s["t5_return_pct"] for s in absent])
        lift  = round(wr_p - baseline_wr5, 1)
        lift_rows.append((lift, name, len(present), wr_p, avg_p, wr_a))

    lift_rows.sort(reverse=True)
    lift_tbl = Table(
        "Factor", "N", "WR%(present)", "Avg%(present)", "WR%(absent)", "Lift vs base",
        box=box.ROUNDED,
        title=f"Factor Lift (T+5)  baseline WR={baseline_wr5:.1f}%  avg={baseline_avg5:+.2f}%",
    )
    for lift, name, n, wr_p, avg_p, wr_a in lift_rows:
        color = "green" if lift > 5 else ("red" if lift < -5 else "white")
        lift_tbl.add_row(
            name, str(n),
            f"{wr_p:.1f}%", f"{avg_p:+.2f}%", f"{wr_a:.1f}%",
            f"[{color}]{lift:+.1f}pp[/{color}]",
        )
    _console.print(lift_tbl)

    # ── Score threshold sweep ──
    _console.print("\n[bold yellow]Score Threshold Analysis (T+5)[/bold yellow]")
    for thr in [28, 40, 50, 55, 60, 65, 70]:
        above = [s["t5_return_pct"] for s in s5 if s["score"] >= thr]
        if above:
            _console.print(
                f"  Score≥{thr:2d}:  n={len(above):4d}  "
                f"WR={_wr(above):.1f}%  avg={_avg(above):+.2f}%  med={_med(above):+.2f}%"
            )

    # ── Suggested optimizations ──
    _console.print("\n[bold cyan]Optimization Suggestions[/bold cyan]")
    alpha_wr = _wr([s["t5_return_pct"] for s in s5 if s["grade"] == "SURGE_ALPHA"])
    gamma_wr = _wr([s["t5_return_pct"] for s in s5 if s["grade"] == "SURGE_GAMMA"])
    if alpha_wr < 50:
        _console.print("  [red]ALPHA 勝率 < 50% → 建議提高 grade_thresholds.SURGE_ALPHA[/red]")
    if gamma_wr < 45:
        _console.print("  [yellow]GAMMA 勝率偏低 → 建議提高最低顯示分數（SURGE_GAMMA 閾值）[/yellow]")

    bb_present = [s["t5_return_pct"] for s in s5 if "BB_SQUEEZE" in (s.get("flags") or "")]
    bb_absent  = [s["t5_return_pct"] for s in s5 if "BB_SQUEEZE" not in (s.get("flags") or "")]
    if bb_present and bb_absent:
        bb_lift = _wr(bb_present) - _wr(bb_absent)
        if abs(bb_lift) >= 5:
            direction = "增強" if bb_lift > 0 else "削減"
            _console.print(f"  BB Squeeze lift={bb_lift:+.1f}pp → 建議{direction} bb_squeeze 分數權重")

    # ── Top 5 best performing signals ──
    best = sorted(s5, key=lambda s: s["t5_return_pct"] or 0, reverse=True)[:10]
    top_tbl = Table(
        "Date", "Ticker", "Grade", "Score", "T+5%", "Vol×", "Gap%",
        box=box.SIMPLE, title="Top 10 Signals by T+5 Return",
    )
    for s in best:
        top_tbl.add_row(
            str(s["signal_date"]), s["ticker"], s["grade"], str(s["score"]),
            f"{s['t5_return_pct']:+.1f}%",
            f"{s.get('vol_ratio', 0):.1f}x",
            f"{s.get('gap_pct', 0):.1f}%",
        )
    _console.print(top_tbl)


# ── Entry ──────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days",       type=int, default=90)
    ap.add_argument("--min-score",  type=int, default=0)
    ap.add_argument("--top",        type=int, default=None, help="Limit to first N tickers (testing)")
    ap.add_argument("--output",     type=str, default=None)
    args = ap.parse_args()

    _console.rule("[bold]Surge Radar Historical Backtest[/bold]")
    signals = run_backtest(days=args.days, min_score=args.min_score, top_n=args.top)
    print_report(signals, output_csv=args.output)


if __name__ == "__main__":
    main()
