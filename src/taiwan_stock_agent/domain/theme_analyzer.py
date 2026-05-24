"""LLM-driven daily theme analyzer.

Reads: heat map + concept heat + international signals + news headlines
Outputs:
  - 1-2 sentence narrative ("今日市場主軸：AI 供應鏈延續，半導體領漲，傳產走弱")
  - dominant_themes: ranked list of concepts/industries to watch
  - suggested_tickers_to_watch: candidate tickers based on theme alignment
  - new_concept_suggestions: LLM proposes new concepts if news mentions them

Designed to be cheap (one LLM call per day) and graceful (works without LLM).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path

from taiwan_stock_agent.domain.llm_provider import LLMProvider, create_llm_provider
from taiwan_stock_agent.domain.market_heat import MarketHeat
from taiwan_stock_agent.domain.concept_heat import ConceptHeatSnapshot
from taiwan_stock_agent.domain.international_signals import InternationalSignals

logger = logging.getLogger(__name__)


@dataclass
class ThemeAnalysis:
    snapshot_date: date
    narrative: str                          # 3-5 sentence market summary
    dominant_themes: list[str]              # ordered concept_keys + industry names
    avoid_themes: list[str]                 # cooling/declining
    rotation_call: str                      # "→ 半導體 → 光電業" etc
    risk_alert: str = ""                    # 市場風險警示
    new_concept_suggestions: list[dict] = field(default_factory=list)
    raw_llm_response: str = ""

    def to_dict(self) -> dict:
        return {
            "snapshot_date": self.snapshot_date.isoformat(),
            **{k: v for k, v in asdict(self).items() if k != "snapshot_date"},
        }


# ── Fallback (no LLM available) ─────────────────────────────────────────────

def _fallback_analysis(
    snapshot_date: date,
    heat: MarketHeat,
    concept_snap: ConceptHeatSnapshot,
    intl: InternationalSignals | None,
) -> ThemeAnalysis:
    """Deterministic narrative when LLM unavailable."""
    sorted_inds = sorted(heat.industries.values(), key=lambda x: x.rank_5d)
    top3 = [ih.industry for ih in sorted_inds[:3]]
    sorted_concepts = sorted(concept_snap.concepts.values(), key=lambda x: -x.ret_5d_pct)
    top_concepts = [c.name_zh for c in sorted_concepts[:3]]

    rot_part = ""
    if heat.rotating_up:
        rot_part = f"，輪入: {', '.join(heat.rotating_up[:3])}"
    if heat.rotating_down:
        rot_part += f"；輪出: {', '.join(heat.rotating_down[:3])}"

    intl_part = ""
    if intl and intl.tailwinds.narrative:
        intl_part = " | " + intl.tailwinds.narrative[0]

    narrative = (
        f"市場主軸：{', '.join(top3)} 領漲；題材聚焦 {', '.join(top_concepts)}{rot_part}{intl_part}"
    )

    return ThemeAnalysis(
        snapshot_date=snapshot_date,
        narrative=narrative,
        dominant_themes=top3 + top_concepts,
        avoid_themes=heat.cold_industries[:3] + [
            concept_snap.concepts[k].name_zh for k in concept_snap.cold_concepts
            if k in concept_snap.concepts
        ][:2],
        rotation_call=" → ".join(top3),
    )


# ── LLM prompt construction ─────────────────────────────────────────────────

def _build_prompt(
    heat: MarketHeat,
    concept_snap: ConceptHeatSnapshot,
    intl: InternationalSignals | None,
    headlines: list[str] | None,
) -> str:
    """Build a structured prompt for the LLM."""
    industries_lines = []
    for ih in sorted(heat.industries.values(), key=lambda x: x.rank_5d)[:10]:
        industries_lines.append(
            f"  #{ih.rank_5d} {ih.industry}: 5d {ih.ret_5d_pct:+.2f}%, "
            f"1d {ih.ret_1d_pct:+.2f}%, 廣度 {ih.breadth_above_ma20_pct:.0f}%, "
            f"加速 {ih.acceleration_pct:+.2f}, 領頭: {','.join(ih.leaders[:2])}"
        )

    concepts_lines = []
    for c in sorted(concept_snap.concepts.values(), key=lambda x: x.rank_5d)[:10]:
        concepts_lines.append(
            f"  #{c.rank_5d} {c.name_zh}: 5d {c.ret_5d_pct:+.2f}%, "
            f"1d {c.ret_1d_pct:+.2f}%, 領頭: {','.join(c.leaders[:2])}"
        )

    intl_lines = []
    if intl:
        for a in intl.assets[:6]:
            intl_lines.append(f"  {a.name}: 隔夜 {a.chg_pct:+.2f}%, 5d {a.chg_pct_5d:+.2f}%")
        for c, s in sorted(intl.tailwinds.concept_tailwinds.items(), key=lambda x: -x[1])[:3]:
            intl_lines.append(f"  順風: {c} {s:+d}")

    news_part = ""
    if headlines:
        top_news = "\n".join(f"  - {h}" for h in headlines[:8])
        news_part = f"\n## 今日主要新聞\n{top_news}\n"

    return f"""你是一個台股市場分析師。根據以下今日市場數據，產生簡潔的市場主軸分析。

