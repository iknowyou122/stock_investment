"""Replay Triple Confirmation scoring from stored score_breakdown without re-running engine.

Used by factor_report.py grid search to evaluate parameter combinations on
historical breakdowns in milliseconds per signal.
"""
from __future__ import annotations

import json
from pathlib import Path

_PARAMS_PATH = Path(__file__).resolve().parents[3] / "config" / "engine_params.json"

DEFAULT_PARAMS: dict = {
    "gate_vol_ratio": 1.2,
    "rsi_momentum_lo": 55,
    "rsi_momentum_hi": 70,
    "breakout_vol_ratio": 1.5,
    "sector_topN_pct": 0.20,
    "long_threshold_uptrend": 63,
    "long_threshold_neutral": 68,
    "long_threshold_downtrend": 73,
    "watch_min": 45,
}

_RISK_FIELDS = frozenset({
    "daytrade_risk", "long_upper_shadow", "overheat_ma20",
    "overheat_ma60", "daytrade_heat", "sbl_breakout_fail", "margin_chase_heat",
})
_NON_SCORE_FIELDS = frozenset({"scoring_version"})


def load_params() -> dict:
    """Load tunable params from config/engine_params.json, falling back to defaults."""
    if _PARAMS_PATH.exists():
        with open(_PARAMS_PATH) as f:
            data = json.load(f)
        return {**DEFAULT_PARAMS, **{k: v for k, v in data.items() if not k.startswith("_")}}
    return dict(DEFAULT_PARAMS)


def _sum_pts(pts: dict) -> int:
    total = 0
    for k, v in pts.items():
        if k in _NON_SCORE_FIELDS:
            continue
        if k in _RISK_FIELDS:
            total -= int(v)
        else:
            total += int(v)
    return max(0, min(100, total))


def recompute_score(breakdown: dict, params: dict) -> tuple[int, str]:
    """Replay scoring with candidate params. Returns (score, action).

    Uses stored breakdown["raw"] raw values + breakdown["pts"] stored pts.
    Only re-evaluates pts for params in the whitelist; all other pts are unchanged.

    Args:
        breakdown: dict with keys "raw", "pts", "flags", "taiex_slope"
        params: parameter dict (use load_params() or DEFAULT_PARAMS as base)

    Returns:
        (score 0–100, action "LONG"|"WATCH"|"CAUTION")
    """
    pts = dict(breakdown.get("pts", {}))
    raw = breakdown.get("raw", {})

    # --- Re-evaluate RSI momentum pts ---
    rsi = raw.get("rsi_14")
    if rsi is not None:
        lo = params.get("rsi_momentum_lo", DEFAULT_PARAMS["rsi_momentum_lo"])
        hi = params.get("rsi_momentum_hi", DEFAULT_PARAMS["rsi_momentum_hi"])
        pts["rsi_momentum_pts"] = 4 if lo <= rsi <= hi else 0

    # --- Re-evaluate breakout volume pts ---
    vol_ratio = raw.get("volume_vs_20ma")
    if vol_ratio is not None:
        bv_thresh = params.get("breakout_vol_ratio", DEFAULT_PARAMS["breakout_vol_ratio"])
        had_breakout = pts.get("breakout_20d_pts", 0) > 0
        pts["breakout_volume_pts"] = (
            3 if (had_breakout and vol_ratio >= bv_thresh) else 0
        )

    # --- Re-evaluate gate_vol ---
    if vol_ratio is not None:
        gate_vol = params.get("gate_vol_ratio", DEFAULT_PARAMS["gate_vol_ratio"])
        flags = breakdown.get("flags", [])
        gate_vol_passed = any(f == "GATE_PASS:VOL" for f in flags)
        if gate_vol_passed and vol_ratio < gate_vol:
            return 0, "CAUTION"

    score = _sum_pts(pts)

    # --- Determine action ---
    taiex_slope = breakdown.get("taiex_slope", "neutral")
    if taiex_slope == "uptrend":
        threshold = params.get("long_threshold_uptrend", DEFAULT_PARAMS["long_threshold_uptrend"])
    elif taiex_slope == "downtrend":
        threshold = params.get("long_threshold_downtrend", DEFAULT_PARAMS["long_threshold_downtrend"])
    else:
        threshold = params.get("long_threshold_neutral", DEFAULT_PARAMS["long_threshold_neutral"])

    watch_min = params.get("watch_min", DEFAULT_PARAMS["watch_min"])

    if score >= threshold:
        action = "LONG"
    elif score >= watch_min:
        action = "WATCH"
    else:
        action = "CAUTION"

    return score, action
