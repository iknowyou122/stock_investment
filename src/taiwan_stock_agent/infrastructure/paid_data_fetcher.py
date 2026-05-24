"""Paid FinMind data fetcher — market-level daily datasets.

All methods return empty collections on failure (graceful degradation).
Results are cached per trade_date within the session.
"""
from __future__ import annotations

import logging
import os
from datetime import date

import requests

logger = logging.getLogger(__name__)

_FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


class PaidDataFetcher:
    """Fetches market-level paid FinMind datasets (one API call per dataset per day).

    Requires FINMIND_API_KEY env var (loaded via dotenv in calling scripts).
    Silently returns empty results if key is missing or API call fails.
    """

    def __init__(self) -> None:
        self._api_key = os.environ.get("FINMIND_API_KEY", "")
        self._disposal_cache: dict[date, frozenset[str]] = {}
        self._halt_cache: dict[date, frozenset[str]] = {}
        self._limit_up_cache: dict[date, frozenset[str]] = {}
        self._daytrade_cache: dict[date, frozenset[str]] = {}
        self._margin_cache: dict[date, float | None] = {}

    def _get(self, dataset: str, trade_date: date) -> list[dict]:
        """Generic FinMind API call. Returns [] on any failure."""
        if not self._api_key:
            return []
        try:
            resp = requests.get(
                _FINMIND_URL,
                params={
                    "dataset": dataset,
                    "start_date": trade_date.isoformat(),
                    "end_date": trade_date.isoformat(),
                    "token": self._api_key,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", []) if isinstance(data, dict) else []
        except Exception as e:
            logger.debug("PaidDataFetcher._get(%s, %s) failed: %s", dataset, trade_date, e)
            return []

    def fetch_disposal_tickers(self, trade_date: date) -> frozenset[str]:
        """Tickers under disposition on trade_date (公布處置有價證券表)."""
        if trade_date in self._disposal_cache:
            return self._disposal_cache[trade_date]
        rows = self._get("TaiwanStockDisposal", trade_date)
        result: set[str] = set()
        for r in rows:
            ticker = str(r.get("stock_id", "")).strip()
            if not ticker:
                continue
            end_str = str(r.get("end_date", "")).strip()
            try:
                end = date.fromisoformat(end_str) if end_str else trade_date
                if end >= trade_date:
                    result.add(ticker)
            except ValueError:
                result.add(ticker)  # conservative: include if parsing fails
        fs = frozenset(result)
        self._disposal_cache[trade_date] = fs
        if result:
            logger.info("PaidDataFetcher: %d disposal tickers on %s", len(result), trade_date)
        return fs

    def fetch_halt_tickers(self, trade_date: date) -> frozenset[str]:
        """Tickers with trading halted (台股暫停交易公告)."""
        if trade_date in self._halt_cache:
            return self._halt_cache[trade_date]
        rows = self._get("TaiwanStockTradingHalt", trade_date)
        result = frozenset(str(r.get("stock_id", "")).strip() for r in rows if r.get("stock_id"))
        self._halt_cache[trade_date] = result
        return result

    def fetch_limit_up_tickers(self, trade_date: date) -> frozenset[str]:
        """Tickers that closed at limit-up price (漲停收盤)."""
        if trade_date in self._limit_up_cache:
            return self._limit_up_cache[trade_date]
        rows = self._get("TaiwanDailyPriceLimit", trade_date)
        result: set[str] = set()
        for r in rows:
            ticker = str(r.get("stock_id", "")).strip()
            close = r.get("close") or r.get("收盤價")
            limit_up = r.get("limit_up_price") or r.get("漲停價") or r.get("漲停")
            if ticker and close is not None and limit_up is not None:
                try:
                    if abs(float(close) - float(limit_up)) < 0.02:
                        result.add(ticker)
                except (TypeError, ValueError):
                    pass
        fs = frozenset(result)
        self._limit_up_cache[trade_date] = fs
        return fs

    def fetch_daytrade_restricted_tickers(self, trade_date: date) -> frozenset[str]:
        """Tickers where 先賣後買當沖 is suspended (暫停先賣後買當沖預告表)."""
        if trade_date in self._daytrade_cache:
            return self._daytrade_cache[trade_date]
        rows = self._get("TaiwanStockDayTradeRestriction", trade_date)
        result = frozenset(str(r.get("stock_id", "")).strip() for r in rows if r.get("stock_id"))
        self._daytrade_cache[trade_date] = result
        return result

    def fetch_market_margin_maintenance(self, trade_date: date) -> float | None:
        """Overall market margin maintenance rate (大盤融資維持率). None if unavailable."""
        if trade_date in self._margin_cache:
            return self._margin_cache[trade_date]
        rows = self._get("TaiwanMarginMaintenanceRatio", trade_date)
        rate: float | None = None
        for r in rows:
            for key in ("margin_maintenance_ratio", "rate", "維持率", "整體維持率"):
                v = r.get(key)
                if v is not None:
                    try:
                        rate = float(v)
                        break
                    except (TypeError, ValueError):
                        pass
            if rate is not None:
                break
        self._margin_cache[trade_date] = rate
        return rate