## 產業熱度排行（按 5d 動量）
{chr(10).join(industries_lines)}

## 概念股 basket 熱度
{chr(10).join(concepts_lines)}

## 國際隔夜訊號
{chr(10).join(intl_lines) if intl_lines else '  (無資料)'}
{news_part}

## 要求

請以 JSON 格式回應，不要有任何 markdown 區塊標記:
{{
  "narrative": "用 3-5 句話描述今日市場主軸。第一句說明整體市場情緒與強弱。第二句點出 2-3 個最強題材及其具體表現（如漲幅、連漲天數）。第三句說明資金輪動方向與背後邏輯（國際訊號或基本面催化劑）。第四五句補充警示或機會（若大盤偏弱需提醒風險，若有新趨勢需點出）",
  "dominant_themes": ["最熱的 3-5 個概念或產業，由強到弱，每個格式為 '名稱（理由）'"],
  "avoid_themes": ["建議避開的 2-3 個題材，每個格式為 '名稱（原因）'"],
  "rotation_call": "具體說明輪動路徑與預期時間：'A（已啟動）→ B（蓄積中，預期X週內）→ C（候補）'",
  "risk_alert": "2-3句說明當前市場主要風險：包括技術面警示（如大盤乖離過大）、基本面不確定性（如財報季/Fed決策）、個股層面注意事項（如高檔量縮）",
  "new_concept_suggestions": [
    {{"name_zh": "概念名稱", "rationale": "為何納入", "candidate_tickers": ["2330","2454"]}}
  ]
}}

注意：
- narrative 要具體引用數字（如漲幅 X%、連漲 X 日），避免「市場震盪、看法分歧」這種廢話
- dominant_themes 要說明為何強（不只列名稱）
- risk_alert 要說明觸發條件，不是籠統警語
- new_concept_suggestions 只在新聞中明顯有未涵蓋的新題材時才提出，否則回 []
- 全部用繁體中文
"""


def _parse_llm_json(text: str) -> dict | None:
    """Robust JSON extraction from LLM response."""
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip markdown code fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Extract first JSON object
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


# ── Public API ──────────────────────────────────────────────────────────────

def analyze_themes(
    snapshot_date: date,
    heat: MarketHeat,
    concept_snap: ConceptHeatSnapshot,
    intl: InternationalSignals | None = None,
    headlines: list[str] | None = None,
    llm: LLMProvider | None = None,
) -> ThemeAnalysis:
    """Produce daily theme analysis. Falls back to deterministic if no LLM."""
    if llm is None:
        llm = create_llm_provider()

    if llm is None:
        logger.info("No LLM provider available, using deterministic fallback")
        return _fallback_analysis(snapshot_date, heat, concept_snap, intl)

    prompt = _build_prompt(heat, concept_snap, intl, headlines)
    try:
        raw = llm.complete(prompt, max_tokens=2000)
    except Exception as e:
        logger.warning("LLM call failed: %s. Using fallback.", e)
        return _fallback_analysis(snapshot_date, heat, concept_snap, intl)

    parsed = _parse_llm_json(raw)
    if not parsed:
        logger.warning("Failed to parse LLM response, using fallback")
        fa = _fallback_analysis(snapshot_date, heat, concept_snap, intl)
        fa.raw_llm_response = raw[:500]
        return fa

    return ThemeAnalysis(
        snapshot_date=snapshot_date,
        narrative=parsed.get("narrative", ""),
        dominant_themes=parsed.get("dominant_themes", []),
        avoid_themes=parsed.get("avoid_themes", []),
        rotation_call=parsed.get("rotation_call", ""),
        risk_alert=parsed.get("risk_alert", ""),
        new_concept_suggestions=parsed.get("new_concept_suggestions", []),
        raw_llm_response=raw[:500],
    )


def save_theme_analysis(analysis: ThemeAnalysis, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"theme_analysis_{analysis.snapshot_date.isoformat()}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(analysis.to_dict(), f, ensure_ascii=False, indent=2)
    return path
