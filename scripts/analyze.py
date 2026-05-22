"""單股深度分析 — 輸入一個股票代號，輸出完整買賣建議與因子解釋。

用法:
    python scripts/analyze.py 2330
    python scripts/analyze.py 2330 --date 2026-05-20
    python scripts/analyze.py 2330 --llm gemini
    python scripts/analyze.py 2330 --no-llm
    make analyze TICKER=2330
    make analyze TICKER=2454 LLM=claude
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.text import Text
from rich.rule import Rule
from rich.columns import Columns
from rich.padding import Padding

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

console = Console()

logging.basicConfig(level=logging.WARNING)
logging.getLogger("taiwan_stock_agent").setLevel(logging.WARNING)

_CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "cache"

# ---------------------------------------------------------------------------
# Factor groups for display
# ---------------------------------------------------------------------------

_PILLAR1_FIELDS = [
    ("volume_ratio_pts",       "量比",            8),
    ("price_direction_pts",    "收盤方向",         3),
    ("close_strength_pts",     "K棒強度",          4),
    ("trend_continuity_pts",   "趨勢連續",         5),
    ("volume_escalation_pts",  "量能遞增",         5),
    ("rsi_momentum_pts",       "RSI動能",          4),
    ("dmi_initiation_pts",     "DMI啟動",          6),
    ("volume_dryup_pts",       "量能萎縮",         8),
    ("volume_climax_pts",      "量能高潮後縮",     4),
    ("ma5_walk_pts",           "MA5 Walking",      2),
    ("vwap_advantage_pts",     "VWAP優勢",         6),
]

_PILLAR2A_FIELDS = [
    ("breadth_pts",            "淨買超廣度",       10),
    ("concentration_pts",      "籌碼集中",         10),
    ("continuity_pts",         "分點持續",         8),
    ("daytrade_filter_pts",    "隔日沖過濾",        7),
    ("foreign_broker_pts",     "外資分點",          5),
]

_PILLAR2B_FIELDS = [
    ("foreign_strength_pts",        "外資強度",         12),
    ("trust_strength_pts",          "投信強度",          8),
    ("dealer_strength_pts",         "自營強度",          4),
    ("institution_continuity_pts",  "法人連買",          8),
    ("institution_consensus_pts",   "三大法人共識",      4),
    ("margin_structure_pts",        "融資結構",          8),
    ("margin_utilization_pts",      "融資使用率",        4),
    ("sbl_pressure_pts",            "借券壓力",          0),
    ("inst_buy_pct_pts",           "法人買超佔比",       6),
    ("inst_synergy_pts",           "土洋合作",           5),
    ("margin_declining_pts",       "融資今日下降",       3),
    ("ownership_concentration_pts","集保大戶週增",       5),
]

_PILLAR2C_FIELDS = [
    ("obv_accumulation_pts",        "OBV吸籌",           5),
    ("vol_asymmetry_pts",           "量能非對稱",         4),
    ("dual_inst_flow_pts",          "法人雙向20D",        5),
    ("chip_cleanliness_pts",        "籌碼乾淨度",        10),
    ("obv_stealth_pts",             "OBV隱蔽吸籌",        3),
    ("margin_persist_decline_pts",  "融資連跌",           4),
    ("holder_count_declining_pts",  "股東人數收縮",       5),
    ("chip_concentration_accel_pts","大戶集中加速",       6),
    ("short_squeeze_setup_pts",     "軋空潛力",           5),
    ("stealth_accum_composite_pts", "隱蔽吸籌複合",      10),
    ("inst_buy_days_ratio_pts",    "法人買超天數比",      4),
    ("inst_flow_accel_pts",        "法人流向加速",        4),
    ("foreign_trend_accel_pts",    "外資趨勢加速",        4),
    ("short_cover_rate_pts",       "融券回補率",          4),
    ("large_holder_2w_trend_pts",  "大戶2週趨勢",         4),
    ("inst_accel_3d_10d_pts",     "法人短期加速",         4),
]

_PILLAR3_FIELDS = [
    ("breakout_20d_pts",       "突破20日高",        8),
    ("breakout_60d_pts",       "突破60日高",        5),
    ("breakout_quality_pts",   "突破質量",          2),
    ("breakout_volume_pts",    "突破量能確認",      3),
    ("ma_alignment_pts",       "均線多頭排列",      5),
    ("ma20_slope_pts",         "MA20斜率",          5),
    ("relative_strength_pts",  "相對強度5D",        5),
    ("longterm_rs_pts",        "長期相對強度",      8),
    ("near_highhist_pts",      "近歷史高點",        5),
    ("upside_space_pts",       "上漲空間",          5),
    ("bb_squeeze_breakout_pts","BB壓縮突破",        5),
    ("proximity_pts",          "突破距離",         12),
    ("bb_compression_pts",     "BB壓縮",            5),
    ("ma_convergence_pts",     "均線收斂",          4),
    ("consolidation_weeks_pts","整理週數",          4),
    ("inside_bar_pts",         "內包線",            3),
    ("prior_advance_pts",      "前段漲幅",          3),
    ("bb_upper_walk_pts",      "BB上軌行走",        3),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_date() -> date:
    try:
        from taiwan_stock_agent.utils.trading_calendar import is_trading_day
    except ImportError:
        is_trading_day = None

    now_hour = __import__("datetime").datetime.now().hour
    cutoff = 17
    candidate = date.today()
    if now_hour < cutoff:
        candidate -= timedelta(days=1)
    # Walk back to last weekday (simple fallback)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _build_market_map() -> dict[str, str]:
    market_cache = _CACHE_DIR / f"market_map_{date.today()}.json"
    if market_cache.exists():
        try:
            data = json.loads(market_cache.read_text())
            if data:
                return data
        except Exception:
            pass
    return {}


def _build_name_map() -> dict[str, str]:
    name_cache = _CACHE_DIR / f"name_map_{date.today()}.json"
    if name_cache.exists():
        try:
            data = json.loads(name_cache.read_text())
            if data:
                return data
        except Exception:
            pass
    return {}


def _pts_bar(pts: int, max_pts: int, width: int = 10) -> str:
    if max_pts <= 0:
        return "─" * width
    ratio = min(pts / max_pts, 1.0)
    filled = round(ratio * width)
    empty = width - filled
    if pts > 0:
        bar = "█" * filled + "░" * empty
    elif pts < 0:
        bar = "▼" * min(abs(filled), width)
    else:
        bar = "░" * width
    return bar


def _action_style(action: str) -> tuple[str, str]:
    return {
        "LONG":    ("bright_green", "建議買進 ▲"),
        "WATCH":   ("yellow",       "觀察等待 ◆"),
        "CAUTION": ("red",          "不建議進場 ▼"),
    }.get(action, ("white", action))


def _render_factor_table(
    title: str,
    fields: list[tuple[str, str, int]],
    pts_dict: dict,
) -> Table | None:
    rows = []
    for key, label, max_pts in fields:
        val = pts_dict.get(key)
        if val is None:
            continue
        rows.append((label, int(val), max_pts))

    if not rows:
        return None

    t = Table(box=box.SIMPLE, show_header=True, header_style="bold dim", padding=(0, 1))
    t.add_column(title, style="dim", min_width=14)
    t.add_column("得分", justify="right", min_width=5)
    t.add_column("上限", justify="right", min_width=5, style="dim")
    t.add_column("", min_width=12)

    total_pts = 0
    total_max = 0
    for label, val, max_pts in rows:
        bar = _pts_bar(val, max_pts)
        if val > 0:
            style = "green"
            bar_style = "bright_green"
        elif val < 0:
            style = "red"
            bar_style = "red"
        else:
            style = "dim"
            bar_style = "dim"
        t.add_row(label, f"[{style}]{val:+d}[/{style}]", str(max_pts), f"[{bar_style}]{bar}[/{bar_style}]")
        total_pts += val
        total_max += max_pts

    t.add_section()
    t.add_row(
        "[bold]小計[/bold]",
        f"[bold cyan]{total_pts:+d}[/bold cyan]",
        f"[dim]{total_max}[/dim]",
        "",
    )
    return t


def _render_reasoning(reasoning) -> None:
    if not reasoning:
        return

    fields = [
        ("verdict",       "操作結論",   "bold white"),
        ("position",      "倉位建議",   "cyan"),
        ("momentum",      "動能分析",   "white"),
        ("chip_analysis", "籌碼分析",   "white"),
        ("risk_factors",  "風險提示",   "yellow"),
    ]
    for attr, label, style in fields:
        val = getattr(reasoning, attr, "").strip()
        if not val:
            continue
        console.print(f"  [bold dim]{label}：[/bold dim][{style}]{val}[/{style}]")
        console.print()


def _translate_flag(flag: str) -> tuple[str, str] | None:
    """Return (style, human_message) for a flag, or None to suppress it."""
    import re

    # ── 硬 Gate 失敗（說明不建議原因）──
    m = re.match(r"GATE_FAIL:G1_TOO_FAR_BELOW:([\d.]+)%", flag)
    if m:
        return "red", f"股價距20日高點太遠（目前在 {m.group(1)}%），尚未進入整理蓄積區（需 ≥85%）"

    if re.match(r"GATE_FAIL:G1_ALREADY_BROKE_OUT", flag):
        m2 = re.search(r":([\d.]+)%", flag)
        pct = m2.group(1) if m2 else "?"
        return "yellow", f"股價已突破整理區太高（{pct}%），不適合在此追高，等回測再評估"

    m = re.match(r"GATE_FAIL:G2_BB_WIDE_PCT:([\w.]+)", flag)
    if m:
        return "red", f"布林帶過寬（壓縮度 {m.group(1)} 百分位），尚未形成蓄積壓縮型態"

    m = re.match(r"GATE_FAIL:G3_LOW_LIQ(?::([\d.]+)M)?", flag)
    if m:
        liq = f"（日均成交 {m.group(1)}M）" if m.group(1) else ""
        return "red", f"流動性不足{liq}，難以有效進出場"

    if re.match(r"GATE_FAIL:G4_REGIME:DOWNTREND", flag):
        return "red", "大盤處於下跌趨勢，整體市場不利多頭操作"

    m = re.match(r"GATE_FAIL:G5_OVERHEAD:([\d.]+)%", flag)
    if m:
        return "red", f"上方套牢壓力明顯（60日高點尚有 {100-float(m.group(1)):.1f}% 壓力帶），突破難度高"

    m = re.match(r"GATE_FAIL:G_CHIP:(\d+)/4", flag)
    if m:
        return "red", f"籌碼面條件不足（僅達到 {m.group(1)}/4 項），主力尚未明確進場"

    if "NO_SETUP" in flag:
        return "red", "未偵測到有效的蓄積型態，不符合進場條件"

    # ── Gate 通過（正面資訊）──
    m = re.match(r"GATE_PASS:G1_ZONE:([\d.]+)%", flag)
    if m:
        return "green", f"位置理想：股價在20日高點的 {m.group(1)}%，處於突破前整理帶"

    m = re.match(r"GATE_PASS:G2_BB_PCT:([\w.]+)", flag)
    if m:
        return "green", f"布林帶壓縮（{m.group(1)} 百分位），蓄積型態成形中"

    m = re.match(r"GATE_PASS:G3_LIQ:([\d.]+)M", flag)
    if m:
        return "green", f"流動性充足（日均成交 {m.group(1)}M）"

    m = re.match(r"GATE_PASS:G4_REGIME:(\w+)", flag)
    if m:
        regime_tw = {"uptrend": "多頭", "neutral": "中性", "bull": "強多頭"}.get(m.group(1), m.group(1))
        return "green", f"大盤 {regime_tw}，整體環境有利"

    m = re.match(r"GATE_PASS:G5_NO_OVERHEAD:([\d.]+)%", flag)
    if m:
        return "green", f"上方無明顯套牢壓力（60日高點重合度 {m.group(1)}%），突破阻力小"

    m = re.match(r"GATE_PASS:G_CHIP:(\d+)/4", flag)
    if m:
        return "green", f"籌碼條件達標（{m.group(1)}/4 項），主力籌碼面良好"

    # ── 正面訊號 ──
    if "COILING_PRIME" in flag:
        return "cyan", "強力壓縮蓄積（PRIME）：量縮、BB窄、籌碼集中，爆發前夕"

    if "COILING" in flag and "FAIL" not in flag and "PRIME" not in flag:
        return "cyan", "壓縮蓄積型態成形，靜待突破"

    m = re.match(r"STEALTH_ACCUM_PRIME:(\d+)/6", flag)
    if m:
        return "cyan", f"隱蔽吸籌 PRIME（{m.group(1)}/6）：多項指標同時顯示主力低調買進"

    m = re.match(r"STEALTH_ACCUM:(\d+)/6", flag)
    if m:
        return "cyan", f"疑似隱蔽吸籌（{m.group(1)}/6）：有主力低調建倉跡象"

    if "OBV_STEALTH" in flag:
        return "cyan", "OBV持續上升但股價橫盤：成交量帶著方向，資金悄悄進場"

    m = re.match(r"HOLDER_SHRINK:(\w+)\(([+-]?\d+)\)", flag)
    if m:
        weeks = "2週" if "2w" in m.group(1) else "1週"
        chg = m.group(2)
        return "cyan", f"股東人數連續{weeks}減少（{chg}人）：籌碼集中，浮籌洗出"

    m = re.match(r"CHIP_ACCEL_PRIME:([+-]?[\d.]+)%/wk", flag)
    if m:
        return "cyan", f"大戶持股加速集中（+{m.group(1)}%/週，千張大戶同步買進），強力吸籌訊號"

    m = re.match(r"CHIP_ACCEL:([+-]?[\d.]+)%/wk", flag)
    if m:
        return "cyan", f"大戶持股集中加速（+{m.group(1)}%/週），主力增加持倉"

    m = re.match(r"SHORT_SQUEEZE_SETUP:SMR=([\d.]+)(?:/SCR=([\d.]+))?", flag)
    if m:
        smr = float(m.group(1))
        scr = float(m.group(2)) if m.group(2) else None
        msg = f"具軋空潛力：券資比 {smr:.2f}"
        if scr:
            msg += f"，空頭已開始回補（回補率 {scr:.0%}）"
        return "cyan", msg

    if "EMERGING_SETUP" in flag:
        return "yellow", "潛在蓄積型態（尚未突破）：法人買進 + 均線整齊，值得持續追蹤"

    if "RS_LEADER" in flag:
        return "green", "長期相對強勢領漲股：近60/120日持續跑贏大盤"

    m = re.match(r"NEAR_HIST_HIGH:(\d+)d", flag)
    if m:
        return "green", f"接近 {m.group(1)} 日高點，位於強勢整理區"

    m = re.match(r"WITHIN_HIST_HIGH_10PCT:(\d+)d", flag)
    if m:
        return "green", f"在 {m.group(1)} 日高點10%以內，歷史壓力帶附近"

    if "INST_SYNERGY" in flag:
        return "green", "外資+投信同日雙買（土洋合作）：法人共識強"

    if "MARGIN_DECLINING" in flag:
        return "green", "融資餘額今日下降：散戶退出，籌碼結構轉佳"

    m = re.match(r"VOL_ASYM:([\d.]+)x", flag)
    if m:
        return "green", f"上漲日成交量是下跌日的 {m.group(1)} 倍：買盤比賣盤積極"

    if "MA5_WALK" in flag:
        return "green", "股價連續走在MA5之上：短線趨勢扎實"

    if "BB_UPPER_COIL" in flag:
        return "green", "沿布林帶上軌行走（強勢蓄積）：壓力帶即將被消化"

    if "DMI_TREND_INIT" in flag:
        return "green", "DMI趨勢啟動：+DI穿越-DI，多頭動能剛起步"

    if "DMI_TREND_CONT" in flag:
        return "green", "DMI趨勢持續：多頭力道仍在延伸"

    if "MOMENTUM_TRACK" in flag:
        return "green", "前日已出現訊號，今日動能持續追蹤中"

    if "BB_SQUEEZE_BREAKOUT" in flag:
        return "green", "布林帶壓縮後向上突破：技術面啟動訊號"

    if "TURNOVER_BREAKOUT" in flag:
        return "green", "換手率突破：新資金進場接手"

    # ── 負面訊號 ──
    if "CLOSE_WEAK_OUT_PATTERN" in flag:
        return "red", "K棒收盤偏弱（收在當日低點附近）：疑似有賣壓在上方壓制"

    if "VOL_EXHAUSTION_RISK" in flag:
        return "yellow", "量能過大（可能是噴出頂部）：小心追高，觀察隔日是否縮量"

    if "TAIFEX_FUTURES_BEARISH" in flag:
        return "red", "台指期外資淨空單：期貨市場偏空，現股操作需謹慎"

    if "SBL_BREAKOUT_FAIL" in flag:
        return "yellow", "借券賣出壓力升高，可能有機構放空動作"

    # ── COILING 細節失敗（靜默，不顯示）──
    if flag.startswith("COILING_FAIL:") or flag.startswith("COILING_Q") or flag.startswith("COILING_GATE") or flag.startswith("COILING_SCORE") or flag.startswith("COILING_SKIP"):
        return None

    # ── 內部旗標（不顯示）──
    suppress_prefixes = (
        "TWSE:", "scoring_version:", "FREE_TIER_MODE",
        "GATE_SKIP:", "GATE_FAIL:", "GATE_PASS:",  # residual after specific matches above
    )
    for prefix in suppress_prefixes:
        if flag.startswith(prefix) or flag == prefix.rstrip(":"):
            return None

    return None  # 不認識的 flag 不顯示


def _render_diagnosis(flags: list[str], action: str) -> None:
    """Render human-readable diagnosis from flags."""
    blockers = []
    positives = []
    cautions = []

    for flag in flags:
        result = _translate_flag(flag)
        if result is None:
            continue
        style, msg = result
        if style == "red":
            blockers.append(msg)
        elif style == "yellow":
            cautions.append(msg)
        else:
            positives.append(msg)

    has_content = blockers or positives or cautions
    if not has_content:
        return

    console.print(Rule("[bold]診斷[/bold]", style="dim"))
    console.print()

    if blockers:
        console.print("  [bold red]不建議進場的原因：[/bold red]")
        for msg in blockers:
            console.print(f"  [red]✗[/red] {msg}")
        console.print()

    if positives:
        label = "[bold green]主要優勢：[/bold green]" if action == "LONG" else "[bold cyan]正面訊號：[/bold cyan]"
        console.print(f"  {label}")
        for msg in positives:
            console.print(f"  [green]✓[/green] {msg}")
        console.print()

    if cautions:
        console.print("  [bold yellow]注意事項：[/bold yellow]")
        for msg in cautions:
            console.print(f"  [yellow]⚠[/yellow] {msg}")
        console.print()


def _filter_data_warnings(data_quality_flags: list[str]) -> list[str]:
    """Convert data_quality_flags to user-facing messages, suppressing internal noise."""
    result = []
    for f in data_quality_flags:
        if f in ("scoring_version:v2", "FREE_TIER_MODE"):
            continue
        if f.startswith("TWSE:") and ("RATE_LIMITED" in f or "T86" in f or "SBL" in f):
            continue
        if f.startswith("GATE_PASS:") or f.startswith("GATE_FAIL:") or f.startswith("GATE_SKIP:"):
            continue
        if f.startswith("TREND_CONT") or f == "NO_SETUP":
            continue
        if f == "NO_BROKER_DATA":
            result.append("分點券商籌碼資料不足（Pillar 2A 無法評分，需付費 FinMind Token）")
            continue
        result.append(f)
    return result


def _compute_breakout_readiness(pts: dict) -> tuple[int, list[str], str]:
    """Return (stars 0-5, active_signal_labels, verdict_text)."""
    signals = [
        (pts.get("bb_upper_walk_pts", 0) > 0,         "沿BB上軌行走（控盤吸籌）"),
        (pts.get("breakout_volume_pts", 0) > 0,        "量能確認突破方向"),
        (pts.get("institution_continuity_pts", 0) >= 4,"法人連買3日以上"),
        (pts.get("foreign_strength_pts", 0) >= 8,      "外資強力買超"),
        (pts.get("volume_dryup_pts", 0) >= 4,          "縮量整理（賣壓萎縮）"),
        (pts.get("short_squeeze_setup_pts", 0) > 0,    "空頭開始回補"),
        (pts.get("margin_persist_decline_pts", 0) >= 2,"融資持續降低（浮額洗出）"),
        (pts.get("chip_concentration_accel_pts", 0) > 0,"大戶加速集中"),
        (pts.get("vol_asymmetry_pts", 0) >= 2,         "上漲日成交量大於下跌日"),
        (pts.get("obv_stealth_pts", 0) > 0,            "OBV隱蔽上升（資金悄悄進場）"),
    ]
    active = [label for hit, label in signals if hit]
    count = len(active)

    if count <= 1:
        stars, verdict = 1, "條件不足，突破動能尚未累積"
    elif count <= 3:
        stars, verdict = 2, "機率偏低，可觀察但不宜追"
    elif count <= 5:
        stars, verdict = 3, "機率中等，需等量能配合再進"
    elif count <= 7:
        stars, verdict = 4, "機率較高，多項訊號支持"
    else:
        stars, verdict = 5, "多項訊號共振，突破動能強"

    return stars, active, verdict


def _infer_target_basis(plan, score_breakdown: dict | None) -> str:
    """Infer which resistance level was used as the target."""
    if not plan or not score_breakdown:
        return "最近壓力帶"
    target = plan.target
    close_est = round(plan.entry_bid_limit / 0.995, 2)
    if close_est <= 0:
        return "最近壓力帶"

    pts = score_breakdown.get("pts", {})
    sixty_d_high = None
    one_twenty_d_high = None
    fifty_two_w_high = None

    # Try to infer from upside_space_pts / longterm_rs context — not available directly.
    # Instead just label by distance bucket.
    up_pct = (target - close_est) / close_est * 100 if close_est > 0 else 0
    if up_pct <= 4.9:
        return "收盤×1.05（已近各壓力帶）"
    elif up_pct <= 12:
        return "60日高點（最近壓力帶）"
    elif up_pct <= 25:
        return "120日高點"
    else:
        return "52週高點（長期壓力帶）"


def _render_execution(plan, close: float | None, score_breakdown: dict | None = None) -> None:
    if not plan:
        return

    entry_mid = (plan.entry_bid_limit + plan.entry_max_chase) / 2
    close_est = round(plan.entry_bid_limit / 0.995, 2)
    entry_range = f"${plan.entry_bid_limit:.2f} – ${plan.entry_max_chase:.2f}"
    stop_str = f"${plan.stop_loss:.2f}"
    target_str = f"${plan.target:.2f}"

    up_pct = (plan.target - entry_mid) / entry_mid * 100 if entry_mid > 0 else 0
    dn_pct = (entry_mid - plan.stop_loss) / entry_mid * 100 if entry_mid > 0 else 0
    upside = f"+{up_pct:.1f}%"
    rr = f"{up_pct/dn_pct:.1f}x" if dn_pct > 0 else ""

    target_basis = _infer_target_basis(plan, score_breakdown)

    t = Table(box=box.SIMPLE_HEAD, show_header=False, padding=(0, 1))
    t.add_column("", style="dim", min_width=8)
    t.add_column("", min_width=22)
    t.add_column("", style="dim italic", min_width=34)

    t.add_row(
        "進場區間",
        f"[cyan]{entry_range}[/cyan]",
        "收盤價 ×0.995 ～ ×1.005（掛限價單 / 最高追價）",
    )
    t.add_row(
        "停損",
        f"[red]{stop_str}[/red]  [dim]（−{dn_pct:.1f}%）[/dim]",
        "收盤價 ×0.97，收盤跌破即停損出場",
    )
    t.add_row(
        "目標價",
        f"[green]{target_str}[/green]  [dim]（{upside}）[/dim]",
        f"依據 {target_basis}",
    )
    if rr:
        t.add_row(
            "風險報酬",
            f"[bold]R:R = {rr}[/bold]",
            "獲利空間 ÷ 停損空間",
        )
    console.print(t)
    console.print()
    console.print(
        "  [dim]目標價 = 上方最近一道真實壓力帶（60日高點 → 120日高點 → 52週高點），"
        "需至少距現價 3%。已在高點附近則用收盤×1.05。[/dim]"
    )

    # Breakout readiness (only show when there's a meaningful target above)
    if score_breakdown and up_pct > 3:
        pts_dict = score_breakdown.get("pts", {})
        stars, active_signals, verdict = _compute_breakout_readiness(pts_dict)
        star_filled = "★" * stars + "☆" * (5 - stars)
        star_color = (
            "bright_green" if stars >= 4
            else "yellow" if stars == 3
            else "red"
        )
        console.print()
        console.print(Rule("[bold]突破研判[/bold]", style="dim"))
        console.print()
        console.print(
            f"  [{star_color}]{star_filled}[/{star_color}]  "
            f"[bold]{verdict}[/bold]  "
            f"[dim]（{stars}/5，{len(active_signals)}/10 項訊號達標）[/dim]"
        )
        if active_signals:
            console.print()
            for sig in active_signals:
                console.print(f"  [green]✓[/green] [dim]{sig}[/dim]")
        console.print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="單股深度分析")
    parser.add_argument("ticker", help="股票代號，例如 2330")
    parser.add_argument("--date", help="分析日期 YYYY-MM-DD（預設最近交易日）")
    parser.add_argument("--llm", help="LLM provider: claude / gemini / openai（預設自動偵測）")
    parser.add_argument("--no-llm", action="store_true", help="關閉 LLM，只跑 deterministic 評分")
    parser.add_argument("--verbose", action="store_true", help="顯示所有因子（含零分）")
    args = parser.parse_args()

    ticker = args.ticker.strip()
    analysis_date = date.fromisoformat(args.date) if args.date else _default_date()

    # Market detection
    market_map = _build_market_map()
    market = market_map.get(ticker, "TSE")

    name_map = _build_name_map()
    company_name = name_map.get(ticker, "")

    # ── Imports ──────────────────────────────────────────────────────────────
    from taiwan_stock_agent.agents.strategist_agent import StrategistAgent, _LLM_DISABLED
    from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
    from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher
    from taiwan_stock_agent.domain.broker_label_classifier import BrokerLabelRepository

    try:
        from taiwan_stock_agent.infrastructure.db import init_pool
        from taiwan_stock_agent.domain.broker_label_classifier import PostgresBrokerLabelRepository
        label_repo: BrokerLabelRepository = PostgresBrokerLabelRepository(None)
    except Exception:
        from taiwan_stock_agent.domain.broker_label_classifier import InMemoryBrokerLabelRepository
        label_repo = InMemoryBrokerLabelRepository()

    llm_provider = None
    if args.no_llm:
        llm_provider = _LLM_DISABLED
    elif args.llm:
        from taiwan_stock_agent.domain.llm_provider import create_llm_provider
        llm_provider = create_llm_provider(provider=args.llm)

    finmind = FinMindClient()
    chip_fetcher = ChipProxyFetcher()

    agent = StrategistAgent(
        finmind=finmind,
        label_repo=label_repo,
        chip_proxy_fetcher=chip_fetcher,
        llm_provider=llm_provider,
    )

    # ── Run ──────────────────────────────────────────────────────────────────
    header_name = f"{ticker} {company_name}".strip()
    console.print()
    with console.status(f"[bold cyan]分析 {header_name} ({analysis_date})…[/bold cyan]"):
        signal = agent.run(ticker, analysis_date, market=market)

    # ── Header ───────────────────────────────────────────────────────────────
    color, label = _action_style(signal.action)
    conf_bar = "█" * (signal.confidence // 10) + "░" * (10 - signal.confidence // 10)
    title_text = (
        f"[bold]{header_name}[/bold]  [{color}]{label}[/{color}]  "
        f"信心指數 [bold cyan]{signal.confidence}[/bold cyan]/100  "
        f"[cyan]{conf_bar}[/cyan]  [dim]{market} · {analysis_date}[/dim]"
    )
    if signal.halt_flag:
        title_text += "  [bold red]⚠ HALT[/bold red]"
    console.print(Panel(title_text, border_style=color, expand=False))
    console.print()

    # ── LLM Reasoning ────────────────────────────────────────────────────────
    if signal.reasoning and any([
        signal.reasoning.verdict, signal.reasoning.position,
        signal.reasoning.momentum, signal.reasoning.chip_analysis,
        signal.reasoning.risk_factors,
    ]):
        console.print(Rule("[bold]AI 分析[/bold]", style="cyan"))
        console.print()
        _render_reasoning(signal.reasoning)
    else:
        console.print(Rule("[dim]AI 分析（未啟用）[/dim]", style="dim"))
        console.print()

    # ── Factor Breakdown ─────────────────────────────────────────────────────
    bd = signal.score_breakdown
    if not bd:
        console.print("[dim]（無因子分數資料）[/dim]")
    else:
        pts = bd.get("pts", {})
        flags = bd.get("flags", [])
        raw = bd.get("raw", {})

        console.print(Rule("[bold]因子分析[/bold]", style="dim"))
        console.print()

        # Raw indicators
        if raw:
            rsi = raw.get("rsi_14")
            vol_ratio = raw.get("volume_vs_20ma")
            ma20_slope = raw.get("ma20_slope_pct")
            parts = []
            if rsi is not None:
                rsi_style = "green" if 55 <= rsi <= 70 else "yellow" if rsi > 70 else "dim"
                parts.append(f"RSI(14)=[{rsi_style}]{rsi:.1f}[/{rsi_style}]")
            if vol_ratio is not None:
                v_style = "green" if 2 <= vol_ratio <= 3 else "bright_green" if vol_ratio >= 3 else "dim"
                parts.append(f"量比=[{v_style}]{vol_ratio:.2f}x[/{v_style}]")
            if ma20_slope is not None:
                s_style = "green" if ma20_slope > 0 else "red"
                parts.append(f"MA20斜率=[{s_style}]{ma20_slope:+.2f}%[/{s_style}]")
            if parts:
                console.print("  " + "  ".join(parts))
                console.print()

        # Pillar tables
        groups = [
            ("Pillar 1  動能 & 量價", _PILLAR1_FIELDS),
            ("Pillar 2A 分點籌碼",   _PILLAR2A_FIELDS),
            ("Pillar 2B 三大法人",   _PILLAR2B_FIELDS),
            ("Pillar 2C 籌碼深度",   _PILLAR2C_FIELDS),
            ("Pillar 3  結構 & 突破", _PILLAR3_FIELDS),
        ]

        for group_title, fields in groups:
            # Filter to only fields that have non-zero values (unless --verbose)
            if args.verbose:
                visible = [(k, l, m) for k, l, m in fields if k in pts]
            else:
                visible = [(k, l, m) for k, l, m in fields if pts.get(k, 0) != 0]

            if not visible:
                continue

            tbl = _render_factor_table(group_title, visible, pts)
            if tbl:
                console.print(Padding(tbl, (0, 2)))

        # Total
        total = sum(v for v in pts.values() if isinstance(v, (int, float)))
        console.print()
        console.print(f"  [bold]總分[/bold]  [bold cyan]{total}[/bold cyan] 分", end="")
        taiex_slope = bd.get("taiex_slope", "")
        if taiex_slope:
            regime_map = {
                "bull": ("[green]多頭擴張[/green]", ""),
                "neutral": ("[yellow]中性震盪[/yellow]", ""),
                "bear": ("[red]空頭警戒[/red]", ""),
            }
            label_tw, _ = regime_map.get(taiex_slope, (taiex_slope, ""))
            console.print(f"   大盤 {label_tw}", end="")
        console.print()

        # Human-readable diagnosis
        console.print()
        _render_diagnosis(flags, signal.action)

    # ── Monthly Revenue (Growth) ──────────────────────────────────────────────
    try:
        from batch_plan import _load_growth_index
        growth_index = _load_growth_index()
        grec = growth_index.get(ticker)
        if grec:
            console.print()
            console.print(Rule("[bold]月營收基本面[/bold]", style="dim"))
            console.print()
            yoy  = grec.get("yoy_pct") or 0.0
            mom  = grec.get("mom_pct") or 0.0
            con  = grec.get("consecutive", 0) or 0
            accel = grec.get("acceleration_pct") or 0.0
            score_g = grec.get("score", 0.0)

            yoy_color  = "red" if yoy > 0 else "green"
            mom_color  = "red" if mom > 0 else "green"
            accel_str  = (f"[red]+{accel:.1f}%[/red]" if accel > 0
                          else f"[green]{accel:.1f}%[/green]")

            from rich.table import Table as _Table
            from rich import box as _box
            gtbl = _Table(box=_box.SIMPLE, show_header=False, padding=(0, 2))
            gtbl.add_column(style="dim", width=20)
            gtbl.add_column()
            gtbl.add_column(style="dim")
            gtbl.add_row("月營收 YoY",   f"[{yoy_color}]{yoy:+.1f}%[/{yoy_color}]",  "年增率")
            gtbl.add_row("月營收 MoM",   f"[{mom_color}]{mom:+.1f}%[/{mom_color}]",  "月增率")
            gtbl.add_row("連續成長",      f"{con} 個月",                               "連續正 YoY 月數")
            gtbl.add_row("成長加速",      accel_str,                                   "本月 YoY − 上月 YoY")
            gtbl.add_row("成長評分",      f"{score_g:.1f} 分",                         "綜合評分（滿分 100）")
            console.print(gtbl)

            if yoy >= 50:
                console.print("  [bold red]★ 高成長股（YoY ≥50%），基本面評分 +8 pts[/bold red]")
            elif yoy >= 30:
                console.print("  [red]▲ 中成長股（YoY ≥30%），基本面評分 +5 pts[/red]")
            elif yoy >= 20:
                console.print("  [dim]▲ 成長股（YoY ≥20%），基本面評分 +3 pts[/dim]")
            if con >= 3:
                console.print(f"  [dim]+ 連續成長 {con} 個月，額外 +2 pts[/dim]")
    except Exception:
        pass

    # ── Execution Plan ────────────────────────────────────────────────────────
    console.print()
    console.print(Rule("[bold]執行計畫[/bold]", style="dim"))
    console.print()
    _render_execution(signal.execution_plan, None, score_breakdown=bd)

    # ── Data Quality ──────────────────────────────────────────────────────────
    data_warnings = _filter_data_warnings(signal.data_quality_flags)
    if data_warnings:
        console.print()
        console.print(Rule("[dim]資料說明[/dim]", style="dim"))
        for msg in data_warnings:
            console.print(f"  [dim]ℹ[/dim] {msg}")

    console.print()


if __name__ == "__main__":
    main()
