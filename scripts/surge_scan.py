"""SurgeRadar scanner — aggressive fresh-ignition detection.

Usage:
    python scripts/surge_scan.py                          # 互動式產業選擇
    python scripts/surge_scan.py --sectors 1 4
    python scripts/surge_scan.py --tickers 2330 2454
    python scripts/surge_scan.py --date 2026-04-21
    python scripts/surge_scan.py --notify                 # Telegram
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from rich import box
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()

from taiwan_stock_agent.domain.models import DailyOHLCV
from taiwan_stock_agent.domain.surge_radar import SurgeRadar
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient

_console = Console()
_lock = Lock()
_gate0_stats: dict[str, int] = {"disposal": 0, "halt": 0, "limit_up": 0, "daytrade": 0}

_ROOT_PATH = Path(__file__).resolve().parents[1]
_SHARES_CACHE = _ROOT_PATH / "data" / "_shares_cache.json"


def _load_shares_map() -> dict[str, int]:
    """Load total shares outstanding for all listed/OTC stocks.

    Fetches from TWSE/TPEx bulk opendata endpoints and caches for 7 days.
    Returns {} on failure so turnover scoring degrades gracefully.
    """
    import json as _json
    from datetime import date as _date

    today = _date.today()
    if _SHARES_CACHE.exists():
        try:
            with open(_SHARES_CACHE, encoding="utf-8") as f:
                cached = _json.load(f)
            if (_date.today() - _date.fromisoformat(cached.get("date", "2000-01-01"))).days < 7:
                return cached.get("shares", {})
        except Exception:
            pass

    shares: dict[str, int] = {}
    # TWSE listed stocks
    try:
        r = requests.get(
            "https://openapi.twse.com.tw/v1/opendata/t187ap04_L", timeout=15
        )
        if r.ok:
            for rec in r.json():
                code = rec.get("公司代號", "").strip()
                raw = rec.get("上市股數", "0").replace(",", "")
                if code and raw.isdigit() and int(raw) > 0:
                    shares[code] = int(raw) * 1000   # 千股 → 股
    except Exception:
        pass
    # TPEx OTC stocks
    try:
        r = requests.get(
            "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O", timeout=15
        )
        if r.ok:
            for rec in r.json():
                code = rec.get("SecuritiesCompanyCode", "").strip()
                raw = rec.get("IssuedShares", "0").replace(",", "")
                if code and raw.isdigit() and int(raw) > 0:
                    shares[code] = int(raw) * 1000
    except Exception:
        pass

    if shares:
        _SHARES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(_SHARES_CACHE, "w", encoding="utf-8") as f:
            _json.dump({"date": str(today), "shares": shares}, f, ensure_ascii=False)

    return shares


# ── TWSE MIS real-time quote helpers ─────────────────────────────────────────

_MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
_MIS_BATCH = 20


def _fetch_realtime_batch(mis_keys: list[str]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for i in range(0, len(mis_keys), _MIS_BATCH):
        batch = mis_keys[i : i + _MIS_BATCH]
        ex_ch = "|".join(batch)
        try:
            resp = requests.get(
                _MIS_URL,
                params={"ex_ch": ex_ch, "json": "1", "delay": "0", "_": str(int(time.time() * 1000))},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
                verify=False,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue
        for item in data.get("msgArray", []):
            ticker = item.get("c", "")
            if not ticker:
                continue
            price: float | None = None
            price_source = ""
            for field, src in [("z", "last"), ("b", "bid"), ("o", "open")]:
                val = item.get(field, "-")
                if val not in ("-", ""):
                    try:
                        price = float(val.split("_")[0]) if src == "bid" else float(val)
                        price_source = src
                        break
                    except ValueError:
                        pass
            if price is None:
                h_str, l_str = item.get("h", "-"), item.get("l", "-")
                if h_str not in ("-", "") and l_str not in ("-", ""):
                    try:
                        price = (float(h_str) + float(l_str)) / 2
                        price_source = "hl_mid"
                    except ValueError:
                        pass
            if price is None:
                continue
            try:
                results[ticker] = {
                    "price": price,
                    "price_source": price_source,
                    "volume": int(item.get("v", "0").replace(",", "")),
                    "yesterday_close": float(item.get("y", "0")),
                    "timestamp": item.get("t", ""),
                    "name": item.get("n", ""),
                    "high": float(item["h"]) if item.get("h", "-") not in ("-", "") else None,
                    "low": float(item["l"]) if item.get("l", "-") not in ("-", "") else None,
                    "open": float(item["o"]) if item.get("o", "-") not in ("-", "") else None,
                }
            except (ValueError, TypeError):
                continue
        if i + _MIS_BATCH < len(mis_keys):
            time.sleep(0.3)
    return results


def _mis_fetch(tickers: list[str]) -> dict[str, dict]:
    """Fetch real-time quotes; retry missing tickers as OTC."""
    results = _fetch_realtime_batch([f"tse_{t}.tw" for t in tickers])
    missing = [t for t in tickers if t not in results]
    if missing:
        results.update(_fetch_realtime_batch([f"otc_{t}.tw" for t in missing]))
    return results


def _get_time_ratio() -> float:
    """Fraction of trading day elapsed (09:00–13:30 = 1.0)."""
    now = datetime.now()
    open_ = now.replace(hour=9, minute=0, second=0, microsecond=0)
    close_ = now.replace(hour=13, minute=30, second=0, microsecond=0)
    elapsed = (now - open_).total_seconds() / 60
    return max(0.0, min(1.0, elapsed / 270))

GRADE_COLOR = {
    "SURGE_ALPHA": "bold red",
    "SURGE_BETA": "bold yellow",
    "SURGE_GAMMA": "cyan",
}

GRADE_ZH = {
    "SURGE_ALPHA": "強噴★",
    "SURGE_BETA": "噴發",
    "SURGE_GAMMA": "量增",
}


def _load_history(
    ticker: str, analysis_date: date, finmind: FinMindClient
) -> list[DailyOHLCV] | None:
    """Fetch ~250 days history; return None if insufficient."""
    try:
        start = analysis_date - timedelta(days=380)
        df = finmind.fetch_ohlcv(ticker, start_date=start, end_date=analysis_date)
        if df is None or df.empty:
            return None
        history: list[DailyOHLCV] = []
        for _, row in df.iterrows():
            history.append(
                DailyOHLCV(
                    ticker=ticker,
                    trade_date=row["trade_date"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                )
            )
        history.sort(key=lambda x: x.trade_date)
        if len(history) < 25:
            return None
        return history
    except Exception:
        return None


def _build_intraday_bar(
    ticker: str, quote: dict, today: date, time_ratio: float
) -> DailyOHLCV | None:
    """Build a synthetic DailyOHLCV from an MIS real-time quote.

    MIS volume is in 張 (lots). Multiply by 1000 → shares, then divide by
    time_ratio to project to full-day volume.
    """
    price = quote.get("price")
    if not price or time_ratio <= 0:
        return None
    vol_lots = quote.get("volume") or 0
    projected_vol = int(vol_lots * 1000 / time_ratio)
    open_price = quote.get("open") or price
    high = quote.get("high") or price
    low = quote.get("low") or price
    return DailyOHLCV(
        ticker=ticker,
        trade_date=today,
        open=float(open_price),
        high=float(high),
        low=float(low),
        close=float(price),
        volume=projected_vol,
    )


def _fetch_intraday_quotes(tickers: list[str]) -> dict[str, dict]:
    """Batch-fetch current MIS quotes for all tickers (TSE → TPEx fallback)."""
    return _mis_fetch(tickers)


def _compute_industry_strength(
    per_ticker_today: dict[str, dict],
    industry_map: dict[str, str],
) -> dict[str, float]:
    """Aggregate per-industry strength score and convert to percentile rank per industry.

    Industry strength = mean(vol_ratio * max(day_chg_pct, 0)) across that industry's
    tickers today. Only counts up-days to avoid noise from declining stocks.

    Returns: {industry_name: percentile_rank (0-100)}
    """
    by_industry: dict[str, list[float]] = {}
    for ticker, payload in per_ticker_today.items():
        industry = industry_map.get(ticker)
        if not industry:
            continue
        vr = payload.get("vol_ratio", 0) or 0
        chg = payload.get("day_chg_pct", 0) or 0
        strength = vr * max(chg, 0)
        by_industry.setdefault(industry, []).append(strength)

    industry_score: dict[str, float] = {
        ind: sum(scores) / len(scores) for ind, scores in by_industry.items() if scores
    }
    if not industry_score:
        return {}

    sorted_inds = sorted(industry_score.items(), key=lambda kv: kv[1])
    n = len(sorted_inds)
    ranks: dict[str, float] = {}
    for rank, (ind, _) in enumerate(sorted_inds):
        # rank 0 is weakest → 0%. Last is strongest → 100%.
        ranks[ind] = round(rank / max(n - 1, 1) * 100, 1)
    return ranks


def _load_heat_lookup(
    heat_dir: Path,
    industry_map: dict[str, str],
) -> dict[str, dict]:
    """Load latest market heat + concept + intl snapshots → per-ticker heat context.

    Returns {} if no snapshots available (graceful fallback).
    """
    if not heat_dir.exists():
        return {}
    try:
        import json as _json

        # Industry heat (5d momentum)
        heat_files = sorted(heat_dir.glob("heat_*.json"))
        ind_heat: dict[str, dict] = {}
        if heat_files:
            with open(heat_files[-1], encoding="utf-8") as f:
                heat_data = _json.load(f)
            for ind, meta in heat_data.get("industries", {}).items():
                ind_heat[ind] = {
                    "rank_pct": meta.get("rank_pct", 0),
                    "accelerating": meta.get("acceleration_pct", 0) > 0.5,
                }

        # Concept heat
        concept_files = sorted(heat_dir.glob("concept_heat_*.json"))
        hot_concept_names: dict[str, str] = {}  # concept_key → name_zh
        concept_tickers: dict[str, list[str]] = {}  # concept_key → tickers
        if concept_files:
            with open(concept_files[-1], encoding="utf-8") as f:
                concept_data = _json.load(f)
            for ck, meta in concept_data.get("concepts", {}).items():
                if meta.get("rank_pct", 0) >= 70:
                    hot_concept_names[ck] = meta.get("name_zh", ck)
            # Load concept definitions for membership
            concepts_path = Path("config/concepts.json")
            if concepts_path.exists():
                with open(concepts_path, encoding="utf-8") as f:
                    concepts_def = _json.load(f)
                for ck, cdef in concepts_def.items():
                    concept_tickers[ck] = cdef.get("tickers", [])

        # International tailwinds
        intl_files = sorted(heat_dir.glob("intl_signals_*.json"))
        intl_ind: dict[str, int] = {}
        intl_concept: dict[str, int] = {}
        if intl_files:
            with open(intl_files[-1], encoding="utf-8") as f:
                intl_data = _json.load(f)
            tw = intl_data.get("tailwinds", {})
            intl_ind = tw.get("industry_tailwinds", {})
            intl_concept = tw.get("concept_tailwinds", {})

        if not ind_heat and not hot_concept_names:
            return {}

        # Build per-ticker lookup
        lookup: dict[str, dict] = {}
        for ticker, industry in industry_map.items():
            ih = ind_heat.get(industry, {})
            ticker_concepts = [ck for ck, tks in concept_tickers.items() if ticker in tks]
            hot = [ck for ck in ticker_concepts if ck in hot_concept_names]
            hot_labels = [hot_concept_names[ck] for ck in hot]

            intl = max(
                (intl_ind.get(industry, 0),
                 max((intl_concept.get(ck, 0) for ck in ticker_concepts), default=0))
            )

            lookup[ticker] = {
                "ind_5d_rank_pct": ih.get("rank_pct", 0),
                "accelerating": ih.get("accelerating", False),
                "hot_concepts": hot_labels,
                "intl_tailwind": max(0, intl),
            }
        return lookup
    except Exception:
        return {}


def _scan_one_surge(
    ticker: str,
    analysis_date: date,
    finmind: FinMindClient,
    chip_fetcher,
    market: str,
    taiex_history: list[DailyOHLCV],
    industry_rank_pct: float | None,
    intraday_bar: DailyOHLCV | None = None,
    heat_context: dict | None = None,
    intraday: bool = False,
    market_margin_rate: float | None = None,
) -> dict | None:
    """Full surge scoring for a single ticker."""
    try:
        if intraday_bar is not None:
            # Intraday mode: FinMind supplies prior history (up to yesterday),
            # MIS bar is today's ohlcv. Chip data uses most recent available day.
            history_end = analysis_date - timedelta(days=1)
            history = _load_history(ticker, history_end, finmind)
            if history is None or len(history) < 20:
                return None
            prior_history = history
            ohlcv = intraday_bar
            chip_date = history_end
        else:
            history = _load_history(ticker, analysis_date, finmind)
            if history is None:
                return None
            ohlcv = history[-1]
            prior_history = history[:-1]
            if len(prior_history) < 20:
                return None
            chip_date = analysis_date

        proxy = chip_fetcher.fetch(ticker, chip_date, today_volume=ohlcv.volume)

        # Gate 0: 處置股 / 暫停交易 → 直接跳過（所有模式）
        if proxy is not None and proxy.is_disposal:
            with _lock:
                _gate0_stats["disposal"] += 1
            return None
        if proxy is not None and proxy.is_trading_halt:
            with _lock:
                _gate0_stats["halt"] += 1
            return None

        # Gate 0 盤中額外過濾：漲停（無法成交）+ 當沖限制（強平假量）
        if intraday:
            if proxy is not None and proxy.is_limit_up:
                with _lock:
                    _gate0_stats["limit_up"] += 1
                return None
            if proxy is not None and proxy.is_daytrade_restricted:
                with _lock:
                    _gate0_stats["daytrade"] += 1
                return None

        # TAIEX regime
        taiex_closes = [b.close for b in sorted(taiex_history, key=lambda x: x.trade_date)]
        taiex_regime = "neutral"
        if len(taiex_closes) >= 63:
            ma20 = sum(taiex_closes[-20:]) / 20
            ma60 = sum(taiex_closes[-60:]) / 60
            if ma20 < ma60 * 0.98:
                taiex_regime = "downtrend"

        turnover_20ma = (
            sum(b.close * b.volume for b in prior_history[-20:]) / 20
            if len(prior_history) >= 20 else 0
        )

        eng = SurgeRadar(market=market)

        # 收盤模式：當沖限制 → volume ratio 門檻上調 20%（盤中已在 Gate 0 跳過）
        if not intraday and proxy is not None and proxy.is_daytrade_restricted:
            gates = eng._params.setdefault("gates", {})
            base = gates.get("vol_ratio_min", 1.5)
            gates["vol_ratio_min"] = base * 1.2

        # 大盤融資維持率注入 heat_context（SurgeRadar macro gate）
        if market_margin_rate is not None:
            heat_context = dict(heat_context) if heat_context else {}
            heat_context["margin_maintenance_rate"] = market_margin_rate

        result = eng.score_full(
            ohlcv=ohlcv,
            history=prior_history,
            proxy=proxy,
            taiex_regime=taiex_regime,
            taiex_history=taiex_history,
            turnover_20ma=turnover_20ma,
            industry_rank_pct=industry_rank_pct,
            heat_context=heat_context,
        )
        if result is None:
            return None

        result["ticker"] = ticker
        result["market"] = market
        result["analysis_date"] = analysis_date.isoformat()
        return result
    except Exception:
        return None


def _precompute_today_snapshot(
    tickers: list[str],
    analysis_date: date,
    finmind: FinMindClient,
    workers: int = 8,
    intraday_quotes: dict[str, dict] | None = None,
    time_ratio: float = 1.0,
) -> dict[str, dict]:
    """Pass 1: fetch today's bar + 20d avg vol for every ticker (for industry ranking).

    Returns: {ticker: {"vol_ratio": float, "day_chg_pct": float}}
    """
    snapshot: dict[str, dict] = {}

    def _one(ticker: str) -> tuple[str, dict] | None:
        if intraday_quotes and ticker in intraday_quotes:
            # Intraday: get 20-day avg from FinMind history (up to yesterday),
            # use MIS quote for today's vol/price.
            history_end = analysis_date - timedelta(days=1)
            history = _load_history(ticker, history_end, finmind)
            if history is None or len(history) < 20:
                return None
            vols = [b.volume for b in history[-20:]]
            vol_20ma = sum(vols) / len(vols) if vols else 0
            q = intraday_quotes[ticker]
            proj_vol = (q.get("volume", 0) * 1000 / time_ratio) if time_ratio > 0 else 0
            vol_ratio = proj_vol / vol_20ma if vol_20ma > 0 else 0
            prev_close = q.get("yesterday_close") or 0
            price = q.get("price") or 0
            day_chg_pct = (price / prev_close - 1) * 100 if prev_close > 0 else 0
            return ticker, {"vol_ratio": vol_ratio, "day_chg_pct": day_chg_pct}
        else:
            history = _load_history(ticker, analysis_date, finmind)
            if history is None or len(history) < 21:
                return None
            today_bar = history[-1]
            prior = history[:-1]
            vols = [b.volume for b in prior[-20:]]
            vol_20ma = sum(vols) / len(vols) if vols else 0
            vol_ratio = today_bar.volume / vol_20ma if vol_20ma > 0 else 0
            prev_close = prior[-1].close if prior else 0
            day_chg_pct = (today_bar.close / prev_close - 1) * 100 if prev_close > 0 else 0
            return ticker, {"vol_ratio": vol_ratio, "day_chg_pct": day_chg_pct}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"Pass 1 產業強度預掃 {len(tickers)} 檔...", total=len(tickers))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_one, t): t for t in tickers}
            for future in as_completed(futures):
                progress.advance(task)
                try:
                    result = future.result()
                    if result:
                        with _lock:
                            snapshot[result[0]] = result[1]
                except Exception:
                    pass
    return snapshot


def _print_score_health(scores: list[int], label: str = "信心分數分布") -> None:
    """Print P25/P50/P75/P95 and warn if top-quartile spread < 10 pts (clustering risk)."""
    if len(scores) < 5:
        return
    s = sorted(scores)
    n = len(s)
    def _p(pct): return s[min(n - 1, int(pct / 100 * n))]
    p25, p50, p75, p95 = _p(25), _p(50), _p(75), _p(95)
    spread = p95 - p75
    ok = spread >= 10
    color = "green" if ok else "red"
    status = "✅" if ok else "⚠ 頂端聚集"
    _console.print(
        f"  [dim]📊 {label}  "
        f"P25=[cyan]{p25}[/cyan]  P50=[cyan]{p50}[/cyan]  "
        f"P75=[cyan]{p75}[/cyan]  P95=[cyan]{p95}[/cyan]  │  "
        f"頂端壓縮 [{color}]{spread}pts {status}[/{color}][/dim]"
    )
    if not ok:
        _console.print(
            "  [yellow]  → P75–P95 差距 < 10pts，因子可能過度聚集，建議 make surge-factor 重新校準[/yellow]"
        )


def _print_surge_table(results: list[dict], scan_date: str, name_map: dict[str, str]) -> None:
    _console.rule(f"[bold red]噴發雷達 {scan_date}[/bold red]")
    if not results:
        _console.print("  [dim]無符合條件的噴發標的[/dim]")
        return
    tbl = Table(
        box=box.ROUNDED, show_header=True, header_style="bold red", border_style="dim",
    )
    tbl.add_column("排名",   justify="right")
    tbl.add_column("代號")
    tbl.add_column("名稱",   max_width=12)
    tbl.add_column("等級",   no_wrap=True)
    tbl.add_column("分數",   justify="right")
    tbl.add_column("量比",   justify="right")
    tbl.add_column("漲幅%",  justify="right")
    tbl.add_column("收位",   justify="right")
    tbl.add_column("跳空%",  justify="right")
    tbl.add_column("爆量日", justify="right")
    tbl.add_column("產業排名", justify="right")
    tbl.add_column("法人連買", justify="right")
    tbl.add_column("RSI",    justify="right")

    sorted_r = sorted(results, key=lambda x: x.get("score", 0), reverse=True)
    for i, r in enumerate(sorted_r, 1):
        grade = r.get("grade", "")
        style = GRADE_COLOR.get(grade, "white")
        ticker = r.get("ticker", "")
        name = name_map.get(ticker, ticker)[:8]
        ind_pct = r.get("industry_rank_pct")
        ind_str = f"{ind_pct:.0f}%" if ind_pct is not None else "--"
        rsi = r.get("rsi")
        rsi_str = f"{rsi:.0f}" if rsi is not None else "--"
        grade_zh = GRADE_ZH.get(grade, grade)
        tbl.add_row(
            str(i),
            f"[{style}]{ticker}[/{style}]",
            name,
            f"[{style}]{grade_zh}[/{style}]",
            str(r.get("score", 0)),
            f"{r.get('vol_ratio', 0):.2f}",
            f"{r.get('day_chg_pct', 0):+.2f}",
            f"{r.get('close_strength', 0):.2f}",
            f"{r.get('gap_pct', 0):+.1f}",
            str(r.get("surge_day", 0)),
            ind_str,
            str(r.get("inst_consec_days", 0)),
            rsi_str,
        )
    _console.print(tbl)


def _fetch_chart_candles(ticker: str, market: str) -> dict:
    """Fetch 3-month daily OHLCV + Bollinger Bands (20,2) via yfinance."""
    suffix = ".TW" if market == "TSE" else ".TWO"
    empty = {"candles": [], "bb_upper": [], "bb_mid": [], "bb_lower": []}
    try:
        import logging as _log
        import warnings as _warn
        import pandas as pd
        import yfinance as yf
        # Use Ticker.history() instead of yf.download() — each call creates an
        # independent session object, safe for concurrent use in ThreadPoolExecutor.
        period = 20
        _yfl = _log.getLogger("yfinance")
        _prev = _yfl.level
        _yfl.setLevel(_log.CRITICAL)
        try:
            with _warn.catch_warnings():
                _warn.simplefilter("ignore")
                hist = yf.Ticker(f"{ticker}{suffix}").history(
                    period="5mo", interval="1d", auto_adjust=True,
                )
        finally:
            _yfl.setLevel(_prev)
        rows = []
        last_nan_date = None
        for idx, row in hist.iterrows():
            try:
                o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
                if any(pd.isna(v) for v in [o, h, l, c]):
                    last_nan_date = idx.date()
                    continue
                rows.append({"time": str(idx.date()), "open": round(o, 2),
                             "high": round(h, 2), "low": round(l, 2), "close": round(c, 2)})
            except Exception:
                continue

        if last_nan_date is not None and (not rows or str(last_nan_date) > rows[-1]["time"]):
            try:
                from datetime import timedelta as _td
                single = yf.Ticker(f"{ticker}{suffix}").history(
                    start=str(last_nan_date),
                    end=str(last_nan_date + _td(days=1)),
                    auto_adjust=True,
                )
                if not single.empty:
                    for idx2, row2 in single.iterrows():
                        o2, h2, l2, c2 = float(row2["Open"]), float(row2["High"]), float(row2["Low"]), float(row2["Close"])
                        if not any(pd.isna(v) for v in [o2, h2, l2, c2]):
                            rows.append({"time": str(idx2.date()), "open": round(o2, 2),
                                         "high": round(h2, 2), "low": round(l2, 2), "close": round(c2, 2)})
            except Exception:
                pass

        if len(rows) < period:
            return empty
        # Bollinger Bands (period=20, multiplier=2) — computed on full history
        closes = [r["close"] for r in rows]
        bb_upper, bb_mid, bb_lower = [], [], []
        for i in range(period - 1, len(rows)):
            window = closes[i - period + 1 : i + 1]
            mean = sum(window) / period
            std = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
            t = rows[i]["time"]
            bb_upper.append({"time": t, "value": round(mean + 2 * std, 2)})
            bb_mid.append({"time": t, "value": round(mean, 2)})
            bb_lower.append({"time": t, "value": round(mean - 2 * std, 2)})
        # Trim candles to only bars that have BB (drop the warmup-only head)
        display_rows = rows[period - 1:]
        return {"candles": display_rows, "bb_upper": bb_upper, "bb_mid": bb_mid, "bb_lower": bb_lower}
    except Exception:
        return empty


_WEEKDAY_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

def _entry_day_str(scan_date_str: str, is_day2: bool) -> str:
    """Return human-readable T+2 entry date string, skipping weekends."""
    from datetime import date as _d, timedelta as _td
    try:
        d = _d.fromisoformat(scan_date_str)
        steps = 1 if is_day2 else 2  # DAY2: original surge was D-1, so T+2 = today+1
        while steps > 0:
            d += _td(days=1)
            if d.weekday() < 5:
                steps -= 1
        return f"{d.month}/{d.day}（{_WEEKDAY_ZH[d.weekday()]}）"
    except Exception:
        return "T+2"


def _buy_verdict(r: dict, scan_date: str = "") -> dict:
    """Derive buy / watch / avoid verdict from a surge result dict."""
    import re as _re

    raw_flags = r.get("flags", "")
    flags = "|".join(raw_flags) if isinstance(raw_flags, list) else (raw_flags or "")
    score = r.get("score", 0)
    vol   = r.get("vol_ratio", 0)
    cs    = r.get("close_strength", 0)

    rsi_m   = _re.search(r'RSI_(?:HEALTHY|WEAK|BREAKOUT):([\d.]+)', flags)
    rsi_val = float(rsi_m.group(1)) if rsi_m else None

    is_day1         = "SURGE_DAY1"       in flags
    is_day2         = "SURGE_DAY2"       in flags
    rsi_healthy     = "RSI_HEALTHY"      in flags
    rsi_weak        = "RSI_WEAK"         in flags
    rsi_overbought  = "RSI_BREAKOUT"     in flags
    margin_cool     = "MARGIN_COOL"      in flags
    margin_warm     = "MARGIN_WARM"      in flags
    margin_hot      = "MARGIN_HOT"       in flags
    has_pocket      = "POCKET_PIVOT"     in flags
    has_ma5_walk    = "MA5_WALK"         in flags
    has_ma5_break   = "MA5_BREAK"        in flags
    has_momentum    = "MOMENTUM_WALK"    in flags
    has_bb_squeeze  = "BB_SQUEEZE_BREAK" in flags
    has_intl        = "INTL_TAIL"        in flags
    has_ind_accel   = "IND_ACCEL"        in flags

    ind_hot_m  = _re.search(r'IND_HOT:(\d+)',  flags)
    ind_warm_m = _re.search(r'IND_WARM:(\d+)', flags)
    ind_cold_m = _re.search(r'IND_COLD:(\d+)', flags)
    ind_score  = int((ind_hot_m or ind_warm_m or ind_cold_m).group(1)) \
                 if (ind_hot_m or ind_warm_m or ind_cold_m) else 0
    is_cold = bool(ind_cold_m)
    is_hot  = bool(ind_hot_m)

    bb_m    = _re.search(r'BB_WIDE:([\d.]+)', flags)
    bb_w    = float(bb_m.group(1)) if bb_m else 0

    entry_day = _entry_day_str(scan_date, is_day2)

    pros: list[str] = []
    cons: list[str] = []

    # ── pros ──────────────────────────────────────────────
    if is_day1:
        pros.append("DAY1 首次噴發，進場時機最佳")
    if has_bb_squeeze:
        pros.append("BB 壓縮後突破，高品質型態")
    if has_pocket:
        pros.append("Pocket Pivot：法人出手確認")
    if rsi_healthy:
        rv = f"{rsi_val:.0f}" if rsi_val else "健康"
        pros.append(f"RSI {rv}，動能健康未過熱")
    if margin_cool:
        pros.append("融資水位低（<15%），籌碼乾淨")
    if is_hot and ind_score >= 80:
        pros.append(f"產業強勢（IND:{ind_score}），順風進場")
    if has_intl:
        pros.append("美股 AI / 半導體隔夜強，國際順風")
    if has_ma5_walk:
        pros.append("MA5 持續向上走（動能健康）")
    if has_momentum:
        pros.append("MOMENTUM_WALK：近期持續站 MA5 上方")
    if has_ind_accel:
        pros.append("產業動能加速（IND_ACCEL）")
    if vol >= 4.0:
        pros.append(f"量能爆發 {vol:.1f}x，主力積極進場")
    elif vol >= 2.5:
        pros.append(f"量能充足 {vol:.1f}x，突破可信")
    if cs >= 0.9:
        pros.append(f"收盤接近最高（{cs:.0%}），無賣壓")

    # ── cons ──────────────────────────────────────────────
    if is_day2:
        cons.append("DAY2 已連漲兩天，進場成本較高")
    if rsi_overbought:
        rv = f"{rsi_val:.0f}" if rsi_val else "70+"
        cons.append(f"RSI 過熱（{rv}），短線回調風險")
    if rsi_weak:
        cons.append("RSI 偏弱，價漲動能未跟上（背離疑慮）")
    if margin_hot:
        cons.append("融資高（>25%），強制賣壓風險大")
    elif margin_warm:
        cons.append("融資偏高（15-25%），注意砍壓")
    if has_ma5_break:
        cons.append("MA5_BREAK：均線結構破壞，趨勢轉弱")
    if is_cold:
        cons.append(f"產業偏冷（IND:{ind_score}），孤立突破風險")
    if vol < 2.0:
        cons.append(f"量能偏弱（{vol:.1f}x），突破可信度低")
    if cs < 0.65:
        cons.append(f"收盤偏低（{cs:.0%}），尾盤有賣壓")
    if bb_w > 0.5:
        cons.append(f"BB 嚴重擴張（{bb_w:.2f}），型態過度延伸")
    if is_day2 and rsi_val and rsi_val >= 67:
        cons.append(f"RSI {rsi_val:.0f} 接近70，DAY2 追高需謹慎")

    # ── verdict ───────────────────────────────────────────
    fatal        = margin_hot or rsi_overbought or has_ma5_break
    cold_weak    = is_cold and score < 72
    strong_buy   = score >= 80 and is_day1 and rsi_healthy and not margin_warm and not is_cold
    decent_buy   = score >= 70 and not fatal and not is_cold and len(cons) <= 1

    if fatal or (cold_weak and len(cons) >= 2):
        verdict, vcls = "不買", "vno"
        summary = "存在重大風險因子，不建議進場"
    elif strong_buy or decent_buy:
        verdict, vcls = "買", "vyes"
        summary = f"強訊號，建議 {entry_day} T+2 進場"
    else:
        verdict, vcls = "觀察", "vwatch"
        summary = f"信號有疑慮，{entry_day} 確認量能後再決定"

    return {"verdict": verdict, "vcls": vcls, "summary": summary, "pros": pros, "cons": cons}


def _parse_llm_verdict(llm_text: str) -> dict | None:
    """Parse structured LLM output into fields. Returns None if unstructured."""
    if not llm_text:
        return None
    fields: dict[str, str] = {}
    for line in llm_text.split("\n"):
        line = line.strip()
        for key, name in [
            ("【判決】", "verdict"), ("【技術解讀】", "analysis"),
            ("【進場】", "entry"),   ("【止損】", "stop"),
            ("【倉位】", "position"), ("【風險】", "risk"),
            ("【關鍵注意】", "note"),
        ]:
            if line.startswith(key):
                fields[name] = line[len(key):].strip()
                break
    if "verdict" not in fields:
        return None
    v = fields["verdict"]
    if "買進" in v:
        vcls, badge = "vyes", "買進"
    elif "不建議" in v or "不買" in v:
        vcls, badge = "vno", "不買"
    else:
        vcls, badge = "vwatch", "待觀察"
    fields["vcls"] = vcls
    fields["badge"] = badge
    return fields


def _generate_html_report(
    results: list[dict],
    scan_date: str,
    name_map: dict[str, str],
    html_path: Path,
    intraday: bool = False,
    industry_map: dict[str, str] | None = None,
) -> None:
    """Generate a dark-themed HTML report with per-stock links."""
    from html import escape as _esc

    sorted_r = sorted(results, key=lambda x: x.get("score", 0), reverse=True)
    alpha = sum(1 for r in sorted_r if r.get("grade") == "SURGE_ALPHA")
    beta  = sum(1 for r in sorted_r if r.get("grade") == "SURGE_BETA")
    gamma = sum(1 for r in sorted_r if r.get("grade") == "SURGE_GAMMA")
    mode  = "盤中即時" if intraday else "收盤掃描"

    _GRADE_CLASS = {"SURGE_ALPHA": "alpha", "SURGE_BETA": "beta", "SURGE_GAMMA": "gamma"}

    # Batch-fetch OHLCV for inline charts (yfinance, parallel)
    _console.print("  [dim]抓取線圖資料（yfinance）…[/dim]")
    pairs = [(r.get("ticker", ""), r.get("market", "TSE")) for r in sorted_r]
    chart_data: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as _pool:
        _chart_futures = {_pool.submit(_fetch_chart_candles, _t, _m): _t for _t, _m in pairs}
        for _fut in as_completed(_chart_futures):
            _ticker = _chart_futures[_fut]
            try:
                chart_data[_ticker] = _fut.result()
            except Exception:
                chart_data[_ticker] = {"candles": [], "bb_upper": [], "bb_mid": [], "bb_lower": []}
    ok = sum(1 for v in chart_data.values() if v.get("candles"))
    _console.print(f"  [dim]線圖資料：{ok}/{len(pairs)} 支取得[/dim]")


    _ind_map = industry_map or {}

    # Collect unique industries for dropdown
    unique_inds = sorted({
        (r.get("industry") or _ind_map.get(r.get("ticker", ""), "")).strip()
        for r in sorted_r
        if (r.get("industry") or _ind_map.get(r.get("ticker", ""), "")).strip()
    })

    _FLAG_MAP: list[tuple[str, str, str]] = [
        # (flag_prefix_or_exact, display, color_class)
        ("BB_SQUEEZE_BREAK", "BB突破", "f-blue"),
        ("POCKET_PIVOT", "Pocket Pivot", "f-blue"),
        ("BREAKOUT_20D", "20日突破", "f-blue"),
        ("INTL_TAIL", "美股順風", "f-green"),
        ("GROWTH_HIGH", "月收成長★", "f-green"),
        ("GROWTH_MID", "月收成長", "f-green"),
        ("MARGIN_DECLINING", "融資減少", "f-green"),
        ("IND_HEAT_HOT", "產業熱", "f-green"),
        ("IND_ACCEL", "產業加速", "f-green"),
        ("CONCEPT_HOT", "熱門概念", "f-purple"),
        ("COILING_PRIME", "強蓄積", "f-purple"),
        ("COILING", "蓄積", "f-purple"),
        ("EMERGING_SETUP", "蓄積中", "f-purple"),
        ("MA5_WALK", "MA5上升", "f-cyan"),
        ("BB_UPPER_COIL", "BB上軌貼行", "f-cyan"),
        ("MOMENTUM_WALK", "動能延伸", "f-cyan"),
        ("IND_HEAT_WARM", "產業溫", "f-cyan"),
        ("LIMIT_UP_CLOSE", "漲停收", "f-yellow"),
        ("DAYTRADE_RESTRICTED", "限當沖", "f-orange"),
        ("MARKET_MARGIN_STRESS", "融資壓力", "f-orange"),
        ("VOL_SURGE", "爆量>5x", "f-orange"),
        ("MARKET_MARGIN_CRISIS", "融資危機", "f-red"),
        ("TAIFEX_FUTURES_BEARISH", "期貨偏空", "f-red"),
        ("MA5_BREAK", "MA5破", "f-red"),
        ("MARGIN_HOT", "融資過熱", "f-red"),
        ("BB_UPPER_EXHAUSTION", "BB高位衰竭", "f-red"),
        ("ADX_EXHAUSTION", "動能衰竭", "f-red"),
    ]

    def _flag_badges(flag_list: list) -> str:
        seen: set[str] = set()
        parts: list[str] = []
        for flag in flag_list:
            flag_str = str(flag)
            for prefix, label, cls in _FLAG_MAP:
                if flag_str.startswith(prefix) and prefix not in seen:
                    seen.add(prefix)
                    parts.append(f'<span class="flag {cls}">{label}</span>')
                    break
        return "".join(parts)

    cards: list[str] = []

    for i, r in enumerate(sorted_r):
        ticker   = r.get("ticker", "")
        name     = _esc(r.get("name") or name_map.get(ticker, ticker))
        industry = _esc(r.get("industry") or _ind_map.get(ticker, ""))
        grade    = r.get("grade", "")
        grade_zh = GRADE_ZH.get(grade, grade)
        gcls     = _GRADE_CLASS.get(grade, "gamma")
        score    = r.get("score", 0)
        vol      = r.get("vol_ratio", 0)
        chg      = r.get("day_chg_pct", 0)
        rsi      = r.get("rsi")
        ind_pct  = r.get("industry_rank_pct")
        inst     = r.get("inst_consec_days", 0)
        market   = r.get("market", "TSE")
        exchange = "TWSE" if market == "TSE" else "TPEX"
        symbol   = f"{exchange}:{ticker}"
        tv_url   = f"https://www.tradingview.com/chart/?symbol={exchange}%3A{ticker}"
        gi_url   = f"https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={ticker}"

        chg_cls  = "pos" if chg > 0 else ("neg" if chg < 0 else "")
        vol_cls  = "pos" if vol >= 2 else ""
        rsi_s    = f"{rsi:.0f}" if rsi is not None else "--"
        ind_s    = f"{ind_pct:.0f}%" if ind_pct is not None else "--"
        delay    = f"{i * 0.05:.2f}"
        flags_list = r.get("flags") or []
        flag_html  = _flag_badges(flags_list) if flags_list else ""

        llm_text   = r.get("llm_analysis", "")
        llm_parsed = _parse_llm_verdict(llm_text)

        if llm_parsed:
            # ── AI-driven verdict ─────────────────────────────────────────
            analysis_html = ""
            if llm_parsed.get("analysis"):
                analysis_html = f'<div class="ai-analysis">{_esc(llm_parsed["analysis"])}</div>'
            trade_rows = ""
            for label, key, color in [
                ("進場", "entry",    "#58a6ff"),
                ("止損", "stop",     "#ffa198"),
                ("倉位", "position", "#e3b341"),
                ("風險", "risk",     "#8b949e"),
            ]:
                val = llm_parsed.get(key, "")
                if val:
                    trade_rows += (
                        f'<div class="aip-row">'
                        f'<span class="aip-label" style="color:{color}">{label}</span>'
                        f'<span class="aip-val">{_esc(val)}</span>'
                        f'</div>'
                    )
            note_html = ""
            if llm_parsed.get("note"):
                note_html = f'<div class="ai-note">⚑ {_esc(llm_parsed["note"])}</div>'
            verdict_html = f"""
      <div class="verdict {_esc(llm_parsed['vcls'])}">
        <div class="verdict-hd">
          <span class="vbadge">{_esc(llm_parsed['badge'])}</span>
          <span class="vsummary">{_esc(llm_parsed['verdict'])}</span>
        </div>
        {analysis_html}
        <div class="ai-plan">{trade_rows}</div>
        {note_html}
      </div>"""
            llm_html = ""
        else:
            # ── Deterministic fallback ────────────────────────────────────
            vd = _buy_verdict(r, scan_date=scan_date)
            pros_html = "".join(f'<li class="pro">{_esc(p)}</li>' for p in vd["pros"])
            cons_html = "".join(f'<li class="con">{_esc(c)}</li>' for c in vd["cons"])
            verdict_html = f"""
      <div class="verdict {_esc(vd['vcls'])}">
        <div class="verdict-hd">
          <span class="vbadge">{_esc(vd['verdict'])}</span>
          <span class="vsummary">{_esc(vd['summary'])}</span>
        </div>
        <ul class="vlist">{pros_html}{cons_html}</ul>
      </div>"""
            llm_html = ""

        rsi_data  = f"{rsi:.0f}" if rsi is not None else "0"
        ind_data  = f"{ind_pct:.0f}" if ind_pct is not None else "0"
        cards.append(f"""
    <div class="card" style="animation-delay:{delay}s"
         data-grade="{_esc(grade)}" data-industry="{industry}"
         data-ticker="{_esc(ticker)}" data-name="{name}"
         data-score="{score}" data-vol="{vol:.2f}" data-chg="{chg:.2f}"
         data-rsi="{rsi_data}" data-inst="{inst}">
      <div class="card-header">
        <div class="rank">{i+1}</div>
        <div class="info">
          <div class="ticker">{_esc(ticker)} <span class="tname">{name}</span></div>
          <div class="cname">{industry}</div>
        </div>
        <div class="badge g-{gcls}">{_esc(grade_zh)}</div>
      </div>
      <div class="metrics">
        <div class="m"><div class="mv">{score}</div><div class="ml">分數</div></div>
        <div class="m"><div class="mv {vol_cls}">{vol:.1f}x</div><div class="ml">量比</div></div>
        <div class="m"><div class="mv {chg_cls}">{chg:+.2f}%</div><div class="ml">漲幅</div></div>
        <div class="m"><div class="mv">{rsi_s}</div><div class="ml">RSI</div></div>
        <div class="m"><div class="mv">{ind_s}</div><div class="ml">產業排名</div></div>
        <div class="m"><div class="mv">{inst}</div><div class="ml">法人連買</div></div>
      </div>
      <div class="flags">{flag_html}</div>
      <div class="chart" data-ticker="{_esc(ticker)}"></div>{verdict_html}{llm_html}
      <div class="links">
        <a class="link-btn tv" href="{tv_url}" target="_blank" rel="noopener">TradingView</a>
        <a class="link-btn gi" href="{gi_url}" target="_blank" rel="noopener">Goodinfo</a>
      </div>
    </div>""")

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>噴發雷達 {_esc(scan_date)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh}}
.header{{background:linear-gradient(135deg,#1a1a2e,#16213e,#0f3460);padding:32px;border-bottom:1px solid #21262d}}
.header h1{{font-size:30px;font-weight:800;color:#ff6b6b;letter-spacing:-0.5px}}
.subtitle{{color:#8b949e;margin-top:6px;font-size:14px}}
.stats{{display:flex;gap:12px;margin-top:20px;flex-wrap:wrap}}
.stat{{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:12px 20px}}
.sv{{font-size:24px;font-weight:700}}.sl{{font-size:11px;color:#8b949e;margin-top:2px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:16px;padding:24px}}
.card{{background:#161b22;border:1px solid #21262d;border-radius:12px;overflow:hidden;
  transition:border-color .2s,transform .2s;animation:fadeIn .5s ease forwards;opacity:0}}
.card:hover{{border-color:#388bfd;transform:translateY(-3px);box-shadow:0 8px 24px rgba(0,0,0,.4)}}
@keyframes fadeIn{{to{{opacity:1}}}}
.card-header{{display:flex;align-items:center;gap:12px;padding:14px 16px;border-bottom:1px solid #21262d}}
.rank{{background:#21262d;border-radius:8px;width:34px;height:34px;display:flex;align-items:center;
  justify-content:center;font-weight:700;font-size:13px;color:#8b949e;flex-shrink:0}}
.info{{flex:1;min-width:0}}
.ticker{{font-size:16px;font-weight:700;letter-spacing:1px;display:flex;align-items:baseline;gap:6px}}
.tname{{font-size:15px;font-weight:600;color:#e6edf3}}
.cname{{font-size:11px;color:#8b949e;margin-top:2px}}
.badge{{padding:5px 12px;border-radius:20px;font-size:13px;font-weight:600;white-space:nowrap;flex-shrink:0}}
.g-alpha{{background:rgba(248,81,73,.15);color:#ff6b6b;border:1px solid rgba(248,81,73,.3)}}
.g-beta{{background:rgba(210,153,34,.15);color:#e3b341;border:1px solid rgba(210,153,34,.3)}}
.g-gamma{{background:rgba(56,189,248,.15);color:#38bdf8;border:1px solid rgba(56,189,248,.3)}}
.metrics{{display:flex;border-bottom:1px solid #21262d}}
.m{{flex:1;padding:10px 6px;text-align:center;border-right:1px solid #21262d}}
.m:last-child{{border-right:none}}
.mv{{font-size:13px;font-weight:600}}.ml{{font-size:10px;color:#8b949e;margin-top:2px}}
.pos{{color:#3fb950}}.neg{{color:#f85149}}
.chart{{height:240px;background:#0d1117;position:relative}}
.chart-ph{{display:flex;align-items:center;justify-content:center;height:100%;color:#484f58;font-size:12px}}
.links{{display:flex;gap:8px;padding:10px 16px;background:#0d1117;border-top:1px solid #21262d}}
.link-btn{{flex:1;display:block;text-align:center;padding:8px;border-radius:6px;font-size:12px;font-weight:600;
  text-decoration:none;transition:opacity .15s}}
.link-btn:hover{{opacity:.8}}
.tv{{background:#1565c0;color:#fff}}
.gi{{background:#1b4332;color:#3fb950;border:1px solid #236840}}
.footer{{text-align:center;padding:32px;color:#484f58;font-size:12px}}
.verdict{{padding:12px 16px;border-top:1px solid #21262d}}
.verdict-hd{{display:flex;align-items:center;gap:10px;margin-bottom:8px}}
.vbadge{{font-size:12px;font-weight:700;padding:3px 10px;border-radius:12px;flex-shrink:0}}
.vsummary{{font-size:11px;color:#8b949e;line-height:1.4}}
.vyes .vbadge{{background:rgba(239,83,80,.2);color:#ef5350;border:1px solid rgba(239,83,80,.4)}}
.vwatch .vbadge{{background:rgba(227,179,65,.2);color:#e3b341;border:1px solid rgba(227,179,65,.4)}}
.vno .vbadge{{background:rgba(139,148,158,.12);color:#8b949e;border:1px solid #30363d}}
.vlist{{list-style:none;display:flex;flex-direction:column;gap:4px}}
.vlist li{{font-size:11px;padding-left:14px;position:relative;line-height:1.5}}
.vlist li::before{{content:"";position:absolute;left:2px;top:6px;width:6px;height:6px;border-radius:50%}}
.pro{{color:#7ee787}}.pro::before{{background:#3fb950}}
.con{{color:#ffa198}}.con::before{{background:#f85149}}
.ai-plan{{display:flex;flex-direction:column;gap:5px;margin-top:6px}}
.aip-row{{display:flex;gap:8px;font-size:11px;line-height:1.55}}
.aip-label{{font-weight:700;min-width:26px;flex-shrink:0}}
.aip-val{{color:#c9d1d9}}
.ai-analysis{{font-size:11px;color:#c9d1d9;line-height:1.65;margin:8px 0 6px;padding:8px 10px;
  background:rgba(88,166,255,.06);border-radius:6px;border-left:2px solid #388bfd}}
.ai-note{{font-size:11px;color:#e3b341;line-height:1.5;margin-top:7px;
  padding:4px 8px;border-left:2px solid #e3b341;background:rgba(227,179,65,.05)}}
.flags{{display:flex;flex-wrap:wrap;gap:4px;padding:6px 16px;border-bottom:1px solid #21262d;min-height:28px}}
.flag{{font-size:10px;font-weight:600;padding:2px 7px;border-radius:10px}}
.f-green{{background:rgba(63,185,80,.13);color:#3fb950;border:1px solid rgba(63,185,80,.28)}}
.f-blue{{background:rgba(88,166,255,.13);color:#58a6ff;border:1px solid rgba(88,166,255,.28)}}
.f-cyan{{background:rgba(56,189,248,.1);color:#38bdf8;border:1px solid rgba(56,189,248,.28)}}
.f-purple{{background:rgba(163,113,247,.13);color:#a371f7;border:1px solid rgba(163,113,247,.28)}}
.f-yellow{{background:rgba(227,179,65,.13);color:#e3b341;border:1px solid rgba(227,179,65,.28)}}
.f-orange{{background:rgba(240,136,62,.13);color:#f0883e;border:1px solid rgba(240,136,62,.28)}}
.f-red{{background:rgba(248,81,73,.13);color:#f85149;border:1px solid rgba(248,81,73,.28)}}
.filterbar{{background:#161b22;border-bottom:1px solid #21262d;padding:10px 24px;
  display:flex;flex-wrap:wrap;gap:14px;align-items:center;position:sticky;top:0;z-index:100;
  box-shadow:0 2px 8px rgba(0,0,0,.4)}}
.fb-group{{display:flex;align-items:center;gap:6px;font-size:12px}}
.fb-label{{color:#8b949e;font-size:11px;white-space:nowrap}}
.fb-check{{display:flex;align-items:center;gap:4px;cursor:pointer;color:#e6edf3;font-size:12px}}
.fb-check input{{cursor:pointer;accent-color:#58a6ff}}
.fb-select,.fb-input{{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;
  padding:5px 8px;font-size:12px;cursor:pointer}}
.fb-input{{width:150px}}
.fb-select:focus,.fb-input:focus{{outline:none;border-color:#58a6ff}}
.fb-count{{margin-left:auto;color:#8b949e;font-size:12px;white-space:nowrap}}
</style>
</head>
<body>
<div class="header">
  <h1>🔥 噴發雷達</h1>
  <div class="subtitle">{_esc(scan_date)} &nbsp;·&nbsp; {_esc(mode)} &nbsp;·&nbsp; 共 {len(sorted_r)} 支</div>
  <div class="stats">
    <div class="stat"><div class="sv" style="color:#ff6b6b">{alpha}</div><div class="sl">強噴★</div></div>
    <div class="stat"><div class="sv" style="color:#e3b341">{beta}</div><div class="sl">噴發</div></div>
    <div class="stat"><div class="sv" style="color:#38bdf8">{gamma}</div><div class="sl">量增</div></div>
  </div>
</div>
<div class="filterbar">
  <div class="fb-group">
    <span class="fb-label">等級</span>
    <label class="fb-check"><input class="grade-check" type="checkbox" value="SURGE_ALPHA" checked> 強噴★</label>
    <label class="fb-check"><input class="grade-check" type="checkbox" value="SURGE_BETA" checked> 噴發</label>
    <label class="fb-check"><input class="grade-check" type="checkbox" value="SURGE_GAMMA" checked> 量增</label>
  </div>
  <div class="fb-group">
    <span class="fb-label">產業</span>
    <select class="fb-select" id="ind-filter">
      <option value="">全部</option>
      {"".join(f'<option value="{_esc(ind)}">{_esc(ind)}</option>' for ind in unique_inds)}
    </select>
  </div>
  <div class="fb-group">
    <input class="fb-input" id="search-filter" type="text" placeholder="搜尋 ticker / 股名">
  </div>
  <div class="fb-group">
    <span class="fb-label">排序</span>
    <select class="fb-select" id="sort-filter">
      <option value="score">分數</option>
      <option value="vol">量比</option>
      <option value="chg">漲幅</option>
      <option value="rsi">RSI</option>
      <option value="inst">法人連買</option>
    </select>
  </div>
  <span class="fb-count">顯示 <span id="visible-count">{len(sorted_r)}</span> / {len(sorted_r)} 支</span>
</div>
<div class="grid" id="card-grid">
{"".join(cards)}
</div>
<div class="footer">噴發雷達自動生成 · {_esc(scan_date)}</div>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
const CHART_DATA = {json.dumps(chart_data, ensure_ascii=False)};

const _obs = new IntersectionObserver(function(entries) {{
  entries.forEach(function(e) {{
    if (!e.isIntersecting || e.target.dataset.init) return;
    e.target.dataset.init = "1";
    _obs.unobserve(e.target);
    const ticker = e.target.dataset.ticker;
    const data = CHART_DATA[ticker];
    if (!data || !data.candles || data.candles.length === 0) {{
      e.target.innerHTML = '<div class="chart-ph">暫無資料</div>';
      return;
    }}
    const chart = LightweightCharts.createChart(e.target, {{
      autoSize: true,
      height: 240,
      layout: {{ background: {{ type: "solid", color: "#0d1117" }}, textColor: "#8b949e" }},
      grid: {{ vertLines: {{ color: "#21262d" }}, horzLines: {{ color: "#21262d" }} }},
      rightPriceScale: {{ borderColor: "#30363d" }},
      timeScale: {{ borderColor: "#30363d", timeVisible: false }},
      crosshair: {{ mode: 1 }},
    }});
    const cs = chart.addCandlestickSeries({{
      upColor: "#ef5350", downColor: "#26a69a",
      borderUpColor: "#ef5350", borderDownColor: "#26a69a",
      wickUpColor: "#ef5350", wickDownColor: "#26a69a",
    }});
    cs.setData(data.candles);
    const lineOpts = {{ lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }};
    const bbMid = chart.addLineSeries(Object.assign({{}}, lineOpts, {{ color: "#58a6ff" }}));
    bbMid.setData(data.bb_mid);
    const bbUp = chart.addLineSeries(Object.assign({{}}, lineOpts, {{ color: "#e3b341", lineStyle: 0 }}));
    bbUp.setData(data.bb_upper);
    const bbLo = chart.addLineSeries(Object.assign({{}}, lineOpts, {{ color: "#a371f7", lineStyle: 0 }}));
    bbLo.setData(data.bb_lower);
    chart.timeScale().fitContent();
  }});
}}, {{ rootMargin: "100px" }});

document.querySelectorAll(".chart[data-ticker]").forEach(function(el) {{ _obs.observe(el); }});

// ── Filter & Sort ──────────────────────────────────────────────────────────
var _grid = document.getElementById("card-grid");
var _totalCount = document.querySelectorAll(".card").length;

function applyAll() {{
  var checkedGrades = new Set(
    [].slice.call(document.querySelectorAll(".grade-check:checked")).map(function(el) {{ return el.value; }})
  );
  var selInd = document.getElementById("ind-filter").value;
  var search  = document.getElementById("search-filter").value.toLowerCase().trim();
  var sortKey = document.getElementById("sort-filter").value;

  var allCards = [].slice.call(document.querySelectorAll(".card"));

  // Sort all cards by selected key (descending)
  allCards.sort(function(a, b) {{
    return parseFloat(b.dataset[sortKey] || 0) - parseFloat(a.dataset[sortKey] || 0);
  }});
  allCards.forEach(function(c) {{ _grid.appendChild(c); }});

  // Apply filter visibility
  var visible = 0;
  allCards.forEach(function(card) {{
    var gradeOk  = checkedGrades.size === 0 || checkedGrades.has(card.dataset.grade);
    var indOk    = !selInd || card.dataset.industry === selInd;
    var haystack = (card.dataset.ticker + " " + card.dataset.name).toLowerCase();
    var searchOk = !search || haystack.indexOf(search) !== -1;
    var show = gradeOk && indOk && searchOk;
    card.style.display = show ? "" : "none";
    if (show) visible++;
  }});

  document.getElementById("visible-count").textContent = visible;
}}

document.querySelectorAll(".grade-check").forEach(function(el) {{ el.addEventListener("change", applyAll); }});
document.getElementById("ind-filter").addEventListener("change", applyAll);
document.getElementById("sort-filter").addEventListener("change", applyAll);
document.getElementById("search-filter").addEventListener("input", applyAll);
</script>
</body>
</html>"""

    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")


