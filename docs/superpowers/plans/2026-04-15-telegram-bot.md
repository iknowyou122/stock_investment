# Telegram Bot + AI Optimize Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Telegram bot daemon that auto-pushes intraday signals and post-market reports, plus an AI agent that tunes engine parameters on a schedule.

**Architecture:** A single `scripts/bot.py` async daemon uses `AsyncIOScheduler` (apscheduler 3.x) and `python-telegram-bot >= 21` on the same asyncio event loop. It orchestrates existing scripts via subprocess and pushes results to Telegram. `scripts/optimize_agent.py` runs `settle` + `factor_report`, reads the already-produced JSON from `data/factor_reports/`, calls the LLM API, and applies safe parameter changes.

**Tech Stack:** `python-telegram-bot>=21`, `apscheduler>=3.10,<4.0`, existing `anthropic`/`google-generativeai`/`openai` extras, `rich` (already in requirements), Python asyncio.

**Spec:** `docs/superpowers/specs/2026-04-15-telegram-bot-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/taiwan_stock_agent/utils/trading_calendar.py` | **Create** | `is_trading_day(d)` shared utility |
| `config/engine_params.json` | **Modify** | Add `tunable_whitelist` array |
| `config/param_history.json` | **Create** | Empty list `[]` — changelog |
| `config/pending_change.json` | **Create** | `null` — awaiting-approval state |
| `scripts/bot_setup.py` | **Create** | Interactive TG token + Chat ID setup → .env |
| `scripts/optimize_agent.py` | **Create** | settle → factor_report JSON → LLM → apply params |
| `scripts/bot.py` | **Create** | Main daemon: scheduler + Telegram + state |
| `tests/unit/test_bot_utils.py` | **Create** | Unit tests for formatters, state, param logic |
| `Makefile` | **Modify** | Add `bot-setup` and `bot` targets |
| `requirements.txt` | **Modify** | Add `python-telegram-bot>=21`, `apscheduler>=3.10,<4.0` |

---

## Task 1: Trading Calendar Utility

**Files:**
- Create: `src/taiwan_stock_agent/utils/trading_calendar.py`
- Create: `tests/unit/test_bot_utils.py`

> **Note:** `_is_trading_day` already exists in `scripts/backtest.py:48` and `scripts/daily_runner.py:194` as private functions. This task extracts a shared importable version. Bot uses `weekday() < 5` (Mon–Fri). TWSE irregular holidays are not handled at this stage — weekday check matches existing project behaviour.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_bot_utils.py
from datetime import date
from taiwan_stock_agent.utils.trading_calendar import is_trading_day

def test_weekday_is_trading_day():
    assert is_trading_day(date(2026, 4, 14)) is True   # Monday

def test_saturday_is_not_trading_day():
    assert is_trading_day(date(2026, 4, 18)) is False  # Saturday

def test_sunday_is_not_trading_day():
    assert is_trading_day(date(2026, 4, 19)) is False  # Sunday

def test_friday_is_trading_day():
    assert is_trading_day(date(2026, 4, 17)) is True   # Friday
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/unit/test_bot_utils.py -v
```
Expected: `ModuleNotFoundError: No module named 'taiwan_stock_agent.utils.trading_calendar'`

- [ ] **Step 3: Create the utility**

```python
# src/taiwan_stock_agent/utils/trading_calendar.py
from datetime import date


def is_trading_day(d: date) -> bool:
    """Return True if d is a Taiwan Stock Exchange trading day (Mon–Fri).

    Note: Does not handle TWSE irregular holidays (typhoon closures etc.).
    Consistent with existing project behaviour in backtest.py and daily_runner.py.
    """
    return d.weekday() < 5
```

Also create `src/taiwan_stock_agent/utils/__init__.py` if it does not exist:
```python
# src/taiwan_stock_agent/utils/__init__.py
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/unit/test_bot_utils.py -v
```
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/taiwan_stock_agent/utils/trading_calendar.py \
        src/taiwan_stock_agent/utils/__init__.py \
        tests/unit/test_bot_utils.py
git commit -m "feat: add is_trading_day shared utility (extracted from backtest/daily_runner)"
```

---

## Task 2: Config — Tunable Whitelist + State Files

**Files:**
- Modify: `config/engine_params.json`
- Create: `config/param_history.json`
- Create: `config/pending_change.json`

- [ ] **Step 1: Add `tunable_whitelist` to engine_params.json**

Open `config/engine_params.json` and add the `tunable_whitelist` key. The whitelist contains parameter names the LLM is allowed to change. All existing numeric params are tunable except thresholds that relate to data availability (`rsi_momentum_lo` excluded — too low breaks RSI logic):

```json
{
  "gate_vol_ratio": 1.2,
  "rsi_momentum_lo": 30,
  "rsi_momentum_hi": 55,
  "breakout_vol_ratio": 1.5,
  "sector_topN_pct": 0.2,
  "long_threshold_uptrend": 50,
  "long_threshold_neutral": 55,
  "long_threshold_downtrend": 60,
  "watch_min": 40,
  "emerging_setup_pts": 10,
  "pullback_setup_pts": 8,
  "bb_squeeze_coiling_pts": 3,
  "_comment": "Tunable parameters for Accumulation Engine (2026-04-13 recalibration).",
  "tunable_whitelist": [
    "gate_vol_ratio",
    "rsi_momentum_hi",
    "breakout_vol_ratio",
    "sector_topN_pct",
    "long_threshold_uptrend",
    "long_threshold_neutral",
    "long_threshold_downtrend",
    "watch_min",
    "emerging_setup_pts",
    "pullback_setup_pts",
    "bb_squeeze_coiling_pts"
  ]
}
```

