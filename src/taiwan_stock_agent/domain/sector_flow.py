"""Sector Capital Flow Analyzer.

Reads the last N daily `heat_*.json` snapshots and turns them into per-industry
time series suitable for sparkline visualisation and trend ranking.

Snapshot schema (per file):
    {
      "snapshot_date": "YYYY-MM-DD",
      "industries": {
        "<industry_name>": {
          "rank_pct": float,            # 0-100 (higher = stronger)
          "ret_1d_pct": float,
          "ret_5d_pct": float,
          "ret_20d_pct": float,
          "breadth_above_ma20_pct": float,
          "top5_vol_concentration": float,
          "leaders": [ticker, ...],
          ...
        },
        ...
      },
      "market_state": "broad_rally" | "mixed" | "risk_off" | ...
    }

This module is intentionally pure / no IO outside `load_heat_snapshots`, so
it is straightforward to unit-test with fabricated snapshots in tmp_path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SectorFlowPoint:
    """One day in an industry's history."""

    date: str
    rank_pct: float
    ret_5d_pct: float
    breadth_above_ma20_pct: float
    top5_vol_concentration: float


@dataclass(frozen=True)
class SectorFlowSeries:
    """Time series of one industry across N daily snapshots.

    All metric attributes default to 0.0 / empty when the snapshot did not
    contain the industry — this keeps downstream code branch-free.
    """

    industry: str
    points: tuple[SectorFlowPoint, ...]  # oldest → newest

    @property
    def latest(self) -> SectorFlowPoint | None:
        return self.points[-1] if self.points else None

    @property
    def oldest(self) -> SectorFlowPoint | None:
        return self.points[0] if self.points else None

    @property
    def rank_pct_series(self) -> list[float]:
        return [p.rank_pct for p in self.points]

    @property
    def rank_delta_total(self) -> float:
        """Today's rank_pct minus oldest snapshot rank_pct."""
        if not self.points or len(self.points) < 2:
            return 0.0
        return self.points[-1].rank_pct - self.points[0].rank_pct

    @property
    def acceleration_3v3(self) -> float:
        """Average of last 3 days' rank_pct minus average of prior 3 days.

        Positive → momentum accelerating; negative → cooling.
        Falls back to using whatever we have when the series is short.
        """
        n = len(self.points)
        if n < 2:
            return 0.0
        window = min(3, n // 2)
        recent = [p.rank_pct for p in self.points[-window:]]
        prior = [p.rank_pct for p in self.points[-2 * window: -window]]
        if not recent or not prior:
            return 0.0
        return sum(recent) / len(recent) - sum(prior) / len(prior)

    @property
    def trend_direction(self) -> str:
        """RISING_FAST | RISING | STABLE | DECLINING | DECLINING_FAST."""
        acc = self.acceleration_3v3
        delta = self.rank_delta_total
        composite = acc + delta * 0.3
        if composite >= 10:
            return "RISING_FAST"
        if composite >= 3:
            return "RISING"
        if composite <= -10:
            return "DECLINING_FAST"
        if composite <= -3:
            return "DECLINING"
        return "STABLE"


@dataclass(frozen=True)
class MarketFlowSummary:
    """The full report produced for HTML / terminal consumption."""

    snapshot_dates: tuple[str, ...]            # newest last
    series: tuple[SectorFlowSeries, ...]       # one per industry
    market_states: tuple[str, ...]             # parallel to snapshot_dates

    def newest_date(self) -> str:
        return self.snapshot_dates[-1] if self.snapshot_dates else ""

    def by_acceleration(self) -> list[SectorFlowSeries]:
        """Sort industries by 3v3 acceleration desc (warming-up first)."""
        return sorted(
            self.series,
            key=lambda s: -s.acceleration_3v3,
        )

    def by_trend(self, direction: str) -> list[SectorFlowSeries]:
        return [s for s in self.series if s.trend_direction == direction]


# ── Loader ──────────────────────────────────────────────────────────────────


_DEFAULT_HEAT_DIR = (
    Path(__file__).resolve().parents[3] / "data" / "market_heat"
)


def load_heat_snapshots(
    days: int = 10,
    heat_dir: Path | None = None,
) -> list[dict]:
    """Load up to `days` newest heat_YYYY-MM-DD.json snapshots, oldest first.

    Silently ignores snapshots that fail to parse. Returns an empty list when
    the directory does not exist or no snapshots match.
    """
    directory = heat_dir or _DEFAULT_HEAT_DIR
    if not directory.exists():
        return []
    candidates = sorted(directory.glob("heat_*.json"))
    out: list[dict] = []
    for path in candidates[-days:]:
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


# ── Analyzer ────────────────────────────────────────────────────────────────


class SectorFlowAnalyzer:
    """Turn raw heat snapshots into ranked time-series for visualisation."""

    def __init__(self, heat_dir: Path | None = None) -> None:
        self._heat_dir = heat_dir

    def analyze(
        self,
        days: int = 10,
        snapshots: Sequence[dict] | None = None,
    ) -> MarketFlowSummary:
        """Build a MarketFlowSummary either from injected snapshots or disk."""
        snaps = list(snapshots) if snapshots is not None else load_heat_snapshots(days=days, heat_dir=self._heat_dir)
        if not snaps:
            return MarketFlowSummary(snapshot_dates=(), series=(), market_states=())

        # Build the universe of industries seen across all snapshots
        industries: set[str] = set()
        for s in snaps:
            industries.update((s.get("industries") or {}).keys())

        dates = tuple(str(s.get("snapshot_date", f"snap_{i}")) for i, s in enumerate(snaps))
        market_states = tuple(str(s.get("market_state", "unknown")) for s in snaps)

        series_list: list[SectorFlowSeries] = []
        for ind in sorted(industries):
            points: list[SectorFlowPoint] = []
            for s in snaps:
                meta = (s.get("industries") or {}).get(ind, {})
                points.append(SectorFlowPoint(
                    date=str(s.get("snapshot_date", "")),
                    rank_pct=float(meta.get("rank_pct", 0) or 0),
                    ret_5d_pct=float(meta.get("ret_5d_pct", 0) or 0),
                    breadth_above_ma20_pct=float(meta.get("breadth_above_ma20_pct", 0) or 0),
                    top5_vol_concentration=float(meta.get("top5_vol_concentration", 0) or 0),
                ))
            series_list.append(SectorFlowSeries(industry=ind, points=tuple(points)))

        return MarketFlowSummary(
            snapshot_dates=dates,
            series=tuple(series_list),
            market_states=market_states,
        )


# ── HTML helpers ────────────────────────────────────────────────────────────


def sparkline_svg(
    values: Sequence[float],
    *,
    width: int = 120,
    height: int = 28,
    stroke: str = "#58a6ff",
    fill_below: bool = True,
) -> str:
    """Render a list of floats as an inline SVG sparkline.

    The path is normalised to fit the box. Returns an HTML string. Used by
    the plan HTML's sector-flow panel.
    """
    if not values or len(values) == 1:
        return f'<svg width="{width}" height="{height}"></svg>'
    lo = min(values)
    hi = max(values)
    if hi - lo < 0.001:
        hi = lo + 1.0
    n = len(values)
    pts: list[str] = []
    for i, v in enumerate(values):
        x = (i / (n - 1)) * (width - 4) + 2
        y = height - 2 - ((v - lo) / (hi - lo)) * (height - 4)
        pts.append(f"{x:.1f},{y:.1f}")
    line_path = "M " + " L ".join(pts)
    fill_path = ""
    if fill_below:
        fill_path = (
            f'<path d="{line_path} L {width-2:.1f},{height-2} L 2,{height-2} Z" '
            f'fill="{stroke}" fill-opacity="0.15" stroke="none"/>'
        )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'{fill_path}'
        f'<path d="{line_path}" fill="none" stroke="{stroke}" stroke-width="1.8" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f'</svg>'
    )


