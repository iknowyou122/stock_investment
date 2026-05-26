"""FinMind API client with tenacity retry/backoff and Parquet file cache."""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

FINMIND_BASE_URL = "https://api.finmindtrade.com/api/v4/data"
CACHE_DIR = Path(__file__).resolve().parents[4] / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Taiwan local time cutoff: T+1 分點 data is typically available after 20:00 CST
_DATA_READY_HOUR_CST = 20
_CST_OFFSET = 8  # UTC+8


class FinMindError(Exception):
    """Raised when FinMind API returns an error response."""


class DataNotYetAvailableError(FinMindError):
    """Raised when requested date's data has not been published yet."""


def _cache_path(dataset: str, ticker: str, start: date, end: date) -> Path:
    return CACHE_DIR / f"{dataset}_{ticker}_{start}_{end}.parquet"


def _is_data_ready_for(target_date: date) -> bool:
    """Return True if it's late enough for T+1 data to be available.

    FinMind publishes 分點 data by ~20:00 Taiwan time (UTC+8) on the day after
    the trading day. So data for trade_date D is available on D+1 after 20:00 CST.
    """
    now_utc = datetime.utcnow()
    now_cst = now_utc + timedelta(hours=_CST_OFFSET)
    today_cst = now_cst.date()

    # T+1 data for target_date is available starting: (target_date + 1 day) at 20:00 CST
    publish_date = target_date + timedelta(days=1)
    if today_cst < publish_date:
        return False
    if today_cst == publish_date and now_cst.hour < _DATA_READY_HOUR_CST:
        return False
    return True


