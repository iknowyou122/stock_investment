"""Batch scanner — runs StrategistAgent on multiple tickers and ranks by confidence.

Usage:
    python scripts/batch_scan.py                                    # 互動式選擇產業
    python scripts/batch_scan.py --sectors 1 4                      # 非互動：用產業代號
    python scripts/batch_scan.py --date 2026-03-25
    python scripts/batch_scan.py --tickers 2330 2454 2317 --date 2026-03-25
    python scripts/batch_scan.py --min-confidence 40
    python scripts/batch_scan.py --top 10 --date 2026-03-25
    python scripts/batch_scan.py --no-llm                           # 純 deterministic scoring
    python scripts/batch_scan.py --llm gemini --llm-top 5           # 非互動：Gemini，只對前5名
    python scripts/batch_scan.py --save-csv                         # 存到 data/scans/
    python scripts/batch_scan.py --save-csv --csv-path results.csv

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
# 預設產業別（互動模式的 Enter 預設值）
# -------------------------------------------------------------------
_DEFAULT_SECTOR_NAMES = {
    "半導體業",
    "光電業",
    "電腦及週邊設備業",
    "電子零組件業",
    "其他電子業",
}

_ISIN_URLS = {
    "twse": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2",  # 上市
    "otc":  "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4",  # 上櫃
}

_CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "watchlist_cache"

_FALLBACK_TICKERS = [
    "2330", "2454", "2303", "2379", "3711", "2408", "2344",
    "2317", "2382", "2356", "2324", "6669", "3231", "2357", "2353", "2308",
    "2409", "3481",
]


def _fetch_isin_tickers(url: str) -> dict[str, str]:
    """Parse TWSE/OTC ISIN page; return {ticker: industry} for ALL valid stocks."""
    import requests
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20, verify=False)
    resp.raise_for_status()
    html = resp.content.decode("big5", errors="replace")
    cells = re.findall(r"<td[^>]*>(.*?)</td>", html, re.DOTALL)
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]

    mapping: dict[str, str] = {}
    for i in range(len(cells) - 6):
        industry = cells[i + 5]
        code_name = cells[i + 1]
        if industry and re.match(r"^\d{4}", code_name):
            code = code_name[:4]
            name = code_name[5:].strip()
            if "*" not in name and "DR" not in name:
                mapping[code] = industry
    return mapping


def _build_industry_map() -> dict[str, str]:
    """Load or fetch full ticker→industry map (ALL sectors), cached daily.

    Cache file: data/watchlist_cache/industry_map_YYYY-MM-DD.json
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
    all_map: dict[str, str] = {}
    for market, url in _ISIN_URLS.items():
        try:
            m = _fetch_isin_tickers(url)
            _console.print(f"  [dim]{market.upper()}: {len(m)} 檔[/dim]")
            all_map.update(m)
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", market, e)

    if all_map:
        counts = Counter(all_map.values())
        total_sectors = len(counts)
        cache_file.write_text(json.dumps(all_map, ensure_ascii=False))
        _console.print(f"  [dim]合計: {len(all_map)} 檔，{total_sectors} 個產業（已快取至 {cache_file.name}）[/dim]\n")
        return all_map

    logger.warning("TWSE/OTC fetch failed; using fallback watchlist")
    return {}


