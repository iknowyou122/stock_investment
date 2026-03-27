#!/usr/bin/env python3
"""Daily Bayesian update for community_signal_win_rate on broker_labels.

Run daily via cron:
    0 2 * * * /path/to/venv/bin/python3 /path/to/scripts/run_bayesian_update.py

What this does
--------------
Queries community_outcomes for all settled (non-NULL outcome) rows, aggregates
wins and total samples per broker branch code, then writes the Laplace-smoothed
posterior win rate back to broker_labels.community_signal_win_rate.

Idempotency
-----------
community_signal_win_rate is computed deterministically from the full cumulative
aggregate each run. Re-running on partial failure is safe — the update will
converge to the correct state.

Exit codes
----------
0 — success (even if 0 branches were updated)
1 — unhandled exception
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the repo root without installing the package:
#   python3 scripts/run_bayesian_update.py
_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "src"))


def main() -> int:
    from taiwan_stock_agent.domain.bayesian_label_updater import BayesianLabelUpdater

    updater = BayesianLabelUpdater()
    n = updater.run_full_update()
    print(f"Updated {n} branch(es).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
