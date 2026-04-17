"""Telegram message formatters for the bot daemon."""
from __future__ import annotations

_COILING_FLAGS = {"COILING_PRIME", "COILING", "EMERGING_SETUP"}


def _action_label(action: str, flags: str = "") -> str:
    if any(f in (flags or "") for f in _COILING_FLAGS):
        return "[蓄積]"
    return "[強勢]"


def format_opening_list(signals: list[dict], scan_date: str) -> str:
    if not signals:
        return f"開盤名單 {scan_date}\n\n無符合條件標的（0 檔）"

    coiling = [s for s in signals if any(f in (s.get("flags") or "") for f in _COILING_FLAGS)]
    lines = [f"開盤監控名單 {scan_date}\n"]
    for s in signals[:10]:
        label = _action_label(s["action"], s.get("flags", ""))
        name = s.get("name", "")
        # 第一行：代號 名稱 (分數)
        lines.append(f"*{s['ticker']} {name}* ({s['confidence']}分)")
        # 第二行：買/止/看 (交易核心數據)
        lines.append(f"買:{s['entry_bid']:.1f} | 止:{s['stop_loss']:.1f} | 看:{s['target']:.1f}\n")
    
    lines.append(f"共 {len(signals)} 檔入監控，蓄積中 {len(coiling)} 檔")
    return "\n".join(lines)


def format_entry_signal(ticker: str, name: str, price: float, entry_low: float, entry_high: float, stop: float) -> str:
    in_zone = entry_low <= price <= entry_high
    status = "(在區間)" if in_zone else f"(等{entry_low:.0f}-{entry_high:.0f})"
    return (
        f"進場訊號\n"
        f"*{ticker} {name}*\n"
        f"現價:{price:.1f} {status}\n"
        f"停損:{stop:.1f}"
    )


def format_postmarket_report(
    yesterday_signals: list[dict],
    intraday_hits: list[dict],
    tomorrow_signals: list[dict],
    report_date: str,
) -> str:
    total = len(yesterday_signals)
    hit_count = sum(1 for h in intraday_hits if h.get("triggered"))
    hit_rate = f"{hit_count}/{total} ({hit_count/total:.0%})" if total else "N/A"

    lines = [f"盤後報告 {report_date}\n"]
    lines.append("--- 今日命中率 ---")
    lines.append(f"昨日 {total} 檔 → {hit_count} 檔進場 ({hit_rate})\n")
    
    # 建立名稱查找表
    name_map = {s["ticker"]: s.get("name", "") for s in yesterday_signals}
    
    for h in intraday_hits[:15]:
        status = "[進場]" if h.get("triggered") else "[未達]"
        ticker = h["ticker"]
        name = name_map.get(ticker, "")
        lines.append(f"{status} {ticker} {name} (價{h.get('price', '–')})")

    lines.append("\n--- 隔日建倉名單 ---")
    for s in tomorrow_signals[:8]:
        name = s.get("name", "")
        # 第一行：代號 名稱 (分數)
        lines.append(f"*{s['ticker']} {name}* ({s['confidence']}分)")
        # 第二行：核心交易區間
        lines.append(f"買:{s['entry_bid']:.1f} | 止:{s['stop_loss']:.1f} | 看:{s['target']:.1f}\n")

    coiling_tomorrow = [s for s in tomorrow_signals if any(f in (s.get("flags") or "") for f in _COILING_FLAGS)]
    if coiling_tomorrow:
        lines.append("--- 蓄積待發標的 ---")
        for s in coiling_tomorrow[:5]:
            name = s.get("name", "")
            lines.append(f"{s['ticker']} {name} (蓄積)")

    return "\n".join(lines)
