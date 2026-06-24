"""Unit tests for CapitalAllocator + AllocationAdvisor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taiwan_stock_agent.agents.allocation_advisor import AllocationAdvisor
from taiwan_stock_agent.domain.capital_allocator import (
    AllocationContext,
    CapitalAllocator,
    ClusterWarning,
    ConcentrationAnalysis,
    RotationMetrics,
    TIER_ORDER,
    TierRecommendation,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def heat_concept_files(tmp_path: Path) -> Path:
    """Write minimal heat_DATE / rotation_signal / concept_heat_DATE JSON.

    Date string '2026-06-04'. Returns the heat_dir path.
    """
    heat_dir = tmp_path / "market_heat"
    heat_dir.mkdir(parents=True)
    date_str = "2026-06-04"

    heat = {
        "snapshot_date": date_str,
        "market_state": "broad_rally",
        "market_breadth": 72.0,
        "industries": {
            "光電業": {
                "industry": "光電業",
                "leaders": ["3008", "2409"],
                "breadth_above_ma20_pct": 80.0,
                "ret_5d_pct": 4.5,
            },
            "電腦及週邊設備業": {
                "industry": "電腦及週邊設備業",
                "leaders": ["2330", "2454"],
                "breadth_above_ma20_pct": 65.0,
            },
            "塑膠工業": {
                "industry": "塑膠工業",
                "leaders": ["1301"],
                "breadth_above_ma20_pct": 20.0,
            },
        },
    }
    (heat_dir / f"heat_{date_str}.json").write_text(
        json.dumps(heat, ensure_ascii=False), encoding="utf-8"
    )

    rotation = {
        "signal_date": date_str,
        "hot_nodes": [
            {"label": "光電業", "state": "HOT", "rank_pct": 88.0, "rank_delta": 12.0, "type": "industry"},
            {"label": "電腦及週邊設備業", "state": "HOT", "rank_pct": 75.0, "rank_delta": 5.0, "type": "industry"},
        ],
        "emerging_nodes": [
            {"label": "農業科技業", "state": "EMERGING", "rank_pct": 18.0, "rank_delta": 11.0, "type": "industry"},
        ],
        "cooling_nodes": [
            {"label": "塑膠工業", "state": "COOLING", "rank_pct": 22.0, "rank_delta": -8.0, "type": "industry"},
        ],
    }
    (heat_dir / "rotation_signal.json").write_text(
        json.dumps(rotation, ensure_ascii=False), encoding="utf-8"
    )

    concept_heat = {
        "snapshot_date": date_str,
        "AI_GPU_supply": {"rank_pct": 92.0, "mom_5d_pct": 6.0},
        "HBM_memory": {"rank_pct": 70.0, "mom_5d_pct": 3.0},
    }
    (heat_dir / f"concept_heat_{date_str}.json").write_text(
        json.dumps(concept_heat, ensure_ascii=False), encoding="utf-8"
    )

    return heat_dir


@pytest.fixture
def concepts_file(tmp_path: Path) -> Path:
    """Minimal concepts.json under config dir."""
    cfg = tmp_path / "concepts.json"
    cfg.write_text(json.dumps({
        "concepts": {
            "AI_GPU_supply": {"tickers": ["2330", "2454", "3661"]},
            "HBM_memory": {"tickers": ["3661", "2408"]},
        },
    }, ensure_ascii=False), encoding="utf-8")
    return cfg


@pytest.fixture
def allocator(heat_concept_files: Path, concepts_file: Path) -> CapitalAllocator:
    return CapitalAllocator(heat_dir=heat_concept_files, concepts_path=concepts_file)


# ── CapitalAllocator core ────────────────────────────────────────────────────


class TestRotationMetrics:
    def test_industry_leader_detected(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "2330", "action": "LONG", "confidence": 80}]
        ctx = allocator.assess(signals, {"2330": "電腦及週邊設備業"}, snapshot_date="2026-06-04")
        m = ctx.rotation_metrics["2330"]
        assert m.is_industry_leader is True
        assert m.industry_state == "HOT"
        assert m.rotation_score > 65.0

    def test_concept_membership(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "3661", "action": "LONG", "confidence": 75}]
        ctx = allocator.assess(signals, {"3661": "半導體業"}, snapshot_date="2026-06-04")
        m = ctx.rotation_metrics["3661"]
        assert "AI_GPU_supply" in m.concept_keys
        assert "HBM_memory" in m.concept_keys
        assert "HOT" in m.concept_states

    def test_cooling_industry_no_tailwind(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "1301", "action": "LONG", "confidence": 70}]
        ctx = allocator.assess(signals, {"1301": "塑膠工業"}, snapshot_date="2026-06-04")
        m = ctx.rotation_metrics["1301"]
        assert m.industry_state == "COOLING"
        assert m.has_headwind is True
        assert m.has_tailwind is False

    def test_unknown_industry_returns_neutral(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "9999", "action": "WATCH", "confidence": 60}]
        ctx = allocator.assess(signals, {"9999": "未上市"}, snapshot_date="2026-06-04")
        m = ctx.rotation_metrics["9999"]
        assert m.industry_state == "NEUTRAL"
        assert m.has_tailwind is False
        assert m.has_headwind is False

    def test_rotation_score_in_bounds(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "3008", "action": "LONG", "confidence": 78}]
        ctx = allocator.assess(signals, {"3008": "光電業"}, snapshot_date="2026-06-04")
        m = ctx.rotation_metrics["3008"]
        assert 0.0 <= m.rotation_score <= 100.0


class TestConcentrationWarnings:
    def test_industry_concentration_3_warns_medium(self, allocator: CapitalAllocator) -> None:
        signals = [
            {"ticker": "2330", "action": "LONG", "confidence": 80},
            {"ticker": "2454", "action": "LONG", "confidence": 75},
            {"ticker": "2308", "action": "WATCH", "confidence": 65},
        ]
        ind = {"2330": "電腦及週邊設備業", "2454": "電腦及週邊設備業", "2308": "電腦及週邊設備業"}
        ctx = allocator.assess(signals, ind, snapshot_date="2026-06-04")
        warns = [w for w in ctx.concentration.warnings if w.cluster_type == "industry"]
        assert any(w.severity == "medium" for w in warns)

    def test_industry_concentration_4_warns_high(self, allocator: CapitalAllocator) -> None:
        signals = [
            {"ticker": str(t), "action": "LONG", "confidence": 75}
            for t in ("2330", "2454", "2308", "3008")
        ]
        ind = {t: "電腦及週邊設備業" for t in ("2330", "2454", "2308")}
        ind["3008"] = "電腦及週邊設備業"
        ctx = allocator.assess(signals, ind, snapshot_date="2026-06-04")
        warns = [w for w in ctx.concentration.warnings if w.cluster_type == "industry"]
        assert any(w.severity == "high" for w in warns)

    def test_concept_concentration_5_warns_high(self, allocator: CapitalAllocator) -> None:
        signals = [
            {"ticker": "2330", "action": "LONG", "confidence": 75},
            {"ticker": "2454", "action": "LONG", "confidence": 70},
            {"ticker": "3661", "action": "LONG", "confidence": 78},
            {"ticker": "2408", "action": "WATCH", "confidence": 65},
        ]
        ind = {"2330": "半導體業", "2454": "半導體業", "3661": "半導體業", "2408": "半導體業"}
        ctx = allocator.assess(signals, ind, snapshot_date="2026-06-04")
        # All 4 belong to AI_GPU or HBM (2408 in HBM, others in AI_GPU). 3661 belongs to both.
        warns = [w for w in ctx.concentration.warnings if w.cluster_type == "concept"]
        # Top concept count should hit medium (3 signals on AI_GPU_supply)
        assert any(w.cluster_type == "concept" for w in warns)

    def test_no_warnings_when_diversified(self, allocator: CapitalAllocator) -> None:
        signals = [
            {"ticker": "2330", "action": "LONG", "confidence": 75},
            {"ticker": "1301", "action": "WATCH", "confidence": 60},
        ]
        ind = {"2330": "電腦及週邊設備業", "1301": "塑膠工業"}
        ctx = allocator.assess(signals, ind, snapshot_date="2026-06-04")
        assert ctx.concentration.has_warnings is False


class TestAllocationContextSnapshot:
    def test_market_state_propagated(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "2330", "action": "LONG", "confidence": 80}]
        ctx = allocator.assess(signals, {"2330": "電腦及週邊設備業"}, snapshot_date="2026-06-04")
        assert ctx.market_state == "broad_rally"
        assert ctx.market_breadth == 72.0

    def test_missing_snapshot_falls_back_gracefully(self, tmp_path: Path) -> None:
        # No JSON files in heat_dir
        empty = tmp_path / "empty"
        empty.mkdir()
        alloc = CapitalAllocator(heat_dir=empty, concepts_path=tmp_path / "noconcepts.json")
        signals = [{"ticker": "2330", "action": "LONG", "confidence": 80}]
        ctx = alloc.assess(signals, {"2330": "光電業"}, snapshot_date="2026-06-04")
        assert ctx.snapshot_date == "2026-06-04"
        m = ctx.rotation_metrics["2330"]
        assert m.industry_state == "NEUTRAL"


# ── AllocationAdvisor — fallback path (LLM unavailable) ──────────────────────


class _FakeLLM:
    """Test double for LLMProvider Protocol."""

    name = "test-fake"

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls = 0

    def complete(self, prompt: str, max_tokens: int = 500) -> str:
        self.calls += 1
        return self._response


class TestAdvisorFallback:
    def test_no_llm_uses_rule_based(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "2330", "action": "LONG", "confidence": 80}]
        ctx = allocator.assess(signals, {"2330": "電腦及週邊設備業"}, snapshot_date="2026-06-04")
        plan = AllocationAdvisor(llm=None).recommend(ctx)
        assert plan.provider.startswith("fallback:")
        all_recs = list(plan.all_recommendations())
        assert len(all_recs) == 1
        # Tailwind + leader + LONG + conf>=75 → S tier
        assert all_recs[0].tier == "S"
        assert all_recs[0].suggested_pct >= 20.0

    def test_no_llm_cooling_industry_dropped_phase_4_50(self, allocator: CapitalAllocator) -> None:
        """Phase 4.50: cooling-industry 0% picks are dropped from fallback plan.

        Previously these polluted the plan with tier C 0% rows that downstream
        consumers (HoldingsManager, DB) didn't filter, causing 200+ row writes.
        New behavior: only emit picks with suggested_pct > 0.
        """
        signals = [{"ticker": "1301", "action": "LONG", "confidence": 70}]
        ctx = allocator.assess(signals, {"1301": "塑膠工業"}, snapshot_date="2026-06-04")
        plan = AllocationAdvisor(llm=None).recommend(ctx)
        # Cooling-industry rule_based_tier returns 0% → must be filtered out
        assert list(plan.all_recommendations()) == []
        assert plan.actionable_recommendations == []

    def test_no_llm_watch_with_tailwind_goes_to_b(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "3008", "action": "WATCH", "confidence": 67}]
        ctx = allocator.assess(signals, {"3008": "光電業"}, snapshot_date="2026-06-04")
        plan = AllocationAdvisor(llm=None).recommend(ctx)
        rec = next(iter(plan.all_recommendations()))
        assert rec.tier == "B"


class TestAdvisorLLMParsing:
    def test_parses_clean_json(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "2330", "action": "LONG", "confidence": 80}]
        ctx = allocator.assess(signals, {"2330": "電腦及週邊設備業"}, snapshot_date="2026-06-04")
        fake = _FakeLLM(json.dumps({
            "summary": "今日首選 2330",
            "recommendations": [
                {"ticker": "2330", "tier": "S", "suggested_pct": 25.0, "reasoning": "領頭羊 + HOT 產業"},
            ],
        }, ensure_ascii=False))
        plan = AllocationAdvisor(llm=fake).recommend(ctx)
        assert fake.calls == 1
        recs = list(plan.all_recommendations())
        assert len(recs) == 1
        assert recs[0].tier == "S"
        assert recs[0].suggested_pct == 25.0
        assert "領頭羊" in recs[0].reasoning
        assert plan.summary == "今日首選 2330"
        assert plan.provider == "test-fake"

    def test_strips_markdown_fences(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "2330", "action": "LONG", "confidence": 80}]
        ctx = allocator.assess(signals, {"2330": "電腦及週邊設備業"}, snapshot_date="2026-06-04")
        fake = _FakeLLM(
            "```json\n"
            + json.dumps({"summary": "x", "recommendations": [
                {"ticker": "2330", "tier": "A", "suggested_pct": 15.0, "reasoning": "ok"}
            ]}, ensure_ascii=False)
            + "\n```"
        )
        plan = AllocationAdvisor(llm=fake).recommend(ctx)
        recs = list(plan.all_recommendations())
        assert len(recs) == 1
        assert recs[0].tier == "A"

    def test_unparseable_response_falls_back(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "2330", "action": "LONG", "confidence": 80}]
        ctx = allocator.assess(signals, {"2330": "電腦及週邊設備業"}, snapshot_date="2026-06-04")
        fake = _FakeLLM("This is not JSON at all")
        plan = AllocationAdvisor(llm=fake).recommend(ctx)
        assert plan.provider == "fallback:parse_error"
        assert len(list(plan.all_recommendations())) == 1

    def test_missing_ticker_dropped_phase_4_50(self, allocator: CapitalAllocator) -> None:
        """Phase 4.50: tickers the LLM forgot are NOT backfilled at C 0%.

        Previously these polluted the plan and downstream consumers wrote
        200+ rows to DB. Now only tickers the LLM explicitly assigned a
        tier+pct survive.
        """
        signals = [
            {"ticker": "2330", "action": "LONG", "confidence": 80},
            {"ticker": "1301", "action": "LONG", "confidence": 65},
        ]
        ind = {"2330": "電腦及週邊設備業", "1301": "塑膠工業"}
        ctx = allocator.assess(signals, ind, snapshot_date="2026-06-04")
        # LLM only returns 2330; 1301 should NOT be backfilled at C 0%
        fake = _FakeLLM(json.dumps({
            "summary": "only 2330",
            "recommendations": [
                {"ticker": "2330", "tier": "S", "suggested_pct": 25.0, "reasoning": "ok"},
            ],
        }, ensure_ascii=False))
        plan = AllocationAdvisor(llm=fake).recommend(ctx)
        tickers = {r.ticker for r in plan.all_recommendations()}
        assert tickers == {"2330"}
        # 1301 has zero entries in any tier (LLM forgot it; we don't backfill)
        assert all(r.ticker != "1301" for r in plan.all_recommendations())

    def test_pct_clamped_to_tier_cap(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "1301", "action": "WATCH", "confidence": 60}]
        ctx = allocator.assess(signals, {"1301": "塑膠工業"}, snapshot_date="2026-06-04")
        # LLM tries to assign 50% to a C-tier ticker — should be clamped to 3
        fake = _FakeLLM(json.dumps({
            "summary": "x",
            "recommendations": [
                {"ticker": "1301", "tier": "C", "suggested_pct": 50.0, "reasoning": "n/a"},
            ],
        }, ensure_ascii=False))
        plan = AllocationAdvisor(llm=fake).recommend(ctx)
        rec = next(iter(plan.all_recommendations()))
        assert rec.suggested_pct <= 3.0

    def test_invalid_tier_normalised_to_c(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "2330", "action": "LONG", "confidence": 80}]
        ctx = allocator.assess(signals, {"2330": "電腦及週邊設備業"}, snapshot_date="2026-06-04")
        fake = _FakeLLM(json.dumps({
            "summary": "",
            "recommendations": [
                {"ticker": "2330", "tier": "Z", "suggested_pct": 10.0, "reasoning": "?"},
            ],
        }, ensure_ascii=False))
        plan = AllocationAdvisor(llm=fake).recommend(ctx)
        rec = next(iter(plan.all_recommendations()))
        assert rec.tier == "C"


class TestPhase450FallbackCap:
    """Phase 4.50 — fallback must never balloon to 200+ DB writes."""

    def test_fallback_capped_at_25_picks(self, allocator: CapitalAllocator) -> None:
        # 100 signals, no LLM → fallback should emit at most 25 (excluding 0%)
        signals = [
            {"ticker": f"{1000+i:04d}", "action": "LONG", "confidence": 90 - i % 30}
            for i in range(100)
        ]
        ind = {s["ticker"]: "電腦及週邊設備業" for s in signals}
        ctx = allocator.assess(signals, ind, snapshot_date="2026-06-04")
        plan = AllocationAdvisor(llm=None).recommend(ctx)
        # All emitted recs must come from top-25 by confidence
        emitted = list(plan.all_recommendations())
        assert len(emitted) <= 25

    def test_fallback_skips_zero_pct_picks(self, allocator: CapitalAllocator) -> None:
        # Cooling industry → rule gives 0% → must be dropped
        signals = [
            {"ticker": "1301", "action": "LONG", "confidence": 70},  # cooling → 0%
            {"ticker": "2330", "action": "LONG", "confidence": 80},  # leader+HOT → S
        ]
        ind = {"1301": "塑膠工業", "2330": "電腦及週邊設備業"}
        ctx = allocator.assess(signals, ind, snapshot_date="2026-06-04")
        plan = AllocationAdvisor(llm=None).recommend(ctx)
        # 1301 dropped (0%), only 2330 survives
        emitted = [r.ticker for r in plan.all_recommendations()]
        assert "1301" not in emitted
        assert "2330" in emitted

    def test_actionable_recommendations_excludes_c_and_zero_pct(
        self, allocator: CapitalAllocator
    ) -> None:
        signals = [{"ticker": "2330", "action": "LONG", "confidence": 80}]
        ctx = allocator.assess(signals, {"2330": "電腦及週邊設備業"}, snapshot_date="2026-06-04")
        # LLM returns mix of tiers including C 0%
        fake = _FakeLLM(json.dumps({
            "summary": "x",
            "recommendations": [
                {"ticker": "2330", "tier": "S", "suggested_pct": 25.0, "reasoning": "good"},
            ],
        }, ensure_ascii=False))
        plan = AllocationAdvisor(llm=fake).recommend(ctx)
        actionable = plan.actionable_recommendations
        assert all(r.tier in ("S", "A", "B") for r in actionable)
        assert all(r.suggested_pct > 0 for r in actionable)


class TestPlanOutputStructure:
    def test_tier_order_iteration(self, allocator: CapitalAllocator) -> None:
        signals = [
            {"ticker": "2330", "action": "LONG", "confidence": 82},
            {"ticker": "1301", "action": "LONG", "confidence": 65},
            {"ticker": "3008", "action": "WATCH", "confidence": 67},
        ]
        ind = {"2330": "電腦及週邊設備業", "1301": "塑膠工業", "3008": "光電業"}
        ctx = allocator.assess(signals, ind, snapshot_date="2026-06-04")
        plan = AllocationAdvisor(llm=None).recommend(ctx)
        tiers_used = [r.tier for r in plan.all_recommendations()]
        # Must come out in S→A→B→C order regardless of input order
        seen_indices = [TIER_ORDER.index(t) for t in tiers_used]
        assert seen_indices == sorted(seen_indices)

    def test_summary_present_in_fallback(self, allocator: CapitalAllocator) -> None:
        signals = [{"ticker": "2330", "action": "LONG", "confidence": 80}]
        ctx = allocator.assess(signals, {"2330": "電腦及週邊設備業"}, snapshot_date="2026-06-04")
        plan = AllocationAdvisor(llm=None).recommend(ctx)
        assert plan.summary
        assert "首選" in plan.summary or "fallback" in plan.summary.lower() or "LLM 不可用" in plan.summary
