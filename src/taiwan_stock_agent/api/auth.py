"""API key authentication dependency for the 分點情報 API.

Behaviour
---------
- If the ``API_KEY`` environment variable is not set (development), all requests
  pass through without authentication so local iteration is frictionless.
- If ``API_KEY`` is set, every request must include the ``X-API-Key`` header
  whose value matches either:
    1. The configured master ``API_KEY`` env-var value, OR
    2. A key found in the ``api_keys`` table with ``is_active = TRUE``.
  If the DB is unavailable, the fallback is env-var-only validation.
- The dependency returns the raw API key string so downstream code (e.g. the
  rate-limiter) can use it as an identifier.
"""
from __future__ import annotations

import logging
import os

from fastapi import Header, HTTPException, status

logger = logging.getLogger(__name__)

_CONFIGURED_KEY: str | None = os.getenv("API_KEY")


def _check_db_key(api_key: str) -> bool:
    """Return True if api_key exists in the api_keys table and is active.

    Wraps the DB call in a broad try/except so a DB outage does NOT block
    requests that would otherwise pass the env-var check.
    """
    try:
        from taiwan_stock_agent.infrastructure.db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT is_active FROM api_keys WHERE api_key = %s AND is_active = TRUE",
                    (api_key,),
                )
                row = cur.fetchone()
        return row is not None
    except Exception as exc:
        logger.warning(
            "auth: DB key lookup failed (falling through to env-var check): %s", exc
        )
        return False


async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    """FastAPI dependency that validates the ``X-API-Key`` header.

    Returns the provided key (or the placeholder ``"__dev__"`` when auth is
    disabled) so callers can use it as a per-key identifier.
    """
    if _CONFIGURED_KEY is None:
        # Auth disabled — development mode.
        return x_api_key or "__dev__"

    if x_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
        )

    # Allow master key
    if x_api_key == _CONFIGURED_KEY:
        return x_api_key

    # Allow keys registered in the api_keys table
    if _check_db_key(x_api_key):
        return x_api_key

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key.",
    )
