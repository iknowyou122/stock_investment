# scripts/optimize.py
"""One-shot optimization orchestrator: settle → factor-report → tune-review.

Usage:
    python scripts/optimize.py              # interactive
    python scripts/optimize.py --auto-approve   # fully automated (cron-safe)
    python scripts/optimize.py --skip-settle    # skip settlement step
    python scripts/optimize.py --dry-run        # report only, no changes
    make optimize
    make optimize AUTO_APPROVE=1
    make optimize DRY_RUN=1
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel

# Add src/ for taiwan_stock_agent package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# Add scripts/ dir so sibling scripts are importable directly
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv()

_console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Full optimization loop")
    parser.add_argument("--auto-approve", action="store_true",
                        help="Apply recommendations without interactive prompt (cron mode)")
    parser.add_argument("--skip-settle", action="store_true",
                        help="Skip settlement step (if already run separately)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only — do not write any changes")
    parser.add_argument("--days", type=int, default=None,
                        help="Days of history for factor report (default: 180, or interactive)")
    args = parser.parse_args()

    today = date.today()

    # ----------------------------------------------------------------
    # Rich header
    # ----------------------------------------------------------------
    _console.print()
    _console.print(Panel(
        "[bold white]Factor Optimization Loop[/bold white]\n"
        "[dim]settle → factor-report → tune-review[/dim]",
        title="[bold cyan]Optimize[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))

    # ----------------------------------------------------------------
    # Interactive mode selection (only when no --auto-approve / --dry-run)
    # ----------------------------------------------------------------
    # Determine mode
    if args.auto_approve:
        mode = "auto"
    elif args.dry_run:
        mode = "dry"
    else:
        _MODES = [
            ("interactive", "Interactive — 每步驟確認"),
            ("auto",        "Auto-approve — 全自動套用（cron 模式）"),
            ("dry",         "Dry-run — 只報告，不寫入任何變更"),
        ]
        mode_table = Table(box=box.SIMPLE, show_header=False, border_style="bright_black")
        mode_table.add_column("#", style="bold cyan", justify="right", width=3)
        mode_table.add_column("模式", style="white")
        for i, (_, label) in enumerate(_MODES, 1):
            mode_table.add_row(str(i), label)
        _console.print()
        _console.print(Panel(mode_table, title="[bold white]執行模式[/bold white]", border_style="bright_black"))
        _console.print("\n[bold yellow]請輸入代號[/bold yellow]，直接 Enter 使用 [dim][1 Interactive][/dim]")
        raw_mode = _console.input("[bold cyan]> [/bold cyan]").strip()
        if raw_mode.isdigit() and 1 <= int(raw_mode) <= len(_MODES):
            mode = _MODES[int(raw_mode) - 1][0]
        else:
            mode = "interactive"
        mode_label = next(l for k, l in _MODES if k == mode)
        _console.print(f"  [dim]→ {mode_label}[/dim]")

    # Skip settle?
    if args.skip_settle:
        skip_settle = True
    elif mode == "auto":
        skip_settle = False
    else:
        _console.print("\n[bold yellow]是否跳過 Settle 步驟？[/bold yellow] [dim](y/N)[/dim]")
        raw_settle = _console.input("[bold cyan]> [/bold cyan]").strip().lower()
        skip_settle = raw_settle in ("y", "yes", "是")

    # Days of history
    if args.days is not None:
        days = args.days
    elif mode == "auto":
        days = 180
    else:
        _console.print("\n[bold yellow]回溯天數（因子分析）[/bold yellow] [dim](預設: 180)[/dim]")
        raw_days = _console.input("[bold cyan]> [/bold cyan]").strip()
        try:
            days = int(raw_days) if raw_days else 180
        except ValueError:
            days = 180

    # Summary panel
    mode_display = {"interactive": "Interactive", "auto": "Auto-approve", "dry": "Dry-run"}[mode]
    settle_display = "跳過" if skip_settle else "執行"
    _console.print()
    _console.print(Panel(
        f"[bold white]日期[/bold white]        {today}\n"
        f"[bold white]執行模式[/bold white]    {mode_display}\n"
        f"[bold white]Settle[/bold white]      {settle_display}\n"
        f"[bold white]回溯天數[/bold white]    {days} 天",
        title="[bold white]執行摘要[/bold white]",
        border_style="bright_black",
        padding=(0, 2),
    ))
    _console.print()

    # ----------------------------------------------------------------
    # Step 1: Settle
    # ----------------------------------------------------------------
    _console.print(Panel(
        "[bold white]Step 1/3[/bold white]  補填未結算訊號",
        border_style="bright_black",
        padding=(0, 1),
    ))
    if not skip_settle:
        from daily_runner import run_settle
        try:
            run_settle(today)
        except Exception as e:
            _console.print(f"  [yellow]WARNING: Settle failed: {e} — continuing...[/yellow]")
    else:
        _console.print("  [dim]略過 settle（skip-settle 模式）[/dim]")

    # ----------------------------------------------------------------
    # Step 2: Factor report
    # ----------------------------------------------------------------
    _console.print()
    _console.print(Panel(
        "[bold white]Step 2/3[/bold white]  跑因子分析 + Grid Search",
        border_style="bright_black",
        padding=(0, 1),
    ))
    from factor_report import run_report
    report_path = run_report(days=days, min_samples=10, scoring_version=None)
    if report_path is None:
        _console.print("  [red]WARNING: Factor report failed or insufficient data — stopping.[/red]")
        sys.exit(1)

    # ----------------------------------------------------------------
    # Step 3: Apply tuning
    # ----------------------------------------------------------------
    _console.print()
    _console.print(Panel(
        "[bold white]Step 3/3[/bold white]  審核調參建議",
        border_style="bright_black",
        padding=(0, 1),
    ))
    if mode == "dry":
        _console.print("  [dim][DRY RUN] 略過套用調參。[/dim]")
    else:
        auto_approve = (mode == "auto")
        from apply_tuning import run_review
        run_review(auto_approve=auto_approve, dry_run=False)

    _console.print()
    _console.print(Panel(
        "[bold green]Optimization loop complete.[/bold green]",
        border_style="green",
        padding=(0, 2),
    ))


if __name__ == "__main__":
    main()
