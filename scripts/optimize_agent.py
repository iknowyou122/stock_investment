"""AI-driven parameter optimization agent — CLI entry point.

Core logic lives in src/taiwan_stock_agent/optimize.py.

Usage (called by bot.py):
    result = await run_optimize(llm_name, send_telegram_fn)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from taiwan_stock_agent.optimize import run_optimize  # noqa: F401 — re-export for bot.py

__all__ = ["run_optimize"]
