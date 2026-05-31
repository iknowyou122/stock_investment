"""Tests for IMS fields in _scan_one result dict and _compute_ims/_ims_bar helpers."""
import pytest


def _make_result(**kwargs) -> dict:
    """Minimal valid result dict for IMS tests."""
    base = {
        "ticker": "2330",
        "action": "LONG",
        "confidence": 70.0,
        "halt": False,
        "error": None,
        "flags": [],
        "entry_bid": 850.0,
        "stop_loss": 810.0,
        "target": 980.0,
        "signal_type": "蓄積",
        # IMS fields
        "stealth_accum_composite_pts": 0.0,
        "inst_synergy_pts": 0.0,
        "foreign_trend_pts": 0.0,
        "vol_asymmetry_pts": 0.0,
        "chip_cleanliness_pts": 0.0,
        "large_2w_trend_pts": 0.0,
        "inst_accel_3d_pts": 0.0,
        "obv_stealth_pts": 0.0,
    }
    base.update(kwargs)
    return base


def test_ims_all_zero_when_no_signals():
    from scripts.batch_plan import _compute_ims
    r = _make_result()
    assert _compute_ims(r) == pytest.approx(0.0)


def test_ims_weights_computed_correctly():
    from scripts.batch_plan import _compute_ims
    r = _make_result(
        stealth_accum_composite_pts=10.0,   # ×2.5 = 25.0
        inst_synergy_pts=5.0,               # ×2.0 = 10.0
        foreign_trend_pts=4.0,              # ×1.5 = 6.0
        large_2w_trend_pts=3.0,             # ×1.5 = 4.5
        chip_cleanliness_pts=7.0,           # ×1.0 = 7.0
        inst_accel_3d_pts=2.0,              # ×1.0 = 2.0
        vol_asymmetry_pts=4.0,              # ×1.0 = 4.0
        obv_stealth_pts=3.0,                # ×1.0 = 3.0
    )
    expected = 25.0 + 10.0 + 6.0 + 4.5 + 7.0 + 2.0 + 4.0 + 3.0  # 61.5
    assert _compute_ims(r) == pytest.approx(expected)


def test_ims_early_accum_bonus_applied():
    """Early accumulation signal types get +5 bonus."""
    from scripts.batch_plan import _compute_ims
    for sig in ("法人建倉", "籌碼轉移", "VCP", "旗形"):
        r = _make_result(signal_type=sig)
        assert _compute_ims(r) == pytest.approx(5.0), f"Expected +5 bonus for {sig}"


def test_ims_no_bonus_for_regular_signals():
    from scripts.batch_plan import _compute_ims
    for sig in ("蓄積", "蓄積★", "趨勢延伸", "回調", "爆量"):
        r = _make_result(signal_type=sig)
        assert _compute_ims(r) == pytest.approx(0.0), f"No bonus expected for {sig}"


def test_ims_missing_fields_default_zero():
    """_compute_ims must not crash when IMS fields are absent (legacy results)."""
    from scripts.batch_plan import _compute_ims
    r = {"ticker": "2330", "signal_type": "蓄積"}
    assert _compute_ims(r) == pytest.approx(0.0)


def test_ims_bar_returns_string():
    from scripts.batch_plan import _ims_bar
    result = _ims_bar(0.0)
    assert isinstance(result, str)
    result_high = _ims_bar(40.0)
    assert isinstance(result_high, str)


def test_ims_bar_low_score_dim():
    from scripts.batch_plan import _ims_bar
    bar = _ims_bar(5.0)
    assert "dim" in bar


def test_ims_bar_high_score_bright_magenta():
    from scripts.batch_plan import _ims_bar
    bar = _ims_bar(35.0)
    assert "bright_magenta" in bar
