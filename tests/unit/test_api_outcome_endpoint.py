"""Unit tests for the Phase 4 API endpoints.

Covers:
  - POST /v1/signals/{signal_id}/outcome  (community label curation)
  - POST /v1/register  (pro-tier stub + free-tier key issuance)

All DB calls are mocked — no real database required.
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from taiwan_stock_agent.api.main import app

client = TestClient(app, raise_server_exceptions=False)

# A stable UUID for all signal-lookup tests
_SIGNAL_ID = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# DB mock helpers
# ---------------------------------------------------------------------------

def _make_cursor(fetchone_side_effect=None, fetchall_return=None):
    """Return a mock cursor that behaves as a context manager."""
    cur = MagicMock()
    if fetchone_side_effect is not None:
        cur.fetchone.side_effect = fetchone_side_effect
    if fetchall_return is not None:
        cur.fetchall.return_value = fetchall_return
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    return cur


def _make_conn(cursor):
    """Return a mock connection that produces the given cursor."""
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _make_cm(conn):
    """Wrap a connection in a context-manager mock (returned by get_connection())."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=conn)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _mock_conn(signal_row=None, count=1):
    """
    Build a two-call mock suitable for the outcome endpoint:
      call 1 (signal lookup): fetchone → signal_row
      call 2 (count query):   fetchone → (count,)

    The same cursor is reused across both get_connection() calls because
    cursor() is called once per connection and both connections share this
    cursor mock via the factory.
    """
    mock_cur = MagicMock()
    mock_cur.fetchone.side_effect = [signal_row, (count,)]
    mock_cur.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=mock_conn)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# Helper: POST outcome with mocked DB
# ---------------------------------------------------------------------------

def _post_outcome(signal_row, count=1, body=None, raise_on_insert=None):
    """
    Fire POST /v1/signals/{_SIGNAL_ID}/outcome with a mocked DB.

    signal_row — tuple returned by the signal-lookup fetchone
    count      — integer returned by the community_count fetchone
    body       — request JSON dict (defaults to a valid win submission)
    raise_on_insert — if not None, the cursor's execute() raises this on
                      the second call (the INSERT)
    """
    if body is None:
        body = {"did_buy": True, "outcome": "win"}

    # We need two independent get_connection() calls: one for lookup, one
    # for insert+count.  We return a fresh CM each time.
    call_count = [0]

    def _conn_factory():
        call_count[0] += 1
        n = call_count[0]

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)

        if n == 1:
            # Signal lookup
            cur.fetchone.return_value = signal_row
        else:
            # Insert + count
            if raise_on_insert is not None:
                cur.execute.side_effect = [raise_on_insert, None]
            cur.fetchone.return_value = (count,)

        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)

        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=conn)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    with patch("taiwan_stock_agent.infrastructure.db.get_connection", side_effect=_conn_factory):
        resp = client.post(f"/v1/signals/{_SIGNAL_ID}/outcome", json=body)

    return resp


# ---------------------------------------------------------------------------
# Outcome endpoint — happy path
# ---------------------------------------------------------------------------

class TestOutcomeEndpointHappyPath:

    def test_outcome_valid_win(self):
        signal_row = ("2330", date(2026, 3, 1), ["9600"])
        resp = _post_outcome(signal_row, count=1, body={"did_buy": True, "outcome": "win"})
        assert resp.status_code == 201

    def test_outcome_valid_lose(self):
        signal_row = ("2330", date(2026, 3, 1), ["9600"])
        resp = _post_outcome(signal_row, count=2, body={"did_buy": True, "outcome": "lose"})
        assert resp.status_code == 201

    def test_outcome_valid_break_even(self):
        signal_row = ("2330", date(2026, 3, 1), [])
        resp = _post_outcome(signal_row, count=1, body={"did_buy": True, "outcome": "break_even"})
        assert resp.status_code == 201

    def test_outcome_null_outcome_accepted(self):
        """outcome=null is valid — user buys but defers reporting the result."""
        signal_row = ("2330", date(2026, 3, 1), [])
        resp = _post_outcome(signal_row, count=1, body={"did_buy": True, "outcome": None})
        assert resp.status_code == 201

    def test_outcome_response_shape(self):
        """Response body must have message, signal_id, and community_count."""
        signal_row = ("2330", date(2026, 3, 1), ["9600"])
        resp = _post_outcome(signal_row, count=7)
        assert resp.status_code == 201
        data = resp.json()
        assert "message" in data
        assert "signal_id" in data
        assert "community_count" in data

    def test_outcome_community_count_increments(self):
        """community_count in the response must match what the DB returns."""
        signal_row = ("2330", date(2026, 3, 1), ["9600"])
        resp = _post_outcome(signal_row, count=5)
        assert resp.status_code == 201
        assert resp.json()["community_count"] == 5

    def test_outcome_signal_id_in_response(self):
        """signal_id in the response must match the path parameter."""
        signal_row = ("2330", date(2026, 3, 1), [])
        resp = _post_outcome(signal_row, count=1)
        assert resp.json()["signal_id"] == _SIGNAL_ID


