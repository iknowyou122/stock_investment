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

from taiwan_stock_agent.agents.allocation_advisor import AllocationAdvisor
from taiwan_stock_agent.agents.holdings_manager import (
    DailyPortfolio,
    HoldingsManager,
    NewBuy,
    HoldingSnapshot,
)
from taiwan_stock_agent.infrastructure.holdings_repository import HoldingsRepository
from taiwan_stock_agent.agents.strategist_agent import StrategistAgent
from taiwan_stock_agent.domain.budget_allocator import (
    BudgetAllocator,
    PortfolioAllocation,
    PositionPlan,
)
from taiwan_stock_agent.domain.capital_allocator import (
    CapitalAllocator,
    TIER_COLORS,
    TIER_ORDER,
)
from taiwan_stock_agent.domain.refined_picks import RefinedPickFilter
from taiwan_stock_agent.domain.sector_flow import (
    ConceptFlowAnalyzer,
    SectorFlowAnalyzer,
    TREND_META,
    sparkline_svg,
)
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
from taiwan_stock_agent.infrastructure.ohlcv_repository import OHLCVRepository
from taiwan_stock_agent.infrastructure.paid_data_fetcher import PaidDataFetcher
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
    """Load ticker→company name map from daily cache, falling back to most recent available."""
    name_cache = _CACHE_DIR / f"name_map_{date.today()}.json"
    if name_cache.exists():
        try:
            data = json.loads(name_cache.read_text())
            if data:
                return data
        except Exception:
            pass
    # Fall back to most recent available cache
    candidates = sorted(_CACHE_DIR.glob("name_map_*.json"), reverse=True)
    for f in candidates:
        try:
            data = json.loads(f.read_text())
            if data:
                return data
        except Exception:
            continue
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
            r["confidence"] = r["confidence"] + bonus
            r["flags"] = list(r.get("flags") or []) + [f"SECTOR_RANK:{rank}/{total}"]
            boosted += 1

    return boosted


def _load_growth_index() -> dict[str, dict]:
    """Load the most recent growth scan results as a ticker→record dict.

    Returns empty dict if no file found (graceful degradation).
    """
    import glob as _glob
    pattern = str(Path(__file__).resolve().parents[1] / "data" / "growth" / "growth_*.json")
    files = sorted(_glob.glob(pattern), reverse=True)
    if not files:
        return {}
    try:
        import json as _json
        data = _json.loads(Path(files[0]).read_text())
        return {r["ticker"]: r for r in data.get("records", [])}
    except Exception:
        return {}


def _apply_growth_bonus(results: list[dict], growth_index: dict[str, dict]) -> int:
    """Boost stocks that appear in the monthly revenue growth scan.

    Tiers (applied to non-halted, non-error stocks only):
      YoY ≥ 50%                   → +8 pts  (GROWTH_HIGH flag)
      YoY ≥ 30%                   → +5 pts  (GROWTH_MID flag)
      YoY ≥ 20%                   → +3 pts  (GROWTH_LOW flag)
      consecutive months ≥ 3      → additional +2 pts (GROWTH_CONSEC flag)

    Returns count of stocks boosted.
    """
    if not growth_index:
        return 0

    boosted = 0
    for r in results:
        if r.get("halt") or r.get("error") is not None:
            continue
        rec = growth_index.get(r["ticker"])
        if not rec:
            continue
        yoy = rec.get("yoy_pct") or 0.0
        if yoy >= 50:
            bonus, flag = 8, "GROWTH_HIGH"
        elif yoy >= 30:
            bonus, flag = 5, "GROWTH_MID"
        elif yoy >= 20:
            bonus, flag = 3, "GROWTH_LOW"
        else:
            continue

        consecutive = rec.get("consecutive", 0) or 0
        consec_bonus = 2 if consecutive >= 3 else 0

        r["confidence"] = r["confidence"] + bonus + consec_bonus
        flags = list(r.get("flags") or [])
        flags.append(f"{flag}:YoY{yoy:.0f}%")
        if consec_bonus:
            flags.append(f"GROWTH_CONSEC:{consecutive}M")
        r["flags"] = flags
        r["growth_yoy"] = yoy
        r["growth_consecutive"] = consecutive
        boosted += 1

    return boosted


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
            "  [yellow]  → P75–P95 差距 < 10pts，因子可能過度聚集，建議 make factor-report 重新校準[/yellow]"
        )


# ── Institutional Momentum Score ────────────────────────────────────────────

_IMS_EARLY_TYPES = frozenset(["法人建倉", "籌碼轉移", "VCP", "旗形"])


def _compute_ims(r: dict) -> float:
    """Institutional Momentum Score — weighted composite of smart-money accumulation signals.

    Higher score = institutional players are quietly building a position before breakout.
    Early-accumulation signal types get +5 bonus since they are the primary target.

    Weights calibrated from 5/29 T+1 flag analysis (n=128 signals, 75.8% win rate):
      OBV_STEALTH 100% / DMI_FRESH_CROSS 100% / INST_MOMENTUM 96.3% / COILING_GATE_PASS 90%
      RS_LEADER 57.4% / TREND_WALK 67.3% / MOMENTUM_TRACK 66.7%
    """
    flags = set(r.get("flags", []))
    early_bonus = 5.0 if r.get("signal_type") in _IMS_EARLY_TYPES else 0.0

    # Flag-based bonuses for high T+1 win-rate flags
    flag_bonus = 0.0
    if "INST_MOMENTUM" in flags:
        flag_bonus += 3.0
    if "COILING_GATE_PASS" in flags:
        flag_bonus += 3.0
    if "DMI_FRESH_CROSS" in flags:
        flag_bonus += 2.0

    # Flag-based penalties for low T+1 win-rate flags
    flag_penalty = 0.0
    if "TREND_WALK" in flags:
        flag_penalty += 2.0
    if "MOMENTUM_TRACK" in flags:
        flag_penalty += 2.0
    if "RS_LEADER" in flags:
        flag_penalty += 1.5

    return (
        r.get("stealth_accum_composite_pts", 0.0) * 2.5
        + r.get("inst_synergy_pts", 0.0) * 2.0
        + r.get("foreign_trend_pts", 0.0) * 1.5
        + r.get("large_2w_trend_pts", 0.0) * 1.5
        + r.get("chip_cleanliness_pts", 0.0) * 1.0
        + r.get("inst_accel_3d_pts", 0.0) * 1.0
        + r.get("vol_asymmetry_pts", 0.0) * 1.0
        + r.get("obv_stealth_pts", 0.0) * 2.5  # boosted: 100% T+1 win rate
        + early_bonus
        + flag_bonus
        - flag_penalty
    )


def _ims_bar(ims: float) -> str:
    """Visual IMS bar scaled 0–60 → 0–10 blocks."""
    filled = max(0, min(10, round(ims / 6.0)))
    bar = "▮" * filled + "▯" * (10 - filled)
    if ims >= 30:
        color = "bright_magenta"
    elif ims >= 15:
        color = "magenta"
    else:
        color = "dim"
    return f"[{color}]{bar}[/{color}] [dim]{ims:.0f}[/dim]"


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


