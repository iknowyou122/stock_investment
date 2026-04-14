"""盤後每日復盤 — T+1 結算、進場成功追蹤、滾動勝率、A/B 參數競賽。

工作流程：
    收盤後  make scan --save-csv --save-db   → 產出信號
    同日    make review                       → T+1 結算昨日信號 + 勝率 + A/B 比較
    或      make daily                        → scan + review 一鍵完成

Usage:
    python scripts/review.py
    python scripts/review.py --date 2026-04-10
    make review
    make daily
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# Allow importing from scripts/ (for precheck helpers)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv()

import urllib3
import requests
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel

from taiwan_stock_agent.infrastructure.db import init_pool, get_connection
from taiwan_stock_agent.domain.scoring_replay import load_params, recompute_score

logger = logging.getLogger(__name__)
_console = Console()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WIN_RATE_TARGET = 0.50         # 14 日勝率低於此 → 觸發 grid search
_ROLLING_WINDOW_DAYS = 14       # 滾動勝率計算窗口
_GRID_SEARCH_DAYS = 30          # mini grid search 資料窗口
_AB_MIN_SIGNALS = 20            # A/B 競賽最低信號數才能結算
_AB_PROMOTE_LIFT = 0.03         # candidate 勝率須高出 active 3pp 才 promote

_CANDIDATE_PARAMS_PATH = Path(__file__).resolve().parents[1] / "config" / "candidate_params.json"
_ACTIVE_PARAMS_PATH = Path(__file__).resolve().parents[1] / "config" / "engine_params.json"

# Grid search parameter space (same as factor_report.py)
_PARAM_GRID: dict[str, list] = {
    "rsi_momentum_lo": [48, 50, 52, 55, 58],
    "rsi_momentum_hi": [67, 70, 72, 75],
    "breakout_vol_ratio": [1.2, 1.3, 1.4, 1.5, 1.7, 2.0],
    "long_threshold_neutral": [63, 65, 68, 70, 72],
}

# TWSE MIS API (reuse logic from precheck.py)
_MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
_MIS_BATCH = 20


# ---------------------------------------------------------------------------
# TWSE MIS API — fetch intraday OHLC (post-close still returns day's data)
# ---------------------------------------------------------------------------

def _fetch_mis_batch(mis_keys: list[str]) -> dict[str, dict]:
    """Fetch real-time / post-close OHLC from TWSE MIS API.

    Returns {ticker: {price, high, low, volume, yesterday_close}}.
    """
    results: dict[str, dict] = {}
    for i in range(0, len(mis_keys), _MIS_BATCH):
        batch = mis_keys[i: i + _MIS_BATCH]
        ex_ch = "|".join(batch)
        try:
            resp = requests.get(
                _MIS_URL,
                params={"ex_ch": ex_ch, "json": "1", "delay": "0",
                        "_": str(int(time.time() * 1000))},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
                verify=False,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("MIS API batch %d failed: %s", i // _MIS_BATCH, e)
            continue

        for item in data.get("msgArray", []):
            ticker = item.get("c", "")
            if not ticker:
                continue

            # close price: z → b → (h+l)/2 → o
            price: float | None = None
            for field, parser in [
                ("z", lambda v: float(v)),
                ("b", lambda v: float(v.split("_")[0])),
                ("o", lambda v: float(v)),
            ]:
                val = item.get(field, "-")
                if val not in ("-", ""):
                    try:
                        price = parser(val)
                        break
                    except (ValueError, IndexError):
                        continue
            if price is None:
                continue

            h_str = item.get("h", "-")
            l_str = item.get("l", "-")
            y_str = item.get("y", "0")
            v_str = item.get("v", "0")
            try:
                results[ticker] = {
                    "price": price,
                    "high": float(h_str) if h_str not in ("-", "") else None,
                    "low": float(l_str) if l_str not in ("-", "") else None,
                    "volume": int(v_str.replace(",", "")),
                    "yesterday_close": float(y_str),
                }
            except (ValueError, TypeError):
                continue

        if i + _MIS_BATCH < len(mis_keys):
            time.sleep(0.3)

    return results


def _fetch_intraday_ohlc(tickers: list[str]) -> dict[str, dict]:
    """Fetch today's OHLC for tickers. Tries TWSE first, retries missing as OTC."""
    tse_keys = [f"tse_{t}.tw" for t in tickers]
    results = _fetch_mis_batch(tse_keys)

    missing = [t for t in tickers if t not in results]
    if missing:
        otc_keys = [f"otc_{t}.tw" for t in missing]
        otc_results = _fetch_mis_batch(otc_keys)
        results.update(otc_results)

    return results


