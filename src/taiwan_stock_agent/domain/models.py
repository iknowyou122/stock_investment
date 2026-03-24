from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    data_quality_flags: list[str] = Field(default_factory=list)


class VolumeProfile(BaseModel):
    """
    Phase 1–3 proxy: POC = 20-day high (real VolumeProfile requires intraday data, Phase 4+).
    Target price = poc_proxy * 1.05 (5% above 20-day high).
    """
    ticker: str
    period_end: date
    poc_proxy: float          # 20-day high; used as resistance proxy
    twenty_day_high: float
    twenty_day_sessions: int  # actual sessions counted (may be <20 near listing or holidays)
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
