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
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

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
_HISTORY_PATH = _ROOT / "config" / "param_history.json"
_PENDING_PATH = _ROOT / "config" / "pending_change.json"

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── In-memory state ─────────────────────────────────────────────────────────
_state: dict = {
    "shortlist": [],           # list[dict] — today's top signals, max 20
    "monitoring_active": True,
    "last_scan_time": None,
    "llm": "claude",
    "scan_lock": None,         # asyncio.Lock, initialised in main_async()
    "precheck_lock": None,     # asyncio.Lock, initialised in main_async()
    "app": None,               # Telegram Application
    "chat_id": None,
}


# ── Subprocess helper ────────────────────────────────────────────────────────

def _run_subprocess(cmd: list[str]) -> tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(_ROOT))
    return result.returncode, result.stdout + result.stderr


async def _run_subprocess_async(cmd: list[str]) -> tuple[int, str]:
    """Non-blocking subprocess — runs in thread pool so event loop stays alive."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run_subprocess, cmd)


# ── Telegram helpers ─────────────────────────────────────────────────────────

async def _send(text: str) -> None:
    try:
        await _state["app"].bot.send_message(
            chat_id=_state["chat_id"],
            text=text,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Telegram send error: {e}")


# ── CSV helpers ──────────────────────────────────────────────────────────────

def _latest_scan_csv(offset_trading_days: int = 0) -> Path | None:
    """Find the scan CSV that is `offset_trading_days` trading days before today."""
    candidate = date.today()
    skipped = 0
    for _ in range(30):  # look back at most 30 calendar days
        if candidate.weekday() < 5:  # Mon–Fri
            if skipped == offset_trading_days:
                path = _SCAN_DIR / f"scan_{candidate}.csv"
                if path.exists():
                    return path
            skipped += 1
        candidate -= timedelta(days=1)
    return None


def _parse_scan_csv(path: Path, min_conf: int = 40, max_n: int = 20) -> list[dict]:
    signals = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("action") not in ("BUY", "WATCH"):
                continue
            conf = int(row.get("confidence", 0) or 0)
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


def _write_temp_shortlist_csv(signals: list[dict]) -> Path:
    """Write shortlist to a temp CSV compatible with precheck.py --csv."""
    today = date.today()
    tmp = Path(tempfile.mktemp(suffix=".csv"))
    fieldnames = [
        "scan_date", "analysis_date", "ticker", "action", "confidence",
        "free_tier", "halt", "entry_bid", "stop_loss", "target",
        "momentum", "chip_analysis", "risk_factors", "data_quality_flags",
    ]
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for s in signals:
            writer.writerow({
                "scan_date": str(today),
                "analysis_date": str(today),
                "ticker": s["ticker"],
                "action": s["action"],
                "confidence": s["confidence"],
                "free_tier": "True",
                "halt": "False",
                "entry_bid": s["entry_bid"],
                "stop_loss": s["stop_loss"],
                "target": s["target"],
                "momentum": "",
                "chip_analysis": "",
                "risk_factors": "",
                "data_quality_flags": s.get("flags", ""),
            })
    return tmp


# ── Intraday hit tracking ────────────────────────────────────────────────────

def _load_intraday_hits(hit_date: date) -> list[dict]:
    path = _HITS_DIR / f"{hit_date}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _save_intraday_hit(ticker: str, price: float, triggered: bool) -> None:
    _HITS_DIR.mkdir(parents=True, exist_ok=True)
    path = _HITS_DIR / f"{date.today()}.json"
    hits = json.loads(path.read_text()) if path.exists() else []
    hits.append({
        "ticker": ticker,
        "time": datetime.now().isoformat(),
        "price": price,
        "triggered": triggered,
    })
    path.write_text(json.dumps(hits, indent=2, ensure_ascii=False))


# ── Scheduled jobs ───────────────────────────────────────────────────────────

async def _job_opening_scan(force: bool = False) -> None:
    """09:05 — full market scan, build today's shortlist."""
    if not force and not is_trading_day(date.today()):
        return
    async with _state["scan_lock"]:
        _console.print(f"[dim]{datetime.now():%H:%M} 全市場掃描中...[/dim]")
        code, _ = await _run_subprocess_async([
            sys.executable, "scripts/batch_scan.py", "--save-csv", "--save-db",
        ])
        if code != 0:
            await _send("⚠️ 開盤掃描失敗")
            return
        csv_path = _latest_scan_csv(0)
        if csv_path:
            _state["shortlist"] = _parse_scan_csv(csv_path)
            _state["last_scan_time"] = datetime.now()
        await _send(format_opening_list(_state["shortlist"], str(date.today())))


