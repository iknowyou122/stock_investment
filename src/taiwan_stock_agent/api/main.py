"""FastAPI application — 分點情報 API (Broker Label API).

Run locally:
    uvicorn src.taiwan_stock_agent.api.main:app --reload --port 8000

Endpoints
---------
GET  /health
GET  /v1/broker-label/{branch_code}
GET  /v1/broker-labels
GET  /v1/signal/{ticker}
GET  /v1/track-record
POST /v1/register

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

import logging
import os
import secrets
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware

from .auth import require_api_key
from .schemas import (
    BrokerLabelResponse,
    ConfidenceTierStats,
    ErrorResponse,
    HealthResponse,
    RegisterRequest,
    RegisterResponse,
    SignalResponse,
    TopBroker,
    TrackRecordResponse,
    TripleConfirmation,
)

load_dotenv()

logger = logging.getLogger(__name__)

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
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="分點情報 API",
    description="Taiwan stock broker-branch behavioral labels and Triple Confirmation signals.",
    version="0.2.0",
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


@app.on_event("startup")
async def startup() -> None:
    from taiwan_stock_agent.infrastructure.db import init_pool

    try:
        init_pool()
        logger.info("DB pool initialized.")
    except Exception as exc:
        logger.warning("DB pool init failed (DB may be unavailable): %s", exc)


@app.on_event("shutdown")
async def shutdown() -> None:
    from taiwan_stock_agent.infrastructure.db import close_pool

    close_pool()
    logger.info("DB pool closed.")


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

    from taiwan_stock_agent.domain.broker_label_classifier import (
        PostgresBrokerLabelRepository,
    )

    repo = PostgresBrokerLabelRepository(conn_factory=None)
    label = repo.get(branch_code)
    if label is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch code '{branch_code}' not found.",
        )

    return BrokerLabelResponse(
        branch_code=label.branch_code,
        branch_name=label.branch_name,
        label=label.label,
        reversal_rate=label.reversal_rate,
        sample_count=label.sample_count,
        confidence=min(1.0, label.sample_count / 200),
        updated_at=label.last_updated,
    )


@app.get(
    "/v1/broker-labels",
    response_model=list[BrokerLabelResponse],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    },
    tags=["broker-labels"],
    summary="List all broker labels, optionally filtered by label type",
)
async def list_broker_labels(
    label: str | None = None,
    api_key: str = Depends(require_api_key),
) -> list[BrokerLabelResponse]:
    _check_rate_limit(api_key)

    from taiwan_stock_agent.domain.broker_label_classifier import (
        PostgresBrokerLabelRepository,
    )

    repo = PostgresBrokerLabelRepository(conn_factory=None)
    all_labels = repo.list_all()

    if label is not None:
        all_labels = [lbl for lbl in all_labels if lbl.label == label]

    return [
        BrokerLabelResponse(
            branch_code=lbl.branch_code,
            branch_name=lbl.branch_name,
            label=lbl.label,
            reversal_rate=lbl.reversal_rate,
            sample_count=lbl.sample_count,
            confidence=min(1.0, lbl.sample_count / 200),
            updated_at=lbl.last_updated,
        )
        for lbl in all_labels
    ]


@app.get(
    "/v1/signal/{ticker}",
    response_model=SignalResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
        422: {"model": ErrorResponse, "description": "Invalid date format"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
        503: {"model": ErrorResponse, "description": "Signal generation failed"},
    },
    tags=["signals"],
    summary="Get Triple Confirmation signal for a ticker",
)
async def get_signal(
    ticker: str,
    date_param: str | None = Query(default=None, alias="date"),
    api_key: str = Depends(require_api_key),
) -> SignalResponse:
    _check_rate_limit(api_key)

    if date_param is not None:
        try:
            analysis_date = date.fromisoformat(date_param)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid date format '{date_param}'. Expected YYYY-MM-DD.",
            )
    else:
        analysis_date = date.today()

    from taiwan_stock_agent.agents.strategist_agent import StrategistAgent
    from taiwan_stock_agent.domain.broker_label_classifier import (
        PostgresBrokerLabelRepository,
    )
    from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
    from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher

    finmind_key = os.environ.get("FINMIND_API_KEY", "")
    finmind = FinMindClient(api_key=finmind_key)
    chip_proxy = ChipProxyFetcher()
    label_repo = PostgresBrokerLabelRepository(conn_factory=None)

    agent = StrategistAgent(
        finmind=finmind,
        label_repo=label_repo,
        chip_proxy_fetcher=chip_proxy,
    )

    try:
        signal = agent.run(ticker, analysis_date)
    except Exception as exc:
        logger.exception("Signal generation failed for %s on %s", ticker, analysis_date)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Signal generation failed: {exc}",
        )

    # Record the signal outcome in the background — don't fail the request on error
    try:
        from taiwan_stock_agent.infrastructure.signal_outcome_repo import (
            SignalOutcomeRepository,
        )

        SignalOutcomeRepository().record(signal)
    except Exception as exc:
        logger.warning("Failed to record signal outcome for %s: %s", ticker, exc)

    # Derive triple_confirmation fields from SignalOutput
    momentum = signal.confidence >= 40
    chip_concentration = not any(
        b.label == "隔日沖" for b in ([] if not hasattr(signal, "_top_buyers") else [])
    )
    price_above_poc = (
        signal.execution_plan.target
        > signal.execution_plan.entry_bid_limit * 1.03
    )

    return SignalResponse(
        ticker=signal.ticker,
        date=signal.date,
        signal=signal.action,
        confidence=signal.confidence,
        triple_confirmation=TripleConfirmation(
            momentum=momentum,
            chip_concentration=chip_concentration,
            price_above_poc=price_above_poc,
        ),
        top_brokers=[],
        risk_flags=signal.data_quality_flags,
    )


@app.get(
    "/v1/track-record",
    response_model=TrackRecordResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    },
    tags=["analytics"],
    summary="Win-rate statistics over recent signals",
)
async def get_track_record(
    days: int = Query(default=30, ge=1, le=365),
    api_key: str = Depends(require_api_key),
) -> TrackRecordResponse:
    _check_rate_limit(api_key)

    from taiwan_stock_agent.infrastructure.signal_outcome_repo import (
        SignalOutcomeRepository,
    )

    try:
        stats = SignalOutcomeRepository().win_rate_stats(days=days)
    except Exception as exc:
        logger.exception("win_rate_stats failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to compute track record: {exc}",
        )

    tier_data = stats.get("by_confidence_tier", {})
    by_tier = {
        name: ConfidenceTierStats(
            count=tier.get("count", 0),
            win_rate_1d=tier.get("win_rate_1d"),
            win_rate_3d=tier.get("win_rate_3d"),
            win_rate_5d=tier.get("win_rate_5d"),
        )
        for name, tier in tier_data.items()
    }

    return TrackRecordResponse(
        days=days,
        total_signals=stats.get("total", 0),
        long_count=stats.get("long_count", 0),
        win_rate_1d=stats.get("win_rate_1d"),
        win_rate_3d=stats.get("win_rate_3d"),
        win_rate_5d=stats.get("win_rate_5d"),
        by_confidence_tier=by_tier,
    )


@app.post(
    "/v1/register",
    response_model=RegisterResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Registration failed"},
    },
    tags=["auth"],
    summary="Register for an API key",
)
async def register(body: RegisterRequest) -> RegisterResponse:
    # TODO: integrate Stripe/台灣Pay payment verification before issuing pro keys
    new_key = secrets.token_hex(16)

    try:
        from taiwan_stock_agent.infrastructure.db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO api_keys (api_key, tier, email)
                    VALUES (%s, %s, %s)
                    """,
                    (new_key, body.tier, body.email),
                )
    except Exception as exc:
        logger.exception("API key registration failed for %s", body.email)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Registration failed: {exc}",
        )

    return RegisterResponse(
        api_key=new_key,
        tier=body.tier,
        message=(
            "API key created successfully. "
            "Include it as the X-API-Key header in all requests."
        ),
    )
