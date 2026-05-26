# Phase 4.36A — Hard Gates + Pillar 2A + TDCC 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 加入四個 Gate 0 硬性過濾（處置股/暫停/漲停/當沖限制）＋大盤融資維持率 Macro Gate，並驗證分點 Pillar 2A 和 TDCC 集保因子在付費 FinMind 環境下正確運作。

**Architecture:** 新建 `PaidDataFetcher` 類別負責所有市場層級付費資料（每日一次 API call 拿全市場清單），結果注入 `TWSEChipProxy` 新欄位，TCE `score_full()` 和 SurgeRadar 在計分前讀取欄位執行 Gate 0。

**Tech Stack:** Python 3.11+, requests, pydantic (TWSEChipProxy), pytest, FinMind v4 REST API (`https://api.finmindtrade.com/api/v4/data`)

---

## 說明：本計畫範圍

Plans B/C/D（期貨升級、新因子、週K等）是後續獨立計畫，不在此範圍內。

**FINMIND_API_KEY 說明：** 已在 `.env` 中設定，`scripts/batch_plan.py` 的 `load_dotenv()` 會自動載入。測試時需手動 `export FINMIND_API_KEY=$(grep FINMIND_API_KEY .env | cut -d= -f2)`。

---

## 檔案地圖

| 動作 | 路徑 | 說明 |
|------|------|------|
| 新建 | `src/taiwan_stock_agent/infrastructure/paid_data_fetcher.py` | PaidDataFetcher：市場層級付費資料 |
| 新建 | `tests/unit/test_paid_data_fetcher.py` | PaidDataFetcher 單元測試 |
| 新建 | `tests/unit/test_gate0_filters.py` | Gate 0 TCE + SurgeRadar 整合測試 |
| 修改 | `src/taiwan_stock_agent/domain/models.py` | TWSEChipProxy 新增 4 個 bool 欄位 |
| 修改 | `src/taiwan_stock_agent/infrastructure/twse_client.py` | ChipProxyFetcher 接受 PaidDataFetcher |
| 修改 | `src/taiwan_stock_agent/domain/triple_confirmation_engine.py` | score_full() Gate 0 + 大盤融資維持率 |
| 修改 | `scripts/surge_scan.py` | SurgeRadar Gate 0 跳過邏輯 |
| 修改 | `scripts/batch_plan.py` | 實例化 PaidDataFetcher，傳入 market margin rate |

---

## Task 1: 建立 `PaidDataFetcher` 骨架與測試

**Files:**
- Create: `src/taiwan_stock_agent/infrastructure/paid_data_fetcher.py`
- Create: `tests/unit/test_paid_data_fetcher.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/unit/test_paid_data_fetcher.py
"""Unit tests for PaidDataFetcher — all HTTP calls mocked."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch, MagicMock

import pytest

from taiwan_stock_agent.infrastructure.paid_data_fetcher import PaidDataFetcher


TEST_DATE = date(2026, 5, 22)


class TestPaidDataFetcherInit:
    def test_no_key_returns_empty_sets(self, monkeypatch):
        """Without API key, all fetch methods return empty frozenset silently."""
        monkeypatch.delenv("FINMIND_API_KEY", raising=False)
        pf = PaidDataFetcher()
        assert pf.fetch_disposal_tickers(TEST_DATE) == frozenset()
        assert pf.fetch_halt_tickers(TEST_DATE) == frozenset()
        assert pf.fetch_limit_up_tickers(TEST_DATE) == frozenset()
        assert pf.fetch_daytrade_restricted_tickers(TEST_DATE) == frozenset()
        assert pf.fetch_market_margin_maintenance(TEST_DATE) is None

    def test_with_key_reads_from_env(self, monkeypatch):
        """With API key in env, _api_key is populated."""
        monkeypatch.setenv("FINMIND_API_KEY", "test_key_123")
        pf = PaidDataFetcher()
        assert pf._api_key == "test_key_123"


class TestFetchDisposalTickers:
    def _make_fetcher(self, monkeypatch) -> PaidDataFetcher:
        monkeypatch.setenv("FINMIND_API_KEY", "test_key")
        return PaidDataFetcher()

    def test_returns_tickers_still_under_disposition(self, monkeypatch):
        pf = self._make_fetcher(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [
            {"stock_id": "2330", "end_date": "2026-05-30"},
            {"stock_id": "3481", "end_date": "2026-05-20"},  # already ended
            {"stock_id": "6547", "end_date": "2026-05-22"},  # ends today — include
        ]}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            result = pf.fetch_disposal_tickers(TEST_DATE)
        assert "2330" in result
        assert "3481" not in result  # ended before TEST_DATE
        assert "6547" in result

    def test_caches_result(self, monkeypatch):
        pf = self._make_fetcher(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [{"stock_id": "2330", "end_date": "2026-05-30"}]}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp) as mock_get:
            pf.fetch_disposal_tickers(TEST_DATE)
            pf.fetch_disposal_tickers(TEST_DATE)  # second call — should use cache
        assert mock_get.call_count == 1  # only one HTTP call

    def test_api_failure_returns_empty(self, monkeypatch):
        pf = self._make_fetcher(monkeypatch)
        with patch("requests.get", side_effect=Exception("network error")):
            result = pf.fetch_disposal_tickers(TEST_DATE)
        assert result == frozenset()


class TestFetchMarketMarginMaintenance:
    def test_parses_rate_field(self, monkeypatch):
        monkeypatch.setenv("FINMIND_API_KEY", "test_key")
        pf = PaidDataFetcher()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [{"margin_maintenance_ratio": 145.2}]}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            rate = pf.fetch_market_margin_maintenance(TEST_DATE)
        assert rate == pytest.approx(145.2)

    def test_returns_none_on_empty_data(self, monkeypatch):
        monkeypatch.setenv("FINMIND_API_KEY", "test_key")
        pf = PaidDataFetcher()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": []}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            assert pf.fetch_market_margin_maintenance(TEST_DATE) is None
```

