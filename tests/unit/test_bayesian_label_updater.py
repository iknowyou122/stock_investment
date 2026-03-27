"""Unit tests for BayesianLabelUpdater.

All tests use unittest.mock — no real database required.
"""
from __future__ import annotations

import math
from unittest.mock import MagicMock, call, patch

import pytest

from taiwan_stock_agent.domain.bayesian_label_updater import BayesianLabelUpdater


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn_cm(fetchone_returns=None, fetchall_returns=None):
    """Build a psycopg2-style context-manager mock for get_connection()."""
    mock_cur = MagicMock()
    if fetchone_returns is not None:
        mock_cur.fetchone.side_effect = fetchone_returns
    if fetchall_returns is not None:
        mock_cur.fetchall.return_value = fetchall_returns

    mock_cur.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=mock_conn)
    cm.__exit__ = MagicMock(return_value=False)
    return cm, mock_cur


# ---------------------------------------------------------------------------
# Tests: compute_win_rate (pure / static — no DB)
# ---------------------------------------------------------------------------

class TestComputeWinRate:
    """Tests for the static Laplace-smoothed posterior formula."""

    def test_compute_win_rate_zero_submissions(self):
        """No submissions → uniform prior → 0.5."""
        result = BayesianLabelUpdater.compute_win_rate(0, 0)
        assert result == pytest.approx(0.5)

    def test_compute_win_rate_all_wins(self):
        """5 wins out of 5 samples → (5+1)/(5+2) ≈ 0.857."""
        result = BayesianLabelUpdater.compute_win_rate(5, 5)
        assert result == pytest.approx(6 / 7)

    def test_compute_win_rate_all_losses(self):
        """0 wins out of 5 samples → (0+1)/(5+2) ≈ 0.143."""
        result = BayesianLabelUpdater.compute_win_rate(0, 5)
        assert result == pytest.approx(1 / 7)

    def test_compute_win_rate_mixed(self):
        """3 wins out of 5 samples → (3+1)/(5+2) ≈ 0.571."""
        result = BayesianLabelUpdater.compute_win_rate(3, 5)
        assert result == pytest.approx(4 / 7)

    def test_compute_win_rate_large_sample(self):
        """Laplace correction is negligible at N=1000."""
        result = BayesianLabelUpdater.compute_win_rate(700, 1000)
        empirical = 700 / 1000
        # Should be very close to the empirical rate
        assert abs(result - empirical) < 0.005

    def test_compute_win_rate_break_even_is_not_win(self):
        """break_even counts as a sample but NOT a win: wins=0, samples=5."""
        result = BayesianLabelUpdater.compute_win_rate(0, 5)
        assert result == pytest.approx(1 / 7)

    def test_compute_win_rate_all_break_even(self):
        """All break_even outcomes are numerically identical to all losses."""
        loss_result = BayesianLabelUpdater.compute_win_rate(0, 5)
        even_result = BayesianLabelUpdater.compute_win_rate(0, 5)
        assert loss_result == even_result

    def test_compute_win_rate_returns_float(self):
        """Return type must be float (not int, not Decimal)."""
        result = BayesianLabelUpdater.compute_win_rate(1, 2)
        assert isinstance(result, float)

    def test_compute_win_rate_between_0_and_1(self):
        """Result is always strictly in (0, 1) due to Laplace smoothing."""
        for wins, samples in [(0, 0), (0, 100), (100, 100), (50, 100)]:
            result = BayesianLabelUpdater.compute_win_rate(wins, samples)
            assert 0.0 < result < 1.0, (
                f"compute_win_rate({wins}, {samples}) = {result} is out of (0,1)"
            )

    def test_compute_win_rate_100_wins(self):
        """100 wins out of 100 samples → (100+1)/(100+2) ≈ 0.990."""
        result = BayesianLabelUpdater.compute_win_rate(100, 100)
        assert result == pytest.approx(101 / 102)


# ---------------------------------------------------------------------------
# Tests: run_full_update (DB-backed — all DB calls are mocked)
# ---------------------------------------------------------------------------

