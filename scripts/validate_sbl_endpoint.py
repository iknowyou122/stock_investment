#!/usr/bin/env python3
"""Validate TWSE TWT93U SBL endpoint availability.

Usage:
    python3 scripts/validate_sbl_endpoint.py 2330 2024-03-04

Prints: AVAILABLE / UNAVAILABLE / SCHEMA_CHANGED
Exits 0 on success (AVAILABLE), 1 on failure.
"""
from __future__ import annotations

import sys
from datetime import date

import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TWSE_SBL_URL = "https://www.twse.com.tw/rwd/zh/shortselling/TWT93U"
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
            TWSE_SBL_URL,
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

    # Required column
    if "證券代號" not in fields:
        print(f"SCHEMA_CHANGED: 證券代號 column missing. fields={fields}")
        return 1

    # Check for a 借券賣出 column (any of the known names)
    sbl_col = next(
        (c for c in ("借券賣出成交股數", "借券賣出張數") if c in fields), None
    )
    total_col = next(
        (c for c in ("當日成交股數", "成交股數", "當日成交量") if c in fields), None
    )

    if sbl_col is None or total_col is None:
        print(f"SCHEMA_CHANGED: expected SBL/volume columns not found. fields={fields}")
        return 1

    # Find ticker row
    code_idx = fields.index("證券代號")
    sbl_idx = fields.index(sbl_col)
    vol_idx = fields.index(total_col)

    for row in body["data"]:
        if row[code_idx].strip() == ticker:
            sbl_raw = row[sbl_idx].replace(",", "").strip()
            vol_raw = row[vol_idx].replace(",", "").strip()
            try:
                sbl_shares = int(sbl_raw)
                total_shares = int(vol_raw)
            except ValueError as e:
                print(f"SCHEMA_CHANGED: parse error — {e}. row={row}")
                return 1
            ratio = sbl_shares / total_shares if total_shares > 0 else 0.0
            print(
                f"AVAILABLE: {ticker} on {trade_date} — "
                f"SBL={sbl_shares:,} shares, total={total_shares:,}, "
                f"ratio={ratio:.2%}"
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
