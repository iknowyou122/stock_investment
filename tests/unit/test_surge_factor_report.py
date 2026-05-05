"""Tests for surge factor lift analysis."""
import json
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../scripts"))


def _make_signals(n=40, win_pct=0.6):
    """Generate n synthetic settled signal dicts."""
    import random
    random.seed(42)
    signals = []
    for i in range(n):
        is_win = random.random() < win_pct
        signals.append({
            "ticker": f"{2000+i}",
            "signal_date": "2026-04-01",
            "grade": "SURGE_ALPHA" if i % 3 == 0 else "SURGE_BETA",
            "score": 55 + (i % 30),
            "vol_ratio": 1.5 + (i % 5),
            "gap_pct": (i % 10) * 0.5,
            "rsi": 50 + (i % 25),
            "inst_consec_days": i % 4,
            "industry_rank_pct": (i % 5) * 20.0,
            "close_strength": 0.5 + (i % 5) * 0.1,
            "score_breakdown": json.dumps({"pocket_pivot": 12 if i % 2 == 0 else 0}),
            "t1_return_pct": 2.5 if is_win else -1.5,
        })
    return signals


class TestComputeLift:
    def test_returns_dict_per_factor(self):
        from surge_factor_report import compute_lift
        signals = _make_signals(40)
        lift = compute_lift(signals)
        assert "vol_ratio_3x" in lift
        assert "gap_1pct" in lift
        assert "pocket_pivot" in lift

    def test_lift_has_required_keys(self):
        from surge_factor_report import compute_lift
        signals = _make_signals(40)
        lift = compute_lift(signals)
        factor = lift["vol_ratio_3x"]
        assert "present_wr" in factor
        assert "absent_wr" in factor
        assert "lift_pp" in factor
        assert "n_present" in factor
        assert "n_absent" in factor

    def test_win_rate_between_0_and_1(self):
        from surge_factor_report import compute_lift
        signals = _make_signals(40)
        lift = compute_lift(signals)
        for f, vals in lift.items():
            assert 0.0 <= vals["present_wr"] <= 1.0, f"{f} present_wr out of range"
            assert 0.0 <= vals["absent_wr"] <= 1.0, f"{f} absent_wr out of range"

    def test_pocket_pivot_from_breakdown(self):
        from surge_factor_report import compute_lift
        signals = _make_signals(40)
        lift = compute_lift(signals)
        assert "pocket_pivot" in lift


class TestBuildGradeSummary:
    def test_grade_summary_keys(self):
        from surge_factor_report import build_grade_summary
        signals = _make_signals(40)
        summary = build_grade_summary(signals)
        assert "SURGE_ALPHA" in summary
        alpha = summary["SURGE_ALPHA"]
        assert "n" in alpha
        assert "t1_wr" in alpha
        assert "t1_avg_ret" in alpha

    def test_win_rate_is_fraction(self):
        from surge_factor_report import build_grade_summary
        signals = _make_signals(40)
        summary = build_grade_summary(signals)
        for grade, vals in summary.items():
            assert 0.0 <= vals["t1_wr"] <= 1.0
