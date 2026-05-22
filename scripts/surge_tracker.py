"""
Surge Signal Tracker — D+1 確認 → T+2 進場提醒

流程：
  D+0 盤後  save_watch()  從 DB surge_signals 取今日 ALPHA 訊號 → 寫入 surge_watch 表
  D+1 盤後  check_d1()   載入昨日 surge_watch，抓今日 OHLCV，驗 D+1 條件 → 回傳已確認清單
  D+2       進場參考價 = close_d0 × 1.02（±2% 區間均可）

D+1 通過條件（三者皆需成立）：
  1. close_d1 ≥ close_d0 × 0.97  (收盤未跌破 -3%)
  2. close_d1 / open_d1 ≥ 0.97   (非強力開高走低黑 K)
  3. close_d1 > open_d1 or close_d1 ≥ close_d0  (收盤強於開盤 or 未跌)
"""

import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

logger = logging.getLogger(__name__)

_SURGE_ALPHA = "SURGE_ALPHA"


def _db_available() -> bool:
    import os
    return bool(os.environ.get("DATABASE_URL"))


def save_watch(scan_date: date) -> int:
    """讀今日 DB surge_signals ALPHA 訊號 → 寫入 surge_watch；回傳筆數。"""
    if not _db_available():
        logger.warning("surge_tracker: DATABASE_URL 未設定，略過 watch 儲存")
        return 0
    from taiwan_stock_agent.infrastructure.db import init_pool
    from taiwan_stock_agent.infrastructure.surge_recorder import query_surge_signals, save_surge_watch
    init_pool()

    alphas = query_surge_signals(scan_date, grades={_SURGE_ALPHA})
    if not alphas:
        logger.info("surge_tracker: 今日無 ALPHA 訊號需追蹤")
        return 0

    n = save_surge_watch(alphas, scan_date)
    logger.info("surge_tracker: 儲存 %d 筆 ALPHA → surge_watch (scan_date=%s)", n, scan_date)
    return n


def check_d1(watch_date: date, market_map: dict | None = None) -> list[dict]:
    """載入 watch_date 的追蹤清單，驗今日 D+1 條件，回傳已確認進場候選。"""
    if not _db_available():
        return []
    from taiwan_stock_agent.infrastructure.db import init_pool
    from taiwan_stock_agent.infrastructure.surge_recorder import confirm_surge_watch, load_surge_watch
    init_pool()

    signals = load_surge_watch(watch_date)
    if not signals:
        logger.info("surge_tracker: 無 watch 記錄 %s", watch_date)
        return []

    import yfinance as yf  # lazy import

    market_map = market_map or {}
    confirmed: list[dict] = []

    for sig in signals:
        ticker = sig["ticker"]
        close_d0 = sig.get("close_price") or 0
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

        if close_d1 < close_d0 * 0.97:
            continue
        if open_d1 > 0 and close_d1 / open_d1 < 0.97:
            continue

        d1_chg = (close_d1 - close_d0) / close_d0 * 100
        confirm_surge_watch(watch_date, ticker, round(close_d1, 2), round(d1_chg, 2))

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
            f"  明日進場參考: ≤ {sig['entry_hi']}　得分: {int(sig['score'] or 0)}\n"
            f"  {sig.get('industry','')} | 量比 {sig.get('vol_ratio', 0):.1f}x"
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
            sig["ticker"], sig.get("name", ""),
            str(sig["close_d0"]), str(sig["close_d1"]),
            f"{sig['d1_chg_pct']:+.1f}%",
            str(int(sig.get("score") or 0)),
            str(sig["entry_hi"]),
        )
    console.print(t)
    console.print(format_d1_alert(confirmed, watch_date))


if __name__ == "__main__":
    import sys as _sys
    logging.basicConfig(level=logging.INFO)
    if len(_sys.argv) > 1:
        _cli_check(_sys.argv[1])
    else:
        print("用法: python surge_tracker.py YYYY-MM-DD")