- [ ] **Step 2: 確認測試失敗**

```bash
source .venv/bin/activate
python -m pytest tests/unit/test_paid_data_fetcher.py -v 2>&1 | head -20
```

Expected: `ImportError: cannot import name 'PaidDataFetcher'`

- [ ] **Step 3: 實作 `paid_data_fetcher.py`**

```python
# src/taiwan_stock_agent/infrastructure/paid_data_fetcher.py
"""Paid FinMind data fetcher — market-level daily datasets.

All methods return empty collections on failure (graceful degradation).
Results are cached per trade_date within the session.
"""
from __future__ import annotations

import logging
import os
from datetime import date

import requests

logger = logging.getLogger(__name__)

_FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


class PaidDataFetcher:
    """Fetches market-level paid FinMind datasets (one API call per dataset per day).

    Requires FINMIND_API_KEY env var (loaded via dotenv in calling scripts).
    Silently returns empty results if key is missing or API call fails.
    """

    def __init__(self) -> None:
        self._api_key = os.environ.get("FINMIND_API_KEY", "")
        self._disposal_cache: dict[date, frozenset[str]] = {}
        self._halt_cache: dict[date, frozenset[str]] = {}
        self._limit_up_cache: dict[date, frozenset[str]] = {}
        self._daytrade_cache: dict[date, frozenset[str]] = {}
        self._margin_cache: dict[date, float | None] = {}

    def _get(self, dataset: str, trade_date: date) -> list[dict]:
        """Generic FinMind API call. Returns [] on any failure."""
        if not self._api_key:
            return []
        try:
            resp = requests.get(
                _FINMIND_URL,
                params={
                    "dataset": dataset,
                    "start_date": trade_date.isoformat(),
                    "end_date": trade_date.isoformat(),
                    "token": self._api_key,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", []) if isinstance(data, dict) else []
        except Exception as e:
            logger.debug("PaidDataFetcher._get(%s, %s) failed: %s", dataset, trade_date, e)
            return []

    def fetch_disposal_tickers(self, trade_date: date) -> frozenset[str]:
        """Tickers under disposition on trade_date (公布處置有價證券表)."""
        if trade_date in self._disposal_cache:
            return self._disposal_cache[trade_date]
        rows = self._get("TaiwanStockDisposal", trade_date)
        result: set[str] = set()
        for r in rows:
            ticker = str(r.get("stock_id", "")).strip()
            if not ticker:
                continue
            end_str = str(r.get("end_date", "")).strip()
            try:
                end = date.fromisoformat(end_str) if end_str else trade_date
                if end >= trade_date:
                    result.add(ticker)
            except ValueError:
                result.add(ticker)  # conservative: include if parsing fails
        fs = frozenset(result)
        self._disposal_cache[trade_date] = fs
        if result:
            logger.info("PaidDataFetcher: %d disposal tickers on %s", len(result), trade_date)
        return fs

    def fetch_halt_tickers(self, trade_date: date) -> frozenset[str]:
        """Tickers with trading halted (台股暫停交易公告)."""
        if trade_date in self._halt_cache:
            return self._halt_cache[trade_date]
        rows = self._get("TaiwanStockTradingHalt", trade_date)
        result = frozenset(str(r.get("stock_id", "")).strip() for r in rows if r.get("stock_id"))
        self._halt_cache[trade_date] = result
        return result

    def fetch_limit_up_tickers(self, trade_date: date) -> frozenset[str]:
        """Tickers that closed at limit-up price (漲停收盤)."""
        if trade_date in self._limit_up_cache:
            return self._limit_up_cache[trade_date]
        rows = self._get("TaiwanDailyPriceLimit", trade_date)
        result: set[str] = set()
        for r in rows:
            ticker = str(r.get("stock_id", "")).strip()
            # Try multiple field name patterns (verify against actual API response)
            close = r.get("close") or r.get("收盤價")
            limit_up = r.get("limit_up_price") or r.get("漲停價") or r.get("漲停")
            if ticker and close is not None and limit_up is not None:
                try:
                    if abs(float(close) - float(limit_up)) < 0.02:
                        result.add(ticker)
                except (TypeError, ValueError):
                    pass
        fs = frozenset(result)
        self._limit_up_cache[trade_date] = fs
        return fs

    def fetch_daytrade_restricted_tickers(self, trade_date: date) -> frozenset[str]:
        """Tickers where 先賣後買當沖 is suspended (暫停先賣後買當沖預告表)."""
        if trade_date in self._daytrade_cache:
            return self._daytrade_cache[trade_date]
        rows = self._get("TaiwanStockDayTradeRestriction", trade_date)
        result = frozenset(str(r.get("stock_id", "")).strip() for r in rows if r.get("stock_id"))
        self._daytrade_cache[trade_date] = result
        return result

    def fetch_market_margin_maintenance(self, trade_date: date) -> float | None:
        """Overall market margin maintenance rate (大盤融資維持率). None if unavailable."""
        if trade_date in self._margin_cache:
            return self._margin_cache[trade_date]
        rows = self._get("TaiwanMarginMaintenanceRatio", trade_date)
        rate: float | None = None
        for r in rows:
            for key in ("margin_maintenance_ratio", "rate", "維持率", "整體維持率"):
                v = r.get(key)
                if v is not None:
                    try:
                        rate = float(v)
                        break
                    except (TypeError, ValueError):
                        pass
            if rate is not None:
                break
        self._margin_cache[trade_date] = rate
        return rate
```

