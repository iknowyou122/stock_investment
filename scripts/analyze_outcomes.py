"""Analyze signal outcomes to validate and calibrate the Triple Confirmation Engine.

Reads signal_outcomes from PostgreSQL, computes win rates by confidence tier,
and suggests threshold adjustments.

Usage:
    python scripts/analyze_outcomes.py
    python scripts/analyze_outcomes.py --days 60
    python scripts/analyze_outcomes.py --min-samples 5
    python scripts/analyze_outcomes.py --scoring-version v2

make analyze
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    from rich.panel import Panel
    _console = Console()
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False
    _console = None  # type: ignore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()


def _print(msg: str) -> None:
    if _HAS_RICH and _console:
        _console.print(msg)
    else:
        print(msg)


def _fetch_outcomes(days: int, scoring_version: str | None) -> list[dict]:
    """Fetch signal_outcomes rows from PostgreSQL."""
    from taiwan_stock_agent.infrastructure.db import init_pool, get_connection
    init_pool()

    query = """
        SELECT
            signal_id,
            ticker,
            signal_date,
            confidence_score,
            action,
            entry_price,
            outcome_1d,
            outcome_3d,
            outcome_5d,
            halt_flag,
            scoring_version
        FROM signal_outcomes
        WHERE signal_date >= CURRENT_DATE - INTERVAL '%s days'
          AND halt_flag = FALSE
          AND outcome_1d IS NOT NULL
    """
    params = [days]

    if scoring_version:
        query += " AND scoring_version = %s"
        params.append(scoring_version)

    query += " ORDER BY signal_date DESC"

    rows = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rows.append(dict(zip(cols, row)))
    return rows


def _compute_tier_stats(rows: list[dict], min_samples: int) -> list[dict]:
    """Compute win rates by confidence tier."""
    tiers = [
        (0, 40, "0-39"),
        (40, 50, "40-49"),
        (50, 60, "50-59"),
        (60, 70, "60-69"),
        (70, 80, "70-79"),
        (80, 101, "80+"),
    ]

    results = []
    for lo, hi, label in tiers:
        bucket = [r for r in rows if lo <= r["confidence_score"] < hi]
        n = len(bucket)
        if n < min_samples:
            results.append({
                "tier": label, "n": n, "skip": True,
                "win_1d": None, "win_3d": None, "win_5d": None,
                "avg_1d": None, "avg_3d": None, "avg_5d": None,
            })
            continue

        def win_rate(field: str) -> float | None:
            vals = [r[field] for r in bucket if r[field] is not None]
            if not vals:
                return None
            return sum(1 for v in vals if v > 0) / len(vals)

        def avg_return(field: str) -> float | None:
            vals = [r[field] for r in bucket if r[field] is not None]
            if not vals:
                return None
            return sum(vals) / len(vals)

        results.append({
            "tier": label,
            "n": n,
            "skip": False,
            "win_1d": win_rate("outcome_1d"),
            "win_3d": win_rate("outcome_3d"),
            "win_5d": win_rate("outcome_5d"),
            "avg_1d": avg_return("outcome_1d"),
            "avg_3d": avg_return("outcome_3d"),
            "avg_5d": avg_return("outcome_5d"),
        })
    return results


def _compute_action_stats(rows: list[dict], min_samples: int) -> list[dict]:
    """Compute win rates by action type."""
    actions = set(r["action"] for r in rows)
    results = []
    for action in sorted(actions):
        bucket = [r for r in rows if r["action"] == action]
        n = len(bucket)
        if n < min_samples:
            continue
        vals_1d = [r["outcome_1d"] for r in bucket if r["outcome_1d"] is not None]
        win_1d = sum(1 for v in vals_1d if v > 0) / len(vals_1d) if vals_1d else None
        avg_1d = sum(vals_1d) / len(vals_1d) if vals_1d else None
        results.append({
            "action": action,
            "n": n,
            "win_1d": win_1d,
            "avg_1d": avg_1d,
        })
    return results


def _suggest_threshold(tier_stats: list[dict]) -> str:
    """Suggest LONG threshold adjustments based on win rates."""
    # Find the lowest tier with win_1d > 55%
    suggestions = []
    baseline = 0.50  # unconditional baseline for Taiwan stocks

    # Check if current LONG threshold area (60-70 tier) meets target
    long_tier = next((t for t in tier_stats if t["tier"] == "60-69"), None)
    if long_tier and not long_tier["skip"] and long_tier["win_1d"] is not None:
        if long_tier["win_1d"] < 0.50:
            suggestions.append(
                f"[red]⚠ 信心 60-69 的 1d 勝率 {long_tier['win_1d']:.1%} < 50% 基準線 "
                "→ 考慮將 LONG 門檻從 68 上調至 73[/red]"
            )
        elif long_tier["win_1d"] > 0.60:
            suggestions.append(
                f"[green]✓ 信心 60-69 的 1d 勝率 {long_tier['win_1d']:.1%} > 60% "
                "→ 門檻可考慮下調至 63（捕獲更多訊號）[/green]"
            )

    tier_70 = next((t for t in tier_stats if t["tier"] == "70-79"), None)
    if tier_70 and not tier_70["skip"] and tier_70["win_1d"] is not None:
        if tier_70["win_1d"] < 0.55:
            suggestions.append(
                f"[red]⚠ 信心 70-79 的 1d 勝率 {tier_70['win_1d']:.1%} < 55% 設計目標 "
                "→ 信號引擎需重新校準[/red]"
            )
        else:
            suggestions.append(
                f"[green]✓ 信心 70-79 的 1d 勝率 {tier_70['win_1d']:.1%} ≥ 55% 設計目標 "
                "→ 引擎在此區間正常運作[/green]"
            )

    if not suggestions:
        suggestions.append("[dim]樣本數不足，無法提供建議（需要更多歷史訊號累積）[/dim]")

    return "\n".join(suggestions)


def analyze(days: int, min_samples: int, scoring_version: str | None) -> None:
    _print(Panel(
        f"[bold white]回溯天數[/bold white]  {days} 天\n"
        f"[bold white]最低樣本[/bold white]  {min_samples} 筆\n"
        f"[bold white]評分版本[/bold white]  {scoring_version or '全部'}",
        title="[bold cyan]Signal Outcome Analyzer[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ) if _HAS_RICH else f"[Analyzer] days={days}, min_samples={min_samples}")

    try:
        rows = _fetch_outcomes(days, scoring_version)
    except Exception as e:
        _print(f"\n[red]❌ 無法連接資料庫: {e}[/red]")
        _print("[dim]請設定 DATABASE_URL 環境變數[/dim]")
        sys.exit(1)

    if not rows:
        _print("\n[yellow]⚠ signal_outcomes 表中沒有已結算的訊號（price_1d 為 NULL）。[/yellow]")
        _print("[dim]請等待 T+1 收盤後由 cron job 填入結算價格，或手動呼叫 POST /v1/signals/{id}/outcome。[/dim]")
        return

    _print(f"\n[bold]找到 {len(rows)} 筆已結算訊號[/bold]")

    # --- Confidence tier win rate table ---
    tier_stats = _compute_tier_stats(rows, min_samples)

    if _HAS_RICH and _console:
        table = Table(
            title=f"勝率分析 — 按信心分數區間 (最近 {days} 天)",
            box=box.ROUNDED,
            header_style="bold white on dark_blue",
            border_style="blue",
            show_lines=True,
        )
        table.add_column("信心分數", justify="center", style="bold", width=10)
        table.add_column("樣本數", justify="right", style="dim", width=8)
        table.add_column("1d 勝率", justify="right", width=10)
        table.add_column("3d 勝率", justify="right", width=10)
        table.add_column("5d 勝率", justify="right", width=10)
        table.add_column("1d 平均報酬", justify="right", width=12)
        table.add_column("3d 平均報酬", justify="right", width=12)

        def _fmt_rate(v: float | None) -> str:
            if v is None:
                return "[dim]—[/dim]"
            color = "green" if v > 0.55 else ("yellow" if v > 0.45 else "red")
            return f"[{color}]{v:.1%}[/{color}]"

        def _fmt_ret(v: float | None) -> str:
            if v is None:
                return "[dim]—[/dim]"
            color = "green" if v > 0 else "red"
            return f"[{color}]{v:+.2%}[/{color}]"

        for t in tier_stats:
            if t["skip"]:
                table.add_row(
                    t["tier"], f"[dim]{t['n']}[/dim]",
                    "[dim]樣本不足[/dim]", "[dim]—[/dim]", "[dim]—[/dim]",
                    "[dim]—[/dim]", "[dim]—[/dim]",
                )
            else:
                table.add_row(
                    t["tier"], str(t["n"]),
                    _fmt_rate(t["win_1d"]),
                    _fmt_rate(t["win_3d"]),
                    _fmt_rate(t["win_5d"]),
                    _fmt_ret(t["avg_1d"]),
                    _fmt_ret(t["avg_3d"]),
                )
        _console.print()
        _console.print(table)
    else:
        print(f"\nWin rates by confidence tier (last {days} days):")
        print(f"{'Tier':>10} {'N':>6} {'1d Win%':>10} {'3d Win%':>10} {'5d Win%':>10}")
        for t in tier_stats:
            if t["skip"]:
                print(f"{t['tier']:>10} {t['n']:>6}  {'(insuff)':>10}")
            else:
                w1 = f"{t['win_1d']:.1%}" if t["win_1d"] is not None else "—"
                w3 = f"{t['win_3d']:.1%}" if t["win_3d"] is not None else "—"
                w5 = f"{t['win_5d']:.1%}" if t["win_5d"] is not None else "—"
                print(f"{t['tier']:>10} {t['n']:>6}  {w1:>10} {w3:>10} {w5:>10}")

    # --- Action type stats ---
    action_stats = _compute_action_stats(rows, min_samples)
    if action_stats and _HAS_RICH and _console:
        tbl2 = Table(
            title="勝率分析 — 按行動類型",
            box=box.SIMPLE,
            header_style="bold cyan",
            border_style="bright_black",
        )
        tbl2.add_column("Action", style="bold", width=12)
        tbl2.add_column("樣本數", justify="right", width=8)
        tbl2.add_column("1d 勝率", justify="right", width=10)
        tbl2.add_column("1d 平均報酬", justify="right", width=12)
        for s in action_stats:
            w = f"{s['win_1d']:.1%}" if s["win_1d"] is not None else "—"
            r = f"{s['avg_1d']:+.2%}" if s["avg_1d"] is not None else "—"
            tbl2.add_row(s["action"], str(s["n"]), w, r)
        _console.print()
        _console.print(tbl2)

    # --- Threshold suggestions ---
    suggestions = _suggest_threshold(tier_stats)
    _print("\n[bold white]閾值校準建議:[/bold white]")
    _print(suggestions)

    # --- Overall stats ---
    all_1d = [r["outcome_1d"] for r in rows if r["outcome_1d"] is not None]
    if all_1d:
        overall_win = sum(1 for v in all_1d if v > 0) / len(all_1d)
        overall_avg = sum(all_1d) / len(all_1d)
        _print(
            f"\n  整體 1d 勝率: [bold]{overall_win:.1%}[/bold]  "
            f"平均報酬: [bold]{overall_avg:+.2%}[/bold]  "
            f"（基準: ~45%）"
        )

    _print(Panel(
        f"[bold green]分析完成[/bold green]  {len(rows)} 筆訊號  •  "
        f"期間 {days} 天  •  版本 {scoring_version or '全部'}",
        border_style="green",
        padding=(0, 2),
    ) if _HAS_RICH else "Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="分析 signal_outcomes 勝率")
    parser.add_argument("--days", type=int, default=90, help="回溯天數（預設: 90）")
    parser.add_argument(
        "--min-samples", type=int, default=5,
        help="每個區間最低樣本數（預設: 5）",
    )
    parser.add_argument(
        "--scoring-version", default=None,
        help="只分析特定評分版本（v2）；預設: 全部",
    )
    args = parser.parse_args()

    analyze(
        days=args.days,
        min_samples=args.min_samples,
        scoring_version=args.scoring_version,
    )


if __name__ == "__main__":
    main()
