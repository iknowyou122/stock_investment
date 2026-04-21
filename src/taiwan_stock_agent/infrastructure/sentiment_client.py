"""Fetches market breadth from TWSE and news headlines from Yahoo Finance RSS."""
from __future__ import annotations
import logging
import urllib.request
import urllib.error
import json
from xml.etree import ElementTree

from taiwan_stock_agent.domain.market_sentiment import BreadthData

logger = logging.getLogger(__name__)

_YAHOO_RSS_URL = "https://tw.stock.yahoo.com/rss"
_TWSE_BREADTH_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.asp?ex_ch=tse_t00.tw&json=1&delay=0"


def fetch_breadth() -> BreadthData | None:
    """Fetch advance/decline ratio from TWSE MIS.
    
    Returns BreadthData or None if fetch fails.
    TWSE MIS tse_t00.tw returns market-wide up/down count.
    """
    try:
        req = urllib.request.Request(_TWSE_BREADTH_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        # Data format: msgArray[0] has fields including "u" (up) and "d" (down)
        msg = data.get("msgArray", [{}])[0]
        up   = float(msg.get("u", 0) or 0)
        down = float(msg.get("d", 0) or 0)
        vol_ratio = 1.0  # placeholder: volume ratio computed elsewhere
        if down == 0:
            return BreadthData(ad_ratio=3.0, volume_ratio=vol_ratio)
        return BreadthData(ad_ratio=up / down, volume_ratio=vol_ratio)
    except Exception as e:
        logger.debug("fetch_breadth error: %s", e)
        return None


def fetch_news_headlines(max_items: int = 20) -> list[str]:
    """Fetch latest headlines from Yahoo Finance Taiwan RSS.
    
    Returns list of headline strings. Empty list on failure.
    """
    try:
        req = urllib.request.Request(_YAHOO_RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            xml_data = resp.read()
        root = ElementTree.fromstring(xml_data)
        titles = []
        for item in root.iter("item"):
            title_el = item.find("title")
            if title_el is not None and title_el.text:
                titles.append(title_el.text.strip())
            if len(titles) >= max_items:
                break
        return titles
    except Exception as e:
        logger.debug("fetch_news_headlines error: %s", e)
        return []
