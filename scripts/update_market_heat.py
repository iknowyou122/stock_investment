"""Update market heat snapshots for surge scoring.

Runs after the postmarket surge scan (17:05).
Saves JSON snapshots to data/market_heat/ so tomorrow's surge scan
can load them via _load_heat_lookup().

Usage:
    python scripts/update_market_heat.py
    make heat-update
"""
from __future__ import annotations

import pickle
import sys
import warnings
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taiwan_stock_agent.domain.market_heat import compute_market_heat, save_heat_snapshot
from taiwan_stock_agent.domain.concept_heat import (
    load_concepts, compute_concept_heat, save_concept_snapshot,
)
from taiwan_stock_agent.domain.international_signals import (
    compute_international_signals, save_intl_snapshot,
)
from taiwan_stock_agent.domain.theme_analyzer import analyze_themes, save_theme_analysis
from taiwan_stock_agent.domain.llm_provider import create_llm_provider

from surge_backtest import _load_industry_map, download_all

_ROOT = Path(__file__).resolve().parents[1]
_HEAT_DIR = _ROOT / "data" / "market_heat"
_CACHE_PATH = _ROOT / "data" / "_ohlcv_cache_90d.pkl"


def _load_or_refresh_ohlcv(industry_map: dict) -> dict:
    today = date.today()
    if _CACHE_PATH.exists():
        mtime = date.fromtimestamp(_CACHE_PATH.stat().st_mtime)
        if mtime == today:
            with open(_CACHE_PATH, "rb") as f:
                return pickle.load(f)
    start = str(today - timedelta(days=150))
    end = str(today + timedelta(days=1))
    tickers = list(industry_map.keys())
    print(f"  Downloading {len(tickers)} tickers…")
    all_bars = download_all(tickers, start, end, workers=20)
    cached = {"all_bars": all_bars}
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CACHE_PATH, "wb") as f:
        pickle.dump(cached, f)
    return cached


def main() -> int:
    industry_map = _load_industry_map()
    if not industry_map:
        print("No industry map — run make scan first", file=sys.stderr)
        return 1

    cached = _load_or_refresh_ohlcv(industry_map)
    all_bars_dict = cached.get("all_bars", {})
    if not all_bars_dict:
        print("No OHLCV data", file=sys.stderr)
        return 1

    latest = max(d for dbs in all_bars_dict.values() for d in dbs)
    bars_up_to = {
        t: [b for d, b in sorted(dbs.items()) if d <= latest]
        for t, dbs in all_bars_dict.items()
        if len([d for d in dbs if d <= latest]) >= 21
    }

    _HEAT_DIR.mkdir(parents=True, exist_ok=True)

    heat = compute_market_heat(bars_up_to, industry_map, latest)
    save_heat_snapshot(heat, _HEAT_DIR)

    concepts_def = load_concepts(_ROOT / "config" / "concepts.json")
    concept_snap = compute_concept_heat(bars_up_to, concepts_def, latest)
    save_concept_snapshot(concept_snap, _HEAT_DIR)

    intl = compute_international_signals(latest)
    save_intl_snapshot(intl, _HEAT_DIR)

    try:
        llm = create_llm_provider()
        analysis = analyze_themes(latest, heat, concept_snap, intl, llm=llm)
        save_theme_analysis(analysis, _HEAT_DIR)
        print(f"  Theme: {analysis.narrative[:60]}")
    except Exception as e:
        print(f"  LLM theme skipped: {e}", file=sys.stderr)

    top_inds = sorted(heat.industries.values(), key=lambda x: -x.rank_pct)[:3]
    top_str = " / ".join(i.industry for i in top_inds)
    print(f"Heat updated: {latest} | Top: {top_str} | {len(heat.industries)} industries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
