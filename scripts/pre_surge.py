"""Daily Pre-Surge watchlist CLI — unified heat + TIGHT_BASE + LLM analysis.

Runs the full pipeline:
  1. Compute market heat (industries)
  2. Compute concept basket heat
  3. Fetch international overnight signals
  4. LLM theme analysis
  5. Detect TIGHT_BASE candidates with heat-aware scoring
  6. Output ranked watchlist for tomorrow's open

Usage:
    make pre-surge
    python scripts/pre_surge.py --refresh
    python scripts/pre_surge.py --min-bonus 5
"""
from __future__ import annotations

import argparse
import pickle
import sys
import warnings
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import json
from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel

from taiwan_stock_agent.domain.market_heat import compute_market_heat, save_heat_snapshot
from taiwan_stock_agent.domain.concept_heat import (
    load_concepts, compute_concept_heat, save_concept_snapshot, get_concept_membership,
)
from taiwan_stock_agent.domain.international_signals import (
    compute_international_signals, save_intl_snapshot,
)
from taiwan_stock_agent.domain.theme_analyzer import analyze_themes, save_theme_analysis
from taiwan_stock_agent.domain.llm_provider import create_llm_provider

from surge_backtest import _load_industry_map, download_all
from tight_base_backtest import detect_tight_base

_console = Console()
_ROOT = Path(__file__).resolve().parents[1]
_HEAT_DIR = _ROOT / "data" / "market_heat"
_WATCHLIST_DIR = _ROOT / "data" / "pre_surge_watchlist"


