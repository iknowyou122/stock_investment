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
import collections
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
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.style import Style
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
_NAME_MAP_DIR = _ROOT / "data" / "watchlist_cache"
_HITS_DIR = _ROOT / "data" / "intraday_hits"
_PARAMS_PATH = _ROOT / "config" / "engine_params.json"
_HISTORY_PATH = _ROOT / "config" / "param_history.json"
_PENDING_PATH = _ROOT / "config" / "pending_change.json"

_LOG_PATH = _ROOT / "logs" / "bot.log"
_LOG_PATH.parent.mkdir(exist_ok=True)

# ── In-screen log buffer ─────────────────────────────────────────────────────
_LOG_LINES: collections.deque[tuple[str, str, str]] = collections.deque(maxlen=6)
# each entry: (time_str, level, message)

class _PanelHandler(logging.Handler):
    """Push formatted records into the in-screen ring buffer."""
    def emit(self, record: logging.LogRecord) -> None:
        level = record.levelname
        msg = record.getMessage()[:120]   # trim very long lines
        _LOG_LINES.append((datetime.now().strftime("%H:%M:%S"), level, msg))

_panel_handler = _PanelHandler()
_panel_handler.setLevel(logging.INFO)

_file_handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _panel_handler])
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
    "last_cmd": None,          # (cmd_name, status, time) — shown in status panel
}


# ── Market data cache ────────────────────────────────────────────────────────

_MARKET_CACHE: dict = {
    "global": {},      # key → {"price": float, "change_pct": float}  (yfinance)
    "sectors": [],     # list[{"code", "abbr", "price", "change_pct"}]  (TWSE MIS)
    "watchlist": {},   # ticker → {"price", "change_pct", "prev_close", "is_live"}
    "sentiment": None, # MarketSentiment | None
    "updated_at": None,
}

# Global / forex / US market symbols via yfinance (TW sectors fetched separately via MIS).
_MARKET_SYMBOLS: dict[str, str] = {
    # Taiwan index
    "taiex":    "^TWII",
    # Forex
    "usd_twd":  "TWD=X",
    "usd_jpy":  "JPY=X",
    "dxy":      "DX-Y.NYB",
    # US equity
    "sox":      "^SOX",
    "nasdaq":   "^IXIC",
    "sp500":    "^GSPC",
    "vix":      "^VIX",
    "dow":      "^DJI",
    # Commodities
    "gold":     "GC=F",
    "silver":   "SI=F",
    "oil_wti":  "CL=F",
    "copper":   "HG=F",
    "natgas":   "NG=F",
    # Crypto
    "btc":      "BTC-USD",
}

# 28 TWSE official sector indices accessible via MIS batch API.
# 4 newer sectors (綠能環保/數位雲端/運動休閒/居家生活) are not yet available via MIS.
_TW_SECTOR_CODES: list[tuple[str, str]] = [
    ("IX0010", "水泥"),
    ("IX0011", "食品"),
    ("IX0012", "塑膠"),
    ("IX0016", "紡織纖維"),
    ("IX0017", "電機機械"),
    ("IX0018", "電器電纜"),
    ("IX0020", "化學"),
    ("IX0021", "生技醫療"),
    ("IX0022", "玻璃陶瓷"),
    ("IX0023", "造紙"),
    ("IX0024", "鋼鐵"),
    ("IX0025", "橡膠"),
    ("IX0026", "汽車"),
    ("IX0028", "半導體"),
    ("IX0029", "電腦週邊"),
    ("IX0030", "光電"),
    ("IX0031", "通信網路"),
    ("IX0032", "電子零組件"),
    ("IX0033", "電子通路"),
    ("IX0034", "資訊服務"),
    ("IX0035", "其他電子"),
    ("IX0036", "建材營造"),
    ("IX0037", "航運"),
    ("IX0038", "觀光餐旅"),
    ("IX0039", "金融保險"),
    ("IX0040", "貿易百貨"),
    ("IX0041", "油電燃氣"),
    ("IX0042", "其他"),
]


