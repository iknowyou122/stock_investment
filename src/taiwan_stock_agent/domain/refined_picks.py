"""RefinedPickFilter — distil 300+ raw scan results down to ~25 high-quality picks.

Background
----------
`make plan` produces 300-400 LONG/WATCH signals every day. Most are noise
(low TCE confidence, Pullback-bug fallouts where the original TCE engine
emitted conf=0 but a secondary detector force-promoted them). Sending all
of them to AllocationAdvisor / HoldingsManager balloons the DB and the
HTML output, defeating the point of a daily plan.

This module filters scan results by three rules in order:

1. Hard floor on real TCE confidence (default >= 85.0)
2. Require a valid `_signal` object (excludes Pullback-only rows that
   bypassed TCE — the cause of the long-standing "TCE 0 in Tier" bug)
3. Composite scoring that rewards BB compression, HOT/EMERGING industry,
   concept tailwind — so the top N are not just "highest TCE" but the
   structurally cleanest setups too.

Result: a stable list of <=N RefinedPick entries that downstream code
(BudgetAllocator, AllocationAdvisor fallback, HTML hero region) can rely
on without re-doing the same filtering everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence


# ── Data class ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RefinedPick:
    """A single high-quality scan pick after filtering.

    Carries the original raw result dict in `raw_result` so downstream
    consumers (allocator, recorder) don't need to refetch.
    """

    ticker: str
    name: str
    sector: str
    confidence: float           # real TCE confidence (>= MIN_CONFIDENCE)
    entry_price: float
    bb_pct: float | None        # BB-width percentile in last 60 bars; None if not detected
    industry_state: str         # HOT | EMERGING | COOLING | COLD | NEUTRAL
    concept_keys: tuple[str, ...]
    concept_tailwind: bool      # True when ticker belongs to a HOT concept basket
    is_held: bool               # True if already in simulated_holdings (OPEN)
    composite_score: float      # final ranking score
    raw_result: dict = field(repr=False)

    @property
    def has_bb_compression(self) -> bool:
        return self.bb_pct is not None and self.bb_pct <= 35.0


# ── Filter ──────────────────────────────────────────────────────────────────


class RefinedPickFilter:
    """Distil noisy scan results to top-N high-quality picks."""

    MIN_CONFIDENCE = 85.0       # baseline floor; excludes Pullback bug + low-quality
    DEFAULT_TOP_N = 25

    # Composite-score bonuses
    BONUS_BB_COMPRESSED = 5.0   # bb_pct <= 35
    BONUS_BB_PRIMED = 8.0       # bb_pct <= 15 (extremely tight)
    BONUS_INDUSTRY_HOT = 8.0
    BONUS_INDUSTRY_EMERGING = 5.0
    PENALTY_INDUSTRY_COOLING = -3.0
    BONUS_CONCEPT_TAILWIND = 3.0
    BONUS_HELD = 2.0            # small bonus for held tickers so they don't disappear

    def refine(
        self,
        results: Sequence[dict],
        *,
        industry_map: Mapping[str, str] | None = None,
        rotation_signal: dict | None = None,
        concept_membership: Mapping[str, list[str]] | None = None,
        hot_concepts: set[str] | None = None,
        held_tickers: set[str] | None = None,
        name_map: Mapping[str, str] | None = None,
        top_n: int = DEFAULT_TOP_N,
        min_confidence: float | None = None,
    ) -> list[RefinedPick]:
        """Apply all filters and return top-N picks sorted by composite_score desc.

        Parameters
        ----------
        results : raw scan result dicts (from `_scan_one` / `_run_phase`)
        industry_map : ticker → industry name
        rotation_signal : data/market_heat/rotation_signal.json contents (optional)
        concept_membership : ticker → [concept_key, ...]
        hot_concepts : set of concept_keys currently HOT
        held_tickers : set of tickers currently OPEN in simulated_holdings
        name_map : ticker → display name
        top_n : cap on output size
        min_confidence : override MIN_CONFIDENCE (for testing)
        """
        industry_map = industry_map or {}
        concept_membership = concept_membership or {}
        hot_concepts = hot_concepts or set()
        held_tickers = held_tickers or set()
        name_map = name_map or {}
        conf_floor = self.MIN_CONFIDENCE if min_confidence is None else min_confidence

        industry_states = self._build_industry_state_map(rotation_signal)

        picks: list[RefinedPick] = []
        for r in results:
            # Rule 1: must be LONG/WATCH
            if r.get("action") not in ("LONG", "WATCH"):
                continue
            if r.get("halt") or r.get("error") is not None:
                continue

            conf = float(r.get("confidence", 0) or 0)
            # Rule 2: TCE confidence floor (excludes noise + Pullback bug residue)
            if conf < conf_floor:
                continue

            # Rule 3: must have a real TCE _signal object (excludes pure Pullback rows)
            if r.get("_signal") is None:
                continue

            ticker = str(r.get("ticker", ""))
            if not ticker:
                continue

            sector = industry_map.get(ticker, "")
            industry_state = industry_states.get(sector, "NEUTRAL")
            bb_pct = self._extract_bb_pct(r)
            ck = tuple(concept_membership.get(ticker, ()))
            tailwind = any(c in hot_concepts for c in ck)
            is_held = ticker in held_tickers

            composite = self._composite_score(
                confidence=conf,
                bb_pct=bb_pct,
                industry_state=industry_state,
                concept_tailwind=tailwind,
                is_held=is_held,
            )

            picks.append(RefinedPick(
                ticker=ticker,
                name=name_map.get(ticker, ticker),
                sector=sector,
                confidence=conf,
                entry_price=float(r.get("entry_bid") or r.get("entry_price") or 0),
                bb_pct=bb_pct,
                industry_state=industry_state,
                concept_keys=ck,
                concept_tailwind=tailwind,
                is_held=is_held,
                composite_score=composite,
                raw_result=r,
            ))

        # Sort by composite_score desc, tiebreak by confidence desc, then ticker asc
        picks.sort(key=lambda p: (-p.composite_score, -p.confidence, p.ticker))
        return picks[:top_n]

    # --- helpers ----------------------------------------------------------

    @classmethod
    def _composite_score(
        cls,
        *,
        confidence: float,
        bb_pct: float | None,
        industry_state: str,
        concept_tailwind: bool,
        is_held: bool,
    ) -> float:
        score = confidence
        if bb_pct is not None:
            if bb_pct <= 15.0:
                score += cls.BONUS_BB_PRIMED
            elif bb_pct <= 35.0:
                score += cls.BONUS_BB_COMPRESSED
        if industry_state == "HOT":
            score += cls.BONUS_INDUSTRY_HOT
        elif industry_state == "EMERGING":
            score += cls.BONUS_INDUSTRY_EMERGING
        elif industry_state in ("COOLING", "COLD"):
            score += cls.PENALTY_INDUSTRY_COOLING
        if concept_tailwind:
            score += cls.BONUS_CONCEPT_TAILWIND
        if is_held:
            score += cls.BONUS_HELD
        return round(score, 2)

    @staticmethod
    def _extract_bb_pct(result: dict) -> float | None:
        """Parse `GATE_PASS:G2_BB_PCT:XX.Xp` out of flags / score_breakdown."""
        flags = result.get("flags") or []
        sb = result.get("_signal")
        if sb is not None and hasattr(sb, "data_quality_flags"):
            flags = list(flags) + list(sb.data_quality_flags or [])
        for f in flags:
            if not isinstance(f, str):
                continue
            if "GATE_PASS:G2_BB_PCT:" in f:
                try:
                    return float(f.split("PCT:")[-1].rstrip("p"))
                except ValueError:
                    continue
            if f == "GATE_PASS:G2_BB_NARROW":
                return 30.0
        return None

    @staticmethod
    def _build_industry_state_map(rotation_signal: dict | None) -> dict[str, str]:
        """rotation_signal.json shape: {hot_nodes: [{label, state}, ...], ...}."""
        out: dict[str, str] = {}
        if not isinstance(rotation_signal, dict):
            return out
        for bucket in ("hot_nodes", "emerging_nodes", "cooling_nodes", "cold_nodes"):
            for node in rotation_signal.get(bucket) or []:
                label = node.get("label") or node.get("key")
                state = node.get("state")
                if label and state:
                    out[str(label)] = str(state)
        return out
