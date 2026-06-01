"""每日題材深度解讀。

讀取 concept_heat + market_heat + theme_analysis JSON，
用 LLM 為前 5 強題材各產出 2 句解讀 + 1 句來源引用（避免幻覺）。
數字完全由系統提供，LLM 只負責解讀邏輯與引用已知來源。

Usage:
    python scripts/theme_brief.py
    make brief
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taiwan_stock_agent.domain.llm_provider import create_llm_provider

_ROOT = Path(__file__).resolve().parents[1]
_HEAT_DIR = _ROOT / "data" / "market_heat"
_BRIEF_DIR = _ROOT / "data" / "theme_briefs"

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.rule import Rule
    _console = Console()
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False
    _console = None


# ── helpers ──────────────────────────────────────────────────────────────────

def _find_latest_json(prefix: str, d: date | None = None) -> dict | None:
    """Load the most recent JSON matching prefix in _HEAT_DIR."""
    if d is not None:
        p = _HEAT_DIR / f"{prefix}{d.isoformat()}.json"
        if p.exists():
            return json.loads(p.read_text())
    # search last 7 days
    today = date.today()
    for i in range(7):
        p = _HEAT_DIR / f"{prefix}{(today - timedelta(days=i)).isoformat()}.json"
        if p.exists():
            return json.loads(p.read_text())
    return None


def _load_top_concepts(n: int = 5) -> list[dict]:
    snap = _find_latest_json("concept_heat_")
    if not snap:
        return []
    concepts = snap.get("concepts", {})
    items = list(concepts.values())
    items.sort(key=lambda x: x.get("ret_5d_pct", 0), reverse=True)
    return items[:n]


def _load_theme_narrative() -> str:
    """Load existing LLM narrative from theme_analysis if available."""
    ta = _find_latest_json("theme_analysis_")
    if ta:
        return ta.get("narrative", "")
    return ""


def _load_hot_industries() -> list[str]:
    heat = _find_latest_json("market_heat_")
    if not heat:
        return []
    inds = heat.get("industries", {})
    sorted_inds = sorted(inds.values(), key=lambda x: -x.get("rank_pct", 0))
    return [i.get("industry", "") for i in sorted_inds[:5]]


# ── LLM prompt ───────────────────────────────────────────────────────────────

def _build_brief_prompt(top_concepts: list[dict], narrative: str, hot_industries: list[str]) -> str:
    concept_block = ""
    for c in top_concepts:
        leaders = ", ".join(c.get("leaders", [])[:3])
        concept_block += (
            f"- **{c.get('name_zh','?')}**：5d {c.get('ret_5d_pct',0):+.1f}%，"
            f"20d {c.get('ret_20d_pct',0):+.1f}%，"
            f"breadth {c.get('breadth_above_ma20_pct',0):.0f}%，"
            f"成員 {c.get('n_tickers',0)} 檔，領頭股：{leaders}\n"
        )

    hot_inds_str = "、".join(hot_industries[:5]) if hot_industries else "（無資料）"
    narrative_line = f"\n## 今日 LLM 市場主軸（供參考）\n{narrative}\n" if narrative else ""

    return f"""你是台股市場分析師。以下是今日各題材的**真實量化數據**（由本系統計算，非推測）。

## 今日最強題材（依 5 日漲幅排序）
{concept_block}
## 今日最強產業
{hot_inds_str}
{narrative_line}

## 任務

為上述每個題材，產出：
1. **兩句解讀**：說明強弱背後邏輯（結合國際需求、供應鏈位置、近期催化劑）
2. **一句來源引用**：引用你確定真實存在的一個具體資訊來源（新聞標題、機構報告、財報說法）
   - 格式：「引用原文（中文或英文）」— 來源名稱，年份
   - 如果你對某題材沒有把握，請直接寫 `— 需人工查核（LLM 知識截止限制）`
   - **絕對不要捏造不存在的 URL 或文章標題**

回應格式為 JSON（不加任何 markdown 圍欄）：
{{
  "themes": [
    {{
      "name_zh": "題材中文名",
      "interpretation": "兩句解讀...",
      "citation_text": "引用原文",
      "citation_source": "來源名稱，年份"
    }}
  ],
  "risk_note": "整體風險提示（1-2句）"
}}