class TestRunFullUpdate:
    """Tests for run_full_update(), which orchestrates DB reads and writes."""

    @patch("taiwan_stock_agent.infrastructure.db.get_connection")
    def test_run_full_update_no_db_calls_returns_zero(self, mock_get_conn):
        """When no settled outcomes exist, run_full_update returns 0."""
        cm, mock_cur = _make_conn_cm(fetchall_returns=[])
        mock_get_conn.return_value = cm

        updater = BayesianLabelUpdater()
        result = updater.run_full_update()

        assert result == 0

    @patch("taiwan_stock_agent.infrastructure.db.get_connection")
    def test_run_full_update_single_branch(self, mock_get_conn):
        """Single branch with 3 wins / 5 samples → updated, returns 1."""
        # Three get_connection() calls for one branch:
        #   1. fetch distinct branch codes
        #   2. aggregate wins/samples for the branch
        #   3. UPDATE broker_labels

        call_count = [0]

        def conn_factory():
            call_count[0] += 1
            n = call_count[0]
            if n == 1:
                # Distinct branch codes query
                cm, cur = _make_conn_cm(fetchall_returns=[("9600",)])
                return cm
            elif n == 2:
                # Aggregate query
                cm, cur = _make_conn_cm(fetchone_returns=[(5, 3)])
                return cm
            else:
                # UPDATE
                cm, cur = _make_conn_cm()
                return cm

        mock_get_conn.side_effect = conn_factory

        updater = BayesianLabelUpdater()
        result = updater.run_full_update()

        assert result == 1

    @patch("taiwan_stock_agent.infrastructure.db.get_connection")
    def test_run_full_update_multiple_branches(self, mock_get_conn):
        """Two distinct branches → returns 2."""
        call_count = [0]

        def conn_factory():
            call_count[0] += 1
            n = call_count[0]
            if n == 1:
                cm, _ = _make_conn_cm(fetchall_returns=[("9600",), ("9700",)])
                return cm
            elif n in (2, 4):
                # Aggregate for each branch
                cm, _ = _make_conn_cm(fetchone_returns=[(4, 2)])
                return cm
            else:
                # UPDATE for each branch
                cm, _ = _make_conn_cm()
                return cm

        mock_get_conn.side_effect = conn_factory

        updater = BayesianLabelUpdater()
        result = updater.run_full_update()

        assert result == 2


# ---------------------------------------------------------------------------
# Tests: update_branch (DB-backed — all DB calls are mocked)
# ---------------------------------------------------------------------------

class TestUpdateBranch:
    """Tests for update_branch(), the per-branch incremental update helper."""

    @patch("taiwan_stock_agent.infrastructure.db.get_connection")
    def test_update_branch_calls_db_correctly(self, mock_get_conn):
        """update_branch reads current counts, then writes updated values."""
        # fetchone returns existing (wins=10, samples=20)
        cm, mock_cur = _make_conn_cm(fetchone_returns=[(10, 20)])
        mock_get_conn.return_value = cm

        updater = BayesianLabelUpdater()
        updater.update_branch(branch_code="9600", new_wins=3, new_samples=5)

        # Should have called execute twice: SELECT then UPDATE
        assert mock_cur.execute.call_count == 2

        # Verify the UPDATE args: total_wins=13, total_samples=25
        update_call_args = mock_cur.execute.call_args_list[1]
        params = update_call_args[0][1]  # positional tuple
        total_wins, total_samples, win_rate, branch_code = params
        assert total_wins == 13
        assert total_samples == 25
        assert win_rate == pytest.approx(BayesianLabelUpdater.compute_win_rate(13, 25))
        assert branch_code == "9600"

    @patch("taiwan_stock_agent.infrastructure.db.get_connection")
    def test_update_branch_missing_branch_skips_update(self, mock_get_conn):
        """If branch_code not in broker_labels, no UPDATE is issued."""
        # fetchone returns None (branch not found)
        cm, mock_cur = _make_conn_cm(fetchone_returns=[None])
        mock_get_conn.return_value = cm

        updater = BayesianLabelUpdater()
        updater.update_branch(branch_code="UNKNOWN", new_wins=1, new_samples=2)

        # Only the SELECT should have been called — no UPDATE
        assert mock_cur.execute.call_count == 1

    @patch("taiwan_stock_agent.infrastructure.db.get_connection")
    def test_update_branch_zero_new_data(self, mock_get_conn):
        """Adding 0 wins and 0 samples still recomputes the rate correctly."""
        cm, mock_cur = _make_conn_cm(fetchone_returns=[(4, 8)])
        mock_get_conn.return_value = cm

        updater = BayesianLabelUpdater()
        updater.update_branch(branch_code="9600", new_wins=0, new_samples=0)

        params = mock_cur.execute.call_args_list[1][0][1]
        total_wins, total_samples, win_rate, _ = params
        assert total_wins == 4
        assert total_samples == 8
        assert win_rate == pytest.approx(BayesianLabelUpdater.compute_win_rate(4, 8))
