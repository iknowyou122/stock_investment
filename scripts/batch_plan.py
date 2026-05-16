"""Batch scanner — runs StrategistAgent on multiple tickers and ranks by confidence.

Usage:
    python scripts/batch_plan.py                                    # 互動式選擇產業
    python scripts/batch_plan.py --sectors 1 4                      # 非互動：用產業代號
    python scripts/batch_plan.py --date 2026-03-25
    python scripts/batch_plan.py --tickers 2330 2454 2317 --date 2026-03-25
    python scripts/batch_plan.py --min-confidence 40
    python scripts/batch_plan.py --top 10 --date 2026-03-25
    python scripts/batch_plan.py --no-llm                           # 純 deterministic scoring
    python scripts/batch_plan.py --llm gemini --llm-top 5           # 非互動：Gemini，只對前5名
    python scripts/batch_plan.py --save-csv                         # 存到 data/scans/
    python scripts/batch_plan.py --save-csv --csv-path results.csv

Interactive (make scan):
    產業選單 → LLM 選單（provider + 前幾名）→ 自動兩階段執行
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from threading import Lock

from rich.console import Console
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, TaskProgressColumn
from rich.panel import Panel
from rich.text import Text
from rich.style import Style
from rich import print as rprint

_console = Console()
_progress_lock = Lock()

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.agents.strategist_agent import StrategistAgent
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher

logging.basicConfig(
    level=logging.WARNING,  # suppress INFO noise during batch run
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# 預設產業別（互動模式的 Enter 預設值：電子相關產業）
# -------------------------------------------------------------------
_DEFAULT_SECTOR_NAMES = {
    "半導體業",
    "電腦及週邊設備業",
    "電子零組件業",
    "通信網路業",
    "光電業",
    "電機機械",
    "其他電子業",
    "資訊服務業",
    "電子通路業",
    "玻璃陶瓷",
}

_ISIN_URLS = {
    "twse": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2",  # 上市
    "otc":  "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4",  # 上櫃
}

_CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "watchlist_cache"

_TREND_FIELDS = [
    "ma_alignment_pts",
    "ma20_slope_pts",
    "relative_strength_pts",
    "proximity_pts",
    "bb_compression_pts",
    "trend_continuity_pts",
    "dmi_initiation_pts",
]

_FALLBACK_TICKERS = [
    "2330", "2454", "2303", "2379", "3711", "2408", "2344",
    "2317", "2382", "2356", "2324", "6669", "3231", "2357", "2353", "2308",
    "2409", "3481",
]


def _fetch_isin_tickers(url: str) -> dict[str, tuple[str, str]]:
    """Parse TWSE/OTC ISIN page; return {ticker: (industry, name)} for ALL valid stocks."""
    import requests
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20, verify=False)
    resp.raise_for_status()
    html = resp.content.decode("big5", errors="replace")
    cells = re.findall(r"<td[^>]*>(.*?)</td>", html, re.DOTALL)
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]

    mapping: dict[str, tuple[str, str]] = {}
    for i in range(len(cells) - 6):
        industry = cells[i + 5]
        code_name = cells[i + 1]
        if industry and re.match(r"^\d{4}", code_name):
            code = code_name[:4]
            name = code_name[5:].strip()
            if "*" not in name and "DR" not in name:
                mapping[code] = (industry, name)
    return mapping


def _build_industry_map() -> dict[str, str]:
    """Load or fetch full ticker→industry map (ALL sectors), cached daily.

    Cache files: industry_map_YYYY-MM-DD.json + name_map_YYYY-MM-DD.json + market_map_YYYY-MM-DD.json
    Returns empty dict if fetch fails (caller handles fallback).
    """
    from collections import Counter
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _CACHE_DIR / f"industry_map_{date.today()}.json"

    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            if data:
                return data
        except Exception:
            pass

    _console.print("[dim]正在從 TWSE/OTC 抓取完整產業清單...[/dim]")
    all_raw: dict[str, tuple[str, str, str]] = {}  # {ticker: (industry, name, market)}
    for market_key, url in _ISIN_URLS.items():
        market_label = "TSE" if market_key == "twse" else "TPEx"
        try:
            m = _fetch_isin_tickers(url)
            _console.print(f"  [dim]{market_label}: {len(m)} 檔[/dim]")
            for ticker, (ind, name) in m.items():
                all_raw[ticker] = (ind, name, market_label)
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", market_key, e)

    if all_raw:
        all_map = {k: v[0] for k, v in all_raw.items()}
        all_names = {k: v[1] for k, v in all_raw.items()}
        all_markets = {k: v[2] for k, v in all_raw.items()}
        counts = Counter(all_map.values())
        total_sectors = len(counts)
        cache_file.write_text(json.dumps(all_map, ensure_ascii=False))
        
        name_cache = _CACHE_DIR / f"name_map_{date.today()}.json"
        name_cache.write_text(json.dumps(all_names, ensure_ascii=False))
        
        market_cache = _CACHE_DIR / f"market_map_{date.today()}.json"
        market_cache.write_text(json.dumps(all_markets, ensure_ascii=False))
        
        _console.print(f"  [dim]合計: {len(all_map)} 檔，{total_sectors} 個產業（已快取至 {cache_file.name}）[/dim]\n")
        return all_map

    logger.warning("TWSE/OTC fetch failed; using fallback watchlist")
    return {}


def _build_name_map() -> dict[str, str]:
    """Load ticker→company name map from daily cache."""
    name_cache = _CACHE_DIR / f"name_map_{date.today()}.json"
    if name_cache.exists():
        try:
            data = json.loads(name_cache.read_text())
            if data:
                return data
        except Exception:
            pass
    return {}


def _build_market_map() -> dict[str, str]:
    """Load ticker→market (TSE/TPEx) map from daily cache."""
    market_cache = _CACHE_DIR / f"market_map_{date.today()}.json"
    if market_cache.exists():
        try:
            data = json.loads(market_cache.read_text())
            if data:
                return data
        except Exception:
            pass
    return {}


def _build_sector_rows(industry_map: dict[str, str]) -> list[tuple[int, str, int]]:
    """Build numbered sector list without printing. Returns [(idx, industry_name, count), ...]."""
    from collections import Counter
    counts = Counter(industry_map.values())
    return [(i, ind, counts[ind]) for i, ind in enumerate(sorted(counts.keys()), start=1)]


def _sector_menu(industry_map: dict[str, str]) -> list[tuple[int, str, int]]:
    """Print numbered sector table. Returns [(idx, industry_name, count), ...]."""
    rows = _build_sector_rows(industry_map)

    table = Table(
        title="可用產業別",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        title_style="bold white",
        border_style="bright_black",
    )
    table.add_column("#", style="dim", justify="right", width=4)
    table.add_column("產業別", style="white", min_width=18)
    table.add_column("檔數", justify="right", style="green")

    for idx, ind, cnt in rows:
        bar = "█" * min(cnt // 10, 20)
        table.add_row(str(idx), ind, f"{cnt:>4}  [dim]{bar}[/dim]")

    _console.print()
    _console.print(table)
    return rows


def _select_sectors(
    rows: list[tuple[int, str, int]],
    default_names: set[str],
) -> set[str]:
    """Prompt user to pick sectors by number.
    'd' or Enter -> Default (Electronics sectors)
    'a' -> All sectors
    """
    all_names = {name for _, name, _ in rows}
    _console.print(f"\n[bold yellow]請輸入產業代號[/bold yellow] (空白分隔)")
    _console.print(f"  [cyan]'d'[/cyan] 或 [white]Enter[/white] : 預設電子產業 [dim]({len(default_names)} 個)[/dim]")
    _console.print(f"  [cyan]'a'[/cyan] : 掃描全市場 [dim]({len(all_names)} 個)[/dim]")

    raw = _console.input("[bold cyan]> [/bold cyan]").strip().lower()

    if not raw or raw == 'd':
        _console.print(f"  [green]→ 使用預設電子產業[/green]")
        return default_names
    if raw == 'a':
        _console.print(f"  [green]→ 掃描全市場[/green]")
        return all_names

    idx_map = {i: name for i, name, _ in rows}
    selected: set[str] = set()
    for token in raw.split():
        try:
            selected.add(idx_map[int(token)])
        except (ValueError, KeyError):
            _console.print(f"  [red]忽略無效代號: {token}[/red]")
    return selected or default_names


def _llm_menu() -> tuple:
    """互動式選擇 LLM provider 與前幾名篩選。回傳 (llm_provider, llm_top)。"""
    from taiwan_stock_agent.domain.llm_provider import create_llm_provider

    _PROVIDERS = [
        ("auto",   "自動偵測（依 API key）"),
        ("gemini", "Google Gemini"),
        ("claude", "Anthropic Claude"),
        ("openai", "OpenAI"),
        ("none",   "不使用 LLM（純 deterministic）"),
    ]

    table = Table(box=box.SIMPLE, show_header=False, border_style="bright_black")
    table.add_column("#", style="bold cyan", justify="right", width=3)
    table.add_column("LLM 引擎", style="white")
    for i, (_, label) in enumerate(_PROVIDERS, 1):
        table.add_row(str(i), label)
    _console.print()
    _console.print(Panel(table, title="[bold white]LLM 引擎選擇[/bold white]", border_style="cyan"))

    _console.print("\n[bold yellow]請輸入代號[/bold yellow]，直接 Enter 使用 [dim][1 自動偵測][/dim]")
    raw = _console.input("[bold cyan]> [/bold cyan]").strip()
    choice = int(raw) if raw.isdigit() and 1 <= int(raw) <= len(_PROVIDERS) else 1
    provider_key, _ = _PROVIDERS[choice - 1]

    if provider_key == "none":
        _console.print("  [dim]→ 純 deterministic 模式（不呼叫 LLM）[/dim]")
        return None, None

    llm_provider = create_llm_provider(None if provider_key == "auto" else provider_key)
    if llm_provider is None:
        _console.print("  [yellow]⚠ 找不到對應 API key，LLM 停用[/yellow]")
        return None, None

    _console.print(f"  [green]→ {llm_provider.name}[/green]\n")

    return llm_provider, None


class _EmptyLabelRepo:
    def get(self, _): return None
    def upsert(self, _): pass
    def list_all(self): return []


# ---------------------------------------------------------------------------
# Post-processing: sector relative ranking + signal persistence
# ---------------------------------------------------------------------------

def _apply_sector_ranks(results: list[dict], industry_map: dict[str, str]) -> int:
    """Boost stocks by sector rank tier (top 5%→+10, top 10%→+7, top 20%→+5).

    Only applied when a sector has ≥ 3 valid (non-halt) results.
    Adds SECTOR_RANK:N/M flag to boosted stocks.
    Returns count of stocks boosted.
    """
    from collections import defaultdict

    sector_valid: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        if r["halt"] or r["error"] is not None:
            continue
        sector = industry_map.get(r["ticker"], "")
        if sector:
            sector_valid[sector].append(r)

    boosted = 0
    for sector, rs in sector_valid.items():
        if len(rs) < 3:
            continue
        sorted_rs = sorted(rs, key=lambda r: (-r["confidence"], r["ticker"]))
        total = len(sorted_rs)
        top_5pct  = max(1, total // 20)
        top_10pct = max(1, total // 10)
        top_20pct = max(1, total // 5)
        for rank, r in enumerate(sorted_rs[:top_20pct], 1):
            if rank <= top_5pct:
                bonus = 10
            elif rank <= top_10pct:
                bonus = 7
            else:
                bonus = 5
            r["confidence"] = min(100, r["confidence"] + bonus)
            r["flags"] = list(r.get("flags") or []) + [f"SECTOR_RANK:{rank}/{total}"]
            boosted += 1

    return boosted


def _apply_catalyst_filter(
    results: list[dict],
    industry_map: dict[str, str],
    industry_strength: dict[str, float],
) -> int:
    """Mark WATCH stocks with NO_CATALYST when they lack both institutional continuity
    and sector momentum.

    LONG stocks are exempt (high confidence implies catalysts already present).
    Returns count of stocks marked NO_CATALYST.
    """
    if not industry_strength:
        return 0
    strength_vals = sorted(industry_strength.values())
    median_strength = strength_vals[len(strength_vals) // 2] if strength_vals else 0.0

    n = 0
    for r in results:
        if r.get("halt") or r.get("error"):
            continue
        if r["action"] in ("LONG", "CAUTION"):
            continue
        # WATCH stock: require at least one catalyst
        has_inst = r.get("institution_continuity_pts", 0) >= 3
        ind = industry_map.get(r["ticker"], "")
        has_hot_sector = industry_strength.get(ind, 0.0) >= median_strength
        if not has_inst and not has_hot_sector:
            flags = list(r.get("flags") or [])
            flags.append("NO_CATALYST")
            r["flags"] = flags
            n += 1
    return n


def _load_recent_csvs(
    analysis_date: date,
    data_dir: Path,
    lookback: int = 3,
    min_conf: int = 40,
) -> list[dict[str, int]]:
    """Load the last N trading days' scan CSVs as [{ticker: confidence}, ...].

    Returns list ordered old→new (index 0 = oldest, index -1 = most recent).
    Only includes tickers with confidence ≥ min_conf.
    """
    csvs: list[dict[str, int]] = []
    candidate = analysis_date - timedelta(days=1)
    days_checked = 0

    while len(csvs) < lookback and days_checked < 10:
        if candidate.weekday() < 5:
            csv_path = data_dir / f"scan_{candidate}.csv"
            if csv_path.exists():
                try:
                    scores: dict[str, int] = {}
                    with csv_path.open(encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            try:
                                conf = int(row.get("confidence", 0))
                                if conf >= min_conf:
                                    scores[row["ticker"]] = conf
                            except (ValueError, KeyError):
                                continue
                    csvs.append(scores)
                except Exception:
                    pass
        candidate -= timedelta(days=1)
        days_checked += 1

    csvs.reverse()  # old → new
    return csvs


def _apply_persistence_bonus(
    results: list[dict],
    analysis_date: date,
    data_dir: Path,
    min_prev_conf: int = 50,
) -> int:
    """Trajectory-aware persistence bonus.

    Reads the last 3 trading days' CSVs and computes per-ticker score trajectory:
      RISING   (3 consecutive days, each score higher than previous) → +7 pts
      STABLE   (appeared yesterday with score ≥ min_prev_conf)      → +5 pts
      DECLINING (appeared yesterday but score dropped > 5 pts)       → +0 pts

    Adds PERSIST_RISING / PERSIST_STABLE flag to boosted stocks.
    Returns count of stocks boosted.
    """
    recent = _load_recent_csvs(analysis_date, data_dir, lookback=3, min_conf=40)
    if not recent:
        return 0

    # Build trajectory: for each ticker, collect [score_d-3, score_d-2, score_d-1]
    all_tickers: set[str] = set()
    for day_scores in recent:
        all_tickers.update(day_scores.keys())

    trajectories: dict[str, list[int | None]] = {}
    for ticker in all_tickers:
        traj = [day_scores.get(ticker) for day_scores in recent]
        trajectories[ticker] = traj

    boosted = 0
    for r in results:
        ticker = r["ticker"]
        if r["halt"] or r["error"] is not None:
            continue
        if ticker not in trajectories:
            continue

        traj = trajectories[ticker]
        yesterday = traj[-1] if traj else None

        if yesterday is None or yesterday < min_prev_conf:
            continue

        # Classify trajectory
        # RISING: 3 consecutive appearances with monotonically increasing scores
        non_none = [(i, s) for i, s in enumerate(traj) if s is not None]
        is_rising = (
            len(non_none) >= 3
            and all(non_none[i + 1][1] > non_none[i][1] for i in range(len(non_none) - 1))
        )

        # DECLINING: appeared yesterday but score dropped > 5 from previous appearance
        prev_appearances = [s for s in traj[:-1] if s is not None]
        is_declining = (
            bool(prev_appearances)
            and yesterday < prev_appearances[-1] - 5
        )

        if is_rising:
            bonus = 7
            flag = f"PERSIST_RISING:{','.join(str(s) for s in traj if s is not None)}"
        elif is_declining:
            bonus = 0
            # No flag, no bonus — silently skip declining stocks
            continue
        else:
            bonus = 5
            flag = f"PERSIST_STABLE:{yesterday}"

        r["confidence"] = min(100, r["confidence"] + bonus)
        r["flags"] = list(r.get("flags") or []) + [flag]
        boosted += 1

    return boosted


def _apply_near_high_first_day(
    results: list[dict],
    analysis_date: date,
    data_dir: Path,
) -> int:
    """Give +4 pts to stocks in the 92-99% zone (proximity_pts=12) on their first scan day.

    Compensates for the missing day-1 persist bonus on strong pre-breakout setups.
    Only activates when the ticker was absent from yesterday's CSV.
    Called after _apply_persistence_bonus so there is no double-count.
    Returns count of stocks boosted.
    """
    recent = _load_recent_csvs(analysis_date, data_dir, lookback=1, min_conf=40)
    yesterday_tickers: set[str] = set(recent[0].keys()) if recent else set()

    boosted = 0
    for r in results:
        if r.get("halt") or r.get("error") is not None:
            continue
        if r["ticker"] in yesterday_tickers:
            continue
        if r.get("proximity_pts", 0) == 12:  # 12 = max proximity band (92-99% of 20d high); see _proximity_score()
            r["confidence"] = min(100, r["confidence"] + 4)
            r["flags"] = list(r.get("flags") or []) + ["NEAR_HIGH_COIL"]
            boosted += 1

    return boosted


def _make_label_repo():
    """Try to connect to PostgreSQL BrokerLabelRepository.

    Falls back to _EmptyLabelRepo (silent, no crash) when:
    - DATABASE_URL is not set
    - DB is unreachable
    - broker_labels table is empty (first run before build-labels)

    Run `make build-labels` to populate the table for full Pillar 2A scoring.
    """
    import os
    if not os.environ.get("DATABASE_URL"):
        return _EmptyLabelRepo()
    try:
        from taiwan_stock_agent.infrastructure.db import init_pool
        from taiwan_stock_agent.domain.broker_label_classifier import PostgresBrokerLabelRepository
        init_pool()
        repo = PostgresBrokerLabelRepository(None)
        count = len(repo.list_all())
        if count == 0:
            _console.print(
                "  [dim yellow]⚠ broker_labels 表為空 — Pillar 2A (隔日沖過濾) 停用。"
                "執行 [bold]make build-labels[/bold] 建立分類資料。[/dim yellow]"
            )
            return _EmptyLabelRepo()
        _console.print(f"  [dim green]✓ BrokerLabelRepository: {count} 筆分點標籤已載入[/dim green]")
        return repo
    except Exception as e:
        logger.debug("BrokerLabelRepository unavailable (%s); using empty repo", e)
        return _EmptyLabelRepo()


def _default_date() -> date:
    from datetime import datetime
    from taiwan_stock_agent.utils.trading_calendar import is_trading_day
    now = datetime.now()
    # 17:00 前用前一交易日；之後用今天（收盤資料已回傳）
    candidate = date.today() if now.hour >= 17 else date.today() - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _make_agent(llm_provider=None, no_llm: bool = False, label_repo=None,
                finmind: "FinMindClient | None" = None,
                chip_fetcher: "ChipProxyFetcher | None" = None) -> StrategistAgent:
    """Create an agent, optionally reusing shared client instances.

    When finmind/chip_fetcher are provided, the agent shares their in-memory
    caches (OHLCV superset, T86/Margin/SBL/DayTrade date caches) across all
    tickers — dramatically reducing API calls in batch scans.
    """
    agent = StrategistAgent(
        finmind or FinMindClient(),
        label_repo or _EmptyLabelRepo(),
        chip_proxy_fetcher=chip_fetcher or ChipProxyFetcher(),
        llm_provider=llm_provider,
    )
    if no_llm:
        agent._llm_provider = None
    return agent


def _scan_one(ticker: str, analysis_date: date, agent: StrategistAgent, market: str = "TSE") -> dict:
    """Run pipeline for one ticker using a shared agent; return result dict.

    The returned dict includes a '_signal' key with the raw SignalOutput object
    (None on error or halt) so that run_batch can optionally record it to DB.
    """
    t0 = time.time()
    try:
        signal = agent.run(ticker, analysis_date, market=market)
        elapsed = time.time() - t0
        breakdown_pts = {}
        if signal.score_breakdown:
            breakdown_pts = signal.score_breakdown.get("pts", {})
        trend_score = sum(breakdown_pts.get(f, 0) for f in _TREND_FIELDS)
        return {
            "ticker": ticker,
            "action": signal.action,
            "confidence": signal.confidence,
            "halt": signal.halt_flag,
            "free_tier": signal.free_tier_mode,
            "flags": signal.data_quality_flags,
            "entry_bid": signal.execution_plan.entry_bid_limit,
            "stop_loss": signal.execution_plan.stop_loss,
            "target": signal.execution_plan.target,
            "verdict": signal.reasoning.verdict if signal.reasoning else "",
            "position": signal.reasoning.position if signal.reasoning else "",
            "momentum": signal.reasoning.momentum if signal.reasoning else "",
            "chip": signal.reasoning.chip_analysis if signal.reasoning else "",
            "risk": signal.reasoning.risk_factors if signal.reasoning else "",
            "elapsed": elapsed,
            "error": None,
            "_signal": signal,
            "trend_score": trend_score,
            "institution_continuity_pts": breakdown_pts.get("institution_continuity_pts", 0),
            "proximity_pts": breakdown_pts.get("proximity_pts", 0),
        }
    except Exception as e:
        return {
            "ticker": ticker,
            "action": "ERROR",
            "confidence": -1,
            "halt": True,
            "free_tier": None,
            "flags": [],
            "entry_bid": 0.0,
            "stop_loss": 0.0,
            "target": 0.0,
            "verdict": "",
            "position": "",
            "momentum": "",
            "chip": "",
            "risk": "",
            "elapsed": time.time() - t0,
            "error": str(e),
            "_signal": None,
            "trend_score": 0,
            "proximity_pts": 0,
        }


CSV_FIELDS = [
    "scan_date", "analysis_date", "ticker", "action", "confidence", "trend_score",
    "free_tier", "halt", "entry_bid", "stop_loss", "target",
    "momentum", "chip_analysis", "risk_factors", "data_quality_flags",
]


def _save_csv(results: list[dict], analysis_date: date, csv_path: Path, sort_by: str = "trend") -> None:
    """Write scan results to a CSV file sorted by trend_score (then confidence)."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    scan_date = date.today().isoformat()

    # Sort before saving so CSV row order matches what the table shows
    ordered = sorted(
        results,
        key=lambda r: (r.get("trend_score", 0), r["confidence"]),
        reverse=True,
    ) if sort_by == "trend" else sorted(
        results, key=lambda r: r["confidence"], reverse=True
    )

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in ordered:
            writer.writerow({
                "scan_date": scan_date,
                "analysis_date": analysis_date.isoformat(),
                "ticker": r["ticker"],
                "action": r["action"],
                "confidence": r["confidence"],
                "trend_score": r.get("trend_score", 0),
                "free_tier": r.get("free_tier", ""),
                "halt": r["halt"],
                "entry_bid": r["entry_bid"],
                "stop_loss": r["stop_loss"],
                "target": r["target"],
                "momentum": r["momentum"],
                "chip_analysis": r["chip"],
                "risk_factors": r["risk"],
                "data_quality_flags": "|".join(r.get("flags") or []),
            })

    _console.print(f"\n  [green]📄 CSV 已儲存:[/green] {csv_path}  ({len(results)} 筆)")