# Trend display metadata for both terminal + HTML
TREND_META: dict[str, tuple[str, str, str]] = {
    # direction → (icon, color_hex, label)
    "RISING_FAST":    ("⬆⬆", "#3fb950", "急速升溫"),
    "RISING":         ("↑",  "#58a6ff", "升溫中"),
    "STABLE":         ("→",  "#8b949e", "持平"),
    "DECLINING":      ("↓",  "#f0b429", "降溫中"),
    "DECLINING_FAST": ("⬇⬇", "#f85149", "急速退燒"),
}


# ── Concept flow (parallels SectorFlowAnalyzer but reads concept snapshots) ──


class ConceptFlowAnalyzer:
    """Reads `concept_heat_*.json` snapshots and emits MarketFlowSummary.

    concept_heat snapshots wrap data one level deeper:
        {"snapshot_date": "...", "concepts": {key: {rank_pct, ret_5d_pct, ...}}}

    The returned MarketFlowSummary uses concept_key as the "industry" label
    so downstream renderers can reuse the same SectorFlow rendering code.
    """

    def __init__(self, heat_dir: Path | None = None) -> None:
        self._heat_dir = heat_dir or _DEFAULT_HEAT_DIR

    def analyze(
        self,
        days: int = 10,
        snapshots: Sequence[dict] | None = None,
        concepts_meta: Mapping[str, dict] | None = None,
    ) -> MarketFlowSummary:
        snaps = list(snapshots) if snapshots is not None else self._load(days)
        if not snaps:
            return MarketFlowSummary(snapshot_dates=(), series=(), market_states=())

        # Collect every concept key seen across snapshots
        keys: set[str] = set()
        for s in snaps:
            keys.update((s.get("concepts") or {}).keys())

        dates = tuple(str(s.get("snapshot_date", f"snap_{i}")) for i, s in enumerate(snaps))
        states = tuple(str(s.get("market_state", "unknown")) for s in snaps)

        # If caller passed concepts.json metadata, prefer name_zh as label
        labels: dict[str, str] = {}
        if concepts_meta:
            for k, meta in concepts_meta.items():
                if isinstance(meta, dict):
                    labels[k] = str(meta.get("name_zh", k))

        series_list: list[SectorFlowSeries] = []
        for key in sorted(keys):
            label = labels.get(key, key)
            points: list[SectorFlowPoint] = []
            for s in snaps:
                meta = (s.get("concepts") or {}).get(key, {})
                points.append(SectorFlowPoint(
                    date=str(s.get("snapshot_date", "")),
                    rank_pct=float(meta.get("rank_pct", 0) or 0),
                    ret_5d_pct=float(meta.get("ret_5d_pct", 0) or 0),
                    breadth_above_ma20_pct=float(meta.get("breadth_above_ma20_pct", 0) or 0),
                    top5_vol_concentration=float(meta.get("top5_vol_concentration", 0) or 0),
                ))
            series_list.append(SectorFlowSeries(industry=label, points=tuple(points)))

        return MarketFlowSummary(
            snapshot_dates=dates,
            series=tuple(series_list),
            market_states=states,
        )

    def _load(self, days: int) -> list[dict]:
        if not self._heat_dir.exists():
            return []
        candidates = sorted(self._heat_dir.glob("concept_heat_*.json"))
        out: list[dict] = []
        for path in candidates[-days:]:
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return out
