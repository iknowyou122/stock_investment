"""Paid FinMind data fetcher — market-level daily datasets.

All methods return empty collections on failure (graceful degradation).
Results are cached per trade_date within the session.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta

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
        # New: futures/options context caches
        self._futures_cache: dict[date, dict] = {}
        self._options_cache: dict[date, dict] = {}
        # Per-ticker caches: {(ticker, date): dict}
        self._per_cache: dict[tuple[str, date], dict] = {}
        self._revenue_cache: dict[tuple[str, date], dict] = {}

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

    def _get_ticker(
        self,
        dataset: str,
        ticker: str,
        trade_date: date,
        start_date: date | None = None,
    ) -> list[dict]:
        """Per-ticker FinMind API call with optional date range. Returns [] on failure."""
        if not self._api_key:
            return []
        sd = (start_date or trade_date).isoformat()
        try:
            resp = requests.get(
                _FINMIND_URL,
                params={
                    "dataset": dataset,
                    "data_id": ticker,
                    "start_date": sd,
                    "end_date": trade_date.isoformat(),
                    "token": self._api_key,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", []) if isinstance(data, dict) else []
        except Exception as e:
            logger.debug("PaidDataFetcher._get_ticker(%s, %s, %s) failed: %s", dataset, ticker, trade_date, e)
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

    # ── New: Market Futures Context ────────────────────────────────────────

    def fetch_futures_context(self, trade_date: date) -> dict:
        """台指期三大法人未平倉 — 多空方向及強度 (FinMind paid).

        Replaces the unreliable TAIFEX opendata fetch in twse_client.
        Dataset: TaiwanFuturesInstitutionalInvestors
        Returns dict:
            tx_foreign_net_oi  — TX 外資淨多口數 (positive=net long, negative=net short)
            tx_trust_net_oi    — TX 投信淨多口數
            te_foreign_net_oi  — TE 電子期貨外資淨多口數
            composite_bearish  — True if TX 外資 strongly net short (< -10000)
            composite_bullish  — True if TX 投信 net long AND 外資 not heavily short (> -30000)
            data_available     — False if API call failed or no key
        """
        if trade_date in self._futures_cache:
            return self._futures_cache[trade_date]

        _safe = {
            "tx_foreign_net_oi": 0, "tx_trust_net_oi": 0, "te_foreign_net_oi": 0,
            "composite_bearish": False, "composite_bullish": False, "data_available": False,
        }
        rows = self._get("TaiwanFuturesInstitutionalInvestors", trade_date)
        if not rows:
            self._futures_cache[trade_date] = _safe
            return _safe

        try:
            tx_foreign_net = 0
            tx_trust_net = 0
            te_foreign_net = 0
            for row in rows:
                fid = str(row.get("futures_id", ""))
                inst = str(row.get("institutional_investors", ""))
                long_oi = int(row.get("long_open_interest_balance_volume", 0) or 0)
                short_oi = int(row.get("short_open_interest_balance_volume", 0) or 0)
                net = long_oi - short_oi
                if fid == "TX":
                    if inst == "外資":
                        tx_foreign_net += net
                    elif inst == "投信":
                        tx_trust_net += net
                elif fid == "TE" and inst == "外資":
                    te_foreign_net += net

            result = {
                "tx_foreign_net_oi": tx_foreign_net,
                "tx_trust_net_oi": tx_trust_net,
                "te_foreign_net_oi": te_foreign_net,
                "composite_bearish": tx_foreign_net < -10000,
                "composite_bullish": tx_trust_net > 0 and tx_foreign_net > -30000,
                "data_available": True,
            }
            self._futures_cache[trade_date] = result
            logger.info(
                "fetch_futures_context(%s): TX外資=%+d 投信=%+d TE外資=%+d",
                trade_date, tx_foreign_net, tx_trust_net, te_foreign_net,
            )
            return result
        except Exception as e:
            logger.debug("fetch_futures_context(%s) parse failed: %s", trade_date, e)
            self._futures_cache[trade_date] = _safe
            return _safe

    # ── New: Market Options PCR Context ────────────────────────────────────

    def fetch_options_context(self, trade_date: date) -> dict:
        """台指選擇權三大法人未平倉 Put/Call Ratio — 大盤情緒指標.

        Dataset: TaiwanOptionInstitutionalInvestors (TXO)
        Returns dict:
            foreign_call_net_oi — 外資買權淨OI
            foreign_put_net_oi  — 外資賣權淨OI
            pcr                 — put_net_oi / call_net_oi (None if call==0)
            dealer_put_net_oi   — 自營商賣權淨OI (dealers hedge → high = market concern)
            signal              — STRONG_BEARISH_HEDGE | BEARISH | NEUTRAL | BULLISH_UNWIND
            data_available
        """
        if trade_date in self._options_cache:
            return self._options_cache[trade_date]

        _safe = {
            "foreign_call_net_oi": 0, "foreign_put_net_oi": 0,
            "pcr": None, "dealer_put_net_oi": 0,
            "signal": "NEUTRAL", "data_available": False,
        }
        if not self._api_key:
            self._options_cache[trade_date] = _safe
            return _safe

        try:
            resp = requests.get(
                _FINMIND_URL,
                params={
                    "dataset": "TaiwanOptionInstitutionalInvestors",
                    "data_id": "TXO",
                    "start_date": trade_date.isoformat(),
                    "end_date": trade_date.isoformat(),
                    "token": self._api_key,
                },
                timeout=15,
            )
            resp.raise_for_status()
            rows = resp.json().get("data", [])
        except Exception as e:
            logger.debug("fetch_options_context(%s) API failed: %s", trade_date, e)
            self._options_cache[trade_date] = _safe
            return _safe

        if not rows:
            self._options_cache[trade_date] = _safe
            return _safe

        try:
            foreign_call_net = 0
            foreign_put_net = 0
            dealer_put_net = 0
            for row in rows:
                inst = str(row.get("institutional_investors", ""))
                cp = str(row.get("call_put", ""))
                long_oi = int(row.get("long_open_interest_balance_volume", 0) or 0)
                short_oi = int(row.get("short_open_interest_balance_volume", 0) or 0)
                net = long_oi - short_oi
                if inst == "外資":
                    if cp == "買權":
                        foreign_call_net += net
                    elif cp == "賣權":
                        foreign_put_net += net
                elif inst == "自營商" and cp == "賣權":
                    dealer_put_net += net

            pcr = (foreign_put_net / foreign_call_net) if foreign_call_net > 0 else None
            if pcr is None:
                signal = "NEUTRAL"
            elif pcr > 2.0:
                signal = "STRONG_BEARISH_HEDGE"
            elif pcr > 1.3:
                signal = "BEARISH"
            elif pcr < 0.8:
                signal = "BULLISH_UNWIND"
            else:
                signal = "NEUTRAL"

            result = {
                "foreign_call_net_oi": foreign_call_net,
                "foreign_put_net_oi": foreign_put_net,
                "pcr": pcr,
                "dealer_put_net_oi": dealer_put_net,
                "signal": signal,
                "data_available": True,
            }
            self._options_cache[trade_date] = result
            logger.info(
                "fetch_options_context(%s): 外資PCR=%.2f signal=%s",
                trade_date, pcr or 0, signal,
            )
            return result
        except Exception as e:
            logger.debug("fetch_options_context(%s) parse failed: %s", trade_date, e)
            self._options_cache[trade_date] = _safe
            return _safe

    # ── New: Per-Ticker Valuation (PER/PBR/Yield) ─────────────────────────

    def fetch_per_context(self, ticker: str, trade_date: date) -> dict:
        """個股本益比、股息殖利率 (TaiwanStockPER, daily per-ticker).

        Returns dict:
            per            — float P/E ratio (e.g. 31.8)
            pbr            — float P/B ratio (e.g. 10.4)
            dividend_yield — float % (e.g. 0.93 = 0.93%)
            data_available
        """
        key = (ticker, trade_date)
        if key in self._per_cache:
            return self._per_cache[key]

        _safe = {"per": None, "pbr": None, "dividend_yield": None, "data_available": False}
        rows = self._get_ticker("TaiwanStockPER", ticker, trade_date)
        if not rows:
            self._per_cache[key] = _safe
            return _safe

        try:
            # Use most recent row (may be today or latest available)
            row = sorted(rows, key=lambda r: r.get("date", ""))[-1]
            result = {
                "per": float(row["PER"]) if row.get("PER") is not None else None,
                "pbr": float(row["PBR"]) if row.get("PBR") is not None else None,
                "dividend_yield": float(row["dividend_yield"]) if row.get("dividend_yield") is not None else None,
                "data_available": True,
            }
            self._per_cache[key] = result
            return result
        except Exception as e:
            logger.debug("fetch_per_context(%s, %s) parse failed: %s", ticker, trade_date, e)
            self._per_cache[key] = _safe
            return _safe

    # ── New: Per-Ticker Revenue Momentum ──────────────────────────────────

    def fetch_revenue_context(self, ticker: str, trade_date: date) -> dict:
        """月營收 YoY 成長動能 (TaiwanStockMonthRevenue, ~14 months history).

        Returns dict:
            yoy_growth              — most recent month YoY % (positive = growing)
            consecutive_positive_yoy — count of consecutive months with positive YoY
            data_available
        """
        key = (ticker, trade_date)
        if key in self._revenue_cache:
            return self._revenue_cache[key]

        _safe = {"yoy_growth": None, "consecutive_positive_yoy": 0, "data_available": False}
        start = trade_date - timedelta(days=420)  # ~14 months back for YoY comparison
        rows = self._get_ticker("TaiwanStockMonthRevenue", ticker, trade_date, start_date=start)
        if len(rows) < 2:
            self._revenue_cache[key] = _safe
            return _safe

        try:
            # Sort by (year, month) ascending
            rows_sorted = sorted(rows, key=lambda r: (int(r.get("revenue_year", 0)), int(r.get("revenue_month", 0))))
            # Build {(year, month): revenue} map
            rev_map: dict[tuple[int, int], float] = {}
            for r in rows_sorted:
                y = int(r.get("revenue_year", 0))
                m = int(r.get("revenue_month", 0))
                v = float(r.get("revenue", 0) or 0)
                if y > 0 and m > 0:
                    rev_map[(y, m)] = v

            # Compute YoY for the most recent 3 months
            sorted_keys = sorted(rev_map.keys())
            recent_keys = sorted_keys[-3:] if len(sorted_keys) >= 3 else sorted_keys
            yoy_values: list[float] = []
            for (y, m) in recent_keys:
                prior_key = (y - 1, m)
                if prior_key in rev_map and rev_map[prior_key] > 0:
                    yoy = (rev_map[(y, m)] / rev_map[prior_key] - 1) * 100
                    yoy_values.append(yoy)

            if not yoy_values:
                self._revenue_cache[key] = _safe
                return _safe

            yoy_growth = yoy_values[-1]  # most recent
            # Count consecutive positive from most recent backwards
            consecutive = 0
            for v in reversed(yoy_values):
                if v > 0:
                    consecutive += 1
                else:
                    break

            result = {
                "yoy_growth": yoy_growth,
                "consecutive_positive_yoy": consecutive,
                "data_available": True,
            }
            self._revenue_cache[key] = result
            return result
        except Exception as e:
            logger.debug("fetch_revenue_context(%s, %s) parse failed: %s", ticker, trade_date, e)
            self._revenue_cache[key] = _safe
            return _safe
