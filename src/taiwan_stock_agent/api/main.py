"""FastAPI application — 分點情報 API (Broker Label API).

Run locally:
    uvicorn src.taiwan_stock_agent.api.main:app --reload --port 8000

Endpoints
---------
GET /health
GET /v1/broker-label/{branch_code}
GET /v1/signal/{ticker}

Authentication
--------------
Pass ``X-API-Key: <key>`` header.  When the ``API_KEY`` env-var is unset,
authentication is skipped (development mode).

Rate limiting
-------------
Simple in-memory counters keyed by API key.  Not persisted across restarts —
swap for Redis calls when moving to production.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import date
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from .auth import require_api_key
from .schemas import (
    BrokerLabelResponse,
    ErrorResponse,
    HealthResponse,
    SignalResponse,
    TopBroker,
    TripleConfirmation,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Rate-limit configuration
# ---------------------------------------------------------------------------

RATE_LIMITS: dict[str, int] = {
    "free": 10_000,
    "pro": 50_000,
}

# Tier assignment by API key prefix for demo purposes.
# In production this would come from a database row.
_KEY_TIER: dict[str, str] = {
    "__dev__": "pro",  # local development gets pro limits
}

# In-memory monthly counter: {api_key: request_count}
# In production replace with Redis INCR + EXPIRE.
_request_counts: dict[str, int] = defaultdict(int)


def _check_rate_limit(api_key: str) -> None:
    """Increment the request counter and raise 429 if the monthly cap is hit."""
    tier = _KEY_TIER.get(api_key, "free")
    limit = RATE_LIMITS[tier]
    _request_counts[api_key] += 1
    if _request_counts[api_key] > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. {tier.capitalize()} tier allows {limit:,} requests/month.",
        )


# ---------------------------------------------------------------------------
# Mock data store
# ---------------------------------------------------------------------------

_BROKER_LABELS: dict[str, dict[str, Any]] = {
    "9600": {
        "branch_code": "9600",
        "branch_name": "凱基-台北",
        "label": "隔日沖",
        "reversal_rate": 0.74,
        "sample_count": 234,
        "confidence": 0.91,
        "updated_at": date(2024, 8, 6),
    },
    "9A00": {
        "branch_code": "9A00",
        "branch_name": "元大-竹北",
        "label": "波段贏家",
        "reversal_rate": 0.21,
        "sample_count": 312,
        "confidence": 0.87,
        "updated_at": date(2024, 8, 6),
    },
    "1480": {
        "branch_code": "1480",
        "branch_name": "摩根大通",
        "label": "代操官股",
        "reversal_rate": 0.18,
        "sample_count": 189,
        "confidence": 0.83,
        "updated_at": date(2024, 8, 6),
    },
    "6460": {
        "branch_code": "6460",
        "branch_name": "國泰-新竹",
        "label": "地緣券商",
        "reversal_rate": 0.38,
        "sample_count": 97,
        "confidence": 0.72,
        "updated_at": date(2024, 8, 6),
    },
}

_SIGNALS: dict[str, dict[str, Any]] = {
    "2330": {
        "ticker": "2330",
        "date": date(2024, 8, 6),
        "signal": "LONG",
        "confidence": 88,
        "triple_confirmation": {
            "momentum": True,
            "chip_concentration": True,
            "price_above_poc": True,
        },
        "top_brokers": [
            {
                "branch_code": "9A00",
                "branch_name": "元大-竹北",
                "label": "波段贏家",
                "buy_volume": 1_250_000,
            },
            {
                "branch_code": "9600",
                "branch_name": "凱基-台北",
                "label": "隔日沖",
                "buy_volume": 840_000,
            },
        ],
        "risk_flags": [],
    },
    "2454": {
        "ticker": "2454",
        "date": date(2024, 8, 6),
        "signal": "WATCH",
        "confidence": 55,
        "triple_confirmation": {
            "momentum": True,
            "chip_concentration": False,
            "price_above_poc": True,
        },
        "top_brokers": [
            {
                "branch_code": "9600",
                "branch_name": "凱基-台北",
                "label": "隔日沖",
                "buy_volume": 620_000,
            },
        ],
        "risk_flags": ["隔日沖_TOP3"],
    },
    "2317": {
        "ticker": "2317",
        "date": date(2024, 8, 6),
        "signal": "CAUTION",
        "confidence": 22,
        "triple_confirmation": {
            "momentum": False,
            "chip_concentration": False,
            "price_above_poc": False,
        },
        "top_brokers": [
            {
                "branch_code": "9600",
                "branch_name": "凱基-台北",
                "label": "隔日沖",
                "buy_volume": 430_000,
            },
        ],
        "risk_flags": ["隔日沖_TOP3", "動能衰竭"],
    },
}


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="分點情報 API",
    description="Taiwan stock broker-branch behavioral labels and Triple Confirmation signals.",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    summary="Health check",
)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get(
    "/v1/broker-label/{branch_code}",
    response_model=BrokerLabelResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
        404: {"model": ErrorResponse, "description": "Branch code not found"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    },
    tags=["broker-labels"],
    summary="Get behavioral label for a broker branch",
)
async def get_broker_label(
    branch_code: str,
    api_key: str = Depends(require_api_key),
) -> BrokerLabelResponse:
    _check_rate_limit(api_key)

    record = _BROKER_LABELS.get(branch_code)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch code '{branch_code}' not found.",
        )

    return BrokerLabelResponse(**record)


@app.get(
    "/v1/signal/{ticker}",
    response_model=SignalResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
        404: {"model": ErrorResponse, "description": "Ticker not found"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    },
    tags=["signals"],
    summary="Get Triple Confirmation signal for a ticker",
)
async def get_signal(
    ticker: str,
    api_key: str = Depends(require_api_key),
) -> SignalResponse:
    _check_rate_limit(api_key)

    record = _SIGNALS.get(ticker)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticker '{ticker}' not found.",
        )

    return SignalResponse(
        ticker=record["ticker"],
        date=record["date"],
        signal=record["signal"],
        confidence=record["confidence"],
        triple_confirmation=TripleConfirmation(**record["triple_confirmation"]),
        top_brokers=[TopBroker(**b) for b in record["top_brokers"]],
        risk_flags=record["risk_flags"],
    )