def _notify_surge_telegram(results: list[dict], scan_date: str) -> None:
    import urllib.request
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        rows = [r for r in results if r.get("grade", "") in ("SURGE_ALPHA", "SURGE_BETA")]
        if not rows:
            return
        lines = [f"噴發雷達 {scan_date}\n"]
        grade_text = {"SURGE_ALPHA": "ALPHA", "SURGE_BETA": "BETA"}
        for r in rows[:12]:
            grade = r.get("grade", "")
            lines.append(
                f"*{r.get('ticker', '')}* {r.get('name', '')}  "
                f"`{r.get('score', '--')}分` ({grade_text.get(grade, grade)})"
            )
            lines.append(
                f"   量比:{r.get('vol_ratio', '--')}x  "
                f"漲:{r.get('day_chg_pct', '--')}%  "
                f"收位:{r.get('close_strength', '--')}  "
                f"產業:{r.get('industry_rank_pct', '--')}%\n"
            )
        text = "\n".join(lines)
        payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        _console.print(f"  [dim red]TG surge notify error: {exc}[/dim red]")


def _run_llm_analysis(results: list[dict], llm_provider, scan_date: str = "") -> None:
    """Call LLM for every result and store 'llm_analysis' field in-place.

    第一檔失敗即中止，不浪費時間在其餘個股。
    """
    import re as _re2
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac

    _FACTOR_ZH = {
        "vol_ratio": "爆量比", "close_strength": "收強位", "inst_buy_fresh": "法人新買",
        "industry_strength": "產業排名", "pocket_pivot": "Pocket Pivot",
        "breakaway_gap": "跳空突破", "relative_strength": "相對強弱",
        "breakout_20d": "20日突破", "rsi_healthy": "RSI健康", "margin_not_hot": "融資清爽",
        "inst_synergy": "籌碼協同", "margin_declining": "融資減少",
        "inst_cumulative_flow": "法人累積", "ownership_concentration": "籌碼集中",
        "bb_squeeze": "BB壓縮突破", "ma5_walk": "MA5走勢", "bb_upper_walk": "BB上軌貼行",
        "market_heat": "市場熱度", "foreign_trend": "外資加速", "short_cover": "空頭回補",
        "large_2w_trend": "大戶趨勢", "inst_accel_short": "法人短期加速",
        "taifex_context": "期貨情境",
    }

    def _build_prompt(r: dict) -> str:
        ticker  = r.get("ticker", "")
        name    = r.get("name") or ticker
        raw     = r.get("flags", "")
        flags   = "|".join(raw) if isinstance(raw, list) else (raw or "")
        fset    = set(flags.split("|"))

        is_day2  = "SURGE_DAY2" in flags
        day      = f"DAY{r.get('surge_day', 2) if is_day2 else 1}（{'連續第'+str(r.get('surge_day',2))+'天' if is_day2 else '首次噴發'}）"
        entry    = _entry_day_str(scan_date, is_day2) if scan_date else "T+2"

        # ── 從 flags 抽出關鍵數值 ───────────────────────────────────────────
        rsi_m       = _re2.search(r'RSI_(\w+):([\d.]+)', flags)
        rsi_val     = rsi_m.group(2) if rsi_m else "--"
        rsi_state   = rsi_m.group(1) if rsi_m else ""
        bk_m        = _re2.search(r'BREAKOUT_20D:([\d.]+)>([\d.]+)', flags)
        breakout_lvl = bk_m.group(2) if bk_m else None   # 20日高點（突破位）
        rs_m        = _re2.search(r'RS:([+-][\d.]+%)', flags)
        rs_str      = rs_m.group(1) if rs_m else "--"
        gap_m       = _re2.search(r'GAP_(?:PARTIAL|FULL):([\d.]+%)', flags)
        gap_str     = gap_m.group(1) if gap_m else "0%"
        heat_m      = _re2.search(r'IND_HEAT_(HOT|WARM|COLD):(\d+)', flags)
        ind_desc    = f"產業熱度 {heat_m.group(1)}:{heat_m.group(2)}" if heat_m else ""
        margin      = ("MARGIN_HOT" if "MARGIN_HOT" in flags
                       else "MARGIN_WARM" if "MARGIN_WARM" in flags
                       else "MARGIN_COOL" if "MARGIN_COOL" in flags else "")

        close        = float(r.get("close_price") or 0)
        close_str    = f"{close:.1f}" if close else "未知"
        score        = r.get("score", 0)
        grade        = r.get("grade", "")
        inst_days    = r.get("inst_consec_days", 0)
        ind_rank     = r.get("industry_rank_pct")
        ind_rank_str = f"{ind_rank:.0f}%" if ind_rank is not None else "--"

        extras = []
        if "BB_SQUEEZE_BREAK" in flags: extras.append("BB 壓縮突破（高品質）")
        if "INTL_TAIL"        in flags: extras.append("美股半導體昨夜強，國際順風")
        if "POCKET_PIVOT"     in flags: extras.append("Pocket Pivot 量型")
        if "MA5_BREAK"        in flags: extras.append("⚠ MA5 結構破壞")
        if "RSI_BREAKOUT"     in flags: extras.append("⚠ RSI 過熱（>70）")
        if "MARGIN_HOT"       in flags: extras.append("⚠ 融資過高，追價賣壓風險")
        if "VOL_SURGE"        in flags: extras.append("⚠ 爆量 >5x，留意高峰賣壓")
        if "MARGIN_DECLINING" in flags: extras.append("融資減少（籌碼清洗正面）")

        stop_hint = (
            f"（突破位 {breakout_lvl} 為強支撐參考）" if breakout_lvl else ""
        )

        bd = r.get("score_breakdown") or {}
        top5 = sorted([(k, v) for k, v in bd.items() if v > 0], key=lambda x: -x[1])[:5]
        top5_str = " / ".join(f"{_FACTOR_ZH.get(k, k)} +{v}" for k, v in top5) if top5 else "無"

        return (
            f"你是台灣短線交易員，策略是噴發信號出現後 T+2 日（{entry}）進場。\n"
            "根據以下數據，給出明確具體的交易判斷，要有數字和邏輯，不要模稜兩可。\n\n"
            f"代號: {ticker} {name} | 產業: {r.get('industry','')} | 信號評分: {score}分（{grade}）\n"
            f"今日收盤: {close_str} | 漲幅: {r.get('day_chg_pct',0):.1f}% | 跳空: {gap_str}\n"
            f"量比: {r.get('vol_ratio',0):.1f}x | 收強: {r.get('close_strength',0):.2f} | "
            f"RSI: {rsi_val}（{rsi_state}）\n"
            f"相對強弱: {rs_str} vs 大盤 | 法人連買: {inst_days}天 | 產業排名: {ind_rank_str}\n"
            f"信號: {day} | {margin} | {ind_desc}\n"
            f"突破位: {breakout_lvl or '--'} | 特徵: {', '.join(extras) if extras else '無特殊項'}\n"
            f"主要得分因子: {top5_str}\n\n"
            "請嚴格按以下格式輸出 7 行，不要其他文字：\n"
            f"【判決】明天買進 / 待觀察 / 不建議（選一，括號補充最關鍵理由）\n"
            f"【技術解讀】2句話解釋為何這次信號可信（結合量比/法人/突破等具體數據，說出邏輯）\n"
            f"【進場】{entry} 開盤後站穩 XXX~XXX（根據收盤 {close_str} 給具體數字區間）\n"
            f"【止損】跌破 XXX 收盤停損{stop_hint}（給具體數字）\n"
            "【倉位】全倉 / 半倉 / 輕倉（選一，括號說明原因）\n"
            "【風險】最主要一個風險（要具體，有數據支撐，非泛泛而談）\n"
            "【關鍵注意】最重要一個操作細節（例如：需開盤前15分鐘站穩 / 需縮量確認 / 注意大盤同步等）\n"
        )

    if not results:
        return

    ticker_map = {r.get("ticker", ""): r for r in results}

    # ── 先跑第一檔做連線測試，失敗就全部跳過 ─────────────────────────────────
    first = results[0]
    first_ticker = first.get("ticker", "")
    _console.print(f"  [dim]🤖 LLM 分析（先測 {first_ticker}）…[/dim]")
    try:
        text = llm_provider.complete(_build_prompt(first), max_tokens=500).strip()
        ticker_map[first_ticker]["llm_analysis"] = text
        _console.print(f"  [dim]  ✓ 1/{len(results)}[/dim]")
    except Exception as e:
        _console.print(f"  [yellow]⚠ LLM 第一檔失敗，略過全部分析: {e}[/yellow]")
        return

    # ── 其餘並行 ──────────────────────────────────────────────────────────────
    remaining = results[1:]
    if not remaining:
        _console.print("  [green]✅ LLM 分析完成[/green]")
        return

    def _one(r: dict) -> tuple[str, str]:
        ticker = r.get("ticker", "")
        try:
            return ticker, llm_provider.complete(_build_prompt(r), max_tokens=500).strip()
        except Exception as e:
            return ticker, f"（LLM 分析失敗: {e}）"

    done = 1
    with _TPE(max_workers=4) as pool:
        futures = {pool.submit(_one, r): r for r in remaining}
        for fut in _ac(futures):
            ticker, text = fut.result()
            if ticker in ticker_map:
                ticker_map[ticker]["llm_analysis"] = text
            done += 1
            if done % 5 == 0 or done == len(results):
                _console.print(f"  [dim]  ✓ {done}/{len(results)}[/dim]")
    _console.print("  [green]✅ LLM 分析完成[/green]")


