"""Surge factor lift analysis and LLM-assisted weight optimization."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich.console import Console
from rich.table import Table
from rich import box

_console = Console()
_PARAMS_PATH = Path(__file__).resolve().parents[1] / "config" / "surge_params.json"

_FACTOR_THRESHOLDS: dict[str, callable] = {
    "vol_ratio_3x":       lambda s: (s.get("vol_ratio") or 0) >= 3.0,
    "vol_ratio_2x":       lambda s: (s.get("vol_ratio") or 0) >= 2.0,
    "gap_3pct":           lambda s: (s.get("gap_pct") or 0) >= 3.0,
    "gap_1pct":           lambda s: (s.get("gap_pct") or 0) >= 1.0,
    "rsi_healthy":        lambda s: 55.0 <= (s.get("rsi") or 0) <= 72.0,
    "inst_buy_1d":        lambda s: (s.get("inst_consec_days") or 0) >= 1,
    "inst_buy_2d":        lambda s: (s.get("inst_consec_days") or 0) >= 2,
    "ind_top_20pct":      lambda s: (s.get("industry_rank_pct") or 0) >= 80.0,
    "ind_top_40pct":      lambda s: (s.get("industry_rank_pct") or 0) >= 60.0,
    "close_strong":       lambda s: (s.get("close_strength") or 0) >= 0.8,
    "score_65plus":       lambda s: (s.get("score") or 0) >= 65,
    "score_55plus":       lambda s: (s.get("score") or 0) >= 55,
    "pocket_pivot":       lambda s: _has_breakdown_flag(s, "pocket_pivot"),
    "breakaway_gap_full": lambda s: _has_breakdown_flag(s, "breakaway_gap_full"),
    "relative_strength":  lambda s: _has_breakdown_flag(s, "relative_strength"),
}


def _has_breakdown_flag(signal: dict, key: str) -> bool:
    try:
        bd = signal.get("score_breakdown")
        if isinstance(bd, str):
            bd = json.loads(bd)
        return bool((bd or {}).get(key, 0))
    except Exception:
        return False


def compute_lift(signals: list[dict]) -> dict[str, dict]:
    """Compute T+1 win-rate lift for each factor threshold."""
    result = {}
    settled = [s for s in signals if s.get("t1_return_pct") is not None]
    if not settled:
        return result

    for name, fn in _FACTOR_THRESHOLDS.items():
        present = [s for s in settled if fn(s)]
        absent  = [s for s in settled if not fn(s)]

        def _wr(lst):
            if not lst:
                return 0.0
            return sum(1 for s in lst if (s.get("t1_return_pct") or 0) > 0) / len(lst)

        def _avg(lst):
            if not lst:
                return 0.0
            return sum(s.get("t1_return_pct") or 0 for s in lst) / len(lst)

        result[name] = {
            "present_wr":      round(_wr(present), 4),
            "absent_wr":       round(_wr(absent), 4),
            "lift_pp":         round((_wr(present) - _wr(absent)) * 100, 1),
            "n_present":       len(present),
            "n_absent":        len(absent),
            "avg_ret_present": round(_avg(present), 2),
            "avg_ret_absent":  round(_avg(absent), 2),
        }
    return result


def build_grade_summary(signals: list[dict]) -> dict[str, dict]:
    """Win rate and avg return per grade for settled T+1 signals."""
    settled = [s for s in signals if s.get("t1_return_pct") is not None]
    grades = ["SURGE_ALPHA", "SURGE_BETA", "SURGE_GAMMA"]
    result = {}
    for g in grades:
        group = [s for s in settled if s.get("grade") == g]
        if not group:
            continue
        wr = sum(1 for s in group if (s.get("t1_return_pct") or 0) > 0) / len(group)
        avg_ret = sum(s.get("t1_return_pct") or 0 for s in group) / len(group)
        result[g] = {"n": len(group), "t1_wr": round(wr, 4), "t1_avg_ret": round(avg_ret, 2)}
    return result


def print_lift_table(lift: dict[str, dict]) -> None:
    tbl = Table(title="因子 Lift 分析 (T+1)", box=box.ROUNDED, show_lines=True)
    tbl.add_column("因子", style="cyan")
    tbl.add_column("有 N", justify="right")
    tbl.add_column("有 勝率", justify="right")
    tbl.add_column("無 N", justify="right")
    tbl.add_column("無 勝率", justify="right")
    tbl.add_column("Lift(pp)", justify="right")
    tbl.add_column("有 均報酬%", justify="right")
    for name, v in sorted(lift.items(), key=lambda x: -x[1]["lift_pp"]):
        lift_pp = v["lift_pp"]
        style = "green" if lift_pp >= 10 else ("yellow" if lift_pp >= 5 else "")
        tbl.add_row(
            name,
            str(v["n_present"]),
            f"{v['present_wr']*100:.1f}%",
            str(v["n_absent"]),
            f"{v['absent_wr']*100:.1f}%",
            f"[{style}]{lift_pp:+.1f}[/{style}]" if style else f"{lift_pp:+.1f}",
            f"{v['avg_ret_present']:+.2f}%",
        )
    _console.print(tbl)


def build_llm_prompt(
    signals: list[dict],
    lift: dict[str, dict],
    grade_summary: dict[str, dict],
    current_params: dict,
) -> str:
    n = len(signals)
    factors_json = json.dumps(current_params.get("factors", {}), ensure_ascii=False, indent=2)
    gates_json = json.dumps(current_params.get("grade_thresholds", {}), ensure_ascii=False, indent=2)

    lift_lines = []
    for name, v in sorted(lift.items(), key=lambda x: -x[1]["lift_pp"]):
        lift_lines.append(
            f"  {name}: 有({v['n_present']}筆)勝率{v['present_wr']*100:.1f}% "
            f"vs 無({v['n_absent']}筆){v['absent_wr']*100:.1f}%  "
            f"Lift={v['lift_pp']:+.1f}pp  均報酬{v['avg_ret_present']:+.2f}%"
        )

    grade_lines = []
    for g, v in grade_summary.items():
        grade_lines.append(
            f"  {g}: N={v['n']}, T+1勝率={v['t1_wr']*100:.1f}%, 均報酬={v['t1_avg_ret']:+.2f}%"
        )

    return f"""你是台灣股市量化交易因子優化專家。以下是「噴發雷達」系統的因子表現數據，請分析並建議優化方向。