- [ ] **Step 2: Create param_history.json**

```bash
echo '[]' > config/param_history.json
```

- [ ] **Step 3: Create pending_change.json**

```bash
echo 'null' > config/pending_change.json
```

- [ ] **Step 4: Write test for param whitelist loading**

Add to `tests/unit/test_bot_utils.py`:

```python
import json
from pathlib import Path

def test_engine_params_has_tunable_whitelist():
    params_path = Path(__file__).parents[2] / "config" / "engine_params.json"
    params = json.loads(params_path.read_text())
    assert "tunable_whitelist" in params
    whitelist = params["tunable_whitelist"]
    assert isinstance(whitelist, list)
    assert len(whitelist) >= 5
    # Every whitelisted key must exist as a numeric param
    for key in whitelist:
        assert key in params
        assert isinstance(params[key], (int, float))
```

- [ ] **Step 5: Run test**

```bash
.venv/bin/pytest tests/unit/test_bot_utils.py::test_engine_params_has_tunable_whitelist -v
```
Expected: PASSED

- [ ] **Step 6: Commit**

```bash
git add config/engine_params.json config/param_history.json config/pending_change.json tests/unit/test_bot_utils.py
git commit -m "feat: add tunable_whitelist to engine_params, create param_history and pending_change state files"
```

---

## Task 3: Parameter Safety Functions

**Files:**
- Create: `src/taiwan_stock_agent/utils/param_safety.py`
- Test: `tests/unit/test_bot_utils.py`

These functions are the safety layer for `optimize_agent.py`. Test them in isolation before wiring into the agent.

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_bot_utils.py`:

```python
from taiwan_stock_agent.utils.param_safety import validate_changes, apply_changes, rollback_params
import json
from pathlib import Path
import tempfile, os

_SAMPLE_PARAMS = {
    "gate_vol_ratio": 1.2,
    "rsi_momentum_hi": 55,
    "watch_min": 40,
    "tunable_whitelist": ["gate_vol_ratio", "rsi_momentum_hi", "watch_min"],
}

def test_validate_changes_rejects_non_whitelisted():
    changes = [{"param": "rsi_momentum_lo", "from": 30, "to": 25}]
    ok, errors = validate_changes(changes, _SAMPLE_PARAMS)
    assert not ok
    assert any("whitelist" in e for e in errors)

def test_validate_changes_rejects_large_delta():
    # 55 * 1.21 = 66.55 → exceeds ±20%
    changes = [{"param": "rsi_momentum_hi", "from": 55, "to": 70}]
    ok, errors = validate_changes(changes, _SAMPLE_PARAMS)
    assert not ok
    assert any("20%" in e for e in errors)

def test_validate_changes_accepts_small_delta():
    changes = [{"param": "rsi_momentum_hi", "from": 55, "to": 58}]
    ok, errors = validate_changes(changes, _SAMPLE_PARAMS)
    assert ok
    assert errors == []

