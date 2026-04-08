"""Historical backtest: run TripleConfirmationEngine on past dates → signal_outcomes.

Usage:
    python scripts/backtest.py --date-from 2025-10-01 --date-to 2026-03-31
    python scripts/backtest.py --date-from 2026-01-15 --date-to 2026-01-15 --tickers 2330 2317
    python scripts/backtest.py --date-from 2026-03-01 --date-to 2026-03-31 --llm none
    python scripts/backtest.py --date-from 2026-03-01 --date-to 2026-03-31 --sectors 1 4 --llm none
    make backtest DATE_FROM=2025-10-01 DATE_TO=2026-03-31
    make backtest DATE_FROM=2026-03-01 DATE_TO=2026-03-31 SECTORS="1 4" LLM=none
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.agents.strategist_agent import StrategistAgent, _LLM_DISABLED
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher
from taiwan_stock_agent.infrastructure.signal_recorder import record_signal
from taiwan_stock_agent.infrastructure.db import init_pool, get_connection

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_console = Console()


def _is_trading_day(d: date) -> bool:
    """Exclude weekends. (Holidays not checked — TWSE will return empty data.)"""
    return d.weekday() < 5


def _date_range(start: date, end: date) -> list[date]:
    days = []
    cur = start
    while cur <= end:
        if _is_trading_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _settle_outcomes(signal_ids: list[tuple[str, str, date]]) -> None:
    """Backfill T+1/T+3/T+5 prices for signals we just inserted.

    signal_ids: list of (signal_id, ticker, signal_date)
    """
    finmind = FinMindClient()
    total = len(signal_ids)
    settled = 0
    skipped = 0
    no_data = 0

    # Group signals by ticker to fetch OHLCV once per ticker (not once per signal)
    from collections import defaultdict
    ticker_signals: dict[str, list[tuple[str, date]]] = defaultdict(list)
    for signal_id, ticker, signal_date in signal_ids:
        ticker_signals[ticker].append((signal_id, signal_date))

    unique_tickers = len(ticker_signals)
    _console.print(f"\n[bold cyan]Settlement[/bold cyan] {total:,} 訊號 / {unique_tickers:,} tickers")

    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.fields[ticker]}[/cyan]"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=_console,
        transient=False,
    ) as progress:
        task_id = progress.add_task("settle", total=unique_tickers, ticker="")

        with get_connection() as conn:
            for ticker, sigs in ticker_signals.items():
                progress.update(task_id, ticker=ticker)

                # Fetch OHLCV once per ticker covering all signal dates
                all_dates = [d for _, d in sigs]
                ohlcv_start = min(all_dates)
                ohlcv_end = max(all_dates) + timedelta(days=14)
                try:
                    df = finmind.fetch_ohlcv(ticker, ohlcv_start, ohlcv_end)
                except Exception as e:
                    logger.debug("settle ohlcv %s: %s", ticker, e)
                    skipped += len(sigs)
                    progress.advance(task_id)
                    continue

                if df.empty:
                    no_data += len(sigs)
                    progress.advance(task_id)
                    continue

                closes: dict[date, float] = {}
                for _, row in df.iterrows():
                    closes[row["trade_date"]] = float(row["close"])
                trading_days = sorted(closes.keys())

                for signal_id, signal_date in sigs:
                    if signal_date not in trading_days:
                        skipped += 1
                        continue
                    signal_idx = trading_days.index(signal_date)

                    def get_offset(n: int) -> float | None:
                        idx = signal_idx + n
                        if 0 <= idx < len(trading_days):
                            return closes[trading_days[idx]]
                        return None

                    p1 = get_offset(1)
                    p3 = get_offset(3)
                    p5 = get_offset(5)
                    entry = closes.get(signal_date)
                    if entry is None:
                        skipped += 1
                        continue

                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE signal_outcomes
                            SET price_1d = %s, price_3d = %s, price_5d = %s,
                                outcome_1d = CASE WHEN %s IS NOT NULL THEN (%s - %s) / %s ELSE NULL END,
                                outcome_3d = CASE WHEN %s IS NOT NULL THEN (%s - %s) / %s ELSE NULL END,
                                outcome_5d = CASE WHEN %s IS NOT NULL THEN (%s - %s) / %s ELSE NULL END
                            WHERE signal_id = %s
                        """, (
                            p1, p3, p5,
                            p1, p1, entry, entry,
                            p3, p3, entry, entry,
                            p5, p5, entry, entry,
                            signal_id,
                        ))
                    settled += 1

                progress.advance(task_id)

    _console.print(Panel(
        f"[bold green]Settlement 完成[/bold green]\n"
        f"  已結算  [green]{settled:,}[/green] 筆\n"
        f"  無資料  [yellow]{no_data:,}[/yellow] 筆\n"
        f"  跳過    [dim]{skipped:,}[/dim] 筆",
        border_style="green",
        padding=(0, 2),
    ))


