"""Interactive review gate for engine parameter tuning.

Reads latest factor report JSON, displays recommendations, prompts for approval.
On approve: updates config/engine_params.json + records to engine_versions table.

Usage:
    python scripts/apply_tuning.py
    python scripts/apply_tuning.py --auto-approve   # for cron
    python scripts/apply_tuning.py --dry-run
    make tune-review
    make tune-review AUTO_APPROVE=1
    make tune-review DRY_RUN=1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.domain.scoring_replay import load_params, DEFAULT_PARAMS
from taiwan_stock_agent.infrastructure.db import init_pool, get_connection

_PARAMS_PATH = Path(__file__).resolve().parents[1] / "config" / "engine_params.json"
_REPORT_DIR = Path(__file__).resolve().parents[1] / "data" / "factor_reports"

# Safety limits for AUTO_APPROVE mode
_MAX_CHANGE_PCT = 0.20   # block if any param changes by > 20%


def _load_latest_report() -> dict | None:
    reports = sorted(_REPORT_DIR.glob("factor_report_*.json"), reverse=True)
    if not reports:
        return None
    with open(reports[0]) as f:
        return json.load(f)


def _apply_params(new_params: dict, old_params: dict, reason: str, lift: float) -> None:
    """Write new params to JSON + record version history to DB."""
    full_params = {
        **old_params,
        **new_params,
        "_comment": "Tunable parameters. Edited by apply_tuning.py.",
    }
    with open(_PARAMS_PATH, "w") as f:
        json.dump(full_params, f, indent=2, ensure_ascii=False)

    try:
        init_pool()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO engine_versions (applied_at, params_before, params_after, reason, lift_estimate)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    datetime.now(),
                    json.dumps(old_params),
                    json.dumps(full_params),
                    reason,
                    lift,
                ))
    except Exception as e:
        print(f"[warn] Could not write to engine_versions table: {e}")


def _safety_check(old_params: dict, new_params: dict) -> list[str]:
    """Return list of violations. Empty = safe to auto-apply."""
    violations = []
    for k, new_val in new_params.items():
        old_val = old_params.get(k)
        if old_val and old_val != 0:
            change_pct = abs(new_val - old_val) / abs(old_val)
            if change_pct > _MAX_CHANGE_PCT:
                violations.append(
                    f"{k}: {old_val} → {new_val} ({change_pct:.0%} change > {_MAX_CHANGE_PCT:.0%} limit)"
                )
    return violations


def run_review(auto_approve: bool, dry_run: bool) -> None:
    report = _load_latest_report()
    if not report:
        print("No factor report found. Run `make factor-report` first.")
        return

    grid_results = report.get("grid_search_top5", [])
    if not grid_results:
        print("No grid search recommendations in latest report (insufficient data).")
        return

    old_params = load_params()
    best = grid_results[0]
    lift = best["avg_test_lift"]

    # Build diff
    changes = {
        k: (old_params.get(k), v)
        for k, v in best["params"].items()
        if old_params.get(k) != v
    }

    print(f"\n{'='*55}")
    print(f"  本週調參建議 (walk-forward lift: +{lift:.1%})")
    print(f"  報告日期: {report['report_date']}")
    print(f"{'='*55}")
    if changes:
        print(f"\n  {'參數':<28} {'目前值':>8}  →  {'建議值':>8}")
        print(f"  {'-'*48}")
        for k, (old, new) in changes.items():
            print(f"  {k:<28} {str(old):>8}  →  {str(new):>8}")
    else:
        print("  (現有參數已是最佳，無需調整)")
        return

    if dry_run:
        print("\n[DRY RUN] 不套用任何變更。")
        return

    # Safety check for auto_approve
    violations = _safety_check(old_params, best["params"])
    if violations and auto_approve:
        print(f"\n⚠ 安全限制觸發（AUTO_APPROVE 不可套用）：")
        for v in violations:
            print(f"  - {v}")
        print("請使用互動模式手動確認。")
        return

    if auto_approve and not violations:
        _apply_params(best["params"], old_params, f"auto-tune {report['report_date']}", lift)
        print(f"\n✅ 自動套用完成 (lift +{lift:.1%})")
        return

    # Interactive
    print(f"\n[A] 全部套用   [S] 略過本週   [D] 顯示所有候選\n")
    choice = input("選擇: ").strip().upper()

    if choice == "A":
        _apply_params(best["params"], old_params, f"manual-tune {report['report_date']}", lift)
        print(f"\n✅ 已套用到 config/engine_params.json")
    elif choice == "D":
        print("\n所有候選:")
        for i, g in enumerate(grid_results, 1):
            print(f"  {i}. lift=+{g['avg_test_lift']:.1%}  {g['params']}")
        idx_str = input("\n輸入編號套用 (Enter 略過): ").strip()
        if idx_str.isdigit() and 1 <= int(idx_str) <= len(grid_results):
            chosen = grid_results[int(idx_str) - 1]
            _apply_params(chosen["params"], old_params, f"manual-tune {report['report_date']}", chosen["avg_test_lift"])
            print(f"✅ 已套用 #{idx_str}")
        else:
            print("略過。")
    else:
        print("略過本週。")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto-approve", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_review(args.auto_approve, args.dry_run)


if __name__ == "__main__":
    main()