# ---------------------------------------------------------------------------
# Outcome endpoint — validation errors
# ---------------------------------------------------------------------------

class TestOutcomeEndpointValidation:

    def test_outcome_invalid_value(self):
        """An outcome value outside the Literal enum must return 422."""
        signal_row = ("2330", date(2026, 3, 1), [])
        resp = _post_outcome(signal_row, body={"did_buy": True, "outcome": "maybe"})
        assert resp.status_code == 422

    def test_outcome_missing_did_buy(self):
        """did_buy is required; omitting it must return 422."""
        signal_row = ("2330", date(2026, 3, 1), [])
        resp = _post_outcome(signal_row, body={"outcome": "win"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Outcome endpoint — error cases
# ---------------------------------------------------------------------------

class TestOutcomeEndpointErrors:

    def test_outcome_signal_not_found(self):
        """When the signal_id does not exist in the DB, return 404."""
        resp = _post_outcome(signal_row=None)  # fetchone returns None
        assert resp.status_code == 404

    def test_outcome_duplicate_submission(self):
        """Duplicate insert (unique constraint violation) must return 409."""
        signal_row = ("2330", date(2026, 3, 1), ["9600"])
        # Simulate psycopg2 raising an exception with 'unique' in the message
        unique_exc = Exception("duplicate key value violates unique constraint idx_community_outcomes_dedup")
        resp = _post_outcome(signal_row, raise_on_insert=unique_exc)
        assert resp.status_code == 409

    def test_outcome_duplicate_submission_409_message(self):
        """409 response should mention 'already submitted'."""
        signal_row = ("2330", date(2026, 3, 1), [])
        unique_exc = Exception("unique constraint violated")
        resp = _post_outcome(signal_row, raise_on_insert=unique_exc)
        assert resp.status_code == 409
        assert "already submitted" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Register endpoint — pro-tier stub and free-tier key issuance
# ---------------------------------------------------------------------------

class TestRegisterEndpoint:

    def test_register_pro_returns_stub(self):
        """POST /v1/register with tier=pro returns checkout_url and api_key=None."""
        resp = client.post("/v1/register", json={"email": "test@example.com", "tier": "pro"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["api_key"] is None
        assert data["payment_status"] == "pending"
        assert data["checkout_url"] is not None
        assert "checkout" in data["checkout_url"].lower()

    def test_register_free_returns_key(self):
        """POST /v1/register with tier=free returns a non-null api_key."""
        cur = _make_cursor()
        conn = _make_conn(cur)
        cm = _make_cm(conn)

        with patch("taiwan_stock_agent.infrastructure.db.get_connection", return_value=cm):
            resp = client.post("/v1/register", json={"email": "user@example.com", "tier": "free"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["api_key"] is not None
        assert len(data["api_key"]) > 0
        assert data["tier"] == "free"

    def test_register_pro_no_db_call(self):
        """Pro-tier stub must NOT attempt any DB insert."""
        with patch("taiwan_stock_agent.infrastructure.db.get_connection") as mock_gc:
            resp = client.post("/v1/register", json={"email": "pro@example.com", "tier": "pro"})

        assert resp.status_code == 200
        # get_connection should not have been called at all for the pro stub path
        # (it may be called by _get_tier inside rate-limit helpers, but NOT for
        #  the registration INSERT)
        insert_called = any(
            "INSERT" in str(c)
            for c in mock_gc.return_value.__enter__.return_value.cursor.return_value.execute.call_args_list
        )
        assert not insert_called

    def test_register_free_message_contains_api_key_hint(self):
        """Free-tier success message must mention the X-API-Key header."""
        cur = _make_cursor()
        conn = _make_conn(cur)
        cm = _make_cm(conn)

        with patch("taiwan_stock_agent.infrastructure.db.get_connection", return_value=cm):
            resp = client.post("/v1/register", json={"email": "user@example.com", "tier": "free"})

        assert "X-API-Key" in resp.json()["message"]