def _load_recent_db(
    analysis_date: date,
    lookback: int = 3,
    min_conf: int = 40,
) -> list[dict[str, int]]:
    """Query signal_outcomes for last N trading days before analysis_date.

    Returns [{ticker: confidence}, ...] ordered old→new.
    Falls back to empty list when DB is unavailable.
    """
    import os
    if not os.environ.get("DATABASE_URL"):
        return []
    try:
        from taiwan_stock_agent.infrastructure.db import get_connection, init_pool
        init_pool()
    except Exception:
        return []

    cutoff = analysis_date - timedelta(days=lookback * 2 + 7)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT signal_date, ticker, confidence_score
                    FROM signal_outcomes
                    WHERE signal_date >= %s
                      AND signal_date < %s
                      AND source = 'live'
                      AND confidence_score >= %s
                    ORDER BY signal_date
                    """,
                    (cutoff, analysis_date, min_conf),
                )
                rows = cur.fetchall()
    except Exception:
        return []

    by_date: dict[date, dict[str, int]] = {}
    for sig_date, ticker, conf in rows:
        if isinstance(sig_date, str):
            sig_date = date.fromisoformat(sig_date)
        by_date.setdefault(sig_date, {})[ticker] = conf

    sorted_dates = sorted(d for d in by_date if d < analysis_date)
    recent_dates = sorted_dates[-lookback:]
    return [by_date[d] for d in recent_dates]


def _apply_persistence_bonus(
    results: list[dict],
    analysis_date: date,
    data_dir: Path,
    min_prev_conf: int = 50,
) -> int:
    """Trajectory-aware persistence bonus.

    Queries signal_outcomes for the last 3 trading days and computes per-ticker
    score trajectory:
      RISING   (3 consecutive days, each score higher than previous) → +7 pts
      STABLE   (appeared yesterday with score ≥ min_prev_conf)      → +5 pts
      DECLINING (appeared yesterday but score dropped > 5 pts)       → +0 pts

    Adds PERSIST_RISING / PERSIST_STABLE flag to boosted stocks.
    Returns count of stocks boosted.
    """
    recent = _load_recent_db(analysis_date, lookback=3, min_conf=40)
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

        r["confidence"] = r["confidence"] + bonus
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
    recent = _load_recent_db(analysis_date, lookback=1, min_conf=40)
    yesterday_tickers: set[str] = set(recent[0].keys()) if recent else set()

    boosted = 0
    for r in results:
        if r.get("halt") or r.get("error") is not None:
            continue
        if r["ticker"] in yesterday_tickers:
            continue
        if r.get("proximity_pts", 0) >= 11.5:  # near-max proximity band (92-99% of 20d high); see _proximity_score()
            r["confidence"] = r["confidence"] + 4
            r["flags"] = list(r.get("flags") or []) + ["NEAR_HIGH_COIL"]
            boosted += 1

    return boosted


def _load_concept_ticker_map(rank_pct_threshold: float = 70.0) -> dict[str, int]:
    """Returns {ticker: n_hot_concepts} for the latest concept heat snapshot.

    Hot concept = rank_pct >= rank_pct_threshold in the snapshot.
    Returns {} if no snapshot or concepts.json found.
    """
    import json as _json
    concept_files = sorted(_HEAT_DIR.glob("concept_heat_*.json"))
    if not concept_files:
        return {}
    try:
        with open(concept_files[-1], encoding="utf-8") as f:
            cd = _json.load(f)
        hot_keys = {
            k for k, v in cd.get("concepts", {}).items()
            if v.get("rank_pct", 0) >= rank_pct_threshold
        }
        if not hot_keys:
            return {}
        concepts_path = _ROOT_PATH / "config" / "concepts.json"
        if not concepts_path.exists():
            return {}
        with open(concepts_path, encoding="utf-8") as f:
            cdefs_raw = _json.load(f)
        cdefs = cdefs_raw.get("concepts", cdefs_raw)
        hot_count: dict[str, int] = {}
        for ck, cdef in cdefs.items():
            if ck in hot_keys:
                for t in cdef.get("tickers", []):
                    hot_count[t] = hot_count.get(t, 0) + 1
        return hot_count
    except Exception:
        return {}


def _apply_concept_heat_bonus(results: list[dict]) -> int:
    """Boost stocks in hot concept baskets.

    +3 pts for 1 hot concept basket, +5 pts for 2+ baskets.
    Hot = rank_pct >= 70 in the latest concept heat snapshot.
    Adds CONCEPT_HEAT flag; skips halted/error results.
    Returns count of stocks boosted.
    """
    ticker_hot = _load_concept_ticker_map()
    if not ticker_hot:
        return 0
    n = 0
    for r in results:
        if r.get("halt") or r.get("error") is not None:
            continue
        count = ticker_hot.get(r["ticker"], 0)
        if count == 0:
            continue
        bonus = 5 if count >= 2 else 3
        r["confidence"] = r["confidence"] + bonus
        tag = f"+{bonus}({'2題材+' if count >= 2 else '1題材'})"
        r["flags"] = list(r.get("flags") or []) + [f"CONCEPT_HEAT:{tag}"]
        n += 1
    return n


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


def _classify_tce_signal_type(flags: list[str]) -> tuple[str, str]:
    """Return (signal_type_zh, horizon_zh) based on TCE flags.

    signal_type: '趨勢延伸' | '蓄積★' | '蓄積'
    horizon:     '波段' (all TCE signals are medium-term)
    """
    if "TREND_WALK" in flags:
        return "趨勢延伸", "波段"
    if "COILING_PRIME" in flags:
        return "蓄積★", "波段"
    return "蓄積", "波段"


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
        _sig_type, _horizon = _classify_tce_signal_type(signal.data_quality_flags)
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
            # IMS (Institutional Momentum Score) component fields
            "stealth_accum_composite_pts": breakdown_pts.get("stealth_accum_composite_pts", 0.0),
            "inst_synergy_pts": breakdown_pts.get("inst_synergy_pts", 0.0),
            "foreign_trend_pts": breakdown_pts.get("foreign_trend_pts", 0.0),
            "vol_asymmetry_pts": breakdown_pts.get("vol_asymmetry_pts", 0.0),
            "chip_cleanliness_pts": breakdown_pts.get("chip_cleanliness_pts", 0.0),
            "large_2w_trend_pts": breakdown_pts.get("large_2w_trend_pts", 0.0),
            "inst_accel_3d_pts": breakdown_pts.get("inst_accel_3d_pts", 0.0),
            "obv_stealth_pts": breakdown_pts.get("obv_stealth_pts", 0.0),
            "signal_type": _sig_type,
            "horizon": _horizon,
            "secondary_types": [],
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
            "institution_continuity_pts": 0,
            "proximity_pts": 0,
            "signal_type": "蓄積",
            "horizon": "波段",
            "secondary_types": [],
        }


def _run_surge_inline(
    tickers: list[str],
    analysis_date: date,
    market_map: dict[str, str] | None = None,
) -> None:
    """Run SurgeRadar scan on the plan's tickers and write results to DB.

    Uses quiet=True to suppress surge's own terminal table — results are
    read back via _load_surge_from_db() and merged into the unified output.
    Silently skips if surge_scan import fails.
    """
    import importlib.util
    scripts_dir = Path(__file__).parent
    spec = importlib.util.spec_from_file_location(
        "_surge_scan_mod", scripts_dir / "surge_scan.py"
    )
    if spec is None or spec.loader is None:
        return
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.run_surge_scan(
            tickers,
            analysis_date,
            market_map=market_map or {},
            no_html=True,
            notify=False,
            quiet=True,
        )
    except Exception as _e:
        _console.print(f"  [dim yellow]⚠ Surge inline scan 失敗，略過: {_e}[/dim yellow]")


_LIQUIDITY_FLOOR_TWD = 8_000_000.0  # NT$/day — mirrors TCE G3 gate (Phase 4.41)


def _passes_liquidity_floor(history: list) -> bool:
    """Match the TCE Gate-3 liquidity check used in StrategistAgent.

    Looks at the last 20 trading days' avg dollar volume and rejects under
    NT$ 8M to prevent low-liquidity tickers from bypassing TCE via the
    pullback / early-accumulation paths.
    """
    if len(history) < 20:
        return False
    last20 = sorted(history, key=lambda x: x.trade_date)[-20:]
    total = sum(float(b.close) * float(b.volume) for b in last20)
    return (total / 20.0) >= _LIQUIDITY_FLOOR_TWD


def _scan_pullback_batch(
    tickers: list[str],
    analysis_date: date,
    agent: "StrategistAgent",
    market_map: dict[str, str] | None = None,
    min_score: int = 40,
) -> list[dict]:
    """Run PullbackDetector on all tickers.

    Uses L2 DB cache (OHLCVRepository) — hits the DB before falling back to API.
    Returns result dicts in the same shape as _scan_one for qualifying stocks only.
    """
    from taiwan_stock_agent.domain.pullback_detector import PullbackDetector

    detector = PullbackDetector()
    results: list[dict] = []

    for ticker in tickers:
        try:
            ohlcv_df = agent._finmind.fetch_ohlcv(
                ticker,
                analysis_date - timedelta(days=130),
                analysis_date,
            )
            history = StrategistAgent._df_to_ohlcv_list(ohlcv_df, ticker)
            if not history:
                continue
            if not _passes_liquidity_floor(history):
                continue
            det = detector.score(history)
            if det is None or det["score"] < min_score:
                continue
            sorted_h = sorted(history, key=lambda x: x.trade_date)
            close = float(sorted_h[-1].close) if sorted_h else 0.0
            ma20 = det["ma20"]
            results.append({
                "ticker": ticker,
                "action": "LONG",
                "confidence": det["score"],
                "halt": False,
                "free_tier": None,
                "flags": det["flags"],
                "entry_bid": round(close * 0.997, 1),
                "stop_loss": round(ma20 * 0.97, 1),
                "target": round(close * 1.08, 1),
                "verdict": f"趨勢回調至 MA20（±{abs(det['ma20_pct']):.1f}%）",
                "position": "",
                "momentum": "",
                "chip": "",
                "risk": "",
                "elapsed": 0.0,
                "error": None,
                "_signal": None,
                "trend_score": 0,
                "institution_continuity_pts": 0,
                "proximity_pts": 0,
                "signal_type": "回調",
                "horizon": "波段",
                "secondary_types": [],
                "change_pct": 0.0,
            })
        except Exception:
            continue

    return results


def _load_surge_from_db(analysis_date: date, min_score: int = 50) -> list[dict]:
    """Load surge signals from surge_signals DB for analysis_date.

    Normalises each row to the same result-dict shape as _scan_one so they
    can be merged with TCE and pullback results without special-casing.
    Returns empty list if DB unavailable or no records.
    """
    import json as _json
    from taiwan_stock_agent.infrastructure.surge_recorder import query_surge_signals

    surge_rows = query_surge_signals(analysis_date, min_score=min_score)
    results: list[dict] = []
    for s in surge_rows:
        grade = s.get("grade", "")
        sig_type = "爆量★" if grade == "SURGE_ALPHA" else "爆量"
        flags = s.get("flags") or []
        if isinstance(flags, str):
            try:
                flags = _json.loads(flags)
            except Exception:
                flags = []
        close = float(s.get("close_price") or 0.0)
        results.append({
            "ticker": s["ticker"],
            "action": "LONG",
            "confidence": int(s.get("score") or 0),
            "halt": False,
            "free_tier": None,
            "flags": flags,
            "entry_bid": round(close * 0.997, 1),
            "stop_loss": round(close * 0.97, 1),
            "target": round(close * 1.05, 1),
            "verdict": f"{sig_type}  Vol×{s.get('vol_ratio', 0):.1f}",
            "position": "",
            "momentum": "",
            "chip": "",
            "risk": "",
            "elapsed": 0.0,
            "error": None,
            "_signal": None,
            "trend_score": 0,
            "institution_continuity_pts": 0,
            "proximity_pts": 0,
            "signal_type": sig_type,
            "horizon": "短線",
            "secondary_types": [],
            "change_pct": float(s.get("day_chg_pct") or 0.0),
        })
    return results


def _early_accum_analysis(det: dict, proxy=None) -> tuple[str, str, str, str]:
    """Generate verdict / position / momentum / chip from detector output (no LLM).

    Returns (verdict, position, momentum, chip).
    """
    sig = det.get("signal_type", "提前佈局")
    flags: list[str] = det.get("flags", [])
    flag_set = set(flags)

    # ── InstAccumDetector ─────────────────────────────────────────────
    if "INST_ACCUM" in flag_set:
        consec = det.get("consec_days", 0)
        dist = det.get("distance_pct", 0)
        verdict = f"法人悄悄建倉：距60日高點 -{dist:.0f}%，連買 {consec} 天，尚未引起市場注意"
        position = f"距高點 -{dist:.0f}%，有充足上漲空間；等待量能回升或突破短期均線確認"
        momentum = "股價仍在整理中，MA 尚未完全多頭排列；耐心等待催化劑"
        chip_parts = []
        if consec >= 5:
            chip_parts.append(f"法人連買 {consec} 天（強烈意圖）")
        else:
            chip_parts.append(f"法人連買 {consec} 天")
        if "VOL_DRYUP_STRONG" in flag_set:
            chip_parts.append("成交量極度萎縮（籌碼乾淨）")
        elif "VOL_DRYUP" in flag_set:
            chip_parts.append("成交量縮減")
        if "LARGE_HOLDER_ACCUM" in flag_set:
            chip_parts.append("大戶持股增加")
        chip = "；".join(chip_parts)
        return verdict, position, momentum, chip

    # ── ChipTransferDetector ──────────────────────────────────────────
    if "CHIP_TRANSFER" in flag_set:
        signals: list[str] = det.get("signals_met", [])
        price_rng = det.get("price_range_pct", 0)
        vol_ratio = det.get("vol_ratio", 1.0)
        margin_streak = det.get("margin_streak", 0)
        consec = det.get("consec_days", 0)
        signal_desc = []
        if "A" in signals and margin_streak:
            signal_desc.append(f"融資連降 {margin_streak} 天")
        if "B" in signals:
            signal_desc.append(f"股價穩定（20日振幅僅 {price_rng:.1f}%）")
        if "C" in signals and consec:
            signal_desc.append(f"法人連買 {consec} 天")
        if "D" in signals:
            signal_desc.append("大戶持股增加")
        if "E" in signals:
            signal_desc.append("散戶持股下降")
        verdict = "散戶出、法人進：" + "，".join(signal_desc[:3])
        position = f"股價平台整理中（{price_rng:.1f}%）；散戶信心減弱卻是法人低調建倉良機"
        momentum = "尚無明顯突破訊號，屬蓄積期；動能訊號出現前保持觀察"
        chip = "；".join(signal_desc) + f"；量縮比 {vol_ratio:.2f}x"
        return verdict, position, momentum, chip

    # ── VCPDetector ───────────────────────────────────────────────────
    if "VCP" in flag_set:
        n_c = det.get("contractions", 2)
        pb = det.get("latest_pullback_pct", 0)
        dist = det.get("dist_from_trough_pct", 0)
        verdict = f"Minervini VCP：{n_c} 次回調幅度遞減，最新回調僅 {pb:.1f}%，波動率持續收斂"
        position = f"目前距最新低點 +{dist:.1f}%，仍在底部建倉區；量縮期為最佳進場窗口"
        bb_tight = "BB 極度壓縮（動能即將釋放）" if "BB_VERY_TIGHT" in flag_set else ("BB 偏緊" if "BB_TIGHT" in flag_set else "")
        momentum = f"{'；'.join(filter(None, ['波動率收縮至低點', bb_tight]))}；等待放量突破確認"
        chip_parts = []
        for f in flags:
            if f.startswith("TROUGH_VOL_DRYUP"):
                chip_parts.append("每次回調量能遞減（主力未出貨）")
            elif f.startswith("TROUGH_VOL_LOW"):
                chip_parts.append("回調量能偏低")
        if "MA_ALIGNED" in flag_set:
            chip_parts.append("均線多頭排列")
        chip = "；".join(chip_parts) if chip_parts else "量縮回調中"
        return verdict, position, momentum, chip

    # ── HTFDetector ───────────────────────────────────────────────────
    if "HTF" in flag_set:
        adv = det.get("prior_advance_pct", 0)
        rng = det.get("consolidation_range_pct", 0)
        vr = det.get("vol_ratio", 1.0)
        fw = det.get("flag_window", 0)
        verdict = f"高緊旗形：急漲 {adv:.0f}% 後低量整理 {fw} 天，旗形振幅僅 {rng:.1f}%"
        position = f"整理 {fw} 天後動能保留；突破旗形上緣即為進場點"
        tight = "FLAG_TIGHT" in flag_set or "FLAG_MOD" in flag_set
        momentum = f"量縮 {vr:.2f}x，{'旗形極緊（爆發力強）' if rng < 8 else '旗形整理中'}；靜待放量突破"
        chip = f"量縮比 {vr:.2f}x（整理期主力未出貨）；旗形寬度 {rng:.1f}%"
        if "MA_ALIGNED" in flag_set:
            chip += "；均線多頭排列"
        return verdict, position, momentum, chip

    # ── PullbackDetector (fallback if called from here) ───────────────
    if "PULLBACK_MA20" in flag_set:
        ma20_pct = det.get("ma20_pct", 0)
        rsi = det.get("rsi", 50)
        pb_days = det.get("pullback_days", 0)
        verdict = f"回調至 MA20 支撐（{ma20_pct:+.1f}%），RSI 冷卻至 {rsi:.0f}，{pb_days} 天回調"
        position = "均線多頭排列，回調至 MA20 為中繼買點；止損設 MA20 下方 3%"
        bounce = "今日出現陽線反彈" if "BOUNCE_CANDLE" in flag_set else "等待反彈訊號確認"
        momentum = f"RSI {rsi:.0f} 已從高位冷卻；{bounce}"
        if "VOL_CONTRACTION_STRONG" in flag_set:
            chip = "量縮整理（籌碼穩定）"
        elif "VOL_CONTRACTION" in flag_set:
            chip = "成交量收縮"
        else:
            chip = "量能中性"
        return verdict, position, momentum, chip

    return f"{sig} 訊號", "", "", ""


def _scan_early_accum_batch(
    tickers: list[str],
    analysis_date: date,
    agent: "StrategistAgent",
    market_map: dict[str, str] | None = None,
    min_score: int = 45,
) -> list[dict]:
    """Run InstAccumDetector, ChipTransferDetector, VCPDetector, HTFDetector on all tickers.

    Returns result dicts in the same shape as _scan_pullback_batch.
    """
    from taiwan_stock_agent.domain.inst_accum_detector import InstAccumDetector
    from taiwan_stock_agent.domain.chip_transfer_detector import ChipTransferDetector
    from taiwan_stock_agent.domain.vcp_detector import VCPDetector
    from taiwan_stock_agent.domain.htf_detector import HTFDetector

    inst_det = InstAccumDetector()
    chip_det = ChipTransferDetector()
    vcp_det = VCPDetector()
    htf_det = HTFDetector()

    results: list[dict] = []

    for ticker in tickers:
        try:
            ohlcv_df = agent._finmind.fetch_ohlcv(
                ticker,
                analysis_date - timedelta(days=130),
                analysis_date,
            )
            history = StrategistAgent._df_to_ohlcv_list(ohlcv_df, ticker)
            if not history:
                continue
            if not _passes_liquidity_floor(history):
                continue
            sorted_h = sorted(history, key=lambda x: x.trade_date)
            close = float(sorted_h[-1].close) if sorted_h else 0.0

            # Try to get proxy (best-effort)
            proxy = None
            try:
                proxy = agent._chip_proxy.fetch(ticker, analysis_date)
            except Exception:
                pass

            # Run each detector; take the highest scorer if multiple fire
            candidates: list[tuple[int, dict]] = []

            det = inst_det.score(history, proxy)
            if det is not None and det["score"] >= min_score:
                candidates.append((det["score"], det))

            det = chip_det.score(history, proxy)
            if det is not None and det["score"] >= min_score:
                candidates.append((det["score"], det))

            det = vcp_det.score(history)
            if det is not None and det["score"] >= min_score:
                candidates.append((det["score"], det))

            det = htf_det.score(history)
            if det is not None and det["score"] >= min_score:
                candidates.append((det["score"], det))

            if not candidates:
                continue

            # Pick highest-confidence
            candidates.sort(key=lambda x: -x[0])
            best_score, best = candidates[0]
            secondary = [c[1]["signal_type"] for c in candidates[1:] if c[1]["signal_type"] != best["signal_type"]]

            from statistics import mean as _mean
            closes = [d.close for d in sorted_h]
            ma20 = _mean(closes[-20:]) if len(closes) >= 20 else close

            _verdict, _position, _momentum, _chip = _early_accum_analysis(best, proxy)

            results.append({
                "ticker": ticker,
                "action": "WATCH",
                "confidence": best_score,
                "halt": False,
                "free_tier": None,
                "flags": best.get("flags", []),
                "entry_bid": round(close * 0.997, 1),
                "stop_loss": round(ma20 * 0.97, 1),
                "target": round(close * 1.10, 1),
                "verdict": _verdict,
                "position": _position,
                "momentum": _momentum,
                "chip": _chip,
                "risk": "提前佈局訊號，尚未突破，持倉前等待量能確認",
                "elapsed": 0.0,
                "error": None,
                "_signal": None,
                "trend_score": 0,
                "institution_continuity_pts": 0,
                "proximity_pts": 0,
                # IMS fields — zeroed for early accum results (signal_type bonus handles ranking)
                "stealth_accum_composite_pts": 0.0,
                "inst_synergy_pts": 0.0,
                "foreign_trend_pts": 0.0,
                "vol_asymmetry_pts": 0.0,
                "chip_cleanliness_pts": 0.0,
                "large_2w_trend_pts": 0.0,
                "inst_accel_3d_pts": 0.0,
                "obv_stealth_pts": 0.0,
                "signal_type": best["signal_type"],
                "horizon": best.get("horizon", "波段"),
                "secondary_types": secondary,
                "change_pct": 0.0,
            })
        except Exception:
            continue

    return results


def _merge_unified_signals(
    tce_results: list[dict],
    pullback_results: list[dict],
    surge_results: list[dict],
    early_results: list[dict] | None = None,
) -> list[dict]:
    """Merge TCE, pullback, surge, and early accumulation results into one unified list.

    Deduplicates by ticker: keeps highest-confidence result as primary;
    appends other signal_types to secondary_types list.
    Halted/error TCE results are replaced if a valid signal exists for that ticker.
    """
    merged: dict[str, dict] = {r["ticker"]: r for r in tce_results}

    all_others = [*pullback_results, *surge_results, *(early_results or [])]
    for r in all_others:
        ticker = r["ticker"]
        new_conf = r.get("confidence", 0)
        if ticker not in merged:
            merged[ticker] = r
        else:
            existing = merged[ticker]
            if existing.get("halt") or existing.get("error"):
                merged[ticker] = r
            elif new_conf > existing.get("confidence", 0):
                existing_type = existing.get("signal_type", "")
                merged[ticker] = r
                if existing_type and existing_type not in r.get("secondary_types", []):
                    merged[ticker].setdefault("secondary_types", []).append(existing_type)
            else:
                new_type = r.get("signal_type", "")
                if new_type and new_type not in existing.get("secondary_types", []):
                    existing.setdefault("secondary_types", []).append(new_type)

    return list(merged.values())


CSV_FIELDS = [
    "scan_date", "analysis_date", "ticker", "action", "confidence", "trend_score",
    "free_tier", "halt", "entry_bid", "stop_loss", "target",
    "momentum", "chip_analysis", "risk_factors", "data_quality_flags",
]




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


def _conf_bar(conf: float) -> str:
    filled = max(0, min(10, round(conf / 10)))
    bar = "█" * filled + "░" * (10 - filled)
    if conf >= 70:
        color = "green"
    elif conf >= 50:
        color = "yellow"
    else:
        color = "red"
    return f"[{color}]{bar}[/{color}] [dim]{conf:.1f}[/dim]"


def _trend_bar(ts: int) -> str:
    if ts >= 25:
        color = "green"
    elif ts >= 15:
        color = "yellow"
    else:
        color = "dim"
    return f"[{color}]{ts}[/{color}][dim]/37[/dim]"


_SIG_COLORS = {
    "爆量★": "bold bright_red",
    "爆量": "red",
    "回調": "bright_yellow",
    "趨勢延伸": "bright_cyan",
    "蓄積★": "bright_green",
    "蓄積": "cyan",
    "法人建倉": "bold bright_magenta",
    "籌碼轉移": "bold magenta",
    "VCP": "bold bright_cyan",
    "旗形": "bold yellow",
}


def _make_signal_cells(r: dict) -> tuple[str, str, str]:
    """Return (sig_cell, horizon_cell, fund_cell) Rich markup strings for a result row."""
    sig_type = r.get("signal_type", "蓄積")
    secondary = r.get("secondary_types") or []
    secondary_str = f"\n[dim]+{secondary[0]}[/dim]" if secondary else ""
    sig_color = _SIG_COLORS.get(sig_type, "white")
    sig_cell = f"[{sig_color}]{sig_type}[/{sig_color}]{secondary_str}"

    horizon = r.get("horizon", "波段")
    horizon_color = "red" if horizon == "短線" else "cyan"
    horizon_cell = f"[{horizon_color}]{horizon}[/{horizon_color}]"

    yoy = r.get("growth_yoy")
    consec = r.get("growth_consecutive", 0)
    if yoy:
        consec_str = f" 連{consec}M" if consec >= 3 else ""
        fund_cell = f"[bright_green]★ +{yoy:.0f}%{consec_str}[/bright_green]"
    else:
        fund_cell = "[dim]—[/dim]"

    return sig_cell, horizon_cell, fund_cell


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
    table.add_column("型態", width=10)
    table.add_column("持倉", width=7)
    table.add_column("Action", width=10)
    table.add_column("Confidence", width=18)
    table.add_column("Entry", justify="right", style="cyan", width=9)
    table.add_column("Stop", justify="right", style="red", width=9)
    table.add_column("Target", justify="right", style="green", width=9)
    table.add_column("Upside", justify="right", style="yellow", width=7)
    table.add_column("基本面", width=14)

    for i, r in enumerate(valid, 1):
        action_str = r["action"] + ("*" if r["free_tier"] else "")

        action_text = Text.from_markup(f"[{_action_style(r['action'])}]{action_str}[/{_action_style(r['action'])}]")

        # Signal type badge
        sig_cell, horizon_cell, fund_cell = _make_signal_cells(r)

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
            sig_cell,
            horizon_cell,
            action_text,
            _conf_bar(r["confidence"]),
            f"{r['entry_bid']:.1f}",
            f"{r['stop_loss']:.1f}",
            f"{r['target']:.1f}",
            f"{upside_pct:+.1f}%",
            fund_cell,
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

        ind_table = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="bold dim",
            show_lines=False,
            padding=(0, 1),
        )
        ind_table.add_column("Ticker", style="bold white", width=11)
        ind_table.add_column("型態", width=10)
        ind_table.add_column("持倉", width=7)
        ind_table.add_column("Action", width=10)
        ind_table.add_column("Confidence", width=18)
        ind_table.add_column("Entry", justify="right", style="cyan", width=9)
        ind_table.add_column("Stop", justify="right", style="red", width=9)
        ind_table.add_column("Target", justify="right", style="green", width=9)
        ind_table.add_column("Upside", justify="right", style="yellow", width=7)
        ind_table.add_column("基本面", width=14)

        for s in stocks:
            ticker = s["ticker"]
            short_name = name_m.get(ticker, "")
            ticker_cell = f"{ticker}\n[dim]{short_name}[/dim]" if short_name else ticker

            action_str = s["action"] + ("*" if s.get("free_tier") else "")
            action_text = Text.from_markup(f"[{_action_style(s['action'])}]{action_str}[/{_action_style(s['action'])}]")

            sig_cell, horizon_cell, fund_cell = _make_signal_cells(s)

            entry_bid = s.get("entry_bid", 0)
            stop_loss = s.get("stop_loss", 0)
            target = s.get("target", 0)
            upside_pct = (target / entry_bid - 1) * 100 if entry_bid > 0 else 0

            ind_table.add_row(
                ticker_cell,
                sig_cell,
                horizon_cell,
                action_text,
                _conf_bar(s["confidence"]),
                f"{entry_bid:.1f}",
                f"{stop_loss:.1f}",
                f"{target:.1f}",
                f"{upside_pct:+.1f}%",
                fund_cell,
            )

        _console.print(ind_table)

    if top and len(valid) > top:
        _console.print(f"\n[dim]  (顯示前 {top} 檔，共 {len(valid)} 檔符合條件)[/dim]")


def _print_focus_list(
    results: list[dict],
    top_conviction: int,
    top_watchlist: int,
    min_confidence: float,
    scan_date: str = "",
    name_map: dict[str, str] | None = None,
) -> None:
    """Two-tier focused output: CONVICTION (IMS-ranked top 10) + WATCHLIST (conf-ranked top 20).

    CONVICTION = highest Institutional Momentum Score — surfaces quiet accumulation before breakout.
    WATCHLIST  = remaining valid results sorted by overall confidence score.
    Use --by-industry to get the legacy industry-grouped view instead.
    """
    from collections import defaultdict as _defaultdict

    valid = [
        r for r in results
        if not r.get("halt") and r.get("error") is None
        and r.get("confidence", 0) >= min_confidence
        and "NO_CATALYST" not in (r.get("flags") or [])
    ]
    if not valid:
        _console.print(Panel(
            f"[dim]無符合條件的標的 (min_confidence={min_confidence})[/dim]",
            border_style="yellow",
        ))
        return

    # Compute IMS for all valid candidates
    for r in valid:
        r["_ims"] = _compute_ims(r)

    # CONVICTION: top N by IMS (ties broken by confidence)
    conviction_pool = sorted(valid, key=lambda r: (r["_ims"], r["confidence"]), reverse=True)
    conviction = conviction_pool[:top_conviction]
    conviction_tickers = {r["ticker"] for r in conviction}

    # WATCHLIST: remaining valid sorted by confidence
    watchlist_pool = [r for r in valid if r["ticker"] not in conviction_tickers]
    watchlist_pool.sort(key=lambda r: r["confidence"], reverse=True)
    watchlist = watchlist_pool[:top_watchlist]

    name_m = name_map or {}

    def _row(r: dict) -> tuple:
        ticker = r["ticker"]
        short = name_m.get(ticker, "")
        ticker_cell = f"{ticker}\n[dim]{short}[/dim]" if short else ticker
        sig_cell, horizon_cell, fund_cell = _make_signal_cells(r)
        action_str = r["action"] + ("*" if r.get("free_tier") else "")
        action_cell = Text.from_markup(
            f"[{_action_style(r['action'])}]{action_str}[/{_action_style(r['action'])}]"
        )
        entry = r.get("entry_bid", 0.0)
        stop  = r.get("stop_loss", 0.0)
        tgt   = r.get("target", 0.0)
        up    = (tgt / entry - 1) * 100 if entry > 0 else 0.0
        return (
            ticker_cell, sig_cell, horizon_cell, action_cell,
            _conf_bar(r["confidence"]),
            _ims_bar(r["_ims"]),
            f"{entry:.1f}", f"{stop:.1f}", f"{tgt:.1f}", f"{up:+.1f}%",
            fund_cell,
        )

    _COLS = [
        ("Rank",       "center", 5),
        ("Ticker",     "left",  11),
        ("型態",       "left",  10),
        ("持倉",       "left",   7),
        ("Action",     "left",  10),
        ("Confidence", "left",  18),
        ("IMS 動能",   "left",  18),
        ("Entry",      "right",  9),
        ("Stop",       "right",  9),
        ("Target",     "right",  9),
        ("Upside",     "right",  7),
        ("基本面",     "left",  14),
    ]

    date_str = f"  {scan_date}" if scan_date else ""

    # ── CONVICTION section ─────────────────────────────────────────────────
    if conviction:
        _console.print(
            f"\n[bold bright_magenta]▶ CONVICTION{date_str}  "
            f"法人動能最強 {len(conviction)} 檔[/bold bright_magenta]"
            f"  [dim]IMS 由高→低排序[/dim]"
        )
        ct = Table(
            box=box.ROUNDED, show_header=True,
            header_style="bold white on dark_blue",
            border_style="magenta", show_lines=True,
        )
        for name, justify, width in _COLS:
            ct.add_column(name, justify=justify, width=width)
        for i, r in enumerate(conviction, 1):
            ct.add_row(str(i), *_row(r))
        _console.print(ct)

    # ── WATCHLIST section ──────────────────────────────────────────────────
    if watchlist:
        _console.print(
            f"\n[bold cyan]▶ WATCHLIST{date_str}  "
            f"信心 {min_confidence:.0f}+ 觀察 {len(watchlist)} 檔[/bold cyan]"
            f"  [dim]信心分排序[/dim]"
        )
        wt = Table(
            box=box.SIMPLE, show_header=True,
            header_style="bold dim", show_lines=False, padding=(0, 1),
        )
        for name, justify, width in _COLS:
            wt.add_column(name, justify=justify, width=width)
        for i, r in enumerate(watchlist, 1):
            wt.add_row(str(i), *_row(r))
        _console.print(wt)

    total_shown = len(conviction) + len(watchlist)
    filtered = max(0, len(valid) - total_shown)
    _console.print(
        f"\n[bold green]  ✓ 精煉清單 {total_shown} 檔[/bold green]"
        f"  [dim]（CONVICTION {len(conviction)} + WATCHLIST {len(watchlist)}）"
        f"  ｜ 已過濾 {filtered} 檔低分雜訊（原始通過門檻 {len(valid)} 檔）[/dim]"
    )


def _run_phase(
    tickers: list[str],
    analysis_date: date,
    workers: int,
    llm_provider=None,
    no_llm: bool = False,
    label_repo=None,
    market_map: dict[str, str] | None = None,
    finmind: "FinMindClient | None" = None,
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
    finmind: optional pre-built FinMindClient to share across phases (L1 cache reuse).
    """
    # 建立共用客戶端 — 所有 worker 共享快取
    shared_finmind = finmind if finmind is not None else FinMindClient(ohlcv_repo=OHLCVRepository())
    shared_paid = PaidDataFetcher()
    shared_chip = ChipProxyFetcher(paid_fetcher=shared_paid)
    shared_chip.shares_map = _load_shares_map()
    shared_agent = _make_agent(
        llm_provider=llm_provider,
        no_llm=no_llm,
        label_repo=label_repo,
        finmind=shared_finmind,
        chip_fetcher=shared_chip,
    )

    # 注入大盤層級市場情境至 taifex_context
    taifex_ctx: dict = {}

    # 1. 大盤融資維持率（Gate 0 macro filter）
    margin_rate = shared_paid.fetch_market_margin_maintenance(analysis_date)
    if margin_rate is not None:
        taifex_ctx["margin_maintenance_rate"] = margin_rate

    # 2. 台指期三大法人未平倉（FinMind paid，取代 TAIFEX opendata）
    futures_ctx = shared_paid.fetch_futures_context(analysis_date)
    if futures_ctx.get("data_available"):
        taifex_ctx["futures_ctx"] = futures_ctx
        taifex_ctx["futures_bearish"] = futures_ctx.get("composite_bearish", False)
    else:
        # fallback: 嘗試舊 TAIFEX opendata（由 strategist_agent 初始化時設定）
        pass

    # 3. 台指選擇權 PCR 市場情緒（FinMind paid TXO）
    options_ctx = shared_paid.fetch_options_context(analysis_date)
    if options_ctx.get("data_available"):
        taifex_ctx["options_ctx"] = options_ctx

    shared_agent._taifex_context = taifex_ctx

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
                    log_line = f"[dim]{ticker:<8}[/dim] [{color}]conf={conf:.1f}[/{color}]"
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


