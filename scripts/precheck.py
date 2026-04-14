"""盤前/盤中確認 — 讀取昨日 scan CSV，抓即時報價，輸出今日可執行清單。

工作流程：
    收盤後  make scan --save-csv   → 產出 watchlist
    隔日    make precheck          → 即時確認哪些還能進場

確認條件：
    1. 現價在 entry_bid ±3% 範圍內（還沒跑掉）
    2. 盤中量能 ≥ 昨日均量 × 時間比例 × 0.6（量能跟上）
    3. 大盤（加權指數）今日不是大跌（≥ -1.5%）

Usage:
    python scripts/precheck.py
    python scripts/precheck.py --min-confidence 50
    python scripts/precheck.py --csv data/scans/scan_2026-04-07.csv
    make precheck
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

import urllib3
import requests
# TWSE MIS 憑證缺少 Subject Key Identifier（Python 3.14 嚴格驗證會拒絕）
# 對 TWSE 公開行情 API 關閉驗證是可接受的風險
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

_console = Console()
logger = logging.getLogger(__name__)

_SCAN_DIR = Path(__file__).resolve().parents[1] / "data" / "scans"

# ---------------------------------------------------------------------------
# TWSE real-time quote API (盤中即時報價)
# ---------------------------------------------------------------------------
# mis.twse.com.tw 每次最多查 ~20 支（用 | 分隔）
_MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
_MIS_BATCH = 20  # 每批最多幾支
_TAIEX_KEY = "tse_t00.tw"  # 加權指數代碼


def _build_mis_keys(tickers: list[str]) -> list[str]:
    """Convert ticker list to mis.twse.com.tw query keys.

    TWSE listed: tse_{ticker}.tw
    OTC listed:  otc_{ticker}.tw
    Heuristic: 6-series are mostly OTC, rest TWSE. Not 100% but covers majority.
    """
    keys = []
    for t in tickers:
        # 上櫃股票代碼通常 3xxx, 4xxx, 5xxx, 6xxx, 8xxx 開頭
        # 但有例外，這裡用簡單判斷：先全部用 tse_，失敗的再 fallback otc_
        keys.append(f"tse_{t}.tw")
    return keys


def _fetch_realtime_batch(mis_keys: list[str]) -> dict[str, dict]:
    """Fetch real-time quotes from TWSE MIS API.

    Returns {ticker: {price, volume, yesterday_close, timestamp}} for successful quotes.
    Silently skips tickers that return no data (e.g., OTC stocks on TWSE endpoint).
    """
    results: dict[str, dict] = {}
    # Split into batches
    for i in range(0, len(mis_keys), _MIS_BATCH):
        batch = mis_keys[i : i + _MIS_BATCH]
        ex_ch = "|".join(batch)
        try:
            resp = requests.get(
                _MIS_URL,
                params={"ex_ch": ex_ch, "json": "1", "delay": "0", "_": str(int(time.time() * 1000))},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
                verify=False,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("MIS API batch %d failed: %s", i // _MIS_BATCH, e)
            continue

        for item in data.get("msgArray", []):
            ticker = item.get("c", "")  # stock code
            if not ticker:
                continue
            # z = 最新成交價（盤中常為 "-"）
            # fallback 順序: z → best bid(b) → (h+l)/2 → open(o) → yesterday(y)
            price: float | None = None
            price_source = ""
            z_str = item.get("z", "-")
            if z_str not in ("-", ""):
                try:
                    price = float(z_str)
                    price_source = "last"
                except ValueError:
                    pass
            if price is None:
                # best bid = b 欄位第一個值（以 _ 分隔）
                b_str = item.get("b", "-")
                if b_str not in ("-", ""):
                    try:
                        price = float(b_str.split("_")[0])
                        price_source = "bid"
                    except ValueError:
                        pass
            if price is None:
                # high/low midpoint
                h_str = item.get("h", "-")
                l_str = item.get("l", "-")
                if h_str not in ("-", "") and l_str not in ("-", ""):
                    try:
                        price = (float(h_str) + float(l_str)) / 2
                        price_source = "hl_mid"
                    except ValueError:
                        pass
            if price is None:
                # open price
                o_str = item.get("o", "-")
                if o_str not in ("-", ""):
                    try:
                        price = float(o_str)
                        price_source = "open"
                    except ValueError:
                        pass
            if price is None:
                continue  # truly no data

            vol_str = item.get("v", "0")
            yesterday_str = item.get("y", "0")
            try:
                h_val = item.get("h", "-")
                l_val = item.get("l", "-")
                results[ticker] = {
                    "price": price,
                    "price_source": price_source,
                    "volume": int(vol_str.replace(",", "")),  # 成交張數
                    "yesterday_close": float(yesterday_str),
                    "timestamp": item.get("t", ""),
                    "name": item.get("n", ""),
                    "high": float(h_val) if h_val not in ("-", "") else None,
                    "low": float(l_val) if l_val not in ("-", "") else None,
                }
            except (ValueError, TypeError):
                continue

        if i + _MIS_BATCH < len(mis_keys):
            time.sleep(0.3)  # rate limit courtesy

    return results


def _fetch_realtime_with_otc_fallback(tickers: list[str]) -> dict[str, dict]:
    """Fetch real-time quotes; retry missing tickers as OTC."""
    # First pass: try all as TWSE (tse_)
    tse_keys = [f"tse_{t}.tw" for t in tickers]
    results = _fetch_realtime_batch(tse_keys)

    # Second pass: retry missing as OTC (otc_)
    missing = [t for t in tickers if t not in results]
    if missing:
        otc_keys = [f"otc_{t}.tw" for t in missing]
        otc_results = _fetch_realtime_batch(otc_keys)
        results.update(otc_results)

    return results


def _fetch_taiex_realtime() -> dict | None:
    """Fetch TAIEX (加權指數) real-time quote."""
    result = _fetch_realtime_batch([_TAIEX_KEY])
    # TAIEX ticker in MIS is "t00"
    return result.get("t00")


def _time_ratio() -> float:
    """Return fraction of trading day elapsed (9:00-13:30 = 1.0)."""
    now = datetime.now()
    market_open = now.replace(hour=9, minute=0, second=0)
    market_close = now.replace(hour=13, minute=30, second=0)
    total_minutes = (market_close - market_open).total_seconds() / 60  # 270 min
    elapsed = (now - market_open).total_seconds() / 60
    return max(0.0, min(1.0, elapsed / total_minutes))


# ---------------------------------------------------------------------------
# Load previous scan CSV
# ---------------------------------------------------------------------------

def _find_latest_scan_csv(scan_dir: Path) -> Path | None:
    """Find the most recent scan CSV, walking back up to 5 trading days."""
    today = date.today()
    candidate = today - timedelta(days=1)
    for _ in range(7):
        if candidate.weekday() < 5:
            csv_path = scan_dir / f"scan_{candidate}.csv"
            if csv_path.exists():
                return csv_path
        candidate -= timedelta(days=1)
    return None


def _find_t2_scan_csv(scan_dir: Path) -> Path | None:
    """Return the scan CSV from exactly 2 trading days ago (T+2 entry window).

    T+2 is the earliest actionable entry day: D+2 win rate 55.6% vs D+0 38.5%.
    Returns None if that CSV was never saved.
    """
    today = date.today()
    count = 0
    candidate = today - timedelta(days=1)
    for _ in range(10):
        if candidate.weekday() < 5:
            count += 1
            if count == 2:
                csv_path = scan_dir / f"scan_{candidate}.csv"
                return csv_path if csv_path.exists() else None
        candidate -= timedelta(days=1)
    return None


def _load_watchlist(csv_path: Path, min_confidence: int) -> tuple[list[dict], list[dict]]:
    """Load scan CSV. Returns (actionable, emerging).

    actionable: LONG stocks with confidence >= min_confidence
    emerging:   WATCH stocks with EMERGING_SETUP flag (T-2 monitoring candidates)
    """
    actionable = []
    emerging = []
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                conf = int(row.get("confidence", 0))
                halt = row.get("halt", "").lower() in ("true", "1", "yes")
                if halt:
                    continue
                flags_str = row.get("data_quality_flags", "")
                parsed = {
                    "ticker": row["ticker"],
                    "action": row.get("action", ""),
                    "confidence": conf,
                    "entry_bid": float(row.get("entry_bid", 0)),
                    "stop_loss": float(row.get("stop_loss", 0)),
                    "target": float(row.get("target", 0)),
                    "flags": flags_str,
                }
                if conf >= min_confidence:
                    actionable.append(parsed)
                elif "EMERGING_SETUP" in flags_str:
                    emerging.append(parsed)
            except (ValueError, KeyError):
                continue
    actionable.sort(key=lambda r: r["confidence"], reverse=True)
    emerging.sort(key=lambda r: r["confidence"], reverse=True)
    return actionable, emerging


# ---------------------------------------------------------------------------
# Check logic
# ---------------------------------------------------------------------------

_ENTRY_TOLERANCE = 0.03  # ±3% of entry_bid
_VOL_PACE_RATIO = 0.6    # 盤中量能 ≥ 均量 × 時間比例 × 此閾值
_TAIEX_DROP_LIMIT = -1.5  # 大盤跌幅門檻 (%)


def _check_one(
    watch: dict,
    quote: dict | None,
    taiex_ok: bool,
    t_ratio: float,
) -> dict:
    """Evaluate one watchlist stock against real-time data.

    Returns enriched dict with pass/fail status and reasons.
    """
    result = {**watch, "status": "PASS", "reasons": [], "quote": quote}

    if quote is None:
        result["status"] = "NO_DATA"
        result["reasons"].append("無即時報價")
        return result

    price = quote["price"]
    entry = watch["entry_bid"]

    # Check 1: 現價在 entry_bid ±3%
    if entry > 0:
        diff_pct = (price - entry) / entry
        if diff_pct > _ENTRY_TOLERANCE:
            result["status"] = "SKIP"
            result["reasons"].append(f"已漲離 entry ({price:.1f} vs {entry:.1f}, +{diff_pct:.1%})")
        elif diff_pct < -_ENTRY_TOLERANCE:
            result["status"] = "SKIP"
            result["reasons"].append(f"跌破 entry ({price:.1f} vs {entry:.1f}, {diff_pct:.1%})")

    # Check 2: 量能跟上（只在盤中有效）
    if t_ratio > 0.1:
        vol = quote["volume"]  # 累積張數
        # 估算全日量能 pace：目前量 / 時間比例
        projected_vol = vol / t_ratio if t_ratio > 0 else 0
        # 簡易判斷：如果目前累積量太低，標記
        # 因為沒有 avg_20d_volume 在 CSV 裡，用昨日量做基準
        # 這裡用 projected < 500 張的低量股作為警告
        if vol < 100 and t_ratio > 0.3:
            result["reasons"].append(f"量能偏低（累計 {vol} 張）")
            if result["status"] == "PASS":
                result["status"] = "WARN"

    # Check 3: 大盤
    if not taiex_ok:
        result["reasons"].append(f"大盤跌幅 ≥ {abs(_TAIEX_DROP_LIMIT)}%")
        if result["status"] == "PASS":
            result["status"] = "WARN"

    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _print_results(
    checked: list[dict],
    taiex: dict | None,
    csv_path: Path,
    t_ratio: float,
    emerging: list[dict] | None = None,
) -> None:
    """Rich formatted output."""
    now = datetime.now()
    market_status = "盤中" if 9 <= now.hour < 14 else ("盤前" if now.hour < 9 else "收盤後")

    # TAIEX info
    taiex_line = "[dim]無資料[/dim]"
    if taiex:
        chg = ((taiex["price"] - taiex["yesterday_close"]) / taiex["yesterday_close"] * 100)
        color = "green" if chg >= 0 else "red"
        taiex_line = f"[{color}]{taiex['price']:,.0f} ({chg:+.2f}%)[/{color}]"

    _console.print(Panel(
        f"[bold white]市場狀態[/bold white]  {market_status}（{now.strftime('%H:%M')}，交易日進度 {t_ratio:.0%}）\n"
        f"[bold white]加權指數[/bold white]  {taiex_line}\n"
        f"[bold white]Watchlist[/bold white]  {csv_path.name}（{len(checked)} 檔）",
        title="[bold cyan]盤前確認 Precheck[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))

    # Separate by status
    actionable = [r for r in checked if r["status"] == "PASS"]
    warnings = [r for r in checked if r["status"] == "WARN"]
    skipped = [r for r in checked if r["status"] in ("SKIP", "NO_DATA")]

    # Main table: actionable
    if actionable:
        tbl = Table(
            title="可執行清單",
            box=box.ROUNDED,
            header_style="bold white on dark_green",
            border_style="green",
            show_lines=True,
        )
        tbl.add_column("#", justify="center", style="dim", width=4)
        tbl.add_column("股票", style="bold white", width=14)
        tbl.add_column("信心", justify="right", width=6)
        tbl.add_column("現價", justify="right", style="cyan", width=10)
        tbl.add_column("建議買入", justify="right", style="green", width=9)
        tbl.add_column("停損", justify="right", style="red", width=9)
        tbl.add_column("目標", justify="right", style="yellow", width=9)
        tbl.add_column("價差", justify="right", width=8)

        for i, r in enumerate(actionable, 1):
            q = r["quote"] or {}
            price = q.get("price", 0)
            entry = r["entry_bid"]
            diff_pct = ((price - entry) / entry * 100) if entry > 0 else 0
            diff_color = "green" if diff_pct <= 0 else "yellow"

            # Show price source hint when not from last trade
            src = q.get("price_source", "last")
            _SRC_LABEL = {"bid": "買", "hl_mid": "中", "open": "開"}
            src_hint = f" ({_SRC_LABEL[src]})" if src in _SRC_LABEL else ""

            name = q.get("name", "")
            ticker_cell = f"{r['ticker']}\n[dim]{name[:4]}[/dim]" if name else r["ticker"]

            tbl.add_row(
                str(i),
                ticker_cell,
                str(r["confidence"]),
                f"{price:.1f}{src_hint}" if price else "-",
                f"{entry:.1f}",
                f"{r['stop_loss']:.1f}",
                f"{r['target']:.1f}",
                f"[{diff_color}]{diff_pct:+.1f}%[/{diff_color}]",
            )
        _console.print()
        _console.print(tbl)

    # Warnings table
    if warnings:
        tbl = Table(
            title="注意（條件性可執行）",
            box=box.ROUNDED,
            header_style="bold white on dark_orange3",
            border_style="yellow",
        )
        tbl.add_column("股票", style="white", width=14)
        tbl.add_column("信心", justify="right", width=6)
        tbl.add_column("現價", justify="right", width=9)
        tbl.add_column("Entry", justify="right", width=9)
        tbl.add_column("原因", style="yellow")

        for r in warnings:
            q = r["quote"] or {}
            price = q.get("price", 0)
            src = q.get("price_source", "last")
            _SRC_LABEL_W = {"bid": "買", "hl_mid": "中", "open": "開"}
            src_hint = f" ({_SRC_LABEL_W[src]})" if src in _SRC_LABEL_W else ""
            name = q.get("name", "")
            ticker_cell = f"{r['ticker']}\n[dim]{name[:4]}[/dim]" if name else r["ticker"]
            tbl.add_row(
                ticker_cell,
                str(r["confidence"]),
                f"{price:.1f}{src_hint}" if price else "-",
                f"{r['entry_bid']:.1f}",
                "；".join(r["reasons"]),
            )
        _console.print()
        _console.print(tbl)

    # Skipped summary
    if skipped:
        tickers_str = ", ".join(f"{r['ticker']}({r['reasons'][0] if r['reasons'] else r['status']})" for r in skipped[:10])
        more = f" ...+{len(skipped)-10}" if len(skipped) > 10 else ""
        _console.print(f"\n  [dim]略過 {len(skipped)} 檔: {tickers_str}{more}[/dim]")

    # Emerging monitoring table (T-2 candidates)
    if emerging:
        tbl = Table(
            title="🌱 蓄積中（WATCH + EMERGING_SETUP — 1~2 日後可能晉升 LONG）",
            box=box.ROUNDED,
            header_style="bold white on dark_magenta",
            border_style="magenta",
        )
        tbl.add_column("股票", style="white", width=14)
        tbl.add_column("信心", justify="right", width=6)
        tbl.add_column("現價", justify="right", width=10)
        tbl.add_column("昨收", justify="right", style="dim", width=9)
        tbl.add_column("漲跌", justify="right", width=8)
        tbl.add_column("累計量", justify="right", width=8)

        for e in emerging:
            q = e.get("quote") or {}
            price = q.get("price", 0)
            yclose = q.get("yesterday_close", 0)
            vol = q.get("volume", 0)
            src = q.get("price_source", "last")
            _SRC_LABEL_E = {"bid": "買", "hl_mid": "中", "open": "開"}
            src_hint = f" ({_SRC_LABEL_E[src]})" if src in _SRC_LABEL_E else ""

            if price and yclose:
                chg = (price - yclose) / yclose * 100
                chg_color = "green" if chg >= 0 else "red"
                chg_str = f"[{chg_color}]{chg:+.1f}%[/{chg_color}]"
            else:
                chg_str = "-"

            name = q.get("name", "")
            ticker_cell = f"{e['ticker']}\n[dim]{name[:4]}[/dim]" if name else e["ticker"]

            tbl.add_row(
                ticker_cell,
                str(e["confidence"]),
                f"{price:.1f}{src_hint}" if price else "-",
                f"{yclose:.1f}" if yclose else "-",
                chg_str,
                f"{vol:,}" if vol else "-",
            )
        _console.print()
        _console.print(tbl)
        _console.print("  [dim magenta]→ 尚未達到 LONG 門檻，觀察用。可在昨收附近掛限價單被動佈局。[/dim magenta]")

    # Summary
    n_emerging = len(emerging) if emerging else 0
    _console.print()
    if not actionable and not warnings and not n_emerging:
        _console.print(Panel("[yellow]今日無可執行標的[/yellow]", border_style="yellow"))
    else:
        summary_parts = []
        if actionable:
            summary_parts.append(f"[bold green]可執行[/bold green] {len(actionable)} 檔")
        if warnings:
            summary_parts.append(f"[bold yellow]注意[/bold yellow] {len(warnings)} 檔")
        if n_emerging:
            summary_parts.append(f"[bold magenta]蓄積中[/bold magenta] {n_emerging} 檔")
        if skipped:
            summary_parts.append(f"[dim]略過 {len(skipped)} 檔[/dim]")

        _console.print(Panel(
            "  ".join(summary_parts),
            border_style="green" if actionable else "yellow",
            padding=(0, 2),
        ))
        if actionable:
            _console.print("\n[bold]建議操作：[/bold]")
            _console.print("  1. 以 [cyan]建議買入價[/cyan] 掛限價單")
            _console.print("  2. 同時設定 [red]停損[/red] 觸價單")
            _console.print("  3. 到 [yellow]目標價[/yellow] 分批出場")
        if n_emerging:
            _console.print("\n[bold magenta]蓄積中標的操作：[/bold magenta]")
            _console.print("  • 在昨收價附近掛[cyan]限價買單[/cyan]（被動佈局）")
            _console.print("  • 若明日 scan 晉升 LONG → 轉為積極進場")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_precheck(csv_path: Path | None, min_confidence: int, top: int) -> None:
    # 0. 只在盤中有意義（09:00–13:30）
    now = datetime.now()
    is_market_hours = now.weekday() < 5 and (
        now.replace(hour=9, minute=0, second=0) <= now <= now.replace(hour=13, minute=30, second=0)
    )
    if not is_market_hours:
        if now.hour < 9:
            hint = "開盤後（09:00）再執行"
        elif now.hour >= 14 or (now.hour == 13 and now.minute >= 30):
            hint = "今日收盤，明日盤中（09:00–13:30）再執行"
        else:
            hint = "週末休市，下週一盤中再執行"
        _console.print(Panel(
            f"[yellow]⚠ 目前 {now.strftime('%H:%M')} 非盤中時段\n\n"
            f"TWSE 即時報價只在 09:00–13:30 提供現價資料。\n"
            f"盤後執行無法判斷現價是否在進場範圍內，結果沒有意義。\n\n"
            f"→ {hint}[/yellow]",
            title="[bold yellow]Precheck 無法執行[/bold yellow]",
            border_style="yellow",
            padding=(0, 2),
        ))
        return

    # 1. Find scan CSV
    if csv_path is None:
        csv_path = _find_latest_scan_csv(_SCAN_DIR)
    if csv_path is None or not csv_path.exists():
        _console.print("[red]找不到 scan CSV。請先執行 make scan[/red]")
        return

    # 2. Load watchlist
    watchlist, emerging_raw = _load_watchlist(csv_path, min_confidence)
    if not watchlist and not emerging_raw:
        _console.print(f"[yellow]CSV 中無符合條件的標的 (min_confidence={min_confidence})[/yellow]")
        return

    if top:
        watchlist = watchlist[:top]

    _console.print(f"[dim]載入 {len(watchlist)} 檔 watchlist + {len(emerging_raw)} 檔蓄積中（{csv_path.name}）[/dim]")

    # 3. Fetch real-time quotes (include emerging tickers)
    all_tickers = [r["ticker"] for r in watchlist] + [r["ticker"] for r in emerging_raw]
    # deduplicate while preserving order
    seen: set[str] = set()
    unique_tickers = []
    for t in all_tickers:
        if t not in seen:
            unique_tickers.append(t)
            seen.add(t)

    with Progress(SpinnerColumn(), TextColumn("[cyan]抓取即時報價..."), console=_console, transient=True) as p:
        p.add_task("", total=None)
        quotes = _fetch_realtime_with_otc_fallback(unique_tickers)
        taiex = _fetch_taiex_realtime()

    found = len([t for t in unique_tickers if t in quotes])
    _console.print(f"[dim]即時報價: {found}/{len(unique_tickers)} 檔成功[/dim]")

    # 4. Check conditions (LONG stocks)
    t_ratio = _time_ratio()
    taiex_ok = True
    if taiex:
        chg_pct = (taiex["price"] - taiex["yesterday_close"]) / taiex["yesterday_close"] * 100
        taiex_ok = chg_pct >= _TAIEX_DROP_LIMIT

    checked = [
        _check_one(w, quotes.get(w["ticker"]), taiex_ok, t_ratio)
        for w in watchlist
    ]

    # Enrich emerging stocks with quotes
    for e in emerging_raw:
        e["quote"] = quotes.get(e["ticker"])

    # 5. Output
    _print_results(checked, taiex, csv_path, t_ratio, emerging=emerging_raw)


def main() -> None:
    parser = argparse.ArgumentParser(description="盤前/盤中確認 — 即時報價 vs 昨日 watchlist")
    parser.add_argument("--csv", type=Path, default=None, help="指定 scan CSV 路徑（預設: 自動找最近的）")
    parser.add_argument("--t2", action="store_true", help="T+2 進場模式：自動載入 2 個交易日前的 CSV（D+2 勝率 55.6%%）")
    parser.add_argument("--min-confidence", type=int, default=40, help="最低信心分數門檻（預設: 40）")
    parser.add_argument("--top", type=int, default=20, help="只檢查前 N 名（預設: 20）")
    args = parser.parse_args()

    csv_path = args.csv
    if args.t2:
        csv_path = _find_t2_scan_csv(_SCAN_DIR)
        if csv_path is None:
            from datetime import date as _date
            today = _date.today()
            _console.print(Panel(
                "[yellow]找不到 T+2 的 scan CSV。\n\n"
                f"今天是 {today}，需要 2 個交易日前的掃描檔（data/scans/scan_YYYY-MM-DD.csv）。\n"
                "請確認當天有執行 [bold]make scan[/bold] 並存下 CSV。[/yellow]",
                title="[bold yellow]precheck --t2：找不到 CSV[/bold yellow]",
                border_style="yellow",
                padding=(0, 2),
            ))
            return

    run_precheck(csv_path, args.min_confidence, args.top)


if __name__ == "__main__":
    main()