# ---------------------------------------------------------------------------
# T+1 Quick Settlement
# ---------------------------------------------------------------------------

def _prev_trading_day(d: date) -> date:
    """Return the most recent trading day before d."""
    candidate = d - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def run_t1_settle(review_date: date) -> list[dict]:
    """Settle T+1 outcomes for yesterday's LONG signals.

    Fetches today's OHLC from TWSE MIS API and computes:
    - entry_success: intraday_low <= entry_price AND intraday_low > stop_loss
    - outcome_1d: (close - entry_price) / entry_price

    Returns list of settled signal dicts for reporting.
    """
    signal_date = _prev_trading_day(review_date)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_id, ticker, entry_price, stop_loss, confidence_score
                FROM signal_outcomes
                WHERE signal_date = %s
                  AND source = 'live'
                  AND action = 'LONG'
                  AND entry_success IS NULL
                  AND halt_flag = FALSE
            """, (signal_date,))
            rows = cur.fetchall()

    if not rows:
        return []

    tickers = list(set(r[1] for r in rows))
    quotes = _fetch_intraday_ohlc(tickers)

    settled = []
    updates = []
    for signal_id, ticker, entry_price, stop_loss, conf in rows:
        q = quotes.get(ticker)
        if q is None or q.get("high") is None or q.get("low") is None:
            continue

        close = q["price"]
        high = q["high"]
        low = q["low"]
        name = q.get("name", "")

        # entry_success: price touched entry zone AND didn't blow stop
        # If stop_loss is NULL (old signals before migration), derive from entry_price
        sl = stop_loss if stop_loss is not None else entry_price * 0.97
        entry_ok = (low <= entry_price) and (low > sl)
        outcome = (close - entry_price) / entry_price if entry_price else None

        updates.append((high, low, entry_ok, close, outcome, signal_id))
        settled.append({
            "signal_id": signal_id,
            "ticker": ticker,
            "name": name,
            "entry_price": entry_price,
            "stop_loss": sl,
            "confidence": conf,
            "close": close,
            "high": high,
            "low": low,
            "entry_success": entry_ok,
            "outcome_1d": outcome,
        })

    if updates:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    UPDATE signal_outcomes
                    SET intraday_high = %s,
                        intraday_low = %s,
                        entry_success = %s,
                        price_1d = %s,
                        outcome_1d = %s
                    WHERE signal_id = %s
                      AND entry_success IS NULL
                """, updates)

    return settled


# ---------------------------------------------------------------------------
# Rolling Win Rate
# ---------------------------------------------------------------------------

def _compute_rolling_win_rate(window_days: int = _ROLLING_WINDOW_DAYS) -> tuple[int, int, float | None]:
    """Compute T+1 win rate over last N calendar days of settled LONG signals.

    Returns (wins, total, win_rate). win_rate is None if total == 0.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE outcome_1d > 0),
                    COUNT(*)
                FROM signal_outcomes
                WHERE source = 'live'
                  AND action = 'LONG'
                  AND halt_flag = FALSE
                  AND outcome_1d IS NOT NULL
                  AND signal_date >= CURRENT_DATE - (%s * INTERVAL '1 day')
            """, (window_days,))
            wins, total = cur.fetchone()

    return wins, total, (wins / total if total > 0 else None)


def _compute_prev_week_win_rate() -> float | None:
    """Last week's win rate (days 8–21) for trend comparison."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE outcome_1d > 0),
                    COUNT(*)
                FROM signal_outcomes
                WHERE source = 'live'
                  AND action = 'LONG'
                  AND halt_flag = FALSE
                  AND outcome_1d IS NOT NULL
                  AND signal_date >= CURRENT_DATE - INTERVAL '21 days'
                  AND signal_date < CURRENT_DATE - INTERVAL '7 days'
            """)
            wins, total = cur.fetchone()
    return wins / total if total > 0 else None


