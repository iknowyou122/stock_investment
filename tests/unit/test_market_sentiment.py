from taiwan_stock_agent.domain.market_sentiment import (
    compute_sentiment, MarketSentiment, BreadthData
)

class TestComputeSentiment:
    def _breadth(self, ad_ratio: float, vol_ratio: float) -> BreadthData:
        return BreadthData(ad_ratio=ad_ratio, volume_ratio=vol_ratio)

    def test_green_label_when_all_positive(self):
        """ad_ratio > 2.0 AND rsi 50-70 AND vol_ratio > 1.0 → 多頭熱絡."""
        s = compute_sentiment(self._breadth(2.5, 1.2), [], taiex_rsi=60.0)
        assert s.label == "多頭熱絡"
        assert s.emoji == "🟢"

    def test_red_label_when_ad_ratio_low(self):
        """ad_ratio < 0.8 → 偏空謹慎."""
        s = compute_sentiment(self._breadth(0.5, 0.9), [], taiex_rsi=45.0)
        assert s.label == "偏空謹慎"
        assert s.emoji == "🔴"

    def test_red_label_when_rsi_low(self):
        """rsi < 40 → 偏空謹慎."""
        s = compute_sentiment(self._breadth(1.5, 1.0), [], taiex_rsi=35.0)
        assert s.label == "偏空謹慎"

    def test_yellow_label_by_default(self):
        """Normal conditions → 中性震盪."""
        s = compute_sentiment(self._breadth(1.2, 0.95), [], taiex_rsi=52.0)
        assert s.label == "中性震盪"
        assert s.emoji == "🟡"

    def test_bearish_keyword_in_headlines_creates_alert(self):
        """Headline with bearish keyword → alert string in s.alerts."""
        headlines = ["台股今日暴跌 外資大量出走", "Fed升息預期升溫"]
        s = compute_sentiment(self._breadth(1.5, 1.0), headlines, taiex_rsi=52.0)
        assert len(s.alerts) > 0
        assert any("暴跌" in a or "升息" in a for a in s.alerts)

    def test_hot_keyword_in_headlines_extracted(self):
        """Headline with hot keyword → keyword in s.hot_keywords."""
        headlines = ["AI伺服器需求大爆發 CoWoS訂單暢旺"]
        s = compute_sentiment(self._breadth(1.5, 1.0), headlines, taiex_rsi=60.0)
        # Check for presence of hot keywords
        assert any(kw in s.hot_keywords for kw in ["AI", "CoWoS"])
