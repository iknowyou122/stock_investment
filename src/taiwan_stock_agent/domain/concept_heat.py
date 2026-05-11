"""Concept basket heat computation.

Concepts cross industry boundaries (AI_GPU_supply spans 半導體 + 光電 + 電子零組件).
Each concept = curated ticker list. We compute basket-level momentum same way as
industries but on the union of tickers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path

from taiwan_stock_agent.domain.models import DailyOHLCV
from taiwan_stock_agent.domain.market_heat import (
    _industry_snapshot, IndustryHeat,
)


@dataclass
class ConceptHeat:
    concept_key: str
    name_zh: str
    description: str
    n_tickers: int
    ret_1d_pct: float
    ret_5d_pct: float
    ret_20d_pct: float
    breadth_above_ma20_pct: float
    breadth_above_ma5_pct: float
    top5_vol_concentration: float
    leaders: list[str] = field(default_factory=list)
    leader_chgs: list[float] = field(default_factory=list)
    rank_5d: int = 0
    rank_pct: float = 0.0
    acceleration_pct: float = 0.0


@dataclass
class ConceptHeatSnapshot:
    snapshot_date: date
    concepts: dict[str, ConceptHeat]
    hot_concepts: list[str]   # rank_pct >= 70
    cold_concepts: list[str]  # rank_pct < 30

    def to_dict(self) -> dict:
        return {
            "snapshot_date": self.snapshot_date.isoformat(),
            "concepts": {k: asdict(v) for k, v in self.concepts.items()},
            "hot_concepts": self.hot_concepts,
            "cold_concepts": self.cold_concepts,
        }


def load_concepts(path: Path) -> dict[str, dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("concepts", {})


def compute_concept_heat(
    all_bars: dict[str, list[DailyOHLCV]],
    concepts_def: dict[str, dict],
    snapshot_date: date,
) -> ConceptHeatSnapshot:
    """Compute heat per concept basket."""
    concepts: dict[str, ConceptHeat] = {}

    for key, meta in concepts_def.items():
        tickers = meta.get("tickers", [])
        members_bars = {
            t: all_bars[t] for t in tickers
            if t in all_bars and len(all_bars[t]) >= 21
        }
        if not members_bars:
            continue
        # Reuse industry snapshot logic with this ticker subset
        snap = _industry_snapshot(key, members_bars)
        if snap is None:
            continue
        ch = ConceptHeat(
            concept_key=key,
            name_zh=meta.get("name_zh", key),
            description=meta.get("description", ""),
            n_tickers=snap.n_tickers,
            ret_1d_pct=snap.ret_1d_pct,
            ret_5d_pct=snap.ret_5d_pct,
            ret_20d_pct=snap.ret_20d_pct,
            breadth_above_ma20_pct=snap.breadth_above_ma20_pct,
            breadth_above_ma5_pct=snap.breadth_above_ma5_pct,
            top5_vol_concentration=snap.top5_vol_concentration,
            leaders=snap.leaders,
            leader_chgs=snap.leader_chgs,
            acceleration_pct=snap.acceleration_pct,
        )
        concepts[key] = ch

    # Rank by 5d return
    sorted_c = sorted(concepts.values(), key=lambda x: x.ret_5d_pct, reverse=True)
    total = len(sorted_c)
    for rank, ch in enumerate(sorted_c, start=1):
        ch.rank_5d = rank
        ch.rank_pct = round((total - rank + 1) / total * 100, 1)

    hot = [c.concept_key for c in concepts.values() if c.rank_pct >= 70]
    cold = [c.concept_key for c in concepts.values() if c.rank_pct < 30]

    return ConceptHeatSnapshot(
        snapshot_date=snapshot_date,
        concepts=concepts,
        hot_concepts=hot,
        cold_concepts=cold,
    )


def save_concept_snapshot(snap: ConceptHeatSnapshot, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"concept_heat_{snap.snapshot_date.isoformat()}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap.to_dict(), f, ensure_ascii=False, indent=2)
    return path


def get_concept_membership(
    ticker: str, concepts_def: dict[str, dict]
) -> list[str]:
    """Which concepts does this ticker belong to?"""
    return [k for k, m in concepts_def.items() if ticker in m.get("tickers", [])]
