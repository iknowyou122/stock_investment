"""月營收成長股掃描 — 從 MOPS 抓取全市場月營收，篩選高成長標的

用法:
    python scripts/growth_scan.py                   # 掃最新完整月份
    python scripts/growth_scan.py --min-yoy 30      # 調高 YoY 門檻（預設 20%）
    python scripts/growth_scan.py --top 30          # 顯示前30名（預設 20）
    python scripts/growth_scan.py --notify          # 推播 Telegram
    make growth
    make growth MIN_YOY=30
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
from datetime import date, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.rule import Rule
from rich import box
from rich.padding import Padding

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dotenv import load_dotenv
load_dotenv()

console = Console()
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

_ROOT       = Path(__file__).resolve().parents[1]
_GROWTH_DIR = _ROOT / "data" / "growth"
_GROWTH_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _latest_revenue_month() -> tuple[int, int]:
    """Return (year_gregorian, month) for the latest published revenue data.

    Taiwan companies must publish monthly revenue by the 10th of the next month.
    So if today >= 10th: last month's data is available.
    If today < 10th:  two months ago is the last complete data.
    """
    today = date.today()
    if today.day >= 10:
        d = (today.replace(day=1) - timedelta(days=1))   # last month
    else:
        d = (today.replace(day=1) - timedelta(days=1))   # last month
        d = (d.replace(day=1) - timedelta(days=1))        # two months ago
    return d.year, d.month


def _prev_months(year: int, month: int, n: int) -> list[tuple[int, int]]:
    """Return list of (year, month) going n months back from (year, month)."""
    result = []
    y, m = year, month
    for _ in range(n):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
        result.append((y, m))
    return result


def _to_roc(year: int) -> int:
    return year - 1911


# ---------------------------------------------------------------------------
# MOPS fetch + parse
# ---------------------------------------------------------------------------

def _fetch_mops(year_roc: int, month: int, typek: str = "sii") -> list[dict]:
    """Fetch monthly revenue table from MOPS. typek: 'sii'=TSE, 'otc'=TPEx."""
    url = "https://mops.twse.com.tw/mops/web/ajax_t05st10_ifrs"
    payload = urllib.parse.urlencode({
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "keyword4": "",
        "code1": "",
        "TYPEK": typek,
        "year": str(year_roc),
        "month": f"{month:02d}",
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={
        "User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://mops.twse.com.tw/mops/web/t05st10_ifrs",
    })

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                html = raw.decode("utf-8")
            except UnicodeDecodeError:
                html = raw.decode("big5", errors="replace")
    except Exception as e:
        logger.warning("MOPS fetch failed (%s, %s): %s", typek, month, e)
        return []

    return _parse_mops_html(html, typek)


def _parse_mops_html(html: str, market: str) -> list[dict]:
    """Extract revenue rows from MOPS HTML table."""
    rows: list[dict] = []

    # Strip HTML tags helper
    def _strip(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s).strip()

    def _num(s: str) -> float | None:
        s = s.replace(",", "").replace("%", "").replace("--", "").strip()
        if not s or s in ("-", "N/A", "－"):
            return None
        try:
            return float(s)
        except ValueError:
            return None

    # Find all <tr> blocks
    tr_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    td_re = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)

    for tr_m in tr_re.finditer(html):
        cells = [_strip(td.group(1)) for td in td_re.finditer(tr_m.group(1))]
        if len(cells) < 9:
            continue
        ticker = cells[0].strip()
        # Only keep numeric 4-digit tickers
        if not re.match(r"^\d{4}$", ticker):
            continue

        rows.append({
            "ticker":        ticker,
            "name":          cells[1].strip(),
            "industry":      cells[2].strip() if len(cells) > 2 else "",
            "cur_rev":       _num(cells[3]) if len(cells) > 3 else None,
            "last_rev":      _num(cells[4]) if len(cells) > 4 else None,
            "prev_year_rev": _num(cells[5]) if len(cells) > 5 else None,
            "mom_pct":       _num(cells[6]) if len(cells) > 6 else None,
            "yoy_pct":       _num(cells[7]) if len(cells) > 7 else None,
            "ytd_yoy_pct":   _num(cells[10]) if len(cells) > 10 else None,
            "market":        "TSE" if market == "sii" else "TPEx",
        })

    return rows


def _fetch_month_with_cache(year: int, month: int, force: bool = False) -> list[dict]:
    """Fetch TSE + TPEx for a given month, with file cache."""
    cache_path = _GROWTH_DIR / f"mops_{year}-{month:02d}.json"

    if not force and cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < 7 * 86400:  # 7 days
            return json.loads(cache_path.read_text())

    year_roc = _to_roc(year)
    tse  = _fetch_mops(year_roc, month, "sii")
    time.sleep(1)
    tpex = _fetch_mops(year_roc, month, "otc")
    combined = tse + tpex

    if combined:
        cache_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2))

    return combined


# ---------------------------------------------------------------------------
# Growth scoring
# ---------------------------------------------------------------------------

def _build_lookup(month_data: list[dict]) -> dict[str, dict]:
    return {r["ticker"]: r for r in month_data}


def _compute_records(
    cur_data:  list[dict],
    hist_data: list[list[dict]],  # [last_month, 2_months_ago, ...]
    min_yoy:   float,
) -> list[dict]:
    """Compute growth metrics and filter."""
    hist_lookups = [_build_lookup(d) for d in hist_data]
    results = []

    for row in cur_data:
        yoy = row.get("yoy_pct")
        if yoy is None or yoy < min_yoy:
            continue
        mom = row.get("mom_pct") or 0.0
        ytd_yoy = row.get("ytd_yoy_pct") or 0.0

        # Acceleration: this month's YoY vs last month's YoY
        prev_yoy = None
        if hist_lookups:
            prev_row = hist_lookups[0].get(row["ticker"])
            if prev_row:
                prev_yoy = prev_row.get("yoy_pct")
        acceleration = (yoy - prev_yoy) if prev_yoy is not None else 0.0

        # Consecutive months of positive YoY (include current month)
        consecutive = 1
        for lookup in hist_lookups:
            prev = lookup.get(row["ticker"])
            if prev and prev.get("yoy_pct") is not None and prev["yoy_pct"] > 0:
                consecutive += 1
            else:
                break

        # Composite score
        score = (
            yoy           * 0.40 +
            max(mom, 0)   * 0.20 +
            max(acceleration, 0) * 0.20 +
            consecutive   * 8   * 0.20
        )

        results.append({
            **row,
            "prev_yoy":     prev_yoy,
            "acceleration": round(acceleration, 1),
            "consecutive":  consecutive,
            "ytd_yoy_pct":  round(ytd_yoy, 1),
            "score":        round(score, 1),
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _render_table(records: list[dict], year: int, month: int, top: int) -> None:
    console.print()
    console.print(Rule(
        f"[bold]📈 成長股雷達  {year}-{month:02d}  "
        f"（共 {len(records)} 檔達標）[/bold]",
        style="cyan",
    ))
    console.print()

    t = Table(box=box.ROUNDED, show_header=True, header_style="bold dim", padding=(0, 1))
    t.add_column("排名", justify="right", min_width=3)
    t.add_column("代號", min_width=5)
    t.add_column("名稱", min_width=8)
    t.add_column("市場", min_width=5)
    t.add_column("YoY%",   justify="right", min_width=7)
    t.add_column("MoM%",   justify="right", min_width=7)
    t.add_column("加速度", justify="right", min_width=7)
    t.add_column("連續成長", justify="right", min_width=6)
    t.add_column("累計YoY%",justify="right", min_width=8)
    t.add_column("綜合分",  justify="right", min_width=6)

    for i, r in enumerate(records[:top], 1):
        yoy = r["yoy_pct"] or 0
        mom = r.get("mom_pct") or 0
        accel = r["acceleration"]
        consec = r["consecutive"]
        ytd = r["ytd_yoy_pct"]
        score = r["score"]

        yoy_style   = "bright_green" if yoy >= 50 else "green" if yoy >= 20 else "white"
        mom_style   = "green" if mom > 0 else "red"
        accel_style = "cyan" if accel > 5 else "white" if accel > 0 else "dim red"
        cons_badge  = f"[cyan]連{consec}月[/cyan]" if consec >= 3 else f"連{consec}月"

        t.add_row(
            str(i),
            r["ticker"],
            r["name"],
            f"[dim]{r['market']}[/dim]",
            f"[{yoy_style}]+{yoy:.1f}%[/{yoy_style}]",
            f"[{mom_style}]{mom:+.1f}%[/{mom_style}]",
            f"[{accel_style}]{accel:+.1f}[/{accel_style}]",
            cons_badge,
            f"{ytd:+.1f}%",
            f"[bold]{score:.0f}[/bold]",
        )

    console.print(Padding(t, (0, 2)))
    console.print()
    console.print(
        "  [dim]YoY = 年增率  MoM = 月增率  加速度 = 本月YoY − 上月YoY  "
        "資料來源：MOPS 公開資訊觀測站[/dim]"
    )
    console.print()


def format_telegram_msg(records: list[dict], year: int, month: int, top: int = 10) -> str:
    if not records:
        return f"📈 *成長股雷達 {year}\\-{month:02d}*\n_無符合條件標的_"

    lines = [
        f"📈 *成長股雷達 {year}\\-{month:02d}*",
        f"月營收 YoY 篩選 · 共 {len(records)} 檔達標",
        "",
    ]
    medals = ["🥇", "🥈", "🥉"] + ["▪"] * 20

    for i, r in enumerate(records[:top]):
        yoy   = r["yoy_pct"] or 0
        mom   = r.get("mom_pct") or 0
        con   = r["consecutive"]
        medal = medals[i] if i < len(medals) else "▪"
        # Escape special chars for Markdown (not MarkdownV2)
        name  = r["name"]
        lines.append(
            f"{medal} *{r['ticker']} {name}*  "
            f"YoY `+{yoy:.1f}%`  MoM `{mom:+.1f}%`  連{con}月"
        )

    lines += ["", "_資料來源：MOPS 公開資訊觀測站_"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_results(records: list[dict], year: int, month: int) -> Path:
    out = {
        "year": year,
        "month": month,
        "scan_date": date.today().isoformat(),
        "records": records,
    }
    path = _GROWTH_DIR / f"growth_{year}-{month:02d}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return path


def load_latest_results() -> tuple[list[dict], int, int] | None:
    """Load the most recent growth scan file. Returns (records, year, month) or None."""
    files = sorted(_GROWTH_DIR.glob("growth_*.json"), reverse=True)
    if not files:
        return None
    try:
        data = json.loads(files[0].read_text())
        return data["records"], data["year"], data["month"]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(
    year: int | None = None,
    month: int | None = None,
    min_yoy: float = 20.0,
    top: int = 20,
    notify: bool = False,
    force: bool = False,
    quiet: bool = False,
) -> list[dict]:
    if year is None or month is None:
        year, month = _latest_revenue_month()

    if not quiet:
        console.print(f"[dim]抓取 {year}-{month:02d} 月營收資料（MOPS）…[/dim]")

    # Fetch current + 2 historical months
    cur_data  = _fetch_month_with_cache(year, month, force=force)
    hist_data = []
    for hy, hm in _prev_months(year, month, 2):
        hist_data.append(_fetch_month_with_cache(hy, hm))
        if not quiet:
            console.print(f"[dim]  歷史 {hy}-{hm:02d} 已載入（{len(hist_data[-1])} 筆）[/dim]")

    if not cur_data:
        console.print("[red]錯誤：無法取得當月資料，請確認網路或稍後再試[/red]")
        return []

    if not quiet:
        console.print(f"[dim]當月 {year}-{month:02d}：{len(cur_data)} 筆，篩選 YoY ≥ {min_yoy}%…[/dim]")

    records = _compute_records(cur_data, hist_data, min_yoy)

    if not quiet:
        _render_table(records, year, month, top)

    path = save_results(records, year, month)
    if not quiet:
        console.print(f"[dim]已存至 {path.relative_to(_ROOT)}（{len(records)} 筆）[/dim]")

    if notify:
        _notify_telegram(records, year, month, top)

    return records


def _notify_telegram(records: list[dict], year: int, month: int, top: int = 10) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("Telegram 未設定，跳過推播")
        return

    msg = format_telegram_msg(records, year, month, top)
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "Markdown",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        logger.warning("Telegram 推播失敗: %s", e)


def main() -> None:
    parser = argparse.ArgumentParser(description="月營收成長股掃描")
    parser.add_argument("--year",    type=int, help="指定年份（西元）")
    parser.add_argument("--month",   type=int, help="指定月份")
    parser.add_argument("--min-yoy", type=float, default=20.0, help="最低 YoY 門檻（預設 20）")
    parser.add_argument("--top",     type=int, default=20, help="顯示前N名（預設 20）")
    parser.add_argument("--notify",  action="store_true", help="推播 Telegram")
    parser.add_argument("--force",   action="store_true", help="強制重新抓取（忽略快取）")
    parser.add_argument("--quiet",   action="store_true", help="不顯示輸出（Bot 用）")
    args = parser.parse_args()

    run(
        year=args.year,
        month=args.month,
        min_yoy=args.min_yoy,
        top=args.top,
        notify=args.notify,
        force=args.force,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
