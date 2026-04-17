"""Grid search + walk-forward parameter optimization for AccumulationEngine.

Searches over grade_thresholds (COIL_PRIME, COIL_MATURE) using a walk-forward
split: train on [date_from, date_to - 60d], test on last 60 days.

Usage:
    python scripts/optimize_coil.py --dry-run --date-from 2026-01-15 --date-to 2026-03-15 --tickers 2330 2454
    python scripts/optimize_coil.py --date-from 2025-10-01 --date-to 2026-02-28
    make optimize-coil DRY_RUN=1
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# Make sibling scripts importable (for coil_backtest imports)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv()

_console = Console()

_PARAMS_PATH = Path(__file__).resolve().parents[1] / "config" / "accumulation_params.json"

# Walk-forward test window in trading days (calendar days approximation)
_TEST_WINDOW_DAYS = 60

# Scoring weight for COIL_PRIME vs COIL_MATURE win rates
_PRIME_WEIGHT = 0.7

# Grid search parameter space
_PRIME_THRESHOLDS = [60, 65, 70, 75, 80]
_MATURE_THRESHOLDS = [40, 45, 50, 55, 60]
_EARLY_THRESHOLD = 35  # fixed


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _compute_score(prime_wr: float, mature_wr: float) -> float:
    """Weighted score: COIL_PRIME win_rate * 0.7 + COIL_MATURE win_rate * 0.3."""
    return prime_wr * _PRIME_WEIGHT + mature_wr * (1 - _PRIME_WEIGHT)


# ---------------------------------------------------------------------------
# Engine run with custom thresholds (no file mutation)
# ---------------------------------------------------------------------------

def _run_with_params(
    prime_thresh: int,
    mature_thresh: int,
    tickers: list[str],
    trading_dates: list[date],
    finmind,
    chip_fetcher,
    market_map: dict[str, str],
    taiex_history_full: list,
) -> dict[str, list[dict]]:
    """Run AccumulationEngine with overridden grade thresholds.

    Returns grade_groups: {grade: [signal_dicts]} for COIL_PRIME and COIL_MATURE.
    Imports coil_backtest internals locally to avoid circular imports.
    """
    from coil_backtest import _scan_ticker_on_date, _fetch_future_bars, _check_success  # type: ignore[import]
    from taiwan_stock_agent.domain.accumulation_engine import AccumulationEngine

    grade_groups: dict[str, list] = {"COIL_PRIME": [], "COIL_MATURE": []}

    custom_params = {
        "grade_thresholds": {
            "COIL_PRIME": prime_thresh,
            "COIL_MATURE": mature_thresh,
            "COIL_EARLY": _EARLY_THRESHOLD,
        },
        "raw_max_pts": 150,
    }

    def _taiex_up_to(d: date) -> list:
        return [b for b in taiex_history_full if b.trade_date <= d]

    for ticker in tickers:
        market = market_map.get(ticker, "TSE")
        for signal_date in trading_dates:
            time.sleep(0.05)  # light rate limiting
            try:
                taiex_slice = _taiex_up_to(signal_date)
                # Use standard scan but then re-grade with custom thresholds
                signal = _scan_ticker_on_date(
                    ticker, signal_date, finmind, chip_fetcher, market, taiex_slice
                )
                if signal is None:
                    continue

                # Re-grade using custom thresholds (override engine's stored grade)
                score = signal["score"]
                if score >= prime_thresh:
                    new_grade = "COIL_PRIME"
                elif score >= mature_thresh:
                    new_grade = "COIL_MATURE"
                else:
                    continue  # below MATURE threshold, skip

                future_bars = _fetch_future_bars(ticker, signal_date, finmind)
                success, days, ret = _check_success(
                    signal["entry_close"], signal["twenty_day_high"], future_bars
                )
                result = {
                    **signal,
                    "grade": new_grade,
                    "success": success,
                    "days_to_event": days,
                    "final_return": ret,
                }
                if new_grade in grade_groups:
                    grade_groups[new_grade].append(result)
            except Exception:
                continue

    return grade_groups


def _grade_win_rate(grade_group: list[dict]) -> float:
    """Return win rate for a list of signal results. Returns 0.0 if empty."""
    if not grade_group:
        return 0.0
    return sum(1 for r in grade_group if r["success"]) / len(grade_group)


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------

def _grid_search(
    tickers: list[str],
    trading_dates: list[date],
    finmind,
    chip_fetcher,
    market_map: dict[str, str],
    taiex_history_full: list,
    label: str = "train",
) -> list[dict]:
    """Run grid search over all valid (prime, mature) threshold combinations.

    Returns list of result dicts sorted by composite_score descending.
    """
    combos = [
        (p, m)
        for p in _PRIME_THRESHOLDS
        for m in _MATURE_THRESHOLDS
        if m < p  # MATURE must be strictly below PRIME
    ]

    _console.print(
        f"  [dim]Grid search: {len(combos)} combinations × {len(tickers)} tickers"
        f" × {len(trading_dates)} dates ({label})[/dim]"
    )

    results = []
    for i, (prime, mature) in enumerate(combos, 1):
        _console.print(
            f"  [{i:2d}/{len(combos)}] PRIME={prime} MATURE={mature} ...",
            end="",
        )
        try:
            grade_groups = _run_with_params(
                prime_thresh=prime,
                mature_thresh=mature,
                tickers=tickers,
                trading_dates=trading_dates,
                finmind=finmind,
                chip_fetcher=chip_fetcher,
                market_map=market_map,
                taiex_history_full=taiex_history_full,
            )
            prime_wr = _grade_win_rate(grade_groups.get("COIL_PRIME", []))
            mature_wr = _grade_win_rate(grade_groups.get("COIL_MATURE", []))
            n_prime = len(grade_groups.get("COIL_PRIME", []))
            n_mature = len(grade_groups.get("COIL_MATURE", []))
            composite = _compute_score(prime_wr, mature_wr)

            _console.print(
                f" PRIME {prime_wr*100:.1f}%(n={n_prime})"
                f" MATURE {mature_wr*100:.1f}%(n={n_mature})"
                f" → score={composite:.3f}"
            )
            results.append({
                "prime_thresh": prime,
                "mature_thresh": mature,
                "prime_wr": prime_wr,
                "mature_wr": mature_wr,
                "n_prime": n_prime,
                "n_mature": n_mature,
                "composite_score": composite,
            })
        except Exception as e:
            _console.print(f" [red]ERROR: {e}[/red]")

    return sorted(results, key=lambda x: x["composite_score"], reverse=True)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_grid_table(grid_results: list[dict], title: str, best_combo: tuple | None = None) -> None:
    tbl = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
        border_style="dim",
        title=f"[bold]{title}[/bold]",
        title_style="bold white",
    )
    tbl.add_column("Rank", justify="right", width=5)
    tbl.add_column("PRIME thresh", justify="right", width=12)
    tbl.add_column("MATURE thresh", justify="right", width=13)
    tbl.add_column("PRIME Win%", justify="right", width=11)
    tbl.add_column("N(PRIME)", justify="right", width=9)
    tbl.add_column("MATURE Win%", justify="right", width=12)
    tbl.add_column("N(MATURE)", justify="right", width=10)
    tbl.add_column("Composite", justify="right", width=10)

    for i, r in enumerate(grid_results[:10], 1):
        is_best = best_combo and (r["prime_thresh"] == best_combo[0] and r["mature_thresh"] == best_combo[1])
        rank_str = f"[bold green]{i}[/bold green]" if is_best else str(i)
        prime_wr_str = f"{r['prime_wr']*100:.1f}%"
        mature_wr_str = f"{r['mature_wr']*100:.1f}%"
        score_str = f"{r['composite_score']:.3f}"

        prime_color = "green" if r["prime_wr"] >= 0.5 else "red"
        mature_color = "green" if r["mature_wr"] >= 0.5 else "red"

        tbl.add_row(
            rank_str,
            str(r["prime_thresh"]),
            str(r["mature_thresh"]),
            f"[{prime_color}]{prime_wr_str}[/{prime_color}]",
            str(r["n_prime"]),
            f"[{mature_color}]{mature_wr_str}[/{mature_color}]",
            str(r["n_mature"]),
            f"[bold]{score_str}[/bold]" if is_best else score_str,
        )

    _console.print(tbl)


# ---------------------------------------------------------------------------
# Params file I/O
# ---------------------------------------------------------------------------

def _load_current_params() -> dict:
    if not _PARAMS_PATH.exists():
        return {"grade_thresholds": {"COIL_PRIME": 70, "COIL_MATURE": 50, "COIL_EARLY": 35}, "raw_max_pts": 150}
    with _PARAMS_PATH.open() as f:
        return json.load(f)


def _write_params(prime_thresh: int, mature_thresh: int, old_params: dict) -> None:
    """Write updated grade thresholds to accumulation_params.json."""
    new_params = {
        **old_params,
        "grade_thresholds": {
            "COIL_PRIME": prime_thresh,
            "COIL_MATURE": mature_thresh,
            "COIL_EARLY": old_params.get("grade_thresholds", {}).get("COIL_EARLY", _EARLY_THRESHOLD),
        },
        "_comment": "Tunable parameters for AccumulationEngine (Phase 4.20). Updated by optimize_coil.py.",
    }
    with _PARAMS_PATH.open("w", encoding="utf-8") as f:
        json.dump(new_params, f, indent=2, ensure_ascii=False)
    _console.print(f"\n  [green]Params written:[/green] {_PARAMS_PATH}")


# ---------------------------------------------------------------------------
# Walk-forward main
# ---------------------------------------------------------------------------

def run_optimize(
    date_from: date,
    date_to: date,
    tickers: list[str],
    workers: int,
    dry_run: bool,
    market_map: dict[str, str] | None = None,
) -> None:
    from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
    from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher
    from coil_backtest import _get_trading_dates  # type: ignore[import]
    from taiwan_stock_agent.domain.models import DailyOHLCV

    if market_map is None:
        market_map = {}

    finmind = FinMindClient()
    chip_fetcher = ChipProxyFetcher()

    # Walk-forward split
    test_split = date_to - timedelta(days=_TEST_WINDOW_DAYS)

    _console.print(Panel(
        f"[bold white]Coil Parameter Optimization[/bold white]\n"
        f"[dim]Walk-forward: train [{date_from} → {test_split}] · test [{test_split} → {date_to}][/dim]\n"
        f"[dim]Tickers: {len(tickers)} · Dry run: {dry_run}[/dim]",
        border_style="cyan",
        padding=(0, 2),
    ))

    if test_split <= date_from:
        _console.print(
            "[red]Date range too short for walk-forward split "
            f"(need > {_TEST_WINDOW_DAYS} days between date_from and date_to).[/red]"
        )
        return

    # Pre-fetch TAIEX history shared by all runs
    _console.print("[dim]Pre-fetching TAIEX history...[/dim]")
    lookback_days = (date_to - date_from).days + 200
    taiex_history_full: list[DailyOHLCV] = []
    try:
        taiex_df = finmind.fetch_taiex_history(date_to, lookback_days=lookback_days)
        if taiex_df is not None and not taiex_df.empty:
            for _, row in taiex_df.iterrows():
                taiex_history_full.append(DailyOHLCV(
                    ticker="TAIEX",
                    trade_date=row["trade_date"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row.get("volume", 0)),
                ))
        taiex_history_full = sorted(taiex_history_full, key=lambda x: x.trade_date)
    except Exception as e:
        _console.print(f"[yellow]Warning: could not fetch TAIEX history: {e}[/yellow]")

    # Fetch trading date calendars
    _console.print("[dim]Fetching trading calendars...[/dim]")
    train_dates = _get_trading_dates(date_from, test_split, finmind)
    test_dates = _get_trading_dates(test_split + timedelta(days=1), date_to, finmind)

    _console.print(
        f"  Train dates: [bold]{len(train_dates)}[/bold]  "
        f"Test dates: [bold]{len(test_dates)}[/bold]"
    )

    if not train_dates:
        _console.print("[red]No trading dates found for training period.[/red]")
        return

    # ---- Step 1: Grid search on training set ----
    _console.print(f"\n[bold cyan]Step 1:[/bold cyan] Grid search on training period")
    train_results = _grid_search(
        tickers=tickers,
        trading_dates=train_dates,
        finmind=finmind,
        chip_fetcher=chip_fetcher,
        market_map=market_map,
        taiex_history_full=taiex_history_full,
        label="train",
    )

    if not train_results:
        _console.print("[red]Grid search produced no results.[/red]")
        return

    best_train = train_results[0]
    best_combo = (best_train["prime_thresh"], best_train["mature_thresh"])
    _console.print(f"\n  Best on train: PRIME={best_combo[0]} MATURE={best_combo[1]}")
    _print_grid_table(train_results, title="Training Grid Search Results", best_combo=best_combo)

    # ---- Step 2: Evaluate best combo on test set ----
    test_results_final: dict | None = None
    if test_dates:
        _console.print(f"\n[bold cyan]Step 2:[/bold cyan] Evaluate best params on test period")
        try:
            grade_groups_test = _run_with_params(
                prime_thresh=best_combo[0],
                mature_thresh=best_combo[1],
                tickers=tickers,
                trading_dates=test_dates,
                finmind=finmind,
                chip_fetcher=chip_fetcher,
                market_map=market_map,
                taiex_history_full=taiex_history_full,
            )
            test_prime_wr = _grade_win_rate(grade_groups_test.get("COIL_PRIME", []))
            test_mature_wr = _grade_win_rate(grade_groups_test.get("COIL_MATURE", []))
            test_n_prime = len(grade_groups_test.get("COIL_PRIME", []))
            test_n_mature = len(grade_groups_test.get("COIL_MATURE", []))
            test_composite = _compute_score(test_prime_wr, test_mature_wr)

            test_results_final = {
                "prime_thresh": best_combo[0],
                "mature_thresh": best_combo[1],
                "prime_wr": test_prime_wr,
                "mature_wr": test_mature_wr,
                "n_prime": test_n_prime,
                "n_mature": test_n_mature,
                "composite_score": test_composite,
            }
            _print_grid_table([test_results_final], title="Test Set Results (best params)", best_combo=best_combo)
        except Exception as e:
            _console.print(f"[yellow]Warning: test evaluation failed: {e}[/yellow]")
    else:
        _console.print("[yellow]No test dates available, skipping test evaluation.[/yellow]")

    # ---- Step 3: Load current params and display diff ----
    old_params = _load_current_params()
    old_thresholds = old_params.get("grade_thresholds", {})
    old_prime = old_thresholds.get("COIL_PRIME", 70)
    old_mature = old_thresholds.get("COIL_MATURE", 50)

    new_prime, new_mature = best_combo

    _console.print("\n[bold]Parameter Change Summary:[/bold]")
    _console.print(f"  COIL_PRIME:  {old_prime} → [bold]{new_prime}[/bold]  "
                   f"{'[green](no change)[/green]' if old_prime == new_prime else ''}")
    _console.print(f"  COIL_MATURE: {old_mature} → [bold]{new_mature}[/bold]  "
                   f"{'[green](no change)[/green]' if old_mature == new_mature else ''}")

    if old_prime == new_prime and old_mature == new_mature:
        _console.print("\n[green]Current params are already optimal — no changes needed.[/green]")
        return

    if dry_run:
        _console.print("\n[dim][DRY RUN] Params not written.[/dim]")
        return

    # ---- Interactive confirmation gate ----
    _console.print()
    _console.print(Panel(
        f"[bold white]Apply these parameters?[/bold white]\n"
        f"  COIL_PRIME:  {old_prime} → [bold cyan]{new_prime}[/bold cyan]\n"
        f"  COIL_MATURE: {old_mature} → [bold cyan]{new_mature}[/bold cyan]",
        border_style="yellow",
        padding=(0, 2),
    ))

    try:
        confirm = input("Apply these parameters? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        confirm = ""

    if confirm in ("y", "yes"):
        _write_params(new_prime, new_mature, old_params)
        _console.print("[bold green]Params applied.[/bold green]")
    else:
        _console.print("[dim]Params not applied.[/dim]")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Grid search + walk-forward optimization for AccumulationEngine grade thresholds."
    )
    parser.add_argument(
        "--date-from",
        default=(date.today() - timedelta(days=120)).isoformat(),
        help="Start of optimization window YYYY-MM-DD (default: 120 days ago)",
    )
    parser.add_argument(
        "--date-to",
        default=(date.today() - timedelta(days=30)).isoformat(),
        help="End of optimization window YYYY-MM-DD (default: 30 days ago)",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        help="Ticker symbols to include (required if not using sector defaults)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel worker count (default: 4, currently unused — sequential for stability)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run optimization but do not write params to file",
    )
    args = parser.parse_args()

    date_from = date.fromisoformat(args.date_from)
    date_to = date.fromisoformat(args.date_to)

    if date_from >= date_to:
        _console.print("[red]Error: --date-from must be before --date-to[/red]")
        sys.exit(1)

    # Resolve tickers
    if args.tickers:
        tickers = args.tickers
        market_map: dict[str, str] = {}
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from batch_plan import _build_market_map  # type: ignore[import]
            market_map = _build_market_map()
        except Exception:
            pass
    else:
        _console.print("[dim]Loading ticker universe from batch_plan...[/dim]")
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from batch_plan import _build_industry_map, _build_market_map, _DEFAULT_SECTOR_NAMES  # type: ignore[import]
            industry_map = _build_industry_map()
            market_map = _build_market_map()
            tickers = sorted(t for t, ind in industry_map.items() if ind in _DEFAULT_SECTOR_NAMES)
        except Exception as e:
            _console.print(f"[red]Could not load ticker universe: {e}[/red]")
            _console.print("[dim]Use --tickers to specify tickers explicitly.[/dim]")
            sys.exit(1)

        if not tickers:
            _console.print("[red]No tickers found. Use --tickers to specify explicitly.[/red]")
            sys.exit(1)
        _console.print(f"  Loaded [bold]{len(tickers)}[/bold] tickers")

    run_optimize(
        date_from=date_from,
        date_to=date_to,
        tickers=tickers,
        workers=args.workers,
        dry_run=args.dry_run,
        market_map=market_map,
    )


if __name__ == "__main__":
    main()
