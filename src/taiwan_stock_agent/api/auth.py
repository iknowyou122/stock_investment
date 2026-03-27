"""API key authentication dependency for the 分點情報 API.

Behaviour
---------
- If the ``API_KEY`` environment variable is not set (development), all requests
  pass through without authentication so local iteration is frictionless.
- If ``API_KEY`` is set, every request must include the ``X-API-Key`` header
  whose value matches the configured key; otherwise 401 is returned.
- The dependency returns the raw API key string so downstream code (e.g. the
  rate-limiter) can use it as an identifier.
"""

from __future__ import annotations

import os

from fastapi import Header, HTTPException, status


_CONFIGURED_KEY: str | None = os.getenv("API_KEY")


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

    if x_api_key != _CONFIGURED_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )

    return x_api_key
