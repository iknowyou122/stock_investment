"""Theme/concept money flow tracker.

Uses FinMind paid institutional buy/sell data to rank concept baskets
and TWSE industries by institutional net flow. Identifies where smart
money is rotating TODAY vs the past 5 trading days.

Usage:
    python scripts/theme_flow.py                # Today's snapshot
    python scripts/theme_flow.py --days 5       # 5-day cumulative
    python scripts/theme_flow.py --top 20       # Show top 20 industries
    make theme-flow
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.infrastructure.paid_data_fetcher import PaidDataFetcher

logger = logging.getLogger(__name__)
_console = Console()

_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_DIR = _ROOT / "config"
_WATCHLIST_CACHE_DIR = _ROOT / "data" / "watchlist_cache"
_HEAT_DIR = _ROOT / "data" / "market_heat"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class BasketFlow:
    key: str
    name_zh: str
    tickers: list[str]
    flow_1d: int = 0        # net shares (foreign + trust), today only
    flow_5d: int = 0        # net shares, past 5 trading days
    days_with_data: int = 0 # how many days had data for this basket


@dataclass
class IndustryFlow:
    industry: str
    ticker_count: int = 0
    flow_1d: int = 0
    flow_5d: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_concepts() -> dict[str, BasketFlow]:
    """Load concepts.json → {key: BasketFlow}."""
    path = _CONFIG_DIR / "concepts.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    concepts_raw = raw.get("concepts", raw)
    result: dict[str, BasketFlow] = {}
    for key, val in concepts_raw.items():
        name_zh = val.get("name_zh", key)
        tickers = val.get("tickers", val.get("members", []))
        result[key] = BasketFlow(key=key, name_zh=name_zh, tickers=tickers)
    return result


def _load_industry_map() -> dict[str, str]:
    """Load most recent cached industry map (ticker → industry)."""
    today = date.today()
    for delta in range(14):
        path = _WATCHLIST_CACHE_DIR / f"industry_map_{today - timedelta(days=delta)}.json"
        if path.exists():
            try:
                return json.load(path.open())
            except Exception:
                continue
    return {}


def _get_trading_dates(trade_date: date, n_days: int) -> list[date]:
    """Return the last n_days dates up to trade_date (skip weekends)."""
    result: list[date] = []
    d = trade_date
    while len(result) < n_days:
        if d.weekday() < 5:  # Mon-Fri only
            result.append(d)
        d -= timedelta(days=1)
    return list(reversed(result))


def _fmt_flow(shares: int) -> str:
    """Format net shares as +X萬張 or -X萬張."""
    lots = shares // 1000  # shares → lots (1 lot = 1000 shares)
    if abs(lots) >= 10000:
        return f"{lots/10000:+.1f}萬張"
    elif abs(lots) >= 1000:
        return f"{lots/1000:+.1f}千張"
    else:
        return f"{lots:+d}張"


def _flow_color(shares: int) -> str:
    if shares >= 500_000:    # +500 lots net
        return "bright_red"
    elif shares >= 100_000:
        return "red"
    elif shares > 0:
        return "dim red"
    elif shares <= -500_000:
        return "bright_green"
    elif shares <= -100_000:
        return "green"
    else:
        return "dim green"


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_theme_flows(
    trade_date: date,
    n_days: int = 5,
) -> tuple[dict[str, BasketFlow], dict[str, IndustryFlow]]:
    """Fetch institutional flows and aggregate by concept basket + industry.

    Returns (basket_flows, industry_flows).
    """
    fetcher = PaidDataFetcher()
    concepts = _load_concepts()
    industry_map = _load_industry_map()

    # Build reverse map: ticker → list[basket_key]
    ticker_to_baskets: dict[str, list[str]] = defaultdict(list)
    for key, bf in concepts.items():
        for t in bf.tickers:
            ticker_to_baskets[t].append(key)

    # Get trading dates
    dates = _get_trading_dates(trade_date, n_days)
    today_date = dates[-1] if dates else trade_date

    if not fetcher._api_key:
        _console.print("[yellow]⚠ FINMIND_API_KEY 未設定，無法查詢機構法人流量[/yellow]")
        return concepts, {}

    # Fetch institution flows for each date
    all_day_data: dict[date, dict[str, tuple[int, int, int]]] = {}
    for d in dates:
        day_data = fetcher.fetch_institution_day(d)
        if day_data:
            all_day_data[d] = day_data

    if not all_day_data:
        _console.print(f"[yellow]⚠ 無法取得 {today_date} 前 {n_days} 個交易日的法人資料[/yellow]")
        return concepts, {}

    # Aggregate by concept basket
    for key, bf in concepts.items():
        flow_1d = 0
        flow_5d = 0
        days_counted = 0
        for d, day_data in sorted(all_day_data.items()):
            day_flow = 0
            for t in bf.tickers:
                if t in day_data:
                    foreign_net, trust_net, _ = day_data[t]
                    day_flow += foreign_net + trust_net  # foreign + trust (不含自營)
            flow_5d += day_flow
            if d == today_date:
                flow_1d = day_flow
            if any(t in day_data for t in bf.tickers):
                days_counted += 1
        bf.flow_1d = flow_1d
        bf.flow_5d = flow_5d
        bf.days_with_data = days_counted

    # Aggregate by industry
    industry_flows: dict[str, IndustryFlow] = {}
    for t, (industry) in industry_map.items():
        if t not in industry_flows:
            industry_flows[industry] = IndustryFlow(industry=industry)
        industry_flows[industry].ticker_count += 1

    for d, day_data in sorted(all_day_data.items()):
        for ticker, (foreign_net, trust_net, _) in day_data.items():
            ind = industry_map.get(ticker, "其他")
            if ind not in industry_flows:
                industry_flows[ind] = IndustryFlow(industry=ind)
            net = foreign_net + trust_net
            industry_flows[ind].flow_5d += net
            if d == today_date:
                industry_flows[ind].flow_1d += net

    return concepts, industry_flows


def save_snapshot(
    basket_flows: dict[str, BasketFlow],
    industry_flows: dict[str, IndustryFlow],
    trade_date: date,
) -> Path:
    """Save snapshot to data/market_heat/theme_flow_YYYY-MM-DD.json."""
    _HEAT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "date": trade_date.isoformat(),
        "baskets": {
            key: {
                "name_zh": bf.name_zh,
                "tickers": bf.tickers,
                "flow_1d": bf.flow_1d,
                "flow_5d": bf.flow_5d,
            }
            for key, bf in basket_flows.items()
        },
        "industries": {
            ind: {
                "ticker_count": ifl.ticker_count,
                "flow_1d": ifl.flow_1d,
                "flow_5d": ifl.flow_5d,
            }
            for ind, ifl in industry_flows.items()
        },
    }
    path = _HEAT_DIR / f"theme_flow_{trade_date.isoformat()}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_latest_snapshot(lookback_days: int = 7) -> dict | None:
    """Load the most recent theme_flow snapshot, or None if not found."""
    today = date.today()
    for delta in range(lookback_days):
        path = _HEAT_DIR / f"theme_flow_{(today - timedelta(days=delta)).isoformat()}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_theme_flow(
    basket_flows: dict[str, BasketFlow],
    industry_flows: dict[str, IndustryFlow],
    trade_date: date,
    top_industry: int = 15,
) -> None:
    """Print Rich tables: concept baskets + TWSE industries sorted by flow."""

    _console.print(Panel(
        f"[bold white]題材資金流向[/bold white]  {trade_date}\n"
        f"[dim]外資+投信 淨買超（5日累計 / 今日）  不含自營[/dim]",
        title="[bold cyan]Theme Money Flow[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))

    # ── Concept Basket table ───────────────────────────────────────────────
    sorted_baskets = sorted(basket_flows.values(), key=lambda b: b.flow_5d, reverse=True)

    bt = Table(
        title="📡 概念板塊  外資+投信 5日淨流向",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white on dark_blue",
        border_style="magenta",
        show_lines=True,
    )
    bt.add_column("板塊", width=20)
    bt.add_column("成員", justify="center", width=5)
    bt.add_column("今日", justify="right", width=12)
    bt.add_column("5日累計", justify="right", width=14)
    bt.add_column("強度", width=14)

    for i, bf in enumerate(sorted_baskets, 1):
        c1d = _flow_color(bf.flow_1d)
        c5d = _flow_color(bf.flow_5d)
        # Strength bar: 5D flow scaled to ±20 lots per 1 block
        bar_val = min(10, max(0, int(bf.flow_5d / 200_000) + 5))
        strength = "▮" * bar_val + "▯" * (10 - bar_val)
        strength_color = "red" if bf.flow_5d > 0 else "green"
        bt.add_row(
            f"[bold]{bf.name_zh}[/bold]",
            str(len(bf.tickers)),
            f"[{c1d}]{_fmt_flow(bf.flow_1d)}[/{c1d}]",
            f"[{c5d}]{_fmt_flow(bf.flow_5d)}[/{c5d}]",
            f"[{strength_color}]{strength}[/{strength_color}]",
        )
    _console.print(bt)

    # ── Industry table ─────────────────────────────────────────────────────
    if not industry_flows:
        return

    sorted_industries = sorted(
        [ifl for ifl in industry_flows.values() if ifl.ticker_count >= 3],
        key=lambda x: x.flow_5d,
        reverse=True,
    )[:top_industry]

    it = Table(
        title=f"🏭 產業別  外資+投信 5日淨流向  (前 {top_industry} 名)",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white on dark_blue",
        border_style="blue",
        show_lines=False,
    )
    it.add_column("產業", width=20)
    it.add_column("檔數", justify="center", width=5)
    it.add_column("今日", justify="right", width=12)
    it.add_column("5日累計", justify="right", width=14)

    for ifl in sorted_industries:
        c1d = _flow_color(ifl.flow_1d)
        c5d = _flow_color(ifl.flow_5d)
        it.add_row(
            ifl.industry,
            str(ifl.ticker_count),
            f"[{c1d}]{_fmt_flow(ifl.flow_1d)}[/{c1d}]",
            f"[{c5d}]{_fmt_flow(ifl.flow_5d)}[/{c5d}]",
        )
    _console.print(it)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Theme money flow tracker")
    parser.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=date.today(),
        help="Analysis date (YYYY-MM-DD, default: today)",
    )
    parser.add_argument("--days", type=int, default=5, help="Lookback trading days (default: 5)")
    parser.add_argument("--top", type=int, default=15, help="Top N industries (default: 15)")
    parser.add_argument("--save", action="store_true", default=True, help="Save snapshot (default: True)")
    args = parser.parse_args()

    basket_flows, industry_flows = compute_theme_flows(args.date, n_days=args.days)

    print_theme_flow(basket_flows, industry_flows, args.date, top_industry=args.top)

    if args.save and industry_flows:
        path = save_snapshot(basket_flows, industry_flows, args.date)
        _console.print(f"\n  [dim cyan]💾 快照已儲存: {path.name}[/dim cyan]")


if __name__ == "__main__":
    main()
