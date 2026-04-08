"""Two jobs: daily scan → DB, and T+N settlement.

Usage:
    python scripts/daily_runner.py daily           # scan today → DB
    python scripts/daily_runner.py settle          # settle pending T+1/T+3/T+5
    python scripts/daily_runner.py settle --date 2026-04-03
    make daily
    make settle
    make settle DATE=2026-04-03
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.infrastructure.db import init_pool, get_connection
from taiwan_stock_agent.infrastructure.signal_recorder import record_signal
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

_console = Console()


# ---------------------------------------------------------------------------
# Job A: daily scan
# ---------------------------------------------------------------------------

def run_daily(analysis_date: date, llm: str | None, sectors: str | None) -> None:
    """Run scan for analysis_date and store results to DB."""
    from taiwan_stock_agent.agents.strategist_agent import StrategistAgent
    from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher

    class _EmptyLabelRepo:
        def get_label(self, branch_code):
            return None
        def get_labels_bulk(self, codes):
            return {}

    init_pool()
    if llm:
        os.environ["LLM_PROVIDER"] = llm
    agent = StrategistAgent(
        finmind=FinMindClient(),
        label_repo=_EmptyLabelRepo(),
        chip_proxy_fetcher=ChipProxyFetcher(),
    )

    # Load watchlist from cache
    data_dir = Path(__file__).resolve().parents[1] / "data" / "watchlist_cache"
    tickers: list[str] = []
    industry_map: dict[str, str] = {}
    for delta in range(0, 8):
        candidate = analysis_date - timedelta(days=delta)
        f = data_dir / f"industry_map_{candidate}.json"
        if f.exists():
            with open(f) as fh:
                industry_map = json.load(fh)
            tickers = list(industry_map.keys())
            break

    if not tickers:
        _console.print("[red]No watchlist cache found — run make scan first to build cache[/red]")
        return

    if sectors:
        sector_filter = [s.strip() for s in sectors.split()]
        tickers = [t for t, ind in industry_map.items() if any(s in ind for s in sector_filter)]

    _console.print(f"  [bold cyan][{analysis_date}][/bold cyan] Running daily scan for [bold]{len(tickers)}[/bold] tickers → DB")
    recorded = 0
    for ticker in tickers:
        try:
            signal = agent.run(ticker, analysis_date)
            if signal.halt_flag:
                continue
            record_signal(signal, source="live")
            recorded += 1
        except Exception as e:
            logger.warning("skip %s: %s", ticker, e)
        time.sleep(0.3)

    _console.print(Panel(
        f"[bold green]Daily Scan 完成[/bold green]  {recorded} 筆訊號已記錄至 signal_outcomes (source=live)",
        border_style="green",
        padding=(0, 2),
    ))


# ---------------------------------------------------------------------------
# Job B: settle outcomes
# ---------------------------------------------------------------------------

def run_settle(settle_date: date) -> None:
    """Backfill T+1/T+3/T+5 outcomes for signals with pending prices."""
    init_pool()
    finmind = FinMindClient()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_id, ticker, signal_date, entry_price
                FROM signal_outcomes
                WHERE price_5d IS NULL
                  AND halt_flag = FALSE
                  AND source = 'live'
                  AND signal_date <= %s - INTERVAL '5 days'
                ORDER BY signal_date DESC
                LIMIT 200
            """, (settle_date,))
            rows = cur.fetchall()

    if not rows:
        _console.print(Panel(
            f"[dim][{settle_date}] 沒有待結算訊號[/dim]",
            border_style="bright_black",
            padding=(0, 2),
        ))
        return

    _console.print(f"  [bold cyan][{settle_date}][/bold cyan] Settling [bold]{len(rows)}[/bold] signals...")

    for signal_id, ticker, signal_date, entry_price in rows:
        try:
            end = signal_date + timedelta(days=14)
            df = finmind.fetch_ohlcv(ticker, signal_date, end)
        except Exception as e:
            logger.warning("settle %s %s: %s", ticker, signal_date, e)
            continue

        if df.empty:
            continue

        closes: dict[date, float] = {}
        for _, row in df.iterrows():
            closes[row["trade_date"]] = float(row["close"])

        trading_days = sorted(closes.keys())
        if signal_date not in trading_days:
            continue

        idx = trading_days.index(signal_date)

        def get_close(offset: int) -> float | None:
            i = idx + offset
            if 0 <= i < len(trading_days):
                return closes[trading_days[i]]
            return None

        p1, p3, p5 = get_close(1), get_close(3), get_close(5)

        def outcome(p: float | None) -> float | None:
            return (p - entry_price) / entry_price if p is not None else None

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE signal_outcomes
                    SET price_1d=%s, price_3d=%s, price_5d=%s,
                        outcome_1d=%s, outcome_3d=%s, outcome_5d=%s
                    WHERE signal_id=%s AND price_5d IS NULL
                """, (p1, p3, p5, outcome(p1), outcome(p3), outcome(p5), signal_id))

        time.sleep(0.2)

    _console.print(Panel(
        "[bold green]Settlement 完成[/bold green]",
        border_style="green",
        padding=(0, 2),
    ))


# ---------------------------------------------------------------------------
# Helpers for interactive mode
# ---------------------------------------------------------------------------

def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5


def _current_analysis_date() -> date:
    """17:00 前用前一交易日；之後用今天。"""
    from datetime import datetime
    now = datetime.now()
    candidate = date.today() if now.hour >= 17 else date.today() - timedelta(days=1)
    while not _is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _llm_menu_daily() -> str | None:
    """互動式選擇 LLM provider。回傳 provider key 或 None。"""
    _PROVIDERS = [
        ("none",   "不使用 LLM（純 deterministic）[預設]"),
        ("auto",   "自動偵測（依 API key）"),
        ("gemini", "Google Gemini"),
        ("claude", "Anthropic Claude"),
        ("openai", "OpenAI"),
    ]

    table = Table(box=box.SIMPLE, show_header=False, border_style="bright_black")
    table.add_column("#", style="bold cyan", justify="right", width=3)
    table.add_column("LLM 引擎", style="white")
    for i, (_, label) in enumerate(_PROVIDERS, 1):
        table.add_row(str(i), label)
    _console.print()
    _console.print(Panel(table, title="[bold white]LLM 引擎選擇[/bold white]", border_style="cyan"))

    _console.print("\n[bold yellow]請輸入代號[/bold yellow]，直接 Enter 使用 [dim][1 不使用 LLM][/dim]")
    try:
        raw = _console.input("[bold cyan]> [/bold cyan]").strip()
    except EOFError:
        raw = ""
    if raw.isdigit() and 1 <= int(raw) <= len(_PROVIDERS):
        choice = int(raw)
    else:
        choice = 1
    provider_key, label = _PROVIDERS[choice - 1]
    _console.print(f"  [dim]→ {label}[/dim]")
    return None if provider_key == "none" else provider_key


def _sector_menu_daily(industry_map: dict[str, str]) -> str | None:
    """互動式選擇產業別。回傳空白分隔的產業名稱字串，或 None（全市場）。"""
    from collections import Counter
    counts = Counter(industry_map.values())
    rows = [(i, ind, counts[ind]) for i, ind in enumerate(sorted(counts.keys()), start=1)]

    table = Table(
        title="可用產業別",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
        border_style="bright_black",
    )
    table.add_column("#", style="dim", justify="right", width=4)
    table.add_column("產業別", style="white", min_width=18)
    table.add_column("檔數", justify="right", style="green")

    for idx, ind, cnt in rows:
        bar = "█" * min(cnt // 10, 20)
        table.add_row(str(idx), ind, f"{cnt:>4}  [dim]{bar}[/dim]")

    _console.print()
    _console.print(table)

    _console.print("\n[bold yellow]請輸入產業代號[/bold yellow]（空白分隔），直接 Enter 掃描全市場：")
    try:
        raw = _console.input("[bold cyan]> [/bold cyan]").strip()
    except EOFError:
        raw = ""
    if not raw:
        return None

    idx_map = {i: name for i, name, _ in rows}
    selected_names: list[str] = []
    for token in raw.split():
        try:
            n = int(token)
            if n in idx_map:
                selected_names.append(idx_map[n])
            else:
                _console.print(f"  [red]忽略無效代號: {n}[/red]")
        except ValueError:
            _console.print(f"  [red]忽略無效輸入: {token}[/red]")

    if not selected_names:
        return None
    # daily_runner uses sector as substring filter; return space-separated names
    return " ".join(selected_names)


def _count_pending_settle() -> int:
    """Quick count of signals awaiting settlement."""
    try:
        init_pool()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM signal_outcomes
                    WHERE price_5d IS NULL
                      AND halt_flag = FALSE
                      AND source = 'live'
                      AND signal_date <= CURRENT_DATE - INTERVAL '5 days'
                """)
                return cur.fetchone()[0]
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="job")  # not required — interactive when omitted

    daily_p = sub.add_parser("daily", help="Scan today and record to DB")
    daily_p.add_argument("--date", type=date.fromisoformat, default=None)
    daily_p.add_argument("--llm", default=None)
    daily_p.add_argument("--sectors", default=None)

    settle_p = sub.add_parser("settle", help="Backfill T+1/T+3/T+5 outcomes")
    settle_p.add_argument("--date", type=date.fromisoformat, default=None)

    args = parser.parse_args()

    # ----------------------------------------------------------------
    # If job was passed as CLI arg, run non-interactively
    # ----------------------------------------------------------------
    if args.job == "daily":
        analysis_date = args.date or date.today()
        run_daily(analysis_date, args.llm, args.sectors)
        return

    if args.job == "settle":
        settle_date = args.date or date.today()
        run_settle(settle_date)
        return

    # ----------------------------------------------------------------
    # Interactive mode — no subcommand given
    # ----------------------------------------------------------------
    today = date.today()
    analysis_date = _current_analysis_date()
    is_trading = _is_trading_day(today)
    trading_label = "[green]是[/green]" if is_trading else "[red]否（週末/假日）[/red]"

    _console.print()
    _console.print(Panel(
        f"[bold white]今日日期[/bold white]   {today}\n"
        f"[bold white]分析日期[/bold white]   {analysis_date}\n"
        f"[bold white]交易日[/bold white]     {trading_label}",
        title="[bold cyan]Daily Runner[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))

    # Mode selection
    _MODES = [
        ("daily",  "Daily Scan — 掃描今日訊號並寫入 DB"),
        ("settle", "Settle — 補填 T+1/T+3/T+5 結算價格"),
    ]

    mode_table = Table(box=box.SIMPLE, show_header=False, border_style="bright_black")
    mode_table.add_column("#", style="bold cyan", justify="right", width=3)
    mode_table.add_column("模式", style="white")
    for i, (_, label) in enumerate(_MODES, 1):
        mode_table.add_row(str(i), label)
    _console.print()
    _console.print(Panel(mode_table, title="[bold white]模式選擇[/bold white]", border_style="bright_black"))
    _console.print("\n[bold yellow]請輸入代號[/bold yellow]，直接 Enter 使用 [dim][1 Daily Scan][/dim]")
    try:
        raw = _console.input("[bold cyan]> [/bold cyan]").strip()
    except EOFError:
        raw = ""
    if raw.isdigit() and 1 <= int(raw) <= len(_MODES):
        mode_key = _MODES[int(raw) - 1][0]
    else:
        mode_key = "daily"
    mode_label = next(label for key, label in _MODES if key == mode_key)
    _console.print(f"  [dim]→ {mode_label}[/dim]")

    if mode_key == "daily":
        # LLM selection
        llm = _llm_menu_daily()

        # Sector selection (load industry map for the prompt)
        data_dir = Path(__file__).resolve().parents[1] / "data" / "watchlist_cache"
        industry_map: dict[str, str] = {}
        for delta in range(0, 8):
            candidate = analysis_date - timedelta(days=delta)
            f = data_dir / f"industry_map_{candidate}.json"
            if f.exists():
                with open(f) as fh:
                    industry_map = json.load(fh)
                break

        sectors: str | None = None
        if industry_map:
            sectors = _sector_menu_daily(industry_map)
            if sectors:
                sector_list = sectors.split()
                _console.print(f"  [green]選擇產業：{sector_list}[/green]")
            else:
                _console.print("  [dim]→ 全市場掃描[/dim]")
        else:
            _console.print("  [yellow]無 industry_map cache — 將嘗試全市場掃描[/yellow]")

        # Summary
        llm_display = llm if llm else "none"
        sector_display = sectors if sectors else "全市場"
        _console.print()
        _console.print(Panel(
            f"[bold white]分析日期[/bold white]  {analysis_date}\n"
            f"[bold white]LLM 引擎[/bold white]  {llm_display}\n"
            f"[bold white]產業過濾[/bold white]  {sector_display}",
            title="[bold white]Daily Scan 執行摘要[/bold white]",
            border_style="bright_black",
            padding=(0, 2),
        ))
        _console.print()

        run_daily(analysis_date, llm, sectors)

    else:  # settle
        pending = _count_pending_settle()
        pending_display = str(pending) if pending >= 0 else "（無法連線 DB）"
        _console.print()
        _console.print(Panel(
            f"[bold white]結算日期[/bold white]  {today}\n"
            f"[bold white]待結算訊號[/bold white] {pending_display} 筆",
            title="[bold white]Settle 執行摘要[/bold white]",
            border_style="bright_black",
            padding=(0, 2),
        ))
        _console.print()

        run_settle(today)


if __name__ == "__main__":
    main()
