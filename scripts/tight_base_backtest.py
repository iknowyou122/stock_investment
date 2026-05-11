"""TIGHT_BASE pre-breakout detector + 3-month backtest.

Pattern: 5-day close range < 3%, mild volume contraction (0.6-1.1x), positioned
near 20D high (top 30%), not yet broken out. Tests whether this D-1 pattern
predicts D-day Surge signals.

Usage:
    python scripts/tight_base_backtest.py
    python scripts/tight_base_backtest.py --days 90 --output tight_base.csv
"""
from __future__ import annotations

import argparse
import sys
import warnings
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd
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

# Reuse helpers from surge_backtest
from surge_backtest import (
    _load_industry_map, _market, download_all, download_taiex,
)

_console = Console()


# ── TIGHT_BASE detector ─────────────────────────────────────────────────────

def detect_tight_base(
    today: DailyOHLCV,
    history: list[DailyOHLCV],
    base_days: int = 5,
    range_pct_max: float = 0.03,
    vol_ratio_min: float = 0.6,
    vol_ratio_max: float = 1.1,
    position_min: float = 0.70,
) -> dict | None:
    """Return detection dict if pattern matches, else None.

    today: the candidate D-1 bar (we'll check D-day breakout next).
    history: at least 30 prior bars.
    """
    if len(history) < 25:
        return None

    sorted_h = sorted(history, key=lambda x: x.trade_date)
    base = sorted_h[-(base_days - 1):] + [today]  # last 5 bars including today
    if len(base) < base_days:
        return None

    closes = [b.close for b in base]
    mean_close = sum(closes) / len(closes)
    range_pct = (max(closes) - min(closes)) / mean_close if mean_close else 1.0

    if range_pct >= range_pct_max:
        return None

    # Volume contraction: avg base vol / 20MA in 0.6-1.1
    last20 = sorted_h[-20:]
    avg20_vol = sum(b.volume for b in last20) / 20 if last20 else 0
    if avg20_vol <= 0:
        return None
    base_avg_vol = sum(b.volume for b in base) / len(base)
    vol_ratio = base_avg_vol / avg20_vol

    if not (vol_ratio_min <= vol_ratio <= vol_ratio_max):
        return None

    # Position: close in top 30% of 20-day range
    last20_with_today = last20 + [today]
    hi20 = max(b.high for b in last20_with_today)
    lo20 = min(b.low for b in last20_with_today)
    if hi20 <= lo20:
        return None
    position = (today.close - lo20) / (hi20 - lo20)

    if position < position_min:
        return None

    # Not yet broken out: today's close <= last 20D high (excluding today)
    hi20_prior = max(b.high for b in last20)
    if today.close > hi20_prior * 1.005:  # 0.5% buffer
        return None

    # ATR-style: avg true range during base
    atr_pcts = []
    for i, b in enumerate(base):
        prev_close = closes[i - 1] if i > 0 else b.close
        tr = max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close))
        atr_pcts.append(tr / b.close if b.close else 0)
    avg_atr_pct = sum(atr_pcts) / len(atr_pcts)

    return {
        "range_pct": range_pct,
        "vol_ratio": vol_ratio,
        "position": position,
        "avg_atr_pct": avg_atr_pct,
        "mean_close": mean_close,
    }


# ── Backtest ────────────────────────────────────────────────────────────────

