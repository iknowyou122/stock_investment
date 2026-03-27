"""TWSE opendata client — free-tier chip proxy fetcher.

Fetches 外資買賣超 (foreign net buy), 融資餘額 (margin balance),
融券餘額 (short balance), and consecutive foreign buy count
from TWSE public REST API. No authentication required.

Cache: 24h TTL Parquet file, same pattern as FinMindClient.
Failure policy: any network or parse error returns a zero-value TWSEChipProxy
with is_available=False. Never raises to callers.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import urllib3
import pandas as pd
import requests

# TWSE's CA certificate chain is missing the Subject Key Identifier extension,
# which OpenSSL 3.x rejects. Suppress the InsecureRequestWarning since verify=False
# is intentional for this government endpoint.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from taiwan_stock_agent.domain.models import TWSEChipProxy

logger = logging.getLogger(__name__)

TWSE_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TWSE_MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"

_TWSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.twse.com.tw/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

CACHE_DIR = Path(__file__).resolve().parents[4] / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)



class ChipProxyFetcher:
    """Fetch free-tier chip proxy data from TWSE opendata.

    Extensibility note: to add a new TWSE data source (e.g. 投信買賣超),
    add a new _fetch_*() method and update fetch() to call it and populate
    TWSEChipProxy.

    Usage::
        fetcher = ChipProxyFetcher()
        proxy = fetcher.fetch("2330", date(2026, 3, 24))
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir or CACHE_DIR

    def fetch(self, ticker: str, trade_date: date) -> TWSEChipProxy:
        """Fetch chip proxy data for ticker on trade_date.

        Populates:
          - foreign_net_buy, trust_net_buy, dealer_net_buy (T86)
          - margin_balance_change (MI_MARGN 融資)
          - foreign_consecutive_buy_days (multi-day T86 lookback)
          - short_balance_increased, short_margin_ratio (MI_MARGN 融券)

        Returns TWSEChipProxy(is_available=False) on any failure — never raises.
        """
        flags: list[str] = []

        foreign_net, trust_net, dealer_net = self._fetch_t86_data(ticker, trade_date, flags)
        margin_change = self._fetch_margin_balance_change(ticker, trade_date, flags)
        foreign_consec = self._fetch_foreign_consecutive_days(ticker, trade_date, flags)
        short_increased, short_margin_ratio = self._fetch_short_data(ticker, trade_date, flags)

        # Only mark available if at least one data source succeeded
        is_available = (
            foreign_net is not None
            or trust_net is not None
            or dealer_net is not None
            or margin_change is not None
        )

        return TWSEChipProxy(
            ticker=ticker,
            trade_date=trade_date,
            foreign_net_buy=foreign_net or 0,
            trust_net_buy=trust_net or 0,
            dealer_net_buy=dealer_net or 0,
            margin_balance_change=margin_change or 0,
            foreign_consecutive_buy_days=foreign_consec,
            short_balance_increased=short_increased,
            short_margin_ratio=short_margin_ratio,
            is_available=is_available,
            data_quality_flags=flags,
        )

    # ------------------------------------------------------------------
    # Private fetch methods — each returns None on failure
    # ------------------------------------------------------------------

    def _fetch_t86_data(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> tuple[int | None, int | None, int | None]:
        """Fetch 外資買賣超, 投信買賣超 and 自營商買賣超 from TWSE T86 (single request).

        Returns (foreign_net_buy, trust_net_buy, dealer_net_buy) in shares;
        any value may be None if unavailable.
        """
        cache = self._cache_dir / f"twse_t86_{ticker}_{trade_date}.parquet"
        if cache.exists():
            try:
                df = pd.read_parquet(cache)
                if not df.empty:
                    foreign = int(df["foreign_net_buy"].iloc[0]) if "foreign_net_buy" in df.columns else None
                    trust = int(df["trust_net_buy"].iloc[0]) if "trust_net_buy" in df.columns else None
                    dealer = int(df["dealer_net_buy"].iloc[0]) if "dealer_net_buy" in df.columns else None
                    return foreign, trust, dealer
            except Exception:
                pass

        # Also check legacy cache key (foreign-only, from prior schema)
        legacy_cache = self._cache_dir / f"twse_foreign_{ticker}_{trade_date}.parquet"
        if legacy_cache.exists():
            try:
                df = pd.read_parquet(legacy_cache)
                if not df.empty:
                    return int(df["foreign_net_buy"].iloc[0]), None, None
            except Exception:
                pass

        try:
            resp = requests.get(
                TWSE_T86_URL,
                params={
                    "date": trade_date.strftime("%Y%m%d"),
                    "selectType": "ALL",
                    "response": "json",
                },
                headers=_TWSE_HEADERS,
                timeout=10,
                verify=False,  # TWSE CA cert missing Subject Key Identifier (OpenSSL 3.x strict)
            )
            resp.raise_for_status()
            body = resp.json()

            if body.get("stat") != "OK" or not body.get("data"):
                flags.append(f"TWSE_T86_NO_DATA:{trade_date}")
                return None, None, None

            fields = body.get("fields", [])
            try:
                code_idx = fields.index("證券代號")
                foreign_idx = fields.index("外陸資買賣超股數")
            except ValueError:
                flags.append("TWSE_T86_SCHEMA_CHANGED")
                return None, None, None

            # 投信買賣超股數 and 自營商買賣超股數 are optional columns
            trust_idx: int | None = fields.index("投信買賣超股數") if "投信買賣超股數" in fields else None
            dealer_idx: int | None = fields.index("自營商買賣超股數") if "自營商買賣超股數" in fields else None

            for row in body["data"]:
                if row[code_idx].strip() == ticker:
                    foreign_raw = row[foreign_idx].replace(",", "").replace("+", "").strip()
                    foreign_val = int(foreign_raw)

                    trust_val: int | None = None
                    if trust_idx is not None:
                        trust_raw = row[trust_idx].replace(",", "").replace("+", "").strip()
                        try:
                            trust_val = int(trust_raw)
                        except ValueError:
                            flags.append("TWSE_T86_TRUST_PARSE_ERROR")

                    dealer_val: int | None = None
                    if dealer_idx is not None:
                        dealer_raw = row[dealer_idx].replace(",", "").replace("+", "").strip()
                        try:
                            dealer_val = int(dealer_raw)
                        except ValueError:
                            flags.append("TWSE_T86_DEALER_PARSE_ERROR")

                    # Cache all three values
                    cache_row: dict = {"foreign_net_buy": foreign_val}
                    if trust_val is not None:
                        cache_row["trust_net_buy"] = trust_val
                    if dealer_val is not None:
                        cache_row["dealer_net_buy"] = dealer_val
                    pd.DataFrame([cache_row]).to_parquet(cache, index=False)
                    return foreign_val, trust_val, dealer_val

            # Ticker not found in today's data (may not have traded)
            flags.append(f"TWSE_T86_TICKER_NOT_FOUND:{ticker}")
            return None, None, None

        except Exception as e:
            logger.warning("ChipProxyFetcher: T86 fetch failed for %s %s: %s", ticker, trade_date, e)
            flags.append(f"TWSE_T86_ERROR:{type(e).__name__}")
            return None, None, None

    def _fetch_margin_balance_change(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> int | None:
        """Fetch 融資餘額 change (today - yesterday) from TWSE MI_MARGN.

        Returns change in shares (negative = decreasing), or None if unavailable.
        Fetches today and previous trading day, computes diff.
        """
        today_balance = self._fetch_margin_balance_one_day(ticker, trade_date, flags)
        if today_balance is None:
            return None

        prev_date = trade_date - timedelta(days=1)
        prev_balance = self._fetch_margin_balance_one_day(ticker, prev_date, flags, silent=True)
        if prev_balance is None:
            # Can't compute change without yesterday; partial data
            flags.append(f"TWSE_MARGIN_NO_PREV:{prev_date}")
            return None

        return today_balance - prev_balance

    def _fetch_margin_balance_one_day(
        self, ticker: str, trade_date: date, flags: list[str], *, silent: bool = False
    ) -> int | None:
        """Fetch 融資餘額 (shares) for one day. Returns None on failure."""
        cache = self._cache_dir / f"twse_margin_{ticker}_{trade_date}.parquet"
        if cache.exists():
            try:
                df = pd.read_parquet(cache)
                if not df.empty:
                    return int(df["margin_balance"].iloc[0])
            except Exception:
                pass

        try:
            resp = requests.get(
                TWSE_MARGIN_URL,
                params={
                    "date": trade_date.strftime("%Y%m%d"),
                    "selectType": "ALL",
                    "response": "json",
                },
                headers=_TWSE_HEADERS,
                timeout=10,
                verify=False,  # TWSE CA cert missing Subject Key Identifier (OpenSSL 3.x strict)
            )
            resp.raise_for_status()
            body = resp.json()

            if body.get("stat") != "OK" or not body.get("data"):
                return None

            fields = body.get("fields", [])
            try:
                code_idx = fields.index("股票代號")
                balance_idx = fields.index("融資餘額")
            except ValueError:
                if not silent:
                    flags.append("TWSE_MARGN_SCHEMA_CHANGED")
                return None

            for row in body["data"]:
                if row[code_idx].strip() == ticker:
                    raw = row[balance_idx].replace(",", "").strip()
                    value = int(raw)
                    pd.DataFrame([{"margin_balance": value}]).to_parquet(cache, index=False)
                    return value

            return None

        except Exception as e:
            if not silent:
                logger.warning(
                    "ChipProxyFetcher: MI_MARGN fetch failed for %s %s: %s",
                    ticker, trade_date, e,
                )
                flags.append(f"TWSE_MARGN_ERROR:{type(e).__name__}")
            return None

    def _fetch_short_balance_one_day(
        self, ticker: str, trade_date: date, flags: list[str], *, silent: bool = False
    ) -> int | None:
        """Fetch 融券餘額 (shares) for one day from TWSE MI_MARGN. Returns None on failure."""
        cache = self._cache_dir / f"twse_short_{ticker}_{trade_date}.parquet"
        if cache.exists():
            try:
                df = pd.read_parquet(cache)
                if not df.empty:
                    return int(df["short_balance"].iloc[0])
            except Exception:
                pass

        try:
            resp = requests.get(
                TWSE_MARGIN_URL,
                params={
                    "date": trade_date.strftime("%Y%m%d"),
                    "selectType": "ALL",
                    "response": "json",
                },
                headers=_TWSE_HEADERS,
                timeout=10,
                verify=False,  # TWSE CA cert missing Subject Key Identifier (OpenSSL 3.x strict)
            )
            resp.raise_for_status()
            body = resp.json()

            if body.get("stat") != "OK" or not body.get("data"):
                return None

            fields = body.get("fields", [])
            try:
                code_idx = fields.index("股票代號")
                short_idx = fields.index("融券餘額")
            except ValueError:
                if not silent:
                    flags.append("TWSE_SHORT_SCHEMA_MISSING")
                return None

            for row in body["data"]:
                if row[code_idx].strip() == ticker:
                    raw = row[short_idx].replace(",", "").strip()
                    value = int(raw)
                    pd.DataFrame([{"short_balance": value}]).to_parquet(cache, index=False)
                    return value

            return None

        except Exception as e:
            if not silent:
                logger.warning(
                    "ChipProxyFetcher: short balance fetch failed for %s %s: %s",
                    ticker, trade_date, e,
                )
                flags.append(f"TWSE_SHORT_ERROR:{type(e).__name__}")
            return None

    def _fetch_short_data(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> tuple[bool, float]:
        """Compute 融券餘額 spike flag and 券資比 for trade_date.

        Returns:
            (short_balance_increased, short_margin_ratio)
            short_balance_increased: True if today's 融券餘額 > yesterday's by > 20%.
            short_margin_ratio: 融券餘額 / 融資餘額 (0.0 if unavailable).
        """
        today_short = self._fetch_short_balance_one_day(ticker, trade_date, flags)
        if today_short is None:
            return False, 0.0

        prev_date = trade_date - timedelta(days=1)
        prev_short = self._fetch_short_balance_one_day(ticker, prev_date, flags, silent=True)

        short_increased = False
        if prev_short is not None and prev_short > 0:
            short_increased = today_short > prev_short * 1.20

        # 券資比 = 融券餘額 / 融資餘額
        today_margin = self._fetch_margin_balance_one_day(ticker, trade_date, flags, silent=True)
        short_margin_ratio = 0.0
        if today_margin is not None and today_margin > 0:
            short_margin_ratio = today_short / today_margin

        return short_increased, short_margin_ratio

    def _fetch_foreign_consecutive_days(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> int:
        """Count consecutive calendar-adjusted days of foreign net buy ending on trade_date.

        Looks back up to 14 calendar days (~7 trading days). Non-trading days
        (weekends/holidays) that return no data are skipped transparently.
        Returns 0 if trade_date itself has no foreign net buy data or if foreign <= 0.
        """
        trading_day_values: list[int] = []

        for offset in range(15):  # 0 = trade_date, 1 = yesterday, ...
            check_date = trade_date - timedelta(days=offset)
            # Use throwaway flags so lookback days don't pollute the main flags
            _silent: list[str] = []
            foreign_val, _, _ = self._fetch_t86_data(ticker, check_date, _silent)
            if foreign_val is not None:
                trading_day_values.append(foreign_val)
                if len(trading_day_values) >= 7:  # 7 trading days is enough
                    break

        # Count consecutive days (newest first) where foreign net buy > 0
        count = 0
        for val in trading_day_values:
            if val > 0:
                count += 1
            else:
                break
        return count