def _load_industry_map(analysis_date: date, data_dir: Path) -> dict[str, str]:
    """Load ticker→industry map for a date (cache-first, then live fetch)."""
    for delta in range(0, 8):
        candidate = analysis_date - timedelta(days=delta)
        cache_file = data_dir / f"industry_map_{candidate}.json"
        if cache_file.exists():
            with open(cache_file) as f:
                return json.load(f)

    # No cache found — fetch live
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from batch_scan import _build_industry_map  # type: ignore[import]
    return _build_industry_map() or {}


def _resolve_sectors(industry_map: dict[str, str], sector_indices: list[int]) -> set[str]:
    """Map sector index numbers → industry name strings.

    Indices match the sorted sector table (1-based), same ordering as batch_scan.
    """
    sorted_sectors = sorted(set(industry_map.values()))
    idx_map = {i: name for i, name in enumerate(sorted_sectors, start=1)}
    selected: set[str] = set()
    for idx in sector_indices:
        if idx in idx_map:
            selected.add(idx_map[idx])
        else:
            print(f"  [警告] 產業代號 {idx} 超出範圍（最大 {len(idx_map)}），忽略")
    return selected


def _print_sector_table(industry_map: dict[str, str]) -> list[tuple[int, str, int]]:
    """Print the sector index table using Rich. Returns [(idx, name, count), ...]."""
    counts = Counter(industry_map.values())
    rows = [(i, name, counts[name]) for i, name in enumerate(sorted(counts.keys()), start=1)]

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
    return rows


def _prompt_sectors(rows: list[tuple[int, str, int]]) -> list[int]:
    """Interactive Rich prompt — returns list of sector indices. Empty = all sectors."""
    _console.print("\n[bold yellow]請輸入產業代號[/bold yellow]（空白分隔），直接 Enter 掃描全市場：")
    try:
        raw = _console.input("[bold cyan]> [/bold cyan]").strip()
    except EOFError:
        raw = ""
    if not raw:
        return []
    indices: list[int] = []
    idx_set = {i for i, _, _ in rows}
    for token in raw.split():
        try:
            n = int(token)
            if n in idx_set:
                indices.append(n)
            else:
                _console.print(f"  [red]忽略無效代號: {n}（最大 {len(rows)}）[/red]")
        except ValueError:
            _console.print(f"  [red]忽略無效輸入: {token}[/red]")
    return indices


def _llm_menu_backtest() -> str:
    """互動式選擇 LLM provider。回傳 provider key 字串（none / auto / gemini / claude / openai）。"""
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
    return provider_key


class _EmptyLabelRepo:
    def get_label(self, branch_code: str):
        return None
    def get_labels_bulk(self, codes):
        return {}


