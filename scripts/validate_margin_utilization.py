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

# TWSE openapi endpoint — returns per-stock list including 融資限額
TWSE_MARGIN_OPENAPI_URL = "https://openapi.twse.com.tw/v1/marginTrading/MI_MARGN"


def validate(ticker: str, trade_date: date) -> int:
    try:
        resp = requests.get(
            TWSE_MARGIN_OPENAPI_URL,
            params={"date": trade_date.strftime("%Y%m%d")},
            timeout=15,
            verify=False,  # openapi.twse.com.tw: Missing Subject Key Identifier (OpenSSL 3.x)
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:
        print(f"UNAVAILABLE: network/HTTP error — {e}")
        return 1

    if not isinstance(rows, list):
        print(f"SCHEMA_CHANGED: expected list, got {type(rows).__name__}")
        return 1

    if not rows:
        print("UNAVAILABLE: empty response list")
        return 1

    # Check schema on first row
    sample = rows[0]
    required = {"股票代號", "融資今日餘額", "融資限額"}
    missing = required - set(sample.keys())
    if missing:
        print(f"SCHEMA_CHANGED: missing keys {missing}. keys={list(sample.keys())}")
        return 1

    for row in rows:
        if str(row.get("股票代號", "")).strip() == ticker:
            balance_raw = row.get("融資今日餘額", "")
            limit_raw = row.get("融資限額", "")
            try:
                balance = int(str(balance_raw).replace(",", "").strip())
                limit = int(str(limit_raw).replace(",", "").strip()) if limit_raw else 0
            except ValueError as e:
                print(f"SCHEMA_CHANGED: parse error — {e}. row={row}")
                return 1
            if limit <= 0:
                print(f"UNAVAILABLE: 融資限額={limit!r} for {ticker} (zero/empty)")
                return 1
            util = balance / limit
            print(
                f"AVAILABLE: {ticker} — "
                f"融資今日餘額={balance:,}, 融資限額={limit:,}, "
                f"utilization={util:.1%}"
            )
            return 0

    print(f"UNAVAILABLE: ticker {ticker!r} not found in response ({len(rows)} rows)")
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
