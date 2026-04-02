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

    print("正在從 TWSE/OTC 抓取完整產業清單...")
    all_map: dict[str, str] = {}
    for market, url in _ISIN_URLS.items():
        try:
            m = _fetch_isin_tickers(url)
            print(f"  {market.upper()}: {len(m)} 檔")
            all_map.update(m)
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", market, e)

    if all_map:
        counts = Counter(all_map.values())
        total_sectors = len(counts)
        cache_file.write_text(json.dumps(all_map, ensure_ascii=False))
        print(f"  合計: {len(all_map)} 檔，{total_sectors} 個產業（已快取至 {cache_file.name}）\n")
        return all_map

    logger.warning("TWSE/OTC fetch failed; using fallback watchlist")
    return {}


def _sector_menu(industry_map: dict[str, str]) -> list[tuple[int, str, int]]:
    """Print numbered sector table. Returns [(idx, industry_name, count), ...]."""
    from collections import Counter
    counts = Counter(industry_map.values())
    rows = [(i, ind, counts[ind]) for i, ind in enumerate(sorted(counts.keys()), start=1)]
    print("\n可用產業別：")
    print(f"  {'#':>3}  {'產業別':<18}  {'檔數':>4}")
    print("  " + "-" * 30)
    for idx, ind, cnt in rows:
        print(f"  {idx:>3}  {ind:<18}  {cnt:>4}")
    return rows


def _select_sectors(
    rows: list[tuple[int, str, int]],
    default_names: set[str],
) -> set[str]:
    """Prompt user to pick sectors by number. Enter → use defaults."""
    default_indices = " ".join(str(i) for i, name, _ in rows if name in default_names)
    prompt = f"\n請輸入產業代號（空白分隔），直接 Enter 使用預設 [{default_indices}]：> "
    raw = input(prompt).strip()
    if not raw:
        return default_names
    idx_map = {i: name for i, name, _ in rows}
    selected: set[str] = set()
    for token in raw.split():
        try:
            selected.add(idx_map[int(token)])
        except (ValueError, KeyError):
            print(f"  忽略無效代號: {token}")
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

    print("\nLLM 引擎：")
    for i, (_, label) in enumerate(_PROVIDERS, 1):
        print(f"  {i}  {label}")

    raw = input("\n請輸入代號，直接 Enter 使用 [1 自動偵測]：> ").strip()
    choice = int(raw) if raw.isdigit() and 1 <= int(raw) <= len(_PROVIDERS) else 1
    provider_key, _ = _PROVIDERS[choice - 1]

    if provider_key == "none":
        print("  → 純 deterministic 模式（不呼叫 LLM）")
        return None, None

    llm_provider = create_llm_provider(None if provider_key == "auto" else provider_key)
    if llm_provider is None:
        print("  ⚠ 找不到對應 API key，LLM 停用")
        return None, None

    print(f"  → {llm_provider.name}（前幾名送 LLM 將在 Phase 1 完成後詢問）\n")

    return llm_provider, None


class _EmptyLabelRepo:
    def get(self, _): return None
    def upsert(self, _): pass
    def list_all(self): return []


def _default_date() -> date:
    from datetime import datetime
    now = datetime.now()
    # 17:00 前用前一交易日；之後用今天（收盤資料已回傳）
    candidate = date.today() if now.hour >= 17 else date.today() - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _make_agent(llm_provider=None, no_llm: bool = False) -> StrategistAgent:
    """Create a thread-local agent with its own FinMind + TWSE clients.

    Each worker thread gets independent HTTP sessions and Parquet cache file
    handles, avoiding lock contention and race conditions on shared state.
    no_llm=True forces LLM off even if ANTHROPIC_API_KEY is in env (Phase 1 use).
    """
    agent = StrategistAgent(
        FinMindClient(),
        _EmptyLabelRepo(),
        chip_proxy_fetcher=ChipProxyFetcher(),
        llm_provider=llm_provider,
    )
    if no_llm:
        agent._llm_provider = None  # override auto-detection in StrategistAgent.__init__
    return agent


def _scan_one(ticker: str, analysis_date: date, llm_provider=None, no_llm: bool = False) -> dict:
    """Run pipeline for one ticker; return result dict."""
    t0 = time.time()
    try:
        signal = _make_agent(llm_provider, no_llm=no_llm).run(ticker, analysis_date)
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

    print(f"\n  📄 CSV 已儲存: {csv_path}  ({len(results)} 筆)")


