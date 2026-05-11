"""Market Heat Engine — Industry & concept rotation analysis.

Computes per-industry momentum signals from OHLCV data alone (no external API):
  - 1d / 5d / 20d cumulative returns
  - Breadth: % of stocks above MA20
  - Concentration: top-5 volume share
  - Rotation: 5-day rank change
  - Acceleration: 1d vs 5d trend

Output: ranked heat map used by Surge/TIGHT_BASE as pre-filter & bonus factor.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path
from typing import Iterable

from taiwan_stock_agent.domain.models import DailyOHLCV


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class IndustryHeat:
    industry: str
    n_tickers: int                      # how many tickers in this industry today
    ret_1d_pct: float                   # avg 1-day return
    ret_5d_pct: float                   # avg 5-day return
    ret_20d_pct: float                  # avg 20-day return
    breadth_above_ma20_pct: float       # % of tickers with close > MA20
    breadth_above_ma5_pct: float        # % of tickers with close > MA5
    top5_vol_concentration: float       # top-5 volume value / industry total
    leaders: list[str] = field(default_factory=list)         # top-3 by volume value
    leader_chgs: list[float] = field(default_factory=list)   # their 1d %
    rank_5d: int = 0                    # 1 = hottest (by 5d return)
    rank_5d_change: int = 0             # rank_5d_today - rank_5d_5d_ago (negative=rising)
    rank_pct: float = 0.0               # percentile, higher = hotter
    acceleration_pct: float = 0.0       # ret_1d - (ret_5d/5), positive=accelerating


@dataclass
class MarketHeat:
    snapshot_date: date
    industries: dict[str, IndustryHeat]
    hot_industries: list[str]            # rank_pct >= 80
    warm_industries: list[str]           # 60 <= rank_pct < 80
    cold_industries: list[str]           # rank_pct < 20
    rotating_up: list[str]               # rank_5d_change <= -3 (improving)
    rotating_down: list[str]             # rank_5d_change >= +3 (declining)
    accelerating: list[str]              # acceleration_pct > 1.0
    market_state: str                    # "broad_rally" / "narrow_lead" / "mixed" / "broad_selloff"
    market_breadth: float                # overall % above MA20

    def to_dict(self) -> dict:
        return {
            "snapshot_date": self.snapshot_date.isoformat(),
            "industries": {k: asdict(v) for k, v in self.industries.items()},
            "hot_industries": self.hot_industries,
            "warm_industries": self.warm_industries,
            "cold_industries": self.cold_industries,
            "rotating_up": self.rotating_up,
            "rotating_down": self.rotating_down,
            "accelerating": self.accelerating,
            "market_state": self.market_state,
            "market_breadth": round(self.market_breadth, 1),
        }


# ── Helpers ─────────────────────────────────────────────────────────────────

def _series_return(bars: list[DailyOHLCV], n: int) -> float | None:
    """N-day cumulative return percentage."""
    if len(bars) < n + 1:
        return None
    today = bars[-1].close
    base = bars[-(n + 1)].close
    if base <= 0:
        return None
    return (today / base - 1) * 100


def _moving_avg(bars: list[DailyOHLCV], n: int) -> float | None:
    if len(bars) < n:
        return None
    return sum(b.close for b in bars[-n:]) / n


# ── Core ────────────────────────────────────────────────────────────────────

def _industry_snapshot(
    industry: str,
    members_bars: dict[str, list[DailyOHLCV]],
) -> IndustryHeat | None:
    """Compute heat snapshot for one industry given its members' bars."""
    if not members_bars:
        return None

    rets_1d, rets_5d, rets_20d = [], [], []
    above_ma20, above_ma5 = 0, 0
    vol_values: list[tuple[str, float, float]] = []  # (ticker, today_vol_value, today_chg)

    for ticker, bars in members_bars.items():
        if len(bars) < 21:
            continue
        r1 = _series_return(bars, 1)
        r5 = _series_return(bars, 5)
        r20 = _series_return(bars, 20)
        if r1 is None:
            continue

        rets_1d.append(r1)
        if r5 is not None:
            rets_5d.append(r5)
        if r20 is not None:
            rets_20d.append(r20)

        ma20 = _moving_avg(bars, 20)
        ma5 = _moving_avg(bars, 5)
        today_close = bars[-1].close
        if ma20 and today_close > ma20:
            above_ma20 += 1
        if ma5 and today_close > ma5:
            above_ma5 += 1

        vol_values.append((ticker, bars[-1].close * bars[-1].volume, r1))

    if not rets_1d:
        return None

    n = len(rets_1d)
    avg_1d = sum(rets_1d) / n
    avg_5d = sum(rets_5d) / max(1, len(rets_5d))
    avg_20d = sum(rets_20d) / max(1, len(rets_20d))

    vol_values.sort(key=lambda x: x[1], reverse=True)
    total_vol = sum(v for _, v, _ in vol_values) or 1.0
    top5_share = sum(v for _, v, _ in vol_values[:5]) / total_vol
    leaders = [t for t, _, _ in vol_values[:3]]
    leader_chgs = [round(c, 2) for _, _, c in vol_values[:3]]

    acceleration = avg_1d - (avg_5d / 5)

    return IndustryHeat(
        industry=industry,
        n_tickers=n,
        ret_1d_pct=round(avg_1d, 2),
        ret_5d_pct=round(avg_5d, 2),
        ret_20d_pct=round(avg_20d, 2),
        breadth_above_ma20_pct=round(above_ma20 / n * 100, 1),
        breadth_above_ma5_pct=round(above_ma5 / n * 100, 1),
        top5_vol_concentration=round(top5_share * 100, 1),
        leaders=leaders,
        leader_chgs=leader_chgs,
        acceleration_pct=round(acceleration, 2),
    )