def run_backtest(days: int = 90) -> tuple[list[dict], dict]:
    today = date.today()
    lookback_extra = 120
    start_date = str(today - timedelta(days=days + lookback_extra))
    end_date = str(today + timedelta(days=1))

    industry_map = _load_industry_map()
    if not industry_map:
        _console.print("[red]No industry map found.[/red]")
        return [], {}

    tickers = list(industry_map.keys())
    _console.print(f"Universe: {len(tickers)} tickers  |  window: {days}d")

    # Cache to avoid re-downloading on retry
    import pickle, time
    cache_path = Path("data") / f"_ohlcv_cache_{days}d.pkl"

    if cache_path.exists():
        _console.print(f"  Loading cached data from {cache_path}")
        with open(cache_path, "rb") as f:
            cached = pickle.load(f)
        all_bars = cached["all_bars"]
        taiex_bars = cached["taiex_bars"]
        _console.print(f"  Loaded {len(all_bars)} tickers + {len(taiex_bars)} TAIEX bars from cache")
    else:
        # TAIEX first (small, less likely to hit rate limit)
        taiex_bars = []
        for attempt in range(5):
            try:
                taiex_bars = download_taiex(start_date, end_date)
                if taiex_bars:
                    break
            except Exception as e:
                _console.print(f"  TAIEX attempt {attempt+1}: {e}")
            time.sleep(30 * (attempt + 1))
        if not taiex_bars:
            _console.print("[red]Failed to fetch TAIEX[/red]")
            return [], {}

        all_bars = download_all(tickers, start_date, end_date, workers=20)
        # Save cache
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump({"all_bars": all_bars, "taiex_bars": taiex_bars}, f)
        _console.print(f"  Cached to {cache_path}")
    taiex_dict = {b.trade_date: b for b in taiex_bars}
    taiex_dates_sorted = sorted(taiex_dict)
    _console.print(f"  TAIEX: {len(taiex_dates_sorted)} bars")

    window_start = today - timedelta(days=days)
    all_trading_days = sorted({
        d for dbs in all_bars.values() for d in dbs
        if window_start <= d < today
    })
    _console.print(f"  Trading days: {len(all_trading_days)}")

    eng = SurgeRadar()
    detections: list[dict] = []

    with Progress(
        SpinnerColumn(), BarColumn(), MofNCompleteColumn(),
        TextColumn("{task.description}"), TimeElapsedColumn(), console=_console,
    ) as prog:
        task = prog.add_task("Scanning TIGHT_BASE", total=len(all_trading_days))
        for scan_date in all_trading_days:
            prog.update(task, description=f"Scanning {scan_date}", advance=1)

            # TAIEX regime for Surge gate check
            taiex_idx = [i for i, d in enumerate(taiex_dates_sorted) if d <= scan_date]
            if len(taiex_idx) < 70:
                continue
            taiex_history = [taiex_dict[taiex_dates_sorted[i]] for i in taiex_idx[-70:]]
            taiex_regime = "neutral"
            if len(taiex_history) >= 60:
                ma20 = sum(b.close for b in taiex_history[-20:]) / 20
                ma60 = sum(b.close for b in taiex_history[-60:]) / 60
                if ma20 < ma60 * 0.98:
                    taiex_regime = "downtrend"

            for ticker, date_bars in all_bars.items():
                if scan_date not in date_bars:
                    continue
                dates_sorted = sorted(date_bars)
                idx = dates_sorted.index(scan_date)
                if idx < 30:
                    continue

                history = [date_bars[dates_sorted[i]] for i in range(idx - 30, idx)]
                d_minus_1 = date_bars[scan_date]

                tb = detect_tight_base(d_minus_1, history)
                if tb is None:
                    continue

                # Check D-day (next trading day) Surge
                if idx + 1 >= len(dates_sorted):
                    continue
                d_day_bar = date_bars[dates_sorted[idx + 1]]

                # Compute turnover_20ma for Surge gate
                recent20 = [date_bars[dates_sorted[i]] for i in range(max(0, idx - 19), idx + 1)]
                turnover_20ma = sum(b.close * b.volume for b in recent20) / len(recent20)

                # Run Surge on D-day using D-1 + earlier as history
                history_for_dday = [date_bars[dates_sorted[i]] for i in range(idx - 59 if idx >= 59 else 0, idx + 1)]
                try:
                    result = eng.score_full(
                        ohlcv=d_day_bar,
                        history=history_for_dday,
                        proxy=None,
                        taiex_regime=taiex_regime,
                        taiex_history=taiex_history + [taiex_dict.get(dates_sorted[idx + 1])] if dates_sorted[idx + 1] in taiex_dict else taiex_history,
                        turnover_20ma=turnover_20ma,
                        industry_rank_pct=None,
                    )
                except Exception:
                    result = None

                surge_hit = result is not None
                d_day_chg = (d_day_bar.close / d_minus_1.close - 1) * 100 if d_minus_1.close else 0

                # Also check D+2, D+3 surge
                surge_hit_d2 = False
                surge_hit_d3 = False
                for k_off in (2, 3):
                    if idx + k_off >= len(dates_sorted):
                        break
                    k_day_bar = date_bars[dates_sorted[idx + k_off]]
                    k_history = [date_bars[dates_sorted[i]] for i in range(max(0, idx + k_off - 60), idx + k_off)]
                    k_recent20 = [date_bars[dates_sorted[i]] for i in range(max(0, idx + k_off - 20), idx + k_off)]
                    k_turnover = sum(b.close * b.volume for b in k_recent20) / len(k_recent20)
                    try:
                        k_result = eng.score_full(
                            ohlcv=k_day_bar, history=k_history, proxy=None,
                            taiex_regime=taiex_regime,
                            taiex_history=taiex_history,
                            turnover_20ma=k_turnover,
                            industry_rank_pct=None,
                        )
                        if k_result is not None:
                            if k_off == 2:
                                surge_hit_d2 = True
                            else:
                                surge_hit_d3 = True
                    except Exception:
                        pass

                detections.append({
                    "d_minus_1_date": scan_date,
                    "ticker": ticker,
                    "market": _market(ticker),
                    "industry": industry_map.get(ticker, ""),
                    "range_pct": round(tb["range_pct"] * 100, 2),
                    "vol_ratio": round(tb["vol_ratio"], 2),
                    "position": round(tb["position"] * 100, 1),
                    "atr_pct": round(tb["avg_atr_pct"] * 100, 2),
                    "close_d1": d_minus_1.close,
                    "close_d0": d_day_bar.close,
                    "d_day_chg_pct": round(d_day_chg, 2),
                    "surge_hit_d1": surge_hit,
                    "surge_hit_d2": surge_hit_d2,
                    "surge_hit_d3": surge_hit_d3,
                    "surge_grade": result.get("grade") if result else "",
                    "surge_score": result.get("score") if result else 0,
                })

    # Aggregate stats
    total = len(detections)
    hit_d1 = sum(1 for d in detections if d["surge_hit_d1"])
    hit_d3_window = sum(1 for d in detections if d["surge_hit_d1"] or d["surge_hit_d2"] or d["surge_hit_d3"])
    avg_chg = sum(d["d_day_chg_pct"] for d in detections) / total if total else 0

    stats = {
        "total_tight_base": total,
        "surge_hit_d1": hit_d1,
        "surge_hit_d1_pct": round(hit_d1 / total * 100, 1) if total else 0,
        "surge_hit_d1_to_d3": hit_d3_window,
        "surge_hit_d1_to_d3_pct": round(hit_d3_window / total * 100, 1) if total else 0,
        "avg_d_day_chg_pct": round(avg_chg, 2),
    }
    return detections, stats


