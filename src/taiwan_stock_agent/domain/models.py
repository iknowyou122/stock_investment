from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Sector heat map models (Phase 2 — ScoutAgent.scan_sectors)
# ---------------------------------------------------------------------------


class DailyOHLCV(BaseModel):
    ticker: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    data_quality_flags: list[str] = Field(default_factory=list)


class BrokerLabel(BaseModel):
    branch_code: str
    branch_name: str
    label: Literal["隔日沖", "波段贏家", "地緣券商", "代操官股", "unknown"]
    reversal_rate: float
    sample_count: int
    last_updated: date
    metadata: dict = Field(default_factory=dict)


class BrokerWithLabel(BaseModel):
    branch_code: str
    branch_name: str
    label: str
    reversal_rate: float
    buy_volume: int
    sell_volume: int


class ChipReport(BaseModel):
    ticker: str
    report_date: date
    # top-15 branches by buy volume, each annotated with label
    top_buyers: list[BrokerWithLabel]
    concentration_top15: float      # top-15 buy vol / total buy vol (0–1)
    net_buyer_count_diff: int       # sum over last 3 days of (buying_branches - selling_branches)
    risk_flags: list[str]           # e.g. ['隔日沖_TOP3']
    active_branch_count: int        # number of branches with buy_volume > 0 today
    # v2 field: historical top-5 buyer lists for continuity scoring
    # index 0 = yesterday, 1 = 2 days ago, etc.
    historical_top5_buyers: list[list[BrokerWithLabel]] = Field(default_factory=list)
    data_quality_flags: list[str] = Field(default_factory=list)


class TWSEChipProxy(BaseModel):
    """Free-tier chip proxy fetched from TWSE opendata (no auth token required).

    Used when FinMind paid plan is unavailable (chip_data_available=False).
    is_available=False means the API call failed or returned no data for this ticker.
    """
    ticker: str
    trade_date: date
    foreign_net_buy: int = 0            # 外資買賣超 (shares); positive = net buy
    trust_net_buy: int = 0              # 投信買賣超 (shares); positive = net buy
    dealer_net_buy: int = 0             # 自營商買賣超 (shares); positive = net buy
    margin_balance_change: int = 0      # 融資餘額變化 vs previous day (shares); negative = decreasing
    # Factor 5: 外資連買天數
    foreign_consecutive_buy_days: int = 0   # consecutive days of foreign net buy (including today)
    # Factor 7: 融券餘額 + 券資比
    short_balance_increased: bool = False   # True when today's 融券餘額 > yesterday's by > 20%
    short_margin_ratio: float = 0.0         # 融券餘額 / 融資餘額 (券資比); deduction when > 0.15
    # Tier A expansion fields (chip-factors-expansion-plan)
    trust_consecutive_buy_days: int = 0       # 投信連買天數
    dealer_consecutive_buy_days: int = 0      # 自營商連買天數
    sbl_ratio: float = 0.0                    # 借券賣出占成交量比重 (0–1)
    sbl_available: bool = False               # True if SBL data was fetched successfully
    margin_utilization_rate: float | None = None  # 融資餘額/融資限額; None if column missing
    daytrade_ratio: float | None = None       # 當沖占成交量比重 (hint only)
    short_cover_days: float | None = None     # derived: short_balance/avg_daily_volume
    # v2 fields
    avg_20d_volume: int = 0                   # 20-day average daily volume (shares); used for ratio scoring
    is_available: bool = False
    data_quality_flags: list[str] = Field(default_factory=list)