def compute_market_heat(
    all_bars: dict[str, list[DailyOHLCV]],
    industry_map: dict[str, str],
    snapshot_date: date,
    prior_5d_ranks: dict[str, int] | None = None,
) -> MarketHeat:
    """Compute MarketHeat snapshot.

    all_bars: ticker → bars up to and including snapshot_date (sorted ascending).
    industry_map: ticker → industry name.
    prior_5d_ranks: optional, industry → rank_5d from 5 trading days ago (for rotation).
    """
    # Group bars by industry
    by_ind: dict[str, dict[str, list[DailyOHLCV]]] = {}
    for ticker, bars in all_bars.items():
        ind = industry_map.get(ticker)
        if not ind or len(bars) < 21:
            continue
        by_ind.setdefault(ind, {})[ticker] = bars

    industries: dict[str, IndustryHeat] = {}
    for ind, members in by_ind.items():
        snap = _industry_snapshot(ind, members)
        if snap:
            industries[ind] = snap

    # Rank by 5d return (descending → rank 1 = hottest)
    sorted_inds = sorted(industries.values(), key=lambda x: x.ret_5d_pct, reverse=True)
    total = len(sorted_inds)
    for rank, ih in enumerate(sorted_inds, start=1):
        ih.rank_5d = rank
        ih.rank_pct = round((total - rank + 1) / total * 100, 1)
        if prior_5d_ranks and ih.industry in prior_5d_ranks:
            ih.rank_5d_change = ih.rank_5d - prior_5d_ranks[ih.industry]

    # Categorize
    hot = [ih.industry for ih in industries.values() if ih.rank_pct >= 80]
    warm = [ih.industry for ih in industries.values() if 60 <= ih.rank_pct < 80]
    cold = [ih.industry for ih in industries.values() if ih.rank_pct < 20]
    rotating_up = [
        ih.industry for ih in industries.values()
        if ih.rank_5d_change <= -3
    ]
    rotating_down = [
        ih.industry for ih in industries.values()
        if ih.rank_5d_change >= 3
    ]
    accelerating = [
        ih.industry for ih in industries.values()
        if ih.acceleration_pct > 1.0
    ]

    # Market state
    all_breadth = (
        sum(ih.breadth_above_ma20_pct for ih in industries.values()) / total
        if total else 0
    )
    if all_breadth >= 70:
        market_state = "broad_rally"
    elif all_breadth <= 30:
        market_state = "broad_selloff"
    elif len(hot) >= 5 and all_breadth < 50:
        market_state = "narrow_leadership"
    else:
        market_state = "mixed"

    return MarketHeat(
        snapshot_date=snapshot_date,
        industries=industries,
        hot_industries=hot,
        warm_industries=warm,
        cold_industries=cold,
        rotating_up=rotating_up,
        rotating_down=rotating_down,
        accelerating=accelerating,
        market_state=market_state,
        market_breadth=all_breadth,
    )


# ── I/O ─────────────────────────────────────────────────────────────────────

def save_heat_snapshot(heat: MarketHeat, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"heat_{heat.snapshot_date.isoformat()}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(heat.to_dict(), f, ensure_ascii=False, indent=2)
    return path


def load_prior_5d_ranks(snapshot_date: date, snapshots_dir: Path) -> dict[str, int]:
    """Load rank_5d from 5 trading days ago, for rotation calculation."""
    files = sorted(snapshots_dir.glob("heat_*.json"))
    if not files:
        return {}
    # Take 5th most recent file before snapshot_date
    candidates = [
        f for f in files
        if f.stem.replace("heat_", "") < snapshot_date.isoformat()
    ]
    if len(candidates) < 5:
        return {}
    target = candidates[-5]
    try:
        with open(target) as f:
            data = json.load(f)
        return {
            ind: meta["rank_5d"]
            for ind, meta in data.get("industries", {}).items()
        }
    except Exception:
        return {}
