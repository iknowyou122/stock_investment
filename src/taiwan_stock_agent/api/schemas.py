"""Pydantic response/request schemas for the 分點情報 API."""

from __future__ import annotations

from datetime import date
from typing import Literal

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
