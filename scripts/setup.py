#!/usr/bin/env python
"""First-time environment setup: PostgreSQL + .env + migrations.

Detects what's missing and fixes it automatically.

Usage:
    make setup
    python scripts/setup.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ENV_FILE = _ROOT / ".env"
_DB_NAME = "taiwan_stock"
_DB_URL = f"postgresql://localhost/{_DB_NAME}"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ── Step 1: PostgreSQL installed? ────────────────────────────────────────────

def _ensure_postgres_installed() -> str:
    """Return path to pg_isready, installing postgresql@17 via brew if needed."""
    pg = shutil.which("pg_isready") or shutil.which(
        "/opt/homebrew/opt/postgresql@17/bin/pg_isready"
    )
    if pg:
        return pg

    print("PostgreSQL not found. Installing via Homebrew...")
    if not shutil.which("brew"):
        sys.exit("❌ Homebrew not found. Install it first: https://brew.sh")

    r = _run(["brew", "install", "postgresql@17"])
    if r.returncode != 0:
        sys.exit(f"❌ brew install failed:\n{r.stderr}")
    print("  ✅ postgresql@17 installed")

    pg = "/opt/homebrew/opt/postgresql@17/bin/pg_isready"
    # Add to PATH for this session
    os.environ["PATH"] = f"/opt/homebrew/opt/postgresql@17/bin:{os.environ['PATH']}"
    return pg


# ── Step 2: PostgreSQL running? ──────────────────────────────────────────────

def _ensure_postgres_running(pg_isready: str) -> None:
    r = _run([pg_isready, "-q"])
    if r.returncode == 0:
        return  # already running

    print("PostgreSQL not running. Starting via brew services...")
    r = _run(["brew", "services", "start", "postgresql@17"])
    if r.returncode != 0:
        sys.exit(f"❌ Could not start PostgreSQL:\n{r.stderr}")

    # Wait up to 5s
    import time
    for _ in range(10):
        time.sleep(0.5)
        if _run([pg_isready, "-q"]).returncode == 0:
            print("  ✅ PostgreSQL started")
            return
    sys.exit("❌ PostgreSQL did not start in time. Check: brew services list")


# ── Step 3: Database exists? ─────────────────────────────────────────────────

def _ensure_database() -> None:
    createdb = shutil.which("createdb") or "/opt/homebrew/opt/postgresql@17/bin/createdb"
    psql = shutil.which("psql") or "/opt/homebrew/opt/postgresql@17/bin/psql"

    r = _run([psql, "-lqt"])
    if _DB_NAME in r.stdout:
        return  # already exists

    print(f"Database '{_DB_NAME}' not found. Creating...")
    r = _run([createdb, _DB_NAME])
    if r.returncode != 0:
        sys.exit(f"❌ createdb failed:\n{r.stderr}")
    print(f"  ✅ Database '{_DB_NAME}' created")


# ── Step 4: .env has DATABASE_URL? ───────────────────────────────────────────

def _ensure_env() -> None:
    if _ENV_FILE.exists():
        content = _ENV_FILE.read_text()
        if "DATABASE_URL" in content:
            return  # already set

    print(f"Adding DATABASE_URL to {_ENV_FILE.name}...")
    with open(_ENV_FILE, "a") as f:
        f.write(f"\nDATABASE_URL={_DB_URL}\n")
    print(f"  ✅ DATABASE_URL={_DB_URL}")

    # Also set for this process so migrate step works immediately
    os.environ["DATABASE_URL"] = _DB_URL


# ── Step 5: Run migrations ────────────────────────────────────────────────────

def _run_migrations() -> None:
    print("Running migrations...")
    r = _run(
        [sys.executable, str(_ROOT / "scripts" / "migrate.py")],
        env={**os.environ, "PYTHONPATH": str(_ROOT / "src")},
    )
    # Print output regardless of success
    if r.stdout:
        for line in r.stdout.strip().splitlines():
            print(f"  {line}")
    if r.returncode != 0:
        sys.exit(f"❌ Migrations failed:\n{r.stderr}")


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n🔧 Setup: Taiwan Stock Investment\n")

    pg_isready = _ensure_postgres_installed()
    _ensure_postgres_running(pg_isready)
    _ensure_database()
    _ensure_env()

    # Reload .env so migrate can connect
    from dotenv import load_dotenv
    load_dotenv(override=True)

    _run_migrations()

    print("\n✅ Setup complete. You can now run:\n")
    print("  make backtest DATE_FROM=2025-01-01 DATE_TO=2026-03-31")
    print("  make optimize\n")


if __name__ == "__main__":
    main()
