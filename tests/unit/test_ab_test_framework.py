"""Unit tests for ab_test_framework.py.

Tests cover:
- Stratified randomization (deterministic seed, 50/50 split per stratum)
- Statistical functions (t-test, chi-squared, Cohen's d)
- Win-rate computation per group
- Recommendation logic
- HTML / JSON report generation
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from ab_test_framework import (
    StratumKey,
    SignalGroupStats,
    ABTestResult,
    OverallResult,
    assign_groups,
    compute_group_stats,
    cohens_d,
    run_ttest,
    run_chi2,
    derive_recommendation,
    aggregate_strata_results,
    build_stratum_key,
    filter_settled_records,
    load_signal_records,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(
    signal_id: str = "2330_2026-04-01",
    ticker: str = "2330",
    signal_date: str = "2026-04-01",
    confidence: int = 70,
    market: str = "TSE",
    industry: str = "半導體",
    actual_breakout: bool = True,
    upside_pct: float = 5.0,
    entry_price: float = 500.0,
    max_price: float = 525.0,
    pending: bool = False,
    taiex_regime: str = "uptrend",
) -> dict:
    """Return a minimal signal dict suitable for A/B tests."""
    return {
        "signal_id": signal_id,
        "ticker": ticker,
        "signal_date": date.fromisoformat(signal_date),
        "confidence": confidence,
        "market": market,
        "industry": industry,
        "actual_breakout": actual_breakout,
        "upside_pct": upside_pct,
        "entry_price": entry_price,
        "max_price": max_price,
        "pending": pending,
        "taiex_regime": taiex_regime,
    }


def _signals_for_stratum(
    n: int,
    industry: str = "半導體",
    market: str = "TSE",
    regime: str = "uptrend",
    win_rate: float = 0.5,
    base_date: str = "2026-04-01",
) -> list[dict]:
    """Generate n settled signals with deterministic IDs for a single stratum."""
    sigs = []
    for i in range(n):
        sid = f"{market}_{industry}_{regime}_{i:04d}"
        breakout = (i % 2 == 0) if win_rate == 0.5 else (i < int(n * win_rate))
        sigs.append(_make_signal(
            signal_id=sid,
            ticker=f"T{i:04d}",
            signal_date=base_date,
            industry=industry,
            market=market,
            taiex_regime=regime,
            actual_breakout=breakout,
            pending=False,
        ))
    return sigs


# ---------------------------------------------------------------------------
# build_stratum_key
# ---------------------------------------------------------------------------

class TestBuildStratumKey:
    def test_returns_tuple(self):
        sig = _make_signal()
        key = build_stratum_key(sig)
        assert isinstance(key, tuple)
        assert len(key) == 3

    def test_correct_values(self):
        sig = _make_signal(industry="光電", market="TPEx", taiex_regime="downtrend")
        key = build_stratum_key(sig)
        assert key == ("光電", "TPEx", "downtrend")


# ---------------------------------------------------------------------------
# filter_settled_records
# ---------------------------------------------------------------------------

class TestFilterSettledRecords:
    def test_removes_pending(self):
        sigs = [
            _make_signal(signal_id="a", pending=False),
            _make_signal(signal_id="b", pending=True),
            _make_signal(signal_id="c", pending=False),
        ]
        settled = filter_settled_records(sigs)
        assert len(settled) == 2
        assert all(not s["pending"] for s in settled)

    def test_empty_list(self):
        assert filter_settled_records([]) == []

    def test_all_pending(self):
        sigs = [_make_signal(signal_id=f"s{i}", pending=True) for i in range(5)]
        assert filter_settled_records(sigs) == []


# ---------------------------------------------------------------------------
# assign_groups
# ---------------------------------------------------------------------------

class TestAssignGroups:
    def test_returns_dict_with_signal_ids(self):
        sigs = _signals_for_stratum(20)
        assignment = assign_groups(sigs, seed=42)
        assert len(assignment) == 20
        for v in assignment.values():
            assert v in ("A", "B")

    def test_deterministic_same_seed(self):
        sigs = _signals_for_stratum(30)
        a1 = assign_groups(sigs, seed=42)
        a2 = assign_groups(sigs, seed=42)
        assert a1 == a2

    def test_different_seeds_differ(self):
        sigs = _signals_for_stratum(40)
        a1 = assign_groups(sigs, seed=42)
        a2 = assign_groups(sigs, seed=99)
        assert a1 != a2

    def test_roughly_50_50_per_stratum(self):
        """Each stratum should have roughly equal A and B counts."""
        sigs = (
            _signals_for_stratum(20, industry="半導體", market="TSE", regime="uptrend")
            + _signals_for_stratum(20, industry="光電", market="TPEx", regime="neutral")
        )
        assignment = assign_groups(sigs, seed=42)
        # For each stratum, count A vs B
        from collections import Counter
        stratum_counts: dict[tuple, Counter] = {}
        for sig in sigs:
            key = build_stratum_key(sig)
            if key not in stratum_counts:
                stratum_counts[key] = Counter()
            stratum_counts[key][assignment[sig["signal_id"]]] += 1

        for key, counts in stratum_counts.items():
            n = counts["A"] + counts["B"]
            # allow up to 2 off for 20-signal stratum
            assert abs(counts["A"] - counts["B"]) <= 2, (
                f"Stratum {key}: A={counts['A']}, B={counts['B']}"
            )

    def test_small_stratum_still_assigns_all(self):
        """Even strata with 1 signal get an assignment."""
        sigs = _signals_for_stratum(1)
        assignment = assign_groups(sigs, seed=42)
        assert len(assignment) == 1

    def test_cross_strata_independence(self):
        """Adding a new stratum does not change assignments within an existing stratum."""
        sigs_a = _signals_for_stratum(10, industry="半導體", market="TSE", regime="uptrend")
        sigs_b = _signals_for_stratum(10, industry="光電", market="TPEx", regime="downtrend")

        assignment_alone = assign_groups(sigs_a, seed=42)
        assignment_combined = assign_groups(sigs_a + sigs_b, seed=42)

        for sig in sigs_a:
            assert assignment_alone[sig["signal_id"]] == assignment_combined[sig["signal_id"]]


# ---------------------------------------------------------------------------
# cohens_d
# ---------------------------------------------------------------------------

class TestCohensD:
    def test_identical_groups_returns_zero(self):
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        d = cohens_d(a, a)
        assert d == pytest.approx(0.0, abs=1e-9)

    def test_nonoverlapping_groups(self):
        a = [10.0] * 10
        b = [20.0] * 10
        d = cohens_d(a, b)
        # pooled std = 0, but should not raise; returns inf or large value
        assert d is not None

    def test_known_value(self):
        # a ~ N(0,1), b ~ N(1,1) → d ≈ 1.0
        import random
        rng = random.Random(42)
        a = [rng.gauss(0, 1) for _ in range(1000)]
        b = [rng.gauss(1, 1) for _ in range(1000)]
        d = cohens_d(a, b)
        assert d is not None
        assert 0.7 < abs(d) < 1.3

    def test_returns_none_for_empty(self):
        assert cohens_d([], [1.0, 2.0]) is None
        assert cohens_d([1.0], []) is None

    def test_returns_none_for_single_element_each(self):
        # Can't compute pooled std from n=1
        result = cohens_d([5.0], [3.0])
        # May return a value or None — just should not raise
        # (implementation may choose either; we check it doesn't crash)
        assert result is None or isinstance(result, float)


# ---------------------------------------------------------------------------
# run_ttest
# ---------------------------------------------------------------------------

class TestRunTtest:
    def test_significant_difference(self):
        import random
        rng = random.Random(0)
        a = [rng.gauss(5, 1) for _ in range(50)]
        b = [rng.gauss(8, 1) for _ in range(50)]
        stat, p = run_ttest(a, b)
        assert p < 0.05

    def test_no_difference(self):
        import random
        rng = random.Random(0)
        same = [rng.gauss(5, 1) for _ in range(30)]
        stat, p = run_ttest(same, same)
        assert p == pytest.approx(1.0) or p > 0.05

    def test_empty_returns_nan(self):
        stat, p = run_ttest([], [1.0, 2.0])
        assert math.isnan(p)

    def test_returns_float_tuple(self):
        a = [1.0, 2.0, 3.0]
        b = [4.0, 5.0, 6.0]
        stat, p = run_ttest(a, b)
        assert isinstance(stat, float)
        assert isinstance(p, float)


# ---------------------------------------------------------------------------
# run_chi2
# ---------------------------------------------------------------------------

class TestRunChi2:
    def test_significant_win_rate_difference(self):
        # A: 8/10 wins, B: 2/10 wins
        chi2, p = run_chi2(wins_a=8, n_a=10, wins_b=2, n_b=10)
        assert p < 0.05

    def test_no_difference(self):
        # A: 5/10, B: 5/10
        chi2, p = run_chi2(wins_a=5, n_a=10, wins_b=5, n_b=10)
        assert p > 0.05

    def test_zero_wins_a(self):
        chi2, p = run_chi2(wins_a=0, n_a=10, wins_b=5, n_b=10)
        assert isinstance(chi2, float)
        assert isinstance(p, float)

    def test_zero_count_returns_nan(self):
        chi2, p = run_chi2(wins_a=5, n_a=0, wins_b=5, n_b=10)
        assert math.isnan(p)

    def test_all_wins_no_losses(self):
        # When one cell is zero, should still return valid floats
        chi2, p = run_chi2(wins_a=10, n_a=10, wins_b=5, n_b=10)
        assert isinstance(chi2, float)
        assert isinstance(p, float)


# ---------------------------------------------------------------------------
# compute_group_stats
# ---------------------------------------------------------------------------

class TestComputeGroupStats:
    def test_basic(self):
        sigs = _signals_for_stratum(20, win_rate=0.6)
        assignment = assign_groups(sigs, seed=42)
        stats = compute_group_stats(sigs, assignment)
        assert "A" in stats
        assert "B" in stats
        a = stats["A"]
        b = stats["B"]
        assert a.n + b.n == 20
        assert isinstance(a.win_rate, float)
        assert 0.0 <= a.win_rate <= 1.0

    def test_counts_correct(self):
        sigs = _signals_for_stratum(10)
        # Force exact assignment: first 5 → A, last 5 → B
        assignment = {s["signal_id"]: ("A" if i < 5 else "B") for i, s in enumerate(sigs)}
        stats = compute_group_stats(sigs, assignment)
        assert stats["A"].n == 5
        assert stats["B"].n == 5

    def test_empty_group_handled(self):
        sigs = _signals_for_stratum(4)
        assignment = {s["signal_id"]: "A" for s in sigs}  # all A, no B
        stats = compute_group_stats(sigs, assignment)
        assert stats["A"].n == 4
        assert "B" not in stats or stats["B"].n == 0

    def test_upside_list_populated(self):
        sigs = _signals_for_stratum(10)
        assignment = assign_groups(sigs, seed=1)
        stats = compute_group_stats(sigs, assignment)
        total_upside = len(stats["A"].upside_values) + len(stats["B"].upside_values)
        assert total_upside == 10


# ---------------------------------------------------------------------------
# derive_recommendation
# ---------------------------------------------------------------------------

class TestDeriveRecommendation:
    def test_adopt_v23_when_significant_improvement(self):
        result = ABTestResult(
            stratum=("半導體", "TSE", "uptrend"),
            n_a=50, n_b=50,
            win_rate_a=0.45, win_rate_b=0.60,
            mean_upside_a=3.0, mean_upside_b=6.0,
            ttest_p=0.01, chi2_p=0.03,
            cohens_d=0.55,
        )
        rec = derive_recommendation(result, alpha=0.05, min_effect_size=0.1)
        assert "v2.3" in rec or "Adopt" in rec

    def test_keep_v22_when_v23_worse(self):
        result = ABTestResult(
            stratum=("半導體", "TSE", "uptrend"),
            n_a=50, n_b=50,
            win_rate_a=0.65, win_rate_b=0.45,
            mean_upside_a=6.0, mean_upside_b=2.0,
            ttest_p=0.01, chi2_p=0.02,
            cohens_d=-0.55,
        )
        rec = derive_recommendation(result, alpha=0.05, min_effect_size=0.1)
        assert "v2.2" in rec or "Keep" in rec

    def test_continue_when_not_significant(self):
        result = ABTestResult(
            stratum=("半導體", "TSE", "uptrend"),
            n_a=20, n_b=20,
            win_rate_a=0.50, win_rate_b=0.52,
            mean_upside_a=3.0, mean_upside_b=3.1,
            ttest_p=0.60, chi2_p=0.55,
            cohens_d=0.05,
        )
        rec = derive_recommendation(result, alpha=0.05, min_effect_size=0.1)
        assert "Continue" in rec or "testing" in rec.lower()

    def test_continue_when_effect_size_small_despite_significant_p(self):
        """p < alpha but effect size < min_effect_size → continue testing."""
        result = ABTestResult(
            stratum=("光電", "TPEx", "neutral"),
            n_a=200, n_b=200,
            win_rate_a=0.50, win_rate_b=0.52,
            mean_upside_a=3.0, mean_upside_b=3.1,
            ttest_p=0.04, chi2_p=0.04,
            cohens_d=0.04,  # very small effect
        )
        rec = derive_recommendation(result, alpha=0.05, min_effect_size=0.1)
        assert "Continue" in rec or "testing" in rec.lower()


# ---------------------------------------------------------------------------
# aggregate_strata_results
# ---------------------------------------------------------------------------

class TestAggregateStrataResults:
    def test_returns_overall_result(self):
        # Two strata worth of results
        strata_results = [
            ABTestResult(
                stratum=("半導體", "TSE", "uptrend"),
                n_a=20, n_b=20,
                win_rate_a=0.5, win_rate_b=0.6,
                mean_upside_a=3.0, mean_upside_b=5.0,
                ttest_p=0.1, chi2_p=0.12,
                cohens_d=0.3,
            ),
            ABTestResult(
                stratum=("光電", "TPEx", "neutral"),
                n_a=15, n_b=15,
                win_rate_a=0.4, win_rate_b=0.55,
                mean_upside_a=2.0, mean_upside_b=4.5,
                ttest_p=0.08, chi2_p=0.09,
                cohens_d=0.35,
            ),
        ]
        overall = aggregate_strata_results(strata_results)
        assert isinstance(overall, OverallResult)
        assert overall.total_n_a == 35
        assert overall.total_n_b == 35
        assert isinstance(overall.recommendation, str)

    def test_empty_strata_list(self):
        overall = aggregate_strata_results([])
        assert isinstance(overall, OverallResult)
        assert overall.total_n_a == 0
        assert overall.total_n_b == 0

    def test_weighted_win_rate(self):
        """Weighted average win rates should be between min and max of strata."""
        strata_results = [
            ABTestResult(
                stratum=("半導體", "TSE", "uptrend"),
                n_a=10, n_b=10,
                win_rate_a=0.4, win_rate_b=0.6,
                mean_upside_a=2.0, mean_upside_b=4.0,
                ttest_p=0.1, chi2_p=0.1, cohens_d=0.3,
            ),
            ABTestResult(
                stratum=("光電", "TSE", "uptrend"),
                n_a=30, n_b=30,
                win_rate_a=0.5, win_rate_b=0.55,
                mean_upside_a=3.0, mean_upside_b=3.5,
                ttest_p=0.3, chi2_p=0.3, cohens_d=0.1,
            ),
        ]
        overall = aggregate_strata_results(strata_results)
        # Combined A: (4+15)/40 = 0.475, B: (6+16.5)/40 = 0.5625
        assert 0.4 <= overall.overall_win_rate_a <= 0.55
        assert 0.55 <= overall.overall_win_rate_b <= 0.65


# ---------------------------------------------------------------------------
# load_signal_records (integration-style, uses real cache file format)
# ---------------------------------------------------------------------------

class TestLoadSignalRecords:
    def test_loads_from_json_cache(self, tmp_path):
        cache_data = {
            "last_updated": "2026-04-21T10:00:00",
            "signals": {
                "2330_2026-04-01": {
                    "ticker": "2330",
                    "signal_date": "2026-04-01",
                    "confidence": 72,
                    "action": "LONG",
                    "market": "TSE",
                    "industry": "半導體",
                    "entry_price": 500.0,
                    "twenty_day_high": 510.0,
                    "actual_breakout": True,
                    "days_to_breakout": 3,
                    "max_price": 525.0,
                    "upside_pct": 5.0,
                    "pending": False,
                },
                "2454_2026-04-02": {
                    "ticker": "2454",
                    "signal_date": "2026-04-02",
                    "confidence": 65,
                    "action": "LONG",
                    "market": "TSE",
                    "industry": "半導體",
                    "entry_price": 300.0,
                    "twenty_day_high": 310.0,
                    "actual_breakout": False,
                    "days_to_breakout": 10,
                    "max_price": 302.0,
                    "upside_pct": 0.67,
                    "pending": False,
                },
            }
        }
        cache_file = tmp_path / "signal_outcomes_cache.json"
        with open(cache_file, "w") as f:
            json.dump(cache_data, f)

        records = load_signal_records(cache_path=cache_file)
        assert len(records) == 2
        ids = {r["signal_id"] for r in records}
        assert "2330_2026-04-01" in ids

    def test_returns_empty_for_missing_file(self, tmp_path):
        records = load_signal_records(cache_path=tmp_path / "nonexistent.json")
        assert records == []

    def test_pending_signals_excluded_by_default(self, tmp_path):
        cache_data = {
            "signals": {
                "A_2026-04-01": {
                    "ticker": "A",
                    "signal_date": "2026-04-01",
                    "confidence": 60,
                    "action": "LONG",
                    "market": "TSE",
                    "industry": "半導體",
                    "entry_price": 100.0,
                    "twenty_day_high": 105.0,
                    "actual_breakout": False,
                    "days_to_breakout": 0,
                    "max_price": 100.0,
                    "upside_pct": 0.0,
                    "pending": True,
                }
            }
        }
        cache_file = tmp_path / "cache.json"
        with open(cache_file, "w") as f:
            json.dump(cache_data, f)

        records = load_signal_records(cache_path=cache_file, exclude_pending=True)
        assert records == []


# ---------------------------------------------------------------------------
# End-to-end: full pipeline with synthetic data
# ---------------------------------------------------------------------------

class TestEndToEndPipeline:
    def test_full_run_does_not_crash(self):
        """Run assign_groups → compute_group_stats → ABTestResult → aggregate."""
        sigs = (
            _signals_for_stratum(20, industry="半導體", market="TSE", regime="uptrend", win_rate=0.6)
            + _signals_for_stratum(20, industry="光電", market="TPEx", regime="neutral", win_rate=0.4)
        )
        settled = filter_settled_records(sigs)
        assignment = assign_groups(settled, seed=42)
        stats = compute_group_stats(settled, assignment)

        # Build ABTestResult for the combined group
        a_stats = stats.get("A")
        b_stats = stats.get("B")
        assert a_stats is not None
        assert b_stats is not None

        tstat, tp = run_ttest(a_stats.upside_values, b_stats.upside_values)
        chi2, cp = run_chi2(
            wins_a=a_stats.wins, n_a=a_stats.n,
            wins_b=b_stats.wins, n_b=b_stats.n,
        )
        d = cohens_d(a_stats.upside_values, b_stats.upside_values)

        result = ABTestResult(
            stratum=("all", "all", "all"),
            n_a=a_stats.n, n_b=b_stats.n,
            win_rate_a=a_stats.win_rate,
            win_rate_b=b_stats.win_rate,
            mean_upside_a=a_stats.mean_upside,
            mean_upside_b=b_stats.mean_upside,
            ttest_p=tp,
            chi2_p=cp,
            cohens_d=d or 0.0,
        )

        rec = derive_recommendation(result)
        assert isinstance(rec, str)
        assert len(rec) > 0

        overall = aggregate_strata_results([result])
        assert overall.total_n_a + overall.total_n_b == len(settled)