def _get_latest_market_map() -> dict[str, str]:
    """Load the most recent market_map_YYYY-MM-DD.json from cache."""
    paths = sorted(_NAME_MAP_DIR.glob("market_map_*.json"), reverse=True)
    if not paths:
        return {}
    try:
        return json.loads(paths[0].read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("Failed to load market map: %s", e)
        return {}


def _fetch_watchlist_prices_sync(tickers: list[str]) -> dict[str, dict | None]:
    """Fetch real-time prices for watchlist tickers via TWSE/TPEx MIS API.

    Returns dict[ticker → {"price", "change_pct", "prev_close", "is_live"}].
    During non-trading hours `z` is "-" so we fall back to prev_close with
    is_live=False to signal the panel to render it as a reference price.
    """
    if not tickers:
        return {}
    import requests
    market_map = _get_latest_market_map()

    parts = []
    for ticker in tickers:
        market = market_map.get(ticker, "TSE")
        prefix = "otc" if market == "TPEx" else "tse"
        parts.append(f"{prefix}_{ticker}.tw")

    ex_ch = "|".join(parts)
    try:
        resp = requests.get(
            "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
            params={"ex_ch": ex_ch},
            timeout=10,
            verify=False,
        )
        items = resp.json().get("msgArray", [])
        lookup = {item["c"]: item for item in items if item.get("c")}
        result: dict[str, dict | None] = {}
        for ticker in tickers:
            item = lookup.get(ticker)
            if not item:
                result[ticker] = None
                continue
            try:
                z = item.get("z", "-")
                y = item.get("y", "-")
                prev = float(y) if y not in ("-", "") else None
                if z not in ("-", "") and prev:
                    price = float(z)
                    chg = (price - prev) / prev * 100
                    is_live = True
                elif prev:
                    price = prev
                    chg = 0.0
                    is_live = False
                else:
                    result[ticker] = None
                    continue
                result[ticker] = {
                    "price": price,
                    "change_pct": chg,
                    "prev_close": prev,
                    "is_live": is_live,
                }
            except (ValueError, ZeroDivisionError):
                result[ticker] = None
        return result
    except Exception as e:
        logger.debug("watchlist price fetch error: %s", e)
        return {}


def _fetch_global_markets_sync() -> dict:
    """Batch-fetch global/forex/US symbols via yfinance fast_info."""
    import yfinance as yf
    result: dict = {}
    for key, sym in _MARKET_SYMBOLS.items():
        try:
            fi = yf.Ticker(sym).fast_info
            price = fi.last_price
            prev = fi.previous_close
            chg = ((price - prev) / prev * 100) if prev else 0.0
            result[key] = {"price": price, "change_pct": chg}
        except Exception as e:
            logger.debug("yfinance %s failed: %s", sym, e)
            result[key] = None
    return result


def _fetch_tw_sectors_sync() -> list[dict]:
    """Fetch 28 TWSE sector indices via MIS batch API (tse_IXnnnn.tw)."""
    import requests
    ex_ch = "|".join(f"tse_{code}.tw" for code, _ in _TW_SECTOR_CODES)
    try:
        resp = requests.get(
            "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
            params={"ex_ch": ex_ch},
            timeout=10,
            verify=False,
        )
        items = resp.json().get("msgArray", [])
        lookup = {item["c"]: item for item in items if item.get("c")}
        result = []
        for code, abbr in _TW_SECTOR_CODES:
            item = lookup.get(code)
            price, chg = None, 0.0
            if item:
                try:
                    z = item.get("z", "-")
                    y = item.get("y", "-")
                    if z not in ("-", "") and y not in ("-", ""):
                        price = float(z)
                        prev = float(y)
                        chg = (price - prev) / prev * 100 if prev else 0.0
                except (ValueError, ZeroDivisionError):
                    pass
            result.append({"code": code, "abbr": abbr, "price": price, "change_pct": chg})
        return result
    except Exception as e:
        logger.debug("MIS sector fetch error: %s", e)
        return [{"code": c, "abbr": a, "price": None, "change_pct": 0.0} for c, a in _TW_SECTOR_CODES]


def _fetch_sentiment_sync() -> "MarketSentiment | None":
    """Fetch TWSE breadth + Yahoo RSS headlines, return MarketSentiment."""
    try:
        from taiwan_stock_agent.domain.market_sentiment import compute_sentiment
        from taiwan_stock_agent.infrastructure.sentiment_client import (
            fetch_breadth,
            fetch_news_headlines,
        )

        # Compute TAIEX RSI from cached global data
        taiex_data = _MARKET_CACHE.get("global", {}).get("taiex")
        taiex_rsi = 50.0  # default neutral
        # Simple approximation: use cached TAIEX price change direction
        if taiex_data:
            chg = taiex_data.get("change_pct", 0.0) or 0.0
            # Crude RSI proxy from recent change; real RSI needs history
            taiex_rsi = 55.0 if chg > 0.5 else (45.0 if chg < -0.5 else 50.0)

        breadth = fetch_breadth()
        headlines = fetch_news_headlines()
        if breadth is None:
            return None
        return compute_sentiment(breadth, headlines, taiex_rsi)
    except Exception as e:
        logger.debug("sentiment fetch error: %s", e)
        return None


async def _refresh_market_loop() -> None:
    """Background task: refresh global (yfinance) + TW sectors (MIS) + watchlist prices every 60 s."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            tickers = [s["ticker"] for s in _state.get("shortlist", [])]
            global_data, sector_data, wl_data, sentiment = await asyncio.gather(
                loop.run_in_executor(None, _fetch_global_markets_sync),
                loop.run_in_executor(None, _fetch_tw_sectors_sync),
                loop.run_in_executor(None, _fetch_watchlist_prices_sync, tickers),
                loop.run_in_executor(None, _fetch_sentiment_sync),
            )
            if global_data:
                _MARKET_CACHE["global"] = global_data
            if sector_data:
                _MARKET_CACHE["sectors"] = sector_data
            if wl_data:
                _MARKET_CACHE["watchlist"] = wl_data
            if sentiment:
                _MARKET_CACHE["sentiment"] = sentiment
                logger.info("市場情緒數據更新成功")
            _MARKET_CACHE["updated_at"] = datetime.now()
        except Exception as e:
            logger.debug("market refresh error: %s", e)
        await asyncio.sleep(30)


# ── Subprocess helper ────────────────────────────────────────────────────────

import re as _re
_ANSI = _re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\].*?\x07|\r")

def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text).strip()


async def _run_subprocess_async(cmd: list[str], **_) -> tuple[int, str]:
    """Stream subprocess stdout+stderr line-by-line into the log panel.

    Uses asyncio.create_subprocess_exec so each output line appears in the
    Rich panel immediately, rather than waiting for the process to finish.
    """
    script = Path(cmd[1]).name if len(cmd) > 1 else "?"
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,   # no TTY → batch_plan skips interactive menus
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(_ROOT),
    )
    collected: list[str] = []
    async for raw in proc.stdout:
        line = _strip_ansi(raw.decode("utf-8", errors="replace"))
        if line:
            collected.append(line)
            logger.info("[%s] %s", script, line[:120])
    await proc.wait()
    code = proc.returncode
    if code != 0:
        logger.error("[%s] exited with code %d", script, code)
    return code, "\n".join(collected)


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


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler — reports any unhandled exception back to Telegram."""
    import traceback
    err = context.error
    tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    logger.error(f"Unhandled exception: {tb}")

    cmd = _state.get("last_cmd")
    cmd_name = f"/{cmd[0]}" if cmd else "unknown"
    _state["last_cmd"] = (cmd[0] if cmd else "?", "❌ 錯誤", datetime.now()) if cmd else ("?", "❌ 錯誤", datetime.now())

    short = str(err)[:200]
    await _send(f"❌ *指令執行失敗* `{cmd_name}`\n```\n{short}\n```")


def _track(cmd_name: str):
    """Decorator that records command start/done in _state['last_cmd']."""
    def decorator(fn):
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            _state["last_cmd"] = (cmd_name, "⏳ 執行中", datetime.now())
            await fn(update, ctx)
            # Only mark done if no exception (error_handler handles failures)
            if _state["last_cmd"] and _state["last_cmd"][1] != "❌ 錯誤":
                _state["last_cmd"] = (cmd_name, "✅ 完成", datetime.now())
        return wrapper
    return decorator


# ── CSV helpers ──────────────────────────────────────────────────────────────

def _get_latest_name_map() -> dict[str, str]:
    """Load the most recent name_map_YYYY-MM-DD.json from cache."""
    paths = sorted(_NAME_MAP_DIR.glob("name_map_*.json"), reverse=True)
    if not paths:
        return {}
    try:
        return json.loads(paths[0].read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to load name map: {e}")
        return {}


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
    name_map = _get_latest_name_map()
    signals = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("action") not in ("LONG", "WATCH"):
                continue
            conf = int(row.get("confidence", 0) or 0)
            if conf < min_conf:
                continue
            ticker = row["ticker"]
            signals.append({
                "ticker": ticker,
                "name": name_map.get(ticker, ""),
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

async def _job_opening_scan(force: bool = False, notify_fn=None) -> None:
    """09:05 — full market scan, build today's shortlist."""
    if not force and not is_trading_day(date.today()):
        logger.info("opening_scan skipped — not a trading day")
        return
    notify = notify_fn or _send
    async with _state["scan_lock"]:
        t0 = datetime.now()
        logger.info("opening_scan START force=%s", force)
        await notify(f"🔍 *掃描開始* {t0:%H:%M}\n正在執行全市場分析，請稍候...")
        code, out = await _run_subprocess_async([
            sys.executable, "scripts/batch_plan.py",
            "--save-csv", "--save-db",
            "--llm", _state["llm"], "--llm-top", "5",
        ])
        elapsed = int((datetime.now() - t0).total_seconds())
        if code != 0:
            logger.error("opening_scan FAILED code=%d elapsed=%ds\n%s", code, elapsed, out[:500])
            await notify(f"❌ 掃描失敗（{elapsed}s）")
            return
        logger.info("opening_scan batch_plan done elapsed=%ds", elapsed)
        await notify(f"📡 掃描完成（{elapsed}s），正在讀取結果...")
        csv_path = _latest_scan_csv(0)
        if csv_path:
            _state["shortlist"] = _parse_scan_csv(csv_path)
            _state["last_scan_time"] = datetime.now()
            logger.info("opening_scan shortlist loaded n=%d from %s", len(_state["shortlist"]), csv_path.name)
        await notify(format_opening_list(_state["shortlist"], str(date.today())))
        await _send_coil_results(str(date.today()))
        logger.info("opening_scan DONE n=%d", len(_state["shortlist"]))


async def _job_hourly_rescan() -> None:
    """Hourly rescan — update shortlist ranking if not already running."""
    if not is_trading_day(date.today()):
        logger.info("hourly_rescan skipped — not a trading day")
        return
    if _state["scan_lock"].locked():
        logger.info("hourly_rescan skipped — previous scan still running")
        return
    async with _state["scan_lock"]:
        t0 = datetime.now()
        logger.info("hourly_rescan START %s", t0.strftime("%H:%M"))
        code, out = await _run_subprocess_async([
            sys.executable, "scripts/batch_plan.py",
            "--save-csv", "--save-db",
            "--llm", _state["llm"], "--llm-top", "5",
        ])
        if code != 0:
            logger.error("hourly_rescan FAILED code=%d\n%s", code, out[:300])
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
        elapsed = int((datetime.now() - t0).total_seconds())
        logger.info("hourly_rescan done elapsed=%ds added=%s removed=%s", elapsed, sorted(added), sorted(removed))
        if added or removed:
            lines = [f"📊 *名單更新*（{t0:%H:%M}，{elapsed}s）"]
            for t in sorted(added):
                lines.append(f"✨ 新進：{t}")
            for t in sorted(removed):
                lines.append(f"⬇️ 移出：{t}")
            await _send("\n".join(lines))
        else:
            await _send(f"🔄 {t0:%H:%M} 重掃完成（{elapsed}s），名單無異動（共 {len(new_list)} 檔）")


async def _job_precheck(force: bool = False, notify_fn=None) -> None:
    """Every 10 min — check entry conditions for shortlist via precheck.py --csv."""
    if not force and not is_trading_day(date.today()):
        logger.info("precheck skipped — not a trading day")
        return
    if not _state["monitoring_active"]:
        logger.info("precheck skipped — monitoring paused")
        return
    if _state["precheck_lock"].locked():
        logger.info("precheck skipped — previous round still running")
        return
    notify = notify_fn or _send
    async with _state["precheck_lock"]:
        if not _state["shortlist"]:
            logger.info("precheck skipped — shortlist empty")
            await notify("⚠ 名單為空，請先執行 /plan 掃描")
            return
        n = len(_state["shortlist"])
        logger.info("precheck START n=%d force=%s", n, force)
        await notify(f"🔔 *Precheck 開始* {datetime.now():%H:%M}\n正在取得 {n} 檔即時報價...")
        tmp_csv = _write_temp_shortlist_csv(_state["shortlist"])
        try:
            t0 = datetime.now()
            code, out = await _run_subprocess_async([
                sys.executable, "scripts/trade.py",
                "--csv", str(tmp_csv), "--min-confidence", "0",
            ])
        finally:
            tmp_csv.unlink(missing_ok=True)
        elapsed = int((datetime.now() - t0).total_seconds())
        await notify(f"📡 報價取得完成（{elapsed}s），正在分析進場條件...")

        # Parse machine-readable result line: PRECHECK_RESULTS:{"ticker": "PASS"|"WARN"|"SKIP"|"NO_DATA"}
        lines = out.split("\n")
        results_map: dict[str, str] = {}
        for line in lines:
            if line.startswith("PRECHECK_RESULTS:"):
                try:
                    results_map = json.loads(line.split(":", 1)[1])
                except Exception:
                    pass
                break

        triggered_count = 0
        for s in _state["shortlist"]:
            ticker = s["ticker"]
            status = results_map.get(ticker, "")
            triggered = status == "PASS"
            _save_intraday_hit(ticker, s["entry_bid"], triggered)
            if triggered:
                triggered_count += 1
                logger.info("precheck TRIGGERED ticker=%s entry=%.2f", ticker, s["entry_bid"])
                await _send(format_entry_signal(
                    ticker, s.get("name", ""),
                    price=s["entry_bid"],
                    entry_low=s["entry_bid"] * 0.97,
                    entry_high=s["entry_bid"] * 1.03,
                    stop=s["stop_loss"],
                ))

        if not results_map:
            # precheck.py exited before market hours or no CSV found
            logger.warning("precheck got no PRECHECK_RESULTS line (exit code=%d)", code)
            await notify("⚠ Precheck 無結果（可能非盤中時段或找不到 CSV）")
        elif triggered_count == 0:
            logger.info("precheck DONE triggered=0/%d", n)
            await notify(f"⏳ Precheck 完成，{n} 檔均未達進場條件")
        else:
            logger.info("precheck DONE triggered=%d/%d", triggered_count, n)
            await notify(f"✅ Precheck 完成，{triggered_count}/{n} 檔達進場條件")


async def _job_postmarket_report(force: bool = False, notify_fn=None) -> None:
    """17:00 — post-market report with hit rate + tomorrow's list."""
    if not force and not is_trading_day(date.today()):
        logger.info("postmarket_report skipped — not a trading day")
        return
    notify = notify_fn or _send
    logger.info("postmarket_report START force=%s", force)
    await notify(f"📈 *盤後報告生成中* {datetime.now():%H:%M}\n正在執行隔日掃描...")
    async with _state["scan_lock"]:
        t0 = datetime.now()
        code, out = await _run_subprocess_async([sys.executable, "scripts/batch_plan.py", "--save-csv", "--save-db"])
        elapsed = int((datetime.now() - t0).total_seconds())
        if code != 0:
            logger.error("postmarket_report scan FAILED code=%d\n%s", code, out[:300])
        else:
            logger.info("postmarket_report scan done elapsed=%ds", elapsed)

    await notify(f"📊 掃描完成（{elapsed}s），正在計算今日命中率...")
    yesterday_csv = _latest_scan_csv(1)
    yesterday_signals = _parse_scan_csv(yesterday_csv) if yesterday_csv else []
    intraday_hits = _load_intraday_hits(date.today())
    tomorrow_csv = _latest_scan_csv(0)
    tomorrow_signals = _parse_scan_csv(tomorrow_csv) if tomorrow_csv else []
    logger.info("postmarket_report yesterday=%d hits=%d tomorrow=%d", len(yesterday_signals), len(intraday_hits), len(tomorrow_signals))

    await _send(format_postmarket_report(
        yesterday_signals=yesterday_signals,
        intraday_hits=intraday_hits,
        tomorrow_signals=tomorrow_signals,
        report_date=str(date.today()),
    ))
    await _send_coil_results(str(date.today()))
    logger.info("postmarket_report DONE")


async def _job_optimize() -> None:
    """Tue/Fri 18:00 — run AI optimization agent."""
    from taiwan_stock_agent.optimize import run_optimize
    logger.info("optimize START llm=%s", _state["llm"])
    result = await run_optimize(_state["llm"], _send)
    logger.info("optimize DONE result=%s", result)


# ── Telegram command handlers ────────────────────────────────────────────────

@_track("top")
async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        format_opening_list(_state["shortlist"], str(date.today())),
        parse_mode="Markdown",
    )


@_track("status")
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


@_track("pause")
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _state["monitoring_active"] = False
    await update.message.reply_text("⏸ 盤中推播已暫停。/resume 恢復")


@_track("resume")
async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _state["monitoring_active"] = True
    await update.message.reply_text("▶️ 盤中推播已恢復")


@_track("params")
async def cmd_params(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    params = json.loads(_PARAMS_PATH.read_text())
    clean = {k: v for k, v in params.items() if not k.startswith("_") and k != "tunable_whitelist"}
    await update.message.reply_text(
        f"```json\n{json.dumps(clean, indent=2, ensure_ascii=False)}\n```",
        parse_mode="Markdown",
    )


@_track("optimize")
async def cmd_optimize(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("CMD /optimize from user=%s", update.effective_user.id if update.effective_user else "?")
    await _send(f"🤖 *優化 Agent 啟動* {datetime.now():%H:%M}\n依序執行：settle → factor\\_report → LLM → 驗證 → 套用")
    await _job_optimize()


@_track("plan")
async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("CMD /plan from user=%s", update.effective_user.id if update.effective_user else "?")
    if _state["scan_lock"].locked():
        await update.message.reply_text("⚠ 掃描進行中，請稍候")
        return
    await _job_opening_scan(force=True, notify_fn=_send)


@_track("trade")
async def cmd_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("CMD /trade from user=%s", update.effective_user.id if update.effective_user else "?")
    if not _state["shortlist"]:
        await update.message.reply_text("⚠ 名單為空，請先執行 /plan")
        return
    if _state["precheck_lock"].locked():
        await update.message.reply_text("⚠ trade 進行中，請稍候")
        return
    await _job_precheck(force=True, notify_fn=_send)


@_track("report")
async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("CMD /report from user=%s", update.effective_user.id if update.effective_user else "?")
    await _job_postmarket_report(force=True, notify_fn=_send)


@_track("test")
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
        f"手動觸發指令：/plan /trade /report /optimize",
        parse_mode="Markdown",
    )


@_track("approve")
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


@_track("rollback")
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


@_track("help")
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "📖 *指令說明*\n"
        "\n"
        "*手動觸發*\n"
        "/plan         全市場掃描，更新今日名單並推播\n"
        "/trade        對名單即時取報價，達進場條件發警報\n"
        "/report       盤後報告（命中率 \\+ 隔日名單）\n"
        "/optimize     啟動 AI 參數優化 Agent\n"
        "\n"
        "*查詢*\n"
        "/top          查看今日名單（不重新掃描）\n"
        "/status       系統狀態（名單檔數、上次掃描、LLM）\n"
        "/params       查看當前引擎參數\n"
        "\n"
        "*控制*\n"
        "/pause        暫停盤中 precheck 自動推播\n"
        "/resume       恢復盤中 precheck 自動推播\n"
        "\n"
        "*優化*\n"
        "/approve      套用待確認的 AI 優化建議\n"
        "/rollback     還原上一版參數，或捨棄待確認建議\n"
        "\n"
        "*診斷*\n"
        "/test         執行指令邏輯自動測試（7 項驗證）\n"
        "/help         顯示此說明\n"
        "\n"
        "⏰ *自動排程*\n"
        "09:05          開盤掃描 \\+ 推播名單\n"
        "10–13:05       每小時重掃，有異動才推\n"
        "09:05–13:25    每 10 分鐘 precheck\n"
        "17:00          盤後報告\n"
        "週二/五 18:00  AI 優化"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── Visual helpers (Taiwan convention: 紅漲綠跌) ──────────────────────────────

def _chg_color(chg: float) -> str:
    """Color with intensity. Taiwan: red = gain, green = loss."""
    if chg >  2.0: return "bold bright_red"
    if chg >  0.5: return "red"
    if chg >  0.1: return "bright_red"
    if chg > -0.1: return "dim"
    if chg > -0.5: return "bright_green"
    if chg > -2.0: return "green"
    return "bold bright_green"

def _arrow(chg: float) -> str:
    if chg >  0.1: return "[red]▲[/red]"
    if chg < -0.1: return "[green]▼[/green]"
    return "[dim]─[/dim]"

def _bar(chg: float, width: int = 5) -> str:
    """Unicode block bar proportional to |chg|, max at ±3%."""
    filled = min(width, round(abs(chg) / 3.0 * width))
    b = "█" * filled + "░" * (width - filled)
    color = "red" if chg >= 0 else "green"
    return f"[{color}]{b}[/{color}]"

def _heat_bg(chg: float) -> str:
    """256-color background for heat map tiles. Taiwan: red = gain, green = loss."""
    if   chg >  3.0: return "color(88)"    # deep red
    elif chg >  1.5: return "color(124)"   # dark red
    elif chg >  0.5: return "color(160)"   # medium red
    elif chg >  0.1: return "color(196)"   # bright red
    elif chg > -0.1: return "color(238)"   # neutral dark gray
    elif chg > -0.5: return "color(34)"    # medium green
    elif chg > -1.5: return "color(28)"    # dark green
    else:            return "color(22)"    # deep green

def _heat_tile(label: str, ticker: str, chg: float, price: float) -> Text:
    """Colored heat map tile: name + price + change %."""
    bg = _heat_bg(chg)
    bold = abs(chg) > 1.5
    fg = Style(bgcolor=bg, color="white", bold=bold)
    t = Text(justify="center", no_wrap=True)
    t.append(f" {label}  {ticker} \n", style=fg)
    t.append(f" {price:>7.2f}  {chg:>+.2f}% ", style=fg)
    return t


def _heat_tile_compact(abbr: str, chg: float, price: float | None) -> Text:
    """2-line sector tile: Chinese name on top, change% below."""
    t = Text(justify="center", no_wrap=True)
    if price is None:
        t.append(f"{abbr}\n", style="dim")
        t.append(" -- ", style="dim")
        return t
    clr = _chg_color(chg)
    sign = "+" if chg >= 0 else ""
    t.append(f"{abbr}\n", style="dim")
    t.append(f"{sign}{chg:.2f}%", style=clr)
    return t


# ── Coil CSV loader + Telegram push ─────────────────────────────────────────

_COIL_GRADE_LABEL_TG = {
    "COIL_PRIME":  "⭐⭐ 極強蓄積",
    "COIL_MATURE": "⭐ 成熟蓄積",
    "COIL_EARLY":  "蓄積初形",
}

def _load_latest_coil_csv() -> list[dict]:
    """Load top 5 rows from latest coil_*.csv. Returns [] if not found."""
    coil_files = sorted(_SCAN_DIR.glob("coil_*.csv"), reverse=True)
    if not coil_files:
        return []
    try:
        with coil_files[0].open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))[:5]
    except Exception:
        return []


async def _send_coil_results(scan_date: str) -> None:
    """Push COIL_MATURE+ results from latest coil CSV to Telegram."""
    coil_files = sorted(_SCAN_DIR.glob("coil_*.csv"), reverse=True)
    if not coil_files:
        return
    try:
        with coil_files[0].open(newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("grade") in _COIL_GRADE_LABEL_TG]
    except Exception:
        return
    if not rows:
        return

    name_map = _get_latest_name_map()
    lines = [f"🔭 *蓄積雷達觀察清單* `{scan_date}`\n"]
    for row in rows[:10]:
        grade  = row.get("grade", "")
        ticker = row.get("ticker", "")
        name   = name_map.get(ticker) or row.get("name") or ""
        score  = row.get("score", "--")
        vs_high = row.get("vs_60d_high_pct", "--")
        consec  = row.get("inst_consec_days", "--")
        weeks   = row.get("weeks_consolidating", "--")
        label   = _COIL_GRADE_LABEL_TG.get(grade, grade)
        try:
            vs_f = f"{float(vs_high):+.1f}%"
        except (ValueError, TypeError):
            vs_f = "--"
        lines.append(
            f"{label} *{ticker}* {name}\n"
            f"  分數 {score}　vs前高 {vs_f}　法人連買 {consec}d　橫盤 {weeks}w"
        )

    await _send("\n".join(lines))


# ── Rich CLI display — header + 4-quadrant layout ─────────────────────────────

def _render_header() -> Panel:
    """Full-width top bar: title · date · clock · market status · LLM."""
    now = datetime.now()
    is_weekday = now.weekday() < 5
    market_open  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
    market_close = now.replace(hour=13, minute=30, second=0, microsecond=0)
    is_open = is_weekday and market_open <= now <= market_close
    mkt_status = "[bold green]● OPEN[/bold green]" if is_open else "[dim]○ CLOSED[/dim]"
    weekday = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][now.weekday()]

    t = Table(box=None, show_header=False, padding=(0, 2), expand=True)
    t.add_column("title",  justify="left",   ratio=2)
    t.add_column("date",   justify="center", ratio=2)
    t.add_column("clock",  justify="center", ratio=2)
    t.add_column("market", justify="center", ratio=1)
    t.add_column("llm",    justify="right",  ratio=2)
    t.add_row(
        "[bold cyan]STOCK SIGNAL BOT[/bold cyan]",
        f"[dim]{now.strftime('%Y-%m-%d')}  {weekday}[/dim]",
        f"[bold white]{now.strftime('%H:%M:%S')}[/bold white]",
        mkt_status,
        f"[dim]engine: {_state['llm']}[/dim]",
    )
    return Panel(t, box=box.HEAVY_HEAD, border_style="blue", padding=(0, 0))


def _render_status_panel() -> Panel:
    """Top-left: bot operational status."""
    now = datetime.now()
    t = Table(box=None, show_header=False, padding=(0, 1), expand=True)
    t.add_column("label", style="dim", min_width=11, no_wrap=True)
    t.add_column("value", no_wrap=True)

    # Watchlist with fill bar
    n = len(_state["shortlist"])
    fill = min(10, n)
    wl_bar = f"[cyan]{'█' * fill}{'░' * (10 - fill)}[/cyan]"
    t.add_row("Watchlist", f"{wl_bar}  [bold]{n}[/bold] stocks")

    # Last scan with age
    last = _state["last_scan_time"]
    if last:
        age_m = int((now - last).total_seconds() / 60)
        scan_val = f"[white]{last.strftime('%H:%M')}[/white]  [dim]{age_m}m ago[/dim]"
    else:
        scan_val = "[dim]not run yet[/dim]"
    t.add_row("Last Scan", scan_val)

    # Alerts
    if _state["monitoring_active"]:
        t.add_row("Alerts", "[green]● Active[/green]")
    else:
        t.add_row("Alerts", "[yellow]⏸ Paused[/yellow]")

    t.add_row("LLM", f"[dim]{_state['llm']}[/dim]")
    t.add_row("", "")

    # Last command
    cmd = _state.get("last_cmd")
    if cmd:
        cmd_name, cmd_status, cmd_time = cmd
        age = int((now - cmd_time).total_seconds())
        age_str = f"{age}s" if age < 60 else f"{age // 60}m"
        clr = "green" if "✅" in cmd_status else "yellow" if "⏳" in cmd_status else "red"
        t.add_row("Last Cmd", f"[{clr}]/{cmd_name}[/{clr}]  {cmd_status}  [dim]{age_str} ago[/dim]")
    else:
        t.add_row("Last Cmd", "[dim]—[/dim]")

    # Pending AI suggestion
    pending_raw = _PENDING_PATH.read_text().strip() if _PENDING_PATH.exists() else "null"
    if pending_raw not in ("null", ""):
        t.add_row("Pending", "[yellow]⚡ AI suggestion  →  /approve[/yellow]")
    else:
        t.add_row("Pending", "[dim]none[/dim]")

    # Market Sentiment widget
    sentiment = _MARKET_CACHE.get("sentiment")
    t.add_row("", "")
    if sentiment:
        st_clr = {"🟢": "green", "🟡": "yellow", "🔴": "red"}.get(sentiment.emoji, "white")
        t.add_row(
            "[dim]市場輿情[/dim]",
            f"[{st_clr}]{sentiment.emoji} {sentiment.label}[/{st_clr}]",
        )
        t.add_row(
            "",
            f"[dim]漲跌比 {sentiment.ad_ratio:.1f} · RSI {sentiment.taiex_rsi:.0f} · 量 {sentiment.volume_ratio:.1f}×[/dim]",
        )
        if sentiment.alerts:
            t.add_row("", f"[yellow]⚠ {sentiment.alerts[0]}[/yellow]")
        if sentiment.hot_keywords:
            kws = " ".join(sentiment.hot_keywords[:3])
            t.add_row("", f"[cyan]🔥 {kws}[/cyan]")
    else:
        t.add_row("[dim]市場輿情[/dim]", "[dim]⌛ 數據讀取中...[/dim]")

    t.add_row("", "")
    t.add_row("[dim]Schedule[/dim]", "[dim]09:05 scan · 10-13 rescan · 09:05-13:25 trade/10min · 17:00 report · Tue/Fri optimize[/dim]")
    t.add_row("[dim]Commands[/dim]", "[dim]/plan  /trade  /report  /optimize  /approve  /rollback  /top  /test[/dim]")

    return Panel(t, title="[bold blue]Bot Status[/bold blue]", border_style="blue", box=box.ROUNDED)


def _render_log_panel() -> Panel:
    """Bottom-left: live rolling activity log."""
    t = Table(box=None, show_header=False, padding=(0, 0), expand=True)
    t.add_column("dot",  width=2, no_wrap=True)
    t.add_column("time", width=9, style="dim", no_wrap=True)
    t.add_column("msg",  no_wrap=True)

    level_cfg = {
        "INFO":     ("[dim]·[/dim]",            "dim"),
        "WARNING":  ("[yellow]●[/yellow]",       "yellow"),
        "ERROR":    ("[bold red]●[/bold red]",   "red"),
        "CRITICAL": ("[bold red]◆[/bold red]",   "bold red"),
    }
    entries = list(_LOG_LINES)
    if entries:
        for ts, lvl, msg in entries:
            dot, clr = level_cfg.get(lvl, ("[dim]·[/dim]", "dim"))
            t.add_row(dot, f" {ts}", f"[{clr}]{msg}[/{clr}]")
    else:
        t.add_row("[dim]·[/dim]", "", "[dim]Waiting for activity...[/dim]")

    return Panel(t, title="[bold]Activity Log[/bold]", border_style="dim", box=box.ROUNDED)


def _render_market_panel() -> Panel:
    """Top-right: TAIEX headline + 4×7 sector heat map (28 TWSE sectors via MIS)."""
    global_data = _MARKET_CACHE.get("global", {})
    sectors = _MARKET_CACHE.get("sectors", [])

    # ── TAIEX headline ──────────────────────────────────────────────────────
    taiex_d = global_data.get("taiex")
    if taiex_d:
        chg   = taiex_d["change_pct"]
        price = taiex_d["price"]
        clr   = _chg_color(chg)
        taiex_row = Table(box=None, show_header=False, padding=(0, 1), expand=True)
        taiex_row.add_column("a", no_wrap=True)
        taiex_row.add_column("b", justify="right", no_wrap=True)
        taiex_row.add_column("c", justify="right", no_wrap=True)
        taiex_row.add_column("d", no_wrap=True)
        taiex_row.add_row(
            f"[bold]{_arrow(chg)} TAIEX[/bold]",
            f"[{clr}][bold]{price:,.0f}[/bold][/{clr}]",
            f"[{clr}]{chg:+.2f}%[/{clr}]",
            _bar(chg, 8),
        )
    else:
        taiex_row = Text("[dim] TAIEX  --[/dim]")

    # ── 4-column × 7-row sector heat map ────────────────────────────────────
    COLS = 4
    grid = Table(box=None, show_header=False, padding=(0, 0), expand=True)
    for _ in range(COLS):
        grid.add_column(ratio=1)

    # Pad to full multiple of COLS (28 = 4×7, already exact)
    tiles = list(sectors)
    while len(tiles) % COLS:
        tiles.append({"abbr": "", "price": None, "change_pct": 0.0})

    for row_start in range(0, len(tiles), COLS):
        row = tiles[row_start : row_start + COLS]
        grid.add_row(*[
            _heat_tile_compact(s["abbr"], s["change_pct"], s["price"])
            for s in row
        ])

    if not sectors:
        loading = Text("[dim]  Loading sector data...[/dim]")
        content = Group(taiex_row, Rule(style="dim"), loading)
    else:
        content = Group(taiex_row, Rule(style="dim"), grid)

    updated = _MARKET_CACHE.get("updated_at")
    sub = f"[dim]Last update {updated.strftime('%H:%M:%S')}[/dim]" if updated else "[dim]loading...[/dim]"
    return Panel(
        content,
        title="[bold green]Market Monitor[/bold green]",
        subtitle=sub,
        border_style="green",
        box=box.ROUNDED,
    )


def _render_global_panel() -> Panel:
    """Bottom-right: forex / US equity / commodities / crypto with trend bars."""
    data = _MARKET_CACHE.get("global", {})

    t = Table(box=None, show_header=False, padding=(0, 0), expand=True)
    t.add_column("arr",   width=2,  no_wrap=True)
    t.add_column("name",  min_width=12, no_wrap=True)
    t.add_column("price", justify="right", min_width=11, no_wrap=True)
    t.add_column("chg",   justify="right", min_width=7,  no_wrap=True)
    t.add_column("bar",   min_width=6, no_wrap=True)

    def _sec(title: str) -> None:
        t.add_row("", f"[dim]{title}[/dim]", "", "", "")

    def _row(label: str, key: str, fmt: str) -> None:
        d = data.get(key)
        if d and d.get("price") is not None:
            price, chg = d["price"], d["change_pct"]
            clr = _chg_color(chg)
            t.add_row(
                _arrow(chg), f" {label}",
                f"[{clr}]{fmt.format(price)}[/{clr}]",
                f"[{clr}]{chg:+.2f}%[/{clr}]",
                _bar(chg),
            )
        else:
            t.add_row("[dim]─[/dim]", f" {label}", "[dim]  --[/dim]", "[dim]  --[/dim]", "[dim]░░░░░[/dim]")

    _sec("── Forex ──────────────────────")
    _row("USD/TWD", "usd_twd", "{:.3f}")
    _row("USD/JPY", "usd_jpy", "{:.2f}")
    _row("DXY",     "dxy",     "{:.2f}")

    _sec("── US Markets ─────────────────")
    _row("SOX (Semi)", "sox",    "{:,.0f}")
    _row("NASDAQ",     "nasdaq", "{:,.0f}")
    _row("S&P 500",    "sp500",  "{:,.0f}")
    _row("Dow Jones",  "dow",    "{:,.0f}")
    _row("VIX",        "vix",    "{:.2f}")

    _sec("── Commodities ────────────────")
    _row("Gold",    "gold",    "${:,.0f}")
    _row("Silver",  "silver",  "${:.2f}")
    _row("WTI Oil", "oil_wti", "${:.2f}")
    _row("Copper",  "copper",  "${:.3f}")
    _row("Nat Gas", "natgas",  "${:.3f}")

    _sec("── Crypto ─────────────────────")
    _row("Bitcoin", "btc", "${:,.0f}")

    updated = _MARKET_CACHE.get("updated_at")
    sub = f"[dim]Last update {updated.strftime('%H:%M:%S')}[/dim]" if updated else "[dim]loading...[/dim]"
    return Panel(t, title="[bold yellow]Global Markets[/bold yellow]", subtitle=sub,
                 border_style="yellow", box=box.ROUNDED)


# ── LLM selection ────────────────────────────────────────────────────────────

def _select_llm(arg: str | None) -> str:
    if arg:
        return arg
    _console.print("\n[bold]Select LLM for optimize_agent:[/bold]")
    _console.print("  [1] Claude (claude-sonnet-4-6)  ← default")
    _console.print("  [2] Gemini (gemini-2.5-flash)")
    _console.print("  [3] OpenAI (gpt-4o)")
    _console.print("  [4] GLM   (glm-4-flash, requires ZHIPUAI_API_KEY)")
    choice = input("Choice (Enter = 1): ").strip()
    return {"2": "gemini", "3": "openai", "4": "glm"}.get(choice, "claude")


def _render_watchlist_detail_panel() -> Panel:
    """Right-column top: live watchlist prices with real market data."""
    shortlist = _state["shortlist"]
    if not shortlist:
        return Panel(Text("\n  (尚無名單數據)", style="dim"), title="[bold cyan]Watchlist Prices[/bold cyan]", border_style="cyan")

    wl_data = _MARKET_CACHE.get("watchlist", {})

    t = Table(box=None, show_header=True, header_style="bold cyan", padding=(0, 1), expand=True)
    t.add_column("代號",   style="dim",     width=6,   no_wrap=True)
    t.add_column("名稱",                    max_width=6, no_wrap=True)
    t.add_column("現價",   justify="right", min_width=7, no_wrap=True)
    t.add_column("漲跌%",  justify="right", min_width=8, no_wrap=True)
    t.add_column("信心",   justify="right", width=4,   no_wrap=True)
    t.add_column("vs進場", justify="right", min_width=7, no_wrap=True)

    for s in shortlist[:20]:
        ticker = s["ticker"]
        name   = (s.get("name") or "")[:5]
        conf   = s.get("confidence", 0)
        entry  = s.get("entry_bid") or 0.0
        action = s.get("action", "")
        d      = wl_data.get(ticker)

        badge_clr = "cyan" if action == "LONG" else "yellow"

        if d and d.get("price") is not None:
            price = d["price"]
            chg   = d["change_pct"]
            live  = d.get("is_live", False)
            clr   = _chg_color(chg)
            sign  = "+" if chg >= 0 else ""
            price_str = f"[{clr}]{price:.2f}[/{clr}]" if live else f"[dim]{price:.2f}[/dim]"
            chg_str   = f"[{clr}]{_arrow(chg)} {sign}{chg:.2f}%[/{clr}]" if live else "[dim] --  --[/dim]"
            if entry > 0:
                diff = (price - entry) / entry * 100
                vs_clr = "red" if diff >= 0 else "green"
                vs_str = f"[{vs_clr}]{diff:+.1f}%[/{vs_clr}]"
            else:
                vs_str = "[dim]--[/dim]"
        else:
            price_str = "[dim]--[/dim]"
            chg_str   = "[dim]--[/dim]"
            vs_str    = "[dim]--[/dim]"

        t.add_row(
            f"[{badge_clr}]{ticker}[/{badge_clr}]",
            name, price_str, chg_str,
            f"[dim]{conf}[/dim]",
            vs_str,
        )

    return Panel(t, title=f"[bold cyan]Watchlist Prices ({len(shortlist)})[/bold cyan]", border_style="cyan", box=box.ROUNDED)


_COIL_GRADE_COLOR = {
    "COIL_PRIME":  "bold magenta",
    "COIL_MATURE": "bold cyan",
    "COIL_EARLY":  "yellow",
}
_COIL_GRADE_LABEL = {
    "COIL_PRIME":  "★★ 極強蓄積",
    "COIL_MATURE": "★ 成熟蓄積",
    "COIL_EARLY":  "蓄積初形",
}


def _latest_coil_csv() -> Path | None:
    """Return the most recent coil_YYYY-MM-DD.csv under data/scans/, up to 7 days back."""
    scans_dir = _ROOT / "data" / "scans"
    for offset in range(7):
        p = scans_dir / f"coil_{(date.today() - timedelta(days=offset)).isoformat()}.csv"
        if p.exists():
            return p
    return None


def _render_coil_panel() -> Panel:
    """Right-column bottom: accumulation radar from latest coil CSV."""
    csv_path = _latest_coil_csv()

    t = Table(box=None, show_header=True, header_style="bold magenta", padding=(0, 1), expand=True)
    t.add_column("代號",   style="dim",     width=6,   no_wrap=True)
    t.add_column("名稱",                    max_width=6, no_wrap=True)
    t.add_column("分數",  justify="right",  width=4,   no_wrap=True)
    t.add_column("等級",                    min_width=10, no_wrap=True)
    t.add_column("vs前高", justify="right", min_width=7, no_wrap=True)

    if csv_path:
        try:
            name_map = _get_latest_name_map()
            with open(csv_path, newline="", encoding="utf-8") as f:
                for r in list(csv.DictReader(f))[:10]:
                    ticker = r["ticker"]
                    name   = (name_map.get(ticker) or r.get("name") or "")[:5]
                    score  = r.get("score", "--")
                    grade  = r.get("grade", "")
                    vs_raw = r.get("vs_60d_high_pct", "")
                    try:
                        vs_str = f"{float(vs_raw):+.1f}%"
                    except (ValueError, TypeError):
                        vs_str = "[dim]--[/dim]"
                    style = _COIL_GRADE_COLOR.get(grade, "white")
                    label = _COIL_GRADE_LABEL.get(grade, grade)
                    t.add_row(
                        f"[{style}]{ticker}[/{style}]",
                        name, score,
                        f"[{style}]{label}[/{style}]",
                        vs_str,
                    )
        except Exception as e:
            logger.warning("coil panel error: %s", e)

    if t.row_count == 0:
        return Panel(
            Text("\n  (尚無蓄積雷達數據)", style="dim"),
            title="[bold magenta]Accumulation Radar[/bold magenta]",
            border_style="magenta",
        )

    subtitle = f"[dim]{csv_path.stem.replace('coil_', '')}[/dim]" if csv_path else ""

    # Append live tracking summary below the table
    tracking_summary = _coil_tracking_summary()
    content = t if not tracking_summary else Table.grid(padding=0)

    if tracking_summary:
        from rich.console import Group
        grid = Table.grid(padding=(0, 0))
        grid.add_column()
        grid.add_row(t)
        grid.add_row(Text(tracking_summary, style="dim"))
        return Panel(grid, title="[bold magenta]Accumulation Radar[/bold magenta]",
                     subtitle=subtitle, border_style="magenta", box=box.ROUNDED)

    return Panel(t, title="[bold magenta]Accumulation Radar[/bold magenta]", subtitle=subtitle,
                 border_style="magenta", box=box.ROUNDED)


def _coil_tracking_summary() -> str:
    """One-line tracking summary for the coil panel footer."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from coil_monitor import get_bot_summary, GRADE_ORDER, MIN_SAMPLE_FOR_WINRATE  # type: ignore[import]
        data = get_bot_summary(days=30)
        if not data:
            return ""
        parts = []
        for grade in GRADE_ORDER:
            gs = data["grade_stats"].get(grade, {})
            n, wins = gs.get("n", 0), gs.get("wins", 0)
            short = {"COIL_PRIME": "PRIME", "COIL_MATURE": "MATURE", "COIL_EARLY": "EARLY"}[grade]
            if n < MIN_SAMPLE_FOR_WINRATE:
                parts.append(f"{short} N={n}不足")
            else:
                pct = wins / n * 100
                parts.append(f"{short} {pct:.0f}%(N={n})")
        pending = data.get("pending", 0)
        new_today = data.get("new_today", 0)
        avg_mae = data.get("avg_mae")
        mae_str = f" MAE avg:{avg_mae:.1f}%" if avg_mae is not None else ""
        return "  ".join(parts) + f"  待:{pending}  今日新增:{new_today}" + mae_str
    except Exception:
        return ""


# ── Main ─────────────────────────────────────────────────────────────────────

def _try_preload_shortlist() -> None:
    """On startup, populate shortlist from the most recent scan CSV (today or yesterday)."""
    for offset in (0, 1, 2):
        csv_path = _latest_scan_csv(offset)
        if csv_path:
            signals = _parse_scan_csv(csv_path)
            if signals:
                _state["shortlist"] = signals
                _state["last_scan_time"] = datetime.fromtimestamp(csv_path.stat().st_mtime)
                logger.info("startup: preloaded shortlist n=%d from %s", len(signals), csv_path.name)
                return
    logger.info("startup: no scan CSV found, shortlist empty")


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
    _try_preload_shortlist()
    logger.info("Bot START llm=%s chat_id=%s log=%s", llm, chat_id, _LOG_PATH)

    app = Application.builder().token(token).build()
    _state["app"] = app
    app.add_error_handler(_error_handler)

    for cmd_name, handler in [
        ("top", cmd_top), ("status", cmd_status),
        ("pause", cmd_pause), ("resume", cmd_resume),
        ("params", cmd_params), ("optimize", cmd_optimize),
        ("approve", cmd_approve), ("rollback", cmd_rollback),
        ("plan", cmd_plan), ("trade", cmd_trade), ("report", cmd_report),
        ("test", cmd_test), ("help", cmd_help),
    ]:
        app.add_handler(CommandHandler(cmd_name, handler))

    scheduler = AsyncIOScheduler()
    # 09:05 opening scan (Mon–Fri)
    scheduler.add_job(_job_opening_scan, "cron", day_of_week="mon-fri", hour=9, minute=5)
    # Hourly rescan 10:05–13:05
    for h in [10, 11, 12, 13]:
        scheduler.add_job(_job_hourly_rescan, "cron", day_of_week="mon-fri", hour=h, minute=5)
    # 10-min precheck 09:05–13:25 (market hours, close at 13:30)
    scheduler.add_job(
        _job_precheck, "cron",
        day_of_week="mon-fri",
        hour="9-12",
        minute="5,15,25,35,45,55",
    )
    scheduler.add_job(
        _job_precheck, "cron",
        day_of_week="mon-fri",
        hour="13",
        minute="5,15,25",
    )
    # 17:00 post-market report
    scheduler.add_job(_job_postmarket_report, "cron", day_of_week="mon-fri", hour=17, minute=0)
    # 18:00 optimize on Tue and Fri
    scheduler.add_job(_job_optimize, "cron", day_of_week="tue,fri", hour=18, minute=0)
    scheduler.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Start background market data refresh
    market_task = asyncio.create_task(_refresh_market_loop())

    # Build dashboard: header strip + main body + log footer
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main", ratio=1),
        Layout(name="footer", size=7),
    )
    layout["main"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=1),
    )
    
    # Left Column: Bot Status, Market Monitor, Global Markets (3 blocks)
    layout["left"].split_column(
        Layout(name="bot_l", ratio=1),
        Layout(name="market", ratio=1),
        Layout(name="global", ratio=1),
    )
    
    # Right Column: Watchlist Prices, Accumulation Radar (2 blocks)
    layout["right"].split_column(
        Layout(name="watchlist", ratio=2),
        Layout(name="coil", ratio=1),
    )

    def _update_layout() -> None:
        layout["header"].update(_render_header())
        layout["bot_l"].update(_render_status_panel())
        layout["market"].update(_render_market_panel())
        layout["global"].update(_render_global_panel())
        layout["watchlist"].update(_render_watchlist_detail_panel())
        layout["coil"].update(_render_coil_panel())
        layout["footer"].update(_render_log_panel())

    _update_layout()
    try:
        with Live(layout, console=_console, refresh_per_second=1, screen=True) as live:
            while True:
                _update_layout()
                live.refresh()
                await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        market_task.cancel()
        scheduler.shutdown(wait=False)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Taiwan stock signal Telegram bot")
    parser.add_argument("--llm", default=None, choices=["claude", "gemini", "openai", "glm"],
                        help="LLM for optimize_agent (skips interactive prompt)")
    args = parser.parse_args()

    _console.print("[bold blue]Stock Signal Bot v1.0[/bold blue]")
    _console.print("─" * 40)
    llm = _select_llm(args.llm)
    _console.print(f"\nStarting... LLM={llm}")

    asyncio.run(main_async(llm))


if __name__ == "__main__":
    main()