def run_backtest(
    date_from: date,
    date_to: date,
    tickers: list[str] | None,
    settle: bool,
    delay: float,
    llm: str | None = "auto",
    sectors: list[int] | None = None,
) -> None:
    init_pool()

    from taiwan_stock_agent.domain.llm_provider import create_llm_provider
    if llm == "none":
        llm_provider = _LLM_DISABLED
    else:
        llm_provider = create_llm_provider(None if llm == "auto" else llm)

    agent = StrategistAgent(
        finmind=FinMindClient(),
        label_repo=_EmptyLabelRepo(),
        chip_proxy_fetcher=ChipProxyFetcher(),
        llm_provider=llm_provider,
    )

    data_dir = Path(__file__).resolve().parents[1] / "data" / "watchlist_cache"
    trading_days = _date_range(date_from, date_to)

    # Resolve ticker universe (once, using the first trading day's industry_map)
    if tickers:
        day_tickers_default = tickers
        _console.print(f"  [dim]使用指定 tickers: {tickers}[/dim]")
    else:
        industry_map = _load_industry_map(date_from, data_dir)
        if not industry_map:
            _console.print("[red]無法載入 industry_map，中止[/red]")
            return

        rows = _print_sector_table(industry_map)

        if sectors is None:
            # 互動式輸入
            sectors = _prompt_sectors(rows)

        if sectors:
            selected_sectors = _resolve_sectors(industry_map, sectors)
            if not selected_sectors:
                _console.print("[red]沒有有效的產業代號，中止[/red]")
                return
            day_tickers_default = [t for t, ind in industry_map.items() if ind in selected_sectors]
            _console.print(f"  [green]選擇產業：{sorted(selected_sectors)}[/green]")
            _console.print(f"  [green]共 {len(day_tickers_default)} 隻股票[/green]")
        else:
            day_tickers_default = list(industry_map.keys())
            _console.print(f"  [green]全市場掃描：{len(day_tickers_default)} 隻股票[/green]")

    # ----------------------------------------------------------------
    # OHLCV Pre-warm: fetch full date range once per ticker.
    # Without this, each (ticker, day) call fetches a slightly different 95-day
    # window → 133,170 API calls. With pre-warm: 1,930 calls + memory slicing.
    # ----------------------------------------------------------------
    ohlcv_prefetch_start = date_from - timedelta(days=95)
    _console.print(f"\n[bold cyan]Phase 1/2[/bold cyan] OHLCV pre-fetch ({len(day_tickers_default)} tickers, {ohlcv_prefetch_start} ~ {date_to})")
    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.fields[ticker]}[/cyan]"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=_console,
        transient=True,
    ) as pre_progress:
        pre_task = pre_progress.add_task("prefetch", total=len(day_tickers_default), ticker="")
        for ticker in day_tickers_default:
            pre_progress.update(pre_task, ticker=ticker)
            try:
                agent._finmind.fetch_ohlcv(ticker, ohlcv_prefetch_start, date_to)
            except Exception as e:
                logger.debug("prefetch skip %s: %s", ticker, e)
            pre_progress.advance(pre_task)
    _console.print(f"  [green]OHLCV pre-fetch done — 主迴路改由 memory 提供資料[/green]\n")

    total = 0
    recorded: list[tuple[str, str, date]] = []
    total_tasks = len(trading_days) * len(day_tickers_default)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.fields[day]}[/bold cyan]"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=_console,
        transient=False,
    ) as progress:
        _console.print(f"[bold cyan]Phase 2/2[/bold cyan] 計分迴路 ({len(trading_days)} 天 × {len(day_tickers_default)} tickers = {total_tasks:,} 任務)")
        task_id = progress.add_task("backtest", total=total_tasks, day="starting...")

        for day in trading_days:
            day_tickers = day_tickers_default
            if not day_tickers:
                logger.warning("No tickers for %s, skipping", day)
                continue

            for ticker in day_tickers:
                progress.update(task_id, day=f"{day} {ticker}")
                try:
                    signal = agent.run(ticker, day)
                    if signal.halt_flag:
                        progress.advance(task_id)
                        continue
                    sid = record_signal(signal, source="backtest")
                    recorded.append((sid, ticker, day))
                    total += 1
                    if delay > 0:
                        time.sleep(delay)
                except Exception as e:
                    logger.warning("skip %s %s: %s", ticker, day, e)
                progress.advance(task_id)

    _console.print(Panel(
        f"[bold green]Backtest 完成[/bold green]  {total} 筆訊號已記錄",
        border_style="green",
        padding=(0, 2),
    ))

    if settle and recorded:
        _console.print(f"  [dim]Settling {len(recorded)} signals (T+1/T+3/T+5)...[/dim]")
        _settle_outcomes(recorded)
        _console.print("  [green]Settlement done.[/green]")