def run_surge_scan(
    tickers: list[str],
    analysis_date: date,
    workers: int = 8,
    market_map: dict[str, str] | None = None,
    name_map: dict[str, str] | None = None,
    industry_map: dict[str, str] | None = None,
    notify: bool = False,
    intraday: bool = False,
    no_html: bool = False,
    llm_provider=None,
) -> list[dict]:
    from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher
    from taiwan_stock_agent.infrastructure.paid_data_fetcher import PaidDataFetcher

    market_map = market_map or {}
    name_map = name_map or {}
    industry_map = industry_map or {}

    global _gate0_stats
    _gate0_stats = {"disposal": 0, "halt": 0, "limit_up": 0, "daytrade": 0}

    finmind = FinMindClient()
    paid_fetcher = PaidDataFetcher()
    chip_fetcher = ChipProxyFetcher(paid_fetcher=paid_fetcher)
    chip_fetcher.shares_map = _load_shares_map()

    # 大盤融資維持率（macro gate）
    market_margin_rate: float | None = None
    try:
        market_margin_rate = paid_fetcher.fetch_market_margin_maintenance(analysis_date)
        if market_margin_rate is not None:
            stress = ""
            if market_margin_rate < 120:
                stress = " [red]⚠ MARGIN_CRISIS[/red]"
            elif market_margin_rate < 130:
                stress = " [yellow]⚠ MARGIN_STRESS[/yellow]"
            _console.print(f"  [dim]大盤融資維持率：{market_margin_rate:.1f}%{stress}[/dim]")
    except Exception:
        pass

    # Shared TAIEX history
    try:
        taiex_df = finmind.fetch_taiex_history(analysis_date, lookback_days=130)
        taiex_history: list[DailyOHLCV] = []
        if taiex_df is not None and not taiex_df.empty:
            for _, row in taiex_df.iterrows():
                taiex_history.append(
                    DailyOHLCV(
                        ticker="TAIEX",
                        trade_date=row["trade_date"],
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(row.get("volume", 0)),
                    )
                )
            taiex_history.sort(key=lambda x: x.trade_date)
    except Exception:
        taiex_history = []

    # Intraday mode: batch-fetch current MIS quotes for all tickers.
    intraday_quotes: dict[str, dict] | None = None
    intraday_bars: dict[str, DailyOHLCV] = {}
    time_ratio = 1.0
    if intraday:
        time_ratio = _get_time_ratio()
        if time_ratio < 0.35:
            _console.print(
                f"  [yellow]⚠ 盤中時間比例 {time_ratio:.0%}（早盤量能外推誤差大，建議 10:30 後執行）[/yellow]"
            )
        _console.print(f"  [dim]盤中模式：抓取 {len(tickers)} 支即時報價…[/dim]")
        intraday_quotes = _fetch_intraday_quotes(tickers)
        for ticker, quote in intraday_quotes.items():
            bar = _build_intraday_bar(ticker, quote, analysis_date, time_ratio)
            if bar is not None:
                intraday_bars[ticker] = bar
        _console.print(f"  [dim]MIS 報價成功 {len(intraday_bars)}/{len(tickers)} 支[/dim]")

    # Pass 1: precompute today's snapshot for industry ranking
    snapshot = _precompute_today_snapshot(
        tickers, analysis_date, finmind, workers,
        intraday_quotes=intraday_quotes,
        time_ratio=time_ratio,
    )
    industry_ranks = _compute_industry_strength(snapshot, industry_map)

    # Load market heat context (pre-surge snapshot from previous close, if available)
    _heat_dir = Path(__file__).resolve().parents[1] / "data" / "market_heat"
    heat_lookup = _load_heat_lookup(_heat_dir, industry_map)
    if heat_lookup:
        _console.print(f"  [dim]市場熱度快照已載入（{len(heat_lookup)} 檔）[/dim]")

    # Pass 2: full surge scoring
    results: list[dict] = []
    scan_date = analysis_date.isoformat()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task(
            f"Pass 2 噴發掃描 {len(tickers)} 檔...", total=len(tickers)
        )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for ticker in tickers:
                ind = industry_map.get(ticker)
                ind_rank = industry_ranks.get(ind) if ind else None
                futures[
                    executor.submit(
                        _scan_one_surge,
                        ticker,
                        analysis_date,
                        finmind,
                        chip_fetcher,
                        market_map.get(ticker, "TSE"),
                        taiex_history,
                        ind_rank,
                        intraday_bars.get(ticker),  # None = use FinMind bar (normal mode)
                        heat_lookup.get(ticker),    # market heat context (optional)
                        intraday,
                        market_margin_rate,
                    )
                ] = ticker
            for future in as_completed(futures):
                progress.advance(task)
                try:
                    result = future.result()
                    if result:
                        with _lock:
                            results.append(result)
                except Exception:
                    pass

    # Growth bonus — load monthly revenue data and boost matching tickers
    try:
        from batch_plan import _apply_growth_bonus, _load_growth_index
        growth_index = _load_growth_index()
        n_growth = _apply_growth_bonus(results, growth_index)
        if n_growth:
            _console.print(f"  [dim]↑ 月營收成長加分: {n_growth} 檔 (GROWTH_HIGH +8 / MID +5 / LOW +3)[/dim]")
            results.sort(key=lambda x: x.get("score", 0), reverse=True)
    except Exception:
        pass

    # Gate 0 過濾統計
    total_g0 = sum(_gate0_stats.values())
    if total_g0:
        parts = []
        if _gate0_stats["disposal"]:
            parts.append(f"處置 {_gate0_stats['disposal']}")
        if _gate0_stats["halt"]:
            parts.append(f"暫停 {_gate0_stats['halt']}")
        if _gate0_stats["limit_up"]:
            parts.append(f"漲停 {_gate0_stats['limit_up']}")
        if _gate0_stats["daytrade"]:
            parts.append(f"當沖限制 {_gate0_stats['daytrade']}")
        _console.print(f"  [dim]Gate 0 過濾：{total_g0} 支（{' / '.join(parts)}）[/dim]")

    _print_surge_table(results, scan_date, name_map)
    _print_score_health(
        [int(r.get("score", 0)) for r in results],
        label="爆量信號分數分布",
    )

    # ── Write to DB ──────────────────────────────────────────────────────────
    if results and os.environ.get("DATABASE_URL"):
        try:
            from taiwan_stock_agent.infrastructure.db import init_pool
            from taiwan_stock_agent.infrastructure.surge_recorder import record_surge_signals
            init_pool()
            scan_date_obj = date.today() if intraday else analysis_date
            inserted = record_surge_signals(results, analysis_date, scan_date_obj)
            _console.print(f"  [dim]📋 surge_signals DB: {inserted} 筆[/dim]")
        except Exception as _e:
            _console.print(f"  [dim yellow]⚠ DB 寫入失敗，略過: {_e}[/dim yellow]")

    if llm_provider is not None and results:
        _run_llm_analysis(results, llm_provider, scan_date=scan_date)

    # ── HTML 報告 ─────────────────────────────────────────────────────────────
    scan_dir = Path(__file__).resolve().parents[1] / "data" / "scans"
    scan_dir.mkdir(parents=True, exist_ok=True)
    suffix = "live" if intraday else analysis_date.isoformat()
    html_path = scan_dir / f"surge_{suffix}.html"
    if no_html:
        _console.print(f"  [dim]📊 HTML 略過（--no-html）[/dim]")
    else:
        _generate_html_report(results, scan_date, name_map or {}, html_path, intraday=intraday, industry_map=industry_map)
        _console.print(f"  [green]📊 HTML 報告:[/green] file://{html_path.resolve()}")
        os.system(f'open "{html_path.resolve()}"')

    if notify:
        _notify_surge_telegram(results, scan_date)

    return results