# ---------------------------------------------------------------------------
# Mini Grid Search (short walk-forward for 30-day T+1 data)
# ---------------------------------------------------------------------------

def _fetch_settled_rows(days: int) -> list[dict]:
    """Fetch settled LONG signals with score_breakdown for grid search."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_id, ticker, signal_date, confidence_score, action,
                       outcome_1d, score_breakdown
                FROM signal_outcomes
                WHERE signal_date >= CURRENT_DATE - (%s * INTERVAL '1 day')
                  AND halt_flag = FALSE
                  AND outcome_1d IS NOT NULL
                  AND score_breakdown IS NOT NULL
                  AND source = 'live'
                ORDER BY signal_date
            """, (days,))
            cols = [d[0] for d in cur.description]
            rows = []
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                if isinstance(d["score_breakdown"], str):
                    d["score_breakdown"] = json.loads(d["score_breakdown"])
                rows.append(d)
    return rows


def _win_rate_at_threshold(rows: list[dict], params: dict) -> float:
    """Win rate among signals that would be LONG under these params."""
    if not rows:
        return 0.0
    longs = [
        r for r in rows
        if r.get("score_breakdown")
        and recompute_score(r["score_breakdown"], params)[1] == "LONG"
    ]
    if len(longs) < 3:
        return 0.0
    return sum(1 for r in longs if r["outcome_1d"] > 0) / len(longs)


def _walk_forward_windows_short(
    rows: list[dict], train_days: int = 21, test_days: int = 7,
) -> list[tuple[list, list]]:
    """Short walk-forward windows for 30-day data (21d train / 7d test)."""
    if not rows:
        return []

    dates = sorted(set(r["signal_date"] for r in rows))
    if not dates:
        return []

    first, last = dates[0], dates[-1]
    windows = []
    window_start = first

    while True:
        train_end = window_start + timedelta(days=train_days)
        test_end = train_end + timedelta(days=test_days)
        if test_end > last:
            break

        train = [r for r in rows if window_start <= r["signal_date"] < train_end]
        test = [r for r in rows if train_end <= r["signal_date"] < test_end]

        if len(train) >= 10 and len(test) >= 3:
            windows.append((train, test))

        window_start += timedelta(days=7)

    return windows


def _run_mini_grid_search(rows: list[dict], n_random: int = 500) -> list[dict]:
    """Grid search on recent T+1 data with short walk-forward validation.

    Returns top 5 candidates (sorted by avg_test_lift descending).
    """
    windows = _walk_forward_windows_short(rows)
    if len(windows) < 1:
        return []

    base_params = load_params()
    all_keys = list(_PARAM_GRID.keys())

    candidates = []
    for _ in range(n_random):
        cand = dict(base_params)
        for k in all_keys:
            cand[k] = random.choice(_PARAM_GRID[k])
        candidates.append(cand)

    results = []
    for params in candidates:
        test_lifts = []
        for train, test in windows:
            base_wr = _win_rate_at_threshold(test, base_params)
            cand_wr = _win_rate_at_threshold(test, params)
            test_lifts.append(cand_wr - base_wr)

        if all(l >= 0 for l in test_lifts) and test_lifts:
            avg_lift = sum(test_lifts) / len(test_lifts)
            if avg_lift > 0:
                results.append({
                    "params": {k: params[k] for k in all_keys},
                    "avg_test_lift": avg_lift,
                    "n_windows": len(windows),
                })

    results.sort(key=lambda x: x["avg_test_lift"], reverse=True)
    return results[:5]


# ---------------------------------------------------------------------------
# A/B Competition Management
# ---------------------------------------------------------------------------

