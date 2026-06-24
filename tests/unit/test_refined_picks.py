"""Unit tests for RefinedPickFilter — distilling 300+ scan results to top 25."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from taiwan_stock_agent.domain.refined_picks import RefinedPick, RefinedPickFilter


# ── Helpers ─────────────────────────────────────────────────────────────────


def _scan_result(
    ticker: str = "2330",
    action: str = "LONG",
    confidence: float = 90.0,
    entry: float = 100.0,
    halt: bool = False,
    error=None,
    signal: object | None = MagicMock(),   # default: has _signal (passes Pullback gate)
    flags: list | None = None,
) -> dict:
    return {
        "ticker": ticker,
        "action": action,
        "confidence": confidence,
        "entry_bid": entry,
        "halt": halt,
        "error": error,
        "_signal": signal,
        "flags": flags or [],
    }


def _rotation(hot=None, emerging=None, cooling=None) -> dict:
    return {
        "hot_nodes": [{"label": s, "state": "HOT"} for s in (hot or [])],
        "emerging_nodes": [{"label": s, "state": "EMERGING"} for s in (emerging or [])],
        "cooling_nodes": [{"label": s, "state": "COOLING"} for s in (cooling or [])],
    }


# ── Filter rules ────────────────────────────────────────────────────────────


class TestFilterRules:
    def test_rejects_low_confidence(self) -> None:
        results = [_scan_result(ticker="A", confidence=80.0)]
        picks = RefinedPickFilter().refine(results)
        assert picks == []

    def test_accepts_at_floor(self) -> None:
        results = [_scan_result(ticker="A", confidence=85.0)]
        picks = RefinedPickFilter().refine(results)
        assert len(picks) == 1

    def test_rejects_caution_action(self) -> None:
        results = [_scan_result(ticker="A", action="CAUTION", confidence=120.0)]
        picks = RefinedPickFilter().refine(results)
        assert picks == []

    def test_accepts_watch(self) -> None:
        results = [_scan_result(ticker="A", action="WATCH", confidence=95.0)]
        picks = RefinedPickFilter().refine(results)
        assert len(picks) == 1

    def test_rejects_halted(self) -> None:
        results = [_scan_result(ticker="A", confidence=95.0, halt=True)]
        picks = RefinedPickFilter().refine(results)
        assert picks == []

    def test_rejects_error(self) -> None:
        results = [_scan_result(ticker="A", confidence=95.0, error="boom")]
        picks = RefinedPickFilter().refine(results)
        assert picks == []

    def test_rejects_signal_none_pullback_bug(self) -> None:
        """The infamous Pullback bug: TCE conf=0 but a Detector promoted it.

        We require `_signal` to be a real TCE SignalOutput, not None.
        """
        results = [_scan_result(ticker="A", confidence=98.0, signal=None)]
        picks = RefinedPickFilter().refine(results)
        assert picks == []

    def test_accepts_with_real_signal(self) -> None:
        results = [_scan_result(ticker="A", confidence=90.0, signal=MagicMock())]
        picks = RefinedPickFilter().refine(results)
        assert len(picks) == 1


# ── Composite score ────────────────────────────────────────────────────────


class TestCompositeScore:
    def test_bb_compressed_gets_bonus(self) -> None:
        compressed = _scan_result(ticker="A", confidence=90.0,
                                   flags=["GATE_PASS:G2_BB_PCT:20.0p"])
        plain = _scan_result(ticker="B", confidence=90.0)
        picks = RefinedPickFilter().refine([compressed, plain])
        assert picks[0].ticker == "A"
        assert picks[0].composite_score > picks[1].composite_score
        assert picks[0].has_bb_compression

    def test_bb_primed_below_15_gets_larger_bonus(self) -> None:
        primed = _scan_result(ticker="A", confidence=90.0,
                              flags=["GATE_PASS:G2_BB_PCT:10.0p"])
        compressed = _scan_result(ticker="B", confidence=90.0,
                                  flags=["GATE_PASS:G2_BB_PCT:30.0p"])
        picks = RefinedPickFilter().refine([primed, compressed])
        assert picks[0].ticker == "A"
        # delta should be at least (8-5) = 3 (PRIMED vs COMPRESSED bonus)
        assert picks[0].composite_score - picks[1].composite_score >= 3

    def test_hot_industry_gets_bonus_over_neutral(self) -> None:
        results = [
            _scan_result(ticker="A", confidence=90.0),  # HOT 半導體
            _scan_result(ticker="B", confidence=90.0),  # NEUTRAL
        ]
        picks = RefinedPickFilter().refine(
            results,
            industry_map={"A": "半導體業", "B": "玻璃陶瓷"},
            rotation_signal=_rotation(hot=["半導體業"]),
        )
        assert picks[0].ticker == "A"
        assert picks[0].industry_state == "HOT"

    def test_cooling_industry_penalised(self) -> None:
        results = [
            _scan_result(ticker="A", confidence=90.0),
            _scan_result(ticker="B", confidence=90.0),
        ]
        picks = RefinedPickFilter().refine(
            results,
            industry_map={"A": "半導體業", "B": "電子零組件業"},
            rotation_signal=_rotation(cooling=["電子零組件業"]),
        )
        assert picks[0].ticker == "A"
        # B got -3 cooling penalty
        assert picks[1].composite_score < picks[0].composite_score

    def test_concept_tailwind_bonus(self) -> None:
        results = [_scan_result(ticker="A", confidence=90.0)]
        picks = RefinedPickFilter().refine(
            results,
            concept_membership={"A": ["AI_GPU"]},
            hot_concepts={"AI_GPU"},
        )
        assert picks[0].concept_tailwind is True
        # bonus +3 vs baseline 90 → score >= 93
        assert picks[0].composite_score >= 93

    def test_held_ticker_marked_and_bonused(self) -> None:
        results = [_scan_result(ticker="A", confidence=90.0)]
        picks = RefinedPickFilter().refine(
            results,
            held_tickers={"A"},
        )
        assert picks[0].is_held is True


# ── Output shape & limits ──────────────────────────────────────────────────


class TestOutputShape:
    def test_top_n_caps_output(self) -> None:
        results = [_scan_result(ticker=f"{i:04d}", confidence=100.0 - i)
                   for i in range(50)]
        picks = RefinedPickFilter().refine(results, top_n=10)
        assert len(picks) == 10
        # Sorted by confidence desc
        assert picks[0].ticker == "0000"

    def test_empty_input_returns_empty(self) -> None:
        assert RefinedPickFilter().refine([]) == []

    def test_picks_are_frozen_dataclass(self) -> None:
        results = [_scan_result(ticker="A", confidence=90.0)]
        picks = RefinedPickFilter().refine(results)
        with pytest.raises(Exception):
            picks[0].confidence = 999  # type: ignore[misc]

    def test_pick_carries_raw_result_reference(self) -> None:
        raw = _scan_result(ticker="A", confidence=90.0)
        picks = RefinedPickFilter().refine([raw])
        assert picks[0].raw_result is raw

    def test_name_from_name_map_used(self) -> None:
        results = [_scan_result(ticker="2330", confidence=90.0)]
        picks = RefinedPickFilter().refine(results, name_map={"2330": "台積電"})
        assert picks[0].name == "台積電"

    def test_name_falls_back_to_ticker_when_missing(self) -> None:
        results = [_scan_result(ticker="9999", confidence=90.0)]
        picks = RefinedPickFilter().refine(results)
        assert picks[0].name == "9999"

    def test_min_confidence_override_used_when_provided(self) -> None:
        results = [_scan_result(ticker="A", confidence=70.0)]
        picks = RefinedPickFilter().refine(results, min_confidence=60.0)
        assert len(picks) == 1


# ── Realistic end-to-end ────────────────────────────────────────────────────


class TestEndToEnd:
    def test_300_signal_dataset_distilled_to_25(self) -> None:
        # 300 noise (low conf), 30 quality (conf >= 90)
        noisy = [_scan_result(ticker=f"N{i:03d}", confidence=70.0)
                 for i in range(300)]
        quality = [_scan_result(ticker=f"Q{i:02d}", confidence=90.0 + i)
                   for i in range(30)]
        picks = RefinedPickFilter().refine(noisy + quality, top_n=25)
        assert len(picks) == 25
        # All picks must be from quality batch (Q-prefix)
        assert all(p.ticker.startswith("Q") for p in picks)

    def test_ranking_combines_confidence_and_tailwind(self) -> None:
        """A 90-conf stock with HOT industry should beat a 95-conf NEUTRAL."""
        results = [
            _scan_result(ticker="HOT_STOCK", confidence=90.0),
            _scan_result(ticker="NEUTRAL_STOCK", confidence=95.0),
        ]
        picks = RefinedPickFilter().refine(
            results,
            industry_map={"HOT_STOCK": "半導體業", "NEUTRAL_STOCK": "其他"},
            rotation_signal=_rotation(hot=["半導體業"]),
        )
        # HOT bonus = 8, beats 5pt confidence gap
        assert picks[0].ticker == "HOT_STOCK"