def _raise_fd_limit() -> None:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(hard, 4096) if hard != resource.RLIM_INFINITY else 4096
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except Exception:
        pass


def main() -> None:
    _raise_fd_limit()
    from batch_plan import (
        _DEFAULT_SECTOR_NAMES,
        _build_industry_map,
        _build_market_map,
        _build_name_map,
        _build_sector_rows,
        _default_date,
        _sector_menu,
        _select_sectors,
    )

    parser = argparse.ArgumentParser(description="噴發雷達掃描（短線爆量捕捉）")
    parser.add_argument("--tickers", nargs="+", help="指定個股代號")
    parser.add_argument("--sectors", nargs="+", type=int, help="產業代號")
    parser.add_argument("--date", default=None, help="分析日期 YYYY-MM-DD")
    parser.add_argument("--notify", action="store_true", help="推播 Telegram")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--intraday", action="store_true", help="盤中即時模式（MIS 報價取代 FinMind 今日 bar）")
    parser.add_argument("--no-html", action="store_true", dest="no_html", help="不產生 HTML 報告也不自動開啟瀏覽器")
    parser.add_argument("--llm", action="store_true", help="對所有個股執行 LLM 評估並嵌入 HTML")
    parser.add_argument("--llm-model", default=None, help="指定 LLM provider: claude / openai / gemini（預設自動偵測）")
    args = parser.parse_args()

    if args.intraday:
        analysis_date = date.today()
    else:
        analysis_date = date.fromisoformat(args.date) if args.date else _default_date()

    industry_map = _build_industry_map()
    name_map = _build_name_map()
    market_map = _build_market_map()

    if args.tickers:
        tickers = args.tickers
    else:
        if not industry_map:
            _console.print("[yellow]找不到 industry_map，無法選擇產業[/yellow]")
            return

        industry_map_rows = _build_sector_rows(industry_map)
        idx_map = {i: name for i, name, _ in industry_map_rows}

        if args.sectors:
            chosen = {idx_map[n] for n in args.sectors if n in idx_map}
            if not chosen:
                _console.print("  [yellow]指定代號無效，使用預設產業[/yellow]")
                chosen = _DEFAULT_SECTOR_NAMES
        else:
            # Always use default sectors unless --sectors explicitly given.
            # Interactive menu is opt-in via --sectors flag only.
            chosen = _DEFAULT_SECTOR_NAMES
            if sys.stdin.isatty():
                _console.print(f"  [dim]使用預設產業（{len(chosen)} 個）；指定 --sectors <代號> 可覆蓋[/dim]")
            else:
                _console.print(f"  [dim]非互動模式，使用預設產業（{len(chosen)} 個）[/dim]")

        tickers = sorted(t for t, ind in industry_map.items() if ind in chosen)

    llm_provider = None
    if args.llm:
        from taiwan_stock_agent.domain.llm_provider import create_llm_provider
        llm_provider = create_llm_provider(args.llm_model)
        if llm_provider is None:
            _console.print("  [yellow]⚠ 找不到 LLM API Key，略過 LLM 分析[/yellow]")

    run_surge_scan(
        tickers=tickers,
        analysis_date=analysis_date,
        workers=args.workers,
        market_map=market_map,
        name_map=name_map,
        industry_map=industry_map,
        notify=args.notify,
        intraday=args.intraday,
        no_html=args.no_html,
        llm_provider=llm_provider,
    )

    # ── 訊號追蹤（盤後模式才執行）────────────────────────────────────────────
    if not args.intraday:
        _run_tracker(analysis_date, market_map, notify=args.notify)


