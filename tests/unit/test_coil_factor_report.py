"""Unit tests for coil_factor_report.py pure functions."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make scripts directory importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from coil_factor_report import compute_lift, factor_status  # type: ignore[import]


class TestComputeLift:
    def test_lift_calculation(self):
        """Lift = win_rate_present / win_rate_absent.

        present: 8/10 = 0.8, absent: 2/10 = 0.2 → lift = 4.0
        """
        lift = compute_lift(
            present_wins=8,
            present_total=10,
            absent_wins=2,
            absent_total=10,
        )
        assert lift == pytest.approx(4.0)

    def test_lift_equal_win_rates(self):
        """If present and absent have same win rate, lift = 1.0."""
        lift = compute_lift(
            present_wins=5,
            present_total=10,
            absent_wins=5,
            absent_total=10,
        )
        assert lift == pytest.approx(1.0)

    def test_lift_zero_absent_returns_none(self):
        """Division by zero case: all signals have factor present (absent_total == 0)."""
        lift = compute_lift(
            present_wins=8,
            present_total=10,
            absent_wins=0,
            absent_total=0,
        )
        assert lift is None

    def test_lift_zero_absent_win_rate_returns_none(self):
        """Absent group has samples but zero wins → win_rate_absent == 0, lift undefined."""
        lift = compute_lift(
            present_wins=5,
            present_total=10,
            absent_wins=0,
            absent_total=5,
        )
        assert lift is None

    def test_lift_zero_present_total_returns_none(self):
        """No signals have the factor present → present_total == 0."""
        lift = compute_lift(
            present_wins=0,
            present_total=0,
            absent_wins=3,
            absent_total=10,
        )
        assert lift is None

    def test_lift_below_one_when_present_underperforms(self):
        """Factor present associated with lower win rate → lift < 1."""
        lift = compute_lift(
            present_wins=2,
            present_total=10,
            absent_wins=8,
            absent_total=10,
        )
        assert lift is not None
        assert lift == pytest.approx(0.25)

    def test_lift_fractional_sample_sizes(self):
        """Standard calculation with non-round sample sizes."""
        # present: 3/7 ≈ 0.4286, absent: 4/13 ≈ 0.3077 → lift ≈ 1.393
        lift = compute_lift(
            present_wins=3,
            present_total=7,
            absent_wins=4,
            absent_total=13,
        )
        assert lift is not None
        assert lift == pytest.approx((3 / 7) / (4 / 13), rel=1e-4)


class TestFactorStatus:
    def test_factor_status_strong(self):
        """lift >= 1.2 → STRONG."""
        assert factor_status(1.2) == "✓ STRONG"
        assert factor_status(2.0) == "✓ STRONG"
        assert factor_status(1.5) == "✓ STRONG"

    def test_factor_status_weak(self):
        """lift < 1.05 → WEAK."""
        assert factor_status(1.04) == "⚠ WEAK"
        assert factor_status(0.5) == "⚠ WEAK"
        assert factor_status(0.0) == "⚠ WEAK"

    def test_factor_status_ok(self):
        """1.05 <= lift < 1.2 → OK."""
        assert factor_status(1.05) == "— OK"
        assert factor_status(1.1) == "— OK"
        assert factor_status(1.19) == "— OK"

    def test_factor_status_none_returns_na(self):
        """None lift (division by zero) → N/A."""
        assert factor_status(None) == "N/A"

    def test_factor_status_boundary_1_05(self):
        """Boundary: exactly 1.05 → OK (not WEAK)."""
        assert factor_status(1.05) == "— OK"

    def test_factor_status_boundary_1_2(self):
        """Boundary: exactly 1.2 → STRONG (not OK)."""
        assert factor_status(1.2) == "✓ STRONG"
