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

    def __init__(self, api_key: str | None = None) -> None:
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
        # Short-circuit flag: if broker_trades returns 422 (paid feature), skip all
        # future calls. Saves ~11K useless HTTP round trips in free-tier backtest.
        self._broker_trades_unavailable = False

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
        """Fetch TaiwanStockBrokerTradingStatement for one ticker over a date range.

        Columns (subset used downstream):
          date, stock_id, branch_broker_id, buy, sell
        """
        _BROKER_COLS = ["trade_date", "ticker", "branch_code", "branch_name", "buy_volume", "sell_volume"]

        self._check_halt()

        # Short-circuit: once we know broker trades is a paid feature, skip all future calls.
        if self._broker_trades_unavailable:
            return pd.DataFrame(columns=_BROKER_COLS)

        if use_cache:
            cached = self._load_cache("broker_trades", ticker, start_date, end_date)
            if cached is not None:
                return cached

        try:
            df = self._fetch(
                dataset="TaiwanStockBrokerTradingStatement",
                stock_id=ticker,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:
            # HTTP 422 = plan restriction (paid feature); return empty so caller
            # can activate free_tier_mode gracefully.
            err_str = str(exc)
            if "422" in err_str or "Unprocessable Entity" in err_str:
                self._broker_trades_unavailable = True
                logger.info(
                    "[權限限制] 券商分點明細為付費功能 — 後續所有 ticker 自動跳過（免費模式）"
                )
                return pd.DataFrame(columns=_BROKER_COLS)
            raise

        df = df.rename(
            columns={
                "date": "trade_date",
                "stock_id": "ticker",
                "broker_id": "branch_code",
                "broker_name": "branch_name",
                "buy": "buy_volume",
                "sell": "sell_volume",
            }
        )
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

        if use_cache:
            self._save_cache(df, "broker_trades", ticker, start_date, end_date)
        return df

    def fetch_ohlcv(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
        *,
        adjusted: bool = True,
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
        if ticker in self._ohlcv_mem:
            mem = self._ohlcv_mem[ticker]
            if not mem.empty:
                mem_min = mem["trade_date"].min()
                mem_max = mem["trade_date"].max()
                if mem_min <= start_date and mem_max >= end_date:
                    mask = (mem["trade_date"] >= start_date) & (mem["trade_date"] <= end_date)
                    return mem[mask].reset_index(drop=True).copy()

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
                logger.warning(
                    "[權限限制] 目前 API 權限無法讀取「還原股價」。"
                    "將改用「一般股價」代替（注意：若遇到除權息，技術指標可能會失真）。"
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

        if use_cache:
            self._save_cache(df, cache_key, ticker, start_date, end_date)
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

        for suffix in (".TW", ".TWO"):
            symbol = f"{ticker}{suffix}"
            try:
                raw = yf.download(
                    symbol,
                    start=str(start_date),
                    end=str(end_date + timedelta(days=1)),  # yfinance end is exclusive
                    auto_adjust=True,
                    progress=False,
                    multi_level_index=False,
                )
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
