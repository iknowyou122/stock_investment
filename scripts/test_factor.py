"""Factor sandbox: test an experimental factor against historical breakdowns.

Usage:
    make test-factor FACTOR=my_factor_name

The factor module must be at:
    src/taiwan_stock_agent/factors/experimental/<FACTOR>.py

It must implement:
    def compute(breakdown: dict) -> int:
        # return additional pts (positive or negative)
        ...
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.infrastructure.db import init_pool, get_connection


def run_test(factor_name: str) -> None:
    factor_path = (
        Path(__file__).resolve().parents[1]
        / "src" / "taiwan_stock_agent" / "factors" / "experimental"
        / f"{factor_name}.py"
    )
    if not factor_path.exists():
        print(f"Factor file not found: {factor_path}")
        print("Create the file with a compute(breakdown: dict) -> int function.")
        sys.exit(1)

    spec = importlib.util.spec_from_file_location(factor_name, factor_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    if not hasattr(mod, "compute"):
        print("Factor module must implement compute(breakdown: dict) -> int")
        sys.exit(1)

    try:
        init_pool()
    except Exception as e:
        print(f"Cannot connect to database: {e}")
        print("Set DATABASE_URL environment variable and run make backtest first.")
        return

    rows = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT outcome_1d, score_breakdown
                    FROM signal_outcomes
                    WHERE outcome_1d IS NOT NULL AND score_breakdown IS NOT NULL
                    ORDER BY signal_date DESC LIMIT 500
                """)
                for outcome, bd in cur.fetchall():
                    if isinstance(bd, str):
                        bd = json.loads(bd)
                    rows.append({"outcome_1d": outcome, "score_breakdown": bd})
    except Exception as e:
        print(f"Error querying signal_outcomes: {e}")
        return

    if not rows:
        print("No data available. Run make backtest first.")
        return

    baseline_win = sum(1 for r in rows if r["outcome_1d"] > 0) / len(rows)

    boosted = []
    for r in rows:
        try:
            extra = mod.compute(r["score_breakdown"])
        except Exception:
            extra = 0
        if extra > 0:
            boosted.append(r)

    if not boosted:
        print(f"Factor '{factor_name}': triggered on 0/{len(rows)} signals.")
        return

    boosted_win = sum(1 for r in boosted if r["outcome_1d"] > 0) / len(boosted)
    lift = boosted_win - baseline_win

    print(f"\nFactor: {factor_name}")
    print(f"  觸發: {len(boosted)}/{len(rows)} 訊號 ({len(boosted)/len(rows):.1%})")
    print(f"  基準勝率: {baseline_win:.1%}")
    print(f"  觸發後勝率: {boosted_win:.1%}")
    print(f"  Lift: {lift:+.1%}")

    if lift > 0.05:
        print("\n  ✅ 建議升級為 active (lift > +5%)")
    elif lift < -0.03:
        print("\n  ❌ 建議丟棄 (lift < -3%)")
    else:
        print("\n  ⚠ 效果不明顯，繼續觀察")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factor", required=True)
    args = parser.parse_args()
    run_test(args.factor)


if __name__ == "__main__":
    main()
