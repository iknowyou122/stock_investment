"""Multi-LLM provider abstraction for StrategistAgent reasoning.

Supported providers:
  - claude  (Anthropic)   — ANTHROPIC_API_KEY
  - openai  (OpenAI)      — OPENAI_API_KEY
  - gemini  (Google)      — GEMINI_API_KEY

Auto-detection order (when LLM_PROVIDER env is not set):
  ANTHROPIC_API_KEY → OPENAI_API_KEY → GEMINI_API_KEY

Usage::
    # Explicit
    provider = AnthropicProvider(api_key="sk-ant-...")
    provider = OpenAIProvider(api_key="sk-...", model="gpt-4o-mini")
    provider = GeminiProvider(api_key="AIza...", model="gemini-2.0-flash")

    # Auto-detect from environment
    provider = create_llm_provider()   # None if no key found
    provider = create_llm_provider("openai")  # explicit, key from env
"""
from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Default model per provider — override via LLM_MODEL env var or model= param
DEFAULT_MODELS: dict[str, str] = {
    "claude": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "gemini": "gemini-2.5-flash",
}


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal interface: send a prompt, get text back."""

    name: str  # "claude" | "openai" | "gemini"

    def complete(self, prompt: str, max_tokens: int = 500) -> str:
        """Return model response text. Raises RuntimeError on failure."""
        ...


class AnthropicProvider:
    name = "claude"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        self._api_key = api_key
        self._model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODELS["claude"]

    def complete(self, prompt: str, max_tokens: int = 500) -> str:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("anthropic not installed. Run: pip install anthropic")

        client = anthropic.Anthropic(api_key=self._api_key)
        try:
            msg = client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text.strip()
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Anthropic API error: {e}") from e
        except anthropic.APIConnectionError as e:
            raise RuntimeError(f"Anthropic connection error: {e}") from e


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        self._api_key = api_key
        self._model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODELS["openai"]

    def complete(self, prompt: str, max_tokens: int = 500) -> str:
        try:
            import openai
        except ImportError:
            raise RuntimeError("openai not installed. Run: pip install openai")

        client = openai.OpenAI(api_key=self._api_key)
        try:
            resp = client.chat.completions.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except openai.APIStatusError as e:
            raise RuntimeError(f"OpenAI API error: {e}") from e
        except openai.APIConnectionError as e:
            raise RuntimeError(f"OpenAI connection error: {e}") from e


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        self._api_key = api_key
        self._model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODELS["gemini"]

    def complete(self, prompt: str, max_tokens: int = 500) -> str:
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError:
            raise RuntimeError(
                "google-genai not installed. Run: pip install google-genai"
            )

        client = genai.Client(api_key=self._api_key)
        try:
            resp = client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=max_tokens,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            return resp.text.strip()
        except Exception as e:
            raise RuntimeError(f"Gemini API error: {e}") from e


def create_llm_provider(
    provider: str | None = None,
    model: str | None = None,
) -> LLMProvider | None:
    """Build an LLM provider from explicit name or env-based auto-detection.

    Auto-detection priority (when provider=None and LLM_PROVIDER not set):
        ANTHROPIC_API_KEY → OPENAI_API_KEY → GEMINI_API_KEY

    Args:
        provider: "claude" | "openai" | "gemini" | None (auto-detect)
        model: optional model name override (also reads LLM_MODEL env var)

    Returns:
        LLMProvider instance, or None if no API key is found.
    """
    name = (provider or os.environ.get("LLM_PROVIDER", "")).strip().lower() or None

    def _key(env_var: str, label: str) -> str | None:
        key = os.environ.get(env_var, "").strip()
        if not key:
            logger.warning("LLM_PROVIDER=%s but %s not set — LLM disabled", label, env_var)
            return None
        return key

    # Explicit provider choice
    if name == "claude":
        k = _key("ANTHROPIC_API_KEY", "claude")
        return AnthropicProvider(k, model) if k else None

    if name == "openai":
        k = _key("OPENAI_API_KEY", "openai")
        return OpenAIProvider(k, model) if k else None

    if name == "gemini":
        k = _key("GEMINI_API_KEY", "gemini")
        return GeminiProvider(k, model) if k else None

    if name is not None:
        logger.warning(
            "Unknown LLM_PROVIDER=%r — supported: claude, openai, gemini. LLM disabled.",
            name,
        )
        return None

    # Auto-detect: use whichever key is present
    if k := os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return AnthropicProvider(k, model)
    if k := os.environ.get("OPENAI_API_KEY", "").strip():
        return OpenAIProvider(k, model)
    if k := os.environ.get("GEMINI_API_KEY", "").strip():
        return GeminiProvider(k, model)

    return None
