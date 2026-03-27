"""Pydantic response/request schemas for the 分點情報 API."""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Broker label endpoint
# ---------------------------------------------------------------------------

class BrokerLabelResponse(BaseModel):
    branch_code: str = Field(..., examples=["9600"])
    branch_name: str = Field(..., examples=["凱基-台北"])
    label: Literal["隔日沖", "波段贏家", "地緣券商", "代操官股", "unknown"]
    reversal_rate: float = Field(..., ge=0.0, le=1.0, examples=[0.74])
    sample_count: int = Field(..., ge=0, examples=[234])
    confidence: float = Field(..., ge=0.0, le=1.0, examples=[0.91])
    updated_at: date


# ---------------------------------------------------------------------------
# Signal endpoint
# ---------------------------------------------------------------------------

class TripleConfirmation(BaseModel):
    momentum: bool
    chip_concentration: bool
    price_above_poc: bool


class TopBroker(BaseModel):
    branch_code: str
    branch_name: str
    label: Literal["隔日沖", "波段贏家", "地緣券商", "代操官股", "unknown"]
    buy_volume: int = Field(..., ge=0)


class SignalResponse(BaseModel):
    ticker: str = Field(..., examples=["2330"])
    date: date
    signal: Literal["LONG", "WATCH", "CAUTION"]
    confidence: int = Field(..., ge=0, le=100)
    triple_confirmation: TripleConfirmation
    top_brokers: list[TopBroker]
    risk_flags: list[str]


# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    detail: str


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Track-record endpoint
# ---------------------------------------------------------------------------

class ConfidenceTierStats(BaseModel):
    count: int
    win_rate_1d: float | None
    win_rate_3d: float | None
    win_rate_5d: float | None


class TrackRecordResponse(BaseModel):
    days: int
    total_signals: int
    long_count: int
    win_rate_1d: float | None
    win_rate_3d: float | None
    win_rate_5d: float | None
    by_confidence_tier: dict[str, ConfidenceTierStats]


# ---------------------------------------------------------------------------
# API key registration
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: str
    tier: Literal["free", "pro"] = "free"


class RegisterResponse(BaseModel):
    api_key: str | None  # None when payment is pending (pro-tier stub)
    tier: str
    message: str
    checkout_url: str | None = None    # populated only for pro-tier stub
    payment_status: str | None = None  # 'pending' for pro stub


# ---------------------------------------------------------------------------
# Community outcome submission (Phase 4)
# ---------------------------------------------------------------------------

class OutcomeRequest(BaseModel):
    did_buy: bool
    outcome: Literal["win", "lose", "break_even"] | None = None


class OutcomeResponse(BaseModel):
    message: str
    signal_id: str
    community_count: int
