"""AllocationAdvisor — LLM consultant for tier-based capital allocation.

Given an AllocationContext (built by CapitalAllocator) this advisor packages
the structured data into a single LLM call and returns a
TierRecommendation list grouped by tier (S/A/B/C).

Design choices:
  * One LLM call per `make plan` run (Gemini free tier is 20 req/day).
  * Strict JSON-only response contract with a deterministic fallback so the
    pipeline never crashes when the LLM is unavailable or returns garbage.
  * Tier semantics are explained in the prompt itself so the advisor can
    swap providers (Claude/OpenAI/Gemini) without behavior drift.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable, Mapping

from ..domain.capital_allocator import (
    AllocationContext,
    AllocationPlan,
    ClusterWarning,
    RotationMetrics,
    TIER_ORDER,
    TierRecommendation,
)
from ..domain.llm_provider import LLMProvider, create_llm_provider

logger = logging.getLogger(__name__)


_TIER_RULES = """\
Tier semantics (apply strictly):
  S (首選, 20-30% each): LONG action AND industry HOT/EMERGING AND high confidence (>=75) AND ideally industry leader or strong concept tailwind.
  A (強勢, 12-18% each): LONG action AND industry HOT/EMERGING AND confidence >= 70 OR S-class minus a single ingredient.
  B (試單, 5-10% each):  WATCH action with tailwind, OR LONG with weaker rotation; cap at 10%.
  C (觀察, 0-3% each):   Any signal whose industry is COOLING/COLD with no concept tailwind, or low confidence.

Concentration rules (apply BEFORE finalising tiers):
  * If same industry has >=3 LONG signals, downgrade everything past the top-2 to one tier lower.
  * If same concept basket has >=5 signals, downgrade everything past the top-2 to one tier lower.