def test_apply_changes_writes_history(tmp_path):
    params_file = tmp_path / "engine_params.json"
    history_file = tmp_path / "param_history.json"
    params_file.write_text(json.dumps(_SAMPLE_PARAMS))
    history_file.write_text("[]")

    changes = [{"param": "rsi_momentum_hi", "from": 55, "to": 58, "reason": "test"}]
    apply_changes(changes, params_path=params_file, history_path=history_file)

    updated = json.loads(params_file.read_text())
    assert updated["rsi_momentum_hi"] == 58

    history = json.loads(history_file.read_text())
    assert len(history) == 1
    assert history[0]["changes"] == changes
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/unit/test_bot_utils.py -k "param" -v
```
Expected: `ImportError`

- [ ] **Step 3: Implement param_safety.py**

```python
# src/taiwan_stock_agent/utils/param_safety.py
"""Safe parameter change validation and application for optimize_agent."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_DEFAULT_PARAMS = _CONFIG_DIR / "engine_params.json"
_DEFAULT_HISTORY = _CONFIG_DIR / "param_history.json"
_HISTORY_LIMIT = 100  # rotate after this many entries


def validate_changes(
    changes: list[dict],
    current_params: dict,
) -> tuple[bool, list[str]]:
    """Validate a list of LLM-proposed changes against whitelist and ±20% cap.

    Returns (ok, error_messages).
    """
    whitelist = set(current_params.get("tunable_whitelist", []))
    errors: list[str] = []

    for c in changes:
        param = c.get("param", "")
        to_val = c.get("to")
        from_val = current_params.get(param)

        if param not in whitelist:
            errors.append(f"'{param}' not in tunable whitelist")
            continue
        if from_val is None or to_val is None:
            errors.append(f"'{param}' missing from/to values")
            continue
        if from_val == 0:
            continue
        delta = abs(to_val - from_val) / abs(from_val)
        if delta > 0.20:
            errors.append(
                f"'{param}' change {from_val}→{to_val} exceeds ±20% cap ({delta:.0%})"
            )

    return len(errors) == 0, errors


def apply_changes(
    changes: list[dict],
    params_path: Path = _DEFAULT_PARAMS,
    history_path: Path = _DEFAULT_HISTORY,
) -> None:
    """Apply validated changes to engine_params.json and record in history."""
    params = json.loads(params_path.read_text())
    for c in changes:
        params[c["param"]] = c["to"]
    params_path.write_text(json.dumps(params, indent=2, ensure_ascii=False))

    history = json.loads(history_path.read_text()) if history_path.exists() else []
    history.append({"timestamp": datetime.now().isoformat(), "changes": changes})
    # Rotate history
    if len(history) > _HISTORY_LIMIT:
        history = history[-_HISTORY_LIMIT:]
    history_path.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def rollback_params(
    params_path: Path = _DEFAULT_PARAMS,
    history_path: Path = _DEFAULT_HISTORY,
) -> list[dict] | None:
    """Revert to previous params. Returns the reverted changes or None if no history."""
    history = json.loads(history_path.read_text()) if history_path.exists() else []
    if not history:
        return None
    last = history.pop()
    # Undo: restore 'from' values
    params = json.loads(params_path.read_text())
    for c in last["changes"]:
        params[c["param"]] = c["from"]
    params_path.write_text(json.dumps(params, indent=2, ensure_ascii=False))
    history_path.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    return last["changes"]
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/unit/test_bot_utils.py -k "param" -v
```
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/taiwan_stock_agent/utils/param_safety.py tests/unit/test_bot_utils.py
git commit -m "feat: param safety functions (whitelist validation, apply with history, rollback)"
```

---

## Task 4: Message Formatters + Hit Rate Calculator

**Files:**
- Create: `src/taiwan_stock_agent/utils/bot_formatters.py`
- Test: `tests/unit/test_bot_utils.py`

All Telegram message strings are built here — isolated from the bot daemon so they're easy to test.

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_bot_utils.py`:

```python
from taiwan_stock_agent.utils.bot_formatters import (
    format_opening_list,
    format_entry_signal,
    format_postmarket_report,
)

_SAMPLE_SIGNALS = [
    {
        "ticker": "6933", "name": "信驊", "action": "WATCH",
        "confidence": 59, "entry_bid": 320.0, "target": 358.0,
        "stop_loss": 312.0, "flags": "COILING_PRIME",
    },
    {
        "ticker": "3704", "name": "合一", "action": "WATCH",
        "confidence": 57, "entry_bid": 85.0, "target": 95.0,
        "stop_loss": 82.0, "flags": "EMERGING_SETUP",
    },
]

def test_format_opening_list_contains_ticker():
    msg = format_opening_list(_SAMPLE_SIGNALS, scan_date="2026-04-15")
    assert "6933" in msg
    assert "信驊" in msg

def test_format_opening_list_shows_coiling():
    msg = format_opening_list(_SAMPLE_SIGNALS, scan_date="2026-04-15")
    assert "蓄積" in msg or "COILING" in msg

def test_format_opening_list_empty():
    msg = format_opening_list([], scan_date="2026-04-15")
    assert "0" in msg or "無" in msg

def test_format_entry_signal():
    msg = format_entry_signal("6933", "信驊", price=322.0, entry_low=318.0, entry_high=328.0, stop=312.0)
    assert "6933" in msg
    assert "322" in msg
    assert "✅" in msg

def test_format_postmarket_report_hit_rate():
    yesterday = _SAMPLE_SIGNALS
    hits = [{"ticker": "6933", "triggered": True, "price": 322.0}]
    msg = format_postmarket_report(
        yesterday_signals=yesterday,
        intraday_hits=hits,
        tomorrow_signals=_SAMPLE_SIGNALS,
        report_date="2026-04-15",
    )
    assert "命中率" in msg
    assert "50%" in msg or "1/2" in msg or "1 檔" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/unit/test_bot_utils.py -k "format" -v
```
Expected: `ImportError`

- [ ] **Step 3: Implement bot_formatters.py**

```python
# src/taiwan_stock_agent/utils/bot_formatters.py
"""Telegram message formatters for the bot daemon."""
from __future__ import annotations


_COILING_FLAGS = {"COILING_PRIME", "COILING", "EMERGING_SETUP"}


def _action_emoji(action: str, flags: str = "") -> str:
    if any(f in (flags or "") for f in _COILING_FLAGS):
        return "⚡"
    if action == "BUY":
        return "🟢"
    if action == "WATCH":
        return "🟢"
    return "🟡"


def format_opening_list(signals: list[dict], scan_date: str) -> str:
    if not signals:
        return f"📋 開盤名單 {scan_date}\n\n無符合條件標的（0 檔）"

    coiling = [s for s in signals if any(f in (s.get("flags") or "") for f in _COILING_FLAGS)]
    lines = [f"📋 *開盤名單* {scan_date}\n"]
    for s in signals[:10]:
        emoji = _action_emoji(s["action"], s.get("flags", ""))
        flag_note = ""
        if s.get("flags"):
            first_flag = s["flags"].split("|")[0]
            flag_note = f"\n   ★ {first_flag}"
        lines.append(
            f"{emoji} *{s['ticker']}* {s.get('name','')}  conf:{s['confidence']}"
            f"  入場 {s['entry_bid']:.0f}  目標 {s['target']:.0f}  停損 {s['stop_loss']:.0f}"
            f"{flag_note}"
        )
    lines.append(f"\n共 {len(signals)} 檔入監控，蓄積待噴發 {len(coiling)} 檔")
    return "\n".join(lines)


def format_entry_signal(ticker: str, name: str, price: float, entry_low: float, entry_high: float, stop: float) -> str:
    in_zone = entry_low <= price <= entry_high
    status = "✅ 在入場區間" if in_zone else f"⏳ 等待（入場區 {entry_low:.0f}–{entry_high:.0f}）"
    return (
        f"🔔 *進場訊號*\n"
        f"*{ticker}* {name}  現價 {price:.0f} {status}\n"
        f"停損 {stop:.0f}"
    )


def format_postmarket_report(
    yesterday_signals: list[dict],
    intraday_hits: list[dict],
    tomorrow_signals: list[dict],
    report_date: str,
) -> str:
    total = len(yesterday_signals)
    hit_count = sum(1 for h in intraday_hits if h.get("triggered"))
    hit_rate = f"{hit_count}/{total} ({hit_count/total:.0%})" if total else "N/A"

    lines = [f"📈 *盤後報告* {report_date}\n"]
    lines.append("━━ 今日命中率 ━━")
    lines.append(f"昨日名單 {total} 檔 → {hit_count} 檔達進場條件 ({hit_rate})")

    for h in intraday_hits:
        icon = "✅" if h.get("triggered") else "⏳"
        lines.append(f"{icon} {h['ticker']}  現價 {h.get('price', '–')}")

    lines.append("\n━━ 隔日建倉名單 ━━")
    for s in tomorrow_signals[:8]:
        lines.append(
            f"🟢 *{s['ticker']}* {s.get('name','')}  conf:{s['confidence']}"
            f"  入場 {s['entry_bid']:.0f}  目標 {s['target']:.0f}  停損 {s['stop_loss']:.0f}"
        )

    coiling_tomorrow = [s for s in tomorrow_signals if any(f in (s.get("flags") or "") for f in _COILING_FLAGS)]
    if coiling_tomorrow:
        lines.append("\n━━ 蓄積待噴發（T-1/T-2 佈局）━━")
        for s in coiling_tomorrow[:4]:
            lines.append(f"⚡ *{s['ticker']}* {s.get('name','')}  {s.get('flags','')}")

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/unit/test_bot_utils.py -k "format" -v
```
Expected: 5 PASSED

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
.venv/bin/pytest tests/unit/ -q
```
Expected: All existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/taiwan_stock_agent/utils/bot_formatters.py tests/unit/test_bot_utils.py
git commit -m "feat: Telegram message formatters (opening list, entry signal, post-market report)"
```

---

## Task 5: optimize_agent.py

**Files:**
- Create: `scripts/optimize_agent.py`

The agent runs `settle`, then `factor_report` (which already writes JSON to `data/factor_reports/factor_report_{date}.json`), reads that JSON, calls the chosen LLM, validates changes, and applies or parks them for approval.

- [ ] **Step 1: Create optimize_agent.py**

```python
# scripts/optimize_agent.py
"""AI-driven parameter optimization agent.

