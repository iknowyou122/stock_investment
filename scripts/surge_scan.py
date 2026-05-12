"""SurgeRadar scanner — aggressive fresh-ignition detection.

Usage:
    python scripts/surge_scan.py                          # 互動式產業選擇
    python scripts/surge_scan.py --sectors 1 4
    python scripts/surge_scan.py --tickers 2330 2454
    python scripts/surge_scan.py --save-csv
    python scripts/surge_scan.py --date 2026-04-21
    python scripts/surge_scan.py --notify                 # Telegram
"""
from __future__ import annotations

import argparse
import csv
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

try:
    from surge_db import insert_signals as _surge_db_insert
    _HAS_SURGE_DB = True
except ImportError:
    _HAS_SURGE_DB = False

_console = Console()
_lock = Lock()

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

SURGE_CSV_FIELDS = [
    "scan_date", "analysis_date", "ticker", "name", "market", "industry",
    "grade", "score", "vol_ratio", "close_strength", "day_chg_pct",
    "gap_pct", "surge_day", "industry_rank_pct", "rsi", "inst_consec_days",
    "close_price", "score_breakdown", "flags",
]

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

        proxy = chip_fetcher.fetch(ticker, chip_date)

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
        import pandas as pd
        import yfinance as yf
        # Use Ticker.history() instead of yf.download() — each call creates an
        # independent session object, safe for concurrent use in ThreadPoolExecutor.
        period = 20
        hist = yf.Ticker(f"{ticker}{suffix}").history(
            period="5mo", interval="1d", auto_adjust=True,
        )
        rows = []
        for idx, row in hist.iterrows():
            try:
                o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
                if any(pd.isna(v) for v in [o, h, l, c]):
                    continue
                rows.append({"time": str(idx.date()), "open": round(o, 2),
                             "high": round(h, 2), "low": round(l, 2), "close": round(c, 2)})
            except Exception:
                continue
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

        llm_text = r.get("llm_analysis", "")
        llm_html = f"""
      <div class="ai-box">
        <div class="ai-label">🤖 AI 評估</div>
        <div class="ai-text">{_esc(llm_text)}</div>
      </div>""" if llm_text else ""

        cards.append(f"""
    <div class="card" style="animation-delay:{delay}s">
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
.ai-box{{padding:10px 16px 14px;border-top:1px solid #21262d;background:rgba(56,139,248,.04)}}
.ai-label{{font-size:10px;color:#58a6ff;font-weight:700;letter-spacing:.5px;margin-bottom:5px}}
.ai-text{{font-size:12px;color:#c9d1d9;line-height:1.65}}
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
<div class="grid">
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
</script>
</body>
</html>"""

    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")


def _save_surge_csv(
    results: list[dict],
    scan_date: str,
    analysis_date: date,
    csv_path: Path,
    name_map: dict[str, str],
    industry_map: dict[str, str],
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SURGE_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in sorted(results, key=lambda x: x.get("score", 0), reverse=True):
            ticker = r.get("ticker", "")
            writer.writerow({
                "scan_date": scan_date,
                "analysis_date": analysis_date.isoformat(),
                "ticker": ticker,
                "name": name_map.get(ticker, ticker),
                "market": r.get("market", ""),
                "industry": industry_map.get(ticker, ""),
                "grade": r.get("grade", ""),
                "score": r.get("score", 0),
                "vol_ratio": r.get("vol_ratio", ""),
                "close_strength": r.get("close_strength", ""),
                "day_chg_pct": r.get("day_chg_pct", ""),
                "gap_pct": r.get("gap_pct", ""),
                "surge_day": r.get("surge_day", ""),
                "industry_rank_pct": r.get("industry_rank_pct", ""),
                "rsi": r.get("rsi", ""),
                "inst_consec_days": r.get("inst_consec_days", 0),
                "close_price": r.get("close_price", ""),
                "score_breakdown": json.dumps(r.get("score_breakdown", {})),
                "flags": "|".join(r.get("flags", [])),
            })
    _console.print(f"\n  [green]Surge CSV 已儲存:[/green] {csv_path}  ({len(results)} 筆)")


def _notify_surge_telegram(csv_path: Path, scan_date: str) -> None:
    import urllib.request
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        rows = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("grade", "") in ("SURGE_ALPHA", "SURGE_BETA"):
                    rows.append(row)
        if not rows:
            return

        lines = [f"噴發雷達 {scan_date}\n"]
        grade_text = {"SURGE_ALPHA": "ALPHA", "SURGE_BETA": "BETA"}
        for row in rows[:12]:
            grade = row.get("grade", "")
            lines.append(
                f"*{row.get('ticker', '')}* {row.get('name', '')}  "
                f"`{row.get('score', '--')}分` ({grade_text.get(grade, grade)})"
            )
            lines.append(
                f"   量比:{row.get('vol_ratio', '--')}x  "
                f"漲:{row.get('day_chg_pct', '--')}%  "
                f"收位:{row.get('close_strength', '--')}  "
                f"產業:{row.get('industry_rank_pct', '--')}%\n"
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
    """Call LLM for every result and store 'llm_analysis' field in-place."""
    import re as _re2
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac

    def _one(r: dict) -> tuple[str, str]:
        ticker  = r.get("ticker", "")
        name    = r.get("name") or ticker
        raw     = r.get("flags", "")
        flags   = "|".join(raw) if isinstance(raw, list) else (raw or "")
        fset    = set(flags.split("|"))

        is_day2  = "SURGE_DAY2" in fset
        day      = "DAY2（連續第二天）" if is_day2 else "DAY1（首次噴發）"
        entry    = _entry_day_str(scan_date, is_day2) if scan_date else "T+2"
        rsi_m    = _re2.search(r'RSI_(\w+):([\d.]+)', flags)
        rsi_desc = f"RSI {rsi_m.group(2)}（{rsi_m.group(1)}）" if rsi_m else ""
        margin   = next((f for f in ["MARGIN_HOT", "MARGIN_WARM", "MARGIN_COOL"] if f in fset), "")
        ind_m    = _re2.search(r'IND_(HOT|WARM|COLD):(\d+)', flags)
        ind_desc = f"產業熱度 {ind_m.group(1)}:{ind_m.group(2)}" if ind_m else ""

        extras = []
        if "BB_SQUEEZE_BREAK" in fset: extras.append("BB 壓縮突破（高品質型態）")
        if "INTL_TAIL"        in fset: extras.append("美股半導體昨夜強，國際順風")
        if "MA5_BREAK"        in fset: extras.append("⚠️ MA5 均線結構破壞")
        if "RSI_BREAKOUT"     in fset: extras.append("⚠️ RSI 已過熱")
        if "MARGIN_HOT"       in fset: extras.append("⚠️ 融資水位過高，強制賣壓風險")

        prompt = (
            f"你是台灣短線交易員，策略是噴發信號出現後 T+2 日（{entry}）進場。\n"
            "請根據以下資料，用繁體中文寫 2-3 句操作建議：明確說買或不買、"
            "指出最值得注意的一個風險或優勢、語氣像交易員告訴同事，不要廢話。\n\n"
            f"代號: {ticker} {name} | 產業: {r.get('industry','')}\n"
            f"今日: 漲 {r.get('day_chg_pct',0):.1f}%，量比 {r.get('vol_ratio',0):.1f}x，"
            f"收盤強度 {r.get('close_strength',0):.2f}\n"
            f"信號: {day} | {rsi_desc} | {margin} | {ind_desc}\n"
            f"特徵: {', '.join(extras) if extras else '無特殊加分/扣分項'}\n\n"
            "操作建議:"
        )
        try:
            return ticker, llm_provider.complete(prompt, max_tokens=120).strip()
        except Exception as e:
            return ticker, f"（LLM 分析失敗: {e}）"

    _console.print(f"  [dim]🤖 LLM 分析 {len(results)} 檔（parallel 4）…[/dim]")
    ticker_map = {r.get("ticker", ""): r for r in results}
    done = 0
    with _TPE(max_workers=4) as pool:
        futures = {pool.submit(_one, r): r for r in results}
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
    csv_path: Path | None = None,
    notify: bool = False,
    intraday: bool = False,
    no_html: bool = False,
    llm_provider=None,
) -> list[dict]:
    from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher

    market_map = market_map or {}
    name_map = name_map or {}
    industry_map = industry_map or {}

    finmind = FinMindClient()
    chip_fetcher = ChipProxyFetcher()

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

    _print_surge_table(results, scan_date, name_map)

    if _HAS_SURGE_DB and results:
        _db_rows = []
        for r in results:
            ticker = r.get("ticker", "")
            _db_rows.append({
                "signal_date": str(scan_date),
                "ticker": ticker,
                "grade": r.get("grade", ""),
                "score": r.get("score", 0),
                "vol_ratio": r.get("vol_ratio"),
                "day_chg_pct": r.get("day_chg_pct"),
                "gap_pct": r.get("gap_pct"),
                "close_strength": r.get("close_strength"),
                "rsi": r.get("rsi"),
                "inst_consec_days": r.get("inst_consec_days", 0),
                "industry_rank_pct": r.get("industry_rank_pct"),
                "close_price": r.get("close_price"),
                "market": r.get("market", "TSE"),
                "industry": industry_map.get(ticker, ""),
                "score_breakdown": json.dumps(r.get("score_breakdown") or {}),
            })
        inserted = _surge_db_insert(_db_rows)
        _console.print(f"  [dim]📋 surge_signals DB: {inserted} 筆新增[/dim]")

    if llm_provider is not None and results:
        _run_llm_analysis(results, llm_provider, scan_date=scan_date)

    if csv_path and results:
        _save_surge_csv(results, scan_date, analysis_date, csv_path, name_map, industry_map)
        html_path = csv_path.with_suffix(".html")
        if no_html:
            _console.print(f"  [dim]📊 HTML 略過（--no-html）: file://{html_path.resolve()}[/dim]")
        else:
            _generate_html_report(results, scan_date, name_map or {}, html_path, intraday=intraday, industry_map=industry_map)
            _console.print(f"  [green]📊 HTML 報告:[/green] file://{html_path.resolve()}")
            os.system(f'open "{html_path.resolve()}"')
        if notify:
            _notify_surge_telegram(csv_path, scan_date)

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
    parser.add_argument("--save-csv", action="store_true", default=True, help="儲存 CSV（預設開啟）")
    parser.add_argument("--no-save", action="store_true", help="不儲存 CSV")
    parser.add_argument("--notify", action="store_true", help="推播 Telegram")
    parser.add_argument("--only-notify", action="store_true", help="僅推播現有 CSV")
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

    scan_dir = Path(__file__).resolve().parents[1] / "data" / "scans"
    suffix = "live" if args.intraday else analysis_date.isoformat()
    csv_path = scan_dir / f"surge_{suffix}.csv"

    if args.only_notify:
        if csv_path.exists():
            _notify_surge_telegram(csv_path, analysis_date.isoformat())
            _console.print(f"  [green]已針對現有 CSV 執行推播:[/green] {csv_path}")
        else:
            _console.print(f"  [red]找不到 CSV 檔案，無法推播:[/red] {csv_path}")
        return

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
        elif not sys.stdin.isatty():
            # Non-interactive (e.g. make flow): use default sectors silently
            chosen = _DEFAULT_SECTOR_NAMES
            _console.print(f"  [dim]非互動模式，使用預設產業（{len(chosen)} 個）[/dim]")
        else:
            rows = _sector_menu(industry_map)
            chosen = _select_sectors(rows, _DEFAULT_SECTOR_NAMES)

        tickers = sorted(t for t, ind in industry_map.items() if ind in chosen)

    save_csv = args.save_csv and not args.no_save
    final_csv_path = csv_path if save_csv else None

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
        csv_path=final_csv_path,
        notify=args.notify,
        intraday=args.intraday,
        no_html=args.no_html,
        llm_provider=llm_provider,
    )

    # ── 訊號追蹤（盤後模式才執行）────────────────────────────────────────────
    if not args.intraday and final_csv_path:
        _run_tracker(final_csv_path, analysis_date, market_map, notify=args.notify)


def _run_tracker(csv_path: Path, scan_date: "date", market_map: dict, notify: bool = False) -> None:
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
    watch_path = surge_tracker.save_watch(csv_path, scan_date)
    if watch_path:
        _console.print(f"  [dim]📌 Watch 已儲存: {watch_path}[/dim]")

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
