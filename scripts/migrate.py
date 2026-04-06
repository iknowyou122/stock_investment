#!/usr/bin/env python
"""Run pending DB migrations from db/migrations/*.sql in filename order.

Tracks applied migrations in a `schema_migrations` table so re-running is safe.

Usage:
    make migrate
    python scripts/migrate.py
    python scripts/migrate.py --dry-run   # show pending only, don't apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.infrastructure.db import init_pool, get_connection

_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "db" / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   VARCHAR(255) PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT NOW()
);
"""


def _applied(cur) -> set[str]:
    cur.execute("SELECT filename FROM schema_migrations")
    return {row[0] for row in cur.fetchall()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show pending migrations without applying them")
    args = parser.parse_args()

    init_pool()

    all_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    if not all_files:
        print("No migration files found in db/migrations/")
        return

    with get_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Bootstrap tracking table
            cur.execute(_BOOTSTRAP)

            applied = _applied(cur)
            pending = [f for f in all_files if f.name not in applied]

            if not pending:
                print("All migrations already applied. Nothing to do.")
                return

            print(f"Pending migrations ({len(pending)}):")
            for f in pending:
                print(f"  {f.name}")

            if args.dry_run:
                print("\n[DRY RUN] Not applying.")
                return

            print()
            for f in pending:
                sql = f.read_text()
                print(f"  Applying {f.name} ...", end=" ", flush=True)
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)",
                    (f.name,)
                )
                print("done")

            print(f"\n✅ Applied {len(pending)} migration(s).")


if __name__ == "__main__":
    main()
