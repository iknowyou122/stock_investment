"""Bayesian win-rate updater for broker branch labels.

Implements a Laplace-smoothed Beta-Bernoulli posterior over community-reported
trade outcomes.  The update formula is:

    community_signal_win_rate = (win_count + 1) / (sample_count + 2)

This is equivalent to a Beta(1, 1) uniform prior updated with observed wins
and losses — Laplace smoothing prevents 0/1 extremes at small sample sizes
and converges to the empirical rate as N grows.

What counts as a "win":
    - outcome = 'win'    → contributes to both wins and samples
    - outcome = 'lose'   → contributes to samples only
    - outcome = 'break_even' → contributes to samples only
    - outcome IS NULL    → excluded entirely (trade not yet settled)

Design note: community_signal_win_rate is kept separate from reversal_rate.
reversal_rate comes from FinMind D+2 institutional flow data; this column
reflects community-reported forward outcomes on actual trades.

Usage
-----
    updater = BayesianLabelUpdater()
    n = updater.run_full_update()
    print(f"Updated {n} branch(es).")

Cron idiom (see scripts/run_bayesian_update.py):
    0 2 * * * python3 scripts/run_bayesian_update.py
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class BayesianLabelUpdater:
    """Compute and persist Laplace-smoothed win rates to broker_labels.

    The constructor takes no required arguments so the class is importable
    and unit-testable without a database connection.  DB access happens
    only inside methods that need it.
    """

    # ------------------------------------------------------------------
    # Pure / static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def compute_win_rate(win_count: int, sample_count: int) -> float:
        """Return the Laplace-smoothed Beta-Bernoulli posterior.

        Formula: (win_count + 1) / (sample_count + 2)

        Works correctly at zero: compute_win_rate(0, 0) == 0.5 (maximum
        uncertainty — uniform prior).  The result is always in (0, 1).

        Parameters
        ----------
        win_count:
            Number of 'win' outcomes observed.
        sample_count:
            Total number of settled outcomes (wins + non-wins).
        """
        return (win_count + 1) / (sample_count + 2)

    # ------------------------------------------------------------------
    # DB-backed methods
    # ------------------------------------------------------------------

    def update_branch(
        self,
        branch_code: str,
        new_wins: int,
        new_samples: int,
    ) -> None:
        """Increment community counts and recompute win rate for one branch.

        Performs an atomic UPDATE on broker_labels:
          - Adds new_wins to community_win_count
          - Adds new_samples to community_sample_count
          - Recomputes community_signal_win_rate via compute_win_rate()

        Parameters
        ----------
        branch_code:
            The broker branch code to update (primary key in broker_labels).
        new_wins:
            Additional win count to add (may be 0).
        new_samples:
            Additional sample count to add (must be >= new_wins).
        """
        from taiwan_stock_agent.infrastructure.db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Read current cumulative counts
                cur.execute(
                    """
                    SELECT community_win_count, community_sample_count
                    FROM broker_labels
                    WHERE branch_code = %s
                    """,
                    (branch_code,),
                )
                row = cur.fetchone()
                if row is None:
                    logger.warning(
                        "update_branch: branch_code '%s' not found in broker_labels — skipping",
                        branch_code,
                    )
                    return

                current_wins: int = row[0]
                current_samples: int = row[1]

                total_wins = current_wins + new_wins
                total_samples = current_samples + new_samples
                new_rate = self.compute_win_rate(total_wins, total_samples)

                cur.execute(
                    """
                    UPDATE broker_labels
                    SET community_win_count    = %s,
                        community_sample_count = %s,
                        community_signal_win_rate = %s
                    WHERE branch_code = %s
                    """,
                    (total_wins, total_samples, new_rate, branch_code),
                )

        logger.debug(
            "update_branch: %s — wins=%d samples=%d rate=%.4f",
            branch_code,
            total_wins,
            total_samples,
            new_rate,
        )

    def run_full_update(self) -> int:
        """Recompute community win rates for all branches with settled outcomes.

        Queries community_outcomes for all distinct branch codes that appear in
        rows with a settled (non-NULL) outcome, then for each branch aggregates
        wins and samples and writes the result to broker_labels.

        The update is idempotent: community_signal_win_rate is derived
        deterministically from cumulative counts each time.

        Returns
        -------
        int
            Number of distinct branch codes updated.  Returns 0 if no settled
            community outcomes exist yet.
        """
        from taiwan_stock_agent.infrastructure.db import get_connection

        # Step 1: collect all branch codes that have at least one settled outcome.
        # unnest() expands the TEXT[] array into individual rows.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT unnest(branch_codes) AS branch_code
                    FROM community_outcomes
                    WHERE outcome IS NOT NULL
                      AND array_length(branch_codes, 1) > 0
                    """
                )
                branch_rows = cur.fetchall()

        if not branch_rows:
            logger.info("run_full_update: no settled community outcomes found — nothing to update")
            return 0

        branch_codes = [r[0] for r in branch_rows]
        logger.info("run_full_update: updating %d branch(es)", len(branch_codes))

        updated = 0
        for branch_code in branch_codes:
            # Step 2: aggregate wins and samples for this branch across all
            # settled community_outcomes rows where the branch appears.
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            COUNT(*) AS total_samples,
                            SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins
                        FROM community_outcomes
                        WHERE outcome IS NOT NULL
                          AND %s = ANY(branch_codes)
                        """,
                        (branch_code,),
                    )
                    agg = cur.fetchone()

            if agg is None or agg[0] == 0:
                logger.debug("run_full_update: no data for branch %s — skipping", branch_code)
                continue

            total_samples: int = int(agg[0])
            wins: int = int(agg[1]) if agg[1] is not None else 0
            win_rate = self.compute_win_rate(wins, total_samples)

            # Step 3: write the result directly (not incremental — we recompute
            # from the full aggregate each run so the update is idempotent).
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE broker_labels
                        SET community_win_count    = %s,
                            community_sample_count = %s,
                            community_signal_win_rate = %s
                        WHERE branch_code = %s
                        """,
                        (wins, total_samples, win_rate, branch_code),
                    )

            logger.debug(
                "run_full_update: %s — wins=%d samples=%d rate=%.4f",
                branch_code,
                wins,
                total_samples,
                win_rate,
            )
            updated += 1

        logger.info("run_full_update: done — updated %d branch(es)", updated)
        return updated
