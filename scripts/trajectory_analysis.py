"""Phase B: 軌跡驗證 — RISING vs STABLE vs FIRST 訊號勝率比較。

從 DB 讀取 backtest 資料，模擬每日 persistence 軌跡分類，
比較不同軌跡類型的勝率差異，驗證 RISING +7 / STABLE +5 / DECLINING +0 的合理性。

同時分析 WATCH→LONG 晉升率，驗證 EMERGING_SETUP 概念。

Usage:
    python scripts/trajectory_analysis.py
    python scripts/trajectory_analysis.py --days 180
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel

_console = Console()


def _fetch_all_signals(days: int) -> list[dict]:
    """Fetch all non-halt signals with outcomes from DB."""
    from taiwan_stock_agent.infrastructure.db import init_pool, get_connection
    init_pool()

    query = """
        SELECT ticker, signal_date, confidence_score, action,
               outcome_1d, outcome_3d, outcome_5d, score_breakdown
        FROM signal_outcomes
        WHERE signal_date >= CURRENT_DATE - (%s * INTERVAL '1 day')
          AND halt_flag = FALSE
          AND source = 'backtest'
        ORDER BY ticker, signal_date
    """
    rows = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, [days])
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                rows.append(dict(zip(cols, row)))
    return rows


def _classify_trajectories(rows: list[dict]) -> list[dict]:
    """For each signal, classify its trajectory based on previous days' scores.

    Simulates what _apply_persistence_bonus would do:
    - RISING: 3+ consecutive days with strictly increasing score
    - STABLE: appeared previous day with score >= 50
    - DECLINING: appeared previous day but score dropped > 5
    - FIRST: first appearance (no previous day data)
    """
    # Group by ticker → sorted by date
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(r)

    classified = []
    for ticker, signals in by_ticker.items():
        signals.sort(key=lambda x: x["signal_date"])

        # Build date→score lookup
        date_score: dict[date, int] = {}
        for s in signals:
            date_score[s["signal_date"]] = s["confidence_score"]

        for i, sig in enumerate(signals):
            if sig["outcome_1d"] is None:
                continue  # not settled

            sig_date = sig["signal_date"]
            score = sig["confidence_score"]

            # Look back 3 trading days (skip weekends)
            prev_scores: list[int | None] = []
            candidate = sig_date - timedelta(days=1)
            days_checked = 0
            while len(prev_scores) < 3 and days_checked < 7:
                if candidate.weekday() < 5:
                    prev_scores.append(date_score.get(candidate))
                candidate -= timedelta(days=1)
                days_checked += 1
            prev_scores.reverse()  # old → new

            # Yesterday's score
            yesterday = prev_scores[-1] if prev_scores else None

            if yesterday is None or yesterday < 40:
                trajectory = "FIRST"
            else:
                # Check RISING: 3 consecutive non-None, strictly increasing, ending with yesterday
                non_none = [s for s in prev_scores if s is not None]
                is_rising = (
                    len(non_none) >= 3
                    and all(non_none[j + 1] > non_none[j] for j in range(len(non_none) - 1))
                )

                if is_rising:
                    trajectory = "RISING"
                elif yesterday < (prev_scores[-2] if len(prev_scores) >= 2 and prev_scores[-2] is not None else yesterday) - 5:
                    trajectory = "DECLINING"
                else:
                    trajectory = "STABLE"

            classified.append({
                **sig,
                "trajectory": trajectory,
                "prev_scores": [s for s in prev_scores if s is not None],
            })

    return classified


def _analyze_watch_to_long(rows: list[dict]) -> dict:
    """Analyze how often WATCH stocks become LONG within 1-3 days."""
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(r)

    total_watch = 0
    promoted_1d = 0
    promoted_2d = 0
    promoted_3d = 0

    for ticker, signals in by_ticker.items():
        signals.sort(key=lambda x: x["signal_date"])
        date_action: dict[date, str] = {s["signal_date"]: s["action"] for s in signals}
        dates = sorted(date_action.keys())

        for i, d in enumerate(dates):
            if date_action[d] != "WATCH":
                continue
            total_watch += 1

            # Check next 1-3 trading days
            for offset in range(1, 4):
                future_idx = i + offset
                if future_idx < len(dates):
                    future_date = dates[future_idx]
                    # Only count if it's within ~5 calendar days (skip gaps)
                    if (future_date - d).days <= 7 and date_action.get(future_date) == "LONG":
                        if offset == 1:
                            promoted_1d += 1
                        elif offset == 2:
                            promoted_2d += 1
                        else:
                            promoted_3d += 1
                        break  # count first promotion only

    return {
        "total_watch": total_watch,
        "promoted_1d": promoted_1d,
        "promoted_2d": promoted_2d,
        "promoted_3d": promoted_3d,
        "promoted_any": promoted_1d + promoted_2d + promoted_3d,
    }


def _print_trajectory_analysis(classified: list[dict]) -> None:
    """Print trajectory win rate comparison."""
    # Group by action + trajectory
    groups: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in classified:
        groups[r["action"]][r["trajectory"]].append(r)

    for action in ("LONG", "WATCH"):
        if action not in groups:
            continue

        tbl = Table(
            title=f"{action} 訊號：軌跡分類勝率比較",
            box=box.ROUNDED,
            header_style="bold white on dark_blue",
            show_lines=True,
        )
        tbl.add_column("軌跡", style="bold white", width=12)
        tbl.add_column("樣本數", justify="right", width=8)
        tbl.add_column("平均信心", justify="right", width=8)
        tbl.add_column("T+1 勝率", justify="right", width=10)
        tbl.add_column("T+3 勝率", justify="right", width=10)
        tbl.add_column("T+5 勝率", justify="right", width=10)
        tbl.add_column("T+1 平均報酬", justify="right", width=12)
        tbl.add_column("T+5 平均報酬", justify="right", width=12)

        traj_order = ["RISING", "STABLE", "FIRST", "DECLINING"]
        traj_labels = {
            "RISING": "📈 RISING",
            "STABLE": "📊 STABLE",
            "FIRST": "🆕 FIRST",
            "DECLINING": "📉 DECLINING",
        }

        for traj in traj_order:
            sigs = groups[action].get(traj, [])
            if not sigs:
                continue

            n = len(sigs)
            avg_conf = sum(s["confidence_score"] for s in sigs) / n

            r1 = [s for s in sigs if s["outcome_1d"] is not None]
            r3 = [s for s in sigs if s["outcome_3d"] is not None]
            r5 = [s for s in sigs if s["outcome_5d"] is not None]

            win_1d = sum(1 for s in r1 if s["outcome_1d"] > 0) / len(r1) if r1 else 0
            win_3d = sum(1 for s in r3 if s["outcome_3d"] > 0) / len(r3) if r3 else 0
            win_5d = sum(1 for s in r5 if s["outcome_5d"] > 0) / len(r5) if r5 else 0
            avg_ret_1d = sum(s["outcome_1d"] for s in r1) / len(r1) if r1 else 0
            avg_ret_5d = sum(s["outcome_5d"] for s in r5) / len(r5) if r5 else 0

            tbl.add_row(
                traj_labels.get(traj, traj),
                str(n),
                f"{avg_conf:.0f}",
                f"{win_1d:.1%}",
                f"{win_3d:.1%}",
                f"{win_5d:.1%}",
                f"{avg_ret_1d:+.2%}",
                f"{avg_ret_5d:+.2%}",
            )

        _console.print()
        _console.print(tbl)


def _print_promotion_analysis(promo: dict) -> None:
    total = promo["total_watch"]
    if total == 0:
        _console.print("\n[dim]WATCH 資料不足，無法分析晉升率[/dim]")
        return

    _console.print()
    tbl = Table(
        title="WATCH → LONG 晉升分析",
        box=box.ROUNDED,
        header_style="bold white on dark_green",
    )
    tbl.add_column("指標", style="white", width=25)
    tbl.add_column("數值", justify="right", width=10)
    tbl.add_column("比率", justify="right", width=10)

    tbl.add_row("WATCH 總筆數", str(total), "")
    tbl.add_row("1 天內晉升 LONG", str(promo["promoted_1d"]), f"{promo['promoted_1d']/total:.1%}")
    tbl.add_row("2 天內晉升 LONG", str(promo["promoted_2d"]), f"{promo['promoted_2d']/total:.1%}")
    tbl.add_row("3 天內晉升 LONG", str(promo["promoted_3d"]), f"{promo['promoted_3d']/total:.1%}")
    tbl.add_row(
        "[bold]合計晉升[/bold]",
        f"[bold]{promo['promoted_any']}[/bold]",
        f"[bold]{promo['promoted_any']/total:.1%}[/bold]",
    )
    _console.print(tbl)


def _print_watch_trajectory_outcomes(classified: list[dict]) -> None:
    """For WATCH stocks that later became LONG, compare trajectories."""
    watch_sigs = [r for r in classified if r["action"] == "WATCH" and r["outcome_5d"] is not None]
    if not watch_sigs:
        return

    _console.print()
    tbl = Table(
        title="WATCH 訊號 T+5 報酬（驗證 EMERGING_SETUP 提前佈局的收益）",
        box=box.ROUNDED,
        header_style="bold white on dark_magenta",
    )
    tbl.add_column("信心區間", style="white", width=12)
    tbl.add_column("樣本數", justify="right", width=8)
    tbl.add_column("T+5 勝率", justify="right", width=10)
    tbl.add_column("T+5 平均報酬", justify="right", width=12)

    # Split by confidence tiers
    tiers = [
        ("45-49", 45, 49),
        ("50-54", 50, 54),
        ("55-59", 55, 59),
        ("60-64", 60, 64),
    ]
    for label, lo, hi in tiers:
        tier_sigs = [s for s in watch_sigs if lo <= s["confidence_score"] <= hi]
        if not tier_sigs:
            continue
        n = len(tier_sigs)
        win = sum(1 for s in tier_sigs if s["outcome_5d"] > 0) / n
        avg = sum(s["outcome_5d"] for s in tier_sigs) / n
        color = "green" if avg > 0 else "red"
        tbl.add_row(label, str(n), f"{win:.1%}", f"[{color}]{avg:+.2%}[/{color}]")

    _console.print(tbl)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase B: 軌跡驗證分析")
    parser.add_argument("--days", type=int, default=180)
    args = parser.parse_args()

    _console.print(Panel(
        "[bold cyan]Phase B: 軌跡驗證分析[/bold cyan]\n"
        "[dim]比較 RISING / STABLE / FIRST / DECLINING 訊號勝率[/dim]",
        border_style="cyan",
        padding=(0, 2),
    ))

    _console.print(f"[dim]從 DB 載入 backtest 訊號（{args.days} 天內）...[/dim]")
    rows = _fetch_all_signals(args.days)
    _console.print(f"[dim]載入 {len(rows)} 筆訊號[/dim]")

    if not rows:
        _console.print("[red]DB 中無 backtest 資料。請先執行 make backtest[/red]")
        return

    # 1. Trajectory classification + win rate comparison
    _console.print(f"[dim]分類軌跡中...[/dim]")
    classified = _classify_trajectories(rows)
    _console.print(f"[dim]已分類 {len(classified)} 筆已結算訊號[/dim]")

    _print_trajectory_analysis(classified)

    # 2. WATCH → LONG promotion analysis
    _console.print(f"\n[dim]分析 WATCH → LONG 晉升率...[/dim]")
    promo = _analyze_watch_to_long(rows)
    _print_promotion_analysis(promo)

    # 3. WATCH tier outcomes by confidence
    _print_watch_trajectory_outcomes(classified)

    # 4. Summary recommendations
    _console.print()
    _console.print("[bold]解讀指南：[/bold]")
    _console.print("  RISING 勝率 > STABLE > FIRST  → 軌跡加分邏輯正確")
    _console.print("  DECLINING 勝率最低            → 不加分的決定正確")
    _console.print("  WATCH 晉升率 > 5%             → EMERGING_SETUP 監控有意義")
    _console.print("  WATCH 55-64 的 T+5 報酬 > 0   → T-2 提前佈局有利可圖")


if __name__ == "__main__":
    main()