def _settle_pending_returns(
    conn,
    date_from: date,
    date_to: date,
) -> int:
    """Compute and write back return_t1/t3/t5 for unsettled signals in [date_from, date_to].

    Uses yfinance for OHLCV (no FinMind API key needed). Signals already having
    return_t5 are skipped.  Returns number of signals newly settled.
    """
    try:
        import yfinance as yf
        _yfl = __import__("logging").getLogger("yfinance")
        _yfl.setLevel(__import__("logging").WARNING)
    except ImportError:
        return 0

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT signal_id, ticker, signal_date, entry_price, action
            FROM signal_outcomes
            WHERE signal_date >= %s AND signal_date <= %s
              AND source = 'live'
              AND return_t5 IS NULL
              AND halt_flag = FALSE
            """,
            (date_from, date_to),
        )
        pending = cur.fetchall()

    if not pending:
        return 0

    # Fetch OHLCV once per ticker (covering up to 8 calendar days ahead of signal_date)
    tickers_needed: dict[str, set[date]] = {}
    for _, ticker, sig_date, _, _ in pending:
        if isinstance(sig_date, str):
            sig_date = date.fromisoformat(sig_date)
        tickers_needed.setdefault(ticker, set()).add(sig_date)

    # Map ticker → DataFrame (indexed by date)
    ohlcv_cache: dict[str, dict] = {}
    for ticker, dates in tickers_needed.items():
        earliest = min(dates)
        fetch_from = earliest + timedelta(days=1)
        fetch_to = max(dates) + timedelta(days=15)
        suffix = ".TW" if not ticker.endswith((".TW", ".TWO")) else ""
        try:
            df = yf.download(
                f"{ticker}{suffix}",
                start=fetch_from.isoformat(),
                end=fetch_to.isoformat(),
                progress=False,
                auto_adjust=False,
            )
            if df.empty and suffix == ".TW":
                df = yf.download(
                    f"{ticker}.TWO",
                    start=fetch_from.isoformat(),
                    end=fetch_to.isoformat(),
                    progress=False,
                    auto_adjust=False,
                )
            if not df.empty:
                # Flatten MultiIndex columns if present
                if isinstance(df.columns, __import__("pandas").MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                closes: dict[date, float] = {
                    d.date(): float(row["Close"])
                    for d, row in df.iterrows()
                    if not __import__("math").isnan(float(row["Close"]))
                }
                ohlcv_cache[ticker] = closes
        except Exception:
            pass

    settled_rows: list[tuple] = []
    for signal_id, ticker, sig_date, entry_price, action in pending:
        if isinstance(sig_date, str):
            sig_date = date.fromisoformat(sig_date)
        closes = ohlcv_cache.get(ticker, {})
        future_dates = sorted(d for d in closes if d > sig_date)
        if len(future_dates) < 5:
            continue
        entry = float(entry_price or 0)
        if entry <= 0:
            continue

        def _ret(idx: int) -> float:
            if idx < len(future_dates):
                return (closes[future_dates[idx]] / entry - 1) * 100
            return 0.0

        settled_rows.append((_ret(0), _ret(2), _ret(4), signal_id))

    if not settled_rows:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE signal_outcomes
               SET return_t1 = %s, return_t3 = %s, return_t5 = %s
             WHERE signal_id = %s AND return_t5 IS NULL
            """,
            settled_rows,
        )
    conn.commit()
    return len(settled_rows)


