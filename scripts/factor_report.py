"""Factor effectiveness analysis + grid search + walk-forward + residual analysis.

Reads signal_outcomes from DB (requires score_breakdown JSONB to be populated).
Outputs JSON recommendation file + rich terminal report.

Usage:
    python scripts/factor_report.py
    python scripts/factor_report.py --days 180
    python scripts/factor_report.py --min-samples 10
    make factor-report
    make factor-report FORCE=1
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

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

from taiwan_stock_agent.domain.scoring_replay import recompute_score, load_params, DEFAULT_PARAMS

from taiwan_stock_agent.infrastructure.db import init_pool, get_connection

_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "factor_reports"


def _fetch_rows(days: int, scoring_version: str | None) -> list[dict]:
    """Load all settled signals with score_breakdown from DB."""
    query = """
        SELECT signal_id, ticker, signal_date, confidence_score, action,
               outcome_1d, outcome_3d, outcome_5d, score_breakdown, source
        FROM signal_outcomes
        WHERE signal_date >= CURRENT_DATE - (%s * INTERVAL '1 day')
          AND halt_flag = FALSE
          AND outcome_1d IS NOT NULL
          AND score_breakdown IS NOT NULL
    """
    params: list[Any] = [days]
    if scoring_version:
        query += " AND scoring_version = %s"
        params.append(scoring_version)
    query += " ORDER BY signal_date"

    rows = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                if d["score_breakdown"] and isinstance(d["score_breakdown"], str):
                    d["score_breakdown"] = json.loads(d["score_breakdown"])
                rows.append(d)
    return rows


# ---------------------------------------------------------------------------
# Analysis 1: Flag lift
# ---------------------------------------------------------------------------

def _compute_lift(rows: list[dict], min_samples: int) -> list[dict]:
    """Compute lift per flag: win_rate_with_flag - win_rate_without_flag."""
    all_flags: set[str] = set()
    for r in rows:
        bd = r.get("score_breakdown") or {}
        all_flags.update(bd.get("flags", []))

    overall_win = sum(1 for r in rows if r["outcome_1d"] > 0) / len(rows) if rows else 0

    results = []
    for flag in sorted(all_flags):
        with_flag = [r for r in rows if flag in (r.get("score_breakdown") or {}).get("flags", [])]
        without_flag = [r for r in rows if flag not in (r.get("score_breakdown") or {}).get("flags", [])]

        if len(with_flag) < min_samples:
            continue

        win_with = sum(1 for r in with_flag if r["outcome_1d"] > 0) / len(with_flag)
        win_without = (
            sum(1 for r in without_flag if r["outcome_1d"] > 0) / len(without_flag)
            if without_flag else overall_win
        )

        results.append({
            "flag": flag,
            "n_with": len(with_flag),
            "n_without": len(without_flag),
            "win_with": win_with,
            "win_without": win_without,
            "lift": win_with - win_without,
        })

    return sorted(results, key=lambda x: x["lift"], reverse=True)


# ---------------------------------------------------------------------------
# Analysis 2: Grid search + walk-forward
# ---------------------------------------------------------------------------

_PARAM_GRID: dict[str, list] = {
    "rsi_momentum_lo": [48, 50, 52, 55, 58],
    "rsi_momentum_hi": [67, 70, 72, 75],
    "breakout_vol_ratio": [1.2, 1.3, 1.4, 1.5, 1.7, 2.0],
    "long_threshold_neutral": [63, 65, 68, 70, 72],
}


def _win_rate_at_threshold(rows: list[dict], params: dict) -> float:
    """Win rate among signals that would be LONG under these params."""
    if not rows:
        return 0.0
    longs = [
        r for r in rows
        if r.get("score_breakdown") and
        recompute_score(r["score_breakdown"], params)[1] == "LONG"
    ]
    if len(longs) < 5:
        return 0.0
    return sum(1 for r in longs if r["outcome_1d"] > 0) / len(longs)


def _walk_forward_windows(rows: list[dict], train_months: int = 6, test_months: int = 1) -> list[tuple[list, list]]:
    """Return [(train_rows, test_rows), ...] sliding windows."""
    if not rows:
        return []

    dates = sorted(set(r["signal_date"] for r in rows))
    if not dates:
        return []

    first = dates[0]
    last = dates[-1]
    windows = []
    window_start = first

    while True:
        train_end = window_start + timedelta(days=30 * train_months)
        test_end = train_end + timedelta(days=30 * test_months)
        if test_end > last:
            break

        train = [r for r in rows if window_start <= r["signal_date"] < train_end]
        test = [r for r in rows if train_end <= r["signal_date"] < test_end]

        if len(train) >= 20 and len(test) >= 5:
            windows.append((train, test))

        window_start += timedelta(days=30)

    return windows


def _grid_search(rows: list[dict], n_random: int = 500) -> list[dict]:
    """Random search over _PARAM_GRID. Returns top 5 candidates validated on walk-forward."""
    windows = _walk_forward_windows(rows)
    if len(windows) < 2:
        return []

    base_params = load_params()
    all_keys = list(_PARAM_GRID.keys())

    # Generate candidate param sets
    candidates = []
    for _ in range(n_random):
        cand = dict(base_params)
        for k in all_keys:
            cand[k] = random.choice(_PARAM_GRID[k])
        candidates.append(cand)

    # Evaluate each candidate on all walk-forward windows
    results = []
    for params in candidates:
        test_lifts = []

        for train, test in windows:
            base_test_win = _win_rate_at_threshold(test, base_params)
            cand_test_win = _win_rate_at_threshold(test, params)
            test_lifts.append(cand_test_win - base_test_win)

        # Only include if ALL test windows show non-negative lift
        if all(l >= 0 for l in test_lifts) and test_lifts:
            avg_test_lift = sum(test_lifts) / len(test_lifts)
            results.append({
                "params": {k: params[k] for k in all_keys},
                "avg_test_lift": avg_test_lift,
                "n_windows": len(windows),
            })

    results.sort(key=lambda x: x["avg_test_lift"], reverse=True)
    return results[:5]


# ---------------------------------------------------------------------------
# Analysis 3: Residual analysis
# ---------------------------------------------------------------------------

def _residual_analysis(rows: list[dict]) -> list[str]:
    """Find patterns in false positives and false negatives."""
    fp = [r for r in rows if r["confidence_score"] >= 65 and r["outcome_1d"] < 0]
    fn = [r for r in rows if r["confidence_score"] < 50 and r["outcome_1d"] > 0.03]

    suggestions = []

    def avg_raw(group: list[dict], key: str) -> float | None:
        vals = [
            r["score_breakdown"]["raw"].get(key)
            for r in group
            if r.get("score_breakdown") and r["score_breakdown"].get("raw")
        ]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    for key in ("rsi_14", "volume_vs_20ma", "ma20_slope_pct"):
        fp_avg = avg_raw(fp, key)
        fn_avg = avg_raw(fn, key)
        if fp_avg is not None and fn_avg is not None and len(fp) >= 5 and len(fn) >= 5:
            diff = abs(fn_avg - fp_avg)
            if diff > 0.1 * abs(fp_avg + 1e-9):
                direction = "higher" if fn_avg > fp_avg else "lower"
                suggestions.append(
                    f"{key}: FN avg={fn_avg:.2f} ({direction} than FP avg={fp_avg:.2f}) "
                    f"— 考慮調整 {key} 閾值 (FP={len(fp)}, FN={len(fn)})"
                )

    if not suggestions:
        suggestions.append("樣本量不足以識別殘差模式 (需要 FP≥5, FN≥5)")

    return suggestions


# ---------------------------------------------------------------------------
# Report output
# ---------------------------------------------------------------------------

def _save_recommendations(lift_results: list[dict], grid_results: list[dict], residual: list[str], report_date: date) -> Path:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _OUTPUT_DIR / f"factor_report_{report_date}.json"
    payload = {
        "report_date": str(report_date),
        "lift_analysis": lift_results,
        "grid_search_top5": grid_results,
        "residual_suggestions": residual,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return out_path


_FLAG_ZH: dict[str, str] = {
    "GATE_PASS:VOL": "✅ 量能充足（今量 > 1.2倍均量）",
    "GATE_FAIL:VOL": "❌ 量能不足",
    "GATE_PASS:VWAP": "✅ 收盤站上 5日均價",
    "GATE_FAIL:VWAP": "❌ 收盤跌破 5日均價",
    "GATE_PASS:HIGH20": "✅ 突破 20 日新高",
    "GATE_FAIL:HIGH20": "❌ 未突破 20 日新高",
    "GATE_PASS:RS": "✅ 強於大盤",
    "GATE_FAIL:RS": "❌ 弱於大盤",
    "GATE_MET:0": "門檻通過 0/4（全沒過）",
    "GATE_MET:1": "門檻通過 1/4",
    "GATE_MET:2": "門檻通過 2/4",
    "GATE_MET:3": "門檻通過 3/4",
    "GATE_MET:4": "門檻通過 4/4（全過）",
    "GATE_AVAILABLE:4": "4 項門檻資料完整",
    "GATE_SKIP:RS": "⏭ 相對強弱無資料（跳過）",
    "NO_SETUP": "不符合進場條件（分數太低）",
    "NO_CHIP_DATA": "無籌碼資料（TWSE 無回傳）",
    "BREAKOUT_WITH_VOL": "突破新高 + 量能確認",
    "LONG_UPPER_SHADOW": "長上影線（盤中賣壓重）",
    "DOJI_OR_HALT": "十字線或漲跌停鎖死",
    "OHLCV_PROXY:yfinance": "OHLCV 由 yfinance 提供",
}


def _translate_flag(flag: str) -> str:
    if flag in _FLAG_ZH:
        return _FLAG_ZH[flag]
    # GATE_SKIP:X pattern
    if flag.startswith("GATE_SKIP:"):
        gate = flag.split(":")[1]
        return f"⏭ {gate} 門檻無資料（跳過）"
    return flag


def _print_report(lift_results: list[dict], grid_results: list[dict], residual: list[str], n_rows: int) -> None:
    if _HAS_RICH and _console:
        _console.print(Panel(f"[bold cyan]因子分析報告[/bold cyan]  {n_rows} 筆已結算訊號", border_style="cyan"))

        tbl = Table(
            title="因子有效性分析（Lift = 有此特徵的勝率 - 無此特徵的勝率）",
            box=box.ROUNDED,
            header_style="bold white on dark_blue",
        )
        tbl.add_column("特徵", width=35)
        tbl.add_column("出現次數", justify="right", width=8)
        tbl.add_column("有此特徵勝率", justify="right", width=12)
        tbl.add_column("無此特徵勝率", justify="right", width=12)
        tbl.add_column("影響", justify="right", width=10)
        for r in lift_results:
            color = "green" if r["lift"] > 0.05 else ("yellow" if r["lift"] > -0.03 else "red")
            label = _translate_flag(r["flag"])
            tbl.add_row(
                label, str(r["n_with"]),
                f"{r['win_with']:.1%}", f"{r['win_without']:.1%}",
                f"[{color}]{r['lift']:+.1%}[/{color}]",
            )
        _console.print(tbl)

        _console.print()
        _console.print("[bold]怎麼看這張表：[/bold]")
        _console.print("  [green]影響 > +5%[/green]  → 好訊號，出現這個特徵的股票更容易漲")
        _console.print("  [red]影響 < -3%[/red]  → 壞訊號，出現這個特徵的股票更容易跌")
        _console.print("  [yellow]中間地帶[/yellow]    → 對漲跌沒有明顯影響")

        if grid_results:
            _console.print("\n[bold]參數優化建議（歷史驗證通過）:[/bold]")
            for i, g in enumerate(grid_results, 1):
                _console.print(f"  {i}. 預期提升 +{g['avg_test_lift']:.1%}  參數={g['params']}")
        else:
            _console.print("\n[dim]參數優化：樣本不足，無法產生建議[/dim]")

        _console.print("\n[bold]誤判分析：[/bold]")
        for s in residual:
            _console.print(f"  • {s}")
    else:
        print(f"\n=== Factor Report ({n_rows} signals) ===")
        for r in lift_results:
            print(f"  {_translate_flag(r['flag'])}: 影響={r['lift']:+.1%} (n={r['n_with']})")


def run_report(days: int, min_samples: int, scoring_version: str | None) -> Path | None:
    init_pool()

    with _console.status("[cyan]從 DB 載入訊號資料...[/cyan]"):
        try:
            rows = _fetch_rows(days, scoring_version)
        except Exception as e:
            print(f"DB error: {e}\n請設定 DATABASE_URL")
            return None

    if len(rows) < 20:
        print(f"⚠ 只有 {len(rows)} 筆資料（需要 ≥20）。請先執行 make backtest 建立基礎資料。")
        return None

    _console.print(f"[dim]載入 {len(rows)} 筆已結算訊號（{days} 天內）[/dim]")

    with _console.status("[cyan]計算因子 Lift 分析...[/cyan]"):
        lift_results = _compute_lift(rows, min_samples)
    _console.print(f"[dim]Lift 分析完成：{len(lift_results)} 個因子[/dim]")

    with _console.status("[cyan]Grid Search + Walk-forward 驗證（500 候選 × 多個時間窗口）...[/cyan]"):
        grid_results = _grid_search(rows)
    _console.print(f"[dim]Grid Search 完成：{len(grid_results)} 個候選參數通過驗證[/dim]")

    with _console.status("[cyan]殘差分析（FP / FN 模式識別）...[/cyan]"):
        residual = _residual_analysis(rows)

    _print_report(lift_results, grid_results, residual, len(rows))

    out_path = _save_recommendations(lift_results, grid_results, residual, date.today())
    print(f"\n報告已儲存至 {out_path}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--min-samples", type=int, default=10)
    parser.add_argument("--scoring-version", default=None)
    args = parser.parse_args()
    run_report(args.days, args.min_samples, args.scoring_version)


if __name__ == "__main__":
    main()