def _load_or_refresh(days: int, refresh: bool, industry_map: dict) -> dict:
    cache_path = Path("data") / f"_ohlcv_cache_{days}d.pkl"
    if cache_path.exists() and not refresh:
        _console.print(f"  Using cache: {cache_path}")
        with open(cache_path, "rb") as f:
            return pickle.load(f)
    today = date.today()
    start = str(today - timedelta(days=days + 60))
    end = str(today + timedelta(days=1))
    tickers = list(industry_map.keys())
    _console.print(f"  Downloading {len(tickers)} tickers…")
    all_bars = download_all(tickers, start, end, workers=20)
    data = {"all_bars": all_bars, "taiex_bars": []}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "wb") as f:
        pickle.dump(data, f)
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--min-bonus", type=int, default=3,
                    help="Minimum heat_bonus to include (default 3)")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    industry_map = _load_industry_map()
    cached = _load_or_refresh(args.days, args.refresh, industry_map)
    all_bars_dict = cached["all_bars"]

    latest = max(d for dbs in all_bars_dict.values() for d in dbs)
    _console.print(f"  Snapshot date: [cyan]{latest}[/cyan]\n")

    bars_up_to = {
        t: [b for d, b in sorted(dbs.items()) if d <= latest]
        for t, dbs in all_bars_dict.items()
    }
    bars_up_to = {t: b for t, b in bars_up_to.items() if len(b) >= 21}

    # ── Layer 1+2+3: Heat
    heat = compute_market_heat(bars_up_to, industry_map, latest)
    save_heat_snapshot(heat, _HEAT_DIR)
    concepts_def = load_concepts(Path("config/concepts.json"))
    concept_snap = compute_concept_heat(bars_up_to, concepts_def, latest)
    save_concept_snapshot(concept_snap, _HEAT_DIR)
    intl = compute_international_signals(latest)
    save_intl_snapshot(intl, _HEAT_DIR)

    # ── Layer LLM: Theme analysis
    llm = None if args.no_llm else create_llm_provider()
    analysis = analyze_themes(latest, heat, concept_snap, intl, llm=llm)
    save_theme_analysis(analysis, _HEAT_DIR)

    # ── Display heat overview
    state_zh = {
        "broad_rally": "全面多頭", "narrow_leadership": "窄幅領漲",
        "mixed": "震盪整理", "broad_selloff": "全面空頭",
    }.get(heat.market_state, heat.market_state)
    _console.print(Panel.fit(
        f"[bold]今日市場主軸[/bold]\n\n{analysis.narrative}\n\n"
        f"[dim]狀態: {state_zh} | 廣度 {heat.market_breadth:.0f}% | "
        f"風險: {'risk-ON' if intl.overall_risk_on else 'risk-OFF'} | "
        f"LLM: {llm.name if llm else 'fallback'}[/dim]",
        title=f"Pre-Surge Watchlist  ({latest})", box=box.DOUBLE,
    ))

    # ── Layer 4: TIGHT_BASE with heat-aware bonus
    ind_heat = {ih.industry: ih for ih in heat.industries.values()}

    candidates = []
    for ticker, bars in bars_up_to.items():
        if len(bars) < 30:
            continue
        today_bar = bars[-1]
        history = bars[-31:-1]
        tb = detect_tight_base(today_bar, history)
        if tb is None:
            continue

        industry = industry_map.get(ticker, "")
        ih = ind_heat.get(industry)

        # Hard filter: cold industries
        if ih and ih.rank_pct < 40:
            continue

        # Heat bonus
        heat_bonus = 0
        if ih:
            if ih.rank_pct >= 80:
                heat_bonus += 5
            elif ih.rank_pct >= 60:
                heat_bonus += 3
            if ih.acceleration_pct > 0.5:
                heat_bonus += 2

        # Concept bonus
        concept_keys = get_concept_membership(ticker, concepts_def)
        hot_concepts = []
        for ck in concept_keys:
            c = concept_snap.concepts.get(ck)
            if c and c.rank_pct >= 70:
                hot_concepts.append(c.name_zh)
                heat_bonus += 3

        # International tailwind
        if ih and ih.industry in intl.tailwinds.industry_tailwinds:
            heat_bonus += max(0, intl.tailwinds.industry_tailwinds[ih.industry])
        for ck in concept_keys:
            if ck in intl.tailwinds.concept_tailwinds:
                heat_bonus += max(0, intl.tailwinds.concept_tailwinds[ck])

        if heat_bonus < args.min_bonus:
            continue

        candidates.append({
            "ticker": ticker, "industry": industry,
            "ind_rank_pct": ih.rank_pct if ih else 0,
            "ind_5d": ih.ret_5d_pct if ih else 0,
            "heat_bonus": heat_bonus,
            "concepts": "/".join(hot_concepts),
            "range_pct": tb["range_pct"] * 100,
            "vol_ratio": tb["vol_ratio"],
            "position": tb["position"] * 100,
            "close": today_bar.close,
        })

    # Sort by heat_bonus desc, then position desc
    candidates.sort(key=lambda x: (-x["heat_bonus"], -x["position"]))

    if not candidates:
        _console.print("\n[yellow]今日無 Pre-Surge 候選（熱度不足）[/yellow]")
        return 0

    # Save
    _WATCHLIST_DIR.mkdir(parents=True, exist_ok=True)
    wl_path = _WATCHLIST_DIR / f"watchlist_{latest.isoformat()}.json"
    with open(wl_path, "w", encoding="utf-8") as f:
        json.dump({
            "snapshot_date": latest.isoformat(),
            "narrative": analysis.narrative,
            "dominant_themes": analysis.dominant_themes,
            "candidates": candidates,
        }, f, ensure_ascii=False, indent=2)

    _console.print()
    t = Table(
        title=f"Pre-Surge 候選 ({len(candidates)} 支，bonus ≥ {args.min_bonus})",
        box=box.ROUNDED,
    )
    t.add_column("Bonus", justify="right")
    t.add_column("代號"); t.add_column("產業")
    t.add_column("產業%", justify="right")
    t.add_column("產業5d%", justify="right")
    t.add_column("熱門概念")
    t.add_column("Range%", justify="right")
    t.add_column("位置%", justify="right")
    t.add_column("收盤", justify="right")

    for c in candidates[:30]:
        if c["heat_bonus"] >= 7:
            bonus_str = f"[bold red]{c['heat_bonus']}[/bold red]"
        elif c["heat_bonus"] >= 5:
            bonus_str = f"[yellow]{c['heat_bonus']}[/yellow]"
        else:
            bonus_str = str(c["heat_bonus"])
        t.add_row(
            bonus_str, c["ticker"], c["industry"][:8],
            f"{c['ind_rank_pct']:.0f}",
            f"{c['ind_5d']:+.1f}",
            c["concepts"][:18] or "—",
            f"{c['range_pct']:.1f}",
            f"{c['position']:.0f}",
            f"{c['close']:.1f}",
        )
    _console.print(t)
    _console.print(f"\n[dim]Saved to {wl_path}[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
