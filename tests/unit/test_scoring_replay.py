from __future__ import annotations
from taiwan_stock_agent.domain.scoring_replay import recompute_score, load_params, DEFAULT_PARAMS


def _base_breakdown(rsi: float = 62.0, vol_ratio: float = 1.8, breakout: bool = True) -> dict:
    """Minimal breakdown dict for testing."""
    return {
        "raw": {
            "rsi_14": rsi,
            "volume_vs_20ma": vol_ratio,
            "ma20_slope_pct": 0.3,
        },
        "pts": {
            "volume_ratio_pts": 8,
            "price_direction_pts": 3,
            "close_strength_pts": 4,
            "vwap_advantage_pts": 6,
            "trend_continuity_pts": 5,
            "volume_escalation_pts": 5,
            "rsi_momentum_pts": 4,        # RSI 62 in [55,70] → 4
            "foreign_strength_pts": 5,
            "trust_strength_pts": 4,
            "dealer_strength_pts": 4,
            "institution_continuity_pts": 4,
            "institution_consensus_pts": 2,
            "margin_structure_pts": 4,
            "margin_utilization_pts": 4,
            "sbl_pressure_pts": 0,
            "breadth_pts": 0,
            "concentration_pts": 0,
            "continuity_pts": 0,
            "daytrade_filter_pts": 0,
            "foreign_broker_pts": 0,
            "breakout_20d_pts": 8 if breakout else 0,
            "breakout_60d_pts": 5,
            "breakout_quality_pts": 2,
            "breakout_volume_pts": 3,    # breakout + vol 1.8 >= 1.5 → 3
            "ma_alignment_pts": 2,
            "ma20_slope_pts": 5,
            "relative_strength_pts": 0,
            "upside_space_pts": 5,
            "daytrade_risk": 0,
            "long_upper_shadow": 0,
            "overheat_ma20": 0,
            "overheat_ma60": 0,
            "daytrade_heat": 0,
            "sbl_breakout_fail": 0,
            "margin_chase_heat": 0,
            "scoring_version": "v2",
        },
        "flags": ["BREAKOUT_WITH_VOL"],
        "taiex_slope": "neutral",
        "scoring_version": "v2",
    }


def test_recompute_score_unchanged_params_gives_same_score():
    bd = _base_breakdown()
    score, action = recompute_score(bd, DEFAULT_PARAMS)
    # Sum all pts manually
    pts = bd["pts"]
    risk_fields = {"daytrade_risk","long_upper_shadow","overheat_ma20",
                   "overheat_ma60","daytrade_heat","sbl_breakout_fail","margin_chase_heat"}
    non_score = {"scoring_version"}
    expected = sum(v for k, v in pts.items()
                   if k not in risk_fields and k not in non_score
                  ) - sum(pts.get(k, 0) for k in risk_fields)
    expected = max(0, min(100, expected))
    assert score == expected


def test_recompute_score_rsi_zone_widened():
    """Widening RSI zone to [50,72] captures RSI=52 that was previously excluded."""
    bd = _base_breakdown(rsi=52.0)
    bd["pts"]["rsi_momentum_pts"] = 0   # RSI 52 < 55 → 0 with defaults

    params_wider = {**DEFAULT_PARAMS, "rsi_momentum_lo": 50, "rsi_momentum_hi": 72}
    score_wider, _ = recompute_score(bd, params_wider)

    score_default, _ = recompute_score(bd, DEFAULT_PARAMS)
    assert score_wider == score_default + 4   # rsi_momentum_pts gained 4


def test_recompute_score_breakout_vol_raised():
    """Raising breakout_vol_ratio to 2.0 removes pts when actual ratio is 1.7."""
    bd = _base_breakdown(vol_ratio=1.7, breakout=True)
    # Default: 1.7 >= 1.5 → breakout_volume_pts = 3
    params_strict = {**DEFAULT_PARAMS, "breakout_vol_ratio": 2.0}
    score_strict, _ = recompute_score(bd, params_strict)
    score_default, _ = recompute_score(bd, DEFAULT_PARAMS)
    assert score_strict == score_default - 3


def test_recompute_score_threshold_change_affects_action():
    """Lowering long threshold should allow lower scores to get LONG."""
    bd = _base_breakdown()
    # Force a moderate score by zeroing some pts
    bd["pts"]["vwap_advantage_pts"] = 0
    bd["pts"]["ma20_slope_pts"] = 0
    bd["pts"]["upside_space_pts"] = 0
    bd["pts"]["breakout_60d_pts"] = 0
    bd["pts"]["breakout_quality_pts"] = 0
    # With neutral threshold=68, this might be WATCH; with threshold=60, more likely LONG
    score, _ = recompute_score(bd, DEFAULT_PARAMS)
    params_lower = {**DEFAULT_PARAMS, "long_threshold_neutral": 60}
    score_lower, action_lower = recompute_score(bd, params_lower)
    assert score == score_lower  # score doesn't change, just threshold
    if score >= 60:
        assert action_lower == "LONG"


def test_load_params_returns_dict():
    params = load_params()
    assert "gate_vol_ratio" in params
    assert "rsi_momentum_lo" in params
    assert isinstance(params["long_threshold_neutral"], (int, float))
