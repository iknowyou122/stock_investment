"""AI-driven parameter optimization agent.

Workflow:
    1. Run settle (fill T+1/T+3/T+5 outcomes)
    2. Run factor_report (writes data/factor_reports/factor_report_{date}.json)
    3. Read that JSON — no terminal output parsing needed
    4. Call LLM API with current params + factor data
    5. Validate changes (whitelist + ±20% cap)
    6. confidence >= 75: apply immediately
       confidence < 75: save to pending_change.json, notify for /approve

Usage (called by bot.py):
    result = await run_optimize(llm_name, send_telegram_fn)
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.utils.param_safety import validate_changes, apply_changes

_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _ROOT / "config"
_PARAMS_PATH = _CONFIG / "engine_params.json"
_HISTORY_PATH = _CONFIG / "param_history.json"
_PENDING_PATH = _CONFIG / "pending_change.json"
_REPORT_DIR = _ROOT / "data" / "factor_reports"
_CONFIDENCE_THRESHOLD = 75
_MIN_SAMPLE_WARNING = "0 筆"


def _run_subprocess(cmd: list[str]) -> tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(_ROOT))
    return result.returncode, result.stdout + result.stderr


def _load_factor_report() -> dict | None:
    """Load today's factor report JSON. Returns None if not found."""
    path = _REPORT_DIR / f"factor_report_{date.today()}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _build_prompt(params: dict, factor_data: dict) -> str:
    whitelist = params.get("tunable_whitelist", [])
    current = {k: params[k] for k in whitelist if k in params}
    return f"""你是台股短線信號引擎的優化顧問。

【當前參數】
{json.dumps(current, indent=2, ensure_ascii=False)}

【可調整參數白名單】
{json.dumps(whitelist, ensure_ascii=False)}

【因子分析報告】
Lift 分析（各 flag 對勝率的影響）：
{json.dumps(factor_data.get('lift_analysis', []), indent=2, ensure_ascii=False)}

Grid Search Top 5：
{json.dumps(factor_data.get('grid_search_top5', [])[:5], indent=2, ensure_ascii=False)}

殘差分析：
{json.dumps(factor_data.get('residual_suggestions', []), ensure_ascii=False)}

【任務】
1. 指出哪些因子表現偏弱（lift < 1.0）
2. 提出具體參數調整（必須在白名單內，每個參數最多 ±20%）
3. 每個調整說明理由和預期改善
4. 給出整體信心分數（0–100）；若樣本數不足，信心不超過 40

【輸出格式】只輸出 JSON，不要其他文字：
{{
  "confidence": 82,
  "changes": [
    {{"param": "rsi_momentum_hi", "from": 55, "to": 58, "reason": "lift 低，提高門檻減少假訊號"}}
  ],
  "summary": "本次調整重點：..."
}}"""


def _call_claude(prompt: str) -> dict | None:
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(msg.content[0].text)
    except Exception as e:
        print(f"Claude API error: {e}")
        return None


def _call_gemini(prompt: str) -> dict | None:
    try:
        import google.generativeai as genai
        model = genai.GenerativeModel("gemini-2.5-flash")
        resp = model.generate_content(prompt)
        return json.loads(resp.text)
    except Exception as e:
        print(f"Gemini API error: {e}")
        return None


def _call_openai(prompt: str) -> dict | None:
    try:
        from openai import OpenAI
        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"OpenAI API error: {e}")
        return None


def _call_llm(llm_name: str, params: dict, factor_data: dict) -> dict | None:
    prompt = _build_prompt(params, factor_data)
    if llm_name == "gemini":
        return _call_gemini(prompt)
    elif llm_name == "openai":
        return _call_openai(prompt)
    else:
        return _call_claude(prompt)


async def run_optimize(llm_name: str, notify_fn) -> str:
    """Run the full optimization pipeline. notify_fn(msg) sends a Telegram message.

    Returns a status string for logging.
    """
    # Step 1: settle
    code, out = _run_subprocess([sys.executable, "scripts/daily_runner.py", "settle"])
    if code != 0 or _MIN_SAMPLE_WARNING in out:
        await notify_fn("🤖 優化 Agent：資料不足，本次跳過優化\n（settle 失敗或無結算訊號）")
        return "skipped: settle failed"

    # Step 2: factor_report (writes JSON as side effect)
    code, out = _run_subprocess([sys.executable, "scripts/factor_report.py"])
    if code != 0:
        await notify_fn(f"🤖 優化 Agent：factor\\_report 失敗\n```\n{out[:300]}\n```")
        return "skipped: factor_report failed"

    # Step 3: read the JSON it produced
    factor_data = _load_factor_report()
    if factor_data is None:
        await notify_fn("🤖 優化 Agent：找不到今日 factor report JSON")
        return "skipped: no factor report json"

    # Step 4: call LLM
    params = json.loads(_PARAMS_PATH.read_text())
    decision = _call_llm(llm_name, params, factor_data)
    if decision is None:
        await notify_fn("🤖 優化 Agent：LLM API 呼叫失敗，跳過本次優化")
        return "skipped: llm error"

    confidence = decision.get("confidence", 0)
    changes = decision.get("changes", [])
    summary = decision.get("summary", "")

    # Step 5: validate
    ok, errors = validate_changes(changes, params)
    if not ok:
        await notify_fn(f"🤖 優化 Agent：變更驗證失敗，跳過\n" + "\n".join(errors))
        return "skipped: validation failed"

    # Step 6: apply or park
    if confidence >= _CONFIDENCE_THRESHOLD:
        apply_changes(changes, _PARAMS_PATH, _HISTORY_PATH)
        change_lines = "\n".join(
            f"  · {c['param']} {c['from']}→{c['to']}（{c.get('reason', '')}）"
            for c in changes
        )
        msg = (
            f"🤖 *優化報告* {date.today()}\n\n"
            f"📊 信心分數：{confidence}/100\n"
            f"🔧 已套用 {len(changes)} 項調整：\n{change_lines}\n\n"
            f"💬 {summary}"
        )
        _PENDING_PATH.write_text("null")
    else:
        _PENDING_PATH.write_text(json.dumps(
            {"confidence": confidence, "changes": changes, "summary": summary},
            ensure_ascii=False,
        ))
        change_lines = "\n".join(
            f"  · {c['param']} {c['from']}→{c['to']}" for c in changes
        )
        msg = (
            f"🤖 *優化建議*（待確認）{date.today()}\n\n"
            f"📊 信心分數：{confidence}/100（低於門檻 {_CONFIDENCE_THRESHOLD}，需手動確認）\n"
            f"建議調整：\n{change_lines}\n\n"
            f"💬 {summary}\n\n"
            f"回覆 /approve 套用，/rollback 取消"
        )

    await notify_fn(msg)
    return f"done: confidence={confidence}, changes={len(changes)}"
