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
import time
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
TWSE_MARGIN_OPENAPI_URL = "https://openapi.twse.com.tw/v1/marginTrading/MI_MARGN"
# TWT93U SBL endpoint — returns 404 as of 2026-03-27; _fetch_sbl_data degrades to 0 gracefully.
TWSE_SBL_URL = "https://www.twse.com.tw/rwd/zh/shortselling/TWT93U"
TWSE_DAYTRADE_URL = "https://www.twse.com.tw/rwd/zh/block/TWTB4U"

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
          - margin_balance_change (openapi MI_MARGN 融資今日/前日餘額)
          - foreign_consecutive_buy_days, trust_consecutive_buy_days,
            dealer_consecutive_buy_days (multi-day T86 lookback)
          - short_balance_increased, short_margin_ratio (openapi MI_MARGN 融券今日/前日餘額)
          - sbl_ratio, sbl_available (TWT93U SBL endpoint)
          - margin_utilization_rate (openapi MI_MARGN 融資限額 column)
          - daytrade_ratio (TWTB4U 當沖 endpoint, hint only)

        Returns TWSEChipProxy(is_available=False) on any failure — never raises.
        """
        flags: list[str] = []

        foreign_net, trust_net, dealer_net = self._fetch_t86_data(ticker, trade_date, flags)
        margin_change = self._fetch_margin_balance_change(ticker, trade_date, flags)
        foreign_consec, trust_consec, dealer_consec = self._fetch_institution_consecutive_days(
            ticker, trade_date, flags
        )
        short_increased, short_margin_ratio = self._fetch_short_data(ticker, trade_date, flags)
        sbl_ratio = self._fetch_sbl_data(ticker, trade_date, flags)
        margin_util = self._fetch_margin_utilization(ticker, trade_date, flags)
        daytrade_ratio = self._fetch_daytrade_data(ticker, trade_date, flags)

        # Only mark available if at least one data source succeeded
        is_available = (
            foreign_net is not None
            or trust_net is not None
            or dealer_net is not None
            or margin_change is not None
        )

        if not is_available:
            rate_limited = any(f.startswith("TWSE_T86_RATE_LIMITED") for f in flags)
            reason = "TWSE 限流（非 JSON 回應）" if rate_limited else "無資料（假日或尚未更新）"
            logger.info("ChipProxy unavailable for %s %s: %s", ticker, trade_date, reason)

        return TWSEChipProxy(
            ticker=ticker,
            trade_date=trade_date,
            foreign_net_buy=foreign_net or 0,
            trust_net_buy=trust_net or 0,
            dealer_net_buy=dealer_net or 0,
            margin_balance_change=margin_change or 0,
            foreign_consecutive_buy_days=foreign_consec,
            trust_consecutive_buy_days=trust_consec,
            dealer_consecutive_buy_days=dealer_consec,
            short_balance_increased=short_increased,
            short_margin_ratio=short_margin_ratio,
            sbl_ratio=sbl_ratio if sbl_ratio is not None else 0.0,
            sbl_available=sbl_ratio is not None,
            margin_utilization_rate=margin_util,
            daytrade_ratio=daytrade_ratio,
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
            body = None
            for _attempt in range(3):
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
                try:
                    body = resp.json()
                    break
                except ValueError:
                    # Empty body or HTML error page — TWSE rate-limiting
                    logger.debug("T86 rate-limited for %s %s (attempt %d)", ticker, trade_date, _attempt + 1)
                    body = None
                if _attempt < 2:
                    time.sleep(1.0 + _attempt * 1.5)
            if body is None:
                logger.debug("T86 unavailable for %s %s after retries — 籌碼資料缺失", ticker, trade_date)
                flags.append(f"TWSE_T86_RATE_LIMITED:{trade_date}")
                return None, None, None

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

    def _fetch_margin_row_openapi(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> tuple[int | None, int | None, int | None, int | None, int | None]:
        """Fetch per-stock margin row from TWSE openapi MI_MARGN endpoint.

        Cache key: twse_margin_row_{ticker}_{trade_date}.parquet
        Columns: today_margin, prev_margin, today_short, prev_short, margin_limit

        The openapi endpoint always returns today's data (date param is ignored).
        Both today and previous day values are embedded in a single response row.

        Returns:
            (today_margin, prev_margin, today_short, prev_short, margin_limit)
            Any value may be None if the field is absent or an empty string.
        """
        cache = self._cache_dir / f"twse_margin_row_{ticker}_{trade_date}.parquet"
        if cache.exists():
            try:
                df = pd.read_parquet(cache)
                if not df.empty:
                    def _col(col: str) -> int | None:
                        if col not in df.columns:
                            return None
                        val = df[col].iloc[0]
                        return None if pd.isna(val) else int(val)
                    return (
                        _col("today_margin"),
                        _col("prev_margin"),
                        _col("today_short"),
                        _col("prev_short"),
                        _col("margin_limit"),
                    )
            except Exception:
                pass

        try:
            resp = requests.get(
                TWSE_MARGIN_OPENAPI_URL,
                timeout=10,
                verify=False,  # openapi.twse.com.tw shares same CA cert issue (Missing Subject Key Identifier)
            )
            resp.raise_for_status()
            rows = resp.json()

            if not isinstance(rows, list):
                flags.append("TWSE_MARGN_ERROR:UnexpectedFormat")
                return (None, None, None, None, None)

            def _parse_int(val: str) -> int | None:
                if val is None or val.strip() == "":
                    return None
                try:
                    return int(val.replace(",", "").strip())
                except ValueError:
                    return None

            for row in rows:
                if row.get("股票代號", "").strip() == ticker:
                    today_margin = _parse_int(row.get("融資今日餘額", ""))
                    prev_margin = _parse_int(row.get("融資前日餘額", ""))
                    today_short = _parse_int(row.get("融券今日餘額", ""))
                    prev_short = _parse_int(row.get("融券前日餘額", ""))
                    margin_limit = _parse_int(row.get("融資限額", ""))

                    pd.DataFrame([{
                        "today_margin": today_margin,
                        "prev_margin": prev_margin,
                        "today_short": today_short,
                        "prev_short": prev_short,
                        "margin_limit": margin_limit,
                    }]).to_parquet(cache, index=False)
                    return today_margin, prev_margin, today_short, prev_short, margin_limit

            # Ticker not in response — stock may not be margin-eligible
            return (None, None, None, None, None)

        except Exception as e:
            logger.warning(
                "ChipProxyFetcher: openapi MI_MARGN fetch failed for %s %s: %s",
                ticker, trade_date, e,
            )
            flags.append(f"TWSE_MARGN_ERROR:{type(e).__name__}")
            return (None, None, None, None, None)

    def _fetch_margin_balance_change(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> int | None:
        """Fetch 融資餘額 change (today - prev) from openapi MI_MARGN.

        Returns change in shares (negative = decreasing), or None if unavailable.
        Both today and previous values come from a single openapi response row.
        """
        today, prev, _, _, _ = self._fetch_margin_row_openapi(ticker, trade_date, flags)
        if today is None or prev is None:
            flags.append(f"TWSE_MARGIN_NO_PREV:{trade_date - timedelta(days=1)}")
            return None
        return today - prev

    def _fetch_short_data(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> tuple[bool, float]:
        """Compute 融券餘額 spike flag and 券資比 for trade_date.

        Returns:
            (short_balance_increased, short_margin_ratio)
            short_balance_increased: True if today's 融券餘額 > yesterday's by > 20%.
            short_margin_ratio: 融券餘額 / 融資餘額 (0.0 if unavailable).
        """
        today_margin, _, today_short, prev_short, _ = self._fetch_margin_row_openapi(
            ticker, trade_date, flags
        )
        if today_short is None:
            return False, 0.0

        short_increased = False
        if prev_short is not None and prev_short > 0:
            short_increased = today_short > prev_short * 1.20

        short_margin_ratio = 0.0
        if today_margin is not None and today_margin > 0:
            short_margin_ratio = today_short / today_margin

        return short_increased, short_margin_ratio

    def _fetch_institution_consecutive_days(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> tuple[int, int, int]:
        """Count consecutive calendar-adjusted buy days for all three institutions.

        Returns (foreign_count, trust_count, dealer_count).

        Looks back up to 14 calendar days (~7 trading days) using the T86 data
        already fetched/cached — zero additional network calls.
        Non-trading days (weekends/holidays) are skipped transparently.
        Returns 0 for an institution if it has no positive net buy on trade_date.
        """
        foreign_vals: list[int] = []
        trust_vals: list[int] = []
        dealer_vals: list[int] = []

        for offset in range(21):  # scan up to 21 calendar days to collect 7 trading days
            check_date = trade_date - timedelta(days=offset)
            # Skip weekends — TWSE returns empty body for non-trading days
            if check_date.weekday() >= 5:
                continue
            # Use throwaway flags so lookback days don't pollute the main flags
            _silent: list[str] = []
            foreign_val, trust_val, dealer_val = self._fetch_t86_data(ticker, check_date, _silent)
            if foreign_val is not None:
                foreign_vals.append(foreign_val)
            if trust_val is not None:
                trust_vals.append(trust_val)
            if dealer_val is not None:
                dealer_vals.append(dealer_val)
            # Stop once we have 7 trading days of foreign data (same budget as original).
            # Trust/dealer may have fewer entries if their columns are absent on some dates.
            if len(foreign_vals) >= 7:
                break

        def _count_consec(vals: list[int]) -> int:
            count = 0
            for val in vals:
                if val > 0:
                    count += 1
                else:
                    break
            return count

        return (
            _count_consec(foreign_vals),
            _count_consec(trust_vals),
            _count_consec(dealer_vals),
        )

    def _fetch_foreign_consecutive_days(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> int:
        """Backward-compat wrapper. Returns foreign consecutive buy days only.

        Prefer _fetch_institution_consecutive_days() for new call sites — it
        returns all three institutions in one pass.
        """
        foreign_count, _, _ = self._fetch_institution_consecutive_days(ticker, trade_date, flags)
        return foreign_count

    def _fetch_sbl_data(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> float | None:
        """Fetch 借券賣出占成交量比重 from TWSE TWT93U SBL endpoint.

        Returns sbl_ratio (0.0–1.0) or None if unavailable/error.
        Cache key: twse_sbl_{ticker}_{date}.parquet
        """
        cache = self._cache_dir / f"twse_sbl_{ticker}_{trade_date}.parquet"
        if cache.exists():
            try:
                df = pd.read_parquet(cache)
                if not df.empty and "sbl_ratio" in df.columns:
                    return float(df["sbl_ratio"].iloc[0])
            except Exception:
                pass

        try:
            resp = requests.get(
                TWSE_SBL_URL,
                params={
                    "date": trade_date.strftime("%Y%m%d"),
                    "selectType": "ALL",
                    "response": "json",
                },
                headers=_TWSE_HEADERS,
                timeout=10,
                verify=False,
            )
            resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError:
                flags.append(f"TWSE_SBL_RATE_LIMITED:{trade_date}")
                return None

            if body.get("stat") != "OK" or not body.get("data"):
                return None

            fields = body.get("fields", [])
            try:
                code_idx = fields.index("證券代號")
            except ValueError:
                flags.append("TWSE_SBL_SCHEMA_CHANGED")
                return None

            # Find 借券賣出成交股數 and 當日成交股數 columns (dynamic lookup)
            sbl_sell_idx: int | None = None
            total_vol_idx: int | None = None
            for candidate in ("借券賣出成交股數", "借券賣出張數"):
                if candidate in fields:
                    sbl_sell_idx = fields.index(candidate)
                    break
            for candidate in ("當日成交股數", "成交股數", "當日成交量"):
                if candidate in fields:
                    total_vol_idx = fields.index(candidate)
                    break

            if sbl_sell_idx is None or total_vol_idx is None:
                flags.append("TWSE_SBL_SCHEMA_CHANGED")
                return None

            for row in body["data"]:
                if row[code_idx].strip() == ticker:
                    sbl_raw = row[sbl_sell_idx].replace(",", "").strip()
                    vol_raw = row[total_vol_idx].replace(",", "").strip()
                    try:
                        sbl_shares = int(sbl_raw)
                        total_shares = int(vol_raw)
                    except ValueError:
                        flags.append("TWSE_SBL_PARSE_ERROR")
                        return None
                    if total_shares <= 0:
                        return None
                    ratio = sbl_shares / total_shares
                    pd.DataFrame([{"sbl_ratio": ratio}]).to_parquet(cache, index=False)
                    return ratio

            return None

        except Exception as e:
            logger.warning(
                "ChipProxyFetcher: SBL fetch failed for %s %s: %s", ticker, trade_date, e
            )
            flags.append(f"TWSE_SBL_ERROR:{type(e).__name__}")
            return None

    def _fetch_margin_utilization(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> float | None:
        """Fetch 融資使用率 = 融資餘額 / 融資限額 from openapi MI_MARGN.

        Returns utilization ratio (0.0–1.0+) or None if 融資限額 is missing or zero.
        No error flag is appended when limit is absent — it's an optional enhancement.
        Cache key: twse_margin_util_{ticker}_{date}.parquet (backward-compat cache)
        """
        util_cache = self._cache_dir / f"twse_margin_util_{ticker}_{trade_date}.parquet"
        if util_cache.exists():
            try:
                df = pd.read_parquet(util_cache)
                if not df.empty and "margin_utilization" in df.columns:
                    val = df["margin_utilization"].iloc[0]
                    return None if pd.isna(val) else float(val)
            except Exception:
                pass

        today, _, _, _, limit = self._fetch_margin_row_openapi(ticker, trade_date, flags)
        if today is None or limit is None or limit <= 0:
            return None
        ratio = today / limit
        # Write backward-compat util cache for any consumers reading twse_margin_util_*
        if not util_cache.exists():
            pd.DataFrame([{"margin_utilization": ratio}]).to_parquet(util_cache, index=False)
        return ratio

    def _fetch_daytrade_data(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> float | None:
        """Fetch 當沖占成交量比重 from TWSE TWTB4U 當沖 endpoint.

        Returns daytrade_ratio (0.0–1.0) or None if unavailable/error.
        Non-scoring: value is for LLM hint only.
        Cache key: twse_daytrade_{ticker}_{date}.parquet
        """
        cache = self._cache_dir / f"twse_daytrade_{ticker}_{trade_date}.parquet"
        if cache.exists():
            try:
                df = pd.read_parquet(cache)
                if not df.empty and "daytrade_ratio" in df.columns:
                    val = df["daytrade_ratio"].iloc[0]
                    return None if pd.isna(val) else float(val)
            except Exception:
                pass

        try:
            resp = requests.get(
                TWSE_DAYTRADE_URL,
                params={
                    "date": trade_date.strftime("%Y%m%d"),
                    "selectType": "ALL",
                    "response": "json",
                },
                headers=_TWSE_HEADERS,
                timeout=10,
                verify=False,
            )
            resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError:
                return None  # TWSE rate-limited — daytrade is hint-only, skip silently

            if body.get("stat") != "OK" or not body.get("data"):
                return None

            fields = body.get("fields", [])
            try:
                code_idx = fields.index("證券代號")
            except ValueError:
                return None

            # Find 當沖占比 column (dynamic lookup across naming variants)
            ratio_idx: int | None = None
            for candidate in ("當沖占成交量比重", "當沖比率", "當沖比例"):
                if candidate in fields:
                    ratio_idx = fields.index(candidate)
                    break
            if ratio_idx is None:
                return None

            for row in body["data"]:
                if row[code_idx].strip() == ticker:
                    raw = row[ratio_idx].replace(",", "").replace("%", "").strip()
                    try:
                        pct = float(raw)
                        # Value may be expressed as percentage (e.g. 23.5) or ratio (0.235)
                        ratio = pct / 100.0 if pct > 1.0 else pct
                        pd.DataFrame([{"daytrade_ratio": ratio}]).to_parquet(cache, index=False)
                        return ratio
                    except ValueError:
                        return None

            return None

        except Exception as e:
            logger.warning(
                "ChipProxyFetcher: daytrade fetch failed for %s %s: %s", ticker, trade_date, e
            )
            return None
