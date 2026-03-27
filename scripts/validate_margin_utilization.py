#!/usr/bin/env python3
"""Validate TWSE MI_MARGN 融資限額 column availability.

Usage:
    python3 scripts/validate_margin_utilization.py 2330 2024-03-04

Prints: AVAILABLE (with sample value) / UNAVAILABLE / SCHEMA_CHANGED
Exits 0 on success (AVAILABLE), 1 on failure.
"""
from __future__ import annotations

import sys
from datetime import date

import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TWSE_MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
_TWSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.twse.com.tw/",
    "Accept": "application/json, text/plain, */*",
}


def validate(ticker: str, trade_date: date) -> int:
    try:
        resp = requests.get(
            TWSE_MARGIN_URL,
            params={
                "date": trade_date.strftime("%Y%m%d"),
                "selectType": "ALL",
                "response": "json",
            },
            headers=_TWSE_HEADERS,
            timeout=15,
            verify=False,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        print(f"UNAVAILABLE: network/HTTP error — {e}")
        return 1

    if body.get("stat") != "OK" or not body.get("data"):
        print(f"UNAVAILABLE: stat={body.get('stat')!r}, data rows={len(body.get('data', []))}")
        return 1

    fields = body.get("fields", [])

    # Required columns
    if "股票代號" not in fields:
        print(f"SCHEMA_CHANGED: 股票代號 column missing. fields={fields}")
        return 1
    if "融資餘額" not in fields:
        print(f"SCHEMA_CHANGED: 融資餘額 column missing. fields={fields}")
        return 1

    # Check for optional 融資限額 column
    if "融資限額" not in fields:
        print(
            f"UNAVAILABLE: 融資限額 column not present in MI_MARGN response.\n"
            f"Available fields: {fields}"
        )
        return 1

    code_idx = fields.index("股票代號")
    balance_idx = fields.index("融資餘額")
    limit_idx = fields.index("融資限額")

    for row in body["data"]:
        if row[code_idx].strip() == ticker:
            try:
                balance = int(row[balance_idx].replace(",", "").strip())
                limit = int(row[limit_idx].replace(",", "").strip())
            except ValueError as e:
                print(f"SCHEMA_CHANGED: parse error — {e}. row={row}")
                return 1
            if limit <= 0:
                print(f"UNAVAILABLE: 融資限額={limit} for {ticker} on {trade_date} (zero/negative)")
                return 1
            util = balance / limit
            print(
                f"AVAILABLE: {ticker} on {trade_date} — "
                f"融資餘額={balance:,}, 融資限額={limit:,}, "
                f"utilization={util:.1%}"
            )
            return 0

    print(f"UNAVAILABLE: ticker {ticker!r} not found in {trade_date} response ({len(body['data'])} rows)")
    return 1


def main() -> int:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <ticker> <YYYY-MM-DD>", file=sys.stderr)
        return 1
    ticker = sys.argv[1]
    try:
        trade_date = date.fromisoformat(sys.argv[2])
    except ValueError as e:
        print(f"Invalid date format: {e}", file=sys.stderr)
        return 1
    return validate(ticker, trade_date)


if __name__ == "__main__":
    sys.exit(main())
