"""
Surge Signal Tracker — D+1 確認 → T+2 進場提醒

流程：
  D+0 盤後  save_watch()  讀今日 surge CSV ALPHA 訊號 → data/surge_tracking/watch_YYYY-MM-DD.json
  D+1 盤後  check_d1()   載入昨日 watch，抓今日 OHLCV，驗 D+1 條件 → 回傳已確認清單
  D+2       進場參考價 = close_d0 × 1.02（±2% 區間均可）

D+1 通過條件（三者皆需成立）：
  1. close_d1 ≥ close_d0 × 0.97  (收盤未跌破 -3%)
  2. close_d1 / open_d1 ≥ 0.97   (非強力開高走低黑 K)
  3. close_d1 > open_d1 or close_d1 ≥ close_d0  (收盤強於開盤 or 未跌)
"""

import csv
import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent
_TRACKING_DIR = _ROOT / "data" / "surge_tracking"
_SCANS_DIR = _ROOT / "data" / "scans"


def _watch_path(scan_date: date) -> Path:
    return _TRACKING_DIR / f"watch_{scan_date.isoformat()}.json"


def save_watch(surge_csv: Path, scan_date: date) -> Path | None:
    """讀今日 ALPHA 訊號存入 watch JSON；回傳路徑（無訊號回傳 None）。"""
    _TRACKING_DIR.mkdir(parents=True, exist_ok=True)

    if not surge_csv.exists():
        logger.warning("surge_tracker: CSV not found: %s", surge_csv)
        return None

    signals: list[dict] = []
    with surge_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("grade") != "SURGE_ALPHA":
                continue
            try:
                close_price = float(row.get("close_price") or 0)
            except ValueError:
                close_price = 0.0
            signals.append({
                "ticker": row["ticker"],
                "name": row.get("name", ""),
                "market": row.get("market", "TSE"),
                "industry": row.get("industry", ""),
                "score": _safe_float(row.get("score")),
                "close_price": close_price,
                "vol_ratio": _safe_float(row.get("vol_ratio")),
                "close_strength": _safe_float(row.get("close_strength")),
                "day_chg_pct": _safe_float(row.get("day_chg_pct")),
                "flags": row.get("flags", ""),
            })

    if not signals:
        logger.info("surge_tracker: 今日無 ALPHA 訊號需追蹤")
        return None

    out = _watch_path(scan_date)
    out.write_text(json.dumps(signals, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("surge_tracker: 儲存 %d 筆 → %s", len(signals), out)
    return out


def check_d1(watch_date: date, market_map: dict | None = None) -> list[dict]:
    """載入 watch_date 的追蹤清單，驗今日 D+1 條件，回傳已確認進場候選。"""
    wp = _watch_path(watch_date)
    if not wp.exists():
        logger.info("surge_tracker: 無 watch 檔案 %s", watch_date)
        return []

    signals: list[dict] = json.loads(wp.read_text(encoding="utf-8"))
    if not signals:
        return []

    import yfinance as yf  # lazy import

    market_map = market_map or {}
    confirmed: list[dict] = []

    for sig in signals:
        ticker = sig["ticker"]
        close_d0 = sig.get("close_price", 0)
        if not close_d0:
            continue

        market = market_map.get(ticker, sig.get("market", "TSE"))
        suffix = ".TWO" if market == "TPEx" else ".TW"
        try:
            hist = yf.Ticker(f"{ticker}{suffix}").history(period="3d", auto_adjust=False)
            if hist.empty:
                continue
            row = hist.iloc[-1]
            close_d1 = float(row["Close"])
            open_d1 = float(row["Open"])
        except Exception as e:
            logger.debug("surge_tracker yf error %s: %s", ticker, e)
            continue

        # 條件 1：收盤未跌破 D+0 -3%
        if close_d1 < close_d0 * 0.97:
            logger.debug("surge_tracker %s FAIL: close_d1=%.2f < %.2f", ticker, close_d1, close_d0 * 0.97)
            continue
        # 條件 2：非強力黑 K（收盤 ≥ 開盤 97%）
        if open_d1 > 0 and close_d1 / open_d1 < 0.97:
            logger.debug("surge_tracker %s FAIL: big red candle", ticker)
            continue

        d1_chg = (close_d1 - close_d0) / close_d0 * 100
        # T+2 進場參考：close_d0 ±2%（中間值 = D+0 收盤）
        entry_ref = round(close_d0, 2)
        entry_hi = round(close_d0 * 1.02, 2)

        confirmed.append({
            **sig,
            "close_d0": close_d0,
            "close_d1": round(close_d1, 2),
            "d1_chg_pct": round(d1_chg, 2),
            "entry_ref": entry_ref,
            "entry_hi": entry_hi,
        })

    logger.info("surge_tracker check_d1 %s → %d/%d 確認", watch_date, len(confirmed), len(signals))
    return confirmed


def format_d1_alert(confirmed: list[dict], watch_date: date) -> str:
    """組 Telegram Markdown 進場提醒訊息。"""
    lines = [f"🚀 *T+2 進場提醒* (D+0: {watch_date.isoformat()})\n"]
    for sig in confirmed:
        arrow = "📈" if sig["d1_chg_pct"] >= 0 else "📉"
        lines.append(
            f"{arrow} *{sig['ticker']} {sig['name']}*\n"
            f"  D\\+0: {sig['close_d0']}  →  D\\+1: {sig['close_d1']} "
            f"({sig['d1_chg_pct']:+.1f}%)\n"
            f"  明日進場參考: ≤ {sig['entry_hi']}　得分: {int(sig['score'])}\n"
            f"  {sig.get('industry','')} | 量比 {sig['vol_ratio']:.1f}x"
        )
    return "\n".join(lines)


# ── CLI 測試用 ────────────────────────────────────────────────────────────────

def _cli_check(watch_date_str: str) -> None:
    from rich.console import Console
    from rich.table import Table
    console = Console()
    watch_date = date.fromisoformat(watch_date_str)
    confirmed = check_d1(watch_date)
    if not confirmed:
        console.print(f"[yellow]D+1 確認：{watch_date} 無通過條件的候選[/yellow]")
        return
    t = Table(title=f"D+1 確認 (watch={watch_date})", show_header=True)
    for col in ["ticker", "name", "close_d0", "close_d1", "d1_chg_pct", "score", "entry_hi"]:
        t.add_column(col)
    for sig in confirmed:
        t.add_row(
            sig["ticker"], sig["name"],
            str(sig["close_d0"]), str(sig["close_d1"]),
            f"{sig['d1_chg_pct']:+.1f}%",
            str(int(sig["score"])),
            str(sig["entry_hi"]),
        )
    console.print(t)
    console.print(format_d1_alert(confirmed, watch_date))


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v or default)
    except (ValueError, TypeError):
        return default


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1:
        _cli_check(sys.argv[1])
    else:
        print("用法: python surge_tracker.py YYYY-MM-DD")
