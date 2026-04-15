"""Telegram message formatters for the bot daemon."""
from __future__ import annotations

_COILING_FLAGS = {"COILING_PRIME", "COILING", "EMERGING_SETUP"}


def _action_emoji(action: str, flags: str = "") -> str:
    if any(f in (flags or "") for f in _COILING_FLAGS):
        return "⚡"
    return "🟢"


def format_opening_list(signals: list[dict], scan_date: str) -> str:
    if not signals:
        return f"📋 開盤名單 {scan_date}\n\n無符合條件標的（0 檔）"

    coiling = [s for s in signals if any(f in (s.get("flags") or "") for f in _COILING_FLAGS)]
    lines = [f"📋 *開盤名單* {scan_date}\n"]
    for s in signals[:10]:
        emoji = _action_emoji(s["action"], s.get("flags", ""))
        name = s.get("name", "")
        lines.append(
            f"{emoji} *{s['ticker']} {name}* \n 信心:{s['confidence']}"
            f"  入場 {s['entry_bid']:.0f}  目標 {s['target']:.0f}  停損 {s['stop_loss']:.0f}"
        )
    lines.append(f"\n共 {len(signals)} 檔入監控，蓄積待噴發 {len(coiling)} 檔")
    return "\n".join(lines)


def format_entry_signal(ticker: str, name: str, price: float, entry_low: float, entry_high: float, stop: float) -> str:
    in_zone = entry_low <= price <= entry_high
    status = "✅ 在入場區間" if in_zone else f"⏳ 等待（入場區 {entry_low:.0f}–{entry_high:.0f}）"
    return (
        f"🔔 *進場訊號*\n"
        f"*{ticker} {name}*  現價 {price:.0f} {status}\n"
        f"停損 {stop:.0f}"
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

    lines = [f"📈 *盤後報告* {report_date}\n"]
    lines.append("━━ 今日命中率 ━━")
    lines.append(f"昨日名單 {total} 檔 → {hit_count} 檔達進場條件 ({hit_rate})")
    
    # 建立名稱查找表
    name_map = {s["ticker"]: s.get("name", "") for s in yesterday_signals}
    
    for h in intraday_hits:
        icon = "✅" if h.get("triggered") else "⏳"
        ticker = h["ticker"]
        name = name_map.get(ticker, "")
        lines.append(f"{icon} *{ticker} {name}*  現價 {h.get('price', '–')}")

    lines.append("\n━━ 隔日建倉名單 ━━")
    for s in tomorrow_signals[:8]:
        name = s.get("name", "")
        lines.append(
            f"🟢 *{s['ticker']} {name}*  信心:{s['confidence']}"
            f"  入場 {s['entry_bid']:.0f}  目標 {s['target']:.0f}  停損 {s['stop_loss']:.0f}"
        )

    coiling_tomorrow = [s for s in tomorrow_signals if any(f in (s.get("flags") or "") for f in _COILING_FLAGS)]
    if coiling_tomorrow:
        lines.append("\n━━ 蓄積待噴發（T-1/T-2 佈局）━━")
        for s in coiling_tomorrow[:4]:
            name = s.get("name", "")
            lines.append(f"⚡ *{s['ticker']} {name}*  {s.get('flags','')}")

    return "\n".join(lines)