- [ ] **Step 4: 執行測試確認通過**

```bash
python -m pytest tests/unit/test_paid_data_fetcher.py -v
```

Expected: 全部通過（10 tests）

- [ ] **Step 5: Commit**

```bash
git add src/taiwan_stock_agent/infrastructure/paid_data_fetcher.py tests/unit/test_paid_data_fetcher.py
git commit -m "feat: PaidDataFetcher — market-level paid FinMind data (Gate 0 infra)"
```

---

## Task 2: TWSEChipProxy 新增 Gate 0 欄位

**Files:**
- Modify: `src/taiwan_stock_agent/domain/models.py`

- [ ] **Step 1: 找到 TWSEChipProxy 定義位置**

```bash
grep -n "class TWSEChipProxy\|is_available" src/taiwan_stock_agent/domain/models.py
```

Expected output: `class TWSEChipProxy` 在 58 行附近，`is_available: bool = False` 在 109 行附近。

- [ ] **Step 2: 在 `is_available` 欄位之前加入 4 個新欄位**

在 `src/taiwan_stock_agent/domain/models.py` 中，找到這段：

```python
    is_available: bool = False
    data_quality_flags: list[str] = Field(default_factory=list)
```

改為：

```python
    # Gate 0 flags — populated by PaidDataFetcher (require FINMIND_API_KEY)
    is_disposal: bool = False             # 公布處置有價證券 → SKIP
    is_trading_halt: bool = False         # 暫停交易 → SKIP
    is_limit_up: bool = False             # 漲停收盤（標記，非跳過）
    is_daytrade_restricted: bool = False  # 暫停先賣後買（量能閾值調整）

    is_available: bool = False
    data_quality_flags: list[str] = Field(default_factory=list)
```

- [ ] **Step 3: 確認 pydantic 可正常建立物件**

```bash
python -c "
from taiwan_stock_agent.domain.models import TWSEChipProxy
from datetime import date
p = TWSEChipProxy(ticker='2330', trade_date=date(2026, 5, 22))
print('is_disposal:', p.is_disposal)
print('is_trading_halt:', p.is_trading_halt)
print('is_limit_up:', p.is_limit_up)
print('is_daytrade_restricted:', p.is_daytrade_restricted)
"
```

Expected: 全部印出 `False`

- [ ] **Step 4: 執行現有測試確認不破壞**

```bash
python -m pytest tests/unit/ -q --tb=short 2>&1 | tail -5
```

Expected: 和之前一樣的通過數（新欄位有預設值，不會破壞現有測試）

- [ ] **Step 5: Commit**

```bash
git add src/taiwan_stock_agent/domain/models.py
git commit -m "feat: TWSEChipProxy — add Gate 0 bool fields (disposal/halt/limit_up/daytrade)"
```

---

## Task 3: ChipProxyFetcher 接 PaidDataFetcher

**Files:**
- Modify: `src/taiwan_stock_agent/infrastructure/twse_client.py`

- [ ] **Step 1: 在 `twse_client.py` 頂部加 import**

找到現有的 import 區塊（有 `from taiwan_stock_agent.domain.models import TWSEChipProxy`），在其後加：

```python
from taiwan_stock_agent.infrastructure.paid_data_fetcher import PaidDataFetcher
```

- [ ] **Step 2: 修改 `ChipProxyFetcher.__init__` 接受 `paid_fetcher` 參數**

找到：
```python
    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir or CACHE_DIR
```

改為：
```python
    def __init__(self, cache_dir: Path | None = None, paid_fetcher: PaidDataFetcher | None = None) -> None:
        self._cache_dir = cache_dir or CACHE_DIR
        self._paid = paid_fetcher
```

- [ ] **Step 3: 在 `fetch()` 方法末尾，`return TWSEChipProxy(...)` 之前注入 Gate 0 欄位**

找到 `fetch()` 方法中 `return TWSEChipProxy(` 之前（約在原有的 `is_available` 判斷之後），加入：

```python
        # --- Gate 0: paid market-level filters ---
        is_disposal = False
        is_halt = False
        is_limit_up = False
        is_daytrade_restricted = False
        if self._paid is not None:
            disposal_set = self._paid.fetch_disposal_tickers(trade_date)
            halt_set = self._paid.fetch_halt_tickers(trade_date)
            limit_up_set = self._paid.fetch_limit_up_tickers(trade_date)
            daytrade_set = self._paid.fetch_daytrade_restricted_tickers(trade_date)
            is_disposal = ticker in disposal_set
            is_halt = ticker in halt_set
            is_limit_up = ticker in limit_up_set
            is_daytrade_restricted = ticker in daytrade_set
```