def _prompt_date(prompt: str, default: date) -> date:
    """Ask user for a date using Rich prompt. Enter = use default."""
    default_str = default.isoformat()
    _console.print(f"[bold yellow]{prompt}[/bold yellow] [dim](預設: {default_str})[/dim]")
    try:
        raw = _console.input("[bold cyan]> [/bold cyan]").strip()
    except EOFError:
        raw = ""
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError:
        _console.print(f"  [red]格式錯誤，使用預設值 {default_str}[/red]")
        return default


def main() -> None:
    parser = argparse.ArgumentParser(description="Historical backtest → signal_outcomes")
    parser.add_argument("--date-from", type=date.fromisoformat, default=None)
    parser.add_argument("--date-to", type=date.fromisoformat, default=None)
    parser.add_argument("--tickers", nargs="*", help="Limit to specific tickers (overrides --sectors)")
    parser.add_argument("--sectors", nargs="*", type=int, metavar="N",
                        help="Filter by sector index numbers (same as batch_scan menu)")
    parser.add_argument("--no-settle", action="store_true", help="Skip T+N outcome settlement")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds between tickers in main loop (default: 0 — OHLCV is pre-fetched)")
    parser.add_argument("--llm", default=None,
                        help="LLM provider: auto | none | anthropic | openai | gemini (default: 互動選單)")
    args = parser.parse_args()

    today = date.today()
    first_of_month = today.replace(day=1)

    # ----------------------------------------------------------------
    # Rich header panel
    # ----------------------------------------------------------------
    _console.print()
    _console.print(Panel(
        f"[bold white]Historical Backtest[/bold white]\n"
        f"[dim]TripleConfirmationEngine → signal_outcomes[/dim]",
        title="[bold cyan]Backtest[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))

    # ----------------------------------------------------------------
    # 互動式日期輸入（未傳 CLI 參數時）
    # ----------------------------------------------------------------
    date_from = args.date_from or _prompt_date("開始日期 (YYYY-MM-DD)", first_of_month)
    date_to   = args.date_to   or _prompt_date("結束日期 (YYYY-MM-DD)", today)

    # ----------------------------------------------------------------
    # LLM 選擇（未傳 CLI 參數時）
    # ----------------------------------------------------------------
    if args.llm is not None:
        llm_key = args.llm
    else:
        llm_key = _llm_menu_backtest()

    # ----------------------------------------------------------------
    # Settle 選擇（未傳 --no-settle 時）
    # ----------------------------------------------------------------
    if args.no_settle:
        do_settle = False
    else:
        _console.print("\n[bold yellow]是否執行 T+N Settlement？[/bold yellow] [dim](Y/n)[/dim]")
        try:
            settle_raw = _console.input("[bold cyan]> [/bold cyan]").strip().lower()
        except EOFError:
            settle_raw = ""
        do_settle = settle_raw not in ("n", "no", "否")

    # ----------------------------------------------------------------
    # Summary panel
    # ----------------------------------------------------------------
    trading_days_count = len([
        d for d in (date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1))
        if d.weekday() < 5
    ])
    llm_display = llm_key if llm_key else "none"
    settle_display = "是" if do_settle else "否"

    _console.print()
    _console.print(Panel(
        f"[bold white]開始日期[/bold white]  {date_from}\n"
        f"[bold white]結束日期[/bold white]  {date_to}\n"
        f"[bold white]交易天數[/bold white]  {trading_days_count} 天\n"
        f"[bold white]LLM 引擎[/bold white]  {llm_display}\n"
        f"[bold white]Settlement[/bold white] {settle_display}",
        title="[bold white]執行摘要[/bold white]",
        border_style="bright_black",
        padding=(0, 2),
    ))
    _console.print()

    run_backtest(
        date_from=date_from,
        date_to=date_to,
        tickers=args.tickers,
        settle=do_settle,
        delay=args.delay,
        llm=llm_key,
        sectors=args.sectors,
    )


if __name__ == "__main__":
    main()
