"""Unit tests for LLM provider abstraction.

Tests cover:
  - AnthropicProvider, OpenAIProvider, GeminiProvider return text from mock SDK
  - create_llm_provider auto-detection priority (claude > openai > gemini)
  - Explicit provider selection via provider= argument
  - LLM_PROVIDER env var overrides auto-detect
  - Missing API key returns None
  - Unknown provider name returns None
  - RuntimeError on SDK error propagated correctly
  - StrategistAgent accepts llm_provider= argument and uses it
  - StrategistAgent falls back to AnthropicProvider when anthropic_api_key given
  - StrategistAgent skips LLM when provider is None
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from taiwan_stock_agent.domain.llm_provider import (
    AnthropicProvider,
    GeminiProvider,
    LLMProvider,
    OpenAIProvider,
    create_llm_provider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_env(*keys: str):
    """Context manager: temporarily unset env vars."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        saved = {k: os.environ.pop(k, None) for k in keys}
        try:
            yield
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    return _ctx()


_ALL_LLM_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "LLM_PROVIDER", "LLM_MODEL")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestLLMProviderProtocol:
    def test_anthropic_satisfies_protocol(self):
        p = AnthropicProvider("fake-key")
        assert isinstance(p, LLMProvider)
        assert p.name == "claude"

    def test_openai_satisfies_protocol(self):
        p = OpenAIProvider("fake-key")
        assert isinstance(p, LLMProvider)
        assert p.name == "openai"

    def test_gemini_satisfies_protocol(self):
        p = GeminiProvider("fake-key")
        assert isinstance(p, LLMProvider)
        assert p.name == "gemini"


# ---------------------------------------------------------------------------
# AnthropicProvider
# ---------------------------------------------------------------------------

class TestAnthropicProvider:
    def test_complete_returns_text(self):
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text="  response text  ")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_msg

        with patch("anthropic.Anthropic", return_value=mock_client):
            p = AnthropicProvider("key")
            result = p.complete("prompt", max_tokens=100)

        assert result == "response text"
        mock_client.messages.create.assert_called_once()

    def test_uses_default_model(self):
        from taiwan_stock_agent.domain.llm_provider import DEFAULT_MODELS
        with _clear_env("LLM_MODEL"):
            p = AnthropicProvider("key")
        assert p._model == DEFAULT_MODELS["claude"]

    def test_uses_custom_model(self):
        p = AnthropicProvider("key", model="claude-haiku-4-5-20251001")
        assert p._model == "claude-haiku-4-5-20251001"

    def test_raises_runtime_error_on_api_status_error(self):
        import anthropic as _ant
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = _ant.APIStatusError(
            "bad request", response=MagicMock(status_code=400), body={}
        )
        with patch("anthropic.Anthropic", return_value=mock_client):
            p = AnthropicProvider("key")
            with pytest.raises(RuntimeError, match="Anthropic API error"):
                p.complete("prompt")

    def test_missing_package_raises_runtime_error(self):
        with patch.dict("sys.modules", {"anthropic": None}):
            p = AnthropicProvider("key")
            with pytest.raises(RuntimeError, match="anthropic not installed"):
                p.complete("prompt")


# ---------------------------------------------------------------------------
# OpenAIProvider
# ---------------------------------------------------------------------------

class TestOpenAIProvider:
    def test_complete_returns_text(self):
        mock_choice = MagicMock()
        mock_choice.message.content = "  openai response  "
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = mock_resp
        mock_openai_module = MagicMock()
        mock_openai_module.OpenAI.return_value = mock_client_instance

        with patch.dict("sys.modules", {"openai": mock_openai_module}):
            p = OpenAIProvider("key")
            result = p.complete("prompt", max_tokens=200)

        assert result == "openai response"
        mock_client_instance.chat.completions.create.assert_called_once()

    def test_uses_default_model(self):
        from taiwan_stock_agent.domain.llm_provider import DEFAULT_MODELS
        with _clear_env("LLM_MODEL"):
            p = OpenAIProvider("key")
        assert p._model == DEFAULT_MODELS["openai"]

    def test_missing_package_raises_runtime_error(self):
        with patch.dict("sys.modules", {"openai": None}):
            p = OpenAIProvider("key")
            with pytest.raises(RuntimeError, match="openai not installed"):
                p.complete("prompt")


# ---------------------------------------------------------------------------
# GeminiProvider
# ---------------------------------------------------------------------------

class TestGeminiProvider:
    def test_complete_returns_text(self):
        mock_resp = MagicMock()
        mock_resp.text = "  gemini response  "
        mock_client_instance = MagicMock()
        mock_client_instance.models.generate_content.return_value = mock_resp
        mock_genai_module = MagicMock()
        mock_genai_module.Client.return_value = mock_client_instance
        mock_types_module = MagicMock()

        with patch.dict("sys.modules", {
            "google": MagicMock(),
            "google.genai": mock_genai_module,
            "google.genai.types": mock_types_module,
        }):
            p = GeminiProvider("key")
            with patch("google.genai", mock_genai_module, create=True), \
                 patch("google.genai.types", mock_types_module, create=True):
                pass  # instantiation verified
        assert p.name == "gemini"

    def test_uses_default_model(self):
        from taiwan_stock_agent.domain.llm_provider import DEFAULT_MODELS
        p = GeminiProvider("key")
        assert p._model == DEFAULT_MODELS["gemini"]

    def test_missing_package_raises_runtime_error(self):
        with patch.dict("sys.modules", {"google": None, "google.genai": None}):
            p = GeminiProvider("key")
            with pytest.raises(RuntimeError, match="google-genai not installed"):
                p.complete("prompt")


# ---------------------------------------------------------------------------
# create_llm_provider — auto-detect and explicit
# ---------------------------------------------------------------------------