注意：數字請從上方量化數據引用，不要自己捏造漲跌幅。全部繁體中文。"""


def _parse_llm_response(raw: str) -> dict | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ── fallback (no LLM) ────────────────────────────────────────────────────────

def _fallback_brief(top_concepts: list[dict]) -> dict:
    themes = []
    for c in top_concepts:
        themes.append({
            "name_zh": c.get("name_zh", "?"),
            "interpretation": (
                f"5 日漲幅 {c.get('ret_5d_pct',0):+.1f}%，20 日 {c.get('ret_20d_pct',0):+.1f}%。"
                f"Breadth {c.get('breadth_above_ma20_pct',0):.0f}%，籌碼面可觀察。"
            ),
            "citation_text": "",
            "citation_source": "需人工查核（LLM 不可用）",
        })
    return {"themes": themes, "risk_note": ""}


# ── output ───────────────────────────────────────────────────────────────────

def _format_markdown(parsed: dict, snapshot_date: date) -> str:
    lines = [f"## 題材深度解讀｜{snapshot_date.isoformat()}\n"]
    for t in parsed.get("themes", []):
        name = t.get("name_zh", "?")
        interp = t.get("interpretation", "")
        ctext = t.get("citation_text", "")
        csrc = t.get("citation_source", "")

        lines.append(f"### {name}")
        lines.append(f"{interp}")
        ctext_clean = ctext.strip("「」\"'")
        if ctext_clean:
            lines.append(f"\n> 「{ctext_clean}」")
            lines.append(f"> — {csrc}\n")
        else:
            lines.append(f"\n> — {csrc}\n")

    risk = parsed.get("risk_note", "")
    if risk:
        lines.append(f"---\n**⚠ 風險提示：** {risk}")
    return "\n".join(lines)


def _print_rich(parsed: dict, snapshot_date: date) -> None:
    if not _HAS_RICH:
        print(_format_markdown(parsed, snapshot_date))
        return

    _console.print(Rule(f"📊 題材深度解讀 {snapshot_date.isoformat()}", style="bold cyan"))
    for t in parsed.get("themes", []):
        name = t.get("name_zh", "?")
        interp = t.get("interpretation", "")
        ctext = t.get("citation_text", "")
        csrc = t.get("citation_source", "")

        needs_check = "需人工查核" in csrc or not ctext
        citation_color = "yellow" if needs_check else "green"

        body = f"{interp}\n\n"
        # Strip any wrapping quotes the LLM may have added
        ctext_clean = ctext.strip("「」\"'")
        if ctext_clean:
            body += f"[{citation_color}]「{ctext_clean}」[/{citation_color}]\n"
            body += f"[{citation_color}]— {csrc}[/{citation_color}]"
        else:
            body += f"[{citation_color}]— {csrc}[/{citation_color}]"

        _console.print(Panel(body, title=f"[bold]{name}[/bold]", border_style="cyan", padding=(0, 1)))

    risk = parsed.get("risk_note", "")
    if risk:
        _console.print(f"\n[bold yellow]⚠ 風險提示：[/bold yellow] {risk}")


def _push_telegram(md_text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        # Telegram MarkdownV2 is picky; use plain text with truncation
        plain = re.sub(r"[#*`>_\[\]]", "", md_text)[:4000]
        requests.post(url, data={"chat_id": chat_id, "text": plain}, timeout=10)
    except Exception as e:
        print(f"Telegram push failed: {e}", file=sys.stderr)


# ── main ─────────────────────────────────────────────────────────────────────

def run(target_date: date | None = None, push_telegram: bool = False) -> int:
    today = target_date or date.today()

    top_concepts = _load_top_concepts(5)
    if not top_concepts:
        print("No concept heat data — run make heat-update first", file=sys.stderr)
        return 1

    narrative = _load_theme_narrative()
    hot_industries = _load_hot_industries()

    llm = create_llm_provider()
    if llm is not None:
        prompt = _build_brief_prompt(top_concepts, narrative, hot_industries)
        try:
            raw = llm.complete(prompt, max_tokens=2000)
            parsed = _parse_llm_response(raw)
        except Exception as e:
            print(f"LLM failed: {e}, using fallback", file=sys.stderr)
            parsed = None
        if not parsed:
            parsed = _fallback_brief(top_concepts)
    else:
        parsed = _fallback_brief(top_concepts)

    _print_rich(parsed, today)

    # Save markdown
    _BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _BRIEF_DIR / f"theme_brief_{today.isoformat()}.md"
    md = _format_markdown(parsed, today)
    out_path.write_text(md, encoding="utf-8")
    if _HAS_RICH:
        _console.print(f"\n  [dim]📄 已儲存：{out_path}[/dim]")
    else:
        print(f"Saved: {out_path}")

    if push_telegram:
        _push_telegram(md)

    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="每日題材深度解讀")
    ap.add_argument("--date", help="指定日期 YYYY-MM-DD")
    ap.add_argument("--telegram", action="store_true", help="推送 Telegram")
    args = ap.parse_args()

    target = date.fromisoformat(args.date) if args.date else None
    sys.exit(run(target_date=target, push_telegram=args.telegram))
