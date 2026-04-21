"""Stratified A/B test framework: v2.2 (control) vs v2.3 (treatment).

Runs a stratified randomized comparison of historical signal performance,
stratified by industry × market × TAIEX regime.

Usage:
    python scripts/ab_test_framework.py
    python scripts/ab_test_framework.py --confidence 0.95 --min-signals-per-stratum 10
    python scripts/ab_test_framework.py --report html report.html
    python scripts/ab_test_framework.py --export-json results.json
    make ab-test
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)
_console = Console()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_DIR = _ROOT / "config"
_CACHE_PATH = _CONFIG_DIR / "signal_outcomes_cache.json"

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# (industry, market, taiex_regime)
StratumKey = tuple[str, str, str]

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SignalGroupStats:
    """Aggregate statistics for a single group (A or B) within a stratum."""
    n: int = 0
    wins: int = 0
    upside_values: list[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if self.n == 0:
            return 0.0
        return self.wins / self.n

    @property
    def mean_upside(self) -> float:
        if not self.upside_values:
            return 0.0
        return sum(self.upside_values) / len(self.upside_values)


@dataclass
class ABTestResult:
    """Statistical test results for a single stratum."""
    stratum: StratumKey
    n_a: int
    n_b: int
    win_rate_a: float
    win_rate_b: float
    mean_upside_a: float
    mean_upside_b: float
    ttest_p: float
    chi2_p: float
    cohens_d: float

    @property
    def win_rate_delta(self) -> float:
        """B (v2.3) minus A (v2.2) win-rate difference."""
        return self.win_rate_b - self.win_rate_a

    @property
    def upside_delta(self) -> float:
        """B (v2.3) minus A (v2.2) mean upside difference."""
        return self.mean_upside_b - self.mean_upside_a

    @property
    def recommendation(self) -> str:
        return derive_recommendation(self)


@dataclass
class OverallResult:
    """Aggregated results across all strata."""
    total_n_a: int
    total_n_b: int
    overall_win_rate_a: float
    overall_win_rate_b: float
    overall_mean_upside_a: float
    overall_mean_upside_b: float
    strata_count: int
    skipped_strata: int
    recommendation: str
    strata_results: list[ABTestResult]


# ---------------------------------------------------------------------------
# Pure functions — fully testable without I/O
# ---------------------------------------------------------------------------


def build_stratum_key(signal: dict) -> StratumKey:
    """Extract the 3-tuple stratification key from a signal dict."""
    return (
        signal.get("industry", "未知"),
        signal.get("market", "TSE"),
        signal.get("taiex_regime", "unknown"),
    )


def filter_settled_records(signals: list[dict]) -> list[dict]:
    """Return only non-pending signals."""
    return [s for s in signals if not s.get("pending", False)]


def assign_groups(
    signals: list[dict],
    seed: int = 42,
) -> dict[str, str]:
    """Stratified random 50/50 assignment of signal IDs to group 'A' or 'B'.

    Within each stratum (industry × market × regime), signals are shuffled
    with a deterministic seed, then alternately assigned A/B so the split is
    exactly 50/50 (±1 for odd-sized strata).

    Returns a dict mapping signal_id → "A" | "B".
    """
    # Group by stratum
    strata: dict[StratumKey, list[dict]] = defaultdict(list)
    for sig in signals:
        strata[build_stratum_key(sig)].append(sig)

    assignment: dict[str, str] = {}
    for stratum_key, stratum_sigs in strata.items():
        # Deterministic shuffle: seed combined with stratum identity
        stratum_seed = seed
        for part in stratum_key:
            stratum_seed ^= hash(part) & 0xFFFFFFFF

        rng = random.Random(stratum_seed)
        shuffled = list(stratum_sigs)
        rng.shuffle(shuffled)

        for i, sig in enumerate(shuffled):
            assignment[sig["signal_id"]] = "A" if i % 2 == 0 else "B"

    return assignment


def cohens_d(
    group_a: list[float],
    group_b: list[float],
) -> Optional[float]:
    """Compute Cohen's d effect size (mean_a - mean_b) / pooled_std.

    Returns None if either group is empty or pooled std is zero with n=1 in either group.
    Returns math.inf / -math.inf when pooled std is exactly 0 (groups identical, no variance).
    """
    n_a = len(group_a)
    n_b = len(group_b)

    if n_a == 0 or n_b == 0:
        return None

    # Need at least 2 total to compute pooled std
    if n_a + n_b < 3:
        return None

    mean_a = sum(group_a) / n_a
    mean_b = sum(group_b) / n_b

    if n_a > 1:
        var_a = sum((x - mean_a) ** 2 for x in group_a) / (n_a - 1)
    else:
        var_a = 0.0

    if n_b > 1:
        var_b = sum((x - mean_b) ** 2 for x in group_b) / (n_b - 1)
    else:
        var_b = 0.0

    pooled_var = ((n_a - 1) * var_a + (n_b - 1) * var_b) / (n_a + n_b - 2)

    if pooled_var < 0:
        pooled_var = 0.0

    pooled_std = math.sqrt(pooled_var)

    if pooled_std == 0.0:
        # Identical distributions; effect size is technically infinite but meaningful
        if mean_a == mean_b:
            return 0.0
        return math.copysign(math.inf, mean_a - mean_b)

    return (mean_a - mean_b) / pooled_std


def run_ttest(
    group_a: list[float],
    group_b: list[float],
) -> tuple[float, float]:
    """Welch's t-test for mean upside comparison.

    Returns (t_statistic, p_value). Returns (nan, nan) if either group is empty
    or has zero variance with n=1.
    """
    if not group_a or not group_b:
        return math.nan, math.nan

    try:
        from scipy.stats import ttest_ind  # type: ignore[import]
        result = ttest_ind(group_a, group_b, equal_var=False)
        p = float(result.pvalue)
        stat = float(result.statistic)
        if math.isnan(p):
            return stat, math.nan
        return stat, p
    except Exception as e:
        logger.debug("run_ttest: scipy unavailable or error: %s", e)
        return _manual_welch_ttest(group_a, group_b)


def _manual_welch_ttest(
    a: list[float],
    b: list[float],
) -> tuple[float, float]:
    """Welch's t-test fallback without scipy."""
    n_a, n_b = len(a), len(b)
    if n_a < 2 or n_b < 2:
        return math.nan, math.nan

    mean_a = sum(a) / n_a
    mean_b = sum(b) / n_b
    var_a = sum((x - mean_a) ** 2 for x in a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in b) / (n_b - 1)

    se = math.sqrt(var_a / n_a + var_b / n_b)
    if se == 0:
        return 0.0, 1.0

    t = (mean_a - mean_b) / se

    # Welch–Satterthwaite degrees of freedom
    numer = (var_a / n_a + var_b / n_b) ** 2
    denom = (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)
    df = numer / denom if denom > 0 else n_a + n_b - 2

    # Approximate p-value via survival function of t-distribution
    # Use scipy if available, else rough approximation
    try:
        from scipy.stats import t as t_dist  # type: ignore[import]
        p = float(2 * t_dist.sf(abs(t), df))
    except ImportError:
        # Very rough: for df > 30, approximate with normal distribution
        # P(|Z| > |t|) ≈ 2 * (1 - Phi(|t|))
        abs_t = abs(t)
        p = 2.0 * (1.0 - _normal_cdf(abs_t))

    return float(t), float(p)


