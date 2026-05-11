"""Daily industry/concept heat scanner.

Usage:
    python scripts/heat_scan.py                # use cached OHLCV if available
    python scripts/heat_scan.py --refresh      # force re-download
    python scripts/heat_scan.py --date 2026-05-08

Outputs: data/market_heat/heat_YYYY-MM-DD.json + Rich console report.
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel

from taiwan_stock_agent.domain.market_heat import (
    compute_market_heat, load_prior_5d_ranks, save_heat_snapshot, MarketHeat,
)
from taiwan_stock_agent.domain.models import DailyOHLCV

# Reuse downloader from surge_backtest
from surge_backtest import (
    _load_industry_map, download_all,
)

_console = Console()
_ROOT = Path(__file__).resolve().parents[1]
_HEAT_DIR = _ROOT / "data" / "market_heat"


def _trim_bars_up_to(
    all_bars_dict: dict[str, dict[date, DailyOHLCV]],
    target_date: date,
) -> dict[str, list[DailyOHLCV]]:
    """Return ticker → sorted bar list ending at target_date."""
    out: dict[str, list[DailyOHLCV]] = {}
    for ticker, dbars in all_bars_dict.items():
        bars = [b for d, b in sorted(dbars.items()) if d <= target_date]
        if bars:
            out[ticker] = bars
    return out


def _format_heat_report(heat: MarketHeat) -> None:
    _console.print()
    state_color = {
        "broad_rally": "bold green",
        "narrow_leadership": "yellow",
        "mixed": "cyan",
        "broad_selloff": "bold red",
    }.get(heat.market_state, "white")
    state_zh = {
        "broad_rally": "全面多頭",
        "narrow_leadership": "窄幅領漲",
        "mixed": "震盪整理",
        "broad_selloff": "全面空頭",
    }.get(heat.market_state, heat.market_state)

    _console.print(Panel.fit(
        f"[bold]Market Heat Snapshot[/bold]\n\n"
        f"日期: [cyan]{heat.snapshot_date}[/cyan]\n"
        f"市場狀態: [{state_color}]{state_zh}[/{state_color}]\n"
        f"整體廣度 (% above MA20): [yellow]{heat.market_breadth:.1f}%[/yellow]\n"
        f"熱產業 (rank_pct ≥ 80): {len(heat.hot_industries)}\n"
        f"輪入產業 (rank↑≥3): {len(heat.rotating_up)}\n"
        f"加速產業: {len(heat.accelerating)}",
        title="總覽", box=box.ROUNDED,
    ))

    # Hot industries table
    sorted_inds = sorted(heat.industries.values(), key=lambda x: x.rank_5d)

    t = Table(title="產業熱度排行（按 5d 動量）", box=box.ROUNDED)
    t.add_column("Rank", justify="right")
    t.add_column("產業")
    t.add_column("1d%", justify="right")
    t.add_column("5d%", justify="right")
    t.add_column("20d%", justify="right")
    t.add_column("MA20廣度", justify="right")
    t.add_column("加速度", justify="right")
    t.add_column("輪動", justify="right")
    t.add_column("領頭羊")

    for ih in sorted_inds[:15]:
        # Color rank
        if ih.rank_pct >= 80:
            rank_str = f"[bold red]{ih.rank_5d}[/bold red]"
        elif ih.rank_pct >= 60:
            rank_str = f"[yellow]{ih.rank_5d}[/yellow]"
        else:
            rank_str = str(ih.rank_5d)

        # Color rotation
        if ih.rank_5d_change <= -3:
            rot = f"[green]↑{abs(ih.rank_5d_change)}[/green]"
        elif ih.rank_5d_change >= 3:
            rot = f"[red]↓{ih.rank_5d_change}[/red]"
        else:
            rot = "—"

        # Acceleration color
        accel_color = "green" if ih.acceleration_pct > 0.5 else ("red" if ih.acceleration_pct < -0.5 else "white")

        leaders_str = " ".join(
            f"{l}({c:+.1f})" for l, c in zip(ih.leaders[:2], ih.leader_chgs[:2])
        )

        t.add_row(
            rank_str, ih.industry,
            f"{ih.ret_1d_pct:+.2f}",
            f"[bold]{ih.ret_5d_pct:+.2f}[/bold]",
            f"{ih.ret_20d_pct:+.2f}",
            f"{ih.breadth_above_ma20_pct:.0f}%",
            f"[{accel_color}]{ih.acceleration_pct:+.2f}[/{accel_color}]",
            rot, leaders_str,
        )
    _console.print(t)

    # Cold industries
    if heat.cold_industries:
        cold_table = Table(title="冷產業（避免追訊號）", box=box.ROUNDED)
        cold_table.add_column("產業")
        cold_table.add_column("5d%", justify="right")
        cold_table.add_column("廣度", justify="right")
        cold_inds_sorted = sorted(
            [heat.industries[i] for i in heat.cold_industries if i in heat.industries],
            key=lambda x: x.ret_5d_pct,
        )
        for ih in cold_inds_sorted[:8]:
            cold_table.add_row(
                ih.industry,
                f"[red]{ih.ret_5d_pct:+.2f}[/red]",
                f"{ih.breadth_above_ma20_pct:.0f}%",
            )
        _console.print(cold_table)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: latest)")
    ap.add_argument("--refresh", action="store_true", help="Force re-download")
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    industry_map = _load_industry_map()
    if not industry_map:
        _console.print("[red]No industry map found.[/red]")
        return 1

    # Reuse cache from tight_base_backtest
    cache_path = Path("data") / f"_ohlcv_cache_{args.days}d.pkl"

    if cache_path.exists() and not args.refresh:
        _console.print(f"  Loading cached OHLCV from {cache_path}")
        with open(cache_path, "rb") as f:
            cached = pickle.load(f)
        all_bars_dict = cached["all_bars"]
    else:
        today = date.today()
        start = str(today - timedelta(days=args.days + 60))
        end = str(today + timedelta(days=1))
        tickers = list(industry_map.keys())
        _console.print(f"Downloading {len(tickers)} tickers…")
        all_bars_dict = download_all(tickers, start, end, workers=20)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump({"all_bars": all_bars_dict, "taiex_bars": []}, f)

    # Determine target date
    if args.date:
        target = datetime.fromisoformat(args.date).date()
    else:
        all_dates = sorted({d for dbs in all_bars_dict.values() for d in dbs})
        target = all_dates[-1] if all_dates else date.today()

    _console.print(f"  Snapshot date: [cyan]{target}[/cyan]")

    bars_up_to = _trim_bars_up_to(all_bars_dict, target)
    _console.print(f"  Tickers with data: {len(bars_up_to)}")

    prior_ranks = load_prior_5d_ranks(target, _HEAT_DIR)
    heat = compute_market_heat(bars_up_to, industry_map, target, prior_ranks)

    out_path = save_heat_snapshot(heat, _HEAT_DIR)
    _console.print(f"  Saved heat snapshot to {out_path}")

    _format_heat_report(heat)
    return 0


if __name__ == "__main__":
    sys.exit(main())