class VolumeProfile(BaseModel):
    """
    Phase 1–3 proxy: POC = highest-volume day's close in last 20 sessions.
    This approximates where the most volume traded (real POC concept) better than
    using the 20-day high. Real VolumeProfile requires intraday tick data (Phase 4+).
    Target price = poc_proxy * 1.05 (5% above POC proxy).
    """
    ticker: str
    period_end: date
    poc_proxy: float          # highest-volume day's close in last 20 sessions
    twenty_day_high: float
    twenty_day_sessions: int  # actual sessions counted (may be <20 near listing or holidays)
    sixty_day_high: float = 0.0
    sixty_day_sessions: int = 0  # actual sessions in 60-day window
    # v2 fields: longer-horizon resistance levels for upside-space scoring
    one_twenty_day_high: float = 0.0        # 120-day high; upper resistance level
    one_twenty_day_sessions: int = 0        # actual sessions in 120-day window
    fiftytwo_week_high: float = 0.0         # 52-week high; annual resistance level
    fiftytwo_week_sessions: int = 0         # actual sessions in 52-week window
    data_quality_flags: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    entry_bid_limit: float    # close * 0.995 — lower bound limit order
    entry_max_chase: float    # close * 1.005 — upper bound max chase
    stop_loss: float          # T+0 closing price (not intraday VWAP — requires tick data)
    target: float             # poc_proxy * 1.05


class Reasoning(BaseModel):
    momentum: str = ""
    chip_analysis: str = ""
    risk_factors: str = ""


class SignalOutput(BaseModel):
    ticker: str
    date: date
    action: Literal["LONG", "WATCH", "CAUTION"]
    confidence: int = Field(ge=0, le=100)
    reasoning: Reasoning
    execution_plan: ExecutionPlan
    halt_flag: bool = False
    data_quality_flags: list[str] = Field(default_factory=list)
    free_tier_mode: bool | None = None   # None=legacy, True=free-tier signals, False=paid-tier
    score_breakdown: dict | None = None


class SectorChipScore(BaseModel):
    sector_name: str
    avg_concentration_top15: float     # mean over scanned tickers
    avg_net_buyer_count_diff: float    # mean over scanned tickers
    positive_signal_count: int         # tickers with confidence >= 50
    total_tickers_scanned: int


class SectorHeatMap(BaseModel):
    scan_date: date
    sectors: list[SectorChipScore]

    def to_text(self) -> str:
        """Plain-text table suitable for LINE group paste."""
        lines = [f"=== 板塊籌碼熱力圖 {self.scan_date} ==="]
        sorted_sectors = sorted(
            self.sectors, key=lambda s: s.positive_signal_count, reverse=True
        )
        for s in sorted_sectors:
            ratio = (
                s.positive_signal_count / s.total_tickers_scanned
                if s.total_tickers_scanned > 0
                else 0.0
            )
            if ratio >= 0.6:
                arrow = "↑↑↑"
            elif ratio >= 0.3:
                arrow = "↑"
            else:
                arrow = "→"

            net_sign = "+" if s.avg_net_buyer_count_diff >= 0 else ""
            lines.append(
                f"{s.sector_name:<6} {arrow:<3}  "
                f"{s.positive_signal_count}/{s.total_tickers_scanned} 強勢  "
                f"集中度 {s.avg_concentration_top15:.0%}  "
                f"淨買超差 {net_sign}{s.avg_net_buyer_count_diff:.0f}"
            )
        return "\n".join(lines)


@dataclass
class AnomalySignal:
    """Phase 2 market anomaly signal produced by ScoutAgent.

    Feeds StrategistAgent with pre-filtered candidates, reducing daily scan
    from O(market) to O(anomalies).

    trigger_type values:
      - "VOLUME_SURGE"       — daily volume exceeded 20-day avg × 2.0
      - "PRICE_BREAKOUT"     — close is within 1% of or above 20-day high
      - "SECTOR_CORRELATION" — >= 3 tickers in the same watchlist all had
                               VOLUME_SURGE + PRICE_BREAKOUT on the same day
    """

    ticker: str
    trade_date: date
    trigger_type: str  # "VOLUME_SURGE" | "PRICE_BREAKOUT" | "SECTOR_CORRELATION"
    magnitude: float   # volume_ratio or price_pct_above_high
    description: str
    data_quality_flags: list[str] = dc_field(default_factory=list)
