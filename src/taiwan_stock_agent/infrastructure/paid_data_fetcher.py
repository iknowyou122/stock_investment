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
        # {date: {stock_id: (foreign_net, trust_net, dealer_net)}}
        self._inst_day_cache: dict[date, dict[str, tuple[int, int, int]]] = {}

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
        """Tickers under disposition on trade_date (公布處置有價證券表).

        Dataset: TaiwanStockDispositionSecuritiesPeriod
        Fields verified 2026-05-25: date, stock_id, stock_name, disposition_cnt,
        condition, measure, period_start, period_end
        """
        if trade_date in self._disposal_cache:
            return self._disposal_cache[trade_date]
        rows = self._get("TaiwanStockDispositionSecuritiesPeriod", trade_date)
        result: set[str] = set()
        for r in rows:
            ticker = str(r.get("stock_id", "")).strip()
            if not ticker:
                continue
            # period_end is the actual end field in TaiwanStockDispositionSecuritiesPeriod
            end_str = str(r.get("period_end", "")).strip()
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
        """Tickers with trading halted (台股暫停交易公告).

        Dataset: TaiwanStockSuspended
        Fields verified 2026-05-25: date, stock_id, suspension_time,
        resumption_date, resumption_time
        """
        if trade_date in self._halt_cache:
            return self._halt_cache[trade_date]
        rows = self._get("TaiwanStockSuspended", trade_date)
        result = frozenset(str(r.get("stock_id", "")).strip() for r in rows if r.get("stock_id"))
        self._halt_cache[trade_date] = result
        return result

    def fetch_limit_up_tickers(self, trade_date: date) -> frozenset[str]:
        """Tickers that closed at limit-up price (漲停收盤).

        Dataset: TaiwanStockPriceLimit
        Fields verified 2026-05-25: date, stock_id, reference_price, limit_up, limit_down
        NOTE: This dataset provides price *limits* (thresholds), not actual closing prices.
        We compare reference_price to limit_up as a proxy — tickers where the reference
        price equals the limit-up price are effectively locked at limit-up from prior day.
        This is an approximation; true same-day close vs limit requires TaiwanStockPrice.
        """
        if trade_date in self._limit_up_cache:
            return self._limit_up_cache[trade_date]
        rows = self._get("TaiwanStockPriceLimit", trade_date)
        result: set[str] = set()
        for r in rows:
            ticker = str(r.get("stock_id", "")).strip()
            # reference_price is yesterday's close; limit_up is today's upper bound
            ref = r.get("reference_price")
            limit_up = r.get("limit_up")
            if ticker and ref is not None and limit_up is not None:
                try:
                    # If reference_price == limit_up, the stock was already at ceiling
                    if abs(float(ref) - float(limit_up)) < 0.02:
                        result.add(ticker)
                except (TypeError, ValueError):
                    pass
        fs = frozenset(result)
        self._limit_up_cache[trade_date] = fs
        return fs

    def fetch_daytrade_restricted_tickers(self, trade_date: date) -> frozenset[str]:
        """Tickers where 先賣後買當沖 is suspended (暫停先賣後買當沖預告表).

        Dataset: TaiwanStockDayTradingSuspension
        Fields verified 2026-05-25: stock_id, date, end_date, reason
        The API returns rows where trade_date falls within [date, end_date].
        """
        if trade_date in self._daytrade_cache:
            return self._daytrade_cache[trade_date]
        rows = self._get("TaiwanStockDayTradingSuspension", trade_date)
        result = frozenset(str(r.get("stock_id", "")).strip() for r in rows if r.get("stock_id"))
        self._daytrade_cache[trade_date] = result
        return result

    def fetch_market_margin_maintenance(self, trade_date: date) -> float | None:
        """Overall market margin maintenance rate (大盤融資維持率). None if unavailable.

        Dataset: TaiwanTotalExchangeMarginMaintenance
        Fields verified 2026-05-25: date, TotalExchangeMarginMaintenance
        Returns a single float (market-wide rate, e.g. 196.864 for 2026-05-22).
        """
        if trade_date in self._margin_cache:
            return self._margin_cache[trade_date]
        rows = self._get("TaiwanTotalExchangeMarginMaintenance", trade_date)
        rate: float | None = None
        for r in rows:
            # Actual field name is TotalExchangeMarginMaintenance (CamelCase)
            v = r.get("TotalExchangeMarginMaintenance")
            if v is not None:
                try:
                    rate = float(v)
                    break
                except (TypeError, ValueError):
                    pass
        self._margin_cache[trade_date] = rate
        return rate

    def fetch_institution_day(
        self, trade_date: date
    ) -> dict[str, tuple[int, int, int]]:
        """Full-market institution buy/sell for trade_date via FinMind paid API.

        Returns {stock_id: (foreign_net, trust_net, dealer_net)} in shares.
        Empty dict on failure or missing API key.

        Dataset: TaiwanStockInstitutionalInvestorsBuySell
        Names: Foreign_Investor → foreign, Investment_Trust → trust,
               Dealer_self + Dealer_Hedging + Foreign_Dealer_Self → dealer
        """
        if trade_date in self._inst_day_cache:
            return self._inst_day_cache[trade_date]
        if not self._api_key:
            return {}
        try:
            resp = requests.get(
                _FINMIND_URL,
                params={
                    "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
                    "start_date": trade_date.isoformat(),
                    "end_date": trade_date.isoformat(),
                    "token": self._api_key,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except Exception as e:
            logger.debug("fetch_institution_day(%s) failed: %s", trade_date, e)
            self._inst_day_cache[trade_date] = {}
            return {}

        # Aggregate by stock_id
        foreign: dict[str, int] = {}
        trust: dict[str, int] = {}
        dealer: dict[str, int] = {}
        _dealer_names = {"Dealer_self", "Dealer_Hedging", "Foreign_Dealer_Self"}

        for row in data:
            sid = str(row.get("stock_id", "")).strip()
            name = row.get("name", "")
            net = int(row.get("buy", 0)) - int(row.get("sell", 0))
            if name == "Foreign_Investor":
                foreign[sid] = net
            elif name == "Investment_Trust":
                trust[sid] = net
            elif name in _dealer_names:
                dealer[sid] = dealer.get(sid, 0) + net

        result: dict[str, tuple[int, int, int]] = {}
        all_ids = foreign.keys() | trust.keys() | dealer.keys()
        for sid in all_ids:
            result[sid] = (foreign.get(sid, 0), trust.get(sid, 0), dealer.get(sid, 0))

        self._inst_day_cache[trade_date] = result
        logger.info(
            "fetch_institution_day(%s): %d tickers loaded from FinMind",
            trade_date, len(result),
        )
        return result