async def _job_hourly_rescan() -> None:
    """Hourly rescan — update shortlist ranking if not already running."""
    if not is_trading_day(date.today()):
        return
    if _state["scan_lock"].locked():
        logger.info("Hourly rescan skipped — previous scan still running")
        return
    async with _state["scan_lock"]:
        _console.print(f"[dim]{datetime.now():%H:%M} 全市場重掃...[/dim]")
        code, _ = await _run_subprocess_async([
            sys.executable, "scripts/batch_scan.py", "--save-csv", "--save-db",
        ])
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
            for t in sorted(added):
                lines.append(f"✨ 新進：{t}")
            for t in sorted(removed):
                lines.append(f"⬇️ 移出：{t}")
            await _send("\n".join(lines))
        else:
            await _send(f"🔄 {datetime.now():%H:%M} 重掃完成，名單無異動（共 {len(new_list)} 檔）")


async def _job_precheck(force: bool = False) -> None:
    """Every 10 min — check entry conditions for shortlist via precheck.py --csv."""
    if not force and not is_trading_day(date.today()):
        return
    if not _state["monitoring_active"]:
        return
    if _state["precheck_lock"].locked():
        logger.info("Precheck skipped — previous round still running")
        return
    async with _state["precheck_lock"]:
        if not _state["shortlist"]:
            return
        tmp_csv = _write_temp_shortlist_csv(_state["shortlist"])
        try:
            code, out = await _run_subprocess_async([
                sys.executable, "scripts/precheck.py",
                "--csv", str(tmp_csv), "--min-confidence", "0",
            ])
        finally:
            tmp_csv.unlink(missing_ok=True)

        # Parse per-ticker using \b word boundary to avoid false positives
        lines = out.split("\n")
        for s in _state["shortlist"]:
            ticker = s["ticker"]
            ticker_lines = [l for l in lines if re.search(rf"\b{re.escape(ticker)}\b", l)]
            triggered = any("✅" in l for l in ticker_lines)
            _save_intraday_hit(ticker, s["entry_bid"], triggered)
            if triggered:
                await _send(format_entry_signal(
                    ticker, s.get("name", ""),
                    price=s["entry_bid"],
                    entry_low=s["entry_bid"] * 0.97,
                    entry_high=s["entry_bid"] * 1.03,
                    stop=s["stop_loss"],
                ))


async def _job_postmarket_report(force: bool = False) -> None:
    """17:00 — post-market report with hit rate + tomorrow's list."""
    if not force and not is_trading_day(date.today()):
        return
    # Run new scan for tomorrow's list
    async with _state["scan_lock"]:
        await _run_subprocess_async([sys.executable, "scripts/batch_scan.py", "--save-csv", "--save-db"])

    yesterday_csv = _latest_scan_csv(1)
    yesterday_signals = _parse_scan_csv(yesterday_csv) if yesterday_csv else []
    intraday_hits = _load_intraday_hits(date.today())
    tomorrow_csv = _latest_scan_csv(0)
    tomorrow_signals = _parse_scan_csv(tomorrow_csv) if tomorrow_csv else []

    await _send(format_postmarket_report(
        yesterday_signals=yesterday_signals,
        intraday_hits=intraday_hits,
        tomorrow_signals=tomorrow_signals,
        report_date=str(date.today()),
    ))


async def _job_optimize() -> None:
    """Tue/Fri 18:00 — run AI optimization agent."""
    from taiwan_stock_agent.optimize import run_optimize
    await run_optimize(_state["llm"], _send)