Workflow:
    1. Run settle (fill T+1/T+3/T+5 outcomes)
    2. Run factor_report (writes data/factor_reports/factor_report_{date}.json)
    3. Read that JSON — no terminal output parsing needed
    4. Call LLM API with current params + factor data
    5. Validate changes (whitelist + ±20% cap)
    6. confidence >= 75: apply immediately
       confidence < 75: save to pending_change.json, notify bot for /approve

Usage (called by bot.py, not directly):
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
_MIN_SAMPLE_WARNING = "0 筆結算"


def _run_subprocess(cmd: list[str]) -> tuple[int, str]:
    """Run a subprocess and return (exit_code, combined_output)."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def _load_factor_report() -> dict | None:
    """Load today's factor report JSON. Returns None if not found."""
    today = date.today()
    path = _REPORT_DIR / f"factor_report_{today}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _call_llm(llm_name: str, params: dict, factor_data: dict) -> dict | None:
    """Call LLM and return parsed JSON response, or None on failure."""
    prompt = _build_prompt(params, factor_data)

    if llm_name == "gemini":
        return _call_gemini(prompt)
    elif llm_name == "openai":
        return _call_openai(prompt)
    else:
        return _call_claude(prompt)


def _build_prompt(params: dict, factor_data: dict) -> str:
    whitelist = params.get("tunable_whitelist", [])
    current = {k: params[k] for k in whitelist if k in params}
    n_signals = len(factor_data.get("lift_analysis", []))
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
1. 指出哪些因子表現偏弱（lift < 1.0 或 lift_analysis 中得分低）
2. 提出具體參數調整（必須在白名單內，每個參數最多 ±20%）
3. 每個調整說明理由和預期改善
4. 給出整體信心分數（0–100）
   - 若樣本數不足，信心不超過 40

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