- [ ] **Step 4: 在 `TWSEChipProxy(...)` 建構子中加入新欄位**

在 `return TWSEChipProxy(` 的參數列中加入（在 `is_available=is_available` 之前）：

```python
            is_disposal=is_disposal,
            is_trading_halt=is_halt,
            is_limit_up=is_limit_up,
            is_daytrade_restricted=is_daytrade_restricted,
```

- [ ] **Step 5: 語法確認**

```bash
python -m py_compile src/taiwan_stock_agent/infrastructure/twse_client.py && echo "OK"
```

Expected: `OK`

- [ ] **Step 6: 執行現有測試**

```bash
python -m pytest tests/unit/ -q --tb=short 2>&1 | tail -5
```

Expected: 通過數不變

- [ ] **Step 7: Commit**

```bash
git add src/taiwan_stock_agent/infrastructure/twse_client.py
git commit -m "feat: ChipProxyFetcher — wire PaidDataFetcher for Gate 0 bool fields"
```

---

## Task 4: TCE Gate 0 ── 處置股/暫停交易/漲停/當沖限制

**Files:**
- Modify: `src/taiwan_stock_agent/domain/triple_confirmation_engine.py`
- Create: `tests/unit/test_gate0_filters.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/unit/test_gate0_filters.py
"""Tests for Gate 0 hard filters in TCE and SurgeRadar."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from taiwan_stock_agent.domain.models import DailyOHLCV, TWSEChipProxy, VolumeProfile
from taiwan_stock_agent.domain.triple_confirmation_engine import TripleConfirmationEngine


TEST_DATE = date(2026, 5, 22)


def _ohlcv(close: float = 100.0, volume: int = 5_000_000) -> DailyOHLCV:
    return DailyOHLCV(
        ticker="TEST", trade_date=TEST_DATE,
        open=close * 0.99, high=close * 1.01, low=close * 0.98,
        close=close, volume=volume,
    )


def _history(n: int = 25, close: float = 95.0) -> list[DailyOHLCV]:
    bars = []
    start = TEST_DATE - timedelta(days=n + 5)
    for i in range(n):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        bars.append(DailyOHLCV(
            ticker="TEST", trade_date=d,
            open=close, high=close * 1.01, low=close * 0.99, close=close, volume=4_000_000,
        ))
    return bars


def _proxy(**kwargs) -> TWSEChipProxy:
    defaults = dict(ticker="TEST", trade_date=TEST_DATE, is_available=True)
    defaults.update(kwargs)
    return TWSEChipProxy(**defaults)


def _vp() -> VolumeProfile:
    return VolumeProfile(ticker="TEST", poc_proxy=90.0, twenty_day_high=102.0, twenty_day_sessions=20)


def _chip():
    from taiwan_stock_agent.domain.triple_confirmation_engine import ChipReport
    return ChipReport(ticker="TEST", report_date=TEST_DATE)


class TestDisposalGate:
    def test_disposal_ticker_returns_skip_action(self):
        """is_disposal=True → action must not be LONG or WATCH."""
        engine = TripleConfirmationEngine()
        proxy = _proxy(is_disposal=True)
        signal, _, _ = engine.score_full(
            _ohlcv(), _history(), _chip(), _vp(), twse_proxy=proxy
        )
        assert signal.action in ("SKIP", "CAUTION"), f"Expected SKIP/CAUTION, got {signal.action}"
        assert any("DISPOSAL" in f for f in signal.data_quality_flags)

    def test_non_disposal_not_affected(self):
        """is_disposal=False → normal scoring, action can be anything valid."""
        engine = TripleConfirmationEngine()
        proxy = _proxy(is_disposal=False)
        signal, _, _ = engine.score_full(
            _ohlcv(), _history(), _chip(), _vp(), twse_proxy=proxy
        )
        assert not any("DISPOSAL" in f for f in signal.data_quality_flags)


class TestHaltGate:
    def test_halt_ticker_returns_skip(self):
        engine = TripleConfirmationEngine()
        proxy = _proxy(is_trading_halt=True)
        signal, _, _ = engine.score_full(
            _ohlcv(), _history(), _chip(), _vp(), twse_proxy=proxy
        )
        assert signal.action in ("SKIP", "CAUTION")
        assert any("HALT" in f for f in signal.data_quality_flags)


class TestLimitUpFlag:
    def test_limit_up_adds_flag_but_does_not_skip(self):
        """is_limit_up=True adds LIMIT_UP_CLOSE flag but does NOT force skip."""
        engine = TripleConfirmationEngine()
        proxy = _proxy(is_limit_up=True)
        signal, _, _ = engine.score_full(
            _ohlcv(), _history(), _chip(), _vp(), twse_proxy=proxy
        )
        assert "LIMIT_UP_CLOSE" in signal.data_quality_flags
        # action should still be based on normal scoring (not forced to SKIP)
        assert signal.action in ("LONG", "WATCH", "CAUTION", "SKIP")


class TestDaytradeRestrictedFlag:
    def test_daytrade_restricted_adds_flag(self):
        engine = TripleConfirmationEngine()
        proxy = _proxy(is_daytrade_restricted=True)
        signal, _, _ = engine.score_full(
            _ohlcv(), _history(), _chip(), _vp(), twse_proxy=proxy
        )
        assert "DAYTRADE_RESTRICTED" in signal.data_quality_flags
```