def _get_active_competition() -> dict | None:
    """Fetch the currently running A/B competition, or None."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, started_at, params_active, params_candidate,
                       reason, lift_estimate,
                       signals_active_wins, signals_active_total,
                       signals_cand_wins, signals_cand_total,
                       status
                FROM ab_competitions
                WHERE status = 'running'
                ORDER BY started_at DESC
                LIMIT 1
            """)
            row = cur.fetchone()
    if row is None:
        return None
    cols = ["id", "started_at", "params_active", "params_candidate",
            "reason", "lift_estimate",
            "signals_active_wins", "signals_active_total",
            "signals_cand_wins", "signals_cand_total",
            "status"]
    comp = dict(zip(cols, row))
    if isinstance(comp["params_active"], str):
        comp["params_active"] = json.loads(comp["params_active"])
    if isinstance(comp["params_candidate"], str):
        comp["params_candidate"] = json.loads(comp["params_candidate"])
    return comp


def _start_competition(
    active_params: dict, candidate_params: dict, reason: str, lift: float,
) -> None:
    """Create a new A/B competition."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ab_competitions
                    (params_active, params_candidate, reason, lift_estimate)
                VALUES (%s, %s, %s, %s)
            """, (
                json.dumps(active_params),
                json.dumps(candidate_params),
                reason,
                lift,
            ))

    # Write candidate_params.json
    with open(_CANDIDATE_PARAMS_PATH, "w") as f:
        json.dump(candidate_params, f, indent=2, ensure_ascii=False)


def _update_ab_scores(comp: dict) -> tuple[int, int, int, int]:
    """Recompute scores under candidate params for signals since competition started.

    Returns (active_wins, active_total, cand_wins, cand_total) cumulative.
    """
    candidate_params = comp["params_candidate"]
    active_params = comp["params_active"]
    started = comp["started_at"]

    # Fetch all settled LONG signals since competition start
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_id, confidence_score, outcome_1d, score_breakdown
                FROM signal_outcomes
                WHERE signal_date >= %s
                  AND source = 'live'
                  AND action = 'LONG'
                  AND halt_flag = FALSE
                  AND outcome_1d IS NOT NULL
                  AND score_breakdown IS NOT NULL
            """, (started,))
            rows = cur.fetchall()

    active_wins = active_total = cand_wins = cand_total = 0
    ab_updates = []

    for signal_id, conf, outcome_1d, breakdown in rows:
        if isinstance(breakdown, str):
            breakdown = json.loads(breakdown)

        # Active params: the signal was already scored with active params
        # so we just check if it would be LONG (it already is since action='LONG')
        active_total += 1
        if outcome_1d > 0:
            active_wins += 1

        # Candidate params: recompute
        cand_score, cand_action = recompute_score(breakdown, candidate_params)
        if cand_action == "LONG":
            cand_total += 1
            if outcome_1d > 0:
                cand_wins += 1

        # Store candidate score
        ab_updates.append((cand_score, signal_id))

    if ab_updates:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    UPDATE signal_outcomes
                    SET ab_candidate_score = %s
                    WHERE signal_id = %s
                """, ab_updates)

    # Update competition tallies
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE ab_competitions
                SET signals_active_wins = %s,
                    signals_active_total = %s,
                    signals_cand_wins = %s,
                    signals_cand_total = %s
                WHERE id = %s
            """, (active_wins, active_total, cand_wins, cand_total, comp["id"]))

    return active_wins, active_total, cand_wins, cand_total


def _evaluate_ab(
    active_wins: int, active_total: int,
    cand_wins: int, cand_total: int,
) -> str:
    """Evaluate competition. Returns 'insufficient' | 'promote' | 'discard' | 'continue'."""
    if active_total < _AB_MIN_SIGNALS:
        return "insufficient"

    active_wr = active_wins / active_total if active_total > 0 else 0
    cand_wr = cand_wins / cand_total if cand_total > 0 else 0

    if cand_total < 5:
        # Candidate produces too few LONGs — likely over-strict threshold
        return "discard"

    if cand_wr > active_wr + _AB_PROMOTE_LIFT:
        return "promote"
    elif active_wr >= cand_wr:
        return "discard"
    else:
        return "continue"


def _promote_candidate(comp: dict) -> None:
    """Write candidate params to engine_params.json and record to engine_versions."""
    from apply_tuning import _apply_params
    old_params = load_params()
    _apply_params(
        comp["params_candidate"], old_params,
        reason=f"ab-promote (competition #{comp['id']})",
        lift=comp["lift_estimate"],
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE ab_competitions
                SET status = 'promoted', resolved_at = CURRENT_DATE,
                    resolution_note = 'candidate win rate exceeded active by >= 3pp'
                WHERE id = %s
            """, (comp["id"],))

    # Clean up candidate_params.json
    if _CANDIDATE_PARAMS_PATH.exists():
        _CANDIDATE_PARAMS_PATH.unlink()