def _normal_cdf(x: float) -> float:
    """Approximate normal CDF using math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def run_chi2(
    wins_a: int,
    n_a: int,
    wins_b: int,
    n_b: int,
) -> tuple[float, float]:
    """Chi-squared test for win-rate difference between groups.

    Contingency table:
        |  Win  | Loss |
    A   | wins_a| n_a - wins_a |
    B   | wins_b| n_b - wins_b |

    Returns (chi2_stat, p_value). Returns (nan, nan) if n_a or n_b is 0.
    """
    if n_a == 0 or n_b == 0:
        return math.nan, math.nan

    losses_a = n_a - wins_a
    losses_b = n_b - wins_b

    # Guard against negative losses (malformed data)
    if losses_a < 0 or losses_b < 0:
        return math.nan, math.nan

    try:
        from scipy.stats import chi2_contingency  # type: ignore[import]
        table = [[wins_a, losses_a], [wins_b, losses_b]]
        chi2, p, dof, expected = chi2_contingency(table, correction=False)
        return float(chi2), float(p)
    except Exception as e:
        logger.debug("run_chi2: scipy error: %s", e)
        return _manual_chi2(wins_a, losses_a, wins_b, losses_b)


def _manual_chi2(
    a11: int, a12: int,
    a21: int, a22: int,
) -> tuple[float, float]:
    """2×2 chi-squared without scipy."""
    n = a11 + a12 + a21 + a22
    if n == 0:
        return math.nan, math.nan

    r1 = a11 + a12
    r2 = a21 + a22
    c1 = a11 + a21
    c2 = a12 + a22

    def expected(ri: int, cj: int) -> float:
        return ri * cj / n

    chi2 = 0.0
    for obs, ri, cj in [(a11, r1, c1), (a12, r1, c2), (a21, r2, c1), (a22, r2, c2)]:
        exp = expected(ri, cj)
        if exp > 0:
            chi2 += (obs - exp) ** 2 / exp

    # 1 degree of freedom for 2×2
    # Approximate p-value using chi-squared CDF
    try:
        from scipy.stats import chi2 as chi2_dist  # type: ignore[import]
        p = float(chi2_dist.sf(chi2, df=1))
    except ImportError:
        # Rough approximation: chi2(1) survival is related to normal
        p = float(1.0 - _normal_cdf(math.sqrt(chi2))) * 2.0
        p = max(0.0, min(1.0, p))

    return float(chi2), float(p)


def compute_group_stats(
    signals: list[dict],
    assignment: dict[str, str],
) -> dict[str, SignalGroupStats]:
    """Compute per-group win-rate and upside statistics.

    Returns dict with keys 'A' and/or 'B' (only groups with ≥1 signal).
    """
    groups: dict[str, SignalGroupStats] = {}

    for sig in signals:
        sid = sig["signal_id"]
        group = assignment.get(sid)
        if group is None:
            continue

        if group not in groups:
            groups[group] = SignalGroupStats()

        stats = groups[group]
        stats.n += 1
        if sig.get("actual_breakout", False):
            stats.wins += 1
        stats.upside_values.append(float(sig.get("upside_pct", 0.0)))

    return groups


def derive_recommendation(
    result: ABTestResult,
    alpha: float = 0.05,
    min_effect_size: float = 0.10,
) -> str:
    """Map test results to a human-readable recommendation string.

    Decision logic:
    - "Adopt v2.3": chi2_p < alpha AND win_rate_delta >= min_effect_size (B better)
    - "Keep v2.2": chi2_p < alpha AND win_rate_delta <= -min_effect_size (A better)
    - "Continue testing": otherwise (not significant or effect too small)
    """
    significant = result.chi2_p < alpha
    large_enough = abs(result.win_rate_delta) >= min_effect_size

    if significant and large_enough:
        if result.win_rate_delta > 0:
            return "Adopt v2.3"
        else:
            return "Keep v2.2"
    return "Continue testing"


def aggregate_strata_results(
    strata_results: list[ABTestResult],
    alpha: float = 0.05,
    min_effect_size: float = 0.10,
) -> OverallResult:
    """Combine per-stratum results into an overall summary.

    Win rates and upside values are weighted by stratum sample size.
    The overall recommendation is derived from the pooled statistics.
    """
    if not strata_results:
        return OverallResult(
            total_n_a=0, total_n_b=0,
            overall_win_rate_a=0.0, overall_win_rate_b=0.0,
            overall_mean_upside_a=0.0, overall_mean_upside_b=0.0,
            strata_count=0, skipped_strata=0,
            recommendation="Insufficient data",
            strata_results=[],
        )

    total_n_a = sum(r.n_a for r in strata_results)
    total_n_b = sum(r.n_b for r in strata_results)

    # Weighted average win rates
    if total_n_a > 0:
        weighted_wr_a = sum(r.win_rate_a * r.n_a for r in strata_results) / total_n_a
        weighted_upside_a = sum(r.mean_upside_a * r.n_a for r in strata_results) / total_n_a
    else:
        weighted_wr_a = 0.0
        weighted_upside_a = 0.0

    if total_n_b > 0:
        weighted_wr_b = sum(r.win_rate_b * r.n_b for r in strata_results) / total_n_b
        weighted_upside_b = sum(r.mean_upside_b * r.n_b for r in strata_results) / total_n_b
    else:
        weighted_wr_b = 0.0
        weighted_upside_b = 0.0

    # Overall recommendation using pooled proportions (Fisher's method or meta-recommendation)
    adopt_count = sum(
        1 for r in strata_results
        if derive_recommendation(r, alpha, min_effect_size) == "Adopt v2.3"
    )
    keep_count = sum(
        1 for r in strata_results
        if derive_recommendation(r, alpha, min_effect_size) == "Keep v2.2"
    )

    if adopt_count > keep_count and adopt_count > 0:
        overall_rec = "Adopt v2.3"
    elif keep_count > adopt_count and keep_count > 0:
        overall_rec = "Keep v2.2"
    else:
        # Fall back to pooled win-rate comparison
        delta = weighted_wr_b - weighted_wr_a
        if abs(delta) >= min_effect_size:
            overall_rec = "Adopt v2.3" if delta > 0 else "Keep v2.2"
        else:
            overall_rec = "Continue testing"

    return OverallResult(
        total_n_a=total_n_a,
        total_n_b=total_n_b,
        overall_win_rate_a=weighted_wr_a,
        overall_win_rate_b=weighted_wr_b,
        overall_mean_upside_a=weighted_upside_a,
        overall_mean_upside_b=weighted_upside_b,
        strata_count=len(strata_results),
        skipped_strata=0,  # populated by caller when filtering strata
        recommendation=overall_rec,
        strata_results=strata_results,
    )


# ---------------------------------------------------------------------------
# TAIEX regime detection
# ---------------------------------------------------------------------------

def _detect_taiex_regime(signal_date: date) -> str:
    """Detect TAIEX regime for a given date using cached OHLCV history.

    Mirrors the regime gate logic from TripleConfirmationEngine:
    - uptrend   : TAIEX close > 63-day EMA
    - neutral   : between 63-day and 250-day EMA
    - downtrend : TAIEX close < 250-day EMA

    Falls back to 'unknown' if data is unavailable.
    """
    try:
        import pandas as pd  # type: ignore[import]
        from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient  # type: ignore[import]

        finmind = FinMindClient()
        start = signal_date - timedelta(days=400)
        df = finmind.fetch_ohlcv("Y9999", start_date=start, end_date=signal_date)
        if df is None or df.empty:
            return "unknown"

        df = df.sort_values("trade_date")
        df["ema63"] = df["close"].ewm(span=63, adjust=False).mean()
        df["ema250"] = df["close"].ewm(span=250, adjust=False).mean()

        on_date = df[df["trade_date"] <= signal_date]
        if on_date.empty:
            return "unknown"

        last = on_date.iloc[-1]
        close = float(last["close"])
        ema63 = float(last["ema63"])
        ema250 = float(last["ema250"])

        if close > ema63:
            return "uptrend"
        elif close > ema250:
            return "neutral"
        else:
            return "downtrend"
    except Exception as e:
        logger.debug("_detect_taiex_regime %s: %s", signal_date, e)
        return "unknown"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_signal_records(
    cache_path: Path = _CACHE_PATH,
    exclude_pending: bool = True,
    enrich_regime: bool = False,
) -> list[dict]:
    """Load signal records from the accuracy_monitor JSON cache.

    Args:
        cache_path: Path to signal_outcomes_cache.json.
        exclude_pending: If True, pending signals are filtered out.
        enrich_regime: If True, attempt TAIEX regime detection per signal date.
                       This triggers API calls and is slow — use with care.

    Returns list of signal dicts ready for stratification.
    """
    if not cache_path.exists():
        logger.warning("Cache not found: %s", cache_path)
        return []

    try:
        with open(cache_path, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load cache %s: %s", cache_path, e)
        return []

    regime_cache: dict[date, str] = {}
    records: list[dict] = []

    for sig_id, d in raw.get("signals", {}).items():
        try:
            sig_date = date.fromisoformat(d["signal_date"])
        except (KeyError, ValueError):
            continue

        pending = bool(d.get("pending", False))
        if exclude_pending and pending:
            continue

        if enrich_regime:
            if sig_date not in regime_cache:
                regime_cache[sig_date] = _detect_taiex_regime(sig_date)
            taiex_regime = regime_cache[sig_date]
        else:
            taiex_regime = d.get("taiex_regime", "unknown")

        records.append({
            "signal_id": sig_id,
            "ticker": d.get("ticker", ""),
            "signal_date": sig_date,
            "confidence": int(d.get("confidence", 0)),
            "action": d.get("action", "LONG"),
            "market": d.get("market", "TSE"),
            "industry": d.get("industry", "未知"),
            "entry_price": float(d.get("entry_price", 0.0)),
            "twenty_day_high": float(d.get("twenty_day_high", 0.0)),
            "actual_breakout": bool(d.get("actual_breakout", False)),
            "days_to_breakout": int(d.get("days_to_breakout", 0)),
            "max_price": float(d.get("max_price", 0.0)),
            "upside_pct": float(d.get("upside_pct", 0.0)),
            "pending": pending,
            "taiex_regime": taiex_regime,
        })

    return records


# ---------------------------------------------------------------------------
# Per-stratum test runner
# ---------------------------------------------------------------------------

def run_ab_test_for_stratum(
    stratum_key: StratumKey,
    signals: list[dict],
    assignment: dict[str, str],
) -> ABTestResult:
    """Compute A/B test statistics for a single stratum."""
    stats = compute_group_stats(signals, assignment)

    a = stats.get("A", SignalGroupStats())
    b = stats.get("B", SignalGroupStats())

    tstat, tp = run_ttest(a.upside_values, b.upside_values)
    chi2_stat, cp = run_chi2(
        wins_a=a.wins, n_a=a.n,
        wins_b=b.wins, n_b=b.n,
    )
    d = cohens_d(a.upside_values, b.upside_values)

    if math.isnan(tp):
        tp = 1.0
    if math.isnan(cp):
        cp = 1.0

    return ABTestResult(
        stratum=stratum_key,
        n_a=a.n,
        n_b=b.n,
        win_rate_a=a.win_rate,
        win_rate_b=b.win_rate,
        mean_upside_a=a.mean_upside,
        mean_upside_b=b.mean_upside,
        ttest_p=tp,
        chi2_p=cp,
        cohens_d=d if d is not None and not math.isinf(d) else 0.0,
    )


def run_full_ab_test(
    signals: list[dict],
    seed: int = 42,
    min_signals_per_stratum: int = 10,
    alpha: float = 0.05,
    min_effect_size: float = 0.10,
) -> tuple[OverallResult, list[ABTestResult], list[StratumKey]]:
    """Run stratified A/B test across all strata.

    Returns (overall_result, per-stratum results, skipped strata keys).
    """
    settled = filter_settled_records(signals)

    if not settled:
        overall = aggregate_strata_results([])
        return overall, [], []

    # Group by stratum
    strata: dict[StratumKey, list[dict]] = defaultdict(list)
    for sig in settled:
        strata[build_stratum_key(sig)].append(sig)

    # Assign groups across all settled signals at once (ensures cross-stratum independence)
    assignment = assign_groups(settled, seed=seed)

    strata_results: list[ABTestResult] = []
    skipped_strata: list[StratumKey] = []

    for stratum_key, stratum_sigs in sorted(strata.items()):
        if len(stratum_sigs) < min_signals_per_stratum:
            skipped_strata.append(stratum_key)
            continue
        result = run_ab_test_for_stratum(stratum_key, stratum_sigs, assignment)
        strata_results.append(result)

    overall = aggregate_strata_results(strata_results, alpha=alpha, min_effect_size=min_effect_size)
    overall.skipped_strata = len(skipped_strata)

    return overall, strata_results, skipped_strata


# ---------------------------------------------------------------------------
# Rich console rendering
# ---------------------------------------------------------------------------

def _rec_color(rec: str) -> str:
    if "Adopt" in rec:
        return "bold green"
    if "Keep" in rec:
        return "bold yellow"
    return "dim"


def _fmt_wr(wr: float, n: int) -> str:
    pct = wr * 100
    color = "green" if pct >= 55 else ("yellow" if pct >= 45 else "red")
    return f"[{color}]{pct:.1f}%[/{color}] [dim]({n})[/dim]"


def _fmt_delta(delta: float) -> str:
    pct = delta * 100
    color = "green" if pct > 0 else ("red" if pct < 0 else "dim")
    sign = "+" if pct > 0 else ""
    return f"[{color}]{sign}{pct:.1f}pp[/{color}]"


def _fmt_p(p: float, alpha: float = 0.05) -> str:
    color = "green" if p < alpha else "dim"
    return f"[{color}]{p:.3f}[/{color}]"


def render_console_report(
    overall: OverallResult,
    strata_results: list[ABTestResult],
    skipped_strata: list[StratumKey],
    alpha: float = 0.05,
    min_signals: int = 10,
) -> None:
    """Render A/B test results as Rich tables to the console."""
    rec_color = _rec_color(overall.recommendation)

    _console.print(Panel(
        f"[bold white]v2.2 vs v2.3 A/B Test — Stratified Analysis[/bold white]\n"
        f"[dim]Strata tested: {overall.strata_count}  |  "
        f"Strata skipped (< {min_signals} signals): {overall.skipped_strata}[/dim]\n"
        f"Overall recommendation: [{rec_color}]{overall.recommendation}[/{rec_color}]",
        title="[bold magenta]A/B Test Framework[/bold magenta]",
        border_style="magenta",
        padding=(0, 2),
    ))

    # Overall summary row
    summary_tbl = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        title="[bold]Overall Results[/bold]",
        title_style="bold white",
    )
    summary_tbl.add_column("Group", width=12)
    summary_tbl.add_column("N", justify="right", width=8)
    summary_tbl.add_column("Win Rate", justify="right", width=16)
    summary_tbl.add_column("Mean Upside", justify="right", width=14)

    summary_tbl.add_row(
        "[dim]A (v2.2)[/dim]",
        str(overall.total_n_a),
        _fmt_wr(overall.overall_win_rate_a, overall.total_n_a),
        f"[dim]{overall.overall_mean_upside_a:.2f}%[/dim]",
    )
    summary_tbl.add_row(
        "[bold]B (v2.3)[/bold]",
        str(overall.total_n_b),
        _fmt_wr(overall.overall_win_rate_b, overall.total_n_b),
        f"{overall.overall_mean_upside_b:.2f}%",
    )
    delta_wr = overall.overall_win_rate_b - overall.overall_win_rate_a
    delta_upside = overall.overall_mean_upside_b - overall.overall_mean_upside_a
    summary_tbl.add_row(
        "[italic]Delta[/italic]",
        "",
        _fmt_delta(delta_wr),
        f"{'+'  if delta_upside >= 0 else ''}{delta_upside:.2f}%",
    )
    _console.print(summary_tbl)

    if not strata_results:
        _console.print(
            f"[yellow]No strata had >= {min_signals} signals. Cannot run statistical tests.[/yellow]"
        )
        return

    # Per-stratum table
    stratum_tbl = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold yellow",
        border_style="dim",
        title="[bold]Per-Stratum Results[/bold]",
        title_style="bold white",
    )
    stratum_tbl.add_column("Industry", width=12)
    stratum_tbl.add_column("Market", width=8)
    stratum_tbl.add_column("Regime", width=11)
    stratum_tbl.add_column("n_A", justify="right", width=6)
    stratum_tbl.add_column("n_B", justify="right", width=6)
    stratum_tbl.add_column("WR_A", justify="right", width=10)
    stratum_tbl.add_column("WR_B", justify="right", width=10)
    stratum_tbl.add_column("ΔWR", justify="right", width=10)
    stratum_tbl.add_column("χ² p", justify="right", width=8)
    stratum_tbl.add_column("d", justify="right", width=6)
    stratum_tbl.add_column("Rec", width=20)

    for r in strata_results:
        industry, market, regime = r.stratum
        rec = derive_recommendation(r, alpha)
        rec_c = _rec_color(rec)
        stratum_tbl.add_row(
            industry[:10],
            market,
            regime,
            str(r.n_a),
            str(r.n_b),
            _fmt_wr(r.win_rate_a, r.n_a),
            _fmt_wr(r.win_rate_b, r.n_b),
            _fmt_delta(r.win_rate_delta),
            _fmt_p(r.chi2_p, alpha),
            f"{r.cohens_d:+.2f}",
            f"[{rec_c}]{rec}[/{rec_c}]",
        )

    _console.print(stratum_tbl)

    # Skipped strata
    if skipped_strata:
        _console.print(f"\n[dim]Skipped strata (< {min_signals} signals):[/dim]")
        for key in skipped_strata:
            _console.print(f"  [dim]• {key[0]} / {key[1]} / {key[2]}[/dim]")

    # Final verdict
    _console.print(
        f"\n[bold]Verdict:[/bold] [{rec_color}]{overall.recommendation}[/{rec_color}]"
    )
    if overall.recommendation == "Adopt v2.3":
        _console.print(
            "  v2.3 shows statistically significant and practically meaningful improvement. "
            "Consider promoting to production."
        )
    elif overall.recommendation == "Keep v2.2":
        _console.print(
            "  v2.2 outperforms v2.3 significantly. Investigate v2.3 regressions before adopting."
        )
    else:
        _console.print(
            "  Results are inconclusive. Collect more signals or revisit stratification."
        )


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------

def export_json(
    overall: OverallResult,
    strata_results: list[ABTestResult],
    skipped_strata: list[StratumKey],
    output_path: Path,
) -> None:
    """Write results as JSON to output_path."""
    payload = {
        "overall": {
            "total_n_a": overall.total_n_a,
            "total_n_b": overall.total_n_b,
            "overall_win_rate_a": overall.overall_win_rate_a,
            "overall_win_rate_b": overall.overall_win_rate_b,
            "overall_mean_upside_a": overall.overall_mean_upside_a,
            "overall_mean_upside_b": overall.overall_mean_upside_b,
            "strata_count": overall.strata_count,
            "skipped_strata": overall.skipped_strata,
            "recommendation": overall.recommendation,
        },
        "strata": [
            {
                "industry": r.stratum[0],
                "market": r.stratum[1],
                "taiex_regime": r.stratum[2],
                "n_a": r.n_a,
                "n_b": r.n_b,
                "win_rate_a": r.win_rate_a,
                "win_rate_b": r.win_rate_b,
                "mean_upside_a": r.mean_upside_a,
                "mean_upside_b": r.mean_upside_b,
                "win_rate_delta": r.win_rate_delta,
                "ttest_p": r.ttest_p,
                "chi2_p": r.chi2_p,
                "cohens_d": r.cohens_d,
                "recommendation": derive_recommendation(r),
            }
            for r in strata_results
        ],
        "skipped_strata": [
            {"industry": k[0], "market": k[1], "taiex_regime": k[2]}
            for k in skipped_strata
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _console.print(f"\n  [green]JSON 已匯出：[/green]{output_path}")


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>A/B Test Report — v2.2 vs v2.3</title>
  <style>
    :root {{
      --bg: #0f1117; --surface: #1a1d2e; --accent: #7c3aed;
      --green: #22c55e; --red: #ef4444; --yellow: #eab308;
      --text: #e2e8f0; --muted: #64748b;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 2rem; }}
    h1 {{ color: #a78bfa; margin-bottom: 0.5rem; font-size: 1.75rem; }}
    h2 {{ color: #c4b5fd; font-size: 1.2rem; margin: 2rem 0 1rem; }}
    .subtitle {{ color: var(--muted); margin-bottom: 2rem; font-size: 0.9rem; }}
    .verdict {{ display: inline-block; padding: 0.6rem 1.4rem; border-radius: 8px;
               font-weight: bold; font-size: 1.1rem; margin-bottom: 2rem; }}
    .verdict.adopt {{ background: rgba(34,197,94,0.15); color: var(--green); border: 1px solid var(--green); }}
    .verdict.keep {{ background: rgba(234,179,8,0.15); color: var(--yellow); border: 1px solid var(--yellow); }}
    .verdict.continue {{ background: rgba(100,116,139,0.15); color: var(--muted); border: 1px solid var(--muted); }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-bottom: 2rem; }}
    .card {{ background: var(--surface); border-radius: 10px; padding: 1.2rem; }}
    .card .label {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; }}
    .card .value {{ font-size: 1.6rem; font-weight: bold; margin-top: 0.2rem; }}
    .card .sub {{ font-size: 0.85rem; color: var(--muted); margin-top: 0.3rem; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--surface); border-radius: 10px; overflow: hidden; }}
    th {{ background: rgba(124,58,237,0.3); color: #c4b5fd; padding: 0.75rem 1rem; text-align: left; font-size: 0.85rem; }}
    td {{ padding: 0.65rem 1rem; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 0.9rem; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: rgba(255,255,255,0.03); }}
    .green {{ color: var(--green); }}
    .red {{ color: var(--red); }}
    .yellow {{ color: var(--yellow); }}
    .muted {{ color: var(--muted); }}
    .sig {{ background: rgba(34,197,94,0.1); border-radius: 4px; padding: 1px 5px; }}
    footer {{ color: var(--muted); font-size: 0.8rem; margin-top: 3rem; text-align: center; }}
  </style>
</head>
<body>
  <h1>A/B Test Report</h1>
  <p class="subtitle">v2.2 (Control, Group A) vs v2.3 (Treatment, Group B) &mdash; Generated {generated_at}</p>

  <div class="verdict {verdict_class}">{verdict_text}</div>

  <div class="summary-grid">
    <div class="card">
      <div class="label">Group A — v2.2 Win Rate</div>
      <div class="value {wr_a_color}">{wr_a_pct}</div>
      <div class="sub">{n_a} signals</div>
    </div>
    <div class="card">
      <div class="label">Group B — v2.3 Win Rate</div>
      <div class="value {wr_b_color}">{wr_b_pct}</div>
      <div class="sub">{n_b} signals</div>
    </div>
    <div class="card">
      <div class="label">Win Rate Delta (B − A)</div>
      <div class="value {delta_color}">{delta_pct}</div>
      <div class="sub">Strata: {strata_count} tested, {skipped_strata} skipped</div>
    </div>
  </div>

  <h2>Per-Stratum Results</h2>
  <table>
    <thead>
      <tr>
        <th>Industry</th><th>Market</th><th>Regime</th>
        <th>n_A</th><th>n_B</th>
        <th>WR_A</th><th>WR_B</th><th>&Delta;WR</th>
        <th>&chi;&sup2; p</th><th>Cohen&rsquo;s d</th><th>Recommendation</th>
      </tr>
    </thead>
    <tbody>
{stratum_rows}
    </tbody>
  </table>

{skipped_section}

  <footer>Taiwan Stock A/B Test Framework &mdash; Phase 4.22</footer>
</body>
</html>
"""

