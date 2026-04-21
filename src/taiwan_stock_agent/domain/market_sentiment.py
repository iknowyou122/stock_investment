from __future__ import annotations
from dataclasses import dataclass, field

_BEARISH_WORDS = ["升息", "制裁", "暴跌", "崩盤", "停牌", "調查", "虧損", "下修", "賣壓"]
_HOT_KEYWORDS  = ["AI", "CoWoS", "HBM", "電動車", "記憶體", "機器人", "散熱", "伺服器", "半導體"]


@dataclass
class BreadthData:
    ad_ratio: float      # advance / decline count ratio
    volume_ratio: float  # today volume / 20d avg volume


@dataclass
class MarketSentiment:
    label: str
    emoji: str
    ad_ratio: float
    taiex_rsi: float
    volume_ratio: float
    alerts: list[str] = field(default_factory=list)
    hot_keywords: list[str] = field(default_factory=list)


def compute_sentiment(
    breadth: BreadthData,
    headlines: list[str],
    taiex_rsi: float,
) -> MarketSentiment:
    """Compute market sentiment from quantitative breadth + news headlines."""
    # Label logic
    is_red = (
        breadth.ad_ratio < 0.8
        or taiex_rsi < 40
    )
    is_green = (
        breadth.ad_ratio > 2.0
        and 50 <= taiex_rsi <= 70
        and breadth.volume_ratio > 1.0
    )
    if is_red:
        label, emoji = "偏空謹慎", "🔴"
    elif is_green:
        label, emoji = "多頭熱絡", "🟢"
    else:
        label, emoji = "中性震盪", "🟡"

    # Scan headlines for keywords
    all_text = " ".join(headlines)
    alerts = [w for w in _BEARISH_WORDS if w in all_text]
    hot_keywords = [w for w in _HOT_KEYWORDS if w in all_text]

    return MarketSentiment(
        label=label,
        emoji=emoji,
        ad_ratio=breadth.ad_ratio,
        taiex_rsi=taiex_rsi,
        volume_ratio=breadth.volume_ratio,
        alerts=alerts,
        hot_keywords=hot_keywords,
    )
