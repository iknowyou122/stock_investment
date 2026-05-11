"""TIGHT_BASE v2 — heat-aware backtest.

v1 baseline: 2.2% D+1 hit rate (1067 detections).
v2 adds:
  - Industry heat pre-filter (drop cold)
  - Industry heat rank bonus to detection score
  - Concept basket membership bonus
  - International tailwind bonus (latest day only)

Compare v1 vs v2 hit rates on same 90-day window.
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import warnings
from collections import defaultdict
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
from taiwan_stock_agent.domain.market_heat import compute_market_heat
from taiwan_stock_agent.domain.concept_heat import (
    load_concepts, compute_concept_heat, get_concept_membership,
)

from surge_backtest import _load_industry_map, _market
from tight_base_backtest import detect_tight_base

_console = Console()
_ROOT = Path(__file__).resolve().parents[1]


def run_v2_backtest(days: int = 90) -> tuple[list[dict], dict]:
    today = date.today()

    industry_map = _load_industry_map()
    if not industry_map:
        _console.print("[red]No industry map found.[/red]")
        return [], {}

    cache_path = Path("data") / f"_ohlcv_cache_{days}d.pkl"
    if not cache_path.exists():
        _console.print(f"[red]Cache not found: {cache_path}[/red]")
        _console.print("[yellow]Run tight_base_backtest.py first to populate cache.[/yellow]")
        return [], {}

    _console.print(f"  Loading cached OHLCV from {cache_path}")
    with open(cache_path, "rb") as f:
        cached = pickle.load(f)
    all_bars = cached["all_bars"]
    taiex_bars = cached["taiex_bars"]
    taiex_dict = {b.trade_date: b for b in taiex_bars}
    taiex_dates_sorted = sorted(taiex_dict)
    _console.print(f"  Loaded {len(all_bars)} tickers, TAIEX {len(taiex_dict)} bars")

    concepts_def = load_concepts(Path("config/concepts.json"))
    _console.print(f"  Concepts: {len(concepts_def)}")

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
        task = prog.add_task("Scanning v2", total=len(all_trading_days))

        for scan_date in all_trading_days:
            prog.update(task, description=f"Scanning {scan_date}", advance=1)

            # Build market heat for this date
            bars_up_to_this = {}
            for ticker, dbars in all_bars.items():
                sub = [b for d, b in sorted(dbars.items()) if d <= scan_date]
                if len(sub) >= 21:
                    bars_up_to_this[ticker] = sub

            heat = compute_market_heat(bars_up_to_this, industry_map, scan_date)
            concept_snap = compute_concept_heat(bars_up_to_this, concepts_def, scan_date)

            # Build heat lookup
            ind_heat = {ih.industry: ih for ih in heat.industries.values()}

            # TAIEX
            taiex_idx = [i for i, d in enumerate(taiex_dates_sorted) if d <= scan_date]
            if len(taiex_idx) < 70:
                continue
            taiex_history = [taiex_dict[taiex_dates_sorted[i]] for i in taiex_idx[-70:]]
            taiex_regime = "neutral"
            ma20 = sum(b.close for b in taiex_history[-20:]) / 20
            ma60 = sum(b.close for b in taiex_history[-60:]) / 60
            if ma20 < ma60 * 0.98:
                taiex_regime = "downtrend"

            for ticker, dbars in all_bars.items():
                if scan_date not in dbars:
                    continue
                dates_sorted = sorted(dbars)
                idx = dates_sorted.index(scan_date)
                if idx < 30 or idx + 1 >= len(dates_sorted):
                    continue

                history = [dbars[dates_sorted[i]] for i in range(idx - 30, idx)]
                d_minus_1 = dbars[scan_date]

                tb = detect_tight_base(d_minus_1, history)
                if tb is None:
                    continue

                # === v2 heat filters/bonuses ===
                industry = industry_map.get(ticker, "")
                ih = ind_heat.get(industry)

                # Hard filter: skip cold industries (rank_pct < 40)
                if ih and ih.rank_pct < 40:
                    continue

                # Compute heat bonus score (0-10)
                heat_bonus = 0
                if ih:
                    if ih.rank_pct >= 80:
                        heat_bonus += 5
                    elif ih.rank_pct >= 60:
                        heat_bonus += 3
                    if ih.acceleration_pct > 0.5:
                        heat_bonus += 2

                # Concept membership bonus
                concept_keys = get_concept_membership(ticker, concepts_def)
                hot_concept_match = []
                for ck in concept_keys:
                    c = concept_snap.concepts.get(ck)
                    if c and c.rank_pct >= 70:
                        hot_concept_match.append(c.name_zh)
                        heat_bonus += 3

                # D-day surge check
                d_day_bar = dbars[dates_sorted[idx + 1]]
                recent20 = [dbars[dates_sorted[i]] for i in range(max(0, idx - 19), idx + 1)]
                turnover_20ma = sum(b.close * b.volume for b in recent20) / len(recent20)
                history_for_dday = [dbars[dates_sorted[i]] for i in range(max(0, idx - 59), idx + 1)]
                try:
                    result = eng.score_full(
                        ohlcv=d_day_bar, history=history_for_dday, proxy=None,
                        taiex_regime=taiex_regime,
                        taiex_history=taiex_history,
                        turnover_20ma=turnover_20ma,
                        industry_rank_pct=None,
                    )
                except Exception:
                    result = None
                surge_hit = result is not None
                d_day_chg = (d_day_bar.close / d_minus_1.close - 1) * 100 if d_minus_1.close else 0

                # D+2, D+3
                surge_hit_d2 = surge_hit_d3 = False
                for k_off in (2, 3):
                    if idx + k_off >= len(dates_sorted):
                        break
                    k_day_bar = dbars[dates_sorted[idx + k_off]]
                    k_history = [dbars[dates_sorted[i]] for i in range(max(0, idx + k_off - 60), idx + k_off)]
                    k_recent20 = [dbars[dates_sorted[i]] for i in range(max(0, idx + k_off - 20), idx + k_off)]
                    k_turnover = sum(b.close * b.volume for b in k_recent20) / len(k_recent20)
                    try:
                        k_result = eng.score_full(
                            ohlcv=k_day_bar, history=k_history, proxy=None,
                            taiex_regime=taiex_regime, taiex_history=taiex_history,
                            turnover_20ma=k_turnover, industry_rank_pct=None,
                        )
                        if k_result is not None:
                            if k_off == 2: surge_hit_d2 = True
                            else: surge_hit_d3 = True
                    except Exception:
                        pass

                detections.append({
                    "d_minus_1_date": scan_date,
                    "ticker": ticker,
                    "industry": industry,
                    "ind_rank_pct": round(ih.rank_pct, 1) if ih else 0,
                    "ind_5d_pct": round(ih.ret_5d_pct, 2) if ih else 0,
                    "heat_bonus": heat_bonus,
                    "concepts": "|".join(hot_concept_match),
                    "range_pct": round(tb["range_pct"] * 100, 2),
                    "vol_ratio": round(tb["vol_ratio"], 2),
                    "position": round(tb["position"] * 100, 1),
                    "close_d1": d_minus_1.close,
                    "close_d0": d_day_bar.close,
                    "d_day_chg_pct": round(d_day_chg, 2),
                    "surge_hit_d1": surge_hit,
                    "surge_hit_d2": surge_hit_d2,
                    "surge_hit_d3": surge_hit_d3,
                    "surge_grade": result.get("grade") if result else "",
                    "surge_score": result.get("score") if result else 0,
                })

    total = len(detections)
    hit_d1 = sum(1 for d in detections if d["surge_hit_d1"])
    hit_d3 = sum(1 for d in detections if d["surge_hit_d1"] or d["surge_hit_d2"] or d["surge_hit_d3"])
    avg_chg = sum(d["d_day_chg_pct"] for d in detections) / total if total else 0

    stats = {
        "total": total,
        "hit_d1": hit_d1,
        "hit_d1_pct": round(hit_d1 / max(1, total) * 100, 1),
        "hit_d1_to_d3": hit_d3,
        "hit_d1_to_d3_pct": round(hit_d3 / max(1, total) * 100, 1),
        "avg_d_day_chg": round(avg_chg, 2),
    }
    return detections, stats


def print_v2_report(detections: list[dict], stats: dict) -> None:
    _console.print()
    _console.print(Panel.fit(
        f"[bold]TIGHT_BASE v2 (Heat-Aware) Backtest[/bold]\n\n"
        f"Total detections (post-filter):  [cyan]{stats['total']}[/cyan]\n"
        f"D+1 Surge hit:                    [green]{stats['hit_d1']} ({stats['hit_d1_pct']}%)[/green]\n"
        f"D+1~D+3 Surge hit:                [green]{stats['hit_d1_to_d3']} ({stats['hit_d1_to_d3_pct']}%)[/green]\n"
        f"Avg D-day price change:           [yellow]{stats['avg_d_day_chg']:+.2f}%[/yellow]\n\n"
        f"[dim]v1 baseline: 1067 detections, 2.2% D+1 hit, 5.2% D+1~3 hit[/dim]",
        title="Summary", box=box.ROUNDED,
    ))

    # Hit rate by heat_bonus
    _console.print()
    t = Table(title="Hit Rate by Heat Bonus Score", box=box.ROUNDED)
    t.add_column("Heat Bonus"); t.add_column("Count", justify="right")
    t.add_column("D+1 Hit %", justify="right"); t.add_column("Avg D-day %", justify="right")
    for lo, hi, lbl in [(0,3,"0-2 冷"),(3,6,"3-5 溫"),(6,9,"6-8 熱"),(9,99,"9+ 烈")]:
        bucket = [d for d in detections if lo <= d["heat_bonus"] < hi]
        if not bucket:
            continue
        h = sum(1 for d in bucket if d["surge_hit_d1"])
        avg = sum(d["d_day_chg_pct"] for d in bucket) / len(bucket)
        color = "red" if "烈" in lbl else "yellow" if "熱" in lbl else "white"
        t.add_row(
            f"[{color}]{lbl}[/{color}]", str(len(bucket)),
            f"{h/len(bucket)*100:.1f}%", f"{avg:+.2f}%",
        )
    _console.print(t)

    # Top winners
    _console.print()
    winners = sorted(
        [d for d in detections if d["surge_hit_d1"]],
        key=lambda x: x["d_day_chg_pct"], reverse=True,
    )[:15]
    t2 = Table(title="Top 15 v2 Hits", box=box.ROUNDED)
    t2.add_column("D-1"); t2.add_column("代號"); t2.add_column("產業")
    t2.add_column("產業%", justify="right"); t2.add_column("Bonus", justify="right")
    t2.add_column("概念"); t2.add_column("D-day %", justify="right")
    for w in winners:
        t2.add_row(
            str(w["d_minus_1_date"]), w["ticker"], w["industry"][:6],
            f"{w['ind_rank_pct']:.0f}", f"{w['heat_bonus']}",
            w["concepts"][:14] or "—",
            f"[green]{w['d_day_chg_pct']:+.1f}[/green]",
        )
    _console.print(t2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--output", type=str, default="data/tight_base_v2_backtest.csv")
    args = ap.parse_args()

    detections, stats = run_v2_backtest(days=args.days)
    if not detections:
        _console.print("[red]No detections.[/red]")
        return 1

    df = pd.DataFrame(detections)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    _console.print(f"\n[green]Saved {len(detections)} detections to {out}[/green]")

    print_v2_report(detections, stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