_STRATUM_ROW_TEMPLATE = """\
      <tr>
        <td>{industry}</td><td>{market}</td><td>{regime}</td>
        <td class="muted">{n_a}</td><td class="muted">{n_b}</td>
        <td class="{wr_a_cls}">{wr_a}</td>
        <td class="{wr_b_cls}">{wr_b}</td>
        <td class="{delta_cls}">{delta}</td>
        <td class="{p_cls}">{chi2_p}</td>
        <td class="{d_cls}">{d_val}</td>
        <td class="{rec_cls}">{rec}</td>
      </tr>"""


def _wr_class(wr: float) -> str:
    if wr >= 0.55:
        return "green"
    if wr >= 0.45:
        return "yellow"
    return "red"


def _delta_class(delta: float) -> str:
    if delta > 0:
        return "green"
    if delta < 0:
        return "red"
    return "muted"


def _p_class(p: float, alpha: float = 0.05) -> str:
    return "green sig" if p < alpha else "muted"


def export_html(
    overall: OverallResult,
    strata_results: list[ABTestResult],
    skipped_strata: list[StratumKey],
    output_path: Path,
    alpha: float = 0.05,
) -> None:
    """Generate an HTML dashboard report."""
    from datetime import datetime

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Verdict styling
    rec = overall.recommendation
    if "Adopt" in rec:
        verdict_class = "adopt"
        verdict_text = f"Adopt v2.3 — treatment outperforms control"
    elif "Keep" in rec:
        verdict_class = "keep"
        verdict_text = "Keep v2.2 — control outperforms treatment"
    else:
        verdict_class = "continue"
        verdict_text = "Continue testing — inconclusive"

    # Summary card values
    wr_a_pct = f"{overall.overall_win_rate_a * 100:.1f}%"
    wr_b_pct = f"{overall.overall_win_rate_b * 100:.1f}%"
    delta = overall.overall_win_rate_b - overall.overall_win_rate_a
    delta_sign = "+" if delta >= 0 else ""
    delta_pct = f"{delta_sign}{delta * 100:.1f}pp"

    wr_a_color = _wr_class(overall.overall_win_rate_a)
    wr_b_color = _wr_class(overall.overall_win_rate_b)
    delta_color = _delta_class(delta)

    # Build stratum rows
    stratum_rows_html = ""
    for r in strata_results:
        industry, market, regime = r.stratum
        rec_str = derive_recommendation(r, alpha)
        rec_cls = ("green" if "Adopt" in rec_str else ("yellow" if "Keep" in rec_str else "muted"))
        d_cls = "green" if r.cohens_d > 0.2 else ("red" if r.cohens_d < -0.2 else "muted")
        delta_wr = r.win_rate_delta
        delta_wr_str = f"{'+'  if delta_wr >= 0 else ''}{delta_wr * 100:.1f}pp"
        stratum_rows_html += _STRATUM_ROW_TEMPLATE.format(
            industry=industry,
            market=market,
            regime=regime,
            n_a=r.n_a,
            n_b=r.n_b,
            wr_a=f"{r.win_rate_a * 100:.1f}%",
            wr_b=f"{r.win_rate_b * 100:.1f}%",
            wr_a_cls=_wr_class(r.win_rate_a),
            wr_b_cls=_wr_class(r.win_rate_b),
            delta=delta_wr_str,
            delta_cls=_delta_class(delta_wr),
            chi2_p=f"{r.chi2_p:.3f}",
            p_cls=_p_class(r.chi2_p, alpha),
            d_val=f"{r.cohens_d:+.2f}",
            d_cls=d_cls,
            rec=rec_str,
            rec_cls=rec_cls,
        )

    # Skipped strata section
    if skipped_strata:
        rows = "".join(
            f'<li class="muted">{k[0]} / {k[1]} / {k[2]}</li>'
            for k in skipped_strata
        )
        skipped_section = (
            f'<h2>Skipped Strata (insufficient signals)</h2>'
            f'<ul style="padding-left:1.5rem;color:var(--muted);font-size:0.9rem;">{rows}</ul>'
        )
    else:
        skipped_section = ""

    html = _HTML_TEMPLATE.format(
        generated_at=generated_at,
        verdict_class=verdict_class,
        verdict_text=verdict_text,
        wr_a_pct=wr_a_pct,
        wr_b_pct=wr_b_pct,
        n_a=overall.total_n_a,
        n_b=overall.total_n_b,
        wr_a_color=wr_a_color,
        wr_b_color=wr_b_color,
        delta_pct=delta_pct,
        delta_color=delta_color,
        strata_count=overall.strata_count,
        skipped_strata=overall.skipped_strata,
        stratum_rows=stratum_rows_html,
        skipped_section=skipped_section,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    _console.print(f"\n  [green]HTML 報告已匯出：[/green]{output_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Stratified A/B test: v2.2 (control) vs v2.3 (treatment) engine performance. "
            "Reads signal_outcomes_cache.json from accuracy_monitor."
        )
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.95,
        help="Statistical confidence level (default: 0.95 → alpha=0.05)",
    )
    parser.add_argument(
        "--min-signals-per-stratum",
        type=int,
        default=10,
        metavar="N",
        help="Minimum settled signals required per stratum to run tests (default: 10)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for deterministic group assignment (default: 42)",
    )
    parser.add_argument(
        "--min-effect-size",
        type=float,
        default=0.10,
        metavar="E",
        help="Minimum win-rate delta (absolute pp) to trigger Adopt/Keep (default: 0.10)",
    )
    parser.add_argument(
        "--report",
        nargs=2,
        metavar=("FORMAT", "PATH"),
        help="Generate a report. FORMAT: 'html'. E.g. --report html report.html",
    )
    parser.add_argument(
        "--export-json",
        default=None,
        metavar="PATH",
        help="Export raw results to JSON",
    )
    parser.add_argument(
        "--cache",
        default=None,
        metavar="PATH",
        help=f"Path to signal_outcomes_cache.json (default: {_CACHE_PATH})",
    )
    parser.add_argument(
        "--enrich-regime",
        action="store_true",
        help="Fetch TAIEX OHLCV to detect regime per signal date (slow, triggers API calls)",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip accuracy_monitor fetch; use existing cache only",
    )
    args = parser.parse_args()

    alpha = 1.0 - args.confidence
    cache_path = Path(args.cache) if args.cache else _CACHE_PATH

    _console.print(Panel(
        f"[bold white]A/B Test Framework[/bold white]\n"
        f"[dim]Cache: {cache_path}  |  "
        f"Alpha: {alpha:.2f}  |  "
        f"Min signals/stratum: {args.min_signals_per_stratum}  |  "
        f"Min effect size: {args.min_effect_size:.0%}[/dim]",
        border_style="cyan",
        padding=(0, 2),
    ))

    # Optionally run accuracy_monitor to update cache first
    if not args.no_fetch and not cache_path.exists():
        _console.print(
            "[yellow]Cache not found. Run [bold]make monitor[/bold] first to build "
            "signal_outcomes_cache.json, then re-run.[/yellow]"
        )
        sys.exit(1)

    # Load records
    _console.print("[dim]載入信號快取...[/dim]")
    signals = load_signal_records(
        cache_path=cache_path,
        exclude_pending=True,
        enrich_regime=args.enrich_regime,
    )

    if not signals:
        _console.print(
            "[yellow]No settled signals found in cache. "
            "Run [bold]make monitor[/bold] to populate the cache.[/yellow]"
        )
        sys.exit(0)

    _console.print(f"  [dim]載入 [bold]{len(signals)}[/bold] 個已結算信號[/dim]")

    # Run A/B test
    _console.print("[dim]執行分層 A/B 測試...[/dim]")
    overall, strata_results, skipped_strata = run_full_ab_test(
        signals=signals,
        seed=args.seed,
        min_signals_per_stratum=args.min_signals_per_stratum,
        alpha=alpha,
        min_effect_size=args.min_effect_size,
    )

    # Console report
    render_console_report(
        overall=overall,
        strata_results=strata_results,
        skipped_strata=skipped_strata,
        alpha=alpha,
        min_signals=args.min_signals_per_stratum,
    )

    # Optional exports
    if args.export_json:
        export_json(overall, strata_results, skipped_strata, Path(args.export_json))

    if args.report:
        fmt, path_str = args.report
        if fmt.lower() == "html":
            export_html(overall, strata_results, skipped_strata, Path(path_str), alpha=alpha)
        else:
            _console.print(f"[red]Unknown report format: {fmt}. Supported: html[/red]")
            sys.exit(1)


if __name__ == "__main__":
    main()
