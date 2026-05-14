"""Automated plan→surge validation + parameter self-optimization.

Pipeline:
  1. plan_surge_label  — label unsettled plan signals WIN/MISS from surge CSVs
  2. factor_report     — grid search + walk-forward on labeled outcomes
  3. auto_apply        — apply params if safe (< 20% change); else save pending

Safe changes are applied immediately to config/engine_params.json and logged to
engine_versions. Unsafe changes go to data/factor_reports/pending_params.json
for human review via `make tune-review`.

Usage:
    python scripts/auto_tune.py
    python scripts/auto_tune.py --dry-run
    python scripts/auto_tune.py --lookback 60   # use 60 days of labeled data
    python scripts/auto_tune.py --skip-label     # skip labeling step
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

_PARAMS_PATH = Path(__file__).resolve().parents[1] / "config" / "engine_params.json"
_REPORT_DIR = Path(__file__).resolve().parents[1] / "data" / "factor_reports"
_PENDING_PATH = _REPORT_DIR / "pending_params.json"
_MAX_CHANGE_PCT = 0.20   # block auto-apply if any param shifts > 20%
_MIN_LIFT_TO_APPLY = 0.02  # require ≥ 2% lift improvement to bother applying

_console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_params() -> dict:
    with open(_PARAMS_PATH) as f:
        return json.load(f)


def _load_latest_report() -> dict | None:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    reports = sorted(_REPORT_DIR.glob("factor_report_*.json"), reverse=True)
    if not reports:
        return None
    with open(reports[0]) as f:
        return json.load(f)


def _safety_check(old: dict, new_params: dict) -> list[str]:
    violations = []
    for k, new_val in new_params.items():
        old_val = old.get(k)
        if old_val is not None and old_val != 0:
            pct = abs(new_val - old_val) / abs(old_val)
            if pct > _MAX_CHANGE_PCT:
                violations.append(f"{k}: {old_val} → {new_val} ({pct:.0%} > {_MAX_CHANGE_PCT:.0%} 上限)")
    return violations


def _apply_params(new_params: dict, old: dict, reason: str, lift: float, dry_run: bool) -> None:
    full = {**old, **new_params, "_comment": f"auto_tune {date.today()}"}
    if dry_run:
        _console.print(f"  [dim][DRY RUN] 套用參數: {new_params}[/dim]")
        return
    with open(_PARAMS_PATH, "w") as f:
        json.dump(full, f, indent=2, ensure_ascii=False)
    _console.print(f"  [green]✅ 已寫入 config/engine_params.json[/green]")

    try:
        from taiwan_stock_agent.infrastructure.db import get_connection, init_pool
        init_pool()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO engine_versions
                       (applied_at, params_before, params_after, reason, lift_estimate)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (datetime.now(), json.dumps(old), json.dumps(full), reason, lift),
                )
            conn.commit()
    except Exception as e:
        logger.warning("engine_versions write failed: %s", e)


def _save_pending(best: dict, violations: list[str], dry_run: bool) -> None:
    payload = {
        "saved_at": str(date.today()),
        "params": best["params"],
        "avg_test_lift": best["avg_test_lift"],
        "violations": violations,
    }
    if dry_run:
        _console.print(f"  [dim][DRY RUN] 儲存 pending: {payload['params']}[/dim]")
        return
    _PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_PENDING_PATH, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    _console.print(f"  [yellow]⚠️  大幅調整 → 存入 {_PENDING_PATH}（make tune-review 審核）[/yellow]")


# ---------------------------------------------------------------------------
# Step 1: Label
# ---------------------------------------------------------------------------

def _run_label(lookback: int, dry_run: bool) -> dict[str, int]:
    from plan_surge_label import label_signals
    _console.print("[bold cyan][Step 1][/bold cyan] 標記 plan 訊號勝負…")
    stats = label_signals(dry_run=dry_run, lookback_days=lookback)
    wins = stats["labeled_win"]
    misses = stats["labeled_miss"]
    total = wins + misses
    rate = wins / total if total else 0.0
    _console.print(
        f"  WIN [green]{wins}[/green] / MISS [red]{misses}[/red] / 合計 {total}  "
        f"命中率 [bold]{rate:.1%}[/bold]"
    )
    return stats


# ---------------------------------------------------------------------------
# Step 2: Grid search via factor_report
# ---------------------------------------------------------------------------

def _run_factor_report() -> bool:
    _console.print("[bold cyan][Step 2][/bold cyan] 執行 factor_report grid search…")
    result = subprocess.run(
        [sys.executable, "scripts/factor_report.py"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        _console.print(f"  [red]factor_report 失敗:[/red]\n{result.stderr[-800:]}")
        return False
    _console.print("  [green]Grid search 完成[/green]")
    return True


# ---------------------------------------------------------------------------
# Step 3: Apply or save pending
# ---------------------------------------------------------------------------

def _run_auto_apply(dry_run: bool) -> None:
    _console.print("[bold cyan][Step 3][/bold cyan] 審核並套用最佳參數…")
    report = _load_latest_report()
    if not report:
        _console.print("  [red]找不到 factor_report，跳過套用[/red]")
        return

    grid_results: list[dict] = report.get("grid_search_top5", [])
    if not grid_results:
        _console.print("  [dim]Grid search 無結果（資料不足），跳過[/dim]")
        return

    best = grid_results[0]
    lift = best.get("avg_test_lift", 0)

    if lift < _MIN_LIFT_TO_APPLY:
        _console.print(
            f"  [dim]最佳 lift={lift:+.2%} < {_MIN_LIFT_TO_APPLY:.0%} 門檻，"
            f"現有參數已是最佳[/dim]"
        )
        return

    old_params = _load_params()
    violations = _safety_check(old_params, best["params"])

    # Print diff table
    t = Table(title="參數變動預覽", show_lines=False)
    t.add_column("參數", style="cyan")
    t.add_column("現在", justify="right")
    t.add_column("建議", justify="right")
    t.add_column("變動", justify="right")
    for k, new_val in best["params"].items():
        old_val = old_params.get(k, "—")
        if old_val != "—":
            chg = (new_val - old_val) / abs(old_val) * 100 if old_val != 0 else float("inf")
            color = "red" if abs(chg) > 20 else ("yellow" if abs(chg) > 10 else "green")
            t.add_row(k, str(old_val), str(new_val), f"[{color}]{chg:+.1f}%[/{color}]")
    _console.print(t)
    _console.print(f"  avg_test_lift: [bold green]{lift:+.2%}[/bold green]")

    if violations:
        _console.print("  [yellow]⚠ 以下參數變動超過上限，存入 pending:[/yellow]")
        for v in violations:
            _console.print(f"    {v}")
        _save_pending(best, violations, dry_run)
    else:
        reason = f"auto_tune {date.today()} lift={lift:+.3f}"
        _apply_params(best["params"], old_params, reason, lift, dry_run)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Auto-tune TCE params from plan→surge labels")
    ap.add_argument("--dry-run", action="store_true", help="不寫入任何檔案")
    ap.add_argument("--lookback", type=int, default=30, help="標記回溯天數")
    ap.add_argument("--skip-label", action="store_true", help="跳過標記步驟（用現有資料）")
    args = ap.parse_args()

    _console.print(Panel(
        f"[bold white]Auto-tune[/bold white]  plan→surge 命中率驅動參數自優化\n"
        f"[bold white]回溯天數[/bold white]  {args.lookback}\n"
        f"[bold white]Dry run [/bold white]  {'是' if args.dry_run else '否'}",
        title="[bold cyan]Auto-Tune Pipeline[/bold cyan]",
        border_style="cyan",
    ))

    if not args.skip_label:
        _run_label(args.lookback, args.dry_run)

    ok = _run_factor_report()
    if not ok:
        sys.exit(1)

    _run_auto_apply(args.dry_run)

    _console.print("\n[bold green]✅ Auto-tune 完成[/bold green]")


if __name__ == "__main__":
    main()
