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

# TPEx (上櫃/OTC) institutional flow — fallback when ticker not found on TWSE T86.
# Date format: YYYY/MM/DD  |  Fields: idx0=代號, idx4=外資買賣超, idx10=投信買賣超, idx16=自營商買賣超
TPEX_T86_URL = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"

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
        # Date-level in-memory cache: {date: {ticker: (foreign, trust, dealer)}}
        # One T86 HTTP request per date serves ALL tickers (selectType=ALL returns full table).
        self._t86_date_cache: dict[date, dict[str, tuple[int | None, int | None, int | None]]] = {}
        # Margin openapi returns ALL stocks in one response — cache once per date.
        # {date: {ticker: (today_margin, prev_margin, today_short, prev_short, margin_limit)}}
        self._margin_date_cache: dict[date, dict[str, tuple[int | None, int | None, int | None, int | None, int | None]]] = {}
        # SBL endpoint returns full market table — cache once per date.
        # {date: {ticker: sbl_ratio}}
        self._sbl_date_cache: dict[date, dict[str, float]] = {}
        # DayTrade endpoint returns full market table — cache once per date.
        # {date: {ticker: daytrade_ratio}}
        self._daytrade_date_cache: dict[date, dict[str, float]] = {}
        # TPEx T86 (上櫃三大法人) — full market table, cache once per date.
        # {date: {ticker: (foreign, trust, dealer)}}
        self._tpex_t86_date_cache: dict[date, dict[str, tuple[int | None, int | None, int | None]]] = {}
        # Rate-limit circuit breaker: after N consecutive rate-limited T86 dates,
        # skip ALL future T86 HTTP calls for this session. Reset on next process start.
        self._t86_consecutive_failures: int = 0
        self._t86_circuit_open: bool = False
        # TDCC 集保股權分散表 — weekly, cache by ISO week string "YYYY-WW"
        # {week_key: {ticker: (large_pct, retail_pct, super_pct, super_count)}}
        self._tdcc_week_cache: dict[str, dict[str, tuple[float, float, float, int]]] = {}
        # 流通股數表 {ticker: shares (in shares, not lots)}; populated externally by caller
        self.shares_map: dict[str, int] = {}

    def fetch(
        self,
        ticker: str,
        trade_date: date,
        today_volume: int = 0,
        total_shares: int = 0,
    ) -> TWSEChipProxy:
        """Fetch chip proxy data for ticker on trade_date.

        today_volume: today's actual traded volume in shares, used to compute
        inst_buy_pct. Pass 0 to skip pct calculation.
        total_shares: total shares outstanding (in shares, not lots); used for 換手率.
        If 0, falls back to self.shares_map.get(ticker, 0).

        Returns TWSEChipProxy(is_available=False) on any failure — never raises.
        """
        if total_shares <= 0:
            total_shares = self.shares_map.get(ticker, 0)

        flags: list[str] = []

        foreign_net, trust_net, dealer_net = self._fetch_t86_data(ticker, trade_date, flags)
        margin_change = self._fetch_margin_balance_change(ticker, trade_date, flags)
        (foreign_consec, trust_consec, dealer_consec, buy_2_of_3,
         cumul_foreign_20d, cumul_trust_20d, inst_buy_days_ratio, inst_flow_accel
         ) = self._fetch_institution_consecutive_days(ticker, trade_date, flags)
        short_increased, short_margin_ratio = self._fetch_short_data(ticker, trade_date, flags)
        sbl_ratio = self._fetch_sbl_data(ticker, trade_date, flags)
        margin_util = self._fetch_margin_utilization(ticker, trade_date, flags)
        daytrade_ratio = self._fetch_daytrade_data(ticker, trade_date, flags)
        large_chg, retail_chg, super_pct_chg, super_count_chg = self._fetch_tdcc_ownership(ticker, trade_date)

        # ── 派生欄位 ──────────────────────────────────────────────────────────
        fn = foreign_net or 0
        tn = trust_net or 0
        foreign_and_trust_both_buy = fn > 0 and tn > 0
        inst_buy_pct: float | None = None
        if today_volume > 0 and (fn != 0 or tn != 0):
            inst_buy_pct = (fn + tn) / today_volume  # ratio, not percentage

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
            foreign_net_buy=fn,
            trust_net_buy=tn,
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
            institution_buy_2_of_3=buy_2_of_3,
            inst_buy_pct=inst_buy_pct,
            foreign_and_trust_both_buy=foreign_and_trust_both_buy,
            large_holder_chg_pct=large_chg,
            retail_holder_chg_pct=retail_chg,
            super_large_holder_chg_pct=super_pct_chg,
            super_large_holder_count_chg=super_count_chg,
            cumul_foreign_20d=cumul_foreign_20d,
            cumul_trust_20d=cumul_trust_20d,
            inst_buy_days_ratio=inst_buy_days_ratio,
            inst_flow_accel=inst_flow_accel,
            total_shares=total_shares,
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
        # 0. Circuit breaker — if TWSE has been rate-limiting us, skip HTTP entirely.
        #    After 3 consecutive rate-limited dates, assume TWSE is blocking this session.
        if self._t86_circuit_open:
            if trade_date not in self._t86_date_cache:
                self._t86_date_cache[trade_date] = {}
            # Still try TPEx for OTC stocks
            tpex_result = self._fetch_tpex_t86_data(ticker, trade_date, flags)
            if any(v is not None for v in tpex_result):
                return tpex_result
            return None, None, None

        # 1. Date-level memory cache — fastest path (dict lookup, ~0.001ms)
        if trade_date in self._t86_date_cache:
            result = self._t86_date_cache[trade_date].get(ticker)
            if result is not None:
                return result
            # Date is cached but ticker not found on TWSE → try TPEx
            tpex_result = self._fetch_tpex_t86_data(ticker, trade_date, flags)
            if any(v is not None for v in tpex_result):
                return tpex_result
            flags.append(f"TWSE_T86_TICKER_NOT_FOUND:{ticker}")
            return None, None, None

        # 2. Per-ticker parquet cache (survives across process restarts)
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

        # 3. API call (slowest — fetches full market table, populates date cache)
        try:
            body = None
            for _attempt in range(3):
                try:
                    resp = requests.get(
                        TWSE_T86_URL,
                        params={
                            "date": trade_date.strftime("%Y%m%d"),
                            "selectType": "ALL",
                            "response": "json",
                        },
                        headers=_TWSE_HEADERS,
                        timeout=15,
                        verify=False,  # TWSE CA cert missing Subject Key Identifier (OpenSSL 3.x strict)
                    )
                except requests.exceptions.Timeout:
                    logger.debug("T86 timeout for %s %s (attempt %d)", ticker, trade_date, _attempt + 1)
                    if _attempt < 2:
                        time.sleep(3.0 + _attempt * 3.0)
                    continue
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
                self._t86_date_cache[trade_date] = {}  # mark date as fetched (empty)
                # Circuit breaker: after 3 consecutive rate-limited dates, stop all T86 HTTP
                self._t86_consecutive_failures += 1
                if self._t86_consecutive_failures >= 3 and not self._t86_circuit_open:
                    self._t86_circuit_open = True
                    logger.warning(
                        "T86 circuit breaker OPEN — %d consecutive rate-limited dates, "
                        "skipping all future TWSE T86 HTTP calls",
                        self._t86_consecutive_failures,
                    )
                return None, None, None

            # Successful fetch — reset circuit breaker counter
            self._t86_consecutive_failures = 0

            if body.get("stat") != "OK" or not body.get("data"):
                flags.append(f"TWSE_T86_NO_DATA:{trade_date}")
                self._t86_date_cache[trade_date] = {}
                return None, None, None

            fields = body.get("fields", [])
            try:
                code_idx = fields.index("證券代號")
                foreign_idx = fields.index("外陸資買賣超股數")
            except ValueError:
                flags.append("TWSE_T86_SCHEMA_CHANGED")
                # Schema change on TWSE side — still try TPEx for OTC stocks
                tpex_result = self._fetch_tpex_t86_data(ticker, trade_date, flags)
                if any(v is not None for v in tpex_result):
                    return tpex_result
                return None, None, None

            # 投信買賣超股數 and 自營商買賣超股數 are optional columns
            trust_idx: int | None = fields.index("投信買賣超股數") if "投信買賣超股數" in fields else None
            dealer_idx: int | None = fields.index("自營商買賣超股數") if "自營商買賣超股數" in fields else None

            # Parse ALL rows into date-level cache and write per-ticker parquet files.
            date_map: dict[str, tuple[int | None, int | None, int | None]] = {}
            for row in body["data"]:
                t = row[code_idx].strip()
                try:
                    f_val = int(row[foreign_idx].replace(",", "").replace("+", "").strip())
                except (ValueError, IndexError):
                    continue

                tr_val: int | None = None
                if trust_idx is not None:
                    try:
                        tr_val = int(row[trust_idx].replace(",", "").replace("+", "").strip())
                    except (ValueError, IndexError):
                        pass

                d_val: int | None = None
                if dealer_idx is not None:
                    try:
                        d_val = int(row[dealer_idx].replace(",", "").replace("+", "").strip())
                    except (ValueError, IndexError):
                        pass

                date_map[t] = (f_val, tr_val, d_val)

                # Write per-ticker parquet so future runs skip HTTP entirely
                t_cache = self._cache_dir / f"twse_t86_{t}_{trade_date}.parquet"
                if not t_cache.exists():
                    cache_row: dict = {"foreign_net_buy": f_val}
                    if tr_val is not None:
                        cache_row["trust_net_buy"] = tr_val
                    if d_val is not None:
                        cache_row["dealer_net_buy"] = d_val
                    try:
                        pd.DataFrame([cache_row]).to_parquet(t_cache, index=False)
                    except Exception:
                        pass

            self._t86_date_cache[trade_date] = date_map

            result = date_map.get(ticker)
            if result is not None:
                return result

            # Ticker not found on TWSE — try TPEx (上櫃 stocks)
            tpex_result = self._fetch_tpex_t86_data(ticker, trade_date, flags)
            if any(v is not None for v in tpex_result):
                return tpex_result
            flags.append(f"TWSE_T86_TICKER_NOT_FOUND:{ticker}")
            return None, None, None

        except Exception as e:
            logger.warning("ChipProxyFetcher: T86 fetch failed for %s %s: %s", ticker, trade_date, e)
            flags.append(f"TWSE_T86_ERROR:{type(e).__name__}")
            # Cache the failure so subsequent tickers skip this date immediately
            # instead of each waiting for another timeout.
            self._t86_date_cache[trade_date] = {}
            return None, None, None

    def _fetch_tpex_t86_data(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> tuple[int | None, int | None, int | None]:
        """Fetch 三大法人買賣超 from TPEx (上櫃/OTC) endpoint.

        Fallback for stocks not found on TWSE T86 (i.e., 上櫃 stocks).
        Uses date-level in-memory cache: one HTTP request per date serves ALL OTC tickers.
        Returns (foreign_net_buy, trust_net_buy, dealer_net_buy) in shares.

        TPEx field layout — v2 (24 cols, 2026+ new API with tables wrapper):
          0: 代號, 1: 名稱
          2-4: 外陸資 買進/賣出/買賣超
          5-7: 外資自營 買進/賣出/買賣超
          8-10: 外資合計 買進/賣出/買賣超   ← v2 added; was 投信 in v1
          11-13: 投信 買進/賣出/買賣超      ← shifted from 8-10 in v1
          14-16: 自營商(自行) 買進/賣出/買賣超  ← shifted from 11-13
          17-19: 自營商(避險) 買進/賣出/買賣超  ← shifted from 14-16
          20-22: 自營商合計 買進/賣出/買賣超    ← shifted from 17-19
          23: 三大法人買賣超股數合計

        v1 legacy (25 cols, aaData wrapper — response was data[col]):
          8-10: 投信;  11-13: 自行;  14-16: 避險;  17-19: 自營合計;  20-22: 三大法人合計
        """
        # 1. Date-level memory cache — fastest path
        if trade_date in self._tpex_t86_date_cache:
            result = self._tpex_t86_date_cache[trade_date].get(ticker)
            if result is not None:
                return result
            return None, None, None

        # 2. Per-ticker parquet cache
        cache = self._cache_dir / f"tpex_t86_{ticker}_{trade_date}.parquet"
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

        # 3. API call — fetches full OTC market table, populates date cache
        try:
            resp = requests.get(
                TPEX_T86_URL,
                params={
                    "l": "zh-tw",
                    "o": "json",
                    "se": "EW",
                    "t": "D",
                    "d": trade_date.strftime("%Y/%m/%d"),
                },
                headers=_TWSE_HEADERS,
                timeout=12,
                verify=False,
            )
            resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError:
                flags.append(f"TPEX_T86_RATE_LIMITED:{trade_date}")
                self._tpex_t86_date_cache[trade_date] = {}
                return None, None, None

            # TPEx changed response format: v1 had root-level "aaData",
            # v2 (2026+) wraps rows in body["tables"][0]["data"]
            tables = body.get("tables") or []
            data = (
                body.get("aaData")
                or body.get("data")
                or (tables[0].get("data") if tables else [])
                or []
            )
            if not data:
                self._tpex_t86_date_cache[trade_date] = {}
                return None, None, None

            def _parse_shares(val: str) -> int | None:
                val = val.replace(",", "").replace("+", "").strip()
                try:
                    return int(val)
                except ValueError:
                    return None

            # Parse ALL rows into date-level cache + write per-ticker parquets.
            # Detect format version by row length:
            #   v2 (24 cols, tables wrapper): trust=[13], dealer_total=[22]
            #   v1 (25 cols, aaData root):    trust=[10], dealer_hedge=[16]
            is_v2 = any(len(r) == 24 for r in data[:5] if r)
            trust_idx = 13 if is_v2 else 10
            dealer_idx = 22 if is_v2 else 16

            date_map: dict[str, tuple[int | None, int | None, int | None]] = {}
            for row in data:
                if len(row) < 17:
                    continue
                code = str(row[0]).strip()
                foreign_val = _parse_shares(str(row[4]))
                trust_val = _parse_shares(str(row[trust_idx])) if len(row) > trust_idx else None
                dealer_val = _parse_shares(str(row[dealer_idx])) if len(row) > dealer_idx else None
                date_map[code] = (foreign_val, trust_val, dealer_val)

                # Write per-ticker parquet for future runs
                t_cache = self._cache_dir / f"tpex_t86_{code}_{trade_date}.parquet"
                if not t_cache.exists():
                    cache_row: dict = {}
                    if foreign_val is not None:
                        cache_row["foreign_net_buy"] = foreign_val
                    if trust_val is not None:
                        cache_row["trust_net_buy"] = trust_val
                    if dealer_val is not None:
                        cache_row["dealer_net_buy"] = dealer_val
                    if cache_row:
                        try:
                            pd.DataFrame([cache_row]).to_parquet(t_cache, index=False)
                        except Exception:
                            pass

            self._tpex_t86_date_cache[trade_date] = date_map

            result = date_map.get(ticker)
            if result is not None:
                flags.append(f"TPEX_T86:{ticker}")
                return result
            return None, None, None

        except Exception as e:
            logger.warning(
                "ChipProxyFetcher: TPEx T86 fetch failed for %s %s: %s", ticker, trade_date, e
            )
            flags.append(f"TPEX_T86_ERROR:{type(e).__name__}")
            self._tpex_t86_date_cache[trade_date] = {}
            return None, None, None

    def _fetch_margin_row_openapi(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> tuple[int | None, int | None, int | None, int | None, int | None]:
        """Fetch per-stock margin row from TWSE openapi MI_MARGN endpoint.

        Cache key: twse_margin_row_{ticker}_{trade_date}.parquet
        Columns: today_margin, prev_margin, today_short, prev_short, margin_limit

        The openapi endpoint always returns today's data (date param is ignored).
        Both today and previous day values are embedded in a single response row.

        Uses date-level in-memory cache: one HTTP request per date serves ALL tickers.

        Returns:
            (today_margin, prev_margin, today_short, prev_short, margin_limit)
            Any value may be None if the field is absent or an empty string.
        """
        # 1. Date-level memory cache — fastest path
        if trade_date in self._margin_date_cache:
            return self._margin_date_cache[trade_date].get(
                ticker, (None, None, None, None, None)
            )

        # 2. Per-ticker parquet cache (survives across process restarts)
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

        # 3. API call (fetches full market table, populates date cache)
        try:
            resp = requests.get(
                TWSE_MARGIN_OPENAPI_URL,
                timeout=10,
                verify=False,
            )
            resp.raise_for_status()
            rows = resp.json()

            if not isinstance(rows, list):
                flags.append("TWSE_MARGN_ERROR:UnexpectedFormat")
                self._margin_date_cache[trade_date] = {}
                return (None, None, None, None, None)

            def _parse_int(val: str) -> int | None:
                if val is None or val.strip() == "":
                    return None
                try:
                    return int(val.replace(",", "").strip())
                except ValueError:
                    return None

            # Parse ALL rows into date-level cache + write per-ticker parquets
            date_map: dict[str, tuple[int | None, int | None, int | None, int | None, int | None]] = {}
            for row in rows:
                t = row.get("股票代號", "").strip()
                if not t:
                    continue
                today_margin = _parse_int(row.get("融資今日餘額", ""))
                prev_margin = _parse_int(row.get("融資前日餘額", ""))
                today_short = _parse_int(row.get("融券今日餘額", ""))
                prev_short = _parse_int(row.get("融券前日餘額", ""))
                margin_limit = _parse_int(row.get("融資限額", ""))

                date_map[t] = (today_margin, prev_margin, today_short, prev_short, margin_limit)

                # Write per-ticker parquet for future runs
                t_cache = self._cache_dir / f"twse_margin_row_{t}_{trade_date}.parquet"
                if not t_cache.exists():
                    try:
                        pd.DataFrame([{
                            "today_margin": today_margin,
                            "prev_margin": prev_margin,
                            "today_short": today_short,
                            "prev_short": prev_short,
                            "margin_limit": margin_limit,
                        }]).to_parquet(t_cache, index=False)
                    except Exception:
                        pass

            self._margin_date_cache[trade_date] = date_map
            return date_map.get(ticker, (None, None, None, None, None))

        except Exception as e:
            logger.warning(
                "ChipProxyFetcher: openapi MI_MARGN fetch failed for %s %s: %s",
                ticker, trade_date, e,
            )
            flags.append(f"TWSE_MARGN_ERROR:{type(e).__name__}")
            self._margin_date_cache[trade_date] = {}
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
    ) -> tuple[int, int, int, bool, int, int, float, float]:
        """Count consecutive buy days + compute 20-day cumulative flow metrics.

        Returns:
            (foreign_consec, trust_consec, dealer_consec, buy_2_of_3,
             cumul_foreign_20d, cumul_trust_20d, inst_buy_days_ratio, inst_flow_accel)

        Lookback up to 60 calendar days to collect 20 trading days of T86 data.
        buy_2_of_3: True if (Foreign OR Trust) net buy on >= 2 of last 3 trading days.
        inst_flow_accel: (近5日速率) / (近20日速率); >1 = 加速買進, <1 = 減速.
        """
        foreign_vals: list[int] = []
        trust_vals: list[int] = []
        dealer_vals: list[int] = []

        for offset in range(60):  # scan up to 60 calendar days to collect 20 trading days
            check_date = trade_date - timedelta(days=offset)
            if check_date.weekday() >= 5:
                continue
            _silent: list[str] = []
            foreign_val, trust_val, dealer_val = self._fetch_t86_data(ticker, check_date, _silent)
            if foreign_val is not None:
                foreign_vals.append(foreign_val)
            if trust_val is not None:
                trust_vals.append(trust_val)
            if dealer_val is not None:
                dealer_vals.append(dealer_val)
            if len(foreign_vals) >= 20:
                break

        def _count_consec(vals: list[int]) -> int:
            count = 0
            for val in vals:
                if val > 0:
                    count += 1
                else:
                    break
            return count

        # buy_2_of_3: Foreign or Trust net buy on >= 2 of last 3 trading days.
        buy_2_of_3 = False
        if len(foreign_vals) >= 3:
            buys = sum(
                1 for i in range(3)
                if (foreign_vals[i] > 0 if i < len(foreign_vals) else False)
                or (trust_vals[i] > 0 if i < len(trust_vals) else False)
            )
            buy_2_of_3 = (buys >= 2)

        # 20-day cumulative metrics
        n = len(foreign_vals)
        cumul_foreign = sum(foreign_vals)
        cumul_trust = sum(trust_vals[:n])

        buy_days_ratio = 0.0
        if n > 0:
            buy_days = sum(
                1 for i in range(n)
                if foreign_vals[i] > 0 or (i < len(trust_vals) and trust_vals[i] > 0)
            )
            buy_days_ratio = buy_days / n

        # Flow acceleration: compare recent-5d avg vs full-20d avg (foreign + trust combined)
        flow_accel = 0.0
        if n >= 5:
            combined = [
                foreign_vals[i] + (trust_vals[i] if i < len(trust_vals) else 0)
                for i in range(n)
            ]
            avg5  = sum(combined[:5]) / 5
            avg20 = sum(combined) / n
            if avg20 > 0:
                flow_accel = avg5 / avg20
            elif avg20 <= 0 and avg5 > 0:
                flow_accel = 2.0  # 從賣轉買，強勢反轉

        return (
            _count_consec(foreign_vals),
            _count_consec(trust_vals),
            _count_consec(dealer_vals),
            buy_2_of_3,
            cumul_foreign,
            cumul_trust,
            buy_days_ratio,
            flow_accel,
        )

    def _fetch_foreign_consecutive_days(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> int:
        """Backward-compat wrapper. Returns foreign consecutive buy days only.

        Prefer _fetch_institution_consecutive_days() for new call sites — it
        returns all three institutions in one pass.
        """
        foreign_count, *_ = self._fetch_institution_consecutive_days(ticker, trade_date, flags)
        return foreign_count

    def _fetch_sbl_data(
        self, ticker: str, trade_date: date, flags: list[str]
    ) -> float | None:
        """Fetch 借券賣出占成交量比重 from TWSE TWT93U SBL endpoint.

        Returns sbl_ratio (0.0–1.0) or None if unavailable/error.
        Uses date-level in-memory cache: one HTTP request per date serves ALL tickers.
        Cache key: twse_sbl_{ticker}_{date}.parquet
        """
        # 1. Date-level memory cache — fastest path
        if trade_date in self._sbl_date_cache:
            return self._sbl_date_cache[trade_date].get(ticker)

        # 2. Per-ticker parquet cache
        cache = self._cache_dir / f"twse_sbl_{ticker}_{trade_date}.parquet"
        if cache.exists():
            try:
                df = pd.read_parquet(cache)
                if not df.empty and "sbl_ratio" in df.columns:
                    return float(df["sbl_ratio"].iloc[0])
            except Exception:
                pass

        # 3. API call
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
                self._sbl_date_cache[trade_date] = {}
                return None

            if body.get("stat") != "OK" or not body.get("data"):
                self._sbl_date_cache[trade_date] = {}
                return None

            fields = body.get("fields", [])
            try:
                code_idx = fields.index("證券代號")
            except ValueError:
                flags.append("TWSE_SBL_SCHEMA_CHANGED")
                self._sbl_date_cache[trade_date] = {}
                return None

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
                self._sbl_date_cache[trade_date] = {}
                return None

            # Parse ALL rows into date-level cache + write per-ticker parquets
            date_map: dict[str, float] = {}
            for row in body["data"]:
                t = row[code_idx].strip()
                sbl_raw = row[sbl_sell_idx].replace(",", "").strip()
                vol_raw = row[total_vol_idx].replace(",", "").strip()
                try:
                    sbl_shares = int(sbl_raw)
                    total_shares = int(vol_raw)
                except ValueError:
                    continue
                if total_shares <= 0:
                    continue
                ratio = sbl_shares / total_shares
                date_map[t] = ratio

                t_cache = self._cache_dir / f"twse_sbl_{t}_{trade_date}.parquet"
                if not t_cache.exists():
                    try:
                        pd.DataFrame([{"sbl_ratio": ratio}]).to_parquet(t_cache, index=False)
                    except Exception:
                        pass

            self._sbl_date_cache[trade_date] = date_map
            return date_map.get(ticker)

        except Exception as e:
            logger.warning(
                "ChipProxyFetcher: SBL fetch failed for %s %s: %s", ticker, trade_date, e
            )
            flags.append(f"TWSE_SBL_ERROR:{type(e).__name__}")
            self._sbl_date_cache[trade_date] = {}
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
        Uses date-level in-memory cache: one HTTP request per date serves ALL tickers.
        Cache key: twse_daytrade_{ticker}_{date}.parquet
        """
        # 1. Date-level memory cache — fastest path
        if trade_date in self._daytrade_date_cache:
            return self._daytrade_date_cache[trade_date].get(ticker)

        # 2. Per-ticker parquet cache
        cache = self._cache_dir / f"twse_daytrade_{ticker}_{trade_date}.parquet"
        if cache.exists():
            try:
                df = pd.read_parquet(cache)
                if not df.empty and "daytrade_ratio" in df.columns:
                    val = df["daytrade_ratio"].iloc[0]
                    return None if pd.isna(val) else float(val)
            except Exception:
                pass

        # 3. API call
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
                self._daytrade_date_cache[trade_date] = {}
                return None  # TWSE rate-limited — daytrade is hint-only, skip silently

            if body.get("stat") != "OK" or not body.get("data"):
                self._daytrade_date_cache[trade_date] = {}
                return None

            fields = body.get("fields", [])
            try:
                code_idx = fields.index("證券代號")
            except ValueError:
                self._daytrade_date_cache[trade_date] = {}
                return None

            ratio_idx: int | None = None
            for candidate in ("當沖占成交量比重", "當沖比率", "當沖比例"):
                if candidate in fields:
                    ratio_idx = fields.index(candidate)
                    break
            if ratio_idx is None:
                self._daytrade_date_cache[trade_date] = {}
                return None

            # Parse ALL rows into date-level cache + write per-ticker parquets
            date_map: dict[str, float] = {}
            for row in body["data"]:
                t = row[code_idx].strip()
                raw = row[ratio_idx].replace(",", "").replace("%", "").strip()
                try:
                    pct = float(raw)
                    ratio = pct / 100.0 if pct > 1.0 else pct
                except ValueError:
                    continue
                date_map[t] = ratio

                t_cache = self._cache_dir / f"twse_daytrade_{t}_{trade_date}.parquet"
                if not t_cache.exists():
                    try:
                        pd.DataFrame([{"daytrade_ratio": ratio}]).to_parquet(t_cache, index=False)
                    except Exception:
                        pass

            self._daytrade_date_cache[trade_date] = date_map
            return date_map.get(ticker)

        except Exception as e:
            logger.warning(
                "ChipProxyFetcher: daytrade fetch failed for %s %s: %s", ticker, trade_date, e
            )
            self._daytrade_date_cache[trade_date] = {}
            return None

    def _fetch_tdcc_ownership(
        self, ticker: str, trade_date: date
    ) -> tuple[float | None, float | None, float | None, int | None]:
        """Fetch 集保股權分散表 via FinMind TaiwanStockShareholding (weekly data).

        Returns (large_chg_pct, retail_chg_pct, super_large_chg_pct, super_large_count_chg):
          - large_chg_pct: 400張+ 持股比例週變化
          - retail_chg_pct: 100張以下持股比例週變化
          - super_large_chg_pct: 千張+ 持股比例週變化（正 = 大戶加碼）
          - super_large_count_chg: 千張+ 大戶人數週變化（正 = 新機構進場）
          All None if FINMIND_API_KEY missing or request fails.

        大戶定義: ≥ 400,000 shares (400張)
        千張大戶: ≥ 1,000,000 shares (1000張，機構/主力等級)
        散戶定義: < 100,000 shares (100張)
        """
        import os as _os
        api_key = _os.environ.get("FINMIND_API_KEY", "")
        if not api_key:
            return None, None

        # 取本週和上週的 ISO week key
        iso_week = trade_date.isocalendar()
        this_week_key = f"{iso_week.year}-{iso_week.week:02d}"
        prev_date = trade_date - timedelta(weeks=1)
        prev_iso = prev_date.isocalendar()
        prev_week_key = f"{prev_iso.year}-{prev_iso.week:02d}"

        def _fetch_week(week_key: str, ref_date: date) -> dict[str, tuple[float, float]] | None:
            if week_key in self._tdcc_week_cache:
                return self._tdcc_week_cache[week_key]
            # 抓包含 ref_date 的那週資料（往前 7 天）
            start = ref_date - timedelta(days=7)
            try:
                resp = requests.get(
                    "https://api.finmindtrade.com/api/v4/data",
                    params={
                        "dataset": "TaiwanStockShareholding",
                        "data_id": ticker,
                        "start_date": str(start),
                        "end_date": str(ref_date),
                        "token": api_key,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                body = resp.json()
                if body.get("status") != 200 or not body.get("data"):
                    self._tdcc_week_cache[week_key] = {}
                    return {}
                records = body["data"]
                # 找最新一筆日期
                dates = sorted({r["date"] for r in records}, reverse=True)
                latest = dates[0]
                rows = [r for r in records if r["date"] == latest]
                # 計算各持股等級的 Percent 與人數加總
                large_pct = sum(
                    float(r.get("percent", 0))
                    for r in rows
                    if self._tdcc_is_large(r)
                )
                retail_pct = sum(
                    float(r.get("percent", 0))
                    for r in rows
                    if self._tdcc_is_retail(r)
                )
                super_rows = [r for r in rows if self._tdcc_is_super_large(r)]
                super_pct = sum(float(r.get("percent", 0)) for r in super_rows)
                # FinMind 欄位: people 或 person_count（人數）
                super_count = sum(
                    int(r.get("people", r.get("person_count", 0)) or 0)
                    for r in super_rows
                )
                result = {ticker: (large_pct, retail_pct, super_pct, super_count)}
                self._tdcc_week_cache[week_key] = result
                return result
            except Exception as e:
                logger.debug("TDCC fetch failed %s %s: %s", ticker, week_key, e)
                self._tdcc_week_cache[week_key] = {}
                return {}

        this_data = _fetch_week(this_week_key, trade_date)
        prev_data = _fetch_week(prev_week_key, prev_date)

        this_entry = (this_data or {}).get(ticker)
        prev_entry = (prev_data or {}).get(ticker)
        if not this_entry or not prev_entry:
            return None, None, None, None

        large_chg = this_entry[0] - prev_entry[0]
        retail_chg = this_entry[1] - prev_entry[1]
        super_pct_chg = this_entry[2] - prev_entry[2]
        super_count_chg = this_entry[3] - prev_entry[3]
        return large_chg, retail_chg, super_pct_chg, super_count_chg

    @staticmethod
    def _tdcc_is_super_large(row: dict) -> bool:
        """千張 (1,000,000 shares) 以上為千張大戶（機構/主力等級）。"""
        try:
            level = str(row.get("HolderCountLevel") or row.get("level", ""))
            lower = int(level.replace(",", "").split("-")[0].strip())
            return lower >= 1_000_000
        except Exception:
            return False

    @staticmethod
    def _tdcc_is_large(row: dict) -> bool:
        """400張 (400,000 shares) 以上為大戶。"""
        try:
            # FinMind HolderCountLevel 格式: "400,001-600,000" 或數字欄位
            level = str(row.get("HolderCountLevel") or row.get("level", ""))
            # 取下界
            lower = int(level.replace(",", "").split("-")[0].strip())
            return lower >= 400_000
        except Exception:
            return False

    @staticmethod
    def _tdcc_is_retail(row: dict) -> bool:
        """100張 (100,000 shares) 以下為散戶。"""
        try:
            level = str(row.get("HolderCountLevel") or row.get("level", ""))
            parts = level.replace(",", "").split("-")
            upper = int(parts[-1].strip()) if len(parts) > 1 else int(parts[0].strip())
            return upper <= 100_000
        except Exception:
            return False