"""


_RESPONSE_FORMAT = """\
Respond with STRICT JSON ONLY (no Markdown fences, no comments). Schema:
{
  "summary": "<2-3 sentence Mandarin briefing for the user>",
  "recommendations": [
    {
      "ticker": "<6 digit code>",
      "tier": "S|A|B|C",
      "suggested_pct": <number 0-30>,
      "reasoning": "<one Mandarin sentence, mention rotation + concentration>"
    }
  ]
}
Include EVERY signal in `recommendations` — do not silently drop any ticker.
"""


class AllocationAdvisor:
    """Wraps an LLM provider with a tier-recommendation prompt."""

    # Cap candidates sent to the LLM to avoid TPM limits. Anything past this
    # cap is tier-assigned via the rule-based fallback (still listed in plan).
    MAX_LLM_CANDIDATES = 35

    def __init__(self, llm: LLMProvider | None = None) -> None:
        self._llm = llm

    @classmethod
    def from_env(cls, provider: str | None = None) -> "AllocationAdvisor":
        return cls(create_llm_provider(provider))

    # --- public ----------------------------------------------------------

    def recommend(self, context: AllocationContext) -> AllocationPlan:
        """Produce an AllocationPlan from the context.

        Falls back to deterministic rule-based tiers when no LLM is configured
        or the LLM response cannot be parsed.
        """
        if self._llm is None:
            return self._fallback_plan(context, "no_llm")

        # Cap candidates to top-N by confidence to stay within TPM limits.
        # Remaining signals are tier-assigned via the rule-based fallback and
        # merged into the final plan.
        ranked = sorted(
            context.signals,
            key=lambda s: -float(s.get("confidence", 0) or 0),
        )
        llm_signals = ranked[: self.MAX_LLM_CANDIDATES]
        leftover = ranked[self.MAX_LLM_CANDIDATES :]
        truncated = len(leftover) > 0
        llm_ctx = AllocationContext(
            signals=tuple(llm_signals),
            rotation_metrics=context.rotation_metrics,
            concentration=context.concentration,
            market_state=context.market_state,
            market_breadth=context.market_breadth,
            snapshot_date=context.snapshot_date,
        )

        prompt = self._build_prompt(llm_ctx)
        try:
            raw = self._llm.complete(prompt, max_tokens=2400)
        except Exception as exc:  # pragma: no cover - network / quota errors
            logger.warning("AllocationAdvisor LLM call failed: %s", exc)
            return self._fallback_plan(context, f"llm_error:{exc.__class__.__name__}")

        parsed = self._parse_response(raw)
        if not parsed or not parsed.get("recommendations"):
            logger.warning("AllocationAdvisor LLM returned unparseable response")
            return self._fallback_plan(context, "parse_error")

        plan = self._plan_from_payload(parsed, llm_ctx, self._llm.name)
        if not truncated:
            return plan
        # Phase 4.50 — leftover (past the LLM cap) are NOT written into the
        # plan. Previously we appended them as tier C @ 0% but downstream
        # consumers (HoldingsManager, DB recorder) ignored the 0% guard and
        # ballooned positions to 200+ rows. Just drop them — the watchlist
        # in HoldingsManager already surfaces overflow picks.
        if leftover:
            return AllocationPlan(
                tiers=plan.tiers,
                warnings=plan.warnings,
                summary=plan.summary + f"（LLM 處理 top {len(llm_signals)}；其餘 {len(leftover)} 支跳過）",
                provider=plan.provider,
                snapshot_date=plan.snapshot_date,
            )
        return plan

    # --- prompt construction --------------------------------------------

    def _build_prompt(self, context: AllocationContext) -> str:
        candidates = self._serialise_candidates(context)
        warnings = [
            {
                "type": w.cluster_type,
                "label": w.label,
                "severity": w.severity,
                "tickers": list(w.tickers),
                "message": w.message,
            }
            for w in context.concentration.warnings
        ]

        payload = {
            "snapshot_date": context.snapshot_date,
            "market_state": context.market_state,
            "market_breadth_pct": context.market_breadth,
            "n_signals": len(context.signals),
            "concentration_warnings": warnings,
            "candidates": candidates,
        }
        payload_json = json.dumps(payload, ensure_ascii=False, indent=None)

        return (
            "你是台股資金配置顧問，使用者資金有限（通常 100 萬以下），\n"
            "需要在多個 LONG / WATCH 訊號中找出『最值得押的少數標的』。\n"
            "請以 Tier 分級給出資金配置建議，並考慮產業/題材集中度。\n\n"
            f"{_TIER_RULES}\n\n"
            f"{_RESPONSE_FORMAT}\n\n"
            f"輸入資料 (JSON):\n{payload_json}"
        )

    def _serialise_candidates(self, context: AllocationContext) -> list[dict]:
        out: list[dict] = []
        for sig in context.signals:
            tk = str(sig.get("ticker", ""))
            metrics = context.rotation_metrics.get(tk)
            out.append({
                "ticker": tk,
                "action": sig.get("action"),
                "confidence": round(float(sig.get("confidence", 0) or 0), 1),
                "score_total": round(float(sig.get("score_total", sig.get("confidence", 0)) or 0), 1),
                "industry": metrics.industry if metrics else "",
                "industry_state": metrics.industry_state if metrics else "NEUTRAL",
                "industry_rank_pct": metrics.industry_rank_pct if metrics else 0.0,
                "is_industry_leader": metrics.is_industry_leader if metrics else False,
                "concept_keys": list(metrics.concept_keys) if metrics else [],
                "concept_states": list(metrics.concept_states) if metrics else [],
                "rotation_score": metrics.rotation_score if metrics else 50.0,
                "flags": list(sig.get("flags") or [])[:12],
                "signal_type": sig.get("signal_type"),
                "horizon": sig.get("horizon"),
            })
        return out

    # --- response parsing -----------------------------------------------

    @staticmethod
    def _parse_response(raw: str) -> dict | None:
        if not raw:
            return None
        text = raw.strip()
        # Strip Markdown fences if the LLM added them
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        # First try the whole string
        for candidate in (text, AllocationAdvisor._extract_json_object(text)):
            if not candidate:
                continue
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _extract_json_object(text: str) -> str | None:
        """Greedy extract the first balanced JSON object from text."""
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start: i + 1]
        return None

    def _plan_from_payload(
        self,
        payload: dict,
        context: AllocationContext,
        provider_name: str,
    ) -> AllocationPlan:
        tiers: dict[str, list[TierRecommendation]] = {t: [] for t in TIER_ORDER}
        seen: set[str] = set()
        for rec in payload.get("recommendations") or []:
            tk = str(rec.get("ticker", "")).strip()
            if not tk or tk in seen:
                continue
            tier = (rec.get("tier") or "").upper().strip()
            if tier not in TIER_ORDER:
                tier = "C"
            pct = self._clamp_pct(rec.get("suggested_pct"), tier)
            metrics = context.rotation_metrics.get(tk)
            rotation = metrics.rotation_score if metrics else 50.0
            tiers[tier].append(TierRecommendation(
                ticker=tk,
                tier=tier,
                suggested_pct=pct,
                reasoning=str(rec.get("reasoning") or "")[:240],
                rotation_score=rotation,
            ))
            seen.add(tk)

        # Phase 4.50 — signals the LLM forgot are NOT backfilled at C 0%.
        # That backfill historically caused 100+ rows of "tier C 0%" to leak
        # into the plan and downstream consumers treated them as picks.
        # If the user wants to see "what was scanned but not picked", that
        # belongs in a separate watchlist UI, not the allocation plan.

        for t in TIER_ORDER:
            tiers[t].sort(key=lambda r: (-r.suggested_pct, -r.rotation_score))

        return AllocationPlan(
            tiers=tiers,
            warnings=context.concentration.warnings,
            summary=str(payload.get("summary") or "").strip()[:400] or self._auto_summary(tiers),
            provider=provider_name,
            snapshot_date=context.snapshot_date,
        )

    @staticmethod
    def _clamp_pct(value, tier: str) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = 0.0
        if v < 0:
            v = 0.0
        caps = {"S": 30.0, "A": 18.0, "B": 10.0, "C": 3.0}
        return round(min(v, caps.get(tier, 30.0)), 1)

    # --- fallback when LLM unavailable ----------------------------------

    def _fallback_plan(self, context: AllocationContext, reason: str) -> AllocationPlan:
        """Phase 4.50 — cap fallback to top FALLBACK_TOP_N picks by confidence.

        Before: processed ALL signals → 200-300 fake A/B-tier recs written
        to DB whenever LLM failed. Now: take only the top-N by confidence,
        skip the rest (downstream HoldingsManager will surface them as
        watchlist if needed).
        """
        FALLBACK_TOP_N = 25
        # Take top-N by confidence so fallback can never balloon
        ranked = sorted(
            context.signals,
            key=lambda s: -float(s.get("confidence", 0) or 0),
        )[:FALLBACK_TOP_N]

        tiers: dict[str, list[TierRecommendation]] = {t: [] for t in TIER_ORDER}
        for sig in ranked:
            tk = str(sig.get("ticker", ""))
            metrics = context.rotation_metrics.get(tk)
            tier, pct = self._rule_based_tier(sig, metrics)
            # Skip if rule says 0% (C-tier observational) — don't pollute DB
            if pct <= 0:
                continue
            tiers[tier].append(TierRecommendation(
                ticker=tk,
                tier=tier,
                suggested_pct=pct,
                reasoning=self._rule_based_reason(sig, metrics, tier),
                rotation_score=metrics.rotation_score if metrics else 50.0,
            ))
        for t in TIER_ORDER:
            tiers[t].sort(key=lambda r: (-r.suggested_pct, -r.rotation_score))
        return AllocationPlan(
            tiers=tiers,
            warnings=context.concentration.warnings,
            summary=self._auto_summary(tiers) + f"（LLM 不可用，規則式 top {FALLBACK_TOP_N}：{reason}）",
            provider=f"fallback:{reason}",
            snapshot_date=context.snapshot_date,
        )

    @staticmethod
    def _rule_based_tier(sig: dict, metrics: RotationMetrics | None) -> tuple[str, float]:
        action = sig.get("action")
        conf = float(sig.get("confidence", 0) or 0)
        tail = metrics.has_tailwind if metrics else False
        head = metrics.has_headwind if metrics else False
        leader = metrics.is_industry_leader if metrics else False

        if action == "LONG" and tail and conf >= 75 and leader:
            return "S", 25.0
        if action == "LONG" and tail and conf >= 70:
            return "A", 15.0
        if action in {"LONG", "WATCH"} and tail and conf >= 65:
            return "B", 8.0
        if head:
            return "C", 0.0
        if action == "LONG" and conf >= 65:
            return "B", 5.0
        return "C", 0.0

    @staticmethod
    def _rule_based_reason(
        sig: dict, metrics: RotationMetrics | None, tier: str,
    ) -> str:
        ind = metrics.industry if metrics else ""
        state = metrics.industry_state if metrics else "NEUTRAL"
        concepts = ", ".join(metrics.concept_keys[:2]) if metrics and metrics.concept_keys else "無題材"
        leader = "領頭羊" if metrics and metrics.is_industry_leader else ""
        conf = sig.get("confidence", 0)
        return (
            f"{tier} 級 ｜ {sig.get('action')} ｜ 信心 {conf} ｜ "
            f"產業 {ind}({state}) ｜ {concepts} ｜ {leader}"
        ).strip()

    @staticmethod
    def _auto_summary(tiers: Mapping[str, Iterable[TierRecommendation]]) -> str:
        counts = {t: len(list(tiers.get(t, []))) for t in TIER_ORDER}
        return (
            f"S 首選 {counts['S']} 支、A 強勢 {counts['A']} 支、"
            f"B 試單 {counts['B']} 支、C 觀察 {counts['C']} 支。"
        )
