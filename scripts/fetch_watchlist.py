"""從 TWSE / TPEX openapi 抓取所有上市上櫃普通股，並依量價條件預篩選。

輸出: 符合條件的股票代碼清單，直接餵給 batch_scan.py

Usage:
    # 只列出代碼（給 batch_scan 用）
    python scripts/fetch_watchlist.py

    # 前 50 大成交量
    python scripts/fetch_watchlist.py --top-volume 50

    # 漲跌幅 ≥ 2%
    python scripts/fetch_watchlist.py --min-change 2.0

    # 同時條件篩選 + 存 CSV
    python scripts/fetch_watchlist.py --top-volume 100 --min-change 1.0 --save

    # 和 batch_scan 串接
    python scripts/fetch_watchlist.py --top-volume 80 | xargs python scripts/batch_scan.py --tickers
"""
from __future__ import annotations

import argparse
import csv
import sys
import urllib3
from datetime import date
from pathlib import Path

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"

_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _is_common_stock(code: str) -> bool:
    """True 代表普通股：4 位純數字，不以 0 開頭。
    排除: ETF (0050/00xxx)、權證、特別股 (含字母) 等。
    """
    return len(code) == 4 and code.isdigit() and code[0] != "0"


def fetch_twse() -> list[dict]:
    """上市 (TWSE) 日報，回傳 list of dict."""
    try:
        resp = requests.get(TWSE_URL, headers=_HEADERS, timeout=15, verify=False)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        print(f"[WARN] TWSE fetch failed: {e}", file=sys.stderr)
        return []

    result = []
    for row in raw:
        code = row.get("Code", "").strip()
        if not _is_common_stock(code):
            continue
        try:
            result.append({
                "ticker": code,
                "name": row.get("Name", ""),
                "market": "TWSE",
                "close": float(row.get("ClosingPrice") or 0),
                "change": float(row.get("Change") or 0),
                "volume": int(str(row.get("TradeVolume") or "0").replace(",", "")),
                "transactions": int(str(row.get("Transaction") or "0").replace(",", "")),
            })
        except (ValueError, TypeError):
            continue
    return result


def fetch_tpex() -> list[dict]:
    """上櫃 (TPEX) 日報，回傳 list of dict."""
    try:
        resp = requests.get(TPEX_URL, headers=_HEADERS, timeout=15, verify=False)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        print(f"[WARN] TPEX fetch failed: {e}", file=sys.stderr)
        return []

    result = []
    for row in raw:
        code = row.get("SecuritiesCompanyCode", "").strip()
        if not _is_common_stock(code):
            continue
        try:
            close_str = str(row.get("Close") or "0").replace(",", "")
            change_str = str(row.get("Change") or "0").replace(",", "").replace("+", "")
            volume_str = str(row.get("TradingShares") or "0").replace(",", "")
            tx_str = str(row.get("TransactionNumber") or "0").replace(",", "")
            result.append({
                "ticker": code,
                "name": row.get("CompanyName", ""),
                "market": "TPEX",
                "close": float(close_str) if close_str and close_str != "--" else 0.0,
                "change": float(change_str) if change_str and change_str not in ("--", "X0.00") else 0.0,
                "volume": int(volume_str) if volume_str else 0,
                "transactions": int(tx_str) if tx_str else 0,
            })
        except (ValueError, TypeError):
            continue
    return result


def build_watchlist(
    top_volume: int | None,
    min_change_pct: float,
    min_price: float,
    max_price: float | None,
) -> list[dict]:
    stocks = fetch_twse() + fetch_tpex()
    if not stocks:
        return []

    # 計算漲跌幅 %
    for s in stocks:
        prev = s["close"] - s["change"]
        s["change_pct"] = (s["change"] / prev * 100) if prev > 0 else 0.0

    # 篩選
    filtered = [s for s in stocks if s["close"] >= min_price]
    if max_price:
        filtered = [s for s in filtered if s["close"] <= max_price]
    if min_change_pct > 0:
        filtered = [s for s in filtered if abs(s["change_pct"]) >= min_change_pct]

    # 去除停牌 (volume = 0)
    filtered = [s for s in filtered if s["volume"] > 0]

    # 排序: 成交量降冪
    filtered.sort(key=lambda s: s["volume"], reverse=True)

    if top_volume:
        filtered = filtered[:top_volume]

    return filtered


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取上市上櫃清單並預篩選")
    parser.add_argument("--top-volume", type=int, default=None,
                        help="取成交量前 N 名（未設則全部）")
    parser.add_argument("--min-change", type=float, default=0.0,
                        help="最小絕對漲跌幅 %（例: 2.0 表示 ≥ 2%%）")
    parser.add_argument("--min-price", type=float, default=10.0,
                        help="最低股價（過濾低價股，預設 10）")
    parser.add_argument("--max-price", type=float, default=None,
                        help="最高股價（可選）")
    parser.add_argument("--save", action="store_true",
                        help="同時存到 data/scans/watchlist_YYYY-MM-DD.csv")
    parser.add_argument("--show-table", action="store_true",
                        help="印出詳細表格（預設只印代碼，方便 xargs 使用）")
    args = parser.parse_args()

    stocks = build_watchlist(
        top_volume=args.top_volume,
        min_change_pct=args.min_change,
        min_price=args.min_price,
        max_price=args.max_price,
    )

    if not stocks:
        print("(無符合條件的標的)", file=sys.stderr)
        sys.exit(1)

    if args.show_table:
        print(f"\n{'代碼':<8} {'名稱':<12} {'市場':<6} {'收盤':>8} {'漲跌%':>7} {'成交量':>14}")
        print("-" * 60)
        for s in stocks:
            print(f"{s['ticker']:<8} {s['name']:<12} {s['market']:<6} "
                  f"{s['close']:>8.2f} {s['change_pct']:>+7.2f}% {s['volume']:>14,}")
        print(f"\n共 {len(stocks)} 檔\n", file=sys.stderr)
    else:
        # 僅印代碼（方便 xargs）
        print(" ".join(s["ticker"] for s in stocks))

    if args.save:
        scan_dir = Path(__file__).resolve().parents[1] / "data" / "scans"
        scan_dir.mkdir(parents=True, exist_ok=True)
        path = scan_dir / f"watchlist_{date.today()}.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["ticker", "name", "market", "close", "change_pct", "volume", "transactions"])
            writer.writeheader()
            writer.writerows({k: s[k] for k in ["ticker", "name", "market", "close", "change_pct", "volume", "transactions"]} for s in stocks)
        print(f"[儲存] {path}  ({len(stocks)} 筆)", file=sys.stderr)


if __name__ == "__main__":
    main()