def _discard_candidate(comp: dict, note: str) -> None:
    """Discard losing candidate."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE ab_competitions
                SET status = 'discarded', resolved_at = CURRENT_DATE,
                    resolution_note = %s
                WHERE id = %s
            """, (note, comp["id"]))

    if _CANDIDATE_PARAMS_PATH.exists():
        _CANDIDATE_PARAMS_PATH.unlink()


# ---------------------------------------------------------------------------
# Rich CLI Report
# ---------------------------------------------------------------------------

def _print_report(
    review_date: date,
    settled: list[dict],
    wins: int, total: int, win_rate: float | None,
    prev_week_wr: float | None,
    comp: dict | None,
    ab_result: str | None,
    ab_stats: tuple[int, int, int, int] | None,
    grid_triggered: bool,
    grid_top: dict | None,
) -> None:
    signal_date = _prev_trading_day(review_date)

    # --- Header panel ---
    entry_ok = sum(1 for s in settled if s["entry_success"])
    outcome_ok = sum(1 for s in settled if s["outcome_1d"] and s["outcome_1d"] > 0)
    n = len(settled)

    header_lines = [
        f"[bold white]復盤日期[/bold white]    {review_date}",
        f"[bold white]信號日期[/bold white]    {signal_date}",
        f"[bold white]結算筆數[/bold white]    {n} 筆（昨日 LONG 信號）",
    ]
    if n > 0:
        header_lines.append(f"[bold white]進場成功[/bold white]    {entry_ok}/{n} ({entry_ok/n:.1%})")
        header_lines.append(f"[bold white]T+1 勝率[/bold white]    {outcome_ok}/{n} ({outcome_ok/n:.1%})")

    _console.print(Panel(
        "\n".join(header_lines),
        title="[bold cyan]盤後復盤 Daily Review[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))

    # --- Settled details table ---
    if settled:
        tbl = Table(
            title="T+1 結算明細",
            box=box.ROUNDED,
            header_style="bold white on dark_blue",
            border_style="bright_black",
        )
        tbl.add_column("股票", style="bold white", width=8)
        tbl.add_column("信心", justify="right", width=6)
        tbl.add_column("Entry", justify="right", width=9)
        tbl.add_column("收盤", justify="right", width=9)
        tbl.add_column("漲跌", justify="right", width=8)
        tbl.add_column("進場", justify="center", width=6)
        tbl.add_column("勝負", justify="center", width=6)

        for s in sorted(settled, key=lambda x: x["outcome_1d"] or 0, reverse=True):
            pct = (s["outcome_1d"] or 0) * 100
            pct_color = "green" if pct > 0 else "red" if pct < 0 else "white"
            entry_icon = "[green]O[/green]" if s["entry_success"] else "[red]X[/red]"
            win_icon = "[green]WIN[/green]" if pct > 0 else "[red]LOSS[/red]"

            name = s.get("name", "")
            ticker_cell = f"{s['ticker']}\n[dim]{name[:4]}[/dim]" if name else s["ticker"]

            tbl.add_row(
                ticker_cell,
                str(s["confidence"]),
                f"{s['entry_price']:.1f}",
                f"{s['close']:.1f}",
                f"[{pct_color}]{pct:+.1f}%[/{pct_color}]",
                entry_icon,
                win_icon,
            )

        _console.print()
        _console.print(tbl)

    # --- Rolling win rate ---
    _console.print()
    if win_rate is not None:
        wr_color = "green" if win_rate >= _WIN_RATE_TARGET else "red"
        target_icon = "[green]v[/green]" if win_rate >= _WIN_RATE_TARGET else "[red]X[/red]"
        trend = ""
        if prev_week_wr is not None:
            if win_rate > prev_week_wr + 0.02:
                trend = f" [green]({prev_week_wr:.1%})[/green]"
            elif win_rate < prev_week_wr - 0.02:
                trend = f" [red]({prev_week_wr:.1%})[/red]"
            else:
                trend = f" [dim]({prev_week_wr:.1%})[/dim]"

        _console.print(
            f"  [bold]滾動 {_ROLLING_WINDOW_DAYS} 日表現[/bold]  "
            f"[{wr_color}]{wins}/{total} ({win_rate:.1%})[/{wr_color}]  "
            f"目標: {_WIN_RATE_TARGET:.0%} {target_icon}"
            f"{trend}"
        )
    else:
        _console.print(f"  [dim]滾動 {_ROLLING_WINDOW_DAYS} 日表現: 無已結算資料[/dim]")

    # --- A/B competition status ---
    _console.print()
    if comp and ab_stats:
        a_w, a_t, c_w, c_t = ab_stats
        a_wr = a_w / a_t if a_t > 0 else 0
        c_wr = c_w / c_t if c_t > 0 else 0
        days_running = (review_date - comp["started_at"]).days

        status_label = {
            "insufficient": f"[yellow]累積中（{a_t}/{_AB_MIN_SIGNALS} 筆）[/yellow]",
            "continue": f"[cyan]比較中（active {a_wr:.1%} vs candidate {c_wr:.1%}）[/cyan]",
            "promote": f"[bold green]PROMOTE — candidate 勝出！[/bold green]",
            "discard": f"[red]DISCARD — active 較優[/red]",
        }.get(ab_result, "[dim]unknown[/dim]")

        _console.print(Panel(
            f"[bold white]A/B 競賽[/bold white]  #{comp['id']}（已執行 {days_running} 天）\n"
            f"[bold white]觸發原因[/bold white]  {comp['reason']}\n"
            f"[bold white]Active[/bold white]     {a_w}/{a_t} ({a_wr:.1%})\n"
            f"[bold white]Candidate[/bold white]  {c_w}/{c_t} ({c_wr:.1%})\n"
            f"[bold white]結果[/bold white]       {status_label}",
            title="[bold magenta]A/B 參數競賽[/bold magenta]",
            border_style="magenta",
            padding=(0, 2),
        ))

        if ab_result == "promote":
            # Show what changed
            changes = {
                k: (comp["params_active"].get(k), v)
                for k, v in comp["params_candidate"].items()
                if comp["params_active"].get(k) != v
            }
            if changes:
                for k, (old, new) in changes.items():
                    _console.print(f"  [green]{k}: {old} -> {new}[/green]")
    elif grid_triggered and grid_top:
        _console.print(Panel(
            f"[bold white]觸發原因[/bold white]  勝率 {win_rate:.1%} < 目標 {_WIN_RATE_TARGET:.0%}\n"
            f"[bold white]Grid Search[/bold white]  lift +{grid_top['avg_test_lift']:.1%}\n"
            f"[bold white]動作[/bold white]       建立 A/B 競賽",
            title="[bold yellow]新 A/B 競賽已啟動[/bold yellow]",
            border_style="yellow",
            padding=(0, 2),
        ))
    elif grid_triggered and not grid_top:
        _console.print("  [dim]Grid search 未找到有效候選（資料不足或無改善空間）[/dim]")
    else:
        action_note = "勝率達標，無需觸發 grid search" if (win_rate and win_rate >= _WIN_RATE_TARGET) else "資料不足，暫不觸發"
        _console.print(f"  [dim]A/B 競賽: 無進行中 — {action_note}[/dim]")

    # --- Summary ---
    _console.print()
    if comp and ab_result == "promote":
        _console.print(Panel(
            "[bold green]已採用新參數 — engine_params.json 已更新[/bold green]",
            border_style="green", padding=(0, 2),
        ))
    elif comp and ab_result == "discard":
        _console.print(Panel(
            "[yellow]候選參數已淘汰 — 維持現有參數[/yellow]",
            border_style="yellow", padding=(0, 2),
        ))
    elif not settled and not comp:
        _console.print(Panel("[dim]無昨日 LONG 信號待結算[/dim]", border_style="bright_black"))
    else:
        _console.print(Panel(
            "[bold green]復盤完成[/bold green]",
            border_style="green", padding=(0, 2),
        ))


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------

def run_review(review_date: date) -> None:
    """Full daily review pipeline."""

    # Step 1: T+1 quick settlement
    _console.print(f"\n[bold cyan][Step 1][/bold cyan] T+1 結算昨日 LONG 信號...")
    settled = run_t1_settle(review_date)
    _console.print(f"  [dim]結算完成: {len(settled)} 筆[/dim]")

    # Step 2: Rolling win rate
    _console.print(f"[bold cyan][Step 2][/bold cyan] 計算滾動 {_ROLLING_WINDOW_DAYS} 日勝率...")
    wins, total, win_rate = _compute_rolling_win_rate()
    prev_week_wr = _compute_prev_week_win_rate()

    # Step 3: A/B competition management
    _console.print("[bold cyan][Step 3][/bold cyan] A/B 競賽管理...")
    comp = _get_active_competition()
    ab_result: str | None = None
    ab_stats: tuple[int, int, int, int] | None = None
    grid_triggered = False
    grid_top: dict | None = None

    if comp:
        # Update and evaluate existing competition
        ab_stats = _update_ab_scores(comp)
        a_w, a_t, c_w, c_t = ab_stats
        ab_result = _evaluate_ab(a_w, a_t, c_w, c_t)

        if ab_result == "promote":
            _promote_candidate(comp)
            _console.print("  [bold green]Candidate PROMOTED — engine_params.json 已更新[/bold green]")
        elif ab_result == "discard":
            _discard_candidate(comp, f"active {a_w}/{a_t} >= candidate {c_w}/{c_t}")
            _console.print("  [yellow]Candidate DISCARDED — 維持現有參數[/yellow]")
        else:
            _console.print(f"  [dim]競賽進行中 ({a_t} 筆，需 {_AB_MIN_SIGNALS} 筆才結算)[/dim]")

    elif win_rate is not None and win_rate < _WIN_RATE_TARGET and total >= 10:
        # No active competition + below target → trigger grid search
        grid_triggered = True
        _console.print(f"  [yellow]勝率 {win_rate:.1%} < 目標 {_WIN_RATE_TARGET:.0%} → 觸發 mini grid search...[/yellow]")

        rows = _fetch_settled_rows(_GRID_SEARCH_DAYS)
        if len(rows) >= 15:
            grid_results = _run_mini_grid_search(rows)
            if grid_results:
                grid_top = grid_results[0]
                active_params = load_params()
                _start_competition(
                    active_params,
                    {**active_params, **grid_top["params"]},
                    reason=f"win_rate {_ROLLING_WINDOW_DAYS}d={win_rate:.1%} < target {_WIN_RATE_TARGET:.0%}",
                    lift=grid_top["avg_test_lift"],
                )
                _console.print(f"  [green]A/B 競賽已建立 (lift +{grid_top['avg_test_lift']:.1%})[/green]")
            else:
                _console.print("  [dim]Grid search 未找到改善方案[/dim]")
        else:
            _console.print(f"  [dim]T+1 已結算資料只有 {len(rows)} 筆（需 ≥15），暫緩 grid search[/dim]")
    else:
        _console.print("  [dim]無需動作[/dim]")

    # Step 4: Print report
    _print_report(
        review_date, settled,
        wins, total, win_rate, prev_week_wr,
        comp, ab_result, ab_stats,
        grid_triggered, grid_top,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    # Raise fd limit (same as batch_scan.py)
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 4096:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, hard), hard))
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="盤後每日復盤 — T+1 結算 + 勝率 + A/B 競賽")
    parser.add_argument(
        "--date", type=lambda s: date.fromisoformat(s), default=None,
        help="復盤日期 YYYY-MM-DD（預設: 今天）",
    )
    args = parser.parse_args()
    review_date = args.date or date.today()

    if not os.environ.get("DATABASE_URL"):
        _console.print("[red]需要 DATABASE_URL。請確認 .env 已設定。[/red]")
        return

    init_pool()
    run_review(review_date)


if __name__ == "__main__":
    main()
