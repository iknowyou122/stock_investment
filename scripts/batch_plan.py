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

from taiwan_stock_agent.agents.strategist_agent import StrategistAgent
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
        if r.get("proximity_pts", 0) == 12:  # 12 = max proximity band (92-99% of 20d high); see _proximity_score()
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


def _merge_unified_signals(
    tce_results: list[dict],
    pullback_results: list[dict],
    surge_results: list[dict],
) -> list[dict]:
    """Merge TCE, pullback, and surge results into one unified list.

    Deduplicates by ticker: keeps highest-confidence result as primary;
    appends other signal_types to secondary_types list.
    Halted/error TCE results are replaced if a valid signal exists for that ticker.
    """
    merged: dict[str, dict] = {r["ticker"]: r for r in tce_results}

    for r in [*pullback_results, *surge_results]:
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


_SIG_COLORS = {
    "爆量★": "bold bright_red",
    "爆量": "red",
    "回調": "bright_yellow",
    "趨勢延伸": "bright_cyan",
    "蓄積★": "bright_green",
    "蓄積": "cyan",
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

    # 注入大盤融資維持率至 taifex_context（Gate 0 macro filter）
    taifex_ctx: dict = {}
    margin_rate = shared_paid.fetch_market_margin_maintenance(analysis_date)
    if margin_rate is not None:
        taifex_ctx["margin_maintenance_rate"] = margin_rate
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
    llm_provider=None,
    llm_top: int | None = None,
    label_repo=None,
    industry_map: dict[str, str] | None = None,
    save_db: bool = True,
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

    # ── Merge all three signal types into one unified result list ──────────────
    results = _merge_unified_signals(results, pullback_results, surge_db_results)

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

    _print_score_health(
        [r["confidence"] for r in results
         if not r.get("halt") and r.get("error") is None
         and r.get("action") in ("LONG", "WATCH")],
    )

    html_path = (Path(__file__).resolve().parents[1] / "data" / "scans" / f"scan_{analysis_date}.html")
    _generate_plan_html(results, str(analysis_date), html_path,
                        name_map=name_map or {}, industry_map=industry_map or {},
                        market_map=market_map or {},
                        heat_summary=_load_heat_summary(),
                        llm_provider=llm_provider,
                        min_confidence=min_confidence,
                        finmind_client=_shared_finmind)
    _console.print(f"  [dim cyan]📄 HTML: file://{html_path.resolve()}[/dim cyan]")
    import subprocess, sys as _sys
    if _sys.platform == "darwin":
        subprocess.Popen(["open", str(html_path)])


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
    empty: dict = {"candles": [], "bb_upper": [], "bb_mid": [], "bb_lower": []}
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
        return {"candles": rows[period - 1:], "bb_upper": bb_upper, "bb_mid": bb_mid, "bb_lower": bb_lower}
    except Exception:
        return empty


def _fetch_plan_chart(ticker: str, market: str) -> dict:
    """Fetch 5-month daily OHLCV + Bollinger Bands (20,2) via yfinance."""
    suffix = ".TW" if market == "TSE" else ".TWO"
    empty: dict = {"candles": [], "bb_upper": [], "bb_mid": [], "bb_lower": []}
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
        return {"candles": display_rows, "bb_upper": bb_upper, "bb_mid": bb_mid, "bb_lower": bb_lower}
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
        yoy = r.get("growth_yoy")
        consec = r.get("growth_consecutive", 0) or 0

        sig_colors = {
            "爆量★": "#ff4444", "爆量": "#ff7744",
            "回調": "#ffcc44", "趨勢延伸": "#44ccff",
            "蓄積★": "#44ff88", "蓄積": "#44aaff",
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

        cards.append(f"""
    <div class="card" data-action="{action}" data-conf="{conf}" data-industry="{_esc(raw_industry)}" data-concepts="{_esc(concept_names_joined)}" style="animation-delay:{delay}s">
      <div class="card-header">
        <div class="rank">{i+1}</div>
        <div class="info">
          <div class="ticker">{_esc(ticker)} <span class="tname">{name}</span></div>
          <div class="cname">{industry}</div>
        </div>
        <div class="badge g-{gcls}">{badge_zh}</div>
      </div>
      {concept_html}
      <div class="type-badges" style="margin:4px 12px 8px">{type_badge}{fund_badge}</div>
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
.ticker{{font-size:16px;font-weight:700;letter-spacing:1px;display:flex;align-items:baseline;gap:6px}}
.tname{{font-size:15px;font-weight:600;color:#e6edf3}}
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
.filter-bar{{position:sticky;top:0;z-index:100;background:#0d1117cc;backdrop-filter:blur(12px);
  border-bottom:1px solid #21262d;padding:10px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
.fb-group{{display:flex;align-items:center;gap:8px}}
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
<div class="filter-bar" id="filterBar">
  <div class="fb-group">
    <span class="fb-label">類型</span>
    <button class="fb-pill active" data-filter-action="ALL">全部</button>
    <button class="fb-pill" data-filter-action="LONG">突破進場</button>
    <button class="fb-pill" data-filter-action="WATCH">等待確認</button>
  </div>
  <div class="fb-group">
    <span class="fb-label">信心 ≥</span>
    <input type="range" class="fb-slider" id="confSlider" min="{min_confidence}" max="150" value="{min_confidence}" step="5">
    <span class="fb-val" id="confVal">{min_confidence}</span>
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
<div class="grid" id="cardGrid">
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

// --- Filter bar ---
(function() {{
  var activeAction = "ALL";
  var minConf = {min_confidence};
  var activeInd = "";
  var selectedConcepts = new Set();

  function applyFilters() {{
    var cards = document.querySelectorAll("#cardGrid .card");
    var visible = 0;
    cards.forEach(function(c) {{
      var a = c.dataset.action;
      var conf = parseInt(c.dataset.conf, 10);
      var ind = c.dataset.industry;
      var cardConceptsRaw = c.dataset.concepts || "";
      var cardConcepts = cardConceptsRaw ? new Set(cardConceptsRaw.split(",")) : new Set();
      var conceptMatch = selectedConcepts.size === 0
        || [...selectedConcepts].every(function(sc) {{ return cardConcepts.has(sc); }});
      var show = (activeAction === "ALL" || a === activeAction)
               && conf >= minConf
               && (activeInd === "" || ind === activeInd)
               && conceptMatch;
      c.classList.toggle("hidden", !show);
      if (show) visible++;
    }});
    document.getElementById("visCount").textContent = visible;
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
    parser.add_argument("--top", type=int, default=10, help="顯示前 N 名（預設: 10）")
    parser.add_argument("--min-confidence", type=int, default=50, help="最低信心分數門檻（預設: 50）")
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