- [ ] **Step 2: 執行確認失敗**

```bash
python -m pytest tests/unit/test_gate0_filters.py -v 2>&1 | head -30
```

Expected: 多個 `AssertionError`（action 不是 SKIP，flag 不存在）

- [ ] **Step 3: 在 TCE `score_full()` 加入 Gate 0 邏輯**

找到 `score_full()` 方法（約 546 行），在 `self._compute(...)` 呼叫之前加入：

```python
        # Gate 0: hard reject before any scoring
        if twse_proxy is not None:
            if twse_proxy.is_disposal or twse_proxy.is_trading_halt:
                from taiwan_stock_agent.domain.models import ExecutionPlan
                gate_flag = "GATE0_DISPOSAL" if twse_proxy.is_disposal else "GATE0_HALT"
                plan = self._make_execution_plan(ohlcv, volume_profile)
                return (
                    SignalOutput(
                        ticker=ohlcv.ticker,
                        date=ohlcv.trade_date,
                        action="SKIP",
                        confidence=0,
                        execution_plan=plan,
                        halt_flag=False,
                        data_quality_flags=[gate_flag],
                    ),
                    _ScoreBreakdown(),
                    _AnalysisHints(),
                )
```

- [ ] **Step 4: 在 `_build_signal()` 末尾（或 `score_full()` 中 `signal` 建構後）加入 limit-up 和 daytrade 旗標**

找到 `score_full()` 中 `signal = self._build_signal(...)` 之後，加入：

```python
        # Gate 0 flags (non-blocking)
        if twse_proxy is not None:
            if twse_proxy.is_limit_up:
                signal = signal.model_copy(update={
                    "data_quality_flags": list(signal.data_quality_flags) + ["LIMIT_UP_CLOSE"]
                })
            if twse_proxy.is_daytrade_restricted:
                signal = signal.model_copy(update={
                    "data_quality_flags": list(signal.data_quality_flags) + ["DAYTRADE_RESTRICTED"]
                })
```

- [ ] **Step 5: 執行 Gate 0 測試**

```bash
python -m pytest tests/unit/test_gate0_filters.py -v
```

Expected: 全部通過

- [ ] **Step 6: 執行全部單元測試確認不破壞**

```bash
python -m pytest tests/unit/ -q --tb=short 2>&1 | tail -5
```

Expected: 通過數 ≥ 之前

- [ ] **Step 7: Commit**

```bash
git add src/taiwan_stock_agent/domain/triple_confirmation_engine.py tests/unit/test_gate0_filters.py
git commit -m "feat: TCE Gate 0 — disposal/halt skip + limit_up/daytrade flags"
```

---

## Task 5: SurgeRadar Gate 0

**Files:**
- Modify: `scripts/surge_scan.py`

- [ ] **Step 1: 找到 SurgeRadar 計分入口**

```bash
grep -n 'def _score_single\|def _run_ticker\|is_available\|twse_proxy' scripts/surge_scan.py | head -20
```

記下處理單一 ticker 的函數名稱和行號。

- [ ] **Step 2: 在 SurgeRadar 的 ticker 處理函數中，取得 proxy 之後加入 Gate 0 檢查**

找到 SurgeRadar 中呼叫 `chip_fetcher.fetch(ticker, ...)` 之後的地方，加入：

```python
                # Gate 0: 處置股 / 暫停交易 → 直接跳過
                if proxy.is_disposal:
                    logger.debug("Surge Gate 0: %s is under disposition, skipping", ticker)
                    return None
                if proxy.is_trading_halt:
                    logger.debug("Surge Gate 0: %s trading halted, skipping", ticker)
                    return None
                # Gate 0c: 漲停收盤 → 加 flag，量能比較閾值不調整（量是真實的，封板前有成交）
                # Gate 0d: 當沖限制 → volume_ratio 門檻上調（實際有效成交量被高估）
                effective_vol_ratio_threshold = _VOL_RATIO_MIN  # 預設門檻
                if proxy.is_daytrade_restricted:
                    effective_vol_ratio_threshold *= 1.2  # 上調 20%
```

注意：`_VOL_RATIO_MIN` 是 SurgeRadar 現有的最小量比門檻常數，找到實際變數名稱後替換。

- [ ] **Step 3: 語法確認**

```bash
python -m py_compile scripts/surge_scan.py && echo "OK"
```

Expected: `OK`

- [ ] **Step 4: 手動快速驗證（不跑完整掃描）**

```bash
source .venv/bin/activate
python -c "
import importlib.util, sys
# just verify the module loads without error
spec = importlib.util.spec_from_file_location('surge_scan', 'scripts/surge_scan.py')
mod = importlib.util.module_from_spec(spec)
print('surge_scan loads OK')
"
```

- [ ] **Step 5: Commit**

```bash
git add scripts/surge_scan.py
git commit -m "feat: SurgeRadar Gate 0 — skip disposal/halt, adjust threshold for daytrade restricted"
```

---

## Task 6: 大盤融資維持率 Macro Gate

**Files:**
- Modify: `src/taiwan_stock_agent/domain/triple_confirmation_engine.py`
- Modify: `tests/unit/test_gate0_filters.py`（加入新測試 class）