def _sector_menu(industry_map: dict[str, str]) -> list[tuple[int, str, int]]:
    """Print numbered sector table. Returns [(idx, industry_name, count), ...]."""
    from collections import Counter
    counts = Counter(industry_map.values())
    rows = [(i, ind, counts[ind]) for i, ind in enumerate(sorted(counts.keys()), start=1)]

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
    """Prompt user to pick sectors by number. Enter → use defaults."""
    default_indices = " ".join(str(i) for i, name, _ in rows if name in default_names)
    _console.print(f"\n[bold yellow]請輸入產業代號[/bold yellow]（空白分隔），直接 Enter 使用預設 [dim][{default_indices}][/dim]")
    raw = _console.input("[bold cyan]> [/bold cyan]").strip()
    if not raw:
        return default_names
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
    """Boost stocks in top 20% of their sector by +5 pts.

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
        sorted_rs = sorted(rs, key=lambda r: r["confidence"], reverse=True)
        top_n = max(1, len(sorted_rs) // 5)  # top 20%
        for rank, r in enumerate(sorted_rs[:top_n], 1):
            r["confidence"] = min(100, r["confidence"] + 5)
            r["flags"] = list(r.get("flags") or []) + [f"SECTOR_RANK:{rank}/{len(sorted_rs)}"]
            boosted += 1

    return boosted


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
    now = datetime.now()
    # 17:00 前用前一交易日；之後用今天（收盤資料已回傳）
    candidate = date.today() if now.hour >= 17 else date.today() - timedelta(days=1)
    while candidate.weekday() >= 5:
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


def _scan_one(ticker: str, analysis_date: date, agent: StrategistAgent) -> dict:
    """Run pipeline for one ticker using a shared agent; return result dict.

    The returned dict includes a '_signal' key with the raw SignalOutput object
    (None on error or halt) so that run_batch can optionally record it to DB.
    """
    t0 = time.time()
    try:
        signal = agent.run(ticker, analysis_date)
        elapsed = time.time() - t0
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
            "momentum": signal.reasoning.momentum if signal.reasoning else "",
            "chip": signal.reasoning.chip_analysis if signal.reasoning else "",
            "risk": signal.reasoning.risk_factors if signal.reasoning else "",
            "elapsed": elapsed,
            "error": None,
            "_signal": signal,
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
            "momentum": "",
            "chip": "",
            "risk": "",
            "elapsed": time.time() - t0,
            "error": str(e),
            "_signal": None,
        }


CSV_FIELDS = [
    "scan_date", "analysis_date", "ticker", "action", "confidence",
    "free_tier", "halt", "entry_bid", "stop_loss", "target",
    "momentum", "chip_analysis", "risk_factors", "data_quality_flags",
]


def _save_csv(results: list[dict], analysis_date: date, csv_path: Path) -> None:
    """Append scan results to a CSV file (creates with header if new)."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists()
    scan_date = date.today().isoformat()

    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        for r in results:
            writer.writerow({
                "scan_date": scan_date,
                "analysis_date": analysis_date.isoformat(),
                "ticker": r["ticker"],
                "action": r["action"],
                "confidence": r["confidence"],
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


def _print_table(results: list[dict], top: int, min_confidence: int) -> None:
    valid = [r for r in results if not r["halt"] and r["error"] is None]
    halted = [r for r in results if r["halt"] or r["error"] is not None]

    valid.sort(key=lambda r: r["confidence"], reverse=True)

    if min_confidence > 0:
        valid = [r for r in valid if r["confidence"] >= min_confidence]
    if top:
        valid = valid[:top]

    table = Table(
        title="BATCH SCAN RESULTS",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white on dark_blue",
        title_style="bold white",
        border_style="blue",
        show_lines=True,
    )
    table.add_column("Rank", justify="center", style="dim", width=5)
    table.add_column("Ticker", style="bold white", width=8)
    table.add_column("Action", width=12)
    table.add_column("Confidence", width=18)
    table.add_column("Entry", justify="right", style="cyan", width=9)
    table.add_column("Stop", justify="right", style="red", width=9)
    table.add_column("Target", justify="right", style="green", width=9)

    for i, r in enumerate(valid, 1):
        action = r["action"] + ("*" if r["free_tier"] else "")
        action_text = Text(action, style=_action_style(r["action"]))
        table.add_row(
            str(i),
            r["ticker"],
            action_text,
            _conf_bar(r["confidence"]),
            f"{r['entry_bid']:.1f}",
            f"{r['stop_loss']:.1f}",
            f"{r['target']:.1f}",
        )

    _console.print()
    if valid:
        _console.print(table)
    else:
        _console.print(Panel(f"[dim]無符合條件的標的 (min_confidence={min_confidence})[/dim]", border_style="yellow"))

    # LLM details
    for r in valid:
        if r["momentum"] or r["chip"] or r["risk"]:
            _console.print(f"\n[bold white]{r['ticker']}[/bold white] LLM 分析")
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

    llm_count = sum(1 for r in results if r.get("momentum") or r.get("chip") or r.get("risk"))
    llm_note = f"，LLM 補充 {llm_count} 檔" if llm_count else ""
    _console.print(Panel(
        f"[bold green]掃描完成[/bold green]  {len(results)} 檔  •  有效訊號 [bold]{len(valid)}[/bold] 檔{llm_note}",
        border_style="green",
        padding=(0, 2),
    ))


def _run_phase(
    tickers: list[str],
    analysis_date: date,
    workers: int,
    llm_provider=None,
    no_llm: bool = False,
    label_repo=None,
) -> list[dict]:
    """執行一批 ticker 的掃描，回傳 results list（順序不保證）。

    共用一組 FinMindClient + ChipProxyFetcher 實例，讓所有 worker 共享
    日期級快取（T86/Margin/SBL/DayTrade/TPEx + OHLCV superset）。
    第一個 ticker 填充快取後，後續 ticker 直接命中記憶體 — 大幅減少 API 呼叫。

    CPython GIL 保證 dict 寫入原子性，最壞情況是前幾個 ticker 重複呼叫 API，
    不會資料錯亂。

    no_llm=True 強制關閉 LLM（Phase 1 deterministc 用，避免 StrategistAgent 自動偵測 API key）。
    label_repo: shared BrokerLabelRepository instance（read-only，多執行緒安全）。
    """
    # 建立共用客戶端 — 所有 worker 共享快取
    shared_finmind = FinMindClient()
    shared_chip = ChipProxyFetcher()
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
                pool.submit(_scan_one, ticker, analysis_date, shared_agent): ticker
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
        results = _run_phase(tickers, analysis_date, workers, no_llm=True, label_repo=label_repo)
    else:
        # 永遠兩階段：Phase 1 全量 deterministic → Phase 2 top N with LLM
        _console.print(f"\n[bold cyan][Phase 1][/bold cyan] deterministic scan：{len(tickers)} 檔")
        results = _run_phase(tickers, analysis_date, workers, no_llm=True, label_repo=label_repo)

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
            phase2 = _run_phase(llm_tickers, analysis_date, p2_workers, llm_provider=llm_provider, label_repo=label_repo)
            p2_valid = {r["ticker"]: r for r in phase2 if r.get("error") is None}
            results = [p2_valid.get(r["ticker"], r) for r in results]

    # --- Post-processing: sector ranking + persistence ---
    scan_data_dir = Path(__file__).resolve().parents[1] / "data" / "scans"

    if industry_map:
        n_sector = _apply_sector_ranks(results, industry_map)
        if n_sector:
            _console.print(f"  [dim]↑ 產業相對排名加分: {n_sector} 檔 (+5 pts each)[/dim]")

    n_persist = _apply_persistence_bonus(results, analysis_date, scan_data_dir)
    if n_persist:
        _console.print(f"  [dim]↑ 持續訊號加分: {n_persist} 檔 (RISING +7 / STABLE +5)[/dim]")

    # --- Optional: record to DB (source=live) for factor analysis ---
    if save_db:
        n_recorded = _record_results(results, analysis_date)
        if n_recorded:
            _console.print(f"  [dim green]✓ {n_recorded} 筆訊號已寫入 DB (source=live)[/dim green]")
        else:
            _console.print("  [dim yellow]⚠ DB 未設定或無法連線，略過寫入[/dim yellow]")

    _print_table(results, top, min_confidence)

    if csv_path:
        _save_csv(results, analysis_date, csv_path)


def main() -> None:
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
    parser.add_argument("--min-confidence", type=int, default=0, help="最低信心分數門檻（預設: 0）")
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
    args = parser.parse_args()

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
    else:
        industry_map = _build_industry_map()
        if not industry_map:
            logger.warning("No industry map available; using fallback ticker list")
            tickers = _FALLBACK_TICKERS
        else:
            rows = _sector_menu(industry_map)
            idx_map = {i: name for i, name, _ in rows}

            if args.sectors:
                # Non-interactive: resolve numeric codes directly
                chosen = {idx_map[n] for n in args.sectors if n in idx_map}
                if not chosen:
                    _console.print("  [yellow]指定代號無效，使用預設產業[/yellow]")
                    chosen = _DEFAULT_SECTOR_NAMES
            else:
                chosen = _select_sectors(rows, _DEFAULT_SECTOR_NAMES)

            tickers = sorted(t for t, ind in industry_map.items() if ind in chosen)
            from collections import Counter
            counts = Counter(ind for t, ind in industry_map.items() if ind in chosen)
            summary = " + ".join(f"{ind}({counts[ind]})" for ind in sorted(chosen))
            _console.print(f"\n[bold]掃描範圍:[/bold] {summary} = [cyan]{len(tickers)}[/cyan] 檔")

    from taiwan_stock_agent.domain.llm_provider import create_llm_provider
    if args.no_llm:
        llm_provider, llm_top = None, None
    elif args.llm is not None or args.llm_top is not None:
        # 非互動模式：CLI 明確指定
        llm_provider = create_llm_provider(args.llm)
        llm_top = args.llm_top
    else:
        # 互動模式：進入選單
        llm_provider, llm_top = _llm_menu()

    # 嘗試載入 BrokerLabelRepository（需要 DATABASE_URL + build-labels 已執行）
    label_repo = _make_label_repo()

    run_batch(
        tickers, args.date, args.top, args.min_confidence, args.workers,
        csv_path, llm_provider, llm_top, label_repo,
        industry_map=industry_map,
        save_db=args.save_db,
    )


if __name__ == "__main__":
    main()