async def run_optimize(llm_name: str, notify_fn) -> str:
    """Run the full optimization pipeline. notify_fn(msg) sends Telegram message.

    Returns a status string for logging.
    """
    # Step 1: settle
    code, out = _run_subprocess([sys.executable, "scripts/daily_runner.py", "settle"])
    if code != 0 or _MIN_SAMPLE_WARNING in out:
        msg = "🤖 優化 Agent：資料不足，本次跳過優化\n（settle 失敗或無結算訊號）"
        await notify_fn(msg)
        return "skipped: settle failed"

    # Step 2: factor_report
    code, out = _run_subprocess([sys.executable, "scripts/factor_report.py"])
    if code != 0:
        msg = f"🤖 優化 Agent：factor_report 失敗\n```\n{out[:300]}\n```"
        await notify_fn(msg)
        return "skipped: factor_report failed"

    # Step 3: read JSON
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
        err_text = "\n".join(errors)
        await notify_fn(f"🤖 優化 Agent：變更驗證失敗，跳過\n{err_text}")
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
        _PENDING_PATH.write_text(json.dumps({"confidence": confidence, "changes": changes, "summary": summary}))
        change_lines = "\n".join(
            f"  · {c['param']} {c['from']}→{c['to']}"
            for c in changes
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
```

- [ ] **Step 2: Smoke test (no API key needed)**

```bash
cd /Users/07683.howard.huang/Documents/code/stock_investment
.venv/bin/python -c "import scripts.optimize_agent; print('import OK')"
```
Expected: `import OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/optimize_agent.py
git commit -m "feat: optimize_agent.py (settle → factor_report JSON → LLM → safe apply)"
```

---

## Task 6: bot_setup.py

**Files:**
- Create: `scripts/bot_setup.py`

- [ ] **Step 1: Create bot_setup.py**

```python
# scripts/bot_setup.py
"""One-time interactive setup: Telegram token + Chat ID → .env

Usage:
    python scripts/bot_setup.py
    make bot-setup
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from rich.console import Console
    from rich.panel import Panel
    _con = Console()
except ImportError:
    _con = None  # type: ignore

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _print(msg: str, style: str = "") -> None:
    if _con:
        _con.print(msg, style=style)
    else:
        print(msg)


def _install_deps() -> None:
    _print("[1/3] 安裝依賴套件...", "bold")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "-q",
        "python-telegram-bot>=21",
        "apscheduler>=3.10,<4.0",
    ])
    _print("  ✅ 完成")


def _setup_telegram() -> tuple[str, str]:
    _print("\n[2/3] Telegram 設定", "bold")
    _print("  請先在 Telegram 搜尋 @BotFather 建立 Bot，取得 token")
    token = input("  Bot Token: ").strip()
    _print("  請在 Telegram 傳一則訊息給 Bot，然後開啟：")
    _print("  https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates")
    _print("  找到 message.chat.id（你的 Chat ID）")
    chat_id = input("  Chat ID: ").strip()
    return token, chat_id


def _test_message(token: str, chat_id: str) -> bool:
    import urllib.request
    import urllib.parse
    text = "✅ 股票信號機器人設定完成！"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        _print(f"  ❌ 發送失敗：{e}", "red")
        return False


def _write_env(token: str, chat_id: str) -> None:
    existing = _ENV_PATH.read_text() if _ENV_PATH.exists() else ""
    lines = [l for l in existing.splitlines() if not l.startswith("TELEGRAM_")]
    lines += [f"TELEGRAM_BOT_TOKEN={token}", f"TELEGRAM_CHAT_ID={chat_id}"]
    _ENV_PATH.write_text("\n".join(lines) + "\n")


def main() -> None:
    if _con:
        _con.print(Panel("股票信號機器人 — 初始設定", style="bold blue"))

    _install_deps()
    token, chat_id = _setup_telegram()

    _print("\n  測試訊息發送中...", "dim")
    if _test_message(token, chat_id):
        _print("  ✅ 收到測試訊息")
    else:
        _print("  ⚠ 測試失敗，請確認 token 和 chat_id 正確", "yellow")

    _print("\n[3/3] 寫入 .env...", "bold")
    _write_env(token, chat_id)
    _print("  ✅ 完成\n")
    _print("設定完成！執行 [bold]make bot[/bold] 啟動機器人")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add scripts/bot_setup.py
git commit -m "feat: bot_setup.py interactive one-time TG configuration"
```

---

## Task 7: bot.py — Core Daemon

**Files:**
- Create: `scripts/bot.py`

This is the main daemon. It wires AsyncIOScheduler + Telegram Application on the same event loop, manages in-memory state, and runs all scheduled jobs.

- [ ] **Step 1: Create bot.py**

```python
# scripts/bot.py
"""Telegram bot daemon for Taiwan stock signals.

Usage:
    python scripts/bot.py              # interactive LLM selection
    python scripts/bot.py --llm gemini # skip interactive, use gemini
    make bot
    make bot LLM=gemini
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from rich.console import Console
from rich.table import Table
from rich import box
from rich.live import Live

from taiwan_stock_agent.utils.trading_calendar import is_trading_day
from taiwan_stock_agent.utils.bot_formatters import (
    format_opening_list,
    format_entry_signal,
    format_postmarket_report,
)
from taiwan_stock_agent.utils.param_safety import validate_changes, apply_changes, rollback_params

_console = Console()
_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIR = _ROOT / "data" / "scans"
_HITS_DIR = _ROOT / "data" / "intraday_hits"
_PARAMS_PATH = _ROOT / "config" / "engine_params.json"
_PENDING_PATH = _ROOT / "config" / "pending_change.json"

# ── State ──────────────────────────────────────────────────────────────────
_state: dict = {
    "shortlist": [],          # list[dict] — today's top signals, max 20
    "monitoring_active": True,
    "last_scan_time": None,
    "llm": "claude",
    "scan_lock": None,        # asyncio.Lock, created in main()
    "precheck_lock": None,    # asyncio.Lock, created in main()
    "app": None,              # Telegram Application
    "chat_id": None,
}

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ── Telegram helpers ────────────────────────────────────────────────────────

async def _send(text: str) -> None:
    """Send a message to the configured chat."""
    try:
        await _state["app"].bot.send_message(
            chat_id=_state["chat_id"],
            text=text,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Telegram send error: {e}")


# ── CSV helpers ─────────────────────────────────────────────────────────────

def _latest_scan_csv(offset_days: int = 0) -> Path | None:
    """Find the scan CSV for today minus offset_days trading days."""
    candidate = date.today()
    skipped = 0
    while skipped <= offset_days + 7:
        if candidate.weekday() < 5:
            if skipped == offset_days:
                path = _SCAN_DIR / f"scan_{candidate}.csv"
                if path.exists():
                    return path
            skipped += 1
        candidate -= timedelta(days=1)
    return None


def _parse_scan_csv(path: Path, min_conf: int = 40, max_n: int = 20) -> list[dict]:
    """Parse scan CSV and return top signals sorted by confidence."""
    signals = []
    with open(path) as f:
        for row in csv.DictReader(f):
            if row.get("action") not in ("BUY", "WATCH"):
                continue
            conf = int(row.get("confidence", 0))
            if conf < min_conf:
                continue
            signals.append({
                "ticker": row["ticker"],
                "name": row.get("name", ""),
                "action": row["action"],
                "confidence": conf,
                "entry_bid": float(row.get("entry_bid", 0) or 0),
                "target": float(row.get("target", 0) or 0),
                "stop_loss": float(row.get("stop_loss", 0) or 0),
                "flags": row.get("data_quality_flags", ""),
            })
    signals.sort(key=lambda x: x["confidence"], reverse=True)
    return signals[:max_n]


def _load_intraday_hits(hit_date: date) -> list[dict]:
    path = _HITS_DIR / f"{hit_date}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _save_intraday_hit(ticker: str, price: float, triggered: bool) -> None:
    _HITS_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()
    path = _HITS_DIR / f"{today}.json"
    hits = json.loads(path.read_text()) if path.exists() else []
    hits.append({"ticker": ticker, "time": datetime.now().isoformat(), "price": price, "triggered": triggered})
    path.write_text(json.dumps(hits, indent=2))


# ── Scheduled jobs ──────────────────────────────────────────────────────────

async def _job_opening_scan() -> None:
    """09:05 — full market scan, build today's shortlist."""
    if not is_trading_day(date.today()):
        return
    async with _state["scan_lock"]:
        _console.print(f"[dim]{datetime.now():%H:%M} 全市場掃描中...[/dim]")
        code, _ = _run_subprocess([sys.executable, "scripts/batch_scan.py", "--save-csv", "--save-db"])
        if code != 0:
            await _send("⚠️ 開盤掃描失敗")
            return
        csv_path = _latest_scan_csv(0)
        if csv_path:
            _state["shortlist"] = _parse_scan_csv(csv_path)
            _state["last_scan_time"] = datetime.now()
        msg = format_opening_list(_state["shortlist"], str(date.today()))
        await _send(msg)


async def _job_hourly_rescan() -> None:
    """Hourly rescan — update shortlist ranking."""
    if not is_trading_day(date.today()):
        return
    if _state["scan_lock"].locked():
        logger.info("Hourly rescan skipped — previous scan still running")
        return
    async with _state["scan_lock"]:
        _console.print(f"[dim]{datetime.now():%H:%M} 全市場重掃...[/dim]")
        code, _ = _run_subprocess([sys.executable, "scripts/batch_scan.py", "--save-csv", "--save-db"])
        if code != 0:
            return
        csv_path = _latest_scan_csv(0)
        if not csv_path:
            return
        new_list = _parse_scan_csv(csv_path)
        old_tickers = {s["ticker"] for s in _state["shortlist"]}
        new_tickers = {s["ticker"] for s in new_list}
        added = new_tickers - old_tickers
        removed = old_tickers - new_tickers
        _state["shortlist"] = new_list
        _state["last_scan_time"] = datetime.now()
        if added or removed:
            lines = ["📊 *名單更新*"]
            for t in added:
                lines.append(f"✨ 新進：{t}")
            for t in removed:
                lines.append(f"⬇️ 移出：{t}")
            await _send("\n".join(lines))


async def _job_precheck() -> None:
    """Every 10 min — check entry conditions for shortlist."""
    if not is_trading_day(date.today()):
        return
    if not _state["monitoring_active"]:
        return
    if _state["precheck_lock"].locked():
        logger.info("Precheck skipped — previous round still running")
        return
    async with _state["precheck_lock"]:
        if not _state["shortlist"]:
            return
        tickers = ",".join(s["ticker"] for s in _state["shortlist"][:20])
        code, out = _run_subprocess([
            sys.executable, "scripts/precheck.py",
            "--tickers", tickers, "--min-confidence", "0",
        ])
        # Parse stdout for triggered signals (precheck outputs structured lines)
        # Record hits for post-market report
        for s in _state["shortlist"]:
            triggered = s["ticker"] in out and "✅" in out
            _save_intraday_hit(s["ticker"], price=0.0, triggered=triggered)
            if triggered:
                await _send(
                    format_entry_signal(
                        s["ticker"], s.get("name", ""),
                        price=s["entry_bid"],
                        entry_low=s["entry_bid"] * 0.97,
                        entry_high=s["entry_bid"] * 1.03,
                        stop=s["stop_loss"],
                    )
                )


async def _job_postmarket_report() -> None:
    """17:00 — post-market report with hit rate + tomorrow's list."""
    if not is_trading_day(date.today()):
        return
    # Run new scan for tomorrow
    async with _state["scan_lock"]:
        _run_subprocess([sys.executable, "scripts/batch_scan.py", "--save-csv", "--save-db"])

    yesterday_csv = _latest_scan_csv(1)
    yesterday_signals = _parse_scan_csv(yesterday_csv) if yesterday_csv else []
    intraday_hits = _load_intraday_hits(date.today())
    tomorrow_csv = _latest_scan_csv(0)
    tomorrow_signals = _parse_scan_csv(tomorrow_csv) if tomorrow_csv else []

    msg = format_postmarket_report(
        yesterday_signals=yesterday_signals,
        intraday_hits=intraday_hits,
        tomorrow_signals=tomorrow_signals,
        report_date=str(date.today()),
    )
    await _send(msg)


async def _job_optimize() -> None:
    """Tue/Fri 18:00 — run AI optimization agent."""
    from scripts.optimize_agent import run_optimize  # type: ignore
    await run_optimize(_state["llm"], _send)


# ── Telegram command handlers ───────────────────────────────────────────────

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = format_opening_list(_state["shortlist"], str(date.today()))
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    last = _state["last_scan_time"]
    last_str = last.strftime("%H:%M") if last else "尚未執行"
    msg = (
        f"📊 *系統狀態*\n"
        f"名單：{len(_state['shortlist'])} 檔\n"
        f"上次掃描：{last_str}\n"
        f"推播：{'✅ 開啟' if _state['monitoring_active'] else '⏸ 暫停'}\n"
        f"LLM：{_state['llm']}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _state["monitoring_active"] = False
    await update.message.reply_text("⏸ 盤中推播已暫停。/resume 恢復")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _state["monitoring_active"] = True
    await update.message.reply_text("▶️ 盤中推播已恢復")


async def cmd_params(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    params = json.loads(_PARAMS_PATH.read_text())
    clean = {k: v for k, v in params.items() if not k.startswith("_")}
    await update.message.reply_text(f"```json\n{json.dumps(clean, indent=2, ensure_ascii=False)}\n```", parse_mode="Markdown")


async def cmd_optimize(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🤖 手動觸發優化，執行中...")
    await _job_optimize()


async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pending_raw = _PENDING_PATH.read_text().strip()
    if pending_raw == "null" or not pending_raw:
        await update.message.reply_text("目前沒有待確認的建議")
        return
    pending = json.loads(pending_raw)
    params = json.loads(_PARAMS_PATH.read_text())
    ok, errors = validate_changes(pending["changes"], params)
    if not ok:
        await update.message.reply_text(f"⚠ 驗證失敗：{'; '.join(errors)}")
        return
    apply_changes(pending["changes"])
    _PENDING_PATH.write_text("null")
    await update.message.reply_text("✅ 已套用優化建議")


async def cmd_rollback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # If pending, discard it
    pending_raw = _PENDING_PATH.read_text().strip()
    if pending_raw != "null" and pending_raw:
        _PENDING_PATH.write_text("null")
        await update.message.reply_text("🗑 已捨棄待確認的建議")
        return
    reverted = rollback_params()
    if reverted is None:
        await update.message.reply_text("無可回滾的歷史記錄")
    else:
        lines = "\n".join(f"  · {c['param']} {c['to']}→{c['from']}" for c in reverted)
        await update.message.reply_text(f"⏪ 已還原上一版參數：\n{lines}")


# ── Subprocess helper ───────────────────────────────────────────────────────

def _run_subprocess(cmd: list[str]) -> tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(_ROOT))
    return result.returncode, result.stdout + result.stderr


# ── CLI display ─────────────────────────────────────────────────────────────

def _render_status_table() -> Table:
    table = Table(box=box.ROUNDED, show_header=False, expand=True)
    table.add_column("key", style="dim", width=20)
    table.add_column("val")
    now = datetime.now()
    table.add_row("時間", now.strftime("%H:%M:%S"))
    table.add_row("今日名單", f"{len(_state['shortlist'])} 檔")
    table.add_row("推播", "✅ 開啟" if _state["monitoring_active"] else "⏸ 暫停")
    table.add_row("LLM", _state["llm"])
    last = _state["last_scan_time"]
    table.add_row("上次掃描", last.strftime("%H:%M") if last else "—")
    return table


# ── LLM selection ───────────────────────────────────────────────────────────

def _select_llm(arg: str | None) -> str:
    if arg:
        return arg
    _console.print("\n[bold]optimize_agent LLM：[/bold]")
    _console.print("  [1] Claude (claude-sonnet-4-6)  ← 預設")
    _console.print("  [2] Gemini (gemini-2.5-flash)")
    _console.print("  [3] OpenAI (gpt-4o)")
    choice = input("選擇 (Enter = 1)：").strip()
    return {"2": "gemini", "3": "openai"}.get(choice, "claude")


# ── Main ────────────────────────────────────────────────────────────────────

async def main_async(llm: str) -> None:
    _state["llm"] = llm
    _state["scan_lock"] = asyncio.Lock()
    _state["precheck_lock"] = asyncio.Lock()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        _console.print("[red]❌ 請先執行 make bot-setup 設定 Telegram[/red]")
        sys.exit(1)
    _state["chat_id"] = chat_id

    app = Application.builder().token(token).build()
    _state["app"] = app

    for cmd, handler in [
        ("top", cmd_top), ("status", cmd_status),
        ("pause", cmd_pause), ("resume", cmd_resume),
        ("params", cmd_params), ("optimize", cmd_optimize),
        ("approve", cmd_approve), ("rollback", cmd_rollback),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    scheduler = AsyncIOScheduler()
    # Opening scan: 09:05 Mon–Fri
    scheduler.add_job(_job_opening_scan, "cron", day_of_week="mon-fri", hour=9, minute=5)
    # Hourly rescan: 10:05–13:05
    for h in [10, 11, 12, 13]:
        scheduler.add_job(_job_hourly_rescan, "cron", day_of_week="mon-fri", hour=h, minute=5)
    # 10-min precheck: 09:05–13:25
    scheduler.add_job(_job_precheck, "cron", day_of_week="mon-fri", hour="9-13", minute="5,15,25,35,45,55")
    # Post-market report: 17:00 Mon–Fri
    scheduler.add_job(_job_postmarket_report, "cron", day_of_week="mon-fri", hour=17, minute=0)
    # Optimize: 18:00 Tue and Fri
    scheduler.add_job(_job_optimize, "cron", day_of_week="tue,fri", hour=18, minute=0)
    scheduler.start()

    _console.print("[green]✅ Telegram Bot 連線[/green]")
    _console.print("[green]✅ 排程載入[/green]")
    _console.print("[green]✅ 監控中（Ctrl+C 停止）[/green]\n")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    try:
        while True:
            _console.clear()
            _console.print(_render_status_table())
            await asyncio.sleep(30)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.shutdown(wait=False)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", default=None, choices=["claude", "gemini", "openai"])
    args = parser.parse_args()

    _console.print("[bold blue]股票信號機器人 v1.0[/bold blue]")
    _console.print("─" * 40)
    llm = _select_llm(args.llm)
    _console.print(f"\n啟動中... LLM={llm}")

    asyncio.run(main_async(llm))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add scripts/bot.py
git commit -m "feat: bot.py daemon (AsyncIOScheduler + Telegram + intraday monitoring)"
```

---

## Task 8: Makefile + requirements.txt

**Files:**
- Modify: `Makefile`
- Modify: `requirements.txt`

- [ ] **Step 1: Add to requirements.txt**

Add two lines to `requirements.txt`:
```
python-telegram-bot>=21
apscheduler>=3.10,<4.0
```

- [ ] **Step 2: Add Makefile targets**

Add to `Makefile` after the `db-init` section:

```makefile
# ── Telegram Bot ──────────────────────────────────────────────────────────────
bot-setup:
	$(PYTHON) scripts/bot_setup.py

LLM ?=
bot:
	$(PYTHON) scripts/bot.py $(if $(LLM),--llm $(LLM))
# 用法: make bot          # 互動選 LLM
#       make bot LLM=gemini  # 略過互動，直接指定

.PHONY: bot-setup bot
```

- [ ] **Step 3: Install deps**

```bash
cd /Users/07683.howard.huang/Documents/code/stock_investment
.venv/bin/pip install "python-telegram-bot>=21" "apscheduler>=3.10,<4.0"
```

- [ ] **Step 4: Verify imports**

```bash
.venv/bin/python -c "from telegram.ext import Application; from apscheduler.schedulers.asyncio import AsyncIOScheduler; print('OK')"
```
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add Makefile requirements.txt
git commit -m "feat: add bot-setup and bot Makefile targets, update requirements"
```

---

## Task 9: Full Test Suite + Smoke Test

- [ ] **Step 1: Run all unit tests**

```bash
cd /Users/07683.howard.huang/Documents/code/stock_investment
.venv/bin/pytest tests/unit/ -q
```
Expected: All existing tests pass + new tests in `test_bot_utils.py`

- [ ] **Step 2: Syntax check all new scripts**

```bash
.venv/bin/python -m py_compile scripts/bot.py scripts/bot_setup.py scripts/optimize_agent.py
echo "syntax OK"
```
Expected: `syntax OK`

- [ ] **Step 3: Import check**

```bash
.venv/bin/python -c "
import scripts.optimize_agent
from taiwan_stock_agent.utils.trading_calendar import is_trading_day
from taiwan_stock_agent.utils.bot_formatters import format_opening_list
from taiwan_stock_agent.utils.param_safety import validate_changes
print('all imports OK')
"
```
Expected: `all imports OK`

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: complete Telegram bot + AI optimize agent implementation"
```

---

## Manual Verification Checklist

After implementation, verify end-to-end:

- [ ] `make bot-setup` — completes without error, test message arrives in Telegram
- [ ] `make bot` — shows LLM selection, starts without error, Rich table displays
- [ ] `make bot LLM=gemini` — starts without interactive prompt
- [ ] `/status` in Telegram → returns system state
- [ ] `/top` in Telegram → returns current shortlist (or empty message)
- [ ] `/pause` then `/resume` → monitoring_active toggles
- [ ] `/params` → shows engine_params.json
- [ ] `/optimize` → triggers optimize pipeline (may skip if no factor data)
- [ ] `config/pending_change.json` remains `null` after successful optimize (or has pending JSON)
- [ ] `/rollback` with no history → "無可回滾" message