## 現有因子權重（config/surge_params.json）
### factors（加分點數）：
{factors_json}

### grade_thresholds（等級門檻）：
{gates_json}

## 統計期間：{n} 筆已結算信號（T+1 收盤結算）

## 等級表現：
{chr(10).join(grade_lines)}

## 因子 Lift 分析（T+1 勝率差異）：
{chr(10).join(lift_lines)}

## 分析要求：
1. 找出 Lift > 10pp 的高效因子，這些應該增加點數
2. 找出 Lift < 3pp 的低效因子，考慮減少點數或移除
3. 若 SURGE_GAMMA 勝率 < 45%，建議提高門檻（減少雜訊）
4. 若樣本數 < 10，標記為「樣本不足，暫不調整」
5. 考慮因子組合效應（例如 gap + inst_buy 同時存在）

## 輸出格式（嚴格遵守 JSON）：
{{
  "reasoning": "2-3句整體評估",
  "factor_changes": {{
    "factor_name": {{"current": N, "proposed": M, "reason": "..."}},
    ...
  }},
  "gate_changes": {{
    "SURGE_ALPHA": {{"current": N, "proposed": M, "reason": "..."}},
    ...
  }},
  "new_factors_to_consider": ["描述1", "描述2"],
  "confidence": "high/medium/low",
  "min_signals_needed": N
}}

