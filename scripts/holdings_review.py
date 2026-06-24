"""Standalone CLI: daily holdings review.

Usage:
    make holdings-review              # auto: today
    python scripts/holdings_review.py # same

Reads `simulated_holdings` table, computes today's P&L for open positions,
recaps past 7 days of closures, prints rule-based + LLM narrative + tuning
suggestions.

Also auto-invoked at the start of `make plan` so the user sees yesterday's
results before today's new picks.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# Bootstrap
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from taiwan_stock_agent.domain.holdings_review import (
    HoldingsReview,
    build_review,
    generate_review_narrative,
)
from taiwan_stock_agent.domain.holdings_optimizer import build_optimization_report
from taiwan_stock_agent.domain.llm_provider import create_llm_provider
from taiwan_stock_agent.infrastructure.holdings_repository import HoldingsRepository

logger = logging.getLogger(__name__)
_console = Console()


# ── Print helpers ───────────────────────────────────────────────────────────


def print_review(review: HoldingsReview, *, narrative: str = "") -> None:
    """Render a full holdings-review report to terminal."""
    _console.print()
    _console.print(Panel(
        Text(f"📊 持倉復盤 · {review.today} · 過去 {review.lookback_days} 天", style="bold cyan"),
        border_style="cyan", expand=False,
    ))

    # Stats line
    pnl_color = "green" if review.portfolio_unrealised_pct > 0.5 else ("red" if review.portfolio_unrealised_pct < -0.5 else "white")
    stats = (
        f"  📦 持倉 {review.n_open}  "
        f"｜ 已平倉 {review.n_closed_in_period}  "
        f"｜ 未實現 [{pnl_color}]{review.portfolio_unrealised_pct:+.2f}% "
        f"(NT${review.portfolio_unrealised_twd:+,})[/{pnl_color}]"
    )
    if review.n_closed_in_period > 0:
        stats += (
            f"  ｜ 實現 NT${review.portfolio_realised_twd:+,}"
            f"  ｜ 勝率 {review.closed_win_rate or 0}%"
        )
    if review.alpha_pct is not None:
        alpha_color = "green" if review.alpha_pct > 0 else "red"
        stats += f"  ｜ Alpha [{alpha_color}]{review.alpha_pct:+.2f}%[/{alpha_color}]"
    _console.print(stats)

    # Narrative
    if narrative:
        _console.print(f"\n  [italic]{narrative}[/italic]")

    # Trigger history
    if review.triggers.total > 0:
        tbl = Table(title="🎯 過去 7 天觸發歷史", box=box.ROUNDED, padding=(0, 1))
        tbl.add_column("觸發類型")
        tbl.add_column("次數", justify="right")
        tbl.add_column("含義", style="dim")
        triggers_data = [
            ("🔴 STOP_LOSS", review.triggers.stop_loss, "停損出場 (-7%)"),
            ("🟢 TAKE_PROFIT", review.triggers.take_profit, "停利出場 (+15%)"),
            ("⏱️ TIME_STOP", review.triggers.time_stop, "10 天未漲過 +5% 強制出"),
            ("📉 TIER_DROP", review.triggers.tier_drop, "TCE conf 崩潰 (<30)"),
            ("✋ MANUAL", review.triggers.manual, "手動出場"),
        ]
        for label, count, desc in triggers_data:
            if count > 0:
                tbl.add_row(label, str(count), desc)
        _console.print(tbl)

    # Tier breakdown
    has_tier_data = any(t.n_total > 0 for t in review.tier_stats)
    if has_tier_data:
        tbl = Table(title="📈 Tier 表現分布", box=box.ROUNDED, padding=(0, 1))
        tbl.add_column("Tier")
        tbl.add_column("總數", justify="right")
        tbl.add_column("持倉", justify="right")
        tbl.add_column("已平倉", justify="right")
        tbl.add_column("勝率", justify="right")
        tbl.add_column("平倉均報酬", justify="right")
        tbl.add_column("持倉均 P&L", justify="right")
        for t in review.tier_stats:
            if t.n_total == 0:
                continue
            tier_color = {"S": "gold1", "A": "cyan", "B": "yellow"}.get(t.tier, "white")
            wr_str = f"{t.closed_win_rate}%" if t.closed_win_rate is not None else "—"
            avg_real = f"{t.avg_realised_pct:+.2f}%" if t.avg_realised_pct is not None else "—"
            avg_unreal = f"{t.avg_unrealised_pct:+.2f}%" if t.avg_unrealised_pct is not None else "—"
            tbl.add_row(
                f"[{tier_color}]{t.tier}[/{tier_color}]",
                str(t.n_total),
                str(t.n_open),
                f"{t.n_closed_win}勝/{t.n_closed_loss}敗" if t.n_closed > 0 else "—",
                wr_str,
                avg_real,
                avg_unreal,
            )
        _console.print(tbl)

    # Risk warnings
    if review.risk_warnings:
        _console.print()
        sev_color = {"high": "red", "medium": "yellow", "low": "dim"}
        for w in review.risk_warnings:
            color = sev_color.get(w.severity, "white")
            icon = "🔴" if w.severity == "high" else "🟡" if w.severity == "medium" else "💡"
            _console.print(
                f"  [{color}]{icon} {w.ticker} {w.name}[/{color}]: {w.message}"
            )


def print_optimization(report) -> None:
    """Render parameter-tuning suggestions (rule-based, never auto-applied)."""
    if not report.suggestions:
        return
    _console.print()
    _console.print(Panel(
        Text(f"🔧 系統優化建議 ({report.n_total} 項)", style="bold magenta"),
        border_style="magenta", expand=False,
    ))
    sev_emoji = {"high": "🔴", "medium": "🟡", "low": "💡"}
    for s in report.suggestions:
        emoji = sev_emoji.get(s.severity, "·")
        _console.print(
            f"  {emoji} [{s.severity}] [bold]{s.parameter}[/bold]: "
            f"{s.current_value} → [cyan]{s.suggested_value}[/cyan]"
        )
        _console.print(f"     [dim]{s.rationale}[/dim]")


# ── Data fetching ──────────────────────────────────────────────────────────


def _fetch_prices_today(tickers: list[str], today: date) -> dict[str, float]:
    """Pull latest close price for each ticker (FinMind, best-effort)."""
    from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
    api_key = os.environ.get("FINMIND_API_KEY")
    if not api_key:
        return {}
    fm = FinMindClient(api_key=api_key)
    prices: dict[str, float] = {}
    # Use a 3-day window so weekend / holiday gaps still get the latest close
    start = (today - timedelta(days=3)).isoformat()
    end = today.isoformat()
    for tk in tickers:
        try:
            df = fm.fetch_ohlcv(ticker=tk, start_date=start, end_date=end)
            if df is None or df.empty:
                continue
            df = df.sort_values("trade_date")
            prices[tk] = float(df.iloc[-1]["close"])
        except Exception:
            continue
        time.sleep(0.03)
    return prices


def _fetch_taiex_return(today: date, lookback_days: int) -> float | None:
    """7-day TAIEX return for alpha calculation."""
    from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
    api_key = os.environ.get("FINMIND_API_KEY")
    if not api_key:
        return None
    fm = FinMindClient(api_key=api_key)
    start = (today - timedelta(days=lookback_days + 5)).isoformat()
    end = today.isoformat()
    try:
        df = fm.fetch_ohlcv(ticker="TAIEX", start_date=start, end_date=end)
        if df is None or df.empty or len(df) < 2:
            return None
        df = df.sort_values("trade_date")
        first = float(df.iloc[0]["close"])
        last = float(df.iloc[-1]["close"])
        if first <= 0:
            return None
        return round((last - first) / first * 100, 2)
    except Exception:
        return None


# ── Main entry point ──────────────────────────────────────────────────────


def run_review(
    *,
    today: date | None = None,
    lookback_days: int = 7,
    budget_twd: int = 3_000_000,
    use_llm: bool = True,
    name_map: dict | None = None,
) -> HoldingsReview:
    """Build + print the daily holdings review. Returns the review object."""
    today = today or date.today()
    repo = HoldingsRepository()
    if not repo.available:
        _console.print("[yellow]⚠ DATABASE_URL 未設定，跳過持倉復盤[/yellow]")
        return None  # type: ignore[return-value]

    open_holdings = repo.list_open()
    closed_in_period = repo.list_recent_closed(
        since=today - timedelta(days=lookback_days)
    )

    if not open_holdings and not closed_in_period:
        _console.print(
            f"[dim]📊 持倉復盤 · {today}: 過去 {lookback_days} 天無持倉、無平倉紀錄[/dim]"
        )
        return None  # type: ignore[return-value]

    # Resolve names if not provided (best-effort)
    if name_map is None:
        try:
            import glob, json
            cache_files = sorted((Path(__file__).resolve().parents[1]
                                   / "data" / "watchlist_cache").glob("name_map_*.json"))
            if cache_files:
                name_map = json.loads(cache_files[-1].read_text(encoding="utf-8"))
            else:
                name_map = {}
        except Exception:
            name_map = {}

    # Fetch current prices for all open + closed tickers
    tickers = list({h.ticker for h in open_holdings}
                   | {h.ticker for h in closed_in_period})
    prices_today = _fetch_prices_today(tickers, today)

    # TAIEX baseline for alpha
    taiex_ret = _fetch_taiex_return(today, lookback_days)

    review = build_review(
        today=today,
        open_holdings=open_holdings,
        closed_in_period=closed_in_period,
        prices_today=prices_today,
        name_map=name_map,
        taiex_return_pct=taiex_ret,
        lookback_days=lookback_days,
        budget_twd=budget_twd,
    )

    # LLM narrative (best-effort)
    narrative = ""
    if use_llm:
        try:
            llm = create_llm_provider()
            narrative = generate_review_narrative(review, llm=llm)
        except Exception as exc:
            logger.warning("LLM narrative skipped: %s", exc)
            narrative = generate_review_narrative(review, llm=None)
    else:
        narrative = generate_review_narrative(review, llm=None)

    print_review(review, narrative=narrative)

    # Optimizer suggestions (rule-based)
    opt_report = build_optimization_report(review)
    print_optimization(opt_report)

    return review


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily holdings review")
    parser.add_argument("--date", type=str, default=None,
                        help="Review date (YYYY-MM-DD, default: today)")
    parser.add_argument("--lookback", type=int, default=7,
                        help="Lookback window in days (default 7)")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM narrative, use rule-based only")
    parser.add_argument("--budget", type=int, default=3_000_000,
                        help="Portfolio budget in NT$ (default 3,000,000)")
    args = parser.parse_args()

    today = date.fromisoformat(args.date) if args.date else date.today()
    run_review(
        today=today,
        lookback_days=args.lookback,
        budget_twd=args.budget,
        use_llm=not args.no_llm,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