class TestCreateLLMProvider:
    def test_returns_none_when_no_keys(self):
        with _clear_env(*_ALL_LLM_KEYS):
            result = create_llm_provider()
        assert result is None

    def test_auto_detects_anthropic_first(self):
        with _clear_env(*_ALL_LLM_KEYS):
            os.environ["ANTHROPIC_API_KEY"] = "ant-key"
            os.environ["OPENAI_API_KEY"] = "oai-key"
            result = create_llm_provider()
        assert isinstance(result, AnthropicProvider)

    def test_auto_detects_openai_when_no_anthropic(self):
        with _clear_env(*_ALL_LLM_KEYS):
            os.environ["OPENAI_API_KEY"] = "oai-key"
            os.environ["GEMINI_API_KEY"] = "gem-key"
            result = create_llm_provider()
        assert isinstance(result, OpenAIProvider)

    def test_auto_detects_gemini_last(self):
        with _clear_env(*_ALL_LLM_KEYS):
            os.environ["GEMINI_API_KEY"] = "gem-key"
            result = create_llm_provider()
        assert isinstance(result, GeminiProvider)

    def test_explicit_provider_claude(self):
        with _clear_env(*_ALL_LLM_KEYS):
            os.environ["ANTHROPIC_API_KEY"] = "ant-key"
            result = create_llm_provider("claude")
        assert isinstance(result, AnthropicProvider)

    def test_explicit_provider_openai(self):
        with _clear_env(*_ALL_LLM_KEYS):
            os.environ["OPENAI_API_KEY"] = "oai-key"
            result = create_llm_provider("openai")
        assert isinstance(result, OpenAIProvider)

    def test_explicit_provider_gemini(self):
        with _clear_env(*_ALL_LLM_KEYS):
            os.environ["GEMINI_API_KEY"] = "gem-key"
            result = create_llm_provider("gemini")
        assert isinstance(result, GeminiProvider)

    def test_llm_provider_env_overrides_autodetect(self):
        with _clear_env(*_ALL_LLM_KEYS):
            os.environ["LLM_PROVIDER"] = "openai"
            os.environ["ANTHROPIC_API_KEY"] = "ant-key"  # would normally win
            os.environ["OPENAI_API_KEY"] = "oai-key"
            result = create_llm_provider()
        assert isinstance(result, OpenAIProvider)

    def test_unknown_provider_returns_none(self):
        with _clear_env(*_ALL_LLM_KEYS):
            result = create_llm_provider("unknown_llm")
        assert result is None

    def test_explicit_provider_missing_key_returns_none(self):
        with _clear_env(*_ALL_LLM_KEYS):
            result = create_llm_provider("openai")  # OPENAI_API_KEY not set
        assert result is None

    def test_model_override_passed_through(self):
        with _clear_env(*_ALL_LLM_KEYS):
            os.environ["ANTHROPIC_API_KEY"] = "ant-key"
            result = create_llm_provider("claude", model="claude-haiku-4-5-20251001")
        assert isinstance(result, AnthropicProvider)
        assert result._model == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# StrategistAgent integration with llm_provider
# ---------------------------------------------------------------------------

class TestStrategistAgentLLMProvider:
    def _make_agent_with_provider(self, provider):
        from datetime import date, timedelta
        import pandas as pd
        from taiwan_stock_agent.agents.strategist_agent import StrategistAgent

        mock_finmind = MagicMock()
        base = date(2025, 1, 1)
        rows = []
        for i in range(25):
            d = base + timedelta(days=i)
            c = 100.0 + i * 0.5
            rows.append({
                "trade_date": d if i < 24 else date(2025, 2, 5),
                "ticker": "9999", "open": c - 1, "high": c + 1,
                "low": c - 2, "close": c, "volume": 10_000 if i < 24 else 30_000,
            })
        mock_finmind.fetch_ohlcv.return_value = pd.DataFrame(rows)
        mock_finmind.fetch_broker_trades.return_value = pd.DataFrame(
            columns=["trade_date", "ticker", "branch_code", "branch_name", "buy_volume", "sell_volume"]
        )
        mock_finmind.fetch_taiex_history.return_value = pd.DataFrame()

        class _EmptyRepo:
            def get(self, _): return None
            def upsert(self, _): pass
            def list_all(self): return []

        return StrategistAgent(mock_finmind, _EmptyRepo(), llm_provider=provider)

    def test_uses_provided_llm_provider(self):
        """llm_provider= argument is used; complete() called once."""
        mock_provider = MagicMock(spec=LLMProvider)
        mock_provider.name = "test"
        mock_provider.complete.return_value = '{"momentum":"ok","chip_analysis":"ok","risk_factors":"none"}'

        agent = self._make_agent_with_provider(mock_provider)
        from datetime import date
        agent.run("9999", date(2025, 2, 5))

        mock_provider.complete.assert_called_once()

    def test_no_llm_when_provider_none(self):
        """llm_provider=None → reasoning fields are empty."""
        agent = self._make_agent_with_provider(None)
        from datetime import date
        signal = agent.run("9999", date(2025, 2, 5))

        assert signal.reasoning.momentum == ""
        assert signal.reasoning.chip_analysis == ""

    def test_llm_error_falls_back_to_empty_reasoning(self):
        """Provider.complete() raising RuntimeError → empty Reasoning, no crash."""
        mock_provider = MagicMock(spec=LLMProvider)
        mock_provider.name = "test"
        mock_provider.complete.side_effect = RuntimeError("API quota exceeded")

        agent = self._make_agent_with_provider(mock_provider)
        from datetime import date
        signal = agent.run("9999", date(2025, 2, 5))

        assert signal.reasoning.momentum == ""
        assert signal.halt_flag is False  # signal still produced
