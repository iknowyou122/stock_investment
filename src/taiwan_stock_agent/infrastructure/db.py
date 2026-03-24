"""PostgreSQL connection pool via psycopg2."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Generator

import psycopg2
from psycopg2 import pool as pg_pool

logger = logging.getLogger(__name__)

_connection_pool: pg_pool.ThreadedConnectionPool | None = None


def _get_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise ValueError(
            "DATABASE_URL environment variable not set. "
            "Example: postgresql://localhost/stock_agent"
        )
    return dsn


def init_pool(minconn: int = 1, maxconn: int = 5) -> None:
    """Initialize the global connection pool.

    Call once at application startup before using get_connection().
    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _connection_pool
    if _connection_pool is not None:
        return
    dsn = _get_dsn()
    _connection_pool = pg_pool.ThreadedConnectionPool(minconn, maxconn, dsn=dsn)
    logger.info("PostgreSQL connection pool initialized (min=%d max=%d)", minconn, maxconn)


def close_pool() -> None:
    """Close the global connection pool. Call at application shutdown."""
    global _connection_pool
    if _connection_pool is not None:
        _connection_pool.closeall()
        _connection_pool = None
        logger.info("PostgreSQL connection pool closed.")


@contextmanager
def get_connection() -> Generator[psycopg2.extensions.connection, None, None]:
    """Yield a psycopg2 connection from the pool.

    Commits on clean exit, rolls back on exception, always returns connection
    to pool.

    Usage::

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    """
    if _connection_pool is None:
        init_pool()

    assert _connection_pool is not None
    conn = _connection_pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _connection_pool.putconn(conn)