def _action_style(action: str) -> str:
    mapping = {
        "BUY": "bold green",
        "STRONG_BUY": "bold bright_green",
        "SELL": "bold red",
        "STRONG_SELL": "bold bright_red",
        "HOLD": "yellow",
        "CAUTION": "dim yellow",
        "WATCH": "cyan",
    }
    return mapping.get(action.upper(), "white")


def _conf_bar(conf: int) -> str:
    filled = round(conf / 10)
    bar = "█" * filled + "░" * (10 - filled)
    if conf >= 70:
        color = "green"
    elif conf >= 50:
        color = "yellow"
    else:
        color = "red"
    return f"[{color}]{bar}[/{color}] [dim]{conf}[/dim]"


def _trend_bar(ts: int) -> str:
    if ts >= 25:
        color = "green"
    elif ts >= 15:
        color = "yellow"
    else:
        color = "dim"
    return f"[{color}]{ts}[/{color}][dim]/37[/dim]"


def _print_table(
    results: list[dict],
    top: int,
    min_confidence: int,
    scan_date: str = "",
    name_map: dict[str, str] | None = None,
    sort_by: str = "trend",
) -> None:
    valid = [
        r for r in results
        if not r["halt"] and r["error"] is None
        and "NO_CATALYST" not in (r.get("flags") or [])
    ]
    halted = [r for r in results if r["halt"] or r["error"] is not None]

    if sort_by == "trend":
        valid.sort(key=lambda r: (r.get("trend_score", 0), r["confidence"]), reverse=True)
        sort_label = "趨勢強度"
    else:
        valid.sort(key=lambda r: r["confidence"], reverse=True)
        sort_label = "信心分數"

    if min_confidence > 0:
        valid = [r for r in valid if r["confidence"] >= min_confidence]
    if top:
        valid = valid[:top]

    title_str = f"BATCH SCAN RESULTS  {scan_date}  [{sort_label}排序]" if scan_date else f"BATCH SCAN RESULTS  [{sort_label}排序]"
    table = Table(
        title=title_str,
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white on dark_blue",
        title_style="bold white",
        border_style="blue",
        show_lines=True,
    )
    table.add_column("Rank", justify="center", style="dim", width=5)
    table.add_column("Ticker", style="bold white", width=11)
    table.add_column("Action", width=12)
    table.add_column("Confidence", width=18)
    table.add_column("趨勢", justify="right", width=9)
    table.add_column("Entry", justify="right", style="cyan", width=9)
    table.add_column("Stop", justify="right", style="red", width=9)
    table.add_column("Target", justify="right", style="green", width=9)
    table.add_column("Upside", justify="right", style="yellow", width=7)

    for i, r in enumerate(valid, 1):
        action_str = r["action"] + ("*" if r["free_tier"] else "")

        action_text = Text.from_markup(f"[{_action_style(r['action'])}]{action_str}[/{_action_style(r['action'])}]")
        upside_pct = (r["target"] / r["entry_bid"] - 1) * 100 if r["entry_bid"] > 0 else 0
        ticker = r["ticker"]
        if name_map:
            short_name = name_map.get(ticker, "")
            ticker_cell = f"{ticker}\n[dim]{short_name}[/dim]" if short_name else ticker
        else:
            ticker_cell = ticker
        table.add_row(
            str(i),
            ticker_cell,
            action_text,
            _conf_bar(r["confidence"]),
            _trend_bar(r.get("trend_score", 0)),
            f"{r['entry_bid']:.1f}",
            f"{r['stop_loss']:.1f}",
            f"{r['target']:.1f}",
            f"{upside_pct:+.1f}%",
        )

    _console.print()
    if valid:
        _console.print(table)
    else:
        _console.print(Panel(f"[dim]無符合條件的標的 (min_confidence={min_confidence})[/dim]", border_style="yellow"))

    # LLM details
    for r in valid:
        if r.get("verdict") or r["momentum"] or r["chip"] or r["risk"]:
            _console.print(f"\n[bold white]{r['ticker']}[/bold white] LLM 分析")
            if r.get("verdict"):
                _console.print(f"  [bold green]判決[/bold green] {r['verdict']}")
            if r.get("position"):
                _console.print(f"  [bold cyan]倉位[/bold cyan] {r['position']}")
            if r["momentum"]:
                _console.print(f"  [cyan]動能[/cyan] {r['momentum']}")
            if r["chip"]:
                _console.print(f"  [magenta]籌碼[/magenta] {r['chip']}")
            if r["risk"]:
                _console.print(f"  [yellow]風險[/yellow] {r['risk']}")

    _console.print(f"\n  [dim]* = free_tier_mode（無分點資料，閾值較低）[/dim]")

    if halted:
        tickers_str = ", ".join(r["ticker"] for r in halted)
        _console.print(f"\n  [dim]略過 {len(halted)} 檔 (HALT/ERROR): {tickers_str}[/dim]")

    llm_count = sum(1 for r in results if r.get("verdict") or r.get("momentum") or r.get("chip") or r.get("risk"))
    llm_note = f"，LLM 補充 {llm_count} 檔" if llm_count else ""
    _console.print(Panel(
        f"[bold green]掃描完成[/bold green]  {len(results)} 檔  •  有效訊號 [bold]{len(valid)}[/bold] 檔{llm_note}",
        border_style="green",
        padding=(0, 2),
    ))