def print_report(detections: list[dict], stats: dict) -> None:
    _console.print()
    _console.print(Panel.fit(
        f"[bold]TIGHT_BASE Backtest Report[/bold]\n\n"
        f"Total TIGHT_BASE detections:  [cyan]{stats['total_tight_base']}[/cyan]\n"
        f"D+1 Surge hit:                 [green]{stats['surge_hit_d1']}[/green] ([green]{stats['surge_hit_d1_pct']}%[/green])\n"
        f"D+1~D+3 Surge hit:             [green]{stats['surge_hit_d1_to_d3']}[/green] ([green]{stats['surge_hit_d1_to_d3_pct']}%[/green])\n"
        f"Avg D-day price change:        [yellow]{stats['avg_d_day_chg_pct']:+.2f}%[/yellow]",
        title="Summary", box=box.ROUNDED,
    ))

    # Hit rate by range tightness
    _console.print()
    t = Table(title="Hit Rate by Range Tightness", box=box.ROUNDED)
    t.add_column("Range %"); t.add_column("Count", justify="right")
    t.add_column("D+1 Hit %", justify="right"); t.add_column("Avg D-day %", justify="right")
    buckets = [(0, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 3.0)]
    for lo, hi in buckets:
        bucket = [d for d in detections if lo <= d["range_pct"] < hi]
        if not bucket:
            continue
        hits = sum(1 for d in bucket if d["surge_hit_d1"])
        avg = sum(d["d_day_chg_pct"] for d in bucket) / len(bucket)
        t.add_row(
            f"{lo:.1f}–{hi:.1f}%", str(len(bucket)),
            f"{hits/len(bucket)*100:.1f}%", f"{avg:+.2f}%",
        )
    _console.print(t)

    # Top 10 winners
    _console.print()
    winners = sorted(
        [d for d in detections if d["surge_hit_d1"]],
        key=lambda x: x["d_day_chg_pct"], reverse=True,
    )[:15]
    t2 = Table(title="Top 15 TIGHT_BASE → D+1 Surge Hits", box=box.ROUNDED)
    t2.add_column("D-1 日期"); t2.add_column("代號"); t2.add_column("產業")
    t2.add_column("Range%", justify="right"); t2.add_column("量比", justify="right")
    t2.add_column("位置%", justify="right"); t2.add_column("收 D-1"); t2.add_column("收 D-day")
    t2.add_column("D-day %", justify="right"); t2.add_column("等級")
    for w in winners:
        t2.add_row(
            str(w["d_minus_1_date"]), w["ticker"], w["industry"][:6],
            f"{w['range_pct']:.1f}", f"{w['vol_ratio']:.2f}",
            f"{w['position']:.0f}",
            f"{w['close_d1']:.1f}", f"{w['close_d0']:.1f}",
            f"[green]{w['d_day_chg_pct']:+.1f}[/green]", w["surge_grade"],
        )
    _console.print(t2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--output", type=str, default="data/tight_base_backtest.csv")
    args = ap.parse_args()

    detections, stats = run_backtest(days=args.days)
    if not detections:
        _console.print("[red]No detections.[/red]")
        return 1

    df = pd.DataFrame(detections)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    _console.print(f"\n[green]Saved {len(detections)} detections to {out}[/green]")

    print_report(detections, stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
