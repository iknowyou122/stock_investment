"""Unit tests for SectorFlowAnalyzer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taiwan_stock_agent.domain.sector_flow import (
    SectorFlowAnalyzer,
    SectorFlowPoint,
    SectorFlowSeries,
    TREND_META,
    load_heat_snapshots,
    sparkline_svg,
)


def _heat(date: str, *industries: tuple[str, float, float, float, float]) -> dict:
    """Build a heat snapshot dict.

    Each industry tuple is (name, rank_pct, ret_5d_pct, breadth, top5_vol).
    """
    return {
        "snapshot_date": date,
        "market_state": "mixed",
        "industries": {
            name: {
                "rank_pct": rank,
                "ret_5d_pct": ret5d,
                "breadth_above_ma20_pct": breadth,
                "top5_vol_concentration": vol,
            }
            for (name, rank, ret5d, breadth, vol) in industries
        },
    }


# ── Loader ────────────────────────────────────────────────────────────────


class TestLoadHeatSnapshots:
    def test_loads_oldest_first(self, tmp_path: Path) -> None:
        (tmp_path / "heat_2026-06-01.json").write_text(json.dumps(_heat("2026-06-01")))
        (tmp_path / "heat_2026-06-02.json").write_text(json.dumps(_heat("2026-06-02")))
        (tmp_path / "heat_2026-06-03.json").write_text(json.dumps(_heat("2026-06-03")))
        snaps = load_heat_snapshots(days=10, heat_dir=tmp_path)
        assert [s["snapshot_date"] for s in snaps] == ["2026-06-01", "2026-06-02", "2026-06-03"]

    def test_caps_to_n_newest(self, tmp_path: Path) -> None:
        for d in ("01", "02", "03", "04", "05"):
            (tmp_path / f"heat_2026-06-{d}.json").write_text(json.dumps(_heat(f"2026-06-{d}")))
        snaps = load_heat_snapshots(days=3, heat_dir=tmp_path)
        assert [s["snapshot_date"] for s in snaps] == ["2026-06-03", "2026-06-04", "2026-06-05"]

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        snaps = load_heat_snapshots(days=10, heat_dir=tmp_path / "no_such_dir")
        assert snaps == []

    def test_corrupt_json_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "heat_2026-06-01.json").write_text("not json")
        (tmp_path / "heat_2026-06-02.json").write_text(json.dumps(_heat("2026-06-02")))
        snaps = load_heat_snapshots(days=10, heat_dir=tmp_path)
        assert len(snaps) == 1
        assert snaps[0]["snapshot_date"] == "2026-06-02"


# ── Analyzer ─────────────────────────────────────────────────────────────


class TestAnalyzerCore:
    def _make_snaps(self) -> list[dict]:
        # 半導體 ramps up; 塑膠 declines; 紡織 stable
        return [
            _heat("2026-06-01",
                  ("半導體業", 30.0, 1.0, 50.0, 60.0),
                  ("塑膠工業", 80.0, 5.0, 70.0, 65.0),
                  ("紡織纖維", 50.0, 2.0, 55.0, 60.0)),
            _heat("2026-06-02",
                  ("半導體業", 35.0, 1.5, 52.0, 60.0),
                  ("塑膠工業", 75.0, 4.0, 68.0, 65.0),
                  ("紡織纖維", 50.0, 2.0, 55.0, 60.0)),
            _heat("2026-06-03",
                  ("半導體業", 50.0, 3.0, 58.0, 62.0),
                  ("塑膠工業", 60.0, 2.0, 60.0, 65.0),
                  ("紡織纖維", 51.0, 2.0, 55.0, 60.0)),
            _heat("2026-06-04",
                  ("半導體業", 70.0, 5.0, 65.0, 68.0),
                  ("塑膠工業", 45.0, 0.0, 55.0, 65.0),
                  ("紡織纖維", 50.0, 2.0, 55.0, 60.0)),
            _heat("2026-06-05",
                  ("半導體業", 85.0, 7.0, 72.0, 70.0),
                  ("塑膠工業", 35.0, -2.0, 50.0, 65.0),
                  ("紡織纖維", 51.0, 2.0, 55.0, 60.0)),
            _heat("2026-06-06",
                  ("半導體業", 90.0, 9.0, 78.0, 72.0),
                  ("塑膠工業", 25.0, -4.0, 45.0, 65.0),
                  ("紡織纖維", 50.0, 2.0, 55.0, 60.0)),
        ]

    def test_analyze_returns_series_for_each_industry(self) -> None:
        snaps = self._make_snaps()
        summary = SectorFlowAnalyzer().analyze(snapshots=snaps)
        names = {s.industry for s in summary.series}
        assert names == {"半導體業", "塑膠工業", "紡織纖維"}
        assert summary.newest_date() == "2026-06-06"

    def test_rising_industry_marked_rising_fast(self) -> None:
        snaps = self._make_snaps()
        summary = SectorFlowAnalyzer().analyze(snapshots=snaps)
        semi = next(s for s in summary.series if s.industry == "半導體業")
        assert semi.rank_delta_total > 50
        assert semi.acceleration_3v3 > 10
        assert semi.trend_direction == "RISING_FAST"

    def test_declining_industry_marked_declining_fast(self) -> None:
        snaps = self._make_snaps()
        summary = SectorFlowAnalyzer().analyze(snapshots=snaps)
        plastic = next(s for s in summary.series if s.industry == "塑膠工業")
        assert plastic.rank_delta_total < -50
        assert plastic.trend_direction == "DECLINING_FAST"

    def test_stable_industry_marked_stable(self) -> None:
        snaps = self._make_snaps()
        summary = SectorFlowAnalyzer().analyze(snapshots=snaps)
        textile = next(s for s in summary.series if s.industry == "紡織纖維")
        assert abs(textile.rank_delta_total) <= 1
        assert textile.trend_direction == "STABLE"

    def test_by_acceleration_sorts_warming_first(self) -> None:
        snaps = self._make_snaps()
        summary = SectorFlowAnalyzer().analyze(snapshots=snaps)
        ordered = summary.by_acceleration()
        assert ordered[0].industry == "半導體業"
        assert ordered[-1].industry == "塑膠工業"

    def test_industry_missing_in_some_snapshots_gets_zero_points(self) -> None:
        # 'AI晶片' only in last 2 snapshots
        snaps = [
            _heat("2026-06-01", ("半導體業", 30.0, 1.0, 50.0, 60.0)),
            _heat("2026-06-02", ("半導體業", 35.0, 1.5, 52.0, 60.0)),
            _heat("2026-06-03",
                  ("半導體業", 50.0, 3.0, 58.0, 62.0),
                  ("AI晶片", 70.0, 4.0, 60.0, 65.0)),
        ]
        summary = SectorFlowAnalyzer().analyze(snapshots=snaps)
        ai = next(s for s in summary.series if s.industry == "AI晶片")
        assert len(ai.points) == 3
        # First two points have rank_pct 0 because industry was missing
        assert ai.points[0].rank_pct == 0.0
        assert ai.points[1].rank_pct == 0.0
        assert ai.points[2].rank_pct == 70.0

    def test_empty_snapshots_returns_empty_summary(self) -> None:
        summary = SectorFlowAnalyzer().analyze(snapshots=[])
        assert summary.series == ()
        assert summary.snapshot_dates == ()

    def test_market_states_propagated(self) -> None:
        snaps = [
            {"snapshot_date": "2026-06-01", "market_state": "broad_rally", "industries": {}},
            {"snapshot_date": "2026-06-02", "market_state": "risk_off", "industries": {}},
        ]
        summary = SectorFlowAnalyzer().analyze(snapshots=snaps)
        assert summary.market_states == ("broad_rally", "risk_off")


class TestSeriesProperties:
    def test_single_point_acceleration_zero(self) -> None:
        s = SectorFlowSeries(
            industry="X",
            points=(SectorFlowPoint("2026-06-01", 50.0, 1.0, 50.0, 60.0),),
        )
        assert s.acceleration_3v3 == 0.0
        assert s.rank_delta_total == 0.0
        assert s.trend_direction == "STABLE"

    def test_rank_pct_series_returns_floats(self) -> None:
        s = SectorFlowSeries(
            industry="X",
            points=tuple(
                SectorFlowPoint(f"d{i}", float(v), 0.0, 0.0, 0.0)
                for i, v in enumerate([10, 20, 30, 40, 50])
            ),
        )
        assert s.rank_pct_series == [10.0, 20.0, 30.0, 40.0, 50.0]

    def test_latest_and_oldest_resolve(self) -> None:
        pts = tuple(
            SectorFlowPoint(f"d{i}", float(i * 10), 0.0, 0.0, 0.0)
            for i in range(5)
        )
        s = SectorFlowSeries(industry="X", points=pts)
        assert s.oldest.rank_pct == 0.0
        assert s.latest.rank_pct == 40.0


# ── HTML helpers ─────────────────────────────────────────────────────────


class TestSparklineSvg:
    def test_returns_svg_string(self) -> None:
        out = sparkline_svg([1, 2, 3, 4, 5])
        assert out.startswith("<svg")
        assert "</svg>" in out
        assert 'stroke="#58a6ff"' in out  # default

    def test_custom_stroke_color(self) -> None:
        out = sparkline_svg([1, 2, 3], stroke="#3fb950")
        assert "#3fb950" in out

    def test_single_value_no_path(self) -> None:
        out = sparkline_svg([5.0])
        assert "<path" not in out  # no path drawn

    def test_flat_values_does_not_crash(self) -> None:
        # All identical values used to cause div-by-zero
        out = sparkline_svg([10, 10, 10, 10])
        assert out.startswith("<svg")

    def test_includes_fill_below_by_default(self) -> None:
        out = sparkline_svg([1, 2, 3, 4])
        assert "fill-opacity" in out


class TestConceptFlowAnalyzer:
    """Mirror tests for the concept-basket variant."""

    def _concept_snap(self, date: str, *baskets):
        return {
            "snapshot_date": date,
            "market_state": "mixed",
            "concepts": {
                key: {
                    "rank_pct": rank,
                    "ret_5d_pct": ret5d,
                    "breadth_above_ma20_pct": breadth,
                    "top5_vol_concentration": vol,
                }
                for (key, rank, ret5d, breadth, vol) in baskets
            },
        }

    def test_reads_concepts_subkey_not_industries(self, tmp_path: Path) -> None:
        from taiwan_stock_agent.domain.sector_flow import ConceptFlowAnalyzer
        (tmp_path / "concept_heat_2026-06-01.json").write_text(
            json.dumps(self._concept_snap("2026-06-01",
                ("CPO", 50.0, 1.0, 50.0, 60.0),
                ("HBM", 30.0, -1.0, 40.0, 60.0))),
            encoding="utf-8")
        (tmp_path / "concept_heat_2026-06-02.json").write_text(
            json.dumps(self._concept_snap("2026-06-02",
                ("CPO", 70.0, 3.0, 60.0, 65.0),
                ("HBM", 20.0, -3.0, 30.0, 60.0))),
            encoding="utf-8")
        analyzer = ConceptFlowAnalyzer(heat_dir=tmp_path)
        summary = analyzer.analyze(days=10)
        labels = {s.industry for s in summary.series}
        assert labels == {"CPO", "HBM"}

    def test_concepts_meta_replaces_label_with_name_zh(self, tmp_path: Path) -> None:
        from taiwan_stock_agent.domain.sector_flow import ConceptFlowAnalyzer
        (tmp_path / "concept_heat_2026-06-01.json").write_text(
            json.dumps(self._concept_snap("2026-06-01",
                ("CPO_silicon_photonics", 50.0, 1.0, 50.0, 60.0))),
            encoding="utf-8")
        analyzer = ConceptFlowAnalyzer(heat_dir=tmp_path)
        summary = analyzer.analyze(
            days=10,
            concepts_meta={
                "CPO_silicon_photonics": {"name_zh": "CPO / 矽光子"},
            },
        )
        labels = {s.industry for s in summary.series}
        assert labels == {"CPO / 矽光子"}

    def test_empty_concept_dir_returns_empty_summary(self, tmp_path: Path) -> None:
        from taiwan_stock_agent.domain.sector_flow import ConceptFlowAnalyzer
        summary = ConceptFlowAnalyzer(heat_dir=tmp_path).analyze(days=10)
        assert summary.series == ()

    def test_trend_classification_works_on_concepts(self, tmp_path: Path) -> None:
        from taiwan_stock_agent.domain.sector_flow import ConceptFlowAnalyzer
        # MLCC ramps; memory crashes
        for i, (mlcc, memo) in enumerate([(30, 80), (35, 70), (50, 55), (75, 40), (90, 20), (98, 10)]):
            (tmp_path / f"concept_heat_2026-06-0{i+1}.json").write_text(
                json.dumps(self._concept_snap(f"2026-06-0{i+1}",
                    ("MLCC", float(mlcc), 1.0, 50.0, 60.0),
                    ("memory_general", float(memo), -1.0, 40.0, 60.0))),
                encoding="utf-8")
        analyzer = ConceptFlowAnalyzer(heat_dir=tmp_path)
        summary = analyzer.analyze(days=10)
        mlcc_s = next(s for s in summary.series if s.industry == "MLCC")
        memo_s = next(s for s in summary.series if s.industry == "memory_general")
        assert mlcc_s.trend_direction == "RISING_FAST"
        assert memo_s.trend_direction == "DECLINING_FAST"


class TestTrendMeta:
    def test_has_all_five_directions(self) -> None:
        assert set(TREND_META.keys()) == {
            "RISING_FAST", "RISING", "STABLE", "DECLINING", "DECLINING_FAST"
        }

    def test_each_entry_is_3_tuple(self) -> None:
        for k, v in TREND_META.items():
            assert len(v) == 3, k
            assert all(isinstance(x, str) for x in v)
