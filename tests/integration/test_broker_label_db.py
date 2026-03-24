"""Integration tests for PostgresBrokerLabelRepository.

Uses a real PostgreSQL instance (via pytest-postgresql fixture from conftest.py).
Tests the full upsert → get → list_all cycle against an actual DB.

NOTE: Per project testing policy, NO mock databases. These tests hit real PostgreSQL.
"""
from __future__ import annotations

from datetime import date

import pytest

from taiwan_stock_agent.domain.models import BrokerLabel


# These tests require the pg_conn fixture from conftest.py
# which provides a real PostgreSQL connection with migrations applied.

def _make_repo(pg_conn):
    """Create a PostgresBrokerLabelRepository wired to the test connection."""
    # Patch get_connection to use our test connection
    import taiwan_stock_agent.infrastructure.db as db_module
    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _test_conn():
        yield pg_conn

    return patch.object(db_module, "get_connection", _test_conn)


@pytest.mark.integration
class TestPostgresBrokerLabelRepository:
    def test_upsert_and_get(self, pg_conn):
        from taiwan_stock_agent.domain.broker_label_classifier import (
            PostgresBrokerLabelRepository,
        )
        import taiwan_stock_agent.infrastructure.db as db_module
        from unittest.mock import patch
        from contextlib import contextmanager

        @contextmanager
        def _test_conn():
            yield pg_conn
            pg_conn.commit()

        with patch.object(db_module, "get_connection", _test_conn):
            repo = PostgresBrokerLabelRepository(conn_factory=None)
            label = BrokerLabel(
                branch_code="1480",
                branch_name="凱基-台北",
                label="隔日沖",
                reversal_rate=0.74,
                sample_count=120,
                last_updated=date(2025, 1, 31),
                metadata={"source": "spike_validate"},
            )
            repo.upsert(label)
            pg_conn.commit()

            retrieved = repo.get("1480")
            assert retrieved is not None
            assert retrieved.label == "隔日沖"
            assert abs(retrieved.reversal_rate - 0.74) < 0.001
            assert retrieved.sample_count == 120
            assert retrieved.branch_name == "凱基-台北"

    def test_upsert_updates_existing(self, pg_conn):
        from taiwan_stock_agent.domain.broker_label_classifier import (
            PostgresBrokerLabelRepository,
        )
        import taiwan_stock_agent.infrastructure.db as db_module
        from unittest.mock import patch
        from contextlib import contextmanager

        @contextmanager
        def _test_conn():
            yield pg_conn
            pg_conn.commit()

        with patch.object(db_module, "get_connection", _test_conn):
            repo = PostgresBrokerLabelRepository(conn_factory=None)

            label_v1 = BrokerLabel(
                branch_code="2001",
                branch_name="元大-板橋",
                label="unknown",
                reversal_rate=0.35,
                sample_count=30,
                last_updated=date(2025, 1, 1),
            )
            repo.upsert(label_v1)
            pg_conn.commit()

            # Update after more data
            label_v2 = BrokerLabel(
                branch_code="2001",
                branch_name="元大-板橋",
                label="隔日沖",
                reversal_rate=0.68,
                sample_count=100,
                last_updated=date(2025, 6, 1),
            )
            repo.upsert(label_v2)
            pg_conn.commit()

            retrieved = repo.get("2001")
            assert retrieved is not None
            assert retrieved.label == "隔日沖"
            assert retrieved.sample_count == 100

    def test_get_nonexistent_returns_none(self, pg_conn):
        from taiwan_stock_agent.domain.broker_label_classifier import (
            PostgresBrokerLabelRepository,
        )
        import taiwan_stock_agent.infrastructure.db as db_module
        from unittest.mock import patch
        from contextlib import contextmanager

        @contextmanager
        def _test_conn():
            yield pg_conn

        with patch.object(db_module, "get_connection", _test_conn):
            repo = PostgresBrokerLabelRepository(conn_factory=None)
            assert repo.get("NONEXISTENT_CODE") is None

    def test_list_all_returns_all_records(self, pg_conn):
        from taiwan_stock_agent.domain.broker_label_classifier import (
            PostgresBrokerLabelRepository,
        )
        import taiwan_stock_agent.infrastructure.db as db_module
        from unittest.mock import patch
        from contextlib import contextmanager

        @contextmanager
        def _test_conn():
            yield pg_conn
            pg_conn.commit()

        with patch.object(db_module, "get_connection", _test_conn):
            repo = PostgresBrokerLabelRepository(conn_factory=None)
            labels = [
                BrokerLabel(
                    branch_code=f"T{i:03}",
                    branch_name=f"TestBranch{i}",
                    label="unknown",
                    reversal_rate=0.3,
                    sample_count=10,
                    last_updated=date(2025, 1, 1),
                )
                for i in range(5)
            ]
            for l in labels:
                repo.upsert(l)
            pg_conn.commit()

            all_records = repo.list_all()
            codes = {r.branch_code for r in all_records}
            assert all(f"T{i:03}" in codes for i in range(5))