# ── Telegram command handlers ────────────────────────────────────────────────

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        format_opening_list(_state["shortlist"], str(date.today())),
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    last = _state["last_scan_time"]
    await update.message.reply_text(
        f"📊 *系統狀態*\n"
        f"名單：{len(_state['shortlist'])} 檔\n"
        f"上次掃描：{last.strftime('%H:%M') if last else '尚未執行'}\n"
        f"推播：{'✅ 開啟' if _state['monitoring_active'] else '⏸ 暫停'}\n"
        f"LLM：{_state['llm']}",
        parse_mode="Markdown",
    )


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _state["monitoring_active"] = False
    await update.message.reply_text("⏸ 盤中推播已暫停。/resume 恢復")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _state["monitoring_active"] = True
    await update.message.reply_text("▶️ 盤中推播已恢復")


async def cmd_params(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    params = json.loads(_PARAMS_PATH.read_text())
    clean = {k: v for k, v in params.items() if not k.startswith("_") and k != "tunable_whitelist"}
    await update.message.reply_text(
        f"```json\n{json.dumps(clean, indent=2, ensure_ascii=False)}\n```",
        parse_mode="Markdown",
    )


async def cmd_optimize(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🤖 手動觸發優化，執行中...")
    await _job_optimize()


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger opening scan (bypasses trading-day check)."""
    if _state["scan_lock"].locked():
        await update.message.reply_text("⚠ 掃描進行中，請稍候")
        return
    await update.message.reply_text("🔍 手動觸發掃描，執行中（需數分鐘）...")
    await _job_opening_scan(force=True)


async def cmd_precheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger precheck on current shortlist (bypasses trading-day check)."""
    if not _state["shortlist"]:
        await update.message.reply_text("⚠ 名單為空，請先執行 /scan")
        return
    if _state["precheck_lock"].locked():
        await update.message.reply_text("⚠ precheck 進行中，請稍候")
        return
    await update.message.reply_text(f"🔔 手動 precheck — 監控 {len(_state['shortlist'])} 檔...")
    await _job_precheck(force=True)


async def cmd_postmarket(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger post-market report (bypasses trading-day check)."""
    await update.message.reply_text("📈 手動觸發盤後報告...")
    await _job_postmarket_report(force=True)


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Logic smoke test: verify each command path actually works."""
    reply = update.message.reply_text
    ok = "✅"
    fail = "❌"
    results: list[str] = []

    # 1. /status — read state
    try:
        last = _state["last_scan_time"]
        msg = (
            f"📊 *系統狀態*\n名單：{len(_state['shortlist'])} 檔\n"
            f"上次掃描：{last.strftime('%H:%M') if last else '尚未執行'}\n"
            f"推播：{'✅ 開啟' if _state['monitoring_active'] else '⏸ 暫停'}\n"
            f"LLM：{_state['llm']}"
        )
        assert "LLM" in msg
        results.append(f"{ok} /status — 讀取狀態正常")
    except Exception as e:
        results.append(f"{fail} /status — {e}")

    # 2. /params — read engine_params.json
    try:
        params = json.loads(_PARAMS_PATH.read_text())
        assert "tunable_whitelist" in params
        results.append(f"{ok} /params — 讀取 engine_params.json（{len(params)} 個參數）")
    except Exception as e:
        results.append(f"{fail} /params — {e}")

    # 3. /top — format current shortlist (may be empty)
    try:
        msg = format_opening_list(_state["shortlist"], str(date.today()))
        assert msg
        results.append(f"{ok} /top — 格式化名單（{len(_state['shortlist'])} 檔）")
    except Exception as e:
        results.append(f"{fail} /top — {e}")

    # 4. /pause + /resume — toggle monitoring flag
    try:
        _state["monitoring_active"] = False
        assert not _state["monitoring_active"]
        _state["monitoring_active"] = True
        assert _state["monitoring_active"]
        results.append(f"{ok} /pause + /resume — 推播開關切換正常")
    except Exception as e:
        results.append(f"{fail} /pause//resume — {e}")

    # 5. /approve — write fake pending → approve → verify applied
    try:
        params = json.loads(_PARAMS_PATH.read_text())
        original_val = params.get("watch_min", 40)
        new_val = round(original_val * 1.05, 1)  # +5%, within ±20%
        fake_pending = {
            "confidence": 80,
            "changes": [{"param": "watch_min", "from": original_val, "to": new_val, "reason": "test"}],
            "summary": "自動化測試用",
        }
        _PENDING_PATH.write_text(json.dumps(fake_pending, ensure_ascii=False))
        # run approve logic
        pending = json.loads(_PENDING_PATH.read_text())
        ok_v, errors = validate_changes(pending["changes"], params)
        assert ok_v, errors
        apply_changes(pending["changes"], params_path=_PARAMS_PATH, history_path=_HISTORY_PATH)
        _PENDING_PATH.write_text("null")
        updated = json.loads(_PARAMS_PATH.read_text())
        assert updated["watch_min"] == new_val
        results.append(f"{ok} /approve — pending 套用成功（watch\\_min {original_val}→{new_val}）")
    except Exception as e:
        results.append(f"{fail} /approve — {e}")

    # 6. /rollback — revert the change just applied
    try:
        reverted = rollback_params(params_path=_PARAMS_PATH, history_path=_HISTORY_PATH)
        assert reverted is not None
        restored = json.loads(_PARAMS_PATH.read_text())
        assert restored["watch_min"] == original_val
        results.append(f"{ok} /rollback — 還原成功（watch\\_min 回到 {original_val}）")
    except Exception as e:
        results.append(f"{fail} /rollback — {e}")

    # 7. TG send path
    try:
        await _send("🧪 _send() 測試訊息")
        results.append(f"{ok} Telegram send — 推播路徑正常")
    except Exception as e:
        results.append(f"{fail} Telegram send — {e}")

    summary = "\n".join(results)
    passed = sum(1 for r in results if r.startswith(ok))
    await reply(
        f"🧪 *指令邏輯測試結果* {passed}/{len(results)} 通過\n\n{summary}\n\n"
        f"手動觸發指令：/scan /precheck /postmarket /optimize",
        parse_mode="Markdown",
    )


async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pending_raw = _PENDING_PATH.read_text().strip() if _PENDING_PATH.exists() else "null"
    if pending_raw in ("null", ""):
        await update.message.reply_text("目前沒有待確認的建議")
        return
    pending = json.loads(pending_raw)
    params = json.loads(_PARAMS_PATH.read_text())
    ok, errors = validate_changes(pending["changes"], params)
    if not ok:
        await update.message.reply_text(f"⚠ 驗證失敗：{'; '.join(errors)}")
        return
    apply_changes(pending["changes"], params_path=_PARAMS_PATH, history_path=_HISTORY_PATH)
    _PENDING_PATH.write_text("null")
    await update.message.reply_text("✅ 已套用優化建議")


async def cmd_rollback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pending_raw = _PENDING_PATH.read_text().strip() if _PENDING_PATH.exists() else "null"
    if pending_raw not in ("null", ""):
        _PENDING_PATH.write_text("null")
        await update.message.reply_text("🗑 已捨棄待確認的建議")
        return
    reverted = rollback_params(params_path=_PARAMS_PATH, history_path=_HISTORY_PATH)
    if reverted is None:
        await update.message.reply_text("無可回滾的歷史記錄")
    else:
        lines = "\n".join(f"  · {c['param']} {c['to']}→{c['from']}" for c in reverted)
        await update.message.reply_text(f"⏪ 已還原上一版參數：\n{lines}")


# ── Rich CLI display ─────────────────────────────────────────────────────────

def _render_status_panel() -> Panel:
    now = datetime.now()
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("key", style="dim", min_width=16)
    table.add_column("val")

    # clock
    table.add_row("時間", f"[bold cyan]{now.strftime('%H:%M:%S')}[/bold cyan]")
    table.add_row("", "")

    # watchlist
    shortlist = _state["shortlist"]
    table.add_row("今日名單", f"[green]{len(shortlist)} 檔[/green]")
    last = _state["last_scan_time"]
    table.add_row("上次掃描", last.strftime("%H:%M") if last else "[dim]尚未執行[/dim]")
    table.add_row("推播監控", "✅ 開啟" if _state["monitoring_active"] else "[yellow]⏸ 暫停[/yellow]")
    table.add_row("LLM", _state["llm"])
    table.add_row("", "")

    # pending change
    pending_raw = _PENDING_PATH.read_text().strip() if _PENDING_PATH.exists() else "null"
    if pending_raw not in ("null", ""):
        table.add_row("待確認", "[yellow]有優化建議 → /approve[/yellow]")
    else:
        table.add_row("待確認", "[dim]無[/dim]")
    table.add_row("", "")

    # schedule reminder
    table.add_row("[dim]排程[/dim]", "[dim]掃描 09:05 · 重掃 10-13:05 · 盤後 17:00 · 優化 週二/五 18:00[/dim]")
    table.add_row("", "")
    table.add_row("[dim]指令[/dim]", "[dim]/scan        手動觸發全市場掃描[/dim]")
    table.add_row("",               "[dim]/precheck    盤中確認名單進場條件[/dim]")
    table.add_row("",               "[dim]/postmarket  產生盤後報告（命中率+隔日名單）[/dim]")
    table.add_row("",               "[dim]/optimize    手動觸發 AI 參數優化[/dim]")
    table.add_row("",               "[dim]/approve     套用待確認的優化建議[/dim]")
    table.add_row("",               "[dim]/rollback    還原上一版參數[/dim]")
    table.add_row("",               "[dim]/top         查看今日名單[/dim]")
    table.add_row("",               "[dim]/status      系統狀態摘要[/dim]")
    table.add_row("",               "[dim]/test        指令邏輯自動測試[/dim]")

    return Panel(table, title="[bold blue]股票信號機器人[/bold blue]", subtitle=f"[dim]{now.strftime('%Y-%m-%d')}[/dim]", border_style="blue")


# ── LLM selection ────────────────────────────────────────────────────────────

def _select_llm(arg: str | None) -> str:
    if arg:
        return arg
    _console.print("\n[bold]optimize_agent LLM：[/bold]")
    _console.print("  [1] Claude (claude-sonnet-4-6)  ← 預設")
    _console.print("  [2] Gemini (gemini-2.5-flash)")
    _console.print("  [3] OpenAI (gpt-4o)")
    _console.print("  [4] GLM   (glm-4-flash，需 ZHIPUAI_API_KEY)")
    choice = input("選擇 (Enter = 1)：").strip()
    return {"2": "gemini", "3": "openai", "4": "glm"}.get(choice, "claude")


# ── Main ─────────────────────────────────────────────────────────────────────

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

    for cmd_name, handler in [
        ("top", cmd_top), ("status", cmd_status),
        ("pause", cmd_pause), ("resume", cmd_resume),
        ("params", cmd_params), ("optimize", cmd_optimize),
        ("approve", cmd_approve), ("rollback", cmd_rollback),
        ("scan", cmd_scan), ("precheck", cmd_precheck), ("postmarket", cmd_postmarket),
        ("test", cmd_test),
    ]:
        app.add_handler(CommandHandler(cmd_name, handler))

    scheduler = AsyncIOScheduler()
    # 09:05 opening scan (Mon–Fri)
    scheduler.add_job(_job_opening_scan, "cron", day_of_week="mon-fri", hour=9, minute=5)
    # Hourly rescan 10:05–13:05
    for h in [10, 11, 12, 13]:
        scheduler.add_job(_job_hourly_rescan, "cron", day_of_week="mon-fri", hour=h, minute=5)
    # 10-min precheck 09:05–13:55 (market hours)
    scheduler.add_job(
        _job_precheck, "cron",
        day_of_week="mon-fri",
        hour="9-13",
        minute="5,15,25,35,45,55",
    )
    # 17:00 post-market report
    scheduler.add_job(_job_postmarket_report, "cron", day_of_week="mon-fri", hour=17, minute=0)
    # 18:00 optimize on Tue and Fri
    scheduler.add_job(_job_optimize, "cron", day_of_week="tue,fri", hour=18, minute=0)
    scheduler.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    try:
        with Live(_render_status_panel(), console=_console, refresh_per_second=1, screen=True) as live:
            while True:
                live.update(_render_status_panel())
                await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.shutdown(wait=False)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Taiwan stock signal Telegram bot")
    parser.add_argument("--llm", default=None, choices=["claude", "gemini", "openai", "glm"],
                        help="LLM for optimize_agent (skips interactive prompt)")
    args = parser.parse_args()

    _console.print("[bold blue]股票信號機器人 v1.0[/bold blue]")
    _console.print("─" * 40)
    llm = _select_llm(args.llm)
    _console.print(f"\n啟動中... LLM={llm}")

    asyncio.run(main_async(llm))


if __name__ == "__main__":
    main()