def _print_by_industry(
    results: list[dict],
    top: int,
    min_confidence: int,
    scan_date: str = "",
    name_map: dict[str, str] | None = None,
    industry_map: dict[str, str] | None = None,
) -> None:
    """Print scan results grouped by industry strength, sorted high→low.

    Each industry section shows: industry name, strength %, qualifying stocks.
    Industries with no qualifying stocks show header only.
    Weak industries (strength < -1%) are shown last with ▼ marker.
    """
    from collections import defaultdict

    valid = [
        r
        for r in results
        if not r["halt"] and r["error"] is None and r["confidence"] >= min_confidence
        and "NO_CATALYST" not in (r.get("flags") or [])
    ]

    if not valid:
        _console.print("[dim]  (無符合條件標的)[/dim]")
        return

    ind_map = industry_map or {}
    name_m = name_map or {}

    # Compute industry strength: median change_pct of all results per industry
    industry_change: dict[str, list[float]] = defaultdict(list)
    for r in results:
        ind = ind_map.get(r["ticker"], "其他")
        chg = r.get("change_pct", 0.0) or 0.0
        industry_change[ind].append(chg)

    def _median(vals: list[float]) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    industry_strength: dict[str, float] = {
        ind: _median(chgs) for ind, chgs in industry_change.items()
    }

    # Group valid stocks by industry
    by_industry: dict[str, list[dict]] = defaultdict(list)
    for r in valid:
        ind = ind_map.get(r["ticker"], "其他")
        by_industry[ind].append(r)

    # Sort stocks within each industry by confidence desc
    for ind in by_industry:
        by_industry[ind].sort(key=lambda r: r["confidence"], reverse=True)

    # Sort industries: strong first, weak last
    all_industries = sorted(
        industry_strength.keys(),
        key=lambda ind: industry_strength[ind],
        reverse=True,
    )

    title = f"掃描結果  {scan_date}  【產業強度排序】" if scan_date else "掃描結果  【產業強度排序】"
    _console.print(f"\n[bold white]{title}[/bold white]")

    for ind in all_industries:
        strength = industry_strength.get(ind, 0.0)
        stocks = by_industry.get(ind, [])
        if not stocks:
            continue  # skip industries with no valid signals

        ready_n = sum(1 for s in stocks if s["action"] == "LONG")

        strength_icon = "▲" if strength >= 0 else "▼"
        strength_color = "green" if strength >= 0 else "red"
        ind_header = (
            f"\n[dim]──[/dim] [bold]{ind}[/bold]  "
            f"[{strength_color}]{strength_icon}{abs(strength):.1f}%[/{strength_color}]"
        )
        ind_header += f"  [dim]({ready_n} 準備突破 / {len(stocks)} 整理中)[/dim]"

        _console.print(ind_header)

        for s in stocks:
            ticker = s["ticker"]
            name = (name_m.get(ticker) or "")[:5]
            action = s["action"]
            conf = s["confidence"]

            action_label = "🚀 準備突破" if action == "LONG" else "🔍 整理中"
            action_clr = "cyan" if action == "LONG" else "yellow"

            conf_bar = _conf_bar(conf)
            _console.print(
                f"  [dim]{ticker}[/dim]  [{action_clr}]{action_label}[/{action_clr}]"
                f"  {conf_bar}  [dim]{name}[/dim]"
            )

    if top and len(valid) > top:
        _console.print(f"\n[dim]  (顯示前 {top} 檔，共 {len(valid)} 檔符合條件)[/dim]")