def _print_yesterday_results(analysis_date: date) -> None:
    """Query signal_outcomes for signals from T-30 to T-7 days ago and print a P&L briefing.

    Automatically settles any unsettled signals first (via yfinance).
    Shows win rate and avg T+5 return split by LONG vs WATCH action.
    Prints nothing when DB is unavailable or no data.
    """
    import os
    if not os.environ.get("DATABASE_URL"):
        return
    try:
        from taiwan_stock_agent.infrastructure.db import get_connection, init_pool
        init_pool()
    except Exception:
        return

    # Signals from 5..15 trading days ago have T+5 settled; use calendar days ×2 as rough buffer
    date_from = analysis_date - timedelta(days=30)
    date_to = analysis_date - timedelta(days=7)

    try:
        with get_connection() as conn:
            _settle_pending_returns(conn, date_from, date_to)
    except Exception:
        pass

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        so.ticker,
                        so.action,
                        so.return_t1,
                        so.return_t3,
                        so.return_t5
                    FROM signal_outcomes so
                    WHERE so.signal_date >= %s
                      AND so.signal_date <= %s
                      AND so.source = 'live'
                      AND so.return_t5 IS NOT NULL
                    ORDER BY so.signal_date DESC, so.ticker
                    """,
                    (date_from, date_to),
                )
                rows = cur.fetchall()
    except Exception:
        return

    if not rows:
        return

    # Group by action
    groups: dict[str, list] = {"LONG": [], "WATCH": []}
    for row in rows:
        ticker, action, rt1, rt3, rt5 = row
        rt1 = float(rt1 or 0)
        rt3 = float(rt3 or 0)
        rt5 = float(rt5 or 0)
        a = action if action in groups else "WATCH"
        groups[a].append((ticker, rt1, rt3, rt5))

    total = sum(len(v) for v in groups.values())
    if total == 0:
        return

    lines: list[str] = []
    for action, items in groups.items():
        if not items:
            continue
        avg_t5 = sum(r[3] for r in items) / len(items)
        pos_rate = sum(1 for r in items if r[3] > 0) / len(items) * 100
        c = "green" if avg_t5 >= 0 else "red"
        lines.append(
            f"[bold]{action}[/bold] {len(items)}筆  "
            f"T+5均值[{c}]{avg_t5:+.1f}%[/{c}]  "
            f"正報酬{pos_rate:.0f}%"
        )

    # Best and worst T+5 among all
    all_items = groups["LONG"] + groups["WATCH"]
    best = max(all_items, key=lambda r: r[3])
    worst = min(all_items, key=lambda r: r[3])

    summary = "  │  ".join(lines)
    detail = (
        f"最佳 [green]{best[0]}[/green] T+5[green]{best[3]:+.1f}%[/green]"
        f"  最差 [red]{worst[0]}[/red] T+5[red]{worst[3]:+.1f}%[/red]"
    )

    _console.print(Panel(
        f"{summary}\n{detail}",
        title=f"[bold yellow]昨日戰績[/bold yellow]  {date_from.strftime('%m/%d')}–{date_to.strftime('%m/%d')}  共 {total} 筆",
        border_style="yellow",
        padding=(0, 2),
    ))


def run_batch(
    tickers: list[str],
    analysis_date: date,
    top: int,
    min_confidence: int,
    workers: int,
    llm_provider=None,
    llm_top: int | None = None,
    label_repo=None,
    industry_map: dict[str, str] | None = None,
    save_db: bool = True,
    name_map: dict[str, str] | None = None,
    market_map: dict[str, str] | None = None,
    sort_by: str = "trend",
    by_industry: bool = False,
) -> None:
    _print_yesterday_results(analysis_date)

    # Phase 4.50.5 — 持倉復盤 (run BEFORE Phase 1 so user sees "yesterday's
    # holdings P&L + risk warnings" before today's scan results)
    try:
        import importlib.util as _ilu
        _hr_path = Path(__file__).resolve().parent / "holdings_review.py"
        _spec = _ilu.spec_from_file_location("_holdings_review_mod", _hr_path)
        if _spec and _spec.loader:
            _hr_mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_hr_mod)
            _hr_mod.run_review(today=analysis_date, lookback_days=7,
                                budget_twd=3_000_000, use_llm=True)
    except Exception as exc:
        logger.warning("Holdings review skipped: %s", exc)

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

    _shared_finmind = FinMindClient(ohlcv_repo=OHLCVRepository())

    if llm_provider is None:
        # 純 deterministic：強制關閉 LLM（避免 StrategistAgent 自動偵測 API key）
        results = _run_phase(tickers, analysis_date, workers, no_llm=True, label_repo=label_repo, market_map=market_map, finmind=_shared_finmind)
    else:
        # 永遠兩階段：Phase 1 全量 deterministic → Phase 2 top N with LLM
        _console.print(f"\n[bold cyan][Phase 1][/bold cyan] deterministic scan：{len(tickers)} 檔")
        results = _run_phase(tickers, analysis_date, workers, no_llm=True, label_repo=label_repo, market_map=market_map, finmind=_shared_finmind)

        # 排序有效結果
        eligible = sorted(
            [r for r in results if not r["halt"] and r["error"] is None],
            key=lambda r: r["confidence"], reverse=True,
        )
        _console.print(f"\n[bold cyan][Phase 1 完成][/bold cyan] {len(results)} 檔（有效 [green]{len(eligible)}[/green] 檔）")
        if eligible:
            top5 = "  ".join(f"[bold]{r['ticker']}[/bold]([green]{r['confidence']:.1f}[/green])" for r in eligible[:5])
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
            p2_workers = 1  # serialize LLM calls to avoid concurrent 429s on shared API key
            phase2 = _run_phase(llm_tickers, analysis_date, p2_workers, llm_provider=llm_provider, label_repo=label_repo, market_map=market_map, finmind=_shared_finmind)
            p2_valid = {r["ticker"]: r for r in phase2 if r.get("error") is None}
            results = [p2_valid.get(r["ticker"], r) for r in results]

    # ── Pullback scan (uses L2 DB cache via OHLCVRepository) ──
    _console.print("\n[bold cyan][Pullback Scan][/bold cyan] 回調型偵測中…")
    _shared_agent = _make_agent(llm_provider=None, label_repo=label_repo, finmind=_shared_finmind)
    pullback_results = _scan_pullback_batch(
        tickers, analysis_date, _shared_agent, market_map=market_map
    )
    _console.print(f"  回調型信號: [green]{len(pullback_results)}[/green] 檔")

    # ── Surge scan (SurgeRadar on same tickers, writes to DB) ─────────────────
    _console.print("\n[bold cyan][Surge Scan][/bold cyan] 爆量偵測中…")
    _run_surge_inline(tickers, analysis_date, market_map=market_map)
    surge_db_results = _load_surge_from_db(analysis_date)
    if surge_db_results:
        _console.print(f"  爆量型信號: [green]{len(surge_db_results)}[/green] 檔")

    # ── Early accumulation scan (InstAccum, ChipTransfer, VCP, HTF) ──────────
    _console.print("\n[bold cyan][Early Accum Scan][/bold cyan] 提前佈局偵測中…")
    early_results = _scan_early_accum_batch(
        tickers, analysis_date, _shared_agent, market_map=market_map
    )
    _console.print(f"  提前佈局信號: [green]{len(early_results)}[/green] 檔")

    # ── Merge all signal types into one unified result list ────────────────────
    results = _merge_unified_signals(results, pullback_results, surge_db_results, early_results)

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

    growth_index = _load_growth_index()
    n_growth = _apply_growth_bonus(results, growth_index)
    if n_growth:
        _console.print(f"  [dim]↑ 月營收成長加分: {n_growth} 檔 (GROWTH_HIGH +8 / MID +5 / LOW +3)[/dim]")

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

    n_concept = _apply_concept_heat_bonus(results)
    if n_concept:
        _console.print(f"  [dim]↑ 熱門題材加成: {n_concept} 檔 (+3/+5 pts)[/dim]")

    # ── Rotation tailwind bonus (fixed: reads rotation_signal.json bucketed schema)
    n_rot_em = n_rot_hot = n_rot_cool = 0
    rotation_file = Path(__file__).resolve().parents[1] / "data" / "market_heat" / "rotation_signal.json"
    if rotation_file.exists() and industry_map:
        try:
            import json as _json_rot
            rot_doc = _json_rot.loads(rotation_file.read_text(encoding="utf-8"))
            emerging = {n.get("label") for n in (rot_doc.get("emerging_nodes") or []) if n.get("label")}
            hot = {n.get("label") for n in (rot_doc.get("hot_nodes") or []) if n.get("label")}
            cooling = {n.get("label") for n in (rot_doc.get("cooling_nodes") or []) if n.get("label")}
            for r in results:
                ind = (industry_map or {}).get(r["ticker"], "")
                if not ind:
                    continue
                if ind in emerging:
                    r["confidence"] = r.get("confidence", 0) + 5
                    r.setdefault("flags", []).append("ROTATION_EMERGING")
                    n_rot_em += 1
                elif ind in hot:
                    r["confidence"] = r.get("confidence", 0) + 3
                    r.setdefault("flags", []).append("ROTATION_HOT")
                    n_rot_hot += 1
                elif ind in cooling:
                    r["confidence"] = max(0, r.get("confidence", 0) - 3)
                    r.setdefault("flags", []).append("ROTATION_COOLING")
                    n_rot_cool += 1
        except Exception:
            pass
    if n_rot_em or n_rot_hot or n_rot_cool:
        _console.print(
            f"  [dim]↑↓ 輪動加減分: EMERGING×{n_rot_em}(+5) HOT×{n_rot_hot}(+3) COOLING×{n_rot_cool}(-3)[/dim]"
        )

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

    if by_industry and industry_map:
        _print_by_industry(
            results,
            top,
            min_confidence,
            scan_date=str(analysis_date),
            name_map=name_map,
            industry_map=industry_map,
        )
    elif by_industry:
        _print_table(
            results,
            top,
            min_confidence,
            scan_date=str(analysis_date),
            name_map=name_map,
            sort_by=sort_by,
        )
    else:
        _print_focus_list(
            results,
            top_conviction=5,
            top_watchlist=20,
            min_confidence=min_confidence,
            scan_date=str(analysis_date),
            name_map=name_map,
        )

    _print_score_health(
        [r["confidence"] for r in results
         if not r.get("halt") and r.get("error") is None
         and r.get("action") in ("LONG", "WATCH")],
    )

    # ── 資金配置 Tier 建議 (LLM advisor) ──────────────────────────────────────
    allocation_plan = _build_allocation_plan(
        results=results,
        industry_map=industry_map or {},
        analysis_date=str(analysis_date),
        llm_provider=llm_provider,
    )
    if allocation_plan is not None:
        _print_allocation_panel(allocation_plan, name_map=name_map or {})

    # ── 持倉模擬 (HoldingsManager) ─────────────────────────────────────────────
    daily_portfolio = None
    budget_allocation = None
    if allocation_plan is not None:
        try:
            daily_portfolio = _build_daily_portfolio(
                allocation_plan=allocation_plan,
                results=results,
                analysis_date=analysis_date,
                name_map=name_map or {},
                industry_map=industry_map or {},
            )
            if daily_portfolio is not None:
                _print_portfolio_panel(daily_portfolio)
        except Exception as exc:
            logger.warning("Portfolio simulation failed: %s", exc)

        # Phase 4.50 — NT$3M actual capital allocation
        try:
            budget_allocation = _build_budget_allocation(
                results=results,
                industry_map=industry_map or {},
                name_map=name_map or {},
                analysis_date=analysis_date,
            )
            if budget_allocation is not None:
                _print_budget_panel(budget_allocation)
        except Exception as exc:
            logger.warning("Budget allocation failed: %s", exc)

    html_path = (Path(__file__).resolve().parents[1] / "data" / "scans" / f"scan_{analysis_date}.html")
    alloc_path = (Path(__file__).resolve().parents[1] / "data" / "scans" / f"allocation_{analysis_date}.html")
    _generate_plan_html(results, str(analysis_date), html_path,
                        name_map=name_map or {}, industry_map=industry_map or {},
                        market_map=market_map or {},
                        heat_summary=_load_heat_summary(),
                        llm_provider=llm_provider,
                        min_confidence=min_confidence,
                        finmind_client=_shared_finmind,
                        allocation_plan=allocation_plan,
                        allocation_html_path=alloc_path,
                        daily_portfolio=daily_portfolio,
                        budget_allocation=budget_allocation)
    # Write standalone allocation HTML (separate tab)
    if allocation_plan is not None:
        _write_allocation_standalone_html(
            allocation_plan,
            name_map=name_map or {},
            industry_map=industry_map or {},
            scan_date=str(analysis_date),
            scan_html_path=html_path,
            out_path=alloc_path,
        )
    _console.print(f"  [dim cyan]📄 HTML: file://{html_path.resolve()}[/dim cyan]")
    if allocation_plan is not None and alloc_path.exists():
        _console.print(f"  [dim cyan]💰 配置: file://{alloc_path.resolve()}[/dim cyan]")
    import subprocess, sys as _sys
    if _sys.platform == "darwin":
        subprocess.Popen(["open", str(html_path)])
        if allocation_plan is not None and alloc_path.exists():
            subprocess.Popen(["open", str(alloc_path)])


# ── Capital allocation (Tier S/A/B/C) ───────────────────────────────────────


def _build_allocation_plan(
    *,
    results: list[dict],
    industry_map: dict[str, str],
    analysis_date: str,
    llm_provider: str | None,
):
    """Run CapitalAllocator + AllocationAdvisor and return an AllocationPlan.

    Returns None when there are no actionable LONG/WATCH signals.
    """
    actionable = [
        r for r in results
        if not r.get("halt") and r.get("error") is None
        and r.get("action") in ("LONG", "WATCH")
    ]
    if not actionable:
        return None

    try:
        allocator = CapitalAllocator()
        ctx = allocator.assess(
            signals=actionable,
            industry_map=industry_map,
            snapshot_date=analysis_date,
        )
        # llm_provider is already an LLMProvider instance (or None) here.
        if llm_provider is not None and hasattr(llm_provider, "complete"):
            advisor = AllocationAdvisor(llm=llm_provider)
        else:
            advisor = AllocationAdvisor.from_env(
                llm_provider if isinstance(llm_provider, str) else None
            )
        return advisor.recommend(ctx)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Allocation advisor failed: %s", exc)
        return None


def _build_daily_portfolio(
    *,
    allocation_plan,
    results: list[dict],
    analysis_date: date,
    name_map: dict[str, str],
    industry_map: dict[str, str],
):
    """Run HoldingsManager.process_day to update simulated holdings."""
    prices: dict[str, float] = {}
    confidences: dict[str, float] = {}
    for r in results:
        tk = str(r.get("ticker", ""))
        if not tk:
            continue
        ep = float(r.get("entry_bid") or 0)
        if ep <= 0:
            continue
        prices[tk] = ep
        confidences[tk] = float(r.get("confidence") or 0)

    # Phase 4.50.8 (revised in 4.50.9) — Price fallback for held tickers NOT
    # in today's scan. Original 4.50.8 hit FinMind API and got 403 Forbidden.
    # Now use L2 cache (OHLCVRepository) first, then yfinance fallback.
    try:
        from taiwan_stock_agent.infrastructure.holdings_repository import HoldingsRepository
        from taiwan_stock_agent.infrastructure.ohlcv_repository import OHLCVRepository
        from datetime import timedelta as _td
        _repo = HoldingsRepository()
        if _repo.available:
            _open_held = _repo.list_open()
            _missing = [h.ticker for h in _open_held if h.ticker not in prices]
            if _missing:
                _ohlcv = OHLCVRepository()
                _start = analysis_date - _td(days=14)
                _end = analysis_date
                for _tk in _missing:
                    _price = None
                    # 1) L2 DB cache
                    try:
                        _df = _ohlcv.get(_tk, _start, _end)
                        if _df is not None and not _df.empty:
                            _df = _df.sort_values("trade_date")
                            _price = float(_df.iloc[-1]["close"])
                    except Exception:
                        pass
                    # 2) yfinance fallback (FinMind API 403's this endpoint)
                    if _price is None:
                        try:
                            import yfinance as _yf
                            _suffix = ".TW"
                            _hist = _yf.Ticker(f"{_tk}{_suffix}").history(period="7d")
                            if _hist.empty:
                                _hist = _yf.Ticker(f"{_tk}.TWO").history(period="7d")
                            if not _hist.empty:
                                _price = float(_hist["Close"].iloc[-1])
                        except Exception:
                            pass
                    if _price and _price > 0:
                        prices[_tk] = _price
    except Exception as _exc:
        logger.debug("Held-ticker price fallback skipped: %s", _exc)

    # Concepts per ticker — read from the concepts.json mapping
    from pathlib import Path as _P
    import json as _j
    concepts_path = _P(__file__).resolve().parents[1] / "config" / "concepts.json"
    concepts_by_ticker: dict[str, list[str]] = {}
    try:
        data = _j.loads(concepts_path.read_text(encoding="utf-8"))
        for key, body in (data.get("concepts") or {}).items():
            for t in body.get("tickers") or []:
                concepts_by_ticker.setdefault(str(t), []).append(key)
    except Exception:
        pass

    manager = HoldingsManager()
    return manager.process_day(
        today=analysis_date,
        plan=allocation_plan,
        prices_today=prices,
        confidences_today=confidences,
        name_map=name_map,
        industry_map=industry_map,
        concepts_by_ticker=concepts_by_ticker,
        commit=True,
    )


def _build_budget_allocation(
    *,
    results: list[dict],
    industry_map: dict[str, str],
    name_map: dict[str, str],
    analysis_date: date,
) -> "PortfolioAllocation | None":
    """Phase 4.50: distil scan results → top-N refined picks → NT$3M plan.

    The output is a `PortfolioAllocation` with concrete share counts and
    NT$ amounts per position, ready for HTML/terminal display.
    """
    from taiwan_stock_agent.infrastructure.holdings_repository import HoldingsRepository

    # Load rotation + concept context (best-effort)
    from pathlib import Path as _P
    import json as _j
    heat_dir = _P(__file__).resolve().parents[1] / "data" / "market_heat"
    rotation_signal = {}
    try:
        rotation_signal = _j.loads((heat_dir / "rotation_signal.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    concepts_path = _P(__file__).resolve().parents[1] / "config" / "concepts.json"
    concept_membership: dict[str, list[str]] = {}
    hot_concepts: set[str] = set()
    try:
        cfg = _j.loads(concepts_path.read_text(encoding="utf-8"))
        for k, body in (cfg.get("concepts") or {}).items():
            for t in body.get("tickers") or []:
                concept_membership.setdefault(str(t), []).append(k)
        # Hot concepts come from latest concept_heat snapshot
        ch_files = sorted(heat_dir.glob("concept_heat_*.json"))
        if ch_files:
            ch = _j.loads(ch_files[-1].read_text(encoding="utf-8"))
            for k, v in (ch.get("concepts") or {}).items():
                if isinstance(v, dict) and float(v.get("rank_pct", 0) or 0) >= 70:
                    hot_concepts.add(k)
    except Exception:
        pass

    repo = HoldingsRepository()
    held = repo.list_open() if repo.available else []
    held_tickers = {h.ticker for h in held}

    refined = RefinedPickFilter().refine(
        results,
        industry_map=industry_map,
        rotation_signal=rotation_signal,
        concept_membership=concept_membership,
        hot_concepts=hot_concepts,
        held_tickers=held_tickers,
        name_map=name_map,
        top_n=25,
    )
    if not refined and not held:
        return None

    return BudgetAllocator().allocate(
        refined_picks=refined,
        held_positions=held,
        today=analysis_date,
    )


def _print_budget_panel(allocation: "PortfolioAllocation") -> None:
    """Phase 4.50 — terminal table for NT$3M actual capital allocation."""
    _console.print()
    _console.print(Panel(
        Text(f"💰 NT${allocation.budget_twd:,} 預算配置（精煉版）", style="bold cyan"),
        border_style="cyan",
        expand=False,
    ))
    # Stats line
    _console.print(
        f"  📊 持倉 {len(allocation.held_positions)} 支  "
        f"｜ 新買 {len(allocation.new_buy_positions)} 支  "
        f"｜ 觀察 {len(allocation.skipped_picks)} 支  "
        f"｜ 持倉占用 NT${allocation.held_value_twd:,}  "
        f"｜ 新買投入 NT${allocation.new_buys_twd:,}  "
        f"｜ 現金 NT${allocation.cash_reserve_twd:,} ({allocation.cash_pct}%)"
    )
    if not allocation.positions:
        _console.print("  [dim]無持倉、無新買[/dim]")
        return

    # Table
    tbl = Table(box=box.ROUNDED, padding=(0, 1))
    tbl.add_column("Tier")
    tbl.add_column("代號")
    tbl.add_column("名稱")
    tbl.add_column("產業", style="dim")
    tbl.add_column("狀態")
    tbl.add_column("股數", justify="right")
    tbl.add_column("Lots/零股", justify="right", style="dim")
    tbl.add_column("進場價", justify="right")
    tbl.add_column("投入 NT$", justify="right", style="bold")
    tbl.add_column("停損", justify="right", style="yellow")
    tbl.add_column("停利", justify="right", style="green")

    for p in allocation.positions:
        tier_color = {"S": "gold1", "A": "cyan", "B": "yellow"}.get(p.tier, "white")
        status = "[blue]📦 持倉[/blue]" if p.is_held else "[green]🆕 新買[/green]"
        lots_str = f"{p.lots}張+{p.odd_shares}零" if p.odd_shares else f"{p.lots}張"
        if p.lots == 0 and p.odd_shares > 0:
            lots_str = f"{p.odd_shares}零股"
        tbl.add_row(
            f"[{tier_color}]{p.tier}[/{tier_color}]",
            p.ticker,
            p.name[:10],
            p.sector[:8],
            status,
            f"{p.shares:,}",
            lots_str,
            f"{p.entry_price:,.1f}",
            f"NT${p.actual_twd:,}",
            f"{p.stop_loss:,.1f}",
            f"{p.take_profit:,.1f}",
        )
    _console.print(tbl)

    # Watchlist (skipped)
    if allocation.skipped_picks:
        skip_str = ", ".join(
            f"{p.ticker}({p.name})" for p in allocation.skipped_picks[:10]
        )
        _console.print(
            f"\n  [dim]📋 觀察清單 (capacity 滿/預算用罄): {skip_str}"
            + (f" ... +{len(allocation.skipped_picks)-10}" if len(allocation.skipped_picks) > 10 else "")
            + "[/dim]"
        )


def _print_portfolio_panel(dp) -> None:
    """Render Holdings / New Buys / Exits in terminal."""
    _console.print()
    _console.print(Panel(
        Text("📦 持倉組合（模擬）", style="bold cyan"),
        border_style="cyan", expand=False,
    ))
    if not dp.holdings and not dp.new_buys and not dp.pending_exits:
        _console.print("  [dim]無持倉，無今日新買，無預期賣出[/dim]")
        return

    # Holdings table
    if dp.holdings:
        tbl = Table(
            title=f"📦 持倉中  共 {len(dp.holdings)} 支  ｜  投入 {dp.total_invested_pct}% / 現金 {dp.cash_pct}%  ｜  Portfolio P&L {dp.portfolio_unrealised_pct:+.2f}%",
            title_style="bold white",
            box=box.ROUNDED,
        )
        tbl.add_column("代號")
        tbl.add_column("名稱")
        tbl.add_column("Tier")
        tbl.add_column("進場日")
        tbl.add_column("進場價", justify="right")
        tbl.add_column("今價", justify="right")
        tbl.add_column("損益%", justify="right")
        tbl.add_column("天數", justify="right")
        tbl.add_column("狀態")
        for h in dp.holdings:
            pl_str = f"{h.unrealised_pct:+.2f}%"
            pl_color = "green" if h.unrealised_pct > 0.5 else "red" if h.unrealised_pct < -0.5 else "white"
            status = "[red]🔴 待賣[/red]" if h.exit_decision.should_close else "[green]✓ 持有[/green]"
            tbl.add_row(
                h.holding.ticker, h.name[:10], h.holding.tier,
                str(h.holding.entry_date), f"{h.holding.entry_price:.1f}",
                f"{h.current_price:.1f}",
                f"[{pl_color}]{pl_str}[/{pl_color}]",
                str(h.days_held), status,
            )
        _console.print(tbl)

    # Pending exits
    if dp.pending_exits:
        _console.print()
        for h in dp.pending_exits:
            _console.print(
                f"  [red bold]🔴 賣出 {h.holding.ticker} {h.name}[/red bold]  "
                f"[red]{h.exit_decision.close_reason}[/red]: {h.exit_decision.rationale}"
            )

    # New buys
    if dp.new_buys:
        _console.print()
        tbl = Table(
            title=f"🆕 今日新買候選  共 {len(dp.new_buys)} 支",
            title_style="bold green",
            box=box.ROUNDED,
        )
        tbl.add_column("代號"); tbl.add_column("名稱"); tbl.add_column("Tier")
        tbl.add_column("配置%", justify="right")
        tbl.add_column("進場", justify="right")
        tbl.add_column("停損", justify="right")
        tbl.add_column("停利", justify="right")
        tbl.add_column("信心", justify="right")
        for b in dp.new_buys:
            tier_color = {"S": "gold1", "A": "cyan", "B": "yellow"}.get(b.tier, "white")
            tbl.add_row(
                b.ticker, b.name[:10],
                f"[{tier_color}]{b.tier}[/{tier_color}]",
                f"{b.suggested_pct:.1f}%",
                f"{b.entry_price:.1f}", f"{b.stop_loss:.1f}",
                f"{b.take_profit:.1f}",
                f"{b.confidence:.0f}",
            )
        _console.print(tbl)


def _print_allocation_panel(plan, *, name_map: dict[str, str]) -> None:
    """Render the AllocationPlan as a Rich tier panel."""
    _console.print()
    header = Text("💰 資金配置建議 (Tier System)", style="bold magenta")
    _console.print(Panel(header, border_style="magenta", expand=False))

    if plan.summary:
        _console.print(f"  [italic]{plan.summary}[/italic]")
        _console.print()

    tier_titles = {
        "S": "🥇 S 首選 (建議重押 20-30%)",
        "A": "🥈 A 強勢 (建議 12-18%)",
        "B": "🥉 B 試單 (建議 5-10%)",
        "C": "⚪ C 觀察 (極小倉或跳過)",
    }
    for tier in TIER_ORDER:
        recs = plan.tiers.get(tier) or []
        if not recs:
            continue
        colour = TIER_COLORS.get(tier, "white")
        tbl = Table(
            title=tier_titles[tier],
            title_style=f"bold {colour}",
            box=box.ROUNDED,
            show_edge=True,
            padding=(0, 1),
        )
        tbl.add_column("代號", style="bold")
        tbl.add_column("名稱", style="white")
        tbl.add_column("配置 %", justify="right", style=f"bold {colour}")
        tbl.add_column("輪動分", justify="right", style="dim")
        tbl.add_column("建議理由", style="white")
        for r in recs:
            name = name_map.get(r.ticker, r.ticker)
            tbl.add_row(
                r.ticker,
                name,
                f"{r.suggested_pct:.1f}%",
                f"{r.rotation_score:.0f}",
                r.reasoning[:80],
            )
        _console.print(tbl)

    if plan.warnings:
        warn_lines = []
        for w in plan.warnings:
            colour = {"high": "red", "medium": "yellow", "low": "dim"}.get(w.severity, "yellow")
            warn_lines.append(f"  [{colour}]⚠ {w.message}[/{colour}]")
        _console.print()
        _console.print(Panel("\n".join(warn_lines), title="集中度警告", border_style="yellow", expand=False))

    # Footer: cash retention suggestion
    invested = sum(r.suggested_pct for r in plan.all_recommendations())
    cash = max(0.0, 100.0 - invested)
    _console.print()
    _console.print(
        f"  [dim]📊 總建議配置: 投入 {invested:.1f}% / 保留現金 {cash:.1f}%  "
        f"(LLM={plan.provider})[/dim]"
    )


# ── Plan HTML generator ──────────────────────────────────────────────────────

_WATCH_FLAGS_CONSOLIDATING = frozenset(["TREND_CONT", "CHIP_LOADING", "NO_CATALYST"])

_ROOT_PATH = Path(__file__).resolve().parents[1]
_HEAT_DIR = _ROOT_PATH / "data" / "market_heat"
_CONCEPT_CACHE_PATH = _ROOT_PATH / "data" / "_concept_classify_cache.json"


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
        # ticker → list of (name_zh, is_hot) tuples — ALL concepts, not just hot
        ticker_concepts: dict[str, list[tuple[str, bool]]] = {}
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
                cdefs = cdefs_raw.get("concepts", cdefs_raw)
                hot_keys = {c["key"] for c in hot}
                # Build full name map from snapshot (has name_zh) + fallback to cdefs
                snap_name: dict[str, str] = {
                    k: v.get("name_zh", k) for k, v in concepts.items()
                }
                for ck, cdef in cdefs.items():
                    name_zh = snap_name.get(ck, cdef.get("name_zh", ck))
                    is_hot = ck in hot_keys
                    for t in cdef.get("tickers", []):
                        ticker_concepts.setdefault(t, []).append((name_zh, is_hot))

        result["ticker_concepts"] = ticker_concepts

        # Rotation radar — load rotation_signal.json if available
        rot_path = _HEAT_DIR / "rotation_signal.json"
        if rot_path.exists():
            try:
                with open(rot_path, encoding="utf-8") as f:
                    rd = _json.load(f)
                result["rotation_candidates"] = rd.get("rotation_candidates", [])[:6]
                result["cooling_nodes"] = [n["label"] for n in rd.get("cooling_nodes", [])][:4]
            except Exception:
                pass

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


def _build_chart_from_df(df) -> dict:
    """Build chart dict from a FinMind OHLCV DataFrame (already in memory)."""
    import pandas as pd
    period = 20
    _empty_ma: dict = {"candles": [], "bb_upper": [], "bb_mid": [], "bb_lower": [],
                       "ma5": [], "ma10": [], "ma20": [], "ma60": []}
    try:
        df = df.sort_values("trade_date").copy()
        df = df.dropna(subset=["open", "high", "low", "close"])
        rows = [
            {"time": str(row["trade_date"]), "open": round(float(row["open"]), 2),
             "high": round(float(row["high"]), 2), "low": round(float(row["low"]), 2),
             "close": round(float(row["close"]), 2)}
            for _, row in df.iterrows()
        ]
        if len(rows) < period:
            return _empty_ma
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

        def _ma(p: int) -> list:
            out = []
            for i, row in enumerate(display_rows):
                orig = i + period - 1
                if orig < p - 1:
                    continue
                w = closes[orig - p + 1: orig + 1]
                out.append({"time": row["time"], "value": round(sum(w) / p, 2)})
            return out

        return {"candles": display_rows, "bb_upper": bb_upper, "bb_mid": bb_mid, "bb_lower": bb_lower,
                "ma5": _ma(5), "ma10": _ma(10), "ma20": _ma(20), "ma60": _ma(60)}
    except Exception:
        return _empty_ma


def _fetch_plan_chart(ticker: str, market: str) -> dict:
    """Fetch 5-month daily OHLCV + Bollinger Bands (20,2) + MA lines via yfinance."""
    suffix = ".TW" if market == "TSE" else ".TWO"
    empty: dict = {"candles": [], "bb_upper": [], "bb_mid": [], "bb_lower": [],
                   "ma5": [], "ma10": [], "ma20": [], "ma60": []}
    try:
        import logging as _log
        import warnings as _warn
        import pandas as pd
        import yfinance as yf
        period = 20
        _yfl = _log.getLogger("yfinance")
        _prev = _yfl.level
        _yfl.setLevel(_log.CRITICAL)
        try:
            with _warn.catch_warnings():
                _warn.simplefilter("ignore")
                hist = yf.Ticker(f"{ticker}{suffix}").history(period="5mo", interval="1d", auto_adjust=True)
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

        # If the most recent day was NaN (yfinance range query incomplete),
        # fetch that single day explicitly — single-day queries return full data.
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

        def _ma(p: int) -> list:
            out = []
            for i, row in enumerate(display_rows):
                orig = i + period - 1
                if orig < p - 1:
                    continue
                w = closes[orig - p + 1: orig + 1]
                out.append({"time": row["time"], "value": round(sum(w) / p, 2)})
            return out

        return {"candles": display_rows, "bb_upper": bb_upper, "bb_mid": bb_mid, "bb_lower": bb_lower,
                "ma5": _ma(5), "ma10": _ma(10), "ma20": _ma(20), "ma60": _ma(60)}
    except Exception:
        return empty


def _classify_concepts_llm(
    tickers_info: list[tuple[str, str, str]],
    llm_provider,
) -> dict[str, list[str]]:
    """Classify tickers into concept basket keys using LLM.
    Returns ticker → [concept_key]. Cached per-ticker (permanent — products rarely change).
    """
    import json as _json
    cached: dict[str, list[str]] = {}
    if _CONCEPT_CACHE_PATH.exists():
        try:
            with open(_CONCEPT_CACHE_PATH, encoding="utf-8") as f:
                cached = _json.load(f).get("classifications", {})
        except Exception:
            pass

    uncached = [(t, n, ind) for t, n, ind in tickers_info if t not in cached]
    if uncached and llm_provider is not None:
        try:
            concept_desc = (
                "AI_GPU_supply: NVIDIA/AMD GPU製造供應鏈、AI推論晶片、高效能運算\n"
                "CoWoS_advanced_packaging: 台積電CoWoS先進封裝、ABF載板、矽中介層\n"
                "CPO_silicon_photonics: 共封裝光學CPO、矽光子元件、光收發模組\n"
                "HBM_memory: 高頻寬記憶體HBM、DRAM設計製造\n"
                "AI_server_cooling: AI伺服器散熱（液冷/均溫板/熱管/散熱風扇/3D VC）\n"
                "robotics_automation: 工業機器人、協作機器人、精密機械、伺服馬達控制\n"
                "low_orbit_satellite: 低軌衛星LEO、Starlink供應鏈、衛星通訊設備\n"
                "heavy_electric: 重電設備（高壓變壓器/配電盤/電力電纜）、電網升級\n"
                "auto_electronics: 車用電子（ADAS/充電樁/OBC/車載半導體/電池管理）\n"
                "PCB_substrate: 高階PCB、ABF載板、IC基板、銅箔基板"
            )
            lines = "\n".join(f"{t} {n}（{ind}）" for t, n, ind in uncached)
            prompt = (
                f"你是台灣股市專家。根據以下概念股分類，判斷每家台灣上市公司屬於哪些概念。\n\n"
                f"概念分類（key: 說明）：\n{concept_desc}\n\n"
                f"公司列表（代號 名稱 產業別）：\n{lines}\n\n"
                f"規則：只標記主要業務確實直接相關的概念，不確定或邊緣相關請給空陣列[]。\n"
                f"只回傳純JSON，格式：{{\"代號\": [\"concept_key\", ...]}}\n"
                f"範例：{{\"2330\": [\"AI_GPU_supply\", \"CoWoS_advanced_packaging\"], \"1234\": []}}"
            )
            import re as _re
            raw = llm_provider.complete(prompt, max_tokens=1500)
            m = _re.search(r'\{[\s\S]*\}', raw)
            if m:
                result = __import__('json').loads(m.group())
                for ticker, keys in result.items():
                    if isinstance(keys, list):
                        cached[ticker] = [k for k in keys if isinstance(k, str)]
        except Exception:
            pass

    try:
        _CONCEPT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CONCEPT_CACHE_PATH, "w", encoding="utf-8") as f:
            __import__('json').dump({"classifications": cached}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return cached


def _render_flow_panel(
    summary,
    *,
    title: str,
    sub: str,
    rising_n: int = 12,
    declining_n: int = 6,
) -> str:
    """Generic capital-flow panel renderer (used for both industry + concept)."""
    from html import escape as _esc
    if not summary.series:
        return ""
    series_sorted = sorted(
        summary.series,
        key=lambda s: (-s.acceleration_3v3, -s.rank_delta_total),
    )
    rising = [s for s in series_sorted if s.acceleration_3v3 > 0][:rising_n]
    declining = [s for s in series_sorted if s.acceleration_3v3 <= 0][-declining_n:]
    return _build_flow_html(title, sub, summary.snapshot_dates, rising, declining)


def _build_flow_html(title, sub, dates, rising, declining):
    from html import escape as _esc

    def _row(s) -> str:
        latest = s.latest
        if latest is None: return ""
        icon, colour, label = TREND_META.get(s.trend_direction, ("→", "#8b949e", "持平"))
        delta = s.rank_delta_total
        delta_color = "#3fb950" if delta > 0 else ("#f85149" if delta < 0 else "#8b949e")
        delta_sign = "+" if delta > 0 else ""
        spark = sparkline_svg(s.rank_pct_series, width=140, height=24, stroke=colour)
        return (
            f'<div class="sf-row">'
            f'  <div class="sf-name">{_esc(s.industry)}</div>'
            f'  <div class="sf-spark">{spark}</div>'
            f'  <div class="sf-now">{latest.rank_pct:.0f}</div>'
            f'  <div class="sf-delta" style="color:{delta_color}">{delta_sign}{delta:.1f}</div>'
            f'  <div class="sf-trend" style="color:{colour}">{icon} {_esc(label)}</div>'
            f'  <div class="sf-meta">廣度 {latest.breadth_above_ma20_pct:.0f}% · 集中 {latest.top5_vol_concentration:.0f}%</div>'
            f'</div>'
        )

    head_dates = " → ".join(dates)
    rising_rows = "".join(_row(s) for s in rising)
    declining_rows = "".join(_row(s) for s in declining)
    head_row = (
        '<div class="sf-row sf-head">'
        '  <div class="sf-name">標的</div>'
        '  <div class="sf-spark">10 日 rank_pct</div>'
        '  <div class="sf-now">今</div>'
        '  <div class="sf-delta">Δ10d</div>'
        '  <div class="sf-trend">趨勢</div>'
        '  <div class="sf-meta">廣度·集中度</div>'
        '</div>'
    )
    return (
        '<div class="sf-panel">'
        f'  <div class="sf-title">{_esc(title)} <span class="sf-sub">{_esc(sub)}</span></div>'
        f'  <div class="sf-period">{_esc(head_dates)}</div>'
        '  <div class="sf-grid">'
        '    <div class="sf-col">'
        f'      <div class="sf-coltitle" style="color:#3fb950">🔥 升溫中 ({len(rising)})</div>'
        f'      <div class="sf-rows">{head_row}{rising_rows}</div>'
        '    </div>'
        '    <div class="sf-col">'
        f'      <div class="sf-coltitle" style="color:#f85149">❄️ 退燒中 ({len(declining)})</div>'
        f'      <div class="sf-rows">{head_row}{declining_rows}</div>'
        '    </div>'
        '  </div>'
        '</div>'
    )


def _render_sector_flow_html(days: int = 10) -> str:
    """Industry-level capital flow panel (10-day)."""
    summary = SectorFlowAnalyzer().analyze(days=days)
    return _render_flow_panel(
        summary,
        title="📈 產業資金流動趨勢",
        sub="10 日熱度時序",
        rising_n=8, declining_n=5,
    )


def _render_concept_flow_html(days: int = 10) -> str:
    """Concept-basket capital flow panel (10-day)."""
    from pathlib import Path as _P
    import json as _j
    concepts_path = _P(__file__).resolve().parents[1] / "config" / "concepts.json"
    meta = {}
    try:
        meta = (_j.loads(concepts_path.read_text(encoding="utf-8")) or {}).get("concepts", {})
    except Exception:
        pass
    summary = ConceptFlowAnalyzer().analyze(days=days, concepts_meta=meta)
    return _render_flow_panel(
        summary,
        title="💎 概念題材資金流動",
        sub=f"10 日熱度時序 · {len(summary.series)} 題材籃子（已合併同主題）",
        rising_n=8, declining_n=6,
    )


def _legacy_render_sector_flow_unused(days: int = 10) -> str:
    """[deprecated - kept only to preserve original function signature for any external caller]."""
    from html import escape as _esc

    analyzer = SectorFlowAnalyzer()
    summary = analyzer.analyze(days=days)
    if not summary.series:
        return ""

    # Sort: warming-up first (high 3v3 acceleration), then by absolute delta
    series_sorted = sorted(
        summary.series,
        key=lambda s: (-s.acceleration_3v3, -s.rank_delta_total),
    )
    # Show top 12 rising + top 6 declining for focus
    rising = [s for s in series_sorted if s.acceleration_3v3 > 0][:12]
    declining = [s for s in series_sorted if s.acceleration_3v3 <= 0][-6:]

    def _row(s) -> str:
        latest = s.latest
        if latest is None:
            return ""
        icon, colour, label = TREND_META.get(s.trend_direction, ("→", "#8b949e", "持平"))
        delta = s.rank_delta_total
        delta_color = "#3fb950" if delta > 0 else ("#f85149" if delta < 0 else "#8b949e")
        delta_sign = "+" if delta > 0 else ""
        # Spark colour follows trend
        spark = sparkline_svg(s.rank_pct_series, width=140, height=24, stroke=colour)
        return (
            f'<div class="sf-row">'
            f'  <div class="sf-name">{_esc(s.industry)}</div>'
            f'  <div class="sf-spark">{spark}</div>'
            f'  <div class="sf-now">{latest.rank_pct:.0f}</div>'
            f'  <div class="sf-delta" style="color:{delta_color}">{delta_sign}{delta:.1f}</div>'
            f'  <div class="sf-trend" style="color:{colour}">{icon} {_esc(label)}</div>'
            f'  <div class="sf-meta">廣度 {latest.breadth_above_ma20_pct:.0f}% · 集中 {latest.top5_vol_concentration:.0f}%</div>'
            f'</div>'
        )

    rising_rows = "".join(_row(s) for s in rising)
    declining_rows = "".join(_row(s) for s in declining)

    head_dates = " → ".join(summary.snapshot_dates)

    return (
        '<div class="sf-panel">'
        '  <div class="sf-title">📈 產業資金流動趨勢 <span class="sf-sub">10 日熱度時序</span></div>'
        f'  <div class="sf-period">{_esc(head_dates)}</div>'
        '  <div class="sf-grid">'
        '    <div class="sf-col">'
        f'      <div class="sf-coltitle" style="color:#3fb950">🔥 升溫中 ({len(rising)})</div>'
        '      <div class="sf-rows">'
        '        <div class="sf-row sf-head">'
        '          <div class="sf-name">產業</div>'
        '          <div class="sf-spark">10 日 rank_pct</div>'
        '          <div class="sf-now">今</div>'
        '          <div class="sf-delta">Δ10d</div>'
        '          <div class="sf-trend">趨勢</div>'
        '          <div class="sf-meta">廣度·集中度</div>'
        '        </div>'
        f'        {rising_rows}'
        '      </div>'
        '    </div>'
        '    <div class="sf-col">'
        f'      <div class="sf-coltitle" style="color:#f85149">❄️ 退燒中 ({len(declining)})</div>'
        '      <div class="sf-rows">'
        '        <div class="sf-row sf-head">'
        '          <div class="sf-name">產業</div>'
        '          <div class="sf-spark">10 日 rank_pct</div>'
        '          <div class="sf-now">今</div>'
        '          <div class="sf-delta">Δ10d</div>'
        '          <div class="sf-trend">趨勢</div>'
        '          <div class="sf-meta">廣度·集中度</div>'
        '        </div>'
        f'        {declining_rows}'
        '      </div>'
        '    </div>'
        '  </div>'
        '</div>'
    )


def _render_portfolio_html(dp) -> str:
    """Render Holdings / New Buys / Pending Exits as the top hero region.

    Three cards side by side; collapses gracefully when fields are empty.
    """
    if dp is None:
        return ""
    from html import escape as _esc

    # --- 1. Pending exits (red, action-required) ---
    exit_rows = ""
    if dp.pending_exits:
        rows = []
        for h in dp.pending_exits:
            rows.append(
                f'<div class="pf-row pf-exit-row">'
                f'  <div class="pf-tk">{_esc(h.holding.ticker)}<br><span class="pf-nm">{_esc(h.name)}</span></div>'
                f'  <div class="pf-detail">'
                f'    進場 <b>{h.holding.entry_price:.1f}</b> → 現價 <b>{h.current_price:.1f}</b><br>'
                f'    <span class="pf-pnl-neg">{h.unrealised_pct:+.2f}%</span> · 持有 {h.days_held} 天'
                f'  </div>'
                f'  <div class="pf-reason"><span class="pf-tag pf-tag-exit">{_esc(h.exit_decision.close_reason or "")}</span><br>'
                f'    <span class="pf-reason-text">{_esc(h.exit_decision.rationale)}</span></div>'
                f'</div>'
            )
        exit_rows = "".join(rows)

    # --- 2. Holdings (current open positions) ---
    hold_rows = ""
    if dp.holdings:
        rows = []
        for h in dp.holdings:
            if h.exit_decision.should_close:
                continue  # shown in exit panel
            pnl_class = (
                "pf-pnl-pos" if h.unrealised_pct > 0.5
                else "pf-pnl-neg" if h.unrealised_pct < -0.5
                else "pf-pnl-neu"
            )
            tier_color = {"S": "#ffd700", "A": "#58a6ff", "B": "#f0b429", "C": "#6e7681"}.get(h.holding.tier, "#8b949e")
            rows.append(
                f'<div class="pf-row">'
                f'  <div class="pf-tk">'
                f'    <span class="pf-tier" style="background:{tier_color}">{h.holding.tier}</span>'
                f'    {_esc(h.holding.ticker)}<br><span class="pf-nm">{_esc(h.name)}</span></div>'
                f'  <div class="pf-detail">'
                f'    {h.holding.entry_price:.1f} → <b>{h.current_price:.1f}</b><br>'
                f'    <span class="pf-stop">停損 {h.holding.stop_loss:.1f}</span> · '
                f'    <span class="pf-tp">停利 {h.holding.take_profit:.1f}</span>'
                f'  </div>'
                f'  <div class="pf-pnl {pnl_class}">{h.unrealised_pct:+.2f}%<br>'
                f'    <span class="pf-days">{h.days_held} 天</span></div>'
                f'</div>'
            )
        hold_rows = "".join(rows)

    # --- 3. New buys (today's fresh tier S/A/B) ---
    buy_rows = ""
    if dp.new_buys:
        rows = []
        for b in dp.new_buys:
            tier_color = {"S": "#ffd700", "A": "#58a6ff", "B": "#f0b429"}.get(b.tier, "#8b949e")
            rows.append(
                f'<div class="pf-row pf-buy-row">'
                f'  <div class="pf-tk">'
                f'    <span class="pf-tier" style="background:{tier_color}">{b.tier}</span>'
                f'    {_esc(b.ticker)}<br><span class="pf-nm">{_esc(b.name)}</span></div>'
                f'  <div class="pf-detail">'
                f'    進場 <b>{b.entry_price:.1f}</b><br>'
                f'    停損 <span class="pf-stop">{b.stop_loss:.1f}</span> · '
                f'    停利 <span class="pf-tp">{b.take_profit:.1f}</span>'
                f'  </div>'
                f'  <div class="pf-alloc">{b.suggested_pct:.0f}%<br>'
                f'    <span class="pf-conf">conf {b.confidence:.0f}</span></div>'
                f'</div>'
            )
        buy_rows = "".join(rows)

    open_holdings_count = sum(1 for h in dp.holdings if not h.exit_decision.should_close)
    pnl = dp.portfolio_unrealised_pct
    pnl_class = "pf-pnl-pos" if pnl > 0.5 else "pf-pnl-neg" if pnl < -0.5 else "pf-pnl-neu"

    return (
        '<div class="pf-hero">'
        '  <div class="pf-hero-head">📦 持倉組合 · 模擬</div>'
        f'  <div class="pf-hero-summary">'
        f'    <span class="pf-stat">📦 持倉 <b>{open_holdings_count}</b></span>'
        f'    <span class="pf-stat">🆕 今日新買 <b>{len(dp.new_buys)}</b></span>'
        f'    <span class="pf-stat">🔴 待賣 <b>{len(dp.pending_exits)}</b></span>'
        f'    <span class="pf-stat">投入 <b>{dp.total_invested_pct}%</b> · 現金 <b>{dp.cash_pct}%</b></span>'
        f'    <span class="pf-stat">Portfolio P&L <b class="{pnl_class}">{pnl:+.2f}%</b></span>'
        '  </div>'
        '  <div class="pf-grid">' +
        (f'<div class="pf-card pf-card-exit"><div class="pf-card-head">🔴 預期賣出 ({len(dp.pending_exits)})</div>{exit_rows}</div>' if exit_rows else "") +
        (f'<div class="pf-card pf-card-hold"><div class="pf-card-head">📦 持倉中 ({open_holdings_count})</div>{hold_rows}</div>' if hold_rows else '<div class="pf-card pf-card-empty">尚無持倉</div>') +
        (f'<div class="pf-card pf-card-buy"><div class="pf-card-head">🆕 今日新買候選 ({len(dp.new_buys)})</div>{buy_rows}</div>' if buy_rows else "") +
        '  </div>'
        '</div>'
    )


def _render_budget_allocation_html(allocation) -> str:
    """Phase 4.50 — NT$3M budget allocation table at the top of scan HTML.

    Renders a single table showing every position with concrete share count,
    NT$ amount, stop/take prices, and held vs new-buy status. Replaces the
    old percentage-based abstraction with actionable numbers.
    """
    if allocation is None:
        return ""
    from html import escape as _esc

    def _format_lots(lots: int, odd: int) -> str:
        if lots > 0 and odd > 0:
            return f"{lots}張 + {odd}零"
        if lots > 0:
            return f"{lots}張"
        if odd > 0:
            return f"{odd}零股"
        return "—"

    rows = []
    for p in allocation.positions:
        tier_bg = {"S": "#ffd700", "A": "#58a6ff", "B": "#f0b429"}.get(p.tier, "#6e7681")
        tier_fg = "#0d1117" if p.tier in ("S", "A", "B") else "#fff"
        status_badge = (
            '<span class="bg-status bg-status-held">📦 持倉</span>'
            if p.is_held else
            '<span class="bg-status bg-status-buy">🆕 新買</span>'
        )
        rows.append(
            f'<tr>'
            f'  <td class="bg-tier" style="background:{tier_bg};color:{tier_fg}">{p.tier}</td>'
            f'  <td><b>{_esc(p.ticker)}</b><br><span class="bg-name">{_esc(p.name)}</span></td>'
            f'  <td class="bg-sector">{_esc(p.sector)}</td>'
            f'  <td>{status_badge}</td>'
            f'  <td class="bg-num">{p.shares:,}</td>'
            f'  <td class="bg-lots">{_esc(_format_lots(p.lots, p.odd_shares))}</td>'
            f'  <td class="bg-num">{p.entry_price:,.1f}</td>'
            f'  <td class="bg-twd"><b>NT${p.actual_twd:,}</b></td>'
            f'  <td class="bg-stop">{p.stop_loss:,.1f}</td>'
            f'  <td class="bg-tp">{p.take_profit:,.1f}</td>'
            f'</tr>'
        )

    skipped_rows = ""
    if allocation.skipped_picks:
        items = ", ".join(
            f"{_esc(p.ticker)} ({_esc(p.name)})"
            for p in allocation.skipped_picks[:15]
        )
        more = f" ... +{len(allocation.skipped_picks)-15}" if len(allocation.skipped_picks) > 15 else ""
        skipped_rows = f'<div class="bg-watch">📋 觀察清單（容量/預算用罄）: {items}{more}</div>'

    invested_pct = round(allocation.total_invested_twd / allocation.budget_twd * 100, 1) if allocation.budget_twd > 0 else 0

    return (
        '<div class="budget-panel">'
        f'  <div class="budget-head">💰 NT${allocation.budget_twd:,} 預算配置 · {allocation.today}</div>'
        f'  <div class="budget-stats">'
        f'    <span class="bs-stat">📦 持倉 <b>{len(allocation.held_positions)}</b></span>'
        f'    <span class="bs-stat">🆕 新買 <b>{len(allocation.new_buy_positions)}</b></span>'
        f'    <span class="bs-stat">📋 觀察 <b>{len(allocation.skipped_picks)}</b></span>'
        f'    <span class="bs-stat">投入 <b>NT${allocation.total_invested_twd:,}</b> ({invested_pct}%)</span>'
        f'    <span class="bs-stat">現金 <b style="color:#3fb950">NT${allocation.cash_reserve_twd:,}</b> ({allocation.cash_pct}%)</span>'
        f'  </div>'
        f'  <table class="budget-table">'
        f'    <thead><tr>'
        f'      <th>Tier</th><th>標的</th><th>產業</th><th>狀態</th>'
        f'      <th>股數</th><th>張/零</th><th>進場價</th><th>投入 NT$</th>'
        f'      <th>停損</th><th>停利</th>'
        f'    </tr></thead>'
        f'    <tbody>{"".join(rows)}</tbody>'
        f'  </table>'
        f'  {skipped_rows}'
        '</div>'
    )


def _render_allocation_link_card(plan, alloc_path) -> str:
    """Compact summary card in main scan HTML that links to the standalone tab."""
    from html import escape as _esc
    if plan is None or alloc_path is None:
        return ""
    counts = {t: len(plan.tiers.get(t, [])) for t in TIER_ORDER}
    invested = sum(r.suggested_pct for r in plan.all_recommendations())
    cash = max(0.0, 100.0 - invested)
    href = _esc(alloc_path.name)
    return (
        '<div class="alloc-link-card">'
        '  <div class="alc-icon">💰</div>'
        '  <div class="alc-body">'
        '    <div class="alc-head">資金配置建議 <span class="alc-sub">Tier S/A/B/C 分級</span></div>'
        f'    <div class="alc-counts">'
        f'      <span class="alc-tier-pill" style="background:#ffd700;color:#000">S {counts["S"]}</span>'
        f'      <span class="alc-tier-pill" style="background:#58a6ff;color:#0d1117">A {counts["A"]}</span>'
        f'      <span class="alc-tier-pill" style="background:#f0b429;color:#0d1117">B {counts["B"]}</span>'
        f'      <span class="alc-tier-pill" style="background:#6e7681;color:#fff">C {counts["C"]}</span>'
        f'      <span class="alc-stat">投入 <b style="color:#ff6b6b">{invested:.1f}%</b> · '
        f'現金 <b style="color:#3fb950">{cash:.1f}%</b></span>'
        '    </div>'
        '  </div>'
        f'  <a class="alc-open" href="{href}" target="_blank">查看完整配置 →</a>'
        '</div>'
    )


def _write_allocation_standalone_html(
    plan,
    *,
    name_map: dict[str, str],
    industry_map: dict[str, str],
    scan_date: str,
    scan_html_path: Path,
    out_path: Path,
) -> None:
    """Write the allocation panel as its own HTML file (opens in a new tab)."""
    from html import escape as _esc

    body = _render_allocation_html(plan, name_map, industry_map)
    css = """
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:0}
.ah-head{padding:24px 28px;background:linear-gradient(135deg,#1a1a2e,#0d2137);border-bottom:1px solid #21262d}
.ah-head h1{margin:0;font-size:22px;color:#e6edf3}
.ah-head .ah-sub{margin-top:6px;color:#8b949e;font-size:12px}
.ah-head a{color:#58a6ff;text-decoration:none;font-size:12px}
.ah-head a:hover{text-decoration:underline}
.alloc-panel{background:linear-gradient(180deg,#1a1a2e,#0d1117);padding:20px 28px}
.alloc-title{font-size:22px;font-weight:700;color:#e6edf3;margin-bottom:6px}
.alloc-sub{font-size:13px;color:#8b949e;font-weight:400;margin-left:8px}
.alloc-summary{font-size:14px;color:#c9d1d9;font-style:italic;margin-bottom:20px;line-height:1.6;padding:12px 16px;background:rgba(255,255,255,.03);border-radius:8px;border-left:3px solid #58a6ff}
.alloc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}
.alloc-tier{border:1px solid;border-radius:10px;padding:14px;background:#161b22}
.alloc-tier-head{display:flex;align-items:center;gap:10px;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,.06)}
.alloc-tier-badge{font-weight:800;padding:3px 12px;border-radius:6px;font-size:15px}
.alloc-tier-label{font-size:13px;color:#8b949e}
.alloc-tier-count{margin-left:auto;font-size:11px;color:#8b949e;background:rgba(255,255,255,.05);padding:3px 10px;border-radius:10px}
.alloc-rows{display:flex;flex-direction:column;gap:6px}
.alloc-row{display:grid;grid-template-columns:1.6fr .6fr .4fr 2.2fr;gap:8px;align-items:center;padding:8px 10px;background:rgba(255,255,255,.02);border-radius:6px;font-size:13px}
.alloc-row:hover{background:rgba(255,255,255,.05)}
.alloc-tk{display:flex;flex-direction:column;gap:2px}
.alloc-code{font-weight:700;color:#e6edf3;font-size:14px}
.alloc-name{color:#c9d1d9;font-size:12px}
.alloc-ind{color:#8b949e;font-size:11px}
.alloc-pct{font-weight:700;text-align:right;font-size:16px}
.alloc-rot{color:#8b949e;text-align:right;font-size:12px;font-variant-numeric:tabular-nums}
.alloc-why{color:#c9d1d9;font-size:12px;line-height:1.5}
.alloc-warns{margin-top:16px;display:flex;flex-direction:column;gap:8px}
.alloc-warn{padding:10px 14px;border-left:3px solid;background:rgba(240,180,41,.08);border-radius:0 6px 6px 0;font-size:13px;color:#c9d1d9}
.alloc-warn-icon{font-weight:700;margin-right:6px}
.alloc-footer{margin-top:18px;padding:14px;background:rgba(255,255,255,.03);border-radius:8px;font-size:14px;color:#c9d1d9;text-align:center}
"""
    rel_scan = scan_html_path.name  # both in same dir
    html = (
        '<!doctype html>\n'
        '<html lang="zh-Hant"><head><meta charset="utf-8">'
        f'<title>💰 資金配置 - {_esc(scan_date)}</title>'
        f'<style>{css}</style></head>\n'
        '<body>\n'
        '<div class="ah-head">'
        '  <h1>💰 資金配置建議</h1>'
        f'  <div class="ah-sub">{_esc(scan_date)} · Tier S/A/B/C 分級 · '
        f'<a href="{_esc(rel_scan)}">← 回掃描結果</a></div>'
        '</div>\n'
        f'{body}\n'
        '</body></html>'
    )
    out_path.write_text(html, encoding="utf-8")


def _render_allocation_html(
    plan,
    name_map: dict[str, str],
    industry_map: dict[str, str],
) -> str:
    """Render the Tier S/A/B/C panel as HTML. Empty string when plan is None."""
    if plan is None:
        return ""
    from html import escape as _esc

    tier_meta = {
        "S": ("🥇", "首選 20-30%", "#ffd700", "#3d2f00"),
        "A": ("🥈", "強勢 12-18%", "#58a6ff", "#0d2b50"),
        "B": ("🥉", "試單 5-10%", "#f0b429", "#3d2a08"),
        "C": ("⚪", "觀察 / 跳過", "#6e7681", "#21262d"),
    }

    tier_blocks: list[str] = []
    for tier in TIER_ORDER:
        recs = plan.tiers.get(tier) or []
        if not recs:
            continue
        emoji, label, colour, bg = tier_meta[tier]
        rows: list[str] = []
        for r in recs:
            nm = _esc(name_map.get(r.ticker, r.ticker))
            ind = _esc(industry_map.get(r.ticker, ""))
            rows.append(
                f'<div class="alloc-row" data-ticker="{_esc(r.ticker)}">'
                f'  <div class="alloc-tk"><span class="alloc-code">{_esc(r.ticker)}</span>'
                f'    <span class="alloc-name">{nm}</span>'
                f'    <span class="alloc-ind">{ind}</span></div>'
                f'  <div class="alloc-pct" style="color:{colour}">{r.suggested_pct:.1f}%</div>'
                f'  <div class="alloc-rot" title="輪動分">{r.rotation_score:.0f}</div>'
                f'  <div class="alloc-why">{_esc(r.reasoning[:140])}</div>'
                f'</div>'
            )
        tier_blocks.append(
            f'<div class="alloc-tier" style="border-color:{colour};background:{bg}">'
            f'  <div class="alloc-tier-head">'
            f'    <span class="alloc-tier-badge" style="background:{colour};color:#0d1117">{emoji} {tier}</span>'
            f'    <span class="alloc-tier-label">{label}</span>'
            f'    <span class="alloc-tier-count">{len(recs)} 支</span>'
            f'  </div>'
            f'  <div class="alloc-rows">{"".join(rows)}</div>'
            f'</div>'
        )

    warn_html = ""
    if plan.warnings:
        warns = []
        for w in plan.warnings:
            colour = {"high": "#f85149", "medium": "#f0b429", "low": "#8b949e"}.get(w.severity, "#f0b429")
            warns.append(
                f'<div class="alloc-warn" style="border-left-color:{colour}">'
                f'<span class="alloc-warn-icon" style="color:{colour}">⚠</span> {_esc(w.message)}'
                f'</div>'
            )
        warn_html = f'<div class="alloc-warns">{"".join(warns)}</div>'

    invested = sum(r.suggested_pct for r in plan.all_recommendations())
    cash = max(0.0, 100.0 - invested)
    footer = (
        f'<div class="alloc-footer">'
        f'  📊 投入 <b style="color:#ff6b6b">{invested:.1f}%</b> · '
        f'保留現金 <b style="color:#3fb950">{cash:.1f}%</b> · '
        f'<span style="color:#8b949e">LLM={_esc(plan.provider)}</span>'
        f'</div>'
    )

    summary = f'<div class="alloc-summary">{_esc(plan.summary)}</div>' if plan.summary else ""

    return (
        '<div class="alloc-panel">'
        '  <div class="alloc-title">💰 資金配置建議 <span class="alloc-sub">Tier System</span></div>'
        f'  {summary}'
        f'  <div class="alloc-grid">{"".join(tier_blocks)}</div>'
        f'  {warn_html}'
        f'  {footer}'
        '</div>'
    )


def _generate_plan_html(
    results: list[dict],
    scan_date: str,
    html_path: Path,
    name_map: dict[str, str],
    industry_map: dict[str, str],
    market_map: dict[str, str],
    heat_summary: dict | None = None,
    llm_provider=None,
    min_confidence: int = 50,
    finmind_client=None,
    allocation_plan=None,
    allocation_html_path: Path | None = None,
    daily_portfolio=None,
    budget_allocation=None,
) -> None:
    """Dark-themed HTML report for plan scan results (LONG + actionable WATCH only)."""
    import json as _json
    from html import escape as _esc

    # Filter: LONG stocks + WATCH stocks that are NOT in consolidation, above min_confidence
    def _is_consolidating(r: dict) -> bool:
        flags = set(r.get("flags") or [])
        return bool(flags & _WATCH_FLAGS_CONSOLIDATING)

    filtered = [
        r for r in results
        if not r.get("halt") and r.get("error") is None
        and r.get("action") in ("LONG", "WATCH")
        and r.get("confidence", 0) >= min_confidence
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

    # Fetch chart data — prefer FinMind in-memory cache (already fetched during scan),
    # fall back to yfinance only for tickers not in cache.
    _console.print("  [dim]抓取線圖資料（plan HTML）…[/dim]")
    chart_data: dict[str, dict] = {}
    mem_cache = getattr(finmind_client, "_ohlcv_mem", {}) if finmind_client else {}
    pairs_yf = []
    for r in filtered:
        t = r["ticker"]
        if t in mem_cache and not mem_cache[t].empty:
            chart_data[t] = _build_chart_from_df(mem_cache[t])
        else:
            pairs_yf.append((t, market_map.get(t, "TSE")))
    if pairs_yf:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_fetch_plan_chart, t, m): t for t, m in pairs_yf}
            for fut in as_completed(futs):
                chart_data[futs[fut]] = fut.result()

    def _rule_rec(r: dict) -> str:
        """Data-grounded recommendation with actual numbers extracted from flags."""
        flags = list(r.get("flags") or [])
        flag_set = set(flags)
        action = r["action"]
        conf = r.get("confidence", 0)
        parts: list[str] = []

        # 1. Price zone (G1)
        for f in flags:
            if "GATE_PASS:G1_ZONE:" in f:
                pct = f.split(":")[-1]
                parts.append(f"收盤在近20日高點{pct}蓄勢區")
                break

        # 2. BB compression (G2)
        for f in flags:
            if "GATE_PASS:G2_BB_PCT:" in f:
                bp = f.split(":")[-1].rstrip("p")
                try:
                    label = "極度壓縮" if float(bp) <= 20 else ("明顯收窄" if float(bp) <= 35 else "收窄")
                except ValueError:
                    label = "收窄"
                parts.append(f"布林帶{label}（{bp}百分位）")
                break

        # 3. Institutional flow — most important, show actual numbers
        for f in flags:
            if "DUAL_FLOW_STRONG:" in f:
                detail = f.split("DUAL_FLOW_STRONG:")[-1]
                parts.append(f"外資+投信同步大力買進（{detail}）")
                break
            elif "DUAL_FLOW:" in f:
                detail = f.split("DUAL_FLOW:")[-1]
                parts.append(f"外資+投信同步買進（{detail}）")
                break
        else:
            for f in flags:
                if "INST_SURGE:" in f:
                    x = f.split(":")[-1]
                    parts.append(f"法人買超衝量（達均量{x}）")
                    break

        # 4. Cumulative flow or foreign acceleration
        for f in flags:
            if "CUMUL_FLOW_HOT:" in f:
                x = f.split(":")[-1]
                parts.append(f"20日累積法人流量熱絡（{x}）")
                break
            elif "CUMUL_FLOW_WARM:" in f:
                x = f.split(":")[-1]
                parts.append(f"20日累積法人流量溫和（{x}）")
                break
        for f in flags:
            if "CONSISTENT_ACCUM:" in f:
                x = f.split(":")[-1]
                parts.append(f"持續吸籌（{x}）")
                break

        # 5. Technical pattern
        if "COILING_PRIME" in flag_set:
            parts.append("強力蓄積型態成型")
        elif "EMERGING_SETUP" in flag_set:
            parts.append("法人+均線共振蓄積確認")
        elif "BB_UPPER_COIL" in flag_set:
            parts.append("布林帶上軌蓄力（突破動能醞釀）")

        # 6. Persistence with date
        for f in flags:
            if "PERSIST_RISING:" in f:
                day = f.split(":")[-1]
                parts.append(f"連續走強（前一日{day}）")
                break
            elif "PERSIST_STABLE:" in f:
                day = f.split(":")[-1]
                parts.append(f"訊號穩定持續（前一日{day}）")
                break

        # 7. Sector rank with numbers
        for f in flags:
            if "SECTOR_RANK:" in f:
                x = f.split(":")[-1]
                parts.append(f"產業內排名 {x}")
                break

        # 8. Market context
        if "ACCUM_MODE" in flag_set:
            parts.append("大盤修正期逆勢法人買進")

        if action == "LONG":
            conclusion = "整體條件全面成熟，建議積極布局。" if conf >= 90 else "突破條件到位，可考慮進場。"
        else:
            conclusion = "型態尚未完成，列入觀察追蹤。"

        if parts:
            return "；".join(parts[:5]) + "。" + conclusion
        return conclusion

    def _key_signals_html(flags: list[str]) -> str:
        """Compact quantitative signal badges — always shown as factual basis for the rec."""
        sigs: list[tuple[str, str, str]] = []  # (label, value, css_class)
        seen: set[str] = set()

        for f in flags:
            if "GATE_PASS:G1_ZONE:" in f and "g1" not in seen:
                sigs.append(("位置", f.split(":")[-1], "ks-neutral"))
                seen.add("g1")
            elif "GATE_PASS:G2_BB_PCT:" in f and "bb" not in seen:
                bp = f.split(":")[-1].rstrip("p")
                try:
                    cls = "ks-hot" if float(bp) <= 20 else "ks-warm" if float(bp) <= 35 else "ks-neutral"
                except ValueError:
                    cls = "ks-neutral"
                sigs.append(("BB", f"{bp}p", cls))
                seen.add("bb")
            elif "DUAL_FLOW_STRONG:" in f and "flow" not in seen:
                sigs.append(("土洋同買↑↑", f.split("DUAL_FLOW_STRONG:")[-1], "ks-hot"))
                seen.add("flow")
            elif "DUAL_FLOW:" in f and "flow" not in seen:
                sigs.append(("土洋同買", f.split("DUAL_FLOW:")[-1], "ks-warm"))
                seen.add("flow")
            elif "INST_SURGE:" in f and "isurge" not in seen:
                sigs.append(("法人衝量", f.split(":")[-1], "ks-warm"))
                seen.add("isurge")
            elif "CUMUL_FLOW_HOT:" in f and "cumul" not in seen:
                sigs.append(("累積流量", f.split(":")[-1], "ks-hot"))
                seen.add("cumul")
            elif "CUMUL_FLOW_WARM:" in f and "cumul" not in seen:
                sigs.append(("累積流量", f.split(":")[-1], "ks-warm"))
                seen.add("cumul")
            elif "FOREIGN_ACCEL:" in f and "faccel" not in seen:
                sigs.append(("外資加速", f.split(":")[-1], "ks-warm"))
                seen.add("faccel")
            elif "CONSISTENT_ACCUM:" in f and "accum" not in seen:
                sigs.append(("持續吸籌", f.split(":")[-1], "ks-warm"))
                seen.add("accum")
            elif "SECTOR_RANK:" in f and "rank" not in seen:
                sigs.append(("產業排名", f.split(":")[-1], "ks-neutral"))
                seen.add("rank")
            elif "NEAR_HIST_HIGH:" in f and "histhigh" not in seen:
                sigs.append(("距高點", f.split(":")[-1] + "日", "ks-neutral"))
                seen.add("histhigh")
            elif f == "RS_STRONG" and "rs" not in seen:
                sigs.append(("相對強勢", "✓", "ks-warm"))
                seen.add("rs")
            elif f in ("COILING_PRIME", "EMERGING_SETUP") and "pattern" not in seen:
                label = "強力蓄積" if f == "COILING_PRIME" else "蓄積確認"
                sigs.append((label, "✓", "ks-hot"))
                seen.add("pattern")
            elif f == "CONCEPT_HEAT" and "concept" not in seen:
                sigs.append(("熱門題材", "✓", "ks-hot"))
                seen.add("concept")
            elif "CONCEPT_HEAT:" in f and "concept" not in seen:
                val = f.split(":")[-1]
                sigs.append(("熱門題材", val, "ks-hot"))
                seen.add("concept")

        if not sigs:
            return ""
        tags = "".join(
            f'<span class="ks-tag {cls}"><span class="ks-k">{_esc(lbl)}</span>'
            f'<span class="ks-v">{_esc(val)}</span></span>'
            for lbl, val, cls in sigs[:7]
        )
        return f'<div class="key-signals"><span class="ks-label">依據</span>{tags}</div>'

    hs = heat_summary or {}
    ticker_concepts: dict[str, list[tuple[str, bool]]] = hs.get("ticker_concepts", {})

    # Build concept_key → (name_zh, is_hot) lookup
    _concept_meta: dict[str, tuple[str, bool]] = {}
    _hot_keys = {c["key"] for c in hs.get("hot_concepts", [])}
    _cdefs_path = _ROOT_PATH / "config" / "concepts.json"
    if _cdefs_path.exists():
        with open(_cdefs_path, encoding="utf-8") as _cf:
            _cd_raw = _json.load(_cf)
        for _ck, _cv in _cd_raw.get("concepts", _cd_raw).items():
            _concept_meta[_ck] = (_cv.get("name_zh", _ck), _ck in _hot_keys)

    # LLM concept classification — enrich ticker_concepts for all filtered tickers
    if llm_provider is not None and _concept_meta:
        _tinfo = [(r["ticker"], name_map.get(r["ticker"], r["ticker"]), industry_map.get(r["ticker"], "")) for r in filtered]
        _llm_cache = _classify_concepts_llm(_tinfo, llm_provider)
        for _t, _keys in _llm_cache.items():
            _existing = {name for name, _ in ticker_concepts.get(_t, [])}
            _extras: list[tuple[str, bool]] = []
            for _k in _keys:
                if _k in _concept_meta:
                    _nm, _ih = _concept_meta[_k]
                    if _nm not in _existing:
                        _extras.append((_nm, _ih))
                        _existing.add(_nm)
            if _extras:
                ticker_concepts[_t] = list(ticker_concepts.get(_t, [])) + _extras

    # Collect unique industries for filter dropdown
    unique_industries: list[str] = []
    _seen_inds: set[str] = set()
    for r in filtered:
        ind = industry_map.get(r["ticker"], "")
        if ind and ind not in _seen_inds:
            _seen_inds.add(ind)
            unique_industries.append(ind)

    # Collect unique signal types (preserve display order)
    _sig_type_order = ["爆量★", "爆量", "趨勢延伸", "蓄積★", "蓄積", "法人建倉", "籌碼轉移", "VCP", "旗形", "回調"]
    _seen_sigtypes: set[str] = {r.get("signal_type", "蓄積") for r in filtered}
    unique_sig_types: list[str] = [s for s in _sig_type_order if s in _seen_sigtypes]

    # Collect all unique concept names for filter pills (hot-first, then alphabetical)
    _seen_cnames: set[str] = set()
    _all_concepts_hot: list[str] = []
    _all_concepts_cold: list[str] = []
    for r in filtered:
        for cname, chot in ticker_concepts.get(r["ticker"], []):
            if cname not in _seen_cnames:
                _seen_cnames.add(cname)
                if chot:
                    _all_concepts_hot.append(cname)
                else:
                    _all_concepts_cold.append(cname)
    all_concept_names: list[str] = sorted(_all_concepts_hot) + sorted(_all_concepts_cold)

    cards: list[str] = []
    for i, r in enumerate(filtered):
        ticker = r["ticker"]
        _raw_name = name_map.get(ticker, "")
        name = _esc(_raw_name) if _raw_name and _raw_name != ticker else ""
        industry = _esc(industry_map.get(ticker, ""))
        action = r["action"]
        conf_raw = r.get("confidence", 0) or 0
        conf = int(round(conf_raw))            # for data-conf (int slider)
        conf_disp = f"{conf_raw:.1f}"          # visible 1-decimal display
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
        llm_risk = r.get("risk") or ""
        if llm_verdict:
            rec_text = _esc(llm_verdict)
            rec_source_html = '<span class="rec-source src-ai">AI 分析</span>'
        else:
            rec_text = _esc(_rule_rec(r))
            rec_source_html = '<span class="rec-source src-rule">規則評估</span>'
        chip_txt = _esc(llm_chip)
        risk_txt = _esc(llm_risk)
        key_sigs_html = _key_signals_html(list(r.get("flags") or []))

        entry_s = f"{entry:.2f}" if entry else "--"
        target_s = f"{target:.2f}" if target else "--"
        stop_s = f"{stop:.2f}" if stop else "--"
        upside_s = f"+{upside_pct:.1f}%" if upside_pct > 0 else "--"
        conf_cls = "pos" if action == "LONG" else "conf-watch"

        # Direct concept basket membership only (from concepts.json curated tickers)
        ctags: list[tuple[str, bool]] = list(ticker_concepts.get(ticker, []))
        # hot concepts first, then cold; cap at 6 total
        ctags_sorted = sorted(ctags, key=lambda x: (not x[1], x[0]))[:6]
        concept_names_joined = ",".join(n for n, _ in ctags_sorted)
        concept_html = (
            '<div class="ctag-row">' +
            "".join(
                f'<span class="ctag {"ctag-hot" if h else "ctag-cold"}">{_esc(n)}</span>'
                for n, h in ctags_sorted
            ) +
            "</div>"
        ) if ctags_sorted else ""

        raw_industry = industry_map.get(ticker, "")

        # Signal type + fundamental badges
        sig_type = r.get("signal_type", "蓄積")
        horizon_val = r.get("horizon", "波段")

        # Strategy category (for filter + card border)
        _EARLY_TYPES = {"法人建倉", "籌碼轉移", "VCP", "旗形"}
        _TRACK_TYPES = {"回調", "爆量★", "爆量"}
        strategy = "early" if sig_type in _EARLY_TYPES else "track" if sig_type in _TRACK_TYPES else "confirm"

        # Rotation status from flags
        _flags_list = r.get("flags") or []
        _flags_str = " ".join(_flags_list) if isinstance(_flags_list, list) else str(_flags_list)
        rotation_status = (
            "EMERGING" if "ROTATION_EMERGING" in _flags_str else
            "HOT"      if "ROTATION_HOT"      in _flags_str else
            "COOLING"  if "ROTATION_COOLING"   in _flags_str else ""
        )

        # BB compression filter: matches when BB width is in the bottom 35
        # percentile (TCE Gate-2) AND no breakout signal has fired yet.
        bb_compressed = "0"
        if isinstance(_flags_list, list):
            bb_pct_val = None
            for _f in _flags_list:
                if isinstance(_f, str) and "GATE_PASS:G2_BB_PCT:" in _f:
                    try:
                        bb_pct_val = float(_f.split("PCT:")[-1].rstrip("p"))
                    except ValueError:
                        pass
                if _f == "GATE_PASS:G2_BB_NARROW":
                    bb_pct_val = 30.0  # legacy flag means narrow
            broke_out = any(
                f in _flags_str
                for f in ("BB_SQUEEZE_BREAKOUT", "BB_UPPER_WALK", "BREAKOUT_20D")
            )
            if bb_pct_val is not None and bb_pct_val < 35 and not broke_out:
                bb_compressed = "1"
        rotation_label = {"EMERGING": "📡升溫", "HOT": "🔥熱門", "COOLING": "🔻降溫"}.get(rotation_status, "")
        yoy = r.get("growth_yoy")
        consec = r.get("growth_consecutive", 0) or 0

        sig_colors = {
            "爆量★": "#ff4444", "爆量": "#ff7744",
            "回調": "#ffcc44", "趨勢延伸": "#44ccff",
            "蓄積★": "#44ff88", "蓄積": "#44aaff",
            "法人建倉": "#dd44ff", "籌碼轉移": "#aa44cc",
            "VCP": "#44eeff", "旗形": "#ffee44",
        }
        sig_bg = sig_colors.get(sig_type, "#888888")
        horizon_bg = "#cc4444" if horizon_val == "短線" else "#226688"

        type_badge = (
            f'<span style="background:{sig_bg};color:#000;'
            f'border-radius:4px;padding:2px 6px;font-size:11px;'
            f'font-weight:bold;margin-right:4px">{sig_type}</span>'
            f'<span style="background:{horizon_bg};color:#fff;'
            f'border-radius:4px;padding:2px 6px;font-size:11px;'
            f'margin-right:4px">{horizon_val}</span>'
            + (f'<span class="rot-badge rot-{rotation_status.lower()}">{rotation_label}</span>' if rotation_label else "")
        )
        if yoy:
            consec_str = f" 連{consec}M" if consec >= 3 else ""
            fund_badge = (
                f'<span style="background:#1a4a2a;color:#44ff88;'
                f'border-radius:4px;padding:2px 6px;font-size:11px">'
                f'★ 月營收 +{yoy:.0f}%{consec_str}</span>'
            )
        else:
            fund_badge = ""

        # Strategy left-border color
        _border_colors = {"early": "#dd44ff", "confirm": "#388bfd", "track": "#ef5350"}
        _border_col = _border_colors.get(strategy, "#388bfd")

        cards.append(f"""
    <div class="card" data-action="{action}" data-conf="{conf}" data-industry="{_esc(raw_industry)}" data-concepts="{_esc(concept_names_joined)}" data-sigtype="{_esc(sig_type)}" data-horizon="{_esc(horizon_val)}" data-strategy="{strategy}" data-rotation="{rotation_status}" data-bb-compressed="{bb_compressed}" style="animation-delay:{delay}s;border-left:4px solid {_border_col}">
      <div class="card-header">
        <div class="rank">{i+1}</div>
        <div class="info">
          <div class="ticker"><span class="tcode">{_esc(ticker)}</span>{f'<span class="tname">{name}</span>' if name else ''}</div>
          <div class="cname">{industry}</div>
        </div>
        <div class="badge g-{gcls}">{badge_zh}</div>
      </div>
      {concept_html}
      <div class="type-badges" style="margin:4px 12px 8px">{type_badge}{fund_badge}</div>
      <div class="metrics">
        <div class="m"><div class="mv {conf_cls}">{conf_disp}</div><div class="ml">信心分</div></div>
        <div class="m"><div class="mv">{entry_s}</div><div class="ml">進場價</div></div>
        <div class="m"><div class="mv pos">{upside_s}</div><div class="ml">目標空間</div></div>
        <div class="m"><div class="mv neg">{stop_s}</div><div class="ml">止損</div></div>
        <div class="m"><div class="mv">{target_s}</div><div class="ml">目標價</div></div>
      </div>
      <div class="chart-wrap">
        <div class="chart-ctrl">
          <button class="ct-btn active" data-s="bb">布林</button>
          <button class="ct-btn" data-s="ma5">5MA</button>
          <button class="ct-btn" data-s="ma10">10MA</button>
          <button class="ct-btn" data-s="ma20">20MA</button>
          <button class="ct-btn" data-s="ma60">60MA</button>
        </div>
        <div class="chart" data-ticker="{_esc(ticker)}"></div>
      </div>
      <div class="rec">
        <div class="rec-header">
          <span class="rec-badge rec-{rec_cls}">{rec_label}</span>
          {rec_source_html}
        </div>
        <div class="rec-text">{rec_text}</div>
        {f'<div class="rec-chip"><span class="rec-chip-label">籌碼</span>{chip_txt}</div>' if chip_txt else ""}
        {f'<div class="rec-risk"><span class="rec-risk-label">風險</span>{risk_txt}</div>' if risk_txt else ""}
      </div>
      {key_sigs_html}
      <div class="links">
        <a class="link-btn tv" href="{tv_url}" target="_blank" rel="noopener">TradingView</a>
        <a class="link-btn gi" href="{gi_url}" target="_blank" rel="noopener">Goodinfo</a>
      </div>
    </div>""")

    # Build rotation radar HTML for header
    def _heat_badge(label: str, cls: str, title: str = "", concept: str = "") -> str:
        t = f' title="{_esc(title)}"' if title else ""
        c = f' data-concept="{_esc(concept)}" onclick="toggleConceptBadge(this)"' if concept else ""
        return f'<span class="hbadge {cls}"{t}{c}>{_esc(label)}</span>'

    radar_rows: list[str] = []
    hot_inds = hs.get("hot_industries", [])
    accel_inds = hs.get("accelerating", [])
    hot_concepts = hs.get("hot_concepts", [])
    rotation_candidates = hs.get("rotation_candidates", [])
    cooling_nodes = hs.get("cooling_nodes", [])
    if hot_inds:
        badges = "".join(_heat_badge(n, "hb-hot") for n in hot_inds)
        radar_rows.append(f'<div class="radar-row"><span class="radar-label">🔥 熱門產業</span>{badges}</div>')
    if accel_inds:
        badges = "".join(_heat_badge(n, "hb-warm") for n in accel_inds)
        radar_rows.append(f'<div class="radar-row"><span class="radar-label">📈 升溫中</span>{badges}</div>')
    if cooling_nodes:
        badges = "".join(_heat_badge(n, "hb-cooling") for n in cooling_nodes)
        radar_rows.append(f'<div class="radar-row"><span class="radar-label">🔻 降溫中</span>{badges}</div>')
    if hot_concepts:
        def _fmt_concept(c: dict) -> str:
            import math as _math
            ret = c.get("ret_5d_pct", 0)
            if ret is None or (isinstance(ret, float) and _math.isnan(ret)):
                return _esc(c["name_zh"])
            sign = "+" if ret >= 0 else ""
            return f'{_esc(c["name_zh"])} {sign}{ret:.1f}%'
        badges = "".join(_heat_badge(_fmt_concept(c), "hb-concept", concept=c["name_zh"]) for c in hot_concepts)
        radar_rows.append(f'<div class="radar-row"><span class="radar-label">💡 熱門題材</span>{badges}</div>')
    if rotation_candidates:
        def _fmt_cand(c: dict) -> str:
            state = c.get("state", "")
            star = "★" if state == "EMERGING" else ""
            triggers = "、".join(c.get("trigger_labels", [])[:2])
            tip = f'觸發: {triggers} | 預期{c.get("avg_lag_weeks",2):.0f}週 | {c.get("note","")}'
            return _heat_badge(f'{star}{c["label"]}', "hb-rotation", tip, concept=c["label"])
        badges = "".join(_fmt_cand(c) for c in rotation_candidates)
        radar_rows.append(f'<div class="radar-row"><span class="radar-label">📡 輪動候選</span>{badges}</div>')
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
.hb-hot{{background:rgba(248,81,73,.12);color:#ff7b7b;border:1px solid rgba(248,81,73,.25);cursor:pointer;transition:all .15s}}
.hb-warm{{background:rgba(63,185,80,.10);color:#52c261;border:1px solid rgba(63,185,80,.25)}}
.hb-cooling{{background:rgba(210,153,34,.10);color:#d29922;border:1px solid rgba(210,153,34,.25)}}
.hb-concept{{background:rgba(163,113,247,.12);color:#a78bfa;border:1px solid rgba(163,113,247,.25);cursor:pointer;transition:all .15s}}
.hb-rotation{{background:rgba(56,139,253,.12);color:#58a6ff;border:1px solid rgba(56,139,253,.25);cursor:pointer;transition:all .15s}}
.hbadge.hb-active{{outline:2px solid #fff;outline-offset:1px;opacity:1!important}}
.hbadge:hover:not(.hb-active){{opacity:.75}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:16px;padding:24px}}
.card{{background:#161b22;border:1px solid #21262d;border-radius:12px;overflow:hidden;
  transition:border-color .2s,transform .2s;animation:fadeIn .5s ease forwards;opacity:0}}
.card:hover{{border-color:#388bfd;transform:translateY(-3px);box-shadow:0 8px 24px rgba(0,0,0,.4)}}
@keyframes fadeIn{{to{{opacity:1}}}}
.card-header{{display:flex;align-items:center;gap:12px;padding:14px 16px;border-bottom:1px solid #21262d}}
.rank{{background:#21262d;border-radius:8px;width:34px;height:34px;display:flex;align-items:center;
  justify-content:center;font-weight:700;font-size:13px;color:#8b949e;flex-shrink:0}}
.info{{flex:1;min-width:0}}
.ticker{{font-size:16px;font-weight:700;display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}}
.tcode{{letter-spacing:1px;color:#e6edf3}}
.tname{{font-size:12px;font-weight:500;color:#8b949e;letter-spacing:0}}
.cname{{font-size:11px;color:#8b949e;margin-top:2px}}
.badge{{padding:5px 12px;border-radius:20px;font-size:13px;font-weight:600;white-space:nowrap;flex-shrink:0}}
.g-alpha{{background:rgba(248,81,73,.15);color:#ff6b6b;border:1px solid rgba(248,81,73,.3)}}
.g-beta{{background:rgba(88,166,255,.15);color:#58a6ff;border:1px solid rgba(88,166,255,.3)}}
.g-accum{{background:rgba(210,153,34,.15);color:#e3a008;border:1px solid rgba(210,153,34,.3)}}
.ctag-row{{padding:6px 16px;border-bottom:1px solid #21262d;display:flex;gap:5px;flex-wrap:wrap}}
.ctag{{font-size:10px;font-weight:600;padding:2px 8px;border-radius:10px}}
.ctag-hot{{background:rgba(163,113,247,.18);color:#c084fc;border:1px solid rgba(163,113,247,.4)}}
.ctag-cold{{background:rgba(139,148,158,.08);color:#6e7681;border:1px solid rgba(139,148,158,.18)}}
.metrics{{display:flex;border-bottom:1px solid #21262d}}
.m{{flex:1;padding:10px 6px;text-align:center;border-right:1px solid #21262d}}
.m:last-child{{border-right:none}}
.mv{{font-size:13px;font-weight:600}}.ml{{font-size:10px;color:#8b949e;margin-top:2px}}
.pos{{color:#3fb950}}.neg{{color:#f85149}}.conf-watch{{color:#58a6ff}}
.chart-wrap{{background:#0d1117;border-top:1px solid #21262d}}
.chart-ctrl{{display:flex;gap:4px;padding:6px 8px 4px}}
.ct-btn{{font-size:10px;font-weight:600;padding:3px 8px;border-radius:10px;border:1px solid #30363d;background:transparent;color:#8b949e;cursor:pointer;transition:all .15s;line-height:1.4}}
.ct-btn.active{{background:#1c2333;color:#e6edf3;border-color:#58a6ff}}
.ct-btn:hover:not(.active){{color:#c9d1d9;border-color:#484f58}}
.ct-btn::before{{margin-right:3px;font-size:9px}}
.ct-btn[data-s="bb"]::before{{content:"●";color:#58a6ff}}
.ct-btn[data-s="ma5"]::before{{content:"●";color:#ffd700}}
.ct-btn[data-s="ma10"]::before{{content:"●";color:#ff7f50}}
.ct-btn[data-s="ma20"]::before{{content:"●";color:#00e5ff}}
.ct-btn[data-s="ma60"]::before{{content:"●";color:#da70d6}}
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
.rec-text{{font-size:13px;color:#c9d1d9;line-height:1.7}}
.rec-chip,.rec-risk{{font-size:11px;line-height:1.5;margin-top:4px;display:flex;gap:6px;align-items:baseline}}
.rec-chip{{color:#8b949e}}
.rec-risk{{color:#d29922}}
.rec-chip-label,.rec-risk-label{{font-size:10px;font-weight:700;padding:1px 6px;border-radius:8px;white-space:nowrap;flex-shrink:0}}
.rec-chip-label{{background:rgba(139,148,158,.15);color:#8b949e}}
.rec-risk-label{{background:rgba(210,153,34,.15);color:#e3a008}}
.key-signals{{display:flex;align-items:center;flex-wrap:wrap;gap:5px;padding:10px 16px;
  border-top:1px solid #21262d;background:rgba(255,255,255,.02)}}
.ks-label{{font-size:10px;color:#484f58;font-weight:600;margin-right:2px;white-space:nowrap}}
.ks-tag{{display:inline-flex;align-items:center;gap:3px;font-size:10px;font-weight:600;
  padding:2px 7px;border-radius:8px;white-space:nowrap}}
.ks-k{{opacity:.75}}
.ks-v{{font-weight:700}}
.ks-neutral{{background:rgba(139,148,158,.1);color:#8b949e;border:1px solid rgba(139,148,158,.2)}}
.ks-warm{{background:rgba(56,139,253,.1);color:#58a6ff;border:1px solid rgba(56,139,253,.2)}}
.ks-hot{{background:rgba(63,185,80,.1);color:#3fb950;border:1px solid rgba(63,185,80,.2)}}
/* ── Portfolio hero ───────────────────────────────────────────────────── */
.pf-hero{{background:linear-gradient(135deg,#0a1f2e,#0d2137,#0a2744);padding:24px 28px;
  border-bottom:1px solid #21262d}}
.pf-hero-head{{font-size:20px;font-weight:800;color:#e6edf3;margin-bottom:8px}}
.pf-hero-summary{{display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin-bottom:18px;
  font-size:13px;color:#c9d1d9}}
.pf-stat{{background:rgba(255,255,255,.05);padding:6px 14px;border-radius:18px;
  border:1px solid rgba(255,255,255,.08)}}
.pf-stat b{{color:#e6edf3;font-size:15px;margin-left:4px}}
.pf-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px}}
.pf-card{{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:12px 14px}}
.pf-card-head{{font-size:13px;font-weight:700;color:#c9d1d9;margin-bottom:10px;
  padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,.06)}}
.pf-card-exit{{border-color:#f85149;background:rgba(248,81,73,.06)}}
.pf-card-hold{{border-color:#388bfd}}
.pf-card-buy{{border-color:#3fb950;background:rgba(63,185,80,.06)}}
.pf-card-empty{{text-align:center;color:#8b949e;font-size:13px;padding:20px}}
.pf-row{{display:grid;grid-template-columns:1.1fr 1.5fr .8fr;gap:8px;align-items:center;
  padding:8px 6px;border-radius:6px;font-size:12px;color:#c9d1d9;
  border-bottom:1px solid rgba(255,255,255,.04)}}
.pf-row:last-child{{border-bottom:none}}
.pf-row:hover{{background:rgba(255,255,255,.03)}}
.pf-tk{{font-weight:700;color:#e6edf3;font-size:13px}}
.pf-nm{{color:#8b949e;font-size:11px;font-weight:400}}
.pf-tier{{display:inline-block;font-size:10px;font-weight:800;padding:1px 6px;border-radius:3px;
  color:#0d1117;margin-right:4px;vertical-align:middle}}
.pf-detail{{font-size:11px;color:#c9d1d9;line-height:1.5}}
.pf-detail b{{color:#e6edf3}}
.pf-stop{{color:#f0b429}}
.pf-tp{{color:#3fb950}}
.pf-pnl{{font-weight:800;text-align:right;font-size:15px;font-variant-numeric:tabular-nums}}
.pf-pnl-pos{{color:#3fb950}}
.pf-pnl-neg{{color:#f85149}}
.pf-pnl-neu{{color:#8b949e}}
.pf-days{{font-size:10px;color:#8b949e;font-weight:400}}
.pf-alloc{{font-weight:800;text-align:right;font-size:18px;color:#58a6ff;font-variant-numeric:tabular-nums}}
.pf-conf{{font-size:10px;color:#8b949e;font-weight:400}}
.pf-reason{{font-size:11px;text-align:right}}
.pf-tag{{display:inline-block;padding:2px 6px;border-radius:3px;font-size:10px;font-weight:700}}
.pf-tag-exit{{background:#f85149;color:#fff}}
.pf-reason-text{{color:#8b949e;font-size:10px}}

/* ── Phase 4.50 — NT$3M budget allocation panel ─────────────────────── */
.budget-panel{{background:linear-gradient(180deg,#0a1f2e,#0d1117);padding:24px 28px;
  border-top:1px solid #21262d;border-bottom:1px solid #21262d}}
.budget-head{{font-size:18px;font-weight:800;color:#e6edf3;margin-bottom:10px}}
.budget-stats{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px;font-size:13px;color:#c9d1d9}}
.bs-stat{{background:rgba(255,255,255,.04);padding:5px 12px;border-radius:14px;
  border:1px solid rgba(255,255,255,.07)}}
.bs-stat b{{color:#e6edf3;font-size:14px;margin-left:3px}}
.budget-table{{width:100%;border-collapse:collapse;font-size:12px;
  background:#161b22;border:1px solid #21262d;border-radius:8px;overflow:hidden}}
.budget-table thead th{{background:#1c2333;color:#8b949e;padding:8px 10px;text-align:left;
  font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;
  border-bottom:1px solid #21262d}}
.budget-table tbody td{{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.04);
  color:#c9d1d9}}
.budget-table tbody tr:hover{{background:rgba(255,255,255,.03)}}
.bg-tier{{font-weight:800;text-align:center;padding:6px 8px !important;
  font-size:13px}}
.bg-name{{font-size:10px;color:#8b949e}}
.bg-sector{{color:#8b949e;font-size:11px}}
.bg-num{{text-align:right;font-variant-numeric:tabular-nums;color:#e6edf3}}
.bg-lots{{color:#8b949e;font-size:11px;text-align:right}}
.bg-twd{{text-align:right;font-variant-numeric:tabular-nums;color:#ff6b6b}}
.bg-stop{{text-align:right;color:#f0b429;font-variant-numeric:tabular-nums;font-size:11px}}
.bg-tp{{text-align:right;color:#3fb950;font-variant-numeric:tabular-nums;font-size:11px}}
.bg-status{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;
  font-weight:700;white-space:nowrap}}
.bg-status-held{{background:rgba(56,139,253,.15);color:#58a6ff;border:1px solid rgba(56,139,253,.3)}}
.bg-status-buy{{background:rgba(63,185,80,.15);color:#3fb950;border:1px solid rgba(63,185,80,.3)}}
.bg-watch{{margin-top:14px;padding:10px 14px;background:rgba(240,180,41,.06);
  border-left:3px solid #f0b429;border-radius:0 6px 6px 0;font-size:11px;color:#c9d1d9}}

.sf-panel{{background:linear-gradient(180deg,#0a1320,#0d1117);border-top:1px solid #21262d;
  border-bottom:1px solid #21262d;padding:18px 28px}}
.sf-title{{font-size:17px;font-weight:700;color:#e6edf3;margin-bottom:2px}}
.sf-sub{{font-size:11px;color:#8b949e;font-weight:400;margin-left:6px}}
.sf-period{{font-size:10px;color:#6e7681;margin-bottom:12px;letter-spacing:.5px}}
.sf-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.sf-col{{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:10px 12px}}
.sf-coltitle{{font-size:13px;font-weight:700;margin-bottom:8px;padding-bottom:6px;
  border-bottom:1px solid rgba(255,255,255,.06)}}
.sf-rows{{display:flex;flex-direction:column;gap:3px}}
.sf-row{{display:grid;grid-template-columns:1.2fr 1.6fr .5fr .6fr .9fr 1.3fr;
  gap:8px;align-items:center;padding:6px 8px;font-size:13px;color:#c9d1d9;
  border-radius:4px}}
.sf-row:hover{{background:rgba(255,255,255,.04)}}
.sf-head{{font-size:10px;color:#6e7681;text-transform:uppercase;letter-spacing:.5px;
  border-bottom:1px solid rgba(255,255,255,.05);padding-bottom:6px;margin-bottom:2px}}
.sf-head:hover{{background:none}}
.sf-name{{font-weight:600;color:#e6edf3;white-space:nowrap}}
.sf-spark{{display:flex;align-items:center}}
.sf-spark svg{{display:block}}
.sf-now{{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}}
.sf-delta{{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}}
.sf-trend{{font-size:11px;white-space:nowrap}}
.sf-meta{{font-size:10px;color:#8b949e}}
@media (max-width: 900px){{
  .sf-grid{{grid-template-columns:1fr}}
}}
.alloc-link-card{{display:flex;align-items:center;gap:16px;padding:14px 24px;
  background:linear-gradient(135deg,#1a1a2e,#1e2a4a);border-top:1px solid #21262d;
  border-bottom:1px solid #21262d}}
.alc-icon{{font-size:28px}}
.alc-body{{flex:1;min-width:0}}
.alc-head{{font-size:15px;font-weight:700;color:#e6edf3;margin-bottom:4px}}
.alc-sub{{font-size:11px;color:#8b949e;font-weight:400;margin-left:6px}}
.alc-counts{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px}}
.alc-tier-pill{{font-weight:700;padding:2px 8px;border-radius:10px;font-size:11px}}
.alc-stat{{color:#c9d1d9;margin-left:8px}}
.alc-open{{padding:8px 16px;background:#238636;color:#fff;border-radius:6px;
  text-decoration:none;font-weight:600;font-size:13px;white-space:nowrap}}
.alc-open:hover{{background:#2ea043}}
.alloc-panel{{background:linear-gradient(180deg,#1a1a2e,#0d1117);border-top:1px solid #21262d;
  border-bottom:1px solid #21262d;padding:20px 28px}}
.alloc-title{{font-size:18px;font-weight:700;color:#e6edf3;margin-bottom:6px}}
.alloc-sub{{font-size:12px;color:#8b949e;font-weight:400;margin-left:6px}}
.alloc-summary{{font-size:13px;color:#c9d1d9;font-style:italic;margin-bottom:14px;line-height:1.5}}
.alloc-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:12px}}
.alloc-tier{{border:1px solid;border-radius:10px;padding:12px;background:#161b22}}
.alloc-tier-head{{display:flex;align-items:center;gap:10px;margin-bottom:8px;padding-bottom:8px;
  border-bottom:1px solid rgba(255,255,255,.06)}}
.alloc-tier-badge{{font-weight:800;padding:2px 10px;border-radius:6px;font-size:14px}}
.alloc-tier-label{{font-size:12px;color:#8b949e}}
.alloc-tier-count{{margin-left:auto;font-size:11px;color:#8b949e;
  background:rgba(255,255,255,.05);padding:2px 8px;border-radius:10px}}
.alloc-rows{{display:flex;flex-direction:column;gap:6px}}
.alloc-row{{display:grid;grid-template-columns:1.6fr .6fr .4fr 2.2fr;gap:8px;align-items:center;
  padding:6px 8px;background:rgba(255,255,255,.02);border-radius:6px;font-size:12px}}
.alloc-row:hover{{background:rgba(255,255,255,.05)}}
.alloc-tk{{display:flex;flex-direction:column;gap:2px}}
.alloc-code{{font-weight:700;color:#e6edf3}}
.alloc-name{{color:#c9d1d9;font-size:11px}}
.alloc-ind{{color:#8b949e;font-size:10px}}
.alloc-pct{{font-weight:700;text-align:right;font-size:14px}}
.alloc-rot{{color:#8b949e;text-align:right;font-size:11px;font-variant-numeric:tabular-nums}}
.alloc-why{{color:#8b949e;font-size:11px;line-height:1.4}}
.alloc-warns{{margin-top:12px;display:flex;flex-direction:column;gap:6px}}
.alloc-warn{{padding:8px 12px;border-left:3px solid;background:rgba(240,180,41,.08);
  border-radius:0 6px 6px 0;font-size:12px;color:#c9d1d9}}
.alloc-warn-icon{{font-weight:700;margin-right:6px}}
.alloc-footer{{margin-top:14px;padding-top:10px;border-top:1px solid #21262d;
  font-size:12px;color:#c9d1d9;text-align:center}}
.filter-bar{{position:sticky;top:0;z-index:100;background:#0d1117cc;backdrop-filter:blur(12px);
  border-bottom:1px solid #21262d;padding:10px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
.fb-group{{display:flex;align-items:center;gap:8px}}
#conceptFilterGroup{{flex-wrap:wrap;row-gap:6px;max-width:100%;align-items:flex-start}}
#conceptFilterGroup .fb-label{{align-self:center}}
.fb-label{{font-size:11px;color:#8b949e;white-space:nowrap}}
.fb-pill{{font-size:12px;font-weight:600;padding:5px 14px;border-radius:20px;border:1px solid #30363d;
  background:transparent;color:#8b949e;cursor:pointer;transition:all .15s;white-space:nowrap}}
.fb-pill.active{{border-color:#388bfd;background:rgba(56,139,253,.15);color:#58a6ff}}
.fb-pill:hover:not(.active){{border-color:#484f58;color:#c9d1d9}}
.fb-slider{{width:100px;accent-color:#58a6ff;cursor:pointer}}
.fb-val{{font-size:12px;font-weight:700;color:#58a6ff;min-width:22px;text-align:right}}
.fb-select{{background:#161b22;border:1px solid #30363d;color:#c9d1d9;font-size:12px;
  padding:5px 10px;border-radius:8px;cursor:pointer;outline:none;max-width:150px}}
.fb-count{{margin-left:auto;font-size:12px;color:#8b949e}}
.fb-count span{{color:#e6edf3;font-weight:700}}
.card.hidden{{display:none}}
.rot-badge{{font-size:10px;font-weight:700;padding:2px 7px;border-radius:8px;vertical-align:middle}}
.rot-emerging{{background:rgba(61,185,130,.18);color:#3fb98b;border:1px solid rgba(61,185,130,.35)}}
.rot-hot{{background:rgba(248,81,73,.15);color:#ff7b7b;border:1px solid rgba(248,81,73,.3)}}
.rot-cooling{{background:rgba(139,148,158,.1);color:#6e7681;border:1px solid rgba(139,148,158,.2)}}
.sig-summary{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:8px 24px;
  background:#0d1117;border-bottom:1px solid #21262d;font-size:11px;color:#8b949e}}
.sig-summary-item{{display:flex;align-items:center;gap:5px;cursor:pointer;padding:3px 8px;
  border-radius:12px;border:1px solid transparent;transition:all .15s}}
.sig-summary-item:hover{{border-color:#30363d;background:rgba(255,255,255,.04)}}
.sig-summary-item.active{{border-color:#388bfd;background:rgba(56,139,253,.1)}}
.sig-dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
.sig-summary-label{{font-size:11px;font-weight:600;color:#c9d1d9}}
.sig-summary-cnt{{font-size:11px;color:#8b949e}}
</style>
</head>
<body>
{_render_portfolio_html(daily_portfolio)}
{_render_budget_allocation_html(budget_allocation)}
<div class="header">
  <h1>📈 預突破掃描</h1>
  <div class="subtitle">{_esc(scan_date)} &nbsp;·&nbsp; 收盤掃描 &nbsp;·&nbsp; 共 {len(filtered)} 支</div>
  <div class="stats">
    <div class="stat"><div class="sv" style="color:#ff6b6b">{n_long}</div><div class="sl">突破進場</div></div>
    <div class="stat"><div class="sv" style="color:#58a6ff">{n_watch}</div><div class="sl">等待確認</div></div>
  </div>
  {radar_html}
</div>
{_render_sector_flow_html()}
{_render_concept_flow_html()}
{_render_allocation_link_card(allocation_plan, allocation_html_path)}
<div class="filter-bar" id="filterBar">
  <div class="fb-group">
    <span class="fb-label">操作</span>
    <button class="fb-pill active" data-filter-action="ALL">全部</button>
    <button class="fb-pill" data-filter-action="LONG">突破進場</button>
    <button class="fb-pill" data-filter-action="WATCH">等待確認</button>
  </div>
  <div class="fb-group">
    <span class="fb-label">策略</span>
    <button class="fb-pill strategy-pill active" data-strategy="ALL">全部</button>
    <button class="fb-pill strategy-pill" data-strategy="early" style="border-color:#6e2a8a">提前佈局</button>
    <button class="fb-pill strategy-pill" data-strategy="confirm" style="border-color:#1a3a6a">確認型</button>
    <button class="fb-pill strategy-pill" data-strategy="track" style="border-color:#6a1a1a">追蹤型</button>
  </div>
  <div class="fb-group">
    <span class="fb-label">持倉</span>
    <button class="fb-pill horizon-pill active" data-horizon="ALL">全部</button>
    <button class="fb-pill horizon-pill" data-horizon="波段">波段</button>
    <button class="fb-pill horizon-pill" data-horizon="短線">短線</button>
  </div>
  <div class="fb-group">
    <span class="fb-label">BB 壓縮</span>
    <button class="fb-pill bb-pill active" data-bb="ALL">全部</button>
    <button class="fb-pill bb-pill" data-bb="ONLY" style="border-color:#3fb950" title="BB 寬度位於 60 日 35 分位內，且尚未突破">壓縮未突破</button>
  </div>
  {(('<div class="fb-group"><span class="fb-label">訊號型態</span>' +
     '<button class="fb-pill sigtype-pill active" data-sigtype="ALL">全部</button>' +
     "".join(f'<button class="fb-pill sigtype-pill" data-sigtype="{_esc(s)}">{_esc(s)}</button>'
             for s in unique_sig_types) +
     '</div>') if len(unique_sig_types) > 1 else "")}
  <div class="fb-group">
    <span class="fb-label">信心 ≥</span>
    <input type="range" class="fb-slider" id="confSlider" min="{min_confidence}" max="150" value="{min_confidence}" step="5">
    <span class="fb-val" id="confVal">{min_confidence}</span>
  </div>
  <div class="fb-group">
    <span class="fb-label">排序</span>
    <select class="fb-select" id="sortSelect">
      <option value="conf">信心分↓</option>
      <option value="strategy">策略優先</option>
      <option value="rotation">輪動熱度</option>
    </select>
  </div>
  <div class="fb-group">
    <span class="fb-label">產業</span>
    <select class="fb-select" id="indSelect">
      <option value="">全部</option>
      {"".join(f'<option value="{_esc(ind)}">{_esc(ind)}</option>' for ind in unique_industries)}
    </select>
  </div>
  {(('<div class="fb-group" id="conceptFilterGroup"><span class="fb-label">題材</span>' +
     "".join(f'<button class="fb-pill concept-pill" data-concept="{_esc(n)}">{_esc(n)}</button>'
             for n in all_concept_names) +
     '</div>') if all_concept_names else "")}
  <div class="fb-count">顯示 <span id="visCount">{len(filtered)}</span> / {len(filtered)} 支</div>
</div>
<div class="sig-summary" id="sigSummary">
  <span style="font-size:11px;color:#8b949e;white-space:nowrap">訊號分布：</span>
</div>
<div class="grid" id="cardGrid">
{"".join(cards)}
</div>
<div class="footer">預突破掃描自動生成 · {_esc(scan_date)}</div>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
const CHART_DATA = {_json.dumps(chart_data, ensure_ascii=False)};
const _CS = {{}};
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
    const bbMid = chart.addLineSeries(Object.assign({{}}, lo, {{ color: "#58a6ff" }}));
    bbMid.setData(data.bb_mid);
    const bbUpper = chart.addLineSeries(Object.assign({{}}, lo, {{ color: "#e3b341" }}));
    bbUpper.setData(data.bb_upper);
    const bbLower = chart.addLineSeries(Object.assign({{}}, lo, {{ color: "#a371f7" }}));
    bbLower.setData(data.bb_lower);
    const ma5  = chart.addLineSeries(Object.assign({{}}, lo, {{ color: "#ffd700" }}));
    const ma10 = chart.addLineSeries(Object.assign({{}}, lo, {{ color: "#ff7f50" }}));
    const ma20 = chart.addLineSeries(Object.assign({{}}, lo, {{ color: "#00e5ff" }}));
    const ma60 = chart.addLineSeries(Object.assign({{}}, lo, {{ color: "#da70d6" }}));
    chart.timeScale().fitContent();
    _CS[ticker] = {{ bb: [bbMid, bbUpper, bbLower], ma5: ma5, ma10: ma10, ma20: ma20, ma60: ma60 }};
    const wrap = e.target.closest('.chart-wrap');
    if (wrap) {{
      wrap.querySelectorAll('.ct-btn').forEach(function(btn) {{
        btn.addEventListener('click', function() {{
          const s = _CS[ticker];
          const name = this.dataset.s;
          const wasActive = this.classList.contains('active');
          if (name === 'bb') {{
            if (wasActive) {{ s.bb.forEach(function(ser) {{ ser.setData([]); }}); }}
            else {{ s.bb[0].setData(data.bb_mid); s.bb[1].setData(data.bb_upper); s.bb[2].setData(data.bb_lower); }}
          }} else {{
            const ser = s[name];
            if (ser) {{ ser.setData(wasActive ? [] : (data[name] || [])); }}
          }}
          this.classList.toggle('active');
        }});
      }});
    }}
  }});
}}, {{ rootMargin: "100px" }});
document.querySelectorAll(".chart[data-ticker]").forEach(function(el) {{ _obs.observe(el); }});

// --- Signal summary bar ---
(function() {{
  var SIG_META = {{
    "爆量★":  {{ color:"#ef5350", strategy:"track" }},
    "爆量":   {{ color:"#f57c70", strategy:"track" }},
    "回調":   {{ color:"#e3b341", strategy:"track" }},
    "趨勢延伸":{{ color:"#58a6ff", strategy:"confirm" }},
    "蓄積★":  {{ color:"#3fb950", strategy:"confirm" }},
    "蓄積":   {{ color:"#26a69a", strategy:"confirm" }},
    "法人建倉":{{ color:"#dd44ff", strategy:"early" }},
    "籌碼轉移":{{ color:"#b044dd", strategy:"early" }},
    "VCP":    {{ color:"#44ddcc", strategy:"early" }},
    "旗形":   {{ color:"#ddcc44", strategy:"early" }},
  }};
  var STRATEGY_LABEL = {{ early:"提前佈局", confirm:"確認型", track:"追蹤型" }};
  var STRATEGY_COLOR = {{ early:"#dd44ff", confirm:"#388bfd", track:"#ef5350" }};
  var summary = document.getElementById("sigSummary");
  var cards = Array.from(document.querySelectorAll("#cardGrid .card"));

  // Count per sig type
  var counts = {{}};
  cards.forEach(function(c) {{
    var t = c.dataset.sigtype || "蓄積";
    counts[t] = (counts[t] || 0) + 1;
  }});

  // Build summary items grouped by strategy
  var strategies = ["early","confirm","track"];
  strategies.forEach(function(strat) {{
    var stratTotal = 0;
    var items = [];
    Object.keys(SIG_META).forEach(function(t) {{
      if (SIG_META[t].strategy !== strat) return;
      var cnt = counts[t] || 0;
      if (!cnt) return;
      stratTotal += cnt;
      items.push({{ t:t, cnt:cnt, color:SIG_META[t].color }});
    }});
    if (!stratTotal) return;
    var grpEl = document.createElement("div");
    grpEl.style.cssText = "display:flex;align-items:center;gap:6px;padding:2px 10px;border-radius:12px;border:1px solid " + STRATEGY_COLOR[strat] + "33;background:" + STRATEGY_COLOR[strat] + "11;cursor:pointer";
    grpEl.dataset.strategyFilter = strat;
    grpEl.title = "點擊篩選 " + STRATEGY_LABEL[strat];
    var grpLabel = document.createElement("span");
    grpLabel.textContent = STRATEGY_LABEL[strat];
    grpLabel.style.cssText = "font-size:10px;font-weight:700;color:" + STRATEGY_COLOR[strat] + ";white-space:nowrap";
    grpEl.appendChild(grpLabel);
    items.forEach(function(it) {{
      var dot = document.createElement("span");
      dot.className = "sig-dot";
      dot.style.background = it.color;
      var lbl = document.createElement("span");
      lbl.className = "sig-summary-label";
      lbl.textContent = it.t;
      var cnt = document.createElement("span");
      cnt.className = "sig-summary-cnt";
      cnt.textContent = "(" + it.cnt + ")";
      var wrap = document.createElement("span");
      wrap.className = "sig-summary-item";
      wrap.dataset.summaryType = it.t;
      [dot, lbl, cnt].forEach(function(el) {{ wrap.appendChild(el); }});
      grpEl.appendChild(wrap);
    }});
    summary.appendChild(grpEl);
  }});
}})();

// --- Filter bar ---
(function() {{
  var activeAction = "ALL";
  var activeSigType = "ALL";
  var activeStrategy = "ALL";
  var activeHorizon = "ALL";
  var activeBB = "ALL";
  var activeSort = "conf";
  var minConf = {min_confidence};
  var activeInd = "";
  var selectedConcepts = new Set();

  var STRATEGY_ORDER = {{ early: 0, confirm: 1, track: 2 }};
  var ROTATION_ORDER = {{ EMERGING: 0, HOT: 1, "": 2, COOLING: 3 }};

  function getCards() {{
    return Array.from(document.querySelectorAll("#cardGrid .card"));
  }}

  function applySort() {{
    var grid = document.getElementById("cardGrid");
    var cards = getCards();
    if (activeSort === "conf") {{
      cards.sort(function(a, b) {{
        return parseInt(b.dataset.conf, 10) - parseInt(a.dataset.conf, 10);
      }});
    }} else if (activeSort === "strategy") {{
      cards.sort(function(a, b) {{
        var sa = STRATEGY_ORDER[a.dataset.strategy] !== undefined ? STRATEGY_ORDER[a.dataset.strategy] : 9;
        var sb = STRATEGY_ORDER[b.dataset.strategy] !== undefined ? STRATEGY_ORDER[b.dataset.strategy] : 9;
        if (sa !== sb) return sa - sb;
        return parseInt(b.dataset.conf, 10) - parseInt(a.dataset.conf, 10);
      }});
    }} else if (activeSort === "rotation") {{
      cards.sort(function(a, b) {{
        var ra = ROTATION_ORDER[a.dataset.rotation] !== undefined ? ROTATION_ORDER[a.dataset.rotation] : 2;
        var rb = ROTATION_ORDER[b.dataset.rotation] !== undefined ? ROTATION_ORDER[b.dataset.rotation] : 2;
        if (ra !== rb) return ra - rb;
        return parseInt(b.dataset.conf, 10) - parseInt(a.dataset.conf, 10);
      }});
    }}
    cards.forEach(function(c) {{ grid.appendChild(c); }});
  }}

  function applyFilters() {{
    var cards = getCards();
    var visible = 0;
    cards.forEach(function(c) {{
      var a = c.dataset.action;
      var conf = parseInt(c.dataset.conf, 10);
      var ind = c.dataset.industry;
      var sigtype = c.dataset.sigtype || "";
      var strategy = c.dataset.strategy || "";
      var horizon = c.dataset.horizon || "";
      var bbCompressed = c.dataset.bbCompressed === "1";
      var cardConceptsRaw = c.dataset.concepts || "";
      var cardConcepts = cardConceptsRaw ? new Set(cardConceptsRaw.split(",")) : new Set();
      var conceptMatch = selectedConcepts.size === 0
        || [...selectedConcepts].every(function(sc) {{ return cardConcepts.has(sc); }});
      var show = (activeAction === "ALL" || a === activeAction)
               && (activeSigType === "ALL" || sigtype === activeSigType)
               && (activeStrategy === "ALL" || strategy === activeStrategy)
               && (activeHorizon === "ALL" || horizon === activeHorizon)
               && (activeBB === "ALL" || (activeBB === "ONLY" && bbCompressed))
               && conf >= minConf
               && (activeInd === "" || ind === activeInd)
               && conceptMatch;
      c.classList.toggle("hidden", !show);
      if (show) visible++;
    }});
    document.getElementById("visCount").textContent = visible;
  }}

  function applyFiltersAndSort() {{
    applySort();
    applyFilters();
  }}

  function toggleConcept(name) {{
    var pill = document.querySelector('.concept-pill[data-concept="' + CSS.escape(name) + '"]');
    if (selectedConcepts.has(name)) {{
      selectedConcepts.delete(name);
      if (pill) pill.classList.remove("active");
    }} else {{
      selectedConcepts.add(name);
      if (pill) pill.classList.add("active");
    }}
    // sync radar badges
    document.querySelectorAll('.hbadge[data-concept]').forEach(function(b) {{
      b.classList.toggle("hb-active", selectedConcepts.has(b.dataset.concept));
    }});
    applyFilters();
  }}

  window.toggleConceptBadge = function(badge) {{
    toggleConcept(badge.dataset.concept);
  }};

  document.querySelectorAll(".concept-pill").forEach(function(btn) {{
    btn.addEventListener("click", function() {{
      toggleConcept(btn.dataset.concept);
    }});
  }});

  document.querySelectorAll("[data-filter-action]").forEach(function(btn) {{
    btn.addEventListener("click", function() {{
      document.querySelectorAll("[data-filter-action]").forEach(function(b) {{ b.classList.remove("active"); }});
      btn.classList.add("active");
      activeAction = btn.dataset.filterAction;
      applyFilters();
    }});
  }});

  document.querySelectorAll(".sigtype-pill").forEach(function(btn) {{
    btn.addEventListener("click", function() {{
      document.querySelectorAll(".sigtype-pill").forEach(function(b) {{ b.classList.remove("active"); }});
      btn.classList.add("active");
      activeSigType = btn.dataset.sigtype;
      applyFilters();
    }});
  }});

  document.querySelectorAll(".strategy-pill").forEach(function(btn) {{
    btn.addEventListener("click", function() {{
      document.querySelectorAll(".strategy-pill").forEach(function(b) {{ b.classList.remove("active"); }});
      btn.classList.add("active");
      activeStrategy = btn.dataset.strategy;
      applyFilters();
    }});
  }});

  document.querySelectorAll(".horizon-pill").forEach(function(btn) {{
    btn.addEventListener("click", function() {{
      document.querySelectorAll(".horizon-pill").forEach(function(b) {{ b.classList.remove("active"); }});
      btn.classList.add("active");
      activeHorizon = btn.dataset.horizon;
      applyFilters();
    }});
  }});

  document.querySelectorAll(".bb-pill").forEach(function(btn) {{
    btn.addEventListener("click", function() {{
      document.querySelectorAll(".bb-pill").forEach(function(b) {{ b.classList.remove("active"); }});
      btn.classList.add("active");
      activeBB = btn.dataset.bb;
      applyFilters();
    }});
  }});

  // clicking a strategy group in sig-summary bar filters by strategy
  document.querySelectorAll("#sigSummary [data-strategy-filter]").forEach(function(el) {{
    el.addEventListener("click", function() {{
      var strat = el.dataset.strategyFilter;
      var pills = document.querySelectorAll(".strategy-pill");
      pills.forEach(function(b) {{ b.classList.remove("active"); }});
      var target = document.querySelector('.strategy-pill[data-strategy="' + strat + '"]');
      if (target) target.classList.add("active");
      activeStrategy = strat;
      applyFilters();
    }});
  }});

  document.getElementById("sortSelect").addEventListener("change", function() {{
    activeSort = this.value;
    applyFiltersAndSort();
  }});

  var slider = document.getElementById("confSlider");
  slider.addEventListener("input", function() {{
    minConf = parseInt(this.value, 10);
    document.getElementById("confVal").textContent = minConf;
    applyFilters();
  }});

  document.getElementById("indSelect").addEventListener("change", function() {{
    activeInd = this.value;
    applyFilters();
  }});
}})();
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
    parser.add_argument("--top", type=int, default=30, help="顯示前 N 名（預設: 30；--by-industry 模式使用）")
    parser.add_argument("--min-confidence", type=int, default=58, help="最低信心分數門檻（預設: 58）")
    parser.add_argument("--workers", type=int, default=5, help="並行 worker 數（預設: 5；建議 3-8，受 FinMind rate limit 限制）")
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
        "--show",
        metavar="DATE",
        help="顯示指定日期的掃描結果（從 DB 查詢，例: --show 2026-04-10）",
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
    parser.add_argument(
        "--by-industry",
        action="store_true",
        help="按產業分組顯示所有結果（舊行為）；預設為 CONVICTION+WATCHLIST 焦點清單",
    )
    args = parser.parse_args()

    # ── show 模式：從 CSV 印出歷史結果 ──────────────────────────────────────
    if args.show is not None:
        import os
        if not os.environ.get("DATABASE_URL"):
            _console.print("[red]--show 需要 DATABASE_URL 設定（signal_outcomes 表）[/red]")
            return
        from taiwan_stock_agent.infrastructure.db import get_connection, init_pool
        init_pool()

        show_date = args.show.strip() if args.show.strip() else ""
        if not show_date:
            # 從 DB 取可用日期
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT signal_date FROM signal_outcomes WHERE source='live' "
                        "ORDER BY signal_date DESC LIMIT 90"
                    )
                    available = [str(r[0]) for r in cur.fetchall()]
            if not available:
                _console.print("[red]DB 中找不到任何掃描結果[/red]")
                return
            import questionary
            show_date = questionary.select(
                "選擇掃描日期",
                choices=available,
                default=available[0],
                style=questionary.Style([
                    ("selected", "fg:cyan bold"),
                    ("pointer", "fg:cyan bold"),
                    ("highlighted", "fg:cyan"),
                    ("question", "bold"),
                ]),
            ).ask()
            if show_date is None:
                return

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ticker, action, confidence_score, entry_price,
                           stop_loss, halt_flag, score_breakdown
                    FROM signal_outcomes
                    WHERE signal_date = %s AND source = 'live'
                    ORDER BY confidence_score DESC
                    """,
                    (show_date,),
                )
                db_rows = cur.fetchall()

        if not db_rows:
            _console.print(f"[yellow]DB 中找不到 {show_date} 的掃描結果[/yellow]")
            return

        results = [
            {
                "ticker": r[0],
                "action": r[1],
                "confidence": r[2],
                "free_tier": True,
                "halt": bool(r[5]),
                "error": None,
                "entry_bid": float(r[3] or 0),
                "stop_loss": float(r[4] or 0),
                "target": 0.0,
                "momentum": "",
                "chip": "",
                "risk": "",
                "flags": [],
                "trend_score": 0,
            }
            for r in db_rows
        ]
        ind_map = _build_industry_map()
        if ind_map:
            _print_by_industry(results, args.top, args.min_confidence, scan_date=show_date, name_map=_build_name_map(), industry_map=ind_map)
        else:
            _print_table(results, args.top, args.min_confidence, scan_date=show_date, name_map=_build_name_map(), sort_by="confidence")
        return

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
        llm_provider, llm_top, label_repo,
        industry_map=industry_map,
        name_map=name_map,
        market_map=market_map,
        sort_by=args.sort_by,
        by_industry=args.by_industry,
    )

    if args.notify:
        _notify_telegram(args.date, args.top, args.min_confidence)


def _tg_escape(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _notify_telegram(scan_date, top: int, min_confidence: int) -> None:
    """Query DB for today's signals and push the opening list to Telegram."""
    try:
        _do_notify_telegram(scan_date, top, min_confidence)
    except Exception as exc:
        import traceback
        _console.print(f"  [red]❌ _notify_telegram 例外：{exc}[/red]")
        _console.print(traceback.format_exc())


def _do_notify_telegram(scan_date, top: int, min_confidence: int) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        _console.print("  [yellow]⚠ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未設定，略過推播[/yellow]")
        return

    import os as _os
    if not _os.environ.get("DATABASE_URL"):
        _console.print("  [yellow]⚠ DATABASE_URL 未設定，略過 TG 推播[/yellow]")
        return

    from taiwan_stock_agent.infrastructure.db import get_connection, init_pool
    init_pool()
    name_map = _build_name_map()
    signals: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ticker, action, confidence_score, entry_price,
                       stop_loss, score_breakdown
                FROM signal_outcomes
                WHERE signal_date = %s AND source = 'live'
                  AND action IN ('LONG', 'WATCH')
                  AND confidence_score >= %s
                  AND halt_flag = FALSE
                ORDER BY confidence_score DESC
                """,
                (scan_date, min_confidence),
            )
            for row in cur.fetchall():
                ticker, action, conf, entry_price, stop_loss, breakdown = row
                signals.append({
                    "ticker": ticker,
                    "name": name_map.get(ticker, ""),
                    "action": action,
                    "confidence": conf,
                    "trend_score": 0,
                    "entry_bid": float(entry_price or 0),
                    "target": 0.0,
                    "stop_loss": float(stop_loss or 0),
                    "flags": "",
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
            f"   信心 {s['confidence']:.1f}\n"
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