只輸出 JSON，不要其他文字。"""


def call_llm_optimization(prompt: str) -> dict | None:
    """Call LLM and parse JSON response. Returns None on failure."""
    try:
        from taiwan_stock_agent.domain.llm_provider import create_llm_provider
        llm = create_llm_provider(None)
        if llm is None:
            _console.print("[yellow]⚠ 找不到 LLM API key，略過 LLM 分析[/yellow]")
            return None
        _console.print(f"  [dim]呼叫 {llm.name} 分析因子…[/dim]")
        raw = llm.complete(prompt, max_tokens=1500)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        _console.print(f"[red]LLM 回應無法解析 JSON: {e}[/red]")
        return None
    except Exception as e:
        _console.print(f"[red]LLM 呼叫失敗: {e}[/red]")
        return None


def print_llm_suggestions(suggestion: dict, current_params: dict) -> None:
    _console.rule("[bold cyan]LLM 優化建議")
    _console.print(f"\n[bold]整體評估：[/bold] {suggestion.get('reasoning', '')}")
    _console.print(f"[dim]信心度：{suggestion.get('confidence', '?')} | "
                   f"建議最少樣本：{suggestion.get('min_signals_needed', '?')} 筆[/dim]\n")

    changes = suggestion.get("factor_changes", {})
    if changes:
        tbl = Table(title="因子權重建議", box=box.SIMPLE_HEAVY)
        tbl.add_column("因子"); tbl.add_column("現在", justify="right")
        tbl.add_column("建議", justify="right"); tbl.add_column("原因")
        for fname, v in changes.items():
            cur = v.get("current", "?"); prop = v.get("proposed", "?")
            diff = "" if cur == prop else (" ▲" if (prop or 0) > (cur or 0) else " ▼")
            style = "green" if diff == " ▲" else ("red" if diff == " ▼" else "")
            tbl.add_row(fname, str(cur), f"[{style}]{prop}{diff}[/{style}]", v.get("reason", ""))
        _console.print(tbl)

    gate_changes = suggestion.get("gate_changes", {})
    if gate_changes:
        tbl2 = Table(title="等級門檻建議", box=box.SIMPLE_HEAVY)
        tbl2.add_column("等級"); tbl2.add_column("現在", justify="right")
        tbl2.add_column("建議", justify="right"); tbl2.add_column("原因")
        for gname, v in gate_changes.items():
            tbl2.add_row(gname, str(v.get("current")), str(v.get("proposed")), v.get("reason", ""))
        _console.print(tbl2)

    new_factors = suggestion.get("new_factors_to_consider", [])
    if new_factors:
        _console.print("\n[bold]建議新增因子：[/bold]")
        for f in new_factors:
            _console.print(f"  • {f}")


def apply_suggestions_interactive(suggestion: dict, params: dict) -> bool:
    """Interactively apply LLM suggestions to surge_params.json."""
    if not sys.stdin.isatty():
        _console.print("[yellow]非互動模式，略過套用[/yellow]")
        return False

    _console.print("\n[bold cyan]是否套用以上建議？[/bold cyan]")
    _console.print("  [y] 全部套用  [n] 略過  [s] 儲存建議至 config/surge_pending.json")
    choice = input("> ").strip().lower()

    if choice == "n":
        return False

    if choice == "s":
        pending_path = _PARAMS_PATH.parent / "surge_pending.json"
        pending_path.write_text(json.dumps(suggestion, ensure_ascii=False, indent=2))
        _console.print(f"[green]建議已儲存至 {pending_path}[/green]")
        return False

    for fname, v in suggestion.get("factor_changes", {}).items():
        if fname in params.get("factors", {}):
            params["factors"][fname] = v["proposed"]

    for gname, v in suggestion.get("gate_changes", {}).items():
        if gname in params.get("grade_thresholds", {}):
            params["grade_thresholds"][gname] = v["proposed"]

    _PARAMS_PATH.write_text(json.dumps(params, ensure_ascii=False, indent=2))
    _console.print(f"[green]✓ 已套用並儲存至 {_PARAMS_PATH}[/green]")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Surge factor lift analysis and LLM optimization")
    parser.add_argument("--llm", action="store_true", help="呼叫 LLM 分析並建議權重")
    parser.add_argument("--apply", action="store_true", help="互動式套用 LLM 建議")
    parser.add_argument("--min-signals", type=int, default=30, help="最少已結算信號數（default: 30）")
    parser.add_argument("--lookback", type=int, default=90, help="回溯天數（default: 90）")
    args = parser.parse_args()

    from surge_db import query_settled
    try:
        signals = query_settled(min_settled=args.min_signals, lookback_days=args.lookback)
    except ValueError as e:
        _console.print(f"[yellow]{e}[/yellow]")
        return

    _console.rule(f"[bold]噴發雷達 因子分析 — {len(signals)} 筆已結算信號")

    lift = compute_lift(signals)
    grade_summary = build_grade_summary(signals)
    print_lift_table(lift)

    _console.print()
    for g, v in grade_summary.items():
        _console.print(f"  {g}: N={v['n']}, T+1勝率={v['t1_wr']*100:.1f}%, 均報酬={v['t1_avg_ret']:+.2f}%")

    if not (args.llm or args.apply):
        return

    current_params = json.loads(_PARAMS_PATH.read_text())
    prompt = build_llm_prompt(signals, lift, grade_summary, current_params)
    suggestion = call_llm_optimization(prompt)
    if suggestion is None:
        return

    print_llm_suggestions(suggestion, current_params)

    if args.apply:
        apply_suggestions_interactive(suggestion, current_params)


if __name__ == "__main__":
    main()