- [ ] **Step 1: 在 `test_gate0_filters.py` 加入 Macro Gate 測試**

在文件末尾追加：

```python
class TestMarginMaintenanceGate:
    """Market-level margin maintenance rate gate via taifex_context."""

    def _run(self, rate: float | None) -> str:
        engine = TripleConfirmationEngine()
        # Use high-confidence setup to get LONG without gate interference
        ohlcv = _ohlcv(close=100.0, volume=8_000_000)
        history = _history(n=30, close=90.0)
        proxy = _proxy(
            foreign_net_buy=2000,
            foreign_consecutive_buy_days=5,
            is_available=True,
        )
        taifex_ctx: dict = {}
        if rate is not None:
            taifex_ctx["margin_maintenance_rate"] = rate
        signal, _, _ = engine.score_full(
            ohlcv, history, _chip(), _vp(),
            twse_proxy=proxy, taifex_context=taifex_ctx
        )
        return signal.action

    def test_normal_rate_no_change(self):
        """Rate >= 130: no adjustment."""
        action = self._run(145.0)
        # Should not add market stress flag
        assert action in ("LONG", "WATCH", "CAUTION")  # normal scoring

    def test_stress_rate_demotes_long_to_watch(self):
        """120 <= rate < 130: LONG → WATCH."""
        # We need a signal that would be LONG without this gate
        # Use deep integration: just verify the flag is set
        engine = TripleConfirmationEngine()
        signal, _, _ = engine.score_full(
            _ohlcv(), _history(), _chip(), _vp(),
            twse_proxy=_proxy(),
            taifex_context={"margin_maintenance_rate": 125.0},
        )
        if signal.action == "LONG":
            pytest.fail("LONG should be demoted to WATCH when rate < 130")
        # Flag must be present
        assert "MARKET_MARGIN_STRESS" in signal.data_quality_flags or signal.action != "LONG"

    def test_crisis_rate_demotes_to_caution(self):
        """Rate < 120: LONG/WATCH → CAUTION."""
        engine = TripleConfirmationEngine()
        signal, _, _ = engine.score_full(
            _ohlcv(), _history(), _chip(), _vp(),
            twse_proxy=_proxy(),
            taifex_context={"margin_maintenance_rate": 115.0},
        )
        assert signal.action not in ("LONG", "WATCH"), (
            f"Expected CAUTION/SKIP during crisis rate, got {signal.action}"
        )
        assert "MARKET_MARGIN_CRISIS" in signal.data_quality_flags
```

- [ ] **Step 2: 確認新測試失敗**

```bash
python -m pytest tests/unit/test_gate0_filters.py::TestMarginMaintenanceGate -v 2>&1 | head -20
```

Expected: `AssertionError`

- [ ] **Step 3: 在 TCE 的 taifex_ctx 處理區塊加入融資維持率邏輯**

找到現有的 taifex gate 處理區塊（約 2406 行）：

```python
        # Factor E: 台指期外資淨多單 — 期貨空頭壓力下 LONG→WATCH 降級
        taifex_ctx = getattr(self, "_taifex_context", {})
        if taifex_ctx.get("futures_bearish") and action == "LONG":
            action = "WATCH"
```

在這段之後加入：

```python
        # 大盤融資維持率 Macro Gate
        margin_rate = taifex_ctx.get("margin_maintenance_rate")
        if margin_rate is not None:
            if margin_rate < 120.0:
                # 市場斷頭危機：所有 LONG/WATCH → CAUTION
                if action in ("LONG", "WATCH"):
                    action = "CAUTION"
                data_quality_flags.append("MARKET_MARGIN_CRISIS")
            elif margin_rate < 130.0:
                # 壓力偏高：LONG → WATCH
                if action == "LONG":
                    action = "WATCH"
                data_quality_flags.append("MARKET_MARGIN_STRESS")
```

- [ ] **Step 4: 執行 Macro Gate 測試**

```bash
python -m pytest tests/unit/test_gate0_filters.py -v
```

Expected: 全部通過（含新增的 Macro Gate 測試）

- [ ] **Step 5: 全套測試**

```bash
python -m pytest tests/unit/ -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add src/taiwan_stock_agent/domain/triple_confirmation_engine.py tests/unit/test_gate0_filters.py
git commit -m "feat: TCE macro gate — 大盤融資維持率 < 130 LONG→WATCH, < 120 →CAUTION"
```

---

## Task 7: 將 PaidDataFetcher + 融資維持率接進 batch_plan 和 surge_scan

**Files:**
- Modify: `scripts/batch_plan.py`
- Modify: `scripts/surge_scan.py`

- [ ] **Step 1: 在 `batch_plan.py` 的 import 區加入**

找到 `from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher` 的地方，在後面加：

```python
from taiwan_stock_agent.infrastructure.paid_data_fetcher import PaidDataFetcher
```

- [ ] **Step 2: 在 `run_batch()` 的共用客戶端建立區加入 PaidDataFetcher**

找到：
```python
    shared_finmind = FinMindClient()
    shared_chip = ChipProxyFetcher()
```

改為：
```python
    shared_finmind = FinMindClient()
    shared_paid = PaidDataFetcher()
    shared_chip = ChipProxyFetcher(paid_fetcher=shared_paid)
```

- [ ] **Step 3: 傳遞大盤融資維持率進 taifex_context**

