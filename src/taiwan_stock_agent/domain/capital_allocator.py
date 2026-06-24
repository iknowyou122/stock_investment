"""Capital Allocation Tier System.

Turns a flat list of scan signals into a tier-based portfolio plan
(S/A/B/C) using rotation, heat, concept, and concentration data.

Pipeline:
    raw signals
        → RotationMetrics per ticker (industry state, concept hotness, leader)
        → ConcentrationAnalysis (warnings for over-clustered picks)
        → AllocationContext (everything packaged for an LLM advisor)

The actual S/A/B/C assignment and resource % is performed downstream by
AllocationAdvisor (LLM); this module only prepares structured context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RotationMetrics:
    """Rotation / heat metrics for a single ticker.

    All scores are 0-100 (higher = stronger tailwind).
    """

    ticker: str
    industry: str
    industry_state: str  # HOT | EMERGING | COOLING | COLD | NEUTRAL
    industry_rank_pct: float  # 0-100
    industry_rank_delta: float  # change vs prior snapshot
    concept_keys: tuple[str, ...]  # concept basket ids the ticker belongs to
    concept_states: tuple[str, ...]  # parallel state list (HOT/EMERGING/...)
    is_industry_leader: bool
    breadth_above_ma20_pct: float  # 0-100, the % of industry above MA20
    rotation_score: float  # composite 0-100

    @property
    def has_tailwind(self) -> bool:
        """True when industry or concept is HOT/EMERGING."""
        return (
            self.industry_state in {"HOT", "EMERGING"}
            or any(s in {"HOT", "EMERGING"} for s in self.concept_states)
        )

    @property
    def has_headwind(self) -> bool:
        return self.industry_state in {"COOLING", "COLD"} and not any(
            s in {"HOT", "EMERGING"} for s in self.concept_states
        )


@dataclass(frozen=True)
class ClusterWarning:
    """One concentration warning."""

    cluster_type: str  # "industry" | "concept"
    label: str
    tickers: tuple[str, ...]
    severity: str  # "high" | "medium" | "low"
    message: str


@dataclass(frozen=True)
class ConcentrationAnalysis:
    """Concentration warnings derived from a batch of signals."""

    industry_counts: Mapping[str, int]
    concept_counts: Mapping[str, int]
    warnings: tuple[ClusterWarning, ...]

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)


@dataclass(frozen=True)
class AllocationContext:
    """Bundle of structured context handed to AllocationAdvisor (the LLM).

    The LLM receives this as JSON and returns a TierRecommendation list.
    """

    signals: tuple[dict, ...]  # original signal dicts (read-only view)
    rotation_metrics: Mapping[str, RotationMetrics]  # ticker → metrics
    concentration: ConcentrationAnalysis
    market_state: str  # broad_rally | mixed | risk_off | unknown
    market_breadth: float  # 0-100
    snapshot_date: str  # date string of rotation/heat snapshot


# ── Loader ──────────────────────────────────────────────────────────────────


_DEFAULT_HEAT_DIR = (
    Path(__file__).resolve().parents[3] / "data" / "market_heat"
)
_DEFAULT_CONCEPTS = (
    Path(__file__).resolve().parents[3] / "config" / "concepts.json"
)


class CapitalAllocator:
    """Loads rotation/heat/concept JSON and computes RotationMetrics."""

    def __init__(
        self,
        heat_dir: Path | None = None,
        concepts_path: Path | None = None,
    ) -> None:
        self._heat_dir = heat_dir or _DEFAULT_HEAT_DIR
        self._concepts_path = concepts_path or _DEFAULT_CONCEPTS
        self._concept_map: dict[str, list[str]] | None = None  # ticker → [concept_key]

    # --- public API -------------------------------------------------------

    def assess(
        self,
        signals: Sequence[dict],
        industry_map: Mapping[str, str],
        snapshot_date: str | None = None,
    ) -> AllocationContext:
        """Build an AllocationContext for the given batch of signals."""
        heat, rotation, concept_heat = self._load_snapshots(snapshot_date)
        snap_date = (
            heat.get("snapshot_date")
            or rotation.get("signal_date")
            or snapshot_date
            or "unknown"
        )

        rotation_index = self._build_rotation_index(rotation)
        industry_index = self._build_industry_index(heat)
        concept_state_index = self._build_concept_state_index(concept_heat)
        concept_membership = self._load_concept_membership()

        metrics: dict[str, RotationMetrics] = {}
        for sig in signals:
            ticker = str(sig.get("ticker", ""))
            if not ticker:
                continue
            industry = industry_map.get(ticker, "")
            metrics[ticker] = self._compute_rotation_metrics(
                ticker=ticker,
                industry=industry,
                rotation_index=rotation_index,
                industry_index=industry_index,
                concept_state_index=concept_state_index,
                concept_membership=concept_membership,
            )

        concentration = self._analyse_concentration(
            signals=signals,
            metrics=metrics,
        )

        return AllocationContext(
            signals=tuple(signals),
            rotation_metrics=metrics,
            concentration=concentration,
            market_state=str(heat.get("market_state", "unknown")),
            market_breadth=float(heat.get("market_breadth", 0) or 0),
            snapshot_date=str(snap_date),
        )

    # --- loading helpers --------------------------------------------------

    def _load_snapshots(
        self, snapshot_date: str | None
    ) -> tuple[dict, dict, dict]:
        """Return (heat, rotation, concept_heat) — each {} if missing."""
        heat = self._load_dated("heat", snapshot_date)
        rotation_path = self._heat_dir / "rotation_signal.json"
        rotation: dict = {}
        if rotation_path.exists():
            try:
                rotation = json.loads(rotation_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                rotation = {}
        concept_heat = self._load_dated("concept_heat", snapshot_date)
        return heat, rotation, concept_heat

    def _load_dated(self, prefix: str, snapshot_date: str | None) -> dict:
        """Load `<prefix>_<date>.json`, falling back to newest available."""
        if snapshot_date:
            target = self._heat_dir / f"{prefix}_{snapshot_date}.json"
            if target.exists():
                return self._safe_read_json(target)
        candidates = sorted(self._heat_dir.glob(f"{prefix}_*.json"))
        if not candidates:
            return {}
        return self._safe_read_json(candidates[-1])

    @staticmethod
    def _safe_read_json(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _load_concept_membership(self) -> dict[str, list[str]]:
        """ticker → list of concept basket keys."""
        if self._concept_map is not None:
            return self._concept_map
        out: dict[str, list[str]] = {}
        if not self._concepts_path.exists():
            self._concept_map = out
            return out
        try:
            data = json.loads(self._concepts_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self._concept_map = out
            return out
        for key, body in (data.get("concepts") or {}).items():
            for t in body.get("tickers") or []:
                out.setdefault(str(t), []).append(key)
        self._concept_map = out
        return out

    # --- index builders ---------------------------------------------------

    @staticmethod
    def _build_rotation_index(rotation: dict) -> dict[str, dict]:
        """label → {state, rank_pct, rank_delta} from rotation_signal.json.

        rotation_signal.json has shape:
          {hot_nodes: [{label, state, rank_pct, rank_delta, ...}], ...}
        """
        idx: dict[str, dict] = {}
        for bucket in ("hot_nodes", "emerging_nodes", "cooling_nodes", "cold_nodes"):
            for node in rotation.get(bucket) or []:
                label = node.get("label") or node.get("key")
                if not label:
                    continue
                idx[str(label)] = {
                    "state": node.get("state", ""),
                    "rank_pct": float(node.get("rank_pct", 0) or 0),
                    "rank_delta": float(node.get("rank_delta", 0) or 0),
                    "type": node.get("type", ""),
                }
        return idx

    @staticmethod
    def _build_industry_index(heat: dict) -> dict[str, dict]:
        """industry → {leaders, breadth, mom_5d, ...}."""
        return {
            str(k): v for k, v in (heat.get("industries") or {}).items()
        }

    @staticmethod
    def _build_concept_state_index(concept_heat: dict) -> dict[str, str]:
        """concept_key → state (HOT/WARM/COLD).

        concept_heat_xxxx.json shape is { concept_key: {rank_pct, mom_5d, ...} }
        at the top level; promote a state label from rank_pct.
        """
        idx: dict[str, str] = {}
        body = concept_heat.get("concepts") or concept_heat
        if not isinstance(body, dict):
            return idx
        for key, v in body.items():
            if not isinstance(v, dict):
                continue
            rank_pct = float(v.get("rank_pct", 0) or 0)
            mom_5d = float(v.get("mom_5d_pct", v.get("mom_5d", 0)) or 0)
            if rank_pct >= 80 or mom_5d >= 5:
                idx[str(key)] = "HOT"
            elif rank_pct >= 60:
                idx[str(key)] = "EMERGING"
            elif rank_pct <= 20:
                idx[str(key)] = "COLD"
            else:
                idx[str(key)] = "NEUTRAL"
        return idx

    # --- per-ticker metrics ----------------------------------------------

    @staticmethod
    def _compute_rotation_metrics(
        *,
        ticker: str,
        industry: str,
        rotation_index: Mapping[str, dict],
        industry_index: Mapping[str, dict],
        concept_state_index: Mapping[str, str],
        concept_membership: Mapping[str, list[str]],
    ) -> RotationMetrics:
        rot = rotation_index.get(industry, {}) if industry else {}
        ind_meta = industry_index.get(industry, {}) if industry else {}
        ind_state = rot.get("state", "NEUTRAL") or "NEUTRAL"
        ind_rank_pct = float(rot.get("rank_pct", 0) or 0)
        ind_rank_delta = float(rot.get("rank_delta", 0) or 0)
        leaders = ind_meta.get("leaders") or []
        is_leader = ticker in set(map(str, leaders))
        breadth = float(ind_meta.get("breadth_above_ma20_pct", 0) or 0)

        # concept membership
        keys = tuple(concept_membership.get(ticker, []))
        states = tuple(concept_state_index.get(k, "NEUTRAL") for k in keys)

        # composite rotation_score (0-100)
        industry_part = max(0.0, min(100.0, ind_rank_pct + ind_rank_delta * 0.5))
        # concept_part: best concept state wins (HOT=80, EMERGING=60, NEUTRAL=40, COLD=20)
        concept_score_map = {
            "HOT": 90.0,
            "EMERGING": 70.0,
            "NEUTRAL": 45.0,
            "COLD": 15.0,
        }
        concept_part = (
            max((concept_score_map.get(s, 45.0) for s in states), default=45.0)
        )
        leader_bonus = 100.0 if is_leader else 50.0
        composite = (
            industry_part * 0.5 + concept_part * 0.3 + leader_bonus * 0.2
        )

        return RotationMetrics(
            ticker=ticker,
            industry=industry,
            industry_state=ind_state,
            industry_rank_pct=ind_rank_pct,
            industry_rank_delta=ind_rank_delta,
            concept_keys=keys,
            concept_states=states,
            is_industry_leader=is_leader,
            breadth_above_ma20_pct=breadth,
            rotation_score=round(composite, 1),
        )

    # --- concentration ----------------------------------------------------

    @staticmethod
    def _analyse_concentration(
        signals: Sequence[dict],
        metrics: Mapping[str, RotationMetrics],
    ) -> ConcentrationAnalysis:
        long_or_watch = [
            s for s in signals
            if s.get("action") in {"LONG", "WATCH"}
        ]
        industry_counts: dict[str, int] = {}
        concept_counts: dict[str, int] = {}
        industry_tickers: dict[str, list[str]] = {}
        concept_tickers: dict[str, list[str]] = {}
        for s in long_or_watch:
            tk = str(s.get("ticker", ""))
            m = metrics.get(tk)
            if not m:
                continue
            if m.industry:
                industry_counts[m.industry] = industry_counts.get(m.industry, 0) + 1
                industry_tickers.setdefault(m.industry, []).append(tk)
            for ck in m.concept_keys:
                concept_counts[ck] = concept_counts.get(ck, 0) + 1
                concept_tickers.setdefault(ck, []).append(tk)

        warnings: list[ClusterWarning] = []
        for ind, n in sorted(industry_counts.items(), key=lambda kv: -kv[1]):
            if n >= 4:
                warnings.append(ClusterWarning(
                    cluster_type="industry",
                    label=ind,
                    tickers=tuple(industry_tickers[ind]),
                    severity="high",
                    message=f"{ind} 出現 {n} 支訊號 → 建議只取 top 2 分散風險",
                ))
            elif n == 3:
                warnings.append(ClusterWarning(
                    cluster_type="industry",
                    label=ind,
                    tickers=tuple(industry_tickers[ind]),
                    severity="medium",
                    message=f"{ind} 出現 3 支訊號 → 注意產業集中度",
                ))
        for ck, n in sorted(concept_counts.items(), key=lambda kv: -kv[1]):
            if n >= 5:
                warnings.append(ClusterWarning(
                    cluster_type="concept",
                    label=ck,
                    tickers=tuple(concept_tickers[ck]),
                    severity="high",
                    message=f"{ck} 題材 {n} 支訊號 → 題材高度集中，連動風險大",
                ))
            elif n >= 3:
                warnings.append(ClusterWarning(
                    cluster_type="concept",
                    label=ck,
                    tickers=tuple(concept_tickers[ck]),
                    severity="medium",
                    message=f"{ck} 題材 {n} 支訊號 → 同題材連動，建議只取 top 2",
                ))

        return ConcentrationAnalysis(
            industry_counts=industry_counts,
            concept_counts=concept_counts,
            warnings=tuple(warnings),
        )


# ── Utility helpers used by the advisor / HTML layers ───────────────────────


TIER_ORDER = ("S", "A", "B", "C")
TIER_COLORS = {
    "S": "gold1",
    "A": "cyan",
    "B": "yellow",
    "C": "grey50",
}


@dataclass(frozen=True)
class TierRecommendation:
    """A single LLM-produced tier recommendation."""

    ticker: str
    tier: str  # S | A | B | C
    suggested_pct: float  # 0-30, suggested portfolio allocation in %
    reasoning: str
    rotation_score: float

    def normalised_tier(self) -> str:
        t = (self.tier or "").upper()
        return t if t in TIER_ORDER else "C"


@dataclass(frozen=True)
class AllocationPlan:
    """Final advisor output, grouped by tier."""

    tiers: dict[str, list[TierRecommendation]]
    warnings: tuple[ClusterWarning, ...]
    summary: str
    provider: str
    snapshot_date: str

    def all_recommendations(self) -> Iterable[TierRecommendation]:
        for t in TIER_ORDER:
            yield from self.tiers.get(t, [])

    @property
    def actionable_recommendations(self) -> list[TierRecommendation]:
        """Tier S/A/B with suggested_pct > 0 — safe to write to DB.

        Phase 4.50: filters out tier C and 0%-allocations so downstream code
        (HoldingsManager.process_day) cannot accidentally write 200+ rows
        when LLM falls back.
        """
        out: list[TierRecommendation] = []
        for t in ("S", "A", "B"):
            for rec in self.tiers.get(t, []):
                if rec.suggested_pct > 0:
                    out.append(rec)
        return out
