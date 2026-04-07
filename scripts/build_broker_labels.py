"""Build broker label database from FinMind historical data.

Fetches 2 years of broker trades + OHLCV for a broad set of liquid Taiwan stocks,
runs BrokerLabelClassifier.fit(), and writes results to PostgreSQL.

Usage:
    python scripts/build_broker_labels.py                        # default 60 liquid tickers
    python scripts/build_broker_labels.py --tickers 2330 2454   # custom ticker list
    python scripts/build_broker_labels.py --start-date 2024-01-01
    python scripts/build_broker_labels.py --dry-run             # show stats, don't write DB
    python scripts/build_broker_labels.py --workers 3           # parallel fetch (default: 3)

make build-labels
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, TaskProgressColumn
    from rich.panel import Panel
    _console = Console()
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False
    _console = None  # type: ignore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
from taiwan_stock_agent.domain.broker_label_classifier import (
    BrokerLabelClassifier,
    PostgresBrokerLabelRepository,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 代表性液流台股清單 — 涵蓋半導體、電子、金融、傳產等主力板塊
# 目的：讓各大券商分點出現足夠次數（≥50 樣本）以供分類
# ---------------------------------------------------------------------------
_DEFAULT_TICKERS = [
    # 半導體 / IC 設計
    "2330", "2454", "2303", "2379", "3711", "2408", "2344", "2388",
    "2337", "3034", "2385", "2449", "2404",
    # 被動元件 / 電子零組件
    "2317", "2382", "2356", "2324", "6669", "3231", "2357", "2353",
    "2308", "2409", "3481", "2368", "2376",
    # 網通 / PCB
    "3008", "2365", "4938", "3706", "2377",
    # 面板
    "2412", "3481",
    # 金融
    "2884", "2882", "2891", "2886", "2881", "2892", "2883",
    # 傳產 / 電信
    "1301", "1303", "2412", "4904", "3045",
    # 其他電子
    "2395", "3533", "6415", "8046", "3035",
]
# 去重並排序
_DEFAULT_TICKERS = sorted(set(_DEFAULT_TICKERS))


def _print(msg: str) -> None:
    if _HAS_RICH and _console:
        _console.print(msg)
    else:
        print(msg)


def fetch_ticker_data(
    client: FinMindClient,
    ticker: str,
    start_date: date,
    end_date: date,
) -> tuple[str, object, object]:
    """Fetch broker trades + OHLCV for one ticker. Returns (ticker, broker_df, ohlcv_df)."""
    import pandas as pd

    try:
        broker_df = client.fetch_broker_trades(ticker, start_date, end_date, use_cache=True)
    except Exception as e:
        logger.warning("broker_trades fetch failed for %s: %s", ticker, e)
        broker_df = pd.DataFrame()

    try:
        ohlcv_df = client.fetch_ohlcv(ticker, start_date, end_date, use_cache=True)
    except Exception as e:
        logger.warning("ohlcv fetch failed for %s: %s", ticker, e)
        ohlcv_df = pd.DataFrame()

    return ticker, broker_df, ohlcv_df


def build_labels(
    tickers: list[str],
    start_date: date,
    end_date: date,
    workers: int = 3,
    dry_run: bool = False,
) -> None:
    import pandas as pd

    client = FinMindClient()

    _print(Panel(
        f"[bold white]標的數量[/bold white]  {len(tickers)} 檔\n"
        f"[bold white]資料區間[/bold white]  {start_date} → {end_date}\n"
        f"[bold white]並行執行[/bold white]  {workers} workers\n"
        f"[bold white]Dry run[/bold white]   {'是（不寫 DB）' if dry_run else '否（寫入 PostgreSQL）'}",
        title="[bold cyan]Broker Label Builder[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ) if _HAS_RICH else
    f"[Broker Label Builder] {len(tickers)} tickers, {start_date}→{end_date}, dry_run={dry_run}"
    )

    all_broker_dfs: list = []
    all_ohlcv_dfs: list = []
    failed: list[str] = []

    # --- Fetch data for all tickers ---
    if _HAS_RICH and _console:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=30, style="cyan", complete_style="green"),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=_console,
            transient=False,
        ) as progress:
            task = progress.add_task(f"抓取分點資料", total=len(tickers))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(fetch_ticker_data, client, t, start_date, end_date): t
                    for t in tickers
                }
                for future in as_completed(futures):
                    ticker, broker_df, ohlcv_df = future.result()
                    if not broker_df.empty:
                        all_broker_dfs.append(broker_df)
                    if not ohlcv_df.empty:
                        all_ohlcv_dfs.append(ohlcv_df)
                    if broker_df.empty and ohlcv_df.empty:
                        failed.append(ticker)
                        label = f"[dim]{ticker:<8}[/dim] [red]FAILED[/red]"
                    elif broker_df.empty:
                        label = f"[dim]{ticker:<8}[/dim] [yellow]no broker data[/yellow] (free tier?)"
                    else:
                        label = f"[dim]{ticker:<8}[/dim] [green]{len(broker_df)}[/green] broker rows"
                    progress.console.print(label)
                    progress.update(task, advance=1)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(fetch_ticker_data, client, t, start_date, end_date): t
                for t in tickers
            }
            for i, future in enumerate(as_completed(futures), 1):
                ticker, broker_df, ohlcv_df = future.result()
                if not broker_df.empty:
                    all_broker_dfs.append(broker_df)
                if not ohlcv_df.empty:
                    all_ohlcv_dfs.append(ohlcv_df)
                if broker_df.empty and ohlcv_df.empty:
                    failed.append(ticker)
                print(f"[{i}/{len(tickers)}] {ticker}: {len(broker_df)} broker rows")

    if not all_broker_dfs:
        _print("\n[red]❌ 沒有任何分點資料可供分類。請確認 FINMIND_API_KEY 及 FinMind 訂閱方案（需含 TaiwanStockBrokerTradingStatement）。[/red]")
        sys.exit(1)

    combined_broker = pd.concat(all_broker_dfs, ignore_index=True)
    combined_ohlcv = pd.concat(all_ohlcv_dfs, ignore_index=True)

    _print(f"\n[bold]合計分點資料:[/bold] {len(combined_broker):,} 筆")
    _print(f"[bold]合計 OHLCV:  [/bold] {len(combined_ohlcv):,} 筆")
    _print(f"[bold]失敗標的:    [/bold] {len(failed)} 檔 {failed if failed else ''}")

    # --- Run classifier ---
    _print("\n[bold cyan]▶ 執行 BrokerLabelClassifier...[/bold cyan]")

    if dry_run:
        # Dry run: use in-memory repo
        from collections import defaultdict

        class _MemRepo:
            def __init__(self):
                self._store = {}
            def get(self, code):
                return self._store.get(code)
            def upsert(self, label):
                self._store[label.branch_code] = label
            def list_all(self):
                return list(self._store.values())

        repo = _MemRepo()
    else:
        from taiwan_stock_agent.infrastructure.db import init_pool
        try:
            init_pool()
        except Exception as e:
            _print(f"\n[red]❌ 無法連接 PostgreSQL: {e}[/red]")
            _print("[dim]請設定 DATABASE_URL 環境變數，例如: postgresql://localhost/stock_agent[/dim]")
            sys.exit(1)
        repo = PostgresBrokerLabelRepository(None)

    classifier = BrokerLabelClassifier(repo)
    t0 = time.time()
    labels = classifier.fit(combined_broker, combined_ohlcv, as_of=end_date)
    elapsed = time.time() - t0

    # --- Summary table ---
    daytrade = [b for b in labels.values() if b.label == "隔日沖"]
    unknown = [b for b in labels.values() if b.label == "unknown"]
    enough_samples = [b for b in labels.values() if b.sample_count >= 50]
    insufficient = [b for b in labels.values() if b.sample_count < 50]

    _print(f"\n[bold green]✓ 分類完成[/bold green] ({elapsed:.1f}s)")
    _print(f"  分點總數:          {len(labels):>6,}")
    _print(f"  樣本 ≥50（可分類）: {len(enough_samples):>6,}")
    _print(f"  樣本不足（<50）:    {len(insufficient):>6,}")
    _print(f"  隔日沖:            {len(daytrade):>6,}")
    _print(f"  unknown:           {len(unknown):>6,}")

    # Top 20 隔日沖 branches by reversal_rate
    if daytrade:
        daytrade.sort(key=lambda b: b.reversal_rate, reverse=True)
        if _HAS_RICH and _console:
            table = Table(
                title="Top 20 隔日沖分點（reversal_rate 最高）",
                box=box.ROUNDED,
                header_style="bold cyan",
                border_style="bright_black",
            )
            table.add_column("分點代號", style="dim", width=10)
            table.add_column("分點名稱", min_width=20)
            table.add_column("reversal_rate", justify="right", style="red")
            table.add_column("樣本數", justify="right", style="green")
            for b in daytrade[:20]:
                table.add_row(
                    b.branch_code,
                    b.branch_name,
                    f"{b.reversal_rate:.1%}",
                    str(b.sample_count),
                )
            _console.print()
            _console.print(table)
        else:
            print("\nTop 20 隔日沖分點:")
            for b in daytrade[:20]:
                print(f"  {b.branch_code:10} {b.branch_name:25} {b.reversal_rate:.1%}  ({b.sample_count} samples)")

    if dry_run:
        _print("\n[yellow]⚠ Dry run 模式：結果未寫入資料庫。移除 --dry-run 以實際寫入。[/yellow]")
    else:
        _print(f"\n[bold green]✓ {len(labels)} 筆標籤已寫入 PostgreSQL broker_labels 表[/bold green]")


def main() -> None:
    parser = argparse.ArgumentParser(description="建立 broker_labels 資料庫")
    parser.add_argument(
        "--tickers",
        nargs="+",
        help="自訂標的清單（預設使用內建 60 檔液流股）",
    )
    parser.add_argument(
        "--start-date",
        type=lambda s: date.fromisoformat(s),
        default=date.today() - timedelta(days=730),  # 2 years
        help="資料起始日 YYYY-MM-DD（預設: 兩年前）",
    )
    parser.add_argument(
        "--end-date",
        type=lambda s: date.fromisoformat(s),
        default=date.today(),
        help="資料截止日 YYYY-MM-DD（預設: 今天）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="並行 worker 數（預設: 3；受 FinMind rate limit 限制）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只分析，不寫入資料庫",
    )
    args = parser.parse_args()

    tickers = args.tickers if args.tickers else _DEFAULT_TICKERS

    build_labels(
        tickers=tickers,
        start_date=args.start_date,
        end_date=args.end_date,
        workers=args.workers,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