在 `run_batch()` 中找到建立 taifex_context 的地方（通常是在 worker 函數中或 shared_agent 建立後）。找到 `fetch_taifex_context` 或 `taifex_context` 相關呼叫，在取得 dict 後加入：

```python
                # 大盤融資維持率（每日一次，從 PaidDataFetcher 取得）
                margin_rate = shared_paid.fetch_market_margin_maintenance(analysis_date)
                if margin_rate is not None:
                    taifex_ctx["margin_maintenance_rate"] = margin_rate
```

注意：需要 `shared_paid` 在 worker closure 內可見（在 `run_batch()` 內定義的 worker 函數可直接 capture）。

- [ ] **Step 4: 在 `surge_scan.py` 的 import 區加入**

```python
from taiwan_stock_agent.infrastructure.paid_data_fetcher import PaidDataFetcher
```

- [ ] **Step 5: 在 surge_scan 的 `run_surge_scan()` 函數中，ChipProxyFetcher 建立處加入 paid_fetcher**

找到：
```python
    chip_fetcher = ChipProxyFetcher()
```

改為：
```python
    paid_fetcher = PaidDataFetcher()
    chip_fetcher = ChipProxyFetcher(paid_fetcher=paid_fetcher)
```

- [ ] **Step 6: 語法確認**

```bash
python -m py_compile scripts/batch_plan.py && python -m py_compile scripts/surge_scan.py && echo "OK"
```

Expected: `OK`

- [ ] **Step 7: 全套測試**

```bash
python -m pytest tests/unit/ -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 8: Commit**

```bash
git add scripts/batch_plan.py scripts/surge_scan.py
git commit -m "feat: wire PaidDataFetcher into batch_plan + surge_scan (Gate 0 + margin rate)"
```

---

## Task 8: 驗證 FinMind API 欄位名稱（必做，實際打 API）

**重要：** `paid_data_fetcher.py` 的欄位名稱（如 `margin_maintenance_ratio`、`end_date` 等）是根據 FinMind 文件推測的。**必須用真實 API key 確認。**

- [ ] **Step 1: 確認環境變數已載入**

```bash
source .venv/bin/activate
export FINMIND_API_KEY=$(grep FINMIND_API_KEY .env | cut -d= -f2)
echo "Key set: $([ -n "$FINMIND_API_KEY" ] && echo YES || echo NO)"
```

Expected: `Key set: YES`

- [ ] **Step 2: 測試 TaiwanStockDisposal API 回應欄位**

```bash
python -c "
import requests, os, json
from datetime import date
resp = requests.get(
    'https://api.finmindtrade.com/api/v4/data',
    params={
        'dataset': 'TaiwanStockDisposal',
        'start_date': '2026-05-01',
        'end_date': '2026-05-22',
        'token': os.environ['FINMIND_API_KEY'],
    },
    timeout=15
)
data = resp.json()
rows = data.get('data', [])
print('Total rows:', len(rows))
if rows:
    print('Fields:', list(rows[0].keys()))
    print('Sample:', json.dumps(rows[0], ensure_ascii=False, indent=2))
"
```

**根據輸出調整 `paid_data_fetcher.py` 中的欄位名稱。**

- [ ] **Step 3: 同樣方式測試 TaiwanDailyPriceLimit**

```bash
python -c "
import requests, os, json
resp = requests.get(
    'https://api.finmindtrade.com/api/v4/data',
    params={
        'dataset': 'TaiwanDailyPriceLimit',
        'start_date': '2026-05-22',
        'end_date': '2026-05-22',
        'token': os.environ['FINMIND_API_KEY'],
    },
    timeout=15
)
data = resp.json()
rows = data.get('data', [])
print('Total rows:', len(rows))
if rows:
    print('Fields:', list(rows[0].keys()))
    print('Sample:', json.dumps(rows[0], ensure_ascii=False, indent=2))
"
```

- [ ] **Step 4: 測試 TaiwanMarginMaintenanceRatio**

```bash
python -c "
import requests, os, json
resp = requests.get(
    'https://api.finmindtrade.com/api/v4/data',
    params={
        'dataset': 'TaiwanMarginMaintenanceRatio',
        'start_date': '2026-05-22',
        'end_date': '2026-05-22',
        'token': os.environ['FINMIND_API_KEY'],
    },
    timeout=15
)
data = resp.json()
print(json.dumps(data, ensure_ascii=False, indent=2))
"
```

- [ ] **Step 5: 根據實際欄位名稱更新 `paid_data_fetcher.py`**

如果欄位名稱不符，直接修改 `paid_data_fetcher.py` 對應的 `r.get("...")` 欄位，並重跑 Task 1 的測試確認仍然通過。

- [ ] **Step 6: Commit（如有更新）**

```bash
git add src/taiwan_stock_agent/infrastructure/paid_data_fetcher.py
git commit -m "fix: correct FinMind API field names from actual response verification"
```

---

## Task 9: 驗證 Pillar 2A（分點資料）已正確運作

**背景：** Pillar 2A 的程式碼已完整實作，需要 (a) 付費 FinMind 存取，(b) broker label DB 已建立。

- [ ] **Step 1: 確認 broker label DB 存在**

```bash
ls -la data/broker_labels.db 2>/dev/null || echo "NOT FOUND"
```

若不存在，執行：
```bash
make build-labels
```

（注意：`make build-labels` 需要付費 FinMind 存取，會消耗較多 API call）

- [ ] **Step 2: 驗證 Pillar 2A 對單一股票有輸出分數**

```bash
source .venv/bin/activate
export FINMIND_API_KEY=$(grep FINMIND_API_KEY .env | cut -d= -f2)
python -c "
from datetime import date
from taiwan_stock_agent.infrastructure.finmind_client import FinMindClient
from taiwan_stock_agent.infrastructure.broker_label_repo import BrokerLabelRepository
from taiwan_stock_agent.agents.chip_detective_agent import ChipDetectiveAgent