def _run_phase(
    tickers: list[str],
    analysis_date: date,
    workers: int,
    llm_provider=None,
    no_llm: bool = False,
    label_repo=None,
    market_map: dict[str, str] | None = None,
) -> list[dict]:
    """執行一批 ticker 的掃描，回傳 results list（順序不保證）。

    共用一組 FinMindClient + ChipProxyFetcher 實例，讓所有 worker 共享
    日期級快取（T86/Margin/SBL/DayTrade/TPEx + OHLCV superset）。
    第一個 ticker 填充快取後，後續 ticker 直接命中記憶體 — 大幅減少 API 呼叫。

    CPython GIL 保證 dict 寫入原子性，最壞情況是前幾個 ticker 重複呼叫 API，
    不會資料錯亂。

    no_llm=True 強制關閉 LLM（Phase 1 deterministc 用，避免 StrategistAgent 自動偵測 API key）。
    label_repo: shared BrokerLabelRepository instance（read-only，多執行緒安全）。
    market_map: {ticker: "TSE"|"TPEx"}
    """
    # 建立共用客戶端 — 所有 worker 共享快取
    shared_finmind = FinMindClient()
    shared_chip = ChipProxyFetcher()
    shared_chip.shares_map = _load_shares_map()
    shared_agent = _make_agent(
        llm_provider=llm_provider,
        no_llm=no_llm,
        label_repo=label_repo,
        finmind=shared_finmind,
        chip_fetcher=shared_chip,
    )

    results: list[dict] = []
    total = len(tickers)
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=30, style="cyan", complete_style="green"),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=_console,
        transient=False,
    ) as progress:
        task = progress.add_task(f"掃描 {total} 檔", total=total)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _scan_one, ticker, analysis_date, shared_agent,
                    market=market_map.get(ticker, "TSE") if market_map else "TSE"
                ): ticker
                for ticker in tickers
            }
            for future in as_completed(futures):
                ticker = futures[future]
                result = future.result()
                results.append(result)
                if result["halt"]:
                    log_line = f"[dim]{ticker:<8}[/dim] [red]HALT[/red]"
                else:
                    conf = result["confidence"]
                    color = "green" if conf >= 60 else "yellow" if conf >= 40 else "white"
                    log_line = f"[dim]{ticker:<8}[/dim] [{color}]conf={conf}[/{color}]"
                with _progress_lock:
                    progress.console.print(log_line)
                    progress.update(task, advance=1)
    return results


def _record_results(results: list[dict], analysis_date: date) -> int:
    """Write non-halted scan results to signal_outcomes DB (source='live').

    Returns count of successfully recorded signals.
    Skips gracefully if DATABASE_URL is not set or DB is unreachable.
    """
    import os
    if not os.environ.get("DATABASE_URL"):
        return 0
    try:
        from taiwan_stock_agent.infrastructure.db import init_pool
        from taiwan_stock_agent.infrastructure.signal_recorder import record_signal
        init_pool()
    except Exception as e:
        logger.debug("DB init failed, skipping record: %s", e)
        return 0

    recorded = 0
    for r in results:
        signal = r.get("_signal")
        if signal is None or r["halt"] or r["error"] is not None:
            continue
        try:
            record_signal(signal, source="live")
            recorded += 1
        except Exception as e:
            logger.debug("record_signal %s: %s", r["ticker"], e)
    return recorded


