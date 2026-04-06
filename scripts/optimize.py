# scripts/optimize.py
"""One-shot optimization orchestrator: settle → factor-report → tune-review.

Usage:
    python scripts/optimize.py              # interactive (tune-review prompts)
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

# Add src/ for taiwan_stock_agent package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# Add scripts/ dir so sibling scripts are importable directly
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="Full optimization loop")
    parser.add_argument("--auto-approve", action="store_true",
                        help="Apply recommendations without interactive prompt (cron mode)")
    parser.add_argument("--skip-settle", action="store_true",
                        help="Skip settlement step (if already run separately)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only — do not write any changes")
    parser.add_argument("--days", type=int, default=180,
                        help="Days of history for factor report (default: 180)")
    args = parser.parse_args()

    today = date.today()
    print(f"\n{'='*55}")
    print(f"  Factor Optimization Loop  [{today}]")
    print(f"{'='*55}\n")

    # Step 1: Settle
    if not args.skip_settle:
        print("Step 1/3: 補填未結算訊號...")
        from daily_runner import run_settle
        try:
            run_settle(today)
        except Exception as e:
            print(f"  WARNING: Settle failed: {e} — continuing...")
    else:
        print("Step 1/3: 略過 settle（--skip-settle）")

    # Step 2: Factor report
    print("\nStep 2/3: 跑因子分析 + Grid Search...")
    from factor_report import run_report
    report_path = run_report(days=args.days, min_samples=10, scoring_version=None)
    if report_path is None:
        print("  WARNING: Factor report failed or insufficient data — stopping.")
        sys.exit(1)

    # Step 3: Apply tuning
    if args.dry_run:
        print("\nStep 3/3: [DRY RUN] 略過套用調參。")
        return

    print("\nStep 3/3: 審核調參建議...")
    from apply_tuning import run_review
    run_review(auto_approve=args.auto_approve, dry_run=False)

    print(f"\n{'='*55}")
    print("  Optimization loop complete.")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