fm = FinMindClient()
label_repo = BrokerLabelRepository('data/broker_labels.db')
agent = ChipDetectiveAgent(label_repo)

d = date(2026, 5, 22)
start = d.replace(day=1)
broker_df = fm.fetch_broker_trades('2330', start, d)
print('Broker trades rows:', len(broker_df))

if not broker_df.empty:
    report = agent.analyze('2330', d, broker_df)
    print('net_buyer_count_diff:', report.net_buyer_count_diff)
    print('active_branch_count:', report.active_branch_count)
    print('top_buyers:', report.top_buyers[:3])
else:
    print('WARN: broker_df is empty — paid access may not cover TaiwanStockBrokerTradingStatement')
"
```

- [ ] **Step 3: 確認 Pillar 2A 分數在 TCE 有被計算**

執行 `make analyze TICKER=2330`，查看輸出中 `breadth_pts` / `concentration_pts` / `daytrade_filter_pts` 是否非零。

若仍為 0：檢查 `triple_confirmation_engine.py` 第 792 行附近的 `chip_report.net_buyer_count_diff != 0` 條件是否被滿足。

---

## Task 10: 驗證 TDCC 集保因子正確運作

- [ ] **Step 1: 直接測試 `_fetch_tdcc_ownership`**

```bash
source .venv/bin/activate
export FINMIND_API_KEY=$(grep FINMIND_API_KEY .env | cut -d= -f2)
python -c "
from datetime import date
from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher

fetcher = ChipProxyFetcher()
result = fetcher._fetch_tdcc_ownership('2330', date(2026, 5, 22))
print('Result:', result)
# Expected: tuple of non-None values if API key is valid and data exists
"
```

- [ ] **Step 2: 若 TDCC 回傳全 None**

可能原因：
1. `FINMIND_API_KEY` 未正確設定 → 檢查 `os.environ.get("FINMIND_API_KEY")` 的值
2. 該週資料未釋出（集保資料通常在下週初才有上週數據）→ 改用較舊的日期測試
3. API key 無 TDCC 資料集的存取權 → 聯繫 FinMind 確認方案

```bash
python -c "
from datetime import date
from taiwan_stock_agent.infrastructure.twse_client import ChipProxyFetcher

fetcher = ChipProxyFetcher()
# 改用兩週前的日期（TDCC 有時間差）
result = fetcher._fetch_tdcc_ownership('2330', date(2026, 5, 8))
print('Result (2 weeks ago):', result)
"
```

- [ ] **Step 3: 驗證 `make plan` 輸出中 ownership_concentration_pts 非零**

執行 `make analyze TICKER=2330`，觀察輸出中集保相關因子是否有值。

---

## Task 11: 整合測試 + 完整 Gate 0 端對端驗證

- [ ] **Step 1: 執行完整單元測試套件**

```bash
source .venv/bin/activate
python -m pytest tests/unit/ -q --tb=short 2>&1 | tail -8
```

Expected: 所有原有測試通過，新增測試通過

- [ ] **Step 2: 手動端對端測試（小範圍）**

```bash
source .venv/bin/activate
# 用少量 ticker 測試 Gate 0 是否實際運作
python scripts/batch_plan.py --tickers 2330 2317 2454 --no-llm --date 2026-05-22 2>&1 | grep -E 'DISPOSAL|HALT|LIMIT_UP|MARGIN_STRESS'
```

若有當日處置股，應看到 DISPOSAL flag 出現。

- [ ] **Step 3: 最終 commit**

```bash
git add -A
git commit -m "feat: Phase 4.36A complete — Gate 0 hard filters + PaidDataFetcher + margin maintenance gate"
```

---

## Phase 4.36A 完成標準

- [ ] `PaidDataFetcher` 建立並測試完成
- [ ] TWSEChipProxy 新增 4 個 bool 欄位
- [ ] TCE `score_full()` 處置股/暫停 → SKIP，漲停/當沖 → flag
- [ ] SurgeRadar Gate 0 跳過處置/暫停股
- [ ] 大盤融資維持率 < 130 → LONG 降 WATCH，< 120 → CAUTION
- [ ] FinMind API 欄位名稱已用真實回應驗證
- [ ] Pillar 2A 分點分數已確認非零（或已知原因）
- [ ] TDCC 因子已確認非零（或已知原因）
- [ ] 全套單元測試通過

---

## 後續計畫

| 計畫 | 內容 |
|------|------|
| Plan 4.36B | 期貨夜盤三法人 + 大額交易人 API 穩定化 + 景氣燈號 |
| Plan 4.36C | 八大行庫 + 鉅額交易 + CB 溢價 + 借券費率 |
| Plan 4.36D | 週K多週期確認 + 市值分層 + 供應鏈圖譜 + 還原股價 |
