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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from threading import Lock

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

from trade import (  # noqa: E402
    _fetch_realtime_with_otc_fallback as _mis_fetch,
    _time_ratio as _get_time_ratio,
)

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

SURGE_CSV_FIELDS = [
    "scan_date", "analysis_date", "ticker", "name", "market", "industry",
    "grade", "score", "vol_ratio", "close_strength", "day_chg_pct",
    "gap_pct", "surge_day", "industry_rank_pct", "rsi", "inst_consec_days",
    "score_breakdown", "flags",
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


def _scan_one_surge(
    ticker: str,
    analysis_date: date,
    finmind: FinMindClient,
    chip_fetcher,
    market: str,
    taiex_history: list[DailyOHLCV],
    industry_rank_pct: float | None,
    intraday_bar: DailyOHLCV | None = None,
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
        # Fetch extra warmup bars so BB covers every displayed candle
        period = 20
        hist = yf.download(
            f"{ticker}{suffix}", period="5mo", interval="1d",
            progress=False, auto_adjust=True, multi_level_index=False,
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

    # Batch-fetch OHLCV for inline charts (yfinance, 8 workers)
    _console.print("  [dim]抓取線圖資料（yfinance）…[/dim]")
    pairs = [(r.get("ticker", ""), r.get("market", "TSE")) for r in sorted_r]
    chart_data: dict[str, list] = {}
    for _t, _m in pairs:
        chart_data[_t] = _fetch_chart_candles(_t, _m)
    ok = sum(1 for v in chart_data.values() if v)
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
      <div class="chart" data-ticker="{_esc(ticker)}"></div>
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
      upColor: "#3fb950", downColor: "#f85149",
      borderUpColor: "#3fb950", borderDownColor: "#f85149",
      wickUpColor: "#3fb950", wickDownColor: "#f85149",
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

    # Pass 2: full surge scoring
    results: list[dict] = []
    scan_date = date.today().isoformat()

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

    if csv_path and results:
        _save_surge_csv(results, scan_date, analysis_date, csv_path, name_map, industry_map)
        html_path = csv_path.with_suffix(".html")
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
    )


if __name__ == "__main__":
    main()