def _run_tracker(scan_date: "date", market_map: dict, notify: bool = False) -> None:
    """D+0 存 watch → D+1 驗條件 → 印出（並選擇性推播）進場提醒。"""
    import sys as _sys
    _scripts = str(Path(__file__).parent)
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
    try:
        import surge_tracker
    except ImportError as e:
        _console.print(f"[yellow]surge_tracker import 失敗，略過追蹤: {e}[/yellow]")
        return

    # D+0：存今日 ALPHA watch
    n_watch = surge_tracker.save_watch(scan_date)
    if n_watch:
        _console.print(f"  [dim]📌 Watch 已儲存: {n_watch} 筆[/dim]")

    # D+1：驗昨日 watch
    from datetime import timedelta
    yesterday = scan_date - timedelta(days=1)
    confirmed = surge_tracker.check_d1(yesterday, market_map)
    if not confirmed:
        _console.print(f"  [dim]🔍 D+1 確認（{yesterday}）：無通過候選[/dim]")
        return

    # 印出確認清單
    from rich.table import Table
    t = Table(title=f"✅ T+2 進場候選（D+0={yesterday}）", show_header=True, box=None)
    for col in ["代號", "名稱", "D+0收", "D+1收", "漲跌%", "得分", "進場參考≤"]:
        t.add_column(col)
    for sig in confirmed:
        arrow = "📈" if sig["d1_chg_pct"] >= 0 else "📉"
        t.add_row(
            sig["ticker"], sig["name"],
            str(sig["close_d0"]), str(sig["close_d1"]),
            f"{arrow}{sig['d1_chg_pct']:+.1f}%",
            str(int(sig["score"])),
            str(sig["entry_hi"]),
        )
    _console.print(t)

    # 選擇性推播 Telegram
    if notify:
        msg = surge_tracker.format_d1_alert(confirmed, yesterday)
        _notify_text_telegram(msg)


def _notify_text_telegram(text: str) -> None:
    import urllib.request, urllib.parse
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10)
    except Exception as e:
        _console.print(f"  [yellow]Telegram 推播失敗: {e}[/yellow]")


if __name__ == "__main__":
    main()