def _print_table(results: list[dict], top: int, min_confidence: int) -> None:
    # Filter out errors and halts
    valid = [r for r in results if not r["halt"] and r["error"] is None]
    halted = [r for r in results if r["halt"] or r["error"] is not None]

    # Sort by confidence desc
    valid.sort(key=lambda r: r["confidence"], reverse=True)

    # Apply filters
    if min_confidence > 0:
        valid = [r for r in valid if r["confidence"] >= min_confidence]

    if top:
        valid = valid[:top]

    print(f"\n{'=' * 72}")
    print(f"  BATCH SCAN RESULTS")
    print(f"{'=' * 72}")
    print(f"  {'Rank':<5} {'Ticker':<8} {'Action':<10} {'Conf':>5} {'Entry':>10} {'Stop':>10} {'Target':>10}")
    print(f"  {'-' * 67}")

    for i, r in enumerate(valid, 1):
        action_display = r["action"]
        if r["free_tier"]:
            action_display += "*"  # mark free-tier results
        print(
            f"  {i:<5} {r['ticker']:<8} {action_display:<10} {r['confidence']:>5} "
            f"{r['entry_bid']:>10.1f} {r['stop_loss']:>10.1f} {r['target']:>10.1f}"
        )
        if r["momentum"]:
            print(f"         動能: {r['momentum']}")
        if r["chip"]:
            print(f"         籌碼: {r['chip']}")
        if r["risk"]:
            print(f"         風險: {r['risk']}")
        if r["momentum"] or r["chip"] or r["risk"]:
            print()

    if not valid:
        print(f"  (無符合條件的標的，min_confidence={min_confidence})")

    print(f"\n  * = free_tier_mode (無分點資料，閾值較低)")

    if halted:
        print(f"\n  略過 {len(halted)} 檔 (HALT/ERROR):", ", ".join(r["ticker"] for r in halted))

    print(f"{'=' * 72}")
    llm_count = sum(1 for r in results if r.get("momentum") or r.get("chip") or r.get("risk"))
    llm_note = f"，LLM 補充 {llm_count} 檔" if llm_count else ""
    print(f"  掃描完成: {len(results)} 檔，有效訊號 {len(valid)} 檔{llm_note}\n")


def _run_phase(
    tickers: list[str],
    analysis_date: date,
    workers: int,
    llm_provider=None,
    no_llm: bool = False,
) -> list[dict]:
    """執行一批 ticker 的掃描，回傳 results list（順序不保證）。
    no_llm=True 強制關閉 LLM（Phase 1 deterministc 用，避免 StrategistAgent 自動偵測 API key）。
    """
    results: list[dict] = []
    total = len(tickers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_scan_one, ticker, analysis_date, llm_provider, no_llm): ticker
            for ticker in tickers
        }
        for i, future in enumerate(as_completed(futures), 1):
            ticker = futures[future]
            result = future.result()
            results.append(result)
            status = "HALT" if result["halt"] else f"conf={result['confidence']}"
            print(f"  [{i:>2}/{total}] {ticker:<8} {status}")
    return results


def run_batch(
    tickers: list[str],
    analysis_date: date,
    top: int,
    min_confidence: int,
    workers: int,
    csv_path: Path | None = None,
    llm_provider=None,
    llm_top: int | None = None,
) -> None:
    llm_label = getattr(llm_provider, "name", None) or "（無 LLM）"
    print(f"\n掃描清單: {len(tickers)} 檔")
    print(f"分析日期: {analysis_date}")
    print(f"LLM 引擎: {llm_label}")
    print(f"並行執行: {workers} 個 worker（每個 worker 獨立 HTTP session）\n")

    if llm_provider is None:
        # 純 deterministic：強制關閉 LLM（避免 StrategistAgent 自動偵測 API key）
        results = _run_phase(tickers, analysis_date, workers, no_llm=True)
    else:
        # 永遠兩階段：Phase 1 全量 deterministic → Phase 2 top N with LLM
        print(f"[Phase 1] deterministic scan：{len(tickers)} 檔")
        results = _run_phase(tickers, analysis_date, workers, no_llm=True)

        # 排序有效結果
        eligible = sorted(
            [r for r in results if not r["halt"] and r["error"] is None],
            key=lambda r: r["confidence"], reverse=True,
        )
        print(f"\n[Phase 1 完成] {len(results)} 檔（有效 {len(eligible)} 檔）")
        if eligible:
            top5 = ", ".join(f"{r['ticker']}({r['confidence']})" for r in eligible[:5])
            print(f"  前幾名: {top5}{'...' if len(eligible) > 5 else ''}")

        # 決定 Phase 2 範圍：CLI 指定優先，否則互動詢問
        if llm_top is None:
            raw = input(f"\n送前幾名給 LLM [{llm_label}]？（Enter = 不送）：> ").strip()
            llm_top = int(raw) if raw.isdigit() and int(raw) > 0 else 0

        llm_tickers = [r["ticker"] for r in eligible[:llm_top]] if llm_top else []

        if not llm_tickers:
            print("  → 跳過 LLM\n")
        else:
            print(f"\n[Phase 2] 送前 {llm_top} 名給 LLM（{llm_label}）：{', '.join(llm_tickers)}")
            p2_workers = min(3, len(llm_tickers))
            phase2 = _run_phase(llm_tickers, analysis_date, p2_workers, llm_provider=llm_provider)
            p2_valid = {r["ticker"]: r for r in phase2 if r.get("error") is None}
            results = [p2_valid.get(r["ticker"], r) for r in results]

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
    args = parser.parse_args()

    csv_path: Path | None = None
    if args.save_csv:
        if args.csv_path:
            csv_path = args.csv_path
        else:
            scan_dir = Path(__file__).resolve().parents[1] / "data" / "scans"
            csv_path = scan_dir / f"scan_{args.date}.csv"

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
                    print("  指定代號無效，使用預設產業")
                    chosen = _DEFAULT_SECTOR_NAMES
            else:
                chosen = _select_sectors(rows, _DEFAULT_SECTOR_NAMES)

            tickers = sorted(t for t, ind in industry_map.items() if ind in chosen)
            from collections import Counter
            counts = Counter(ind for t, ind in industry_map.items() if ind in chosen)
            summary = " + ".join(f"{ind}({counts[ind]})" for ind in sorted(chosen))
            print(f"\n掃描範圍: {summary} = {len(tickers)} 檔")

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

    run_batch(tickers, args.date, args.top, args.min_confidence, args.workers, csv_path, llm_provider, llm_top)


if __name__ == "__main__":
    main()