def run_batch(
    tickers: list[str],
    analysis_date: date,
    top: int,
    min_confidence: int,
    workers: int,
    csv_path: Path | None = None,
    llm_provider=None,
    llm_top: int | None = None,
    label_repo=None,
    industry_map: dict[str, str] | None = None,
    save_db: bool = False,
    name_map: dict[str, str] | None = None,
    market_map: dict[str, str] | None = None,
    sort_by: str = "trend",
) -> None:
    llm_label = getattr(llm_provider, "name", None) or "（無 LLM）"
    label_status = (
        f"[green]{len(label_repo.list_all())} 筆標籤[/green]"
        if label_repo is not None and not isinstance(label_repo, _EmptyLabelRepo)
        else "[dim yellow]空（Pillar 2A 停用）[/dim yellow]"
    )
    _console.print(Panel(
        f"[bold white]掃描清單[/bold white]  {len(tickers)} 檔\n"
        f"[bold white]分析日期[/bold white]  {analysis_date}\n"
        f"[bold white]LLM 引擎[/bold white]  [cyan]{llm_label}[/cyan]\n"
        f"[bold white]分點標籤[/bold white]  {label_status}\n"
        f"[bold white]並行執行[/bold white]  {workers} workers",
        title="[bold cyan]Taiwan Stock Scanner[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))

    if llm_provider is None:
        # 純 deterministic：強制關閉 LLM（避免 StrategistAgent 自動偵測 API key）
        results = _run_phase(tickers, analysis_date, workers, no_llm=True, label_repo=label_repo, market_map=market_map)
    else:
        # 永遠兩階段：Phase 1 全量 deterministic → Phase 2 top N with LLM
        _console.print(f"\n[bold cyan][Phase 1][/bold cyan] deterministic scan：{len(tickers)} 檔")
        results = _run_phase(tickers, analysis_date, workers, no_llm=True, label_repo=label_repo, market_map=market_map)

        # 排序有效結果
        eligible = sorted(
            [r for r in results if not r["halt"] and r["error"] is None],
            key=lambda r: r["confidence"], reverse=True,
        )
        _console.print(f"\n[bold cyan][Phase 1 完成][/bold cyan] {len(results)} 檔（有效 [green]{len(eligible)}[/green] 檔）")
        if eligible:
            top5 = "  ".join(f"[bold]{r['ticker']}[/bold]([green]{r['confidence']}[/green])" for r in eligible[:5])
            _console.print(f"  前幾名: {top5}{'[dim]...[/dim]' if len(eligible) > 5 else ''}")

        # 決定 Phase 2 範圍：CLI 指定優先，否則互動詢問
        if llm_top is None:
            raw = input(f"\n送前幾名給 LLM [{llm_label}]？（Enter = 不送）：> ").strip()
            llm_top = int(raw) if raw.isdigit() and int(raw) > 0 else 0

        llm_tickers = [r["ticker"] for r in eligible[:llm_top]] if llm_top else []

        if not llm_tickers:
            _console.print("  [dim]→ 跳過 LLM[/dim]\n")
        else:
            _console.print(f"\n[bold cyan][Phase 2][/bold cyan] 送前 {llm_top} 名給 [cyan]{llm_label}[/cyan]：{', '.join(llm_tickers)}")
            p2_workers = min(3, len(llm_tickers))
            phase2 = _run_phase(llm_tickers, analysis_date, p2_workers, llm_provider=llm_provider, label_repo=label_repo, market_map=market_map)
            p2_valid = {r["ticker"]: r for r in phase2 if r.get("error") is None}
            results = [p2_valid.get(r["ticker"], r) for r in results]

    # --- Post-processing: sector ranking + persistence ---
    scan_data_dir = Path(__file__).resolve().parents[1] / "data" / "scans"

    # Compute industry strength (median change_pct per industry) for catalyst filter
    from collections import defaultdict as _defaultdict
    _industry_change: dict[str, list[float]] = _defaultdict(list)
    for r in results:
        _ind = (industry_map or {}).get(r["ticker"], "其他")
        _chg = r.get("change_pct", 0.0) or 0.0
        _industry_change[_ind].append(_chg)

    def _median_local(vals: list[float]) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    industry_strength: dict[str, float] = {
        ind: _median_local(chgs) for ind, chgs in _industry_change.items()
    }

    if industry_map:
        n_sector = _apply_sector_ranks(results, industry_map)
        if n_sector:
            _console.print(f"  [dim]↑ 產業相對排名加分: {n_sector} 檔 (+5/+7/+10 tier)[/dim]")

    n_no_catalyst = _apply_catalyst_filter(results, industry_map, industry_strength)
    if n_no_catalyst:
        _console.print(f"  [dim]↓ 無題材標記（WATCH）: {n_no_catalyst} 檔[/dim]")

    n_persist = _apply_persistence_bonus(results, analysis_date, scan_data_dir)
    if n_persist:
        _console.print(f"  [dim]↑ 持續訊號加分: {n_persist} 檔 (RISING +7 / STABLE +5)[/dim]")

    n_near_high = _apply_near_high_first_day(results, analysis_date, scan_data_dir)
    if n_near_high:
        _console.print(f"  [dim]↑ 近高蓄積首日補償: {n_near_high} 檔 (NEAR_HIGH_COIL +4)[/dim]")

    # Re-evaluate action after post-processing bonuses may have crossed a threshold.
    # A CAUTION that reaches ≥ 45 after bonuses becomes WATCH.
    _WATCH_MIN_PP = 45
    for r in results:
        if r.get("action") == "CAUTION" and r.get("confidence", 0) >= _WATCH_MIN_PP:
            r["action"] = "WATCH"

    # --- Optional: record to DB (source=live) for factor analysis ---
    if save_db:
        n_recorded = _record_results(results, analysis_date)
        if n_recorded:
            _console.print(f"  [dim green]✓ {n_recorded} 筆訊號已寫入 DB (source=live)[/dim green]")
        else:
            _console.print("  [dim yellow]⚠ DB 未設定或無法連線，略過寫入[/dim yellow]")

    if industry_map:
        _print_by_industry(
            results,
            top,
            min_confidence,
            scan_date=str(analysis_date),
            name_map=name_map,
            industry_map=industry_map,
        )
    else:
        _print_table(
            results,
            top,
            min_confidence,
            scan_date=str(analysis_date),
            name_map=name_map,
            sort_by=sort_by,
        )

    if csv_path:
        _save_csv(results, analysis_date, csv_path, sort_by=sort_by)

    html_path = (csv_path or Path("data/scans") / f"scan_{analysis_date}.csv").with_suffix(".html")
    _generate_plan_html(results, str(analysis_date), html_path,
                        name_map=name_map or {}, industry_map=industry_map or {},
                        market_map=market_map or {},
                        heat_summary=_load_heat_summary())
    _console.print(f"  [dim cyan]📄 HTML: file://{html_path.resolve()}[/dim cyan]")
    import subprocess, sys as _sys
    if _sys.platform == "darwin":
        subprocess.Popen(["open", str(html_path)])


# ── Plan HTML generator ──────────────────────────────────────────────────────

_WATCH_FLAGS_CONSOLIDATING = frozenset(["TREND_CONT", "CHIP_LOADING", "NO_CATALYST"])

_ROOT_PATH = Path(__file__).resolve().parents[1]
_HEAT_DIR = _ROOT_PATH / "data" / "market_heat"


def _load_heat_summary() -> dict:
    """Load latest market heat + concept snapshots for HTML rotation radar.

    Returns {} if no snapshots are available.
    """
    if not _HEAT_DIR.exists():
        return {}
    try:
        import json as _json

        result: dict = {}

        heat_files = sorted(_HEAT_DIR.glob("heat_*.json"))
        if heat_files:
            with open(heat_files[-1], encoding="utf-8") as f:
                hd = _json.load(f)
            result["date"] = hd.get("snapshot_date", "")
            result["hot_industries"] = hd.get("hot_industries", [])[:5]
            result["accelerating"] = [
                x for x in hd.get("accelerating", [])
                if x not in hd.get("hot_industries", [])
            ][:4]
            result["rotating_up"] = hd.get("rotating_up", [])[:3]
            result["market_state"] = hd.get("market_state", "")

        concept_files = sorted(_HEAT_DIR.glob("concept_heat_*.json"))
        ticker_concepts: dict[str, list[str]] = {}
        if concept_files:
            with open(concept_files[-1], encoding="utf-8") as f:
                cd = _json.load(f)
            concepts = cd.get("concepts", {})
            hot = [
                {"key": k, "name_zh": v.get("name_zh", k),
                 "ret_5d_pct": v.get("ret_5d_pct", 0), "rank_pct": v.get("rank_pct", 0)}
                for k, v in concepts.items()
                if v.get("rank_pct", 0) >= 60
            ]
            hot.sort(key=lambda x: -x["rank_pct"])
            result["hot_concepts"] = hot[:6]

            concepts_path = _ROOT_PATH / "config" / "concepts.json"
            if concepts_path.exists():
                with open(concepts_path, encoding="utf-8") as f:
                    cdefs_raw = _json.load(f)
                # concepts.json may be {"concepts": {...}} or flat {key: {...}}
                cdefs = cdefs_raw.get("concepts", cdefs_raw)
                hot_map = {c["key"]: c["name_zh"] for c in hot}
                for ck, cdef in cdefs.items():
                    if ck in hot_map:
                        for t in cdef.get("tickers", []):
                            ticker_concepts.setdefault(t, []).append(hot_map[ck])

        result["ticker_concepts"] = ticker_concepts
        return result
    except Exception:
        return {}


_SHARES_CACHE = _ROOT_PATH / "data" / "_shares_cache.json"


def _load_shares_map() -> dict[str, int]:
    """Load total shares outstanding for all listed/OTC stocks.

    Fetches from TWSE/TPEx bulk opendata endpoints and caches for 7 days.
    Returns {} on failure so turnover scoring degrades gracefully.
    """
    import json as _json
    from datetime import date as _date
    try:
        import requests as _req
    except ImportError:
        return {}

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
        r = _req.get(
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
        r = _req.get(
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


def _fetch_plan_chart(ticker: str, market: str) -> dict:
    """Fetch 5-month daily OHLCV + Bollinger Bands (20,2) via yfinance."""
    suffix = ".TW" if market == "TSE" else ".TWO"
    empty: dict = {"candles": [], "bb_upper": [], "bb_mid": [], "bb_lower": []}
    try:
        import pandas as pd
        import yfinance as yf
        period = 20
        hist = yf.Ticker(f"{ticker}{suffix}").history(period="5mo", interval="1d", auto_adjust=True)
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
        closes = [r["close"] for r in rows]
        bb_upper, bb_mid, bb_lower = [], [], []
        for i in range(period - 1, len(rows)):
            window = closes[i - period + 1: i + 1]
            mean = sum(window) / period
            std = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
            t = rows[i]["time"]
            bb_upper.append({"time": t, "value": round(mean + 2 * std, 2)})
            bb_mid.append({"time": t, "value": round(mean, 2)})
            bb_lower.append({"time": t, "value": round(mean - 2 * std, 2)})
        display_rows = rows[period - 1:]
        return {"candles": display_rows, "bb_upper": bb_upper, "bb_mid": bb_mid, "bb_lower": bb_lower}
    except Exception:
        return empty


def _generate_plan_html(
    results: list[dict],
    scan_date: str,
    html_path: Path,
    name_map: dict[str, str],
    industry_map: dict[str, str],
    market_map: dict[str, str],
    heat_summary: dict | None = None,
) -> None:
    """Dark-themed HTML report for plan scan results (LONG + actionable WATCH only)."""
    import json as _json
    from html import escape as _esc

    # Filter: LONG stocks + WATCH stocks that are NOT in consolidation
    def _is_consolidating(r: dict) -> bool:
        flags = set(r.get("flags") or [])
        return bool(flags & _WATCH_FLAGS_CONSOLIDATING)

    filtered = [
        r for r in results
        if not r.get("halt") and r.get("error") is None
        and r.get("action") in ("LONG", "WATCH")
        and not (r.get("action") == "WATCH" and _is_consolidating(r))
    ]
    filtered.sort(key=lambda r: (r.get("confidence", 0), r.get("trend_score", 0)), reverse=True)

    if not filtered:
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(
            f'<!DOCTYPE html><html lang="zh-TW"><head><meta charset="utf-8"></head>'
            f'<body style="background:#0d1117;color:#e6edf3;font-family:sans-serif;padding:40px">'
            f'<h2>預突破掃描 {scan_date}</h2>'
            f'<p>今日無符合條件的個股。</p></body></html>',
            encoding="utf-8",
        )
        return

    n_long = sum(1 for r in filtered if r["action"] == "LONG")
    n_watch = sum(1 for r in filtered if r["action"] == "WATCH")

    # Fetch chart data
    _console.print("  [dim]抓取線圖資料（plan HTML）…[/dim]")
    chart_data: dict[str, dict] = {}
    pairs = [(r["ticker"], market_map.get(r["ticker"], "TSE")) for r in filtered]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_fetch_plan_chart, t, m): t for t, m in pairs}
        for fut in as_completed(futs):
            chart_data[futs[fut]] = fut.result()

    def _rule_rec(r: dict) -> str:
        """Plain-Chinese fallback recommendation when LLM verdict is absent."""
        flag_set = set(r.get("flags") or [])
        action = r["action"]
        conf = r.get("confidence", 0)
        parts: list[str] = []

        if "ACCUM_MODE" in flag_set:
            parts.append("大盤處於修正期，法人籌碼出現逆勢買進跡象")

        if any("RISING" in f for f in flag_set):
            parts.append("訊號持續走強")
        elif any("STABLE" in f for f in flag_set):
            parts.append("訊號保持穩定")

        if "COILING_PRIME" in flag_set:
            parts.append("呈現強力蓄積型態")
        elif "COILING" in flag_set:
            parts.append("已有蓄積跡象")
        if "NEAR_HIGH_COIL" in flag_set:
            parts.append("貼近近期高點蓄勢待發")
        if "BB_UPPER_COIL" in flag_set:
            parts.append("布林帶上軌蓄力")
        if "MA5_WALK" in flag_set:
            parts.append("均線多頭向上排列")
        if "MOMENTUM_WALK" in flag_set:
            parts.append("突破後動能延續")

        if any("DUAL_FLOW_STRONG" in f for f in flag_set):
            parts.append("外資與投信同步大力買進")
        elif any("DUAL_FLOW" in f for f in flag_set):
            parts.append("外資與投信同步買進")
        if any("OBV_ACCUM_PRIME" in f for f in flag_set):
            parts.append("成交量顯示法人暗中大量吸籌")
        elif any("OBV_ACCUM" in f for f in flag_set):
            parts.append("成交量顯示法人持續積累")
        if any("VOL_ASYMMETRY" in f for f in flag_set):
            parts.append("上漲日成交量明顯大於下跌日")
        if any("SECTOR_RANK" in f for f in flag_set):
            parts.append("產業排名靠前")

        if action == "LONG":
            conclusion = "整體條件成熟，建議積極布局。" if conf >= 70 else "條件符合突破要求，可考慮進場。"
        else:
            conclusion = "尚待突破確認，建議列入觀察。"

        if parts:
            return "、".join(parts[:4]) + "。" + conclusion
        return conclusion

    hs = heat_summary or {}
    ticker_concepts: dict[str, list[str]] = hs.get("ticker_concepts", {})

    cards: list[str] = []
    for i, r in enumerate(filtered):
        ticker = r["ticker"]
        name = _esc(name_map.get(ticker, ticker))
        industry = _esc(industry_map.get(ticker, ""))
        action = r["action"]
        conf = r.get("confidence", 0)
        entry = r.get("entry_bid") or 0.0
        target = r.get("target") or 0.0
        stop = r.get("stop_loss") or 0.0
        market = market_map.get(ticker, "TSE")
        exchange = "TWSE" if market == "TSE" else "TPEX"
        tv_url = f"https://www.tradingview.com/chart/?symbol={exchange}%3A{ticker}"
        gi_url = f"https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={ticker}"

        upside_pct = ((target - entry) / entry * 100) if entry > 0 and target > entry else 0.0
        delay = f"{i * 0.05:.2f}"

        flag_set = set(r.get("flags") or [])
        is_accum = "ACCUM_MODE" in flag_set
        if action == "LONG":
            badge_zh = "底部買進" if is_accum else "突破進場"
            gcls = "alpha"
            rec_label = "建議買進"
            rec_cls = "buy"
        else:
            badge_zh = "底部吸籌" if is_accum else "等待確認"
            gcls = "accum" if is_accum else "beta"
            rec_label = "底部布局" if is_accum else "持續觀察"
            rec_cls = "accum" if is_accum else "watch"

        llm_verdict = r.get("verdict") or ""
        llm_chip = r.get("chip") or ""
        if llm_verdict:
            rec_text = _esc(llm_verdict)
            rec_source_html = '<span class="rec-source src-ai">AI 分析</span>'
        else:
            rec_text = _esc(_rule_rec(r))
            rec_source_html = '<span class="rec-source src-rule">規則評估</span>'
        chip_txt = _esc(llm_chip)

        entry_s = f"{entry:.2f}" if entry else "--"
        target_s = f"{target:.2f}" if target else "--"
        stop_s = f"{stop:.2f}" if stop else "--"
        upside_s = f"+{upside_pct:.1f}%" if upside_pct > 0 else "--"
        conf_cls = "pos" if action == "LONG" else "conf-watch"

        ctags = ticker_concepts.get(ticker, [])
        concept_html = (
            '<div class="ctag-row">' +
            "".join(f'<span class="ctag">{_esc(c)}</span>' for c in ctags[:3]) +
            "</div>"
        ) if ctags else ""

        cards.append(f"""
    <div class="card" style="animation-delay:{delay}s">
      <div class="card-header">
        <div class="rank">{i+1}</div>
        <div class="info">
          <div class="ticker">{_esc(ticker)} <span class="tname">{name}</span></div>
          <div class="cname">{industry}</div>
        </div>
        <div class="badge g-{gcls}">{badge_zh}</div>
      </div>
      {concept_html}
      <div class="metrics">
        <div class="m"><div class="mv {conf_cls}">{conf}</div><div class="ml">信心分</div></div>
        <div class="m"><div class="mv">{entry_s}</div><div class="ml">進場價</div></div>
        <div class="m"><div class="mv pos">{upside_s}</div><div class="ml">目標空間</div></div>
        <div class="m"><div class="mv neg">{stop_s}</div><div class="ml">止損</div></div>
        <div class="m"><div class="mv">{target_s}</div><div class="ml">目標價</div></div>
      </div>
      <div class="chart" data-ticker="{_esc(ticker)}"></div>
      <div class="rec">
        <div class="rec-header">
          <span class="rec-badge rec-{rec_cls}">{rec_label}</span>
          {rec_source_html}
        </div>
        <div class="rec-text">{rec_text}</div>
        {f'<div class="rec-chip">{chip_txt}</div>' if chip_txt else ""}
      </div>
      <div class="links">
        <a class="link-btn tv" href="{tv_url}" target="_blank" rel="noopener">TradingView</a>
        <a class="link-btn gi" href="{gi_url}" target="_blank" rel="noopener">Goodinfo</a>
      </div>
    </div>""")

    # Build rotation radar HTML for header
    def _heat_badge(label: str, cls: str) -> str:
        return f'<span class="hbadge {cls}">{_esc(label)}</span>'

    radar_rows: list[str] = []
    hot_inds = hs.get("hot_industries", [])
    accel_inds = hs.get("accelerating", [])
    hot_concepts = hs.get("hot_concepts", [])
    if hot_inds:
        badges = "".join(_heat_badge(n, "hb-hot") for n in hot_inds)
        radar_rows.append(f'<div class="radar-row"><span class="radar-label">🔥 熱門產業</span>{badges}</div>')
    if accel_inds:
        badges = "".join(_heat_badge(n, "hb-warm") for n in accel_inds)
        radar_rows.append(f'<div class="radar-row"><span class="radar-label">📈 升溫中</span>{badges}</div>')
    if hot_concepts:
        def _fmt_concept(c: dict) -> str:
            ret = c.get("ret_5d_pct", 0)
            sign = "+" if ret >= 0 else ""
            return f'{_esc(c["name_zh"])} {sign}{ret:.1f}%'
        badges = "".join(_heat_badge(_fmt_concept(c), "hb-concept") for c in hot_concepts)
        radar_rows.append(f'<div class="radar-row"><span class="radar-label">💡 熱門題材</span>{badges}</div>')
    radar_html = (
        f'<div class="radar">{"".join(radar_rows)}</div>'
        if radar_rows else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>預突破掃描 {_esc(scan_date)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh}}
.header{{background:linear-gradient(135deg,#1a1a2e,#0d2137,#0a2744);padding:32px;border-bottom:1px solid #21262d}}
.header h1{{font-size:30px;font-weight:800;color:#58a6ff;letter-spacing:-0.5px}}
.subtitle{{color:#8b949e;margin-top:6px;font-size:14px}}
.stats{{display:flex;gap:12px;margin-top:20px;flex-wrap:wrap}}
.stat{{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:12px 20px}}
.sv{{font-size:24px;font-weight:700}}.sl{{font-size:11px;color:#8b949e;margin-top:2px}}
.radar{{margin-top:20px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);
  border-radius:10px;padding:14px 16px;display:flex;flex-direction:column;gap:10px}}
.radar-row{{display:flex;align-items:center;flex-wrap:wrap;gap:6px}}
.radar-label{{font-size:11px;color:#8b949e;min-width:80px;flex-shrink:0}}
.hbadge{{display:inline-block;font-size:11px;font-weight:600;padding:3px 10px;border-radius:12px;white-space:nowrap}}
.hb-hot{{background:rgba(248,81,73,.12);color:#ff7b7b;border:1px solid rgba(248,81,73,.25)}}
.hb-warm{{background:rgba(63,185,80,.10);color:#52c261;border:1px solid rgba(63,185,80,.25)}}
.hb-concept{{background:rgba(163,113,247,.12);color:#a78bfa;border:1px solid rgba(163,113,247,.25)}}
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
.g-beta{{background:rgba(88,166,255,.15);color:#58a6ff;border:1px solid rgba(88,166,255,.3)}}
.g-accum{{background:rgba(210,153,34,.15);color:#e3a008;border:1px solid rgba(210,153,34,.3)}}
.ctag-row{{padding:6px 16px;border-bottom:1px solid #21262d;display:flex;gap:5px;flex-wrap:wrap}}
.ctag{{font-size:10px;font-weight:600;padding:2px 8px;border-radius:10px;
  background:rgba(163,113,247,.12);color:#a78bfa;border:1px solid rgba(163,113,247,.2)}}
.metrics{{display:flex;border-bottom:1px solid #21262d}}
.m{{flex:1;padding:10px 6px;text-align:center;border-right:1px solid #21262d}}
.m:last-child{{border-right:none}}
.mv{{font-size:13px;font-weight:600}}.ml{{font-size:10px;color:#8b949e;margin-top:2px}}
.pos{{color:#3fb950}}.neg{{color:#f85149}}.conf-watch{{color:#58a6ff}}
.chart{{height:240px;background:#0d1117;position:relative}}
.links{{display:flex;gap:8px;padding:10px 16px;background:#0d1117;border-top:1px solid #21262d}}
.link-btn{{flex:1;display:block;text-align:center;padding:8px;border-radius:6px;font-size:12px;font-weight:600;
  text-decoration:none;transition:opacity .15s}}
.link-btn:hover{{opacity:.8}}
.tv{{background:#1565c0;color:#fff}}
.gi{{background:#1b4332;color:#3fb950;border:1px solid #236840}}
.footer{{text-align:center;padding:32px;color:#484f58;font-size:12px}}
.rec{{padding:14px 16px;border-top:1px solid #21262d;display:flex;flex-direction:column;gap:8px}}
.rec-header{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.rec-badge{{display:inline-block;font-size:12px;font-weight:700;padding:4px 12px;border-radius:14px}}
.rec-buy{{background:rgba(63,185,80,.18);color:#3fb950;border:1px solid rgba(63,185,80,.35)}}
.rec-watch{{background:rgba(88,166,255,.15);color:#58a6ff;border:1px solid rgba(88,166,255,.3)}}
.rec-accum{{background:rgba(210,153,34,.18);color:#e3a008;border:1px solid rgba(210,153,34,.35)}}
.rec-source{{font-size:10px;font-weight:600;padding:3px 8px;border-radius:10px}}
.src-ai{{background:rgba(163,113,247,.15);color:#a78bfa;border:1px solid rgba(163,113,247,.3)}}
.src-rule{{background:rgba(139,148,158,.1);color:#8b949e;border:1px solid rgba(139,148,158,.2)}}
.rec-text{{font-size:12px;color:#c9d1d9;line-height:1.6}}
.rec-chip{{font-size:11px;color:#8b949e;line-height:1.5;margin-top:2px}}
</style>
</head>
<body>
<div class="header">
  <h1>📈 預突破掃描</h1>
  <div class="subtitle">{_esc(scan_date)} &nbsp;·&nbsp; 收盤掃描 &nbsp;·&nbsp; 共 {len(filtered)} 支</div>
  <div class="stats">
    <div class="stat"><div class="sv" style="color:#ff6b6b">{n_long}</div><div class="sl">突破進場</div></div>
    <div class="stat"><div class="sv" style="color:#58a6ff">{n_watch}</div><div class="sl">等待確認</div></div>
  </div>
  {radar_html}
</div>
<div class="grid">
{"".join(cards)}
</div>
<div class="footer">預突破掃描自動生成 · {_esc(scan_date)}</div>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
const CHART_DATA = {_json.dumps(chart_data, ensure_ascii=False)};
const _obs = new IntersectionObserver(function(entries) {{
  entries.forEach(function(e) {{
    if (!e.isIntersecting || e.target.dataset.init) return;
    e.target.dataset.init = "1";
    _obs.unobserve(e.target);
    const ticker = e.target.dataset.ticker;
    const data = CHART_DATA[ticker];
    if (!data || !data.candles || data.candles.length === 0) {{
      e.target.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#484f58;font-size:12px">暫無資料</div>';
      return;
    }}
    const chart = LightweightCharts.createChart(e.target, {{
      autoSize: true, height: 240,
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
    const lo = {{ lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }};
    chart.addLineSeries(Object.assign({{}}, lo, {{ color: "#58a6ff" }})).setData(data.bb_mid);
    chart.addLineSeries(Object.assign({{}}, lo, {{ color: "#e3b341" }})).setData(data.bb_upper);
    chart.addLineSeries(Object.assign({{}}, lo, {{ color: "#a371f7" }})).setData(data.bb_lower);
    chart.timeScale().fitContent();
  }});
}}, {{ rootMargin: "100px" }});
document.querySelectorAll(".chart[data-ticker]").forEach(function(el) {{ _obs.observe(el); }});
</script>
</body>
</html>"""

    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")


def main() -> None:
    # 大批次掃描（728 檔）會消耗大量 socket fd；macOS 預設只有 256。
    # 在這裡嘗試提高到 4096，避免 "Too many open files" 錯誤。
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 4096:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, hard), hard))
    except Exception:
        pass  # 不支援的平台或無權限時靜默忽略

    parser = argparse.ArgumentParser(description="批量掃描台股，依信心分數排序")
    parser.add_argument("--tickers", nargs="+", help="自訂標的清單（跳過產業選單）")
    parser.add_argument(
        "--sectors",
        nargs="+",
        type=int,
        metavar="N",
        help="產業代號（數字，非互動模式；例: --sectors 1 4）",
    )
    parser.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=_default_date(),
        help="分析日期 YYYY-MM-DD（預設: 最近交易日）",
    )
    parser.add_argument("--top", type=int, default=10, help="顯示前 N 名（預設: 10）")
    parser.add_argument("--min-confidence", type=int, default=50, help="最低信心分數門檻（預設: 50）")
    parser.add_argument("--workers", type=int, default=5, help="並行 worker 數（預設: 5；建議 3-8，受 FinMind rate limit 限制）")
    parser.add_argument("--save-csv", action="store_true", help="儲存結果到 CSV 檔案")
    parser.add_argument(
        "--csv-path",
        type=Path,
        default=None,
        help="CSV 路徑（預設: data/scans/scan_YYYY-MM-DD.csv）",
    )
    parser.add_argument(
        "--llm",
        default=None,
        metavar="PROVIDER",
        help="LLM 引擎（gemini/claude/openai）；未指定時進入互動選單",
    )
    parser.add_argument(
        "--llm-top",
        type=int,
        default=None,
        metavar="N",
        help="僅對前 N 名呼叫 LLM（非互動模式用）",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="關閉 LLM reasoning，只跑 deterministic scoring",
    )
    parser.add_argument(
        "--save-db",
        action="store_true",
        help="將訊號寫入 signal_outcomes DB (source=live)，用於 factor-report 分析",
    )
    parser.add_argument(
        "--show",
        metavar="DATE",
        help="顯示指定日期的掃描結果（從 CSV 讀取，例: --show 2026-04-10）",
    )
    parser.add_argument(
        "--sort-by",
        choices=["trend", "confidence"],
        default="trend",
        help="排序方式：trend（趨勢強度，預設）或 confidence（信心分數）",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="掃描完成後將結果推送到 Telegram（需要 .env TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）",
    )
    args = parser.parse_args()

    # ── show 模式：從 CSV 印出歷史結果 ──────────────────────────────────────
    if args.show is not None:
        scan_dir = Path(__file__).resolve().parents[1] / "data" / "scans"
        available = sorted(p.stem.replace("scan_", "") for p in scan_dir.glob("scan_*.csv"))

        show_date = args.show.strip() if args.show.strip() else ""
        if not show_date:
            # 互動式選擇（上下鍵）
            if not available:
                _console.print("[red]找不到任何掃描結果（data/scans/ 目錄為空）[/red]")
                return
            import questionary
            rev = list(reversed(available))
            show_date = questionary.select(
                "選擇掃描日期",
                choices=rev,
                default=rev[0],
                style=questionary.Style([
                    ("selected", "fg:cyan bold"),
                    ("pointer", "fg:cyan bold"),
                    ("highlighted", "fg:cyan"),
                    ("question", "bold"),
                ]),
            ).ask()
            if show_date is None:
                return  # Ctrl-C

        csv_file = scan_dir / f"scan_{show_date}.csv"
        if not csv_file.exists():
            if not available:
                _console.print("[red]找不到任何掃描結果（data/scans/ 目錄為空）[/red]")
                return
            _console.print(f"[yellow]找不到 {show_date} 的資料，請重新選擇：[/yellow]")
            import questionary
            rev = list(reversed(available))
            show_date = questionary.select(
                "選擇掃描日期",
                choices=rev,
                default=rev[0],
                style=questionary.Style([
                    ("selected", "fg:cyan bold"),
                    ("pointer", "fg:cyan bold"),
                    ("highlighted", "fg:cyan"),
                    ("question", "bold"),
                ]),
            ).ask()
            if show_date is None:
                return
            csv_file = scan_dir / f"scan_{show_date}.csv"
        import csv as _csv
        with open(csv_file, newline="", encoding="utf-8") as f:
            rows_raw = list(_csv.DictReader(f))
        # Dedup by ticker: keep last (newest) row per ticker (guards against legacy append CSVs)
        seen: dict[str, dict] = {}
        for r in rows_raw:
            seen[r["ticker"]] = r
        results = [
            {
                "ticker": r["ticker"],
                "action": r["action"],
                "confidence": int(r["confidence"]),
                "free_tier": r.get("free_tier", "True") == "True",
                "halt": r.get("halt", "False") == "True",
                "error": None,
                "entry_bid": float(r["entry_bid"]),
                "stop_loss": float(r["stop_loss"]),
                "target": float(r["target"]),
                "momentum": r.get("momentum", ""),
                "chip": r.get("chip_analysis", ""),
                "risk": r.get("risk_factors", ""),
            }
            for r in seen.values()
        ]
        ind_map = _build_industry_map()
        if ind_map:
            _print_by_industry(results, args.top, args.min_confidence, scan_date=show_date, name_map=_build_name_map(), industry_map=ind_map)
        else:
            _print_table(results, args.top, args.min_confidence, scan_date=show_date, name_map=_build_name_map(), sort_by="confidence")
        return

    csv_path: Path | None = None
    if args.save_csv:
        if args.csv_path:
            csv_path = args.csv_path
        else:
            scan_dir = Path(__file__).resolve().parents[1] / "data" / "scans"
            csv_path = scan_dir / f"scan_{args.date}.csv"

    industry_map: dict[str, str] = {}

    if args.tickers:
        tickers = args.tickers
        args.min_confidence = 0  # 指定個股時不設門檻
    else:
        industry_map = _build_industry_map()
        if not industry_map:
            logger.warning("No industry map available; using fallback ticker list")
            tickers = _FALLBACK_TICKERS
        else:
            industry_map_rows = _build_sector_rows(industry_map)
            idx_map = {i: name for i, name, _ in industry_map_rows}

            _is_tty = sys.stdin.isatty()
            if args.sectors:
                # Non-interactive: resolve numeric codes directly (skip menu display)
                chosen = {idx_map[n] for n in args.sectors if n in idx_map}
                if not chosen:
                    _console.print("  [yellow]指定代號無效，使用預設產業[/yellow]")
                    chosen = _DEFAULT_SECTOR_NAMES
            elif _is_tty:
                rows = _sector_menu(industry_map)
                chosen = _select_sectors(rows, _DEFAULT_SECTOR_NAMES)
            else:
                # Non-TTY (e.g. bot subprocess): silently use default sectors
                chosen = _DEFAULT_SECTOR_NAMES
                _console.print(f"  [dim]非互動模式 → 使用預設產業（{len(chosen)} 個）[/dim]")

            tickers = sorted(t for t, ind in industry_map.items() if ind in chosen)
            from collections import Counter
            counts = Counter(ind for t, ind in industry_map.items() if ind in chosen)
            summary = " + ".join(f"{ind}({counts[ind]})" for ind in sorted(chosen))
            _console.print(f"\n[bold]掃描範圍:[/bold] {summary} = [cyan]{len(tickers)}[/cyan] 檔")

    from taiwan_stock_agent.domain.llm_provider import create_llm_provider
    _is_tty = sys.stdin.isatty()
    if args.no_llm:
        llm_provider, llm_top = None, None
    elif args.llm is not None or args.llm_top is not None:
        # 非互動模式：CLI 明確指定
        llm_provider = create_llm_provider(args.llm)
        llm_top = args.llm_top
    elif _is_tty:
        # 互動模式：進入選單
        llm_provider, llm_top = _llm_menu()
    else:
        # Non-TTY (e.g. bot subprocess): auto-detect LLM from env, top 5
        llm_provider = create_llm_provider(None)
        llm_top = 5
        _console.print(f"  [dim]非互動模式 → LLM 自動偵測，前 {llm_top} 名[/dim]")

    # 嘗試載入 BrokerLabelRepository（需要 DATABASE_URL + build-labels 已執行）
    label_repo = _make_label_repo()

    name_map = _build_name_map()
    market_map = _build_market_map()

    run_batch(
        tickers, args.date, args.top, args.min_confidence, args.workers,
        csv_path, llm_provider, llm_top, label_repo,
        industry_map=industry_map,
        save_db=args.save_db,
        name_map=name_map,
        market_map=market_map,
        sort_by=args.sort_by,
    )

    if args.notify and csv_path and csv_path.exists():
        _notify_telegram(csv_path, args.date, args.top, args.min_confidence)


def _tg_escape(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _notify_telegram(csv_path: Path, scan_date, top: int, min_confidence: int) -> None:
    """Read the saved CSV and push the opening list to Telegram."""
    try:
        _do_notify_telegram(csv_path, scan_date, top, min_confidence)
    except Exception as exc:
        import traceback
        _console.print(f"  [red]❌ _notify_telegram 例外：{exc}[/red]")
        _console.print(traceback.format_exc())


def _do_notify_telegram(csv_path: Path, scan_date, top: int, min_confidence: int) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        _console.print("  [yellow]⚠ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未設定，略過推播[/yellow]")
        return

    name_map = _build_name_map()
    signals: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("action") not in ("LONG", "WATCH"):
                continue
            conf = int(row.get("confidence", 0) or 0)
            if conf < min_confidence:
                continue
            if "NO_CATALYST" in (row.get("flags") or ""):
                continue
            signals.append({
                "ticker": row["ticker"],
                "name": name_map.get(row["ticker"], ""),
                "action": row["action"],
                "confidence": conf,
                "trend_score": int(row.get("trend_score", 0) or 0),
                "entry_bid": float(row.get("entry_bid", 0) or 0),
                "target": float(row.get("target", 0) or 0),
                "stop_loss": float(row.get("stop_loss", 0) or 0),
                "flags": row.get("data_quality_flags", ""),
            })

    _console.print(f"  [dim]TG notify: {len(signals)} 筆 LONG/WATCH (min_conf={min_confidence}, top={top})[/dim]")

    if not signals:
        _tg_send(token, chat_id, f"📋 {scan_date} 隔日建倉名單\n目前無 LONG/WATCH 標的")
        return

    long_n  = sum(1 for s in signals if s["action"] == "LONG")
    watch_n = len(signals) - long_n
    # CSV already sorted by trend_score descending from _save_csv
    lines = [
        f"📋 {scan_date} 隔日建倉名單",
        f"🟢 LONG {long_n} 檔  🟡 WATCH {watch_n} 檔  （趨勢強度排序）",
        "",
    ]
    for i, s in enumerate(signals[:top], 1):
        action_icon = "🟢" if s["action"] == "LONG" else "🟡"
        name = s.get("name") or ""
        ticker_name = f"{s['ticker']} {name}" if name else s["ticker"]
        entry  = f"{s['entry_bid']:.1f}" if s["entry_bid"] else "—"
        target = f"{s['target']:.1f}"    if s["target"]    else "—"
        stop   = f"{s['stop_loss']:.1f}" if s["stop_loss"] else "—"
        filled = min(s["trend_score"] // 4, 5)
        trend_bar = "█" * filled + "░" * (5 - filled) + f" {s['trend_score']}"
        upside = (
            f" +{((s['target'] - s['entry_bid']) / s['entry_bid'] * 100):.1f}%"
            if s["entry_bid"] and s["target"] else ""
        )
        key_flags = [
            fl for fl in (s.get("flags") or "").split("|")
            if any(k in fl for k in ("BREAKOUT", "EMERGING", "RISING", "SECTOR_RANK"))
        ]
        flag_str = f"  [{' | '.join(key_flags)}]" if key_flags else ""
        lines.append(
            f"{i}. {action_icon} {ticker_name}\n"
            f"   信心 {s['confidence']}\n"
            f"   進場 {entry} → 目標 {target}{upside}  停損 {stop}{flag_str}"
        )

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n…（已截斷）"

    ok = _tg_send(token, chat_id, msg)
    if ok:
        _console.print(f"  [green]✅ TG 推播成功（{len(signals)} 檔，顯示前 {min(top, len(signals))} 名）[/green]")
    else:
        _console.print("  [red]❌ TG 推播失敗[/red]")


def _tg_send(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as r:
            return r.status == 200
    except Exception as e:
        _console.print(f"  [red]TG error: {e}[/red]")
        return False


if __name__ == "__main__":
    main()
