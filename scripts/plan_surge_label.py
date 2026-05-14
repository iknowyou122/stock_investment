"""Label plan signals with surge confirmation outcomes.

For each unsettled plan signal in signal_outcomes (action=LONG/WATCH, outcome_1d IS NULL),
check if the ticker appeared in surge CSVs within D+1~D+3 (ALPHA or BETA grade).

  WIN  → outcome_1d = actual day_chg_pct from surge CSV (positive return)
  MISS → outcome_1d = 0  (no surge within lookforward window)

Only marks signals old enough that D+3 has passed (so MISSes are final).

Usage:
    python scripts/plan_surge_label.py
    python scripts/plan_surge_label.py --dry-run
    python scripts/plan_surge_label.py --lookback 60
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taiwan_stock_agent.infrastructure.db import get_connection, init_pool

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_SCANS_DIR = Path(__file__).resolve().parents[1] / "data" / "scans"
_SURGE_GRADES = {"SURGE_ALPHA", "SURGE_BETA"}
# Calendar days to search forward for a surge hit
_CAL_LOOKFORWARD = 5


def _load_surge_tickers(surge_date: date) -> dict[str, dict]:
    """Return {ticker: row} for ALPHA/BETA entries on surge_date."""
    path = _SCANS_DIR / f"surge_{surge_date}.csv"
    if not path.exists():
        return {}
    result: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("grade", "") in _SURGE_GRADES:
                result[row["ticker"]] = row
    return result


def _find_surge_hit(ticker: str, signal_date: date) -> tuple[float | None, str | None]:
    """Return (day_chg_pct, surge_date_str) if ticker surged within D+1~D+N, else (None, None)."""
    for offset in range(1, _CAL_LOOKFORWARD + 1):
        check_date = signal_date + timedelta(days=offset)
        hits = _load_surge_tickers(check_date)
        if ticker in hits:
            chg = float(hits[ticker].get("day_chg_pct") or 0)
            return chg, str(check_date)
    return None, None


def label_signals(
    dry_run: bool = False,
    lookback_days: int = 30,
) -> dict[str, int]:
    """Mark unsettled plan signals WIN/MISS based on surge CSV appearance.

    Returns stats dict: {labeled_win, labeled_miss, too_recent, skipped}.
    """
    init_pool()
    stats = {"labeled_win": 0, "labeled_miss": 0, "too_recent": 0, "skipped": 0}

    # Only finalize signals where D+3 calendar has already passed
    # (use _CAL_LOOKFORWARD as the settle delay)
    settle_cutoff = date.today() - timedelta(days=_CAL_LOOKFORWARD)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT signal_id, ticker, signal_date
                FROM signal_outcomes
                WHERE action IN ('LONG', 'WATCH')
                  AND halt_flag = FALSE
                  AND outcome_1d IS NULL
                  AND score_breakdown IS NOT NULL
                  AND signal_date >= CURRENT_DATE - (%s * INTERVAL '1 day')
                  AND signal_date <= %s
                ORDER BY signal_date
                """,
                (lookback_days, settle_cutoff),
            )
            rows = cur.fetchall()

        updates: list[tuple] = []
        for signal_id, ticker, signal_date in rows:
            if signal_date > settle_cutoff:
                stats["too_recent"] += 1
                continue

            chg, surge_date_str = _find_surge_hit(ticker, signal_date)

            if chg is not None:
                updates.append((chg, chg, chg, signal_id))
                stats["labeled_win"] += 1
                logger.info(
                    "WIN  %s  plan=%s  surge=%s  chg=+%.1f%%",
                    ticker, signal_date, surge_date_str, chg,
                )
            else:
                updates.append((0.0, 0.0, 0.0, signal_id))
                stats["labeled_miss"] += 1
                logger.info("MISS %s  plan=%s  (no surge within D+%d)", ticker, signal_date, _CAL_LOOKFORWARD)

        if not dry_run and updates:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    UPDATE signal_outcomes
                    SET outcome_1d = %s, outcome_3d = %s, outcome_5d = %s
                    WHERE signal_id = %s
                    """,
                    updates,
                )
            conn.commit()
            logger.info("Committed %d updates.", len(updates))
        elif dry_run:
            logger.info("[DRY RUN] Would update %d records.", len(updates))

    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Label plan signals with surge outcomes")
    ap.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    ap.add_argument("--lookback", type=int, default=30, help="Days of history to label")
    args = ap.parse_args()

    stats = label_signals(dry_run=args.dry_run, lookback_days=args.lookback)
    wins = stats["labeled_win"]
    misses = stats["labeled_miss"]
    total = wins + misses
    rate = wins / total if total else 0.0
    print(
        f"\n{'[DRY RUN] ' if args.dry_run else ''}"
        f"標記完成：WIN {wins} / MISS {misses} / 合計 {total}  "
        f"命中率 {rate:.1%}"
    )


if __name__ == "__main__":
    main()