class FinMindClient:
    """Thin wrapper around the FinMind v4 REST API.

    Handles:
    - API key injection from environment
    - tenacity retry with exponential backoff (network/5xx errors)
    - Parquet file cache (keyed by dataset, ticker, date range)
    - T+1 data freshness guard (aborts if data not yet published)
    - halt_flag: if set True externally, all fetch calls raise immediately
    """

    def __init__(self, api_key: str | None = None, ohlcv_repo=None) -> None:
        self.api_key = api_key or os.environ.get("FINMIND_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "FinMind API key required. Set FINMIND_API_KEY env var or pass api_key."
            )
        self.halt_flag = False
        # In-memory superset OHLCV cache: {ticker: DataFrame covering widest fetched range}
        # Allows backtest to pre-fetch the full date range once per ticker, then serve
        # all per-day slices from memory — eliminates 99% of OHLCV API calls in backtest.
        self._ohlcv_mem: dict[str, pd.DataFrame] = {}
        # Optional DB-backed L2 cache (OHLCVRepository). When set, fetch_ohlcv uses
        # DB-first pattern: read DB → fill gaps via API → write back to DB.
        self._ohlcv_repo = ohlcv_repo
        # (removed free-tier short-circuit flag — broker data now available via paid plan)
        # Suppress repeated warnings after the first occurrence
        self._warned_adj_unavailable = False
        self._warned_ohlcv_402 = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch_broker_trades(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
        *,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """Fetch broker branch trading data (分點買賣明細) for one ticker.

        Dataset: TaiwanStockTradingDailyReport
        Columns returned: trade_date, ticker, branch_code, branch_name, buy_volume, sell_volume
        Note: buy_volume / sell_volume are in shares (股), not 張.

        The API only supports single-day queries, so we iterate day by day and
        cache each day individually under key "broker_day_{ticker}_{date}".
        """
        _BROKER_COLS = ["trade_date", "ticker", "branch_code", "branch_name", "buy_volume", "sell_volume"]

        self._check_halt()

        # Enumerate calendar days (skip weekends; FinMind returns empty for holidays naturally)
        day = start_date
        day_frames: list[pd.DataFrame] = []
        while day <= end_date:
            if day.weekday() < 5:  # Mon–Fri only
                day_df = self._fetch_broker_day(ticker, day, use_cache=use_cache)
                if day_df is not None and not day_df.empty:
                    day_frames.append(day_df)
            day += timedelta(days=1)

        if not day_frames:
            return pd.DataFrame(columns=_BROKER_COLS)

        return pd.concat(day_frames, ignore_index=True)

    def _fetch_broker_day(
        self, ticker: str, day: date, *, use_cache: bool = True
    ) -> pd.DataFrame | None:
        """Fetch broker trades for a single trading day; return None on auth failure."""
        _BROKER_COLS = ["trade_date", "ticker", "branch_code", "branch_name", "buy_volume", "sell_volume"]

        if use_cache:
            cached = self._load_cache("broker_day", ticker, day, day)
            if cached is not None:
                return cached

        try:
            # Pass same day for both start/end — API rejects multi-day ranges for this dataset
            df = self._fetch(
                dataset="TaiwanStockTradingDailyReport",
                stock_id=ticker,
                start_date=day,
                end_date=day,
            )
        except Exception as exc:
            err_str = str(exc)
            if "403" in err_str or "Forbidden" in err_str or "401" in err_str:
                logger.warning("[FinMind] 分點資料權限不足，請確認付費方案包含 TaiwanStockTradingDailyReport")
                return None
            # Non-auth errors (network, 5xx): propagate so tenacity can retry upstream
            raise

        if df.empty:
            # Cache empty result so we don't re-query holidays
            if use_cache:
                self._save_cache(pd.DataFrame(columns=_BROKER_COLS), "broker_day", ticker, day, day)
            return pd.DataFrame(columns=_BROKER_COLS)

        df = df.rename(
            columns={
                "date": "trade_date",
                "stock_id": "ticker",
                "securities_trader_id": "branch_code",
                "securities_trader": "branch_name",
                "buy": "buy_volume",
                "sell": "sell_volume",
            }
        )
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

        if use_cache:
            self._save_cache(df, "broker_day", ticker, day, day)
        return df

    def fetch_ohlcv(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
        *,
        adjusted: bool = False,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """Fetch daily OHLCV.

        Priority:
          1. TaiwanStockPriceAdj (FinMind, adjusted, paid plan)
          2. TaiwanStockPrice    (FinMind, unadjusted, free plan)
          3. yfinance            (free fallback when FinMind returns 402)

        Columns returned: trade_date, ticker, open, high, low, close, volume
        """
        self._check_halt()
        dataset = "TaiwanStockPriceAdj" if adjusted else "TaiwanStockPrice"
        cache_key = f"ohlcv_{'adj' if adjusted else 'raw'}"

        # 0. In-memory superset cache — fastest path (covers backtest pre-warm use case)
        # Only check that data reaches the end_date (analysis date). The start_date
        # may precede the earliest available trading day — that's fine, we'll just
        # get fewer rows (engine handles INSUFFICIENT_HISTORY gracefully).
        if ticker in self._ohlcv_mem:
            mem = self._ohlcv_mem[ticker]
            if not mem.empty and mem["trade_date"].max() >= end_date:
                mask = (mem["trade_date"] >= start_date) & (mem["trade_date"] <= end_date)
                result = mem[mask]
                if not result.empty:
                    return result.reset_index(drop=True).copy()

        # 1. DB cache (L2) — persistent across sessions, source-agnostic
        if self._ohlcv_repo is not None and not adjusted:
            db_df = self._ohlcv_repo.get(ticker, start_date, end_date)
            if not db_df.empty:
                db_max = db_df["trade_date"].max()
                if db_max >= end_date:
                    # DB has full range — serve directly
                    self._update_ohlcv_mem(ticker, db_df)
                    return db_df.reset_index(drop=True).copy()
                # DB has partial data — only fetch the missing tail from API
                start_date = db_max  # re-fetch from last known date (inclusive overlap is OK)

        if use_cache:
            cached = self._load_cache(cache_key, ticker, start_date, end_date)
            if cached is not None:
                self._update_ohlcv_mem(ticker, cached)
                return cached

        df: pd.DataFrame | None = None
        try:
            df = self._fetch(
                dataset=dataset,
                stock_id=ticker,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as e:
            err_str = str(e)
            # TaiwanStockPriceAdj requires paid plan → try unadjusted first
            if adjusted and ("400" in err_str or "register" in err_str.lower()):
                if not self._warned_adj_unavailable:
                    self._warned_adj_unavailable = True
                    logger.warning(
                        "[權限限制] 還原股價不可用，改用一般股價（後續不再重複提示）"
                    )
                try:
                    df = self._fetch(
                        dataset="TaiwanStockPrice",
                        stock_id=ticker,
                        start_date=start_date,
                        end_date=end_date,
                    )
                except Exception as e2:
                    if "402" in str(e2) or "Payment Required" in str(e2):
                        df = None  # fall through to yfinance
                    else:
                        raise
            elif "402" in err_str or "Payment Required" in err_str:
                df = None  # fall through to yfinance
            else:
                raise

        _used_yfinance = df is None
        if df is None:
            df = self._fetch_ohlcv_yfinance(ticker, start_date, end_date)

        if df is None or df.empty:
            return pd.DataFrame(columns=["trade_date", "ticker", "open", "high", "low", "close", "volume"])

        df = df.rename(
            columns={
                "date": "trade_date",
                "stock_id": "ticker",
                "Trading_Volume": "volume",
                "open": "open",
                "max": "high",
                "min": "low",
                "close": "close",
            }
        )
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        cols = [c for c in ["trade_date", "ticker", "open", "high", "low", "close", "volume"] if c in df.columns]
        df = df[cols]
        for col in ["trade_date", "ticker", "open", "high", "low", "close", "volume"]:
            if col not in df.columns:
                df[col] = None
        df = df[["trade_date", "ticker", "open", "high", "low", "close", "volume"]]

        # FinMind sometimes returns rows with NaN close (data not yet published).
        # Detect this for the end_date row and patch via yfinance.
        end_row = df[df["trade_date"] == end_date] if not df.empty else pd.DataFrame()
        if not end_row.empty and pd.isna(end_row["close"].iloc[0]):
            yf_df = self._fetch_ohlcv_yfinance(ticker, end_date, end_date)
            if yf_df is not None and not yf_df.empty:
                # yfinance already returns standard columns (trade_date, open, high, low, close, volume, ticker)
                cols = [c for c in ["trade_date", "ticker", "open", "high", "low", "close", "volume"] if c in yf_df.columns]
                yf_row = yf_df[cols]
                df = df[df["trade_date"] != end_date]
                df = pd.concat([df, yf_row], ignore_index=True)
                df = df.sort_values("trade_date").reset_index(drop=True)
                logger.info(
                    "[FinMind] %s %s close=NaN, patched via yfinance (close=%.2f)",
                    ticker, end_date, float(yf_row["close"].iloc[0]),
                )

        if use_cache:
            self._save_cache(df, cache_key, ticker, start_date, end_date)

        # Write to DB (L2 cache) so future sessions skip the API call
        if self._ohlcv_repo is not None and not adjusted:
            self._ohlcv_repo.upsert(df, source="yfinance" if _used_yfinance else "finmind")

        self._update_ohlcv_mem(ticker, df)
        return df

    def _update_ohlcv_mem(self, ticker: str, df: pd.DataFrame) -> None:
        """Merge df into the in-memory superset cache for ticker."""
        if df.empty:
            return
        if ticker not in self._ohlcv_mem or self._ohlcv_mem[ticker].empty:
            self._ohlcv_mem[ticker] = df.copy()
        else:
            combined = (
                pd.concat([self._ohlcv_mem[ticker], df])
                .drop_duplicates("trade_date")
                .sort_values("trade_date")
                .reset_index(drop=True)
            )
            self._ohlcv_mem[ticker] = combined

    @staticmethod
    def _fetch_ohlcv_yfinance(
        ticker: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame | None:
        """Fallback OHLCV fetch via yfinance (.TW then .TWO suffixes)."""
        try:
            import yfinance as yf  # optional dependency
        except ImportError:
            logger.warning("yfinance not installed; cannot fall back for %s", ticker)
            return None

        import logging as _logging
        import warnings as _warnings
        for suffix in (".TW", ".TWO"):
            symbol = f"{ticker}{suffix}"
            try:
                # Suppress yfinance's noisy logs AND warnings for delisted/not-found tickers;
                # "possibly delisted; no timezone found" comes via warnings.warn(), not logger.
                _yf_logger = _logging.getLogger("yfinance")
                _prev_level = _yf_logger.level
                _yf_logger.setLevel(_logging.CRITICAL)
                try:
                    with _warnings.catch_warnings():
                        _warnings.simplefilter("ignore")
                        # Use Ticker.history() instead of yf.download() — each Ticker object
                        # has an independent session, making it safe for concurrent threads.
                        raw = yf.Ticker(symbol).history(
                            start=str(start_date),
                            end=str(end_date + timedelta(days=1)),
                            auto_adjust=True,
                            actions=False,
                        )
                finally:
                    _yf_logger.setLevel(_prev_level)
            except Exception as exc:
                logger.debug("yfinance %s failed: %s", symbol, exc)
                continue

            if raw is None or raw.empty:
                continue

            raw = raw.reset_index()
            raw.columns = [c.lower() for c in raw.columns]
            raw = raw.rename(columns={"date": "trade_date"})
            raw["trade_date"] = pd.to_datetime(raw["trade_date"]).dt.date
            raw["ticker"] = ticker
            raw = raw.rename(columns={"adj close": "close"} if "adj close" in raw.columns else {})
            logger.info("yfinance fallback OK: %s (%d rows)", symbol, len(raw))
            return raw

        logger.warning("yfinance fallback failed for %s (tried .TW and .TWO)", ticker)
        return None

    def fetch_taiex_history(
        self,
        end_date: date,
        lookback_days: int = 35,
        *,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """Fetch TAIEX (台灣加權指數) daily OHLCV for RS vs 大盤 scoring (Factor 6).

        Uses FinMind dataset TaiwanStockPrice with data_id "TAIEX".
        Returns columns: trade_date, ticker, open, high, low, close, volume.
        Returns empty DataFrame if the index data is unavailable on this plan.
        """
        self._check_halt()
        start_date = end_date - timedelta(days=lookback_days)
        cache_key = "ohlcv_taiex"
        ticker = "TAIEX"

        if use_cache:
            cached = self._load_cache(cache_key, ticker, start_date, end_date)
            if cached is not None:
                return cached

        try:
            df = self._fetch(
                dataset="TaiwanStockPrice",
                stock_id=ticker,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:
            logger.warning(
                "fetch_taiex_history failed for %s-%s: %s — RS factor will be skipped",
                start_date,
                end_date,
                exc,
            )
            return pd.DataFrame()

        if df.empty:
            return pd.DataFrame()

        df = df.rename(
            columns={
                "date": "trade_date",
                "stock_id": "ticker",
                "Trading_Volume": "volume",
                "max": "high",
                "min": "low",
            }
        )
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        # Keep only columns that exist in the response
        cols = [c for c in ["trade_date", "ticker", "open", "high", "low", "close", "volume"] if c in df.columns]
        df = df[cols]

        if use_cache:
            self._save_cache(df, cache_key, ticker, start_date, end_date)
        return df

    def verify_data_freshness(self, ticker: str, expected_date: date) -> None:
        """Raise DataNotYetAvailableError if T+1 data for expected_date is not ready.

        Call this before running a daily analysis run to guard against operating on
        stale data (yesterday's results returned because today's aren't published yet).
        """
        if not _is_data_ready_for(expected_date):
            raise DataNotYetAvailableError(
                f"T+1 data for {expected_date} not yet available. "
                f"Run after {_DATA_READY_HOUR_CST}:00 CST on "
                f"{expected_date + timedelta(days=1)}."
            )

        # Cross-check: fetch latest broker trade and confirm trade_date matches
        end = expected_date
        start = expected_date - timedelta(days=5)  # small window
        df = self.fetch_broker_trades(ticker, start, end, use_cache=False)
        if df.empty:
            raise DataNotYetAvailableError(
                f"No broker trade data returned for {ticker} around {expected_date}. "
                "FinMind may not have published it yet."
            )
        latest = df["trade_date"].max()
        if latest < expected_date:
            raise DataNotYetAvailableError(
                f"WARNING: T+1 data not yet available for {expected_date}. "
                f"Latest date in FinMind response: {latest}. Aborting run."
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_halt(self) -> None:
        if self.halt_flag:
            raise FinMindError("FinMindClient halt_flag is set — all fetches aborted.")

    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _fetch(
        self,
        dataset: str,
        stock_id: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        params: dict[str, Any] = {
            "dataset": dataset,
            "data_id": stock_id,
            "start_date": str(start_date),
            "end_date": str(end_date),
            "token": self.api_key,
        }
        logger.debug("GET %s params=%s", FINMIND_BASE_URL, params)
        resp = requests.get(FINMIND_BASE_URL, params=params, timeout=30)
        resp.raise_for_status()

        body = resp.json()
        if body.get("status") != 200:
            raise FinMindError(
                f"FinMind API error {body.get('status')}: {body.get('msg', 'unknown')}"
            )

        records = body.get("data", [])
        if not records:
            return pd.DataFrame()
        return pd.DataFrame(records)

    @staticmethod
    def _load_cache(
        dataset: str, ticker: str, start: date, end: date
    ) -> pd.DataFrame | None:
        path = _cache_path(dataset, ticker, start, end)
        if path.exists():
            logger.debug("Cache hit: %s", path)
            return pd.read_parquet(path)
        return None

    @staticmethod
    def _save_cache(
        df: pd.DataFrame, dataset: str, ticker: str, start: date, end: date
    ) -> None:
        if df.empty:
            return
        path = _cache_path(dataset, ticker, start, end)
        df.to_parquet(path, index=False)
        logger.debug("Cache saved: %s", path)
