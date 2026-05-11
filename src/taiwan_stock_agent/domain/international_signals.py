"""International overnight signals — genuine D-1 leading indicator.

US markets close before Taiwan opens. Major asset overnight moves predict
Taiwan opening flows:
  - NVDA ±3% → AI semi tailwind/headwind
  - SOX (PHLX 半導體指數) ±2% → 半導體業 directional bias
  - SMH (semi ETF) → confirmation
  - DXY (美元指數) up → 出口股相對受惠
  - 10y UST yield → 高估值科技股反向

Map: asset move → Taiwan concept/industry tailwind direction.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path
import json

import yfinance as yf


@dataclass
class AssetMove:
    symbol: str
    name: str
    close: float
    chg_pct: float
    chg_pct_5d: float


@dataclass
class TailwindMap:
    """Concept/industry → tailwind score (-2 strong headwind … +2 strong tailwind)."""
    concept_tailwinds: dict[str, int] = field(default_factory=dict)
    industry_tailwinds: dict[str, int] = field(default_factory=dict)
    narrative: list[str] = field(default_factory=list)


@dataclass
class InternationalSignals:
    snapshot_date: date
    assets: list[AssetMove]
    tailwinds: TailwindMap
    overall_risk_on: bool       # net positive signal for risk assets

    def to_dict(self) -> dict:
        return {
            "snapshot_date": self.snapshot_date.isoformat(),
            "assets": [asdict(a) for a in self.assets],
            "tailwinds": asdict(self.tailwinds),
            "overall_risk_on": self.overall_risk_on,
        }


# ── Asset → Taiwan mapping rules ────────────────────────────────────────────

# (concept_key | industry_name, sensitivity_weight)
_MAPPING = {
    "NVDA": {
        "concept": [("AI_GPU_supply", 2), ("CoWoS_advanced_packaging", 2),
                    ("HBM_memory", 2), ("AI_server_cooling", 1)],
        "industry": [("半導體業", 2), ("光電業", 1)],
    },
    "AMD": {
        "concept": [("AI_GPU_supply", 1)],
        "industry": [("半導體業", 1)],
    },
    "^SOX": {  # PHLX Semi Index
        "concept": [("AI_GPU_supply", 1), ("CoWoS_advanced_packaging", 1)],
        "industry": [("半導體業", 2)],
    },
    "SMH": {  # Semi ETF
        "industry": [("半導體業", 1)],
    },
    "TSM": {  # TSMC ADR
        "industry": [("半導體業", 2)],
        "concept": [("AI_GPU_supply", 1), ("CoWoS_advanced_packaging", 2)],
    },
    "SPY": {  # broad market risk
        "industry": [("金融保險業", 1)],
    },
    "AVGO": {  # Broadcom — CPO/optical
        "concept": [("CPO_silicon_photonics", 2)],
    },
    "ANET": {  # Arista — networking
        "concept": [("CPO_silicon_photonics", 1)],
    },
}


def _classify_move(chg_pct: float) -> int:
    """Map % change to discrete signal: -2..+2."""
    if chg_pct >= 3.0: return 2
    if chg_pct >= 1.0: return 1
    if chg_pct <= -3.0: return -2
    if chg_pct <= -1.0: return -1
    return 0


def fetch_asset(symbol: str, lookback_days: int = 10) -> AssetMove | None:
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period=f"{lookback_days}d", interval="1d", auto_adjust=False)
        hist = hist.dropna(subset=["Close"])
        if len(hist) < 2:
            return None
        close = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        chg = (close / prev - 1) * 100
        chg_5d = ((close / float(hist["Close"].iloc[-6]) - 1) * 100
                  if len(hist) >= 6 else 0.0)
        # Friendly names
        names = {
            "NVDA": "NVIDIA", "AMD": "AMD", "^SOX": "PHLX 半導體",
            "SMH": "Semi ETF", "TSM": "台積 ADR", "SPY": "S&P 500",
            "AVGO": "Broadcom", "ANET": "Arista",
        }
        return AssetMove(
            symbol=symbol, name=names.get(symbol, symbol),
            close=round(close, 2), chg_pct=round(chg, 2), chg_pct_5d=round(chg_5d, 2),
        )
    except Exception:
        return None


def compute_international_signals(
    snapshot_date: date | None = None,
    symbols: list[str] | None = None,
) -> InternationalSignals:
    if symbols is None:
        symbols = list(_MAPPING.keys())
    if snapshot_date is None:
        snapshot_date = date.today()

    assets = [a for a in (fetch_asset(s) for s in symbols) if a]

    # Aggregate tailwinds
    concept_scores: dict[str, int] = {}
    industry_scores: dict[str, int] = {}
    narrative: list[str] = []

    for asset in assets:
        signal = _classify_move(asset.chg_pct)
        if signal == 0:
            continue
        rules = _MAPPING.get(asset.symbol, {})
        for concept, weight in rules.get("concept", []):
            concept_scores[concept] = concept_scores.get(concept, 0) + signal * weight
        for industry, weight in rules.get("industry", []):
            industry_scores[industry] = industry_scores.get(industry, 0) + signal * weight
        if abs(signal) >= 1:
            direction = "上漲" if signal > 0 else "下跌"
            narrative.append(f"{asset.name} 隔夜{direction} {asset.chg_pct:+.2f}%")

    # Clamp scores to -3..+3
    concept_scores = {k: max(-3, min(3, v)) for k, v in concept_scores.items()}
    industry_scores = {k: max(-3, min(3, v)) for k, v in industry_scores.items()}

    net_score = sum(industry_scores.values()) + sum(concept_scores.values())
    risk_on = net_score > 0

    return InternationalSignals(
        snapshot_date=snapshot_date,
        assets=assets,
        tailwinds=TailwindMap(
            concept_tailwinds=concept_scores,
            industry_tailwinds=industry_scores,
            narrative=narrative,
        ),
        overall_risk_on=risk_on,
    )


def save_intl_snapshot(signals: InternationalSignals, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"intl_signals_{signals.snapshot_date.isoformat()}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(signals.to_dict(), f, ensure_ascii=False, indent=2)
    return path
