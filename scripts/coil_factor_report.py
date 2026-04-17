"""Coil factor lift analysis.

Reads a backtest results CSV (from coil_backtest.py --save-results) and computes
per-factor lift: win_rate_present / win_rate_absent for each of the 15 score_breakdown
factors.

Usage:
    python scripts/coil_factor_report.py
    python scripts/coil_factor_report.py --results-file data/backtest/coil_backtest_20260416.csv
    python scripts/coil_factor_report.py --min-samples 20
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

_console = Console()

# Canonical factor order (matches AccumulationEngine.score_full factors list)
FACTOR_NAMES = [
    "bb_compression",
    "volume_dryup",
    "consolidation_range",
    "atr_contraction",
    "inside_bars",
    "ma_convergence",
    "obv_trend",
    "kd_low_flat",
    "close_above_midline",
    "inst_consec",
    "inst_net_trend",
    "updown_volume",
    "market_strength",
    "proximity_resistance",
    "prior_advance",
]

_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "backtest"


# ---------------------------------------------------------------------------
# Core lift computation (pure functions — testable without I/O)
# ---------------------------------------------------------------------------

def compute_lift(
    present_wins: int,
    present_total: int,
    absent_wins: int,
    absent_total: int,
) -> float | None:
    """Compute lift = win_rate_present / win_rate_absent.

    Returns None on division by zero (absent_total == 0 or absent win_rate == 0).
    """
    if present_total == 0 or absent_total == 0:
        return None
    wr_present = present_wins / present_total
    wr_absent = absent_wins / absent_total
    if wr_absent == 0:
        return None
    return wr_present / wr_absent


def factor_status(lift: float | None) -> str:
    """Return status label based on lift value."""
    if lift is None:
        return "N/A"
    if lift >= 1.2:
        return "✓ STRONG"
    if lift < 1.05:
        return "⚠ WEAK"
    return "— OK"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _find_latest_csv() -> Path | None:
    """Return the most recently dated coil_backtest_*.csv file, or None."""
    candidates = sorted(_DATA_DIR.glob("coil_backtest_*.csv"), reverse=True)
    return candidates[0] if candidates else None


def _load_results(path: Path) -> list[dict]:
    """Load rows from backtest CSV. score_breakdown is parsed from JSON string."""
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                breakdown_raw = row.get("score_breakdown", "{}")
                row["score_breakdown"] = json.loads(breakdown_raw) if breakdown_raw else {}
            except json.JSONDecodeError:
                row["score_breakdown"] = {}
            # Normalize success field to bool
            row["_success"] = row.get("success", "").lower() in ("true", "1", "yes")
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _compute_factor_stats(
    rows: list[dict],
    min_samples: int,
) -> list[dict]:
    """For each factor, compute present/absent win rates and lift."""
    stats = []
    for factor in FACTOR_NAMES:
        present_wins = 0
        present_total = 0
        absent_wins = 0
        absent_total = 0

        for row in rows:
            breakdown = row.get("score_breakdown", {})
            pts = breakdown.get(factor, 0)
            success = row["_success"]

            if pts > 0:
                present_total += 1
                if success:
                    present_wins += 1
            else:
                absent_total += 1
                if success:
                    absent_wins += 1

        skip = present_total < min_samples or absent_total < min_samples
        lift = compute_lift(present_wins, present_total, absent_wins, absent_total)

        wr_present = present_wins / present_total if present_total > 0 else None
        wr_absent = absent_wins / absent_total if absent_total > 0 else None

        stats.append({
            "factor": factor,
            "n_present": present_total,
            "n_absent": absent_total,
            "wins_present": present_wins,
            "wins_absent": absent_wins,
            "wr_present": wr_present,
            "wr_absent": wr_absent,
            "lift": lift,
            "status": factor_status(lift),
            "skipped": skip,
        })

    return stats


def _grade_win_rates(rows: list[dict]) -> dict[str, tuple[int, int]]:
    """Return {grade: (wins, total)} for each grade found."""
    grade_map: dict[str, tuple[int, int]] = {}
    for row in rows:
        grade = row.get("grade", "UNKNOWN")
        wins, total = grade_map.get(grade, (0, 0))
        total += 1
        if row["_success"]:
            wins += 1
        grade_map[grade] = (wins, total)
    return grade_map


# ---------------------------------------------------------------------------
# Rich output
# ---------------------------------------------------------------------------

def _print_factor_table(stats: list[dict], min_samples: int) -> None:
    """Print Rich table of factor lift, sorted by lift descending."""
    tbl = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
        border_style="dim",
        title="[bold]Factor Lift Analysis[/bold]",
        title_style="bold white",
    )
    tbl.add_column("Factor", width=24)
    tbl.add_column("N(present)", justify="right", width=10)
    tbl.add_column("Win%(present)", justify="right", width=13)
    tbl.add_column("N(absent)", justify="right", width=10)
    tbl.add_column("Win%(absent)", justify="right", width=12)
    tbl.add_column("Lift", justify="right", width=6)
    tbl.add_column("Status", width=12)

    # Sort by lift descending; None lift goes to bottom
    def _sort_key(s: dict):
        if s["lift"] is None:
            return -999.0
        return s["lift"]

    sorted_stats = sorted(stats, key=_sort_key, reverse=True)

    for s in sorted_stats:
        factor = s["factor"]
        n_p = s["n_present"]
        n_a = s["n_absent"]
        wr_p = s["wr_present"]
        wr_a = s["wr_absent"]
        lift = s["lift"]
        status = s["status"]
        skipped = s["skipped"]

        # Dim skipped rows
        dim = "[dim]" if skipped else ""
        dim_close = "[/dim]" if skipped else ""

        wr_p_str = f"{wr_p*100:.1f}%" if wr_p is not None else "N/A"
        wr_a_str = f"{wr_a*100:.1f}%" if wr_a is not None else "N/A"
        lift_str = f"{lift:.2f}" if lift is not None else "N/A"

        if skipped:
            status_str = f"[dim](< {min_samples} samples)[/dim]"
        elif status == "✓ STRONG":
            status_str = "[bold green]✓ STRONG[/bold green]"
        elif status == "⚠ WEAK":
            status_str = "[bold red]⚠ WEAK[/bold red]"
        else:
            status_str = "[yellow]— OK[/yellow]"

        tbl.add_row(
            f"{dim}{factor}{dim_close}",
            f"{dim}{n_p}{dim_close}",
            f"{dim}{wr_p_str}{dim_close}",
            f"{dim}{n_a}{dim_close}",
            f"{dim}{wr_a_str}{dim_close}",
            f"{dim}{lift_str}{dim_close}",
            status_str,
        )

    _console.print(tbl)


def _print_summary(rows: list[dict], stats: list[dict], min_samples: int) -> None:
    """Print overall win rate, grade breakdown, and recommendations."""
    total = len(rows)
    wins = sum(1 for r in rows if r["_success"])
    overall_wr = wins / total if total > 0 else 0.0

    _console.print(f"\n[bold]Overall Win Rate:[/bold] {overall_wr*100:.1f}%  ({wins}/{total} signals)")

    # Win rate by grade
    grade_stats = _grade_win_rates(rows)
    grade_order = ["COIL_PRIME", "COIL_MATURE", "COIL_EARLY"]
    grade_colors = {
        "COIL_PRIME": "bold magenta",
        "COIL_MATURE": "bold cyan",
        "COIL_EARLY": "yellow",
    }

    _console.print("\n[bold]Win Rate by Grade:[/bold]")
    for grade in grade_order:
        if grade not in grade_stats:
            continue
        g_wins, g_total = grade_stats[grade]
        g_wr = g_wins / g_total if g_total > 0 else 0.0
        color = grade_colors.get(grade, "white")
        color_wr = "green" if g_wr >= 0.5 else "red"
        _console.print(
            f"  [{color}]{grade}[/{color}]  "
            f"[{color_wr}]{g_wr*100:.1f}%[/{color_wr}]  "
            f"({g_wins}/{g_total})"
        )

    # Recommendations: factors with lift < 1.05 (and sufficient samples)
    weak = [
        s["factor"] for s in stats
        if not s["skipped"] and s["lift"] is not None and s["lift"] < 1.05
    ]
    if weak:
        _console.print(
            f"\n[bold yellow]Recommendation — factors to review (lift < 1.05):[/bold yellow]"
        )
        for f in weak:
            _console.print(f"  • {f}")
    else:
        _console.print(
            "\n[bold green]No weak factors detected (all lift ≥ 1.05 where sampled).[/bold green]"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_factor_report(results_file: Path | None, min_samples: int) -> None:
    # Resolve results file
    if results_file is None:
        results_file = _find_latest_csv()
        if results_file is None:
            _console.print(
                "[yellow]No results file found.[/yellow]\n"
                "Run [bold]make coil-backtest --save-results[/bold] first, or pass "
                "[bold]--results-file[/bold] explicitly.\n"
                f"Expected directory: [dim]{_DATA_DIR}[/dim]"
            )
            return

    if not results_file.exists():
        _console.print(f"[red]Results file not found: {results_file}[/red]")
        return

    _console.print(Panel(
        f"[bold white]Coil Factor Lift Report[/bold white]\n"
        f"[dim]{results_file}[/dim]",
        border_style="cyan",
        padding=(0, 2),
    ))

    rows = _load_results(results_file)

    if len(rows) == 0:
        _console.print("[red]Results file is empty.[/red]")
        return

    if len(rows) < 10:
        _console.print(
            f"[yellow]Warning: only {len(rows)} rows — results may not be statistically meaningful.[/yellow]"
        )

    stats = _compute_factor_stats(rows, min_samples=min_samples)
    _print_factor_table(stats, min_samples=min_samples)
    _print_summary(rows, stats, min_samples=min_samples)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Coil factor lift analysis — reads coil_backtest CSV and computes per-factor win-rate lift."
    )
    parser.add_argument(
        "--results-file",
        type=Path,
        default=None,
        help="Path to coil_backtest_YYYYMMDD.csv (auto-detects latest if omitted)",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=10,
        help="Skip factor rows with fewer than this many samples in either present/absent group (default: 10)",
    )
    args = parser.parse_args()
    run_factor_report(results_file=args.results_file, min_samples=args.min_samples)


if __name__ == "__main__":
    main()
